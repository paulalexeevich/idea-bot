"""
Query agent — answers questions about saved tasks, notes, ideas, and reminders.

Unlike task_agent.py this agent never saves tasks. It only fetches data and
formats a reply. Called when the classifier returns type="query".
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from config import settings
from db.client import get_recent_tasks, get_upcoming_reminders, search_tasks

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

class _ListTasksInput(BaseModel):
    type: str = Field(
        default="",
        description=(
            "Filter by task type: idea, todo, note, reminder, shopping, learning, "
            "architecture, question. Empty string = all types."
        ),
    )
    status: str = Field(
        default="pending",
        description="Filter by status: pending, done, all. Default: pending.",
    )
    limit: int = Field(default=10, description="Max results to return (1–20).")


class _SearchTasksInput(BaseModel):
    query: str = Field(description="Keyword(s) to search for in task text.")
    limit: int = Field(default=10, description="Max results to return (1–20).")


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

_TYPE_EMOJI = {
    "idea": "💡", "todo": "📋", "note": "📝", "learning": "🧠",
    "architecture": "🏗️", "question": "❓", "shopping": "🛒", "reminder": "⏰",
}
_STATUS_EMOJI = {"pending": "⏳", "done": "✅", "error": "❌", "processing": "🔄"}


def _format_task(t: Any, user_tz: str = "UTC") -> str:
    emoji = _TYPE_EMOJI.get(t.type, "•")
    status = _STATUS_EMOJI.get(t.status, "")
    text = t.text[:120] + ("…" if len(t.text) > 120 else "")
    line = f"{emoji}{status} #{t.id} — {text}"
    if t.due_date and t.due_time:
        time_str = f"{t.due_date} {t.due_time} UTC"
        if user_tz != "UTC":
            try:
                from zoneinfo import ZoneInfo
                dt_utc = datetime.strptime(
                    f"{t.due_date} {t.due_time}", "%Y-%m-%d %H:%M"
                ).replace(tzinfo=ZoneInfo("UTC"))
                dt_local = dt_utc.astimezone(ZoneInfo(user_tz))
                time_str = f"{dt_local.strftime('%Y-%m-%d %H:%M')} {dt_local.strftime('%Z')}"
            except Exception:
                pass
        line += f"\n   ⏰ {time_str}"
    return line


async def _execute_tool(name: str, args: dict, user_tz: str) -> str:
    if name == "list_tasks":
        task_type = args.get("type") or None
        raw_status = args.get("status", "pending")
        status = None if raw_status == "all" else raw_status
        limit = min(int(args.get("limit", 10)), 20)
        try:
            from db.client import get_recent_tasks as _get_tasks
            import httpx
            from config import settings as cfg
            async with httpx.AsyncClient(
                base_url=cfg.data_api_url,
                headers={"X-API-Key": cfg.data_api_key},
                timeout=15.0,
            ) as client:
                params: dict = {"limit": limit}
                if task_type:
                    params["type"] = task_type
                if status:
                    params["status"] = status
                r = await client.get("/tasks", params=params)
                r.raise_for_status()
                from db.client import _to_task
                tasks = [_to_task(d) for d in r.json()]
        except Exception as e:
            return f"Error fetching tasks: {e}"
        if not tasks:
            return "No tasks found."
        lines = [_format_task(t, user_tz) for t in tasks]
        return "\n".join(lines)

    if name == "search_tasks":
        query = args.get("query", "")
        limit = min(int(args.get("limit", 10)), 20)
        if not query:
            return "No search query provided."
        try:
            tasks = await search_tasks(query, limit)
        except Exception as e:
            return f"Error searching tasks: {e}"
        if not tasks:
            return f"No tasks found matching '{query}'."
        lines = [_format_task(t, user_tz) for t in tasks]
        return f"Found {len(tasks)} result(s) for '{query}':\n" + "\n".join(lines)

    return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# LLM factory (same as task_agent)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

_SYSTEM = """You are a personal assistant. The user is asking a question about their saved tasks, notes, ideas, or reminders.

Use the available tools to fetch the relevant data, then write a clear, concise reply.

Guidelines:
- Use list_tasks to get tasks by type/status (e.g. all pending ideas, done reminders).
- Use search_tasks to find tasks matching keywords the user mentioned.
- You may call multiple tools if needed (e.g. search + list).
- After fetching data, reply in the SAME LANGUAGE the user wrote in.
- Format lists as clean bullet points — no raw JSON, no task IDs unless helpful.
- For reminders show the date and time in the user's local timezone.
- If nothing is found, say so clearly.
- Keep replies concise — if there are many results, summarise.

Today: {today}. User timezone: {user_tz}.

Recent conversation (for context):
{context}"""

_QUERY_TOOL_NAMES = {"list_tasks", "search_tasks"}


async def run_query(
    question: str,
    recent_messages: list[dict],
    user_tz: str = "UTC",
) -> str:
    """
    Run the query agent. Returns a formatted reply string.
    Does NOT save any tasks or messages — purely read-only.
    """
    from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage
    from langchain_core.tools import StructuredTool

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    context_str = "\n".join(
        f"{m['role']}: {m['content'][:200]}" for m in recent_messages[-20:]
    )

    def _noop(**kwargs: Any) -> str:
        return ""

    query_tools = [
        StructuredTool.from_function(
            func=_noop,
            name="list_tasks",
            args_schema=_ListTasksInput,
            description=(
                "Fetch tasks from the database filtered by type and/or status. "
                "Use this to list all ideas, pending todos, done reminders, etc."
            ),
        ),
        StructuredTool.from_function(
            func=_noop,
            name="search_tasks",
            args_schema=_SearchTasksInput,
            description=(
                "Search task text for specific keywords. "
                "Use when the user asks about a specific topic or mentions keywords."
            ),
        ),
    ]

    # Optionally add MCP memory tools
    mcp_tools: list = []
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
                mcp_tools = mcp_client.get_tools()
        except Exception as e:
            logger.debug("MCP tools unavailable for query agent: %s", e)

    mcp_tool_map = {t.name: t for t in mcp_tools}
    all_tools = query_tools + mcp_tools

    llm = _get_llm().bind_tools(all_tools)

    messages: list = [
        SystemMessage(
            content=_SYSTEM.format(today=today, user_tz=user_tz, context=context_str)
        ),
        HumanMessage(content=question),
    ]

    for _ in range(6):
        response = await llm.ainvoke(messages)

        if not response.tool_calls:
            content = response.content
            if isinstance(content, list):
                content = "".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in content
                )
            return content or "No results found."

        messages.append(response)

        for tool_call in response.tool_calls:
            name = tool_call["name"]
            args = tool_call["args"]
            call_id = tool_call["id"]

            if name in mcp_tool_map:
                try:
                    result = await mcp_tool_map[name].ainvoke(args)
                    result_text = str(result)
                except Exception as e:
                    result_text = f"Memory tool error: {e}"
            elif name in _QUERY_TOOL_NAMES:
                result_text = await _execute_tool(name, args, user_tz)
            else:
                result_text = f"Unknown tool: {name}"

            messages.append(ToolMessage(content=result_text, tool_call_id=call_id))

    return "Sorry, I couldn't retrieve that information."
