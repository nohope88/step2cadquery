import json
import sys
import types

import numpy as np
import pytest

import evaluate


class FakeMesh:
    def __init__(self, pts, volume):
        self.pts = np.asarray(pts, dtype=float)
        self.volume = volume

    @property
    def bounds(self):
        return np.vstack([self.pts.min(axis=0), self.pts.max(axis=0)])

    def apply_translation(self, v):
        self.pts = self.pts + np.asarray(v, dtype=float)

    def sample(self, n):
        return self.pts

    def copy(self):
        return FakeMesh(self.pts.copy(), self.volume)

    def apply_transform(self, m):
        self.pts = (m[:3, :3] @ self.pts.T).T + m[:3, 3]


class FakeKDTree:
    """Brute-force stand-in for scipy.spatial.cKDTree."""

    def __init__(self, pts):
        self.pts = np.asarray(pts, dtype=float)

    def query(self, qs):
        qs = np.asarray(qs, dtype=float)
        d = np.linalg.norm(qs[:, None, :] - self.pts[None, :, :], axis=2).min(axis=1)
        return d, None


def rotation_matrix(angle, axis):
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[c, -s, 0, 0], [s, c, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])


@pytest.fixture(autouse=True)
def stub_geometry_deps(monkeypatch):
    spatial = types.ModuleType("scipy.spatial")
    spatial.cKDTree = FakeKDTree
    scipy = types.ModuleType("scipy")
    scipy.spatial = spatial
    monkeypatch.setitem(sys.modules, "scipy", scipy)
    monkeypatch.setitem(sys.modules, "scipy.spatial", spatial)

    transformations = types.ModuleType("trimesh.transformations")
    transformations.rotation_matrix = rotation_matrix
    trimesh = types.ModuleType("trimesh")
    trimesh.transformations = transformations
    monkeypatch.setitem(sys.modules, "trimesh", trimesh)
    monkeypatch.setitem(sys.modules, "trimesh.transformations", transformations)
    return trimesh


CUBE = [(0, 0, 0), (10, 0, 0), (0, 10, 0), (0, 0, 10),
        (10, 10, 0), (10, 0, 10), (0, 10, 10), (10, 10, 10)]


def test_identical_meshes_score_100():
    r = evaluate.evaluate(FakeMesh(CUBE, 1000.0), FakeMesh(CUBE, 1000.0))
    assert r["score"] == 100.0 and r["chamfer_mm"] == 0.0
    assert r["volume_err_pct"] == 0.0 and not r["flipped_180"]
    assert r["bbox_err_pct"] == {"x": 0.0, "y": 0.0, "z": 0.0}


def test_flip_detection():
    # an asymmetric point set, rebuilt facing 180° the other way about z
    src = [(1, 0, 0), (0, 1, 0), (0, 0, 0), (0, 0, 1), (-2, 0, 0)]
    rot = [(-x, -y, z) for x, y, z in src]
    r = evaluate.evaluate(FakeMesh(src, 5.0), FakeMesh(rot, 5.0))
    assert r["flipped_180"] and r["chamfer_mm"] == 0.0


def test_size_and_volume_errors_reduce_score():
    small = [(x * 0.8, y * 0.8, z * 0.8) for x, y, z in CUBE]
    r = evaluate.evaluate(FakeMesh(CUBE, 1000.0), FakeMesh(small, 500.0))
    assert r["score"] < 80.0
    assert r["volume_err_pct"] == 50.0
    assert r["bbox_err_pct"]["x"] == 20.0 and r["chamfer_mm"] > 0


def test_load_mesh_stl(stub_geometry_deps, tmp_path):
    stl = tmp_path / "m.stl"
    stl.write_text("solid\n")
    stub_geometry_deps.load = lambda p, force: ("loaded", p)
    assert evaluate.load_mesh(stl) == ("loaded", str(stl))


def test_load_mesh_step_via_cadquery(stub_geometry_deps, tmp_path, monkeypatch):
    step = tmp_path / "m.step"
    step.write_text("ISO-10303-21;")
    cq = types.ModuleType("cadquery")
    cq.importers = types.SimpleNamespace(importStep=lambda p: "wp")
    cq.exporters = types.SimpleNamespace(
        export=lambda wp, p: __import__("pathlib").Path(p).write_text("solid\n"))
    monkeypatch.setitem(sys.modules, "cadquery", cq)
    stub_geometry_deps.load = lambda p, force: ("tessellated", p)
    result = evaluate.load_mesh(step)
    assert result[0] == "tessellated" and result[1].endswith("m.stl")


def test_main(monkeypatch, capsys, tmp_path):
    a, b = tmp_path / "a.stl", tmp_path / "b.stl"
    a.write_text("solid")
    b.write_text("solid")
    monkeypatch.setattr(evaluate, "load_mesh", lambda p: FakeMesh(CUBE, 1000.0))
    monkeypatch.setattr(sys, "argv", ["evaluate.py", str(a), str(b)])
    assert evaluate.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["score"] == 100.0 and out["source"] == str(a)
