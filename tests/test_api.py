"""API 端到端测试（FastAPI TestClient，使用临时 SQLite 库）。"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["DIAG_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="tile_diag_test_"), "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def sample_tiles():
    return [
        {"row": 0, "col": 0, "min_x": 0.0, "min_y": 0.0,
         "max_x": 1.0, "max_y": 1.0, "resolution": 0.05},
        {"row": 0, "col": 1, "min_x": 1.0, "min_y": 0.0,
         "max_x": 2.0, "max_y": 1.0, "resolution": 0.05},
        {"row": 1, "col": 0, "min_x": 0.0, "min_y": 1.0,
         "max_x": 1.0, "max_y": 2.0, "resolution": 0.05},
        # 故意缺失 (row=1, col=1)，制造 1x1 缺口
    ]


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_validate_tile_ok():
    resp = client.post("/api/v1/tiles/validate", json={
        "row": 0, "col": 0, "min_x": 0, "min_y": 0,
        "max_x": 100, "max_y": 100, "resolution": 0.5,
    })
    body = resp.json()
    assert resp.status_code == 200
    assert body["valid"] is True
    assert body["errors"] == []
    assert body["normalized"]["resolution"] == 0.5


def test_validate_tile_errors():
    resp = client.post("/api/v1/tiles/validate", json={
        "row": -1, "col": 0, "min_x": 10, "min_y": 0,
        "max_x": 5, "max_y": 1, "resolution": -2,
    })
    body = resp.json()
    assert body["valid"] is False
    assert any("row" in e for e in body["errors"])
    assert any("max_x" in e for e in body["errors"])
    assert any("resolution" in e for e in body["errors"])


def test_validate_tile_missing_field():
    resp = client.post("/api/v1/tiles/validate", json={"row": 0})
    body = resp.json()
    assert body["valid"] is False
    assert any("col" in e for e in body["errors"])


def test_diagnose_get_export_flow():
    resp = client.post("/api/v1/diagnostics", json={
        "job_name": "测试测区",
        "tolerance": 0.001,
        "tiles": sample_tiles(),
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "completed"
    assert len(body["checksum"]) == 64

    diag = body["diagnostic"]
    assert diag["summary"]["gap_count"] == 1
    assert diag["summary"]["gap_area"] == 1.0
    assert diag["summary"]["component_count"] == 1
    assert diag["summary"]["seamless"] is False

    task_id = body["task_id"]

    # 查询
    got = client.get(f"/api/v1/diagnostics/{task_id}")
    assert got.status_code == 200
    assert got.json()["checksum"] == body["checksum"]
    assert got.json()["diagnostic"]["summary"] == diag["summary"]

    # 列表
    listed = client.get("/api/v1/diagnostics")
    assert listed.status_code == 200
    ids = [item["task_id"] for item in listed.json()["items"]]
    assert task_id in ids

    # 导出
    exported = client.get(f"/api/v1/diagnostics/{task_id}/export")
    assert exported.status_code == 200
    assert "attachment" in exported.headers["content-disposition"]
    doc = exported.json()
    assert doc["task_id"] == task_id
    assert doc["input"]["tolerance"] == 0.001
    assert doc["diagnostic"]["input_hash"] == diag["input_hash"]


def test_diagnose_reproducible_checksum():
    payload = {"tolerance": 0.0, "tiles": sample_tiles()}
    a = client.post("/api/v1/diagnostics", json=payload).json()
    b = client.post("/api/v1/diagnostics",
                    json={"tolerance": 0.0,
                          "tiles": list(reversed(sample_tiles()))}).json()
    assert a["checksum"] == b["checksum"]
    assert a["diagnostic"]["input_hash"] == b["diagnostic"]["input_hash"]


def test_get_missing_task_404():
    assert client.get("/api/v1/diagnostics/nonexistent").status_code == 404
    assert client.get(
        "/api/v1/diagnostics/nonexistent/export").status_code == 404


def test_diagnose_invalid_tile_422():
    resp = client.post("/api/v1/diagnostics", json={
        "tiles": [{"row": 0, "col": 0, "min_x": 0, "min_y": 0,
                   "max_x": 0, "max_y": 1}],
    })
    assert resp.status_code == 422
