"""Fetch the GO and local-bus GTFS feeds and boil them down to the few trips the
planner needs, so answering a request never touches the 118 MB GO
stop_times.txt. The result is cached as data/index.json and rebuilt whenever a
feed is re-downloaded or the home/station config changes.

Memory matters (this runs on a shared Raspberry Pi): only GO *train* trips are
held in full; GO buses are picked up with a second, targeted pass.
"""
import csv
import datetime as dt
import email.utils
import hashlib
import io
import json
import math
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
import zipfile

from .config import ROOT

GO_URL = "https://assets.metrolinx.com/raw/upload/Documents/Metrolinx/Open%20Data/GO-GTFS.zip"
FOOT_ROUTER = "https://routing.openstreetmap.de/routed-foot/route/v1/foot/{},{};{},{}?overview=false"
UA = {"User-Agent": "TrainTripTime/1.0 (+https://github.com/shreywy/TrainTripTime)"}
INDEX_VERSION = 2
DATA = os.environ.get("TTT_DATA") or os.path.join(ROOT, "data")
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
BUS_NEAR_STATION_M = 400

_lock = threading.Lock()


def feeds(cfg):
    return {"go": GO_URL, "bt": cfg["bus"]["gtfs_url"]}


def feed_names(cfg):
    return {"go": "GO Transit GTFS (Metrolinx open data)", "bt": f"{cfg['bus'].get('agency_name') or 'Local bus'} GTFS"}


def secs(t):
    h, m, s = t.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def distance_m(lat1, lon1, lat2, lon2):
    la1, lo1, la2, lo2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(a))


def _reader(zf, name):
    """csv.reader over a zip member -> (column index map, row iterator)."""
    if name not in zf.namelist():
        return {}, iter(())
    rows = csv.reader(io.TextIOWrapper(zf.open(name), encoding="utf-8-sig", newline=""))
    header = next(rows, [])
    return {h.strip(): i for i, h in enumerate(header)}, rows


def _dicts(zf, name):
    cols, rows = _reader(zf, name)
    for row in rows:
        yield {k: (row[i].strip() if i < len(row) else "") for k, i in cols.items()}


def _latlon(stop):
    try:
        return float(stop["stop_lat"]), float(stop["stop_lon"])
    except (KeyError, ValueError):
        return None


def _calendar(zf):
    days = {}
    for r in _dicts(zf, "calendar.txt"):
        d = dt.datetime.strptime(r["start_date"], "%Y%m%d").date()
        end = dt.datetime.strptime(r["end_date"], "%Y%m%d").date()
        while d <= end:
            if r[DAYS[d.weekday()]] == "1":
                days.setdefault(d.strftime("%Y%m%d"), set()).add(r["service_id"])
            d += dt.timedelta(days=1)
    for r in _dicts(zf, "calendar_dates.txt"):
        s = days.setdefault(r["date"], set())
        if r["exception_type"] == "1":
            s.add(r["service_id"])
        else:
            s.discard(r["service_id"])
    return {k: sorted(v) for k, v in sorted(days.items()) if v}


def _feed_info(zf):
    info = next(_dicts(zf, "feed_info.txt"), {})
    return {k: info.get("feed_" + k, "") for k in ("version", "start_date", "end_date")}


def _download(url, dest, log):
    headers = dict(UA)
    if os.path.exists(dest):
        headers["If-Modified-Since"] = email.utils.formatdate(os.path.getmtime(dest), usegmt=True)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=180) as r:
            tmp = dest + ".part"
            with open(tmp, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
        zipfile.ZipFile(tmp).close()  # refuse to replace a good feed with an error page
        os.replace(tmp, dest)
        log(f"downloaded {url}")
        return True
    except urllib.error.HTTPError as e:
        if e.code != 304:
            raise
        os.utime(dest)
        log(f"not modified: {url}")
        return False


def _walk(home, lat, lon, cache):
    key = f"{home['lat']:.6f},{home['lon']:.6f}>{lat:.6f},{lon:.6f}"
    if key in cache:
        return cache[key]
    url = FOOT_ROUTER.format(home["lon"], home["lat"], lon, lat)
    try:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                route = json.load(r)["routes"][0]
        except urllib.error.URLError as e:
            # routing.openstreetmap.de has served an expired certificate; a walking
            # distance isn't worth failing over, so retry this one host unverified.
            if not isinstance(e.reason, ssl.SSLCertVerificationError):
                raise
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20, context=ctx) as r:
                route = json.load(r)["routes"][0]
        cache[key] = {"m": round(route["distance"]), "min": round(route["duration"] / 60, 1),
                      "source": "OpenStreetMap walking route"}
        return cache[key]
    except Exception:
        m = distance_m(home["lat"], home["lon"], lat, lon) * 1.35
        return {"m": round(m), "min": round(m / 75, 1), "source": "straight-line estimate (routing unavailable)"}


def go_stations(path):
    """Train stations in the GO feed (their stop_ids are letter codes like MO, UN)."""
    zf = zipfile.ZipFile(path)
    out = []
    for r in _dicts(zf, "stops.txt"):
        ll = _latlon(r)
        if r["stop_id"].isalpha() and ll:
            out.append({"code": r["stop_id"], "name": r["stop_name"], "lat": ll[0], "lon": ll[1]})
    return sorted(out, key=lambda s: s["name"])


def build_go(path, cfg):
    g = cfg["go"]
    frm, to = g["from_station"], g["to_station"]
    zf = zipfile.ZipFile(path)
    routes = {r["route_id"]: r for r in _dicts(zf, "routes.txt")}
    stops = {r["stop_id"]: r for r in _dicts(zf, "stops.txt")}
    names = {k: v["stop_name"] for k, v in stops.items()}
    if frm not in stops or to not in stops:
        raise ValueError(f"GO station code {frm if frm not in stops else to!r} isn't in the GO feed")

    def near(code):  # GO bus stops serving a train station
        la, lo = _latlon(stops[code])
        return {sid for sid, s in stops.items() if not sid.isalpha() and (ll := _latlon(s))
                and distance_m(la, lo, *ll) <= BUS_NEAR_STATION_M}

    bus_from = {g["bus_from_stop"]} if g.get("bus_from_stop") else near(frm)
    bus_to = {g["bus_to_stop"]} if g.get("bus_to_stop") else near(to)
    bus_watch = bus_from | bus_to

    trains = {}
    for r in _dicts(zf, "trips.txt"):
        route = routes.get(r["route_id"], {})
        if route.get("route_type") == "2":
            trains[r["trip_id"]] = (r["service_id"], route.get("route_short_name", ""),
                                    route.get("route_long_name", ""), r["trip_headsign"])

    cols, rows = _reader(zf, "stop_times.txt")
    ti, si, sq, ar, de = (cols[c] for c in ("trip_id", "stop_id", "stop_sequence", "arrival_time", "departure_time"))
    per, bus_rows = {}, {}
    for row in rows:
        tid = row[ti]
        if tid in trains:
            per.setdefault(tid, []).append((int(row[sq]), row[si], row[ar], row[de]))
        elif row[si] in bus_watch:
            bus_rows.setdefault(tid, []).append((int(row[sq]), row[si], row[ar], row[de]))

    bus_info = {}
    if bus_rows:  # second, targeted pass for just the GO buses we saw
        for r in _dicts(zf, "trips.txt"):
            if r["trip_id"] in bus_rows:
                route = routes.get(r["route_id"], {})
                bus_info[r["trip_id"]] = (r["service_id"], route.get("route_short_name", ""),
                                          route.get("route_long_name", ""), r["trip_headsign"])

    full, short, back = [], [], []
    for tid, st in per.items():
        st.sort()
        sid, line, line_name, head = trains[tid]
        ids = [s[1] for s in st]
        if frm in ids and to in ids and ids.index(to) < ids.index(frm):
            back.append({"service_id": sid, "mode": "train", "number": tid.rsplit("-", 1)[-1], "line": line,
                         "dep": secs(st[ids.index(to)][3]), "arr": secs(st[ids.index(frm)][2])})
        if frm not in ids:
            continue
        rest = st[ids.index(frm):]
        j = next((k for k, s in enumerate(rest) if s[1] == to), None)
        seg = rest if j is None else rest[: j + 1]
        trip = {
            "trip_id": tid, "service_id": sid, "number": tid.rsplit("-", 1)[-1], "mode": "train",
            "line": line, "line_name": line_name, "headsign": head,
            "dep": secs(seg[0][3]),
            "arr": secs(seg[-1][2]) if j is not None else None,
            "end": {"stop": seg[-1][1], "name": names.get(seg[-1][1], seg[-1][1]), "time": secs(seg[-1][2])},
            "stops": [[s[1], names.get(s[1], s[1]), secs(s[3] if k == 0 else s[2])] for k, s in enumerate(seg)],
        }
        (full if j is not None else short).append(trip)

    want_bus = "bus" in g.get("modes", ["train"])
    for tid, st in bus_rows.items():
        if tid not in bus_info:
            continue
        st.sort()
        sid, line, line_name, head = bus_info[tid]
        a = next((s for s in st if s[1] in bus_to), None)
        b = next((s for s in st if s[1] in bus_from and a and s[0] > a[0]), None)
        if a and b:
            back.append({"service_id": sid, "mode": "bus", "number": tid.rsplit("-", 1)[-1], "line": line,
                         "dep": secs(a[3]), "arr": secs(b[2])})
        a = next((s for s in st if s[1] in bus_from), None)
        b = next((s for s in st if s[1] in bus_to and a and s[0] > a[0]), None)
        if want_bus and a and b:
            full.append({"trip_id": tid, "service_id": sid, "number": tid.rsplit("-", 1)[-1], "mode": "bus",
                         "line": line, "line_name": line_name, "headsign": head, "dep": secs(a[3]), "arr": secs(b[2]),
                         "end": {"stop": b[1], "name": names.get(b[1], b[1]), "time": secs(b[2])},
                         "stops": [[a[1], names.get(a[1], a[1]), secs(a[3])], [b[1], names.get(b[1], b[1]), secs(b[2])]]})

    # A train that stops at the home station but never reaches the destination only
    # matters if it heads that way (e.g. weekend construction short-turns).
    corridor = {s[0] for t in full if t["mode"] == "train" for s in t["stops"]} - {frm}
    short = [t for t in short if t["end"]["stop"] in corridor]

    return {
        "feed": _feed_info(zf),
        "calendar": _calendar(zf),
        "trips": sorted(full + short, key=lambda t: (t["service_id"], t["dep"])),
        "back": sorted(back, key=lambda t: (t["service_id"], t["dep"])),
        "station_names": {k: names[k] for k in (frm, to)},
        "station_coords": {k: list(_latlon(stops[k])) for k in (frm, to)},
    }


def build_bt(path, cfg, walk_cache, station_ll):
    b, home = cfg["bus"], cfg["home"]
    zf = zipfile.ZipFile(path)
    routes = {r["route_id"]: r.get("route_short_name") or r["route_id"] for r in _dicts(zf, "routes.txt")}
    parents = set(b.get("destination_parents") or [])
    stops, origin, dest = {}, set(), set()
    for r in _dicts(zf, "stops.txt"):
        ll = _latlon(r)
        if not ll:
            continue
        stops[r["stop_id"]] = {"name": r["stop_name"], "code": r.get("stop_code", ""), "lat": ll[0], "lon": ll[1]}
        is_stop = r.get("location_type", "0") in ("0", "")
        if (r.get("parent_station") in parents) if parents else \
                (is_stop and distance_m(*station_ll, *ll) <= b.get("station_radius_m", 350)):
            dest.add(r["stop_id"])
        elif is_stop and distance_m(home["lat"], home["lon"], *ll) <= b["stop_radius_m"]:
            origin.add(r["stop_id"])

    trips = {r["trip_id"]: (r["service_id"], routes.get(r["route_id"], r["route_id"]), r["trip_headsign"])
             for r in _dicts(zf, "trips.txt")}
    cols, rows = _reader(zf, "stop_times.txt")
    ti, si, sq, ar, de = (cols[c] for c in ("trip_id", "stop_id", "stop_sequence", "arrival_time", "departure_time"))
    watch = origin | dest
    per = {}
    for row in rows:
        if row[si] in watch and row[ti] in trips:
            per.setdefault(row[ti], []).append((int(row[sq]), row[si], secs(row[ar]), secs(row[de])))

    out, used = [], set()
    for tid, st in per.items():
        st.sort()
        board = []
        for _, sid, arr, dep in st:
            if sid in origin:
                board.append([sid, dep])
            elif board:  # first station stop after boarding near home
                service, route, head = trips[tid]
                out.append({"trip_id": tid, "service_id": service, "route": route, "headsign": head,
                            "board": board, "alight": [sid, arr]})
                used.update(x[0] for x in board)
                used.add(sid)
                board = []

    kept = {}
    for sid in used:
        s = dict(stops[sid])
        if sid in origin:
            s["walk"] = _walk(home, s["lat"], s["lon"], walk_cache)
        kept[sid] = s
    return {"feed": _feed_info(zf), "calendar": _calendar(zf),
            "trips": sorted(out, key=lambda t: (t["service_id"], t["alight"][1])), "stops": kept}


def _cfg_hash(cfg):
    blob = json.dumps([cfg["home"], cfg["go"], cfg["bus"].get("gtfs_url"), cfg["bus"].get("stop_radius_m"),
                       cfg["bus"].get("station_radius_m"), cfg["bus"].get("destination_parents")], sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


def fetch_feeds(cfg, force=False, keys=None, log=print):
    """Download stale feeds; returns True if any changed."""
    os.makedirs(DATA, exist_ok=True)
    changed = False
    for key, url in feeds(cfg).items():
        if keys and key not in keys:
            continue
        zp = os.path.join(DATA, f"{key}.zip")
        age = time.time() - os.path.getmtime(zp) if os.path.exists(zp) else None
        if force or age is None or age > cfg["refresh_hours"] * 3600:
            try:
                changed |= _download(url, zp, log)
            except Exception as e:
                if not os.path.exists(zp):
                    raise
                log(f"download failed for {key}, keeping previous copy: {e}")
    return changed


def ensure_index(cfg, force=False, log=print):
    """Download stale feeds and rebuild the index if anything changed."""
    with _lock:
        idx_path = os.path.join(DATA, "index.json")
        idx = _read_json(idx_path)
        changed = fetch_feeds(cfg, force, log=log)
        if idx and not changed and idx.get("version") == INDEX_VERSION and idx.get("config") == _cfg_hash(cfg):
            return idx

        log("building timetable index...")
        started = time.time()
        walk_path = os.path.join(DATA, "walk-cache.json")
        walk_cache = _read_json(walk_path, {})
        go = build_go(os.path.join(DATA, "go.zip"), cfg)
        bt = build_bt(os.path.join(DATA, "bt.zip"), cfg, walk_cache, go["station_coords"][cfg["go"]["from_station"]])
        idx = {"version": INDEX_VERSION, "config": _cfg_hash(cfg), "built_at": time.time(), "go": go, "bt": bt}
        names = feed_names(cfg)
        for key, url in feeds(cfg).items():
            idx[key].update(url=url, name=names[key], downloaded_at=os.path.getmtime(os.path.join(DATA, f"{key}.zip")))
        _write_json(walk_path, walk_cache)
        _write_json(idx_path, idx)
        log(f"index built in {time.time() - started:.1f}s "
            f"({len(go['trips'])} GO trips, {len(go['back'])} return trips, {len(bt['trips'])} bus trips)")
        return idx
