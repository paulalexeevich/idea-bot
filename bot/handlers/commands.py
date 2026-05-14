import datetime
import json

from telegram import Update
from telegram.ext import ContextTypes

from config import settings
from db.client import get_discovery_for_task, get_recent_tasks, get_setting, get_task_by_id, get_task_counts, set_setting
from bot.jobs.buyer import HOME_KEY, CURRENT_KEY

USER_TZ_KEY = "user_timezone"

_STATUS_EMOJI = {"pending": "⏳", "processing": "🔄", "done": "✅", "error": "❌"}
_TYPE_EMOJI = {"idea": "💡", "todo": "📋", "note": "📝"}


def _guard(update: Update) -> bool:
    return update.effective_user.id == settings.telegram_user_id


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update):
        return
    tasks = await get_recent_tasks(10)
    if not tasks:
        await update.message.reply_text("No tasks yet. Just send me a message!")
        return
    lines = [
        f"{_STATUS_EMOJI.get(t.status, '?')} {_TYPE_EMOJI.get(t.type, '•')} "
        f"#{t.id} — {t.text[:55]}{'…' if len(t.text) > 55 else ''}"
        for t in tasks
    ]
    await update.message.reply_text("\n".join(lines))


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update):
        return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /report <task_id>")
        return

    task_id = int(args[0])
    task = await get_task_by_id(task_id)
    if not task:
        await update.message.reply_text(f"Task #{task_id} not found.")
        return

    discovery = await get_discovery_for_task(task_id)
    if not discovery:
        await update.message.reply_text(
            f"No discovery yet for task #{task_id} (status: {task.status}).\n"
            "Discovery runs nightly. Use /debug_run to trigger manually."
        )
        return

    score_bar = "█" * round(discovery.score or 0) + "░" * (10 - round(discovery.score or 0))
    competitors = ""
    if discovery.full_report:
        comps = discovery.full_report.get("competitors", []) if isinstance(discovery.full_report, dict) else []
        if comps:
            competitors = "\n\n*Competitors:*\n" + "\n".join(f"• {c}" for c in comps)

    report = (
        f"*Task #{task_id}* [{task.type}]\n{task.text}\n\n"
        f"*Score:* {discovery.score:.1f}/10  [{score_bar}]\n\n"
        f"*Verdict:*\n{discovery.verdict}\n\n"
        f"*Market Size:*\n{discovery.market_size}\n\n"
        f"*Community Sentiment:*\n"
        f"{discovery.ih_summary or discovery.reddit_summary or discovery.hn_summary or 'N/A'}"
        f"{competitors}"
    )
    await update.message.reply_text(report, parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update):
        return
    counts = await get_task_counts()
    total = sum(counts.values())
    pending = counts.get("pending", 0)
    done = counts.get("done", 0)
    error = counts.get("error", 0)

    now = datetime.datetime.now(datetime.timezone.utc)
    next_run = now.replace(
        hour=settings.discovery_hour,
        minute=settings.discovery_minute,
        second=0,
        microsecond=0,
    )
    if next_run <= now:
        next_run += datetime.timedelta(days=1)

    await update.message.reply_text(
        f"Tasks: {total} total | {pending} pending | {done} done | {error} errors\n"
        f"Next discovery run: {next_run.strftime('%Y-%m-%d %H:%M UTC')}"
    )


async def cmd_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update):
        return
    home = await get_setting(HOME_KEY) or settings.home_location or "not set"
    current = await get_setting(CURRENT_KEY) or home
    await update.message.reply_text(
        f"🏠 Home: *{home}*\n📍 Current: *{current}*\n\n"
        "Use `/setlocation <city, country>` to update current location.\n"
        "Use `/sethome <city, country>` to update home.",
        parse_mode="Markdown",
    )


async def cmd_setlocation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /setlocation <city, country>\nExample: /setlocation Amsterdam, Netherlands")
        return
    location = " ".join(context.args)
    await set_setting(CURRENT_KEY, location)
    await update.message.reply_text(f"📍 Current location set to *{location}*", parse_mode="Markdown")


async def cmd_sethome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /sethome <city, country>\nExample: /sethome Moscow, Russia")
        return
    location = " ".join(context.args)
    await set_setting(HOME_KEY, location)
    await update.message.reply_text(f"🏠 Home location set to *{location}*", parse_mode="Markdown")


async def cmd_debug_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update):
        return
    from bot.jobs.discovery import run_discovery
    await update.message.reply_text("Starting discovery run now...")
    await run_discovery(context)


async def cmd_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update):
        return
    tz = await get_setting(USER_TZ_KEY) or "UTC"
    await update.message.reply_text(
        f"🕐 Your timezone: *{tz}*\n\nUse `/settimezone Europe/Budapest` to change.\n"
        "Use IANA names: Europe/Budapest, America/New_York, Asia/Tokyo, UTC",
        parse_mode="Markdown",
    )


async def cmd_settimezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /settimezone <timezone>\n"
            "Examples: `/settimezone Europe/Budapest` · `/settimezone UTC`",
            parse_mode="Markdown",
        )
        return
    tz = " ".join(context.args)
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
    except Exception:
        await update.message.reply_text(
            f"❌ Unknown timezone `{tz}`.\n"
            "Use IANA names like `Europe/Budapest`, `America/New_York`, `Asia/Tokyo`, or `UTC`.",
            parse_mode="Markdown",
        )
        return
    await set_setting(USER_TZ_KEY, tz)
    await update.message.reply_text(f"🕐 Timezone set to *{tz}*", parse_mode="Markdown")


async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update):
        return
    from db.client import get_upcoming_reminders
    from zoneinfo import ZoneInfo

    user_tz = await get_setting(USER_TZ_KEY) or "UTC"
    reminders = await get_upcoming_reminders()

    if not reminders:
        await update.message.reply_text("No upcoming reminders.")
        return

    lines = []
    for t in reminders:
        due_date = t.due_date or "?"
        due_time = t.due_time or "?"
        local_str = ""
        if t.due_date and t.due_time and user_tz != "UTC":
            try:
                from datetime import datetime
                dt_utc = datetime.strptime(f"{t.due_date} {t.due_time}", "%Y-%m-%d %H:%M").replace(
                    tzinfo=ZoneInfo("UTC")
                )
                dt_local = dt_utc.astimezone(ZoneInfo(user_tz))
                local_str = f" ({dt_local.strftime('%H:%M')} {dt_local.strftime('%Z')})"
            except Exception:
                pass
        lines.append(f"⏰ #{t.id} — {due_date} {due_time} UTC{local_str}\n   {t.text[:60]}")

    await update.message.reply_text("\n\n".join(lines))
