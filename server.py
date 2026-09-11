"""TrainTripTime web server (stdlib only, runs the same on Windows and a Pi).

    python server.py            # serves on config host:port (default 0.0.0.0:8765)

On the Pi it runs under systemd socket activation (deploy/commute-tool.socket):
systemd holds the port, starts this process on the first connection, and the
process exits after `idle_exit_minutes` without requests — so it uses no memory
while nobody is planning a trip. Timetable rebuilds (~100 MB peak) run in a
short-lived child process for the same reason.
"""
import datetime as dt
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from commute import calendar_export, config, gtfs, planner, realtime, tz

STATIC = os.path.join(config.ROOT, "static")
INDEX = os.path.join(gtfs.DATA, "index.json")
CFG = config.load()
STATE = {"index": None, "mtime": 0, "error": None, "building": False, "last": time.time()}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def load_index():
    """(Re)read index.json if a build has replaced it."""
    try:
        m = os.path.getmtime(INDEX)
    except OSError:
        return
    if m != STATE["mtime"]:
        with open(INDEX, encoding="utf-8") as f:
            STATE["index"] = json.load(f)
        STATE["mtime"] = m


def feed_times():
    out = {}
    for key in gtfs.feeds(CFG):
        try:
            out[key] = os.path.getmtime(os.path.join(gtfs.DATA, f"{key}.zip"))
        except OSError:
            pass
    return out


def stale():
    idx, t = STATE["index"], feed_times()
    return (not idx or idx.get("config") != gtfs._cfg_hash(CFG) or idx.get("version") != gtfs.INDEX_VERSION
            or len(t) < 2 or time.time() - min(t.values()) > CFG["refresh_hours"] * 3600)


def refresh(force=False):
    """Download/rebuild in a child process so its memory goes back to the OS."""
    if STATE["building"] or not config.configured(CFG):
        return
    STATE["building"] = True

    def run():
        try:
            cmd = [sys.executable, os.path.join(config.ROOT, "cli.py"), "--build-only"] + (["--refresh"] if force else [])
            if os.name == "posix":
                cmd = ["nice", "-n", "15"] + cmd
            r = subprocess.run(cmd, cwd=config.ROOT, capture_output=True, text=True, timeout=900)
            for line in (r.stdout + r.stderr).strip().splitlines():
                log("build: " + line)
            STATE["error"] = None if r.returncode == 0 else "timetable build failed (see log)"
            load_index()
        except Exception as e:
            STATE["error"] = f"{type(e).__name__}: {e}"
            log("refresh failed: " + STATE["error"])
        finally:
            STATE["building"] = False

    threading.Thread(target=run, daemon=True).start()


def parse_time(s):
    """'12', '12pm', '9:30am', '14:05', '1430' -> seconds after midnight."""
    s = (s or "").strip().lower().replace(" ", "").replace(".", "")
    m = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?(am|pm|a|p)?", s)
    if not m:
        raise ValueError(f"Can't read the time {s!r}")
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap:
        h = h % 12 + (12 if ap.startswith("p") else 0)
    if h > 23 or mi > 59:
        raise ValueError(f"Can't read the time {s!r}")
    return h * 3600 + mi * 60


def parse_date(s):
    today = tz.now().date()
    s = (s or "").strip().lower()
    if s in ("", "today"):
        return today
    if s == "tomorrow":
        return today + dt.timedelta(days=1)
    return dt.date.fromisoformat(s)


def live_for(date, index, cfg=None):
    """Realtime is only meaningful for today; GO notices and alerts are always worth showing."""
    cfg = cfg or CFG
    lines = {t["line"] for t in index["go"]["trips"] if t["mode"] == "train"}
    today = date == tz.now().date()
    out = {"go_notices": realtime.go_notices(lines),
           "go": realtime.go_live(cfg["go"]["from_station"], departures=today)}
    if today:
        bt, b = index["bt"], cfg["bus"]
        out["brampton"] = realtime.brampton({t["trip_id"] for t in bt["trips"]}, {t["route"] for t in bt["trips"]},
                                            b.get("realtime_trip_updates"), b.get("realtime_alerts"))
    return out


def names(index):
    n = index["go"]["station_names"]
    return {"home_station": n[CFG["go"]["from_station"]], "destination": n[CFG["go"]["to_station"]]}


def to12(text):
    """'10:44' -> '10:44 AM' in human-readable messages (data fields stay 24h)."""
    def sub(m):
        h, mi = int(m.group(1)) % 24, m.group(2)
        return f"{(h % 12) or 12}:{mi} {'AM' if h < 12 else 'PM'}"
    return re.sub(r"\b(\d{1,2}):(\d{2})\b(?!\s?[AP]M)", sub, text)


def make_plan(q):
    code, p = _make_plan(q)
    for key in ("reasoning", "warnings", "details"):
        if isinstance(p.get(key), list):
            p[key] = [to12(s) for s in p[key]]
    if isinstance(p.get("error"), str):
        p["error"] = to12(p["error"])
    return code, p


def _make_plan(q):
    if not config.configured(CFG):
        return 409, {"error": "Not set up yet", "setup": True}
    load_index()
    if stale():
        refresh()  # serve the current index meanwhile
    index = STATE["index"]
    if not index or index.get("config") != gtfs._cfg_hash(CFG):
        return 503, {"error": "Timetables are still loading — try again in a minute." if STATE["building"] or not STATE["error"]
                     else f"Timetables unavailable: {STATE['error']}"}
    try:
        date, arrive = parse_date(q.get("date")), parse_time(q.get("arrive"))
    except ValueError as e:
        return 400, {"error": str(e)}
    live = live_for(date, index)
    extra = {"notices": realtime.notices_for_week(live["go_notices"]["notices"], date),
             "live_alerts": live["go"]["alerts"], "return_trips": planner.return_trips(index, date),
             "stations": names(index)}
    try:
        p = planner.plan(index, CFG, date, arrive, live)
    except planner.PlanError as e:
        return 200, {"ok": False, "kind": e.kind, "date": date.isoformat(), "error": str(e), "details": e.details,
                     **extra}
    p.update(extra, ok=True)
    p["bus_alerts"] = (live.get("brampton") or {}).get("alerts", [])
    p["live_errors"] = sum((v.get("errors", []) for v in live.values() if isinstance(v, dict)), [])
    p["checked_at"] = tz.now().strftime("%Y-%m-%d %H:%M")
    qs = urllib.parse.urlencode({"date": p["date"], "arrive": p["arrive_by"]})
    p["calendar"] = {"ics": f"/api/plan.ics?{qs}", "google": calendar_export.google_link(p, CFG)}
    return 200, p


# ---------- setup ---------------------------------------------------------------------

def api_stations():
    gtfs.fetch_feeds(CFG, keys=["go"], log=log)
    return 200, {"stations": gtfs.go_stations(os.path.join(gtfs.DATA, "go.zip"))}


def api_geocode(q):
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": q, "format": "json", "limit": 5, "countrycodes": "ca"})
    with urllib.request.urlopen(urllib.request.Request(url, headers=gtfs.UA), timeout=15) as r:
        found = json.load(r)
    return 200, {"results": [{"label": f["display_name"], "lat": float(f["lat"]), "lon": float(f["lon"])} for f in found]}


def api_routes():
    """Bus routes that connect stops near home to the home GO station."""
    load_index()
    idx = STATE["index"]
    if not idx or idx.get("config") != gtfs._cfg_hash(CFG):
        return 503, {"error": "Timetables are still building", "building": STATE["building"]}
    routes = {}
    for t in idx["bt"]["trips"]:
        r = routes.setdefault(t["route"], {"route": t["route"], "headsigns": set(), "trips": 0, "stops": set()})
        r["headsigns"].add(t["headsign"])
        r["trips"] += 1
        r["stops"].update(s[0] for s in t["board"])
    stops = idx["bt"]["stops"]
    out = []
    for r in routes.values():
        walks = [stops[s]["walk"]["min"] for s in r["stops"] if "walk" in stops[s]]
        out.append({"route": r["route"], "headsigns": sorted(r["headsigns"])[:3], "trips_per_week": r["trips"],
                    "closest_walk_min": round(min(walks)) if walks else None})
    return 200, {"routes": sorted(out, key=lambda r: (r["closest_walk_min"] or 99, -r["trips_per_week"]))}


def api_save_config(body):
    global CFG
    current = {}
    if os.path.exists(config.CONFIG_PATH):
        with open(config.CONFIG_PATH, encoding="utf-8") as f:
            current = json.load(f)
        if "brampton" in current and "bus" not in current:
            current["bus"] = current.pop("brampton")
    config.save(config._merge(current, body))
    CFG = config.load()
    refresh()
    return 200, {"ok": True, "configured": config.configured(CFG)}


class Handler(BaseHTTPRequestHandler):
    server_version = "TrainTripTime"

    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}")

    def _send(self, code, body, ctype, extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        STATE["last"] = time.time()
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == "/api/plan":
                self._json(*make_plan(q))
            elif u.path == "/api/plan.ics":
                code, p = make_plan(q)
                if not p.get("ok"):
                    return self._json(code if code != 200 else 422, p)
                self._send(200, calendar_export.ics(p, CFG), "text/calendar; charset=utf-8",
                           {"Content-Disposition": f'inline; filename="traintriptime-{p["date"]}.ics"'})
            elif u.path == "/api/status":
                load_index()
                idx = STATE["index"] or {}
                self._json(200, {
                    "configured": config.configured(CFG), "ready": bool(idx),
                    "building": STATE["building"], "error": STATE["error"], "built_at": idx.get("built_at"),
                    "checked": feed_times(), "now": tz.now().isoformat(timespec="minutes"),
                    "stations": names(idx) if idx and idx.get("config") == gtfs._cfg_hash(CFG) else None,
                })
            elif u.path == "/api/config":
                self._json(200, {"configured": config.configured(CFG), "config": CFG})
            elif u.path == "/api/stations":
                self._json(*api_stations())
            elif u.path == "/api/geocode":
                self._json(*api_geocode(q.get("q", "")))
            elif u.path == "/api/routes":
                self._json(*api_routes())
            elif u.path == "/setup":
                self._static("/setup.html")
            else:
                self._static(u.path)
        except Exception as e:
            traceback.print_exc()
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        STATE["last"] = time.time()
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/refresh":
                refresh(force=True)
                return self._json(202, {"ok": True})
            if path == "/api/config":
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                return self._json(*api_save_config(body))
            self._json(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def _static(self, path):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        full = os.path.realpath(os.path.join(STATIC, rel))
        if not full.startswith(os.path.realpath(STATIC)) or not os.path.isfile(full):
            return self._json(404, {"error": "not found"})
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith(".webmanifest"):
            ctype = "application/manifest+json"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)


def idle_watch(httpd, minutes):
    while True:
        time.sleep(20)
        if not STATE["building"] and time.time() - STATE["last"] > minutes * 60:
            log(f"idle for {minutes} min — exiting; systemd starts it again on the next visit")
            httpd.shutdown()
            return


def main():
    socket_activated = os.environ.get("LISTEN_PID") == str(os.getpid()) and int(os.environ.get("LISTEN_FDS", 0)) > 0
    if socket_activated:
        httpd = ThreadingHTTPServer(("", 0), Handler, bind_and_activate=False)
        httpd.socket.close()
        httpd.socket = socket.socket(fileno=3)  # the port systemd is holding for us
        threading.Thread(target=idle_watch, args=(httpd, CFG.get("idle_exit_minutes", 10)), daemon=True).start()
        log("started by systemd socket activation")
    else:
        host, port = CFG.get("host", "0.0.0.0"), int(CFG.get("port", 8765))
        httpd = ThreadingHTTPServer((host, port), Handler)
        log(f"serving on http://{host}:{port}")
    load_index()
    if stale():
        refresh()
    httpd.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
