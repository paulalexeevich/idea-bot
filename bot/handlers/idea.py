import asyncio
import logging
from datetime import datetime, timezone

import httpx
from telegram import Update
from telegram.ext import ContextTypes

from agent.deadline import parse_deadline, parse_reminder_datetime
from agent.unified_agent import run as run_unified_agent
from bot.integrations.github import save_to_github
from config import settings
from db.client import (
    get_recent_messages,
    get_setting,
    save_message,
    set_setting,
    update_task_deadline,
    update_task_reminder,
)

logger = logging.getLogger(__name__)

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
    """Save a bot reply to the messages DB and trigger Tier 1 memory extraction."""
    await save_message("bot", content)
    memory_url = getattr(settings, "memory_agent_url", "")
    if memory_url:
        try:
            async with httpx.AsyncClient(timeout=5.0) as hc:
                await hc.post(f"{memory_url}/memory/process-now")
        except Exception:
            pass


def _format_reminder_confirm(due_date: str, due_time_utc: str, user_tz: str = "UTC") -> str:
    """Format reminder confirmation with local time when TZ differs from UTC."""
    if user_tz == "UTC":
        return f"Reminder set for *{due_date}* at *{due_time_utc}* UTC."
    try:
        from zoneinfo import ZoneInfo
        dt_utc = datetime.strptime(f"{due_date} {due_time_utc}", "%Y-%m-%d %H:%M").replace(
            tzinfo=ZoneInfo("UTC")
        )
        dt_local = dt_utc.astimezone(ZoneInfo(user_tz))
        return (
            f"Reminder set for *{due_date}* at "
            f"*{dt_local.strftime('%H:%M')}* {dt_local.strftime('%Z')} "
            f"(*{due_time_utc}* UTC)."
        )
    except Exception:
        return f"Reminder set for *{due_date}* at *{due_time_utc}* UTC."


# ── Main handler ──────────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != settings.telegram_user_id:
        return

    text = update.message.text.strip()
    if not text:
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    asyncio.create_task(save_message("user", text))
    asyncio.create_task(set_setting("last_user_message_at", now_iso))

    # ── Awaiting flows (Phase 1 — state-machine for multi-turn clarification) ─

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

    awaiting = await get_setting(AWAITING_TASK_KEY)
    if awaiting:
        await _handle_deadline_reply(int(awaiting), text, update)
        return

    # ── Unified agent handles all fresh messages ───────────────────────────────

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    try:
        recent = await get_recent_messages(limit=20)
    except Exception:
        recent = []

    user_tz = await _get_user_tz()
    result = await run_unified_agent(text, recent_messages=recent, user_tz=user_tz)

    await update.message.reply_text(result.reply, parse_mode="Markdown")
    asyncio.create_task(_save_and_extract(result.reply))

    # Persist any awaiting state the agent set (reminder needs date/time)
    for key, value in result.awaiting.items():
        asyncio.create_task(set_setting(key, value))

    # GitHub integration for architecture and learning tasks
    if result.task_type in {"architecture", "learning"} and result.task_id:
        asyncio.create_task(
            _save_to_github_bg(result.task_id, result.task_type, text)
        )


# ── Background helpers ────────────────────────────────────────────────────────

async def _save_to_github_bg(task_id: int, task_type: str, text: str) -> None:
    try:
        await save_to_github(
            task_id=task_id, task_type=task_type, title=text[:60], body=text
        )
    except Exception as e:
        logger.warning("GitHub save failed for task #%d: %s", task_id, e)


# ── Awaiting-state handlers (Phase 1 — kept until Phase 2 removes state machine) ─

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

        loc_hint = {
            "local": "📍 local stores", "online": "🌐 online", "any": "🔍 all sources"
        }.get(location_type, "")
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
        logger.warning("Deadline reply failed for task #%d: %s", task_id, e)
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
    """User was asked for a time. Use LLM parser first, fall back to regex."""
    await set_setting(AWAITING_REMINDER_TIME_KEY, "")
    stored_date = await get_setting(AWAITING_REMINDER_DATE_KEY)
    user_tz = await _get_user_tz()

    dt = await parse_reminder_datetime(text, user_tz)
    due_time = dt.due_time

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
