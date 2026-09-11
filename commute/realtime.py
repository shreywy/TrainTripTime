"""Live checks layered on top of the timetable.

- Brampton Transit publishes GTFS-realtime as plain JSON, no key needed.
- GO's posted service notices (construction, schedule changes) are scraped from
  the data embedded in gotransit.com/en/service-updates.
- Live GO departures (status, delay, platform) and live station alerts come from
  the same keyless api.metrolinx.com endpoints gotransit.com's departure board uses.
Every fetch is cached briefly and failures are reported, never raised.
"""
import datetime as dt
import gzip
import html as htmllib
import json
import re
import time
import urllib.request

BT_TRIP_UPDATES = "https://gtfs-rt-merge.prod.bt-cadavl.com/BramptonTransit/GTFS/merged_TripUpdate.json"
BT_ALERTS = "https://gtfs-rt-merge.prod.bt-cadavl.com/BramptonTransit/GTFS/merged_Alert.json"
GO_UPDATES_PAGE = "https://www.gotransit.com/en/service-updates"
GO_API = "https://api.metrolinx.com/external/go/"

_cache = {}


def _get(url, ttl, as_json=True):
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (commute-tool)", "Accept": "application/json",
                                               "Accept-Encoding": "gzip", "Referer": "https://www.gotransit.com/"})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":  # api.metrolinx.com gzips even when not asked
        raw = gzip.decompress(raw)
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError:  # its alert text is sometimes Windows-1252
        body = raw.decode("cp1252", errors="replace")
    val = json.loads(body) if as_json else body
    _cache[url] = (time.time(), val)
    return val


def _text(translated):
    for t in (translated or {}).get("translation", []):
        if t.get("language") in (None, "", "en"):
            return t.get("text", "")
    return ""


def brampton(trip_ids, route_ids, trip_url=BT_TRIP_UPDATES, alert_url=BT_ALERTS):
    """Live predictions for the given trips, plus alerts touching the given routes.
    Works with any agency that publishes GTFS-realtime as JSON (Brampton does)."""
    out = {"trips": {}, "alerts": [], "errors": []}
    if not trip_url:
        return out
    try:
        feed = _get(trip_url, 30)
        out["fetched"] = int(feed.get("header", {}).get("timestamp", 0))
        for e in feed.get("entity", []):
            tu = e.get("trip_update") or {}
            tid = tu.get("trip", {}).get("trip_id")
            if tid not in trip_ids:
                continue
            stops = {}
            for u in tu.get("stop_time_update", []):
                stops[u.get("stop_id")] = {
                    "arr": int(u["arrival"]["time"]) if "arrival" in u else None,
                    "dep": int(u["departure"]["time"]) if "departure" in u else None,
                    "skipped": u.get("schedule_relationship") == "SKIPPED",
                }
            out["trips"][tid] = {"cancelled": tu.get("trip", {}).get("schedule_relationship") == "CANCELED",
                                 "stops": stops}
    except Exception as e:
        out["errors"].append(f"Brampton Transit live trips unavailable: {e}")
    try:
        for e in (_get(alert_url, 120).get("entity", []) if alert_url else []):
            a = e.get("alert") or {}
            if any(i.get("route_id") in route_ids for i in a.get("informed_entity", [])):
                out["alerts"].append({"title": _text(a.get("header_text")), "text": _text(a.get("description_text"))})
    except Exception as e:
        out["errors"].append(f"Brampton Transit alerts unavailable: {e}")
    return out


def _rich_text(node):
    """Flatten gotransit.com's rich-text JSON into paragraphs."""
    paras = []

    def walk(n, buf):
        if isinstance(n, dict):
            if isinstance(n.get("text"), str):
                buf.append(n["text"])
            for k, v in n.items():
                if isinstance(v, (dict, list)) and k != "attrs":
                    walk(v, buf)
            if n.get("type") in ("p", "li") and buf:
                paras.append("".join(buf).strip())
                buf.clear()
        elif isinstance(n, list):
            for c in n:
                walk(c, buf)

    walk(node, [])
    return [p for p in paras if p]


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                        "nov", "dec"), 1)}
_DATES = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)[a-z]*\.?\s+"
                    r"((?:\d{1,2}(?:\s*[-–]\s*\d{1,2})?(?:\s*(?:,|and|&)\s*)?)+)", re.I)


def notice_dates(text, year):
    """Dates a notice mentions, e.g. "Sept. 11, 14-18 and 21-22" -> 8 dates."""
    out = set()
    for m in _DATES.finditer(text):
        month = _MONTHS[m.group(1).lower()[:3]]
        for a, b in re.findall(r"(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?", m.group(2)):
            for d in range(int(a), int(b or a) + 1):
                try:
                    out.add(dt.date(year, month, d))
                except ValueError:
                    pass
    return out


def notices_for_week(notices, day):
    """Keep only notices that mention a date in the Monday–Sunday week containing `day`."""
    start = day - dt.timedelta(days=day.weekday())
    end = start + dt.timedelta(days=6)
    return [n for n in notices
            if any(start <= d <= end for d in notice_dates(" ".join([n["title"], *n["text"]]), day.year))]


def go_notices(line_codes):
    """Posted notices for the given GO line codes (e.g. {"KI"})."""
    out = {"notices": [], "errors": [], "source": GO_UPDATES_PAGE}
    try:
        html = _get(GO_UPDATES_PAGE, 300, as_json=False)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        updates = json.loads(m.group(1))["props"]["pageProps"]["content"]["staticUpdates"]
        for kind, entries in updates.items():
            for line in entries or []:
                if line.get("code") not in line_codes:
                    continue
                for a in line.get("alerts", []):
                    out["notices"].append({"kind": kind, "line": line.get("code"), "title": (a.get("title") or "").strip(),
                                           "text": _rich_text(a.get("description")), "published": line.get("publishDate")})
    except Exception as e:
        out["errors"].append(f"GO service notices unavailable: {e}")
    return out


def _html_paras(markup):
    markup = re.sub(r"(?is)<style.*?</style>", "", markup or "")
    markup = re.sub(r"(?i)<br\s*/?>|</(p|div|li)>", "\n", markup)
    text = htmllib.unescape(re.sub(r"<[^>]+>", "", markup))
    paras = []
    for p in text.split("\n"):
        p = re.sub(r"\s+", " ", p).strip()
        if p and (not paras or paras[-1] != p):
            paras.append(p)
    return paras


def go_live(station, departures=True):
    """Live status of today's departures from `station`, keyed by trip number,
    plus live alerts (delays, disruptions) posted for that station's lines."""
    out = {"running": {}, "cancelled": {}, "alerts": [], "errors": []}
    if departures:
        try:
            d = _get(f"{GO_API}departures/stops/{station}/status/departures?page=1&transitTypeName=All&pageLimit=50", 30)
            for it in (d.get("allDepartures") or {}).get("items") or []:
                status = (it.get("status") or "").lower()
                delay = it.get("delaySeconds") or 0
                info = {"status": status, "platform": it.get("platform") or None,
                        "scheduled": it.get("scheduledTime"), "expected": it.get("delayedDepartureTime") or None,
                        "delay_min": round(delay / 60) if delay > 0 else 0, "message": it.get("delayMessage") or ""}
                out["running"][str(it.get("tripNumber"))] = info
                if "cancel" in status:
                    out["cancelled"][str(it.get("tripNumber"))] = info
        except Exception as e:
            out["errors"].append(f"GO live departures unavailable: {e}")
    try:
        s = _get(f"{GO_API}serviceupdate/en/stations/{station}/all", 60)
        for group, key in (("Trains", "Train"), ("Stations", "Station")):
            for line in (s.get(group) or {}).get(key) or []:
                for n in (line.get("Notifications") or {}).get("Notification") or []:
                    out["alerts"].append({"title": (n.get("MessageSubject") or "").strip(),
                                          "text": _html_paras(n.get("MessageBody")), "kind": n.get("SubCategory"),
                                          "line": line.get("CorridorName") or line.get("Name")})
    except Exception as e:
        out["errors"].append(f"GO live alerts unavailable: {e}")
    return out
