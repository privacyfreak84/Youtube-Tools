"""The menu flows for the tools that need no plan screen of their own: research, stitch, doctor, and making transition
videos (stingers). Like every flow they only ask questions and build the command line the flags would have made, then
hand it to `self.run` (see ytt/ui/menu.py)."""
import shlex
from pathlib import Path

from ytt.ops.compile import stingers as stingers_mod
from ytt.ops.compile import stitch as stitch_mod
from ytt.sources import channel as chan
from ytt.ui import answers
from ytt.ui.prompts import GoBack

RESEARCH = [
    ("Find channels (from a search, or from another channel's list)", "channels"),
    ("Find videos that beat their own channel's usual (outliers)", "outliers"),
    ("Search a niche: find channels, then the outliers across them", "niche"),
    ("Find fresh clips worth using", "clip"),
    ("A channel's videos as a table", "table"),
    ("Who is live right now", "live"),
    ("Tags and title words across some videos", "tags"),
    ("Back", "back"),
]
WHICH_VIDEOS = [("Videos", "videos"), ("Shorts", "shorts"), ("Live streams", "streams"), ("All of these", "all")]
TABLE_ORDER = [("The newest first", "latest"), ("The oldest first", "oldest"), ("The most viewed first", "popular")]
STITCH_TRANSITIONS = [("A fade (the usual)", "fade"), ("A hard cut, nothing in between", "cut"),
                      ("A different one each time", "random"), ("Another one, by name", "other")]
STITCH_ORDERS = [("Sorted by name", "name"), ("Sorted by date", "date"), ("Smallest files first", "size"),
                 ("Shortest videos first", "duration"), ("Mixed up", "shuffle")]
STINGER_SIZES = [("Vertical, like Shorts (1080x1920)", "1080x1920"), ("Horizontal (1920x1080)", "1920x1080"),
                 ("Square (1080x1080)", "1080x1080"), ("Another size", "other")]


# ---------------------------------------------------------------- checks on typed answers
def _something(what):
    return lambda text: None if text.strip() else f"Type {what}."


def _channels_problem(text):
    return None if text.split() else "Type at least one channel: a name like @Name, or a link (spaces between them)."


def _videos_problem(text):
    words = text.split()
    if not words:
        return "Type at least one YouTube video link or id (spaces between them)."
    for w in words:
        if chan.video_id(w) is None:
            return f"That is not a YouTube video link or id: {w[:40]}"
    return None


def _paths_problem(text):
    try:
        words = shlex.split(text)
    except ValueError:
        return "A quote is not closed."
    return None if words else "Name at least one video file, folder or pattern like clips/*.mp4."


def _video_name_problem(text):
    t = text.strip()
    if Path(t).suffix.lower() not in stitch_mod.OUTPUT_EXTS:
        return f"Use a name ending in {', '.join(stitch_mod.OUTPUT_EXTS)}."
    return None


def _csv_problem(text):
    p = Path(text.strip()).expanduser()
    if not text.strip() or p.suffix.lower() != ".csv":
        return "Use a file name ending in .csv."
    if p.is_dir() or not p.parent.is_dir():
        return "That folder does not exist." if not p.parent.is_dir() else "That is a folder, not a file."
    return None


def _size_or_nothing(text):
    t = text.strip().lower()
    if not t:
        return None
    try:
        stitch_mod.engine.parse_resolution(t)
    except stitch_mod.EngineError:
        return "Type a size like 1920x1080 or 1080p, or leave it empty."
    return None


def _stinger_size_problem(text):
    try:
        stingers_mod.engine.parse_size(text)
    except stingers_mod.EngineError:
        return "Type a size like 1080x1920."
    return None


class ToolFlows:
    """Mixed into Menu. Needs self.ask, self.say, self.run, self._do and self._cmdline."""

    def _go(self, argv, make_label):
        """For commands with no plan screen that asks first: show the command, then make it or only describe it."""
        self.say("Command line for this:  " + self._cmdline(argv))
        what = self.ask.select("Ready. What next?", [
            (make_label, "go"), ("Show what would be done, and do nothing", "dry"), ("Go back to the menu", "back")])
        if what != "back":
            self.run(argv + (["--dry-run"] if what == "dry" else []))

    # ------------------------------------------------------------ research
    def flow_research(self):
        while True:
            tool = self.ask.select("Research (it only reads from YouTube and changes nothing)", RESEARCH)
            if tool == "back":
                return
            try:
                argv = ["research", tool] + getattr(self, "_research_" + tool)()
                if self.ask.confirm("Also save the results to a CSV file?", default=False):
                    name = self.ask.text("What should the file be called?", default=f"{tool}.csv", validate=_csv_problem)
                    argv += ["--csv", name.strip()]
                self._do(argv)
            except GoBack:
                continue

    def _scan_options(self, default_type):
        """Which videos, how far above usual counts as standing out, how many to show: the questions outliers and
        niche share. Only what differs from the usual becomes a flag."""
        argv = []
        tab = self.ask.select("Which videos?", WHICH_VIDEOS, default=default_type)
        if tab != "videos":
            argv += ["--type", tab]
        times = self.ask.text("How far above its channel's usual must a video be, in times the median?",
                              default="3", validate=answers.number)
        if float(times) != 3:
            argv += ["--multiplier", times.strip()]
        top = self.ask.text("Show at most how many? (leave empty for all)", validate=answers.whole_number_or_nothing).strip()
        return argv + (["--top", top] if top else [])

    def _research_channels(self):
        how = self.ask.select("How should it find channels?", [
            ("Search YouTube for a word or phrase", "search"), ("Look at the channels another channel features", "seed")])
        if how == "search":
            argv = ["--search", self.ask.text("What should it search for?", validate=_something("something to search for")).strip()]
        else:
            argv = ["--seed", self.ask.text("Which channel? (a name like @Name, or a link)", validate=answers.channel_problem).strip()]
        if self.ask.confirm("Also look up each channel's subscribers and verified badge? (slower: one request each)", default=False):
            argv.append("--with-subs")
        return argv

    def _research_outliers(self):
        channels = self.ask.text("Which channels? (names like @Name, or links, with spaces between them)", validate=_channels_problem)
        return channels.split() + self._scan_options("videos")

    def _research_niche(self):
        word = self.ask.text("What should it search for?", validate=_something("something to search for")).strip()
        return ["--search", word] + self._scan_options("videos")

    def _research_clip(self):
        argv = ["--search", self.ask.text("What should it search for?", validate=_something("something to search for")).strip()]
        tab = self.ask.select("Which videos?", WHICH_VIDEOS, default="all")
        if tab != "all":
            argv += ["--type", tab]
        times = self.ask.text("How far above its channel's usual must a video be, in times the median?",
                              default="3", validate=answers.number)
        if float(times) != 3:
            argv += ["--multiplier", times.strip()]
        days = self.ask.text("How many days old can a clip be?", default="7", validate=answers.whole_number).strip()
        if days != "7":
            argv += ["--max-age-days", days]
        top = self.ask.text("Show at most how many? (leave empty for all)", default="20", validate=answers.whole_number_or_nothing).strip()
        if not top:
            argv += ["--top", "0"]
        elif top != "20":
            argv += ["--top", top]
        return argv

    def _research_table(self):
        argv = [self.ask.text("Which channel? (a name like @Name, or a link)", validate=answers.channel_problem).strip()]
        tab = self.ask.select("Which videos?", WHICH_VIDEOS, default="videos")
        if tab != "videos":
            argv += ["--type", tab]
        order = self.ask.select("Which come first?", TABLE_ORDER)
        if order != "latest":
            argv += ["--sort", order]
        limit = self.ask.text("Show at most how many? (leave empty for all)", validate=answers.whole_number_or_nothing).strip()
        if limit:
            argv += ["--limit", limit]
        if self.ask.confirm("Look up exact dates and lengths? (slower: one request per video)", default=False):
            argv.append("--full")
        return argv

    def _research_live(self):
        return self.ask.text("Which channels? (names like @Name, or links, with spaces between them)",
                             validate=_channels_problem).split()

    def _research_tags(self):
        where = self.ask.select("Where are the videos?", [("I will paste links or ids", "paste"), ("They are listed in a file", "file")])
        if where == "paste":
            return self.ask.text("Paste the video links or ids, with spaces between them:", validate=_videos_problem).split()
        return ["--videos", self.ask.text("Which file? (one video link or id per line)", validate=answers.videos_file_problem).strip()]

    # ------------------------------------------------------------ stitch
    def flow_stitch(self):
        argv = ["stitch"] + shlex.split(self.ask.text(
            "Which videos? (files, folders or patterns like clips/*.mp4, with spaces between them)", validate=_paths_problem))
        name = self.ask.text("What should the new video file be called?", default="compilation.mp4",
                             validate=_video_name_problem).strip()
        if name != "compilation.mp4":
            argv += ["-o", name]
        kind = self.ask.select("What goes between the videos?", STITCH_TRANSITIONS)
        if kind == "other":
            kind = self.ask.text("Which one? (`ytt stitch --list-transitions` shows them all)",
                                 validate=lambda t: None if t.strip() and not any(c.isspace() for c in t.strip())
                                 else "Type one name, with no spaces.").strip()
        if kind != "fade":
            argv += ["-t", kind]
        if kind != "cut":
            seconds = self.ask.text("How long should each one last, in seconds?", default="1", validate=answers.number).strip()
            if float(seconds) != 1:
                argv += ["-d", seconds]
        more = self.ask.checkbox("Change anything else?", [
            ("The order of the videos", "order"), ("The size and speed (frames per second)", "size"), ("Leave out the sound", "silent")])
        if "order" in more:
            how = self.ask.select("In what order?", STITCH_ORDERS)
            argv += ["--shuffle"] if how == "shuffle" else ["--sort", how]
        if "size" in more:
            size = self.ask.text("Size, like 1920x1080 or 1080p (leave empty for the first video's):",
                                 validate=_size_or_nothing).strip()
            if size:
                argv += ["--resolution", size]
            fps = self.ask.text("Frames per second? (leave empty for the first video's)", validate=answers.number_or_nothing).strip()
            if fps:
                argv += ["--fps", fps]
        if "silent" in more:
            argv.append("--no-audio")
        self._go(argv, "Make the video")

    # ------------------------------------------------------------ doctor
    def flow_doctor(self):
        self._do(["doctor"])

    # ------------------------------------------------------------ transition videos
    def _style_stingers(self):
        argv = ["style", "stingers"]
        folder = self.ask.text("Which folder should they go in?", default="stingers", validate=_something("a folder name")).strip()
        if folder != "stingers":
            argv += ["-o", folder]
        size = self.ask.select("What shape are your videos?", STINGER_SIZES)
        if size == "other":
            size = self.ask.text("Size, like 1080x1920:", validate=_stinger_size_problem).strip()
        if size != "1080x1920":
            argv += ["--size", size]
        if self.ask.select("Which ones?", [("All of them", "all"), ("Only some", "some")]) == "some":
            picked = self.ask.checkbox("Which ones?", [(f"{n}: {h}", n) for n, h in stingers_mod.list_stingers()], required=True)
            argv += ["--only", ",".join(picked)]
        word = self.ask.text("The word on the starburst (leave empty for none)", default="CRUNCH!")
        if word != "CRUNCH!":
            argv += ["--text", word]
        self._go(argv, "Make them")
