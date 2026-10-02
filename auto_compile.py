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
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SETTINGS_FILE = HERE / "auto_settings.json"
ARCHIVE_FILE = HERE / "fetch_archive.json"          # every video id downloaded so far
MAKE = HERE / "make_compilations.py"
COMPILE_SETTINGS = HERE / "compile_settings.json"
COMPILE_LEDGER = HERE / "compile_ledger.json"

DEFAULTS = {
    "channel": "", "type": "videos", "sort": "popular", "pick": "new:30",
    "date_from": "", "date_to": "", "dest": str(HERE / "fetched"),
    "max_height": 1080, "min_minutes": 0, "max_minutes": 0, "min_views": 0,
    "play": "asis", "delete_after": False,
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
PICKS = [("first", "The first N videos"),
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
    m = re.fullmatch(r"(first|last|new|random|every):(\d+)", t)
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
    if len(val) == 1 and val[0][2] > 1 and val[0][0] == 1 and val[0][1] is None:
        return f"every {val[0][2]}th video"
    bits = []
    for a, b, step in val:
        s = f"{a}" if b == a else f"{a}-{b if b is not None else 'end'}"
        bits.append(s + (f" (every {step}th)" if step > 1 else ""))
    return "positions " + ", ".join(bits)


# --------------------------------------------------------------------------- downloading

def download_one(url, outtmpl, max_height, cookies_opts):
    def hook(d):
        if d.get("status") == "downloading":
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


# --------------------------------------------------------------------------- questions

def pick_defaults(spec):
    try:
        kind, val = parse_pick(spec)
    except ValueError:
        return "new", 30
    if kind in ("first", "last", "new", "random"):
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
    mode = mc.ask_choice("5. Which ones from that ordered list? (position 1 = first in the order above)", PICKS, mode)
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
    ap.add_argument("--delete-after", action="store_true")
    ap.add_argument("--no-compile", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--include-leftover", action="store_true")
    ap.add_argument("--cookies-from-browser", metavar="BROWSER")
    ap.add_argument("--cookies", metavar="FILE")
    args = ap.parse_args()

    if args.pick and args.top:
        sys.exit("Error: use either --pick or --top, not both")
    if args.reverse and args.reverse_each:
        sys.exit("Error: use either --reverse or --reverse-each, not both")
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        sys.exit("Error: ffmpeg/ffprobe not found on PATH")
    if not MAKE.is_file():
        sys.exit(f"Error: make_compilations.py must be in the same folder as this script ({HERE})")

    flag_pick = args.pick or (f"new:{args.top}" if args.top else None)
    flag_play = "reverse" if args.reverse else "each" if args.reverse_each else None
    limit_flags = ("min_views", "min_minutes", "max_minutes", "max_height", "dest")

    saved = mc.load_json(SETTINGS_FILE, {})
    if "pick" not in saved and "top" in saved:                 # settings from the first version
        saved["pick"] = f"new:{saved['top']}"
    if args.channel:
        # One-line mode: only what you type counts, so an old date range can never sneak in.
        # (Only the download folder and quality cap are remembered between runs.)
        s = {**DEFAULTS, **{k: saved[k] for k in ("dest", "max_height") if k in saved}}
        s["channel"] = args.channel
        for key in ("type", "sort", "date_from", "date_to") + limit_flags:
            if getattr(args, key, None) is not None:
                s[key] = getattr(args, key)
        s["pick"] = flag_pick or DEFAULTS["pick"]
        s["play"] = flag_play or "asis"
        s["delete_after"] = args.delete_after
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
    picked = apply_pick(ranked, s["pick"], downloaded)
    what = describe_pick(s["pick"])
    if not picked:
        if parse_pick(s["pick"])[0] == "new":
            sys.exit("Nothing new: every video in that list has already been downloaded." + range_note)
        sys.exit(f"Your pick ({what}) doesn't match anything - the list only has {len(ranked)} videos."
                 + range_note)
    new_picked = [(p, r) for p, r in picked if r["id"] not in downloaded]
    already = len(picked) - len(new_picked)
    if not new_picked:
        sys.exit(f"All {len(picked)} videos in that selection ({what}) were downloaded in an earlier run.\n"
                 "To get the next batch, pick different positions (e.g. 61-120) or use new:N." + range_note)

    known = [r["duration"] for _, r in new_picked if r.get("duration")]
    length = f", about {mc.fmt_duration(sum(known))} of video" if known else ""
    print(f"\nPlan: order by {SORT_WORDS[s['sort']]}, take {what} of the {len(ranked)} in that list")
    print(f"      = {mc.plural(len(picked), 'video')}"
          + (f", {already} already downloaded earlier (skipped)" if already else "")
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

    if sys.stdin.isatty() and not args.yes:
        if mc.ask_text("Start? (y/n)", "y").lower().startswith("n"):
            return

    # ---- one-time look-and-feel setup for the compilations, asked up front rather than mid-way
    if not args.no_compile and not COMPILE_SETTINGS.exists():
        print("\nFirst time: a short one-time setup for how the compilations should look.")
        mc.wizard({**mc.DEFAULTS}, ask_folder=False)

    # ---- queue: exactly the picked positions. Only "new:N" fills a failed download with the next one in line.
    queue = list(new_picked)
    if parse_pick(s["pick"])[0] == "new":
        used_ids = {r["id"] for _, r in new_picked}
        queue += [(i + 1, r) for i, r in enumerate(ranked) if r["id"] not in downloaded and r["id"] not in used_ids]
    target = len(new_picked)

    # ---- download
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    files, failed, started = [], [], time.time()
    try:
        for pos, r in queue:
            if len(files) >= target:
                break
            stem = f"{stamp}_{len(files) + 1:04d}_{r['id']}"      # file names keep the chosen order
            print(f"[{len(files) + 1}/{target}] #{pos} {r['title']}")
            try:
                download_one(f"https://www.youtube.com/watch?v={r['id']}", str(dest / (stem + ".%(ext)s")),
                             s["max_height"], cookies_opts)
                got = find_downloaded(dest, stem)
                if got is None:
                    raise RuntimeError("finished but no file was found")
            except KeyboardInterrupt:
                raise
            except Exception as e:
                failed.append(pos)
                print(f"   skipped ({str(e).splitlines()[0][:150] if str(e) else 'download failed'})")
                clean_partials(dest, stem)
                continue
            files.append(got)
            archive[r["id"]] = {"file": got.name, "title": r["title"], "views": r["views"],
                                "channel": s["channel"], "run": stamp}
            mc.save_json(ARCHIVE_FILE, archive)
    except KeyboardInterrupt:
        clean_partials(dest, stamp)
        print("\n\nStopped. Clips downloaded so far are kept. To turn them into compilations later, run:")
        print(f"   python make_compilations.py all --clips \"{dest}\" --order name")
        return

    print(f"\nDownloaded {mc.plural(len(files), 'video')} in {mc.fmt_duration(time.time() - started)}"
          + (f" (positions {', '.join('#' + str(p) for p in failed)} could not be downloaded)." if failed else "."))
    if failed:
        print("  If many fail, update yt-dlp first:  pip install -U yt-dlp")
    if args.no_compile:
        print(f"Saved in: {dest}")
        return
    if not files:
        return                          # nothing new arrived (compilations only start from new downloads)

    # ---- compile exactly these clips, in the order they were chosen
    print("\nMaking compilations...")
    cmd = [sys.executable, str(MAKE), "all", "--clips", str(dest), "--order", "name"]
    if s["play"] == "reverse":
        cmd.append("--reverse")
    elif s["play"] == "each":
        cmd.append("--reverse-each")
    if args.include_leftover:
        cmd.append("--include-leftover")
    rc = subprocess.run(cmd).returncode

    if s["delete_after"] and rc == 0:
        used = set()
        for e in mc.load_json(COMPILE_LEDGER, {}).get("compilations", {}).values():
            used.update(e["clips"])
        removed = 0
        for f in files:
            if f.exists() and f.relative_to(dest).as_posix() in used:
                f.unlink()
                removed += 1
        if removed:
            print(f"Deleted {mc.plural(removed, 'downloaded clip')} that are now inside compilations.")


def fmt_views(n):
    return yt_toolkit.fmt_count(n)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled")
