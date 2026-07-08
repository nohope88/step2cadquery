import asyncio
import json
import sys

import pytest

import gen_model
from sdk_stub import (
    AssistantMessage,
    ProcessError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    make_sdk,
)


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(gen_model, "TEXT_DIR", tmp_path / "text")
    monkeypatch.setattr(gen_model, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(gen_model, "SESSIONS_DIR", tmp_path / "sessions")
    return tmp_path


@pytest.fixture
def sdk(monkeypatch):
    def install(**kwargs):
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", make_sdk(**kwargs))

    return install


def make_good_project(out_dir, slug="part", is_solid=True, warnings=(), sidecar=True):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "main.py").write_text("def gen_step(): pass\n")
    (out_dir / "params.py").write_text("L = 10\n")
    (out_dir / "spec.md").write_text("# Nice Part\n\nA nice part.\n")
    (out_dir / f"{slug}.step").write_text("ISO-10303-21;\n")
    (out_dir / f"{slug}.stl").write_text("solid part\n")
    if sidecar:
        (out_dir / f"{slug}.step.json").write_text(json.dumps({
            "volume_mm3": 123.4, "bbox": [1, 2, 3],
            "is_solid": is_solid, "warnings": list(warnings),
        }))
    return out_dir


# ---------- build_prompt ----------

def test_build_prompt_uses_brief_when_present(dirs):
    text_dir = gen_model.TEXT_DIR / "s"
    text_dir.mkdir(parents=True)
    (text_dir / "brief.md").write_text("# T\nbody\n")
    assert "AUTHORITATIVE" in gen_model.build_prompt("s")


def test_build_prompt_creativity_without_brief(dirs):
    (gen_model.TEXT_DIR / "s").mkdir(parents=True)
    assert "CREATIVITY" in gen_model.build_prompt("s")


# ---------- _collect_warnings ----------

def test_collect_warnings_merges_and_filters():
    meta = {"warnings": [{"kind": "sliver"}, "not-a-dict"],
            "validation": {"warnings": [{"kind": "collision"}]}}
    kinds = [w["kind"] for w in gen_model._collect_warnings(meta)]
    assert kinds == ["sliver", "collision"]


def test_collect_warnings_ignores_non_list():
    assert gen_model._collect_warnings({"warnings": "nope"}) == []


# ---------- verify ----------

def test_verify_missing_out_dir(dirs):
    v = gen_model.verify("nope")
    assert v == {"ok": False, "problems": ["output dir missing"]}


def test_verify_good_project(dirs):
    make_good_project(gen_model.OUT_DIR / "s")
    v = gen_model.verify("s")
    assert v["ok"] and v["volume_mm3"] == 123.4 and v["bbox"] == [1, 2, 3]


def test_verify_empty_project(dirs):
    (gen_model.OUT_DIR / "s").mkdir(parents=True)
    v = gen_model.verify("s")
    assert not v["ok"]
    for frag in ("no .step file", "no .stl file", "missing main.py",
                 "missing params.py", "missing spec.md", "no .step.json sidecar"):
        assert any(frag in p for p in v["problems"])


def test_verify_spec_without_h1(dirs):
    out = make_good_project(gen_model.OUT_DIR / "s")
    (out / "spec.md").write_text("no title here\n")
    assert "spec.md does not start with an H1 title" in gen_model.verify("s")["problems"]


def test_verify_unreadable_sidecar(dirs):
    out = make_good_project(gen_model.OUT_DIR / "s", sidecar=False)
    (out / "part.step.json").write_text("{broken")
    assert any("unreadable .step.json" in p for p in gen_model.verify("s")["problems"])


def test_verify_not_solid(dirs):
    make_good_project(gen_model.OUT_DIR / "s", is_solid=False)
    assert "is_solid=false" in gen_model.verify("s")["problems"]


def test_verify_missing_is_solid(dirs):
    out = make_good_project(gen_model.OUT_DIR / "s", sidecar=False)
    (out / "part.step.json").write_text(json.dumps({"volume_mm3": 1.0, "warnings": []}))
    assert "is_solid not recorded in sidecar" in gen_model.verify("s")["problems"]


def test_verify_nested_is_solid_false(dirs):
    out = make_good_project(gen_model.OUT_DIR / "s", sidecar=False)
    (out / "part.step.json").write_text(json.dumps(
        {"validation": {"is_solid": False, "warnings": []}}))
    assert "is_solid=false" in gen_model.verify("s")["problems"]


def test_verify_blocking_warnings(dirs):
    make_good_project(gen_model.OUT_DIR / "s",
                      warnings=[{"kind": "collision"}, {"kind": "thin", "severity": "warning"}])
    assert any("blocking warnings" in p for p in gen_model.verify("s")["problems"])


# ---------- prune ----------

def test_prune_strips_to_base_files(dirs):
    out = make_good_project(gen_model.OUT_DIR / "s")
    scratch = out / "part_review"
    scratch.mkdir()
    (scratch / "render.png").write_text("png")
    (out / "notes.txt").write_text("scratch")
    removed = gen_model.prune("s")
    assert "notes.txt" in removed and "part_review/render.png" in removed
    assert "part_review/" in removed  # emptied dir removed too
    kept = {p.name for p in out.iterdir()}
    assert kept == {"main.py", "params.py", "spec.md", "part.step", "part.stl"}
    assert "part.step.json" in removed  # sidecar is not a base file


# ---------- run_session ----------

def run(coro):
    return asyncio.run(coro)


def test_run_session_happy_path(dirs, sdk, capsys):
    sdk(messages=[
        SystemMessage(subtype="init", data={"session_id": "sid-1"}),
        AssistantMessage(content=[TextBlock(text="working on it"),
                                  ToolUseBlock(name="Bash", input={"command": "ls"})]),
        AssistantMessage(content=[TextBlock(text="   ")]),  # empty preview branch
        ResultMessage(session_id="sid-1", num_turns=7, is_error=False),
    ])
    r = run(gen_model.run_session("s", "prompt", "transcript"))
    assert r["status"] == "done" and r["turns"] == 7
    assert r["session_id"] == "sid-1" and not r["limit_hit"]
    assert (gen_model.SESSIONS_DIR / "s" / "transcript.jsonl").is_file()
    assert (gen_model.SESSIONS_DIR / "s" / "transcript.md").is_file()
    out = capsys.readouterr().out
    assert "session_id=sid-1" in out and "-> Bash" in out


def test_run_session_limit_banner_and_error_result(dirs, sdk):
    sdk(messages=[
        AssistantMessage(content=[TextBlock(text="You have hit your usage limit.")]),
        ResultMessage(session_id="x", num_turns=1, is_error=True, result="limit reached"),
    ])
    r = run(gen_model.run_session("s", "p", "t"))
    assert r["status"] == "error" and r["limit_hit"] and r["error"] == "limit reached"


def test_run_session_process_error(dirs, sdk):
    sdk(raise_exc=ProcessError("proc died"))
    r = run(gen_model.run_session("s", "p", "t"))
    assert r["status"] == "error" and "proc died" in r["error"]


def test_run_session_generic_exception(dirs, sdk):
    sdk(raise_exc=ValueError("boom"))
    r = run(gen_model.run_session("s", "p", "t"))
    assert r["status"] == "error" and "ValueError" in r["error"]


def test_run_session_old_sdk_without_buffer_knob(dirs, sdk):
    sdk(messages=[ResultMessage(session_id="x", num_turns=1, is_error=False)],
        strict_options=True)
    r = run(gen_model.run_session("s", "p", "t"))
    assert r["status"] == "done"


# ---------- generate ----------

def test_generate_requires_crawled_text(dirs):
    r = run(gen_model.generate("missing"))
    assert r["status"] == "error" and "no crawled text" in r["error"]


def _text_input(slug):
    text_dir = gen_model.TEXT_DIR / slug
    text_dir.mkdir(parents=True)
    (text_dir / "design_readme.md").write_text("# Obj\n")
    return text_dir


def test_generate_success_prunes(dirs, monkeypatch):
    _text_input("s")
    stale = gen_model.OUT_DIR / "s"  # pre-existing dir must be wiped
    stale.mkdir(parents=True)
    (stale / "old.txt").write_text("old")

    async def fake_session(slug, prompt, log_name, max_turns=0, model=""):
        out = make_good_project(gen_model.OUT_DIR / slug)
        (out / "scratch.txt").write_text("x")
        return {"status": "done", "slug": slug, "turns": 3, "minutes": 1.0,
                "session_id": "sid", "limit_hit": False}

    monkeypatch.setattr(gen_model, "run_session", fake_session)
    r = run(gen_model.generate("s"))
    assert r["status"] == "done" and r["verify"]["ok"]
    assert r["pruned"] >= 1 and r["out_dir"].endswith("s")
    assert not (gen_model.OUT_DIR / "s" / "old.txt").exists()


def test_generate_incomplete_when_verify_fails(dirs, monkeypatch):
    _text_input("s")

    async def fake_session(slug, prompt, log_name, max_turns=0, model=""):
        return {"status": "done", "slug": slug, "turns": 1, "minutes": 0.1,
                "session_id": "sid", "limit_hit": False}

    monkeypatch.setattr(gen_model, "run_session", fake_session)
    r = run(gen_model.generate("s"))
    assert r["status"] == "incomplete" and not r["verify"]["ok"]


# ---------- main ----------

def test_main_success(dirs, monkeypatch, capsys):
    _text_input("s")

    async def fake_session(slug, prompt, log_name, max_turns=0, model=""):
        make_good_project(gen_model.OUT_DIR / slug)
        return {"status": "done", "slug": slug, "turns": 3, "minutes": 1.0,
                "session_id": "sid", "limit_hit": False}

    monkeypatch.setattr(gen_model, "run_session", fake_session)
    monkeypatch.setattr(sys, "argv", ["gen_model.py", "s"])
    assert gen_model.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "done"


def test_main_failure_exit_code(dirs, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["gen_model.py", "missing"])
    assert gen_model.main() == 1
