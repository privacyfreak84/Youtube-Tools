"""One-time import of the old scripts' state into a workspace (DESIGN.md section 19).

It is a migration, not runtime compatibility: it reads the old JSON files, writes the new database, and reports.
Nothing in the old folders is changed or moved. Running it twice is safe: videos and clips are updated in place,
compilations already imported are skipped, and settings you changed in the new workspace are kept.
"""
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from ytt.ops.compile.style import DEFAULT_NAME, Style, StyleError
from ytt.workspace import config as cfgmod
from ytt.workspace import store
from ytt.workspace.errors import WorkspaceError

OLD_FILES = ("auto_settings.json", "fetch_archive.json", "compile_settings.json", "compile_ledger.json")
CACHE_FILE = "compile_cache.json"          # clip lengths only; rebuilt on demand, so not imported
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".wmv", ".mpg", ".mpeg", ".ts"}
ID = r"[A-Za-z0-9_-]{11}"
OWN_NAME = re.compile(rf"^\d{{8}}-\d{{4}}_\d{{4}}_({ID})$")      # YYYYMMDD-HHMM_0001_<id>: names the old tool gave
YTDLP_NAME = re.compile(rf"\[({ID})\]\s*$")                      # Title [id].mp4: yt-dlp's usual name


@dataclass
class ImportReport:
    legacy_dir: str
    dry_run: bool
    files_found: list = field(default_factory=list)
    added: dict = field(default_factory=dict)             # sources / videos / clips / compilations / styles
    clips_ready: int = 0
    clips_missing: int = 0
    clips_recovered: int = 0                              # not in the old archive; found by file name
    unidentified_files: int = 0                           # video files whose name holds no YouTube id
    compilations_already_present: int = 0
    compilations_with_unresolved_clips: int = 0
    unresolved_clips: int = 0
    unreadable_in_old_ledger: int = 0
    settings_applied: list = field(default_factory=list)
    settings_kept: list = field(default_factory=list)
    style_created: bool = False
    warnings: list = field(default_factory=list)


def extract_id(file_name):
    """The YouTube id inside a clip's file name, or None. Only the two shapes below are trusted, so an ordinary
    name that merely ends in 11 letters is never mistaken for an id."""
    name = Path(file_name).name
    if re.search(r"\.f\d+\.", name):
        return None                                        # per-stream leftover of an unfinished download
    stem = Path(name).stem
    for pattern in (OWN_NAME, YTDLP_NAME):
        m = pattern.search(stem)
        if m:
            return m.group(1)
    return None


def _load(path, report):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        report.warnings.append(f"{path.name} could not be read ({e.__class__.__name__}); skipped")
        return {}
    if not isinstance(data, dict):
        report.warnings.append(f"{path.name} does not look like the old format; skipped")
        return {}
    report.files_found.append(path.name)
    return data


def _abs(text, base):
    p = Path(os.path.expanduser(text))
    return p if p.is_absolute() else Path(base) / p


def _norm(path):
    return os.path.normpath(str(path))


def import_legacy(ws, legacy_dir, dry_run=False):
    legacy_dir = Path(legacy_dir)
    if not legacy_dir.is_dir():
        raise WorkspaceError(f"{legacy_dir} is not a folder. Point this at the folder the old scripts ran in.")
    report = ImportReport(legacy_dir=str(legacy_dir), dry_run=dry_run)
    auto = _load(legacy_dir / "auto_settings.json", report)
    archive = _load(legacy_dir / "fetch_archive.json", report)
    comp = _load(legacy_dir / "compile_settings.json", report)
    ledger = _load(legacy_dir / "compile_ledger.json", report)
    if not report.files_found:
        raise WorkspaceError(f"No old ytt state found in {legacy_dir} (looked for {', '.join(OLD_FILES)}).")

    dest = _abs(auto.get("dest") or "fetched", legacy_dir)
    clips_dir = _abs(comp.get("clips_dir") or str(dest), legacy_dir)
    output_dir = _abs(comp.get("output_dir") or "compilations", legacy_dir)

    before = ws.counts()
    with ws.transaction(dry_run=dry_run):
        conn = ws.conn
        path_to_id = {}

        def ensure_clip(vid, path):
            """Record the file for a video; keeps a working clip rather than replacing it with a missing one."""
            exists = Path(path).is_file()
            existing = store.get_clip(conn, vid)
            if existing and existing["status"] == "ready" and not exists:
                clip_id = existing["id"]
            else:
                clip_id, _ = store.upsert_clip(conn, vid, ws.to_stored(path), "ready" if exists else "missing")
                if exists:
                    report.clips_ready += 1
                else:
                    report.clips_missing += 1
            path_to_id[_norm(path)] = vid
            return clip_id

        # ---- the old download archive: every video id the old tool ever fetched
        for vid, entry in archive.items():
            if not isinstance(entry, dict) or not vid:
                continue
            channel = (entry.get("channel") or "").strip()
            source_id = store.add_source(conn, channel) if channel else None
            store.upsert_video(conn, vid, title=entry.get("title"), views=entry.get("views"),
                               duration=entry.get("duration"), source_id=source_id)
            if entry.get("file"):
                ensure_clip(vid, _abs(entry["file"], dest))
        if auto.get("channel"):
            store.add_source(conn, auto["channel"].strip())

        # ---- clips on disk that the archive does not know (downloaded by hand, or lost from the list)
        folders = []
        for folder in (clips_dir, dest):
            if folder.is_dir() and _norm(folder) not in [_norm(f) for f in folders]:
                folders.append(folder)
        skip = _norm(output_dir)
        for folder in folders:
            for p in sorted(folder.rglob("*")):
                if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS or p.name.startswith(".") \
                        or ".part." in p.name or _norm(p).startswith(skip + os.sep) or _norm(p) in path_to_id:
                    continue
                vid = extract_id(p.name)
                if vid is None:
                    report.unidentified_files += 1
                    continue
                if store.get_clip(conn, vid):
                    continue                                     # the archive already gave this video a file
                store.upsert_video(conn, vid)
                ensure_clip(vid, p)
                report.clips_recovered += 1

        # ---- finished compilations
        report.unreadable_in_old_ledger = len(ledger.get("skipped") or {})
        for name, entry in (ledger.get("compilations") or {}).items():
            if store.get_compilation(conn, name):
                report.compilations_already_present += 1
                continue
            resolved, unresolved = [], 0
            for key in entry.get("clips") or []:
                path = clips_dir / key
                vid = path_to_id.get(_norm(path)) or extract_id(key)
                if vid is None:
                    resolved.append((None, key))
                    unresolved += 1
                    continue
                clip = store.get_clip(conn, vid)
                if clip is None:
                    store.upsert_video(conn, vid)
                    clip_id = ensure_clip(vid, path)
                    report.clips_recovered += 1
                else:
                    clip_id = clip["id"]
                resolved.append((clip_id, None))
            if unresolved:
                report.compilations_with_unresolved_clips += 1
                report.unresolved_clips += unresolved
            out = entry.get("file")
            store.add_compilation(conn, name, clips=resolved, imported=True, made=entry.get("made"),
                                  output_path=ws.to_stored(_abs(out, legacy_dir)) if out else None)

        # ---- the look: old compilation setup becomes the style named "default"
        style_data = {k: comp[k] for k in ("transition", "transition_seconds", "stinger_dir", "stinger_key",
                                           "stinger_sim", "stinger_blend", "stinger_despill", "stinger_audio",
                                           "intro", "outro", "quality") if k in comp}
        if "max_height" in auto:
            style_data["max_height"] = auto["max_height"]
        if comp or "max_height" in auto:
            if store.get_style(conn, DEFAULT_NAME) is not None:
                report.settings_kept.append("style 'default'")
            else:
                try:
                    style = Style.from_dict(style_data)
                except StyleError as e:
                    report.warnings.append(f"the old look could not be imported as a style ({e}); defaults used")
                    style = Style()
                store.save_style(conn, DEFAULT_NAME, style.to_dict())
                report.style_created = True

        after = ws.counts()                    # inside the transaction: a dry run rolls back right after this
        report.added = {k: after[k] - before[k] for k in ("sources", "videos", "clips", "compilations", "styles")}

    # ---- workspace.toml: only settings still at their built-in value are changed
    wanted = {}
    if auto or comp:
        wanted["clips_dir"] = str(clips_dir)
        wanted["compilations_dir"] = str(output_dir)
    if "delete_after" in auto:
        wanted["delete_used_clips"] = bool(auto["delete_after"])
    if auto.get("check_folders"):
        wanted["watch_folders"] = [str(_abs(f, legacy_dir)) for f in auto["check_folders"]]
    make = {}
    for old, new in (("size_mode", "size_mode"), ("clips_per_video", "clips_each"),
                     ("minutes_per_video", "minutes_each"), ("order", "order"), ("prefix", "prefix")):
        if old in comp:
            make[new] = comp[old]
    for key, value in wanted.items():
        if ws.config.get(key) == cfgmod.DEFAULTS[key]:
            if value != ws.config.get(key):
                ws.config[key] = value
                report.settings_applied.append(key)
        elif ws.config.get(key) != value:
            report.settings_kept.append(key)
    for key, value in make.items():
        if ws.config["make"].get(key) == cfgmod.DEFAULTS["make"][key]:
            if value != ws.config["make"][key]:
                ws.config["make"][key] = value
                report.settings_applied.append(f"make.{key}")
        elif ws.config["make"].get(key) != value:
            report.settings_kept.append(f"make.{key}")
    if dry_run:
        ws.config = cfgmod.load(ws.root / "workspace.toml")      # leave the in-memory copy as it was too
    elif report.settings_applied:
        ws.save_config()
    return report
