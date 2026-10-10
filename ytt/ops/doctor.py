"""`ytt doctor`: is everything ytt needs there, and does it work? (DESIGN.md section 17.)
It only looks; it never creates or changes anything. Everything outside the program (programs, network, disk,
the date) comes in through a Probes object, so the tests can hand in a fake machine."""
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ytt.ops.compile import style as style_mod
from ytt.sources import ytdlp
from ytt.workspace import config as cfgmod
from ytt.workspace import db, paths
from ytt.workspace.errors import WorkspaceError

OK, WARN, FAIL = "ok", "warn", "fail"
MIN_PYTHON = (3, 11)
YTDLP_OLD_DAYS = 60                  # YouTube changes often; an old yt-dlp is the usual reason downloads stop working
LOW_DISK = 5 * 10 ** 9
NEEDED_ENCODERS = ("libx264", "aac")
YOUTUBE_URL = "https://www.youtube.com/robots.txt"
INTERNET_URL = "https://pypi.org/simple/"


@dataclass
class Check:
    name: str
    status: str                      # ok | warn | fail
    detail: str = ""
    fix: str = ""


class Probes:
    """The real machine. A test replaces this with a fake that has the same methods."""

    def python_version(self):
        return tuple(sys.version_info[:3])

    def which(self, program):
        return shutil.which(program)

    def run(self, command, timeout=20):
        """(exit code, output) of a program, or (None, reason) when it could not be run at all."""
        try:
            res = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            return None, str(e)
        return res.returncode, res.stdout + res.stderr

    def ytdlp_version(self):
        return ytdlp.yt_dlp_version()

    def today(self):
        return date.today()

    def disk_free(self, path):
        return shutil.disk_usage(path).free

    def reach(self, url, timeout=10):
        """(True, '') when the address answers, else (False, why)."""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ytt-doctor"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read(1)
            return True, ""
        except Exception as e:                        # any failure means "not reachable"; the reason is shown
            return False, str(getattr(e, "reason", None) or e)[:120]


# ---------------------------------------------------------------- the programs
def check_python(probes):
    v = probes.python_version()
    text = ".".join(str(x) for x in v)
    if tuple(v[:2]) < MIN_PYTHON:
        return [Check("Python", FAIL, text, f"ytt needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer")]
    return [Check("Python", OK, text)]


def _encoders(text):
    return set(re.findall(r"^\s*[VASFXBD.]{6}\s+(\S+)", text, re.M))


def check_ffmpeg(probes):
    out = []
    ffprobe_ok = False
    for program in ("ffmpeg", "ffprobe"):
        if not probes.which(program):
            out.append(Check(program, FAIL, "not found",
                             "install ffmpeg (it brings ffprobe too). On Fedora: sudo dnf install ffmpeg "
                             "(needs the RPM Fusion repository)"))
            continue
        code, text = probes.run([program, "-version"])
        if code != 0:
            out.append(Check(program, FAIL, "found but would not run", f"reinstall ffmpeg ({text.strip()[:80]})"))
            continue
        m = re.search(r"version\s+(\S+)", text)
        out.append(Check(program, OK, m.group(1) if m else "installed"))
        if program == "ffprobe":
            ffprobe_ok = True
    if any(c.name == "ffmpeg" and c.status == OK for c in out):
        code, text = probes.run(["ffmpeg", "-hide_banner", "-encoders"])
        have = _encoders(text) if code == 0 else set()
        missing = [e for e in NEEDED_ENCODERS if e not in have]
        if missing:
            out.append(Check("ffmpeg encoders", FAIL, f"missing {', '.join(missing)}",
                             "rendering needs the H.264 (libx264) and AAC encoders. On Fedora the stock "
                             "ffmpeg-free has no libx264; RPM Fusion's ffmpeg does"))
        else:
            out.append(Check("ffmpeg encoders", OK, ", ".join(NEEDED_ENCODERS)))
    return out


def _ytdlp_date(version):
    m = re.match(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", version or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def check_ytdlp(probes):
    version = probes.ytdlp_version()
    if not version:
        return [Check("yt-dlp", FAIL, "not installed", "pip install yt-dlp")]
    built = _ytdlp_date(version)
    if built is None:
        return [Check("yt-dlp", OK, version)]
    age = (probes.today() - built).days
    if age > YTDLP_OLD_DAYS:
        return [Check("yt-dlp", WARN, f"{version} ({age} days old)",
                      "YouTube changes often and an old yt-dlp is the usual reason downloads stop working: "
                      "pip install -U yt-dlp")]
    return [Check("yt-dlp", OK, f"{version} ({max(age, 0)} days old)")]


def check_screen_libraries():
    missing = []
    for name in ("rich", "questionary"):
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if missing:
        return [Check("menu libraries", WARN, f"missing {', '.join(missing)}",
                      f"the guided menu needs them; commands still work: pip install {' '.join(missing)}")]
    return [Check("menu libraries", OK, "rich, questionary")]


# ---------------------------------------------------------------- the workspace
def _writable(folder):
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".ytt_doctor_"):
            pass
        return True
    except OSError:
        return False


def check_workspace(root, probes):
    """Returns (checks, cfg or None). cfg is None when there is no readable workspace to look further into."""
    root = Path(root)
    if not root.exists() or not (root / paths.DB_NAME).exists():
        return [Check("Workspace", WARN, f"none at {root}",
                      "research works without one. To compile, create it with: ytt workspace init")], None
    out = []
    if not _writable(root):
        out.append(Check("Workspace", FAIL, f"{root} is not writable", "check the folder's permissions"))
    else:
        out.append(Check("Workspace", OK, str(root)))
    try:
        cfg = cfgmod.load(root / paths.CONFIG_NAME)
    except WorkspaceError as e:
        out.append(Check("Settings", FAIL, str(e), "fix or delete workspace.toml (defaults are used when it is missing)"))
        return out, None
    out.append(Check("Settings", OK, paths.CONFIG_NAME))
    for label, key, default in (("Clips folder", "clips_dir", "clips"), ("Compilations folder", "compilations_dir", "compilations")):
        folder = Path(cfg[key]) if cfg[key] else root / default
        if not folder.is_dir():
            out.append(Check(label, WARN, f"{folder} does not exist", "create it, or change the setting with: ytt workspace set"))
        elif not _writable(folder):
            out.append(Check(label, FAIL, f"{folder} is not writable", "check the folder's permissions"))
        else:
            free = probes.disk_free(folder)
            gb = f"{free / 1e9:.1f} GB free"
            out.append(Check(label, WARN if free < LOW_DISK else OK, f"{folder} ({gb})",
                             "rendering needs room; free some space" if free < LOW_DISK else ""))
    if cfg["cookies_file"] and not Path(cfg["cookies_file"]).expanduser().is_file():
        out.append(Check("Cookies file", FAIL, f"{cfg['cookies_file']} does not exist",
                         "fix it with: ytt workspace set cookies_file PATH"))
    for folder in cfg["watch_folders"]:
        if not Path(folder).expanduser().is_dir():
            out.append(Check("Watch folder", WARN, f"{folder} does not exist", "remove it or fix it with: ytt workspace set"))
    return out, cfg


def _open_read_only(root):
    conn = sqlite3.connect(f"file:{Path(root) / paths.DB_NAME}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def check_database(root):
    """(checks, connection or None). The connection is read-only and the caller closes it."""
    try:
        conn = _open_read_only(root)
        found = db.version(conn)
        verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.Error as e:
        return [Check("Database", FAIL, f"cannot be read ({e})", "restore ytt.db from a backup, or move it away and run ytt workspace init")], None
    if verdict != "ok":
        return [Check("Database", FAIL, f"damaged ({verdict[:80]})", "restore ytt.db from a backup")], None
    if found > db.LATEST:
        return [Check("Database", FAIL, f"version {found}, made by a newer ytt (this one knows {db.LATEST})",
                      "update ytt")], None
    if found < db.LATEST:
        return [Check("Database", WARN, f"version {found}; this ytt uses {db.LATEST}",
                      "it is brought up to date the next time a command opens the workspace")], (conn if found >= 1 else None)
    return [Check("Database", OK, f"version {found}, no damage found")], conn


def check_styles(conn, cfg):
    out = []
    try:
        names = [r["name"] for r in conn.execute("SELECT name FROM styles ORDER BY name")]
    except sqlite3.Error:
        return [Check("Styles", FAIL, "cannot be read", "")]
    bad = []
    for name in names:
        row = conn.execute("SELECT data FROM styles WHERE name = ?", (name,)).fetchone()
        try:
            style = style_mod.Style.from_dict(json.loads(row["data"]))
        except (ValueError, TypeError) as e:
            bad.append(f"{name}: {e}")
            continue
        for key in ("intro", "outro"):
            value = getattr(style, key)
            if value and not Path(value).expanduser().is_file():
                bad.append(f"{name}: {key} file {value} does not exist")
        if style.stinger_dir and not Path(style.stinger_dir).expanduser().is_dir():
            bad.append(f"{name}: stinger folder {style.stinger_dir} does not exist")
    if bad:
        out.append(Check("Styles", FAIL, "; ".join(bad[:3]) + (f" (+{len(bad) - 3} more)" if len(bad) > 3 else ""),
                         "fix with: ytt style set NAME KEY VALUE"))
    else:
        out.append(Check("Styles", OK, f"{len(names)} saved" if names else "none yet (the default is created on first use)"))
    default = cfg["default_style"] if cfg else style_mod.DEFAULT_NAME
    if names and default not in names:
        out.append(Check("Default style", WARN, f"'{default}' is not a saved style", "pick one with: ytt workspace set default_style NAME"))
    return out


def _file(root, text):
    p = Path(text).expanduser()
    return p if p.is_absolute() else Path(root) / p


def check_library_files(root, conn):
    """Whether the files the records point to are there (also how a migrated workspace is verified)."""
    try:
        clips = conn.execute("SELECT path FROM clips WHERE status = 'ready'").fetchall()
        comps = conn.execute("SELECT name, output_path FROM compilations").fetchall()
    except sqlite3.Error:
        return []
    gone_clips = [_file(root, r["path"]) for r in clips if not _file(root, r["path"]).is_file()]
    gone_comps = [(r["name"], _file(root, r["output_path"])) for r in comps
                  if r["output_path"] and not _file(root, r["output_path"]).is_file()]
    out = []
    if gone_clips:
        out.append(Check("Clip files", WARN, f"{len(gone_clips)} of {len(clips)} ready clips have no file "
                                             f"(the first should be at {gone_clips[0]})",
                         "ytt remake downloads them again when it needs them; ytt library clips shows which"))
    else:
        out.append(Check("Clip files", OK, f"{len(clips)} ready clips, all present"))
    if gone_comps:
        names = [n for n, _ in gone_comps]
        shown = ", ".join(names[:3]) + (f" (+{len(names) - 3} more)" if len(names) > 3 else "")
        out.append(Check("Compilation files", WARN, f"{len(gone_comps)} of {len(comps)} have no file: {shown} "
                                                    f"(the first should be at {gone_comps[0][1]})",
                         "ytt remake makes them again; ytt library forget takes them out of the records"))
    else:
        out.append(Check("Compilation files", OK, f"{len(comps)} compilations, all present"))
    return out


# ---------------------------------------------------------------- the network
def check_network(probes):
    ok, why = probes.reach(INTERNET_URL)
    if not ok:
        return [Check("Internet", FAIL, f"not reachable ({why})", "check the connection"),
                Check("YouTube", WARN, "not tried (no internet)")]
    out = [Check("Internet", OK)]
    ok, why = probes.reach(YOUTUBE_URL)
    out.append(Check("YouTube", OK) if ok else
               Check("YouTube", FAIL, f"not reachable ({why})", "a firewall, VPN or DNS setting may be blocking youtube.com"))
    return out


# ---------------------------------------------------------------- all of it
def run_doctor(root, probes=None, offline=False):
    probes = probes or Probes()
    checks = check_python(probes) + check_ffmpeg(probes) + check_ytdlp(probes) + check_screen_libraries()
    ws_checks, cfg = check_workspace(root, probes)
    checks += ws_checks
    if cfg is not None:
        db_checks, conn = check_database(root)
        checks += db_checks
        if conn is not None:
            try:
                checks += check_styles(conn, cfg)
                checks += check_library_files(root, conn)
            finally:
                conn.close()
    if not offline:
        checks += check_network(probes)
    return checks


def summary(checks):
    """(problems, warnings) counts."""
    return (sum(1 for c in checks if c.status == FAIL), sum(1 for c in checks if c.status == WARN))
