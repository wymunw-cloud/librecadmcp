"""Unit tests for the command builders (no LibreCAD needed)."""

import math

import pytest

from librecad_mcp import commands as C
from librecad_mcp.controller import fmt


def test_fmt_numbers():
    assert fmt(0) == "0"
    assert fmt(-0.0) == "0"
    assert fmt(100) == "100"
    assert fmt(12.5) == "12.5"
    assert fmt(1 / 3) == "0.333333"
    assert fmt(-2.50) == "-2.5"
    assert "e" not in fmt(1e-7)


def test_point_and_polar():
    assert C.pt((10, 20)) == "10,20"
    assert C.rel(5, -5) == "@5,-5"
    assert C.polar(50, 45) == "50<45"
    assert C.polar(50, 45, relative=True) == "@50<45"
    with pytest.raises(ValueError):
        C.pt((1,))


def test_line_and_polyline():
    assert C.line([(0, 0), (100, 50)]) == ["li", "0,0", "100,50", "k"]
    assert C.line([(0, 0), (10, 0), (10, 10)], close=True, finish=False) == ["li", "0,0", "10,0", "10,10", "0,0"]
    assert C.polyline([(0, 0), (10, 0), (5, 5)], close=True) == ["pl", "0,0", "10,0", "5,5", "close", "k"]
    with pytest.raises(ValueError):
        C.line([(0, 0)])


def test_rectangle_circle_arc():
    assert C.rectangle(0, 0, 100, 50) == ["rect", "0,0", "100,50", "k"]
    assert C.rectangle_wh(10, 10, 20, 5) == ["rect", "10,10", "30,15", "k"]
    # ci = centre + point on circle (cr takes its radius from the toolbar, so it is avoided)
    assert C.circle(50, 25, 20) == ["ci", "50,25", "70,25", "k"]
    assert C.arc(0, 0, 10, 0, 90) == ["ar", "0,0", "10", "0", "90", "k"]
    assert C.arc(0, 0, 10, 0, 90, clockwise=True) == ["ar", "0,0", "10", "90", "0", "k"]
    with pytest.raises(ValueError):
        C.circle(0, 0, 0)


def test_polygon_is_closed_polyline():
    cmds = C.polygon(0, 0, 10, 4)
    assert cmds[0] == "pl" and cmds[-2:] == ["close", "k"]
    pts = cmds[1:-2]
    assert len(pts) == 4
    xs = [float(p.split(",")[0]) for p in pts]
    assert math.isclose(max(xs), 10) and math.isclose(min(xs), -10)


def test_dimension_kinds():
    assert C.dimension("horizontal", (0, 0), (100, 0), (50, -15))[0] == "dh"
    assert C.dimension("vertical", (0, 0), (0, 10), (5, 5))[0] == "dv"
    assert C.dimension("linear", (0, 0), (3, 4), (1, 1))[0] == "dl"
    with pytest.raises(ValueError):
        C.dimension("radial", (0, 0), (1, 1), (2, 2))


def test_simple_commands():
    assert C.select_all() == ["sa"]
    assert C.deselect_all() == ["tn"]
    assert C.undo(2) == ["u", "u"]
    assert C.redo() == ["r"]
    assert C.zoom_auto() == ["za"]
    assert C.zoom_window(0, 0, 10, 10) == ["zw", "0,0", "10,10"]


def test_reference_mentions_core_syntax():
    for token in ("@dx,dy", "r<angle", "escape", "li ", "sa ", "za "):
        assert token in C.COMMAND_REFERENCE
