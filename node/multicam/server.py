"""Local map UI for live fusion: the satellite reference with camera
footprints, live global tracks, gate counters, and per-camera thumbnails.
Serves on localhost only -- this is an operator console, not a public app."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, Response

HERE = Path(__file__).resolve().parent


def create_app(fusion, ref_path: Path) -> FastAPI:
    app = FastAPI(title="twatch multicam")
    ui = (HERE / "ui.html").read_text(encoding="utf-8")

    @app.get("/")
    def index():
        return HTMLResponse(ui)

    @app.get("/ref.jpg")
    def ref():
        return FileResponse(ref_path)

    @app.get("/meta")
    def meta():
        out = {
            "mode": "geo" if fusion.frame else "ref-px",
            "cameras": fusion.cameras,
            "footprints": {c: fusion.cals[c].footprint()
                           .astype(float).round(2).tolist()
                           for c in fusion.cameras},
            "gates": fusion.engine.gates,
            "site": fusion.cfg.get("label", "twatch multicam"),
        }
        if fusion.frame:
            out["anchor"] = [fusion.frame.lat0, fusion.frame.lon0]
        else:
            import cv2
            img = cv2.imread(str(ref_path))
            out["ref"] = {"w": img.shape[1], "h": img.shape[0]}
        return out

    @app.get("/state")
    def state():
        return fusion.state()

    @app.get("/thumb/{cam}")
    def thumb(cam: str):
        import time
        for w in fusion.workers:
            if w.cam_id == cam:
                # demand signal: workers render annotated frames only while
                # a viewer keeps requesting them
                w.thumb_until = time.time() + 10
                if w.annotated:
                    return Response(w.annotated, media_type="image/jpeg")
        return Response(status_code=404)

    return app


def serve(fusion, ref_path: Path, port: int = 8799):
    import uvicorn
    app = create_app(fusion, ref_path)
    print(f"\nmap UI: http://127.0.0.1:{port}\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
