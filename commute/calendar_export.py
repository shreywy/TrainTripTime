"""Calendar hand-off without any Google sign-in.

The event starts at the time to leave home. A Google "add event" link can't carry
reminders — Google applies the calendar's default notification — so the .ics
(which can) carries `leave_alarms_min` for calendars that import it.
"""
import datetime as dt
import urllib.parse

from . import tz


def _utc(iso):
    return tz.to_utc(dt.datetime.fromisoformat(iso)).strftime("%Y%m%dT%H%M%SZ")


def _esc(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line):
    out, b = [], line.encode("utf-8")
    while len(b) > 74:
        cut = 74
        while (b[cut] & 0xC0) == 0x80:  # don't split a UTF-8 sequence
            cut -= 1
        out.append(b[:cut].decode("utf-8"))
        b = b" " + b[cut:]
    out.append(b.decode("utf-8"))
    return "\r\n".join(out)


def _12h(hhmm):
    h, m = map(int, hhmm.split(":"))
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def title(p):
    s = p["summary"]
    return f"Leave home {_12h(s['leave'])} · Route {s['bus_route']} {_12h(s['bus_depart'])} · GO {_12h(s['train_depart'])}"


def details(p):
    s, legs = p["summary"], p["legs"]
    bus, train = legs[1], legs[3]
    lines = [
        f"Leave home {_12h(s['leave'])} (wake {_12h(s['wake'])})",
        f"Walk {legs[0]['minutes']} min to {bus['from']} (stop #{bus['stop_code']})",
        f"Route {bus['route']} {_12h(bus['depart'])} → {bus['to']} {_12h(bus['arrive'])}",
        f"GO {train['line']} #{train['number']} {_12h(train['depart'])} → {train['to']} {_12h(train['arrive'])}",
    ]
    if p.get("backup_bus"):
        b = p["backup_bus"]
        lines.append(f"Backup: Route {b['route']} at {_12h(b['depart'])} (leave {_12h(b['leave'])})")
    lines += [f"⚠ {w}" for w in p.get("warnings", [])]
    return "\n".join(lines)


def ics(p, cfg):
    t = p["times"]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    desc = details(p)
    events = [(f"ttt-{p['date']}-leave@traintriptime", title(p), t["leave"], t["train_arrive"],
               cfg["calendar"]["leave_alarms_min"])]
    if cfg["calendar"].get("include_wake_event"):
        events.append((f"ttt-{p['date']}-wake@traintriptime", f"Wake up (leave at {_12h(p['summary']['leave'])})",
                       t["wake"], t["wake"], [0]))
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//TrainTripTime//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    for uid, name, start, end, alarms in events:
        out += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}", f"DTSTART:{_utc(start)}", f"DTEND:{_utc(end)}",
                f"SUMMARY:{_esc(name)}", f"DESCRIPTION:{_esc(desc)}", f"LOCATION:{_esc(cfg['home']['label'])}"]
        for m in alarms:
            out += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_esc(name)}", f"TRIGGER:-PT{int(m)}M", "END:VALARM"]
        out.append("END:VEVENT")
    out.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in out) + "\r\n"


def google_link(p, cfg):
    t = p["times"]
    q = {
        "action": "TEMPLATE",
        "text": title(p),
        "dates": f"{_utc(t['leave'])}/{_utc(t['train_arrive'])}",  # starts at leave-home time
        "details": details(p),
        "location": cfg["home"]["label"],
    }
    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(q)
