"""Periodic job: check for due reminders and send Telegram notifications."""
import logging
from datetime import datetime, timezone

from telegram.ext import ContextTypes

from config import settings
from db.client import get_due_reminders, mark_task_notified

logger = logging.getLogger(__name__)


async def check_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    try:
        due = await get_due_reminders(now_iso)
    except Exception as e:
        logger.warning("Failed to fetch due reminders: %s", e)
        return

    for task in due:
        task_id = task["id"]
        title = task.get("text", "")[:80]
        due_date = task.get("due_date", "")
        due_time = task.get("due_time", "")

        # Show local time if user has a timezone set
        local_str = ""
        try:
            from db.client import get_setting
            user_tz = await get_setting("user_timezone") or "UTC"
            if user_tz != "UTC" and due_date and due_time:
                from zoneinfo import ZoneInfo
                dt_utc = datetime.strptime(f"{due_date} {due_time}", "%Y-%m-%d %H:%M").replace(
                    tzinfo=ZoneInfo("UTC")
                )
                dt_local = dt_utc.astimezone(ZoneInfo(user_tz))
                local_str = f" ({dt_local.strftime('%H:%M')} {dt_local.strftime('%Z')})"
        except Exception:
            pass

        try:
            await context.bot.send_message(
                chat_id=settings.telegram_user_id,
                text=f"⏰ Reminder #{task_id}: {title}\nScheduled: {due_date} {due_time} UTC{local_str}",
            )
            await mark_task_notified(task_id)
            logger.info("Reminder #%d notified.", task_id)
        except Exception as e:
            logger.warning("Failed to notify reminder #%d: %s", task_id, e)
