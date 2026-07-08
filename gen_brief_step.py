#!/usr/bin/env python3
"""Stage 1 — renders + measurements → faithful-reconstruction design brief.

One headless Claude session Reads the six labeled view renders (vision), gets
the STEP file's measured bbox/volume embedded in its prompt as ground truth,
and writes:

    text/<slug>/design_readme.md  — vision description of the object
    text/<slug>/brief.md          — the build spec (gen_model.py's BRIEF_SECTION format)

No creative candidates: the goal is a faithful reconstruction of the object
that is actually in the STEP file.

Usage:
    uv run --python 3.12 --with claude-agent-sdk python3 gen_brief_step.py <slug>
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gen_model  # noqa: E402
from gen_model import HEADLESS_SESSION_WARNING, run_session  # noqa: E402

BRIEF_MAX_TURNS = 60
# The brief decides the build's direction — worth a stronger model than the
# build stage (a brief run is only ~3-5 min of turns).
BRIEF_MODEL = os.environ.get("BRIEF_MODEL", "claude-opus-4-8")


def build_brief_prompt(slug: str) -> str:
    text_dir = gen_model.TEXT_DIR / slug
    measurements = (text_dir / "measurements.json").read_text(encoding="utf-8").strip()
    return f"""{HEADLESS_SESSION_WARNING}

You are a CAD reverse-engineering analyst. A STEP file was rendered to images
from six labeled angles. Produce a FAITHFUL RECONSTRUCTION design brief that a
separate CAD-build agent will implement in parametric CadQuery. You write NO
CAD code — only the description and the brief. This is NOT a creative
reinterpretation: describe and specify the object exactly as it is.

GROUND TRUTH — measured from the STEP geometry itself (renders carry no scale
reference, so never second-guess these numbers from the images):
{measurements}

VOLUME SANITY CHECK — compare volume_mm3 against the solid envelope
(bbox x·y·z). A much smaller volume means the object is hollow, shelled or
openwork: infer wall thicknesses and cavity depths so a build following your
brief lands within ~15% of the measured volume, and state those numbers in
the brief.

INPUT — Read every PNG in `{text_dir}/showcase_images/`. Filenames carry the
view angle (iso, front, back, left, right, top). Study all six before writing.

VISION ANALYSIS — work view by view:
- identify WHAT the object is (use the WebSearch tool at most twice, only if
  you cannot confidently name it from the renders alone)
- overall form and proportions, mapped onto the measured bbox
- every distinct feature: holes, pockets, ribs, embossed/engraved details,
  fillets, curved surfaces — estimate each feature's dimensions as fractions
  of the measured envelope and state them in mm
- symmetries, and the print orientation the geometry implies

WRITE exactly two files:
1. `{text_dir}/design_readme.md` — the object description: H1 first line
   `# <object name>`, one paragraph on what it is/does, then a per-view
   geometric description and a feature list.
2. `{text_dir}/brief.md` — the build spec, exactly this format:
   - H1 first line: `# <object name>`
   - one paragraph: what it is (becomes the published description)
   - `## Features` — the feature list
   - `## Dimensions (mm)` — MUST restate the measured bbox verbatim, plus
     every derived feature dimension the builder needs
   - `## Printability` — orientation and how overhangs/bridges are handled
     on a 200x200 mm FDM bed
   - `## Build notes` — CadQuery construction plan: primitives, booleans,
     lofts/revolves, build order. Where the shape is organic or sculpted,
     specify an explicit parametric approximation with concrete section
     positions and sizes in mm — the builder cannot sculpt freeform
     surfaces. Use AT LEAST 8 loft stations for organic hulls/bodies and
     smooth spline/arc section profiles (not straight polylines) so the
     lofted surface reads smooth, not faceted.

FAITHFULNESS RULE: someone comparing the rebuilt model to these renders must
recognize the SAME object at the SAME size. Simplify only where parametric
CadQuery genuinely cannot follow, and note each simplification in the brief.

Write nothing else anywhere. Reply with ONE short line: the object name and
the measured bbox."""


def verify_brief(slug: str) -> dict:
    """Check the brief deliverables landed and are well-formed."""
    text_dir = gen_model.TEXT_DIR / slug
    problems = []

    brief = text_dir / "brief.md"
    if not brief.is_file():
        problems.append("brief.md missing")
    else:
        body = brief.read_text(encoding="utf-8")
        if not body.lstrip().startswith("# "):
            problems.append("brief.md does not start with an H1 title")
        if len(body) < 300:
            problems.append("brief.md suspiciously short")
        for section in ("## Dimensions", "## Build notes"):
            if section not in body:
                problems.append(f"brief.md missing '{section}' section")

    if not (text_dir / "design_readme.md").is_file():
        problems.append("design_readme.md missing")

    return {"ok": not problems, "problems": problems}


async def make_brief(slug: str) -> dict:
    text_dir = gen_model.TEXT_DIR / slug
    if not (text_dir / "measurements.json").is_file():
        return {"status": "error",
                "error": f"no measurements at {text_dir} — run render_step.py first"}
    if not list((text_dir / "showcase_images").glob("*.png")):
        return {"status": "error",
                "error": f"no renders at {text_dir}/showcase_images — run render_step.py first"}

    for stale in ("brief.md", "design_readme.md"):
        (text_dir / stale).unlink(missing_ok=True)  # a rerun is an intentional redo

    result = await run_session(slug, build_brief_prompt(slug), "brief_transcript",
                               max_turns=BRIEF_MAX_TURNS, model=BRIEF_MODEL)
    result["model"] = BRIEF_MODEL

    v = verify_brief(slug)
    result["verify"] = v
    if result["status"] == "done" and not v["ok"]:
        result["status"] = "incomplete"
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("slug", help="model slug — must exist under text/<slug>/")
    args = ap.parse_args()
    result = asyncio.run(make_brief(args.slug))
    print(json.dumps(result))
    return 0 if result["status"] == "done" and result.get("verify", {}).get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
