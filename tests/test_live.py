"""Live tests against a running LibreCAD. Skipped unless LIBRECAD_LIVE=1.

They create a new drawing, draw into it and leave it open (unsaved) so you
can inspect the result.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("LIBRECAD_LIVE") != "1",
                                reason="set LIBRECAD_LIVE=1 with LibreCAD running")


@pytest.fixture(scope="module")
def lc():
    from librecad_mcp.controller import LibreCAD
    from librecad_mcp import dialogs

    lc = LibreCAD()
    lc.connect()
    dialogs.new_drawing(lc)
    return lc


def test_status(lc):
    st = lc.status()
    assert st["running"] and st["prompt"]
    assert st["active_document"].startswith("unnamed document")


def test_draw_and_select(lc):
    from librecad_mcp import commands as C

    assert lc.run(C.rectangle(0, 0, 100, 50)).completed
    assert lc.run(C.circle(50, 25, 20)).completed
    r = lc.run(["sa"])
    assert r.completed
    assert lc.status()["selected"] not in ("", "0")
    lc.run(["tn"])


def test_unknown_command_is_reported(lc):
    r = lc.run(["definitelynotacommand"], finish=False)
    assert "Unknown command" in r.output


def test_delete_all(lc):
    from librecad_mcp import dialogs

    lc.run(["sa"])
    r = dialogs.delete_selected(lc)
    assert r.completed
    assert lc.status()["selected"] in ("", "0")
