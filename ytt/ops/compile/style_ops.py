"""Styles as a person manages them: list, show, change (`set` and the guided `edit`) and delete (DESIGN.md section 9).

A style is flat (no inheritance) and lives in the database; a compilation keeps its own snapshot, so changing or
deleting a style never changes a compilation that was already made. Changes go through the same pipeline as the other
commands: parse, validate, plan (SavePlan), then execute. This module never prints or prompts.
"""
import json
import re
from dataclasses import dataclass, field, fields
from pathlib import Path

from ytt.engine import stitch
from ytt.ops.compile.style import Style, StyleError
from ytt.ops.errors import OpError
from ytt.ops.plan import Action, Plan
from ytt.workspace import store

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
FILE_SETTINGS = ("intro", "outro", "stinger_dir")
HELP = {
    "transition": "how clips are joined: fade, cut, random, wipeleft, ... or stinger (needs stinger_dir)",
    "transition_seconds": "how long each transition lasts, in seconds",
    "transition_mode": "hold: every clip plays in full (default); overlap: transitions hide the clip ends",
    "stinger_dir": "folder of transition videos (for transition stinger)",
    "stinger_key": "stinger background to remove: auto, none, green, blue, black, white or a colour like 00FF00",
    "stinger_sim": "how close to the key colour counts as background (0-1)",
    "stinger_blend": "edge softness of the key (0-1)",
    "stinger_despill": "remove colour spill from the key colour: yes or no",
    "stinger_audio": "play the stinger's sound: yes or no",
    "intro": "a video put before every compilation (empty for none)",
    "outro": "a video put after every compilation (empty for none)",
    "quality": "fast, balanced or best (slower renders, better pictures)",
    "max_height": "the tallest picture to download, in pixels",
}
_TRUE, _FALSE = {"yes", "true", "on", "1", "y"}, {"no", "false", "off", "0", "n"}


@dataclass
class SavePlan:
    plan: Plan
    name: str
    before: dict = None              # None for a new style
    after: dict = None
    changed: bool = False


def keys():
    return [f.name for f in fields(Style)]


def normalize_key(key):
    k = key.strip().lower().replace("-", "_")
    if k not in keys():
        raise OpError(f"There is no style setting '{key}'. The settings are: {', '.join(keys())}.")
    return k


def _shown(value):
    if value == "":
        return "(none)"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def parse_value(key, text):
    """Text typed by a person -> the value of that style setting. Raises OpError saying what is allowed."""
    value = _convert(key, text)
    try:
        Style.from_dict({key: value})                       # the style's own rules (allowed words, ranges)
    except StyleError as e:
        raise OpError(str(e))
    return value


def _convert(key, text):
    default = getattr(Style(), key)
    text = text.strip()
    try:
        if isinstance(default, bool):
            if text.lower() not in _TRUE | _FALSE:
                raise ValueError
            return text.lower() in _TRUE
        if isinstance(default, int):
            return int(text)
        if isinstance(default, float):
            return float(text)
    except ValueError:
        kind = "yes or no" if isinstance(default, bool) else "a whole number" if isinstance(default, int) else "a number"
        raise OpError(f"'{key}' needs {kind} (got {text!r}).")
    if key == "transition" and not text.startswith("@") \
            and text.partition(":")[0].strip().lower() not in stitch.CHOICES | {"stinger"}:
        raise OpError(f"'{text}' is not a transition. Use a name like fade, wipeleft or cut, random, or stinger "
                      f"(`ytt style show default` lists the settings).")
    if key in FILE_SETTINGS and text:
        text = str(Path(text).expanduser().absolute())          # relative paths would depend on where ytt is run
    return text


def _name_ok(name):
    if not NAME_RE.match(name or ""):
        raise OpError(f"'{name}' is not a good style name. Use letters, digits, '-', '_' or '.', starting with a letter or digit.")


def load(ws, name):
    """-> Style. Raises OpError when there is no such style or it cannot be read."""
    data = store.get_style(ws.conn, name)
    if data is None:
        names = ", ".join(store.list_styles(ws.conn)) or "none yet"
        raise OpError(f"There is no style called '{name}'. Saved styles: {names}.")
    try:
        return Style.from_dict(data)
    except StyleError as e:
        raise OpError(f"Style '{name}' is not valid: {e}")


def list_styles(ws):
    """-> [(name, is_default, Style or None, problem or '')], by name."""
    out = []
    default = ws.config["default_style"]
    for name in sorted(store.list_styles(ws.conn)):
        try:
            out.append((name, name == default, Style.from_dict(store.get_style(ws.conn, name)), ""))
        except StyleError as e:
            out.append((name, name == default, None, str(e)))
    return out


def plan_save(ws, name, changes, new=False, source=None):
    """Change a style (or create one with new=True, starting from the built-in look, or source=NAME, starting from
    a copy of another). changes: {setting: text as typed}. Raises OpError for a request that makes no sense."""
    _name_ok(name)
    if new and source:
        raise OpError("Use either --new or --from, not both.")
    existing = store.get_style(ws.conn, name)
    if existing is None:
        if not (new or source):
            raise OpError(f"There is no style called '{name}'. Create it with --new (starts from the built-in look) "
                          f"or --from NAME (starts from a copy of that style), or with `ytt style edit {name}`.")
        base = load(ws, source).to_dict() if source else Style().to_dict()
        before = None
    else:
        if new or source:
            raise OpError(f"A style called '{name}' already exists. Leave out --new/--from to change it.")
        before = base = load(ws, name).to_dict()
    values = {}
    for k, v in changes.items():
        values[normalize_key(k)] = parse_value(normalize_key(k), v)
    try:
        after = Style.from_dict({**base, **values}).to_dict()
    except StyleError as e:
        raise OpError(str(e))

    verb = "Create" if before is None else "Change"
    plan = Plan(title=f"{verb} style '{name}'" + (f" (a copy of '{source}')" if source else ""))
    sp = SavePlan(plan, name, before, after)
    for k in keys():
        if after[k] != base[k]:
            plan.actions.append(Action("style", f"{k}: {_shown(base[k])} → {_shown(after[k])}", {"key": k}))
    sp.changed = before is None or after != before
    if before is None and not plan.actions:
        plan.actions.append(Action("style", f"start from {'a copy of ' + repr(source) if source else 'the built-in look'}"))
    if not sp.changed:
        plan.notes.append("Nothing changes: those are the values the style already has.")
    for k in FILE_SETTINGS:
        if after[k] and after[k] != base[k] and not Path(after[k]).exists():
            plan.warnings.append(f"{k} {after[k]} does not exist yet; it is only needed when a compilation is made.")
    if name == ws.config["default_style"]:
        plan.notes.append("This is the workspace's default style: every make and remake that does not name another uses it.")
    plan.notes.append("Compilations already made keep their own copy of the style; only new ones are affected.")
    return sp


def run_save(ws, sp):
    if not sp.plan.ok:
        raise OpError("The plan has errors; nothing was done.")
    store.save_style(ws.conn, sp.name, sp.after)
    ws.conn.commit()


def plan_delete(ws, name):
    """-> a Plan. Deleting the workspace's default style is an error: make and remake could not find it."""
    load_exists = store.get_style(ws.conn, name)
    if load_exists is None:
        names = ", ".join(store.list_styles(ws.conn)) or "none yet"
        raise OpError(f"There is no style called '{name}'. Saved styles: {names}.")
    plan = Plan(title=f"Delete style '{name}'")
    if name == ws.config["default_style"]:
        plan.errors.append(f"'{name}' is the workspace's default style. Pick another default first: "
                           f"ytt workspace set default_style NAME")
        return plan
    plan.actions.append(Action("delete", f"delete style '{name}'"))
    used = 0
    for row in store.list_compilations(ws.conn):
        try:
            if row["request"] and json.loads(row["request"]).get("style") == name:
                used += 1
        except ValueError:
            pass
    if used:
        plan.notes.append(f"{used} compilation{'s were' if used != 1 else ' was'} made with it; "
                          f"they keep their own copy, so nothing about them changes.")
    return plan


def run_delete(ws, name, plan):
    if not plan.ok:
        raise OpError("The plan has errors; nothing was done.")
    store.delete_style(ws.conn, name)
    ws.conn.commit()
