"""HTTP client for the data-api service. All DB access goes through here."""
from datetime import datetime

import httpx

from config import settings
from db.models import Discovery, Task

_client: httpx.AsyncClient | None = None


def _get() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=settings.data_api_url,
            headers={"X-API-Key": settings.data_api_key},
            timeout=30.0,
        )
    return _client


def _to_task(d: dict) -> Task:
    return Task(
        id=d["id"],
        text=d["text"],
        type=d["type"],
        created_at=datetime.fromisoformat(d["created_at"]),
        status=d["status"],
        deadline=d.get("deadline"),
        urgency=d.get("urgency"),
        due_date=d.get("due_date"),
        due_time=d.get("due_time"),
        notified_at=d.get("notified_at"),
        completed_notified=d.get("completed_notified", 0),
    )


def _to_discovery(d: dict) -> Discovery:
    return Discovery(
        id=d["id"],
        task_id=d["task_id"],
        ran_at=datetime.fromisoformat(d["ran_at"]),
        reddit_summary=d.get("reddit_summary"),
        hn_summary=d.get("hn_summary"),
        ph_summary=d.get("ph_summary"),
        ih_summary=d.get("ih_summary"),
        verdict=d.get("verdict"),
        score=d.get("score"),
        market_size=d.get("market_size"),
        full_report=d.get("full_report"),
    )


async def create_task(text: str, type: str = "idea") -> int:
    r = await _get().post("/tasks", json={"text": text, "type": type})
    r.raise_for_status()
    return r.json()["id"]


async def get_recent_tasks(limit: int = 10) -> list[Task]:
    r = await _get().get("/tasks", params={"limit": limit})
    r.raise_for_status()
    return [_to_task(d) for d in r.json()]


async def get_upcoming_reminders(limit: int = 10) -> list[Task]:
    r = await _get().get("/tasks", params={"type": "reminder", "status": "pending", "limit": limit})
    r.raise_for_status()
    return [_to_task(d) for d in r.json()]


async def get_pending_tasks(type: str | None = None) -> list[Task]:
    params: dict = {"status": "pending"}
    if type:
        params["type"] = type
    r = await _get().get("/tasks", params=params)
    r.raise_for_status()
    return [_to_task(d) for d in r.json()]


async def get_task_by_id(task_id: int) -> Task | None:
    r = await _get().get(f"/tasks/{task_id}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return _to_task(r.json())


async def set_task_status(task_id: int, status: str) -> None:
    r = await _get().patch(f"/tasks/{task_id}/status", json={"status": status})
    r.raise_for_status()


async def set_task_type(task_id: int, type: str) -> None:
    r = await _get().patch(f"/tasks/{task_id}/type", json={"type": type})
    r.raise_for_status()


async def save_discovery(
    task_id: int,
    reddit_summary: str | None,
    hn_summary: str | None,
    ph_summary: str | None,
    ih_summary: str | None,
    verdict: str | None,
    score: float | None,
    market_size: str | None,
    full_report: dict | None,
) -> None:
    r = await _get().post(f"/tasks/{task_id}/discovery", json={
        "reddit_summary": reddit_summary,
        "hn_summary": hn_summary,
        "ph_summary": ph_summary,
        "ih_summary": ih_summary,
        "verdict": verdict,
        "score": score,
        "market_size": market_size,
        "full_report": full_report,
    })
    r.raise_for_status()


async def get_discovery_for_task(task_id: int) -> Discovery | None:
    r = await _get().get(f"/tasks/{task_id}/discovery")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return _to_discovery(r.json())


async def get_setting(key: str) -> str | None:
    r = await _get().get(f"/settings/{key}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()["value"]


async def set_setting(key: str, value: str) -> None:
    r = await _get().put(f"/settings/{key}", json={"value": value})
    r.raise_for_status()


async def update_task_deadline(task_id: int, deadline: str | None, urgency: str | None) -> None:
    r = await _get().patch(f"/tasks/{task_id}/deadline", json={"deadline": deadline, "urgency": urgency})
    r.raise_for_status()


async def save_offer(task_id: int, title: str, price: str | None, store: str | None, url: str, snippet: str | None = None, location_context: str | None = None, delivery_days_estimate: int | None = None) -> None:
    r = await _get().post(f"/tasks/{task_id}/offers", json={
        "title": title, "price": price, "store": store, "url": url,
        "snippet": snippet, "location_context": location_context,
        "delivery_days_estimate": delivery_days_estimate,
    })
    r.raise_for_status()


async def get_offers(task_id: int) -> list[dict]:
    r = await _get().get(f"/tasks/{task_id}/offers")
    r.raise_for_status()
    return r.json()


async def get_task_counts() -> dict[str, int]:
    r = await _get().get("/counts")
    r.raise_for_status()
    return r.json()


async def update_task_reminder(task_id: int, due_date: str | None, due_time: str | None) -> None:
    r = await _get().patch(f"/tasks/{task_id}/reminder", json={"due_date": due_date, "due_time": due_time})
    r.raise_for_status()


async def get_due_reminders(now_iso: str) -> list[dict]:
    r = await _get().get("/reminders/due", params={"now": now_iso})
    r.raise_for_status()
    return r.json()


async def mark_task_notified(task_id: int) -> None:
    r = await _get().post(f"/tasks/{task_id}/notified")
    r.raise_for_status()


async def get_newly_done_tasks() -> list[dict]:
    r = await _get().get("/tasks/done/new")
    r.raise_for_status()
    return r.json()


async def mark_completion_notified(task_id: int) -> None:
    r = await _get().post(f"/tasks/{task_id}/completion-notified")
    r.raise_for_status()


# ---------------------------------------------------------------------------
# Messages — conversation history
# ---------------------------------------------------------------------------

async def save_message(role: str, content: str) -> None:
    """Fire-and-forget: save a Telegram exchange. Errors are non-fatal."""
    try:
        r = await _get().post("/messages", json={"role": role, "content": content})
        r.raise_for_status()
    except Exception:
        pass  # message logging must never break the bot


async def get_recent_messages(limit: int = 20) -> list[dict]:
    """Return last N messages in chronological order [{role, content, created_at}]."""
    r = await _get().get("/messages/recent", params={"limit": limit})
    r.raise_for_status()
    return r.json()
