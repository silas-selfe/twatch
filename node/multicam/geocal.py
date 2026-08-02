"""Browser-based geographic calibration: click a ground point on the camera
still, then the same physical point on real satellite map tiles. Points are
stored as lat/lon, the homography maps image pixels to site-local METERS --
so a calibration works identically on any machine (nothing to distribute)
and every speed downstream is true m/s.

  python watch_multi.py calibrate [--port 8798]

The first camera saved fixes the site anchor (its first clicked point);
later cameras reuse it, keeping all cameras in one local frame."""
from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np
import yaml
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from .geometry import NODE, GeoFrame

HERE = Path(__file__).resolve().parent


def _existing_anchor():
    for p in sorted((NODE / "cameras").glob("*/calibration.yaml")):
        d = yaml.safe_load(p.read_text())
        if d.get("mode") == "geo" and d.get("anchor"):
            return d["anchor"]
    return None


def _still(cam: str) -> bytes | None:
    """A frame to calibrate against: today's discovery still if present,
    else one live frame from the stream."""
    p = NODE / "discovered" / f"ch{cam}.jpg"
    if p.exists():
        return p.read_bytes()
    try:
        import sys
        sys.path.insert(0, str(NODE))
        from watch import sibling_source, PyAVCapture
        from .record import primary_source
        cap = PyAVCapture(sibling_source(primary_source(), cam))
        t_end = time.time() + 10
        while time.time() < t_end:
            ok, frame = cap.read()
            if ok:
                cap.release()
                ok2, buf = cv2.imencode(".jpg", frame,
                                        [cv2.IMWRITE_JPEG_QUALITY, 90])
                return buf.tobytes() if ok2 else None
        cap.release()
    except Exception:
        pass
    return None


class SavePayload(BaseModel):
    cam: str
    points_image: list[list[float]]   # [[x_px, y_px], ...]
    points_latlon: list[list[float]]  # [[lat, lon], ...]


class GeorefPayload(BaseModel):
    points_image: list[list[float]]   # screenshot pixels
    points_latlon: list[list[float]]  # matching map points


GEOREF = NODE / "cameras" / "georef.yaml"
REF_IMG = NODE / "discovered" / "ref.PNG"


def _fit_affine(px, world):
    """Least-squares affine screenshot-px -> local meters. Affine (not a
    similarity) because pixel y points down while north points up -- the
    mapping includes a reflection -- and a slightly oblique screenshot adds
    shear a similarity cannot express. 3 pairs = exact, 4+ = error signal."""
    rows, rhs = [], []
    for (x, y), (X, Y) in zip(px, world):
        rows += [[x, y, 1, 0, 0, 0], [0, 0, 0, x, y, 1]]
        rhs += [X, Y]
    (p, q, tx, r, s, ty), *_ = np.linalg.lstsq(
        np.float64(rows), np.float64(rhs), rcond=None)
    def apply(x, y):
        # plain floats: these flow into yaml.safe_dump, which refuses numpy
        return float(p * x + q * y + tx), float(r * x + s * y + ty)
    return apply


def create_app(cameras: list[str]) -> FastAPI:
    app = FastAPI(title="twatch geo calibration")
    ui = (HERE / "geocal.html").read_text(encoding="utf-8")

    @app.get("/")
    def index():
        return HTMLResponse(ui)

    @app.get("/config")
    def config():
        state = {}
        for c in cameras:
            p = NODE / "cameras" / f"cam{c}" / "calibration.yaml"
            if p.exists():
                d = yaml.safe_load(p.read_text())
                state[c] = {"mode": d.get("mode", "ref-px"),
                            "n_points": len(d.get("points_image", [])),
                            "err": d.get("reproj_err",
                                         d.get("reproj_err_px"))}
        return {"cameras": cameras, "anchor": _existing_anchor(),
                "calibrated": state}

    @app.get("/still/{cam}")
    def still(cam: str):
        data = _still(cam)
        if data is None:
            return Response(status_code=404)
        return Response(data, media_type="image/jpeg")

    @app.get("/refimg")
    def refimg():
        if not REF_IMG.exists():
            return Response(status_code=404)
        return Response(REF_IMG.read_bytes(), media_type="image/png")

    @app.get("/georef")
    def georef_get():
        if not GEOREF.exists():
            return {"exists": False}
        return {"exists": True, **yaml.safe_load(GEOREF.read_text())}

    @app.post("/georef")
    def georef_save(p: GeorefPayload):
        """Drape the trusted screenshot at its true position: fit a
        similarity from screenshot px to local meters, emit the three
        corner lat/lons the rotated image overlay needs."""
        n = min(len(p.points_image), len(p.points_latlon))
        if n < 3:
            return {"ok": False, "error": f"need 3+ pairs (4 recommended), have {n}"}
        img = cv2.imread(str(REF_IMG))
        if img is None:
            return {"ok": False, "error": "discovered/ref.PNG not found"}
        h, w = img.shape[:2]
        anchor = _existing_anchor() or list(p.points_latlon[0])
        frame = GeoFrame(*anchor)
        world = [frame.to_local(lat, lon) for lat, lon in p.points_latlon[:n]]
        apply = _fit_affine(p.points_image[:n], world)
        err = [float(np.hypot(*(np.subtract(apply(x, y), wpt))))
               for (x, y), wpt in zip(p.points_image[:n], world)]
        corners = {k: list(frame.to_latlon(*apply(x, y)))
                   for k, (x, y) in (("tl", (0, 0)), ("tr", (w, 0)),
                                     ("bl", (0, h)))}
        data = {"image": "/refimg", "anchor": anchor, "corners": corners,
                "fit_err_m": [round(e, 2) for e in err]}
        GEOREF.parent.mkdir(parents=True, exist_ok=True)
        GEOREF.write_text(yaml.safe_dump(data, sort_keys=False))
        return {"ok": True, **data}

    @app.post("/save")
    def save(p: SavePayload):
        n = min(len(p.points_image), len(p.points_latlon))
        if n < 4:
            return {"ok": False, "error": f"need 4+ pairs, have {n}"}
        anchor = _existing_anchor() or list(p.points_latlon[0])
        frame = GeoFrame(*anchor)
        world = [list(frame.to_local(lat, lon))
                 for lat, lon in p.points_latlon[:n]]
        a = np.float64(p.points_image[:n]).reshape(-1, 1, 2)
        b = np.float64(world).reshape(-1, 1, 2)
        method = cv2.RANSAC if n > 5 else 0
        H, _ = cv2.findHomography(a, b, method, 1.0)  # 1 m RANSAC threshold
        if H is None:
            return {"ok": False,
                    "error": "homography failed -- spread the points out"}
        proj = cv2.perspectiveTransform(a, H)
        err = np.linalg.norm(proj - b, axis=2).ravel()
        d = NODE / "cameras" / f"cam{p.cam}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "calibration.yaml").write_text(yaml.safe_dump({
            "id": f"cam{p.cam}",
            "mode": "geo",
            "anchor": anchor,                       # [lat0, lon0]
            "points_image": [list(q) for q in p.points_image[:n]],
            "points_latlon": [list(q) for q in p.points_latlon[:n]],
            "points_world": world,                  # local meters
            "H": H.tolist(),                        # image px -> local meters
            "reproj_err": {"mean": float(err.mean()),
                           "max": float(err.max()), "unit": "m"},
        }, sort_keys=False))
        # projected image points back to lat/lon so the UI can show the fit
        proj_ll = [list(frame.to_latlon(x, y)) for x, y in
                   proj.reshape(-1, 2)]
        return {"ok": True, "anchor": anchor,
                "err_mean_m": round(float(err.mean()), 2),
                "err_max_m": round(float(err.max()), 2),
                "per_point_m": [round(float(e), 2) for e in err],
                "projected_latlon": proj_ll}

    return app


def serve(cameras: list[str], port: int = 8798):
    import uvicorn
    print(f"\ngeo calibration: http://127.0.0.1:{port}\n")
    uvicorn.run(create_app(cameras), host="127.0.0.1", port=port,
                log_level="warning")
