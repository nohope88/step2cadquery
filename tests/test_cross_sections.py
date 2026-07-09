import json
import sys
import types

import numpy as np
import pytest

import cross_sections


class FakeBB:
    def __init__(self, xmin, xmax, ymin, ymax, zmin, zmax):
        self.xmin, self.xmax = xmin, xmax
        self.ymin, self.ymax = ymin, ymax
        self.zmin, self.zmax = zmin, zmax


class FakeSolid:
    def __init__(self, bb, volume=100.0, volume_raises=False):
        self._bb = bb
        self._volume = volume
        self._volume_raises = volume_raises

    def BoundingBox(self):
        return self._bb

    def Volume(self):
        if self._volume_raises:
            raise RuntimeError("no volume")
        return self._volume


class FakeSolids:
    def __init__(self, solids):
        self._solids = solids

    def vals(self):
        return self._solids


class FakeWP:
    def __init__(self, solids):
        self._solids = solids

    def solids(self):
        return FakeSolids(self._solids)


class FakeSection:
    def __init__(self, verts):
        self.vertices = np.asarray(verts, dtype=float)


class FakeMesh:
    """section() always returns the same small cross-section regardless of
    which solid it was tessellated from, so tests only need to check station
    *positions/count*, not per-solid geometry."""

    def section(self, plane_origin, plane_normal):
        return FakeSection([(plane_origin[0], -1.0, -0.5), (plane_origin[0], 1.0, 0.5)])


SOLIDS = [
    # x 0..20 -> spans the full global range -> gets every global station
    FakeSolid(FakeBB(0.0, 20.0, -2.0, 2.0, -1.0, 1.0), volume=50.0),
    # x 5..10 -> only the global stations that fall inside 5..10
    FakeSolid(FakeBB(5.0, 10.0, -1.0, 1.0, 0.0, 1.0), volume=5.0, volume_raises=True),
]


@pytest.fixture
def stub_deps(monkeypatch, tmp_path):
    cq = types.ModuleType("cadquery")
    cq.importers = types.SimpleNamespace(importStep=lambda p: FakeWP(SOLIDS))
    cq.exporters = types.SimpleNamespace(
        export=lambda wp, p, **kw: __import__("pathlib").Path(p).write_text("solid\n"))
    monkeypatch.setitem(sys.modules, "cadquery", cq)

    trimesh = types.ModuleType("trimesh")
    trimesh.load = lambda p, force: FakeMesh()
    monkeypatch.setitem(sys.modules, "trimesh", trimesh)
    return tmp_path


def test_compute_reports_per_solid_bbox_and_volume(stub_deps):
    result = cross_sections.compute(stub_deps / "part.step")
    assert result["principal_axis"] == "x"
    assert len(result["solids"]) == 2
    assert result["solids"][0]["bbox_mm"]["x"] == [0.0, 20.0]
    assert result["solids"][0]["volume_mm3"] == 50.0
    assert result["solids"][1]["volume_mm3"] is None  # Volume() raised, tolerated


def test_stations_scoped_to_each_solids_own_axis_range(stub_deps):
    result = cross_sections.compute(stub_deps / "part.step")
    full_span, narrow_span = result["solids"]

    assert len(full_span["stations"]) == cross_sections.NUM_STATIONS
    first = full_span["stations"][0]
    assert "x" in first and first["y_range"] == [-1.0, 1.0] and first["z_range"] == [-0.5, 0.5]

    assert 0 < len(narrow_span["stations"]) < cross_sections.NUM_STATIONS
    assert all(5.0 <= s["x"] <= 10.0 for s in narrow_span["stations"])


def test_main_writes_cross_sections_json(stub_deps, monkeypatch, capsys):
    text_dir = stub_deps / "text" / "myslug"
    text_dir.mkdir(parents=True)
    (text_dir / "meta.json").write_text(json.dumps({"source_step": str(stub_deps / "p.step")}))
    monkeypatch.setattr(cross_sections, "TEXT_DIR", stub_deps / "text")
    monkeypatch.setattr(sys, "argv", ["cross_sections.py", "myslug"])

    rc = cross_sections.main()
    out = json.loads(capsys.readouterr().out.strip())
    assert rc == 0 and out["status"] == "done" and out["num_solids"] == 2
    assert out["num_stations"] > 0
    saved = json.loads((text_dir / "cross_sections.json").read_text())
    assert saved["principal_axis"] == "x"


def test_main_missing_meta_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cross_sections, "TEXT_DIR", tmp_path / "text")
    monkeypatch.setattr(sys, "argv", ["cross_sections.py", "ghost"])
    rc = cross_sections.main()
    out = json.loads(capsys.readouterr().out.strip())
    assert rc == 1 and out["status"] == "error"
