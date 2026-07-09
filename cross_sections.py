#!/usr/bin/env python3
"""Stage 0.5 — exact per-solid + per-station geometry from the STEP file.

The vision brief stage estimates feature positions and hull-profile stations
by eye from six renders (which carry no scale). This stage instead measures
them directly from the imported solids: each solid's own bounding box +
volume (so the brief states which features are actually unioned together,
not a guess) plus a station table sliced along the part's longest axis,
scoped to THAT solid alone — so a hull's gunwale line isn't contaminated by
a cabin sitting on top of it, and vice versa.

Output: text/<slug>/cross_sections.json
    principal_axis  the bbox-longest axis stations are sliced along
    solids: [{
        index, bbox_mm: {x:[min,max], y:[...], z:[...]}, volume_mm3,
        stations: [{<axis>: pos, <other1>_range: [min,max], <other2>_range: [min,max]}, ...]
    }, ...]
    stations are only emitted at global positions that fall inside that
    solid's own axis range.

Usage:
    uv run --python 3.12 --with cadquery --with trimesh --with numpy \
        python3 cross_sections.py <slug>
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEXT_DIR = HERE / "text"
AXES = "xyz"
# Longitudinal station density. 16 got a best-of-2 rebuild to chamfer ~1.0 mm
# (score ~94.8); denser stations let the loft track hull curvature tighter to
# shave the last of the chamfer term. Overridable via CROSS_SECTION_STATIONS.
NUM_STATIONS = int(os.environ.get("CROSS_SECTION_STATIONS", "24"))
STL_TOLERANCE = 0.02
STL_ANGULAR_TOLERANCE = 1.0


def _tessellate(shape):
    import cadquery as cq
    import trimesh

    with tempfile.TemporaryDirectory() as td:
        stl_path = Path(td) / "m.stl"
        cq.exporters.export(shape, str(stl_path), tolerance=STL_TOLERANCE,
                            angularTolerance=STL_ANGULAR_TOLERANCE)
        return trimesh.load(str(stl_path), force="mesh")


def _stations_for(mesh, axis: int, other: list, xs) -> list:
    stations = []
    normal = [0.0, 0.0, 0.0]
    normal[axis] = 1.0
    for x in xs:
        origin = [0.0, 0.0, 0.0]
        origin[axis] = float(x)
        section = mesh.section(plane_origin=origin, plane_normal=normal)
        if section is None or len(section.vertices) == 0:
            continue
        v = section.vertices
        vlo, vhi = v.min(axis=0), v.max(axis=0)
        stations.append({
            AXES[axis]: round(float(x), 2),
            f"{AXES[other[0]]}_range": [round(float(vlo[other[0]]), 2), round(float(vhi[other[0]]), 2)],
            f"{AXES[other[1]]}_range": [round(float(vlo[other[1]]), 2), round(float(vhi[other[1]]), 2)],
        })
    return stations


def compute(step_path: Path) -> dict:
    import cadquery as cq
    import numpy as np

    wp = cq.importers.importStep(str(step_path))
    solids = wp.solids().vals()

    bboxes = [s.BoundingBox() for s in solids]
    lo = np.array([min(bb.xmin for bb in bboxes), min(bb.ymin for bb in bboxes),
                   min(bb.zmin for bb in bboxes)])
    hi = np.array([max(bb.xmax for bb in bboxes), max(bb.ymax for bb in bboxes),
                   max(bb.zmax for bb in bboxes)])
    extents = hi - lo
    axis = int(np.argmax(extents))
    other = [i for i in range(3) if i != axis]
    inset = min(0.5, (hi[axis] - lo[axis]) * 0.02)
    xs_all = np.linspace(lo[axis] + inset, hi[axis] - inset, NUM_STATIONS)

    solid_info = []
    for i, (s, bb) in enumerate(zip(solids, bboxes)):
        try:
            vol = s.Volume()
        except Exception:  # noqa: BLE001 — volume is informative, not load-bearing
            vol = None
        axis_lo = [bb.xmin, bb.ymin, bb.zmin][axis]
        axis_hi = [bb.xmax, bb.ymax, bb.zmax][axis]
        xs = xs_all[(xs_all >= axis_lo) & (xs_all <= axis_hi)]
        mesh = _tessellate(s)
        solid_info.append({
            "index": i,
            "bbox_mm": {"x": [round(bb.xmin, 2), round(bb.xmax, 2)],
                        "y": [round(bb.ymin, 2), round(bb.ymax, 2)],
                        "z": [round(bb.zmin, 2), round(bb.zmax, 2)]},
            "volume_mm3": round(vol, 1) if vol is not None else None,
            "stations": _stations_for(mesh, axis, other, xs),
        })

    return {"principal_axis": AXES[axis], "solids": solid_info}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("slug", help="model slug — must exist under text/<slug>/ with a meta.json")
    args = ap.parse_args()

    text_dir = TEXT_DIR / args.slug
    meta_path = text_dir / "meta.json"
    if not meta_path.is_file():
        print(json.dumps({"status": "error", "error": f"no meta.json at {text_dir} — run render_step.py first"}))
        return 1

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    step_path = Path(meta["source_step"])
    result = compute(step_path)
    (text_dir / "cross_sections.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"status": "done", "slug": args.slug,
                      "num_solids": len(result["solids"]),
                      "num_stations": sum(len(s["stations"]) for s in result["solids"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
