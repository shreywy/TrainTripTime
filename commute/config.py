"""Settings: config.example.json holds generic defaults (checked in); config.json
holds one person's setup (gitignored) and is layered on top. TTT_CONFIG / TTT_DATA
env vars point a second instance (e.g. a demo) at different files."""
import copy
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE_PATH = os.path.join(ROOT, "config.example.json")
CONFIG_PATH = os.environ.get("TTT_CONFIG") or os.path.join(ROOT, "config.json")


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load():
    with open(EXAMPLE_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user = json.load(f)
        if "brampton" in user and "bus" not in user:  # older configs
            user["bus"] = user.pop("brampton")
        cfg = _merge(cfg, user)
    return cfg


def configured(cfg):
    return bool(cfg["home"].get("lat") is not None and cfg["go"].get("from_station") and cfg["go"].get("to_station"))


def save(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)
