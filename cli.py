"""Plan from the terminal, handy for checking the logic:

    python cli.py 2026-09-14 12pm
    python cli.py tomorrow 9:30 --json
    python cli.py --refresh
"""
import argparse
import json

from commute import config, gtfs, planner
from server import live_for, parse_date, parse_time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default="today")
    ap.add_argument("arrive", nargs="?", default="12pm")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="force re-download of both feeds")
    ap.add_argument("--offline", action="store_true", help="skip live checks")
    ap.add_argument("--build-only", action="store_true", help="just download/rebuild the timetable index")
    a = ap.parse_args()

    cfg = config.load()
    index = gtfs.ensure_index(cfg, force=a.refresh)
    if a.build_only:
        return
    date = parse_date(a.date)
    live = {} if a.offline else live_for(date, index)
    try:
        p = planner.plan(index, cfg, date, parse_time(a.arrive), live)
    except planner.PlanError as e:
        print("✗", e, *e.details, sep="\n  ")
        return
    if a.json:
        print(json.dumps(p, indent=2, ensure_ascii=False))
        return
    s = p["summary"]
    print(f"{p['weekday']} {p['date']} — at Union by {p['arrive_by']}\n")
    print(f"  Wake   {s['wake']}\n  Leave  {s['leave']}")
    print(f"  Bus    Route {s['bus_route']}  {s['bus_depart']} → {s['bus_arrive']}")
    print(f"  Train  GO #{s['train_number']}  {s['train_depart']} → {s['train_arrive']}\n")
    for r in p["reasoning"]:
        print("  •", r)
    for w in p["warnings"]:
        print("  ⚠", w)
    if p.get("backup_bus"):
        b = p["backup_bus"]
        print(f"  ↺ Backup: Route {b['route']} {b['depart']} from {b['board_name']} (leave {b['leave']})")
    print("\n  Buses considered:")
    for b in p["considered"]["buses"]:
        print(f"    {b['status']:>9}  Rt {b['route']:<4} {b['dep']} → {b['arr']}  from {b['board']}  (leave {b['leave']})")
    for n in (live.get("go_notices") or {}).get("notices", []):
        print(f"\n  GO notice: {n['title']}")
        for para in n["text"][:4]:
            print("    ", para)


if __name__ == "__main__":
    main()
