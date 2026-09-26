# LibreCAD MCP

Control [LibreCAD](https://github.com/LibreCAD/LibreCAD) with natural language.

This project has two parts:

* **An MCP server** (`librecad-mcp serve`) that exposes drawing, editing, viewing
  and file tools to any Model Context Protocol client (Claude Desktop, Claude
  Code, ...). The tools drive the LibreCAD window that is already running on
  your Windows PC.
* **A prompt bar** (`librecad-mcp bar`) that docks to the bottom of the
  LibreCAD window. Type "draw a 100×50 plate with four M6 holes" and press
  Enter; the bar runs the request through Claude with the MCP tools and shows
  what it does.

LibreCAD itself is not modified and needs no plugin: everything works with the
stock LibreCAD 2.2.x installer.

## How it works

LibreCAD has no scripting API, but it is a Qt application and Qt publishes its
widgets through Windows UI Automation. The controller

1. finds LibreCAD's main window and its command line (`QG_CommandEdit`) and
   command history (`QG_CommandHistory`) through UI Automation,
2. writes text into the command line and posts a Return key message to the
   window (Qt delivers posted keys to the widget that last had focus inside the
   window, so this works while LibreCAD is behind other windows or minimized,
   and normally without stealing your keyboard focus),
3. reads the command history, the prompt label ("Specify first point") and the
   status bar back to report what happened,
4. drives the few dialogs the command line cannot answer (text, move/rotate/
   scale/mirror options, file dialogs) through UI Automation as well.

LibreCAD's command line accepts several inputs separated by `;`, so a whole
shape is usually one round trip (about 0.1 s). A 2D drawing can be read back by
saving it and parsing the DXF with [ezdxf](https://ezdxf.mozman.at/).

## Requirements

* Windows 10/11
* LibreCAD 2.2.x (tested with 2.2.1, Qt 5.15) installed and **running**, with
  the Command Line dock visible (Widgets > Dock Widgets > Command Line) and the
  default File toolbar (New/Open/Save) visible
* Python 3.10+ ([uv](https://docs.astral.sh/uv/) recommended)
* For the prompt bar: [Claude Code](https://claude.com/claude-code) installed
  and logged in from a terminal (`claude` on PATH; run `claude` once and use
  `/login` if it says "Not logged in", even if the Claude desktop app is signed
  in). The bar uses `claude -p`, so it needs no API key.

## Install

```bash
git clone https://github.com/wymunw-cloud/librecadmcp
cd librecadmcp
uv sync
```

Check that the controller sees LibreCAD:

```bash
uv run librecad-mcp status
uv run librecad-mcp run "li;0,0;100,50;k"
```

## Use with Claude Code

```bash
claude mcp add librecad -- uv --directory "C:\path\to\librecad-mcp" run librecad-mcp serve
```

Then, in Claude Code: *"In LibreCAD, draw a 200×100 rectangle with a 40 mm
circle in the centre and dimension the width."*

## Use with Claude Desktop

Add to `%APPDATA%\Claude\claude_desktop_config.json` (or run
`uv run librecad-mcp config` to print a snippet with the exact interpreter
path):

```json
{
  "mcpServers": {
    "librecad": {
      "command": "uv",
      "args": ["--directory", "C:\\path\\to\\librecad-mcp", "run", "librecad-mcp", "serve"]
    }
  }
}
```

## The prompt bar

```bash
uv run librecad-mcp bar
```

A dark bar appears along the bottom of the LibreCAD window and follows it
around. Type a request and press Enter. The status line shows each tool call as
it happens; **Log ▴** opens a transcript, **New chat** forgets the conversation
(otherwise follow-ups like "make it twice as big" refer to the previous
request), **Stop** kills a running request.

* Lines starting with `>` bypass Claude and go straight to LibreCAD's command
  line: `> ci;50,25;70,25;k`
* `--position below` puts the bar under the window instead of overlaying its
  status bar; drag the `⋮⋮` grip to move it, double-click the grip to re-dock.
* `--model claude-opus-5` (or any model name) is passed to `claude -p`.
* Press Enter with an empty prompt to start LibreCAD when it is not running.

## Tools

| Tool | What it does |
|---|---|
| `get_status` | running?, active drawing, file, unsaved changes, prompt, selection count, layer, dialogs |
| `launch_librecad`, `show_window`, `screenshot`, `command_history` | process/window helpers and feedback |
| `run_commands`, `get_command_reference` | raw LibreCAD command-line input (also the `librecad://commands` resource) |
| `draw_line`, `draw_lines`, `draw_polyline`, `draw_rectangle`, `draw_circle`, `draw_arc`, `draw_arc_3p`, `draw_ellipse`, `draw_polygon`, `draw_point`, `draw_text`, `draw_dimension` | geometry |
| `select_all`, `deselect_all`, `delete_selected`, `move_selected`, `rotate_selected`, `scale_selected`, `mirror_selected`, `undo`, `redo` | editing (the `*_selected` tools act on the current selection) |
| `zoom` | auto / window / previous / redraw |
| `new_drawing`, `open_drawing`, `save_drawing`, `read_drawing` | files; `read_drawing` parses the saved DXF with ezdxf |

Coordinates are drawing units with y up; angles are degrees counter-clockwise.

## Limitations

* Windows only (UI Automation + Win32 messages).
* Entity selection from the command line is all-or-nothing (`select_all`);
  picking individual entities needs the mouse. Work around it with `undo`, by
  drawing on separate layers, or by editing the DXF.
* Layer and pen changes are not exposed yet (they live in dialogs/dock widgets
  that are not automated).
* `read_drawing` reads the file on disk, so save first.
* Confirming a selection for `delete_selected` gives Qt keyboard focus to the
  drawing view inside LibreCAD; it does not activate the LibreCAD window.
* LibreCAD's `cr` circle command takes its radius from the toolbar, so
  `draw_circle` uses `ci` (centre + point on circle) instead.

## Development

```bash
uv sync --extra dev
uv run pytest                      # unit tests (no LibreCAD needed)
LIBRECAD_LIVE=1 uv run pytest -q   # live tests against a running LibreCAD
uv run librecad-mcp screenshot out.png
```

Layout: `controller.py` (window discovery, UIA, posted input, status,
screenshot), `commands.py` (command-line builders + reference), `dialogs.py`
(dialog automation), `dxf.py` (ezdxf reader), `server.py` (MCP tools),
`agent.py` + `bar.py` (prompt bar), `cli.py`.

## License

MIT
