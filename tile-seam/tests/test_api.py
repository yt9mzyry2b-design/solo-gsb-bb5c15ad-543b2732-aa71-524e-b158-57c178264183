"""FastAPI 接口端到端测试（内存/临时 SQLite）。"""
import os
import tempfile
import unittest

os.environ["TILE_SEAM_DB"] = os.path.join(tempfile.mkdtemp(), "test-tasks.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def tile(tid, r, c, x, y, size=100):
    return dict(tile_id=tid, row=r, col=c, xmin=x, ymin=y,
                xmax=x + size, ymax=y + size, resolution=0.1, crs="EPSG:32650")


PERFECT = [tile(f"t{r}_{c}", r, c, c * 100, r * 100)
           for r in range(2) for c in range(2)]
GAP = [t for t in [tile(f"t{r}_{c}", r, c, c * 100, r * 100)
                   for r in range(3) for c in range(3)]
       if not (t["row"] == 1 and t["col"] == 1)]


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._cm = TestClient(app)
        cls.client = cls._cm.__enter__()
        cls.addClassCleanup(cls._cm.__exit__, None, None, None)

    def test_health_and_info(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")
        r = self.client.get("/api/v1")
        self.assertEqual(r.status_code, 200)

    def test_validate_single_tile(self):
        r = self.client.post("/api/v1/tiles/validate", json=tile("a", 0, 0, 0, 0))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["count"], 1)
        self.assertTrue(body["results"]["valid"])
        self.assertEqual(body["results"]["normalized_bbox"], [0.0, 0.0, 100.0, 100.0])

    def test_validate_batch_with_bad_tile(self):
        good = tile("ok", 0, 0, 0, 0)
        bad = dict(good)
        bad.update(tile_id="bad", xmax=0)
        r = self.client.post("/api/v1/tiles/validate", json={"tiles": [good, bad]})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["valid_count"], 1)
        self.assertIsInstance(body["results"], list)
        self.assertFalse(body["results"][1]["valid"])

    def test_validate_schema_error_422(self):
        r = self.client.post("/api/v1/tiles/validate", json={"tiles": [{"tile_id": "x"}]})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["results"]["valid"])

    def test_diagnose_persist_list_get_export_delete(self):
        payload = {"tiles": GAP, "tolerance": 0.5}
        r = self.client.post("/api/v1/diagnose", json=payload)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["stitchable"])
        task_id = body["task_id"]
        self.assertTrue(task_id.startswith("task_"))

        # 幂等：相同输入 -> 相同 task_id
        r2 = self.client.post("/api/v1/diagnose", json=payload)
        self.assertEqual(r2.json()["task_id"], task_id)

        r = self.client.get("/api/v1/tasks")
        self.assertTrue(any(t["id"] == task_id for t in r.json()["tasks"]))

        r = self.client.get(f"/api/v1/tasks/{task_id}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["id"], task_id)
        self.assertEqual(r.json()["result"]["request_id"], body["request_id"])

        r = self.client.get(f"/api/v1/tasks/{task_id}/export?format=geojson")
        self.assertEqual(r.status_code, 200)
        gj = r.json()
        self.assertEqual(gj["type"], "FeatureCollection")
        layers = {f["properties"]["layer"] for f in gj["features"]}
        self.assertIn("tiles", layers)
        self.assertIn("gaps", layers)
        self.assertIn("components", layers)
        gap_features = [f for f in gj["features"] if f["properties"]["layer"] == "gaps"]
        self.assertEqual(len(gap_features), 1)
        self.assertEqual(gap_features[0]["geometry"]["type"], "Polygon")

        r = self.client.get(f"/api/v1/tasks/{task_id}/export?format=json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["request_id"], body["request_id"])

        r = self.client.delete(f"/api/v1/tasks/{task_id}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["deleted"], task_id)

        r = self.client.get(f"/api/v1/tasks/{task_id}")
        self.assertEqual(r.status_code, 404)

    def test_preview_not_persisted(self):
        before = self.client.get("/api/v1/tasks").json()
        r = self.client.post("/api/v1/diagnose/preview", json={"tiles": PERFECT})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["stitchable"])
        self.assertNotIn("task_id", r.json())
        after = self.client.get("/api/v1/tasks").json()
        self.assertEqual(len(before["tasks"]), len(after["tasks"]))

    def test_diagnose_schema_error(self):
        r = self.client.post("/api/v1/diagnose", json={"tiles": []})
        self.assertEqual(r.status_code, 422)

    def test_export_unknown_task(self):
        r = self.client.get("/api/v1/tasks/task_nonexistent/export")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
