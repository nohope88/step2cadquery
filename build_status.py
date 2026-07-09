#!/usr/bin/env python3
"""Live status of a best-of-N parallel build (for monitoring).

Reads each candidate's streamed log at sessions/<slug>__cN/build.log (written
by parallel_build.py) plus its out/<slug>__cN/ dir, and reports per candidate:

    state    running | done | incomplete | error | pending
    turns    tool calls so far (a rough progress proxy)
    step/stl whether artifacts exist yet
    score    fidelity score once the build's self-eval / final JSON reports it
    last     last thing the build said (truncated)

Usage:
    python3 build_status.py <slug>              # human table
    python3 build_status.py <slug> --oneline    # one compact line (for a watch loop)
    python3 build_status.py <slug> --json       # machine-readable
    python3 build_status.py <slug> --all-done   # exit 0 iff every candidate finished

A candidate is any out/<slug>__c* or sessions/<slug>__c* on disk.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "out"
SESSIONS_DIR = HERE / "sessions"
# Process liveness is the source of truth for "running"; the log-quiet window is
# only a grace fallback for the brief gap between a worker exiting and its final
# JSON landing. Headless LLM builds go quiet for minutes (model reasoning, image
# reads, long cad subprocesses), so this must be generous, not tight.
GRACE_AFTER_LAST_LOG_S = 600


def _worker_alive(name: str) -> bool:
    """True if a gen_model build process for this candidate is still running."""
    try:
        r = subprocess.run(["pgrep", "-f", f"out-name {name}"],
                           capture_output=True, text=True)
        return r.returncode == 0 and bool(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def _final_result(log_text: str) -> dict | None:
    """The last JSON object line gen_model printed, if the run finished."""
    for line in reversed(log_text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and '"status"' in line:
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                return None
    return None


def _last_activity(log_text: str) -> str:
    for line in reversed(log_text.splitlines()):
        s = line.strip()
        if s and not s.startswith("{"):
            return s.split("] ", 1)[-1][:70]
    return ""


def candidate_status(name: str) -> dict:
    log_path = SESSIONS_DIR / name / "build.log"
    out_dir = OUT_DIR / name
    st = {"name": name, "state": "pending", "turns": 0, "step": False,
          "stl": False, "score": None, "last": ""}

    st["step"] = bool(list(out_dir.glob("*.step"))) if out_dir.is_dir() else False
    st["stl"] = bool(list(out_dir.glob("*.stl"))) if out_dir.is_dir() else False

    # parallel_build writes the candidate's authoritative score here once it has
    # built AND been scored against the source.
    fid_path = out_dir / "fidelity.json"
    if fid_path.is_file():
        try:
            st["score"] = json.loads(fid_path.read_text(encoding="utf-8")).get("score")
        except (json.JSONDecodeError, OSError):
            pass

    if not log_path.is_file():
        return st

    text = log_path.read_text(encoding="utf-8", errors="replace")
    st["turns"] = text.count("] -> ")  # each "[name] -> Tool" line is a tool call
    st["last"] = _last_activity(text)

    result = _final_result(text)
    if result is not None:
        # authoritative: the build printed its final JSON, so it is terminal
        status = result.get("status")
        st["state"] = {"done": "done"}.get(status, status or "error")
        v = result.get("verify") or {}
        if v.get("ok") is False:
            st["state"] = "incomplete" if status == "done" else st["state"]
        if result.get("limit_hit"):
            st["last"] = "usage limit hit — " + st["last"]
    elif _worker_alive(name):
        st["state"] = "running"  # process is alive, however quiet the log is
    elif time.time() - log_path.stat().st_mtime < GRACE_AFTER_LAST_LOG_S:
        st["state"] = "running"  # just exited; final JSON not flushed yet
    else:
        st["state"] = "stalled"  # process gone, no result, long quiet -> crashed

    return st


def discover(slug: str) -> list[str]:
    names = set()
    for base in (OUT_DIR, SESSIONS_DIR):
        if base.is_dir():
            names.update(p.name for p in base.glob(f"{slug}__c*") if p.is_dir())
    return sorted(names)


def collect(slug: str) -> list[dict]:
    return [candidate_status(n) for n in discover(slug)]


def format_table(rows: list[dict]) -> str:
    if not rows:
        return "(no candidates yet)"
    out = [f"{'candidate':<18} {'state':<10} {'turns':>5} {'step':>4} {'stl':>4} {'score':>6}  last"]
    for r in rows:
        score = "" if r["score"] is None else f"{r['score']:.1f}"
        out.append(f"{r['name']:<18} {r['state']:<10} {r['turns']:>5} "
                   f"{'Y' if r['step'] else '-':>4} {'Y' if r['stl'] else '-':>4} "
                   f"{score:>6}  {r['last']}")
    return "\n".join(out)


def format_oneline(rows: list[dict]) -> str:
    if not rows:
        return "no candidates"
    parts = []
    for r in rows:
        score = "" if r["score"] is None else f"={r['score']:.1f}"
        parts.append(f"{r['name'].split('__')[-1]}:{r['state']}(t{r['turns']}{score})")
    return " | ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("slug")
    ap.add_argument("--oneline", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all-done", action="store_true",
                    help="exit 0 iff every candidate reached a terminal state")
    args = ap.parse_args()

    rows = collect(args.slug)
    terminal = {"done", "incomplete", "error", "stalled"}

    if args.all_done:
        done = bool(rows) and all(r["state"] in terminal for r in rows)
        return 0 if done else 1
    if args.json:
        print(json.dumps({"slug": args.slug, "candidates": rows}))
    elif args.oneline:
        print(format_oneline(rows))
    else:
        print(format_table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
