"""
Offline tests for auto_compile.py. No network and no YouTube: the channel list and the downloads are faked
(downloads are copies of one tiny generated clip), but ffmpeg really renders the compilations.

Run from the repo folder:   python -m unittest discover -s tests -v
Needs: ffmpeg + ffprobe on PATH, and  pip install yt-dlp tabulate
"""
import builtins
import contextlib
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
MODULES = ("auto_compile", "make_compilations", "yt_toolkit", "stitch_videos")
HAVE_TOOLS = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
try:
    import yt_dlp  # noqa: F401
    HAVE_YTDLP = True
except ImportError:
    HAVE_YTDLP = False

_TEMPLATE = {}


def template_clip():
    """One 1-second test clip, generated once and copied for every 'download'."""
    if "path" not in _TEMPLATE:
        d = Path(tempfile.mkdtemp(prefix="ac_tmpl_"))
        p = d / "t.mp4"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=96x96:rate=25",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-shortest",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(p)], check=True)
        _TEMPLATE["path"] = p
    return _TEMPLATE["path"]


def make_rows(n, newest=datetime(2026, 9, 30, 12, tzinfo=timezone.utc), title="Clip {i}", duration=20):
    """n fake channel entries, newest first, one per day going back. ids are vid00000000, vid00000001, ... (11 chars like real ones)"""
    return [{"id": f"vid{i:08d}", "title": title.format(i=i), "views": 1000 + i, "duration": duration,
             "sort_ts": (newest - timedelta(days=i)).timestamp(), "approx": False,
             "upload_date": (newest - timedelta(days=i)).strftime("%Y-%m-%d")} for i in range(n)]


@unittest.skipUnless(HAVE_TOOLS and HAVE_YTDLP, "needs ffmpeg and yt-dlp")
class WorldTest(unittest.TestCase):
    """A scratch copy of the scripts (so state files stay out of the repo) wired to a fake YouTube."""

    CLIPS_PER_VIDEO = 4

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ac_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for name in MODULES + ("make_transitions",):          # make_transitions is only run as a script
            shutil.copy(REPO / f"{name}.py", self.tmp / f"{name}.py")
        for name in MODULES:
            sys.modules.pop(name, None)
        sys.path.insert(0, str(self.tmp))
        self.addCleanup(sys.path.remove, str(self.tmp))
        self.addCleanup(lambda: [sys.modules.pop(n, None) for n in MODULES])
        self.ac = importlib.import_module("auto_compile")
        self.rows = make_rows(40)
        self.fail_ids = set()
        self.downloads = []                      # video ids "downloaded", in call order
        self.write_compile_settings()
        self.ac.list_tab = lambda *a, **k: self.rows
        self.ac.download_one = self.fake_download

    # ---- fake world ----
    def fake_download(self, url, outtmpl, max_height, cookies_opts, **kw):
        vid = url.rsplit("=", 1)[-1]
        if vid in self.fail_ids:
            raise RuntimeError("HTTP Error 403: Forbidden")
        self.downloads.append(vid)
        shutil.copy(template_clip(), outtmpl.replace("%(ext)s", "mp4"))

    def write_compile_settings(self, **over):
        s = {"size_mode": "count", "clips_per_video": self.CLIPS_PER_VIDEO, "transition": "cut", "quality": "fast",
             "output_dir": str(self.tmp / "compilations"), "clips_dir": ""}
        s.update(over)
        (self.tmp / "compile_settings.json").write_text(json.dumps(s))

    # ---- helpers ----
    @property
    def dest(self):
        return self.tmp / "fetched"

    def state(self, name):
        p = self.tmp / name
        return json.loads(p.read_text()) if p.exists() else {}

    def clips_on_disk(self):
        return sorted(p.name for p in self.dest.glob("*.mp4")) if self.dest.exists() else []

    def made(self):
        return sorted(self.state("compile_ledger.json").get("compilations", {}))

    def run_ac(self, *argv, inputs=(), tty=False):
        """Run auto_compile.main() with fake input. Returns everything it printed."""
        answers = iter(inputs)
        out = io.StringIO()
        real_run = subprocess.run

        def captured_run(cmd, **kw):                      # make_compilations runs for real; keep its output
            p = real_run(cmd, capture_output=True, text=True)
            out.write(p.stdout + p.stderr)
            return p

        patches = [mock.patch.object(sys, "argv", ["auto_compile.py", *argv]),
                   mock.patch.object(builtins, "input", lambda *_: next(answers, "")),
                   mock.patch.object(self.ac.subprocess, "run", captured_run)]
        if hasattr(self.ac, "is_tty"):
            patches.append(mock.patch.object(self.ac, "is_tty", lambda: tty))
        with contextlib.ExitStack() as st, contextlib.redirect_stdout(out):
            for p in patches:
                st.enter_context(p)
            try:
                self.ac.main()
            except SystemExit as e:                       # sys.exit("message") is how it reports a stop
                if e.code not in (None, 0):
                    out.write(str(e.code) + "\n")
        return out.getvalue()

    def base(self, *extra):
        return ["@Chan", "--type", "shorts", "--sort", "oldest", "--dest", str(self.dest), "--yes", *extra]


class BaselineTests(WorldTest):
    def test_downloads_then_compiles_whole_compilations(self):
        out = self.run_ac(*self.base("--pick", "8"))
        self.assertEqual(len(self.downloads), 8, out)
        self.assertEqual(len(self.made()), 2, out)
        self.assertEqual(len(self.state("fetch_archive.json")), 8)

    def test_stale_date_range_is_not_carried_into_the_next_interactive_run(self):
        # regression: an old FROM date used to come back as the Enter-default and silently shrank the list
        (self.tmp / "auto_settings.json").write_text(json.dumps(
            {"channel": "@Chan", "type": "shorts", "sort": "oldest", "pick": "5", "date_from": "2026-09-30",
             "dest": str(self.dest)}))
        out = self.run_ac("--dry-run", inputs=[])
        self.assertIn("take the first 5 of the 40 in that list", out)

    def test_failed_download_is_replaced_by_next_in_line_and_order_is_kept(self):
        self.fail_ids = {"vid00000039"}                       # the oldest one fails (sort is oldest-first)
        out = self.run_ac(*self.base("--pick", "new:8"))
        self.assertEqual(len(self.clips_on_disk()), 8, out)
        self.assertNotIn("vid00000039", self.state("fetch_archive.json"))
        ids = [n.rsplit("_", 1)[-1][:-4] for n in self.clips_on_disk()]
        self.assertEqual(ids, [f"vid{i:08d}" for i in range(38, 30, -1)])   # oldest-first order survived

    def test_delete_after_removes_used_clips_but_keeps_unused_ones(self):
        out = self.run_ac(*self.base("--pick", "6", "--delete-after"))   # 4 get compiled, 2 are left over
        self.assertEqual(len(self.made()), 1, out)
        self.assertEqual(len(self.clips_on_disk()), 2, out)

    def test_pick_parsing(self):
        p = self.ac.parse_pick
        self.assertEqual(p("60"), ("first", 60))
        self.assertEqual(p("new:7"), ("new", 7))
        self.assertEqual(p("last:3"), ("last", 3))


class DetectionTests(WorldTest):
    def test_clips_already_on_disk_are_adopted_not_redownloaded(self):
        self.dest.mkdir()
        other = self.tmp / "other"
        other.mkdir()
        shutil.copy(template_clip(), self.dest / "20260101-0000_0001_vid00000039.mp4")     # our own naming
        shutil.copy(template_clip(), self.dest / "Some Title [vid00000038].mp4")          # yt-dlp's naming
        shutil.copy(template_clip(), other / "mine_vid00000037.mp4")                      # a different folder
        out = self.run_ac(*self.base("--pick", "new:5", "--no-compile", "--check-folder", str(other)))
        self.assertIn("3 videos already on your disk", out)
        self.assertIn("1 in other folders", out)
        self.assertEqual(sorted(self.downloads), [f"vid{i:08d}" for i in range(32, 37)])  # 36..32 fetched
        archive = self.state("fetch_archive.json")
        self.assertTrue(archive["vid00000038"]["adopted"])
        self.assertEqual(self.state("auto_settings.json")["check_folders"], [str(other.resolve())])

    def test_adopting_is_skipped_by_redownload(self):
        self.dest.mkdir()
        shutil.copy(template_clip(), self.dest / "x_vid00000039.mp4")
        self.run_ac(*self.base("--pick", "2", "--no-compile", "--redownload"))
        self.assertIn("vid00000039", self.downloads)

    def test_reuploads_with_same_title_and_length_are_skipped(self):
        self.rows[39]["title"] = self.rows[38]["title"] = self.rows[37]["title"] = "Same Clip!"   # three look-alikes
        out = self.run_ac(*self.base("--pick", "new:4", "--no-compile"))
        self.assertIn("2 videos look like re-uploads", out)
        self.assertEqual(sorted(self.downloads),                       # first of the three kept, then 36, 35, 34
                         ["vid00000034", "vid00000035", "vid00000036", "vid00000039"])

    def test_lookalike_of_an_archived_clip_is_skipped_and_keep_duplicates_overrides(self):
        (self.tmp / "fetch_archive.json").write_text(json.dumps(
            {"someOtherId1": {"file": "gone.mp4", "title": "clip 39", "duration": 20}}))        # re-upload of row 39
        out = self.run_ac(*self.base("--pick", "new:2", "--no-compile"))
        self.assertNotIn("vid00000039", self.downloads, out)
        self.downloads.clear()
        self.run_ac(*self.base("--pick", "new:2", "--no-compile", "--keep-duplicates"))
        self.assertIn("vid00000039", self.downloads)

    def test_titles_without_a_length_are_never_called_duplicates(self):
        for r in self.rows:
            r["title"], r["duration"] = "Same", None
        self.run_ac(*self.base("--pick", "new:3", "--no-compile"))
        self.assertEqual(len(self.downloads), 3)


class CompilationPlanTests(WorldTest):
    def put_waiting(self, n):
        self.dest.mkdir(exist_ok=True)
        for i in range(n):
            shutil.copy(template_clip(), self.dest / f"waiting_{i}.mp4")

    def test_parse_and_describe_comps(self):
        self.assertEqual(self.ac.parse_pick("comps:3"), ("comps", 3))
        self.assertEqual(self.ac.describe_pick("comps:1"), "enough new videos for 1 compilation")
        with self.assertRaises(ValueError):
            self.ac.apply_pick(self.rows, "comps:3", set())

    def test_downloads_exactly_what_is_missing_for_n_full_compilations(self):
        out = self.run_ac(*self.base("--compilations", "2"))              # 2 x 4 clips, nothing waiting
        self.assertEqual(len(self.downloads), 8, out)
        self.assertEqual(len(self.made()), 2, out)
        self.assertEqual(len(self.clips_on_disk()), 8)                    # all used, none left over

    def test_clips_already_waiting_are_counted(self):
        self.put_waiting(3)
        out = self.run_ac(*self.base("--pick", "comps:2"))
        self.assertIn("3 clips already waiting", out)
        self.assertEqual(len(self.downloads), 5, out)                     # 8 needed - 3 waiting
        self.assertEqual(len(self.made()), 2, out)

    def test_no_download_when_enough_clips_are_waiting(self):
        self.put_waiting(9)
        out = self.run_ac(*self.base("--compilations", "2"))
        self.assertEqual(self.downloads, [], out)
        self.assertIn("already enough", out)
        self.assertEqual(len(self.made()), 2, out)                        # exactly 2 even though 9 clips wait
        self.assertEqual(len(self.state("compile_ledger.json")["compilations"]), 2)

    def test_limited_by_what_the_channel_has(self):
        self.rows = make_rows(6)
        out = self.run_ac(*self.base("--compilations", "2"))
        self.assertEqual(len(self.downloads), 6, out)
        self.assertIn("Only 6 new videos are available", out)
        self.assertIn("1 full compilation at most", out)
        self.assertEqual(len(self.made()), 1, out)

    def test_by_minutes_the_size_is_estimated_from_clip_lengths(self):
        self.write_compile_settings(size_mode="minutes", minutes_per_video=1)    # 60s / 20s clips = 3 clips
        out = self.run_ac(*self.base("--compilations", "2", "--dry-run"))
        self.assertIn("2 compilations of about 3 clips", out)
        self.assertIn("downloading 6 new", out)

    def test_wizard_offers_compilations_as_the_first_pick_option(self):
        # channel, shorts, no from, no to, oldest, pick option 1 (compilations), 2 of them, defaults for the rest
        out = self.run_ac("--dry-run", "--dest", str(self.dest), inputs=["@Chan", "2", "", "", "3", "1", "2"])
        self.assertIn("1) Enough new videos for N compilations", out)
        self.assertIn("make 2 compilations of 4 clips each = 8 clips", out)
        self.assertEqual(self.state("auto_settings.json")["pick"], "comps:2")

    def test_nothing_available_and_nothing_waiting_is_a_clear_stop(self):
        self.rows = make_rows(3)
        (self.tmp / "fetch_archive.json").write_text(json.dumps({r["id"]: {"file": "x.mp4"} for r in self.rows}))
        out = self.run_ac(*self.base("--compilations", "1"))
        self.assertIn("Nothing to do", out)


class LeftoverTests(WorldTest):
    """6 clips with 4 per compilation = 1 compilation and 2 clips left over."""

    def test_default_without_a_person_present_is_keep(self):
        out = self.run_ac(*self.base("--pick", "6"))
        self.assertEqual(len(self.made()), 1, out)
        self.assertIn("Kept 2 leftover clips for next time", out)

    def test_short_makes_a_shorter_compilation_from_the_leftovers(self):
        out = self.run_ac(*self.base("--pick", "6", "--leftover", "short"))
        self.assertEqual(len(self.made()), 2, out)
        self.assertEqual(len(self.clips_on_disk()), 6)

    def test_topup_downloads_just_enough_more_and_completes_a_full_one(self):
        out = self.run_ac(*self.base("--pick", "6", "--leftover", "topup"))
        self.assertEqual(len(self.downloads), 8, out)                   # 6, then 2 more
        self.assertEqual(sorted(self.downloads[6:]), ["vid00000032", "vid00000033"])   # the next two in line
        self.assertEqual(len(self.made()), 2, out)
        names = self.clips_on_disk()
        self.assertEqual(names, sorted(names))                          # top-up files sort after the first batch

    def test_topup_falls_back_to_keep_when_the_list_has_nothing_more(self):
        self.rows = make_rows(6)
        out = self.run_ac(*self.base("--pick", "6", "--leftover", "topup"))
        self.assertIn("isn't possible here", out)
        self.assertEqual(len(self.made()), 1, out)

    def test_the_users_real_case_twelve_clips_but_fifteen_needed(self):
        self.write_compile_settings(clips_per_video=15)
        self.rows = make_rows(12)
        out = self.run_ac(*self.base("--compilations", "1", "--leftover", "short"))
        self.assertEqual(len(self.downloads), 12, out)
        self.assertEqual(len(self.made()), 1, out)                      # one shorter compilation from all 12

    def test_interactive_prompt_offers_options_and_remembers_the_choice(self):
        out = self.run_ac("@Chan", "--type", "shorts", "--sort", "oldest", "--dest", str(self.dest),
                          "--pick", "6", inputs=["y", "3"], tty=True)        # Start? y, then option 3 = top up
        self.assertIn("2 clips left over - a full compilation needs 4.", out)
        self.assertIn("3) Download 2 more to fill one", out)
        self.assertEqual(len(self.made()), 2, out)
        self.assertEqual(self.state("auto_settings.json")["leftover"], "topup")

    def test_no_nagging_when_the_requested_number_of_compilations_was_made(self):
        self.dest.mkdir()
        for i in range(6):
            shutil.copy(template_clip(), self.dest / f"waiting_{i}.mp4")
        out = self.run_ac("@Chan", "--type", "shorts", "--dest", str(self.dest), "--compilations", "1",
                          inputs=["y"], tty=True)
        self.assertEqual(len(self.made()), 1, out)
        self.assertNotIn("left over", out)                              # 2 spare clips, but 1 compilation was asked for


class ParallelTests(WorldTest):
    def test_order_of_file_names_follows_the_queue_even_if_downloads_finish_out_of_order(self):
        import time as _t
        real = self.fake_download

        def slow_first(url, outtmpl, *a, **k):
            if url.endswith("vid00000039"):
                _t.sleep(0.4)                               # the first in line finishes last
            return real(url, outtmpl, *a, **k)
        self.ac.download_one = slow_first
        self.run_ac(*self.base("--pick", "6", "--no-compile", "--workers", "3"))
        ids = [n.rsplit("_", 1)[-1][:-4] for n in self.clips_on_disk()]
        self.assertEqual(ids, [f"vid{i:08d}" for i in range(39, 33, -1)])

    def test_a_failure_is_replaced_by_the_next_in_line(self):
        self.fail_ids = {"vid00000038", "vid00000036"}
        out = self.run_ac(*self.base("--pick", "new:6", "--no-compile", "--workers", "3"))
        self.assertEqual(len(self.clips_on_disk()), 6, out)
        self.assertEqual(len(self.state("fetch_archive.json")), 6)
        self.assertIn("skipped #2", out)

    def test_never_downloads_more_than_asked(self):
        self.run_ac(*self.base("--pick", "new:5", "--no-compile", "--workers", "4"))
        self.assertEqual(len(self.downloads), 5)

    def test_workers_1_still_works_one_by_one(self):
        out = self.run_ac(*self.base("--pick", "4", "--workers", "1"))
        self.assertEqual(len(self.made()), 1, out)
        self.assertIn("[1/4] #1", out)

    def test_ctrl_c_keeps_what_was_downloaded_and_stops_cleanly(self):
        real = self.fake_download

        def boom(url, outtmpl, *a, **k):
            if url.endswith("vid00000036"):
                raise KeyboardInterrupt
            return real(url, outtmpl, *a, **k)
        self.ac.download_one = boom
        out = self.run_ac(*self.base("--pick", "6", "--workers", "3"))
        self.assertIn("Stopped. Clips downloaded so far are kept", out)
        self.assertEqual(self.made(), [])                   # no compilation after an interrupt


class AgainAndSummaryTests(WorldTest):
    def test_again_repeats_the_last_run_with_the_next_batch_and_keeps_its_date_range(self):
        first = self.run_ac(*self.base("--compilations", "1", "--from", "2026-09-01"))
        batch1 = list(self.downloads)
        self.assertEqual(len(batch1), 4, first)
        self.downloads.clear()
        out = self.run_ac("--again", "--yes")
        self.assertIn("Repeating your last run: @Chan (shorts), order by oldest, "
                      "enough new videos for 1 compilation, uploaded 2026-09-01 to today.", out)
        self.assertEqual(len(self.downloads), 4, out)
        self.assertFalse(set(batch1) & set(self.downloads))              # a fresh batch, not the same videos
        self.assertEqual(len(self.made()), 2, out)
        self.assertEqual(self.state("auto_settings.json")["date_from"], "2026-09-01")

    def test_again_overrides_with_flags(self):
        self.run_ac(*self.base("--compilations", "1"))
        self.downloads.clear()
        self.run_ac("--again", "--yes", "--compilations", "2")
        self.assertEqual(len(self.downloads), 8)

    def test_again_needs_a_previous_run_and_no_channel(self):
        self.assertIn("nothing to repeat yet", self.run_ac("--again", "--yes"))
        self.assertIn("leave the channel out", self.run_ac("--again", "@Chan"))

    def test_summary_lists_what_was_made_what_waits_and_how_to_go_again(self):
        out = self.run_ac(*self.base("--pick", "6"))
        tail = out[out.rindex("=" * 50):]
        self.assertIn("All done: 1 compilation made.", tail)
        self.assertIn("compilation_001.mp4", tail)
        self.assertIn("2 clips waiting in fetched/ for the next batch.", tail)
        self.assertIn("python auto_compile.py --again --yes", tail)


class FirstRunTests(WorldTest):
    def test_compilation_setup_is_asked_before_the_plan_and_points_at_the_download_folder(self):
        (self.tmp / "compile_settings.json").unlink()
        out = self.run_ac(*self.base("--compilations", "1", "--leftover", "keep"))   # every setup question: Enter
        self.assertLess(out.index("First time: a short one-time setup"), out.index("Plan: make 1 compilation of 15"))
        self.assertEqual(self.state("compile_settings.json")["clips_dir"], str(self.dest.resolve()))
        self.assertEqual(len(self.downloads), 15, out)
        self.assertEqual(len(self.made()), 1, out)


class NothingNewButClipsWaitingTests(WorldTest):
    """The user's second run: all 12 videos in range were fetched earlier and 12 clips (15 needed) still wait."""

    def setUp(self):
        super().setUp()
        self.write_compile_settings(clips_per_video=15)
        self.rows = make_rows(12)
        self.run_ac(*self.base("--pick", "new:30", "--no-compile"))          # the earlier run: 12 downloaded, not compiled
        self.assertEqual(len(self.clips_on_disk()), 12)
        self.downloads.clear()

    def test_does_not_dead_end_it_works_on_the_waiting_clips(self):
        out = self.run_ac(*self.base("--pick", "new:30", "--leftover", "short"))
        self.assertEqual(self.downloads, [], out)
        self.assertIn("12 clips are waiting", out)
        self.assertEqual(len(self.made()), 1, out)                           # one shorter compilation from the 12

    def test_by_hand_it_offers_the_leftover_choices(self):
        out = self.run_ac("@Chan", "--type", "shorts", "--sort", "oldest", "--dest", str(self.dest),
                          "--pick", "new:30", inputs=["y", "2"], tty=True)   # Start? y ... then 2 = shorter one
        self.assertIn("12 clips left over - a full compilation needs 15.", out)
        self.assertEqual(len(self.made()), 1, out)

    def test_no_misleading_range_note_when_the_range_cut_nothing_off(self):
        out = self.run_ac(*self.base("--pick", "new:30", "--from", "2026-01-01", "--leftover", "keep"))
        self.assertNotIn("narrowed", out)

    def test_with_nothing_waiting_it_still_stops_with_the_old_message(self):
        for f in self.dest.glob("*.mp4"):
            f.unlink()
        out = self.run_ac(*self.base("--pick", "new:30"))
        self.assertIn("Nothing new: every video in that list has already been downloaded", out)


class CompilationLookTests(WorldTest):
    WIZARD = ["", "", "", "", "", "", "", "2"]       # keep folder/size/clips/order/transition/intro/outro; quality = balanced

    def test_the_plan_shows_how_the_compilations_will_look(self):
        out = self.run_ac(*self.base("--compilations", "1", "--dry-run"))
        self.assertIn("Compilations: 4 clips each, hard cuts, fast quality   (change with --setup)", out)

    def test_look_is_not_shown_when_not_compiling(self):
        out = self.run_ac(*self.base("--pick", "2", "--no-compile"))
        self.assertNotIn("Compilations:", out)

    def test_interactive_question_8_shows_the_look_and_n_keeps_it(self):
        out = self.run_ac("--dest", str(self.dest), "--dry-run",
                          inputs=["@Chan", "2", "", "", "3", "1", "1", "", ""])
        self.assertNotIn("8. Compilations", out)                      # dry runs have no side effects, so no question

        out = self.run_ac("--dest", str(self.dest), inputs=["@Chan", "2", "", "", "3", "1", "1", "", "", "n"])
        self.assertIn("8. Compilations: 4 clips each, hard cuts, fast quality", out)
        self.assertEqual(self.state("compile_settings.json")["quality"], "fast")

    def test_interactive_question_8_yes_reruns_the_setup_with_current_choices_as_defaults(self):
        out = self.run_ac("--dest", str(self.dest),
                          inputs=["@Chan", "2", "", "", "3", "1", "1", "", "", "y", *self.WIZARD])
        self.assertIn("Compilation setup. Your current choices are the defaults", out)
        cfg = self.state("compile_settings.json")
        self.assertEqual((cfg["quality"], cfg["transition"], cfg["clips_per_video"]), ("balanced", "cut", 4))
        self.assertIn("balanced quality", out)                        # the plan already uses the new choices
        self.assertEqual(len(self.made()), 1, out)

    def test_setup_flag_reruns_the_setup_in_one_line_mode(self):
        out = self.run_ac(*self.base("--compilations", "1", "--setup"), inputs=self.WIZARD)
        self.assertEqual(self.state("compile_settings.json")["quality"], "balanced")
        self.assertIn("Compilation setup.", out)


class DateRangeUsedUpTests(WorldTest):
    """The user's third run: every short in the date range was fetched and used; hundreds more exist outside it."""

    def setUp(self):
        super().setUp()
        # rows 0..5 are the ones uploaded 2026-09-25..09-30: all downloaded already, nothing waiting
        (self.tmp / "fetch_archive.json").write_text(json.dumps(
            {f"vid{i:08d}": {"file": f"gone{i}.mp4", "title": f"Clip {i}", "duration": 20} for i in range(6)}))
        self.args = ["@Chan", "--type", "shorts", "--sort", "oldest", "--dest", str(self.dest),
                     "--from", "2026-09-25", "--compilations", "1"]

    def test_by_hand_it_offers_to_look_beyond_the_date_range(self):
        out = self.run_ac(*self.args, inputs=["y", "y"], tty=True)         # widen? y   Start? y
        self.assertIn("Everything in your date range (2026-09-25 to today) is already downloaded: 6 shorts. "
                      "34 more exist outside it.", out)
        self.assertEqual(len(self.downloads), 4, out)
        self.assertEqual(sorted(self.downloads), [f"vid{i:08d}" for i in range(36, 40)])   # the oldest four
        self.assertEqual(len(self.made()), 1, out)
        self.assertEqual(self.state("auto_settings.json")["date_from"], "")

    def test_saying_no_keeps_the_range_and_stops(self):
        out = self.run_ac(*self.args, inputs=["n"], tty=True)
        self.assertEqual(self.downloads, [], out)
        self.assertIn("Nothing to do", out)

    def test_unattended_never_asks_and_never_widens(self):
        out = self.run_ac(*self.args, "--yes")
        self.assertNotIn("Look at those too", out)
        self.assertEqual(self.downloads, [])
        self.assertIn("Clear or widen it", out)


try:
    import PIL  # noqa: F401
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


class TransitionLookTests(WorldTest):
    """How transition VIDEOS are shown (background colour removal, sound), chosen instead of auto-detected."""

    def mc(self):
        return self.ac.mc

    def wizard(self, answers, **cfg):
        out = io.StringIO()
        base = {**self.mc().DEFAULTS, "output_dir": str(self.tmp / "compilations"), **cfg}
        it = iter(answers)
        with mock.patch.object(builtins, "input", lambda *_: next(it, "")), contextlib.redirect_stdout(out):
            self.mc().wizard(base, ask_folder=False)
        return out.getvalue(), self.state("compile_settings.json")

    def stitch_command(self, **over):
        mc, seen = self.mc(), []
        s = {**mc.DEFAULTS, "quality": "fast", **over}
        with mock.patch.object(mc.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or mock.Mock(returncode=1)):
            mc.render([Path("a.mp4"), Path("b.mp4")], self.tmp / "o.mp4", s, ("96x96", "25"))
        return seen[0]

    # ---- what reaches stitch_videos.py
    def test_the_chosen_look_is_passed_to_stitch(self):
        cmd = self.stitch_command(stinger_dir="S", stinger_key="none", stinger_audio=False,
                                  stinger_despill=True, stinger_sim=0.3, stinger_blend=0.1)
        self.assertEqual(cmd[cmd.index("--key") + 1], "none")
        self.assertEqual(cmd[cmd.index("--key-similarity") + 1], "0.3")
        self.assertEqual(cmd[cmd.index("--key-blend") + 1], "0.1")
        self.assertIn("--despill", cmd)
        self.assertIn("--no-stinger-audio", cmd)

    def test_defaults_keep_the_old_behaviour(self):
        cmd = self.stitch_command(stinger_dir="S")
        self.assertEqual(cmd[cmd.index("--key") + 1], "auto")
        self.assertNotIn("--despill", cmd)
        self.assertNotIn("--no-stinger-audio", cmd)

    def test_no_transition_video_flags_without_a_transition_folder(self):
        self.assertNotIn("--key", self.stitch_command())

    # ---- the questions
    def test_wizard_lets_you_choose_colour_sound_and_fine_tuning(self):
        out, cfg = self.wizard(["", "", "", "", "4", str(self.tmp),          # ... transitions = own videos, folder
                                "3", "n", "y", "0.2", "0.1", "y"])            # green, no sound, fine-tune: 0.2 / 0.1 / despill
        self.assertIn("Background of the transition videos:", out)
        self.assertEqual((cfg["transition"], cfg["stinger_key"], cfg["stinger_audio"]), ("stinger", "green", False))
        self.assertEqual((cfg["stinger_sim"], cfg["stinger_blend"], cfg["stinger_despill"]), (0.2, 0.1, True))

    def test_wizard_custom_colour_is_validated_and_normalised(self):
        _, cfg = self.wizard(["", "", "", "", "4", str(self.tmp), "7", "zzz", "#ff00ff"])
        self.assertEqual(cfg["stinger_key"], "FF00FF")

    def test_wizard_none_skips_the_fine_tuning_question(self):
        out, cfg = self.wizard(["", "", "", "", "4", str(self.tmp), "2"])
        self.assertEqual(cfg["stinger_key"], "none")
        self.assertNotIn("Fine-tune", out)

    def test_wizard_keeps_current_choices_when_you_just_press_enter(self):
        _, cfg = self.wizard([], transition="stinger", stinger_dir=str(self.tmp), stinger_key="blue",
                             stinger_audio=False, stinger_sim=0.2)
        self.assertEqual((cfg["stinger_key"], cfg["stinger_audio"], cfg["stinger_sim"]), ("blue", False, 0.2))

    def test_the_questions_are_not_asked_for_other_transitions(self):
        out, _ = self.wizard(["", "", "", "", "1"])                          # smooth fade
        self.assertNotIn("Background of the transition videos", out)

    # ---- what the plan says
    def test_the_plan_line_describes_how_transition_videos_are_shown(self):
        d = self.ac.describe_compile
        base = {**self.mc().DEFAULTS, "transition": "stinger"}
        self.assertIn("your own transition videos (background auto-detected)", d(base))
        self.assertIn("(shown as they are)", d({**base, "stinger_key": "none"}))
        self.assertIn("(green background removed)", d({**base, "stinger_key": "green"}))

    # ---- for real
    @unittest.skipUnless(HAVE_PIL, "needs Pillow to make a transition video")
    def test_real_render_with_a_generated_green_screen_transition_under_each_choice(self):
        subprocess.run([sys.executable, str(self.tmp / "make_transitions.py"), "--only", "burst", "--size", "160x160",
                        "-o", str(self.tmp / "stingers")], check=True, capture_output=True)
        long_clip = self.tmp / "long.mp4"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=3:size=96x96:rate=25",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-shortest", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", str(long_clip)], check=True)
        self.ac.download_one = lambda url, outtmpl, *a, **k: shutil.copy(long_clip, outtmpl.replace("%(ext)s", "mp4"))
        for n, key in enumerate(("green", "none", "auto"), 1):
            self.write_compile_settings(transition="stinger", stinger_dir=str(self.tmp / "stingers"),
                                        stinger_key=key, stinger_audio=(key != "none"))
            out = self.run_ac(*self.base("--compilations", "1"))
            self.assertEqual(len(self.made()), n, f"{key}: {out[-1500:]}")


class RedoCompilerTests(WorldTest):
    """make_compilations.py --redo: same clips, same order, current settings."""

    def setUp(self):
        super().setUp()
        self.run_ac(*self.base("--pick", "8"))                       # compilation_001 and compilation_002
        self.assertEqual(self.made(), ["compilation_001", "compilation_002"])

    def mc_run(self, *args):
        p = subprocess.run([sys.executable, str(self.tmp / "make_compilations.py"), *args, "--clips", str(self.dest)],
                           capture_output=True, text=True, cwd=self.tmp)
        return p.stdout + p.stderr

    def comps(self):
        return self.state("compile_ledger.json")["compilations"]

    def test_redo_makes_a_new_video_from_the_same_clips_in_the_same_order_and_keeps_the_old_one(self):
        before = dict(self.comps())
        out = self.mc_run("--redo", "last")
        after = self.comps()
        self.assertEqual(sorted(after), ["compilation_001", "compilation_002", "compilation_003"], out)
        self.assertEqual(after["compilation_003"]["clips"], before["compilation_002"]["clips"])
        self.assertEqual(after["compilation_002"], before["compilation_002"])          # old entry untouched
        self.assertTrue((self.tmp / "compilations" / "compilation_003.mp4").is_file())
        self.assertTrue((self.tmp / "compilations" / "compilation_002.mp4").is_file())

    def test_replace_overwrites_the_old_video_instead_of_adding_one(self):
        f = Path(self.comps()["compilation_001"]["file"])
        old = f.stat().st_mtime_ns
        out = self.mc_run("--redo", "1", "--replace")
        self.assertEqual(sorted(self.comps()), ["compilation_001", "compilation_002"], out)
        self.assertGreater(f.stat().st_mtime_ns, old)

    def test_reverse_plays_the_same_clips_backwards(self):
        first = self.comps()["compilation_001"]["clips"]
        self.mc_run("--redo", "1", "--reverse")
        self.assertEqual(self.comps()["compilation_003"]["clips"], first[::-1])

    def test_all_and_lists_and_names(self):
        self.mc_run("--redo", "all")
        self.assertEqual(len(self.comps()), 4)
        self.mc_run("--redo", "compilation_001,2")
        self.assertEqual(len(self.comps()), 6)

    def test_unknown_compilation_is_a_clear_error(self):
        self.assertIn("No compilation matches '9'", self.mc_run("--redo", "9"))

    def test_missing_clips_are_reported_not_guessed(self):
        gone = self.comps()["compilation_001"]["clips"][0]
        (self.dest / gone).unlink()
        out = self.mc_run("--redo", "1")
        self.assertIn("Can't remake compilation_001: 1 of its clip no longer in", out)
        self.assertEqual(len(self.comps()), 2)

    def test_dry_run_changes_nothing(self):
        out = self.mc_run("--redo", "all", "--dry-run")
        self.assertIn("compilation_001  (4 clips)  ->  compilation_003", out)
        self.assertEqual(len(self.comps()), 2)

    def test_replace_alone_is_an_error(self):
        self.assertIn("--replace only works together with --redo", self.mc_run("--replace"))

    def test_redo_uses_the_settings_as_they_are_now(self):
        self.write_compile_settings(intro=str(self.tmp / "nope.mp4"))     # a missing intro must stop it like a normal run
        self.assertIn("your intro video is missing", self.mc_run("--redo", "last"))


if __name__ == "__main__":
    unittest.main()
