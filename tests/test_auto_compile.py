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
        for name in MODULES:
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


if __name__ == "__main__":
    unittest.main()
