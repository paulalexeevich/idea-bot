import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator

import aiosqlite

DB_PATH = os.environ.get("DB_PATH", "./data/tasks.db")


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                text       TEXT NOT NULL,
                type       TEXT NOT NULL DEFAULT 'idea',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status     TEXT DEFAULT 'pending'
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS discoveries (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id        INTEGER NOT NULL REFERENCES tasks(id),
                ran_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reddit_summary TEXT,
                hn_summary     TEXT,
                ph_summary     TEXT,
                ih_summary     TEXT,
                verdict        TEXT,
                score          REAL,
                market_size    TEXT,
                full_report    TEXT
            )
        """)

        # Migrate from old schema: ideas → tasks
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ideas'"
        ) as cur:
            if await cur.fetchone():
                await db.execute("""
                    INSERT OR IGNORE INTO tasks (id, text, type, created_at, status)
                    SELECT id, text, 'idea', created_at, status FROM ideas
                """)
                await db.execute(
                    "INSERT OR REPLACE INTO sqlite_sequence (name, seq) "
                    "SELECT 'tasks', MAX(id) FROM tasks"
                )
                await db.execute("DROP TABLE ideas")

        # Migrate discoveries: idea_id → task_id (drop if old schema, no data yet)
        async with db.execute("PRAGMA table_info(discoveries)") as cur:
            cols = [row[1] for row in await cur.fetchall()]
        if cols and "idea_id" in cols:
            await db.execute("DROP TABLE discoveries")
            await db.execute("""
                CREATE TABLE discoveries (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id        INTEGER NOT NULL REFERENCES tasks(id),
                    ran_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    reddit_summary TEXT,
                    hn_summary     TEXT,
                    ph_summary     TEXT,
                    ih_summary     TEXT,
                    verdict        TEXT,
                    score          REAL,
                    market_size    TEXT,
                    full_report    TEXT
                )
            """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS offers (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id          INTEGER NOT NULL REFERENCES tasks(id),
                title            TEXT NOT NULL,
                price            TEXT,
                store            TEXT,
                url              TEXT NOT NULL,
                snippet          TEXT,
                location_context TEXT,
                found_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Migrate tasks: add deadline + urgency columns if missing
        async with db.execute("PRAGMA table_info(tasks)") as cur:
            task_cols = [row[1] for row in await cur.fetchall()]
        if task_cols and "deadline" not in task_cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN deadline TEXT")
        if task_cols and "urgency" not in task_cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN urgency TEXT")
        if task_cols and "due_date" not in task_cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN due_date TEXT")
        if task_cols and "due_time" not in task_cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN due_time TEXT")
        if task_cols and "notified_at" not in task_cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN notified_at TEXT")
        if task_cols and "completed_notified" not in task_cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN completed_notified INTEGER DEFAULT 0")
            # Silence existing done tasks so they don't trigger notifications
            await db.execute("UPDATE tasks SET completed_notified = 1 WHERE status = 'done'")

        # Migrate offers table: add missing columns
        async with db.execute("PRAGMA table_info(offers)") as cur:
            offer_cols = [row[1] for row in await cur.fetchall()]
        if offer_cols and "location_context" not in offer_cols:
            await db.execute("ALTER TABLE offers ADD COLUMN location_context TEXT")
        if offer_cols and "delivery_days_estimate" not in offer_cols:
            await db.execute("ALTER TABLE offers ADD COLUMN delivery_days_estimate INTEGER")

        # Messages table — every Telegram exchange saved here
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed  INTEGER DEFAULT 0
            )
        """)

        await db.commit()


@asynccontextmanager
async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        yield db


async def db_create_task(text: str, type: str) -> int:
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO tasks (text, type) VALUES (?, ?)", (text, type)
        )
        await db.commit()
        return cursor.lastrowid


async def db_get_tasks(status: str | None = None, type: str | None = None, limit: int = 50) -> list[dict]:
    filters, params = [], []
    if status:
        filters.append("status = ?")
        params.append(status)
    if type:
        filters.append("type = ?")
        params.append(type)
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    params.append(limit)
    async with get_db() as db:
        async with db.execute(
            f"SELECT * FROM tasks {where} ORDER BY id DESC LIMIT ?", params
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def db_get_task(task_id: int) -> dict | None:
    async with get_db() as db:
        async with db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def db_set_task_status(task_id: int, status: str) -> None:
    async with get_db() as db:
        await db.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
        await db.commit()


async def db_set_task_type(task_id: int, type: str) -> None:
    async with get_db() as db:
        await db.execute("UPDATE tasks SET type = ? WHERE id = ?", (type, task_id))
        await db.commit()


async def db_get_setting(key: str) -> str | None:
    async with get_db() as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
    return row["value"] if row else None


async def db_set_setting(key: str, value: str) -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()


async def db_update_task_deadline(task_id: int, deadline: str | None, urgency: str | None) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE tasks SET deadline = ?, urgency = ? WHERE id = ?",
            (deadline, urgency, task_id),
        )
        await db.commit()


async def db_save_offer(task_id: int, title: str, price: str | None, store: str | None, url: str, snippet: str | None, location_context: str | None = None, delivery_days_estimate: int | None = None) -> int:
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO offers (task_id, title, price, store, url, snippet, location_context, delivery_days_estimate) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, title, price, store, url, snippet, location_context, delivery_days_estimate),
        )
        await db.commit()
        return cursor.lastrowid


async def db_get_offers(task_id: int) -> list[dict]:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM offers WHERE task_id = ? ORDER BY id ASC", (task_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def db_save_discovery(
    task_id: int,
    reddit_summary: str | None,
    hn_summary: str | None,
    ph_summary: str | None,
    ih_summary: str | None,
    verdict: str | None,
    score: float | None,
    market_size: str | None,
    full_report: dict | None,
) -> int:
    async with get_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO discoveries
                (task_id, reddit_summary, hn_summary, ph_summary, ih_summary,
                 verdict, score, market_size, full_report)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id, reddit_summary, hn_summary, ph_summary, ih_summary,
                verdict, score, market_size,
                json.dumps(full_report) if full_report else None,
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def db_get_discovery(task_id: int) -> dict | None:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM discoveries WHERE task_id = ? ORDER BY ran_at DESC LIMIT 1",
            (task_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def db_get_task_counts() -> dict[str, int]:
    async with get_db() as db:
        async with db.execute("SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status") as cur:
            rows = await cur.fetchall()
    return {r["status"]: r["cnt"] for r in rows}


async def db_update_task_reminder(task_id: int, due_date: str | None, due_time: str | None) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE tasks SET due_date = ?, due_time = ? WHERE id = ?",
            (due_date, due_time, task_id),
        )
        await db.commit()


async def db_get_due_reminders(now_iso: str) -> list[dict]:
    """Return reminder tasks whose due_date+due_time <= now and not yet notified."""
    async with get_db() as db:
        async with db.execute(
            """
            SELECT * FROM tasks
            WHERE type = 'reminder'
              AND status != 'done'
              AND notified_at IS NULL
              AND due_date IS NOT NULL
              AND due_time IS NOT NULL
              AND (due_date || 'T' || due_time) <= ?
            ORDER BY due_date ASC, due_time ASC
            """,
            (now_iso,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def db_mark_notified(task_id: int, notified_at: str) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE tasks SET notified_at = ?, status = 'done' WHERE id = ?",
            (notified_at, task_id),
        )
        await db.commit()


async def db_search_tasks(query: str, limit: int = 20) -> list[dict]:
    """Keyword search across task text (case-insensitive LIKE)."""
    pattern = f"%{query}%"
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM tasks WHERE text LIKE ? ORDER BY id DESC LIMIT ?",
            (pattern, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def db_get_newly_done_tasks() -> list[dict]:
    """Tasks recently marked done but not yet notified (excludes reminder type)."""
    async with get_db() as db:
        async with db.execute(
            """
            SELECT * FROM tasks
            WHERE status = 'done'
              AND (completed_notified IS NULL OR completed_notified = 0)
              AND type != 'reminder'
            ORDER BY id ASC
            """,
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def db_mark_completion_notified(task_id: int) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE tasks SET completed_notified = 1 WHERE id = ?", (task_id,)
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Messages (conversation history for short-term + long-term memory)
# ---------------------------------------------------------------------------

async def db_save_message(role: str, content: str) -> int:
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO messages (role, content) VALUES (?, ?)", (role, content)
        )
        await db.commit()
        return cursor.lastrowid


async def db_get_recent_messages(limit: int = 20) -> list[dict]:
    async with get_db() as db:
        async with db.execute(
            "SELECT id, role, content, created_at FROM messages ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    return list(reversed([dict(r) for r in rows]))  # chronological order


async def db_get_unprocessed_messages(limit: int = 50) -> list[dict]:
    async with get_db() as db:
        async with db.execute(
            "SELECT id, role, content, created_at FROM messages WHERE processed = 0 ORDER BY id ASC LIMIT ?",
            (limit,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def db_mark_messages_processed(ids: list[int]) -> None:
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    async with get_db() as db:
        await db.execute(
            f"UPDATE messages SET processed = 1 WHERE id IN ({placeholders})", ids
        )
        await db.commit()
