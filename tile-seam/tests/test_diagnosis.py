"""核心诊断算法单元测试（标准库 unittest，无额外依赖即可跑）。"""
import json
import unittest

from app.diagnosis import diagnose
from app.geometry import intersect_area, rect_distance
from app.models import DiagnoseRequest


class _R:
    def __init__(self, x0, y0, x1, y1):
        self.xmin, self.ymin, self.xmax, self.ymax = x0, y0, x1, y1


def tile(tid, r, c, x, y, w=100, h=100, res=0.1, crs="EPSG:32650"):
    return dict(tile_id=tid, row=r, col=c, xmin=x, ymin=y, xmax=x + w, ymax=y + h,
                resolution=res, crs=crs)


def grid(rows, cols, skip=(), gap=0.0, overlap=0.0, size=100.0, res=0.1):
    # overlap 模式：瓦片宽高=size+overlap，步进=size（边-边重叠恰为 overlap）
    step = size if overlap else size + gap
    tile_size = size + overlap if overlap else size
    out = []
    for r in range(rows):
        for c in range(cols):
            if (r, c) in skip:
                continue
            x, y = c * step, r * step
            out.append(tile(f"t{r}_{c}", r, c, x, y, w=tile_size, h=tile_size, res=res))
    return out


class GeometryTests(unittest.TestCase):
    def test_intersect_and_distance(self):
        a, b = _R(0, 0, 100, 100), _R(90, 0, 190, 100)
        self.assertAlmostEqual(intersect_area(a, b), 1000)
        self.assertAlmostEqual(rect_distance(a, b), 0)
        c = _R(105, 0, 205, 100)
        self.assertAlmostEqual(rect_distance(a, c), 5)
        self.assertEqual(intersect_area(a, c), 0)
        d = _R(0, 120, 100, 220)
        self.assertAlmostEqual(rect_distance(a, d), 20)


class DiagnosisTests(unittest.TestCase):
    def test_perfect_grid_stitchable(self):
        res = diagnose(DiagnoseRequest(tiles=grid(3, 3)))
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["stitchable"])
        self.assertEqual(res["statistics"]["component_count"], 1)
        self.assertEqual(res["statistics"]["overlap_area"], 0.0)
        self.assertEqual(res["statistics"]["gap_cluster_count"], 0)
        self.assertEqual(res["statistics"]["union_area"], 90000)

    def test_missing_center_creates_gap(self):
        res = diagnose(DiagnoseRequest(tiles=grid(3, 3, skip={(1, 1)})))
        self.assertFalse(res["stitchable"])
        self.assertEqual(res["statistics"]["gap_cluster_count"], 1)
        self.assertAlmostEqual(res["statistics"]["gap_area_total"], 10000)
        self.assertEqual(res["gaps"][0]["polygon_rects"], [[100.0, 100.0, 200.0, 200.0]])
        self.assertEqual(res["missing_grid_cells"]["count"], 1)
        self.assertTrue(any("缺口" in s for s in res["summary"]["not_stitchable_reasons"]))

    def test_disjoint_becomes_two_components(self):
        res = diagnose(DiagnoseRequest(tiles=grid(1, 2, gap=5), tolerance=0))
        self.assertEqual(res["statistics"]["component_count"], 2)
        self.assertFalse(res["stitchable"])
        self.assertEqual(res["component_separations"][0]["nearest_distance"], 5.0)
        self.assertEqual(res["component_separations"][0]["component_ids"], [0, 1])

    def test_tolerance_bridges_seam(self):
        res = diagnose(DiagnoseRequest(tiles=grid(1, 2, gap=5), tolerance=6))
        self.assertEqual(res["statistics"]["component_count"], 1)
        self.assertEqual(res["statistics"]["seam_pair_count"], 1)
        self.assertAlmostEqual(res["seam_pairs"][0]["gap_width"], 5.0)
        self.assertTrue(any(w["code"] == "TOLERANCE_BRIDGED_GAPS" for w in res["warnings"]))

    def test_tolerant_seam_not_gap_but_hole_kept(self):
        # 容差桥接缝时，洞面积必须保持精确（不被容差扩张侵蚀）：
        # 4x4 抽中心 2x2（洞 200x200=40000）
        hole = grid(4, 4, skip={(1, 1), (1, 2), (2, 1), (2, 2)})
        res = diagnose(DiagnoseRequest(tiles=hole, tolerance=1.0, min_gap_area=0))
        self.assertEqual(res["statistics"]["component_count"], 1)
        self.assertEqual(res["statistics"]["gap_cluster_count"], 1)
        self.assertAlmostEqual(res["gaps"][0]["area"], 40000.0)

    def test_narrow_seam_not_reported_as_hole(self):
        # 十字窄缝（宽 2 < tolerance 3）：四块连通，缝隙不算内部缺口
        thin = grid(2, 2, gap=2)
        res = diagnose(DiagnoseRequest(tiles=thin, tolerance=3))
        self.assertEqual(res["statistics"]["component_count"], 1)
        self.assertEqual(res["statistics"]["gap_cluster_count"], 0)

    def test_overlap_detected(self):
        # 步进 100、瓦片宽高 110 -> 相邻边-边重叠 10*110=1100
        res = diagnose(DiagnoseRequest(tiles=grid(1, 2, overlap=10)))
        self.assertEqual(res["statistics"]["overlap_area"], 1100.0)
        self.assertEqual(res["statistics"]["overlap_pair_count"], 1)
        self.assertAlmostEqual(res["overlap_pairs"][0]["intersection_area"], 1100.0)
        self.assertEqual(res["overlaps"][0]["tile_pair_count"], 1)
        self.assertTrue(res["summary"]["has_overlaps"])

    def test_reproducible_independent_of_order(self):
        tiles = grid(4, 4)
        shuffled = tiles[::-1]
        a = diagnose(DiagnoseRequest(tiles=tiles, tolerance=0.5, min_gap_area=1))
        b = diagnose(DiagnoseRequest(tiles=shuffled, tolerance=0.5, min_gap_area=1))
        self.assertEqual(
            json.dumps(a, sort_keys=True, ensure_ascii=False),
            json.dumps(b, sort_keys=True, ensure_ascii=False),
        )
        self.assertEqual(a["request_id"], b["request_id"])

    def test_invalid_tiles_reported(self):
        bad = [
            tile("x", 0, 0, 0, 0, w=0),
            tile("x", 1, 0, 0, 100),
        ]
        res = diagnose(DiagnoseRequest(tiles=bad))
        self.assertEqual(res["status"], "invalid")
        self.assertFalse(res["stitchable"])
        codes = {e["code"] for e in res["errors"]}
        self.assertIn("DEGENERATE_RECTANGLE", codes)
        self.assertIn("DUPLICATE_TILE_ID", codes)

    def test_min_gap_area_filter(self):
        # 内部缺口（4x4 抽掉中心 2x2）大于容差时必须报；
        # 容差桥接外框（缺口 100 宽 > tolerance=2），缺口仍存在。
        hole = grid(4, 4, skip={(1, 1), (1, 2), (2, 1), (2, 2)})
        res = diagnose(DiagnoseRequest(tiles=hole, tolerance=2, min_gap_area=50000))
        # 2x2 缺口面积 40000，阈值 50000 -> 滤除
        self.assertEqual(res["statistics"]["gap_cluster_count"], 0)
        self.assertTrue(res["stitchable"])
        res0 = diagnose(DiagnoseRequest(tiles=hole, tolerance=2, min_gap_area=0))
        self.assertGreater(res0["statistics"]["gap_cluster_count"], 0)
        self.assertFalse(res0["stitchable"])

    def test_resolution_warning(self):
        tiles = grid(1, 2)
        tiles[1]["resolution"] = 0.2  # 与中位数偏差 100%
        res = diagnose(DiagnoseRequest(tiles=tiles, resolution_rel_tol=0.02))
        self.assertTrue(any(w["code"] == "RESOLUTION_MISMATCH" for w in res["warnings"]))


if __name__ == "__main__":
    unittest.main()
