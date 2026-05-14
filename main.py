import asyncio
import logging
from datetime import time, timezone

from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

from bot.handlers.commands import cmd_debug_run, cmd_list, cmd_location, cmd_reminders, cmd_report, cmd_sethome, cmd_setlocation, cmd_settimezone, cmd_status, cmd_timezone
from bot.handlers.idea import handle_message
from bot.jobs.discovery import run_discovery
from bot.jobs.memory import check_session_idle, daily_reflection
from bot.jobs.reminders import check_reminders
from bot.jobs.notifier import check_completions
from config import settings

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def post_init(application) -> None:
    # Claim the polling session: delete any existing webhook and flush pending updates.
    # This prevents 409 Conflict when the container restarts.
    await application.bot.delete_webhook(drop_pending_updates=True)
    # Brief pause so Telegram can close any in-flight long-poll from the old instance.
    await asyncio.sleep(2)
    logger.info("Session claimed, starting polling.")


def main() -> None:
    app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("debug_run", cmd_debug_run))
    app.add_handler(CommandHandler("location", cmd_location))
    app.add_handler(CommandHandler("setlocation", cmd_setlocation))
    app.add_handler(CommandHandler("sethome", cmd_sethome))
    app.add_handler(CommandHandler("timezone", cmd_timezone))
    app.add_handler(CommandHandler("settimezone", cmd_settimezone))
    app.add_handler(CommandHandler("reminders", cmd_reminders))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.job_queue.run_daily(run_discovery, time=settings.discovery_time, name="nightly_discovery")
    logger.info(
        "Nightly discovery scheduled at %02d:%02d UTC.",
        settings.discovery_hour,
        settings.discovery_minute,
    )
    app.job_queue.run_repeating(check_reminders, interval=60, first=10, name="reminder_check")
    app.job_queue.run_repeating(check_completions, interval=60, first=15, name="completion_check")
    logger.info("Reminder and completion check jobs registered (every 60s).")

    # Memory update jobs
    app.job_queue.run_repeating(check_session_idle, interval=60, first=30, name="session_idle_check")
    app.job_queue.run_daily(
        daily_reflection,
        time=time(3, 0, tzinfo=timezone.utc),
        name="daily_reflection",
    )
    logger.info("Memory jobs registered (session idle: 60s, daily reflection: 03:00 UTC).")

    logger.info("Bot starting (long polling)...")
    app.run_polling(drop_pending_updates=True, allowed_updates=["message"])


if __name__ == "__main__":
    main()
