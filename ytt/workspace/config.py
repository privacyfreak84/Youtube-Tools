"""workspace.toml: facts about this machine and workspace. Read with tomllib, written by a tiny writer
(the file is flat on purpose: scalars, lists of strings, and one [make] table)."""
import json
import os
import tomllib

from ytt.workspace.errors import WorkspaceError

DEFAULTS = {
    "clips_dir": "",                  # "" = <workspace>/clips
    "compilations_dir": "",           # "" = <workspace>/compilations
    "workers": 3,                     # downloads at the same time
    "cookies_from_browser": "",
    "cookies_file": "",
    "delete_used_clips": False,       # delete clips once they are safely inside a compilation
    "watch_folders": [],              # other folders to look in for videos you already have
    "default_style": "default",       # the NAME of a style; the style itself lives in the database
    "make": {                         # defaults for `make` requests (not rendering: that is the style)
        "size_mode": "count",         # count = N clips per compilation, minutes = aim for N minutes
        "clips_each": 15,
        "minutes_each": 10,
        "order": "name",              # name | oldest | newest | random
        "prefix": "compilation",
    },
}


def _merge(defaults, loaded):
    out = {}
    for key, value in defaults.items():
        if isinstance(value, dict):
            out[key] = _merge(value, loaded.get(key, {}) if isinstance(loaded.get(key), dict) else {})
        else:
            out[key] = loaded.get(key, value)
    for key, value in loaded.items():              # keep unknown keys: a newer ytt may have written them
        out.setdefault(key, value)
    return out


def load(path):
    try:
        with open(path, "rb") as f:
            loaded = tomllib.load(f)
    except FileNotFoundError:
        loaded = {}
    except tomllib.TOMLDecodeError as e:
        raise WorkspaceError(f"{path} is not valid TOML ({e}). Fix or delete it; defaults are used when it is missing.")
    return _merge(DEFAULTS, loaded)


def _value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)        # JSON string escapes are valid TOML basic strings
    if isinstance(v, list):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    raise TypeError(f"cannot write {type(v).__name__} to workspace.toml")


def dumps(cfg):
    lines = ["# ytt workspace settings. Edit with `ytt workspace set KEY VALUE` or by hand.", ""]
    tables = []
    for key, value in cfg.items():
        if isinstance(value, dict):
            tables.append((key, value))
        elif value is not None:
            lines.append(f"{key} = {_value(value)}")
    for name, table in tables:
        lines += ["", f"[{name}]"]
        lines += [f"{k} = {_value(v)}" for k, v in table.items() if v is not None]
    return "\n".join(lines) + "\n"


def save(path, cfg):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(dumps(cfg))
    os.replace(tmp, path)                  # atomic: a crash cannot leave a half-written settings file
