"""Fast single-call LLM task classifier. No LangGraph needed — one prompt, one response."""
import json
import logging
from dataclasses import dataclass

from config import settings

logger = logging.getLogger(__name__)

TASK_TYPES = {
    "idea": "startup idea, product concept, business opportunity to explore or validate",
    "todo": "action item, task, thing to build or do",
    "note": "reference info, fact, link, or resource to save",
    "learning": "lesson learned, personal insight, or discovery worth remembering",
    "architecture": "technical design decision, system architecture, or engineering choice",
    "question": "open question to research or think through",
}

_TYPE_LIST = "\n".join(f'- {k}: {v}' for k, v in TASK_TYPES.items())


@dataclass
class TaskClassification:
    type: str
    title: str       # short form, max ~60 chars
    reason: str      # why this type was chosen


async def classify_task(text: str) -> TaskClassification:
    prompt = f"""Classify the following message into exactly one task type.

Message: "{text}"

Types:
{_TYPE_LIST}

Rules:
- If it describes a startup/product concept → idea
- If it starts with a verb and is actionable → todo
- If it records a decision or technical tradeoff → architecture
- If it's something the person learned → learning
- Default to note if nothing else fits

Respond with JSON only, no markdown:
{{"type": "...", "title": "...", "reason": "..."}}

Title should be a concise version of the message (max 60 chars)."""

    try:
        result = await _call_llm(prompt)
        data = json.loads(result.strip())
        task_type = data.get("type", "note")
        if task_type not in TASK_TYPES:
            task_type = "note"
        return TaskClassification(
            type=task_type,
            title=data.get("title", text[:60]),
            reason=data.get("reason", ""),
        )
    except Exception as e:
        logger.warning("Classification failed, defaulting to 'note': %s", e)
        return TaskClassification(type="note", title=text[:60], reason="classification failed")


async def _call_llm(prompt: str) -> str:
    if settings.llm_provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        from langchain_core.messages import HumanMessage
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.0-flash",  # fast + cheap for classification
            google_api_key=settings.google_gemini_api_key,
        )
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        content = response.content
        if isinstance(content, list):
            content = "".join(
                p.get("text", "") if isinstance(p, dict) else (p.text if hasattr(p, "text") else str(p))
                for p in content
            )
        return content

    elif settings.llm_provider == "claude":
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage
        llm = ChatAnthropic(model="claude-sonnet-4-6", api_key=settings.anthropic_api_key)
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        return response.content

    elif settings.llm_provider == "openai":
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage
        llm = ChatOpenAI(model="gpt-4o", api_key=settings.openai_api_key)
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        return response.content

    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider}")
