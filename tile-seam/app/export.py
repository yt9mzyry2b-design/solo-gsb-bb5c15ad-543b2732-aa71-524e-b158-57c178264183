"""把诊断结果导出为 GeoJSON FeatureCollection（矩形多边形用 5 点闭合环）。"""
from __future__ import annotations

from typing import Any


def _rect_geometry(rect: list[float]) -> dict[str, Any]:
    x0, y0, x1, y1 = rect
    return {
        "type": "Polygon",
        "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]],
    }


def _multi_geometry(rects: list[list[float]]) -> dict[str, Any]:
    return {
        "type": "MultiPolygon",
        "coordinates": [_rect_geometry(r)["coordinates"] for r in rects],
    }


def to_geojson(result: dict[str, Any]) -> dict[str, Any]:
    """诊断 JSON -> GeoJSON FeatureCollection。

    图层：tiles（瓦片）/ gaps（缺口）/ overlaps（重叠区）/
    components（分块外接矩形）。结果不含时间戳，同样可复现。
    """
    features: list[dict[str, Any]] = []
    fc_id = result["request_id"]

    for t in result["tiles"]:
        features.append({
            "type": "Feature",
            "id": f"tile:{t['tile_id']}",
            "geometry": _rect_geometry([t["xmin"], t["ymin"], t["xmax"], t["ymax"]]),
            "properties": {
                "layer": "tiles",
                "tile_id": t["tile_id"],
                "row": t["row"],
                "col": t["col"],
                "resolution": t["resolution"],
                "crs": t["crs"],
                "component_id": t["component_id"],
                "issue_count": len(t["issues"]),
            },
        })

    for g in result["gaps"]:
        rects = g["polygon_rects"]
        features.append({
            "type": "Feature",
            "id": f"gap:{g['gap_id']}",
            "geometry": _multi_geometry(rects) if len(rects) != 1 else _rect_geometry(rects[0]),
            "properties": {
                "layer": "gaps",
                "gap_id": g["gap_id"],
                "component_id": g["component_id"],
                "area": g["area"],
                "polygon_truncated": g["polygon_truncated"],
            },
        })

    for o in result["overlaps"]:
        rects = o["polygon_rects"]
        features.append({
            "type": "Feature",
            "id": f"overlap:{o['cluster_id']}",
            "geometry": _multi_geometry(rects) if len(rects) != 1 else _rect_geometry(rects[0]),
            "properties": {
                "layer": "overlaps",
                "cluster_id": o["cluster_id"],
                "area": o["area"],
                "depth_max": o["depth_max"],
                "tile_pair_count": o["tile_pair_count"],
                "component_ids": o["component_ids"],
                "polygon_truncated": o["polygon_truncated"],
            },
        })

    for c in result["components"]:
        features.append({
            "type": "Feature",
            "id": f"component:{c['component_id']}",
            "geometry": _rect_geometry(c["bbox"]),
            "properties": {
                "layer": "components",
                "component_id": c["component_id"],
                "tile_count": c["tile_count"],
                "union_area": c["union_area"],
                "gap_area": c["gap_area"],
            },
        })

    fc: dict[str, Any] = {
        "type": "FeatureCollection",
        "name": "tile-seam-diagnosis",
        "request_id": fc_id,
        "schema_version": result["schema_version"],
        "features": features,
    }
    crs_values = result["statistics"].get("crs_values") or []
    if len(crs_values) == 1 and crs_values[0]:
        # GeoJSON 默认 CRS84；这里只声明源数据坐标系，不做重投影
        fc["source_crs"] = crs_values[0]
    return fc
