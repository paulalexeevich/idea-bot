"""Tests for db/client.py — the HTTP client that wraps the data-api."""
import pytest
import respx
import httpx
from datetime import datetime
from unittest.mock import patch, MagicMock

BASE = "http://data-api-test:8001"


@pytest.fixture(autouse=True)
def patch_api_url():
    mock_cfg = MagicMock()
    mock_cfg.data_api_url = BASE
    mock_cfg.data_api_key = "test-key"
    with patch("db.client._client", None), patch("db.client.settings", mock_cfg):
        yield


@pytest.mark.asyncio
@respx.mock
async def test_create_task_returns_id():
    respx.post(f"{BASE}/tasks").mock(return_value=httpx.Response(200, json={"id": 42}))
    from db.client import create_task
    assert await create_task("Build an AI recipe app", type="idea") == 42


@pytest.mark.asyncio
@respx.mock
async def test_get_recent_tasks():
    payload = [
        {"id": 1, "text": "Idea A", "type": "idea", "created_at": "2026-03-25T10:00:00", "status": "pending"},
        {"id": 2, "text": "Buy milk", "type": "shopping", "created_at": "2026-03-26T09:00:00", "status": "done"},
    ]
    respx.get(f"{BASE}/tasks").mock(return_value=httpx.Response(200, json=payload))
    from db.client import get_recent_tasks
    tasks = await get_recent_tasks(10)
    assert len(tasks) == 2
    assert tasks[0].type == "idea"
    assert tasks[1].status == "done"


@pytest.mark.asyncio
@respx.mock
async def test_get_task_by_id_not_found():
    respx.get(f"{BASE}/tasks/99").mock(return_value=httpx.Response(404, json={"detail": "Not found"}))
    from db.client import get_task_by_id
    assert await get_task_by_id(99) is None


@pytest.mark.asyncio
@respx.mock
async def test_get_due_reminders():
    payload = [
        {"id": 7, "text": "Call dentist", "type": "reminder", "created_at": "2026-05-14T08:00:00",
         "status": "pending", "due_date": "2026-05-15", "due_time": "09:00"},
    ]
    respx.get(f"{BASE}/reminders/due").mock(return_value=httpx.Response(200, json=payload))
    from db.client import get_due_reminders
    tasks = await get_due_reminders("2026-05-15T09:01")
    assert len(tasks) == 1
    assert tasks[0]["text"] == "Call dentist"


@pytest.mark.asyncio
@respx.mock
async def test_save_message():
    respx.post(f"{BASE}/messages").mock(return_value=httpx.Response(200, json={"id": 5}))
    from db.client import save_message
    await save_message("user", "hello")  # should not raise


@pytest.mark.asyncio
@respx.mock
async def test_get_recent_messages():
    payload = [
        {"id": 1, "role": "user", "content": "remind me tomorrow", "created_at": "2026-05-14T10:00:00"},
        {"id": 2, "role": "bot", "content": "Task #1 saved ✓", "created_at": "2026-05-14T10:00:01"},
    ]
    respx.get(f"{BASE}/messages/recent").mock(return_value=httpx.Response(200, json=payload))
    from db.client import get_recent_messages
    msgs = await get_recent_messages(20)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
