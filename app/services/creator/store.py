from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def data_root() -> Path:
    root = Path(os.environ.get("MPT_CREATOR_DATA") or Path(__file__).resolve().parents[3] / "storage" / "creator")
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def new_id() -> str:
    return uuid.uuid4().hex


@contextmanager
def connection():
    conn = sqlite3.connect(data_root() / "creator.sqlite3", timeout=20)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY(kind,id))")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_record(kind: str, ident: str, data: dict) -> dict:
    result = dict(data, id=ident)
    now = datetime.now(timezone.utc).isoformat()
    result.setdefault("created_at", now)
    result["updated_at"] = now
    with connection() as conn:
        conn.execute("INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data,updated=excluded.updated", (kind, ident, json.dumps(result, ensure_ascii=False, default=str), now))
    return result


def get_record(kind: str, ident: str) -> dict | None:
    with connection() as conn:
        row = conn.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, ident)).fetchone()
    return json.loads(row[0]) if row else None


def list_records(kind: str) -> list[dict]:
    with connection() as conn:
        rows = conn.execute("SELECT data FROM records WHERE kind=? ORDER BY updated DESC", (kind,)).fetchall()
    return [json.loads(row[0]) for row in rows]


def delete_record(kind: str, ident: str):
    with connection() as conn:
        conn.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, ident))


def update_record(kind: str, ident: str, changes: dict) -> dict:
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, ident)).fetchone()
        if row is None:
            raise KeyError(ident)
        result = json.loads(row[0])
        result.update(changes)
        result["updated_at"] = datetime.now(timezone.utc).isoformat()
        conn.execute("UPDATE records SET data=?,updated=? WHERE kind=? AND id=?", (json.dumps(result, ensure_ascii=False, default=str), result["updated_at"], kind, ident))
    return result
