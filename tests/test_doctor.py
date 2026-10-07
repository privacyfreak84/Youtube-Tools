"""ytt doctor: each check against a pretend machine (a fake Probes), plus a few against the real one."""
import hashlib
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from ytt.ops import doctor
from ytt.ops.doctor import FAIL, OK, WARN
from ytt.workspace import db, store
from ytt.workspace.workspace import Workspace

ENCODERS = """Encoders:
 V..... = Video
 ------
 V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC (codec h264)
 A....D aac                  AAC (Advanced Audio Coding)
"""
NO_X264 = ENCODERS.replace(" V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC (codec h264)\n", "")


class FakeProbes:
    """A machine where everything is fine, with knobs to break one thing at a time."""

    def __init__(self):
        self.python = (3, 12, 3)
        self.programs = {"ffmpeg": "/usr/bin/ffmpeg", "ffprobe": "/usr/bin/ffprobe"}
        self.encoders = ENCODERS
        self.ytdlp = "2026.09.20"
        self.now = date(2026, 10, 7)
        self.free = 100 * 10 ** 9
        self.unreachable = {}                 # url -> reason
        self.asked = []

    def python_version(self):
        return self.python

    def which(self, program):
        return self.programs.get(program)

    def run(self, command, timeout=20):
        if command[1:] == ["-version"]:
            return 0, f"{command[0]} version 6.1.1 Copyright (c) the FFmpeg developers"
        if command[1:] == ["-hide_banner", "-encoders"]:
            return 0, self.encoders
        return 1, "unexpected"

    def ytdlp_version(self):
        return self.ytdlp

    def today(self):
        return self.now

    def disk_free(self, path):
        return self.free

    def reach(self, url, timeout=10):
        self.asked.append(url)
        if url in self.unreachable:
            return False, self.unreachable[url]
        return True, ""


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytt_doctor_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "ws"
        self.probes = FakeProbes()

    def make_workspace(self):
        return Workspace.init(self.root)

    def run_doctor(self, **kw):
        return doctor.run_doctor(self.root, self.probes, **kw)

    def get(self, checks, name):
        found = [c for c in checks if c.name == name]
        self.assertEqual(len(found), 1, f"{name}: expected one check, got {[c.name for c in checks]}")
        return found[0]


class ProgramsTest(DoctorTest):
    def test_a_healthy_machine_and_workspace_has_no_problems_and_no_warnings(self):
        self.make_workspace().close()
        checks = self.run_doctor()
        self.assertEqual(doctor.summary(checks), (0, 0), [(c.name, c.status, c.detail) for c in checks if c.status != OK])
        self.assertEqual(self.get(checks, "ffmpeg").detail, "6.1.1")
        self.assertEqual(self.probes.asked, [doctor.INTERNET_URL, doctor.YOUTUBE_URL])

    def test_old_python_is_a_problem(self):
        self.probes.python = (3, 10, 12)
        c = self.get(self.run_doctor(), "Python")
        self.assertEqual(c.status, FAIL)
        self.assertIn("3.11", c.fix)

    def test_missing_ffmpeg_and_ffprobe_are_each_a_problem_with_a_fix(self):
        self.probes.programs = {}
        checks = self.run_doctor()
        for name in ("ffmpeg", "ffprobe"):
            c = self.get(checks, name)
            self.assertEqual(c.status, FAIL)
            self.assertIn("install ffmpeg", c.fix)
        self.assertNotIn("ffmpeg encoders", [c.name for c in checks])         # nothing to ask without ffmpeg

    def test_ffmpeg_without_the_h264_encoder_is_a_problem_that_names_it(self):
        self.probes.encoders = NO_X264
        c = self.get(self.run_doctor(), "ffmpeg encoders")
        self.assertEqual(c.status, FAIL)
        self.assertIn("libx264", c.detail)
        self.assertNotIn("aac", c.detail)
        self.assertIn("RPM Fusion", c.fix)

    def test_ytdlp_missing_old_recent_and_odd_versions(self):
        self.probes.ytdlp = None
        self.assertEqual(self.get(self.run_doctor(), "yt-dlp").status, FAIL)
        self.probes.ytdlp = "2026.06.01"                                       # 128 days before 2026-10-07
        c = self.get(self.run_doctor(), "yt-dlp")
        self.assertEqual(c.status, WARN)
        self.assertIn("128 days old", c.detail)
        self.assertIn("pip install -U yt-dlp", c.fix)
        self.probes.ytdlp = (self.probes.now - timedelta(days=60)).strftime("%Y.%m.%d")
        self.assertEqual(self.get(self.run_doctor(), "yt-dlp").status, OK)     # exactly the limit is still fine
        self.probes.ytdlp = "nightly-build"
        self.assertEqual(self.get(self.run_doctor(), "yt-dlp").status, OK)     # a version it cannot read is not an error

    def test_a_yt_dlp_dated_in_the_future_is_not_negative_days(self):
        self.probes.ytdlp = "2026.12.01"
        self.assertIn("0 days old", self.get(self.run_doctor(), "yt-dlp").detail)


class NetworkTest(DoctorTest):
    def test_no_internet_is_a_problem_and_youtube_is_not_blamed(self):
        self.probes.unreachable = {doctor.INTERNET_URL: "timed out"}
        checks = self.run_doctor()
        self.assertEqual(self.get(checks, "Internet").status, FAIL)
        self.assertEqual(self.get(checks, "YouTube").status, WARN)
        self.assertEqual(self.probes.asked, [doctor.INTERNET_URL])             # did not bother to ask YouTube

    def test_internet_but_no_youtube_is_a_problem_with_the_reason(self):
        self.probes.unreachable = {doctor.YOUTUBE_URL: "name not resolved"}
        checks = self.run_doctor()
        self.assertEqual(self.get(checks, "Internet").status, OK)
        c = self.get(checks, "YouTube")
        self.assertEqual(c.status, FAIL)
        self.assertIn("name not resolved", c.detail)

    def test_offline_skips_the_network_checks_altogether(self):
        checks = self.run_doctor(offline=True)
        self.assertEqual(self.probes.asked, [])
        self.assertNotIn("Internet", [c.name for c in checks])


class WorkspaceTest(DoctorTest):
    def test_no_workspace_is_a_warning_that_says_research_still_works_and_creates_nothing(self):
        checks = self.run_doctor()
        c = self.get(checks, "Workspace")
        self.assertEqual(c.status, WARN)
        self.assertIn("research works without one", c.fix)
        self.assertIn("ytt workspace init", c.fix)
        self.assertNotIn("Database", [x.name for x in checks])
        self.assertFalse(self.root.exists())

    def test_doctor_changes_nothing_in_a_real_workspace(self):
        ws = self.make_workspace()
        store.upsert_video(ws.conn, "vid00000001", title="T")
        ws.conn.commit()
        ws.close()

        def snapshot():
            return {str(p.relative_to(self.root)): hashlib.md5(p.read_bytes()).hexdigest()
                    for p in sorted(self.root.rglob("*")) if p.is_file()} | \
                   {str(p.relative_to(self.root)) + "/": "" for p in self.root.rglob("*") if p.is_dir()}
        before = snapshot()
        self.run_doctor()
        self.assertEqual(snapshot(), before)

    def test_a_broken_settings_file_is_a_problem_and_stops_the_deeper_checks(self):
        self.make_workspace().close()
        (self.root / "workspace.toml").write_text("this is [not toml")
        checks = self.run_doctor()
        self.assertEqual(self.get(checks, "Settings").status, FAIL)
        self.assertNotIn("Database", [c.name for c in checks])

    def test_low_disk_space_is_a_warning(self):
        self.make_workspace().close()
        self.probes.free = 2 * 10 ** 9
        c = self.get(self.run_doctor(), "Compilations folder")
        self.assertEqual(c.status, WARN)
        self.assertIn("2.0 GB free", c.detail)

    def test_a_missing_folder_cookies_file_and_watch_folder_are_reported(self):
        ws = self.make_workspace()
        ws.config["clips_dir"] = str(self.tmp / "nowhere")
        ws.config["cookies_file"] = str(self.tmp / "cookies.txt")
        ws.config["watch_folders"] = [str(self.tmp / "gone")]
        ws.save_config()
        ws.close()
        checks = self.run_doctor()
        self.assertEqual(self.get(checks, "Clips folder").status, WARN)
        self.assertEqual(self.get(checks, "Cookies file").status, FAIL)
        self.assertEqual(self.get(checks, "Watch folder").status, WARN)


class DatabaseTest(DoctorTest):
    def test_a_database_from_a_newer_ytt_is_a_problem(self):
        self.make_workspace().close()
        conn = sqlite3.connect(self.root / "ytt.db")
        conn.execute(f"PRAGMA user_version = {db.LATEST + 5}")
        conn.commit()
        conn.close()
        c = self.get(self.run_doctor(), "Database")
        self.assertEqual(c.status, FAIL)
        self.assertIn("newer ytt", c.detail)

    def test_an_older_database_is_a_warning_and_is_left_as_it_is(self):
        self.root.mkdir()
        conn = sqlite3.connect(self.root / "ytt.db")
        conn.executescript(db.MIGRATIONS[0])
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()
        checks = self.run_doctor()
        c = self.get(checks, "Database")
        self.assertEqual(c.status, WARN)
        self.assertIn("version 1", c.detail)
        conn = sqlite3.connect(self.root / "ytt.db")
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)    # not upgraded by looking at it
        conn.close()

    def test_the_database_is_opened_so_that_nothing_can_be_written_or_created(self):
        self.make_workspace().close()
        conn = doctor._open_read_only(self.root)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE sneaky (a)")
        conn.close()
        with self.assertRaises(sqlite3.OperationalError):
            doctor._open_read_only(self.tmp / "no-such-workspace")
        self.assertFalse((self.tmp / "no-such-workspace").exists())

    def test_a_file_that_is_not_a_database_is_a_problem(self):
        self.root.mkdir()
        (self.root / "ytt.db").write_bytes(b"this is not a database at all" * 50)
        checks = self.run_doctor()
        c = self.get(checks, "Database")
        self.assertEqual(c.status, FAIL)
        self.assertIn("backup", c.fix)


class StylesAndFilesTest(DoctorTest):
    def test_a_style_with_a_bad_value_or_a_missing_file_is_a_problem(self):
        ws = self.make_workspace()
        store.save_style(ws.conn, "default", {})
        store.save_style(ws.conn, "broken", {"quality": "ultra"})
        store.save_style(ws.conn, "intro_gone", {"intro": str(self.tmp / "intro.mp4")})
        ws.conn.commit()
        ws.close()
        c = self.get(self.run_doctor(), "Styles")
        self.assertEqual(c.status, FAIL)
        self.assertIn("broken: quality must be one of", c.detail)
        self.assertIn("intro_gone: intro file", c.detail)

    def test_good_styles_are_counted_and_a_missing_default_is_noted(self):
        ws = self.make_workspace()
        store.save_style(ws.conn, "plain", {})
        ws.conn.commit()
        ws.close()
        checks = self.run_doctor()
        self.assertEqual(self.get(checks, "Styles").detail, "1 saved")
        self.assertEqual(self.get(checks, "Default style").status, WARN)

    def test_clips_and_compilations_whose_files_are_gone_are_counted(self):
        ws = self.make_workspace()
        (self.root / "clips" / "a.mp4").write_bytes(b"x")
        for vid, name in (("vid00000001", "a.mp4"), ("vid00000002", "b.mp4")):
            store.upsert_video(ws.conn, vid, title=vid)
            store.upsert_clip(ws.conn, vid, f"clips/{name}", "ready")
        store.add_compilation(ws.conn, "compilation_001", clips=[], output_path="compilations/compilation_001.mp4")
        ws.conn.commit()
        ws.close()
        checks = self.run_doctor()
        c = self.get(checks, "Clip files")
        self.assertEqual((c.status, c.detail), (WARN, "1 of 2 ready clips have no file"))
        c = self.get(checks, "Compilation files")
        self.assertEqual(c.status, WARN)
        self.assertIn("compilation_001", c.detail)

    def test_clips_not_marked_ready_are_not_reported_as_missing(self):
        ws = self.make_workspace()
        store.upsert_video(ws.conn, "vid00000001", title="T")
        store.upsert_clip(ws.conn, "vid00000001", "clips/none.mp4", "failed")
        ws.conn.commit()
        ws.close()
        self.assertEqual(self.get(self.run_doctor(), "Clip files").status, OK)


class RealMachineTest(unittest.TestCase):
    def test_the_real_probes_answer_with_the_right_kinds_of_things(self):
        p = doctor.Probes()
        self.assertGreaterEqual(p.python_version(), (3, 11))
        self.assertIsNone(p.which("a-program-that-does-not-exist-ytt"))
        code, text = p.run(["a-program-that-does-not-exist-ytt"])
        self.assertIsNone(code)
        self.assertGreater(p.disk_free(tempfile.gettempdir()), 0)
        self.assertIsInstance(p.today(), date)

    def test_the_encoder_list_of_the_real_ffmpeg_is_understood(self):
        p = doctor.Probes()
        if not p.which("ffmpeg"):
            self.skipTest("no ffmpeg here")
        code, text = p.run(["ffmpeg", "-hide_banner", "-encoders"])
        self.assertEqual(code, 0)
        found = doctor._encoders(text)
        self.assertIn("aac", found)
        self.assertGreater(len(found), 20)

    def test_the_real_doctor_runs_offline_on_a_real_workspace_without_error(self):
        tmp = Path(tempfile.mkdtemp(prefix="ytt_doctor_real_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        Workspace.init(tmp / "ws").close()
        checks = doctor.run_doctor(tmp / "ws", offline=True)
        self.assertIn("Python", [c.name for c in checks])
        self.assertEqual(next(c for c in checks if c.name == "Database").status, OK)

    def test_the_version_of_yt_dlp_is_read(self):
        from ytt.sources.ytdlp import yt_dlp_version
        v = yt_dlp_version()
        if v is None:
            self.skipTest("yt-dlp is not installed here")
        self.assertRegex(v, r"^\d{4}\.\d{2}\.\d{2}")


if __name__ == "__main__":
    unittest.main()
