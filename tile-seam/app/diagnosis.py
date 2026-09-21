"""瓦片接缝诊断核心算法。

流程（全部使用确定性计算，同一输入 + 同一 SCHEMA_VERSION 必得同一结果）：

1. 校验：退化矩形、重复 id / 行列号、CRS 不一致、分辨率偏差、像素取整偏差。
2. 扫描线生成候选邻居（x 方向活动集 + y 跨度剪枝，搜索半径自适应），
   对每个候选计算矩形最小距离 d 与相交面积：
     - d ≤ tolerance  -> 并查集合并（容差桥接，允许宽度 ≤ tolerance 的缝）；
     - area > 0       -> 物理重叠对；
     - 0 < d ≤ tol    -> 被容差桥接的接缝对；
     - 跨连通块且 d > tol -> 连通块最小间距。
3. 全局坐标压缩网格扫描：统计覆盖层数 depth，depth≥2 的格元做
   4-连通聚簇得到重叠区域；同时得到各连通块并集面积。
4. 逐连通块局部压缩网格：对块外接矩形内的未覆盖格元从边界泛洪，
   残留格元即“被包围的覆盖缺口”，4-连通聚簇并按 min_gap_area 过滤。
5. 按 row/col 推断规则栅格，报出缺失格位。
"""
from __future__ import annotations

import hashlib
import json
import statistics
from bisect import bisect_left
from collections import deque
from dataclasses import dataclass
from typing import Any

from .geometry import intersect_area, rect_distance
from .models import DiagnoseRequest

SCHEMA_VERSION = "1.0.0"
ALGORITHM = "sweepline-dsu + compressed-grid 2d scan"

_EPS = 1e-9
# 输出多边形最多保留多少个矩形条带（面积等统计不受影响）
_MAX_POLY_RECTS = 500


def r6(v: float) -> float:
    """统一保留 6 位小数并消除 -0.0，保证 JSON 可复现。"""
    out = round(float(v), 6)
    return 0.0 if out == 0 else out


@dataclass(slots=True)
class _Tile:
    idx: int
    tile_id: str
    row: int
    col: int
    xmin: float
    ymin: float
    xmax: float
    ymax: float
    resolution: float
    crs: str | None


class _DSU:
    __slots__ = ("p",)

    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        p = self.p
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # 以较小根号挂接，保证分块标号稳定
            if ra < rb:
                self.p[rb] = ra
            else:
                self.p[ra] = rb


def _canonical_json(req: DiagnoseRequest) -> str:
    """规范化序列化：瓦片按 tile_id/row/col 排序，使同一数据集乱序提交得到同一指纹。"""
    payload = req.model_dump()
    payload["tiles"] = sorted(
        payload["tiles"],
        key=lambda t: (t["tile_id"], t["row"], t["col"]),
    )
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _dedupe_edges(values: list[float]) -> list[float]:
    """坐标去重：浮点意义下相邻（差 ≤ 1e-9）的边视为同一条。"""
    values.sort()
    out: list[float] = []
    for v in values:
        if not out or abs(v - out[-1]) > _EPS:
            out.append(v)
    return out


def _row_run_polygon(
    rows: dict[int, list[tuple[int, int]]],
    xs: list[float],
    ys: list[float],
) -> list[list[float]]:
    """把 {格元行: [(起始列, 结束列), ...]} 合并为矩形条带多边形。"""
    rects: list[list[float]] = []
    for r in sorted(rows):
        for c0, c1 in rows[r]:
            rects.append([r6(xs[c0]), r6(ys[r]), r6(xs[c1 + 1]), r6(ys[r + 1])])
            if len(rects) >= _MAX_POLY_RECTS:
                return rects
    return rects


def _cluster_cells(
    seeds: deque,
    marked: bytearray,
    nx: int,
    ny: int,
) -> list[list[int]]:
    """从 seeds 出发在 marked 布尔格元上做 4-连通聚簇，返回每簇的扁平格元号。

    marked 同时承担访问标记（访问过即清零）。
    """
    clusters: list[list[int]] = []
    while seeds:
        start = seeds.popleft()
        if marked[start] == 0:
            continue
        marked[start] = 0
        cluster = [start]
        q = deque([start])
        while q:
            k = q.popleft()
            c, r = divmod(k, ny)
            for c2, r2 in ((c - 1, r), (c + 1, r), (c, r - 1), (c, r + 1)):
                if 0 <= c2 < nx and 0 <= r2 < ny:
                    n2 = c2 * ny + r2
                    if marked[n2]:
                        marked[n2] = 0
                        cluster.append(n2)
                        q.append(n2)
        clusters.append(cluster)
    return clusters


def diagnose(req: DiagnoseRequest) -> dict[str, Any]:
    """执行完整诊断，返回可 JSON 序列化、可复现的结果字典。"""
    canonical = _canonical_json(req)
    request_id = "req_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    # ---- 1. 逐瓦片校验与归一化 --------------------------------------
    raw_tiles: list[_Tile] = []
    seen_ids: dict[str, int] = {}
    seen_rc: dict[tuple[int, int], int] = {}
    tile_errors: dict[int, list[dict[str, str]]] = {}

    def add_tile_error(i: int, code: str, message: str) -> None:
        tile_errors.setdefault(i, []).append({"code": code, "message": message})
        errors.append({"code": code, "message": message, "tile_id": raw_tiles[i].tile_id})

    for i, t in enumerate(req.tiles):
        tile = _Tile(
            idx=i,
            tile_id=t.tile_id,
            row=t.row,
            col=t.col,
            xmin=min(t.xmin, t.xmax),
            xmax=max(t.xmin, t.xmax),
            ymin=min(t.ymin, t.ymax),
            ymax=max(t.ymin, t.ymax),
            resolution=t.resolution,
            crs=t.crs,
        )
        raw_tiles.append(tile)
        if tile.tile_id in seen_ids:
            add_tile_error(i, "DUPLICATE_TILE_ID", f"tile_id 重复：{tile.tile_id!r}")
        else:
            seen_ids[tile.tile_id] = i
        if (tile.row, tile.col) in seen_rc:
            add_tile_error(i, "DUPLICATE_ROW_COL", f"行列号 ({tile.row},{tile.col}) 重复")
        else:
            seen_rc[(tile.row, tile.col)] = i
        if tile.xmax - tile.xmin <= _EPS or tile.ymax - tile.ymin <= _EPS:
            add_tile_error(i, "DEGENERATE_RECTANGLE", "瓦片矩形宽或高为 0/负，无法构成有效覆盖")

    crs_values = sorted({t.crs for t in raw_tiles if t.crs is not None})
    crs_mismatch = len(crs_values) > 1
    if crs_mismatch:
        errors.append({
            "code": "CRS_MISMATCH",
            "message": f"瓦片坐标系不一致：{', '.join(crs_values)}",
            "tile_id": None,
        })

    # 参与几何诊断的瓦片：自身无错误，且在重复 id 组中只保留首次出现者，
    # 避免重复瓦片虚增覆盖层数
    kept_id: set[str] = set()
    valid_idx: list[int] = []
    for i, t in enumerate(raw_tiles):
        if i in tile_errors:
            continue
        if t.tile_id in kept_id:
            continue
        kept_id.add(t.tile_id)
        valid_idx.append(i)
    # 几何处理一律按 tile_id 排序，结果与输入顺序无关
    valid_idx.sort(key=lambda i: raw_tiles[i].tile_id)
    tiles = [raw_tiles[i] for i in valid_idx]
    n = len(tiles)

    tile_issues: dict[str, list[dict[str, Any]]] = {t.tile_id: [] for t in tiles}

    # ---- 2. 分辨率 / 像素尺寸检查 -----------------------------------
    res_warn_ids: list[str] = []
    if tiles:
        med_res = statistics.median(t.resolution for t in tiles)
        for t in tiles:
            rel = abs(t.resolution - med_res) / med_res if med_res > 0 else 0.0
            if rel > req.resolution_rel_tol + _EPS:
                msg = (
                    f"分辨率 {t.resolution:g} 相对中位 {med_res:g} "
                    f"偏差 {rel * 100:.2f}%（阈值 {req.resolution_rel_tol * 100:.2f}%）"
                )
                tile_issues[t.tile_id].append({
                    "code": "RESOLUTION_MISMATCH", "message": msg, "value": r6(rel),
                })
                res_warn_ids.append(t.tile_id)
        if res_warn_ids:
            warnings.append({
                "code": "RESOLUTION_MISMATCH",
                "message": f"{len(res_warn_ids)} 张瓦片分辨率偏差超阈值",
                "tile_ids": sorted(res_warn_ids),
            })

    for t in tiles:
        w, h = t.xmax - t.xmin, t.ymax - t.ymin
        for axis, px in (("x", w / t.resolution), ("y", h / t.resolution)):
            nearest = round(px)
            if nearest < 1:
                tile_issues[t.tile_id].append({
                    "code": "PIXEL_SIZE_MISMATCH",
                    "message": f"{axis} 方向跨度仅 {px:.3f} 像素，不足 1 像素",
                    "axis": axis,
                    "value": r6(px),
                })
                continue
            rel = abs(px - nearest) / nearest
            if rel > 0.01:
                tile_issues[t.tile_id].append({
                    "code": "PIXEL_SIZE_MISMATCH",
                    "message": f"{axis} 方向 {px:.3f} 像素非整数（最近 {nearest}，偏差 {rel * 100:.2f}%）",
                    "axis": axis,
                    "value": r6(rel),
                })

    if not tiles:
        return _assemble(
            req=req,
            request_id=request_id,
            status="invalid",
            errors=errors,
            warnings=warnings,
            tiles_out=[],
            components=[],
            gap_clusters=[],
            overlap_clusters=[],
            overlap_pairs=[],
            seam_pairs=[],
            component_separations=[],
            missing_cells=[],
            missing_count=0,
            stats={},
            bbox=None,
            neighbor_radius=0.0,
            bridged_count=0,
            n_valid=0,
        )

    # ---- 3. 扫描线候选邻居 ------------------------------------------
    spans = [max(t.xmax - t.xmin, t.ymax - t.ymin) for t in tiles]
    median_span = statistics.median(spans)
    # 半径内保证无漏检：x/y 轴间距 ≤ R 的矩形对必成候选（见模块文档）
    search_radius = max(req.tolerance, 2.0 * median_span)

    order_x = sorted(range(n), key=lambda i: (tiles[i].xmin, tiles[i].tile_id))
    dsu = _DSU(n)
    overlap_pairs: list[tuple[int, int, float, float]] = []
    seam_pairs: list[tuple[int, int, float]] = []
    # 半径内的全部邻近对（含容差外），供分块间距统计
    near_pairs: list[tuple[int, int, float, float]] = []
    cross_sep: dict[tuple[int, int], tuple[float, int, int]] = {}
    active: list[int] = []

    for j in order_x:
        tj = tiles[j]
        kept: list[int] = []
        cutoff = tj.xmin - search_radius - _EPS
        for i in active:
            if tiles[i].xmax >= cutoff:
                kept.append(i)
        active = kept
        for i in active:
            ti = tiles[i]
            y_gate = (ti.ymax - ti.ymin + tj.ymax - tj.ymin) / 2.0 + search_radius
            if abs((ti.ymin + ti.ymax) / 2.0 - (tj.ymin + tj.ymax) / 2.0) > y_gate + _EPS:
                continue
            d = rect_distance(ti, tj)
            if d > search_radius + _EPS:
                continue
            area = intersect_area(ti, tj)
            near_pairs.append((i, j, d, area))
            if d <= req.tolerance + _EPS:
                dsu.union(i, j)
                if area > _EPS:
                    overlap_pairs.append((i, j, d, area))
                elif d > _EPS:
                    seam_pairs.append((i, j, d))
            elif area > _EPS:
                # 容差外仍物理重叠：几何上必相接，仍记录重叠
                overlap_pairs.append((i, j, d, area))
        active.append(j)

    # ---- 4. 连通分块（标号按最小 tile_id 排序，稳定可复现）------------
    roots_of: dict[int, list[int]] = {}
    for i in range(n):
        roots_of.setdefault(dsu.find(i), []).append(i)
    root_order = sorted(roots_of, key=lambda r: min(tiles[i].tile_id for i in roots_of[r]))
    comp_no: dict[int, int] = {}
    comp_members: list[list[int]] = []
    for cid, r in enumerate(root_order):
        comp_no[r] = cid
        comp_members.append(roots_of[r])

    # 跨连通块最小间距（此时并查集已收敛；near_pairs 覆盖半径内全部邻近对）
    for i, j, d, _area in near_pairs:
        ri, rj = dsu.find(i), dsu.find(j)
        if ri != rj:
            key = (min(ri, rj), max(ri, rj))
            cur = cross_sep.get(key)
            if cur is None or d < cur[0]:
                cross_sep[key] = (d, i, j)

    # ---- 5. 全局坐标压缩网格：层数 / 重叠聚簇 / 块并集面积 -------------
    gx = _dedupe_edges([v for t in tiles for v in (t.xmin, t.xmax)])
    gy = _dedupe_edges([v for t in tiles for v in (t.ymin, t.ymax)])
    nx, ny = len(gx) - 1, len(gy) - 1
    total_cells = nx * ny
    depth = bytearray(total_cells) if n < 255 else _int_array(total_cells, 0)
    owner = bytearray(total_cells) if len(comp_members) < 256 else _int_array(total_cells, -1)
    for k in range(total_cells):
        owner[k] = 255 if isinstance(owner, bytearray) else -1

    for i in range(n):
        t = tiles[i]
        # 边值直接取自同一批浮点坐标，严格相等，故用 bisect_left 精确定位
        c0 = bisect_left(gx, t.xmin)
        c1 = bisect_left(gx, t.xmax) - 1
        r0 = bisect_left(gy, t.ymin)
        r1 = bisect_left(gy, t.ymax) - 1
        c0 = max(0, c0)
        r0 = max(0, r0)
        c1 = min(nx - 1, c1)
        r1 = min(ny - 1, r1)
        root = dsu.find(i)
        cid = comp_no[root]
        for r in range(r0, r1 + 1):
            base = r
            for c in range(c0, c1 + 1):
                k = c * ny + base
                depth[k] += 1
                if owner[k] == (255 if isinstance(owner, bytearray) else -1):
                    owner[k] = cid

    comp_union_area = [0.0] * len(comp_members)
    total_overlap_area = 0.0       # 被 ≥2 张覆盖的面积（重叠区面积）
    total_excess_area = 0.0        # 多出来的覆盖量（3 层则该格计 2 份）
    overlap_mark = bytearray(total_cells)
    for c in range(nx):
        dx = gx[c + 1] - gx[c]
        base_x = c * ny
        for r in range(ny):
            k = base_x + r
            dd = depth[k]
            if dd:
                area = dx * (gy[r + 1] - gy[r])
                comp_union_area[owner[k]] += area
                if dd >= 2:
                    total_overlap_area += area
                    total_excess_area += (dd - 1) * area
                    overlap_mark[k] = 1

    overlap_seeds = deque(i for i, v in enumerate(overlap_mark) if v)
    raw_ov_clusters = _cluster_cells(overlap_seeds, overlap_mark, nx, ny)

    # 重叠对 -> 聚簇（相交矩形中点落在某个 depth≥2 格元中）
    # 注意：此时 overlap_pairs 尚未排序，聚簇编号按原始顺序绑定后再一起排序
    cell_cluster = [-1] * total_cells
    overlap_clusters: list[dict[str, Any]] = []
    for cl_id, cells in enumerate(raw_ov_clusters):
        area = 0.0
        dmax = 0
        runs: dict[int, list[tuple[int, int]]] = {}
        for k in cells:
            cell_cluster[k] = cl_id
            c, r = divmod(k, ny)
            area += (gx[c + 1] - gx[c]) * (gy[r + 1] - gy[r])
            dmax = max(dmax, depth[k])
        # 条带合并：按行收集连续列段
        for k in cells:
            c, r = divmod(k, ny)
            row = runs.setdefault(r, [])
            if row and row[-1][1] == c - 1:
                row[-1] = (row[-1][0], c)
            else:
                row.append((c, c))
        overlap_clusters.append({
            "cells": cells,
            "area": area,
            "depth_max": dmax,
            "polygon_rects": _row_run_polygon(runs, gx, gy),
            "polygon_truncated": sum(len(v) for v in runs.values()) > _MAX_POLY_RECTS,
        })
    pair_records: list[tuple[int, int, float, float, int]] = []
    for i, j, d, area in overlap_pairs:
        ti, tj = tiles[i], tiles[j]
        mx = (max(ti.xmin, tj.xmin) + min(ti.xmax, tj.xmax)) / 2.0
        my = (max(ti.ymin, tj.ymin) + min(ti.ymax, tj.ymax)) / 2.0
        # 中点所在格元：最靠右的“边 ≤ 中点”位置
        c = bisect_left(gx, mx + _EPS) - 1
        r = bisect_left(gy, my + _EPS) - 1
        cl = -1
        if 0 <= c < nx and 0 <= r < ny:
            cl = cell_cluster[c * ny + r]
        pair_records.append((i, j, d, area, cl))

    # ---- 6. 逐连通块局部网格：覆盖缺口 --------------------------------
    # 密网格边 = 原始瓦片边 ± tol。密网格上区分：
    #   covered_raw 格元（瓦片中）/ covered_dil 格元（仅落在 tol 扩张带，桥）。
    # 外部泛洪只走“原始未覆盖且非桥”格元；宽度 ≤ tol 的缝被桥接通到外部。
    # 洞面积/多边形仍取原始网格（密网格仅用于判定包围性，故面积不被容差侵蚀）。
    gap_clusters: list[dict[str, Any]] = []
    tol = req.tolerance
    for cid, members in enumerate(comp_members):
        rx = _dedupe_edges([v for i in members for v in (tiles[i].xmin, tiles[i].xmax)])
        ry = _dedupe_edges([v for i in members for v in (tiles[i].ymin, tiles[i].ymax)])
        rnx, rny = len(rx) - 1, len(ry) - 1
        if rnx <= 0 or rny <= 0:
            continue
        nraw = rnx * rny
        cover_raw = bytearray(nraw)
        for i in members:
            t = tiles[i]
            c0 = max(0, bisect_left(rx, t.xmin))
            c1 = min(rnx - 1, bisect_left(rx, t.xmax) - 1)
            r0 = max(0, bisect_left(ry, t.ymin))
            r1 = min(rny - 1, bisect_left(ry, t.ymax) - 1)
            for c in range(c0, c1 + 1):
                base = c * rny
                for r in range(r0, r1 + 1):
                    cover_raw[base + r] = 1

        # 密网格：边取原始边及其 ±tol 偏移
        dx_edges = _dedupe_edges([v for i in members
                                  for v in (tiles[i].xmin, tiles[i].xmax,
                                            tiles[i].xmin - tol, tiles[i].xmax + tol)])
        dy_edges = _dedupe_edges([v for i in members
                                  for v in (tiles[i].ymin, tiles[i].ymax,
                                            tiles[i].ymin - tol, tiles[i].ymax + tol)])
        dnx, dny = len(dx_edges) - 1, len(dy_edges) - 1
        # 密格元按中点归类（密网格边恰为扩张边界，中点严格落在某一侧）
        d_raw = bytearray(dnx * dny)
        d_dil = bytearray(dnx * dny)
        for i in members:
            t = tiles[i]
            c0 = max(0, bisect_left(dx_edges, t.xmin - tol))
            c1 = min(dnx - 1, bisect_left(dx_edges, t.xmax + tol) - 1)
            r0 = max(0, bisect_left(dy_edges, t.ymin - tol))
            r1 = min(dny - 1, bisect_left(dy_edges, t.ymax + tol) - 1)
            rc0 = max(0, bisect_left(dx_edges, t.xmin))
            rc1 = min(dnx - 1, bisect_left(dx_edges, t.xmax) - 1)
            rr0 = max(0, bisect_left(dy_edges, t.ymin))
            rr1 = min(dny - 1, bisect_left(dy_edges, t.ymax) - 1)
            for c in range(c0, c1 + 1):
                base = c * dny
                for r in range(r0, r1 + 1):
                    d_dil[base + r] = 1
            for c in range(rc0, rc1 + 1):
                base = c * dny
                for r in range(rr0, rr1 + 1):
                    d_raw[base + r] = 1
        # 可通行（外部泛洪）：既非原始覆盖也非扩张桥
        passable = bytearray(dnx * dny)
        for k in range(dnx * dny):
            if not d_raw[k] and not d_dil[k]:
                passable[k] = 1
        outside = bytearray(dnx * dny)
        flood: deque[int] = deque()
        for c in range(dnx):
            for r in (0, dny - 1):
                k = c * dny + r
                if passable[k]:
                    outside[k] = 1
                    flood.append(k)
        for r in range(dny):
            for c in (0, dnx - 1):
                k = c * dny + r
                if passable[k] and not outside[k]:
                    outside[k] = 1
                    flood.append(k)
        while flood:
            k = flood.popleft()
            c, r = divmod(k, dny)
            for c2, r2 in ((c - 1, r), (c + 1, r), (c, r - 1), (c, r + 1)):
                if 0 <= c2 < dnx and 0 <= r2 < dny:
                    k2 = c2 * dny + r2
                    if passable[k2] and not outside[k2]:
                        outside[k2] = 1
                        flood.append(k2)

        def _dense_enclosed(x: float, y: float) -> bool:
            """原始格元内点在密网格中是否为“被包围的真缺口”。

            在中点上叠加极小偏移（格宽的 1e-6），避免采样点恰好落在
            tol 扩张边线上被误判为桥。
            """
            sx = 1e-6 * (rx[min(rnx, 1)] - rx[0]) if rnx > 0 else 0.0
            sy = 1e-6 * (ry[min(rny, 1)] - ry[0]) if rny > 0 else 0.0
            c = bisect_left(dx_edges, x + sx + _EPS) - 1
            r = bisect_left(dy_edges, y + sy + _EPS) - 1
            if not (0 <= c < dnx and 0 <= r < dny):
                return False
            k = c * dny + r
            return bool(passable[k] and not outside[k])

        # 原始未覆盖格元：中点在密网格中被包围才记为缺口
        gap_mark = bytearray(nraw)
        gap_seeds: deque[int] = deque()
        for c in range(rnx):
            mxc = (rx[c] + rx[c + 1]) / 2.0
            base = c * rny
            for r in range(rny):
                k = base + r
                if cover_raw[k]:
                    continue
                myr = (ry[r] + ry[r + 1]) / 2.0
                if _dense_enclosed(mxc, myr):
                    gap_mark[k] = 1
                    gap_seeds.append(k)
        for cells in _cluster_cells(gap_seeds, gap_mark, rnx, rny):
            area = 0.0
            runs: dict[int, list[tuple[int, int]]] = {}
            for k in cells:
                c, r = divmod(k, rny)
                area += (rx[c + 1] - rx[c]) * (ry[r + 1] - ry[r])
            if area < req.min_gap_area - _EPS:
                continue
            for k in cells:
                c, r = divmod(k, rny)
                row = runs.setdefault(r, [])
                if row and row[-1][1] == c - 1:
                    row[-1] = (row[-1][0], c)
                else:
                    row.append((c, c))
            gap_clusters.append({
                "component_id": cid,
                "area": area,
                "polygon_rects": _row_run_polygon(runs, rx, ry),
                "polygon_truncated": sum(len(v) for v in runs.values()) > _MAX_POLY_RECTS,
            })
    gap_clusters.sort(key=lambda g: (-g["area"], g["component_id"]))

    # ---- 7. 规则栅格缺失格位 -----------------------------------------
    missing_cells: list[dict[str, Any]] = []
    missing_count = 0
    if req.expect_grid and tiles:
        present_rc = {(t.row, t.col) for t in tiles}
        for cid, members in enumerate(comp_members):
            rows = sorted(tiles[i].row for i in members)
            cols = sorted(tiles[i].col for i in members)
            rmin, rmax, cmin, cmax = rows[0], rows[-1], cols[0], cols[-1]
            if rmax == rmin or cmax == cmin:
                continue
            mwidth = statistics.median(tiles[i].xmax - tiles[i].xmin for i in members)
            mheight = statistics.median(tiles[i].ymax - tiles[i].ymin for i in members)
            bxmin = min(tiles[i].xmin for i in members)
            bymin = min(tiles[i].ymin for i in members)
            for r in range(rmin, rmax + 1):
                for c in range(cmin, cmax + 1):
                    if (r, c) in present_rc:
                        continue
                    missing_count += 1
                    if len(missing_cells) < 1000:
                        missing_cells.append({
                            "row": r,
                            "col": c,
                            "component_id": cid,
                            "estimated_center": [
                                r6(bxmin + (c - cmin + 0.5) * mwidth),
                                r6(bymin + (r - rmin + 0.5) * mheight),
                            ],
                        })
        if missing_count:
            warnings.append({
                "code": "EXPECTED_TILE_MISSING",
                "message": f"按行列号推断的规则栅格中缺失 {missing_count} 个格位",
                "count": missing_count,
            })

    bridged_count = len(seam_pairs)
    if bridged_count:
        warnings.append({
            "code": "TOLERANCE_BRIDGED_GAPS",
            "message": f"{bridged_count} 对瓦片之间存在 ≤ 容差的缝隙，已按可拼接处理",
            "count": bridged_count,
        })

    # ---- 8. 组装输出 -------------------------------------------------
    bbox = [
        r6(min(t.xmin for t in tiles)),
        r6(min(t.ymin for t in tiles)),
        r6(max(t.xmax for t in tiles)),
        r6(max(t.ymax for t in tiles)),
    ]

    components_out = []
    for cid, members in enumerate(comp_members):
        members_sorted = sorted(members, key=lambda i: tiles[i].tile_id)
        cb = [
            r6(min(tiles[i].xmin for i in members)),
            r6(min(tiles[i].ymin for i in members)),
            r6(max(tiles[i].xmax for i in members)),
            r6(max(tiles[i].ymax for i in members)),
        ]
        gap_area = sum(g["area"] for g in gap_clusters if g["component_id"] == cid)
        components_out.append({
            "component_id": cid,
            "tile_count": len(members),
            "tile_ids": [tiles[i].tile_id for i in members_sorted],
            "bbox": cb,
            "union_area": r6(comp_union_area[cid]),
            "gap_area": r6(gap_area),
        })

    def id_pair(i: int, j: int) -> list[str]:
        a, b = tiles[i].tile_id, tiles[j].tile_id
        return sorted((a, b))

    pair_records.sort(key=lambda p: (-p[3], id_pair(p[0], p[1])))
    overlap_pairs_out = []
    for i, j, d, area, cl in pair_records:
        ti, tj = tiles[i], tiles[j]
        ow = min(ti.xmax, tj.xmax) - max(ti.xmin, tj.xmin)
        oh = min(ti.ymax, tj.ymax) - max(ti.ymin, tj.ymin)
        overlap_pairs_out.append({
            "tile_ids": id_pair(i, j),
            "intersection_area": r6(area),
            "overlap_width": r6(max(0.0, ow)),
            "overlap_height": r6(max(0.0, oh)),
            "component_ids": sorted({comp_no[dsu.find(i)], comp_no[dsu.find(j)]}),
            "cluster_id": cl if cl >= 0 else None,
            "distance": r6(d),
        })
    overlap_pair_total = len(overlap_pairs_out)
    overlap_pairs_out = overlap_pairs_out[: req.max_pairs]

    seam_pairs.sort(key=lambda p: (-p[2], id_pair(p[0], p[1])))
    seam_pairs_out = [{
        "tile_ids": id_pair(i, j),
        "gap_width": r6(d),
        "component_ids": sorted({comp_no[dsu.find(i)], comp_no[dsu.find(j)]}),
    } for i, j, d in seam_pairs[: req.max_pairs]]

    root_to_cid = {r: comp_no[r] for r in root_order}
    separations = []
    for (ra, rb), (d, i, j) in cross_sep.items():
        separations.append((d, root_to_cid[ra], root_to_cid[rb], id_pair(i, j)))
    separations.sort(key=lambda s: (s[0], s[1], s[2]))
    component_separations = [{
        "component_ids": [ca, cb],
        "nearest_distance": r6(d),
        "nearest_tile_pair": pair,
    } for d, ca, cb, pair in separations[: req.max_pairs]]

    overlap_clusters_out = []
    for cl_id, cl in enumerate(overlap_clusters):
        overlap_clusters_out.append({
            "cluster_id": cl_id,
            "area": r6(cl["area"]),
            "depth_max": cl["depth_max"],
            "tile_pair_count": sum(1 for rec in pair_records if rec[4] == cl_id),
            "component_ids": sorted({
                c for c in range(len(comp_members))
                if _cluster_touches_component(cl, c, owner, ny)
            }),
            "polygon_rects": cl["polygon_rects"],
            "polygon_truncated": cl["polygon_truncated"],
        })

    tile_area_sum = sum((t.xmax - t.xmin) * (t.ymax - t.ymin) for t in tiles)
    gap_area_total = sum(g["area"] for g in gap_clusters)
    idx_by_id = {t.tile_id: i for i, t in enumerate(tiles)}
    tiles_out = []
    for t in sorted(tiles, key=lambda x: x.tile_id):
        tiles_out.append({
            "tile_id": t.tile_id,
            "row": t.row,
            "col": t.col,
            "xmin": r6(t.xmin), "ymin": r6(t.ymin),
            "xmax": r6(t.xmax), "ymax": r6(t.ymax),
            "resolution": r6(t.resolution),
            "crs": t.crs,
            "component_id": comp_no[dsu.find(idx_by_id[t.tile_id])],
            "issues": tile_issues[t.tile_id],
        })

    has_gaps = len(gap_clusters) > 0
    has_overlaps = len(overlap_clusters) > 0
    is_single = len(comp_members) == 1
    stitchable = is_single and not has_gaps and not errors
    if errors:
        status = "invalid"
    else:
        status = "ok"
    not_stitch_reasons: list[str] = []
    if errors:
        not_stitch_reasons.append("存在输入错误")
    if not is_single:
        not_stitch_reasons.append(f"存在 {len(comp_members)} 个互不连通分块（容差={req.tolerance:g}）")
    if has_gaps:
        not_stitch_reasons.append("连通块内部存在覆盖缺口")

    stats = {
        "tile_count_input": len(raw_tiles),
        "tile_count_valid": n,
        "tile_count_rejected": len(raw_tiles) - n,
        "component_count": len(comp_members),
        "gap_cluster_count": len(gap_clusters),
        "gap_area_total": r6(gap_area_total),
        "overlap_cluster_count": len(overlap_clusters),
        "overlap_pair_count": overlap_pair_total,
        "overlap_pair_returned": len(overlap_pairs_out),
        "seam_pair_count": len(seam_pairs),
        "seam_pair_returned": len(seam_pairs_out),
        "missing_grid_cell_count": missing_count,
        "total_tile_area": r6(tile_area_sum),
        "union_area": r6(sum(comp_union_area)),
        "overlap_area": r6(total_overlap_area),
        "excess_overlap_area": r6(total_excess_area),
        "resolution_median": r6(statistics.median(t.resolution for t in tiles)),
        "crs_values": crs_values,
    }

    return _assemble(
        req=req,
        request_id=request_id,
        status=status,
        errors=errors,
        warnings=warnings,
        tiles_out=tiles_out,
        components=components_out,
        gap_clusters=[{
            "gap_id": gid,
            "component_id": g["component_id"],
            "area": r6(g["area"]),
            "polygon_rects": g["polygon_rects"],
            "polygon_truncated": g["polygon_truncated"],
        } for gid, g in enumerate(gap_clusters)],
        overlap_clusters=overlap_clusters_out,
        overlap_pairs=overlap_pairs_out,
        seam_pairs=seam_pairs_out,
        component_separations=component_separations,
        missing_cells=missing_cells,
        missing_count=missing_count,
        stats=stats,
        bbox=bbox,
        neighbor_radius=search_radius,
        bridged_count=bridged_count,
        n_valid=n,
        stitchable=stitchable,
        is_single=is_single,
        has_gaps=has_gaps,
        has_overlaps=has_overlaps,
        reasons=not_stitch_reasons,
    )


def _int_array(n: int, fill: int = 0):
    from array import array
    return array("i", [fill] * n)


def _cluster_touches_component(cluster: dict, cid: int, owner, ny: int) -> bool:
    for k in cluster["cells"]:
        if owner[k] == cid:
            return True
    return False


def _assemble(
    *, req, request_id, status, errors, warnings, tiles_out, components,
    gap_clusters, overlap_clusters, overlap_pairs, seam_pairs,
    component_separations, missing_cells, missing_count, stats, bbox,
    neighbor_radius, bridged_count, n_valid,
    stitchable=False, is_single=False, has_gaps=False, has_overlaps=False,
    reasons=None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "status": status,
        "stitchable": stitchable,
        "summary": {
            "is_single_component": is_single,
            "has_gaps": has_gaps,
            "has_overlaps": has_overlaps,
            "not_stitchable_reasons": reasons or [],
        },
        "params": {
            "tolerance": r6(req.tolerance),
            "min_gap_area": r6(req.min_gap_area),
            "max_pairs": req.max_pairs,
            "expect_grid": req.expect_grid,
            "resolution_rel_tol": r6(req.resolution_rel_tol),
            "neighbor_search_radius": r6(neighbor_radius),
        },
        "bbox": bbox,
        "statistics": stats,
        "components": components,
        "gaps": gap_clusters,
        "overlaps": overlap_clusters,
        "overlap_pairs": overlap_pairs,
        "seam_pairs": seam_pairs,
        "component_separations": component_separations,
        "missing_grid_cells": {
            "count": missing_count,
            "returned": len(missing_cells),
            "cells": missing_cells,
        },
        "tiles": tiles_out,
        "errors": errors,
        "warnings": warnings,
        "reproducibility": {
            "request_sha256": request_id.replace("req_", "", 1),
            "schema_version": SCHEMA_VERSION,
            "algorithm": ALGORITHM,
            "deterministic": True,
            "note": "结果仅取决于输入与 schema_version，不含时间戳/随机数；时间戳仅保存在任务记录中",
            "tolerance_bridged_pair_count": bridged_count,
            "valid_tiles": n_valid,
        },
    }
