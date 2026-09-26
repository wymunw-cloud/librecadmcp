"""Automation of the LibreCAD dialogs that some operations pop up.

LibreCAD's command line cannot answer dialogs, so text insertion (MText
dialog), the modify tools (Move/Rotate/Scale/Mirror option dialogs) and file
operations (native Windows file dialogs) are driven through UI Automation.
Qt reports each dialog's class name (``QG_DlgMText``, ``QG_DlgMove`` ...) and
labels its fields after their buddy labels (``Height:``, ``Number of copies``),
which is what the helpers below rely on.
"""

from __future__ import annotations

import os
import time
from typing import Callable, Optional

from .controller import (VK_RETURN, WM_KEYDOWN, WM_KEYUP, CommandResult, LibreCAD, LibreCADError, fmt)
from .controller import user32 as _user32

NATIVE_DIALOG_CLASS = "#32770"
EM_SETSEL = 0x00B1
WM_CHAR = 0x0102


# --- generic widget helpers ------------------------------------------------

def _name(el) -> str:
    try:
        return (el.element_info.name or "").replace("&", "").strip()
    except Exception:
        return ""


def find_button(dialog, *names: str):
    wanted = {n.lower() for n in names}
    for b in dialog.descendants(control_type="Button"):
        if _name(b).lower() in wanted:
            return b
    return None


def click(dialog, *names: str) -> bool:
    b = find_button(dialog, *names)
    if b is None:
        return False
    b.iface_invoke.Invoke()
    return True


def find_edit(dialog, label_substring: str = "", class_name: Optional[str] = None,
              automation_id: Optional[str] = None):
    for e in dialog.descendants(control_type="Edit"):
        info = e.element_info
        if class_name and info.class_name != class_name:
            continue
        if automation_id and info.automation_id != automation_id:
            continue
        if label_substring and label_substring.lower() not in _name(e).lower():
            continue
        return e
    return None


def set_edit(dialog, label_substring: str, value: str, class_name: Optional[str] = None) -> bool:
    e = find_edit(dialog, label_substring, class_name)
    if e is None:
        return False
    e.iface_value.SetValue(value)
    return True


def select_radio(dialog, name: str) -> bool:
    for r in dialog.descendants(control_type="RadioButton"):
        if _name(r).lower() == name.lower():
            try:
                r.iface_selection_item.Select()
            except Exception:
                r.iface_invoke.Invoke()
            return True
    return False


def set_checkbox(dialog, name: str, checked: bool) -> bool:
    for c in dialog.descendants(control_type="CheckBox"):
        if _name(c).lower() == name.lower():
            try:
                state = c.get_toggle_state()  # 1 = on
            except Exception:
                return False
            if bool(state) != checked:
                c.iface_toggle.Toggle()
            return True
    return False


def dialog_texts(dialog) -> list[str]:
    out = []
    for t in dialog.descendants(control_type="Text"):
        n = _name(t)
        if n:
            out.append(n)
    return out


# --- MText -----------------------------------------------------------------

def insert_text(lc: LibreCAD, text: str, x: float, y: float, height: Optional[float] = None,
                timeout: float = 6.0) -> CommandResult:
    """Insert multi-line text at (x, y) through LibreCAD's MText dialog."""
    if not text:
        raise LibreCADError("text must not be empty")
    with lc._lock:
        lc.run(["k"])
        r = lc.send_line("mt", expect_dialog=True)
        d, dlg = lc.wait_dialog(lambda d, w: w.element_info.class_name in ("QG_DlgMText", "QG_DlgText"),
                                timeout)
        if dlg is None:
            raise LibreCADError("The MText dialog did not open: " + r.summary())
        box = find_edit(dlg, class_name="QTextEdit") or find_edit(dlg, "Text")
        if box is None:
            click(dlg, "Cancel")
            raise LibreCADError("Could not find the text box in the MText dialog.")
        box.iface_value.SetValue(text)
        if height is not None:
            set_edit(dlg, "Height", fmt(height))
        if not click(dlg, "OK"):
            click(dlg, "Cancel")
            raise LibreCADError("Could not find the OK button in the MText dialog.")
        lc.wait_dialogs_closed(3)
        time.sleep(0.1)
        # The action only accepts a typed insertion point after the cursor was over the canvas once.
        lc.hover_graphic_view()
        time.sleep(0.08)
        r2 = lc.send_line(f"{fmt(x)},{fmt(y)}")
        r3 = lc.run(["k"])
        out = CommandResult(commands=["mt", text, f"{fmt(x)},{fmt(y)}", "k"],
                            output="\n".join(o for o in (r2.output, r3.output) if o),
                            prompt=r3.prompt, dialogs=r3.dialogs)
        if r3.dialogs:
            out.notes.append("a dialog is still open")
        return out


# --- modify tools with option dialogs -----------------------------------------

def _copies_options(dialog, copies: int) -> None:
    """Configure the Delete/Keep original + number-of-copies part of a modify dialog."""
    if copies <= 0:
        select_radio(dialog, "Delete Original")
    elif copies == 1:
        select_radio(dialog, "Keep Original")
    else:
        select_radio(dialog, "Multiple Copies")
        set_edit(dialog, "Number of copies", str(int(copies)))


def modify_selected(lc: LibreCAD, command: str, points: list[str],
                    configure: Optional[Callable[[object], None]] = None,
                    timeout: float = 5.0) -> CommandResult:
    """Run a modify command on the current selection.

    Flow: ``command`` -> confirm the selection step -> answer the point prompts
    -> fill the options dialog -> OK.
    """
    with lc._lock:
        lc.ensure()
        r = lc.send_line(command)
        outputs = [r.output]
        prompt = r.prompt
        if prompt.lower().startswith("select"):
            rc = lc.confirm_selection()
            outputs.append(rc.output)
            if not rc.completed:
                lc.run(["k"])
                return CommandResult(commands=[command], output="\n".join(o for o in outputs if o),
                                     prompt=lc.prompt(), completed=False,
                                     error="nothing is selected; call select_all first")
        for p in points[:-1]:
            rp = lc.send_line(p)
            outputs.append(rp.output)
        last = lc.send_line(points[-1], expect_dialog=True)
        outputs.append(last.output)
        d, dlg = lc.wait_dialog(timeout=timeout if last.dialogs else 0.8)
        dialog_title = d.title if d else ""
        if dlg is not None:
            if configure:
                try:
                    configure(dlg)
                except Exception as exc:  # keep going; OK with defaults
                    outputs.append(f"[warning] could not set dialog options: {exc}")
            if not click(dlg, "OK"):
                click(dlg, "Cancel")
                raise LibreCADError(f"Dialog {dialog_title!r} has no OK button.")
            lc.wait_dialogs_closed(timeout)
        time.sleep(0.1)
        res = CommandResult(commands=[command, *points], output="\n".join(o for o in outputs if o),
                            prompt=lc.prompt(), dialogs=[x.title for x in lc.dialogs()])
        if dialog_title:
            res.notes.append(f"options dialog: {dialog_title}")
        return res


def move_selected(lc: LibreCAD, dx: float, dy: float, copies: int = 0) -> CommandResult:
    return modify_selected(lc, "mv", ["0,0", f"{fmt(dx)},{fmt(dy)}"],
                           lambda dlg: _copies_options(dlg, copies))


def _unnamed_line_edits(dialog) -> list:
    """QLineEdits without an accessible name, in layout order (fields whose labels are not buddies)."""
    return [e for e in dialog.descendants(control_type="Edit")
            if e.element_info.class_name == "QLineEdit" and not _name(e)]


def rotate_selected(lc: LibreCAD, cx: float, cy: float, angle: float, copies: int = 0) -> CommandResult:
    """Rotate: centre, then a reference point equal to the centre makes LibreCAD open the
    options dialog directly, where the angle is typed."""
    def configure(dlg):
        _copies_options(dlg, copies)
        if not set_edit(dlg, "Angle", fmt(angle)):
            edits = _unnamed_line_edits(dlg)
            if edits:
                edits[0].iface_value.SetValue(fmt(angle))
    centre = f"{fmt(cx)},{fmt(cy)}"
    return modify_selected(lc, "ro", [centre, centre], configure)


def scale_selected(lc: LibreCAD, cx: float, cy: float, factor: float, factor_y: Optional[float] = None,
                   copies: int = 0) -> CommandResult:
    def configure(dlg):
        _copies_options(dlg, copies)
        isotropic = factor_y is None or abs(factor_y - factor) < 1e-9
        set_checkbox(dlg, "Isotropic Scaling", isotropic)
        edits = _unnamed_line_edits(dlg)  # [factor X, factor Y] in the dialog's layout order
        if edits:
            edits[0].iface_value.SetValue(fmt(factor))
        if not isotropic and len(edits) > 1:
            edits[1].iface_value.SetValue(fmt(factor_y))
    return modify_selected(lc, "sz", [f"{fmt(cx)},{fmt(cy)}"], configure)


def mirror_selected(lc: LibreCAD, x1: float, y1: float, x2: float, y2: float,
                    keep_original: bool = True) -> CommandResult:
    return modify_selected(lc, "mi", [f"{fmt(x1)},{fmt(y1)}", f"{fmt(x2)},{fmt(y2)}"],
                           lambda dlg: _copies_options(dlg, 1 if keep_original else 0))


def delete_selected(lc: LibreCAD) -> CommandResult:
    with lc._lock:
        lc.ensure()
        before = lc.status()
        r = lc.send_line("er")
        if not r.prompt.lower().startswith("select"):
            return r
        rc = lc.confirm_selection()
        if not rc.completed:
            lc.run(["k"])
            return CommandResult(commands=["er"], output=rc.output, prompt=lc.prompt(), completed=False,
                                 error="nothing is selected; call select_all first")
        after = lc.status()
        res = CommandResult(commands=["er", "<Enter>"], output=rc.output, prompt=after["prompt"])
        res.notes.append(f"selected before: {before['selected'] or '?'}, after: {after['selected'] or '0'}")
        return res


# --- file dialogs --------------------------------------------------------------

def _native_dialog(lc: LibreCAD, timeout: float):
    return lc.wait_dialog(lambda d, w: d.class_name == NATIVE_DIALOG_CLASS, timeout)


def _accept_button(dlg, name: str):
    """The real Open/Save button of a Windows file dialog (automation id "1"), not the
    split-button drop-down part that shares its name."""
    buttons = dlg.descendants(control_type="Button")
    for b in buttons:
        if b.element_info.automation_id == "1":
            return b
    for b in buttons:
        if _name(b).lower() == name.lower() and b.element_info.automation_id != "DropDown":
            return b
    return None


def _file_name_edit(dlg):
    return (find_edit(dlg, "File name") or find_edit(dlg, automation_id="1001")
            or find_edit(dlg, automation_id="1148"))


def _fill_file_dialog(lc: LibreCAD, dlg, path: str, button: str, timeout: float) -> None:
    # The dialog fills in its default name ("Untitled") shortly after opening; wait for the
    # box to exist and then make sure our path really landed before pressing the button.
    deadline = time.time() + min(timeout, 5.0)
    edit = None
    while edit is None and time.time() < deadline:
        edit = _file_name_edit(dlg)
        if edit is None:
            time.sleep(0.1)
    if edit is None:
        click(dlg, "Cancel")
        raise LibreCADError("File dialog has no file-name box.")
    time.sleep(0.4)
    # Setting the value through UIA is not enough: the dialog only notices text that
    # arrives as typed characters. Post WM_CHAR messages to the edit control's own
    # window handle (no keyboard focus needed), then press Enter in the box.
    edit_hwnd = edit.handle
    typed = False
    for _ in range(3):
        _user32.SendMessageW(edit_hwnd, EM_SETSEL, 0, -1)
        for ch in path:
            _user32.PostMessageW(edit_hwnd, WM_CHAR, ord(ch), 0)
        time.sleep(0.3)
        try:
            if edit.get_value() == path:
                typed = True
                break
        except Exception:
            pass
    if not typed:
        click(dlg, "Cancel")
        raise LibreCADError("Could not type the path into the file dialog.")
    _user32.PostMessageW(edit_hwnd, WM_KEYDOWN, VK_RETURN, 0x001C0001)
    _user32.PostMessageW(edit_hwnd, WM_KEYUP, VK_RETURN, 0xC01C0001)
    time.sleep(0.6)
    if any(d.hwnd == dlg.handle for d in lc.dialogs()):
        # Enter did not close it (e.g. autocomplete popup); press the real button.
        accept = _accept_button(dlg, button)
        if accept is not None:
            try:
                accept.iface_invoke.Invoke()
            except Exception:
                pass
    # Follow-up boxes: overwrite confirmation, or an error message.
    deadline = time.time() + timeout
    while time.time() < deadline:
        dialogs = lc.dialogs()
        if not dialogs:
            return
        for d in dialogs:
            if d.class_name != NATIVE_DIALOG_CLASS and not d.title:
                continue
            w = lc.dialog_wrapper(d.hwnd)
            if find_button(w, "Yes"):
                click(w, "Yes")
                time.sleep(0.3)
                continue
            if d.title and "Confirm" not in d.title and find_button(w, "OK") and not find_edit(w):
                texts = " ".join(dialog_texts(w))
                click(w, "OK")
                lc.wait_dialogs_closed(2)
                raise LibreCADError(f"LibreCAD reported: {texts.strip() or d.title}")
        time.sleep(0.1)
    raise LibreCADError("The file dialog did not close in time.")


def _is_save_as_button(name: str) -> bool:
    return name.startswith("Save") and name.endswith(" as")


def _is_save_button(name: str) -> bool:
    return name.startswith("Save") and not name.endswith(" as") and name != "Save All" \
        and not name.startswith("Save All")


def save_as(lc: LibreCAD, path: str, timeout: float = 10.0) -> str:
    path = os.path.abspath(path)
    if not os.path.splitext(path)[1]:
        path += ".dxf"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with lc._lock:
        lc.ensure()
        lc.run(["k"])
        lc.click_button(_is_save_as_button)
        d, dlg = _native_dialog(lc, timeout)
        if dlg is None:
            raise LibreCADError("The Save As dialog did not open (is the File toolbar visible?).")
        _fill_file_dialog(lc, dlg, path, "Save", timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(path) and os.path.basename(path).lower() in lc.active_document().lower():
                break
            time.sleep(0.1)
        if not os.path.exists(path):
            raise LibreCADError(f"LibreCAD did not write {path}; the active document is now "
                                f"'{lc.active_document()}'.")
        lc.remember_file(path)
        return path


def save(lc: LibreCAD, timeout: float = 6.0) -> str:
    with lc._lock:
        lc.ensure()
        current = lc.current_file()
        if not current:
            raise LibreCADError("The drawing has never been saved; call save_drawing with a path.")
        lc.run(["k"])
        lc.click_button(_is_save_button)
        d, dlg = _native_dialog(lc, 1.5)
        if dlg is not None:  # unexpected: LibreCAD asked for a name after all
            click(dlg, "Cancel")
            raise LibreCADError("LibreCAD opened a Save dialog; use save_drawing with a path.")
        time.sleep(0.3)
        return current


def open_file(lc: LibreCAD, path: str, timeout: float = 15.0) -> str:
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with lc._lock:
        lc.ensure()
        lc.run(["k"])
        lc.click_button("Open")
        d, dlg = _native_dialog(lc, timeout)
        if dlg is None:
            raise LibreCADError("The Open dialog did not open (is the File toolbar visible?).")
        _fill_file_dialog(lc, dlg, path, "Open", timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.basename(path).lower() in lc.active_document().lower():
                break
            time.sleep(0.15)
        lc.remember_file(path)
        return path


def new_drawing(lc: LibreCAD, timeout: float = 5.0) -> str:
    with lc._lock:
        lc.ensure()
        before = len(lc.documents())
        lc.click_button("New")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(lc.documents()) > before:
                break
            time.sleep(0.1)
        time.sleep(0.2)
        return lc.active_document()
