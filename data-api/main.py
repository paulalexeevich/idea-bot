import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel

from database import (
    db_create_task,
    db_get_discovery,
    db_get_due_reminders,
    db_get_newly_done_tasks,
    db_get_offers,
    db_get_recent_messages,
    db_get_setting,
    db_get_task,
    db_get_task_counts,
    db_get_tasks,
    db_get_unprocessed_messages,
    db_mark_completion_notified,
    db_mark_messages_processed,
    db_mark_notified,
    db_save_discovery,
    db_save_message,
    db_save_offer,
    db_search_tasks,
    db_set_setting,
    db_set_task_status,
    db_set_task_type,
    db_update_task_deadline,
    db_update_task_reminder,
    init_db,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_KEY = os.environ.get("DATA_API_KEY", "")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)


async def verify_key(key: str = Security(api_key_header)) -> None:
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Database initialized.")
    yield


app = FastAPI(title="Task Data API", lifespan=lifespan)


# --- Request / Response models ---

class TaskCreate(BaseModel):
    text: str
    type: str = "idea"


class StatusUpdate(BaseModel):
    status: str


class TypeUpdate(BaseModel):
    type: str


class SettingUpdate(BaseModel):
    value: str


class DeadlineUpdate(BaseModel):
    deadline: str | None = None   # ISO date string or null
    urgency: str | None = None    # asap | fast | week | flexible | any


class ReminderUpdate(BaseModel):
    due_date: str | None = None   # ISO date: YYYY-MM-DD
    due_time: str | None = None   # HH:MM (24h)


class OfferCreate(BaseModel):
    title: str
    price: str | None = None
    store: str | None = None
    url: str
    snippet: str | None = None
    location_context: str | None = None
    delivery_days_estimate: int | None = None


class MessageCreate(BaseModel):
    role: str    # "user" or "bot"
    content: str


class MessagesProcessed(BaseModel):
    ids: list[int]


class DiscoveryCreate(BaseModel):
    reddit_summary: str | None = None
    hn_summary: str | None = None
    ph_summary: str | None = None
    ih_summary: str | None = None
    verdict: str | None = None
    score: float | None = None
    market_size: str | None = None
    full_report: dict | None = None


# --- Helpers ---

def _parse_full_report(row: dict) -> dict:
    if row.get("full_report") and isinstance(row["full_report"], str):
        try:
            row["full_report"] = json.loads(row["full_report"])
        except Exception:
            pass
    return row


# --- Routes ---

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/tasks", dependencies=[Depends(verify_key)])
async def create_task(body: TaskCreate):
    task_id = await db_create_task(body.text, body.type)
    return {"id": task_id}


@app.get("/tasks", dependencies=[Depends(verify_key)])
async def list_tasks(status: str | None = None, type: str | None = None, limit: int = 50):
    tasks = await db_get_tasks(status=status, type=type, limit=limit)
    return tasks


@app.get("/tasks/search", dependencies=[Depends(verify_key)])
async def search_tasks(q: str, limit: int = 20):
    """Keyword search in task text. q is matched case-insensitively."""
    return await db_search_tasks(q, limit)


@app.get("/tasks/done/new", dependencies=[Depends(verify_key)])
async def get_newly_done():
    return await db_get_newly_done_tasks()


@app.get("/tasks/{task_id}", dependencies=[Depends(verify_key)])
async def get_task(task_id: int):
    task = await db_get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    discovery = await db_get_discovery(task_id)
    return {**task, "discovery": _parse_full_report(discovery) if discovery else None}


@app.patch("/tasks/{task_id}/status", dependencies=[Depends(verify_key)])
async def update_status(task_id: int, body: StatusUpdate):
    task = await db_get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await db_set_task_status(task_id, body.status)
    return {"ok": True}


@app.patch("/tasks/{task_id}/type", dependencies=[Depends(verify_key)])
async def update_type(task_id: int, body: TypeUpdate):
    task = await db_get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await db_set_task_type(task_id, body.type)
    return {"ok": True}


@app.post("/tasks/{task_id}/discovery", dependencies=[Depends(verify_key)])
async def save_discovery(task_id: int, body: DiscoveryCreate):
    task = await db_get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    discovery_id = await db_save_discovery(
        task_id=task_id,
        reddit_summary=body.reddit_summary,
        hn_summary=body.hn_summary,
        ph_summary=body.ph_summary,
        ih_summary=body.ih_summary,
        verdict=body.verdict,
        score=body.score,
        market_size=body.market_size,
        full_report=body.full_report,
    )
    return {"id": discovery_id}


@app.get("/tasks/{task_id}/offers", dependencies=[Depends(verify_key)])
async def get_offers(task_id: int):
    return await db_get_offers(task_id)


@app.patch("/tasks/{task_id}/deadline", dependencies=[Depends(verify_key)])
async def update_deadline(task_id: int, body: DeadlineUpdate):
    task = await db_get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await db_update_task_deadline(task_id, body.deadline, body.urgency)
    return {"ok": True}


@app.post("/tasks/{task_id}/offers", dependencies=[Depends(verify_key)])
async def save_offer(task_id: int, body: OfferCreate):
    task = await db_get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    offer_id = await db_save_offer(task_id, body.title, body.price, body.store, body.url, body.snippet, body.location_context, body.delivery_days_estimate)
    return {"id": offer_id}


@app.get("/tasks/{task_id}/discovery", dependencies=[Depends(verify_key)])
async def get_discovery(task_id: int):
    discovery = await db_get_discovery(task_id)
    if not discovery:
        raise HTTPException(status_code=404, detail="No discovery for this task")
    return _parse_full_report(discovery)


@app.patch("/tasks/{task_id}/reminder", dependencies=[Depends(verify_key)])
async def update_reminder(task_id: int, body: ReminderUpdate):
    task = await db_get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await db_update_task_reminder(task_id, body.due_date, body.due_time)
    return {"ok": True}


@app.get("/reminders/due", dependencies=[Depends(verify_key)])
async def get_due_reminders(now: str):
    """now = ISO datetime string YYYY-MM-DDTHH:MM passed by caller."""
    return await db_get_due_reminders(now)


@app.post("/tasks/{task_id}/notified", dependencies=[Depends(verify_key)])
async def mark_notified(task_id: int):
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    await db_mark_notified(task_id, now_iso)
    return {"ok": True}


@app.post("/tasks/{task_id}/completion-notified", dependencies=[Depends(verify_key)])
async def mark_completion_notified(task_id: int):
    await db_mark_completion_notified(task_id)
    return {"ok": True}


@app.get("/settings/{key}", dependencies=[Depends(verify_key)])
async def get_setting(key: str):
    value = await db_get_setting(key)
    if value is None:
        raise HTTPException(status_code=404, detail="Setting not found")
    return {"key": key, "value": value}


@app.put("/settings/{key}", dependencies=[Depends(verify_key)])
async def set_setting(key: str, body: SettingUpdate):
    await db_set_setting(key, body.value)
    return {"ok": True}


@app.get("/counts", dependencies=[Depends(verify_key)])
async def task_counts():
    return await db_get_task_counts()


# ---------------------------------------------------------------------------
# Messages — conversation history
# ---------------------------------------------------------------------------

@app.post("/messages", dependencies=[Depends(verify_key)])
async def save_message(body: MessageCreate):
    msg_id = await db_save_message(body.role, body.content)
    return {"id": msg_id}


@app.get("/messages/recent", dependencies=[Depends(verify_key)])
async def get_recent_messages(limit: int = 20):
    return await db_get_recent_messages(limit)


@app.get("/messages/unprocessed", dependencies=[Depends(verify_key)])
async def get_unprocessed_messages(limit: int = 50):
    return await db_get_unprocessed_messages(limit)


@app.post("/messages/processed", dependencies=[Depends(verify_key)])
async def mark_messages_processed(body: MessagesProcessed):
    await db_mark_messages_processed(body.ids)
    return {"ok": True}
