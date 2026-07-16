"""twatch web UI -- authenticated analytics over the central store.

Reads monitor_traffic in the twatch database with a read-only role.
Session-cookie auth against monitor_traffic.users (argon2id hashes).

Env:
  TW_CENTRAL_DSN   postgres DSN for the tw_web role   (required)
  TW_SECRET_KEY    session-cookie signing secret       (required)

Run:  uvicorn webapp.app:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg_pool import ConnectionPool
from starlette.middleware.sessions import SessionMiddleware

HERE = Path(__file__).resolve().parent
SCHEMA = "monitor_traffic"

app = FastAPI(title="twatch")
app.add_middleware(SessionMiddleware,
                   secret_key=os.environ.get("TW_SECRET_KEY", ""),
                   https_only=os.environ.get("TW_INSECURE_COOKIES") != "1",
                   max_age=14 * 86400)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))
hasher = PasswordHasher()

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(os.environ["TW_CENTRAL_DSN"],
                               min_size=1, max_size=5, open=True)
    return _pool


def user_of(request: Request) -> str | None:
    return request.session.get("user")


def guard(request: Request):
    """Page guard: redirect anonymous visitors to /login."""
    if not user_of(request):
        return RedirectResponse("/login", status_code=303)
    return None


# ---------------------------------------------------------------- auth ----
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    with pool().connection() as con:
        row = con.execute(
            f"SELECT pw_hash FROM {SCHEMA}.users WHERE username = %s",
            (username.strip(),)).fetchone()
    try:
        if row is None:
            raise VerifyMismatchError
        hasher.verify(row[0], password)
    except VerifyMismatchError:
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Wrong username or password."}, status_code=401)
    request.session["user"] = username.strip()
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --------------------------------------------------------------- pages ----
@app.get("/", response_class=HTMLResponse)
def fleet_page(request: Request):
    if (r := guard(request)):
        return r
    return templates.TemplateResponse(request, "fleet.html",
                                      {"user": user_of(request)})


@app.get("/site/{site_id}", response_class=HTMLResponse)
def site_page(request: Request, site_id: str):
    if (r := guard(request)):
        return r
    with pool().connection() as con:
        row = con.execute(
            f"SELECT site_id, label, timezone, dir_ltr_label, dir_rtl_label"
            f" FROM {SCHEMA}.sites WHERE site_id = %s", (site_id,)).fetchone()
    if row is None:
        return HTMLResponse("unknown site", status_code=404)
    site = dict(zip(("site_id", "label", "timezone", "ltr", "rtl"), row))
    return templates.TemplateResponse(request, "site.html",
                                      {"user": user_of(request), "site": site})


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ----------------------------------------------------------------- api ----
def api_guard(request: Request):
    if not user_of(request):
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    return None


@app.get("/api/fleet")
def api_fleet(request: Request):
    if (r := api_guard(request)):
        return r
    with pool().connection() as con:
        sites = con.execute(f"""
            SELECT s.site_id, s.label, s.timezone,
                   hb.reported_at, hb.node_version,
                   today.n_today, today.uptime_today
            FROM {SCHEMA}.sites s
            LEFT JOIN LATERAL (
                SELECT reported_at, node_version FROM {SCHEMA}.node_heartbeat
                WHERE site_id = s.site_id ORDER BY reported_at DESC LIMIT 1
            ) hb ON true
            LEFT JOIN LATERAL (
                SELECT COALESCE(sum(n), 0) AS n_today,
                       round(avg(uptime_pct)::numeric, 1) AS uptime_today
                FROM {SCHEMA}.hourly_summary h
                WHERE h.site_id = s.site_id
                  AND h.class <> '__coverage__'
                  AND (h.hour_start AT TIME ZONE s.timezone)::date
                      = (now() AT TIME ZONE s.timezone)::date
            ) today ON true
            ORDER BY s.site_id""").fetchall()
        daily = con.execute(f"""
            SELECT site_id, local_date::text, sum(n)
            FROM {SCHEMA}.daily_by_site
            WHERE class <> '__coverage__'
              AND local_date > current_date - 15
            GROUP BY site_id, local_date ORDER BY local_date""").fetchall()
    return {
        "sites": [{"site_id": s[0], "label": s[1], "tz": s[2],
                   "last_seen": s[3].isoformat() if s[3] else None,
                   "version": (s[4] or "")[:7],
                   "today": int(s[5] or 0),
                   "uptime_today": float(s[6]) if s[6] is not None else None}
                  for s in sites],
        "daily": [{"site_id": d[0], "date": d[1], "n": int(d[2])} for d in daily],
    }


@app.get("/api/site/{site_id}/hours")
def api_site_hours(request: Request, site_id: str, days: int = 7):
    if (r := api_guard(request)):
        return r
    days = max(1, min(days, 60))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    with pool().connection() as con:
        rows = con.execute(f"""
            SELECT to_char(h.hour_start AT TIME ZONE s.timezone, 'YYYY-MM-DD') AS d,
                   to_char(h.hour_start AT TIME ZONE s.timezone, 'Dy')         AS dow,
                   extract(hour FROM h.hour_start AT TIME ZONE s.timezone)::int AS hr,
                   h.class, h.direction, h.n, h.uptime_pct, h.median_speed
            FROM {SCHEMA}.hourly_summary h
            JOIN {SCHEMA}.sites s USING (site_id)
            WHERE h.site_id = %s AND h.hour_start >= %s
            ORDER BY h.hour_start""", (site_id, since)).fetchall()
    hours: dict[tuple, dict] = {}
    VEH = {"car", "truck", "bus", "motorcycle"}
    for d, dow, hr, cls, direction, n, uptime, med in rows:
        k = (d, hr)
        h = hours.setdefault(k, {"d": d, "dow": dow, "h": hr, "veh": 0,
                                 "ped": 0, "bike": 0, "ltr": 0, "rtl": 0,
                                 "u": round(uptime / 100, 2), "classes": {},
                                 "spd": None})
        if cls == "__coverage__":
            continue
        h["classes"][cls] = h["classes"].get(cls, 0) + n
        if cls in VEH:
            h["veh"] += n
        elif cls == "person":
            h["ped"] += n
        else:
            h["bike"] += n
        if direction == "left_to_right":
            h["ltr"] += n
        else:
            h["rtl"] += n
        if cls == "car" and med is not None:
            h["spd"] = med if h["spd"] is None else h["spd"]
    return {"rows": list(hours.values())}


def main():  # `python -m webapp.app` local dev entrypoint
    import uvicorn
    uvicorn.run("webapp.app:app", host="0.0.0.0",
                port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
