"""测绘瓦片接缝诊断 API。

端点一览：
- GET  /health                              健康检查
- POST /api/v1/tiles/validate               单瓦片元数据校验
- POST /api/v1/diagnostics                  提交瓦片集合并执行接缝诊断（入库）
- GET  /api/v1/diagnostics                  任务列表
- GET  /api/v1/diagnostics/{task_id}        查询诊断结果
- GET  /api/v1/diagnostics/{task_id}/export 导出诊断 JSON 文件
"""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import Response

from . import db
from .diagnostics import (input_fingerprint, normalize_tiles,
                          result_checksum, run_diagnostics)
from .schemas import (DiagnoseRequest, ValidateTileResponse,
                      validate_single_tile)


@asynccontextmanager
async def lifespan(_: FastAPI):
    path = db.init_db()  # 首次启动自动建库建表
    print(f"[startup] SQLite 数据库就绪: {path}")
    yield


app = FastAPI(
    title="测绘瓦片接缝诊断 API",
    version="1.0.0",
    description="接收无人机正射瓦片元数据，按容差做二维矩形扫描，"
                "识别覆盖缺口、重叠区域与无法拼接的连通分块，"
                "输出可复现的诊断 JSON。",
    lifespan=lifespan,
)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def index() -> dict[str, Any]:
    return {
        "service": "测绘瓦片接缝诊断 API",
        "version": app.version,
        "docs": "/docs",
        "endpoints": [
            "POST /api/v1/tiles/validate",
            "POST /api/v1/diagnostics",
            "GET /api/v1/diagnostics",
            "GET /api/v1/diagnostics/{task_id}",
            "GET /api/v1/diagnostics/{task_id}/export",
        ],
    }


@app.post("/api/v1/tiles/validate",
          response_model=ValidateTileResponse, tags=["tiles"])
def validate_tile(payload: Any = Body(...)) -> ValidateTileResponse:
    """校验单张瓦片元数据，返回结构化错误与告警（不产生 422）。"""
    return validate_single_tile(payload)


@app.post("/api/v1/diagnostics", status_code=201, tags=["diagnostics"])
def create_diagnostic(request: DiagnoseRequest) -> dict[str, Any]:
    """执行接缝诊断并持久化任务，返回完整诊断结果。"""
    raw_tiles = [t.model_dump() for t in request.tiles]
    tiles = normalize_tiles(raw_tiles)

    diagnostic = run_diagnostics(tiles, request.tolerance)
    diagnostic["input_hash"] = input_fingerprint(raw_tiles,
                                                 request.tolerance)
    checksum = result_checksum(diagnostic)

    task_id = uuid.uuid4().hex
    created_at = db.utc_now_iso()
    db.save_task(
        task_id=task_id,
        created_at=created_at,
        job_name=request.job_name,
        tolerance=request.tolerance,
        tile_count=len(tiles),
        status="completed",
        checksum=checksum,
        input_hash=diagnostic["input_hash"],
        input_payload=request.model_dump(),
        result=diagnostic,
    )

    return {
        "task_id": task_id,
        "created_at": created_at,
        "status": "completed",
        "job_name": request.job_name,
        "checksum": checksum,
        "diagnostic": diagnostic,
        "export_url": f"/api/v1/diagnostics/{task_id}/export",
    }


@app.get("/api/v1/diagnostics", tags=["diagnostics"])
def list_diagnostics(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """按创建时间倒序列出诊断任务（含摘要）。"""
    return {"items": db.list_tasks(limit=limit, offset=offset),
            "limit": limit, "offset": offset}


def _load_task_or_404(task_id: str) -> dict[str, Any]:
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404,
                            detail=f"任务不存在: {task_id}")
    return task


@app.get("/api/v1/diagnostics/{task_id}", tags=["diagnostics"])
def get_diagnostic(task_id: str) -> dict[str, Any]:
    """查询指定任务的完整诊断结果。"""
    task = _load_task_or_404(task_id)
    return {
        "task_id": task["task_id"],
        "created_at": task["created_at"],
        "status": task["status"],
        "job_name": task["job_name"],
        "checksum": task["checksum"],
        "diagnostic": json.loads(task["result_json"]),
        "export_url": f"/api/v1/diagnostics/{task_id}/export",
    }


@app.get("/api/v1/diagnostics/{task_id}/export", tags=["diagnostics"])
def export_diagnostic(task_id: str) -> Response:
    """把诊断结果（含输入与校验和）导出为可下载的 JSON 文件。"""
    task = _load_task_or_404(task_id)
    document = {
        "task_id": task["task_id"],
        "created_at": task["created_at"],
        "status": task["status"],
        "job_name": task["job_name"],
        "checksum": task["checksum"],
        "input": json.loads(task["input_json"]),
        "diagnostic": json.loads(task["result_json"]),
    }
    payload = json.dumps(document, ensure_ascii=False, indent=2,
                         sort_keys=True)
    return Response(
        content=payload,
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="diagnostic_{task_id}.json"')
        },
    )
