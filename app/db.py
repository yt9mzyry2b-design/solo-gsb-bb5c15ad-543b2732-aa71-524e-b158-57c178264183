"""SQLite 任务持久层（标准库实现，无第三方依赖）。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

_lock = threading.Lock()
_initialized = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id    TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    job_name   TEXT,
    tolerance  REAL NOT NULL,
    tile_count INTEGER NOT NULL,
    status     TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    input_json TEXT NOT NULL,
    result_json TEXT NOT NULL
);
"""


def db_path() -> str:
    return os.environ.get("DIAG_DB_PATH",
                          os.path.join("data", "diagnostics.db"))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> str:
    """建库建表（幂等），返回数据库文件路径。"""
    global _initialized
    path = db_path()
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with _lock, _connect() as conn:
        conn.executescript(_SCHEMA)
    _initialized = True
    return path


def _ensure_db() -> None:
    """惰性初始化：即使跳过应用启动钩子（如测试客户端）也能正常工作。"""
    if not _initialized:
        init_db()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def save_task(*, task_id: str, created_at: str, job_name: str | None,
              tolerance: float, tile_count: int, status: str,
              checksum: str, input_hash: str,
              input_payload: dict[str, Any],
              result: dict[str, Any]) -> None:
    _ensure_db()
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO tasks (task_id, created_at, job_name, tolerance,"
            " tile_count, status, checksum, input_hash, input_json, result_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, created_at, job_name, tolerance, tile_count, status,
             checksum, input_hash,
             json.dumps(input_payload, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False)),
        )


def get_task(task_id: str) -> dict[str, Any] | None:
    _ensure_db()
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?",
                           (task_id,)).fetchone()
    return dict(row) if row else None


def list_tasks(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    _ensure_db()
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT task_id, created_at, job_name, tolerance, tile_count,"
            " status, checksum, input_hash, result_json"
            " FROM tasks ORDER BY created_at DESC, task_id DESC"
            " LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        result = json.loads(item.pop("result_json"))
        item["summary"] = result.get("summary", {})
        items.append(item)
    return items
