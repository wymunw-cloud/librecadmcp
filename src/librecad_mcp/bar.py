"""A floating prompt bar that docks to the bottom of the LibreCAD window.

Type a request in plain language and press Enter; the bar runs it through
Claude (via ``claude -p``) with the LibreCAD MCP tools and shows what happens.
Lines starting with ``>`` are sent straight to LibreCAD's command line, e.g.
``> li;0,0;100,50;k``.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import queue
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import ttk
from typing import Optional

from .agent import AgentEvent, ClaudeCodeEngine
from .controller import LibreCAD, LibreCADNotRunning, find_main_windows

user32 = ctypes.windll.user32
SPI_GETWORKAREA = 0x0030

BG = "#1b1b1b"
BG2 = "#262626"
FG = "#e8e8e8"
DIM = "#9a9a9a"
ACCENT = "#3ddc84"
ERR = "#ff6b6b"


def _work_area() -> tuple[int, int, int, int]:
    r = wt.RECT()
    user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0)
    return r.left, r.top, r.right, r.bottom


def _window_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


class PromptBar:
    HEIGHT = 46
    LOG_HEIGHT = 150

    def __init__(self, model: Optional[str] = None, position: str = "overlay", width: Optional[int] = None):
        self.position = position
        self.fixed_width = width
        self.engine = ClaudeCodeEngine(model=model)
        self.lc = LibreCAD()
        # One long-lived thread owns all direct LibreCAD (UI Automation) work.
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="librecad")
        self.events: "queue.Queue[AgentEvent]" = queue.Queue()
        self.busy = False
        self.history: list[str] = []
        self.hist_index = 0
        self.log_visible = False
        self._last_geom = None
        self._drag_offset = None
        self._manual_pos: Optional[tuple[int, int]] = None

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("LibreCAD prompt")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)
        self._build()
        self.root.after(200, self._follow_librecad)
        self.root.after(100, self._drain_events)

    # --------------------------------------------------------------------- UI
    def _build(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Bar.TButton", background=BG2, foreground=FG, borderwidth=0, padding=(10, 4))
        style.map("Bar.TButton", background=[("active", "#333333")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#0b0b0b", borderwidth=0, padding=(12, 4))
        style.map("Accent.TButton", background=[("active", "#5ee89a")])

        self.frame = tk.Frame(self.root, bg=BG, highlightbackground="#3a3a3a", highlightthickness=1)
        self.frame.pack(fill="both", expand=True)

        top = tk.Frame(self.frame, bg=BG)
        top.pack(fill="x", side="top")

        self.grip = tk.Label(top, text="⋮⋮", bg=BG, fg=DIM, cursor="fleur", padx=6)
        self.grip.pack(side="left")
        self.grip.bind("<ButtonPress-1>", self._drag_start)
        self.grip.bind("<B1-Motion>", self._drag_move)
        self.grip.bind("<Double-Button-1>", lambda e: self._reset_position())

        self.dot = tk.Label(top, text="●", bg=BG, fg=DIM, font=("Segoe UI", 12))
        self.dot.pack(side="left", padx=(0, 4))

        self.entry = tk.Entry(top, bg=BG2, fg=FG, insertbackground=FG, relief="flat",
                              font=("Segoe UI", 11), highlightthickness=1, highlightbackground="#3a3a3a",
                              highlightcolor=ACCENT)
        self.entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 6), pady=6)
        self.entry.insert(0, "")
        self.entry.bind("<Return>", lambda e: self.submit())
        self.entry.bind("<Escape>", lambda e: self.entry.delete(0, "end"))
        self.entry.bind("<Up>", self._hist_prev)
        self.entry.bind("<Down>", self._hist_next)

        self.send_btn = ttk.Button(top, text="Send", style="Accent.TButton", command=self.submit)
        self.send_btn.pack(side="left", padx=(0, 4))
        self.stop_btn = ttk.Button(top, text="Stop", style="Bar.TButton", command=self.cancel)
        self.new_btn = ttk.Button(top, text="New chat", style="Bar.TButton", command=self.new_chat)
        self.new_btn.pack(side="left", padx=(0, 4))
        self.log_btn = ttk.Button(top, text="Log ▴", style="Bar.TButton", command=self.toggle_log)
        self.log_btn.pack(side="left", padx=(0, 4))
        self.close_btn = tk.Label(top, text="✕", bg=BG, fg=DIM, padx=8, cursor="hand2")
        self.close_btn.pack(side="left")
        self.close_btn.bind("<Button-1>", lambda e: self.quit())

        self.status_var = tk.StringVar(value="")
        self.status_lbl = tk.Label(self.frame, textvariable=self.status_var, bg=BG, fg=DIM,
                                   anchor="w", font=("Segoe UI", 9), padx=10)

        self.log_frame = tk.Frame(self.frame, bg=BG)
        self.log = tk.Text(self.log_frame, bg="#141414", fg=FG, relief="flat", height=8,
                           font=("Consolas", 9), wrap="word", state="disabled", padx=8, pady=4)
        self.log.tag_configure("tool", foreground=ACCENT)
        self.log.tag_configure("dim", foreground=DIM)
        self.log.tag_configure("err", foreground=ERR)
        self.log.tag_configure("user", foreground="#8ab4f8")
        sb = ttk.Scrollbar(self.log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._set_placeholder()

    def _set_placeholder(self) -> None:
        if not self.entry.get():
            self.status_var.set("Describe what to draw, e.g. “draw a 100×50 rectangle at the origin with a "
                                "20 mm hole in the middle”.  Use “> li;0,0;50,50;k” for raw LibreCAD commands.")
            self.status_lbl.pack(fill="x", side="top")

    # ---------------------------------------------------------------- geometry
    def _follow_librecad(self) -> None:
        try:
            wins = find_main_windows()
            if not wins:
                self._show_not_running()
            else:
                hwnd = wins[0].hwnd
                if user32.IsIconic(hwnd):
                    self.root.withdraw()
                else:
                    self._place(hwnd)
                    if self.root.state() == "withdrawn":
                        self.root.deiconify()
                        self.root.attributes("-topmost", True)
        except Exception:
            pass
        self.root.after(250, self._follow_librecad)

    def _place(self, hwnd: int) -> None:
        left, top, right, bottom = _window_rect(hwnd)
        _, _, _, work_bottom = _work_area()
        self.root.update_idletasks()
        height = max(self.HEIGHT, self.frame.winfo_reqheight()) + (self.LOG_HEIGHT if self.log_visible else 0)
        if self._manual_pos:
            x, y = self._manual_pos
            width = self.fixed_width or max(600, right - left - 16)
        else:
            width = self.fixed_width or max(600, right - left - 16)
            x = left + 8
            if self.position == "below" and bottom + height <= work_bottom:
                y = bottom + 2
            else:
                y = bottom - height - 8
        geom = (x, y, width, height)
        if geom != self._last_geom:
            self.root.geometry(f"{width}x{height}+{x}+{y}")
            self._last_geom = geom

    def _show_not_running(self) -> None:
        if self.root.state() == "withdrawn":
            self.root.deiconify()
        _, _, right, bottom = _work_area()
        width = self.fixed_width or 800
        self.root.geometry(f"{width}x{self.HEIGHT + 22}+{(right - width) // 2}+{bottom - self.HEIGHT - 40}")
        if "not running" not in self.status_var.get():
            self.status_var.set("LibreCAD is not running.  Press Enter with an empty prompt to launch it.")
            self.status_lbl.pack(fill="x", side="top")
            self.dot.configure(fg=ERR)

    def _drag_start(self, e) -> None:
        self._drag_offset = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e) -> None:
        if self._drag_offset:
            x = e.x_root - self._drag_offset[0]
            y = e.y_root - self._drag_offset[1]
            self._manual_pos = (x, y)
            self._last_geom = None

    def _reset_position(self) -> None:
        self._manual_pos = None
        self._last_geom = None

    # ------------------------------------------------------------------ input
    def _hist_prev(self, e) -> None:
        if self.history and self.hist_index > 0:
            self.hist_index -= 1
            self.entry.delete(0, "end")
            self.entry.insert(0, self.history[self.hist_index])

    def _hist_next(self, e) -> None:
        if self.history and self.hist_index < len(self.history) - 1:
            self.hist_index += 1
            self.entry.delete(0, "end")
            self.entry.insert(0, self.history[self.hist_index])
        else:
            self.hist_index = len(self.history)
            self.entry.delete(0, "end")

    def submit(self) -> None:
        text = self.entry.get().strip()
        if self.busy:
            return
        if not text:
            if not find_main_windows():
                self._launch_librecad()
            return
        self.entry.delete(0, "end")
        self.entry.focus_set()
        self.history.append(text)
        self.hist_index = len(self.history)
        self.status_lbl.pack_forget()
        self._log(f"› {text}\n", "user")
        if text.startswith(">"):
            self._run_raw(text[1:].strip())
            return
        self._set_busy(True)
        threading.Thread(target=self._agent_thread, args=(text,), daemon=True).start()

    def _run_raw(self, raw: str) -> None:
        def work():
            try:
                cmds = [c.strip() for c in raw.split(";") if c.strip()]
                r = self.lc.run(cmds)
                # Keep LibreCAD's messages (errors, prompts) but not its "Command: x (y)" confirmations.
                messages = [ln.strip() for ln in r.output.splitlines()
                            if ln.strip() and not ln.startswith("Command:")]
                if r.error:
                    messages.append(r.error)
                if r.prompt and r.prompt != "Command:":
                    messages.append(f"LibreCAD is waiting: {r.prompt}")
                sent = ";".join(cmds)
                failed = bool(r.error) or any(
                    key in m for m in messages
                    for key in ("Unknown command", "Not a valid", "Syntax Error", "No entity", "Nothing to"))
                if messages:
                    lines = [("✕ " if failed else "ℹ ") + " · ".join(messages), f"sent: {sent}"]
                else:
                    lines = [f"✓ {sent}"]
                self.events.put(AgentEvent("result", "\n".join(lines)))
            except LibreCADNotRunning as exc:
                self.events.put(AgentEvent("error", str(exc)))
            except Exception as exc:  # noqa: BLE001
                self.events.put(AgentEvent("error", f"{type(exc).__name__}: {exc}"))
        self._set_busy(True)
        self.worker.submit(work)

    def _launch_librecad(self) -> None:
        def work():
            try:
                self.events.put(AgentEvent("status", "Starting LibreCAD…"))
                self.lc.launch()
                self.events.put(AgentEvent("result", "LibreCAD started."))
            except Exception as exc:  # noqa: BLE001
                self.events.put(AgentEvent("error", str(exc)))
        self.worker.submit(work)

    def _agent_thread(self, text: str) -> None:
        try:
            self.engine.run(text, self.events.put)
        except Exception as exc:  # noqa: BLE001
            self.events.put(AgentEvent("error", f"{type(exc).__name__}: {exc}"))
        finally:
            self.events.put(AgentEvent("status", "__done__"))

    def cancel(self) -> None:
        self.engine.cancel()
        self._set_busy(False)
        self._status("Stopped.")

    def new_chat(self) -> None:
        self.engine.reset()
        self._log("— new conversation —\n", "dim")
        self._status("New conversation started.")

    def toggle_log(self) -> None:
        self.log_visible = not self.log_visible
        if self.log_visible:
            self.log_frame.pack(fill="both", expand=True, side="bottom")
            self.log_btn.configure(text="Log ▾")
        else:
            self.log_frame.pack_forget()
            self.log_btn.configure(text="Log ▴")
        self._last_geom = None

    # ---------------------------------------------------------------- feedback
    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        if busy:
            self.dot.configure(fg=ACCENT)
            self.send_btn.pack_forget()
            self.stop_btn.pack(side="left", padx=(0, 4), before=self.new_btn)
            self._pulse()
        else:
            self.dot.configure(fg=DIM)
            self.stop_btn.pack_forget()
            self.send_btn.pack(side="left", padx=(0, 4), before=self.new_btn)

    def _pulse(self) -> None:
        if not self.busy:
            return
        cur = self.dot.cget("fg")
        self.dot.configure(fg=ACCENT if cur != ACCENT else "#1f6f44")
        self.root.after(450, self._pulse)

    def _status(self, text: str, error: bool = False) -> None:
        self.status_var.set(text)
        self.status_lbl.configure(fg=ERR if error else DIM)
        if not self.status_lbl.winfo_ismapped():
            self.status_lbl.pack(fill="x", side="top")
        self._last_geom = None

    def _log(self, text: str, tag: str = "") -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text, tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain_events(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                self._handle_event(ev)
        except queue.Empty:
            pass
        self.root.after(80, self._drain_events)

    def _handle_event(self, ev: AgentEvent) -> None:
        if ev.kind == "status":
            if ev.text == "__done__":
                self._set_busy(False)
            else:
                self._status(ev.text)
        elif ev.kind == "tool":
            args = ", ".join(f"{k}={v}" for k, v in ev.data.get("input", {}).items())
            if len(args) > 90:
                args = args[:87] + "…"
            self._status(f"⚙ {ev.text}({args})")
            self._log(f"  ⚙ {ev.text}({args})\n", "tool")
        elif ev.kind == "text":
            self._log(ev.text.strip() + "\n")
        elif ev.kind == "result":
            self._set_busy(False)
            first = ev.text.strip().splitlines()[0] if ev.text.strip() else "Done."
            self._status(first if len(first) < 160 else first[:157] + "…")
            self._log(ev.text.strip() + "\n", "")
            self.dot.configure(fg=DIM)
            self.entry.focus_set()
        elif ev.kind == "error":
            self._set_busy(False)
            self._status(ev.text.splitlines()[0][:200], error=True)
            self._log(f"  ✕ {ev.text}\n", "err")

    # --------------------------------------------------------------- lifecycle
    def run(self) -> None:
        self.root.deiconify()
        self.entry.focus_set()
        if not self.engine.available():
            self._status("The 'claude' CLI was not found; only '> raw commands' will work.", error=True)
        self.root.mainloop()

    def quit(self) -> None:
        self.engine.cancel()
        self.root.destroy()


def _enable_dpi_awareness() -> None:
    """Use physical pixels everywhere so the bar lines up with the LibreCAD window
    on scaled displays. Must run before Tk creates its first window."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def main(model: Optional[str] = None, position: str = "overlay", width: Optional[int] = None) -> None:
    _enable_dpi_awareness()
    PromptBar(model=model, position=position, width=width).run()


if __name__ == "__main__":
    main()
