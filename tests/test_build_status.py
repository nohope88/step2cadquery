import json
import os
import time

import pytest

import build_status


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(build_status, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(build_status, "SESSIONS_DIR", tmp_path / "sessions")
    (tmp_path / "out").mkdir()
    (tmp_path / "sessions").mkdir()
    return tmp_path


def write_log(dirs, name, text):
    d = build_status.SESSIONS_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "build.log").write_text(text)


def write_out(dirs, name, *, step=False, stl=False, score=None):
    d = build_status.OUT_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    if step:
        (d / f"{name}.step").write_text("ISO-10303-21;")
    if stl:
        (d / f"{name}.stl").write_text("solid")
    if score is not None:
        (d / "fidelity.json").write_text(json.dumps({"score": score}))


DONE_LOG = ('    [s__c0] -> Bash\n    [s__c0] -> Write\n'
            '{"status": "done", "slug": "s", "turns": 60, "verify": {"ok": true}}\n')
INCOMPLETE_LOG = ('    [s__c1] -> Bash\n'
                  '{"status": "done", "slug": "s", "verify": {"ok": false, "problems": ["no .step file"]}}\n')
ERROR_LOG = ('    [s__c2] -> Bash\n    [s__c2] You have hit your usage limit\n'
             '{"status": "error", "limit_hit": true, "verify": {"ok": false}}\n')
RUNNING_LOG = '    [s__c3] session_id=x\n    [s__c3] -> Bash\n    [s__c3] -> Read\n    [s__c3] writing files\n'


def test_done_candidate_reports_score_and_artifacts(dirs):
    write_log(dirs, "s__c0", DONE_LOG)
    write_out(dirs, "s__c0", step=True, stl=True, score=94.8)
    st = build_status.candidate_status("s__c0")
    assert st["state"] == "done" and st["score"] == 94.8
    assert st["step"] and st["stl"] and st["turns"] == 2


def test_incomplete_candidate(dirs):
    write_log(dirs, "s__c1", INCOMPLETE_LOG)
    st = build_status.candidate_status("s__c1")
    assert st["state"] == "incomplete" and st["score"] is None


def test_error_candidate_flags_limit(dirs):
    write_log(dirs, "s__c2", ERROR_LOG)
    st = build_status.candidate_status("s__c2")
    assert st["state"] == "error" and "usage limit hit" in st["last"]


def test_running_candidate_no_final_json(dirs, monkeypatch):
    monkeypatch.setattr(build_status, "_worker_alive", lambda name: False)
    write_log(dirs, "s__c3", RUNNING_LOG)  # fresh mtime -> running via grace window
    st = build_status.candidate_status("s__c3")
    assert st["state"] == "running" and st["turns"] == 2
    assert st["last"] == "writing files"


def _age_log(name):
    log = build_status.SESSIONS_DIR / name / "build.log"
    ancient = time.time() - 10 * build_status.GRACE_AFTER_LAST_LOG_S
    os.utime(log, (ancient, ancient))


def test_alive_worker_is_running_even_when_log_is_old(dirs, monkeypatch):
    write_log(dirs, "s__c4", RUNNING_LOG)
    _age_log("s__c4")  # beyond the grace window
    monkeypatch.setattr(build_status, "_worker_alive", lambda name: True)
    assert build_status.candidate_status("s__c4")["state"] == "running"


def test_dead_worker_old_log_is_stalled(dirs, monkeypatch):
    write_log(dirs, "s__c5", RUNNING_LOG)
    _age_log("s__c5")
    monkeypatch.setattr(build_status, "_worker_alive", lambda name: False)
    assert build_status.candidate_status("s__c5")["state"] == "stalled"


def test_discover_and_collect(dirs):
    write_log(dirs, "s__c0", DONE_LOG)
    write_out(dirs, "s__c0", step=True, score=90.0)
    write_log(dirs, "s__c1", RUNNING_LOG)
    assert build_status.discover("s") == ["s__c0", "s__c1"]
    rows = build_status.collect("s")
    assert [r["name"] for r in rows] == ["s__c0", "s__c1"]


def test_all_done_exit_code(dirs, monkeypatch, capsys):
    write_log(dirs, "s__c0", DONE_LOG)
    write_log(dirs, "s__c1", ERROR_LOG)
    monkeypatch.setattr("sys.argv", ["build_status.py", "s", "--all-done"])
    assert build_status.main() == 0  # both terminal

    write_log(dirs, "s__c2", RUNNING_LOG)
    assert build_status.main() == 1  # c2 still running


def test_oneline_and_json_output(dirs, monkeypatch, capsys):
    write_log(dirs, "s__c0", DONE_LOG)
    write_out(dirs, "s__c0", step=True, score=93.2)
    monkeypatch.setattr("sys.argv", ["build_status.py", "s", "--oneline"])
    assert build_status.main() == 0
    assert "c0:done" in capsys.readouterr().out

    monkeypatch.setattr("sys.argv", ["build_status.py", "s", "--json"])
    assert build_status.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidates"][0]["score"] == 93.2


def test_no_candidates(dirs, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["build_status.py", "ghost"])
    assert build_status.main() == 0
    assert "no candidates" in capsys.readouterr().out
