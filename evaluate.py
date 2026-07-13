#!/usr/bin/env python3
"""Fidelity check: how close is a rebuilt model to the source STEP?

Aligns both meshes on their bbox centers (also trying a 180° flip about z, in
case the rebuild faced the other way), then removes any small residual pose
error (a few degrees/mm of build inaccuracy, not a real shape difference)
with a capped ICP refinement, and reports:

    bbox_err_pct   per-axis bounding-box error (%)
    volume_err_pct volume error (%)
    chamfer_mm     bidirectional nearest-neighbor surface distance (mean, mm)
    chamfer_pct    chamfer as % of the source bbox diagonal
    icp_rotation_deg  rotation ICP wanted to apply on top of the flip search
    icp_applied    whether that correction was small enough to trust (see
                   ICP_MAX_ROTATION_DEG) and actually used for chamfer/score
    score          0-100 fidelity score (100 = identical)

Usage:
    uv run --python 3.12 --with cadquery --with trimesh --with scipy \
        python3 evaluate.py <source.step|.stl> <rebuilt.step|.stl>
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

SAMPLES = 100000
# Tight STL tessellation tolerance for STEP ground truth — cadquery's export
# default (0.1mm / 0.1rad ~= 5.7deg) is coarser than the rebuild's own STL
# export and adds tessellation noise to the chamfer distance that has
# nothing to do with actual shape fidelity.
STEP_TOLERANCE = 0.02
STEP_ANGULAR_TOLERANCE = 1.0
# ICP correction above this angle is treated as a real shape/orientation
# difference, not build noise, and is left unapplied (score stays honest
# about it) rather than let alignment quietly absorb a genuine mismatch.
ICP_MAX_ROTATION_DEG = 10.0


def load_mesh(path):
    """Load a mesh from .stl directly, or via CadQuery tessellation for .step."""
    import trimesh

    p = Path(path)
    if p.suffix.lower() in (".step", ".stp"):
        import cadquery as cq

        wp = cq.importers.importStep(str(p))
        with tempfile.TemporaryDirectory() as td:
            stl = Path(td) / "m.stl"
            cq.exporters.export(wp, str(stl), tolerance=STEP_TOLERANCE,
                                angularTolerance=STEP_ANGULAR_TOLERANCE)
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


def icp_align(src_pts, pts, max_iterations=25, tol=1e-5):
    """Point-to-point ICP (Kabsch-per-iteration rigid fit, no scaling):
    nudges `pts` onto `src_pts` starting from identity — the meshes are
    already bbox-centered, so this only needs to correct a small residual
    pose error, not find a gross alignment. Returns (aligned_pts,
    total_rotation_deg) so the caller can decide whether to trust it."""
    from scipy.spatial import cKDTree
    import numpy as np

    tree = cKDTree(src_pts)
    cur = pts
    r_total = np.eye(3)
    prev_err = None
    for _ in range(max_iterations):
        dist, idx = tree.query(cur)
        matched = src_pts[idx]
        cur_c, matched_c = cur.mean(axis=0), matched.mean(axis=0)
        h = (cur - cur_c).T @ (matched - matched_c)
        u, _, vt = np.linalg.svd(h)
        d = np.sign(np.linalg.det(vt.T @ u.T))
        r = vt.T @ np.diag([1.0, 1.0, d]) @ u.T
        t = matched_c - r @ cur_c
        cur = (r @ cur.T).T + t
        r_total = r @ r_total
        err = float(dist.mean())
        if prev_err is not None and abs(prev_err - err) < tol:
            break
        prev_err = err
    angle_deg = float(np.degrees(np.arccos(np.clip((np.trace(r_total) - 1.0) / 2.0, -1.0, 1.0))))
    return cur, angle_deg


def evaluate(source_mesh, rebuilt_mesh):
    import numpy as np

    ms, mr = center(source_mesh), center(rebuilt_mesh)
    es = ms.bounds[1] - ms.bounds[0]
    er = mr.bounds[1] - mr.bounds[0]
    bbox_err = np.abs(er - es) / np.where(es == 0, 1.0, es) * 100.0
    vol_err = abs(abs(mr.volume) - abs(ms.volume)) / max(abs(ms.volume), 1e-9) * 100.0

    src_pts = ms.sample(SAMPLES)
    best_d, best_flipped, best_pts = None, False, None
    for flipped in (False, True):
        m = mr.copy()
        if flipped:
            import trimesh.transformations as tt

            m.apply_transform(tt.rotation_matrix(np.pi, [0, 0, 1]))
        pts = m.sample(SAMPLES)
        d = chamfer(src_pts, pts)
        if best_d is None or d < best_d:
            best_d, best_flipped, best_pts = d, flipped, pts

    icp_pts, icp_angle = icp_align(src_pts, best_pts)
    icp_d = chamfer(src_pts, icp_pts)
    icp_applied = icp_angle <= ICP_MAX_ROTATION_DEG and icp_d < best_d
    if icp_applied:
        best_d = icp_d

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
        "icp_rotation_deg": round(icp_angle, 2),
        "icp_applied": icp_applied,
        "score": round(score, 1),
    }


def stop_verdict(score, target, prev_score, min_delta):
    """Turn a raw score into an explicit loop directive the builder must obey.
    Returns (stop: bool, verdict: str). The whole point is a HARD stop the
    moment the target is met — a passing score and a higher one are the same
    `done`, and free-running past it only burns cycles + context."""
    if score >= target:
        return True, (f"STOP — target met (score {score} >= target {target}). Do NOT "
                      "refine further: a passing score and a higher score are the SAME "
                      "'done'. Hand off the model now.")
    if prev_score is not None and (score - prev_score) < min_delta:
        return True, (f"STOP — diminishing returns (gained {round(score - prev_score, 1)} "
                      f"pts < min_delta {min_delta} vs previous {prev_score}). Another "
                      "cycle costs context for no real gain. Hand off now.")
    return False, (f"CONTINUE — score {score} is below target {target}. Fix ONLY the single "
                   f"worst metric with the smallest responsible param change, re-cad, then "
                   f"re-score passing `--prev-score {score}`.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", help="source .step/.stl (ground truth)")
    ap.add_argument("rebuilt", help="rebuilt .step/.stl to grade")
    ap.add_argument("--target", type=float,
                    default=float(os.environ.get("FIDELITY_TARGET", "95")),
                    help="score at or above which the loop must STOP (default 95)")
    ap.add_argument("--prev-score", type=float, default=None,
                    help="last cycle's score — lets the scorer flag diminishing returns")
    ap.add_argument("--min-delta", type=float,
                    default=float(os.environ.get("FIDELITY_MIN_DELTA", "1.0")),
                    help="minimum score gain that justifies another cycle (default 1.0)")
    args = ap.parse_args()
    result = evaluate(load_mesh(args.source), load_mesh(args.rebuilt))
    stop, verdict = stop_verdict(result["score"], args.target, args.prev_score, args.min_delta)
    result["target"] = args.target
    result["stop"] = stop
    result["verdict"] = verdict
    result["source"] = str(args.source)
    result["rebuilt"] = str(args.rebuilt)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
