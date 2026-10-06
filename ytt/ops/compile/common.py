"""Small helpers the compile operations share (kept here so they exist once)."""
import re
from pathlib import Path

from ytt.workspace import store


def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def mmss(seconds):
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def nearest_existing(path):
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def next_names(ws, count):
    """The next `count` compilation names (compilation_007, ...): above every number used in the database or
    already on disk in the compilations folder, so nothing is ever overwritten by accident."""
    prefix = ws.config["make"]["prefix"]
    pattern = re.compile(rf"^{re.escape(prefix)}_(\d+)$")
    used = [int(m.group(1)) for n in store.compilation_names(ws.conn) if (m := pattern.match(n))]
    folder = ws.compilations_dir
    if folder.is_dir():
        used += [int(m.group(1)) for p in folder.iterdir() if (m := pattern.match(p.stem))]
    start = max(used, default=0) + 1
    return [f"{prefix}_{n:03d}" for n in range(start, start + count)]
