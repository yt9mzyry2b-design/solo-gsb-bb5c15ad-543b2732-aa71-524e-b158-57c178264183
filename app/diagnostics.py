"""测绘瓦片接缝诊断核心算法。

基于"坐标吸附 + 二维差分数组扫描 + 行程连通"的矩形集合分析：

1. 坐标吸附（容差处理）：把所有瓦片边的 x / y 坐标分别排序聚簇，
   间距不超过容差 tolerance 的坐标吸附到同一代表值，消除接边处的
   细小裂缝 / 重叠毛刺。
2. 二维矩形扫描：吸附后的坐标把外包矩形切分为基本单元格，
   用二维差分数组 O(单元格数) 计算每个单元格的覆盖次数。
3. 区域提取：对覆盖次数为 0（缺口）或 >= 2（重叠）的单元格做
   行程压缩 + 并查集连通合并，输出最大化区域。
4. 连通分块：按"闭矩形相交（含共边 / 共点）"构建瓦片邻接关系，
   并查集求连通分量；分量数 > 1 即存在无法拼接的孤立分块。

整个流程对输入顺序不敏感（先排序规范化），输出可复现。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

AREA_DIGITS = 6   # 面积 / 尺寸输出保留小数位
COORD_DIGITS = 9  # 坐标输出保留小数位


@dataclass(frozen=True)
class Tile:
    tile_id: str
    row: int
    col: int
    min_x: float
    min_y: float
    max_x: float
    max_y: float
    resolution: float | None = None

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    @property
    def area(self) -> float:
        return self.width * self.height


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _round(value: float, digits: int) -> float:
    """统一舍入，避免 -0.0 与浮点尾差影响可复现性。"""
    r = round(float(value), digits)
    return 0.0 if r == 0 else r


def _snap_coords(values: list[float], tol: float) -> dict[float, float]:
    """把相距不超过 tol 的坐标吸附到簇首（排序后贪心聚簇，确定性）。"""
    mapping: dict[float, float] = {}
    rep: float | None = None
    for v in sorted(values):
        if rep is None or v - rep > tol:
            rep = v
        mapping[v] = rep
    return mapping


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _UnionFind:
    def __init__(self) -> None:
        self.parent: list[int] = []

    def add(self) -> int:
        self.parent.append(len(self.parent))
        return len(self.parent) - 1

    def find(self, a: int) -> int:
        parent = self.parent
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


# ---------------------------------------------------------------------------
# 覆盖网格（二维差分扫描）
# ---------------------------------------------------------------------------

def _build_coverage(tiles: list[Tile], tol: float):
    """返回 (xs, ys, snapped_bounds, counts, stride)。

    counts[i * stride + j] 表示单元格 (xs[i], xs[i+1]) x (ys[j], ys[j+1])
    的覆盖次数。
    """
    xmap = _snap_coords([c for t in tiles for c in (t.min_x, t.max_x)], tol)
    ymap = _snap_coords([c for t in tiles for c in (t.min_y, t.max_y)], tol)
    xs = sorted(set(xmap.values()))
    ys = sorted(set(ymap.values()))
    nx, ny = len(xs) - 1, len(ys) - 1
    xidx = {x: i for i, x in enumerate(xs)}
    yidx = {y: j for j, y in enumerate(ys)}

    stride = ny + 2
    diff = [0] * ((nx + 2) * stride)
    snapped: list[tuple[float, float, float, float]] = []
    for t in tiles:
        sx0, sx1 = xmap[t.min_x], xmap[t.max_x]
        sy0, sy1 = ymap[t.min_y], ymap[t.max_y]
        snapped.append((sx0, sy0, sx1, sy1))
        x0, x1 = xidx[sx0], xidx[sx1]
        y0, y1 = yidx[sy0], yidx[sy1]
        diff[x0 * stride + y0] += 1
        diff[x0 * stride + y1] -= 1
        diff[x1 * stride + y0] -= 1
        diff[x1 * stride + y1] += 1

    # 二维前缀和，得到每个单元格的覆盖次数
    for i in range(nx + 1):
        base = i * stride
        for j in range(1, ny + 1):
            diff[base + j] += diff[base + j - 1]
    for j in range(ny + 1):
        for i in range(1, nx + 1):
            diff[i * stride + j] += diff[(i - 1) * stride + j]

    return xs, ys, snapped, diff, stride


def _extract_regions(
    xs: list[float],
    ys: list[float],
    counts: list[int],
    stride: int,
    predicate: Callable[[int], bool],
) -> list[dict[str, Any]]:
    """把满足 predicate 的单元格按 4-连通合并为区域（行程压缩 + 并查集）。"""
    nx, ny = len(xs) - 1, len(ys) - 1
    uf = _UnionFind()
    rows: list[list[tuple[int, int, int, float, int]]] = []  # (i0, i1, uid, area, depth)
    prev: list[tuple[int, int, int, float, int]] = []

    for j in range(ny):
        cur: list[tuple[int, int, int, float, int]] = []
        cell_h = ys[j + 1] - ys[j]
        i = 0
        while i < nx:
            if predicate(counts[i * stride + j]):
                i0 = i
                depth = 0
                while i < nx and predicate(counts[i * stride + j]):
                    depth = max(depth, counts[i * stride + j])
                    i += 1
                area = (xs[i] - xs[i0]) * cell_h
                cur.append((i0, i - 1, uf.add(), area, depth))
            else:
                i += 1
        # 与上一行重叠的行程做并查集合并（双指针）
        p = 0
        for a0, a1, aid, _, _ in cur:
            while p < len(prev) and prev[p][1] < a0:
                p += 1
            q = p
            while q < len(prev) and prev[q][0] <= a1:
                uf.union(aid, prev[q][2])
                q += 1
        rows.append(cur)
        prev = cur

    groups: dict[int, dict[str, Any]] = {}
    for j, row_runs in enumerate(rows):
        for i0, i1, uid, area, depth in row_runs:
            root = uf.find(uid)
            g = groups.get(root)
            if g is None:
                groups[root] = {
                    "i0": i0, "i1": i1, "j0": j, "j1": j,
                    "area": area, "depth": depth,
                }
            else:
                g["i0"] = min(g["i0"], i0)
                g["i1"] = max(g["i1"], i1)
                g["j0"] = min(g["j0"], j)
                g["j1"] = max(g["j1"], j)
                g["area"] += area
                g["depth"] = max(g["depth"], depth)

    regions = []
    for g in groups.values():
        regions.append({
            "min_x": xs[g["i0"]],
            "max_x": xs[g["i1"] + 1],
            "min_y": ys[g["j0"]],
            "max_y": ys[g["j1"] + 1],
            "area": g["area"],
            "depth": g["depth"],
        })
    # 确定性排序：面积降序，再按坐标升序
    regions.sort(key=lambda r: (-r["area"], r["min_y"], r["min_x"],
                                r["max_y"], r["max_x"]))
    return regions


# ---------------------------------------------------------------------------
# 瓦片连通分块
# ---------------------------------------------------------------------------

def _tile_components(
    tiles: list[Tile],
    snapped: list[tuple[float, float, float, float]],
) -> list[list[int]]:
    """闭矩形相交（含共边 / 共点）即视为可拼接相邻，返回瓦片下标分量。"""
    n = len(tiles)
    uf = _UnionFind()
    for _ in range(n):
        uf.add()
    order = sorted(range(n), key=lambda k: (snapped[k][0], snapped[k][2]))
    for a in range(n):
        i = order[a]
        _, y0, x1, y1 = snapped[i]
        for b in range(a + 1, n):
            j = order[b]
            if snapped[j][0] > x1:  # 按 min_x 排序，后续不可能再相交
                break
            if snapped[j][1] <= y1 and snapped[j][3] >= y0:
                uf.union(i, j)

    groups: dict[int, list[int]] = {}
    for k in range(n):
        groups.setdefault(uf.find(k), []).append(k)
    comps = list(groups.values())
    comps.sort(key=lambda c: (min(c), len(c)))
    return comps


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def normalize_tiles(raw_tiles: list[dict[str, Any]]) -> list[Tile]:
    """排序并补全 tile_id，保证诊断结果与输入顺序无关。"""
    indexed = sorted(
        enumerate(raw_tiles),
        key=lambda kv: (kv[1]["row"], kv[1]["col"],
                        kv[1]["min_y"], kv[1]["min_x"], kv[0]),
    )
    tiles: list[Tile] = []
    seen_ids: set[str] = set()
    for pos, (_, raw) in enumerate(indexed):
        tid = raw.get("tile_id") or f"tile_{pos:04d}"
        while tid in seen_ids:  # 防御重复 id，保证确定性
            tid = f"{tid}#{pos}"
        seen_ids.add(tid)
        tiles.append(Tile(
            tile_id=tid,
            row=int(raw["row"]),
            col=int(raw["col"]),
            min_x=float(raw["min_x"]),
            min_y=float(raw["min_y"]),
            max_x=float(raw["max_x"]),
            max_y=float(raw["max_y"]),
            resolution=(None if raw.get("resolution") is None
                        else float(raw["resolution"])),
        ))
    return tiles


def run_diagnostics(tiles: list[Tile], tolerance: float) -> dict[str, Any]:
    """执行接缝诊断，返回可复现的诊断字典（不含任务元数据）。"""
    tol = max(0.0, float(tolerance))
    xs, ys, snapped, counts, stride = _build_coverage(tiles, tol)
    nx, ny = len(xs) - 1, len(ys) - 1

    # ---- 面积统计 ----
    union_area = 0.0
    gap_area = 0.0
    overlap_area = 0.0        # 覆盖次数 >= 2 的面积
    excess_area = 0.0         # 超出单层的冗余覆盖面积 sum(count-1)*cell
    for i in range(nx):
        w = xs[i + 1] - xs[i]
        base = i * stride
        for j in range(ny):
            c = counts[base + j]
            if c == 0:
                gap_area += w * (ys[j + 1] - ys[j])
            else:
                cell = w * (ys[j + 1] - ys[j])
                union_area += cell
                if c >= 2:
                    overlap_area += cell
                    excess_area += cell * (c - 1)

    bbox = {"min_x": xs[0], "min_y": ys[0], "max_x": xs[-1], "max_y": ys[-1]}
    bbox_area = (xs[-1] - xs[0]) * (ys[-1] - ys[0])

    # ---- 缺口 / 重叠区域 ----
    gap_regions = _extract_regions(xs, ys, counts, stride, lambda c: c == 0)
    overlap_regions = _extract_regions(xs, ys, counts, stride, lambda c: c >= 2)

    gaps = [{
        "bbox": {
            "min_x": _round(r["min_x"], COORD_DIGITS),
            "min_y": _round(r["min_y"], COORD_DIGITS),
            "max_x": _round(r["max_x"], COORD_DIGITS),
            "max_y": _round(r["max_y"], COORD_DIGITS),
        },
        "width": _round(r["max_x"] - r["min_x"], COORD_DIGITS),
        "height": _round(r["max_y"] - r["min_y"], COORD_DIGITS),
        "area": _round(r["area"], AREA_DIGITS),
    } for r in gap_regions]

    overlaps = []
    for r in overlap_regions:
        involved = sorted(
            t.tile_id for t, (x0, y0, x1, y1) in zip(tiles, snapped)
            if x0 < r["max_x"] and x1 > r["min_x"]
            and y0 < r["max_y"] and y1 > r["min_y"]
        )
        overlaps.append({
            "bbox": {
                "min_x": _round(r["min_x"], COORD_DIGITS),
                "min_y": _round(r["min_y"], COORD_DIGITS),
                "max_x": _round(r["max_x"], COORD_DIGITS),
                "max_y": _round(r["max_y"], COORD_DIGITS),
            },
            "area": _round(r["area"], AREA_DIGITS),
            "max_depth": r["depth"],
            "tile_ids": involved,
        })

    # ---- 连通分块 ----
    comp_indices = _tile_components(tiles, snapped)
    components = []
    for cid, members in enumerate(comp_indices):
        cx0 = min(snapped[k][0] for k in members)
        cy0 = min(snapped[k][1] for k in members)
        cx1 = max(snapped[k][2] for k in members)
        cy1 = max(snapped[k][3] for k in members)
        components.append({
            "component_id": cid,
            "tile_count": len(members),
            "tile_ids": [tiles[k].tile_id for k in members],
            "bbox": {
                "min_x": _round(cx0, COORD_DIGITS),
                "min_y": _round(cy0, COORD_DIGITS),
                "max_x": _round(cx1, COORD_DIGITS),
                "max_y": _round(cy1, COORD_DIGITS),
            },
        })

    # ---- 告警 ----
    warnings: list[dict[str, Any]] = []
    rc_seen: dict[tuple[int, int], str] = {}
    for t in tiles:
        key = (t.row, t.col)
        if key in rc_seen:
            warnings.append({
                "code": "DUPLICATE_ROW_COL",
                "message": f"行列表号 ({t.row}, {t.col}) 被多个瓦片使用",
                "tile_ids": sorted([rc_seen[key], t.tile_id]),
            })
        else:
            rc_seen[key] = t.tile_id

    resolutions = [t.resolution for t in tiles if t.resolution is not None]
    if resolutions and max(resolutions) > min(resolutions) * (1 + 1e-6):
        warnings.append({
            "code": "INCONSISTENT_RESOLUTION",
            "message": (f"瓦片分辨率不一致：min={min(resolutions)}, "
                        f"max={max(resolutions)}"),
            "tile_ids": sorted(t.tile_id for t in tiles
                               if t.resolution is not None),
        })
    bad_pixel = [
        t.tile_id for t in tiles
        if t.resolution and t.resolution > 0 and (
            abs(t.width / t.resolution - round(t.width / t.resolution)) > 1e-6
            or abs(t.height / t.resolution - round(t.height / t.resolution)) > 1e-6
        )
    ]
    if bad_pixel:
        warnings.append({
            "code": "NON_INTEGER_PIXEL_EXTENT",
            "message": "部分瓦片宽高不是分辨率的整数倍，可能存在元数据误差",
            "tile_ids": sorted(bad_pixel),
        })

    summary = {
        "tile_count": len(tiles),
        "bbox": {k: _round(v, COORD_DIGITS) for k, v in bbox.items()},
        "bbox_area": _round(bbox_area, AREA_DIGITS),
        "covered_area": _round(union_area, AREA_DIGITS),
        "gap_area": _round(gap_area, AREA_DIGITS),
        "overlap_area": _round(overlap_area, AREA_DIGITS),
        "excess_overlap_area": _round(excess_area, AREA_DIGITS),
        "coverage_ratio": (_round(union_area / bbox_area, 6)
                           if bbox_area > 0 else 1.0),
        "gap_count": len(gaps),
        "overlap_region_count": len(overlaps),
        "component_count": len(components),
        "is_single_component": len(components) == 1,
        "seamless": len(gaps) == 0 and len(components) == 1,
    }

    return {
        "tolerance": tol,
        "tile_count": len(tiles),
        "summary": summary,
        "gaps": gaps,
        "overlaps": overlaps,
        "components": components,
        "warnings": warnings,
    }


def input_fingerprint(raw_tiles: list[dict[str, Any]], tolerance: float) -> str:
    """对规范化后的输入计算指纹，验证"相同输入 -> 相同诊断"的可复现性。"""
    canonical = [{
        "tile_id": t.tile_id, "row": t.row, "col": t.col,
        "min_x": t.min_x, "min_y": t.min_y,
        "max_x": t.max_x, "max_y": t.max_y,
        "resolution": t.resolution,
    } for t in normalize_tiles(raw_tiles)]
    return _sha256_text(_canonical_json({"tolerance": float(tolerance),
                                         "tiles": canonical}))


def result_checksum(diagnostic: dict[str, Any]) -> str:
    """诊断内容校验和（不含任务 id / 时间戳等元数据）。"""
    return _sha256_text(_canonical_json(diagnostic))
