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

        # Migrate offers table: add location_context column if missing
        async with db.execute("PRAGMA table_info(offers)") as cur:
            offer_cols = [row[1] for row in await cur.fetchall()]
        if offer_cols and "location_context" not in offer_cols:
            await db.execute("ALTER TABLE offers ADD COLUMN location_context TEXT")

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


async def db_save_offer(task_id: int, title: str, price: str | None, store: str | None, url: str, snippet: str | None, location_context: str | None = None) -> int:
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO offers (task_id, title, price, store, url, snippet, location_context) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, title, price, store, url, snippet, location_context),
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
