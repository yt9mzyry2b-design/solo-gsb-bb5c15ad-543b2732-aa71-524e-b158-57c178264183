"""SQLite 任务存储（标准库 sqlite3，零额外服务/账号）。"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

_DB_PATH = os.environ.get(
    "TILE_SEAM_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tasks.db"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    request_id    TEXT NOT NULL,
    status        TEXT NOT NULL,
    stitchable    INTEGER NOT NULL,
    params        TEXT NOT NULL,
    result        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_request ON tasks(request_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or _DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # 防御性建表：即使未经过 startup 钩子直接调用也能工作（幂等）
    conn.executescript(_SCHEMA)
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    """首次启动自动建库（幂等）。"""
    with connect(db_path) as conn:
        conn.executescript(_SCHEMA)


def _row_to_summary(row: sqlite3.Row) -> dict[str, Any]:
    params = json.loads(row["params"])
    # 列表摘要不回显完整瓦片列表（详情接口才返回）
    params_summary = {k: v for k, v in params.items() if k != "tiles"}
    params_summary["tile_count"] = len(params.get("tiles", []))
    return {
        "id": row["id"],
        "request_id": row["request_id"],
        "status": row["status"],
        "stitchable": bool(row["stitchable"]),
        "params": params_summary,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_task(task_id: str, result: dict[str, Any], params: dict[str, Any],
                db_path: Optional[str] = None) -> None:
    now = _now()
    with connect(db_path) as conn:
        conn.execute(
            """INSERT INTO tasks(id, request_id, status, stitchable, params,
                                 result, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                result["request_id"],
                result["status"],
                1 if result["stitchable"] else 0,
                json.dumps(params, sort_keys=True, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                now,
                now,
            ),
        )


def get_task(task_id: str, db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    summary = _row_to_summary(row)
    summary["result"] = json.loads(row["result"])
    return summary


def list_tasks(limit: int = 50, offset: int = 0, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC, id ASC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [_row_to_summary(r) for r in rows]


def delete_task(task_id: str, db_path: Optional[str] = None) -> bool:
    with connect(db_path) as conn:
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cur.rowcount > 0


def count_tasks(db_path: Optional[str] = None) -> int:
    with connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
