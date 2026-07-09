import asyncio
import json
import sys

import pytest

import gen_brief_step
import gen_model


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(gen_model, "TEXT_DIR", tmp_path / "text")
    monkeypatch.setattr(gen_model, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(gen_model, "SESSIONS_DIR", tmp_path / "sessions")
    return tmp_path


MEASUREMENTS = {"bbox_mm": {"x": 59.45, "y": 30.21, "z": 51.24},
                "volume_mm3": 15082.6, "num_solids": 2}

GOOD_BRIEF = """# Benchy

A calibration boat.

## Features
- hull

## Dimensions (mm)
- x 59.45, y 30.21, z 51.24 — measured ground truth restated for the builder

## Printability
- keel down, no supports needed on a standard FDM bed

## Build notes
- loft hull sections at fixed stations, then cut windows and add the funnel
"""


def make_stage0_output(slug, with_renders=True):
    text_dir = gen_model.TEXT_DIR / slug
    (text_dir / "showcase_images").mkdir(parents=True)
    (text_dir / "measurements.json").write_text(json.dumps(MEASUREMENTS))
    if with_renders:
        (text_dir / "showcase_images" / "00_iso.png").write_bytes(b"png")
    return text_dir


def run(coro):
    return asyncio.run(coro)


# ---------- build_brief_prompt ----------

def test_prompt_embeds_measurements_and_paths(dirs):
    text_dir = make_stage0_output("s")
    prompt = gen_brief_step.build_brief_prompt("s")
    assert "59.45" in prompt and "GROUND TRUTH" in prompt
    assert str(text_dir / "showcase_images") in prompt
    assert "FAITHFUL RECONSTRUCTION" in prompt
    assert '"volume_mm3": 42.0' not in prompt  # no cross_sections.json written


def test_prompt_embeds_cross_sections_when_present(dirs):
    text_dir = make_stage0_output("s")
    cross = {"principal_axis": "x", "solids": [{"index": 0, "bbox_mm": {"x": [0, 10]},
                                                "volume_mm3": 42.0, "stations": []}]}
    (text_dir / "cross_sections.json").write_text(json.dumps(cross))
    prompt = gen_brief_step.build_brief_prompt("s")
    assert "MEASURED CROSS-SECTIONS" in prompt and '"volume_mm3": 42.0' in prompt
    assert "This is ground truth:" in prompt


# ---------- verify_brief ----------

def test_verify_all_good(dirs):
    text_dir = make_stage0_output("s")
    (text_dir / "brief.md").write_text(GOOD_BRIEF)
    (text_dir / "design_readme.md").write_text("# Benchy\n")
    assert gen_brief_step.verify_brief("s") == {"ok": True, "problems": []}


def test_verify_missing_everything(dirs):
    make_stage0_output("s")
    v = gen_brief_step.verify_brief("s")
    assert not v["ok"]
    assert "brief.md missing" in v["problems"]
    assert "design_readme.md missing" in v["problems"]


def test_verify_malformed_brief(dirs):
    text_dir = make_stage0_output("s")
    (text_dir / "brief.md").write_text("no h1, too short")
    v = gen_brief_step.verify_brief("s")
    assert "brief.md does not start with an H1 title" in v["problems"]
    assert "brief.md suspiciously short" in v["problems"]
    assert "brief.md missing '## Dimensions' section" in v["problems"]
    assert "brief.md missing '## Build notes' section" in v["problems"]


# ---------- make_brief ----------

def test_make_brief_requires_measurements(dirs):
    r = run(gen_brief_step.make_brief("missing"))
    assert r["status"] == "error" and "no measurements" in r["error"]


def test_make_brief_requires_renders(dirs):
    make_stage0_output("s", with_renders=False)
    r = run(gen_brief_step.make_brief("s"))
    assert r["status"] == "error" and "no renders" in r["error"]


def test_make_brief_success_deletes_stale(dirs, monkeypatch):
    text_dir = make_stage0_output("s")
    (text_dir / "brief.md").write_text("stale")  # must be wiped before the session

    async def fake_session(slug, prompt, log_name, max_turns=0, model=""):
        assert not (text_dir / "brief.md").exists()  # stale file was deleted
        (text_dir / "brief.md").write_text(GOOD_BRIEF)
        (text_dir / "design_readme.md").write_text("# Benchy\n")
        return {"status": "done", "slug": slug, "turns": 5, "minutes": 2.0,
                "session_id": "sid", "limit_hit": False}

    monkeypatch.setattr(gen_brief_step, "run_session", fake_session)
    r = run(gen_brief_step.make_brief("s"))
    assert r["status"] == "done" and r["verify"]["ok"]
    assert r["model"] == gen_brief_step.BRIEF_MODEL


def test_make_brief_incomplete_when_files_missing(dirs, monkeypatch):
    make_stage0_output("s")

    async def fake_session(slug, prompt, log_name, max_turns=0, model=""):
        return {"status": "done", "slug": slug, "turns": 1, "minutes": 0.5,
                "session_id": "sid", "limit_hit": False}

    monkeypatch.setattr(gen_brief_step, "run_session", fake_session)
    r = run(gen_brief_step.make_brief("s"))
    assert r["status"] == "incomplete" and not r["verify"]["ok"]


# ---------- main ----------

def test_main_success(dirs, monkeypatch, capsys):
    text_dir = make_stage0_output("s")

    async def fake_session(slug, prompt, log_name, max_turns=0, model=""):
        (text_dir / "brief.md").write_text(GOOD_BRIEF)
        (text_dir / "design_readme.md").write_text("# Benchy\n")
        return {"status": "done", "slug": slug, "turns": 5, "minutes": 2.0,
                "session_id": "sid", "limit_hit": False}

    monkeypatch.setattr(gen_brief_step, "run_session", fake_session)
    monkeypatch.setattr(sys, "argv", ["gen_brief_step.py", "s"])
    assert gen_brief_step.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "done"


def test_main_failure_exit_code(dirs, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["gen_brief_step.py", "missing"])
    assert gen_brief_step.main() == 1
