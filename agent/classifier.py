"""
Task classifier using LangChain structured output (provider-agnostic).
Works with Gemini, OpenAI, or Claude — reads LLM_PROVIDER from config.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from config import settings

logger = logging.getLogger(__name__)

TASK_TYPES = {
    "idea":         "startup idea, product concept, business opportunity to explore or validate",
    "todo":         "action item, task, thing to build or do — no notification needed",
    "note":         "reference info, fact, link, or resource to save",
    "learning":     "lesson learned, personal insight, or discovery worth remembering",
    "architecture": "technical design decision, system architecture, or engineering choice",
    "question":     "open question to research or think through",
    "shopping":     "request to find and compare buying options for a product or item",
    "reminder":     "time-based alert — user wants to be notified at a specific date/time",
}

_SYSTEM = """You are a task classifier. Classify the user's message and extract all structured information.

Task types:
- reminder: user wants to be notified at a specific time ("remind me", "alert me at 3pm", "don't forget to call")
- shopping: find or buy a product
- idea: startup or product concept to validate
- todo: something to do, no specific notification time
- architecture: technical design decision
- learning: insight or lesson learned
- question: open question to think through
- note: anything else — link, fact, reference (default)

Examples:
- "Remind me to call Olga at 22:27 pm" → type=reminder, due_time=22:27 (it's already 24h, drop the pm)
- "Set alarm for 3 pm meeting" → type=reminder, due_time=15:00
- "Don't forget dentist tomorrow 9am" → type=reminder, due_date=<tomorrow>, due_time=09:00
- "Buy a standing desk" → type=shopping
- "Refactor auth module" → type=todo (no notification wanted)
- "Read the new RFC" → type=todo

Always extract:
- due_date: ISO date YYYY-MM-DD if explicitly mentioned, else empty string
- due_time: 24-hour HH:MM if a time is mentioned ("3 pm" → "15:00", "22:27 pm" → "22:27"), else empty string

Today is {today}.

{context_section}"""


class _ClassifyOutput(BaseModel):
    type: Literal["idea", "todo", "note", "learning", "architecture", "question", "shopping", "reminder"]
    title: str = Field(description="Concise version of the message, max 60 chars")
    reason: str = Field(description="One sentence explaining why this type was chosen")
    due_date: str = Field(default="", description="ISO date YYYY-MM-DD if mentioned, else empty string")
    due_time: str = Field(default="", description="24h time HH:MM if mentioned, else empty string")
    search_query: str = Field(default="", description="Clean search term for shopping tasks, else empty string")
    location: Literal["local", "online", "any"] = Field(
        default="any",
        description="For shopping: local=physical store, online=ship anywhere, any=both",
    )


@dataclass
class TaskClassification:
    type: str
    title: str
    reason: str
    search_query: str = ""
    location: str = "any"
    due_date: str | None = None
    due_time: str | None = None


def _get_llm():
    """Return a LangChain chat model for the configured provider."""
    if settings.llm_provider == "claude":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model="claude-haiku-4-5-20251001", api_key=settings.anthropic_api_key)
    elif settings.llm_provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model="gpt-4o-mini", api_key=settings.openai_api_key)
    else:  # gemini (default)
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model="gemini-3.1-flash-lite",
            google_api_key=settings.google_gemini_api_key,
        )


async def classify_task(
    text: str,
    context: list[dict] | None = None,
    long_term_context: str | None = None,
) -> TaskClassification:
    """
    Classify a task with optional memory injection.

    context: recent conversation [{role, content}, ...] for short-term memory.
    long_term_context: formatted string from knowledge graph for long-term memory.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    context_parts: list[str] = []
    if long_term_context:
        context_parts.append(f"What you know about this user:\n{long_term_context}")
    if context:
        history = "\n".join(f"{m['role']}: {m['content']}" for m in context[-20:])
        context_parts.append(f"Recent conversation:\n{history}")
    context_section = "\n\n".join(context_parts)

    try:
        from langchain_core.messages import SystemMessage, HumanMessage

        llm = _get_llm().with_structured_output(_ClassifyOutput)
        messages = [
            SystemMessage(content=_SYSTEM.format(today=today, context_section=context_section)),
            HumanMessage(content=text),
        ]
        data: _ClassifyOutput = await llm.ainvoke(messages)

        return TaskClassification(
            type=data.type,
            title=data.title,
            reason=data.reason,
            search_query=data.search_query,
            location=data.location,
            due_date=data.due_date or None,
            due_time=data.due_time or None,
        )

    except Exception as e:
        logger.warning("Classification failed, defaulting to 'note': %s", e)
        return TaskClassification(type="note", title=text[:60], reason="classification failed")
