"""`ytt research ...`: look things up on YouTube. Needs no workspace and never changes anything. Every tool prints a
table by default, or --json (everything, with the video ids), or --ids (just the ids, one per line, which
`ytt make --videos -` and `ytt fetch --videos -` read). --csv also saves the rows to a file."""
import csv
import json
import sys
from pathlib import Path

from ytt.ops.research import outliers as outliers_mod
from ytt.ops.research.common import parse_channel_list
from ytt.sources.ytdlp import YtDlpBackend
from ytt.ui.render import count, table, yes_no
from ytt.workspace import config as cfgmod
from ytt.workspace import paths
from ytt.workspace.errors import WorkspaceError

TOOLS = {
    "outliers": "videos that do far better than their own channel's usual",
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


def add_parser(sub):
    rs = sub.add_parser("research", help="look things up on YouTube (no workspace needed): " + ", ".join(TOOLS),
                        description="Research tools. They only read from YouTube and change nothing. "
                                    "Run `ytt research TOOL --help` for a tool's options.")
    rsub = rs.add_subparsers(dest="tool", metavar="TOOL")

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
        print(json.dumps({"results": rows, "ids": ids, "notes": notes}, indent=2, ensure_ascii=False))
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


RUNNERS = {"outliers": research_outliers}


def cmd_research(args, root):
    if not args.tool:
        print("Research tools (they only read from YouTube):")
        for name, how in TOOLS.items():
            print(f"  ytt research {name:<10} {how}")
        print("\nAdd --help to a tool for its options. Every tool can give --json, --ids or --csv FILE.")
        return 0
    return RUNNERS[args.tool](args, root)
