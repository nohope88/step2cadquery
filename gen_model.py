#!/usr/bin/env python3
"""Stage 2 — text → CadQuery model folder.

Spawns one headless Claude session (Claude Agent SDK) that reads the crawled
text description + reference images in text/<slug>/ and, following the cadcode
skill (~/.claude/skills/cadcode), designs an original parametric CadQuery
project at out/<slug>/ with STEP + STL artifacts.

Usage:
    uv run --python 3.12 --with claude-agent-sdk python3 gen_model.py <slug>

Exit 0 and a final JSON line {"status": "done", ...} when the deliverables
verify (main.py, params.py, spec.md, .step, .stl, is_solid, no blocking
warnings); the project is then pruned to base files ready for import.
"""

import argparse
import asyncio
import dataclasses
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEXT_DIR = HERE / "text"
OUT_DIR = HERE / "out"
SESSIONS_DIR = HERE / "sessions"

MODEL = os.environ.get("GEN_MODEL", "claude-sonnet-5")
MAX_TURNS = int(os.environ.get("GEN_MAX_TURNS", "250"))
PERMISSION_MODE = "bypassPermissions"  # unattended headless run

CADCODE_SKILL = Path.home() / ".claude" / "skills" / "cadcode"
CAD_CMD = f"uv run --python 3.12 --with cadquery python3 {CADCODE_SKILL}/scripts/cad"
REVIEW_CMD = f"uv run --python 3.12 --with cadquery python3 {CADCODE_SKILL}/scripts/review"

BASE_EXTS = {".py", ".step", ".stp", ".stl"}
KEEP_FILES = {"spec.md"}  # Panda importer reads its H1 as title, first paragraph as description
BLOCKING_WARNINGS = {"disconnected_bodies", "collision", "sliver", "invalid_brep", "empty", "check_failed"}

HEADLESS_SESSION_WARNING = """IMPORTANT — this is a one-shot, unattended, headless session (spawned via the
Claude Agent SDK's `query()`, not the interactive Claude Code CLI). There is no
future turn that will "wake you up": tools like ScheduleWakeup, or a Monitor call
that waits on a background process and expects to notify you later, DO NOTHING
useful here — if you call them and then stop issuing tool calls, your turn simply
ends and this session terminates as if you were finished, even though the task
isn't done. If you need to wait on a slow command, wait for it SYNCHRONOUSLY
inside a single Bash call and keep issuing tool calls until the work is actually
complete. Never end your turn assuming something will check back on you later."""


CREATIVITY_SECTION = """CREATIVITY — the crawled product is inspiration, NOT a spec to clone:
- Keep its functional purpose (what the object is for and must do), but design
  your OWN fresher take: a different geometric family, a smarter feature set, or
  a bolder form language. Example: a plain XYZ calibration cube -> a faceted
  calibration polyhedron with embossed numbers on its faces and per-face
  overhang/bridge test features.
- FIRST use the WebSearch tool to research the category: typical real-world
  dimensions, popular variants, and what makes the top designs interesting.
  2-3 searches are enough — pick one clear creative direction and commit; do
  not rabbit-hole.
- Printability and the skill's non-negotiables beat novelty on every conflict:
  the result must still be a clean, watertight, parametric design that prints
  well on a 200x200 mm FDM bed.
- Name YOUR design in spec.md — a fresh title for what you actually made, not
  a repeat of the source title — and let the one-paragraph description mention
  the creative twist."""

BRIEF_SECTION = """DESIGN BRIEF — `{brief_path}` is the AUTHORITATIVE spec. It was selected from
scored, ranked candidate concepts and already fixes the creative direction:
title, concept, features, dimensions, and printability plan. Read it right
after the readme and BUILD EXACTLY THAT design:
- Do NOT invent a different concept or add/drop major features — the creative
  decisions are made; your job is a faithful, high-quality parametric build.
- Use the brief's dimensions as params.py values; use WebSearch only if a
  dimension you need is missing from the brief.
- spec.md's H1 title must be the brief's title, and its one-paragraph
  description should match the brief's concept.
- Printability and the skill's non-negotiables still beat the brief on any
  conflict — deviate minimally and note it in a code comment."""


def build_prompt(slug: str) -> str:
    text_dir = TEXT_DIR / slug
    out_dir = OUT_DIR / slug
    brief_path = text_dir / "brief.md"
    direction_section = (
        BRIEF_SECTION.format(brief_path=brief_path) if brief_path.is_file() else CREATIVITY_SECTION
    )
    return f"""{HEADLESS_SESSION_WARNING}

You are designing an ORIGINAL parametric CadQuery model INSPIRED BY a real
3D-printable product, working from its text description and reference photos
only — you have NO source geometry files and must not download any.

INPUT — read these first:
- `{text_dir}/design_readme.md` — the product's title, summary, description,
  tags, category, and print settings (num_pieces is a strong hint for part count).
- Every image in `{text_dir}/showcase_images/` — Read each one; the photos show
  the object's purpose, proportions, and style. Estimate real-world dimensions
  in mm from context (typical product sizes) and state them as assumptions in
  params.py comments.

{direction_section}

SKILL — Read `{CADCODE_SKILL}/SKILL.md` in full and follow it: its loop
(understand -> plan -> write .py -> run -> inspect renders -> fix), its
non-negotiables, and its cadlib helpers. Load its references/patterns only as
needed. This is a "new part" task per the skill's classification.

OUTPUT — create the project at `{out_dir}` (create the directory). Prefer a FLAT
single-part project: spec.md, params.py, validation.py, main.py with `gen_step()`
at module scope. All dimensions live in params.py. Use a project subfolder layout
(parts/, assemblies/) only if the object is genuinely multi-part.

TOOLS — generate artifacts with:
  {CAD_CMD} {out_dir}
and visually review with:
  {REVIEW_CMD} {out_dir}
then Read every PNG it produces and fix what looks wrong. Iterate until the cad
JSON reports ok=true, is_solid=true, and the warnings array has no blocking or
functional entries. Delete the *_review/ folder and any scratch renders when done.

spec.md is the Panda importer's ONLY source for the published title and
description. It must be exactly: a single H1 first line `# <human-readable
product title>` (real words, e.g. `# Ringosaurus Ring Holder`, NOT the folder
slug), then ONE short paragraph (1-2 sentences) describing what the object is
and does. No other sections.

Work only inside `{out_dir}` (plus reading `{text_dir}` and the skill). Do NOT
write skill-feedback or pitfalls files. When the model verifies clean, reply
with ONE short line: the project path and the final bbox + volume."""


def _collect_warnings(meta: dict) -> list[dict]:
    """All warning entries from a .step.json, wherever cadpy nested them."""
    found = []
    for container in (meta, meta.get("validation") or {}):
        w = container.get("warnings")
        if isinstance(w, list):
            found.extend(x for x in w if isinstance(x, dict))
    return found


def verify(slug: str) -> dict:
    """Check deliverables on disk — a session can end without finishing."""
    out_dir = OUT_DIR / slug
    problems = []
    if not out_dir.is_dir():
        return {"ok": False, "problems": ["output dir missing"]}

    files = list(out_dir.rglob("*"))
    names = [p.name.lower() for p in files if p.is_file()]
    steps = [p for p in files if p.suffix.lower() in (".step", ".stp")]
    if not steps:
        problems.append("no .step file")
    if not any(n.endswith(".stl") for n in names):
        problems.append("no .stl file")
    for req in ("main.py", "params.py", "spec.md"):
        if not (out_dir / req).is_file():
            problems.append(f"missing {req}")

    spec = out_dir / "spec.md"
    if spec.is_file() and not spec.read_text(encoding="utf-8").lstrip().startswith("# "):
        problems.append("spec.md does not start with an H1 title")

    meta_info = {}
    sidecars = [p for p in files if p.name.endswith(".step.json")]
    if not sidecars:
        problems.append("no .step.json sidecar (cad tool never succeeded?)")
    else:
        try:
            meta = json.loads(sidecars[0].read_text(encoding="utf-8"))
            meta_info = {"volume_mm3": meta.get("volume_mm3"), "bbox": meta.get("bbox")}
            is_solid = meta.get("is_solid")
            if is_solid is None:
                is_solid = (meta.get("validation") or {}).get("is_solid")
            if is_solid is False:
                problems.append("is_solid=false")
            blocking = [
                w for w in _collect_warnings(meta)
                if w.get("kind") in BLOCKING_WARNINGS or w.get("severity") == "warning"
            ]
            if blocking:
                problems.append(f"blocking warnings: {[w.get('kind') for w in blocking]}")
        except (json.JSONDecodeError, OSError) as e:
            problems.append(f"unreadable .step.json: {e}")

    return {"ok": not problems, "problems": problems, **meta_info}


def prune(slug: str) -> list[str]:
    """Strip the project to importable base files (.py/.step/.stp/.stl + spec.md),
    mirroring the old pipeline's strip_to_base_files. Returns removed paths."""
    out_dir = OUT_DIR / slug
    removed = []
    for p in sorted(out_dir.rglob("*"), key=lambda x: -len(x.parts)):
        if p.is_file():
            if p.suffix.lower() in BASE_EXTS or p.name in KEEP_FILES:
                continue
            p.unlink()
            removed.append(str(p.relative_to(out_dir)))
        elif p.is_dir() and not any(p.iterdir()):
            p.rmdir()
            removed.append(str(p.relative_to(out_dir)) + "/")
    return removed


async def run_session(slug: str, prompt: str, log_name: str, max_turns: int = MAX_TURNS,
                      model: str = MODEL) -> dict:
    """Generic headless session runner shared by the build and brief stages.
    Logs to sessions/<slug>/<log_name>.{jsonl,md}; returns status/turns/minutes."""
    from claude_agent_sdk import (  # imported here so verify/prune stay importable without the SDK
        AssistantMessage,
        ClaudeAgentOptions,
        ProcessError,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ToolUseBlock,
        query,
    )

    log_dir = SESSIONS_DIR / slug
    log_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = log_dir / f"{log_name}.jsonl"
    md_path = log_dir / f"{log_name}.md"

    opt_kwargs = dict(
        cwd=str(HERE),
        permission_mode=PERMISSION_MODE,
        model=model,
        max_turns=max_turns,
    )
    try:
        # reading several showcase photos in one turn overflows the SDK's 1MB default
        options = ClaudeAgentOptions(**opt_kwargs, max_buffer_size=32 * 1024 * 1024)
    except TypeError:  # older SDK without the knob
        options = ClaudeAgentOptions(**opt_kwargs)

    # The usage-limit banner only appears as assistant text, never in the error
    # field — detect it here so callers can treat the failure as temporary.
    # Also covers gateway-side exhaustion (OpenRouter daily key limit → 403s).
    limit_re = re.compile(
        r"hit your (session|usage|weekly) limit|usage limit reached|limit resets|"
        r"Key limit exceeded|Failed to authenticate|API Error: 4(01|02|03|29)|"
        r"credit|rate.?limit|overloaded",
        re.I,
    )
    limit_hit = False

    start = time.time()
    status, error_text, session_id, num_turns = "unknown", None, None, 0
    with jsonl_path.open("a", encoding="utf-8") as jf, md_path.open("a", encoding="utf-8") as mf:
        mf.write(f"\n\n---\n# Run started {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        try:
            async for message in query(prompt=prompt, options=options):
                jf.write(json.dumps(dataclasses.asdict(message), default=str) + "\n")
                jf.flush()
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    session_id = message.data.get("session_id")
                    print(f"    [{slug}] session_id={session_id}", flush=True)
                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            mf.write(block.text + "\n\n")
                            if limit_re.search(block.text):
                                limit_hit = True
                            preview = block.text.strip().replace("\n", " ")[:160]
                            if preview:
                                print(f"    [{slug}] {preview}", flush=True)
                        elif isinstance(block, ToolUseBlock):
                            mf.write(f"\n> **tool:** `{block.name}` {json.dumps(block.input)[:300]}\n\n")
                            print(f"    [{slug}] -> {block.name}", flush=True)
                elif isinstance(message, ResultMessage):
                    session_id = message.session_id or session_id
                    num_turns = message.num_turns
                    status = "error" if message.is_error else "done"
                    if message.is_error:
                        error_text = message.result
        except ProcessError as exc:
            status, error_text = "error", str(exc)
        except Exception as exc:  # noqa: BLE001 — log and report, don't crash the pipeline
            status, error_text = "error", repr(exc)

    elapsed = time.time() - start
    result = {"status": status, "slug": slug, "turns": num_turns,
              "minutes": round(elapsed / 60, 1), "session_id": session_id,
              "limit_hit": limit_hit}
    if error_text:
        result["error"] = error_text[:500]
    return result


async def generate(slug: str) -> dict:
    text_dir = TEXT_DIR / slug
    if not (text_dir / "design_readme.md").is_file():
        return {"status": "error", "error": f"no crawled text at {text_dir} — run crawl_text.py first"}

    out_dir = OUT_DIR / slug
    if out_dir.exists():
        shutil.rmtree(out_dir)  # a rerun is an intentional redo

    result = await run_session(slug, build_prompt(slug), "transcript")

    v = verify(slug)
    result["verify"] = v
    if result["status"] == "done" and v["ok"]:
        result["pruned"] = len(prune(slug))
        result["out_dir"] = str(OUT_DIR / slug)
    elif result["status"] == "done":
        result["status"] = "incomplete"  # session reported success but deliverables fail
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("slug", help="model slug — must exist under text/<slug>/")
    args = ap.parse_args()
    result = asyncio.run(generate(args.slug))
    print(json.dumps(result))
    return 0 if result["status"] == "done" and result.get("verify", {}).get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
