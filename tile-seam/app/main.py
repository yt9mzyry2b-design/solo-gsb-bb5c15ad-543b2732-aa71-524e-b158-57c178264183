"""FastAPI 入口：测绘瓦片接缝诊断 API。

接口总览：
  GET  /health                    健康检查
  GET  /                          API 元信息
  POST /api/v1/tiles/validate     单瓦片校验（也支持批量）
  POST /api/v1/diagnose           执行诊断并把任务持久化到 SQLite
  POST /api/v1/diagnose/preview   只诊断不落库（可复现 JSON 预览）
  GET  /api/v1/tasks              任务列表
  GET  /api/v1/tasks/{id}         任务详情（诊断结果）
  GET  /api/v1/tasks/{id}/export  导出诊断结果（JSON / GeoJSON）
  DELETE /api/v1/tasks/{id}       删除任务

默认无需任何第三方账号或外部服务；SQLite 数据文件位于 data/tasks.db。
"""
from __future__ import annotations

import hashlib
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse

from . import __version__
from .db import count_tasks, create_task, delete_task, get_task, init_db, list_tasks
from .diagnosis import SCHEMA_VERSION, diagnose
from .export import to_geojson
from .models import DiagnoseRequest, TileIn

app = FastAPI(
    title="测绘瓦片接缝诊断 API",
    description=(
        "接收带行列号、坐标范围与分辨率的无人机正射瓦片元数据，"
        "按容差进行二维矩形扫描，识别覆盖缺口、重叠区域与无法拼接的连通分块，"
        "返回可复现的诊断 JSON，并使用 SQLite 持久化任务。"
    ),
    version=__version__,
)


@app.on_event("startup")
def _startup() -> None:
    # 首次启动自动建库（幂等）
    init_db()


# ---- 基础 ---------------------------------------------------------------

@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        task_count = count_tasks()
    except Exception:  # noqa: BLE001 - 健康检查不依赖库文件存在
        init_db()
        task_count = count_tasks()
    return {
        "status": "ok",
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "tasks": task_count,
    }


@app.get("/api/v1")
def api_info() -> dict[str, Any]:
    return {
        "name": "tile-seam-diagnosis",
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "storage": "sqlite (data/tasks.db, 可通过 TILE_SEAM_DB 覆盖)",
        "endpoints": [
            "POST /api/v1/tiles/validate",
            "POST /api/v1/diagnose",
            "POST /api/v1/diagnose/preview",
            "GET  /api/v1/tasks",
            "GET  /api/v1/tasks/{task_id}",
            "GET  /api/v1/tasks/{task_id}/export?format=json|geojson",
            "DELETE /api/v1/tasks/{task_id}",
        ],
        "docs": "/docs",
        "openapi": "/openapi.json",
    }


# ---- 单瓦片校验 ----------------------------------------------------------

def _validate_one(t: TileIn) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    xmin, xmax = min(t.xmin, t.xmax), max(t.xmin, t.xmax)
    ymin, ymax = min(t.ymin, t.ymax), max(t.ymin, t.ymax)
    width, height = xmax - xmin, ymax - ymin

    if width <= 1e-9:
        errors.append({"code": "DEGENERATE_RECTANGLE", "message": "x 方向宽度为 0 或负"})
    if height <= 1e-9:
        errors.append({"code": "DEGENERATE_RECTANGLE", "message": "y 方向高度为 0 或负"})
    if t.resolution <= 0:
        errors.append({"code": "INVALID_RESOLUTION", "message": "分辨率必须为正数"})

    if width > 1e-9 and t.resolution > 0:
        px = width / t.resolution
        nearest = round(px)
        if nearest < 1:
            warnings.append({"code": "PIXEL_SIZE_MISMATCH",
                             "message": f"x 方向仅 {px:.3f} 像素，不足 1 像素"})
        elif abs(px - nearest) / nearest > 0.01:
            warnings.append({"code": "PIXEL_SIZE_MISMATCH",
                             "message": f"x 方向 {px:.3f} 像素非整数（偏差 >1%）"})
    if height > 1e-9 and t.resolution > 0:
        py = height / t.resolution
        nearest = round(py)
        if nearest < 1:
            warnings.append({"code": "PIXEL_SIZE_MISMATCH",
                             "message": f"y 方向仅 {py:.3f} 像素，不足 1 像素"})
        elif abs(py - nearest) / nearest > 0.01:
            warnings.append({"code": "PIXEL_SIZE_MISMATCH",
                             "message": f"y 方向 {py:.3f} 像素非整数（偏差 >1%）"})

    return {
        "tile_id": t.tile_id,
        "row": t.row,
        "col": t.col,
        "valid": not errors,
        "normalized_bbox": [
            round(xmin, 6), round(ymin, 6), round(xmax, 6), round(ymax, 6)
        ],
        "width": round(width, 6),
        "height": round(height, 6),
        "pixel_extent": [round(width / t.resolution, 3), round(height / t.resolution, 3)]
        if t.resolution > 0 else None,
        "resolution": t.resolution,
        "crs": t.crs,
        "errors": errors,
        "warnings": warnings,
    }


@app.post("/api/v1/tiles/validate")
def validate_tiles(payload: dict[str, Any]) -> dict[str, Any]:
    """单瓦片校验；请求体可为单个瓦片对象、{"tile": ...} 或 {"tiles": [...]} 批量形式。"""
    if "tiles" in payload:
        raw = payload["tiles"]
    elif "tile" in payload:
        raw = [payload["tile"]]
    else:
        raw = [payload]

    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=422, detail="请提供非空瓦片对象列表（tiles）")

    results, valid_count = [], 0
    for item in raw:
        try:
            tile = TileIn.model_validate(item)
        except Exception as exc:  # noqa: BLE001
            results.append({
                "valid": False,
                "errors": [{"code": "SCHEMA_VALIDATION_ERROR", "message": str(exc)}],
                "warnings": [],
                "raw": item if isinstance(item, dict) else None,
            })
            continue
        out = _validate_one(tile)
        valid_count += 1 if out["valid"] else 0
        results.append(out)

    single = isinstance(raw, list) and len(raw) == 1
    return {
        "schema_version": SCHEMA_VERSION,
        "count": len(results),
        "valid_count": valid_count,
        "invalid_count": len(results) - valid_count,
        "results": results[0] if single else results,
    }


# ---- 诊断 ---------------------------------------------------------------

def _run_and_store(req: DiagnoseRequest, persist: bool) -> dict[str, Any]:
    result = diagnose(req)
    if persist:
        # 任务 id 由确定性指纹派生：同样的输入返回同样的任务
        task_id = "task_" + hashlib.sha256(
            (result["request_id"] + ":v" + SCHEMA_VERSION).encode("utf-8")
        ).hexdigest()[:16]
        existing = get_task(task_id)
        if existing is None:
            create_task(
                task_id,
                result,
                params=req.model_dump(),
            )
        result_with_id: dict[str, Any] = {"task_id": task_id, **result}
        return result_with_id
    return result


@app.post("/api/v1/diagnose")
def create_diagnosis(req: DiagnoseRequest) -> dict[str, Any]:
    """执行诊断并持久化任务；同参数重复提交返回相同 task_id（幂等）。"""
    return _run_and_store(req, persist=True)


@app.post("/api/v1/diagnose/preview")
def preview_diagnosis(req: DiagnoseRequest) -> dict[str, Any]:
    """只计算诊断结果，不写入 SQLite。"""
    return _run_and_store(req, persist=False)


# ---- 任务管理 ------------------------------------------------------------

@app.get("/api/v1/tasks")
def get_tasks(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    return {"limit": limit, "offset": offset, "tasks": list_tasks(limit, offset)}


@app.get("/api/v1/tasks/{task_id}")
def get_task_detail(task_id: str) -> dict[str, Any]:
    task = get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    return task


@app.get("/api/v1/tasks/{task_id}/export")
def export_task(task_id: str, format: str = Query("json", pattern="^(json|geojson)$")):
    """导出诊断结果：format=json 返回原始诊断 JSON；format=geojson 返回 GeoJSON。"""
    task = get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    result = task["result"]
    if format == "geojson":
        return JSONResponse(to_geojson(result),
                            headers={"Content-Disposition":
                                     f'attachment; filename="{task_id}.geojson"'})
    return JSONResponse(result,
                        headers={"Content-Disposition":
                                 f'attachment; filename="{task_id}.json"'})


@app.delete("/api/v1/tasks/{task_id}")
def remove_task(task_id: str) -> dict[str, Any]:
    if not delete_task(task_id):
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    return {"deleted": task_id}
