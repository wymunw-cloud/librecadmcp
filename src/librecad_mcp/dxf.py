"""Read DXF files with ezdxf so the model can inspect what a drawing contains.

LibreCAD exposes no API for reading entities from a live document, so the
practical route is: save the drawing (LibreCAD writes DXF), then parse the file.
"""

from __future__ import annotations

import math
import os
from typing import Any


def _r(v: float, nd: int = 4) -> float:
    return round(float(v), nd)


def _xy(p) -> list[float]:
    return [_r(p[0]), _r(p[1])]


def describe_entity(e) -> dict[str, Any]:
    t = e.dxftype()
    d: dict[str, Any] = {"type": t, "layer": e.dxf.layer, "handle": e.dxf.handle}
    color = e.dxf.get("color", None)
    if color not in (None, 256):
        d["color"] = color
    try:
        if t == "LINE":
            s, t2 = e.dxf.start, e.dxf.end
            d["start"], d["end"] = _xy(s), _xy(t2)
            d["length"] = _r(math.hypot(t2.x - s.x, t2.y - s.y))
        elif t == "CIRCLE":
            d["center"], d["radius"] = _xy(e.dxf.center), _r(e.dxf.radius)
        elif t == "ARC":
            d["center"], d["radius"] = _xy(e.dxf.center), _r(e.dxf.radius)
            d["start_angle"], d["end_angle"] = _r(e.dxf.start_angle), _r(e.dxf.end_angle)
        elif t == "ELLIPSE":
            d["center"], d["major_axis"] = _xy(e.dxf.center), _xy(e.dxf.major_axis)
            d["ratio"] = _r(e.dxf.ratio)
        elif t in ("LWPOLYLINE",):
            pts = [[_r(p[0]), _r(p[1])] for p in e.get_points("xy")]
            d["points"], d["closed"] = pts, bool(e.closed)
        elif t == "POLYLINE":
            pts = [_xy(v.dxf.location) for v in e.vertices]
            d["points"], d["closed"] = pts, bool(e.is_closed)
        elif t == "POINT":
            d["location"] = _xy(e.dxf.location)
        elif t in ("TEXT", "MTEXT"):
            d["text"] = e.dxf.text if t == "TEXT" else e.text
            d["insert"] = _xy(e.dxf.insert)
            d["height"] = _r(e.dxf.height if t == "TEXT" else e.dxf.char_height)
        elif t == "INSERT":
            d["block"], d["insert"] = e.dxf.name, _xy(e.dxf.insert)
        elif t.startswith("DIM"):
            d["dimension_text"] = e.dxf.get("text", "")
        elif t == "SPLINE":
            d["control_points"] = len(list(e.control_points))
        elif t == "HATCH":
            d["pattern"] = e.dxf.pattern_name
    except Exception as exc:  # keep going even for odd entities
        d["error"] = str(exc)
    return d


def read_dxf(path: str, max_entities: int = 500) -> dict[str, Any]:
    """Summarize a DXF file: layers, entity counts, extents and entity details."""
    import ezdxf  # lazy import: heavy

    if not os.path.exists(path):
        raise FileNotFoundError(path)
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    entities = list(msp)
    counts: dict[str, int] = {}
    for e in entities:
        counts[e.dxftype()] = counts.get(e.dxftype(), 0) + 1
    details = [describe_entity(e) for e in entities[:max_entities]]
    xs: list[float] = []
    ys: list[float] = []
    for d in details:
        for key in ("start", "end", "center", "location", "insert"):
            if key in d:
                xs.append(d[key][0]); ys.append(d[key][1])
        for p in d.get("points", []):
            xs.append(p[0]); ys.append(p[1])
        if "radius" in d and "center" in d:
            cx, cy, r = d["center"][0], d["center"][1], d["radius"]
            xs += [cx - r, cx + r]; ys += [cy - r, cy + r]
    extents = None
    if xs and ys:
        extents = {"min": [_r(min(xs)), _r(min(ys))], "max": [_r(max(xs)), _r(max(ys))]}
    layers = []
    for layer in doc.layers:
        layers.append({"name": layer.dxf.name, "color": layer.dxf.color,
                       "on": not layer.is_off(), "frozen": layer.is_frozen(), "locked": layer.is_locked()})
    return {
        "file": path,
        "dxf_version": doc.dxfversion,
        "units": doc.header.get("$INSUNITS", 0),
        "layers": layers,
        "entity_counts": counts,
        "entity_total": len(entities),
        "extents": extents,
        "entities": details,
        "truncated": len(entities) > max_entities,
    }
