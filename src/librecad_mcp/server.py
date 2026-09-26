"""The LibreCAD MCP server.

Exposes drawing, editing, viewing and file tools that drive the running
LibreCAD window (see :mod:`librecad_mcp.controller`).  Run it with
``librecad-mcp serve`` (stdio transport) from Claude Desktop, Claude Code or
any other MCP client.
"""

from __future__ import annotations

import os
from typing import Annotated, Optional

from mcp.server.mcpserver import Image, MCPServer
from pydantic import Field

from . import commands as C
from . import dialogs
from .controller import CommandResult, LibreCAD, LibreCADError, LibreCADNotRunning
from .dxf import read_dxf

INSTRUCTIONS = """\
Tools for controlling a running LibreCAD (2D CAD) window on Windows.

Coordinates are drawing units with y pointing up; angles are degrees,
counter-clockwise.  Draw with the draw_* tools; use run_commands for anything
else (see the librecad://commands resource or get_command_reference).  Editing
tools act on the current selection: call select_all first, then
move/rotate/scale/mirror/delete.  Call zoom("auto") after drawing so the user
sees the result, and screenshot to verify visually.  read_drawing parses the
saved DXF file, so save_drawing first if the drawing has unsaved changes.
If LibreCAD is not running, launch_librecad starts it.
"""

server = MCPServer(
    "librecad",
    instructions=INSTRUCTIONS,
    version="0.1.0",
)

_lc = LibreCAD()

Num = Annotated[float, Field(description="drawing units")]
Points = Annotated[list[list[float]], Field(description="list of [x, y] points in drawing units")]


class ToolFailure(RuntimeError):
    """Raised for user-facing failures; MCP reports it as a tool error."""


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except LibreCADNotRunning as exc:
        raise ToolFailure(f"{exc} (use launch_librecad to start it)") from exc
    except FileNotFoundError as exc:
        raise ToolFailure(f"File not found: {exc}") from exc
    except LibreCADError as exc:
        raise ToolFailure(str(exc)) from exc


def _cmds(commands: list[str], finish: bool = True) -> str:
    return _run(_lc.run, commands, finish=finish).summary()


def _summary(r: CommandResult) -> str:
    return r.summary()


# --- status and window -----------------------------------------------------

@server.tool(description="Report LibreCAD's state: whether it runs, the active drawing and file, "
                         "unsaved changes, the command prompt, selection count, current layer and "
                         "any open dialog. Call this first when unsure what is going on.")
def get_status() -> dict:
    if not _lc.is_running():
        return {"running": False, "hint": "LibreCAD is not running; call launch_librecad or start it."}
    st = _run(_lc.status)
    st["unsaved_changes"] = "*" in st.get("active_document", "")
    return st


@server.tool(description="Start LibreCAD if it is not already running and wait for its window.")
def launch_librecad() -> str:
    info = _run(_lc.launch)
    return f"LibreCAD is running: {info.title}"


@server.tool(description="Restore the LibreCAD window if it is minimized; with activate=true also bring "
                         "it to the foreground so the user sees it.")
def show_window(activate: bool = False) -> str:
    _run(_lc.show, activate=activate)
    return "LibreCAD window shown" + (" and activated." if activate else ".")


@server.tool(description="Take a screenshot of the LibreCAD window (PNG) to see the drawing, the "
                         "command history and the status bar.")
def screenshot() -> Image:
    png = _run(_lc.screenshot_png)
    return Image(data=png, format="png")


@server.tool(description="Return the last lines of LibreCAD's command history (echoed commands, "
                         "prompts and error messages such as 'Unknown command').")
def command_history(lines: Annotated[int, Field(ge=1, le=500)] = 40) -> str:
    text = _run(_lc.history_text)
    return "\n".join(text.splitlines()[-lines:])


# --- raw commands ------------------------------------------------------------

@server.tool(description="Send raw LibreCAD command-line inputs in order, e.g. "
                         "['li', '0,0', '100,50'] or ['ci', '50,25', '70,25']. Coordinates: 'x,y', "
                         "relative '@dx,dy', polar 'r<angle'. finish=true appends 'k' (kill all "
                         "actions) so no action is left waiting. See get_command_reference.")
def run_commands(commands: Annotated[list[str], Field(min_length=1)], finish: bool = True) -> str:
    return _cmds(commands, finish=finish)


@server.tool(description="Reference of LibreCAD's command-line vocabulary and coordinate syntax.")
def get_command_reference() -> str:
    return C.COMMAND_REFERENCE


@server.resource("librecad://commands", name="LibreCAD command reference", mime_type="text/plain",
                 description="Command-line vocabulary and coordinate syntax accepted by run_commands.")
def commands_resource() -> str:
    return C.COMMAND_REFERENCE


# --- drawing -----------------------------------------------------------------

@server.tool(description="Draw a straight line from (x1, y1) to (x2, y2).")
def draw_line(x1: Num, y1: Num, x2: Num, y2: Num) -> str:
    return _cmds(C.line([(x1, y1), (x2, y2)]))


@server.tool(description="Draw connected line segments through the points (separate line entities). "
                         "close=true adds a segment back to the first point.")
def draw_lines(points: Points, close: bool = False) -> str:
    return _cmds(C.line(points, close=close))


@server.tool(description="Draw one polyline entity through the points; close=true closes it.")
def draw_polyline(points: Points, close: bool = False) -> str:
    return _cmds(C.polyline(points, close=close))


@server.tool(description="Draw an axis-aligned rectangle (four lines) between two opposite corners.")
def draw_rectangle(x1: Num, y1: Num, x2: Num, y2: Num) -> str:
    return _cmds(C.rectangle(x1, y1, x2, y2))


@server.tool(description="Draw a circle from its centre and radius.")
def draw_circle(cx: Num, cy: Num, radius: Annotated[float, Field(gt=0)]) -> str:
    return _cmds(C.circle(cx, cy, radius))


@server.tool(description="Draw an arc from centre, radius, start and end angle (degrees, "
                         "counter-clockwise from +x). clockwise=true draws the other way round.")
def draw_arc(cx: Num, cy: Num, radius: Annotated[float, Field(gt=0)],
             start_angle: Annotated[float, Field(description="degrees")],
             end_angle: Annotated[float, Field(description="degrees")], clockwise: bool = False) -> str:
    return _cmds(C.arc(cx, cy, radius, start_angle, end_angle, clockwise=clockwise))


@server.tool(description="Draw an arc through three points: start, a point on the arc, end.")
def draw_arc_3p(x1: Num, y1: Num, x2: Num, y2: Num, x3: Num, y3: Num) -> str:
    return _cmds(C.arc_3p((x1, y1), (x2, y2), (x3, y3)))


@server.tool(description="Draw a full ellipse: centre, major half-axis vector (major_dx, major_dy) "
                         "and the minor half-axis length.")
def draw_ellipse(cx: Num, cy: Num, major_dx: Num, major_dy: Num,
                 minor_length: Annotated[float, Field(gt=0)]) -> str:
    return _cmds(C.ellipse(cx, cy, major_dx, major_dy, minor_length))


@server.tool(description="Draw a regular polygon (closed polyline) inscribed in a circle of the given "
                         "radius; rotation rotates the first vertex from +x (degrees).")
def draw_polygon(cx: Num, cy: Num, radius: Annotated[float, Field(gt=0)],
                 sides: Annotated[int, Field(ge=3, le=360)], rotation: float = 0.0) -> str:
    return _cmds(C.polygon(cx, cy, radius, sides, rotation))


@server.tool(description="Draw a point entity.")
def draw_point(x: Num, y: Num) -> str:
    return _cmds(C.point(x, y))


@server.tool(description="Insert text (MText) with its insertion point at (x, y); height in drawing "
                         "units (default: LibreCAD's last used height). Newlines make multiple lines.")
def draw_text(text: Annotated[str, Field(min_length=1)], x: Num, y: Num,
              height: Optional[Annotated[float, Field(gt=0)]] = None) -> str:
    return _summary(_run(dialogs.insert_text, _lc, text, x, y, height))


@server.tool(description="Add a dimension between (x1, y1) and (x2, y2); kind is 'linear' (aligned "
                         "with the points), 'horizontal' or 'vertical'. (lx, ly) is a point on the "
                         "dimension line and sets its offset from the measured points.")
def draw_dimension(kind: Annotated[str, Field(pattern="^(linear|aligned|horizontal|vertical)$")],
                   x1: Num, y1: Num, x2: Num, y2: Num, lx: Num, ly: Num) -> str:
    return _cmds(C.dimension(kind, (x1, y1), (x2, y2), (lx, ly)))


# --- selection and editing --------------------------------------------------

@server.tool(description="Select every entity in the drawing (needed before the *_selected tools).")
def select_all() -> str:
    r = _run(_lc.run, C.select_all())
    st = _run(_lc.status)
    return f"{r.summary()}\nselected: {st.get('selected') or '0'}, total length: {st.get('total_length') or '0'}"


@server.tool(description="Clear the selection.")
def deselect_all() -> str:
    return _cmds(C.deselect_all(), finish=False)


@server.tool(description="Delete the selected entities (select_all first to clear the drawing).")
def delete_selected() -> str:
    return _summary(_run(dialogs.delete_selected, _lc))


@server.tool(description="Move the selected entities by (dx, dy). copies=0 moves them; copies>=1 keeps "
                         "the original and makes that many copies.")
def move_selected(dx: Num, dy: Num, copies: Annotated[int, Field(ge=0, le=100)] = 0) -> str:
    return _summary(_run(dialogs.move_selected, _lc, dx, dy, copies))


@server.tool(description="Rotate the selected entities by angle (degrees, counter-clockwise) around "
                         "(cx, cy). copies>=1 keeps the original and makes copies.")
def rotate_selected(cx: Num, cy: Num, angle: Annotated[float, Field(description="degrees")],
                    copies: Annotated[int, Field(ge=0, le=100)] = 0) -> str:
    return _summary(_run(dialogs.rotate_selected, _lc, cx, cy, angle, copies))


@server.tool(description="Scale the selected entities about (cx, cy) by factor (factor_y for "
                         "non-uniform scaling). copies>=1 keeps the original.")
def scale_selected(cx: Num, cy: Num, factor: Annotated[float, Field(gt=0)],
                   factor_y: Optional[Annotated[float, Field(gt=0)]] = None,
                   copies: Annotated[int, Field(ge=0, le=100)] = 0) -> str:
    return _summary(_run(dialogs.scale_selected, _lc, cx, cy, factor, factor_y, copies))


@server.tool(description="Mirror the selected entities across the line through (x1, y1) and (x2, y2). "
                         "keep_original=false moves them instead of copying.")
def mirror_selected(x1: Num, y1: Num, x2: Num, y2: Num, keep_original: bool = True) -> str:
    return _summary(_run(dialogs.mirror_selected, _lc, x1, y1, x2, y2, keep_original))


@server.tool(description="Undo the last drawing operation(s).")
def undo(times: Annotated[int, Field(ge=1, le=50)] = 1) -> str:
    return _cmds(C.undo(times), finish=False)


@server.tool(description="Redo previously undone operation(s).")
def redo(times: Annotated[int, Field(ge=1, le=50)] = 1) -> str:
    return _cmds(C.redo(times), finish=False)


@server.tool(description="Change the view: mode 'auto' fits the whole drawing, 'window' zooms to the "
                         "rectangle (x1, y1)-(x2, y2), 'previous' restores the last view, 'redraw' "
                         "repaints.")
def zoom(mode: Annotated[str, Field(pattern="^(auto|window|previous|redraw)$")] = "auto",
         x1: float = 0, y1: float = 0, x2: float = 0, y2: float = 0) -> str:
    if mode == "auto":
        cmds = C.zoom_auto()
    elif mode == "window":
        cmds = C.zoom_window(x1, y1, x2, y2)
    elif mode == "previous":
        cmds = C.zoom_previous()
    else:
        cmds = C.redraw()
    return _cmds(cmds, finish=False)


# --- files -------------------------------------------------------------------

@server.tool(description="Create a new, empty drawing (becomes the active document).")
def new_drawing() -> str:
    name = _run(dialogs.new_drawing, _lc)
    return f"New drawing created: {name}"


@server.tool(description="Open a DXF/DWG file in LibreCAD (absolute path).")
def open_drawing(path: str) -> str:
    p = _run(dialogs.open_file, _lc, path)
    return f"Opened {p}; active document: {_lc.active_document()}"


@server.tool(description="Save the active drawing. With a path, 'Save As' to that .dxf file; without, "
                         "save in place (only works if the drawing already has a file).")
def save_drawing(path: Optional[str] = None) -> str:
    if path:
        p = _run(dialogs.save_as, _lc, path)
    else:
        p = _run(dialogs.save, _lc)
    return f"Saved: {p}"


@server.tool(description="Read a DXF file with ezdxf and summarize its layers, entity counts, extents "
                         "and entities (type, layer, geometry). Without a path, reads the active "
                         "drawing's saved file; save_drawing first if there are unsaved changes.")
def read_drawing(path: Optional[str] = None,
                 max_entities: Annotated[int, Field(ge=1, le=5000)] = 300) -> dict:
    if not path:
        if not _lc.is_running():
            raise ToolFailure("LibreCAD is not running and no path was given.")
        path = _run(_lc.current_file)
        if not path:
            raise ToolFailure("The active drawing has never been saved; call save_drawing with a path "
                              "first, or pass a path.")
        if not os.path.isabs(path):
            raise ToolFailure(f"The active drawing is '{path}' but its folder is unknown; pass the full "
                              "path or save_drawing to a known path.")
    if not os.path.exists(path):
        raise ToolFailure(f"File not found: {path}")
    data = read_dxf(path, max_entities=max_entities)
    if _lc.is_running() and _lc.has_unsaved_changes():
        data["warning"] = "LibreCAD has unsaved changes; this reflects the file on disk."
    return data


# --- prompt --------------------------------------------------------------------

@server.prompt(name="drafting_assistant", description="Guidelines for drafting in LibreCAD with these tools.")
def drafting_assistant(task: str = "") -> str:
    return (
        "You control LibreCAD through MCP tools. Work in drawing units (y up, angles in degrees "
        "counter-clockwise). Plan the geometry, draw it with the draw_* tools, then zoom('auto') and, "
        "for anything non-trivial, screenshot to verify. Use select_all + the *_selected tools to edit, "
        "and undo to fix mistakes.\n\nTask: " + (task or "(describe what to draw)")
    )


def run_server() -> None:
    server.run("stdio")


if __name__ == "__main__":
    run_server()
