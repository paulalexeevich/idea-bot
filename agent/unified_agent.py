"""
Unified agent — handles ALL incoming user messages.

Replaces: classifier.py routing + _classify_and_followup + query_agent.run_query

Every message is:
  1. Optionally looked up in memory / DB (query_memory, list_tasks, search_tasks)
  2. Always saved as a task (save_reminder or save_task)
  3. Answered with a natural reply in the user's language

The agent runs as a tool-calling loop (up to 10 rounds).
MCP memory tools are loaded once at the start and kept open for the full run.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from config import settings

logger = logging.getLogger(__name__)


# ── Result ────────────────────────────────────────────────────────────────────

@dataclass
class AgentResult:
    reply: str
    task_id: int | None = None
    task_type: str | None = None
    awaiting: dict[str, str] = field(default_factory=dict)


# ── Tool schemas ──────────────────────────────────────────────────────────────

class _ListTasksInput(BaseModel):
    type: str = Field(
        default="",
        description=(
            "Filter by type: idea, todo, note, reminder, shopping, learning, "
            "architecture, question, query. Empty = all types."
        ),
    )
    status: str = Field(default="pending", description="pending | done | all")
    limit: int = Field(default=10, description="Max results 1–20")


class _SearchTasksInput(BaseModel):
    query: str = Field(description="Keywords to search in task text")
    limit: int = Field(default=10, description="Max results 1–20")


class _SaveReminderInput(BaseModel):
    text: str = Field(description="Original user message verbatim")
    title: str = Field(description="Concise title, max 60 chars")
    due_date: str = Field(
        default="",
        description="Date in user's LOCAL timezone YYYY-MM-DD. Empty if not mentioned.",
    )
    due_time: str = Field(
        default="",
        description="Time in user's LOCAL timezone HH:MM. Empty if not mentioned.",
    )


class _SaveTaskInput(BaseModel):
    text: str = Field(description="Original user message verbatim")
    title: str = Field(description="Concise title, max 60 chars")
    type: str = Field(
        description=(
            "Task type: idea | todo | note | learning | architecture | "
            "question | shopping | query"
        ),
    )


class _AskClarificationInput(BaseModel):
    question: str = Field(description="Specific question to ask the user")


# ── Helpers ───────────────────────────────────────────────────────────────────

_TYPE_EMOJI = {
    "idea": "💡", "todo": "📋", "note": "📝", "learning": "🧠",
    "architecture": "🏗️", "question": "❓", "shopping": "🛒",
    "reminder": "⏰", "query": "🔍",
}
_STATUS_EMOJI = {"pending": "⏳", "done": "✅", "error": "❌"}


def _fmt_task(t: Any, user_tz: str = "UTC") -> str:
    emoji = _TYPE_EMOJI.get(t.type, "•")
    status = _STATUS_EMOJI.get(t.status, "")
    text = t.text[:120] + ("…" if len(t.text) > 120 else "")
    line = f"{emoji}{status} #{t.id} — {text}"
    if t.due_date and t.due_time:
        time_str = f"{t.due_date} {t.due_time} UTC"
        if user_tz != "UTC":
            try:
                from zoneinfo import ZoneInfo
                dt = datetime.strptime(
                    f"{t.due_date} {t.due_time}", "%Y-%m-%d %H:%M"
                ).replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(user_tz))
                time_str = f"{dt.strftime('%Y-%m-%d %H:%M')} {dt.strftime('%Z')}"
            except Exception:
                pass
        line += f"\n   ⏰ {time_str}"
    return line


def _local_to_utc(date_str: str, time_str: str, user_tz: str) -> tuple[str, str]:
    """Convert local date+time to UTC. Returns (utc_date, utc_time)."""
    if user_tz == "UTC" or not (date_str and time_str):
        return date_str, time_str
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.strptime(
            f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=ZoneInfo(user_tz)).astimezone(ZoneInfo("UTC"))
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
    except Exception:
        return date_str, time_str


async def _api_get(path: str, params: dict) -> list[dict]:
    import httpx
    async with httpx.AsyncClient(
        base_url=settings.data_api_url,
        headers={"X-API-Key": settings.data_api_key},
        timeout=15.0,
    ) as c:
        r = await c.get(path, params=params)
        r.raise_for_status()
        return r.json()


async def _api_post(path: str, body: dict) -> dict:
    import httpx
    async with httpx.AsyncClient(
        base_url=settings.data_api_url,
        headers={"X-API-Key": settings.data_api_key},
        timeout=15.0,
    ) as c:
        r = await c.post(path, json=body)
        r.raise_for_status()
        return r.json()


async def _api_patch(path: str, body: dict) -> None:
    import httpx
    async with httpx.AsyncClient(
        base_url=settings.data_api_url,
        headers={"X-API-Key": settings.data_api_key},
        timeout=15.0,
    ) as c:
        r = await c.patch(path, json=body)
        r.raise_for_status()


# ── Tool execution ────────────────────────────────────────────────────────────

_TERMINAL_TOOLS = {"save_reminder", "save_task", "ask_clarification"}
_LOCAL_TOOLS = {
    "list_tasks", "search_tasks",
    "save_reminder", "save_task", "ask_clarification",
}


async def _exec_tool(
    name: str, args: dict, user_tz: str
) -> tuple[str, AgentResult | None]:
    """
    Execute a local tool.
    Returns (result_text_for_llm, AgentResult | None).
    AgentResult is set only for terminal tools.
    """
    from db.client import _to_task

    # ── list_tasks ────────────────────────────────────────────────────────────
    if name == "list_tasks":
        task_type = args.get("type") or None
        raw_status = args.get("status", "pending")
        status = None if raw_status == "all" else raw_status
        limit = min(int(args.get("limit", 10)), 20)
        params: dict = {"limit": limit}
        if task_type:
            params["type"] = task_type
        if status:
            params["status"] = status
        try:
            rows = await _api_get("/tasks", params)
            tasks = [_to_task(d) for d in rows]
        except Exception as e:
            return f"Error fetching tasks: {e}", None
        if not tasks:
            return "No tasks found.", None
        return "\n".join(_fmt_task(t, user_tz) for t in tasks), None

    # ── search_tasks ──────────────────────────────────────────────────────────
    if name == "search_tasks":
        query = args.get("query", "")
        limit = min(int(args.get("limit", 10)), 20)
        try:
            rows = await _api_get("/tasks/search", {"q": query, "limit": limit})
            tasks = [_to_task(d) for d in rows]
        except Exception as e:
            return f"Error searching: {e}", None
        if not tasks:
            return f"No tasks found matching '{query}'.", None
        return (
            f"Found {len(tasks)} result(s) for '{query}':\n"
            + "\n".join(_fmt_task(t, user_tz) for t in tasks)
        ), None

    # ── save_task ─────────────────────────────────────────────────────────────
    if name == "save_task":
        try:
            resp = await _api_post("/tasks", {"text": args["text"], "type": args["type"]})
            task_id = resp["id"]
        except Exception as e:
            return f"Error saving task: {e}", AgentResult(reply=f"Error: {e}")
        return (
            f"Task #{task_id} saved as {args['type']}.",
            AgentResult(reply="", task_id=task_id, task_type=args["type"]),
        )

    # ── save_reminder ─────────────────────────────────────────────────────────
    if name == "save_reminder":
        try:
            resp = await _api_post("/tasks", {"text": args["text"], "type": "reminder"})
            task_id = resp["id"]
        except Exception as e:
            return f"Error saving reminder: {e}", AgentResult(reply=f"Error: {e}")

        due_date = (args.get("due_date") or "").strip()
        due_time = (args.get("due_time") or "").strip()
        due_date_utc, due_time_utc = _local_to_utc(due_date, due_time, user_tz)

        if due_date_utc or due_time_utc:
            try:
                await _api_patch(f"/tasks/{task_id}/reminder", {
                    "due_date": due_date_utc or None,
                    "due_time": due_time_utc or None,
                })
            except Exception as e:
                logger.warning("Reminder patch failed for #%d: %s", task_id, e)

        awaiting: dict[str, str] = {}
        if not due_date_utc and not due_time_utc:
            awaiting = {
                "awaiting_reminder_task_id": str(task_id),
                "awaiting_reminder_date": "NEEDED",
                "awaiting_reminder_time": "NEEDED",
            }
            tool_text = f"Reminder #{task_id} created. Missing: date and time."
        elif not due_date_utc:
            awaiting = {
                "awaiting_reminder_task_id": str(task_id),
                "awaiting_reminder_date": "NEEDED",
                "awaiting_reminder_time": due_time_utc,
            }
            tool_text = f"Reminder #{task_id} created. Got time={due_time_utc} UTC. Missing: date."
        elif not due_time_utc:
            awaiting = {
                "awaiting_reminder_task_id": str(task_id),
                "awaiting_reminder_date": due_date_utc,
                "awaiting_reminder_time": "NEEDED",
            }
            tool_text = f"Reminder #{task_id} created. Got date={due_date_utc}. Missing: time."
        else:
            tool_text = (
                f"Reminder #{task_id} saved for {due_date_utc} at {due_time_utc} UTC."
            )

        return tool_text, AgentResult(
            reply="", task_id=task_id, task_type="reminder", awaiting=awaiting
        )

    # ── ask_clarification ─────────────────────────────────────────────────────
    if name == "ask_clarification":
        return args["question"], AgentResult(reply=args["question"])

    return f"Unknown tool: {name}", None


# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM = """\
You are a personal productivity assistant. Every message must be saved AND answered.

Today: {today}. User's local timezone: {user_tz}.
Discovery for ideas runs nightly at {discovery_hour:02d}:{discovery_minute:02d} UTC.

--- Conversation history (last 20 messages) ---
{context}
--- End of history ---

How to handle every message:

LOOKUP (optional — call when relevant):
  query_memory    → check long-term memory for people/places/preferences mentioned
  list_tasks      → when user asks to see their saved data by type or status
  search_tasks    → when user mentions specific keywords to find

SAVE (required — always call exactly one):
  save_reminder   → time-based alerts ("remind me", "don't forget", "alert at")
                    due_date and due_time in user's LOCAL timezone (e.g. "10:00" not "08:00")
                    leave empty if not mentioned; the bot will ask for the missing piece
  save_task       → everything else
                    type: idea | todo | note | learning | architecture | question | shopping | query
                    use "query" when user is asking about their own saved data

STORE (optional):
  save_memory → when you learn a new lasting fact (who someone is, a preference, a routine)

REPLY:
  After all tool calls, write a concise natural reply. Rules:
  - Confirm what was saved in plain language (not "Task #N saved")
  - For reminders with full date+time: show the time in LOCAL timezone
  - For reminders with missing date or time: ask naturally for what's missing
  - For queries: summarise the results you found
  - For ideas: mention discovery runs tonight at {discovery_hour:02d}:{discovery_minute:02d} UTC
  - Reply in the SAME LANGUAGE the user used
"""


# ── LLM ───────────────────────────────────────────────────────────────────────

def _get_llm():
    if settings.llm_provider == "claude":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model="claude-haiku-4-5-20251001", api_key=settings.anthropic_api_key
        )
    elif settings.llm_provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model="gpt-4o-mini", api_key=settings.openai_api_key)
    else:
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model="gemini-2.5-flash", google_api_key=settings.google_gemini_api_key
        )


# ── Inner agent loop ──────────────────────────────────────────────────────────

async def _run_loop(
    text: str,
    recent: list[dict],
    user_tz: str,
    mcp_tools: list,
) -> AgentResult:
    from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage
    from langchain_core.tools import StructuredTool

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    context_str = (
        "\n".join(f"{m['role']}: {m['content'][:200]}" for m in recent[-20:])
        or "(no history yet)"
    )

    def _noop(**kwargs: Any) -> str:
        return ""

    local_tools = [
        StructuredTool.from_function(
            func=_noop, name="list_tasks", args_schema=_ListTasksInput,
            description="Fetch saved tasks filtered by type and/or status.",
        ),
        StructuredTool.from_function(
            func=_noop, name="search_tasks", args_schema=_SearchTasksInput,
            description="Search task text for keywords.",
        ),
        StructuredTool.from_function(
            func=_noop, name="save_reminder", args_schema=_SaveReminderInput,
            description=(
                "Save a time-based reminder. Use for 'remind me', 'alert me', "
                "'don't forget'. Provide date+time in user's local timezone."
            ),
        ),
        StructuredTool.from_function(
            func=_noop, name="save_task", args_schema=_SaveTaskInput,
            description="Save any non-reminder task (idea, todo, note, query, etc.).",
        ),
        StructuredTool.from_function(
            func=_noop, name="ask_clarification", args_schema=_AskClarificationInput,
            description=(
                "Ask the user for a specific missing piece. "
                "Use ONLY after save_reminder when date or time was not provided."
            ),
        ),
    ]

    mcp_tool_map = {t.name: t for t in mcp_tools}
    llm = _get_llm().bind_tools(local_tools + mcp_tools)

    messages: list = [
        SystemMessage(content=_SYSTEM.format(
            today=today,
            user_tz=user_tz,
            context=context_str,
            discovery_hour=settings.discovery_hour,
            discovery_minute=settings.discovery_minute,
        )),
        HumanMessage(content=text),
    ]

    pending: AgentResult | None = None  # set after a terminal tool fires

    for _ in range(10):
        response = await llm.ainvoke(messages)

        # No more tool calls → final reply
        if not response.tool_calls:
            content = response.content
            if isinstance(content, list):
                content = "".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in content
                )
            reply_text = (content or "").strip() or "Got it."
            if pending is not None:
                pending.reply = reply_text
                return pending
            return AgentResult(reply=reply_text)

        messages.append(response)

        for tc in response.tool_calls:
            name, args, call_id = tc["name"], tc["args"], tc["id"]

            if name in mcp_tool_map:
                try:
                    result_text = str(await mcp_tool_map[name].ainvoke(args))
                except Exception as e:
                    result_text = f"Memory error: {e}"
                messages.append(ToolMessage(content=result_text, tool_call_id=call_id))

            elif name in _LOCAL_TOOLS:
                result_text, final = await _exec_tool(name, args, user_tz)
                messages.append(ToolMessage(content=result_text, tool_call_id=call_id))
                if final is not None and name in _TERMINAL_TOOLS:
                    pending = final
            else:
                messages.append(ToolMessage(
                    content=f"Unknown tool: {name}", tool_call_id=call_id,
                ))

    reply = (pending.reply if pending and pending.reply else "Saved.")
    if pending:
        pending.reply = reply
        return pending
    return AgentResult(reply=reply)


# ── Public entry point ────────────────────────────────────────────────────────

async def run(
    text: str,
    recent_messages: list[dict],
    user_tz: str = "UTC",
) -> AgentResult:
    """
    Process one user message. Always saves it as a task and returns a reply.
    Opens MCP connection once for the full run if memory_agent_url is configured.
    """
    if settings.memory_agent_url:
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
            async with MultiServerMCPClient({
                "memory": {
                    "url": f"{settings.memory_agent_url}/mcp",
                    "transport": "streamable_http",
                }
            }) as client:
                mcp_tools = client.get_tools()
                return await _run_loop(text, recent_messages, user_tz, mcp_tools)
        except Exception as e:
            logger.debug("MCP unavailable, running without memory tools: %s", e)

    return await _run_loop(text, recent_messages, user_tz, [])
