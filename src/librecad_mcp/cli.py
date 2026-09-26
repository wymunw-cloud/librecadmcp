"""Command-line entry point: ``librecad-mcp <command>``."""

from __future__ import annotations

import argparse
import json
import sys
import warnings


def main(argv: list[str] | None = None) -> int:
    warnings.filterwarnings("ignore", category=SyntaxWarning)
    parser = argparse.ArgumentParser(
        prog="librecad-mcp",
        description="Control LibreCAD from an LLM: MCP server, prompt bar and helpers.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="run the MCP server over stdio (for Claude Desktop / Claude Code)")

    p_bar = sub.add_parser("bar", help="open the floating prompt bar docked to the LibreCAD window")
    p_bar.add_argument("--model", help="Claude model passed to `claude -p` (default: your CLI default)")
    p_bar.add_argument("--position", choices=["overlay", "below"], default="overlay",
                       help="overlay the bottom of the LibreCAD window (default) or sit below it")
    p_bar.add_argument("--width", type=int, help="fixed bar width in pixels")

    p_run = sub.add_parser("run", help="send raw LibreCAD command-line input, e.g. run 'li;0,0;100,50;k'")
    p_run.add_argument("commands", nargs="+", help="commands (use ; or separate arguments)")

    sub.add_parser("status", help="print what the controller sees in LibreCAD")

    p_shot = sub.add_parser("screenshot", help="save a PNG of the LibreCAD window")
    p_shot.add_argument("path", nargs="?", default="librecad.png")

    p_read = sub.add_parser("read", help="summarize a DXF file with ezdxf")
    p_read.add_argument("path")

    sub.add_parser("config", help="print the JSON snippet for Claude Desktop / Claude Code")
    sub.add_parser("launch", help="start LibreCAD if it is not running")

    args = parser.parse_args(argv)

    if args.cmd == "serve":
        from .server import run_server

        run_server()
        return 0

    if args.cmd == "bar":
        from .bar import main as bar_main

        bar_main(model=args.model, position=args.position, width=args.width)
        return 0

    if args.cmd == "config":
        print(json.dumps({
            "mcpServers": {
                "librecad": {"command": sys.executable, "args": ["-m", "librecad_mcp", "serve"]}
            }
        }, indent=2))
        return 0

    if args.cmd == "read":
        from .dxf import read_dxf

        print(json.dumps(read_dxf(args.path), indent=1))
        return 0

    from .controller import LibreCAD, LibreCADNotRunning

    lc = LibreCAD()
    try:
        if args.cmd == "launch":
            info = lc.launch()
            print(f"LibreCAD window: {info.title}")
        elif args.cmd == "status":
            print(json.dumps(lc.status(), indent=1))
        elif args.cmd == "run":
            cmds = []
            for c in args.commands:
                cmds += [x for x in c.split(";") if x.strip()]
            print(lc.run(cmds).summary())
        elif args.cmd == "screenshot":
            data = lc.screenshot_png()
            with open(args.path, "wb") as f:
                f.write(data)
            print(f"saved {args.path} ({len(data)} bytes)")
    except LibreCADNotRunning as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
