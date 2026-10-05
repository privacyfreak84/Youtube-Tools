"""`ytt fetch` (and remake's fetching again) through main(), the way the real command runs, with the fake YouTube
handed in where the real one would be reached."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ytt.ui import cli
from ytt.workspace import store
from ytt.workspace.workspace import Workspace

try:
    from fakes import FakeBackend, make_videos
    from test_auto_compile import HAVE_TOOLS
except ImportError:
    from tests.fakes import FakeBackend, make_videos
    from tests.test_auto_compile import HAVE_TOOLS


def vid(n):
    return f"vid{n:08d}"


@unittest.skipUnless(HAVE_TOOLS, "needs ffmpeg")
class FetchCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytt_clif_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "ws"
        self.backend = FakeBackend()
        for patch in (mock.patch.object(cli, "make_backend", lambda ws: self.backend),
                      mock.patch.object(cli, "is_tty", lambda: False)):
            patch.start()
            self.addCleanup(patch.stop)
        self.run_cli("workspace", "init")

    def run_cli(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if stdin is not None:
                with mock.patch("sys.stdin", io.StringIO(stdin)):
                    code = cli.main(["--workspace", str(self.root), *argv])
            else:
                code = cli.main(["--workspace", str(self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def fetch(self, *argv, **kw):
        return self.run_cli("fetch", "@Chan", "--type", "shorts", "--sort", "oldest", *argv, **kw)

    def library_ids(self):
        with Workspace.open(self.root) as ws:
            return [c["youtube_id"] for c in store.list_clips(ws.conn)]


class FetchCommand(FetchCliTest):
    def test_a_dry_run_shows_the_plan_lists_every_clip_and_does_nothing(self):
        code, out, _ = self.fetch("--clips", "15", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Fetch from @Chan: 15 clips to download", out)
        self.assertIn("Clip 39", out)
        self.assertIn("Clip 25", out)                                  # all 15 are listed, not just the first few
        self.assertIn("(dry run: nothing was done)", out)
        self.assertEqual((self.backend.started, self.library_ids()), ([], []))

    def test_a_real_fetch_downloads_records_and_reports(self):
        code, out, _ = self.fetch("--clips", "3", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("[3/3]", out)
        self.assertIn("Completed: 3 clips downloaded", out)
        self.assertEqual(self.library_ids(), [vid(39), vid(38), vid(37)])
        _, out, _ = self.run_cli("library", "clips")
        self.assertIn("fetched", out)
        _, out, _ = self.run_cli("library", "runs")
        self.assertIn("fetch", out)
        self.assertIn("completed", out)

    def test_a_long_plan_is_shortened_when_you_are_asked_to_confirm_but_a_dry_run_shows_everything(self):
        _, dry, _ = self.fetch("--clips", "30", "--dry-run")
        for n in (39, 30, 10):
            self.assertIn(f"Clip {n} ", dry)
        self.assertNotIn("more (--dry-run", dry)
        _, real, _ = self.fetch("--clips", "30", "--yes")
        shown = real.split("[1/30]")[0]
        self.assertIn("Clip 39 ", shown)
        self.assertNotIn("Clip 10 ", shown)
        self.assertIn("and 20 more (--dry-run lists them all)", shown)
        self.assertEqual(len(self.library_ids()), 30)

    def test_without_a_terminal_it_refuses_to_guess_and_downloads_nothing(self):
        code, out, err = self.fetch("--clips", "3")
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)
        self.assertEqual(self.backend.started, [])

    def test_at_a_terminal_it_asks_and_no_means_no(self):
        with mock.patch.object(cli, "is_tty", lambda: True), mock.patch("builtins.input", lambda prompt="": "n"):
            code, out, _ = self.fetch("--clips", "3")
        self.assertEqual(code, 0)
        self.assertIn("Cancelled; nothing was done.", out)
        self.assertEqual(self.library_ids(), [])

    def test_at_a_terminal_enter_means_yes(self):
        with mock.patch.object(cli, "is_tty", lambda: True), mock.patch("builtins.input", lambda prompt="": ""):
            code, _, _ = self.fetch("--clips", "2")
        self.assertEqual((code, len(self.library_ids())), (0, 2))

    def test_it_says_what_is_missing_instead_of_guessing_a_count(self):
        code, out, err = self.run_cli("fetch", "@Chan")
        self.assertEqual(code, 1)
        self.assertIn("How many?", err)

    def test_a_channel_is_required(self):
        code, _, err = self.run_cli("fetch", "--clips", "3")
        self.assertEqual(code, 1)
        self.assertIn("which channel", err)

    def test_two_ways_of_saying_how_many_are_refused_by_the_parser(self):
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            self.fetch("--clips", "3", "--range", "1-5")
        self.assertEqual(cm.exception.code, 2)

    def test_an_unreadable_channel_shows_the_error_in_the_plan_and_exits_1(self):
        self.backend.list_error = "HTTP Error 404"
        code, out, _ = self.fetch("--clips", "3", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("Errors", out)
        self.assertIn("404", out)

    def test_nothing_new_is_not_an_error_and_makes_no_run(self):
        self.fetch("--range", "1-3", "--yes")
        code, out, _ = self.fetch("--range", "1-3", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("Nothing to do.", out)
        _, runs, _ = self.run_cli("library", "runs", "--json")
        self.assertEqual(len(json.loads(runs)), 1)

    def test_a_failed_clip_is_named_and_replaced_and_the_exit_code_is_0(self):
        self.backend.fail = {vid(38)}
        code, out, _ = self.fetch("--clips", "3", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("skipped", out)
        self.assertIn("403", out)
        self.assertIn("1 failed", out)
        self.assertEqual(self.library_ids(), [vid(39), vid(37), vid(36)])

    def test_fewer_clips_than_asked_is_exit_1_and_says_to_run_again(self):
        self.backend.fail = {vid(38)}
        code, out, _ = self.fetch("--range", "1-3", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("Partial", out)
        self.assertIn("run the same command again", out)

    def test_ctrl_c_exits_130_keeps_finished_clips_and_the_next_run_continues(self):
        self.backend.interrupt = {vid(37)}
        code, out, _ = self.fetch("--clips", "5", "--yes", "--workers", "1")
        self.assertEqual(code, 130)
        self.assertIn("Cancelled", out)
        self.assertEqual(self.library_ids(), [vid(39), vid(38)])
        self.backend.interrupt = set()
        code, out, _ = self.fetch("--clips", "3", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(self.library_ids(), [vid(39), vid(38), vid(37), vid(36), vid(35)])

    def test_options_reach_the_request(self):
        code, out, _ = self.fetch("--clips", "50", "--min-views", "1030", "--from", "2026-08-22", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Selection: 50 new clips", out)
        self.assertIn("Limits: 30 left out", out)

    def test_a_bad_date_is_a_clear_error(self):
        code, _, err = self.fetch("--clips", "3", "--from", "soon")
        self.assertEqual(code, 1)
        self.assertIn("can't read the date", err)

    def test_a_list_of_videos_from_a_file(self):
        f = self.tmp / "list.txt"
        f.write_text(f"# mine\n{vid(5)}\nhttps://youtu.be/{vid(6)}\n")
        code, out, _ = self.run_cli("fetch", "--videos", str(f), "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(self.library_ids(), [vid(5), vid(6)])
        self.assertEqual(self.backend.listed, [])

    def test_a_list_of_videos_from_stdin(self):
        code, _, _ = self.run_cli("fetch", "--videos", "-", "--yes", stdin=f"{vid(7)}\n")
        self.assertEqual((code, self.library_ids()), (0, [vid(7)]))

    def test_a_bad_line_in_the_list_names_the_line_and_downloads_nothing(self):
        f = self.tmp / "list.txt"
        f.write_text(f"{vid(5)}\nthis is not a video\n")
        code, _, err = self.run_cli("fetch", "--videos", str(f), "--yes")
        self.assertEqual(code, 1)
        self.assertIn("line 2", err)
        self.assertEqual(self.backend.started, [])

    def test_a_missing_list_file_is_a_clear_error(self):
        code, _, err = self.run_cli("fetch", "--videos", str(self.tmp / "nope.txt"), "--yes")
        self.assertEqual(code, 1)
        self.assertIn("can't read the list", err)

    def test_a_list_cannot_be_combined_with_a_channel(self):
        f = self.tmp / "list.txt"
        f.write_text(vid(5) + "\n")
        code, _, err = self.run_cli("fetch", "@Chan", "--videos", str(f), "--yes")
        self.assertEqual(code, 1)
        self.assertIn("leave the channel out", err)

    def test_before_a_workspace_exists_it_says_how_to_start(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--workspace", str(self.tmp / "none"), "fetch", "@Chan", "--clips", "1", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("ytt workspace init", err.getvalue())


@unittest.skipUnless(HAVE_TOOLS, "needs ffmpeg")
class RemakeThroughTheCommandLine(FetchCliTest):
    def test_remake_fetches_missing_clips_again_through_the_same_backend(self):
        self.fetch("--clips", "4", "--yes")
        with Workspace.open(self.root) as ws:
            ids = [c["id"] for c in store.list_clips(ws.conn)]
            store.add_compilation(ws.conn, "compilation_001", clips=[(i, None) for i in ids],
                                  output_path="compilations/compilation_001.mp4", made="2026-01-01 00:00")
            ws.conn.commit()
            for c in store.list_clips(ws.conn):
                ws.from_stored(c["path"]).unlink()
        self.backend.started.clear()
        code, out, _ = self.run_cli("remake", "last", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("fetch 4 clips again", out)
        self.assertEqual(self.backend.started, [])
        code, out, _ = self.run_cli("remake", "last", "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.backend.started), 4)
        self.assertIn("fetched again", out)
        self.assertIn("made compilation_002", out)


if __name__ == "__main__":
    unittest.main()
