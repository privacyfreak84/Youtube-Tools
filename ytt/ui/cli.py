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
from ytt.ops.compile import groups as grp_mod
from ytt.ops.compile import make as make_mod
from ytt.ops.compile import remake as remake_mod
from ytt.ops.compile import style as style_mod
from ytt.ops.compile import style_ops
from ytt.ops.errors import OpError
from ytt.ops.library import forget as forget_mod
from ytt.ops.library import views
from ytt.ops.legacy_import import import_legacy
from ytt.sources import channel as chan
from ytt.sources import selection as sel
from ytt.sources.errors import SourceError
from ytt.sources.ytdlp import YtDlpBackend
from ytt.workspace import config as cfgmod
from ytt.workspace import paths
from ytt.workspace import store
from ytt.workspace.errors import WorkspaceError
from ytt.workspace.workspace import Workspace


def _add_query_flags(p, d, n_in_group):
    """The flags that choose which videos to get from a channel, shared by `fetch` and `make`. d(value) gives a
    flag's default: the value itself for fetch, argparse.SUPPRESS for make (so a flag that was typed can be told
    from one that was not, which `make --like` needs). n_in_group: -n is one of the ways to say how many (fetch);
    in make it is a separate thing (how many compilations)."""
    p.add_argument("--type", choices=chan.TABS, default=d("videos"), help="which tab: videos (default) or shorts")
    p.add_argument("--from", dest="date_from", metavar="DATE", default=d(""), help="uploaded on or after, e.g. 2025-01-31")
    p.add_argument("--to", dest="date_to", metavar="DATE", default=d(""), help="uploaded on or before")
    p.add_argument("--min-views", type=int, default=d(0), metavar="N")
    p.add_argument("--min-length", type=float, default=d(0), metavar="MIN", help="shortest, in minutes")
    p.add_argument("--max-length", type=float, default=d(0), metavar="MIN", help="longest, in minutes")
    p.add_argument("--sort", choices=sel.SORTS, default=d("popular"), help="popular (default), latest, oldest, ...")
    how = p.add_mutually_exclusive_group()
    how.add_argument("--clips", type=int, default=d(0), metavar="N", help="N NEW clips (not already in the library)")
    if n_in_group:
        how.add_argument("-n", dest="compilations", type=int, default=d(0), metavar="N",
                         help="enough new clips for N full compilations (clips already waiting count)")
    how.add_argument("--range", default=d(""), metavar="A-B", help="exact positions in the sorted list, e.g. 25-70")
    how.add_argument("--take", default=d(""), metavar="EXPR", help="advanced: last:20, every:5, random:30, new:60, comps:5")
    how.add_argument("--videos", metavar="FILE", default=d(None),
                     help="a list of YouTube ids or links, one per line (- for stdin), instead of a channel")
    p.add_argument("--keep-duplicates", action="store_true", default=d(False), help="also download look-alike re-uploads")
    p.add_argument("--refetch", action="store_true", default=d(False), help="download again even videos the library already has")
    p.add_argument("--max-height", type=int, default=d(0), metavar="PIXELS", help="quality cap (default: the default style's)")
    p.add_argument("--workers", type=int, default=d(0), metavar="N", help="downloads at once (default: the workspace setting)")


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
    _add_query_flags(fe, lambda v: v, n_in_group=True)
    fe.add_argument("--dry-run", action="store_true", help="show the plan, download nothing")
    fe.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")

    mk = sub.add_parser("make", help="fetch clips and make compilations from them",
                        description="Make compilations. Give a CHANNEL to fetch new clips first (then everything waiting "
                                    "in the library is used), or none to use the clips already in the library. "
                                    "Without -n everything that fills a compilation is made; the clips that cannot fill "
                                    "one are the leftover (see --if-short).")
    mk.add_argument("channel", nargs="?", default=argparse.SUPPRESS, metavar="CHANNEL", help="@Name or a channel link")
    _add_query_flags(mk, lambda v: argparse.SUPPRESS, n_in_group=False)
    mk.add_argument("-n", dest="compilations", type=int, default=argparse.SUPPRESS, metavar="N",
                    help="make N compilations (with a channel and no --clips/--range/--take, also fetches what is missing)")
    mk.add_argument("--all", action="store_true", default=argparse.SUPPRESS,
                    help="no channel: make as many compilations as the clips in the library allow")
    mk.add_argument("--like", metavar="ID", default=argparse.SUPPRESS,
                    help="repeat the request that made this compilation (a number, name or 'last') on fresh clips; "
                         "flags you add override it")
    mk.add_argument("--style", metavar="NAME", default=argparse.SUPPRESS, help="a saved style (default: the workspace's default)")
    size = mk.add_mutually_exclusive_group()
    size.add_argument("--per", type=int, metavar="N", default=argparse.SUPPRESS, help="clips per compilation")
    size.add_argument("--per-minutes", type=float, metavar="MIN", default=argparse.SUPPRESS, help="or about this many minutes")
    mk.add_argument("--order", choices=grp_mod.ORDERS, default=argparse.SUPPRESS,
                    help="which clips come first: name (the order they were fetched in), oldest, newest, random")
    play = mk.add_mutually_exclusive_group()
    play.add_argument("--reverse", dest="play", action="store_const", const="reverse", default=argparse.SUPPRESS,
                      help="play the whole sequence backwards")
    play.add_argument("--reverse-each", dest="play", action="store_const", const="each", default=argparse.SUPPRESS,
                      help="play each compilation backwards")
    mk.add_argument("--if-short", choices=grp_mod.IF_SHORT, default=argparse.SUPPRESS,
                    help="clips that cannot fill a compilation: keep them for next time (default), make a shorter "
                         "last one (short), or fetch just enough more to fill one (fetch)")
    used = mk.add_mutually_exclusive_group()
    used.add_argument("--delete-used-clips", dest="delete_used", action="store_true", default=argparse.SUPPRESS,
                      help="delete downloaded clips once they are in a compilation")
    used.add_argument("--keep-used-clips", dest="delete_used", action="store_false", default=argparse.SUPPRESS)
    mk.add_argument("--retry-failed", action="store_true", default=argparse.SUPPRESS,
                    help="try clips that could not be read earlier once more")
    mk.add_argument("--dry-run", action="store_true", help="show the plan, do nothing")
    mk.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")

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

    stl = sub.add_parser("style", help="how compilations look: transitions, intro, outro, quality",
                         description="Saved styles. A compilation keeps its own copy of the style it was made with, so "
                                     "changing or deleting a style never changes compilations already made.")
    ssub = stl.add_subparsers(dest="verb")
    sl = ssub.add_parser("list", help="the saved styles (the default)")
    sw = ssub.add_parser("show", help="all the settings of one style")
    sw.add_argument("name", metavar="NAME")
    se = ssub.add_parser("set", help="change settings, e.g.  ytt style set default quality best transition-seconds 0.5",
                         description="Change one or more settings of a style: NAME KEY VALUE [KEY VALUE ...]. "
                                     "To make a new style use --new or --from.")
    se.add_argument("name", metavar="NAME")
    se.add_argument("pairs", nargs="+", metavar="KEY VALUE", help="a setting and its new value, repeated")
    newish = se.add_mutually_exclusive_group()
    newish.add_argument("--new", action="store_true", help="create the style, starting from the built-in look")
    newish.add_argument("--from", dest="source", metavar="NAME", help="create the style as a copy of this one")
    se.add_argument("--dry-run", action="store_true", help="show the change, save nothing")
    se.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    ed = ssub.add_parser("edit", help="change a style by answering questions (creates it if it is new)",
                         description="Asks about each setting; press Enter to keep it, - to clear a text setting. "
                                     "For scripts use `style set`.")
    ed.add_argument("name", metavar="NAME")
    de = ssub.add_parser("delete", help="delete a style")
    de.add_argument("name", metavar="NAME")
    de.add_argument("--dry-run", action="store_true", help="show what would happen, delete nothing")
    de.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    for sp in (sl, sw):
        sp.add_argument("--json", action="store_true", help="machine-readable output")

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
    lf = lsub.add_parser("forget", help="take compilations out of the records so their clips can be used again",
                         description="Forget compilations: their clips go back into the pool for the next make. The "
                                     "video files are not touched. TARGETS: last, all, a number, a name or a list (1,3).")
    lf.add_argument("targets", metavar="TARGETS")
    lf.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    lf.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
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
    for a in shown:
        out.append(f"  {a.text}")
        if full:
            out += [f"      - {line}" for line in a.data.get("lines", [])]
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


def _library_forget(ws, args):
    fp = forget_mod.plan_forget(ws, args.targets)
    print(render_plan(fp.plan, full=True))
    if args.dry_run:
        print("\n(dry run: nothing was done)")
        return 0
    if not _confirm(args):
        print("Cancelled; nothing was done.")
        return 0
    result = forget_mod.run_forget(ws, fp)
    print(f"\nForgot {_plural(result.counts['forgotten'], 'compilation')}; "
          f"{_plural(result.counts['available'], 'clip')} can be used again (run {result.run_id}).")
    return 0


def cmd_library(args, root):
    what = args.what or "stats"
    with Workspace.open(root) as ws:
        if what == "forget":
            return _library_forget(ws, args)
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


_FETCH_FLAGS = ("channel", "type", "date_from", "date_to", "min_views", "min_length", "max_length", "sort", "clips",
                "range", "take", "videos", "keep_duplicates", "refetch", "max_height", "workers")
_HOW_MANY = ("clips", "range", "take", "videos")
_PLAIN_FLAGS = ("style", "per", "per_minutes", "order", "play", "if_short", "delete_used", "retry_failed")


def make_request_from_args(ws, args):
    """The command line as a MakeRequest. Only flags that were typed count (the make parser leaves the others out),
    so with --like they override the recorded request and everything else is kept."""
    from dataclasses import replace
    typed = vars(args)
    like = typed.get("like")
    request = make_mod.request_like(ws, like) if like else make_mod.MakeRequest()
    fetch = request.fetch
    given = {k: typed[k] for k in _FETCH_FLAGS if k in typed}
    if given:
        fetch = fetch or fetch_mod.FetchRequest()
        if any(k in given for k in _HOW_MANY):
            fetch = replace(fetch, clips=0, compilations=0, range="", take="", videos=None)
        if "videos" in given:
            given["videos"] = _read_video_list(given["videos"])
            fetch = replace(fetch, channel="")
        elif given.get("channel"):
            fetch = replace(fetch, videos=None)
        fetch = replace(fetch, **given)
    request = replace(request, fetch=fetch)
    if "compilations" in typed:
        request = replace(request, compilations=typed["compilations"], everything=False)
    if typed.get("all"):
        request = replace(request, everything=True, compilations=0)
    plain = {k: typed[k] for k in _PLAIN_FLAGS if k in typed}
    if "per" in typed:                      # a size typed now replaces the other way of saying it (with --like)
        plain["per_minutes"] = 0
    elif "per_minutes" in typed:
        plain["per"] = 0
    return replace(request, **plain), like


def _progress_say():
    """-> (say, clear): what is being read, on stderr so the plan on stdout stays clean."""
    stage = [None]

    def say(text):
        if sys.stderr.isatty():
            print(f"\r{text}   ", end="", file=sys.stderr, flush=True)
        elif stage[0] != text.split(" ")[0]:
            print(text, file=sys.stderr, flush=True)
        stage[0] = text.split(" ")[0]

    def clear():
        if sys.stderr.isatty() and stage[0]:
            print("\r" + " " * 78 + "\r", end="", file=sys.stderr)

    return say, clear


def cmd_make(args, root):
    with Workspace.open(root) as ws:
        request, like = make_request_from_args(ws, args)
        backend = make_backend(ws) if request.fetch is not None else None
        say, clear = _progress_say()
        if like:
            print(f"Repeating how {like if not like.isdigit() else 'compilation ' + like} was made; "
                  f"flags you add on top override it.\n")
        mp = make_mod.plan_make(ws, request, backend, progress=say)
        clear()
        print(render_plan(mp.plan, full=args.dry_run))
        if not mp.plan.ok:
            return 1
        if mp.nothing_to_do:
            print("\nNothing to do.")
            return 0
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

        def event(outcome, done, target):
            where = f"#{outcome.job.position} " if outcome.job.position else ""
            if outcome.status == "completed":
                print(f"  [{done}/{target}] {where}{_short(outcome.job.label) or outcome.job.video_id}", flush=True)
            else:
                print(f"  skipped {where}{_short(outcome.job.label) or outcome.job.video_id}: {outcome.detail}", flush=True)

        print()
        result = make_mod.run_make(ws, mp, on_progress=progress, backend=backend, on_fetch=event)
        if sys.stdout.isatty():
            print()
        for item in result.items:
            if item.status == "completed":
                if item.what in result.made:
                    print(f"  made {item.what}")
            else:
                print(f"  {item.status.upper()} {item.what}" + (f": {item.detail}" if item.detail else ""))
        c = result.counts
        bits = [f"{_plural(len(result.made), 'compilation')} made"]
        if c["downloaded"] or c["adopted"]:
            bits.append(f"{_plural(c['downloaded'], 'clip')} downloaded"
                        + (f", {c['adopted']} already on your disk added" if c["adopted"] else ""))
        if c["deleted"]:
            bits.append(f"{_plural(c['deleted'], 'used clip')} deleted")
        print(f"\n{result.status.capitalize()}: {', '.join(bits)} (run {result.run_id}).")
        if result.made:
            print(f"  Compilations are in {ws.compilations_dir}")
        if c["left_over"]:
            print(f"  {_plural(c['left_over'], 'clip')} left over, kept for the next make.")
        if result.status == "partial":
            print("  Not everything was done; run the same command again to try for the rest.")
        return {"completed": 0, "cancelled": 130}.get(result.status, 1)


def _style_lines(name, style, is_default):
    d = style.to_dict()
    width = max(len(k) for k in d)
    head = f"Style '{name}'" + (" (the workspace's default)" if is_default else "")
    return [head] + [f"  {k.ljust(width)}  {style_ops._shown(v)}" for k, v in d.items()]


def _style_list(ws, args):
    rows = style_ops.list_styles(ws)
    if args.json:
        print(json.dumps([{"name": n, "default": d, "settings": st.to_dict() if st else None, "problem": prob}
                          for n, d, st, prob in rows], indent=2))
        return 0
    if not rows:
        print("No styles yet. `ytt style set NAME quality best --new` makes one.")
        return 0
    table = []
    for name, is_default, st, problem in rows:
        if st is None:
            table.append([name, "(not valid)", problem, "", ""])
            continue
        table.append([name + (" *" if is_default else ""), st.transition, st.quality,
                      "yes" if st.intro else "-", "yes" if st.outro else "-"])
    print(_table(table, ["NAME", "TRANSITION", "QUALITY", "INTRO", "OUTRO"]))
    print("\n* = the workspace's default style")
    return 0


def _style_show(ws, args):
    style = style_ops.load(ws, args.name)
    is_default = args.name == ws.config["default_style"]
    if args.json:
        print(json.dumps({"name": args.name, "default": is_default, "settings": style.to_dict()}, indent=2))
    else:
        print("\n".join(_style_lines(args.name, style, is_default)))
    return 0


def _style_save(ws, sp, args):
    """Show a style change, ask, save."""
    print(render_plan(sp.plan, full=True))
    if not sp.plan.ok:
        return 1
    if not sp.changed:
        print("\nNothing to do.")
        return 0
    if getattr(args, "dry_run", False):
        print("\n(dry run: nothing was done)")
        return 0
    if not _confirm(args):
        print("Cancelled; nothing was done.")
        return 0
    style_ops.run_save(ws, sp)
    print(f"\nSaved style '{sp.name}'.")
    return 0


def _style_set(ws, args):
    if len(args.pairs) % 2:
        raise OpError("style set needs settings in pairs: NAME KEY VALUE [KEY VALUE ...] "
                      "(for example: ytt style set default quality best).")
    changes = dict(zip(args.pairs[0::2], args.pairs[1::2]))
    return _style_save(ws, style_ops.plan_save(ws, args.name, changes, new=args.new, source=args.source), args)


def _style_edit(ws, args):
    if not is_tty():
        raise OpError("style edit asks questions, so it needs a terminal. In a script use: "
                      "ytt style set NAME KEY VALUE [KEY VALUE ...]")
    exists = store.get_style(ws.conn, args.name) is not None
    current = style_ops.load(ws, args.name).to_dict() if exists else style_mod.Style().to_dict()
    print(f"{'Editing' if exists else 'New style'} '{args.name}'. Enter keeps a value; - clears a text setting.\n")
    changes = {}
    for key in style_ops.keys():
        while True:
            answer = input(f"{key} [{style_ops._shown(current[key])}]  ({style_ops.HELP[key]})\n> ").strip()
            if not answer:
                break
            text = "" if answer == "-" else answer
            try:
                value = style_ops.parse_value(key, text)
            except OpError as e:
                print(f"  {e}")
                continue
            if value != current[key]:
                changes[key] = text
            break
    print()
    from types import SimpleNamespace
    sp = style_ops.plan_save(ws, args.name, changes, new=not exists)
    return _style_save(ws, sp, SimpleNamespace(yes=False, dry_run=False))


def _style_delete(ws, args):
    plan = style_ops.plan_delete(ws, args.name)
    print(render_plan(plan, full=True))
    if not plan.ok:
        return 1
    if args.dry_run:
        print("\n(dry run: nothing was done)")
        return 0
    if not _confirm(args):
        print("Cancelled; nothing was done.")
        return 0
    style_ops.run_delete(ws, args.name, plan)
    print(f"\nDeleted style '{args.name}'.")
    return 0


def cmd_style(args, root):
    verb = args.verb or "list"
    if verb == "list" and not hasattr(args, "json"):
        args.json = False
    with Workspace.open(root) as ws:
        style_mod.ensure_default(ws.conn)
        ws.conn.commit()
        return {"list": _style_list, "show": _style_show, "set": _style_set, "edit": _style_edit,
                "delete": _style_delete}[verb](ws, args)


COMMANDS = {"init": cmd_init, "show": cmd_show, "set": cmd_set, "import": cmd_import}


def dispatch(args, root):
    """Run one parsed command. The errors a person can cause become a message and exit code 1; Ctrl-C exit code 130."""
    try:
        if args.command == "fetch":
            return cmd_fetch(args, root)
        if args.command == "make":
            return cmd_make(args, root)
        if args.command == "remake":
            return cmd_remake(args, root)
        if args.command == "style":
            return cmd_style(args, root)
        if args.command == "library":
            return cmd_library(args, root)
        return COMMANDS[args.action](args, root)
    except (WorkspaceError, OpError, SourceError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


def run_argv(argv, root):
    """Run a command given as a list of words, exactly as if it had been typed after `ytt`. The guided menu builds
    such lists, so a menu answer and a flag are the same thing and go through the same parser and command."""
    try:
        args = build_parser().parse_args(["--workspace", str(root), *argv])
    except SystemExit as e:                 # argparse already printed what is wrong
        return e.code if isinstance(e.code, int) else 2
    return dispatch(args, root)


def start_menu(args, environ=None):
    """The guided front door (ytt/ui/menu.py). Its libraries are only loaded here, so every other command works
    without them."""
    try:
        from ytt.ui.menu import Menu
        from ytt.ui.prompts import QuestionaryPrompter
    except ImportError as e:
        print(f"The guided menu needs the 'questionary' and 'rich' packages ({e}).\n"
              f"Install them with: pip install questionary rich\nThe commands still work: ytt --help", file=sys.stderr)
        return 1
    root = paths.resolve(args.workspace, os.environ if environ is None else environ)
    try:
        return Menu(root, lambda words: run_argv(words, root), QuestionaryPrompter(),
                    workspace_flag=args.workspace).start()
    except KeyboardInterrupt:
        return 130


def main(argv=None, environ=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.command is None:
        if is_tty() and sys.stdout.isatty():               # a person at a terminal: ask questions; a script: help
            return start_menu(args, environ)
        ap.print_help()
        return 0
    if args.command == "workspace" and args.action is None:
        ap.parse_args(["workspace", "--help"])
    root = paths.resolve(args.workspace, os.environ if environ is None else environ)
    return dispatch(args, root)


if __name__ == "__main__":
    sys.exit(main())
