"""Low-level driver for a running LibreCAD 2.2.x instance on Windows.

How it works
------------
LibreCAD is a Qt application, and Qt exposes its widgets through Windows UI
Automation (UIA).  We locate the command-line edit box (``QG_CommandEdit``) and
the command history (``QG_CommandHistory``) inside LibreCAD's main window, write
text into the edit box with UIA's ValuePattern, and post a ``WM_KEYDOWN`` /
``WM_KEYUP`` pair for the Return key to LibreCAD's window.  Qt delivers posted
key messages to the widget that last had focus inside that window, so this works
while LibreCAD is in the background, and even while it is minimized, normally
without stealing keyboard focus from whatever the user is doing.

Feedback comes from three places: the command history text (which LibreCAD
fills with echoed commands, prompts and errors such as ``Unknown command``),
the prompt label above the edit box (``Specify first point`` ...), and the
status bar (selection count, total length, current layer, coordinates).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import io
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32

# --- Win32 constants -------------------------------------------------------
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
VK_RETURN = 0x0D
SC_RETURN = 0x1C
SW_SHOWNOACTIVATE = 4
SW_RESTORE = 9
SW_MAXIMIZE = 3
WPF_RESTORETOMAXIMIZED = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PW_CLIENTONLY = 0x1
PW_RENDERFULLCONTENT = 0x2

LIBRECAD_EXE_NAMES = {"librecad.exe"}
DEFAULT_EXE_CANDIDATES = [
    r"C:\Program Files\LibreCAD\LibreCAD.exe",
    r"C:\Program Files (x86)\LibreCAD\LibreCAD.exe",
]

_EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wt.UINT),
        ("flags", wt.UINT),
        ("showCmd", wt.UINT),
        ("ptMinPosition", wt.POINT),
        ("ptMaxPosition", wt.POINT),
        ("rcNormalPosition", wt.RECT),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


class LibreCADError(RuntimeError):
    """Base class for controller errors."""


class LibreCADNotRunning(LibreCADError):
    """Raised when no LibreCAD main window can be found."""


@dataclass
class WindowInfo:
    hwnd: int
    pid: int
    class_name: str
    title: str


@dataclass
class CommandResult:
    """Outcome of sending one or more command-line inputs to LibreCAD."""

    commands: list[str]
    output: str = ""            # new command-history text produced by the commands
    prompt: str = ""            # prompt label after execution, e.g. "Specify next point"
    dialogs: list[str] = field(default_factory=list)  # titles of dialogs left open
    completed: bool = True      # False if LibreCAD did not consume the input in time
    notes: list[str] = field(default_factory=list)
    error: str = ""             # a specific failure explanation, if any

    def summary(self) -> str:
        parts = []
        if self.output.strip():
            parts.append(self.output.strip())
        if self.prompt:
            parts.append(f"[prompt] {self.prompt}")
        if self.dialogs:
            parts.append("[dialog open] " + ", ".join(self.dialogs))
        if self.error:
            parts.append(f"[error] {self.error}")
        elif not self.completed:
            parts.append("[warning] LibreCAD did not consume the input (a modal dialog may be open)")
        parts.extend(f"[note] {n}" for n in self.notes)
        return "\n".join(parts) if parts else "OK"


# --- small Win32 helpers -----------------------------------------------------

def _window_text(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _window_pid(hwnd: int) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


_exe_cache: dict[int, str] = {}


def _process_exe(pid: int) -> str:
    if pid in _exe_cache:
        return _exe_cache[pid]
    name = ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if h:
        try:
            size = wt.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = buf.value
        finally:
            kernel32.CloseHandle(h)
    _exe_cache[pid] = name
    return name


def _enum_top_windows() -> list[WindowInfo]:
    found: list[WindowInfo] = []

    def cb(hwnd, _lparam):
        found.append(WindowInfo(hwnd, _window_pid(hwnd), _class_name(hwnd), _window_text(hwnd)))
        return True

    user32.EnumWindows(_EnumWindowsProc(cb), 0)
    return found


def librecad_pids() -> set[int]:
    pids = set()
    for w in _enum_top_windows():
        exe = os.path.basename(_process_exe(w.pid)).lower()
        if exe in LIBRECAD_EXE_NAMES:
            pids.add(w.pid)
    return pids


def find_main_windows() -> list[WindowInfo]:
    """Visible top-level LibreCAD main windows (class ``Qt*QWindowIcon`` from LibreCAD.exe)."""
    result = []
    for w in _enum_top_windows():
        if not user32.IsWindowVisible(w.hwnd):
            continue
        if not (w.class_name.startswith("Qt") and "QWindow" in w.class_name):
            continue
        exe = os.path.basename(_process_exe(w.pid)).lower()
        if exe not in LIBRECAD_EXE_NAMES:
            continue
        if not w.title.startswith("LibreCAD"):
            continue
        result.append(w)
    # Prefer the window whose title carries a document name.
    result.sort(key=lambda w: (0 if " - " in w.title else 1, w.hwnd))
    return result


def _ensure_com() -> None:
    """Initialize COM for the current thread (pywinauto/comtypes need it)."""
    try:
        import comtypes

        comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
    except Exception:
        pass


def fmt(v: float) -> str:
    """Format a number for LibreCAD's command line (no exponent, no trailing zeros)."""
    s = f"{float(v):.6f}".rstrip("0").rstrip(".")
    if s in ("", "-", "-0"):
        s = "0"
    return s


# --- the controller ----------------------------------------------------------

class LibreCAD:
    """Drive the running LibreCAD window.

    All public methods are thread-safe (serialized by a lock) and reconnect
    automatically if LibreCAD was restarted.
    """

    EDIT_CLASS = "QG_CommandEdit"
    HISTORY_CLASS = "QG_CommandHistory"
    COMMAND_WIDGET_CLASS = "QG_CommandWidget"
    GRAPHIC_VIEW_CLASS = "QG_GraphicView"

    def __init__(self, exe_path: Optional[str] = None):
        self._lock = threading.RLock()
        self.exe_path = exe_path or os.environ.get("LIBRECAD_EXE")
        self._hwnd: Optional[int] = None
        self._pid: Optional[int] = None
        self._win = None
        self._edit = None
        self._hist = None
        self._label = None
        self._desktop = None
        self._known_files: dict[str, str] = {}

    # ------------------------------------------------------------------ setup
    def _desktop_obj(self):
        if self._desktop is None:
            _ensure_com()
            from pywinauto import Desktop  # imported lazily: slow and Windows-only

            self._desktop = Desktop(backend="uia")
        return self._desktop

    def is_running(self) -> bool:
        return bool(find_main_windows())

    def launch(self, wait: float = 30.0) -> WindowInfo:
        """Start LibreCAD.exe if it is not running and wait for its main window."""
        wins = find_main_windows()
        if wins:
            return wins[0]
        exe = self.exe_path or next((p for p in DEFAULT_EXE_CANDIDATES if os.path.exists(p)), None)
        if not exe or not os.path.exists(exe):
            raise LibreCADNotRunning("LibreCAD is not running and LibreCAD.exe was not found; "
                                     "set the LIBRECAD_EXE environment variable to its path.")
        subprocess.Popen([exe], cwd=os.path.dirname(exe), close_fds=True,
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        deadline = time.time() + wait
        while time.time() < deadline:
            wins = find_main_windows()
            if wins:
                time.sleep(1.5)  # let the UI finish building
                return wins[0]
            time.sleep(0.3)
        raise LibreCADNotRunning("LibreCAD did not open a main window in time.")

    def connect(self) -> None:
        """Locate LibreCAD's main window and its command-line widgets."""
        with self._lock:
            wins = find_main_windows()
            if not wins:
                raise LibreCADNotRunning("LibreCAD is not running (no main window found). "
                                         "Start LibreCAD first, or call launch().")
            w = wins[0]
            desk = self._desktop_obj()
            win = desk.window(handle=w.hwnd).wrapper_object()
            edits = win.descendants(control_type="Edit", class_name=self.EDIT_CLASS)
            hists = win.descendants(control_type="Edit", class_name=self.HISTORY_CLASS)
            if not edits or not hists:
                raise LibreCADError("Found LibreCAD but not its command line widget. "
                                    "Enable it with Widgets > Dock Widgets > Command Line.")
            self._hwnd, self._pid, self._win = w.hwnd, w.pid, win
            self._edit, self._hist = edits[0], hists[0]
            self._label = self._find_prompt_label()

    def _find_prompt_label(self):
        node = self._edit
        for _ in range(4):
            try:
                node = node.parent()
            except Exception:
                return None
            if node is None:
                return None
            if node.element_info.class_name == self.COMMAND_WIDGET_CLASS:
                labels = node.descendants(control_type="Text", class_name="QLabel")
                return labels[0] if labels else None
        return None

    def _connected(self) -> bool:
        if not self._hwnd or not user32.IsWindow(self._hwnd) or self._edit is None:
            return False
        try:
            self._edit.element_info.name  # cheap liveness probe
            return True
        except Exception:
            return False

    def ensure(self) -> None:
        if not self._connected():
            self.connect()

    @property
    def hwnd(self) -> int:
        self.ensure()
        return self._hwnd  # type: ignore[return-value]

    # ------------------------------------------------------------- primitives
    def _edit_value(self) -> str:
        try:
            return self._edit.get_value()
        except Exception:
            try:
                return self._edit.iface_value.CurrentValue
            except Exception:
                return ""

    def history_text(self) -> str:
        self.ensure()
        try:
            return self._hist.iface_text.DocumentRange.GetText(-1)
        except Exception:
            try:
                return self._hist.get_value()
            except Exception:
                return ""

    def prompt(self) -> str:
        self.ensure()
        try:
            return self._label.element_info.name if self._label is not None else ""
        except Exception:
            return ""

    def _post_key(self, vk: int, scan: int = 0, extended: bool = False) -> None:
        """Post a key press to LibreCAD's window.

        ``extended`` sets the extended-key flag; for VK_RETURN that makes Qt see
        the numpad ``Key_Enter`` instead of ``Key_Return``, which is the key
        LibreCAD's selection step listens for.
        """
        ext = (1 << 24) if extended else 0
        down = 1 | (scan << 16) | ext
        up = down | (1 << 30) | (1 << 31)
        user32.PostMessageW(self._hwnd, WM_KEYDOWN, vk, down)
        user32.PostMessageW(self._hwnd, WM_KEYUP, vk, up)

    def _client_point(self, wrapper) -> tuple[int, int]:
        r = wrapper.rectangle()
        wr = self._win.rectangle()
        return (int((r.left + r.right) / 2 - wr.left), int((r.top + r.bottom) / 2 - wr.top))

    def _post_click(self, wrapper, button: str = "left") -> None:
        """Post a synthetic mouse click to the main window at a child widget's centre.

        Posted mouse messages are handled by Qt without Windows activating the
        window, so this moves Qt's focus without stealing the foreground.
        """
        x, y = self._client_point(wrapper)
        lparam = (y << 16) | (x & 0xFFFF)
        if button == "left":
            user32.PostMessageW(self._hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
            user32.PostMessageW(self._hwnd, WM_LBUTTONUP, 0, lparam)
        else:
            user32.PostMessageW(self._hwnd, WM_RBUTTONDOWN, MK_RBUTTON, lparam)
            user32.PostMessageW(self._hwnd, WM_RBUTTONUP, 0, lparam)

    def _focus_edit(self, allow_foreground_change: bool = False) -> None:
        fg = user32.GetForegroundWindow()
        if fg == self._hwnd or allow_foreground_change:
            try:
                self._edit.element_info.element.SetFocus()
            except Exception:
                pass
            if fg and fg != self._hwnd and allow_foreground_change:
                # UIA SetFocus activates LibreCAD; hand the foreground back.
                time.sleep(0.05)
                user32.SetForegroundWindow(fg)
            return
        self._post_click(self._edit, "left")

    def _wait_edit_cleared(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._edit_value() == "":
                return True
            time.sleep(0.02)
        return self._edit_value() == ""

    # ---------------------------------------------------------------- sending
    def send_line(self, text: str, wait: float = 4.0, expect_dialog: bool = False) -> CommandResult:
        """Type ``text`` into LibreCAD's command line and press Return.

        Several commands can be separated with ``;`` (LibreCAD splits them).
        Returns the new history output, the current prompt and any dialogs
        that are open afterwards.  With ``expect_dialog`` the method returns as
        soon as a dialog window appears instead of waiting for the input to be
        consumed (modal dialogs block LibreCAD's command processing).
        """
        with self._lock:
            self.ensure()
            before = self.history_text()
            self._edit.iface_value.SetValue(text)
            self._post_key(VK_RETURN, SC_RETURN)
            notes: list[str] = []
            completed = self._wait_for_consumption(wait if not expect_dialog else min(wait, 1.5),
                                                   expect_dialog)
            if not completed and not expect_dialog and not self.dialogs():
                # Return went to another widget; move Qt's focus to the edit and retry.
                self._focus_edit()
                self._post_key(VK_RETURN, SC_RETURN)
                completed = self._wait_edit_cleared(1.5)
                if not completed and not self.dialogs():
                    self._focus_edit(allow_foreground_change=True)
                    self._post_key(VK_RETURN, SC_RETURN)
                    completed = self._wait_edit_cleared(1.5)
                    notes.append("had to focus the command line explicitly")
            time.sleep(0.08)
            after = self.history_text()
            output = after[len(before):] if after.startswith(before) else after[-1500:]
            output = self._strip_echo(output, text)
            dialogs = [d.title for d in self.dialogs()]
            return CommandResult(commands=[text], output=output, prompt=self.prompt(),
                                 dialogs=dialogs, completed=completed or bool(dialogs), notes=notes)

    def _wait_for_consumption(self, timeout: float, expect_dialog: bool) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._edit_value() == "":
                return True
            if expect_dialog and self.dialogs():
                return True
            time.sleep(0.02)
        return self._edit_value() == ""

    @staticmethod
    def _strip_echo(output: str, sent: str) -> str:
        lines = output.split("\n")
        sent_simplified = " ".join(sent.split())
        cleaned = [ln for ln in lines if " ".join(ln.split()) != sent_simplified]
        return "\n".join(ln for ln in cleaned if ln.strip())

    def run(self, commands: Iterable[str], finish: bool = False, batch: int = 40,
            wait: float = 4.0) -> CommandResult:
        """Send a sequence of LibreCAD command-line inputs.

        Consecutive commands are joined with ``;`` so LibreCAD executes them in
        one go.  ``finish=True`` appends ``k`` (kill all actions), which ends any
        running drawing action and clears the selection.
        """
        cmds = [str(c).strip() for c in commands if str(c).strip()]
        if finish:
            cmds.append("k")
        if not cmds:
            return CommandResult(commands=[], output="", prompt=self.prompt())
        for c in cmds:
            if "=" in c or "\\" in c:
                raise LibreCADError(f"Command {c!r} contains '=' or a backslash, which LibreCAD's "
                                    "command line treats as variable syntax.")
        lines: list[str] = []
        chunk: list[str] = []
        for c in cmds:
            chunk.append(c)
            if len(chunk) >= batch:
                lines.append(";".join(chunk))
                chunk = []
        if chunk:
            lines.append(";".join(chunk))
        merged = CommandResult(commands=cmds)
        outputs: list[str] = []
        with self._lock:
            for line in lines:
                r = self.send_line(line, wait=wait)
                outputs.append(r.output)
                merged.prompt, merged.dialogs, merged.completed = r.prompt, r.dialogs, r.completed
                merged.notes.extend(r.notes)
                if not r.completed:
                    break
        merged.output = "\n".join(o for o in outputs if o)
        return merged

    def press_escape(self, times: int = 1) -> CommandResult:
        """Send LibreCAD's ``escape`` command (steps back / cancels the current action)."""
        r = None
        for _ in range(times):
            r = self.send_line("escape")
        return r or CommandResult(commands=[])

    # ------------------------------------------------------------------ state
    def dialogs(self) -> list[WindowInfo]:
        """Visible top-level windows of the LibreCAD process other than the main window."""
        if not self._pid:
            return []
        out = []
        for w in _enum_top_windows():
            if w.pid != self._pid or w.hwnd == self._hwnd:
                continue
            if not user32.IsWindowVisible(w.hwnd):
                continue
            if w.class_name in ("MSCTFIME UI", "IME") or "ToolTip" in w.class_name or "Popup" in w.class_name:
                continue
            if not w.title and w.class_name != "#32770":
                continue
            out.append(w)
        return out

    def dialog_wrapper(self, hwnd: int):
        return self._desktop_obj().window(handle=hwnd).wrapper_object()

    def wait_dialog(self, predicate: Optional[Callable[[WindowInfo, object], bool]] = None,
                    timeout: float = 5.0):
        """Wait for a dialog window; returns ``(WindowInfo, uia_wrapper)`` or ``(None, None)``."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for d in self.dialogs():
                try:
                    wrapper = self.dialog_wrapper(d.hwnd)
                except Exception:
                    continue
                if predicate is None or predicate(d, wrapper):
                    return d, wrapper
            time.sleep(0.05)
        return None, None

    def wait_dialogs_closed(self, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.dialogs():
                return True
            time.sleep(0.05)
        return not self.dialogs()

    def is_minimized(self) -> bool:
        return bool(user32.IsIconic(self.hwnd))

    def is_foreground(self) -> bool:
        return user32.GetForegroundWindow() == self.hwnd

    def show(self, activate: bool = False) -> None:
        """Restore LibreCAD if minimized; optionally bring it to the foreground."""
        hwnd = self.hwnd
        if user32.IsIconic(hwnd):
            wp = WINDOWPLACEMENT()
            wp.length = ctypes.sizeof(WINDOWPLACEMENT)
            user32.GetWindowPlacement(hwnd, ctypes.byref(wp))
            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            if wp.flags & WPF_RESTORETOMAXIMIZED:
                user32.ShowWindow(hwnd, SW_MAXIMIZE)
            time.sleep(0.3)
        if activate:
            self.bring_to_front()

    def bring_to_front(self) -> None:
        hwnd = self.hwnd
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        if not user32.SetForegroundWindow(hwnd):
            # Windows refuses foreground changes from background processes; use
            # the AttachThreadInput trick as a fallback.
            fg = user32.GetForegroundWindow()
            fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
            my_tid = kernel32.GetCurrentThreadId()
            if fg_tid and fg_tid != my_tid:
                user32.AttachThreadInput(my_tid, fg_tid, True)
                user32.SetForegroundWindow(hwnd)
                user32.AttachThreadInput(my_tid, fg_tid, False)

    def main_window_info(self) -> WindowInfo:
        hwnd = self.hwnd
        return WindowInfo(hwnd, self._pid or 0, _class_name(hwnd), _window_text(hwnd))

    def documents(self) -> list[str]:
        """Titles of the open drawing windows (MDI sub-windows)."""
        self.ensure()
        names = []
        try:
            for w in self._win.descendants(control_type="Window"):
                if w.element_info.class_name in ("QC_MDIWindow", "QMdiSubWindow"):
                    names.append(w.element_info.name)
        except Exception:
            pass
        return names

    def active_document(self) -> str:
        title = _window_text(self.hwnd)
        m = re.match(r"LibreCAD\s*-\s*\[(.*)\]\s*$", title)
        if m:
            return m.group(1)
        docs = self.documents()
        return docs[0] if docs else ""

    def has_unsaved_changes(self) -> bool:
        return "*" in self.active_document()

    def remember_file(self, path: str) -> None:
        """Record a path we opened or saved, so the bare title can be resolved later."""
        self._known_files[os.path.basename(path).lower()] = os.path.abspath(path)

    @staticmethod
    def recent_files() -> list[str]:
        """LibreCAD's recent-files list (from its settings in the registry)."""
        try:
            import winreg
        except ImportError:
            return []
        out: list[str] = []
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\LibreCAD\LibreCAD\RecentFiles")
        except OSError:
            return out
        with key:
            for i in range(1, 40):
                try:
                    value, _ = winreg.QueryValueEx(key, f"File{i}")
                except OSError:
                    break
                if value:
                    out.append(str(value))
        return out

    def current_file(self) -> Optional[str]:
        """Full path of the active drawing if it has been saved, else None.

        LibreCAD shows only the file name in the window title, so the directory
        is recovered from files this controller opened/saved or from LibreCAD's
        recent-files list.
        """
        doc = self.active_document().replace("*", "").strip()
        if not doc or doc.startswith("unnamed document") or doc.startswith("Block '"):
            return None
        if os.path.isabs(doc) and os.path.exists(doc):
            return doc
        known = getattr(self, "_known_files", {}).get(doc.lower())
        if known and os.path.exists(known):
            return known
        for p in self.recent_files():
            if os.path.basename(p).lower() == doc.lower() and os.path.exists(p):
                return p
        return doc  # name only; the directory is unknown

    def _status_group_labels(self, class_name: str) -> list[str]:
        """Texts of the QLabels inside one status-bar widget (by Qt class name)."""
        try:
            groups = self._win.descendants(class_name=class_name)
            if not groups:
                return []
            out = []
            for lb in groups[0].descendants(control_type="Text"):
                try:
                    out.append(lb.element_info.name)
                except Exception:
                    out.append("")
            return out
        except Exception:
            return []

    def status(self) -> dict:
        """Snapshot of LibreCAD's state, read from the window and the status bar."""
        with self._lock:
            self.ensure()
            coords = self._status_group_labels("QG_CoordinateWidget")
            layer = self._status_group_labels("QG_ActiveLayerName")
            grid = self._status_group_labels("TwoStackedLabels")
            mouse = [t for t in self._status_group_labels("QG_MouseWidget") if t]
            sel_values: list[str] = []
            for _ in range(4):  # the selection widget is briefly empty while LibreCAD updates it
                selection = self._status_group_labels("QG_SelectionWidget")
                sel_values = [t for t in selection if t and t not in ("Selected", "Total Length")]
                if sel_values:
                    break
                time.sleep(0.08)
            info = self.main_window_info()
            return {
                "running": True,
                "window_title": info.title,
                "minimized": bool(user32.IsIconic(info.hwnd)),
                "foreground": user32.GetForegroundWindow() == info.hwnd,
                "active_document": self.active_document(),
                "file": self.current_file(),
                "documents": self.documents(),
                "prompt": self.prompt(),
                "mouse_hints": mouse,
                "selected": sel_values[0] if len(sel_values) > 0 else "",
                "total_length": sel_values[1] if len(sel_values) > 1 else "",
                "current_layer": next((t for t in layer if t != "Current Layer"), ""),
                "grid": next((t for t in grid if t != "Grid Status"), ""),
                "cursor": {
                    "absolute": coords[0] if len(coords) > 0 else "",
                    "absolute_polar": coords[1] if len(coords) > 1 else "",
                    "relative": coords[2] if len(coords) > 2 else "",
                    "relative_polar": coords[3] if len(coords) > 3 else "",
                },
                "dialogs": [d.title for d in self.dialogs()],
            }

    # ----------------------------------------------------- views and buttons
    def active_mdi_window(self):
        """UIA wrapper of the active drawing's MDI window (or None)."""
        self.ensure()
        active = self.active_document().replace("*", "").strip()
        wins = [w for w in self._win.descendants(control_type="Window")
                if w.element_info.class_name in ("QC_MDIWindow", "QMdiSubWindow")]
        for w in wins:
            if w.element_info.name.replace("*", "").strip() == active:
                return w
        return wins[0] if wins else None

    def active_graphic_view(self):
        mdi = self.active_mdi_window()
        if mdi is None:
            raise LibreCADError("No drawing is open in LibreCAD.")
        views = mdi.descendants(class_name=self.GRAPHIC_VIEW_CLASS)
        if not views:
            raise LibreCADError("Could not find the drawing view of the active document.")
        return views[0]

    def focus_graphic_view(self) -> bool:
        """Give Qt keyboard focus to the active drawing view without activating the window.

        Uses the LegacyIAccessible pattern's ``Select(SELFLAG_TAKEFOCUS)``, which Qt
        maps to ``QWidget::setFocus`` and which, unlike UIA ``SetFocus``, does not
        bring LibreCAD to the foreground.
        """
        gv = self.active_graphic_view()
        try:
            from pywinauto.uia_defines import IUIA

            pattern = gv.element_info.element.GetCurrentPattern(10018)  # UIA_LegacyIAccessiblePatternId
            if pattern:
                legacy = pattern.QueryInterface(IUIA().UIA_dll.IUIAutomationLegacyIAccessiblePattern)
                legacy.Select(1)  # SELFLAG_TAKEFOCUS
                return True
        except Exception:
            pass
        # Fallback: UIA SetFocus (activates LibreCAD), then hand the foreground back.
        fg = user32.GetForegroundWindow()
        try:
            gv.element_info.element.SetFocus()
        except Exception:
            return False
        if fg and fg != self._hwnd:
            time.sleep(0.05)
            user32.SetForegroundWindow(fg)
        return True

    def confirm_selection(self, wait: float = 1.5) -> CommandResult:
        """Confirm a pending 'Select ...' step (what Enter on the canvas does).

        LibreCAD's selection step reacts only to the numpad-style Enter key
        delivered to the drawing view, so we focus the view and post that key.
        """
        with self._lock:
            self.ensure()
            before_prompt = self.prompt()
            before_hist = self.history_text()
            self.focus_graphic_view()
            time.sleep(0.05)
            self._post_key(VK_RETURN, SC_RETURN, extended=True)
            deadline = time.time() + wait
            while time.time() < deadline:
                if self.prompt() != before_prompt:
                    break
                time.sleep(0.03)
            time.sleep(0.05)
            after = self.history_text()
            output = after[len(before_hist):] if after.startswith(before_hist) else ""
            r = CommandResult(commands=["<Enter>"], output=output.strip(), prompt=self.prompt(),
                              dialogs=[d.title for d in self.dialogs()])
            if r.prompt == before_prompt and not r.dialogs:
                r.completed = False
                r.error = "the selection step did not advance (nothing selected?)"
            return r

    def find_button(self, name):
        """Find a toolbar button by exact name or by a predicate on the name.

        LibreCAD renames some actions after the document ("Save test.dxf as"),
        so callers pass a predicate for those.
        """
        self.ensure()
        match = name if callable(name) else (lambda n, want=name: n == want)
        for b in self._win.descendants(control_type="Button"):
            try:
                if match(b.element_info.name or ""):
                    return b
            except Exception:
                continue
        label = getattr(name, "__name__", None) if callable(name) else name
        raise LibreCADError(f"Toolbar button {label!r} not found (is its toolbar visible?).")

    def click_button(self, name) -> None:
        """Click a toolbar button by posting a synthetic mouse click.

        A posted click returns immediately even when the button opens a modal
        dialog, unlike UIA Invoke, which blocks until the dialog closes.
        """
        with self._lock:
            self._post_click(self.find_button(name), "left")

    def hover_graphic_view(self) -> None:
        """Post a mouse-move over the active drawing view.

        Some LibreCAD actions (e.g. text insertion) only accept a typed
        coordinate after the cursor has been over the canvas once.
        """
        gv = self.active_graphic_view()
        x, y = self._client_point(gv)
        # Two moves at different positions: Qt drops a move that repeats the last known position.
        for dx, dy in ((12, 12), (-12, -12)):
            lp = ((y + dy) << 16) | ((x + dx) & 0xFFFF)
            user32.PostMessageW(self._hwnd, 0x0200, 0, lp)  # WM_MOUSEMOVE
            time.sleep(0.04)

    # ------------------------------------------------------------- screenshot
    def screenshot_png(self, restore_if_minimized: bool = True) -> bytes:
        """Capture LibreCAD's client area (works while it is behind other windows)."""
        from PIL import Image  # lazy import

        with self._lock:
            hwnd = self.hwnd
            if user32.IsIconic(hwnd):
                if not restore_if_minimized:
                    raise LibreCADError("LibreCAD is minimized; restore it first (show_window).")
                self.show(activate=False)
            rect = wt.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(rect))
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if w <= 0 or h <= 0:
                raise LibreCADError("LibreCAD window has no visible client area.")
            hdc_screen = user32.GetDC(0)
            hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
            bmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
            old = gdi32.SelectObject(hdc_mem, bmp)
            try:
                ok = user32.PrintWindow(hwnd, hdc_mem, PW_CLIENTONLY | PW_RENDERFULLCONTENT)
                if not ok:
                    user32.PrintWindow(hwnd, hdc_mem, PW_CLIENTONLY)
                bmi = BITMAPINFOHEADER()
                bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
                bmi.biWidth, bmi.biHeight = w, -h
                bmi.biPlanes, bmi.biBitCount, bmi.biCompression = 1, 32, 0
                buf = ctypes.create_string_buffer(w * h * 4)
                gdi32.GetDIBits(hdc_mem, bmp, 0, h, buf, ctypes.byref(bmi), 0)
                img = Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1).convert("RGB")
            finally:
                gdi32.SelectObject(hdc_mem, old)
                gdi32.DeleteObject(bmp)
                gdi32.DeleteDC(hdc_mem)
                user32.ReleaseDC(0, hdc_screen)
            out = io.BytesIO()
            img.save(out, format="PNG", optimize=True)
            return out.getvalue()
