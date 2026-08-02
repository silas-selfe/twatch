"""Calibrations and image->world projection.

Two world modes:

GEO (preferred) -- world coordinates are SITE-LOCAL METERS east/north of an
anchor lat/lon. Calibration points are clicked against real satellite map
tiles in the browser (watch_multi.py calibrate), stored as lat/lon, so a
site's calibration is a handful of numbers that work identically on any
machine -- nothing to distribute, and speeds are true m/s. Equirectangular
around the anchor is exact to well under a centimeter at block scale.

REF-PX (legacy/offline) -- world coordinates are pixels on a local
reference screenshot (calibrate.py cv2 clicking). Still supported: it
needs no internet and no known location.

Either way, a track's world position is its bbox bottom-center (ground
contact) pushed through the camera's homography."""
from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np
import yaml

NODE = Path(__file__).resolve().parent.parent

M_PER_DEG_LAT = 110574.0


class GeoFrame:
    """Site-local meters: x east, y north of the anchor."""

    def __init__(self, lat0: float, lon0: float):
        self.lat0, self.lon0 = float(lat0), float(lon0)
        self.m_per_deg_lon = 111320.0 * math.cos(math.radians(self.lat0))

    def to_local(self, lat: float, lon: float) -> tuple[float, float]:
        return ((lon - self.lon0) * self.m_per_deg_lon,
                (lat - self.lat0) * M_PER_DEG_LAT)

    def to_latlon(self, x: float, y: float) -> tuple[float, float]:
        return (self.lat0 + y / M_PER_DEG_LAT,
                self.lon0 + x / self.m_per_deg_lon)


class Calibration:
    def __init__(self, cam_id: str, data: dict):
        self.cam_id = cam_id
        self.mode = data.get("mode", "ref-px")
        self.H = np.float64(data["H"])   # image px -> world (ref px | meters)
        self.anchor = data.get("anchor")  # [lat0, lon0] in geo mode
        self.points_world = data.get("points_world") or data.get("points_ref")
        self.err_mean = (data.get("reproj_err", data.get("reproj_err_px", {}))
                         .get("mean"))

    def project(self, x: float, y: float) -> tuple[float, float]:
        """Image pixel -> world coordinates."""
        p = cv2.perspectiveTransform(np.float64([[[x, y]]]), self.H)
        return float(p[0, 0, 0]), float(p[0, 0, 1])

    def footprint(self, pad: float = 1.25) -> np.ndarray:
        """Validated world region: padded hull of the calibration points."""
        hull = cv2.convexHull(np.float32(self.points_world).reshape(-1, 1, 2))
        c = hull.reshape(-1, 2).mean(axis=0)
        return c + (hull.reshape(-1, 2) - c) * pad


def load_calibrations(cam_ids) -> dict[str, Calibration]:
    out, modes, anchors = {}, set(), set()
    for cid in cam_ids:
        p = NODE / "cameras" / f"cam{cid}" / "calibration.yaml"
        if not p.exists():
            raise FileNotFoundError(
                f"{p} missing -- calibrate cam{cid} first "
                "(watch_multi.py calibrate, or legacy calibrate.py)")
        cal = Calibration(cid, yaml.safe_load(p.read_text()))
        out[cid] = cal
        modes.add(cal.mode)
        if cal.anchor:
            anchors.add(tuple(cal.anchor))
    if len(modes) > 1:
        raise ValueError(f"mixed calibration modes {modes} -- recalibrate so "
                         "all cameras share one world")
    if len(anchors) > 1:
        raise ValueError(f"multiple site anchors {anchors} -- all cameras "
                         "must share the first camera's anchor")
    return out


def site_frame(cals: dict[str, Calibration]) -> GeoFrame | None:
    """The site's geo frame, if calibrations are geographic."""
    for c in cals.values():
        if c.mode == "geo" and c.anchor:
            return GeoFrame(*c.anchor)
    return None


def load_multicam_config() -> dict:
    # utf-8-sig: Windows editors/tools like to prepend a BOM
    return yaml.safe_load(
        (NODE / "multicam.yaml").read_text(encoding="utf-8-sig"))


def resolve_device(dev):
    """'auto' -> mps (Apple) / cuda / cpu, same policy as watch.py."""
    if dev != "auto":
        return dev
    import torch
    if torch.backends.mps.is_available():
        return "mps"
    return 0 if torch.cuda.is_available() else "cpu"
