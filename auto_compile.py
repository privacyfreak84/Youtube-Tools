#!/usr/bin/env python3
"""
auto_compile.py - one command: pick videos from a YouTube channel, download them, make compilations.

Put this file in the same folder as yt_toolkit.py, make_compilations.py and stitch_videos.py.
Needs:  pip install yt-dlp tabulate     and ffmpeg on your PATH.

HOW TO USE
----------
  python auto_compile.py                   asks a few questions and then does everything. Your answers are
                                           remembered, so next time it is mostly pressing Enter.

  Or in one line, e.g.:
  python auto_compile.py @Channel --sort oldest --pick 60                 the 60 oldest videos
  python auto_compile.py @Channel --sort oldest --pick 25-70              the 25th to the 70th oldest
  python auto_compile.py @Channel --sort popular --pick last:20           the 20 LEAST viewed of the list
  python auto_compile.py @Channel --sort popular --pick 1-10,25,40-50     exactly those positions
  python auto_compile.py @Channel --sort latest --pick every:5            every 5th newest video
  python auto_compile.py @Channel --sort oldest --pick 25-70 --reverse    same, but played newest-to-oldest
  python auto_compile.py @Channel --from 2024-01-01 --to 2024-06-30 --sort popular --pick new:60

HOW SELECTION WORKS
-------------------
  1. Take the channel's list and keep only what matches your dates / length / views limits.
  2. Put that list in the order you chose with --sort.
  3. Pick positions from that ordered list with --pick (position 1 = first in that order).
  4. Download the picked videos (skipping any you downloaded before) and compile them in that order.

  --sort   popular (most viewed) | unpopular | oldest | latest (newest) | longest | shortest | title | random
  --pick   60            the first 60                     last:20     the last 20
           25-70         positions 25 to 70               25-         position 25 to the end
           -30           up to position 30                1-100/10    every 10th within 1-100
           every:5       every 5th video                  random:30   30 random ones (kept in list order)
           1-10,25,40-   any mix of the above, separated by commas
           new:60        the next 60 you have NOT downloaded yet (skips ones you already have and
                         moves further down the list - handy for repeated runs)
  Positions always count within the list from step 1/2, so "25-70" means the same videos every time;
  ones you already downloaded are simply skipped.

OTHER OPTIONS
-------------
  --type videos|shorts   which tab of the channel (default: videos)
  --from / --to DATE     e.g. 2024-01-31  (also 20240131 or 31-01-2024). Leave out for no limit
  --min-views N          --min-minutes M   --max-minutes M     only videos within these limits
  --max-height 1080      download quality cap (default 1080)
  --reverse              compile in reverse: the last picked video first
  --reverse-each         same clips in each compilation, but each one played backwards
  --top N                same as --pick new:N
  --redownload           ignore the "already downloaded" list
  --delete-after         delete downloaded clips once they are safely inside a compilation
  --no-compile           only download, don't make compilations
  --dry-run              show which videos WOULD be downloaded, and stop
  --yes                  don't ask for confirmation
  --include-leftover     also make a final shorter compilation from leftover clips
  --cookies-from-browser firefox      (passed to yt-dlp, for age-restricted/members videos)

Only use this on videos you own or have the right to reuse.
"""

import argparse
import json
import math
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime, timezone
from types import SimpleNamespace
from pathlib import Path

HERE = Path(__file__).resolve().parent
SETTINGS_FILE = HERE / "auto_settings.json"
ARCHIVE_FILE = HERE / "fetch_archive.json"          # every video id downloaded so far
MAKE = HERE / "make_compilations.py"
COMPILE_SETTINGS = HERE / "compile_settings.json"
COMPILE_LEDGER = HERE / "compile_ledger.json"

DEFAULTS = {
    "channel": "", "type": "videos", "sort": "popular", "pick": "comps:3",
    "date_from": "", "date_to": "", "dest": str(HERE / "fetched"),
    "max_height": 1080, "min_minutes": 0, "max_minutes": 0, "min_views": 0,
    "play": "asis", "delete_after": False, "check_folders": [],
    "leftover": "keep",
}
SORTS = [("popular", "Most viewed first"), ("unpopular", "Least viewed first"),
         ("oldest", "Oldest first"), ("latest", "Newest first"),
         ("longest", "Longest first"), ("shortest", "Shortest first"),
         ("title", "Title A to Z"), ("random", "Random")]
SORT_WORDS = {"popular": "most viewed", "unpopular": "least viewed", "oldest": "oldest", "latest": "newest",
              "longest": "longest", "shortest": "shortest", "title": "title A-Z", "random": "random"}
PLAYS = [("asis", "As selected (1st picked video first)"),
         ("reverse", "Reversed (last picked video first)"),
         ("each", "Each compilation played backwards (same clips in each)")]
PICKS = [("comps", "Enough new videos for N compilations (best for mass production)"),
         ("first", "The first N videos"),
         ("new", "The next N videos I haven't downloaded yet"),
         ("range", "A range: from position X to position Y"),
         ("last", "The last N videos"),
         ("every", "Every Nth video (e.g. every 5th)"),
         ("random", "A random N videos"),
         ("custom", "A custom list of positions, e.g. 1-10, 25, 40-50")]

sys.path.insert(0, str(HERE))
try:
    import yt_toolkit
except ImportError:
    sys.exit(f"Error: yt_toolkit.py must be in the same folder as this script ({HERE})")
try:
    import make_compilations as mc
except ImportError:
    sys.exit(f"Error: make_compilations.py must be in the same folder as this script ({HERE})")
import yt_dlp
from yt_toolkit import ChannelTable, build_cookies_opts


# --------------------------------------------------------------------------- dates

def parse_date(text):
    """2024-01-31, 20240131 or 31-01-2024 -> date. Raises ValueError."""
    t = text.strip().replace("/", "-").replace(".", "-")
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t) or re.fullmatch(r"(\d{4})(\d{2})(\d{2})", t)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})-(\d{4})", t)
    if m:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    raise ValueError(f"can't read the date '{text}' (use something like 2024-01-31)")


def ask_date(prompt, default=""):
    hint = "Enter = keep it, - = no limit" if default else "Enter = no limit"
    while True:
        ans = mc.ask_text(f"{prompt} ({hint})", default)
        if not ans or ans.lower() in ("none", "no", "-"):
            return ""
        try:
            return parse_date(ans).isoformat()
        except ValueError as e:
            print(f"   {e}")


# --------------------------------------------------------------------------- reading the channel

def list_tab(base_url, tab, cookies_opts):
    """The channel's video list for one tab, newest first (YouTube's own order)."""
    return ChannelTable.fetch_flat(base_url, tab, None, cookies_opts)


def probe_date(video_id, cookies_opts):
    """Exact upload date of one video (one request), or None if it can't be read."""
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True, "ignoreerrors": True,
        "socket_timeout": 20, "extract_flat": False,
        "extractor_args": {"youtube": {"player_client": ["android", "ios"], "player_skip": ["js", "configs"]}},
        "youtube_include_dash_manifest": False, "youtube_include_hls_manifest": False,
        **cookies_opts,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://youtu.be/{video_id}", download=False)
        ud = (info or {}).get("upload_date")
        return datetime.strptime(ud, "%Y%m%d").date() if ud else None
    except Exception:
        return None


class DateResolver:
    """Looks up exact dates only when asked, and remembers them."""

    def __init__(self, cookies_opts):
        self.cookies_opts = cookies_opts
        self.cache = {}
        self.probes = 0

    def at(self, row):
        vid = row["id"]
        if vid in self.cache:
            return self.cache[vid]
        d = None
        ts = row.get("sort_ts")
        if ts is not None and not row.get("approx"):
            d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        if d is None:
            self.probes += 1
            print(".", end="", flush=True)
            d = probe_date(vid, self.cookies_opts)
        if d is None and ts is not None:                      # last resort: the channel page's rough date
            d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        self.cache[vid] = d
        return d

    def nearby(self, rows, mid, lo, hi):
        """First readable date at or around index mid (staying inside lo..hi-1)."""
        order = list(range(mid, min(hi, mid + 4))) + list(range(mid - 1, max(lo, mid - 4) - 1, -1))
        for k in order:
            d = self.at(rows[k])
            if d is not None:
                return k, d
        return None, None


def first_index(rows, pred, resolver, lo=0):
    """rows are newest-first, so dates only go down the list. Find the first row whose date satisfies
    pred (pred is False for the newer rows and True from some point on). Needs only ~log2(n) lookups."""
    hi = len(rows)
    while lo < hi:
        mid = (lo + hi) // 2
        k, d = resolver.nearby(rows, mid, lo, hi)
        if d is None:
            raise RuntimeError("couldn't read upload dates around one part of the list "
                               "(try again later, or update yt-dlp:  pip install -U yt-dlp)")
        if pred(d):
            hi = k
        else:
            lo = k + 1
    return lo


def select_range(rows, d_from, d_to, resolver):
    start = 0 if d_to is None else first_index(rows, lambda d: d <= d_to, resolver)
    end = len(rows) if d_from is None else first_index(rows, lambda d: d < d_from, resolver, lo=start)
    return rows[start:end]


def _by(rows, field, descending):
    have = [r for r in rows if r.get(field) is not None]
    missing = [r for r in rows if r.get(field) is None]            # unknown values always go last
    return sorted(have, key=lambda r: r[field], reverse=descending) + missing


def rank_rows(rows, how):
    """rows come newest-first (YouTube's own order). Ties keep that order."""
    if how == "popular":
        return _by(rows, "views", True)
    if how == "unpopular":
        return _by(rows, "views", False)
    if how == "oldest":
        return list(reversed(rows))
    if how == "longest":
        return _by(rows, "duration", True)
    if how == "shortest":
        return _by(rows, "duration", False)
    if how == "title":
        return sorted(rows, key=lambda r: (r.get("title") or "").casefold())
    if how == "random":
        shuffled = list(rows)
        random.shuffle(shuffled)
        return shuffled
    return list(rows)                                              # "latest"


# --------------------------------------------------------------------------- picking positions

def parse_pick(spec):
    """'60' | 'last:20' | 'new:60' | 'random:30' | 'every:5' | '25-70' | '1-10,25,40-' ...
    -> ('first'|'last'|'new'|'random', N)  or  ('positions', [(start, end_or_None, step), ...])"""
    t = re.sub(r"\s+", "", str(spec).lower())
    if not t:
        raise ValueError("nothing to pick - try something like 60, 25-70 or last:20")
    if t.isdigit():
        if int(t) < 1:
            raise ValueError("the number must be at least 1")
        return ("first", int(t))
    m = re.fullmatch(r"(first|last|new|random|every|comps):(\d+)", t)
    if m:
        n = int(m.group(2))
        if n < 1:
            raise ValueError("the number must be at least 1")
        return ("positions", [(1, None, n)]) if m.group(1) == "every" else (m.group(1), n)
    parts = []
    for tok in t.split(","):
        if not tok:
            continue
        m = re.fullmatch(r"(\d*)(-(\d*))?(?:/(\d+))?", tok)
        if not m or (not m.group(1) and not m.group(2)):
            raise ValueError(f"can't read '{tok}' - use things like 25, 25-70, 25-, -30 or 1-100/10")
        a = int(m.group(1)) if m.group(1) else 1
        if not m.group(2):
            b = a                                                   # a single position
        else:
            b = int(m.group(3)) if m.group(3) else None             # open-ended: to the last video
        step = int(m.group(4)) if m.group(4) else 1
        if a < 1 or step < 1 or (b is not None and b < a):
            raise ValueError(f"'{tok}' doesn't make sense (positions start at 1 and must go upwards)")
        parts.append((a, b, step))
    if not parts:
        raise ValueError("nothing to pick")
    return ("positions", parts)


def apply_pick(rows, spec, downloaded):
    """rows: the filtered, sorted list. Returns [(position, row), ...] in list order (positions are 1-based)."""
    kind, val = parse_pick(spec)
    if kind == "comps":
        raise ValueError("a 'comps:N' pick has to be turned into 'new:N' first (see compilations_to_videos)")
    n = len(rows)
    if kind == "first":
        idx = list(range(min(val, n)))
    elif kind == "last":
        idx = list(range(max(0, n - val), n))
    elif kind == "random":
        idx = sorted(random.sample(range(n), min(val, n)))
    elif kind == "new":
        idx = [i for i, r in enumerate(rows) if r["id"] not in downloaded][:val]
    else:
        chosen = set()
        for a, b, step in val:
            for p in range(a, (n if b is None else min(b, n)) + 1, step):
                chosen.add(p - 1)
        idx = sorted(chosen)
    return [(i + 1, rows[i]) for i in idx]


def describe_pick(spec):
    kind, val = parse_pick(spec)
    if kind == "first":
        return f"the first {val}"
    if kind == "last":
        return f"the last {val}"
    if kind == "random":
        return f"{val} random ones (shown in list order)"
    if kind == "new":
        return f"the next {val} not downloaded yet"
    if kind == "comps":
        return f"enough new videos for {mc.plural(val, 'compilation')}"
    if len(val) == 1 and val[0][2] > 1 and val[0][0] == 1 and val[0][1] is None:
        return f"every {val[0][2]}th video"
    bits = []
    for a, b, step in val:
        s = f"{a}" if b == a else f"{a}-{b if b is not None else 'end'}"
        bits.append(s + (f" (every {step}th)" if step > 1 else ""))
    return "positions " + ", ".join(bits)


# --------------------------------------------------------------------------- downloading

def is_tty():
    return sys.stdin.isatty()


class StopDownload(Exception):
    """Raised inside a running download when the user pressed Ctrl-C, so it ends quickly."""


def download_one(url, outtmpl, max_height, cookies_opts, stop=None, show_progress=True):
    def hook(d):
        if stop is not None and stop.is_set():
            raise StopDownload()
        if show_progress and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                print(f"\r   downloading... {d.get('downloaded_bytes', 0) * 100 // total}%   ", end="", flush=True)

    h = int(max_height)
    opts = {
        "format": f"bv*[height<={h}]+ba/b[height<={h}]/b",
        "merge_output_format": "mp4", "outtmpl": outtmpl, "quiet": True, "no_warnings": True,
        "noplaylist": True, "concurrent_fragment_downloads": 4, "progress_hooks": [hook], **cookies_opts,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    if show_progress:
        print("\r" + " " * 40 + "\r", end="")


def find_downloaded(dest, stem):
    """The finished file for a download (ignores half-finished .part and per-stream leftovers)."""
    good = [p for p in dest.glob(stem + ".*")
            if p.suffix.lower() in mc.VIDEO_EXTS and not re.search(r"\.f\d+\.", p.name)]
    return max(good, key=lambda p: p.stat().st_size) if good else None


def clean_partials(dest, prefix):
    for p in dest.glob(prefix + "*"):
        if p.suffix.lower() in (".part", ".ytdl", ".temp") or re.search(r"\.f\d+\.", p.name):
            p.unlink(missing_ok=True)


def _remember(archive, r, got, channel, stamp):
    archive[r["id"]] = {"file": got.name, "title": r["title"], "views": r["views"],
                        "duration": r.get("duration"), "channel": channel, "run": stamp}
    mc.save_json(ARCHIVE_FILE, archive)


def _why(e):
    return str(e).splitlines()[0][:150] if str(e) else "download failed"


def download_batch(queue, target, dest, stamp, first_index, max_height, cookies_opts, archive, channel, workers=1):
    """Download from queue ([(position, row), ...] in the chosen order) until `target` clips have arrived.
    A clip that fails is skipped and the next one in the queue takes its place. File names carry the
    order (stamp_NNNN_id) even when several download at once, and every success goes into the archive
    straight away. Returns (files in queue order, failed_positions, next_index)."""
    if workers <= 1:
        files, failed, index = [], [], first_index
        for pos, r in queue:
            if len(files) >= target:
                break
            stem = f"{stamp}_{index:04d}_{r['id']}"
            index += 1
            print(f"[{len(files) + 1}/{target}] #{pos} {r['title']}")
            try:
                download_one(f"https://www.youtube.com/watch?v={r['id']}", str(dest / (stem + ".%(ext)s")),
                             max_height, cookies_opts)
                got = find_downloaded(dest, stem)
                if got is None:
                    raise RuntimeError("finished but no file was found")
            except KeyboardInterrupt:
                raise
            except Exception as e:
                failed.append(pos)
                print(f"   skipped ({_why(e)})")
                clean_partials(dest, stem)
                continue
            files.append(got)
            _remember(archive, r, got, channel, stamp)
        return files, failed, index

    stop = threading.Event()
    pending, inflight, done_files, failed, index = list(queue), {}, {}, [], first_index

    def work(stem, r):
        download_one(f"https://www.youtube.com/watch?v={r['id']}", str(dest / (stem + ".%(ext)s")),
                     max_height, cookies_opts, stop=stop, show_progress=False)
        got = find_downloaded(dest, stem)
        if got is None:
            raise RuntimeError("finished but no file was found")
        return got

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        while True:
            while pending and len(done_files) + len(inflight) < target and len(inflight) < workers:
                pos, r = pending.pop(0)
                stem = f"{stamp}_{index:04d}_{r['id']}"
                inflight[pool.submit(work, stem, r)] = (index, pos, r, stem)
                index += 1
            if not inflight:
                break
            finished, _ = wait(inflight, return_when=FIRST_COMPLETED)
            for fut in finished:
                idx, pos, r, stem = inflight.pop(fut)
                try:
                    got = fut.result()
                except Exception as e:                      # (Ctrl-C is not an Exception: it goes to the handler below)
                    failed.append(pos)
                    clean_partials(dest, stem)
                    print(f"   skipped #{pos} {r['title']} ({_why(e)})")
                    continue
                done_files[idx] = got
                _remember(archive, r, got, channel, stamp)
                print(f"[{len(done_files)}/{target}] #{pos} {r['title']}")
    except KeyboardInterrupt:
        stop.set()                                          # running downloads end at their next progress tick
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return [done_files[i] for i in sorted(done_files)], failed, index


def compile_now(dest, play, count="all", include_leftover=False):
    """Run make_compilations.py on the download folder. Returns (exit code, names of the compilations made)."""
    before = set(mc.load_json(COMPILE_LEDGER, {}).get("compilations", {}))
    cmd = [sys.executable, str(MAKE), str(count), "--clips", str(dest), "--order", "name"]
    if play == "reverse":
        cmd.append("--reverse")
    elif play == "each":
        cmd.append("--reverse-each")
    if include_leftover:
        cmd.append("--include-leftover")
    rc = subprocess.run(cmd).returncode
    after = mc.load_json(COMPILE_LEDGER, {}).get("compilations", {})
    return rc, [n for n in after if n not in before]


def delete_used_clips(dest, archive):
    """Delete clips that are now inside a compilation. Only files this script downloaded (never ones that
    were adopted from elsewhere). Includes leftovers from earlier runs that have just been used."""
    used = set()
    for e in mc.load_json(COMPILE_LEDGER, {}).get("compilations", {}).values():
        used.update(e["clips"])
    ours = {v["file"] for v in archive.values() if v.get("file") and not v.get("adopted")}
    removed = 0
    for key in used:
        f = dest / key
        if key in ours and f.is_file():
            f.unlink()
            removed += 1
    return removed


# --------------------------------------------------------------------------- what do we already have?

def find_on_disk(folders, wanted_ids):
    """Video files in these folders whose name contains one of the wanted video ids -> {id: path}.
    Works for our own names (stamp_0001_ID.mp4) and yt-dlp's usual 'Title [ID].mp4'. An id is 11 characters,
    so a stray match inside some other name is practically impossible."""
    found = {}
    for folder in folders:
        if not Path(folder).is_dir():
            continue
        for _, path in mc.scan_clips(folder):
            name = path.name
            if re.search(r"\.f\d+\.", name):                    # per-stream leftovers of an unfinished download
                continue
            for i in range(len(name) - 10):
                vid = name[i:i + 11]
                if vid in wanted_ids and vid not in found:
                    found[vid] = path
    return found


def adopt_found(found, rows_by_id, archive, dest, channel):
    """Add clips that were on disk but not in the download list to it. Returns how many were added."""
    added = 0
    for vid, path in found.items():
        if vid in archive:
            continue
        r = rows_by_id[vid]
        try:
            where = path.relative_to(dest).as_posix()
        except ValueError:
            where = str(path)
        archive[vid] = {"file": where, "title": r["title"], "views": r["views"], "duration": r.get("duration"),
                        "channel": channel, "run": "adopted", "adopted": True}
        added += 1
    return added


def dup_key(title, duration):
    """Same title (ignoring case and punctuation) and same length to the second = probably the same clip."""
    t = re.sub(r"[\W_]+", " ", (title or "").casefold()).strip()
    if not t or duration is None:
        return None
    return (t, int(round(duration)))


def find_likely_duplicates(ranked, known, archive):
    """Ids in `ranked` that look like re-uploads: same title and length as a clip we already have (known,
    or in the archive) or as one that ranks higher in this list. A guess, so it is always shown to the user."""
    seen = {k for k in (dup_key(e.get("title"), e.get("duration")) for e in archive.values()) if k}
    for r in ranked:
        if r["id"] in known:
            k = dup_key(r["title"], r.get("duration"))
            if k:
                seen.add(k)
    dups = set()
    for r in ranked:
        if r["id"] in known:
            continue
        k = dup_key(r["title"], r.get("duration"))
        if not k:
            continue
        if k in seen:
            dups.add(r["id"])
        else:
            seen.add(k)
    return dups


# --------------------------------------------------------------------------- planning by compilations

def compile_config():
    """The compilation settings from make_compilations.py (its defaults where nothing is saved yet)."""
    return {**mc.DEFAULTS, **mc.load_json(COMPILE_SETTINGS, {})}


def waiting_clips(dest, cfg):
    """Clips in the download folder that are not inside any compilation yet -> [(key, path)]."""
    if not dest.is_dir():
        return []
    ledger = mc.load_json(COMPILE_LEDGER, {})
    ledger.setdefault("compilations", {})
    ledger.setdefault("skipped", {})
    used = mc.used_keys(ledger)
    return [(k, p) for k, p in mc.scan_clips(dest, skip_dir=Path(cfg["output_dir"])) if k not in used]


def clips_per_compilation(cfg, rows):
    """How many clips make one compilation -> (number, exact?). By count it is exact; by minutes it is
    estimated from the average clip length in the list."""
    if cfg["size_mode"] == "count":
        return cfg["clips_per_video"], True
    lens = [r["duration"] for r in rows if r.get("duration")]
    avg = sum(lens) / len(lens) if lens else 30
    return max(2, math.ceil(cfg["minutes_per_video"] * 60 / avg)), False


def eff_need(new_spec):
    """N out of a 'new:N' pick."""
    return parse_pick(new_spec)[1]


def compilations_to_videos(k, per, waiting):
    """How many NEW videos are needed so that k full compilations can be made, counting the clips that
    are already waiting in the download folder."""
    return max(0, k * per - waiting)


def leftover_step(job, args, capped):
    """After compiling: clips that can't fill a whole compilation are still lying there. Ask what to do
    (or follow --leftover). capped = the user asked for N compilations and got them, so the rest is just
    spare, not leftover. Returns the names of any compilations made here."""
    cfg, dest = job.cfg, job.dest
    n = len(waiting_clips(dest, cfg))
    per, exact = clips_per_compilation(cfg, job.ranked)
    if n == 0 or n >= per or capped:
        return []
    missing = per - n
    blocked = job.skip | {k for k, v in job.archive.items() if v.get("run") == job.stamp}
    spare = [(i + 1, r) for i, r in enumerate(job.ranked) if r["id"] not in blocked]
    options = [("keep", "Keep them for next time (they join your next compilation)")]
    if n >= 2:
        options.append(("short", "Make a shorter compilation from them now"))
    if len(spare) >= missing:
        options.append(("topup", f"Download {missing} more to fill one{'' if exact else ' (about)'}"))
    keys = [k for k, _ in options]

    ask = is_tty() and not args.yes and not args.leftover
    if ask:
        print(f"\n{mc.plural(n, 'clip')} left over - a full compilation needs {'' if exact else 'about '}{per}.")
        choice = mc.ask_choice("What now?", options, job.s["leftover"] if job.s["leftover"] in keys else "keep")
        job.s["leftover"] = choice
        mc.save_json(SETTINGS_FILE, job.s)
    else:
        choice = args.leftover or "keep"
        if choice not in keys:
            why = {"short": "need at least 2 clips", "topup": "the list has no more new videos to add"}[choice]
            print(f"\n  --leftover {choice} isn't possible here ({why}); keeping the {mc.plural(n, 'clip')} for next time.")
            choice = "keep"

    if choice == "keep":
        print(f"\nKept {mc.plural(n, 'leftover clip')} for next time (a full compilation needs {per}).")
        return []
    if choice == "short":
        _, made = compile_now(dest, job.s["play"], "all", include_leftover=True)
        return made
    print(f"\nDownloading {missing} more to fill the last compilation...")
    try:
        files, failed, job.next_index = download_batch(spare, missing, dest, job.stamp, job.next_index,
                                                       job.s["max_height"], job.cookies_opts, job.archive,
                                                       job.s["channel"], job.workers)
    except KeyboardInterrupt:
        clean_partials(dest, job.stamp)
        print("\nStopped. The leftover clips are kept.")
        return []
    if len(files) < missing:
        print(f"  Only {len(files)} of {missing} arrived, so there still aren't enough for a full one - "
              "keeping the clips for next time.")
        return []
    _, made = compile_now(dest, job.s["play"], "all")
    return made


def print_summary(dest, cfg, made):
    """The last thing on screen: what was made, where, and what is left."""
    left = len(waiting_clips(dest, cfg))
    ledger = mc.load_json(COMPILE_LEDGER, {}).get("compilations", {})
    print("\n" + "=" * 50)
    if made:
        print(f"All done: {mc.plural(len(made), 'compilation')} made.")
        for name in made[:8]:
            f = Path(ledger.get(name, {}).get("file", ""))
            size = f"  ({f.stat().st_size / 1e6:.0f} MB)" if f.is_file() else ""
            print(f"   {f.name or name}{size}")
        if len(made) > 8:
            print(f"   ... and {len(made) - 8} more")
        print(f"   in {cfg['output_dir']}")
    else:
        print("No compilation was made this time.")
    if left:
        print(f"{mc.plural(left, 'clip')} waiting in {dest.name}/ for the next batch.")
    print("Next batch, no questions:   python auto_compile.py --again --yes")


# --------------------------------------------------------------------------- questions

def pick_defaults(spec):
    try:
        kind, val = parse_pick(spec)
    except ValueError:
        return "comps", 3
    if kind in ("first", "last", "new", "random", "comps"):
        return kind, val
    if len(val) == 1:
        a, b, step = val[0]
        if a == 1 and b is None and step > 1:
            return "every", step
        if step == 1 and b is not None and b != a:
            return "range", 30
    return "custom", 30


def ask_pick(spec):
    mode, n_default = pick_defaults(spec)
    saved_mode = mode
    mode = mc.ask_choice("5. Which ones from that ordered list? (position 1 = first in the order above)", PICKS, mode)
    if mode == "comps":
        n = mc.ask_int("   how many compilations (clips already waiting in the folder are counted)",
                       n_default if saved_mode == "comps" else 3, 1, 1000)
        return f"comps:{n}"
    if mode in ("first", "new", "last", "random"):
        n = mc.ask_int("   how many", n_default, 1, 100000)
        return str(n) if mode == "first" else f"{mode}:{n}"
    if mode == "every":
        n = mc.ask_int("   every how many (5 = every 5th video)", n_default if n_default > 1 else 5, 2, 100000)
        return f"every:{n}"
    if mode == "range":
        a = mc.ask_int("   from position", 1, 1, 1000000)
        b = mc.ask_int("   to position", a + 29, a, 1000000)
        return f"{a}-{b}"
    while True:
        text = mc.ask_text("   positions, e.g. 1-10, 25, 40-50 (40- means 40 to the end)", "")
        try:
            parse_pick(text)
            return text
        except ValueError as e:
            print(f"   {e}")


def interview(s):
    print("\nA few questions. Press Enter to accept the [default].\n")
    s["channel"] = mc.ask_text("1. Channel (link or @handle)", s["channel"]).strip()
    while not s["channel"]:
        s["channel"] = mc.ask_text("   a channel is needed", "").strip()
    s["type"] = mc.ask_choice("2. What to take from it?",
                              [("videos", "Regular videos"), ("shorts", "Shorts")], s["type"])
    s["date_from"] = ask_date("3. Only videos uploaded FROM this date, e.g. 2024-01-31", s["date_from"])
    s["date_to"] = ask_date("   ...and up TO this date", s["date_to"])
    s["sort"] = mc.ask_choice("4. Put the videos in which order first?", SORTS, s["sort"])
    s["pick"] = ask_pick(s["pick"])
    s["play"] = mc.ask_choice("6. Order inside the compilations?", PLAYS, s["play"])
    ans = mc.ask_text("7. Delete the downloaded clips afterwards to save disk space? (y/n)",
                      "y" if s["delete_after"] else "n").lower()
    s["delete_after"] = ans.startswith("y")
    print()


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Pick videos from a channel, download them, make compilations.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("channel", nargs="?", help="channel link or @handle (leave out to be asked)")
    ap.add_argument("--type", choices=["videos", "shorts"])
    ap.add_argument("--sort", choices=[k for k, _ in SORTS])
    ap.add_argument("--pick", metavar="WHICH", help="e.g. 60, 25-70, last:20, every:5, new:60, 1-10,25")
    ap.add_argument("--top", type=int, metavar="N", help="same as --pick new:N")
    ap.add_argument("--compilations", "-n", type=int, metavar="N",
                    help="same as --pick comps:N - download just enough new videos to make N full compilations")
    ap.add_argument("--from", dest="date_from", metavar="DATE")
    ap.add_argument("--to", dest="date_to", metavar="DATE")
    ap.add_argument("--min-views", type=int, metavar="N")
    ap.add_argument("--min-minutes", type=float, metavar="M")
    ap.add_argument("--max-minutes", type=float, metavar="M")
    ap.add_argument("--max-height", type=int, metavar="PX")
    ap.add_argument("--dest", metavar="FOLDER", help="where downloads go (default: 'fetched' next to the scripts)")
    ap.add_argument("--reverse", action="store_true", help="compile with the last picked video first")
    ap.add_argument("--reverse-each", action="store_true", help="play each compilation backwards")
    ap.add_argument("--redownload", action="store_true", help="ignore the already-downloaded list")
    ap.add_argument("--keep-duplicates", action="store_true",
                    help="also download videos that look like re-uploads (same title and length) of ones you have")
    ap.add_argument("--check-folder", action="append", metavar="FOLDER",
                    help="also look in this folder for videos you already have (remembered; can be repeated)")
    ap.add_argument("--delete-after", action="store_true")
    ap.add_argument("--no-compile", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--again", action="store_true",
                    help="repeat your last run (same channel, order, dates and picks) with the next batch, no questions")
    ap.add_argument("--workers", type=int, default=3, metavar="N",
                    help="how many videos to download at the same time (default 3; 1 = one by one)")
    ap.add_argument("--include-leftover", action="store_true",
                    help="make the shorter final compilation from leftover clips right away (same as --leftover short)")
    ap.add_argument("--leftover", choices=["keep", "short", "topup"],
                    help="what to do when clips are left that can't fill a whole compilation: keep them for next "
                         "time, make a shorter compilation, or download just enough more to fill one "
                         "(asked when you run it by hand; with --yes the default is keep)")
    ap.add_argument("--cookies-from-browser", metavar="BROWSER")
    ap.add_argument("--cookies", metavar="FILE")
    args = ap.parse_args()

    if args.again and args.channel:
        sys.exit("Error: --again repeats your last run, so leave the channel out")
    if sum(bool(x) for x in (args.pick, args.top, args.compilations)) > 1:
        sys.exit("Error: use only one of --pick, --top and --compilations")
    workers = max(1, min(args.workers, 8))
    if args.compilations is not None and args.compilations < 1:
        sys.exit("Error: --compilations must be at least 1")
    if args.reverse and args.reverse_each:
        sys.exit("Error: use either --reverse or --reverse-each, not both")
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        sys.exit("Error: ffmpeg/ffprobe not found on PATH")
    if not MAKE.is_file():
        sys.exit(f"Error: make_compilations.py must be in the same folder as this script ({HERE})")

    flag_pick = args.pick or (f"new:{args.top}" if args.top else None) \
        or (f"comps:{args.compilations}" if args.compilations else None)
    flag_play = "reverse" if args.reverse else "each" if args.reverse_each else None
    limit_flags = ("min_views", "min_minutes", "max_minutes", "max_height", "dest")

    saved = mc.load_json(SETTINGS_FILE, {})
    if "pick" not in saved and "top" in saved:                 # settings from the first version
        saved["pick"] = f"new:{saved['top']}"
    if args.channel:
        # One-line mode: only what you type counts, so an old date range can never sneak in.
        # (Only the download folder and quality cap are remembered between runs.)
        s = {**DEFAULTS, **{k: saved[k] for k in ("dest", "max_height", "check_folders", "leftover") if k in saved}}
        s["channel"] = args.channel
        for key in ("type", "sort", "date_from", "date_to") + limit_flags:
            if getattr(args, key, None) is not None:
                s[key] = getattr(args, key)
        s["pick"] = flag_pick or DEFAULTS["pick"]
        s["play"] = flag_play or "asis"
        s["delete_after"] = args.delete_after
    elif args.again:
        if not saved.get("channel"):
            sys.exit("Error: nothing to repeat yet - run  python auto_compile.py  once first")
        s = {**DEFAULTS, **saved}                      # unlike a fresh interactive run, the last date range is kept
        for key in limit_flags + ("date_from", "date_to"):
            if getattr(args, key, None) is not None:
                s[key] = getattr(args, key)
        if flag_pick:
            s["pick"] = flag_pick
        if flag_play:
            s["play"] = flag_play
        if args.delete_after:
            s["delete_after"] = True
        if s["sort"] not in SORT_WORDS:
            s["sort"] = "popular"
        when = f", uploaded {s['date_from'] or 'the start'} to {s['date_to'] or 'today'}" \
            if s["date_from"] or s["date_to"] else ""
        print(f"Repeating your last run: {s['channel']} ({s['type']}), order by {SORT_WORDS[s['sort']]}, "
              f"{describe_pick(s['pick'])}{when}.")
        if parse_pick(s["pick"])[0] not in ("new", "comps"):
            print("  note: that pick can select the same videos again (ones you already have are skipped). "
                  "For fresh batches use --pick comps:N or --pick new:N.")
    else:
        s = {**DEFAULTS, **saved}
        # A date range is a one-off, so it is never carried over from the last run
        # (a stale default silently shrank the list). --from/--to can still prefill it.
        s["date_from"], s["date_to"] = "", ""
        for key in limit_flags + ("date_from", "date_to"):
            if getattr(args, key, None) is not None:
                s[key] = getattr(args, key)
        if flag_pick:
            s["pick"] = flag_pick
        if flag_play:
            s["play"] = flag_play
        interview(s)
    if s["sort"] not in SORT_WORDS:
        s["sort"] = "popular"
    for f in args.check_folder or []:
        if not Path(mc.clean_path(f)).is_dir():
            sys.exit(f"Error: --check-folder not found: {f}")
    s["check_folders"] = sorted(set(s.get("check_folders") or [])
                                | {str(Path(mc.clean_path(f)).resolve()) for f in args.check_folder or []})

    try:
        d_from = parse_date(s["date_from"]) if s["date_from"] else None
        d_to = parse_date(s["date_to"]) if s["date_to"] else None
        parse_pick(s["pick"])
    except ValueError as e:
        sys.exit(f"Error: {e}")
    if d_from and d_to and d_from > d_to:
        sys.exit("Error: the FROM date is after the TO date")
    mc.save_json(SETTINGS_FILE, s)

    cookies_opts = build_cookies_opts(argparse.Namespace(
        cookies_from_browser=args.cookies_from_browser, cookies=args.cookies))
    dest = Path(s["dest"])
    archive = mc.load_json(ARCHIVE_FILE, {})
    downloaded = {} if args.redownload else archive

    # ---- one-time look-and-feel setup for the compilations. Asked before anything is downloaded and before
    # the plan, because "N compilations" needs to know how big one is.
    if not args.no_compile and not args.dry_run and not COMPILE_SETTINGS.exists():
        print("\nFirst time: a short one-time setup for how the compilations should look.")
        mc.wizard({**mc.DEFAULTS}, ask_folder=False)
        cfg = compile_config()
        if not cfg["clips_dir"]:                          # so a later `python make_compilations.py` finds these clips
            cfg["clips_dir"] = str(dest.resolve())
            mc.save_json(COMPILE_SETTINGS, cfg)

    # ---- read the channel
    base_url = ChannelTable.base_channel_url(s["channel"])
    print(f"Reading the {s['type']} list of {s['channel']} ... (a big channel can take a minute)")
    rows = [r for r in list_tab(base_url, s["type"], cookies_opts) if r.get("id")]
    if not rows:
        sys.exit("No videos found - check the channel link/@handle (or try --cookies-from-browser).")
    print(f"  found {len(rows)} {s['type']}.")

    # ---- 1. narrow by date, only looking up exact dates where it matters
    if d_from or d_to:
        resolver = DateResolver(cookies_opts)
        print("Checking dates", end="", flush=True)
        try:
            rows_in = select_range(rows, d_from, d_to, resolver)
        except RuntimeError as e:
            print()
            sys.exit(f"Error: {e}")
        print(f" done ({resolver.probes} lookups).")
        print(f"  {len(rows_in)} of them were uploaded {d_from or 'at the start'} to {d_to or 'today'}.")
        range_note = (f"\nNote: your date range ({d_from or 'start'} to {d_to or 'today'}) narrowed the "
                      f"{len(rows)} {s['type']} down to {len(rows_in)}. Clear or widen it to reach more.")
    else:
        rows_in = rows
        range_note = ""

    # ---- length / views limits
    def keep(r):
        dur, views = r.get("duration"), r.get("views")
        if dur is not None and s["max_minutes"] and dur > s["max_minutes"] * 60:
            return False
        if dur is not None and s["min_minutes"] and dur < s["min_minutes"] * 60:
            return False
        if views is not None and s["min_views"] and views < s["min_views"]:
            return False
        return True

    before = len(rows_in)
    rows_in = [r for r in rows_in if keep(r)]
    if len(rows_in) < before:
        print(f"  {before - len(rows_in)} left out by your length/views limits.")
    if not rows_in:
        sys.exit("No videos match those limits.")

    # ---- 2. order, 3. pick positions
    ranked = rank_rows(rows_in, s["sort"])

    # ---- what do we already have? Clips on disk count even if they are missing from the download list
    # (downloaded by hand, history file lost, ...). Look-alikes of clips we have are skipped too.
    if not args.redownload:
        folders = [dest] + [Path(f) for f in s["check_folders"]]
        cfg_dir = mc.load_json(COMPILE_SETTINGS, {}).get("clips_dir")
        if cfg_dir:
            folders.append(Path(cfg_dir))
        folders = list(dict.fromkeys(f.resolve() for f in folders if f.is_dir()))
        on_disk = find_on_disk(folders, {r["id"] for r in rows})
        fresh = [v for v in on_disk if v not in archive]
        if fresh:
            elsewhere = sum(1 for v in fresh if dest.resolve() not in on_disk[v].parents)
            print(f"  {mc.plural(len(fresh), 'video')} already on your disk but missing from the download list"
                  " - counting them as downloaded"
                  + (f" ({elsewhere} in other folders, so they won't go into the compilations)." if elsewhere else "."))
            adopt_found(on_disk, {r["id"]: r for r in rows}, archive, dest.resolve(), s["channel"])
            if not args.dry_run:
                mc.save_json(ARCHIVE_FILE, archive)
    dup_ids = set() if args.keep_duplicates else find_likely_duplicates(ranked, set(downloaded), downloaded)
    skip = set(downloaded) | dup_ids
    if dup_ids:
        print(f"  {mc.plural(len(dup_ids), 'video')} look like re-uploads (same title and length as a clip you "
              "already have, or as a higher-ranked one) - skipping them. Use --keep-duplicates to include them.")
        if args.dry_run:
            for r in [r for r in ranked if r["id"] in dup_ids][:5]:
                print(f"      look-alike: {r['title']}")

    kind, val = parse_pick(s["pick"])
    cfg = compile_config()
    waiting, per, comps_text = 0, None, ""
    if kind == "comps":
        per, exact = clips_per_compilation(cfg, ranked)
        waiting = len(waiting_clips(dest, cfg))
        need = compilations_to_videos(val, per, waiting)
        eff_pick = f"new:{need}" if need else None
        comps_text = ("" if exact else "about ") + mc.plural(per, "clip")
    else:
        eff_pick = s["pick"]
    what = describe_pick(s["pick"])
    picked = apply_pick(ranked, eff_pick, skip) if eff_pick else []

    if kind != "comps":
        if not picked:
            if kind == "new":
                sys.exit("Nothing new: every video in that list has already been downloaded"
                         + (" or looks like a re-upload" if dup_ids else "") + "." + range_note)
            sys.exit(f"Your pick ({what}) doesn't match anything - the list only has {len(ranked)} videos."
                     + range_note)
    new_picked = [(p, r) for p, r in picked if r["id"] not in skip]
    already = sum(1 for _, r in picked if r["id"] in downloaded)
    lookalikes = len(picked) - len(new_picked) - already
    if kind != "comps" and not new_picked:
        sys.exit(f"All {len(picked)} videos in that selection ({what}) were downloaded in an earlier run"
                 + (" or look like re-uploads" if lookalikes else "") + ".\n"
                 "To get the next batch, pick different positions (e.g. 61-120) or use new:N." + range_note)
    if kind == "comps" and not new_picked and waiting < 2:
        sys.exit("Nothing to do: no new videos are available in that list"
                 + (" or they all look like re-uploads" if dup_ids else "") + f" and only {mc.plural(waiting, 'clip')} waiting."
                 + range_note)

    known = [r["duration"] for _, r in new_picked if r.get("duration")]
    length = f", about {mc.fmt_duration(sum(known))} of video" if known else ""
    if kind == "comps":
        print(f"\nPlan: make {mc.plural(val, 'compilation')} of {comps_text} each = {val * per} clips; "
              f"order by {SORT_WORDS[s['sort']]} from the {len(ranked)} videos in that list.")
        have = f"{mc.plural(waiting, 'clip')} already waiting in {dest.name}/" if waiting else "no clips waiting yet"
        if eff_pick is None:
            print(f"      {have} - already enough, nothing to download.")
        else:
            print(f"      {have}, so downloading {len(new_picked)} new{length}.")
            if len(new_picked) < eff_need(eff_pick):
                full = (waiting + len(new_picked)) // per
                print(f"      Only {len(new_picked)} new videos are available in that list, which makes "
                      f"{mc.plural(full, 'full compilation')} at most." + range_note.replace("\n", " ").rstrip())
    else:
        print(f"\nPlan: order by {SORT_WORDS[s['sort']]}, take {what} of the {len(ranked)} in that list")
        print(f"      = {mc.plural(len(picked), 'video')}"
              + (f", {already} already downloaded earlier (skipped)" if already else "")
              + (f", {lookalikes} look like re-uploads (skipped)" if lookalikes else "")
              + f" -> downloading {len(new_picked)}{length}.")
    if s["play"] != "asis":
        print("      Compilations will be built " + ("in reverse order." if s["play"] == "reverse"
                                                      else "with each one played backwards."))

    if args.dry_run:
        for p, r in picked:
            tag = "  (already downloaded)" if r["id"] in downloaded else ""
            print(f"  {p:>5}. {fmt_views(r['views']):>7}  {r['upload_date']:<11} {r['title']}{tag}")
        print("\n(dry run - nothing was downloaded)")
        return

    if is_tty() and not args.yes:
        if mc.ask_text("Start? (y/n)", "y").lower().startswith("n"):
            return

    # ---- queue: exactly the picked positions. Only "new:N" fills a failed download with the next one in line.
    queue = list(new_picked)
    if kind in ("new", "comps") and new_picked:
        used_ids = {r["id"] for _, r in new_picked}
        queue += [(i + 1, r) for i, r in enumerate(ranked) if r["id"] not in skip and r["id"] not in used_ids]
    target = len(new_picked)

    # ---- download
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    started = time.time()
    try:
        files, failed, next_index = download_batch(queue, target, dest, stamp, 1, s["max_height"], cookies_opts,
                                                   archive, s["channel"], workers) if target else ([], [], 1)
    except KeyboardInterrupt:
        clean_partials(dest, stamp)
        print("\n\nStopped. Clips downloaded so far are kept. To turn them into compilations later, run:")
        print(f"   python make_compilations.py all --clips \"{dest}\" --order name")
        return

    if target:
        print(f"\nDownloaded {mc.plural(len(files), 'video')} in {mc.fmt_duration(time.time() - started)}"
              + (f" (positions {', '.join('#' + str(p) for p in failed)} could not be downloaded)." if failed else "."))
    if failed:
        print("  If many fail, update yt-dlp first:  pip install -U yt-dlp")
    if args.no_compile:
        print(f"Saved in: {dest}")
        return
    if not files and kind != "comps":
        return                          # nothing new arrived (compilations only start from new downloads)

    # ---- compile: everything unused in the folder (new clips plus any leftovers), in name order
    print("\nMaking compilations...")
    rc, made = compile_now(dest, s["play"], val if kind == "comps" else "all", args.include_leftover)
    if rc == 0 and not args.include_leftover:
        job = SimpleNamespace(dest=dest, cfg=cfg, s=s, ranked=ranked, skip=skip, archive=archive,
                              cookies_opts=cookies_opts, stamp=stamp, next_index=next_index, workers=workers)
        made += leftover_step(job, args, capped=(kind == "comps" and len(made) >= val))

    if s["delete_after"] and rc == 0:
        removed = delete_used_clips(dest, archive)
        if removed:
            print(f"Deleted {mc.plural(removed, 'downloaded clip')} that are now inside compilations.")
    print_summary(dest, cfg, made)


def fmt_views(n):
    return yt_toolkit.fmt_count(n)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled")
