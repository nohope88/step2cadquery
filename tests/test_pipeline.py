import json
import sys

import pytest

import pipeline


# ---------- run_stage ----------

def test_run_stage_streams_and_returns_output(capsys):
    rc, out = pipeline.run_stage("ECHO", [sys.executable, "-c", "print('hello'); print('{\"a\": 1}')"])
    assert rc == 0 and "hello" in out and '{"a": 1}' in out
    printed = capsys.readouterr().out
    assert "=== ECHO ===" in printed and "hello" in printed


def test_run_stage_nonzero_exit():
    rc, _ = pipeline.run_stage("FAIL", [sys.executable, "-c", "import sys; sys.exit(3)"])
    assert rc == 3


# ---------- last_json_line ----------

def test_last_json_line_picks_last():
    out = 'noise\n{"a": 1}\nmore noise\n  {"b": 2}\n'
    assert pipeline.last_json_line(out) == {"b": 2}


def test_last_json_line_raises_without_json():
    with pytest.raises(RuntimeError, match="no JSON result line"):
        pipeline.last_json_line("just noise\n")


# ---------- is_quota_error ----------

@pytest.mark.parametrize("result,expected", [
    ({"limit_hit": True}, True),
    ({"error": "API overloaded, retry"}, True),
    ({"verify": {"problems": ["usage limit reached banner seen"]}}, True),
    ({"error": "genuine geometry failure"}, False),
    ({}, False),
])
def test_is_quota_error(result, expected):
    assert pipeline.is_quota_error(result) is expected


# ---------- llm_stage ----------

def test_llm_stage_success(monkeypatch):
    canned = json.dumps({"status": "done", "verify": {"ok": True}})
    monkeypatch.setattr(pipeline, "run_stage", lambda label, cmd: (0, canned + "\n"))
    failed, result = pipeline.llm_stage("L", "gen_brief_step.py", "s")
    assert not failed and result["status"] == "done"


def test_llm_stage_failure_on_bad_status(monkeypatch):
    canned = json.dumps({"status": "incomplete", "verify": {"ok": False}})
    monkeypatch.setattr(pipeline, "run_stage", lambda label, cmd: (0, canned + "\n"))
    failed, _ = pipeline.llm_stage("L", "gen_brief_step.py", "s")
    assert failed


def test_llm_stage_failure_on_exit_code(monkeypatch):
    canned = json.dumps({"status": "done", "verify": {"ok": True}})
    monkeypatch.setattr(pipeline, "run_stage", lambda label, cmd: (1, canned + "\n"))
    failed, _ = pipeline.llm_stage("L", "gen_brief_step.py", "s")
    assert failed


# ---------- main ----------

RENDER_JSON = json.dumps({"status": "done", "slug": "s",
                          "bbox_mm": {"x": 1, "y": 2, "z": 3}, "num_solids": 1})
BRIEF_OK = {"status": "done", "verify": {"ok": True}, "minutes": 2.0}
GEN_OK = {"status": "done", "verify": {"ok": True, "volume_mm3": 9.0},
          "minutes": 10.0, "turns": 42, "out_dir": "/out/s"}


def set_argv(monkeypatch, step):
    monkeypatch.setattr(sys, "argv", ["pipeline.py", str(step)])


def test_main_missing_file(tmp_path, monkeypatch, capsys):
    set_argv(monkeypatch, tmp_path / "ghost.step")
    assert pipeline.main() == 1
    assert "no such file" in capsys.readouterr().out


def test_main_render_failure(tmp_path, monkeypatch):
    step = tmp_path / "p.step"
    step.write_text("x")
    set_argv(monkeypatch, step)
    monkeypatch.setattr(pipeline, "run_stage", lambda label, cmd: (1, ""))
    assert pipeline.main() == 1


EVAL_JSON = json.dumps({"score": 90.0, "chamfer_mm": 1.0, "chamfer_pct": 1.2,
                        "volume_err_pct": 5.0, "flipped_180": False})


def make_stage_mocks(monkeypatch, brief=(False, BRIEF_OK), gen=(False, GEN_OK), eval_rc=0):
    def fake_run_stage(label, cmd):
        if label.startswith("EVALUATE"):
            return eval_rc, EVAL_JSON + "\n"
        return 0, RENDER_JSON + "\n"

    monkeypatch.setattr(pipeline, "run_stage", fake_run_stage)
    results = {"gen_brief_step.py": brief, "gen_model.py": gen}
    monkeypatch.setattr(pipeline, "llm_stage",
                        lambda label, script, slug: results[script])


def test_main_success_without_stl_skips_eval(tmp_path, monkeypatch, capsys):
    step = tmp_path / "p.step"
    step.write_text("x")
    set_argv(monkeypatch, step)
    make_stage_mocks(monkeypatch)  # GEN_OK's out_dir has no .stl on disk
    assert pipeline.main() == 0
    out = capsys.readouterr().out
    assert "DONE: /out/s" in out and "turns=42" in out
    assert "no .stl found to evaluate" in out


def test_main_success_with_fidelity_report(tmp_path, monkeypatch, capsys):
    step = tmp_path / "p.step"
    step.write_text("x")
    out_dir = tmp_path / "out" / "s"
    out_dir.mkdir(parents=True)
    (out_dir / "s.stl").write_text("solid")
    set_argv(monkeypatch, step)
    make_stage_mocks(monkeypatch, gen=(False, dict(GEN_OK, out_dir=str(out_dir))))
    monkeypatch.setattr(pipeline, "HERE", tmp_path)
    assert pipeline.main() == 0
    assert "fidelity: score=90.0" in capsys.readouterr().out
    saved = json.loads((tmp_path / "text" / "s" / "fidelity.json").read_text())
    assert saved["score"] == 90.0


def test_main_evaluate_failure_is_non_fatal(tmp_path, monkeypatch, capsys):
    step = tmp_path / "p.step"
    step.write_text("x")
    out_dir = tmp_path / "out" / "s"
    out_dir.mkdir(parents=True)
    (out_dir / "s.stl").write_text("solid")
    set_argv(monkeypatch, step)
    make_stage_mocks(monkeypatch, gen=(False, dict(GEN_OK, out_dir=str(out_dir))), eval_rc=1)
    assert pipeline.main() == 0
    assert "evaluate failed (non-fatal)" in capsys.readouterr().out


def test_main_brief_quota_failure_exits_75(tmp_path, monkeypatch):
    step = tmp_path / "p.step"
    step.write_text("x")
    set_argv(monkeypatch, step)
    make_stage_mocks(monkeypatch, brief=(True, {"status": "error", "limit_hit": True}))
    assert pipeline.main() == 75


def test_main_brief_hard_failure_exits_1(tmp_path, monkeypatch):
    step = tmp_path / "p.step"
    step.write_text("x")
    set_argv(monkeypatch, step)
    make_stage_mocks(monkeypatch, brief=(True, {"status": "error", "error": "bad geometry"}))
    assert pipeline.main() == 1


def test_main_build_failure_exits_1(tmp_path, monkeypatch):
    step = tmp_path / "p.step"
    step.write_text("x")
    set_argv(monkeypatch, step)
    make_stage_mocks(monkeypatch, gen=(True, {"status": "incomplete",
                                              "verify": {"ok": False, "problems": ["no .step file"]}}))
    assert pipeline.main() == 1
