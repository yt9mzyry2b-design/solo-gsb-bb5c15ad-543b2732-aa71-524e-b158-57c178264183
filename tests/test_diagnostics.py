"""核心诊断算法的纯 Python 单元测试（不依赖 FastAPI）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.diagnostics import (Tile, input_fingerprint, normalize_tiles,
                             result_checksum, run_diagnostics)


def make_tile(row, col, x0, y0, x1, y1, res=0.05, tid=None):
    return Tile(tile_id=tid or f"t{row}_{col}", row=row, col=col,
                min_x=x0, min_y=y0, max_x=x1, max_y=y1, resolution=res)


def grid_tiles(rows, cols, size=1.0, skip=()):
    tiles = []
    for r in range(rows):
        for c in range(cols):
            if (r, c) in skip:
                continue
            tiles.append(make_tile(r, c, c * size, r * size,
                                   (c + 1) * size, (r + 1) * size))
    return tiles


def test_perfect_grid_is_seamless():
    diag = run_diagnostics(grid_tiles(3, 4), tolerance=0.0)
    s = diag["summary"]
    assert s["tile_count"] == 12
    assert s["gap_count"] == 0
    assert s["overlap_region_count"] == 0
    assert s["component_count"] == 1
    assert s["seamless"] is True
    assert s["coverage_ratio"] == 1.0
    assert s["covered_area"] == 12.0


def test_missing_tile_produces_single_gap():
    diag = run_diagnostics(grid_tiles(3, 3, skip={(1, 1)}), tolerance=0.0)
    s = diag["summary"]
    assert s["gap_count"] == 1
    assert s["gap_area"] == 1.0
    assert s["seamless"] is False
    gap = diag["gaps"][0]
    assert gap["bbox"] == {"min_x": 1.0, "min_y": 1.0,
                           "max_x": 2.0, "max_y": 2.0}
    assert gap["width"] == 1.0 and gap["height"] == 1.0


def test_overlap_detected_with_tiles():
    tiles = grid_tiles(1, 2)
    # 第二块向左平移 0.25，与第一块重叠 0.25 x 1
    tiles[1] = make_tile(0, 1, 0.75, 0.0, 1.75, 1.0)
    diag = run_diagnostics(tiles, tolerance=0.0)
    s = diag["summary"]
    assert s["overlap_region_count"] == 1
    assert s["overlap_area"] == 0.25
    ov = diag["overlaps"][0]
    assert ov["max_depth"] == 2
    assert ov["tile_ids"] == ["t0_0", "t0_1"]
    assert ov["bbox"]["min_x"] == 0.75 and ov["bbox"]["max_x"] == 1.0


def test_disconnected_components():
    tiles = grid_tiles(2, 2)
    # 远处孤岛
    tiles.append(make_tile(5, 5, 100.0, 100.0, 101.0, 101.0))
    diag = run_diagnostics(tiles, tolerance=0.0)
    s = diag["summary"]
    assert s["component_count"] == 2
    assert s["is_single_component"] is False
    sizes = sorted(c["tile_count"] for c in diag["components"])
    assert sizes == [1, 4]


def test_edge_touching_counts_as_connected():
    tiles = grid_tiles(1, 2)  # 共边 x=1
    diag = run_diagnostics(tiles, tolerance=0.0)
    assert diag["summary"]["component_count"] == 1


def test_tolerance_snaps_small_misalignment():
    tiles = grid_tiles(1, 2)
    # 第二块右移 0.004，形成 0.004 宽裂缝；容差 0.01 应吸附掉
    tiles[1] = make_tile(0, 1, 1.004, 0.0, 2.004, 1.0)
    diag_strict = run_diagnostics(tiles, tolerance=0.0)
    assert diag_strict["summary"]["gap_count"] == 1
    diag_tol = run_diagnostics(tiles, tolerance=0.01)
    assert diag_tol["summary"]["gap_count"] == 0
    assert diag_tol["summary"]["seamless"] is True


def test_duplicate_row_col_warning():
    tiles = grid_tiles(1, 2)
    tiles[1] = make_tile(0, 0, 1.0, 0.0, 2.0, 1.0)  # 与 t0_0 重复 (row, col)
    diag = run_diagnostics(tiles, tolerance=0.0)
    codes = {w["code"] for w in diag["warnings"]}
    assert "DUPLICATE_ROW_COL" in codes


def test_inconsistent_resolution_warning():
    tiles = grid_tiles(1, 2)
    tiles[1] = make_tile(0, 1, 1.0, 0.0, 2.0, 1.0, res=0.1)
    diag = run_diagnostics(tiles, tolerance=0.0)
    codes = {w["code"] for w in diag["warnings"]}
    assert "INCONSISTENT_RESOLUTION" in codes


def test_reproducible_regardless_of_input_order():
    raw = [
        {"row": 0, "col": 0, "min_x": 0, "min_y": 0, "max_x": 1, "max_y": 1,
         "resolution": 0.05},
        {"row": 0, "col": 1, "min_x": 1, "min_y": 0, "max_x": 2, "max_y": 1,
         "resolution": 0.05},
        {"row": 1, "col": 0, "min_x": 0, "min_y": 1, "max_x": 1, "max_y": 2,
         "resolution": 0.05},
    ]
    diag_a = run_diagnostics(normalize_tiles(raw), 0.01)
    diag_b = run_diagnostics(normalize_tiles(list(reversed(raw))), 0.01)
    assert result_checksum(diag_a) == result_checksum(diag_b)
    assert input_fingerprint(raw, 0.01) == input_fingerprint(
        list(reversed(raw)), 0.01)


def test_l_shaped_gap_region():
    # 3x3 去掉 (0,0) 与 (1,0)：缺口为 L 形，面积 2
    diag = run_diagnostics(grid_tiles(3, 3, skip={(0, 0), (1, 0)}),
                           tolerance=0.0)
    assert diag["summary"]["gap_count"] == 1
    assert diag["summary"]["gap_area"] == 2.0
