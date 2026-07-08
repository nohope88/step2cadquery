import json
import sys
import types

import pytest

import render_step


# ---------- fake cadquery / cadpy ----------

class FakeBB:
    xlen, ylen, zlen = 59.454, 30.214, 51.239


class FakeShape:
    def __init__(self, volume=15082.6, volume_raises=False):
        self._volume = volume
        self._volume_raises = volume_raises

    def BoundingBox(self):
        return FakeBB()

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
    def __init__(self, shapes, solids):
        self._shapes = shapes
        self._solids = solids

    def vals(self):
        return self._shapes

    def solids(self):
        return FakeSolids(self._solids)


def install_fakes(monkeypatch, shapes, solids, render_ok=True):
    cq = types.ModuleType("cadquery")
    cq.importers = types.SimpleNamespace(importStep=lambda p: FakeWP(shapes, solids))
    cq.exporters = types.SimpleNamespace(
        export=lambda wp, p: __import__("pathlib").Path(p).write_text("solid\n"))
    cq.Compound = types.SimpleNamespace(makeCompound=lambda shapes: FakeShape())
    monkeypatch.setitem(sys.modules, "cadquery", cq)

    cadpy = types.ModuleType("cadpy")
    render_part = types.ModuleType("cadpy.render_part")
    calls = []

    def fake_render(stl, png, *, views):
        calls.append((png, views))
        return png if render_ok else None

    render_part.render_stl_to_png = fake_render
    cadpy.render_part = render_part
    monkeypatch.setitem(sys.modules, "cadpy", cadpy)
    monkeypatch.setitem(sys.modules, "cadpy.render_part", render_part)
    return calls


@pytest.fixture
def text_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(render_step, "TEXT_DIR", tmp_path / "text")
    return tmp_path / "text"


def run_main(monkeypatch, step_file, capsys):
    monkeypatch.setattr(sys, "argv", ["render_step.py", str(step_file)])
    rc = render_step.main()
    return rc, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


# ---------- slugify ----------

@pytest.mark.parametrize("stem,slug", [
    ("3DBenchyStepFile", "3dbenchystepfile"),
    ("My Part_v2 (final)", "my-part-v2-final"),
    ("--weird--", "weird"),
])
def test_slugify(stem, slug):
    assert render_step.slugify(stem) == slug


# ---------- main ----------

def test_rejects_non_step_input(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "part.txt"
    bad.write_text("nope")
    rc, out = run_main(monkeypatch, bad, capsys)
    assert rc == 1 and "not a STEP file" in out["error"]


def test_rejects_missing_file(tmp_path, monkeypatch, capsys):
    rc, out = run_main(monkeypatch, tmp_path / "ghost.step", capsys)
    assert rc == 1 and "not a STEP file" in out["error"]


def test_rejects_empty_import(text_dir, tmp_path, monkeypatch, capsys):
    install_fakes(monkeypatch, shapes=[], solids=[])
    step = tmp_path / "part.step"
    step.write_text("ISO-10303-21;")
    rc, out = run_main(monkeypatch, step, capsys)
    assert rc == 1 and "no shapes" in out["error"]


def test_single_solid_success(text_dir, tmp_path, monkeypatch, capsys):
    shape = FakeShape()
    calls = install_fakes(monkeypatch, shapes=[shape], solids=[shape])
    step = tmp_path / "MyPart.step"
    step.write_text("ISO-10303-21;")
    rc, out = run_main(monkeypatch, step, capsys)
    assert rc == 0 and out["status"] == "done" and out["slug"] == "mypart"
    assert out["bbox_mm"] == {"x": 59.45, "y": 30.21, "z": 51.24}
    assert out["volume_mm3"] == 15082.6 and out["num_solids"] == 1
    assert len(calls) == len(render_step.VIEWS)
    assert out["renders"] == [f"{i:02d}_{label}.png"
                              for i, (label, _, _) in enumerate(render_step.VIEWS)]
    saved = json.loads((text_dir / "mypart" / "measurements.json").read_text())
    assert saved["bbox_mm"]["x"] == 59.45
    meta = json.loads((text_dir / "mypart" / "meta.json").read_text())
    assert meta["slug"] == "mypart" and meta["source_step"].endswith("MyPart.step")


def test_multi_solid_uses_compound_and_wipes_stale(text_dir, tmp_path, monkeypatch, capsys):
    shape = FakeShape()
    install_fakes(monkeypatch, shapes=[shape, shape], solids=[shape, shape])
    stale = text_dir / "mypart" / "showcase_images" / "99_old.png"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    step = tmp_path / "MyPart.step"
    step.write_text("ISO-10303-21;")
    rc, out = run_main(monkeypatch, step, capsys)
    assert rc == 0 and out["num_solids"] == 2
    assert out["volume_mm3"] == pytest.approx(2 * 15082.6)
    assert not stale.exists()


def test_volume_failure_is_tolerated(text_dir, tmp_path, monkeypatch, capsys):
    shape = FakeShape(volume_raises=True)
    install_fakes(monkeypatch, shapes=[shape], solids=[shape])
    step = tmp_path / "part.step"
    step.write_text("ISO-10303-21;")
    rc, out = run_main(monkeypatch, step, capsys)
    assert rc == 0 and out["volume_mm3"] is None


def test_no_solids_falls_back_to_compound_volume(text_dir, tmp_path, monkeypatch, capsys):
    shape = FakeShape(volume=42.0)
    install_fakes(monkeypatch, shapes=[shape], solids=[])
    step = tmp_path / "part.step"
    step.write_text("ISO-10303-21;")
    rc, out = run_main(monkeypatch, step, capsys)
    assert rc == 0 and out["volume_mm3"] == 42.0 and out["num_solids"] == 0


def test_render_failure_aborts(text_dir, tmp_path, monkeypatch, capsys):
    shape = FakeShape()
    install_fakes(monkeypatch, shapes=[shape], solids=[shape], render_ok=False)
    step = tmp_path / "part.step"
    step.write_text("ISO-10303-21;")
    rc, out = run_main(monkeypatch, step, capsys)
    assert rc == 1 and "render failed" in out["error"]
