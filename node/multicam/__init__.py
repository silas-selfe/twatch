"""Cross-camera tracking: overlapping cameras -> one shared ground plane ->
global object identities -> world-space counting.

The pipeline (validated offline before going live):

  calibrate.py (repo root of node/)   one-time click calibration per camera
  geometry.py                         calibrations, image->world projection
  associate.py                        the association engine: concurrent
                                      matching + temporal handoff stitching.
                                      ONE implementation serves both live
                                      fusion and offline replay, so what you
                                      validate offline is what runs live.
  record.py                           synchronized multi-camera clips
  offline.py                          replay clips through the engine
  fusion.py                           live capture + tracking + fusion
  server.py                           local map UI (FastAPI)

Entry point: node/watch_multi.py
"""
