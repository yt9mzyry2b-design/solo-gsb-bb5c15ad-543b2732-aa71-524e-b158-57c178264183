"""请求数据模型（Pydantic v2）。

正射瓦片以轴对齐二维矩形描述：xmin/ymin/xmax/ymax 为瓦片四角坐标
（坐标顺序无关，内部统一归一化为 min/max），resolution 为地面采样距离
（单位/像素，必须为正）。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _finite(v: float) -> float:
    # inf / nan 会让后续扫描与面积统计产生不可复现的错误结果，直接拒绝
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError("必须为有限数值")
    return v


class TileIn(BaseModel):
    """单张无人机正射瓦片的元数据。"""

    model_config = ConfigDict(extra="forbid")

    tile_id: str = Field(..., min_length=1, description="瓦片唯一标识")
    row: int = Field(..., ge=0, description="瓦片行号（自 0 起）")
    col: int = Field(..., ge=0, description="瓦片列号（自 0 起）")
    xmin: float = Field(..., description="西边界坐标")
    ymin: float = Field(..., description="南边界坐标")
    xmax: float = Field(..., description="东边界坐标")
    ymax: float = Field(..., description="北边界坐标")
    resolution: float = Field(..., gt=0, description="地面采样距离（坐标单位/像素）")
    crs: Optional[str] = Field(None, description="坐标参考，如 EPSG:32650；可空")

    @field_validator("tile_id")
    @classmethod
    def _id_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("tile_id 不能为空白字符")
        return v

    @field_validator("xmin", "ymin", "xmax", "ymax")
    @classmethod
    def _finite_coord(cls, v: float) -> float:
        return _finite(v)

    @field_validator("resolution")
    @classmethod
    def _finite_res(cls, v: float) -> float:
        return _finite(v)


class DiagnoseRequest(BaseModel):
    """诊断任务请求。"""

    model_config = ConfigDict(extra="forbid")

    tiles: list[TileIn] = Field(..., min_length=1, description="瓦片元数据列表")
    tolerance: float = Field(
        0.0,
        ge=0.0,
        description="接缝容差（坐标单位）：矩形最小距离 ≤ 该值即视为可拼接连通",
    )
    min_gap_area: float = Field(
        0.0,
        ge=0.0,
        description="小于该面积的缺口将被忽略（坐标单位²），用于滤除数值碎屑",
    )
    max_pairs: int = Field(
        100,
        ge=1,
        le=10000,
        description="重叠对/接缝对明细的最大返回条数（总数仍在 *_pair_count 中给出）",
    )
    expect_grid: bool = Field(
        True, description="是否按行/列号推断规则栅格并报出缺失格位"
    )
    resolution_rel_tol: float = Field(
        0.02,
        ge=0.0,
        le=1.0,
        description="分辨率相对中位数偏差告警阈值（默认 2%）",
    )

    @field_validator("tolerance", "min_gap_area", "resolution_rel_tol")
    @classmethod
    def _finite_param(cls, v: float) -> float:
        return _finite(v)
