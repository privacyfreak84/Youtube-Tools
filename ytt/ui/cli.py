"""Command line entry point. The only layer that prints or prompts."""
import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

from ytt import __version__
from ytt.ops.compile import style as style_mod
from ytt.ops.legacy_import import import_legacy
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
    return ap


# ---------------------------------------------------------------- showing things
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
        return COMMANDS[args.action](args, root)
    except WorkspaceError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
