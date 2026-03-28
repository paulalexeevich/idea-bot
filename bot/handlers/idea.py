import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from agent.classifier import classify_task
from agent.deadline import parse_deadline
from bot.integrations.github import save_to_github
from config import settings
from db.client import create_task, get_setting, set_setting, set_task_type, update_task_deadline

logger = logging.getLogger(__name__)

_TYPE_EMOJI = {
    "idea": "💡",
    "todo": "📋",
    "note": "📝",
    "learning": "🧠",
    "architecture": "🏗️",
    "question": "❓",
    "shopping": "🛒",
}

_DISCOVERY_TYPES = {"idea"}
_GITHUB_TYPES = {"architecture", "learning"}
_BUYER_TYPES = {"shopping"}

AWAITING_TASK_KEY = "awaiting_task_id"
AWAITING_QUERY_KEY = "awaiting_search_query"
AWAITING_LOCATION_KEY = "awaiting_location_type"


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != settings.telegram_user_id:
        return

    text = update.message.text.strip()
    if not text:
        return

    # Check if we're waiting for a deadline reply for a shopping task
    awaiting = await get_setting(AWAITING_TASK_KEY)
    if awaiting:
        await _handle_deadline_reply(int(awaiting), text, update)
        return

    # Save immediately with default type, reply at once
    task_id = await create_task(text, type="note")
    await update.message.reply_text(f"Task #{task_id} saved ✓")

    # Classify in background — no await
    asyncio.create_task(_classify_and_followup(task_id, text, update))


async def _handle_deadline_reply(task_id: int, text: str, update: Update) -> None:
    try:
        # Clear the awaiting state first
        await set_setting(AWAITING_TASK_KEY, "")
        search_query = await get_setting(AWAITING_QUERY_KEY) or ""
        location_type = await get_setting(AWAITING_LOCATION_KEY) or "any"

        deadline_info = await parse_deadline(text)
        await update_task_deadline(
            task_id,
            deadline_info.date.isoformat() if deadline_info.date else None,
            deadline_info.strategy,
        )

        loc_hint = {"local": "📍 local stores", "online": "🌐 online", "any": "🔍 all sources"}.get(location_type, "")
        await update.message.reply_text(
            f"🗓 Deadline: *{deadline_info.label}* → strategy: `{deadline_info.strategy}`\nSearching {loc_hint}…",
            parse_mode="Markdown",
        )

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
        await update.message.reply_text("Something went wrong parsing the deadline. Searching anyway…")
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


async def _classify_and_followup(task_id: int, text: str, update: Update) -> None:
    try:
        classification = await classify_task(text)
        await set_task_type(task_id, classification.type)

        emoji = _TYPE_EMOJI.get(classification.type, "•")
        lines = [f"→ {emoji} *{classification.type}*"]

        if classification.type in _DISCOVERY_TYPES:
            lines.append(f"Discovery runs tonight at {settings.discovery_hour:02d}:{settings.discovery_minute:02d} UTC.")

        if classification.type in _BUYER_TYPES:
            # Ask for deadline before running buyer
            search_query = classification.search_query or text
            location_type = classification.location or "any"
            await set_setting(AWAITING_TASK_KEY, str(task_id))
            await set_setting(AWAITING_QUERY_KEY, search_query)
            await set_setting(AWAITING_LOCATION_KEY, location_type)
            lines.append("When do you need this by? _(e.g. today, end of week, no rush)_")
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
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

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    except Exception as e:
        logger.warning("Background classification failed for task #%d: %s", task_id, e)
