"""Builders that translate geometric requests into LibreCAD 2.2 command-line input.

LibreCAD's command line understands:

* commands and their aliases, e.g. ``li`` (line), ``cr`` (circle centre+radius);
* absolute coordinates ``x,y``;
* relative coordinates ``@dx,dy`` (relative to the last point / relative zero);
* absolute polar ``r<angle`` and relative polar ``@r<angle`` (degrees);
* plain numbers (radius, angle, length) and arithmetic expressions;
* action keywords such as ``close``/``undo`` (polyline) and ``escape``;
* ``k`` (kill all actions) to end whatever is running and clear the selection.

Several inputs can be joined with ``;`` on one line.  Builders below return a
list of individual inputs; :meth:`librecad_mcp.controller.LibreCAD.run` joins
them.  Every builder finishes its action with ``k`` unless told otherwise, so
LibreCAD is left idle and predictable afterwards.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from .controller import fmt

Point = Sequence[float]


def pt(p: Point) -> str:
    """Format an (x, y) pair as an absolute coordinate."""
    if len(p) < 2:
        raise ValueError(f"point needs two coordinates, got {p!r}")
    return f"{fmt(p[0])},{fmt(p[1])}"


def rel(dx: float, dy: float) -> str:
    return f"@{fmt(dx)},{fmt(dy)}"


def polar(r: float, angle_deg: float, relative: bool = False) -> str:
    return f"{'@' if relative else ''}{fmt(r)}<{fmt(angle_deg)}"


def _finish(cmds: list[str], finish: bool) -> list[str]:
    if finish:
        cmds.append("k")
    return cmds


# --- drawing -----------------------------------------------------------------

def line(points: Iterable[Point], close: bool = False, finish: bool = True) -> list[str]:
    """Connected line segments through ``points`` (2 or more)."""
    pts = [pt(p) for p in points]
    if len(pts) < 2:
        raise ValueError("a line needs at least two points")
    cmds = ["li", *pts]
    if close and len(pts) > 2:
        cmds.append(pts[0])
    return _finish(cmds, finish)


def segments(pairs: Iterable[tuple[Point, Point]], finish: bool = True) -> list[str]:
    """Independent line segments, each given as ((x1, y1), (x2, y2))."""
    cmds: list[str] = []
    for a, b in pairs:
        cmds += ["li", pt(a), pt(b), "escape", "escape"]
    return _finish(cmds, finish)


def polyline(points: Iterable[Point], close: bool = False, finish: bool = True) -> list[str]:
    """A single polyline entity through ``points``; ``close`` closes it."""
    pts = [pt(p) for p in points]
    if len(pts) < 2:
        raise ValueError("a polyline needs at least two points")
    cmds = ["pl", *pts]
    if close:
        cmds.append("close")
    return _finish(cmds, finish)


def rectangle(x1: float, y1: float, x2: float, y2: float, finish: bool = True) -> list[str]:
    """Axis-aligned rectangle (four lines) from two opposite corners."""
    return _finish(["rect", pt((x1, y1)), pt((x2, y2))], finish)


def rectangle_wh(x: float, y: float, width: float, height: float, finish: bool = True) -> list[str]:
    return rectangle(x, y, x + width, y + height, finish)


def circle(cx: float, cy: float, radius: float, finish: bool = True) -> list[str]:
    """Circle by centre and radius.

    Uses ``ci`` (centre + point on the circle): the ``cr`` command takes its
    radius from the options toolbar rather than from typed input.
    """
    if radius <= 0:
        raise ValueError("radius must be positive")
    return _finish(["ci", pt((cx, cy)), pt((cx + radius, cy))], finish)


def circle_2p(p1: Point, p2: Point, finish: bool = True) -> list[str]:
    """Circle through two diametrically opposite points."""
    return _finish(["c2", pt(p1), pt(p2)], finish)


def circle_3p(p1: Point, p2: Point, p3: Point, finish: bool = True) -> list[str]:
    return _finish(["c3", pt(p1), pt(p2), pt(p3)], finish)


def arc(cx: float, cy: float, radius: float, start_angle: float, end_angle: float,
        clockwise: bool = False, finish: bool = True) -> list[str]:
    """Arc by centre, radius, start and end angle (degrees, counter-clockwise).

    LibreCAD draws arcs counter-clockwise from the start to the end angle;
    ``clockwise=True`` swaps the angles to draw the complementary direction.
    """
    if radius <= 0:
        raise ValueError("radius must be positive")
    a1, a2 = (end_angle, start_angle) if clockwise else (start_angle, end_angle)
    return _finish(["ar", pt((cx, cy)), fmt(radius), fmt(a1), fmt(a2)], finish)


def arc_3p(p1: Point, p2: Point, p3: Point, finish: bool = True) -> list[str]:
    """Arc through three points (start, a point on the arc, end)."""
    return _finish(["a3", pt(p1), pt(p2), pt(p3)], finish)


def ellipse(cx: float, cy: float, major_dx: float, major_dy: float, minor_length: float,
            finish: bool = True) -> list[str]:
    """Full ellipse: centre, end point of the major axis (relative to the centre),
    and the half-length of the minor axis."""
    if minor_length <= 0:
        raise ValueError("minor_length must be positive")
    return _finish(["ea", pt((cx, cy)), pt((cx + major_dx, cy + major_dy)), fmt(minor_length)], finish)


def point(x: float, y: float, finish: bool = True) -> list[str]:
    return _finish(["po", pt((x, y))], finish)


def polygon(cx: float, cy: float, radius: float, sides: int, rotation_deg: float = 0.0,
            finish: bool = True) -> list[str]:
    """Regular polygon as a closed polyline inscribed in a circle of ``radius``."""
    import math

    if sides < 3:
        raise ValueError("a polygon needs at least 3 sides")
    pts = []
    for i in range(sides):
        a = math.radians(rotation_deg + 360.0 * i / sides)
        pts.append((cx + radius * math.cos(a), cy + radius * math.sin(a)))
    return polyline(pts, close=True, finish=finish)


def dimension(kind: str, p1: Point, p2: Point, location: Point, finish: bool = True) -> list[str]:
    """Linear dimension: ``kind`` is ``linear`` (aligned to the points), ``horizontal`` or ``vertical``.

    ``location`` is a point on the dimension line (controls its offset).
    """
    cmd = {"linear": "dl", "aligned": "dl", "horizontal": "dh", "vertical": "dv"}.get(kind.lower())
    if cmd is None:
        raise ValueError("kind must be linear, horizontal or vertical")
    return _finish([cmd, pt(p1), pt(p2), pt(location)], finish)


# --- editing -------------------------------------------------------------------

def select_all() -> list[str]:
    return ["sa"]


def deselect_all() -> list[str]:
    return ["tn"]


def undo(times: int = 1) -> list[str]:
    return ["u"] * max(1, int(times))


def redo(times: int = 1) -> list[str]:
    return ["r"] * max(1, int(times))


def zoom_auto() -> list[str]:
    return ["za"]


def zoom_window(x1: float, y1: float, x2: float, y2: float) -> list[str]:
    return ["zw", pt((x1, y1)), pt((x2, y2))]


def zoom_previous() -> list[str]:
    return ["zv"]


def redraw() -> list[str]:
    return ["rg"]


def set_relative_zero(x: float, y: float) -> list[str]:
    return ["rz", pt((x, y))]


# --- reference text for the model ---------------------------------------------

COMMAND_REFERENCE = """\
LibreCAD 2.2 command-line reference (what run_commands accepts)
================================================================
Input syntax
  x,y            absolute coordinates          e.g. 100,50
  @dx,dy         relative to the last point    e.g. @0,25
  r<angle        absolute polar (degrees)      e.g. 50<45
  @r<angle       relative polar (degrees)      e.g. @30<90
  number         a radius / angle / length     e.g. 12.5  or  2*pi
  0              shortcut for the origin 0,0
  a;b;c          several inputs on one line (run_commands does this for you)
  escape         step back / cancel the current action (acts like a right-click)
  k              kill all actions and clear the selection (use to finish)
  Avoid '=' and backslashes: the command line treats them as variables.

Drawing (type the command, then answer its prompts with coordinates/numbers)
  li  line            points... (keeps asking; end with k or escape)
  pl  polyline        points..., then 'close' to close, 'undo' removes last node
  rect rectangle      first corner, second corner (draws 4 lines)
  rect2               rectangle by 2 points as a polyline
  cr  circle          centre, radius
  c2  circle 2 points diameter end points
  c3  circle 3 points three points on the circle
  ar  arc             centre, radius, start angle, end angle (counter-clockwise)
  a3  arc 3 points    start, point on arc, end
  ea  ellipse         centre, major-axis end point, minor axis length
  ae  ellipse arc     centre, major-axis end, minor length, start angle, end angle
  po  point           location
  mt  mtext / tx text (opens a dialog: use draw_text instead)
  ha  hatch           (needs a selection and a dialog: not scriptable here)
  la  angled line     ... ; lo perpendicular line ; pa parallel/offset line
  dl  dimension       first point, second point, dimension-line location (aligned)
  dh / dv             horizontal / vertical dimension: same three points
  ld  leader          points...

Selection and modification
  sa  select all      tn deselect all       is invert selection
  er  delete selected (needs the selection confirmed: use delete_selected)
  mv move  ro rotate  sz scale  mi mirror  (modify tools, need selection +
     confirmation and open an options dialog: use the move/rotate/scale/mirror tools)
  u   undo            r  redo
  tm trim  t2 multi trim  le lengthen  bev bevel  fi fillet  div divide
  xp explode block/polyline   xt explode text

View
  za zoom auto (fit)  zw zoom window (2 corners)  zv zoom previous
  zp zoom pan         rg redraw

Snap / restrict (toggle)
  sg grid  se endpoints  sm middle  sc centre  si intersection  sn on entity
  so free  rn no restriction  rr orthogonal  rh horizontal  rv vertical
  rz set relative zero (then a point)

Notes
  * Coordinates are in drawing units; angles in degrees, counter-clockwise.
  * Commands inside a running action are read as answers to its prompt, so
    always finish an action (k) before starting another one.
  * Anything LibreCAD does not understand shows up as 'Unknown command'.
"""
