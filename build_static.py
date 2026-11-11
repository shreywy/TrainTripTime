"""Pre-compute plans into a static site, for hosting without a server (GitHub Pages).

    python build_static.py site/shrey

Plans every arrival time the page offers for the next DAYS days, using whatever
config.json holds. The GitHub Action re-runs this every 30 minutes, so live data
on the page is at most that old.
"""
import datetime as dt
import json
import os
import shutil
import sys
import time
import urllib.parse

import server
from commute import calendar_export, gtfs, tz

DAYS = 14
TIMES = [f"{m // 60:02d}:{m % 60:02d}" for m in range(6 * 60, 22 * 60 + 1, 15)]  # matches buildTimes() in index.html
COMMON = ("notices", "live_alerts", "return_trips", "stations", "bus_alerts", "live_errors")  # same for every time on a day

_live, _orig_live = {}, server.live_for


def cached_live(date, index, cfg=None):
    """One round of live fetches per day, not per arrival time."""
    if date not in _live:
        _live[date] = _orig_live(date, index, cfg)
    return _live[date]


server.live_for = cached_live


def scrub(text):
    """The site is public: keep the home address out of it (it shows up in the walk leg and calendar links)."""
    label = server.CFG["home"]["label"]
    for form in {label, urllib.parse.quote_plus(label), urllib.parse.quote(label), urllib.parse.quote(label, safe="")}:
        text = text.replace(form, "Home") if form else text
    return text


def main(out):
    gtfs.ensure_index(server.CFG)
    server.load_index()
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(server.STATIC, out, ignore=shutil.ignore_patterns("setup.html"))
    with open(os.path.join(out, "index.html"), encoding="utf-8") as f:
        html = f.read()
    marker = "const STATIC_PLANS = null;"
    assert marker in html, "index.html no longer has the STATIC_PLANS marker"
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(html.replace(marker, 'const STATIC_PLANS = "plans/";'))
    with open(os.path.join(out, "gcal.html"), "w", encoding="utf-8") as f:
        # Same job as the server's /api/gcal redirect: keeps iOS from handing the link to the app.
        f.write('<!doctype html><meta charset="utf-8"><script>location.replace(decodeURIComponent(location.hash.slice(1)))</script>')

    os.makedirs(os.path.join(out, "plans"))
    today = tz.now().date()
    for i in range(DAYS):
        date = (today + dt.timedelta(days=i)).isoformat()
        day = {"common": None, "plans": {}}
        for t in TIMES:
            code, p = server.make_plan({"date": date, "arrive": t})
            if code != 200:
                sys.exit(f"{date} {t}: {code} {p.get('error')}")
            p = json.loads(scrub(json.dumps(p, ensure_ascii=False)))
            if p.get("ok"):
                p["calendar"]["redirect"] = "gcal.html#" + urllib.parse.quote(p["calendar"]["google"], safe="")
            common = {k: p.pop(k) for k in COMMON if k in p}
            day["common"] = {**common, **(day["common"] or {})}
            day["plans"][t] = p
        with open(os.path.join(out, "plans", f"{date}.json"), "w", encoding="utf-8") as f:
            json.dump(day, f, ensure_ascii=False, separators=(",", ":"))
        print(date, "done", flush=True)
    with open(os.path.join(out, "status.json"), "w", encoding="utf-8") as f:
        json.dump({"configured": True, "ready": True, "checked": {"built": time.time()},
                   "stations": server.names(server.STATE["index"])}, f)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "site")
