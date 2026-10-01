#!/usr/bin/env python3
"""
yt_toolkit.py — standalone YouTube research toolkit.

A single, self-contained file with NO sibling-script imports: channel
discovery, outlier detection, channel tables, live-stream extraction, tag
extraction, and the two "cheap tier" combo tools (niche report, clip
finder) are all implemented directly in this file. Each tool is namespaced
under its own class (ChannelFinder, OutlierFinder, ChannelTable,
LiveExtractor, TagExtractor, NicheReport, ClipFinder) so identically-named
helpers across the original seven scripts — base_channel_url, print_table,
write_csv, and so on, each defined 2-4 times slightly differently in the
originals — don't collide here.

This does NOT import or shell out to channel_finder.py, outlier_finder.py,
yt_channel_table.py, live_extractor.py, tag_extractor.py, niche_report.py,
or clip_finder.py. Those scripts remain independently runnable, unchanged,
but yt_toolkit.py itself needs only this one file (plus yt-dlp, and
optionally tabulate) to run the entire toolkit — nothing else has to sit
next to it on disk.

Genuinely shared, byte-identical-in-the-originals logic (view/sub-count
formatting, the verified-badge formatter, the tabulate-missing fallback
table printer, cookie-flag handling) is factored into a few small
module-level helpers used by every class. Anything that differed even
slightly between the originals — each tool's own base_channel_url tab-
stripping list, its own print_table columns, its own write_csv fields —
is kept as that tool's own method rather than forced into a shared one, so
behavior matches each original exactly.

Install:
    pip install yt-dlp tabulate

Usage:
    python yt_toolkit.py                                       # interactive menu
    python yt_toolkit.py channels --search "lofi hip hop"
    python yt_toolkit.py outliers "@Channel1" "@Channel2" --type shorts
    python yt_toolkit.py table "@SomeChannel" --sort popular
    python yt_toolkit.py live "@LofiGirl"
    python yt_toolkit.py tags --csv outliers.csv --top 30
    python yt_toolkit.py niche --search "lofi hip hop" --top 20
    python yt_toolkit.py clip --search "reddit stories" --max-age-days 3
    python yt_toolkit.py <tool> --help                          # that tool's own flags
"""

import argparse
import csv
import json
import re
import shlex
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone

try:
    import yt_dlp
except ImportError:
    sys.exit("Missing dependency. Run: pip install yt-dlp")

try:
    from tabulate import tabulate
    HAVE_TABULATE = True
except ImportError:
    HAVE_TABULATE = False


# --------------------------------------------------------------------------
# Shared helpers — only for pieces that were genuinely byte-identical
# across the original scripts. Everything else stays per-class.
# --------------------------------------------------------------------------

def build_cookies_opts(args):
    """--cookies-from-browser / --cookies pass-through to yt-dlp. Identical
    across all seven originals."""
    cookies_opts = {}
    if getattr(args, "cookies_from_browser", None):
        cookies_opts["cookiesfrombrowser"] = (args.cookies_from_browser,)
    if getattr(args, "cookies", None):
        cookies_opts["cookiefile"] = args.cookies
    return cookies_opts


def add_cookie_arguments(parser):
    parser.add_argument("--cookies-from-browser", metavar="BROWSER",
                         help="Pass-through to yt-dlp, e.g. firefox, chrome")
    parser.add_argument("--cookies", metavar="FILE", help="Pass-through to yt-dlp: a cookies.txt file")


def fmt_count(n):
    """M/K count formatter — byte-identical across channel_finder
    (fmt_count), outlier_finder and yt_channel_table (both fmt_views) in
    the originals, so it's shared here under one name."""
    if n is None:
        return "N/A"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def fmt_verified(v):
    """Byte-identical across channel_finder and outlier_finder."""
    if v is None:
        return "N/A"
    return "Yes" if v else "No"


def print_plain_table(rows, headers):
    """The tabulate-missing fallback every original script repeated
    inline, identically. Callers handle their own empty-result messaging
    before calling this — it assumes rows is non-empty."""
    widths = [max(len(str(h)), *(len(str(row[i])) for row in rows)) for i, h in enumerate(headers)]
    print(" | ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(" | ".join(str(c).ljust(w) for c, w in zip(row, widths)))


# --------------------------------------------------------------------------
# channels — channel_finder.py
# --------------------------------------------------------------------------

class ChannelFinder:
    """channels: auto-discover candidate channels via a YouTube keyword
    --search and/or a seed channel's --seed 'Channels' tab."""

    TABS = ("videos", "shorts", "streams", "channels", "featured", "about")

    @classmethod
    def base_channel_url(cls, channel):
        channel = channel.strip().rstrip("/")
        if channel.startswith("@"):
            url = f"https://www.youtube.com/{channel}"
        elif channel.startswith("http"):
            url = channel
        else:
            url = f"https://www.youtube.com/{channel}"
        for tab in cls.TABS:
            suffix = f"/{tab}"
            if url.endswith(suffix):
                url = url[: -len(suffix)]
                break
        return url

    @staticmethod
    def add_found(all_found, new_found):
        for key, info in new_found.items():
            if key in all_found:
                if info["source"] not in all_found[key]["source"]:
                    all_found[key]["source"] += f", {info['source']}"
            else:
                all_found[key] = info

    @staticmethod
    def search_channels(query, count, cookies_opts):
        ydl_opts = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "ignoreerrors": True, "extract_flat": True, **cookies_opts,
        }
        url = f"ytsearch{count}:{query}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
            except Exception as e:
                print(f"  (search failed: {e})", file=sys.stderr)
                return {}

        found = {}
        for e in (info or {}).get("entries", []) or []:
            if not e:
                continue
            cid = e.get("channel_id")
            name = e.get("channel") or e.get("uploader")
            chan_url = e.get("channel_url") or (f"https://www.youtube.com/channel/{cid}" if cid else None)
            if not chan_url or not name:
                continue
            key = cid or chan_url
            found.setdefault(key, {"name": name, "url": chan_url, "subs": None, "verified": None,
                                    "source": f'search:"{query}"'})
        return found

    @classmethod
    def seed_related_channels(cls, seed, cookies_opts):
        ydl_opts = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "ignoreerrors": True, "extract_flat": True, **cookies_opts,
        }
        base = cls.base_channel_url(seed)
        url = f"{base}/channels"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
            except Exception as e:
                print(f"  (no Channels tab for {seed}: {e})", file=sys.stderr)
                return {}

        found = {}
        for e in (info or {}).get("entries", []) or []:
            if not e:
                continue
            cid = e.get("channel_id") or e.get("id")
            name = e.get("channel") or e.get("title") or e.get("uploader")
            chan_url = e.get("url") or (f"https://www.youtube.com/channel/{cid}" if cid else None)
            if not chan_url or not name:
                continue
            key = cid or chan_url
            found.setdefault(key, {"name": name, "url": chan_url, "subs": None, "verified": None,
                                    "source": f"featured-by:{seed}"})
        return found

    @classmethod
    def fetch_channel_details(cls, channel_url, cookies_opts):
        ydl_opts = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "ignoreerrors": True, "extract_flat": True, "playlist_items": "1:1",
            **cookies_opts,
        }
        url = f"{cls.base_channel_url(channel_url)}/videos"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
            except Exception:
                return None, None
        if not info:
            return None, None
        return info.get("channel_follower_count"), info.get("channel_is_verified")

    @classmethod
    def discover_channels(cls, searches, seeds, count, cookies_opts):
        """The shared discovery step (search + seed, no --with-subs) — used
        by this tool's own CLI, and reused directly by NicheReport /
        ClipFinder instead of a second copy of the same loop."""
        all_found = {}
        for q in searches:
            print(f"Searching: {q}", file=sys.stderr)
            cls.add_found(all_found, cls.search_channels(q, count, cookies_opts))
        for s in seeds:
            print(f"Reading Channels tab: {s}", file=sys.stderr)
            cls.add_found(all_found, cls.seed_related_channels(s, cookies_opts))
        return list(all_found.values())

    @staticmethod
    def print_table(channels, show_subs):
        if show_subs:
            headers = ["Name", "Verified", "Subscribers", "URL", "Found via"]
        else:
            headers = ["Name", "URL", "Found via"]
        rows = []
        for c in channels:
            row = [c["name"]]
            if show_subs:
                row.append(fmt_verified(c["verified"]))
                row.append(fmt_count(c["subs"]))
            row.append(c["url"])
            row.append(c["source"])
            rows.append(row)
        if HAVE_TABULATE:
            print(tabulate(rows, headers=headers, tablefmt="github"))
        else:
            print_plain_table(rows, headers)

    @staticmethod
    def add_arguments(parser):
        parser.add_argument("--search", action="append", default=[], metavar="KEYWORD",
                             help="Search YouTube for this keyword and collect the channels behind "
                                  "the results. Repeatable.")
        parser.add_argument("--count", type=int, default=30,
                             help="Results to pull per --search term (default: 30)")
        parser.add_argument("--seed", action="append", default=[], metavar="CHANNEL",
                             help="Pull channels featured on this channel's 'Channels' tab, if any. "
                                  "Repeatable.")
        parser.add_argument("--with-subs", action="store_true",
                             help="Fetch subscriber count and verified-badge status per channel found "
                                  "(one extra request each)")
        parser.add_argument("--urls-only", action="store_true",
                             help="Print bare channel URLs, one per line, for piping into other tools")
        parser.add_argument("--csv", metavar="FILE",
                             help="Also write results (name, verified, subs, url, source) to CSV")
        add_cookie_arguments(parser)

    @classmethod
    def run(cls, args):
        if not args.search and not args.seed:
            print("Give at least one --search keyword or --seed channel.", file=sys.stderr)
            return 1

        cookies_opts = build_cookies_opts(args)

        all_found = {}
        for q in args.search:
            print(f"Searching: {q}", file=sys.stderr)
            cls.add_found(all_found, cls.search_channels(q, args.count, cookies_opts))
        for s in args.seed:
            print(f"Reading Channels tab: {s}", file=sys.stderr)
            cls.add_found(all_found, cls.seed_related_channels(s, cookies_opts))

        if not all_found:
            print("No channels found.", file=sys.stderr)
            return 1

        if args.with_subs:
            total = len(all_found)
            for i, c in enumerate(all_found.values(), 1):
                print(f"  [{i}/{total}] details for {c['name']}", file=sys.stderr, flush=True)
                c["subs"], c["verified"] = cls.fetch_channel_details(c["url"], cookies_opts)

        channels = list(all_found.values())
        print(f"# {len(channels)} unique channel(s) found", file=sys.stderr)

        if args.urls_only:
            for c in channels:
                print(c["url"])
        else:
            cls.print_table(channels, show_subs=args.with_subs)

        if args.csv:
            with open(args.csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["name", "verified", "subs", "url", "source"])
                writer.writeheader()
                writer.writerows(channels)
            print(f"Saved {len(channels)} rows to {args.csv}", file=sys.stderr)

        return 0


# --------------------------------------------------------------------------
# outliers — outlier_finder.py
# --------------------------------------------------------------------------

class OutlierFinder:
    """outliers: flag videos that sit well above their own channel's
    baseline (median views for that channel/tab)."""

    TABS = ("videos", "shorts", "streams")
    MIN_SAMPLE = 3  # below this, a "median" isn't a meaningful baseline

    SORT_FIELDS = {
        "ratio": lambda r: r["ratio"],
        "views": lambda r: r["views"],
        "subs": lambda r: r["subs"],
        "days_ago": lambda r: r["days_ago"],
        "baseline": lambda r: r["baseline_median"],
        "channel": lambda r: r["channel"].lower(),
        "type": lambda r: r["type"],
    }
    # Default direction per key: True = highest/most-recent first.
    SORT_DEFAULT_DESC = {
        "ratio": True, "views": True, "subs": True,
        "days_ago": False, "baseline": True, "channel": False, "type": False,
    }

    @classmethod
    def base_channel_url(cls, channel):
        channel = channel.strip().rstrip("/")
        if channel.startswith("@"):
            url = f"https://www.youtube.com/{channel}"
        elif channel.startswith("http"):
            url = channel
        else:
            url = f"https://www.youtube.com/{channel}"
        for tab in (*cls.TABS, "featured"):
            suffix = f"/{tab}"
            if url.endswith(suffix):
                url = url[: -len(suffix)]
                break
        return url

    @staticmethod
    def approx_days_ago(entry):
        ts = entry.get("timestamp") or entry.get("release_timestamp")
        if not ts:
            return None
        d = datetime.fromtimestamp(ts, tz=timezone.utc)
        return (datetime.now(timezone.utc) - d).days

    @classmethod
    def fetch_channel_tab(cls, base_url, tab, limit, cookies_opts):
        """One request for the whole tab. Returns (channel_name, subs, verified, rows)."""
        ydl_opts = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "ignoreerrors": True, "extract_flat": True,
            "extractor_args": {"youtubetab": {"approximate_date": [""]}},
            **cookies_opts,
        }
        if limit:
            ydl_opts["playlist_items"] = f"1:{limit}"

        url = f"{base_url}/{tab}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
            except Exception as e:
                print(f"  (skipping {base_url} [{tab}]: {e})", file=sys.stderr)
                return None, None, None, []

        if not info:
            return None, None, None, []

        channel_name = info.get("channel") or info.get("uploader") or base_url
        subs = info.get("channel_follower_count")
        verified = info.get("channel_is_verified")

        rows = []
        for e in info.get("entries", []) or []:
            if not e:
                continue
            views = e.get("view_count")
            if views is None:
                continue  # can't baseline or flag without a view count
            rows.append({
                "title": (e.get("title") or "N/A")[:70],
                "views": views,
                "days_ago": cls.approx_days_ago(e),
                "type": tab,
                "url": f"https://youtu.be/{e.get('id')}" if e.get("id") else "N/A",
            })
        return channel_name, subs, verified, rows

    @staticmethod
    def resolve_missing_dates(results, cookies_opts):
        """Per-video fetch for whatever's still missing days_ago after the
        flat scrape — mainly Shorts, whose tab page never carries a date.
        Runs only on the already-filtered outlier list, not the full scan."""
        missing = [r for r in results if r["days_ago"] is None and r["url"] != "N/A"]
        if not missing:
            return

        ydl_opts = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "ignoreerrors": True, "extract_flat": False,
            "extractor_args": {
                "youtube": {"player_client": ["android", "ios"], "player_skip": ["js", "configs"]},
            },
            "youtube_include_dash_manifest": False,
            "youtube_include_hls_manifest": False,
            **cookies_opts,
        }
        total = len(missing)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            for i, r in enumerate(missing, 1):
                print(f"  [{i}/{total}] resolving date: {r['title'][:50]}", file=sys.stderr, flush=True)
                try:
                    info = ydl.extract_info(r["url"], download=False)
                except Exception as e:
                    print(f"      skipped ({e})", file=sys.stderr, flush=True)
                    continue
                upload_date = (info or {}).get("upload_date")
                if not upload_date:
                    continue
                try:
                    d = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
                    r["days_ago"] = (datetime.now(timezone.utc) - d).days
                except ValueError:
                    pass

    @classmethod
    def find_outliers(cls, channels, content_type, limit, multiplier, cookies_opts):
        tabs = cls.TABS if content_type == "all" else (content_type,)
        results = []

        for channel in channels:
            base_url = cls.base_channel_url(channel)
            found_any = False

            for tab in tabs:
                print(f"Fetching {channel} [{tab}]...", file=sys.stderr)
                name, subs, verified, rows = cls.fetch_channel_tab(base_url, tab, limit, cookies_opts)
                if not rows:
                    continue
                found_any = True

                if len(rows) < cls.MIN_SAMPLE:
                    print(f"  (only {len(rows)} video(s) in [{tab}] — too few for a "
                          f"reliable baseline, skipping)", file=sys.stderr)
                    continue

                baseline = statistics.median(r["views"] for r in rows)
                for r in rows:
                    ratio = r["views"] / baseline if baseline else 0
                    if ratio >= multiplier:
                        results.append({
                            "channel": name or channel,
                            "subs": subs,
                            "verified": verified,
                            "baseline_median": baseline,
                            "ratio": ratio,
                            **r,
                        })

            if not found_any:
                print(f"  (no videos found for {channel})", file=sys.stderr)

        return results

    @staticmethod
    def print_table(results):
        headers = ["Channel", "Verified", "Title", "Views", "Channel median", "x Baseline", "Days ago", "Type"]
        rows = []
        for r in results:
            rows.append([
                r["channel"][:25],
                fmt_verified(r["verified"]),
                r["title"],
                fmt_count(r["views"]),
                fmt_count(int(r["baseline_median"])),
                f"{r['ratio']:.1f}x",
                r["days_ago"] if r["days_ago"] is not None else "N/A",
                r["type"],
            ])
        if HAVE_TABULATE:
            print(tabulate(rows, headers=headers, tablefmt="github"))
        else:
            print_plain_table(rows, headers)

    @staticmethod
    def write_csv(results, path):
        fields = ["channel", "verified", "subs", "title", "views", "baseline_median", "ratio", "days_ago", "type", "url"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for r in results:
                writer.writerow({k: r.get(k) for k in fields})

    @classmethod
    def apply_sort(cls, results, field, reverse):
        """Sort by `field`, honoring `reverse`. Entries with no value for
        the chosen key always sort last, in either direction."""
        get_key = cls.SORT_FIELDS[field]
        with_value = [r for r in results if get_key(r) is not None]
        missing = [r for r in results if get_key(r) is None]
        with_value.sort(key=get_key, reverse=reverse)
        return with_value + missing

    @classmethod
    def group_and_print(cls, results, group_by):
        groups = {}
        order = []
        for r in results:
            key = r[group_by]
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(r)

        for i, key in enumerate(order):
            if i > 0:
                print()
            if group_by == "channel":
                subs = groups[key][0]["subs"]
                label = f"{key} ({fmt_count(subs)} subs)" if subs is not None else f"{key} (subs N/A)"
            else:
                label = key
            print(f"== {label} ==")
            cls.print_table(groups[key])

    @classmethod
    def add_arguments(cls, parser):
        parser.add_argument("channels", nargs="*",
                             help="Channel URLs or @handles (optional if --channels-file is given)")
        parser.add_argument("--channels-file", metavar="FILE",
                             help="Read channel URLs from a file, one per line (blank lines and lines "
                                  "starting with # are skipped). Pairs with 'channels --urls-only'.")
        parser.add_argument("--type", choices=["videos", "shorts", "streams", "all"], default="videos",
                             help="Content tab to scan (default: videos)")
        parser.add_argument("--multiplier", type=float, default=3.0,
                             help="Flag videos at least this many times the channel's median views (default: 3.0)")
        parser.add_argument("--limit", type=int, default=None,
                             help="Max videos per channel/tab to scan (default: all)")
        parser.add_argument("--top", type=int, default=None, help="Only show the top N outliers overall")
        parser.add_argument("--sort", choices=list(cls.SORT_FIELDS), default="ratio",
                             help="Sort key (default: ratio, the outlier score). views/subs/ratio/baseline "
                                  "default to highest-first; days_ago/channel/type default to "
                                  "soonest/A-Z-first. Use --reverse to flip.")
        parser.add_argument("--reverse", action="store_true", help="Reverse the default direction for --sort")
        parser.add_argument("--group-by", choices=["none", "channel", "type"], default="none",
                             help="Print as separate tables per channel or per content type "
                                  "instead of one flat table (default: none)")
        parser.add_argument("--resolve-dates", action="store_true",
                             help="Per-video fetch to fill in real dates for outliers missing one "
                                  "(mainly Shorts, which show N/A by default). Only hits the videos "
                                  "that made it past --multiplier, not the whole scan — and if --top "
                                  "is also set, only the top N that actually get printed (unless "
                                  "--sort days_ago, which needs dates resolved before it can rank).")
        parser.add_argument("--csv", metavar="FILE", help="Also write results to a CSV file")
        add_cookie_arguments(parser)

    @classmethod
    def run(cls, args):
        channels = list(args.channels)
        if args.channels_file:
            with open(args.channels_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        channels.append(line)

        if not channels:
            print("Give at least one channel, or --channels-file.", file=sys.stderr)
            return 1

        cookies_opts = build_cookies_opts(args)

        results = cls.find_outliers(channels, args.type, args.limit, args.multiplier, cookies_opts)
        if not results:
            print(f"No outliers found at {args.multiplier}x baseline. Try a lower --multiplier or "
                  f"check the channel(s).", file=sys.stderr)
            return 1

        # Sorting by days_ago needs real dates to rank correctly, so in that
        # one case resolve against the full outlier list before sorting/
        # --top touch it. For every other sort key, sort + --top run first
        # and the per-video fetch only hits whatever's left afterward —
        # --top N then bounds it to N fetches instead of one per outlier.
        resolve_before_sort = args.resolve_dates and args.sort == "days_ago"
        if resolve_before_sort:
            cls.resolve_missing_dates(results, cookies_opts)

        results = cls.apply_sort(results, args.sort, cls.SORT_DEFAULT_DESC[args.sort] != args.reverse)
        if args.top:
            results = results[: args.top]

        if args.resolve_dates and not resolve_before_sort:
            cls.resolve_missing_dates(results, cookies_opts)

        if args.group_by != "none":
            cls.group_and_print(results, args.group_by)
        else:
            cls.print_table(results)

        if args.csv:
            cls.write_csv(results, args.csv)
            print(f"\nSaved {len(results)} rows to {args.csv}", file=sys.stderr)

        return 0


# --------------------------------------------------------------------------
# table — yt_channel_table.py
# --------------------------------------------------------------------------

class ChannelTable:
    """table: scrape a channel's videos into a table — title, views,
    upload date, days since published, duration."""

    TABS = ("videos", "shorts", "streams")

    @classmethod
    def base_channel_url(cls, channel):
        channel = channel.strip().rstrip("/")
        if channel.startswith("@"):
            url = f"https://www.youtube.com/{channel}"
        elif channel.startswith("http"):
            url = channel
        else:
            url = f"https://www.youtube.com/{channel}"
        for tab in (*cls.TABS, "featured"):
            suffix = f"/{tab}"
            if url.endswith(suffix):
                url = url[: -len(suffix)]
                break
        return url

    @staticmethod
    def fmt_duration(sec):
        if sec is None:
            return "N/A"
        m, s = divmod(int(sec), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    @staticmethod
    def resolve_date(entry):
        """Return (display_date, days_ago, sort_ts, is_approx). Flat
        channel-tab entries only carry an approximate 'timestamp'; full
        per-video entries carry an exact 'upload_date' (YYYYMMDD)."""
        upload_date = entry.get("upload_date")
        if upload_date:
            try:
                d = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
                return d.strftime("%Y-%m-%d"), (datetime.now(timezone.utc) - d).days, d.timestamp(), False
            except ValueError:
                pass

        ts = entry.get("timestamp") or entry.get("release_timestamp")
        if ts:
            d = datetime.fromtimestamp(ts, tz=timezone.utc)
            return d.strftime("%Y-%m-%d") + "~", (datetime.now(timezone.utc) - d).days, ts, True

        return "N/A", "N/A", None, False

    @classmethod
    def build_row(cls, e, tab):
        display_date, days_ago, sort_ts, is_approx = cls.resolve_date(e)
        views = e.get("view_count")
        if views is None:
            views = e.get("concurrent_view_count")
        return {
            "id": e.get("id"),
            "title": (e.get("title") or "N/A")[:70],
            "views": views,
            "upload_date": display_date,
            "days_ago": days_ago,
            "sort_ts": sort_ts,
            "approx": is_approx,
            "duration": e.get("duration"),
            "type": tab,
            "url": f"https://youtu.be/{e.get('id')}" if e.get("id") else "N/A",
        }

    @classmethod
    def fetch_flat(cls, base_url, tab, limit, cookies_opts):
        ydl_opts = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "ignoreerrors": True, "extract_flat": True,
            "extractor_args": {"youtubetab": {"approximate_date": [""]}},
            **cookies_opts,
        }
        if limit:
            ydl_opts["playlist_items"] = f"1:{limit}"
        url = f"{base_url}/{tab}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
            except Exception as e:
                print(f"  (skipping {tab}: {e})", file=sys.stderr)
                return []

        entries = info.get("entries", []) or [] if info else []
        return [cls.build_row(e, tab) for e in entries if e]

    @classmethod
    def full_resolve(cls, rows, cookies_opts):
        """Re-fetch each row's own video page for exact upload_date/duration."""
        ydl_opts = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "ignoreerrors": True, "socket_timeout": 20, "extract_flat": False,
            "extractor_args": {
                "youtube": {"player_client": ["android", "ios"], "player_skip": ["js", "configs"]},
            },
            "youtube_include_dash_manifest": False,
            "youtube_include_hls_manifest": False,
            **cookies_opts,
        }
        total = len(rows)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            for i, r in enumerate(rows, 1):
                print(f"  [{i}/{total}] {r['title'][:55]}", file=sys.stderr, flush=True)
                if not r["id"]:
                    continue
                try:
                    info = ydl.extract_info(f"https://youtu.be/{r['id']}", download=False)
                except Exception as e:
                    print(f"      skipped ({e})", file=sys.stderr, flush=True)
                    continue
                if not info:
                    continue
                display_date, days_ago, sort_ts, is_approx = cls.resolve_date(info)
                if sort_ts is not None:
                    r["upload_date"], r["days_ago"], r["sort_ts"], r["approx"] = (
                        display_date, days_ago, sort_ts, is_approx,
                    )
                if info.get("duration") is not None:
                    r["duration"] = info["duration"]
                if info.get("view_count") is not None:
                    r["views"] = info["view_count"]

    @classmethod
    def fetch_channel_videos(cls, channel, content_type, limit, cookies_opts):
        base_url = cls.base_channel_url(channel)
        tabs = cls.TABS if content_type == "all" else (content_type,)

        seen_ids = set()
        rows = []
        for tab in tabs:
            print(f"Fetching {tab}: {base_url}/{tab}", file=sys.stderr)
            for r in cls.fetch_flat(base_url, tab, limit, cookies_opts):
                if r["id"] and r["id"] in seen_ids:
                    continue
                seen_ids.add(r["id"])
                rows.append(r)

        return rows

    @staticmethod
    def sort_rows(rows, sort):
        if sort == "popular":
            return sorted(rows, key=lambda r: r["views"] if r["views"] is not None else -1, reverse=True)
        if sort == "oldest":
            return sorted(rows, key=lambda r: r["sort_ts"] if r["sort_ts"] is not None else float("inf"))
        return sorted(rows, key=lambda r: r["sort_ts"] if r["sort_ts"] is not None else float("-inf"), reverse=True)

    @classmethod
    def print_table(cls, rows, show_type):
        headers = ["Title", "Views", "Uploaded", "Days ago", "Duration"]
        if show_type:
            headers.append("Type")

        table_rows = []
        any_approx = False
        for r in rows:
            any_approx = any_approx or r["approx"]
            row = [r["title"], fmt_count(r["views"]), r["upload_date"], r["days_ago"], cls.fmt_duration(r["duration"])]
            if show_type:
                row.append(r["type"])
            table_rows.append(row)

        if HAVE_TABULATE:
            print(tabulate(table_rows, headers=headers, tablefmt="github"))
        else:
            print_plain_table(table_rows, headers)

        if any_approx:
            print("\n~ = approximate date, parsed from the channel page's relative time text "
                  "(e.g. \"3 weeks ago\"). Pass --full for exact dates.", file=sys.stderr)

    @staticmethod
    def write_csv(rows, path):
        fields = ["title", "type", "views", "upload_date", "days_ago", "approx", "duration", "url"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for r in rows:
                writer.writerow({k: r[k] for k in fields})

    @staticmethod
    def add_arguments(parser):
        parser.add_argument("channel", help="Channel URL or @handle")
        parser.add_argument("--type", choices=["videos", "shorts", "streams", "all"], default="videos",
                             help="Content tab to pull from (default: videos)")
        parser.add_argument("--sort", choices=["latest", "oldest", "popular"], default="latest",
                             help="Sort order (default: latest)")
        parser.add_argument("--limit", type=int, default=None, help="Max videos per tab (default: all)")
        parser.add_argument("--csv", metavar="FILE", help="Also write results to a CSV file")
        parser.add_argument("--full", action="store_true",
                             help="Fully resolve each video for exact upload date and duration "
                                  "(needed for Shorts, which don't expose either on the channel tab). "
                                  "Slower — one request per video — but doesn't need a JS runtime.")
        add_cookie_arguments(parser)

    @classmethod
    def run(cls, args):
        cookies_opts = build_cookies_opts(args)

        # "popular"/"oldest" have no equivalent pre-sorted feed on YouTube's
        # side, so the whole tab has to be walked and sorted here, THEN cut
        # to --limit — capping the fetch itself would grab an arbitrary
        # recent window and sort just that, silently producing the wrong
        # top-N.
        fetch_limit = args.limit if args.sort == "latest" else None
        if fetch_limit is None and args.limit:
            print(f"--sort {args.sort} needs to scan the whole {args.type} list before it can "
                  f"pick the top {args.limit} — YouTube doesn't serve a pre-sorted feed for this. "
                  f"May take a while on a channel with a lot of content.", file=sys.stderr)

        rows = cls.fetch_channel_videos(args.channel, args.type, fetch_limit, cookies_opts)
        if not rows:
            print("No videos found — check the channel URL/handle.", file=sys.stderr)
            return 1

        rows = cls.sort_rows(rows, args.sort)
        if args.limit:
            rows = rows[: args.limit]

        if args.full:
            print(f"Resolving exact dates/durations for {len(rows)} video(s)...", file=sys.stderr)
            cls.full_resolve(rows, cookies_opts)

        cls.print_table(rows, show_type=(args.type == "all"))

        if args.csv:
            cls.write_csv(rows, args.csv)
            print(f"\nSaved {len(rows)} rows to {args.csv}", file=sys.stderr)

        return 0


# --------------------------------------------------------------------------
# live — live_extractor.py
# --------------------------------------------------------------------------

class LiveExtractor:
    """live: extract only currently-live streams from one or more
    channels, one JSON file per channel."""

    TABS = ("videos", "shorts", "streams", "live", "featured", "about", "channels")

    @classmethod
    def base_channel_url(cls, channel):
        channel = channel.strip().rstrip("/")
        if channel.startswith("@"):
            url = f"https://www.youtube.com/{channel}"
        elif channel.startswith("http"):
            url = channel
        else:
            url = f"https://www.youtube.com/{channel}"
        for tab in cls.TABS:
            suffix = f"/{tab}"
            if url.endswith(suffix):
                url = url[: -len(suffix)]
                break
        return url

    @classmethod
    def normalize_channel_url(cls, channel):
        raw = channel.strip().rstrip("/")
        if "/watch" in raw or "/playlist" in raw:
            return raw
        tab = "live" if raw.endswith("/live") else "streams"
        return cls.base_channel_url(raw) + f"/{tab}"

    @staticmethod
    def video_url(entry):
        if entry.get("webpage_url"):
            return entry["webpage_url"]
        if entry.get("id"):
            return f"https://www.youtube.com/watch?v={entry['id']}"
        return ""

    @classmethod
    def extract_current_lives(cls, channel_url, limit, cookies_opts):
        flat_opts = {
            "quiet": True, "no_warnings": True, "ignoreerrors": True, "skip_download": True,
            "extract_flat": "in_playlist", **cookies_opts,
        }
        if limit:
            flat_opts["playlist_items"] = f"1:{limit}"

        with yt_dlp.YoutubeDL(flat_opts) as flat:
            try:
                playlist = flat.extract_info(channel_url, download=False)
            except Exception as e:
                print(f"  (skipping {channel_url}: {e})", file=sys.stderr)
                return []

        entries = [e for e in (playlist or {}).get("entries") or [] if isinstance(e, dict)]
        if not entries:
            return []

        full_opts = {
            "quiet": True, "no_warnings": True, "ignoreerrors": True, "skip_download": True,
            "extractor_args": {
                "youtube": {"player_client": ["android", "ios"], "player_skip": ["js", "configs"]},
            },
            "youtube_include_dash_manifest": False,
            "youtube_include_hls_manifest": False,
            **cookies_opts,
        }

        results = []
        seen = set()
        total = len(entries)
        with yt_dlp.YoutubeDL(full_opts) as full_ydl:
            for i, entry in enumerate(entries, 1):
                url = cls.video_url(entry)
                if not url:
                    continue

                title_hint = (entry.get("title") or url)[:50]
                print(f"  [{i}/{total}] checking: {title_hint}", file=sys.stderr, flush=True)
                try:
                    info = full_ydl.extract_info(url, download=False)
                except Exception as e:
                    print(f"      skipped ({e})", file=sys.stderr, flush=True)
                    continue

                if not isinstance(info, dict):
                    continue
                if info.get("live_status") != "is_live" or info.get("is_live") is not True:
                    continue

                live_url = info.get("webpage_url") or url
                if live_url in seen:
                    continue
                seen.add(live_url)

                results.append({
                    "title": info.get("title", "(untitled)"),
                    "channel": info.get("channel") or info.get("uploader") or "Unknown",
                    "url": live_url,
                    "concurrent_view_count": info.get("concurrent_view_count"),
                    "view_count": info.get("view_count"),
                })

        return results

    @staticmethod
    def output_filename(channel_url):
        parts = channel_url.rstrip("/").split("/")
        name = parts[-2] if parts[-1] in ("streams", "live") else parts[-1]
        if name.startswith("@"):
            name = name[1:]
        if not name:
            name = "channel"
        invalid = '<>:"/\\|?*'
        return "".join("_" if c in invalid else c for c in name) + ".json"

    @staticmethod
    def add_arguments(parser):
        parser.add_argument("channels", nargs="+", help="One or more channel URLs or @handles.")
        parser.add_argument("--limit", type=int, default=15,
                             help="Max entries to check per channel's streams tab (default: 15). "
                                  "Live/upcoming streams are listed before past ones, so this rarely "
                                  "needs to be large. Pass 0 to scan the whole tab.")
        add_cookie_arguments(parser)

    @classmethod
    def run(cls, args):
        cookies_opts = build_cookies_opts(args)
        any_live = False

        for channel in args.channels:
            channel_url = cls.normalize_channel_url(channel)
            print(f"Checking {channel_url} ...", file=sys.stderr)

            try:
                lives = cls.extract_current_lives(channel_url, args.limit, cookies_opts)
            except Exception as e:
                print(f"  (failed: {e})", file=sys.stderr)
                lives = []

            output = {"channel_url": channel_url, "live_count": len(lives), "streams": lives}

            filename = cls.output_filename(channel_url)
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(output, f, indent=2, ensure_ascii=False)

            print(f"Wrote {filename}")

            if lives:
                any_live = True

        return 0 if any_live else 1


# --------------------------------------------------------------------------
# tags — tag_extractor.py
# --------------------------------------------------------------------------

class TagExtractor:
    """tags: extract tag/title-keyword frequency across a set of videos."""

    STOPWORDS = {
        "the", "a", "an", "and", "or", "but", "of", "in", "on", "at", "to", "for",
        "with", "by", "from", "up", "about", "into", "over", "after", "is", "are",
        "was", "were", "be", "been", "being", "this", "that", "these", "those",
        "it", "its", "as", "vs", "you", "your", "i", "my", "we", "our", "he",
        "she", "they", "them", "his", "her", "their", "not", "no", "so", "if",
        "than", "then", "how", "what", "when", "why", "who", "which", "do",
        "does", "did", "will", "would", "can", "could", "should", "just", "out",
        "off", "all", "new", "part", "video", "official",
    }

    @staticmethod
    def load_urls_from_csv(path):
        urls = []
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if "url" not in (reader.fieldnames or []):
                raise ValueError(f"{path}: no 'url' column found (expected an 'outliers --csv' file)")
            for row in reader:
                u = (row.get("url") or "").strip()
                if u and u != "N/A":
                    urls.append(u)
        return urls

    @staticmethod
    def load_urls_from_file(path):
        urls = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
        return urls

    @classmethod
    def tokenize_title(cls, title, min_len):
        words = re.findall(r"[a-zA-Z0-9']+", title.lower())
        return [w for w in words if len(w) >= min_len and w not in cls.STOPWORDS and not w.isdigit()]

    @staticmethod
    def fetch_tags_and_title(url, ydl):
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:
            print(f"      skipped ({e})", file=sys.stderr, flush=True)
            return None, None
        if not info:
            return None, None
        return info.get("tags") or [], info.get("title") or ""

    @classmethod
    def analyze(cls, urls, cookies_opts, min_word_len):
        ydl_opts = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "ignoreerrors": True, "extract_flat": False,
            "extractor_args": {
                "youtube": {"player_client": ["android", "ios"], "player_skip": ["js", "configs"]},
            },
            "youtube_include_dash_manifest": False,
            "youtube_include_hls_manifest": False,
            **cookies_opts,
        }

        tag_counter = Counter()
        word_counter = Counter()
        videos_with_tags = 0
        videos_total = 0

        total = len(urls)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            for i, url in enumerate(urls, 1):
                print(f"  [{i}/{total}] fetching tags: {url}", file=sys.stderr, flush=True)
                tags, title = cls.fetch_tags_and_title(url, ydl)
                if tags is None and title is None:
                    continue
                videos_total += 1
                if tags:
                    videos_with_tags += 1
                    seen_this_video = set()
                    for t in tags:
                        t_norm = t.strip().lower()
                        if not t_norm or t_norm in seen_this_video:
                            continue
                        seen_this_video.add(t_norm)
                        tag_counter[t_norm] += 1
                if title:
                    for w in cls.tokenize_title(title, min_word_len):
                        word_counter[w] += 1

        return tag_counter, word_counter, videos_with_tags, videos_total

    @staticmethod
    def print_freq_table(counter, top, label):
        print(f"\n== Top {label} ==")
        if not counter:
            print(f"(none found)")
            return
        rows = counter.most_common(top)
        headers = [label[:-1].capitalize() if label.endswith("s") else label.capitalize(), "Count"]
        if HAVE_TABULATE:
            print(tabulate(rows, headers=headers, tablefmt="github"))
        else:
            print_plain_table(rows, headers)

    @staticmethod
    def write_csv(tag_counter, word_counter, path, top):
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["kind", "value", "count"])
            for tag, count in tag_counter.most_common(top):
                writer.writerow(["tag", tag, count])
            for word, count in word_counter.most_common(top):
                writer.writerow(["title_word", word, count])

    @staticmethod
    def add_arguments(parser):
        parser.add_argument("urls", nargs="*", help="Video URLs to analyze")
        parser.add_argument("--csv", dest="csv_in", metavar="FILE",
                             help="Read video URLs from an 'outliers --csv' file (its 'url' column)")
        parser.add_argument("--urls-file", metavar="FILE",
                             help="Read video URLs from a plain text file, one per line")
        parser.add_argument("--top", type=int, default=25, help="Show top N tags/words (default: 25)")
        parser.add_argument("--min-word-len", type=int, default=3,
                             help="Minimum title word length to count (default: 3)")
        parser.add_argument("--out-csv", metavar="FILE", help="Also write tag/word counts to a CSV file")
        add_cookie_arguments(parser)

    @classmethod
    def run(cls, args):
        urls = list(args.urls)
        if args.csv_in:
            try:
                urls.extend(cls.load_urls_from_csv(args.csv_in))
            except (OSError, ValueError) as e:
                print(str(e), file=sys.stderr)
                return 1
        if args.urls_file:
            try:
                urls.extend(cls.load_urls_from_file(args.urls_file))
            except OSError as e:
                print(str(e), file=sys.stderr)
                return 1

        seen = set()
        urls = [u for u in urls if not (u in seen or seen.add(u))]

        if not urls:
            print("Give video URLs, or --csv/--urls-file to read them from.", file=sys.stderr)
            return 1

        cookies_opts = build_cookies_opts(args)

        print(f"Analyzing {len(urls)} video(s) (one fetch each — tags aren't exposed anywhere flatter)...",
              file=sys.stderr)
        tag_counter, word_counter, with_tags, total = cls.analyze(urls, cookies_opts, args.min_word_len)

        if total == 0:
            print("Couldn't fetch any of the given videos.", file=sys.stderr)
            return 1

        print(f"\n{with_tags}/{total} video(s) had at least one tag "
              f"({with_tags/total:.0%} tag coverage).", file=sys.stderr)

        cls.print_freq_table(tag_counter, args.top, "tags")
        cls.print_freq_table(word_counter, args.top, "title words")

        if args.out_csv:
            cls.write_csv(tag_counter, word_counter, args.out_csv, args.top)
            print(f"\nSaved to {args.out_csv}", file=sys.stderr)

        return 0


# --------------------------------------------------------------------------
# niche — niche_report.py
# --------------------------------------------------------------------------

class NicheReport:
    """niche: discover the channels behind a --search/--seed, then run
    OutlierFinder's analysis across all of them in one pass."""

    @staticmethod
    def discover(searches, seeds, count, cookies_opts):
        """Thin wrapper around ChannelFinder.discover_channels — kept as
        its own name/entry point since ClipFinder refers to 'niche's
        discovery step' the same way the original clip_finder.py did."""
        return ChannelFinder.discover_channels(searches, seeds, count, cookies_opts)

    @staticmethod
    def run_outlier_pass(channel_urls, args, cookies_opts):
        """Returns None (and prints why) on no results, instead of exiting
        the process, so interactive mode can reprompt instead of dying."""
        results = OutlierFinder.find_outliers(channel_urls, args.type, args.limit, args.multiplier, cookies_opts)
        if not results:
            print(f"No outliers found at {args.multiplier}x baseline across {len(channel_urls)} channel(s). "
                  f"Try a lower --multiplier.", file=sys.stderr)
            return None
        return results

    @staticmethod
    def add_arguments(parser):
        parser.add_argument("--search", action="append", default=[], metavar="KEYWORD",
                             help="Search YouTube for this keyword and analyze the channels behind the "
                                  "results. Repeatable.")
        parser.add_argument("--seed", action="append", default=[], metavar="CHANNEL",
                             help="Analyze channels featured on this channel's 'Channels' tab, if any. "
                                  "Repeatable.")
        parser.add_argument("--count", type=int, default=30,
                             help="Results to pull per --search term (default: 30)")
        parser.add_argument("--type", choices=["videos", "shorts", "streams", "all"], default="videos",
                             help="Content tab to scan per discovered channel (default: videos)")
        parser.add_argument("--multiplier", type=float, default=3.0,
                             help="Flag videos at least this many times the channel's median views (default: 3.0)")
        parser.add_argument("--limit", type=int, default=None,
                             help="Max videos per channel/tab to scan (default: all)")
        parser.add_argument("--top", type=int, default=None, help="Only show the top N outliers overall")
        parser.add_argument("--sort", choices=list(OutlierFinder.SORT_FIELDS), default="ratio",
                             help="Sort key (default: ratio)")
        parser.add_argument("--reverse", action="store_true", help="Reverse the default sort direction")
        parser.add_argument("--group-by", choices=["none", "channel", "type"], default="none",
                             help="Print as separate tables per channel or per content type (default: none)")
        parser.add_argument("--resolve-dates", action="store_true",
                             help="Per-video fetch to fill in real dates for outliers missing one "
                                  "(bounded the same way as 'outliers': to --top when possible, "
                                  "to the whole list when --sort days_ago needs them first)")
        parser.add_argument("--csv", metavar="FILE", help="Also write results to a CSV file")
        add_cookie_arguments(parser)

    @classmethod
    def run(cls, args):
        if not args.search and not args.seed:
            print("Give at least one --search keyword or --seed channel.", file=sys.stderr)
            return 1

        cookies_opts = build_cookies_opts(args)

        channels = cls.discover(args.search, args.seed, args.count, cookies_opts)
        if not channels:
            print("No channels found for the given --search/--seed.", file=sys.stderr)
            return 1
        print(f"# {len(channels)} unique channel(s) discovered — scanning for outliers...\n", file=sys.stderr)

        channel_urls = [c["url"] for c in channels]
        results = cls.run_outlier_pass(channel_urls, args, cookies_opts)
        if results is None:
            return 1

        resolve_before_sort = args.resolve_dates and args.sort == "days_ago"
        if resolve_before_sort:
            OutlierFinder.resolve_missing_dates(results, cookies_opts)

        results = OutlierFinder.apply_sort(
            results, args.sort, OutlierFinder.SORT_DEFAULT_DESC[args.sort] != args.reverse
        )
        if args.top:
            results = results[: args.top]

        if args.resolve_dates and not resolve_before_sort:
            OutlierFinder.resolve_missing_dates(results, cookies_opts)

        if args.group_by != "none":
            OutlierFinder.group_and_print(results, args.group_by)
        else:
            OutlierFinder.print_table(results)

        if args.csv:
            OutlierFinder.write_csv(results, args.csv)
            print(f"\nSaved {len(results)} rows to {args.csv}", file=sys.stderr)

        return 0


# --------------------------------------------------------------------------
# clip — clip_finder.py
# --------------------------------------------------------------------------

class ClipFinder:
    """clip: same discovery + outlier engine as niche, tuned for sourcing
    fresh clips — always resolves real dates for whatever clears the
    outlier filter, then drops anything older than --max-age-days (or
    with an unresolvable date)."""

    @staticmethod
    def add_arguments(parser):
        parser.add_argument("--search", action="append", default=[], metavar="KEYWORD",
                             help="Search YouTube for this keyword. Repeatable.")
        parser.add_argument("--seed", action="append", default=[], metavar="CHANNEL",
                             help="Pull channels featured on this channel's 'Channels' tab. Repeatable.")
        parser.add_argument("--count", type=int, default=30, help="Results per --search term (default: 30)")
        parser.add_argument("--type", choices=["videos", "shorts", "streams", "all"], default="all",
                             help="Content tab to scan (default: all — clips can be Shorts or long-form)")
        parser.add_argument("--multiplier", type=float, default=3.0,
                             help="Flag videos at least this many times the channel's median views (default: 3.0)")
        parser.add_argument("--max-age-days", type=int, default=7,
                             help="Drop outliers older than this many days (default: 7) — freshness is "
                                  "the whole point for a repost/remix workflow")
        parser.add_argument("--limit", type=int, default=None, help="Max videos per channel/tab to scan")
        parser.add_argument("--top", type=int, default=20, help="Show top N fresh outliers (default: 20)")
        parser.add_argument("--csv", metavar="FILE", help="Also write results to a CSV file")
        add_cookie_arguments(parser)

    @classmethod
    def run(cls, args):
        if not args.search and not args.seed:
            print("Give at least one --search keyword or --seed channel.", file=sys.stderr)
            return 1

        cookies_opts = build_cookies_opts(args)

        channels = NicheReport.discover(args.search, args.seed, args.count, cookies_opts)
        if not channels:
            print("No channels found for the given --search/--seed.", file=sys.stderr)
            return 1
        print(f"# {len(channels)} unique channel(s) discovered — scanning for outliers...\n", file=sys.stderr)

        channel_urls = [c["url"] for c in channels]
        results = OutlierFinder.find_outliers(channel_urls, args.type, args.limit, args.multiplier, cookies_opts)
        if not results:
            print(f"No outliers found at {args.multiplier}x baseline across {len(channels)} channel(s).",
                  file=sys.stderr)
            return 1

        print(f"Resolving real dates for {len(results)} candidate(s) (freshness needs exact dates, "
              f"not the approximate tab-page text)...", file=sys.stderr)
        OutlierFinder.resolve_missing_dates(results, cookies_opts)

        fresh = [r for r in results if r["days_ago"] is not None and r["days_ago"] <= args.max_age_days]
        dropped = len(results) - len(fresh)
        if dropped:
            print(f"Dropped {dropped} outlier(s) older than {args.max_age_days} day(s) "
                  f"or with an unresolvable date.", file=sys.stderr)
        if not fresh:
            print(f"No outliers within {args.max_age_days} day(s). Try a larger --max-age-days.",
                  file=sys.stderr)
            return 1

        fresh = OutlierFinder.apply_sort(fresh, "ratio", True)
        if args.top:
            fresh = fresh[: args.top]

        OutlierFinder.print_table(fresh)

        if args.csv:
            OutlierFinder.write_csv(fresh, args.csv)
            print(f"\nSaved {len(fresh)} rows to {args.csv}", file=sys.stderr)

        return 0


# --------------------------------------------------------------------------
# CLI dispatch — direct subcommands + interactive menu mode
# --------------------------------------------------------------------------

TOOLS = {
    "channels": (ChannelFinder, "Auto-discover candidate channels (search + a seed's Channels tab)"),
    "outliers": (OutlierFinder, "Flag videos that outperform their own channel's baseline"),
    "table": (ChannelTable, "Scrape a channel's videos into a table"),
    "live": (LiveExtractor, "Extract currently-live streams from one or more channels"),
    "tags": (TagExtractor, "Extract tag/title-keyword frequency across a set of videos"),
    "niche": (NicheReport, "Discover a niche's channels, then run outlier analysis across all of them"),
    "clip": (ClipFinder, "Find fresh, currently-outperforming clips worth remixing"),
}


class ToolArgumentError(Exception):
    """Raised by ToolArgumentParser instead of the process exiting, so the
    interactive menu can reprompt within the same tool rather than dying."""


class ToolArgumentParser(argparse.ArgumentParser):
    """argparse.ArgumentParser that raises instead of calling sys.exit, so
    a bad flag (or --help) in interactive mode reprompts within the same
    tool instead of killing the whole toolkit."""

    def error(self, message):
        self.print_usage(sys.stderr)
        raise ToolArgumentError(message)

    def exit(self, status=0, message=None):
        if message:
            self._print_message(message, sys.stderr if status else sys.stdout)
        raise ToolArgumentError(None)


def build_dispatch_parser():
    parser = argparse.ArgumentParser(
        prog="yt_toolkit.py",
        description="Standalone YouTube research toolkit — channel/outlier discovery, channel "
                     "tables, live-stream extraction, tag extraction, and the niche/clip combo "
                     "tools, all implemented in this one file. Run with no arguments for an "
                     "interactive menu.",
    )
    sub = parser.add_subparsers(dest="tool", metavar="TOOL")
    for name, (cls, desc) in TOOLS.items():
        p = sub.add_parser(name, help=desc, description=desc)
        cls.add_arguments(p)
    return parser


def interactive_menu():
    names = list(TOOLS.keys())
    print("YouTube Toolkit — interactive mode\n")
    while True:
        print("Tools:")
        for i, name in enumerate(names, 1):
            print(f"  {i}. {name:<9} {TOOLS[name][1]}")
        print("  q. quit")
        try:
            choice = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if choice.lower() in ("q", "quit", "exit"):
            print("Bye.")
            return 0
        if not choice:
            print()
            continue

        selected = None
        if choice.isdigit() and 1 <= int(choice) <= len(names):
            selected = names[int(choice) - 1]
        elif choice in TOOLS:
            selected = choice
        else:
            print(f"Unrecognized choice: {choice!r}\n")
            continue

        print()
        outcome = interactive_tool_loop(selected)
        if outcome == "quit":
            print("Bye.")
            return 0
        print()


def interactive_tool_loop(name):
    """Runs one tool's own prompt loop. Returns 'quit' if the user asked to
    exit the whole toolkit, or None to fall back to the main menu."""
    cls, desc = TOOLS[name]
    print(f"-- {name}: {desc} --")
    print("Type this tool's flags as you would on the command line "
          "(e.g. --search \"lofi hip hop\"), '--help' for its full help, "
          "'cancel' to return to the menu, or 'quit' to exit.\n")
    while True:
        try:
            line = input(f"{name}> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return "quit"

        if line.lower() in ("cancel", "back", "c"):
            return None
        if line.lower() in ("quit", "exit", "q"):
            return "quit"
        if not line:
            continue

        try:
            tokens = shlex.split(line)
        except ValueError as e:
            print(f"Couldn't parse that: {e}\n")
            continue

        parser = ToolArgumentParser(prog=name, description=desc)
        cls.add_arguments(parser)
        try:
            args = parser.parse_args(tokens)
        except ToolArgumentError as e:
            if e.args and e.args[0]:
                print(f"error: {e.args[0]}")
            print()
            continue

        try:
            cls.run(args)
        except KeyboardInterrupt:
            print("\nInterrupted.")
        except Exception as e:
            print(f"Unexpected error: {e}")
        print()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return interactive_menu()

    parser = build_dispatch_parser()
    args = parser.parse_args(argv)
    if not args.tool:
        parser.print_help()
        return 1

    cls = TOOLS[args.tool][0]
    try:
        return cls.run(args) or 0
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
