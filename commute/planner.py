"""Work backwards from "be at Union by T" to a wake-up time.

1. Latest GO train Mount Pleasant -> Union that arrives by T.
2. Bus into Mount Pleasant that arrives between `max_platform_wait_min` and
   `platform_buffer_min` before that train leaves; of those, the one that lets
   you leave home latest.
3. Leave = bus departure - walk to the stop - `stop_buffer_min`.
4. Wake = leave - prep, optionally rounded down to the start of the hour.

Every step is recorded in `reasoning` so the result can be checked by hand.
"""
import datetime as dt
import math

from . import tz


def hm(s):
    s = int(s) // 60
    return f"{s // 60 % 24:02d}:{s % 60:02d}"


def mins(s):
    return int(round(s / 60))


class PlanError(Exception):
    """kind: "no_service" (trains don't reach Union that day, e.g. construction),
    "out_of_range", "too_early" or "no_route"."""

    def __init__(self, message, details=None, kind="no_route"):
        super().__init__(message)
        self.details = details or []
        self.kind = kind


def _walk_min(stop_id, stop, cfg):
    over = cfg["bus"].get("walk_overrides_min", {})
    if stop_id in over or stop.get("code") in over:
        return over.get(stop_id, over.get(stop.get("code"))), "your override"
    return math.ceil(stop["walk"]["min"]), stop["walk"]["source"]


def bus_options(index, cfg, day_key, train_dep, skip_trips=()):
    rules, bt = cfg["rules"], index["bt"]
    sids = set(bt["calendar"].get(day_key, []))
    latest = train_dep - rules["platform_buffer_min"] * 60
    earliest = train_dep - rules["max_platform_wait_min"] * 60
    allowed = set(cfg["bus"].get("allowed_routes") or [])
    opts = []
    for t in bt["trips"]:
        if t["service_id"] not in sids or t["trip_id"] in skip_trips or (allowed and t["route"] not in allowed):
            continue
        alight, arr = t["alight"]
        for stop_id, dep in t["board"]:
            stop = bt["stops"][stop_id]
            walk, walk_src = _walk_min(stop_id, stop, cfg)
            opts.append({
                "trip_id": t["trip_id"], "route": t["route"], "headsign": t["headsign"],
                "board_stop": stop_id, "board_name": stop["name"], "board_code": stop["code"],
                "dep": dep, "arr": arr, "alight_stop": alight, "alight_name": bt["stops"][alight]["name"],
                "walk_min": walk, "walk_m": None if walk_src == "your override" else stop["walk"]["m"],
                "walk_source": walk_src,
                "leave": dep - (walk + rules["stop_buffer_min"]) * 60,
                "wait_min": mins(train_dep - arr),
                "fits": earliest <= arr <= latest, "too_late": arr > latest,
            })
    return opts, earliest, latest


def _latest_leave(pool):
    return max(pool, key=lambda o: (o["leave"], -o["walk_min"]), default=None)


def choose_bus(opts, cfg, train_dep):
    """Preferred routes win whenever they work, even with a somewhat longer wait
    at the station; other routes are the fallback."""
    b, rules = cfg["bus"], cfg["rules"]
    pref = set(b.get("preferred_routes") or [])
    fits = [o for o in opts if o["fits"]]
    early = [o for o in opts if not o["too_late"] and not o["fits"] and o["arr"] >= train_dep - 3600]
    stretch = train_dep - (rules["max_platform_wait_min"] + b.get("preferred_extra_wait_min", 0)) * 60
    tiers = ([o for o in fits if o["route"] in pref],
             [o for o in early if o["route"] in pref and o["arr"] >= stretch],
             fits,
             [o for o in early if o["route"] in pref],
             early)
    return next((p for p in map(_latest_leave, tiers) if p), None)


def _per_trip(opts):
    """Best boarding stop per bus trip (latest leave, then shortest walk)."""
    best = {}
    for o in opts:
        b = best.get(o["trip_id"])
        if not b or (o["leave"], -o["walk_min"]) > (b["leave"], -b["walk_min"]):
            best[o["trip_id"]] = o
    return sorted(best.values(), key=lambda o: o["arr"])


def plan(index, cfg, date, arrive_by, live=None):
    """date: datetime.date; arrive_by: seconds after midnight at Union."""
    live = live or {}
    rules, go = cfg["rules"], index["go"]
    key = date.strftime("%Y%m%d")
    names = go["station_names"]
    home_st, union = names[cfg["go"]["from_station"]], names[cfg["go"]["to_station"]]

    if key not in go["calendar"]:
        f = go["feed"]
        raise PlanError(f"The GO timetable I have covers {f['start_date']}–{f['end_date']}, not {date.isoformat()}.",
                        kind="out_of_range")
    if key not in index["bt"]["calendar"]:
        f = index["bt"]["feed"]
        raise PlanError(f"The Brampton Transit timetable I have covers {f['start_date']}–{f['end_date']}, "
                        f"not {date.isoformat()}.", kind="out_of_range")

    sids = set(go["calendar"][key])
    day = [t for t in go["trips"] if t["service_id"] in sids]
    modes = set(cfg["go"]["modes"])
    reach = [t for t in day if t["arr"] is not None and t["mode"] in modes]
    short = [t for t in day if t["arr"] is None]
    reasoning, warnings = [], []
    if not reach:
        # e.g. weekend construction: trains short-turn before Union.
        details = []
        if short:
            ends = sorted({t["end"]["name"] for t in short})
            details.append(f"Service is cut short: trains from {home_st} only run as far as {', '.join(ends)} "
                           f"this day (usually planned construction — see the GO notice below).")
            ok = [t for t in short if t["end"]["time"] <= arrive_by]
            if ok:
                best = max(ok, key=lambda t: t["dep"])
                details.append(f"Closest: the {hm(best['dep'])} train reaches {best['end']['name']} at "
                               f"{hm(best['end']['time'])}, then you'd need the TTC from there.")
        raise PlanError(f"No routes found — no trains run from {home_st} to {union} on "
                        f"{date.strftime('%A, %B ')}{date.day}.", details, kind="no_service")

    latest_arr = arrive_by - rules["arrive_buffer_at_union_min"] * 60
    cands = sorted((t for t in reach if t["arr"] <= latest_arr), key=lambda t: t["dep"], reverse=True)
    if not cands:
        first = min(reach, key=lambda t: t["arr"])
        raise PlanError(f"The first train that day reaches {union} at {hm(first['arr'])}, "
                        f"after your {hm(arrive_by)} target.", kind="too_early")

    reasoning.append(f"Target: at {union} by {hm(arrive_by)}"
                     + (f" (minus {rules['arrive_buffer_at_union_min']} min buffer → {hm(latest_arr)})"
                        if rules["arrive_buffer_at_union_min"] else "") + ".")

    go_live = live.get("go") or {}
    cancelled = go_live.get("cancelled", {})  # only filled in for today
    bt_live = live.get("brampton") or {}
    dead_buses = {tid for tid, t in bt_live.get("trips", {}).items() if t.get("cancelled")}

    train = bus = None
    for t in cands[:4]:
        if t["number"] in cancelled:
            warnings.append(f"GO train {t['number']} ({hm(t['dep'])} from {home_st}) is cancelled — using an earlier one.")
            continue
        opts, earliest, latest = bus_options(index, cfg, key, t["dep"], dead_buses)
        pick = choose_bus(opts, cfg, t["dep"])
        if pick:
            train, bus = t, pick
            break
        warnings.append(f"No bus gets you to {home_st} in time for the {hm(t['dep'])} train — trying an earlier train.")
    if not train:
        raise PlanError("Couldn't find a bus + train combination that works.", warnings)

    later = [t for t in reach if t["dep"] > train["dep"]]
    nxt = min(later, key=lambda t: t["dep"], default=None)
    reasoning.append(f"Latest {train['mode']} arriving in time: GO {train['line']} #{train['number']} leaves "
                     f"{train['stops'][0][1]} {hm(train['dep'])}, arrives {train['stops'][-1][1]} {hm(train['arr'])}"
                     + (f" (the next one, {hm(nxt['dep'])} → {hm(nxt['arr'])}, is too late)." if nxt else "."))
    reasoning.append(f"Be at {home_st} {rules['platform_buffer_min']}–{rules['max_platform_wait_min']} min before "
                     f"{hm(train['dep'])} → bus must arrive between {hm(earliest)} and {hm(latest)}.")
    cut_short = sorted((t for t in short if t["dep"] > train["dep"] and t["end"]["time"] <= arrive_by),
                       key=lambda t: t["dep"])
    if cut_short:
        warnings.append(f"Later trains (from {hm(cut_short[0]['dep'])}) only run to {cut_short[0]['end']['name']}, "
                        f"so this is the latest one that reaches {union}.")
    pref = cfg["bus"].get("preferred_routes") or []
    if pref and bus["route"] not in pref:
        warnings.append(f"No Route {'/'.join(pref)} works for this train — using Route {bus['route']} instead.")
    if not bus["fits"]:
        (reasoning if bus["route"] in pref else warnings).append(
            f"The bus arrives {bus['wait_min']} min before the train — longer than your "
            f"{rules['max_platform_wait_min']} min limit" + (f", but Route {bus['route']} is your preferred route."
                                                             if bus["route"] in pref else "."))
    reasoning.append(f"Bus: Route {bus['route']} ({bus['headsign'].title()}) from {bus['board_name']} "
                     f"(stop #{bus['board_code']}) at {hm(bus['dep'])}, reaches {bus['alight_name']} at "
                     f"{hm(bus['arr'])} → {bus['wait_min']} min before the train.")
    reasoning.append(f"Walk to that stop: " + (f"{bus['walk_m']} m, " if bus["walk_m"] else "")
                     + f"about {bus['walk_min']} min ({bus['walk_source']}), "
                     f"plus {rules['stop_buffer_min']} min early at the stop → leave by {hm(bus['leave'])}.")

    w = rules["wake"]
    wake = bus["leave"] - w["prep_min"] * 60
    if w["mode"] == "start_of_previous_hour":
        wake = wake // 3600 * 3600
        reasoning.append(f"Wake {w['prep_min']} min before leaving, rounded down to the hour → {hm(wake)}.")
    else:
        reasoning.append(f"Wake {w['prep_min']} min before leaving → {hm(wake)}.")

    # Everything else that was looked at, so the choice can be checked.
    opts, _, _ = bus_options(index, cfg, key, train["dep"], dead_buses)
    considered_buses = [
        {**o, "status": "chosen" if o["trip_id"] == bus["trip_id"] else
            ("fits" if o["fits"] else ("too late" if o["too_late"] else "too early"))}
        for o in _per_trip(opts) if train["dep"] - 3600 <= o["arr"] <= train["dep"] + 900
    ]
    earlier = [o for o in considered_buses if o["status"] in ("fits", "too early") and o["arr"] < bus["arr"]]
    backup = max([o for o in earlier if o["route"] in pref] or earlier, key=lambda o: o["arr"], default=None)
    considered_trains = [
        {"number": t["number"], "line": t["line"], "dep": t["dep"], "arr": t["arr"],
         "status": "chosen" if t is train else ("cancelled" if t["number"] in cancelled else
                                                ("too late" if t["arr"] > latest_arr else "earlier"))}
        for t in sorted(reach, key=lambda t: t["dep"]) if abs(t["dep"] - train["dep"]) <= 3 * 3600
    ]

    live_bus = _bus_live(bus, bt_live, date)
    if live_bus and live_bus.get("note"):
        (warnings if live_bus.get("late") else reasoning).append(live_bus["note"])

    train_live = go_live.get("running", {}).get(train["number"])
    if train_live and train_live["delay_min"] > 0:
        warnings.append(f"Live: GO #{train['number']} is running {train_live['delay_min']} min late"
                        + (f", now leaving {train_live['expected']}" if train_live["expected"] else "")
                        + " — the plan still uses the scheduled time so you don't miss it if it catches up.")
    return {
        "date": date.isoformat(),
        "weekday": date.strftime("%A"),
        "arrive_by": hm(arrive_by),
        "summary": {
            "wake": hm(wake), "leave": hm(bus["leave"]),
            "bus_route": bus["route"], "bus_depart": hm(bus["dep"]), "bus_arrive": hm(bus["arr"]),
            "train_number": train["number"], "train_depart": hm(train["dep"]), "train_arrive": hm(train["arr"]),
        },
        "times": {k: tz.at(date, v).isoformat() for k, v in (
            ("wake", wake), ("leave", bus["leave"]), ("bus_depart", bus["dep"]), ("bus_arrive", bus["arr"]),
            ("train_depart", train["dep"]), ("train_arrive", train["arr"]))},
        "legs": [
            {"type": "walk", "from": cfg["home"]["label"], "to": bus["board_name"], "depart": hm(bus["leave"]),
             "minutes": bus["walk_min"], "metres": bus["walk_m"], "source": bus["walk_source"],
             "buffer_min": rules["stop_buffer_min"]},
            {"type": "bus", "route": bus["route"], "headsign": bus["headsign"], "trip_id": bus["trip_id"],
             "from": bus["board_name"], "stop_code": bus["board_code"], "depart": hm(bus["dep"]),
             "to": bus["alight_name"], "arrive": hm(bus["arr"]), "live": live_bus},
            {"type": "wait", "at": home_st, "minutes": bus["wait_min"]},
            {"type": train["mode"], "line": train["line"], "line_name": train["line_name"], "number": train["number"],
             "headsign": train["headsign"], "trip_id": train["trip_id"], "from": train["stops"][0][1],
             "depart": hm(train["dep"]), "to": train["stops"][-1][1], "arrive": hm(train["arr"]),
             "stops": [{"name": s[1], "time": hm(s[2])} for s in train["stops"]],
             "live": train_live},
        ],
        "backup_bus": backup and {k: backup[k] for k in ("route", "board_name", "board_code", "walk_min")}
        | {"depart": hm(backup["dep"]), "arrive": hm(backup["arr"]), "leave": hm(backup["leave"])},
        "reasoning": reasoning,
        "warnings": warnings,
        "considered": {
            "trains": [{**t, "dep": hm(t["dep"]), "arr": hm(t["arr"])} for t in considered_trains],
            "buses": [{"route": o["route"], "board": o["board_name"], "dep": hm(o["dep"]), "arr": hm(o["arr"]),
                       "leave": hm(o["leave"]), "wait_min": o["wait_min"], "status": o["status"]}
                      for o in considered_buses],
        },
        "sources": {k: {"name": index[k]["name"], "url": index[k]["url"], "version": index[k]["feed"]["version"],
                        "valid": f"{index[k]['feed']['start_date']}–{index[k]['feed']['end_date']}",
                        "downloaded_at": index[k]["downloaded_at"]} for k in ("go", "bt")},
    }


def return_trips(index, date):
    """Every GO train (and GO bus) from the destination back to the home station that day."""
    sids = set(index["go"]["calendar"].get(date.strftime("%Y%m%d"), []))
    return [{"mode": t["mode"], "line": t["line"], "number": t["number"], "dep": hm(t["dep"]), "arr": hm(t["arr"])}
            for t in sorted((t for t in index["go"].get("back", []) if t["service_id"] in sids), key=lambda t: t["dep"])]


def _bus_live(bus, bt_live, date):
    if date != tz.now().date():
        return None
    trip = bt_live.get("trips", {}).get(bus["trip_id"])
    if not trip:
        return {"status": "no live data yet", "note": None}
    if trip["cancelled"]:
        return {"status": "cancelled", "late": True, "note": f"Live: the {hm(bus['dep'])} Route {bus['route']} is cancelled."}
    s = trip["stops"].get(bus["board_stop"]) or {}
    t = s.get("dep") or s.get("arr")
    if not t:
        return {"status": "live, stop not in prediction", "note": None}
    predicted = tz.from_epoch(t)
    sched = tz.at(date, bus["dep"])
    delay = round((predicted - sched).total_seconds() / 60)
    status = "on time" if abs(delay) <= 1 else (f"{delay} min late" if delay > 0 else f"{-delay} min early")
    note = f"Live: Route {bus['route']} predicted at your stop {predicted.strftime('%H:%M')} ({status})."
    return {"status": status, "predicted": predicted.strftime("%H:%M"), "delay_min": delay,
            "late": delay > 3, "note": note if abs(delay) > 1 else None}
