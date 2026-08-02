"""The cross-camera association engine.

Two mechanisms, both validated offline before this module existed:

CONCURRENT -- same place + same time = same object. Every `bin_s` seconds,
track positions from different cameras are Hungarian-matched (class-group
compatible, distance-gated). A pair of per-camera tracks merges after
`min_votes` co-matched bins, or a single very tight match (<= tight_px):
short-lived tracks (a distant camera's median track can be a few frames)
never get a second bin to vote in.

TEMPORAL -- cameras mounted on one wall facing opposite directions share a
blind wedge; an object hands off with a gap of a second or two and is never
co-observed. When a young cluster is born moving, we look for a recently
ended cluster whose exit velocity predicts the birth position (and whose
heading agrees) and stitch them.

One implementation serves live fusion and offline replay: feed observations
with `observe()`, advance time with `step()`. Counting is world-space:
a cluster crossing a gate segment counts once per gate, after accumulating
`min_travel_px` of net path (the single-camera counter's anti-jitter rule,
promoted to the map)."""
from __future__ import annotations

import itertools
import math
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field

import numpy as np
import lap

CLS_NAME = {0: "person", 1: "bicycle", 2: "car", 3: "moto", 5: "bus", 7: "truck"}
GROUP = {0: "ped", 1: "ped", 2: "veh", 3: "veh", 5: "veh", 7: "veh"}


@dataclass
class Cluster:
    gid: int
    group: str
    born: float
    last_seen: float
    keys: set = field(default_factory=set)
    # (t, x, y) one smoothed point per step; bounded for trails/velocity
    path: deque = field(default_factory=lambda: deque(maxlen=400))
    cls_votes: Counter = field(default_factory=Counter)
    path_len: float = 0.0
    counted_gates: set = field(default_factory=set)

    def velocity(self, window: float = 1.0):
        pts = [p for p in self.path if p[0] >= self.path[-1][0] - window] \
            if self.path else []
        if len(pts) < 3 or pts[-1][0] - pts[0][0] < 0.3:
            return None
        dt = pts[-1][0] - pts[0][0]
        return np.array([(pts[-1][1] - pts[0][1]) / dt,
                         (pts[-1][2] - pts[0][2]) / dt])

    @property
    def cls(self):
        return self.cls_votes.most_common(1)[0][0] if self.cls_votes else -1


def _seg_cross(p, q, a, b):
    """Do segment p->q and gate a->b intersect? Returns 0 no, +1/-1 by side
    (direction of crossing relative to the gate's normal)."""
    def d(o, s, e):
        return (e[0] - s[0]) * (o[1] - s[1]) - (e[1] - s[1]) * (o[0] - s[0])
    d1, d2 = d(p, a, b), d(q, a, b)
    d3, d4 = d(a, p, q), d(b, p, q)
    if (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0):
        return 1 if d1 < d2 else -1
    return 0


class Engine:
    def __init__(self, cfg: dict, gates: list | None = None):
        a = cfg
        self.bin_s = a.get("bin_s", 0.25)
        self.gate = a.get("gate_px", 35.0)
        self.tight = a.get("tight_px", 12.0)
        self.min_votes = a.get("min_votes", 2)
        self.stitch_dt = a.get("stitch_dt_s", 4.0)
        self.stitch_gate = a.get("stitch_gate_px", 55.0)
        self.min_speed = a.get("min_speed_px_s", 8.0)
        self.min_travel = a.get("min_travel_px", 60.0)
        self.gates = gates or []          # [{id, a:[x,y], b:[x,y]}]
        self.counts = defaultdict(lambda: defaultdict(int))  # gate -> dir -> n

        self._next_gid = itertools.count(1)
        self._obs = defaultdict(list)     # (cam,tid) -> [(t,x,y,cls)] this bin
        self._cluster_of: dict = {}       # (cam,tid) -> Cluster
        self.clusters: dict = {}          # gid -> Cluster (live + recent dead)
        self._votes = defaultdict(list)   # (keyA,keyB) -> [(t, dist)]
        self._last_step = None
        self.events = []                  # counted crossings (dicts)

    # ---- input ---------------------------------------------------------
    def observe(self, cam, tid, cls, wx, wy, t):
        self._obs[(cam, tid)].append((t, wx, wy, cls))

    # ---- merge machinery ----------------------------------------------
    def _merge(self, ca: Cluster, cb: Cluster):
        if ca.gid == cb.gid:
            return ca
        keep, drop = (ca, cb) if ca.born <= cb.born else (cb, ca)
        keep.keys |= drop.keys
        keep.cls_votes += drop.cls_votes
        keep.counted_gates |= drop.counted_gates
        merged = sorted(list(keep.path) + list(drop.path))
        keep.path = deque(merged, maxlen=keep.path.maxlen)
        keep.path_len += drop.path_len
        keep.last_seen = max(keep.last_seen, drop.last_seen)
        keep.born = min(keep.born, drop.born)
        for k in drop.keys:
            self._cluster_of[k] = keep
        self.clusters.pop(drop.gid, None)
        return keep

    # ---- the periodic step --------------------------------------------
    def step(self, now: float):
        """Advance to `now`: fold this bin's observations into clusters,
        vote/merge concurrent matches, stitch handoffs, update counts."""
        if self._last_step is not None and now - self._last_step < self.bin_s:
            return
        self._last_step = now
        binned = {}
        for key, obs in self._obs.items():
            xs = [o[1] for o in obs]
            ys = [o[2] for o in obs]
            cls = obs[-1][3]
            binned[key] = (float(np.mean(xs)), float(np.mean(ys)), cls)
        self._obs.clear()

        # fold into clusters (create for new keys)
        for key, (x, y, cls) in binned.items():
            cl = self._cluster_of.get(key)
            if cl is None:
                cl = Cluster(gid=next(self._next_gid),
                             group=GROUP.get(cls, "veh"),
                             born=now, last_seen=now)
                cl.keys.add(key)
                self._cluster_of[key] = cl
                self.clusters[cl.gid] = cl
            if cl.path:
                prev = cl.path[-1]
                cl.path_len += math.hypot(x - prev[1], y - prev[2])
            cl.path.append((now, x, y))
            cl.cls_votes[cls] += 1
            cl.last_seen = now

        # concurrent voting between camera pairs
        by_cam = defaultdict(list)
        for key, (x, y, cls) in binned.items():
            by_cam[key[0]].append((key, np.array([x, y])))
        cams = sorted(by_cam)
        for i in range(len(cams)):
            for j in range(i + 1, len(cams)):
                a, b = by_cam[cams[i]], by_cam[cams[j]]
                cost = np.full((len(a), len(b)), 1e6)
                for ai, (ka, pa) in enumerate(a):
                    for bi, (kb, pb) in enumerate(b):
                        if self._cluster_of[ka].group != self._cluster_of[kb].group:
                            continue
                        d = np.linalg.norm(pa - pb)
                        if d < self.gate:
                            cost[ai, bi] = d
                if not len(a) or not len(b):
                    continue
                _, xs, _ = lap.lapjv(cost, extend_cost=True)
                for ai, bi in enumerate(xs):
                    if bi < 0 or cost[ai, bi] >= self.gate:
                        continue
                    ka, kb = a[ai][0], b[bi][0]
                    pair = (ka, kb) if ka < kb else (kb, ka)
                    self._votes[pair].append((now, cost[ai, bi]))
                    ds = [d for _, d in self._votes[pair]]
                    if len(ds) >= self.min_votes or min(ds) <= self.tight:
                        self._merge(self._cluster_of[ka], self._cluster_of[kb])

        # temporal stitching: young moving clusters vs recently dead ones
        young, dead = [], []
        for cl in self.clusters.values():
            if cl.last_seen >= now - self.bin_s and now - cl.born <= 1.5:
                young.append(cl)
            elif cl.last_seen < now - 2 * self.bin_s \
                    and now - cl.last_seen <= self.stitch_dt:
                dead.append(cl)
        for yc in young:
            vy = yc.velocity()
            if vy is None or np.linalg.norm(vy) < self.min_speed:
                continue
            best = None
            for dc in dead:
                if dc.group != yc.group or dc.gid == yc.gid:
                    continue
                vd = dc.velocity()
                if vd is None or np.linalg.norm(vd) < self.min_speed:
                    continue
                dt = yc.path[0][0] - dc.last_seen
                if not (0 < dt <= self.stitch_dt):
                    continue
                pred = np.array(dc.path[-1][1:]) + vd * dt
                d = np.linalg.norm(pred - np.array(yc.path[0][1:]))
                cos = float(np.dot(vd, vy)
                            / (np.linalg.norm(vd) * np.linalg.norm(vy)))
                if d <= self.stitch_gate and cos > 0.7:
                    if best is None or d < best[0]:
                        best = (d, dc)
            if best:
                self._merge(best[1], yc)

        # counting: last two smoothed points vs each gate
        for cl in self.clusters.values():
            if len(cl.path) < 2 or cl.path[-1][0] != now:
                continue
            p, q = cl.path[-2][1:], cl.path[-1][1:]
            for g in self.gates:
                if g["id"] in cl.counted_gates:
                    continue
                side = _seg_cross(p, q, g["a"], g["b"])
                if side and cl.path_len >= self.min_travel:
                    cl.counted_gates.add(g["id"])
                    d = "a_to_b" if side > 0 else "b_to_a"
                    self.counts[g["id"]][d] += 1
                    self.events.append({
                        "t": now, "gate": g["id"], "dir": d, "gid": cl.gid,
                        "cls": CLS_NAME.get(cl.cls, str(cl.cls)),
                        "cams": sorted({k[0] for k in cl.keys})})

        # prune: stale votes, long-dead clusters
        for pair in [p for p, v in self._votes.items()
                     if v[-1][0] < now - 10]:
            del self._votes[pair]
        for gid in [g for g, c in self.clusters.items()
                    if c.last_seen < now - self.stitch_dt - 4]:
            cl = self.clusters.pop(gid)
            for k in list(cl.keys):
                if self._cluster_of.get(k) is cl:
                    del self._cluster_of[k]

    # ---- output --------------------------------------------------------
    def snapshot(self, now: float):
        """Live clusters for the UI: position, velocity, trail, cameras."""
        out = []
        for cl in self.clusters.values():
            if cl.last_seen < now - 1.0 or not cl.path:
                continue
            v = cl.velocity()
            out.append({
                "gid": cl.gid,
                "cls": CLS_NAME.get(cl.cls, str(cl.cls)),
                "x": round(cl.path[-1][1], 1),
                "y": round(cl.path[-1][2], 1),
                "speed": round(float(np.linalg.norm(v)), 1) if v is not None else 0,
                "cams": sorted({k[0] for k in cl.keys}),
                "trail": [[round(x, 1), round(y, 1)]
                          for _, x, y in list(cl.path)[-60:]],
            })
        return out
