import asyncio
import logging
from datetime import datetime, timezone

import httpx
from telegram import Update
from telegram.ext import ContextTypes

from agent.classifier import classify_task
from agent.deadline import parse_deadline, parse_reminder_datetime
from bot.integrations.github import save_to_github
from config import settings
from db.client import (
    create_task,
    get_recent_messages,
    get_setting,
    save_message,
    set_setting,
    set_task_type,
    update_task_deadline,
    update_task_reminder,
)

logger = logging.getLogger(__name__)

_TYPE_EMOJI = {
    "idea": "💡",
    "todo": "📋",
    "note": "📝",
    "learning": "🧠",
    "architecture": "🏗️",
    "question": "❓",
    "shopping": "🛒",
    "reminder": "⏰",
}

_DISCOVERY_TYPES = {"idea"}
_GITHUB_TYPES = {"architecture", "learning"}
_BUYER_TYPES = {"shopping"}

AWAITING_TASK_KEY = "awaiting_task_id"
AWAITING_QUERY_KEY = "awaiting_search_query"
AWAITING_LOCATION_KEY = "awaiting_location_type"
AWAITING_REMINDER_TASK_KEY = "awaiting_reminder_task_id"
AWAITING_REMINDER_DATE_KEY = "awaiting_reminder_date"
AWAITING_REMINDER_TIME_KEY = "awaiting_reminder_time"
USER_TZ_KEY = "user_timezone"


async def _get_user_tz() -> str:
    tz = await get_setting(USER_TZ_KEY)
    return tz or "UTC"


async def _save_and_extract(content: str) -> None:
    """Save a bot reply to messages DB and trigger Tier 1 memory extraction."""
    await save_message("bot", content)
    memory_url = getattr(settings, "memory_agent_url", "")
    if memory_url:
        try:
            async with httpx.AsyncClient(timeout=5.0) as hc:
                await hc.post(f"{memory_url}/memory/process-now")
        except Exception:
            pass


def _format_reminder_confirm(due_date: str, due_time_utc: str, user_tz: str = "UTC") -> str:
    """Format reminder confirmation. Shows local time if TZ differs from UTC."""
    if user_tz == "UTC":
        return f"Reminder set for *{due_date}* at *{due_time_utc}* UTC."
    try:
        from zoneinfo import ZoneInfo
        dt_utc = datetime.strptime(f"{due_date} {due_time_utc}", "%Y-%m-%d %H:%M").replace(
            tzinfo=ZoneInfo("UTC")
        )
        dt_local = dt_utc.astimezone(ZoneInfo(user_tz))
        local_str = dt_local.strftime("%H:%M")
        tz_label = dt_local.strftime("%Z")
        return f"Reminder set for *{due_date}* at *{local_str}* {tz_label} (*{due_time_utc}* UTC)."
    except Exception:
        return f"Reminder set for *{due_date}* at *{due_time_utc}* UTC."


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != settings.telegram_user_id:
        return

    text = update.message.text.strip()
    if not text:
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    asyncio.create_task(save_message("user", text))
    asyncio.create_task(set_setting("last_user_message_at", now_iso))

    # Check if awaiting reminder clarification
    awaiting_reminder = await get_setting(AWAITING_REMINDER_TASK_KEY)
    if awaiting_reminder:
        awaiting_date = await get_setting(AWAITING_REMINDER_DATE_KEY)
        awaiting_time = await get_setting(AWAITING_REMINDER_TIME_KEY)
        if awaiting_date == "NEEDED":
            await _handle_reminder_date_reply(int(awaiting_reminder), text, update)
            return
        if awaiting_time == "NEEDED":
            await _handle_reminder_time_reply(int(awaiting_reminder), text, update)
            return

    # Check if awaiting deadline reply for a shopping task
    awaiting = await get_setting(AWAITING_TASK_KEY)
    if awaiting:
        await _handle_deadline_reply(int(awaiting), text, update)
        return

    # New message: save immediately, reply, classify in background
    task_id = await create_task(text, type="note")
    reply = f"Task #{task_id} saved ✓"
    await update.message.reply_text(reply)
    asyncio.create_task(save_message("bot", reply))

    try:
        recent = await get_recent_messages(limit=20)
    except Exception:
        recent = []

    asyncio.create_task(_classify_and_followup(task_id, text, update, recent))


async def _handle_deadline_reply(task_id: int, text: str, update: Update) -> None:
    try:
        await set_setting(AWAITING_TASK_KEY, "")
        search_query = await get_setting(AWAITING_QUERY_KEY) or ""
        location_type = await get_setting(AWAITING_LOCATION_KEY) or "any"

        deadline_info = await parse_deadline(text)
        await update_task_deadline(
            task_id,
            deadline_info.date.isoformat() if deadline_info.date else None,
            deadline_info.strategy,
        )

        loc_hint = {"local": "📍 local stores", "online": "🌐 online", "any": "🔍 all sources"}.get(
            location_type, ""
        )
        msg = (
            f"🗓 Deadline: *{deadline_info.label}* → strategy: `{deadline_info.strategy}`\n"
            f"Searching {loc_hint}…"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        asyncio.create_task(_save_and_extract(msg))

        from db.client import get_task_by_id
        task = await get_task_by_id(task_id)
        from bot.jobs.buyer import run_buyer
        await run_buyer(
            task_id=task_id,
            task_text=task.text if task else search_query,
            search_query=search_query,
            location_type=location_type,
            strategy=deadline_info.strategy,
            deadline_days=deadline_info.days_until,
            bot=update.get_bot(),
        )
    except Exception as e:
        logger.warning("Deadline reply handling failed for task #%d: %s", task_id, e)
        err_msg = "Something went wrong parsing the deadline. Searching anyway…"
        await update.message.reply_text(err_msg)
        asyncio.create_task(_save_and_extract(err_msg))
        from db.client import get_task_by_id
        task = await get_task_by_id(task_id)
        search_query = await get_setting(AWAITING_QUERY_KEY) or (task.text if task else "")
        location_type = await get_setting(AWAITING_LOCATION_KEY) or "any"
        from bot.jobs.buyer import run_buyer
        await run_buyer(
            task_id=task_id,
            task_text=task.text if task else search_query,
            search_query=search_query,
            location_type=location_type,
            bot=update.get_bot(),
        )


async def _handle_reminder_date_reply(task_id: int, text: str, update: Update) -> None:
    """User was asked for a date. Try to extract date and time together."""
    await set_setting(AWAITING_REMINDER_DATE_KEY, "")
    user_tz = await _get_user_tz()
    dt = await parse_reminder_datetime(text, user_tz)
    stored_time = await get_setting(AWAITING_REMINDER_TIME_KEY)

    # Use the freshly parsed time, or fall back to previously stored time
    resolved_time = dt.due_time or (
        stored_time if stored_time and stored_time not in ("NEEDED", "") else None
    )

    if dt.due_date and resolved_time:
        await set_setting(AWAITING_REMINDER_TASK_KEY, "")
        await set_setting(AWAITING_REMINDER_TIME_KEY, "")
        await update_task_reminder(task_id, dt.due_date, resolved_time)
        msg = _format_reminder_confirm(dt.due_date, resolved_time, user_tz)
        await update.message.reply_text(msg, parse_mode="Markdown")
        asyncio.create_task(_save_and_extract(msg))
    elif dt.due_date:
        await set_setting(AWAITING_REMINDER_DATE_KEY, dt.due_date)
        await set_setting(AWAITING_REMINDER_TIME_KEY, "NEEDED")
        msg = f"Got it — *{dt.label}*. At what time? _(e.g. 09:00, 3pm, 18:30)_"
        await update.message.reply_text(msg, parse_mode="Markdown")
        asyncio.create_task(_save_and_extract(msg))
    elif dt.due_time:
        # User gave time instead of date — store it, ask for date
        await set_setting(AWAITING_REMINDER_TIME_KEY, dt.due_time)
        await set_setting(AWAITING_REMINDER_DATE_KEY, "NEEDED")
        msg = f"Got *{dt.due_time}* UTC. What date? _(e.g. today, tomorrow, Apr 5)_"
        await update.message.reply_text(msg, parse_mode="Markdown")
        asyncio.create_task(_save_and_extract(msg))
    else:
        msg = "Couldn't parse that date. Try: *tomorrow*, *Apr 5*, *2026-04-10*."
        await update.message.reply_text(msg, parse_mode="Markdown")
        asyncio.create_task(_save_and_extract(msg))
        await set_setting(AWAITING_REMINDER_DATE_KEY, "NEEDED")


async def _handle_reminder_time_reply(task_id: int, text: str, update: Update) -> None:
    """User was asked for a time. Use LLM parser to handle timezone-qualified times."""
    await set_setting(AWAITING_REMINDER_TIME_KEY, "")
    stored_date = await get_setting(AWAITING_REMINDER_DATE_KEY)
    user_tz = await _get_user_tz()

    # Try LLM-based parsing first (handles "10am Budapest", "в 10", etc.)
    dt = await parse_reminder_datetime(text, user_tz)
    due_time = dt.due_time

    # Fall back to fast regex parser for plain times like "10:00", "3pm"
    if not due_time:
        from agent.time_parser import parse_time
        due_time = parse_time(text)

    if due_time:
        if stored_date and stored_date not in ("NEEDED", ""):
            await set_setting(AWAITING_REMINDER_TASK_KEY, "")
            await set_setting(AWAITING_REMINDER_DATE_KEY, "")
            await update_task_reminder(task_id, stored_date, due_time)
            msg = _format_reminder_confirm(stored_date, due_time, user_tz)
            await update.message.reply_text(msg, parse_mode="Markdown")
            asyncio.create_task(_save_and_extract(msg))
        else:
            await set_setting(AWAITING_REMINDER_TIME_KEY, due_time)
            await set_setting(AWAITING_REMINDER_DATE_KEY, "NEEDED")
            msg = f"Got *{due_time}* UTC. What date? _(e.g. today, tomorrow, Apr 5)_"
            await update.message.reply_text(msg, parse_mode="Markdown")
            asyncio.create_task(_save_and_extract(msg))
    else:
        msg = "Couldn't parse that time. Try: *09:00*, *3pm*, *18:30*."
        await update.message.reply_text(msg, parse_mode="Markdown")
        asyncio.create_task(_save_and_extract(msg))
        await set_setting(AWAITING_REMINDER_TIME_KEY, "NEEDED")


async def _classify_and_followup(
    task_id: int, text: str, update: Update, context: list[dict] | None = None
) -> None:
    try:
        long_term_context: str | None = None
        memory_url = getattr(settings, "memory_agent_url", "")
        if memory_url:
            try:
                from langchain_mcp_adapters.client import MultiServerMCPClient
                async with MultiServerMCPClient({
                    "memory": {
                        "url": f"{memory_url}/mcp",
                        "transport": "streamable_http",
                    }
                }) as mcp_client:
                    tools = {t.name: t for t in mcp_client.get_tools()}
                    if "query_memory" in tools:
                        result = await tools["query_memory"].ainvoke({"query": text})
                        long_term_context = str(result) if result else None
            except Exception as e:
                logger.debug("MCP memory query skipped: %s", e)

        user_tz = await _get_user_tz()
        classification = await classify_task(
            text, context=context, long_term_context=long_term_context, user_tz=user_tz
        )
        await set_task_type(task_id, classification.type)

        async def _reply(msg: str) -> None:
            await update.message.reply_text(msg, parse_mode="Markdown")
            asyncio.create_task(_save_and_extract(msg))

        emoji = _TYPE_EMOJI.get(classification.type, "•")
        lines = [f"→ {emoji} *{classification.type}*"]

        if classification.type in _DISCOVERY_TYPES:
            lines.append(
                f"Discovery runs tonight at {settings.discovery_hour:02d}:{settings.discovery_minute:02d} UTC."
            )

        if classification.type in _BUYER_TYPES:
            search_query = classification.search_query or text
            location_type = classification.location or "any"
            await set_setting(AWAITING_TASK_KEY, str(task_id))
            await set_setting(AWAITING_QUERY_KEY, search_query)
            await set_setting(AWAITING_LOCATION_KEY, location_type)
            lines.append("When do you need this by? _(e.g. today, end of week, no rush)_")
            await _reply("\n".join(lines))
            return

        if classification.type == "reminder":
            due_date = classification.due_date
            due_time = classification.due_time

            if due_date and due_time:
                await update_task_reminder(task_id, due_date, due_time)
                lines.append(_format_reminder_confirm(due_date, due_time, user_tz))
                await _reply("\n".join(lines))
            elif due_date and not due_time:
                await set_setting(AWAITING_REMINDER_TASK_KEY, str(task_id))
                await set_setting(AWAITING_REMINDER_DATE_KEY, due_date)
                await set_setting(AWAITING_REMINDER_TIME_KEY, "NEEDED")
                lines.append(f"Got date *{due_date}*. At what time? _(e.g. 09:00, 3pm)_")
                await _reply("\n".join(lines))
            elif due_time and not due_date:
                await set_setting(AWAITING_REMINDER_TASK_KEY, str(task_id))
                await set_setting(AWAITING_REMINDER_TIME_KEY, due_time)
                await set_setting(AWAITING_REMINDER_DATE_KEY, "NEEDED")
                lines.append(f"Got time *{due_time}* UTC. What date? _(e.g. today, tomorrow, Apr 5)_")
                await _reply("\n".join(lines))
            else:
                await set_setting(AWAITING_REMINDER_TASK_KEY, str(task_id))
                await set_setting(AWAITING_REMINDER_DATE_KEY, "NEEDED")
                await set_setting(AWAITING_REMINDER_TIME_KEY, "NEEDED")
                lines.append("When should I remind you? _(e.g. tomorrow at 9am, Apr 5 18:00)_")
                await _reply("\n".join(lines))
            return

        if classification.type in _GITHUB_TYPES:
            github_url = await save_to_github(
                task_id=task_id,
                task_type=classification.type,
                title=classification.title,
                body=text,
            )
            if github_url:
                lines.append(f"[Saved to GitHub]({github_url})")

        await _reply("\n".join(lines))

    except Exception as e:
        logger.warning("Background classification failed for task #%d: %s", task_id, e)
