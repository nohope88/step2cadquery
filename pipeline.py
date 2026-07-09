#!/usr/bin/env python3
"""step2cadquery orchestrator: STEP file -> brief -> build.

For one local .step file:
  1. render_step.py    - measure bbox/volume + render 6 labeled view PNGs
  2. gen_brief_step.py - one vision session writes design_readme.md + brief.md
                         (faithful reconstruction, no creative candidates)
  3. gen_model.py      - a headless session builds the brief as a parametric
                         CadQuery project via the cadcode skill

Input:  path/to/part.step
Output: out/<slug>/ - main.py, params.py, spec.md, <slug>.step, <slug>.stl

Every invocation is a redo - each stage wipes its own stale outputs.
Stdlib-only; the dependency-bearing stages run under `uv run`.

Usage:
    python3 pipeline.py <file.step>
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
UV = ["uv", "run", "--python", "3.12"]


def run_stage(label: str, cmd: list[str]) -> tuple[int, str]:
    """Run a stage streaming its output; return (exit_code, stdout)."""
    print(f"\n=== {label} ===\n$ {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, cwd=str(HERE))
    lines = []
    for line in proc.stdout:
        print(line, end="", flush=True)
        lines.append(line)
    proc.wait()
    return proc.returncode, "".join(lines)


def last_json_line(output: str) -> dict:
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise RuntimeError("stage printed no JSON result line")


def is_quota_error(result: dict) -> bool:
    """Claude usage-window exhaustion -> temp failure (exit 75): retry later."""
    if result.get("limit_hit"):
        return True
    text = (result.get("error") or "") + " " + " ".join(result.get("verify", {}).get("problems") or [])
    return bool(re.search(r"usage limit|session limit|rate.?limit|quota|overloaded|529|too many requests", text, re.I))


def llm_stage(label: str, script: str, slug: str) -> tuple[bool, dict]:
    rc, out = run_stage(label, UV + ["--with", "claude-agent-sdk", "python3",
                                     str(HERE / script), slug])
    result = last_json_line(out)
    failed = rc != 0 or not (result["status"] == "done" and result.get("verify", {}).get("ok"))
    return failed, result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("step_file", help="path to a .step/.stp file")
    args = ap.parse_args()

    step_path = Path(args.step_file).resolve()
    if not step_path.is_file():
        print(f"no such file: {step_path}")
        return 1

    # 1) render + measure
    rc, out = run_stage("RENDER + MEASURE", UV + [
        "--with", "cadquery", "--with", "trimesh", "--with", "matplotlib",
        "python3", str(HERE / "render_step.py"), str(step_path)])
    if rc != 0:
        print(f"\n render stage failed (exit {rc})")
        return 1
    render = last_json_line(out)
    slug = render["slug"]
    print(f"\nrendered: {slug} bbox={render['bbox_mm']} solids={render['num_solids']}")

    # 1b) exact per-solid + per-station geometry (ground truth the brief
    # stage should read instead of eyeballing proportions from renders)
    rc, out = run_stage("CROSS-SECTIONS (measured geometry)", UV + [
        "--with", "cadquery", "--with", "trimesh", "--with", "numpy",
        "python3", str(HERE / "cross_sections.py"), slug])
    if rc != 0:
        print(f"\n cross-sections stage failed (exit {rc}) — continuing without it")
    else:
        cross = last_json_line(out)
        print(f"\n cross-sections: {cross['num_solids']} solids, {cross['num_stations']} stations")

    # 2) faithful-reconstruction brief (vision over the renders)
    brief_failed, brief = llm_stage("BRIEF (faithful reconstruction)", "gen_brief_step.py", slug)
    if brief_failed:
        print(f"\n brief stage failed: status={brief['status']} problems={brief.get('verify', {}).get('problems')}")
        return 75 if is_quota_error(brief) else 1
    print(f"\n brief ready ({brief['minutes']}min)")

    # 3) build (gen_model.py verifies + prunes on success)
    gen_failed, gen = llm_stage("BUILD (cadcode skill)", "gen_model.py", slug)
    if gen_failed:
        print(f"\n build failed: status={gen['status']} problems={gen.get('verify', {}).get('problems')}")
        return 75 if is_quota_error(gen) else 1
    print(f"\n DONE: {gen['out_dir']}  (turns={gen['turns']}, {gen['minutes']}min, "
          f"volume={gen['verify'].get('volume_mm3')})")

    # 4) fidelity report vs the source (informational — never fails the run).
    # Grade against the rebuilt .step, not the deliverable .stl: evaluate.py
    # re-tessellates a STEP at its own fine tolerance, so a STEP-vs-STEP
    # comparison measures true reconstruction fidelity instead of charging
    # the score for the .stl's coarser export mesh. Fall back to the .stl.
    out_files = list(Path(gen["out_dir"]).glob("*.step")) + list(Path(gen["out_dir"]).glob("*.stp"))
    out_files = out_files or sorted(Path(gen["out_dir"]).glob("*.stl"))
    if out_files:
        rc, out = run_stage("EVALUATE (fidelity vs source)", UV + [
            "--with", "cadquery", "--with", "trimesh", "--with", "scipy",
            "python3", str(HERE / "evaluate.py"), str(step_path), str(out_files[0])])
        if rc == 0:
            fidelity = last_json_line(out)
            fid_path = HERE / "text" / slug / "fidelity.json"
            fid_path.parent.mkdir(parents=True, exist_ok=True)
            fid_path.write_text(json.dumps(fidelity), encoding="utf-8")
            print(f"\n fidelity: score={fidelity['score']} chamfer={fidelity['chamfer_mm']}mm "
                  f"volume_err={fidelity['volume_err_pct']}%")
        else:
            print("\n evaluate failed (non-fatal)")
    else:
        print("\n no model file found to evaluate (non-fatal)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
