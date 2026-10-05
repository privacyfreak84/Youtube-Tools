"""Command line entry point. The only layer that prints or prompts."""
import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from ytt import __version__
from ytt.ops.compile import fetch as fetch_mod
from ytt.ops.compile import remake as remake_mod
from ytt.ops.compile import style as style_mod
from ytt.ops.errors import OpError
from ytt.ops.library import views
from ytt.ops.legacy_import import import_legacy
from ytt.sources import channel as chan
from ytt.sources import selection as sel
from ytt.sources.errors import SourceError
from ytt.sources.ytdlp import YtDlpBackend
from ytt.workspace import config as cfgmod
from ytt.workspace import paths
from ytt.workspace.errors import WorkspaceError
from ytt.workspace.workspace import Workspace


def build_parser():
    ap = argparse.ArgumentParser(prog="ytt", description="Research YouTube and make compilations.")
    ap.add_argument("--version", action="version", version=f"ytt {__version__}")
    ap.add_argument("--workspace", metavar="PATH", help=f"the workspace folder (default: ${paths.ENV_VAR} or ~/Videos/ytt)")
    sub = ap.add_subparsers(dest="command")

    ws = sub.add_parser("workspace", help="the folder where everything lives",
                        description="Create, inspect and configure the workspace.")
    wsub = ws.add_subparsers(dest="action")
    wsub.add_parser("init", help="create the workspace (safe to run again)")
    wsub.add_parser("show", help="where it is, what is in it, and its settings")
    st = wsub.add_parser("set", help="change one setting, e.g.  ytt workspace set workers 5")
    st.add_argument("key")
    st.add_argument("value")
    im = wsub.add_parser("import", help="bring in the state the old scripts left behind (one time)",
                         description="Reads the old scripts' state files in OLD_FOLDER and adds them to the workspace. "
                                     "Nothing in OLD_FOLDER is changed. Safe to run more than once.")
    im.add_argument("old_folder", metavar="OLD_FOLDER", help="the folder the old scripts ran in")
    im.add_argument("--dry-run", action="store_true", help="show what would be imported, change nothing")

    fe = sub.add_parser("fetch", help="get clips from a channel into the library",
                        description="Download clips into the library. Say which channel, how many, and (optionally) "
                                    "which ones. Videos the library already has are never downloaded again.")
    fe.add_argument("channel", nargs="?", default="", metavar="CHANNEL", help="@Name or a channel link")
    fe.add_argument("--type", choices=chan.TABS, default="videos", help="which tab: videos (default) or shorts")
    fe.add_argument("--from", dest="date_from", metavar="DATE", default="", help="uploaded on or after, e.g. 2025-01-31")
    fe.add_argument("--to", dest="date_to", metavar="DATE", default="", help="uploaded on or before")
    fe.add_argument("--min-views", type=int, default=0, metavar="N")
    fe.add_argument("--min-length", type=float, default=0, metavar="MIN", help="shortest, in minutes")
    fe.add_argument("--max-length", type=float, default=0, metavar="MIN", help="longest, in minutes")
    fe.add_argument("--sort", choices=sel.SORTS, default="popular", help="popular (default), latest, oldest, ...")
    how = fe.add_mutually_exclusive_group()
    how.add_argument("--clips", type=int, default=0, metavar="N", help="N NEW clips (not already in the library)")
    how.add_argument("-n", dest="compilations", type=int, default=0, metavar="N",
                     help="enough new clips for N full compilations (clips already waiting count)")
    how.add_argument("--range", default="", metavar="A-B", help="exact positions in the sorted list, e.g. 25-70")
    how.add_argument("--take", default="", metavar="EXPR", help="advanced: last:20, every:5, random:30, new:60, comps:5")
    how.add_argument("--videos", metavar="FILE", help="a list of YouTube ids or links, one per line (- for stdin), "
                                                       "instead of a channel")
    fe.add_argument("--keep-duplicates", action="store_true", help="also download look-alike re-uploads")
    fe.add_argument("--refetch", action="store_true", help="download again even videos the library already has")
    fe.add_argument("--max-height", type=int, default=0, metavar="PIXELS", help="quality cap (default: the default style's)")
    fe.add_argument("--workers", type=int, default=0, metavar="N", help="downloads at once (default: the workspace setting)")
    fe.add_argument("--dry-run", action="store_true", help="show the plan, download nothing")
    fe.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")

    rm = sub.add_parser("remake", help="render finished compilations again, with changes",
                        description="Render recorded compilations again from the same clips in the same order. "
                                    "TARGETS: last, all, a number (3), a name (compilation_003) or a list (1,3).")
    rm.add_argument("targets", metavar="TARGETS", help="which compilations")
    rm.add_argument("--style", metavar="NAME", help="use this saved style (default: the one it was made with)")
    rm.add_argument("--order", choices=remake_mod.ORDERS, default="recorded",
                    help="recorded (as before) or reversed (played backwards)")
    rm.add_argument("--replace", action="store_true", help="overwrite the old video instead of making a new compilation")
    rm.add_argument("--dry-run", action="store_true", help="show the plan, do nothing")
    rm.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")

    lib = sub.add_parser("library", help="what you have: clips, compilations, sources",
                         description="Look at what is in the library. With no argument: a summary.")
    lsub = lib.add_subparsers(dest="what")
    lsub.add_parser("stats", help="a summary (the default)")
    lc = lsub.add_parser("clips", help="the clips you have")
    lc.add_argument("--status", choices=("ready", "missing", "failed"))
    lc.add_argument("--unused", action="store_true", help="only ready clips no compilation has used")
    lsub.add_parser("compilations", help="finished compilations")
    lsub.add_parser("sources", help="channels you have fetched from")
    lr = lsub.add_parser("runs", help="what recent actions did")
    lr.add_argument("--limit", type=int, default=10)
    for sp in (lib, *lsub.choices.values()):
        sp.add_argument("--json", action="store_true", help="machine-readable output")
    return ap


def make_backend(ws):
    """How YouTube is reached. A function of its own so tests can hand in a fake instead."""
    return YtDlpBackend.from_config(ws.config)


# ---------------------------------------------------------------- showing things
def is_tty():
    return sys.stdin.isatty()


def _table(rows, headers):
    cells = [[str(c) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(headers)]
    line = lambda r: "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip()
    return "\n".join([line(headers), line(["-" * w for w in widths]), *[line(r) for r in cells]])


def _gb(n):
    return f"{n / 1e9:.1f} GB"


def _dur(seconds):
    if seconds is None:
        return "-"
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


def render_plan(plan, full=True, limit=12):
    """full=False shortens a long list of actions (a fetch of 45 clips) to the first few; --dry-run shows all."""
    out = [plan.title]
    actions = plan.actions
    shown = actions if full or len(actions) <= limit else actions[:limit - 2]
    out += [f"  {a.text}" for a in shown]
    if len(shown) < len(actions):
        out.append(f"  ... and {len(actions) - len(shown)} more (--dry-run lists them all)")
    for label, items in (("Errors", plan.errors), ("Warnings", plan.warnings), ("Notes", plan.notes)):
        if items:
            out += ["", label] + [f"  - {i}" for i in items]
    return "\n".join(out)


def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def render_import_report(r):
    out = []
    out.append(("Preview of the import from " if r.dry_run else "Imported from ") + r.legacy_dir
               + ("  (nothing was written)" if r.dry_run else ""))
    a = r.added
    out.append(f"  {_plural(a['sources'], 'source')} · {_plural(a['videos'], 'video')} · {_plural(a['clips'], 'clip')} · "
               f"{_plural(a['compilations'], 'compilation')} · {_plural(a['styles'], 'style')} added")
    out.append(f"  clips: {r.clips_ready} ready, {r.clips_missing} missing (known, but the file is gone; they can be fetched again)")
    if r.clips_recovered:
        out.append(f"  {_plural(r.clips_recovered, 'clip')} the old archive did not know were found by file name")
    if r.compilations_already_present:
        out.append(f"  {_plural(r.compilations_already_present, 'compilation')} were already imported and were left alone")
    if r.settings_applied:
        out.append("  settings taken over: " + ", ".join(r.settings_applied))
    if r.style_created:
        out.append("  your old compilation look is now the style 'default'")
    if r.settings_kept:
        out.append("  kept as they were in the workspace: " + ", ".join(r.settings_kept))
    notes = []
    if r.unidentified_files:
        notes.append(f"{_plural(r.unidentified_files, 'video file')} had no YouTube id in the name and were not imported")
    if r.unresolved_clips:
        notes.append(f"{_plural(r.unresolved_clips, 'clip')} in {_plural(r.compilations_with_unresolved_clips, 'compilation')} "
                     f"could not be tied to a YouTube video; the compilations are kept, but those clips cannot be re-fetched")
    if r.unreadable_in_old_ledger:
        notes.append(f"{_plural(r.unreadable_in_old_ledger, 'clip')} were marked unreadable by the old tool (not imported as such)")
    notes += r.warnings
    if notes:
        out.append("  Notes")
        out += [f"    - {n}" for n in notes]
    return "\n".join(out)


def render_workspace(ws):
    c = ws.counts()
    out = [f"Workspace   {ws.root}",
           f"  clips folder         {ws.clips_dir}",
           f"  compilations folder  {ws.compilations_dir}",
           f"  library              {_plural(c['clips'], 'clip')} ({c['missing_clips']} missing) · "
           f"{_plural(c['compilations'], 'compilation')} · {_plural(c['sources'], 'source')}",
           "", "Settings"]
    for key, value in ws.config.items():
        if isinstance(value, dict):
            out += [f"  [{key}]"] + [f"    {k} = {v}" for k, v in value.items()]
        else:
            out.append(f"  {key} = {value}")
    return "\n".join(out)


# ---------------------------------------------------------------- commands
def cmd_init(args, root):
    existed = (root / paths.DB_NAME).exists()
    with Workspace.init(root) as ws:
        style_mod.ensure_default(ws.conn)
        ws.conn.commit()
        print(f"{'Workspace already set up at' if existed else 'Created workspace at'} {ws.root}")
    return 0


def cmd_show(args, root):
    with Workspace.open(root) as ws:
        print(render_workspace(ws))
    return 0


def cmd_set(args, root):
    with Workspace.open(root) as ws:
        value = cfgmod.set_value(ws.config, args.key, args.value)
        ws.save_config()
        print(f"{args.key} = {value}")
    return 0


def cmd_import(args, root):
    temp = None
    made_root = False                                      # did this command create the workspace folder?
    if not (root / paths.DB_NAME).exists():
        if args.dry_run:                                   # a preview must not leave a workspace behind
            temp = Path(tempfile.mkdtemp(prefix="ytt_preview_"))
            ws = Workspace.init(temp / "ws")
        else:
            made_root = not root.exists()
            ws = Workspace.init(root)
            print(f"Created workspace at {ws.root}")
    else:
        ws = Workspace.open(root)
    try:
        report = import_legacy(ws, args.old_folder, dry_run=args.dry_run)
        if not args.dry_run:
            style_mod.ensure_default(ws.conn)
            ws.conn.commit()
        print(render_import_report(report))
    except WorkspaceError:
        ws.close()
        if made_root:                                      # do not leave an empty workspace after a failed first import
            shutil.rmtree(root, ignore_errors=True)
        raise
    finally:
        ws.close()
        if temp:
            shutil.rmtree(temp, ignore_errors=True)
    return 0


def _confirm(args):
    """Ask before doing anything. --yes skips it; a script (no terminal) must say --yes."""
    if args.yes:
        return True
    if not is_tty():
        raise OpError("This needs a yes and there is no terminal to ask. Add --yes to go ahead.")
    answer = input("\nProceed? [Y/n] ").strip().lower()
    return answer in ("", "y", "yes")


def _read_video_list(source):
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise OpError(f"can't read the list of videos {source}: {e}")
    try:
        return chan.parse_video_ids(text)
    except ValueError as e:
        raise OpError(f"{source}: {e}")


def _short(text, width=60):
    text = text or ""
    return text if len(text) <= width else text[:width - 1] + "…"


def cmd_fetch(args, root):
    request = fetch_mod.FetchRequest(
        channel=args.channel, type=args.type, date_from=args.date_from, date_to=args.date_to,
        min_views=args.min_views, min_length=args.min_length, max_length=args.max_length, sort=args.sort,
        clips=args.clips, compilations=args.compilations, range=args.range, take=args.take,
        videos=_read_video_list(args.videos) if args.videos else None,
        keep_duplicates=args.keep_duplicates, refetch=args.refetch, max_height=args.max_height, workers=args.workers)
    with Workspace.open(root) as ws:
        backend = make_backend(ws)
        stage = [None]

        def say(text):
            """What is being read, on stderr so the plan on stdout stays clean."""
            if sys.stderr.isatty():
                print(f"\r{text}   ", end="", file=sys.stderr, flush=True)
            elif stage[0] != text.split(" ")[0]:
                print(text, file=sys.stderr, flush=True)
            stage[0] = text.split(" ")[0]

        fp = fetch_mod.plan_fetch(ws, request, backend, progress=say)
        if sys.stderr.isatty() and stage[0]:
            print("\r" + " " * 78 + "\r", end="", file=sys.stderr)
        print(render_plan(fp.plan, full=args.dry_run))
        if not fp.plan.ok:
            return 1
        if not fp.target and not fp.adopt:
            print("\nNothing to do.")
            return 0
        if args.dry_run:
            print("\n(dry run: nothing was done)")
            return 0
        if not _confirm(args):
            print("Cancelled; nothing was done.")
            return 0

        def event(outcome, done, target):
            where = f"#{outcome.job.position} " if outcome.job.position else ""
            if outcome.status == "completed":
                print(f"  [{done}/{target}] {where}{_short(outcome.job.label) or outcome.job.video_id}", flush=True)
            else:
                print(f"  skipped {where}{_short(outcome.job.label) or outcome.job.video_id}: {outcome.detail}", flush=True)

        print()
        result = fetch_mod.run_fetch(ws, fp, backend, on_event=event)
        c = result.counts
        bits = [f"{_plural(c['downloaded'], 'clip')} downloaded"]
        if c["adopted"]:
            bits.append(f"{c['adopted']} already on your disk added")
        if c["failed"]:
            bits.append(f"{c['failed']} failed")
        print(f"\n{result.status.capitalize()}: {', '.join(bits)} (run {result.run_id}). Clips are in {ws.clips_dir}")
        if result.status == "partial":
            print("  Fewer clips than asked arrived; run the same command again to try for the rest.")
        return {"completed": 0, "cancelled": 130}.get(result.status, 1)


def cmd_remake(args, root):
    with Workspace.open(root) as ws:
        rp = remake_mod.plan_remake(ws, remake_mod.RemakeRequest(args.targets, args.style, args.order, args.replace))
        print(render_plan(rp.plan, full=args.dry_run))
        if not rp.plan.ok:
            return 1
        if args.dry_run:
            print("\n(dry run: nothing was done)")
            return 0
        if not _confirm(args):
            print("Cancelled; nothing was done.")
            return 0
        shown = {}

        def progress(name, done, total):
            pct = min(100, int(done * 100 / total)) if total else 0
            if sys.stdout.isatty():
                print(f"\r  {name}: {pct}%", end="", flush=True)
            elif pct // 25 > shown.get(name, -1):
                shown[name] = pct // 25
                print(f"  {name}: {pct}%", flush=True)

        def fetched(outcome):
            if outcome.status == "completed":
                print(f"  fetched again {_short(outcome.job.label)}", flush=True)
            else:
                print(f"  could not fetch {_short(outcome.job.label)}: {outcome.detail}", flush=True)

        print()
        result = remake_mod.run_remake(ws, rp, on_progress=progress, backend=make_backend(ws), on_fetch=fetched)
        if sys.stdout.isatty():
            print()
        for item in result.items:
            if item.status == "completed":
                print(f"  made {item.what}")
            else:
                print(f"  {item.status.upper()} {item.what}" + (f": {item.detail}" if item.detail else ""))
        print(f"\n{result.status.capitalize()}: {_plural(len(result.made), 'compilation')} made (run {result.run_id}).")
        return {"completed": 0, "cancelled": 130}.get(result.status, 1)


def cmd_library(args, root):
    what = args.what or "stats"
    with Workspace.open(root) as ws:
        if what == "stats":
            data = views.stats(ws)
            if args.json:
                print(json.dumps(data, indent=2))
                return 0
            print(f"Library   {_plural(data['clips'], 'clip')} ({data['ready']} ready, {data['missing']} missing"
                  + (f", {data['failed']} failed" if data["failed"] else "")
                  + f") · {_plural(data['compilations'], 'compilation')} · {_plural(data['sources'], 'source')}")
            print(f"  ready clips   {data['used']} used by compilations · {data['unused']} unused")
            print(f"  storage       clips {_gb(data['clips_bytes'])} · compilations {_gb(data['compilations_bytes'])}")
            if data["compilations_missing_file"]:
                print(f"  {_plural(data['compilations_missing_file'], 'compilation')} no longer have their video file")
            return 0
        if what == "clips":
            data = views.clips(ws, status=args.status, unused=args.unused)
            headers, rows = ["id", "youtube id", "status", "from", "used", "length", "views", "title"], \
                [[c["id"], c["youtube_id"], c["status"], c["origin"], "yes" if c["used"] else "no", _dur(c["duration"]),
                  c["views"] if c["views"] is not None else "-", (c["title"] or "")[:50]] for c in data]
        elif what == "compilations":
            data = views.compilations(ws)
            headers, rows = ["name", "clips", "made", "style", "from", "file"], \
                [[c["name"], c["clips"], c["made"] or "-", "recorded" if c["style_recorded"] else "unknown",
                  c["parent"] or "-", "ok" if c["output_exists"] else ("gone" if c["output"] else "-")] for c in data]
        elif what == "sources":
            data = views.sources(ws)
            headers, rows = ["source", "videos known", "clips"], [[c["handle"], c["videos"], c["clips"]] for c in data]
        else:
            data = views.runs(ws, args.limit)
            headers, rows = ["run", "what", "status", "started", "results"], \
                [[r["id"], r["kind"], r["status"], r["started"][:16].replace("T", " "),
                  ", ".join(f"{i['what']} {i['status']}" for i in r["items"])[:60]] for r in data]
        if args.json:
            print(json.dumps(data, indent=2))
        elif rows:
            print(_table(rows, headers))
        else:
            print("Nothing here yet.")
    return 0


COMMANDS = {"init": cmd_init, "show": cmd_show, "set": cmd_set, "import": cmd_import}


def main(argv=None, environ=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.command is None:
        ap.print_help()
        return 0
    if args.command == "workspace" and args.action is None:
        ap.parse_args(["workspace", "--help"])
    root = paths.resolve(args.workspace, os.environ if environ is None else environ)
    try:
        if args.command == "fetch":
            return cmd_fetch(args, root)
        if args.command == "remake":
            return cmd_remake(args, root)
        if args.command == "library":
            return cmd_library(args, root)
        return COMMANDS[args.action](args, root)
    except (WorkspaceError, OpError, SourceError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
