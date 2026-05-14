"""Parse natural-language deadline text into a concrete date and urgency strategy."""
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class DeadlineInfo:
    date: date | None           # concrete date, or None if "no rush"
    days_until: int | None      # days from today, None = no rush
    label: str                  # human-readable: "today", "Fri Apr 4", "no rush"
    strategy: str               # asap | fast | week | flexible | any


@dataclass
class ReminderDatetime:
    due_date: str | None   # YYYY-MM-DD or None
    due_time: str | None   # HH:MM UTC or None
    label: str             # human-readable


async def parse_reminder_datetime(text: str, user_tz: str = "UTC") -> ReminderDatetime:
    """Extract date and time from a reminder reply. Converts local time to UTC."""
    today = datetime.now(timezone.utc).date()
    prompt = f"""Today is {today.isoformat()} ({today.strftime('%A')}). User's timezone: {user_tz}.

Parse this reminder scheduling text. The user may write in any language including Russian.

Text: "{text}"

Extract the intended date and time. If the user specifies a local time (not UTC),
convert it to UTC using their timezone.

Examples with user timezone Europe/Budapest (UTC+2 in summer, UTC+1 in winter):
- "tomorrow at 10" → date=tomorrow, time=10:00 local → 08:00 UTC
- "завтра в 10" (Russian: tomorrow at 10) → date=tomorrow, time=08:00 UTC
- "в 10" (Russian: at 10) → time=08:00 UTC
- "April 5" → date=2026-04-05, time=null
- "14:30" → time=14:30 local → 12:30 UTC
- "10:00" alone when asked for time → time=10:00 local → convert to UTC

Respond with JSON only, no markdown:
{{"due_date": "YYYY-MM-DD or null", "due_time": "HH:MM UTC or null", "label": "brief human-readable in original language"}}"""

    try:
        content = await _call_llm(prompt)
        text_r = content.strip()
        if "```" in text_r:
            text_r = text_r.split("```")[1]
            if text_r.startswith("json"):
                text_r = text_r[4:]
        data = json.loads(text_r.strip())
        due_date = data.get("due_date")
        due_time = data.get("due_time")
        return ReminderDatetime(
            due_date=due_date if due_date and due_date != "null" else None,
            due_time=due_time if due_time and due_time != "null" else None,
            label=data.get("label", text[:40]),
        )
    except Exception as e:
        logger.warning("Reminder datetime parse failed: %s", e)
        return ReminderDatetime(due_date=None, due_time=None, label=text[:40])


def _strategy_from_days(days: int | None) -> str:
    if days is None:
        return "any"
    if days == 0:
        return "asap"
    if days <= 3:
        return "fast"
    if days <= 7:
        return "week"
    return "flexible"


async def parse_deadline(text: str) -> DeadlineInfo:
    today = datetime.now(timezone.utc).date()
    prompt = f"""Today is {today.isoformat()} ({today.strftime('%A')}).

Parse this deadline/urgency text and return a JSON object.

Text: "{text}"

Rules:
- "today" / "asap" / "now" / "immediately" → date = today
- "tomorrow" → date = tomorrow
- "end of week" / "this week" / "by friday" → date = this coming Friday
- "end of month" / "this month" → date = last day of current month
- "next week" → date = next Friday
- "no rush" / "whenever" / "no deadline" / "eventually" → date = null
- Specific dates like "April 10" or "10.04" → parse literally

Respond with JSON only:
{{"date": "YYYY-MM-DD or null", "label": "human-readable short label", "reasoning": "brief"}}"""

    try:
        content = await _call_llm(prompt)
        text_r = content.strip()
        if "```" in text_r:
            text_r = text_r.split("```")[1]
            if text_r.startswith("json"):
                text_r = text_r[4:]
        data = json.loads(text_r.strip())

        raw_date = data.get("date")
        parsed_date = None
        days_until = None

        if raw_date and raw_date != "null":
            parsed_date = date.fromisoformat(raw_date)
            days_until = (parsed_date - today).days
            if days_until < 0:
                days_until = 0

        label = data.get("label", text[:30])
        strategy = _strategy_from_days(days_until)
        return DeadlineInfo(date=parsed_date, days_until=days_until, label=label, strategy=strategy)

    except Exception as e:
        logger.warning("Deadline parse failed: %s", e)
        return DeadlineInfo(date=None, days_until=None, label="no rush", strategy="any")


async def _call_llm(prompt: str) -> str:
    from langchain_core.messages import HumanMessage
    if settings.llm_provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        llm = ChatGoogleGenerativeAI(
            model="gemini-3.1-flash-lite",
            google_api_key=settings.google_gemini_api_key,
        )
    elif settings.llm_provider == "claude":
        from langchain_anthropic import ChatAnthropic
        llm = ChatAnthropic(model="claude-sonnet-4-6", api_key=settings.anthropic_api_key)
    elif settings.llm_provider == "openai":
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(model="gpt-4o", api_key=settings.openai_api_key)
    else:
        raise ValueError(f"Unknown provider: {settings.llm_provider}")

    response = await llm.ainvoke([HumanMessage(content=prompt)])
    content = response.content
    if isinstance(content, list):
        content = "".join(
            p.get("text", "") if isinstance(p, dict) else (p.text if hasattr(p, "text") else str(p))
            for p in content
        )
    return content
