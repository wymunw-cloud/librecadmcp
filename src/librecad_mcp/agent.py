"""LLM engine for the prompt bar: runs Claude Code in print mode with the LibreCAD MCP server.

The bar does not talk to the Claude API directly.  It launches ``claude -p``
(the Claude Code CLI that is already installed and logged in on the machine)
with a private MCP configuration that points at this package's ``serve``
command, and streams the JSON events back so the UI can show tool calls as
they happen.  Conversation memory across prompts comes from ``--resume``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

STATE_DIR = Path(os.environ.get("LIBRECAD_MCP_HOME", Path.home() / ".librecad-mcp"))

SYSTEM_PROMPT = """\
You are the drafting assistant inside LibreCAD (a 2D CAD program). The user types
short requests in a prompt bar; you carry them out with the `librecad` MCP tools,
which drive the running LibreCAD window.

Guidelines:
- Work in drawing units; angles are in degrees, counter-clockwise; y points up.
- Prefer the dedicated tools (draw_line, draw_rectangle, draw_circle, ...). Use
  run_commands for anything else, following the command reference resource.
- If a request is ambiguous about size or position, pick sensible defaults
  (origin, round numbers) and say what you chose instead of asking.
- After drawing something non-trivial, call zoom (auto) so the user sees it; use
  screenshot only when you need to verify a result.
- To modify existing geometry, select first (select_all) and use the modify tools.
- Keep replies to one or two short sentences; the user reads them in a small bar.
"""


@dataclass
class AgentEvent:
    kind: str                 # "status" | "tool" | "text" | "result" | "error"
    text: str = ""
    data: dict = field(default_factory=dict)


class ClaudeCodeEngine:
    """Run prompts through ``claude -p`` with the LibreCAD MCP server attached."""

    def __init__(self, model: Optional[str] = None, claude_path: Optional[str] = None,
                 extra_args: Optional[list[str]] = None):
        self.model = model
        self.claude = claude_path or shutil.which("claude") or shutil.which("claude.cmd")
        self.extra_args = extra_args or []
        self.session_id: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.mcp_config_path = STATE_DIR / "mcp-config.json"
        self._write_mcp_config()

    # ----------------------------------------------------------------- config
    def _write_mcp_config(self) -> None:
        config = {
            "mcpServers": {
                "librecad": {
                    "command": sys.executable,
                    "args": ["-m", "librecad_mcp", "serve"],
                }
            }
        }
        self.mcp_config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    def available(self) -> bool:
        return bool(self.claude)

    def reset(self) -> None:
        self.session_id = None

    def cancel(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.kill()
                except OSError:
                    pass

    # -------------------------------------------------------------------- run
    def run(self, prompt: str, on_event: Callable[[AgentEvent], None]) -> Optional[str]:
        """Run one prompt; stream events to ``on_event``; return the final text."""
        if not self.claude:
            on_event(AgentEvent("error", "The 'claude' CLI was not found on PATH. Install Claude Code "
                                          "(https://claude.com/claude-code) and log in, then restart the bar."))
            return None
        cmd = [
            self.claude, "-p", prompt,
            "--output-format", "stream-json", "--verbose",
            "--mcp-config", str(self.mcp_config_path), "--strict-mcp-config",
            "--allowedTools", "mcp__librecad",
            "--append-system-prompt", SYSTEM_PROMPT,
        ]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        if self.model:
            cmd += ["--model", self.model]
        cmd += self.extra_args

        env = os.environ.copy()
        # Allow launching from inside a Claude Code session: drop its per-session variables.
        for key in list(env):
            if key.startswith("CLAUDE_CODE_") or key in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT",
                                                          "CLAUDE_AGENT_SDK_VERSION"):
                env.pop(key, None)
        cwd = STATE_DIR
        on_event(AgentEvent("status", "Thinking…"))
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            on_event(AgentEvent("error", f"Could not start claude: {exc}"))
            return None
        with self._lock:
            self._proc = proc

        final_text: Optional[str] = None
        assistant_text: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("type")
            sid = ev.get("session_id")
            if sid:
                self.session_id = sid
            if kind == "assistant":
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        name = block.get("name", "").replace("mcp__librecad__", "")
                        on_event(AgentEvent("tool", name, {"input": block.get("input", {})}))
                    elif block.get("type") == "text" and block.get("text"):
                        assistant_text.append(block["text"])
                        on_event(AgentEvent("text", block["text"]))
            elif kind == "user":
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "tool_result" and block.get("is_error"):
                        content = block.get("content")
                        if isinstance(content, list):
                            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                        on_event(AgentEvent("status", f"tool error: {str(content)[:200]}"))
            elif kind == "result":
                final_text = ev.get("result") or ("\n".join(assistant_text) if assistant_text else None)
                if ev.get("is_error") or ev.get("subtype", "").startswith("error"):
                    on_event(AgentEvent("error", final_text or ev.get("subtype", "error")))
                    final_text = None
                else:
                    on_event(AgentEvent("result", final_text or "Done.", {
                        "cost_usd": ev.get("total_cost_usd"), "duration_ms": ev.get("duration_ms"),
                        "turns": ev.get("num_turns")}))
        stderr = proc.stderr.read() if proc.stderr else ""
        rc = proc.wait()
        with self._lock:
            self._proc = None
        if rc != 0 and final_text is None:
            msg = (stderr or "").strip().splitlines()
            on_event(AgentEvent("error", f"claude exited with code {rc}: {msg[-1] if msg else 'no details'}"))
        return final_text
