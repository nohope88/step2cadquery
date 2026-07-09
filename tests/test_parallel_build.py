import asyncio
import json

import pytest

import gen_model
import parallel_build


def run(coro):
    return asyncio.run(coro)


# ---------- last_json_line ----------

def test_last_json_line_picks_last():
    assert parallel_build.last_json_line('noise\n{"a": 1}\n{"score": 9}\n') == {"score": 9}


def test_last_json_line_raises_without_json():
    with pytest.raises(RuntimeError, match="no JSON result line"):
        parallel_build.last_json_line("just noise\n")


# ---------- best_candidate ----------

def test_best_candidate_picks_highest_score():
    results = [
        {"name": "a", "ok": True, "score": 88.0},
        {"name": "b", "ok": True, "score": 94.5},
        {"name": "c", "ok": True, "score": 91.0},
    ]
    assert parallel_build.best_candidate(results)["name"] == "b"


def test_best_candidate_ignores_failed_and_unscored():
    results = [
        {"name": "a", "ok": False, "score": None},
        {"name": "b", "ok": True, "score": None},   # built but evaluate failed
        {"name": "c", "ok": True, "score": 70.0},
    ]
    assert parallel_build.best_candidate(results)["name"] == "c"


def test_best_candidate_none_when_all_fail():
    results = [{"name": "a", "ok": False, "score": None},
               {"name": "b", "ok": True, "score": None}]
    assert parallel_build.best_candidate(results) is None


# ---------- orchestrate ----------

@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(gen_model, "TEXT_DIR", tmp_path / "text")
    monkeypatch.setattr(gen_model, "OUT_DIR", tmp_path / "out")
    return tmp_path


def _make_candidate_output(name):
    """Simulate one finished build's out/<name>/ on disk."""
    d = gen_model.OUT_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.step").write_text("ISO-10303-21;")
    (d / "main.py").write_text("def gen_step(): pass\n")
    return d


def test_orchestrate_promotes_best(dirs, monkeypatch):
    (gen_model.TEXT_DIR / "s").mkdir(parents=True)
    (gen_model.TEXT_DIR / "s" / "meta.json").write_text(json.dumps({"source_step": "/src.step"}))

    scores = {"s__c0": 88.0, "s__c1": 95.0, "s__c2": 90.0}

    async def fake_build(slug, name):
        _make_candidate_output(name)
        return {"name": name, "ok": True, "score": scores[name],
                "fidelity": {"score": scores[name], "chamfer_mm": 1.0}}

    monkeypatch.setattr(parallel_build, "build_candidate", fake_build)
    result = run(parallel_build.orchestrate("s", 3))

    assert result["winner"] == "s__c1" and result["best_score"] == 95.0
    assert result["promoted_to"].endswith("out/s")
    # winner's files copied to out/s AND renamed from the candidate suffix
    assert (gen_model.OUT_DIR / "s" / "s.step").is_file()
    assert not (gen_model.OUT_DIR / "s" / "s__c1.step").exists()
    saved = json.loads((gen_model.TEXT_DIR / "s" / "fidelity.json").read_text())
    assert saved["score"] == 95.0
    assert [c["score"] for c in result["candidates"]] == [88.0, 95.0, 90.0]


def test_rename_candidate_files_strips_suffix(dirs):
    dest = gen_model.OUT_DIR / "s"
    (dest / "s__c0_parts").mkdir(parents=True)
    (dest / "s__c0.step").write_text("x")
    (dest / "s__c0.stl").write_text("x")
    (dest / "s__c0_parts" / "s__c0_hull.stl").write_text("x")
    (dest / "main.py").write_text("x")  # non-candidate files untouched
    parallel_build._rename_candidate_files(dest, "s__c0", "s")
    names = sorted(str(p.relative_to(dest)) for p in dest.rglob("*"))
    assert "s.step" in names and "s.stl" in names
    assert "s_parts/s_hull.stl" in names and "main.py" in names
    assert not any("__c0" in n for n in names)


def test_orchestrate_no_winner_when_all_fail(dirs, monkeypatch):
    (gen_model.TEXT_DIR / "s").mkdir(parents=True)

    async def fake_build(slug, name):
        return {"name": name, "ok": False, "score": None}

    monkeypatch.setattr(parallel_build, "build_candidate", fake_build)
    result = run(parallel_build.orchestrate("s", 2))
    assert result["winner"] is None and result["best_score"] is None
    assert result["promoted_to"] is None
    assert not (gen_model.OUT_DIR / "s").exists()
