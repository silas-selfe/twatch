"""trafficwatch multicam -- cross-camera tracking over one shared ground
plane. Companion to watch.py (single-camera counting): overlapping cameras
are fused into global object identities, counting happens in world space,
and a local map UI shows the physical space live.

  python watch_multi.py record [--seconds 150]   # synchronized clips
  python watch_multi.py replay [--retrack]       # validate assoc. offline
  python watch_multi.py live [--port 8799] [--no-web]

Prerequisites: calibrate.py run per camera (cameras/<id>/calibration.yaml),
multicam.yaml for cameras/model/association/gates, TW_CAMERA_SOURCE in
node/.env (channel siblings are derived the same way --also-show does)."""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from multicam.geometry import NODE, load_multicam_config


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_rec = sub.add_parser("record", help="synchronized clips from all cameras")
    p_rec.add_argument("--seconds", type=float, default=150)
    p_rep = sub.add_parser("replay", help="run the engine over recorded clips")
    p_rep.add_argument("--retrack", action="store_true",
                       help="redo the YOLO pass (ignore cached world_tracks)")
    p_live = sub.add_parser("live", help="live fusion + map UI")
    p_live.add_argument("--port", type=int, default=8799)
    p_live.add_argument("--no-web", action="store_true",
                        help="fusion without the UI server")
    args = ap.parse_args()

    cfg = load_multicam_config()
    cameras = [str(c) for c in cfg["cameras"]]
    ref_path = NODE / cfg["ref"]

    if args.cmd == "record":
        from multicam.record import record
        record(cameras, args.seconds)
    elif args.cmd == "replay":
        from multicam.offline import track_clips, replay
        track_clips(cameras, cfg.get("model_offline", cfg.get("model", {})),
                    force=args.retrack)
        replay(cameras, cfg, ref_path)
    elif args.cmd == "live":
        from multicam.fusion import Fusion
        fusion = Fusion(cfg)
        fusion.start()
        if args.no_web:
            print("fusion running headless; ctrl-c to stop")
            try:
                while True:
                    time.sleep(10)
                    s = fusion.state()
                    print(f"{len(s['objects'])} live objects, "
                          f"counts {dict(s['counts'])}, cams "
                          + ", ".join(f"{c}:{v['fps']}fps"
                                      for c, v in s["cams"].items()))
            except KeyboardInterrupt:
                pass
        else:
            from multicam.server import serve
            serve(fusion, ref_path, args.port)


if __name__ == "__main__":
    main()
