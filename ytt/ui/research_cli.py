"""`ytt research ...`: look things up on YouTube. Needs no workspace and never changes anything. Every tool prints a
table by default, or --json (everything, with the video ids), or --ids (just the ids, one per line, which
`ytt make --videos -` and `ytt fetch --videos -` read). --csv also saves the rows to a file."""
import csv
import json
import sys
from pathlib import Path

from ytt.ops.research import channels as channels_mod
from ytt.ops.research import outliers as outliers_mod
from ytt.ops.research import table as table_mod
from ytt.ops.research.common import parse_channel_list, utc_now
from ytt.sources.ytdlp import YtDlpBackend
from ytt.ui.render import count, table, yes_no
from ytt.workspace import config as cfgmod
from ytt.workspace import paths
from ytt.workspace.errors import WorkspaceError

TOOLS = {
    "channels": "find channels by search or from another channel's Channels tab",
    "outliers": "videos that do far better than their own channel's usual",
    "table": "a channel's videos as a table: views, date, length",
}


def make_backend(args, root):
    """How YouTube is reached. Cookie options on the command line win; otherwise the workspace's settings are used
    when there is a workspace. A function of its own so tests can hand in a fake."""
    browser, file = args.cookies_from_browser or "", args.cookies or ""
    if not (browser or file) and (Path(root) / paths.CONFIG_NAME).exists():
        try:
            cfg = cfgmod.load(Path(root) / paths.CONFIG_NAME)
            browser, file = cfg["cookies_from_browser"], cfg["cookies_file"]
        except WorkspaceError:
            pass                                      # `ytt doctor` reports a broken settings file; research goes on
    return YtDlpBackend(browser, file)


# ---------------------------------------------------------------- the parser
def _common(sp, ids=True):
    out = sp.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="everything as JSON (with the video ids)")
    if ids:
        out.add_argument("--ids", action="store_true", help="only the video ids, one per line (for ytt make --videos -)")
    sp.add_argument("--csv", metavar="FILE", help="also save the rows to a CSV file")
    sp.add_argument("--cookies-from-browser", metavar="BROWSER", help="pass-through to yt-dlp, e.g. firefox, chrome")
    sp.add_argument("--cookies", metavar="FILE", help="pass-through to yt-dlp: a cookies.txt file")
    return out


def add_parser(sub):
    rs = sub.add_parser("research", help="look things up on YouTube (no workspace needed): " + ", ".join(TOOLS),
                        description="Research tools. They only read from YouTube and change nothing. "
                                    "Run `ytt research TOOL --help` for a tool's options.")
    rsub = rs.add_subparsers(dest="tool", metavar="TOOL")

    c = rsub.add_parser("channels", help=TOOLS["channels"],
                        description="Find channels worth looking at: by YouTube search, and/or the channels another "
                                    "channel features on its Channels tab. Pipe `--urls-only` into "
                                    "`ytt research outliers --channels-file -`.")
    c.add_argument("--search", action="append", default=[], metavar="KEYWORD", help="search YouTube for this and collect the "
                                                                                    "channels behind the results (repeatable)")
    c.add_argument("--count", type=int, default=30, help="results per --search (default 30)")
    c.add_argument("--seed", action="append", default=[], metavar="CHANNEL",
                   help="collect the channels this channel features on its Channels tab (repeatable)")
    c.add_argument("--with-subs", action="store_true", help="also look up subscribers and the verified badge (one request per channel)")
    group = _common(c, ids=False)
    group.add_argument("--urls-only", action="store_true", help="only the channel links, one per line")

    o = rsub.add_parser("outliers", help=TOOLS["outliers"],
                        description="Find videos that sit well above their own channel's baseline (the channel's "
                                    "median views for that tab).")
    o.add_argument("channels", nargs="*", metavar="CHANNEL", help="@handles, names or channel links")
    o.add_argument("--channels-file", metavar="FILE", help="read channels from a file, one per line ('-' for stdin; "
                                                           "blank lines and lines starting with # are skipped)")
    o.add_argument("--type", choices=[*outliers_mod.TABS, "all"], default="videos", help="which tab to scan (default: videos)")
    o.add_argument("--multiplier", type=float, default=3.0, help="flag videos at least this many times the channel's median (default 3.0)")
    o.add_argument("--limit", type=int, help="only the newest N videos per channel and tab (default: all)")
    o.add_argument("--top", type=int, help="only the best N overall")
    o.add_argument("--sort", choices=outliers_mod.SORTS, default="ratio",
                   help="ratio, views, subs and baseline show the highest first; days_ago, channel and type the "
                        "soonest/A-Z first; --reverse flips it (default: ratio)")
    o.add_argument("--reverse", action="store_true", help="flip the direction of --sort")
    o.add_argument("--group-by", choices=outliers_mod.GROUPS, default="none", help="one table per channel or per type")
    o.add_argument("--resolve-dates", action="store_true",
                   help="look up the real date of outliers that have none (mainly Shorts); only the rows that are shown")
    _common(o)

    t = rsub.add_parser("table", help=TOOLS["table"],
                        description="A channel's videos as a table: title, views, upload date, days since, length.")
    t.add_argument("channel", metavar="CHANNEL", help="@handle, name or channel link")
    t.add_argument("--type", choices=[*table_mod.TABS, "all"], default="videos", help="which tab (default: videos)")
    t.add_argument("--sort", choices=table_mod.SORTS, default="latest", help="latest (default), oldest or popular")
    t.add_argument("--limit", type=int, help="only N videos (after sorting)")
    t.add_argument("--full", action="store_true",
                   help="read each video's own page for the exact date and length (Shorts need this); one request per video")
    _common(t)
    return rs


# ---------------------------------------------------------------- running
def _say(text):
    print(f"  {text}", file=sys.stderr, flush=True)


def _read_channels(args):
    channels = list(args.channels)
    if args.channels_file:
        text = sys.stdin.read() if args.channels_file == "-" else Path(args.channels_file).read_text(encoding="utf-8")
        channels += parse_channel_list(text)
    return channels


def _emit(args, rows, ids, notes, csv_fields, show):
    """What every tool does with its result: notes to stderr, optional CSV, then JSON, ids or the table."""
    for note in notes:
        print(f"  ({note})", file=sys.stderr)
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"Saved {len(rows)} rows to {args.csv}", file=sys.stderr)
    if getattr(args, "json", False):
        data = {"results": rows, "notes": notes} if ids is None else {"results": rows, "ids": ids, "notes": notes}
        print(json.dumps(data, indent=2, ensure_ascii=False))
    elif getattr(args, "ids", False):
        print("\n".join(ids))
    else:
        show()


def _unique(ids):
    return list(dict.fromkeys(i for i in ids if i))


def _outliers_table(rows):
    return table([[r.channel[:25], yes_no(r.verified), r.title[:70], count(r.views), count(int(r.baseline_median)),
                   f"{r.ratio:.1f}x", "N/A" if r.days_ago is None else r.days_ago, r.type] for r in rows],
                 ["Channel", "Verified", "Title", "Views", "Channel median", "x Baseline", "Days ago", "Type"])


def _show_outliers(rows, group_by):
    if group_by == "none":
        print(_outliers_table(rows))
        return
    for i, (key, group) in enumerate(outliers_mod.grouped(rows, group_by)):
        if i:
            print()
        label = key
        if group_by == "channel":
            subs = group[0].subs
            label = f"{key} ({count(subs)} subs)" if subs is not None else f"{key} (subs N/A)"
        print(f"== {label} ==")
        print(_outliers_table(group))


OUTLIER_CSV = ["channel", "verified", "subs", "title", "views", "baseline_median", "ratio", "days_ago", "type", "url", "id"]


def research_outliers(args, root):
    request = outliers_mod.OutlierRequest(
        channels=_read_channels(args), content_type=args.type, multiplier=args.multiplier, limit=args.limit,
        top=args.top, sort=args.sort, reverse=args.reverse, resolve_dates=args.resolve_dates)
    result = outliers_mod.find_outliers(request, make_backend(args, root), on_progress=_say)
    if not result.rows:
        for note in result.notes:
            print(f"  ({note})", file=sys.stderr)
        print(f"No outliers found at {args.multiplier:g}x baseline. Try a lower --multiplier or check the channel(s).",
              file=sys.stderr)
        if args.json:
            print(json.dumps({"results": [], "ids": [], "notes": result.notes}, indent=2))
        return 1
    rows = [r.as_dict() for r in result.rows]
    _emit(args, rows, _unique(r.id for r in result.rows), result.notes, OUTLIER_CSV,
          lambda: _show_outliers(result.rows, args.group_by))
    return 0


CHANNEL_CSV = ["name", "verified", "subs", "url", "source"]


def research_channels(args, root):
    request = channels_mod.ChannelsRequest(searches=args.search, seeds=args.seed, count=args.count, with_subs=args.with_subs)
    result = channels_mod.find_channels(request, make_backend(args, root), on_progress=_say)
    if not result.channels:
        for note in result.notes:
            print(f"  ({note})", file=sys.stderr)
        print("No channels found.", file=sys.stderr)
        if args.json:
            print(json.dumps({"results": [], "notes": result.notes}, indent=2))
        return 1
    print(f"# {len(result.channels)} unique channel(s) found", file=sys.stderr)

    def show():
        if args.urls_only:
            print("\n".join(c.url for c in result.channels))
        elif args.with_subs:
            print(table([[c.name, yes_no(c.verified), count(c.subs), c.url, c.source] for c in result.channels],
                        ["Name", "Verified", "Subscribers", "URL", "Found via"]))
        else:
            print(table([[c.name, c.url, c.source] for c in result.channels], ["Name", "URL", "Found via"]))

    _emit(args, [c.as_dict() for c in result.channels], None, result.notes, CHANNEL_CSV, show)
    return 0


def _duration(seconds):
    if seconds is None:
        return "N/A"
    m, sec = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


TABLE_CSV = ["title", "type", "views", "upload_date", "days_ago", "approx", "duration", "url", "id"]


def research_table(args, root):
    now = utc_now()
    request = table_mod.TableRequest(channel=args.channel, content_type=args.type, sort=args.sort, limit=args.limit, full=args.full)
    result = table_mod.channel_table(request, make_backend(args, root), now=now, on_progress=_say)
    if not result.rows:
        for note in result.notes:
            print(f"  ({note})", file=sys.stderr)
        print("No videos found. Check the channel link or @handle.", file=sys.stderr)
        if args.json:
            print(json.dumps({"results": [], "ids": [], "notes": result.notes}, indent=2))
        return 1
    rows = [r.as_dict(now) for r in result.rows]

    def show():
        show_type = args.type == "all"
        headers = ["Title", "Views", "Uploaded", "Days ago", "Duration"] + (["Type"] if show_type else [])
        body = []
        for r in result.rows:
            uploaded = "N/A" if r.date is None else r.date.isoformat() + ("~" if r.approx else "")
            days = r.days_ago(now)
            body.append([r.title[:70], count(r.views), uploaded, "N/A" if days is None else days, _duration(r.duration)]
                        + ([r.type] if show_type else []))
        print(table(body, headers))
        if any(r.approx for r in result.rows):
            print("\n~ = approximate date, worked out from the channel page's relative time (like \"3 weeks ago\"). "
                  "Use --full for exact dates.", file=sys.stderr)

    _emit(args, rows, _unique(r.id for r in result.rows), result.notes, TABLE_CSV, show)
    return 0


RUNNERS = {"channels": research_channels, "outliers": research_outliers, "table": research_table}


def cmd_research(args, root):
    if not args.tool:
        print("Research tools (they only read from YouTube):")
        for name, how in TOOLS.items():
            print(f"  ytt research {name:<10} {how}")
        print("\nAdd --help to a tool for its options. Every tool can give --json, --ids or --csv FILE.")
        return 0
    return RUNNERS[args.tool](args, root)
