"""The guided front door: `ytt` with no arguments.

The menu is a request builder and nothing more (DESIGN.md section 6). It asks plain questions and turns the answers
into the command line the flags would have made, as a list of words, then hands that list to `run` (cli.run_argv),
the same parser and the same commands as typing it. So there is no second way of making a compilation: the plan
screen, the confirmation, the progress and the exit codes are the ones you get from the command line, and the menu
prints the command it built so the flags can be learned from it.

The menu never shows list-position syntax (last:20, every:5 ...). `--take` is for the command line only.
"""
import copy
import shlex
from pathlib import Path

from rich.console import Console

from ytt.ops.compile import groups as grp_mod
from ytt.ops.compile import make as make_mod
from ytt.ops.compile import style_ops
from ytt.ops.errors import OpError
from ytt.ops.library import views
from ytt.sources import channel as chan
from ytt.sources import selection as sel
from ytt.sources.errors import SourceError
from ytt.workspace import config as cfgmod
from ytt.workspace import paths
from ytt.workspace.errors import WorkspaceError
from ytt.workspace.workspace import Workspace

from ytt.ui.prompts import GoBack

MAIN = [
    ("Make compilations", "make"),
    ("Get clips only (no compiling)", "fetch"),
    ("Remake a finished compilation", "remake"),
    ("Look at the library", "library"),
    ("Styles (how compilations look)", "style"),
    ("Workspace and settings", "workspace"),
    ("Quit", "quit"),
]
SORT_LABELS = {
    "popular": "The most viewed first", "unpopular": "The least viewed first", "oldest": "The oldest first",
    "latest": "The newest first", "longest": "The longest first", "shortest": "The shortest first",
    "title": "By title, A to Z", "random": "In random order",
}
ORDER_LABELS = {
    "name": "The order they were downloaded in", "oldest": "Oldest uploads first",
    "newest": "Newest uploads first", "random": "Random",
}
NEW_STYLE = ""            # the value of "A new style" in the style list (a real style name is never empty)
IF_SHORT_LABELS = {
    "keep": "Keep them for the next make", "short": "Make one shorter compilation from them",
    "fetch": "Download just enough more clips to fill one",
}


# ---------------------------------------------------------------- small helpers
def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def _num(x):
    """5.0 -> '5', 2.5 -> '2.5' (so the command line reads naturally)."""
    x = float(x)
    return str(int(x)) if x.is_integer() else str(x)


def _day(text):
    return (text or "-")[:10]


def _whole_number(text):
    t = text.strip()
    return None if t.isdigit() and int(t) > 0 else "Type a whole number, 1 or more."


def _number(text):
    try:
        return None if float(text) > 0 else "Type a number above 0."
    except ValueError:
        return "Type a number above 0."


def _channel_problem(text):
    t = text.strip()
    if not t:
        return "Type the channel's name (like @Name) or a link."
    if any(c.isspace() for c in t):
        return "A channel name or link has no spaces."
    return None


def _date_or_nothing(text):
    if not text.strip():
        return None
    try:
        sel.parse_date(text)
    except ValueError as e:
        return str(e)
    return None


def _whole_number_or_nothing(text):
    return None if not text.strip() else _whole_number(text)


def _number_or_nothing(text):
    return None if not text.strip() else _number(text)


def _videos_file_problem(text):
    path = Path(text.strip()).expanduser()
    if not path.is_file():
        return "There is no file there."
    try:
        ids = chan.parse_video_ids(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        return f"Can't read it: {e}"
    except ValueError as e:
        return str(e)
    return None if ids else "That file lists no videos."


def _shown_setting(value):
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(value) if value else "(none)"
    return "(none)" if value in ("", None) else str(value)


def _folder_problem(text):
    return None if Path(text.strip()).expanduser().is_dir() else "There is no folder there."


class Menu:
    def __init__(self, root, run, ask, console=None, workspace_flag=None):
        """root: the workspace folder. run(words) -> exit code (cli.run_argv). ask: a Prompter. workspace_flag: the
        --workspace the person typed, if any, so the command lines shown still work."""
        self.root = Path(root)
        self.run = run
        self.ask = ask
        self.console = console or Console(highlight=False)
        self.workspace_flag = workspace_flag

    # ------------------------------------------------------------ output and plumbing
    def say(self, text=""):
        self.console.print(text, markup=False, soft_wrap=True)       # soft_wrap: a command line stays copyable

    def _read(self, fn):
        """Look something up in the workspace (read-only) and let go of it again."""
        with Workspace.open(self.root) as ws:
            return fn(ws)

    def _cmdline(self, argv):
        words = ["ytt"] + (["--workspace", self.workspace_flag] if self.workspace_flag else []) + list(argv)
        return shlex.join(words)

    def _do(self, argv):
        """Show the command being run, run it."""
        self.say("Command line for this:  " + self._cmdline(argv))
        return self.run(argv)

    def _finish(self, argv):
        """For commands that make a plan: show the command, then run it for real (it asks before doing anything)
        or as a dry run, which lists every detail of the plan and changes nothing."""
        self.say("Command line for this:  " + self._cmdline(argv))
        what = self.ask.select("Ready. What next?", [
            ("Show me the plan", "go"),
            ("Show me every detail of the plan, and do nothing", "dry"),
            ("Go back to the menu", "back")])
        if what != "back":
            self.run(argv + (["--dry-run"] if what == "dry" else []))

    # ------------------------------------------------------------ the main loop
    def start(self):
        try:
            while True:
                if not (self.root / paths.DB_NAME).exists():
                    if not self._first_run():
                        return 0
                    continue
                self._header()
                try:
                    pick = self.ask.select("What do you want to do?", MAIN)
                except GoBack:
                    return 0
                if pick == "quit":
                    return 0
                try:
                    getattr(self, "flow_" + pick)()
                except GoBack:
                    self.say("Back at the menu.")
                except (WorkspaceError, OpError, SourceError) as e:
                    self.say(f"Error: {e}")
        except WorkspaceError as e:
            self.say(f"Error: {e}")
            return 1

    def _header(self):
        data = self._read(views.stats)
        self.say()
        self.console.rule("ytt")
        self.say(f"Workspace  {self.root}")
        self.say(f"Library    {plural(data['clips'], 'clip')} ({data['unused']} not used yet) · "
                 f"{plural(data['compilations'], 'compilation')}")
        self.say()

    def _first_run(self):
        """No workspace yet. True when there is one now."""
        self.say(f"There is no workspace at {self.root} yet. It is the folder where clips, compilations and "
                 f"settings are kept.")
        try:
            what = self.ask.select("Set it up?", [
                ("Yes, an empty one", "empty"),
                ("Yes, and bring in what the old scripts made", "old"),
                ("No, quit", "quit")])
        except GoBack:
            return False
        if what == "quit":
            return False
        try:
            if what == "empty":
                self._do(["workspace", "init"])
            else:
                self._import_old()
        except GoBack:
            pass
        return True

    # ------------------------------------------------------------ make and fetch
    def flow_make(self):
        repeatable = self._read(self._repeatable)
        choices = [("A YouTube channel (new clips are downloaded first)", "channel"),
                   ("The clips already in my library", "library"),
                   ("A list of videos in a file", "list")]
        if repeatable:
            choices.append(("Do again what an earlier make did, with fresh clips", "like"))
        source = self.ask.select("Where do the clips come from?", choices)
        argv = ["make"]
        if source == "channel":
            argv += self._channel_query(for_make=True)
        elif source == "library":
            argv += self._library_amount()
        elif source == "list":
            argv += self._videos_file()
        else:
            argv += ["--like", self.ask.select("Which one should be repeated?", repeatable)]
        argv += self._extras(make=True, fetching=source in ("channel", "list"), channel=source == "channel")
        self._finish(argv)

    def flow_fetch(self):
        source = self.ask.select("Where do the clips come from?", [
            ("A YouTube channel", "channel"), ("A list of videos in a file", "list")])
        argv = ["fetch"]
        argv += self._channel_query(for_make=False) if source == "channel" else self._videos_file()
        argv += self._extras(make=False, fetching=True, channel=source == "channel")
        self._finish(argv)

    @staticmethod
    def _repeatable(ws):
        """Compilations `make --like` can repeat, newest first: (label, name)."""
        out = []
        for c in reversed(views.compilations(ws)):
            try:
                make_mod.request_like(ws, c["name"])
            except OpError:
                continue
            out.append((f"{c['name']} · {plural(c['clips'], 'clip')} · made {_day(c['made'])}", c["name"]))
        return out

    def _videos_file(self):
        path = self.ask.text("Which file lists the videos? (one YouTube id or link per line)",
                             validate=_videos_file_problem).strip()
        return ["--videos", str(Path(path).expanduser())]

    def _library_amount(self):
        how = self.ask.select("How many compilations?", [
            ("A number of them", "n"), ("As many as the clips in my library allow", "all")])
        if how == "all":
            return ["--all"]
        return ["-n", self.ask.text("How many compilations?", validate=_whole_number).strip()]

    def _channel_query(self, for_make):
        """The channel and which of its videos -> words for the command line, the channel first."""
        argv = [self.ask.text("Which channel? (a name like @Name, or a link)", validate=_channel_problem).strip()]
        tab = self.ask.select("Videos or Shorts?", [("Videos", "videos"), ("Shorts", "shorts")])
        if tab != "videos":
            argv += ["--type", tab]
        order = self.ask.select("Which come first?", [(SORT_LABELS[s], s) for s in sel.SORTS])
        if order != "popular":
            argv += ["--sort", order]
        argv += self._how_much(for_make)
        argv += self._filters()
        return argv

    def _how_much(self, for_make):
        clips = ("A number of new clips" + (" (everything that fills a compilation is then made)" if for_make else ""),
                 "clips")
        comps = (("A number of compilations (the clips are downloaded as needed)" if for_make
                  else "Enough new clips for a number of compilations"), "compilations")
        stretch = ("One stretch of the list, such as the 25th to the 70th", "range")
        how = self.ask.select("How much do you want?", [comps, clips, stretch] if for_make else [clips, comps, stretch])
        if how == "clips":
            return ["--clips", self.ask.text("How many new clips?", validate=_whole_number).strip()]
        if how == "compilations":
            return ["-n", self.ask.text("How many compilations?", validate=_whole_number).strip()]
        start = self.ask.text("From which place in the list? (1 is the first)", validate=_whole_number).strip()
        end = self.ask.text("Up to which place? (empty: to the end of the list)",
                            validate=lambda t: self._end_problem(start, t)).strip()
        return ["--range", f"{start}-{end}"]

    @staticmethod
    def _end_problem(start, text):
        if not text.strip():
            return None
        return _whole_number(text) or (None if int(text) >= int(start) else f"That is before place {start}.")

    def _filters(self):
        picked = self.ask.checkbox("Narrow it down? (nothing ticked = no limits)", [
            ("Only videos uploaded within certain dates", "dates"),
            ("Only videos with at least a number of views", "views"),
            ("Only videos of a certain length", "length")])
        argv = []
        if "dates" in picked:
            for flag, text in (("--from", "From which date? (empty: no limit; like 2025-01-31)"),
                               ("--to", "Up to which date? (empty: no limit)")):
                value = self.ask.text(text, validate=_date_or_nothing).strip()
                if value:
                    argv += [flag, value]
        if "views" in picked:
            argv += ["--min-views", self.ask.text("At least how many views?", validate=_whole_number).strip()]
        if "length" in picked:
            for flag, text in (("--min-length", "Shortest, in minutes? (empty: no limit)"),
                               ("--max-length", "Longest, in minutes? (empty: no limit)")):
                value = self.ask.text(text, validate=_number_or_nothing).strip()
                if value:
                    argv += [flag, _num(value)]
        return argv

    def _extras(self, make, fetching, channel):
        """'Change anything else?' Nothing ticked means the usual settings (the workspace's and the default style).
        make: compiling options apply. fetching: the clips are downloaded, so the download options apply."""
        choices = []
        if make:
            choices += [("How the compilations look (the style)", "style"),
                        ("How many clips go into one compilation", "size"),
                        ("Which clips go first", "order"),
                        ("Playing backwards", "play"),
                        ("What happens to clips that cannot fill a compilation", "if_short"),
                        ("Deleting the downloaded clips once they are used", "delete_used"),
                        ("Trying clips again that could not be read before", "retry")]
        if fetching:
            choices += [("Also download look-alike re-uploads", "dups"),
                        ("Download again even what I already have", "refetch"),
                        ("The picture quality (tallest picture)", "quality")]
        picked = self.ask.checkbox("Change anything else? (nothing ticked = the usual settings)", choices)
        cfg = self._read(lambda ws: copy.deepcopy(ws.config))
        argv = []
        for _, key in choices:
            if key not in picked:
                continue
            argv += getattr(self, "_extra_" + key)(cfg, channel)
        return argv

    def _extra_style(self, cfg, channel):
        styles = self._read(lambda ws: [(n, d) for n, d, st, _ in style_ops.list_styles(ws) if st is not None])
        default = next((n for n, d in styles if d), None)
        name = self.ask.select("Which style?", [(n + (" (the default)" if d else ""), n) for n, d in styles],
                               default=default)
        return ["--style", name]

    def _extra_size(self, cfg, channel):
        how = self.ask.select("How long should each compilation be?", [
            ("A number of clips", "clips"), ("About a number of minutes", "minutes")])
        if how == "clips":
            return ["--per", self.ask.text("How many clips in each?", default=str(cfg["make"]["clips_each"]),
                                           validate=_whole_number).strip()]
        return ["--per-minutes", _num(self.ask.text("About how many minutes each?", default=_num(cfg["make"]["minutes_each"]),
                                                     validate=_number).strip())]

    def _extra_order(self, cfg, channel):
        return ["--order", self.ask.select("Which clips go first?", [(ORDER_LABELS[o], o) for o in grp_mod.ORDERS],
                                           default=cfg["make"]["order"])]

    def _extra_play(self, cfg, channel):
        how = self.ask.select("Play in which direction?", [
            ("Normal", "normal"), ("The whole sequence backwards", "reverse"), ("Each compilation backwards", "each")])
        return {"normal": [], "reverse": ["--reverse"], "each": ["--reverse-each"]}[how]

    def _extra_if_short(self, cfg, channel):
        choices = [(IF_SHORT_LABELS[k], k) for k in grp_mod.IF_SHORT if channel or k != "fetch"]
        usual = cfg["make"]["if_short"]
        return ["--if-short", self.ask.select("Clips that cannot fill a compilation:", choices,
                                              default=usual if usual in [v for _, v in choices] else None)]

    def _extra_delete_used(self, cfg, channel):
        yes = self.ask.confirm("Delete the clips ytt downloaded once they are in a compilation?",
                               default=bool(cfg["delete_used_clips"]))
        return ["--delete-used-clips" if yes else "--keep-used-clips"]

    def _extra_retry(self, cfg, channel):
        return ["--retry-failed"]

    def _extra_dups(self, cfg, channel):
        return ["--keep-duplicates"]

    def _extra_refetch(self, cfg, channel):
        return ["--refetch"]

    def _extra_quality(self, cfg, channel):
        return ["--max-height", self.ask.text("The tallest picture to download, in pixels? (like 720 or 1080)",
                                              validate=_whole_number).strip()]

    # ------------------------------------------------------------ remake
    def flow_remake(self):
        comps = self._read(views.compilations)
        if not comps:
            self.say("Nothing has been made yet, so there is nothing to remake.")
            return
        targets = self._which_compilations(comps)
        argv = ["remake", targets]
        if self.ask.select("Which style?", [("The one it was made with", "same"),
                                            ("A different saved style", "other")]) == "other":
            argv += self._extra_style({}, False)
        if self.ask.select("In which order?", [("The same as before", "recorded"), ("Played backwards", "reversed")]) \
                == "reversed":
            argv += ["--order", "reversed"]
        if self.ask.select("What should happen to the old video?", [
                ("Nothing: make a new compilation next to it", "new"),
                ("Replace it with the new one", "replace")]) == "replace":
            argv += ["--replace"]
        self._finish(argv)

    def _which_compilations(self, comps):
        """-> the TARGETS word for remake / library forget: last, all, or names separated by commas."""
        which = self.ask.select("Which compilations?", [
            ("The last one", "last"), ("Pick from a list", "pick"), ("All of them", "all")])
        if which != "pick":
            return which
        picked = self.ask.checkbox("Which compilations?", [
            (f"{c['name']} · {plural(c['clips'], 'clip')} · made {_day(c['made'])} · "
             f"{'video is there' if c['output_exists'] else 'video is gone'}", c["name"])
            for c in reversed(comps)], required=True)
        return ",".join(picked)

    # ------------------------------------------------------------ library
    def flow_library(self):
        while True:
            what = self.ask.select("The library", [
                ("A summary", "stats"), ("The clips", "clips"), ("The compilations", "compilations"),
                ("The channels I have fetched from", "sources"), ("What recent actions did", "runs"),
                ("Forget compilations (so their clips can be used again)", "forget"), ("Back", "back")])
            if what == "back":
                return
            try:
                if what == "clips":
                    only = self.ask.select("Which clips?", [
                        ("All of them", "all"), ("Not used in a compilation yet", "unused"),
                        ("Missing (the file is gone)", "missing"), ("Could not be read", "failed")])
                    self.run(["library", "clips"] + {"all": [], "unused": ["--unused"],
                                                     "missing": ["--status", "missing"],
                                                     "failed": ["--status", "failed"]}[only])
                elif what == "forget":
                    comps = self._read(views.compilations)
                    if not comps:
                        self.say("There are no compilations to forget.")
                        continue
                    self._finish(["library", "forget", self._which_compilations(comps)])
                else:
                    self.run(["library", what])
            except GoBack:
                continue

    # ------------------------------------------------------------ styles
    def flow_style(self):
        while True:
            what = self.ask.select("Styles", [
                ("The styles I have", "list"), ("Look at one", "show"), ("Change one, or make a new one", "edit"),
                ("Delete one", "delete"), ("Back", "back")])
            if what == "back":
                return
            try:
                self._style_action(what)
            except GoBack:
                continue

    def _style_action(self, what):
        names = self._read(lambda ws: [(n, d) for n, d, _, _ in style_ops.list_styles(ws)])
        label = lambda n, d: n + (" (the default)" if d else "")
        if what == "list":
            self.run(["style", "list"])
        elif what == "show":
            self.run(["style", "show", self.ask.select("Which style?", [(label(n, d), n) for n, d in names])])
        elif what == "delete":
            others = [(label(n, d), n) for n, d in names if not d]
            if not others:
                self.say("The only style is the workspace's default, which cannot be deleted.")
                return
            self._finish(["style", "delete", self.ask.select("Which style?", others)])
        else:
            existing = {n for n, _ in names}
            pick = self.ask.select("Which style?", [(label(n, d), n) for n, d in names] + [("A new style", NEW_STYLE)])
            name = pick if pick != NEW_STYLE else self.ask.text("What should the new style be called?", validate=lambda t: (
                "Use letters, digits, - _ . and no spaces." if not style_ops.NAME_RE.match(t.strip())
                else "There is already a style with that name." if t.strip() in existing else None)).strip()
            self._do(["style", "edit", name])

    # ------------------------------------------------------------ workspace and settings
    def flow_workspace(self):
        while True:
            what = self.ask.select("Workspace and settings", [
                ("Show where everything is, and the settings", "show"), ("Change a setting", "set"),
                ("Bring in what the old scripts made", "old"), ("Back", "back")])
            if what == "back":
                return
            try:
                if what == "show":
                    self.run(["workspace", "show"])
                elif what == "set":
                    self._change_setting()
                else:
                    self._import_old()
            except GoBack:
                continue

    def _change_setting(self):
        cfg = self._read(lambda ws: copy.deepcopy(ws.config))
        flat = []
        for key, value in cfg.items():
            flat += [(f"{key}.{k}", v) for k, v in value.items()] if isinstance(value, dict) else [(key, value)]
        key = self.ask.select("Which setting?", [(f"{k}   (now: {_shown_setting(v)})", k) for k, v in flat])
        current = dict(flat)[key]
        if isinstance(current, bool):
            value = "yes" if self.ask.confirm(f"{key}: yes or no?", default=current) else "no"
        else:
            def problem(text):
                try:
                    cfgmod.set_value(copy.deepcopy(cfg), key, text)
                except WorkspaceError as e:
                    return str(e)
                return None
            value = self.ask.text(f"New value for {key}?", default=", ".join(current) if isinstance(current, list)
                                  else ("" if current is None else str(current)), validate=problem).strip()
        self._do(["workspace", "set", key, value])

    def _import_old(self):
        """Bring in the old scripts' state: show what would come in, then ask."""
        folder = self.ask.text("Which folder did the old scripts run in? (the one with fetch_archive.json and "
                               "the other state files)", validate=_folder_problem).strip()
        argv = ["workspace", "import", str(Path(folder).expanduser())]
        self._do(argv + ["--dry-run"])
        if self.ask.confirm("Import it for real?", default=True):
            self._do(argv)
