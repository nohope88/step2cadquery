#!/usr/bin/env python3
"""Fidelity check: how close is a rebuilt model to the source STEP?

Aligns both meshes on their bbox centers (also trying a 180° flip about z, in
case the rebuild faced the other way) and reports:

    bbox_err_pct   per-axis bounding-box error (%)
    volume_err_pct volume error (%)
    chamfer_mm     bidirectional nearest-neighbor surface distance (mean, mm)
    chamfer_pct    chamfer as % of the source bbox diagonal
    score          0-100 fidelity score (100 = identical)

Usage:
    uv run --python 3.12 --with cadquery --with trimesh --with scipy \
        python3 evaluate.py <source.step|.stl> <rebuilt.step|.stl>
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

SAMPLES = 30000


def load_mesh(path):
    """Load a mesh from .stl directly, or via CadQuery tessellation for .step."""
    import trimesh

    p = Path(path)
    if p.suffix.lower() in (".step", ".stp"):
        import cadquery as cq

        wp = cq.importers.importStep(str(p))
        with tempfile.TemporaryDirectory() as td:
            stl = Path(td) / "m.stl"
            cq.exporters.export(wp, str(stl))
            return trimesh.load(str(stl), force="mesh")
    return trimesh.load(str(p), force="mesh")


def center(mesh):
    mesh.apply_translation(-(mesh.bounds[0] + mesh.bounds[1]) / 2.0)
    return mesh


def chamfer(a_pts, b_pts):
    from scipy.spatial import cKDTree

    d_ab = cKDTree(b_pts).query(a_pts)[0]
    d_ba = cKDTree(a_pts).query(b_pts)[0]
    return float((d_ab.mean() + d_ba.mean()) / 2.0)


def evaluate(source_mesh, rebuilt_mesh):
    import numpy as np

    ms, mr = center(source_mesh), center(rebuilt_mesh)
    es = ms.bounds[1] - ms.bounds[0]
    er = mr.bounds[1] - mr.bounds[0]
    bbox_err = np.abs(er - es) / np.where(es == 0, 1.0, es) * 100.0
    vol_err = abs(abs(mr.volume) - abs(ms.volume)) / max(abs(ms.volume), 1e-9) * 100.0

    src_pts = ms.sample(SAMPLES)
    best_d, best_flipped = None, False
    for flipped in (False, True):
        m = mr.copy()
        if flipped:
            import trimesh.transformations as tt

            m.apply_transform(tt.rotation_matrix(np.pi, [0, 0, 1]))
        d = chamfer(src_pts, m.sample(SAMPLES))
        if best_d is None or d < best_d:
            best_d, best_flipped = d, flipped

    diag = float(np.linalg.norm(es))
    cham_pct = best_d / diag * 100.0
    score = max(0.0, 100.0 - 4.0 * cham_pct - 0.5 * float(bbox_err.mean()) - 0.2 * vol_err)
    return {
        "bbox_err_pct": {"x": round(float(bbox_err[0]), 2),
                         "y": round(float(bbox_err[1]), 2),
                         "z": round(float(bbox_err[2]), 2)},
        "volume_err_pct": round(vol_err, 2),
        "chamfer_mm": round(best_d, 3),
        "chamfer_pct": round(cham_pct, 2),
        "flipped_180": best_flipped,
        "score": round(score, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", help="source .step/.stl (ground truth)")
    ap.add_argument("rebuilt", help="rebuilt .step/.stl to grade")
    args = ap.parse_args()
    result = evaluate(load_mesh(args.source), load_mesh(args.rebuilt))
    result["source"] = str(args.source)
    result["rebuilt"] = str(args.rebuilt)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
