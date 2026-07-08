#!/usr/bin/env python3
"""Stage 0 — STEP file → multi-angle renders + ground-truth measurements.

Imports the STEP with CadQuery, measures the exact bounding box / volume /
solid count (ground truth the brief stage must not second-guess from photos),
then tessellates to a temporary STL and renders six labeled single-view PNGs
with the cadcode skill's headless renderer.

Output (the layout the downstream brief + build stages expect):
    text/<slug>/showcase_images/00_iso.png ... 05_top.png
    text/<slug>/measurements.json
    text/<slug>/meta.json

Usage:
    uv run --python 3.12 --with cadquery --with trimesh --with matplotlib \
        python3 render_step.py <file.step>
"""

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEXT_DIR = HERE / "text"
CADPY_PACKAGES = Path.home() / ".claude" / "skills" / "cadcode" / "scripts" / "packages"

# (label, elev, azim) for mplot3d view_init — iso/top match the skill's
# DEFAULT_VIEWS; the four elevation-0 views walk around the object.
VIEWS = [
    ("iso", 24.0, -58.0),
    ("front", 0.0, -90.0),
    ("back", 0.0, 90.0),
    ("left", 0.0, 180.0),
    ("right", 0.0, 0.0),
    ("top", 89.0, -90.0),
]


def slugify(stem: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", stem.lower())).strip("-")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("step_file", help="path to a .step/.stp file")
    args = ap.parse_args()

    step_path = Path(args.step_file).resolve()
    if not step_path.is_file() or step_path.suffix.lower() not in (".step", ".stp"):
        print(json.dumps({"status": "error", "error": f"not a STEP file: {step_path}"}))
        return 1

    slug = slugify(step_path.stem)
    text_dir = TEXT_DIR / slug
    img_dir = text_dir / "showcase_images"
    img_dir.mkdir(parents=True, exist_ok=True)
    for stale in img_dir.glob("*.png"):  # a rerun is an intentional redo
        stale.unlink()

    import cadquery as cq

    wp = cq.importers.importStep(str(step_path))
    shapes = wp.vals()
    if not shapes:
        print(json.dumps({"status": "error", "error": "STEP import produced no shapes"}))
        return 1
    compound = shapes[0] if len(shapes) == 1 else cq.Compound.makeCompound(shapes)
    bb = compound.BoundingBox()
    solids = wp.solids().vals()
    try:
        volume = sum(s.Volume() for s in solids) if solids else compound.Volume()
    except Exception:  # noqa: BLE001 — volume is informative, not load-bearing
        volume = None

    measurements = {
        "bbox_mm": {"x": round(bb.xlen, 2), "y": round(bb.ylen, 2), "z": round(bb.zlen, 2)},
        "volume_mm3": round(volume, 1) if volume is not None else None,
        "num_solids": len(solids),
    }
    (text_dir / "measurements.json").write_text(
        json.dumps(measurements, indent=2), encoding="utf-8")
    (text_dir / "meta.json").write_text(json.dumps({
        "name": step_path.stem, "slug": slug, "source_step": str(step_path),
    }, indent=2), encoding="utf-8")

    sys.path.insert(0, str(CADPY_PACKAGES))
    from cadpy.render_part import render_stl_to_png

    renders = []
    with tempfile.TemporaryDirectory() as td:
        stl_path = Path(td) / "part.stl"
        cq.exporters.export(wp, str(stl_path))
        for i, (label, elev, azim) in enumerate(VIEWS):
            png = img_dir / f"{i:02d}_{label}.png"
            if render_stl_to_png(stl_path, png, views=[(label, elev, azim)]) is None:
                print(json.dumps({"status": "error", "error": f"render failed for view {label}"}))
                return 1
            renders.append(png.name)

    print(json.dumps({"status": "done", "slug": slug, "folder": str(text_dir),
                      "renders": renders, **measurements}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
