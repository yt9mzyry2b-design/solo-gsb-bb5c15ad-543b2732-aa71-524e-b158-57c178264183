"""API 请求 / 响应数据模型与单瓦片校验。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class TileIn(BaseModel):
    """无人机正射瓦片元数据。"""

    tile_id: str | None = Field(default=None, max_length=128,
                                description="瓦片标识，缺省时由系统按行列生成")
    row: int = Field(ge=0, description="行号（从 0 开始）")
    col: int = Field(ge=0, description="列号（从 0 开始）")
    min_x: float = Field(description="覆盖范围最小 X（投影坐标）")
    min_y: float = Field(description="覆盖范围最小 Y")
    max_x: float = Field(description="覆盖范围最大 X")
    max_y: float = Field(description="覆盖范围最大 Y")
    resolution: float | None = Field(default=None, gt=0,
                                     description="地面分辨率（坐标单位/像素）")

    @model_validator(mode="after")
    def _check_bounds(self) -> "TileIn":
        if not self.max_x > self.min_x:
            raise ValueError("max_x 必须大于 min_x")
        if not self.max_y > self.min_y:
            raise ValueError("max_y 必须大于 min_y")
        return self


class DiagnoseRequest(BaseModel):
    """接缝诊断请求。"""

    job_name: str | None = Field(default=None, max_length=128,
                                 description="任务名称（可选）")
    tolerance: float = Field(default=0.0, ge=0.0,
                             description="接边容差（坐标单位），小于等于该值的"
                                         "边错位会被吸附对齐")
    tiles: list[TileIn] = Field(min_length=1, description="瓦片元数据列表")


class ValidateTileResponse(BaseModel):
    valid: bool
    errors: list[str]
    warnings: list[str]
    normalized: dict[str, Any] | None


# ---------------------------------------------------------------------------
# 单瓦片校验（手动逐项校验，返回结构化错误而非 422）
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = ("row", "col", "min_x", "min_y", "max_x", "max_y")


def validate_single_tile(payload: Any) -> ValidateTileResponse:
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(payload, dict):
        return ValidateTileResponse(valid=False, errors=["请求体必须是 JSON 对象"],
                                    warnings=[], normalized=None)

    values: dict[str, Any] = {}
    for name in _REQUIRED_FIELDS:
        if name not in payload:
            errors.append(f"缺少必填字段: {name}")
            continue
        v = payload[name]
        if name in ("row", "col"):
            if isinstance(v, bool) or not isinstance(v, int):
                errors.append(f"字段 {name} 必须是整数")
                continue
            if v < 0:
                errors.append(f"字段 {name} 必须 >= 0")
        else:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                errors.append(f"字段 {name} 必须是数值")
                continue
            v = float(v)
        values[name] = v

    tile_id = payload.get("tile_id")
    if tile_id is not None and not isinstance(tile_id, str):
        errors.append("字段 tile_id 必须是字符串")
        tile_id = None

    resolution = payload.get("resolution")
    if resolution is not None:
        if isinstance(resolution, bool) or not isinstance(resolution, (int, float)):
            errors.append("字段 resolution 必须是数值")
            resolution = None
        else:
            resolution = float(resolution)
            if resolution <= 0:
                errors.append("字段 resolution 必须 > 0")
                resolution = None

    if all(k in values for k in ("min_x", "max_x")):
        if not values["max_x"] > values["min_x"]:
            errors.append("max_x 必须大于 min_x")
    if all(k in values for k in ("min_y", "max_y")):
        if not values["max_y"] > values["min_y"]:
            errors.append("max_y 必须大于 min_y")

    if not errors:
        width = values["max_x"] - values["min_x"]
        height = values["max_y"] - values["min_y"]
        if resolution:
            for label, extent in (("宽度", width), ("高度", height)):
                px = extent / resolution
                if abs(px - round(px)) > 1e-6:
                    warnings.append(
                        f"{label}不是分辨率的整数倍（{extent}/{resolution}={px:.4f}），"
                        "可能存在元数据误差")
            if resolution > min(width, height):
                warnings.append("分辨率大于瓦片边长，数值可疑")
        if max(width, height) > 1e7:
            warnings.append("瓦片边长超过 1e7 坐标单位，数值可疑")

    normalized = None
    if not errors:
        normalized = {
            "tile_id": tile_id,
            "row": values["row"],
            "col": values["col"],
            "min_x": values["min_x"],
            "min_y": values["min_y"],
            "max_x": values["max_x"],
            "max_y": values["max_y"],
            "resolution": resolution,
        }

    return ValidateTileResponse(valid=not errors, errors=errors,
                                warnings=warnings, normalized=normalized)
