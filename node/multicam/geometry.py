"""Calibrations and image->world projection.

World coordinates are REFERENCE-IMAGE PIXELS (the satellite screenshot used
in calibrate.py). If site.yaml/multicam.yaml provides meters_per_ref_px,
speeds become m/s; until then they are ref-px/s. A track's world position is
its bbox bottom-center (ground contact) pushed through the camera's
homography."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import yaml

NODE = Path(__file__).resolve().parent.parent


class Calibration:
    def __init__(self, cam_id: str, data: dict):
        self.cam_id = cam_id
        self.H = np.float64(data["H"])
        self.points_ref = data["points_ref"]
        self.ref_path = data["ref"]
        self.err_mean = data.get("reproj_err_px", {}).get("mean")

    def project(self, x: float, y: float) -> tuple[float, float]:
        """Image pixel -> reference pixel."""
        p = cv2.perspectiveTransform(np.float64([[[x, y]]]), self.H)
        return float(p[0, 0, 0]), float(p[0, 0, 1])

    def footprint(self, pad: float = 1.25) -> np.ndarray:
        """Validated region on the reference: padded hull of the clicked
        points (same rule as calibrate.py's previews)."""
        hull = cv2.convexHull(np.float32(self.points_ref).reshape(-1, 1, 2))
        c = hull.reshape(-1, 2).mean(axis=0)
        return (c + (hull.reshape(-1, 2) - c) * pad).astype(int)


def load_calibrations(cam_ids) -> dict[str, Calibration]:
    out = {}
    for cid in cam_ids:
        p = NODE / "cameras" / f"cam{cid}" / "calibration.yaml"
        if not p.exists():
            raise FileNotFoundError(
                f"{p} missing -- run calibrate.py for cam{cid} first")
        out[cid] = Calibration(cid, yaml.safe_load(p.read_text()))
    return out


def load_multicam_config() -> dict:
    cfg = yaml.safe_load((NODE / "multicam.yaml").read_text())
    return cfg


def resolve_device(dev):
    """'auto' -> mps (Apple) / cuda / cpu, same policy as watch.py."""
    if dev != "auto":
        return dev
    import torch
    if torch.backends.mps.is_available():
        return "mps"
    return 0 if torch.cuda.is_available() else "cpu"
