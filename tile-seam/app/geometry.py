"""二维矩形基础几何工具。

所有瓦片都是轴对齐矩形（AABB）。两矩形关系由
最小欧氏距离 dist 与相交面积 area 共同刻画：

* area > 0           -> 物理重叠
* area == 0 且 dist == 0 -> 边/点恰好相接
* area == 0 且 dist > 0  -> 存在宽度为 dist 的缺口
"""
from __future__ import annotations


def intersect_area(a, b) -> float:
    """两 AABB 的相交面积；仅边接触时返回 0。"""
    ow = min(a.xmax, b.xmax) - max(a.xmin, b.xmin)
    oh = min(a.ymax, b.ymax) - max(a.ymin, b.ymin)
    if ow <= 0 or oh <= 0:
        return 0.0
    return ow * oh


def rect_distance(a, b) -> float:
    """两 AABB 之间的最小欧氏距离；相交/包含/相接时为 0。"""
    dx = max(0.0, max(a.xmin, b.xmin) - min(a.xmax, b.xmax))
    dy = max(0.0, max(a.ymin, b.ymin) - min(a.ymax, b.ymax))
    return (dx * dx + dy * dy) ** 0.5


def overlap_dimensions(a, b):
    """返回 (x 方向重叠宽, y 方向重叠高)，不重叠方向为 0。"""
    ow = min(a.xmax, b.xmax) - max(a.xmin, b.xmin)
    oh = min(a.ymax, b.ymax) - max(a.ymin, b.ymin)
    return max(0.0, ow), max(0.0, oh)
