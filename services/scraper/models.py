"""SQLite-backed job store for the scraper service."""

import json
import uuid
from datetime import datetime, timezone

import aiosqlite

DB_PATH = "/data/scraper.db"

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    label TEXT NOT NULL DEFAULT '',
    urls TEXT NOT NULL DEFAULT '[]',
    selectors TEXT NOT NULL DEFAULT '{}',
    output_schema TEXT NOT NULL DEFAULT '{}',
    follow_links INTEGER NOT NULL DEFAULT 0,
    max_pages INTEGER NOT NULL DEFAULT 50,
    delay REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    progress TEXT NOT NULL DEFAULT '{}',
    output_file TEXT NOT NULL DEFAULT '',
    error TEXT
)
"""


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def init_db() -> None:
    db = await get_db()
    try:
        await db.execute(CREATE_TABLE)
        await db.commit()
    finally:
        await db.close()


async def create_job(
    label: str,
    urls: list[str],
    selectors: dict,
    output_schema: dict | None = None,
    follow_links: bool = False,
    max_pages: int = 50,
    delay: float = 1.0,
    output_file: str = "",
) -> dict:
    job_id = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).isoformat()
    if not output_file:
        slug = label.lower().replace(" ", "-")[:40] if label else job_id
        output_file = f"{now[:10]}_{slug}.jsonl"

    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO jobs (id, label, urls, selectors, output_schema, follow_links, max_pages, delay, created_at, output_file) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, label, json.dumps(urls), json.dumps(selectors), json.dumps(output_schema or {}), int(follow_links), max_pages, delay, now, output_file),
        )
        await db.commit()
    finally:
        await db.close()

    return {"id": job_id, "status": "pending", "output_file": output_file}


async def get_job(job_id: str) -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        return _row_to_dict(row) if row else None
    finally:
        await db.close()


async def list_jobs() -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM jobs ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        await db.close()


async def update_job(job_id: str, **kwargs) -> None:
    sets = []
    vals = []
    for k, v in kwargs.items():
        if k in ("progress", "urls", "selectors", "output_schema"):
            v = json.dumps(v)
        sets.append(f"{k} = ?")
        vals.append(v)
    vals.append(job_id)
    db = await get_db()
    try:
        await db.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", vals)
        await db.commit()
    finally:
        await db.close()


async def delete_job(job_id: str) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


def _row_to_dict(row) -> dict:
    d = dict(row)
    for k in ("urls", "selectors", "output_schema", "progress"):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except (json.JSONDecodeError, TypeError):
                pass
    d["follow_links"] = bool(d.get("follow_links", 0))
    return d
