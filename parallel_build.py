#!/usr/bin/env python3
"""Best-of-N parallel build — converge on a high-fidelity model faster.

The build stage (a headless Claude session) is the pipeline's slow, stochastic
step: one run of `gen_model.py` might land at score 91, another at 96. Instead
of running builds one at a time, this spawns N independent builds CONCURRENTLY
(each into its own out/<slug>__cN/ + sessions/<slug>__cN/ so they don't
collide), scores every finished candidate against the source STEP with
evaluate.py, and promotes the highest-scoring one to out/<slug>/.

Wall-clock ≈ one build; fidelity ≈ the best of N. Cost ≈ N× the LLM budget, so
size N to what your usage window allows.

Prerequisite: text/<slug>/ already has design_readme.md + brief.md + meta.json
(run render_step.py -> cross_sections.py -> gen_brief_step.py first, or just
`pipeline.py` up through the brief).

Usage:
    uv run --python 3.12 --with claude-agent-sdk python3 parallel_build.py <slug> [--candidates N]
"""

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gen_model  # noqa: E402

UV = ["uv", "run", "--python", "3.12"]
EVAL_DEPS = ["--with", "cadquery", "--with", "trimesh", "--with", "scipy"]
DEFAULT_CANDIDATES = 3


def last_json_line(output: str) -> dict:
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise RuntimeError("no JSON result line")


def best_candidate(results: list[dict]) -> dict | None:
    """Highest-scoring candidate whose build verified and scored. `results`
    items look like {name, ok, score, fidelity?}. Returns None if none
    produced a score."""
    scored = [r for r in results if r.get("ok") and r.get("score") is not None]
    if not scored:
        return None
    return max(scored, key=lambda r: r["score"])


async def _run(cmd: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=str(HERE))
    out, _ = await proc.communicate()
    return proc.returncode, out.decode("utf-8", "replace")


async def _run_streaming(cmd: list[str], log_path: Path) -> tuple[int, str]:
    """Like _run but tees the child's output to `log_path` line-by-line as it
    arrives, so build_status.py can report live progress for each candidate."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=str(HERE))
    lines = []
    with log_path.open("w", encoding="utf-8") as f:
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace")
            f.write(line)
            f.flush()
            lines.append(line)
    await proc.wait()
    return proc.returncode, "".join(lines)


async def build_candidate(slug: str, name: str) -> dict:
    """Run one gen_model build into out/<name>/, then score it against the
    source STEP. Returns {name, ok, score, fidelity, problems?}."""
    log_path = gen_model.SESSIONS_DIR / name / "build.log"
    rc, out = await _run_streaming(UV + ["--with", "claude-agent-sdk", "python3",
                                         str(HERE / "gen_model.py"), slug, "--out-name", name],
                                   log_path)
    try:
        gen = last_json_line(out)
    except RuntimeError:
        return {"name": name, "ok": False, "score": None, "error": "build printed no JSON"}
    if not (gen.get("status") == "done" and gen.get("verify", {}).get("ok")):
        return {"name": name, "ok": False, "score": None,
                "problems": gen.get("verify", {}).get("problems"), "limit_hit": gen.get("limit_hit")}

    source_step = json.loads((gen_model.TEXT_DIR / slug / "meta.json").read_text())["source_step"]
    steps = list((gen_model.OUT_DIR / name).glob("*.step")) or list((gen_model.OUT_DIR / name).glob("*.stl"))
    if not steps:
        return {"name": name, "ok": False, "score": None, "error": "no model file to score"}
    rc, out = await _run(UV + EVAL_DEPS + ["python3", str(HERE / "evaluate.py"),
                                           source_step, str(steps[0])])
    if rc != 0:
        return {"name": name, "ok": True, "score": None, "error": "evaluate failed"}
    fidelity = last_json_line(out)
    # drop the candidate's authoritative score next to its build so
    # build_status.py can report final per-candidate scores as they land.
    (gen_model.OUT_DIR / name / "fidelity.json").write_text(json.dumps(fidelity), encoding="utf-8")
    return {"name": name, "ok": True, "score": fidelity["score"], "fidelity": fidelity}


def _rename_candidate_files(dest: Path, winner_name: str, slug: str) -> None:
    """The winner's artifacts carry its candidate name (e.g. slug__c0.step);
    rename the promoted copies to the plain slug so out/<slug>/ looks like a
    normal single build (<slug>.step / .stl / _parts/ etc.)."""
    for p in sorted(dest.rglob(f"{winner_name}*"), key=lambda x: -len(x.parts)):
        new = p.with_name(p.name.replace(winner_name, slug))
        if new != p:
            p.rename(new)


async def orchestrate(slug: str, n: int) -> dict:
    names = [f"{slug}__c{i}" for i in range(n)]
    results = await asyncio.gather(*(build_candidate(slug, name) for name in names))
    results = list(results)
    winner = best_candidate(results)

    promoted = None
    if winner:
        dest = gen_model.OUT_DIR / slug
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(gen_model.OUT_DIR / winner["name"], dest)
        _rename_candidate_files(dest, winner["name"], slug)
        fid_path = gen_model.TEXT_DIR / slug / "fidelity.json"
        fid_path.parent.mkdir(parents=True, exist_ok=True)
        fid_path.write_text(json.dumps(winner["fidelity"]), encoding="utf-8")
        promoted = str(dest)

    return {
        "slug": slug,
        "candidates": [{"name": r["name"], "ok": r["ok"], "score": r.get("score")} for r in results],
        "winner": winner["name"] if winner else None,
        "best_score": winner["score"] if winner else None,
        "promoted_to": promoted,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("slug", help="model slug — must exist under text/<slug>/ with a brief")
    ap.add_argument("--candidates", type=int, default=DEFAULT_CANDIDATES,
                    help=f"number of parallel builds (default {DEFAULT_CANDIDATES})")
    args = ap.parse_args()
    result = asyncio.run(orchestrate(args.slug, args.candidates))
    print(json.dumps(result))
    return 0 if result["winner"] else 1


if __name__ == "__main__":
    sys.exit(main())
