"""Tests for the DXF reader using a file generated with ezdxf."""

import ezdxf
import pytest

from librecad_mcp.dxf import read_dxf


@pytest.fixture()
def sample_dxf(tmp_path):
    doc = ezdxf.new("R2007")
    doc.layers.add("WALLS", color=1)
    msp = doc.modelspace()
    msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "WALLS"})
    msp.add_circle((50, 25), 20)
    msp.add_arc((0, 0), 10, 0, 90)
    msp.add_lwpolyline([(0, 0), (10, 0), (10, 10)], close=True)
    msp.add_text("Hello", dxfattribs={"insert": (5, 5), "height": 2.5})
    msp.add_point((3, 4))
    path = tmp_path / "sample.dxf"
    doc.saveas(path)
    return str(path)


def test_read_dxf_summary(sample_dxf):
    data = read_dxf(sample_dxf)
    assert data["entity_total"] == 6
    assert data["entity_counts"] == {"LINE": 1, "CIRCLE": 1, "ARC": 1, "LWPOLYLINE": 1, "TEXT": 1, "POINT": 1}
    assert any(layer["name"] == "WALLS" for layer in data["layers"])
    assert data["extents"]["min"] == [-20.0, -20.0] or data["extents"]["min"][0] <= 0
    assert data["extents"]["max"][0] >= 100
    line = next(e for e in data["entities"] if e["type"] == "LINE")
    assert line["start"] == [0.0, 0.0] and line["end"] == [100.0, 0.0] and line["length"] == 100.0
    assert line["layer"] == "WALLS"
    poly = next(e for e in data["entities"] if e["type"] == "LWPOLYLINE")
    assert poly["closed"] is True and len(poly["points"]) == 3
    text = next(e for e in data["entities"] if e["type"] == "TEXT")
    assert text["text"] == "Hello" and text["height"] == 2.5
    assert data["truncated"] is False


def test_read_dxf_truncation(sample_dxf):
    data = read_dxf(sample_dxf, max_entities=2)
    assert len(data["entities"]) == 2 and data["truncated"] is True


def test_missing_file():
    with pytest.raises(FileNotFoundError):
        read_dxf("Z:/does/not/exist.dxf")
