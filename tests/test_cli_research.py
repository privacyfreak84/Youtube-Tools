"""`ytt research ...` through main(), with a fake YouTube handed in where the real one would be."""
import contextlib
import csv
import io
import json
import shutil
import tempfile
import unittest
from argparse import Namespace
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from ytt.sources import channel as chan
from ytt.ui import cli, research_cli
from ytt.workspace.workspace import Workspace

try:
    from fakes import FakeBackend
    from test_research_outliers import channel, vids
except ImportError:
    from tests.fakes import FakeBackend
    from tests.test_research_outliers import channel, vids


class ResearchCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytt_cli_research_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "no-workspace-here"
        self.backend = FakeBackend()
        patcher = mock.patch.object(research_cli, "make_backend", lambda args, root: self.backend)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if stdin is not None:
                with mock.patch("sys.stdin", io.StringIO(stdin)):
                    code = cli.main(["--workspace", str(self.root), "research", *argv])
            else:
                code = cli.main(["--workspace", str(self.root), "research", *argv])
        return code, out.getvalue(), err.getvalue()


class OutliersCliTest(ResearchCliTest):
    def setUp(self):
        super().setUp()
        channel(self.backend, "@Big", "Big Channel", 1_500_000, True, videos=vids("a", [1000, 1200, 900, 1100, 4400]))
        channel(self.backend, "@Small", "Small", None, None, videos=vids("b", [10, 10, 10, 95]))

    def test_the_table_shows_one_row_per_outlier_with_readable_numbers(self):
        code, out, err = self.run_cli("outliers", "@Big", "@Small")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), ["Channel", "Verified", "Title", "Views", "Channel", "median", "x", "Baseline", "Days", "ago", "Type"])
        self.assertTrue(set(lines[1].replace(" ", "")) == {"-"})
        self.assertEqual(len(lines), 4)                                           # headers, rule, two outliers
        self.assertRegex(lines[2], r"^Small\s+N/A\s+b 3\s+95\s+10\s+9\.5x\s+4\s+videos$")
        self.assertRegex(lines[3], r"^Big Channel\s+Yes\s+a 4\s+4\.4K\s+1\.1K\s+4\.0x\s+5\s+videos$")
        self.assertIn("fetching @Big [videos]", err)

    def test_json_has_the_rows_the_ids_and_the_notes_and_nothing_else_on_stdout(self):
        channel(self.backend, "@Tiny", "Tiny", None, None, videos=vids("t", [1, 2]))
        code, out, err = self.run_cli("outliers", "@Big", "@Tiny", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(list(data), ["results", "ids", "notes"])
        self.assertEqual(data["ids"], [data["results"][0]["id"]])
        self.assertEqual(data["results"][0]["channel"], "Big Channel")
        self.assertEqual(data["results"][0]["url"], f"https://youtu.be/{data['ids'][0]}")
        self.assertEqual(data["notes"], ["@Tiny [videos]: only 2 video(s), too few for a reliable baseline; skipped"])

    def test_ids_prints_only_ids_one_per_line_that_make_and_fetch_can_read_back(self):
        code, out, err = self.run_cli("outliers", "@Big", "@Small", "--ids", "--multiplier", "1.05")
        self.assertEqual(code, 0)
        ids = out.split()
        self.assertGreater(len(ids), 2)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(chan.parse_video_ids(out), ids)
        self.assertNotIn("Channel", out)

    def test_a_video_found_in_two_tabs_is_one_id_but_two_rows(self):
        same = vids("x", [10, 10, 10, 90])[3]
        channel(self.backend, "@Both", "Both", None, None, videos=vids("x", [10, 10, 10, 90]),
                streams=[*vids("y", [10, 10, 10], "streams"), same])
        code, out, _ = self.run_cli("outliers", "@Both", "--type", "all", "--ids")
        self.assertEqual(out.split(), [same.id])
        code, out, _ = self.run_cli("outliers", "@Both", "--type", "all", "--json")
        data = json.loads(out)
        self.assertEqual(len(data["results"]), 2)
        self.assertEqual(data["ids"], [same.id])

    def test_a_long_channel_name_is_shortened_in_the_table_only(self):
        name = "A Very Long Channel Name Indeed"
        channel(self.backend, "@Long", name, None, None, videos=vids("l", [10, 10, 10, 90]))
        code, out, _ = self.run_cli("outliers", "@Long")
        self.assertIn(name[:25], out)
        self.assertNotIn(name[:26], out)
        code, out, _ = self.run_cli("outliers", "@Long", "--json")
        self.assertEqual(json.loads(out)["results"][0]["channel"], name)

    def test_csv_is_saved_and_the_table_still_shown(self):
        path = self.tmp / "out.csv"
        code, out, err = self.run_cli("outliers", "@Big", "--csv", str(path))
        self.assertEqual(code, 0)
        self.assertIn("Big Channel", out)
        self.assertIn(f"Saved 1 rows to {path}", err)
        with path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(list(rows[0]), ["channel", "verified", "subs", "title", "views", "baseline_median", "ratio",
                                         "days_ago", "type", "url", "id"])
        self.assertEqual((rows[0]["channel"], rows[0]["views"], rows[0]["subs"]), ("Big Channel", "4400", "1500000"))

    def test_json_and_ids_cannot_be_asked_for_together(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stopped:
            cli.main(["research", "outliers", "@Big", "--json", "--ids"])
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("not allowed with argument", err.getvalue())

    def test_nothing_found_says_so_on_stderr_exits_1_and_still_gives_valid_json_when_asked(self):
        code, out, err = self.run_cli("outliers", "@Big", "--multiplier", "50")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("No outliers found at 50x baseline", err)
        code, out, _ = self.run_cli("outliers", "@Big", "--multiplier", "50", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["results"], [])

    def test_channels_come_from_a_file_with_comments_or_from_stdin(self):
        path = self.tmp / "channels.txt"
        path.write_text("# my list\n@Big\n\n  @Small  \n", encoding="utf-8")
        code, out, _ = self.run_cli("outliers", "--channels-file", str(path), "--ids")
        self.assertEqual((code, len(out.split())), (0, 2))
        code, out, _ = self.run_cli("outliers", "--channels-file", "-", "--ids", stdin="@Big\n")
        self.assertEqual((code, len(out.split())), (0, 1))
        code, out, _ = self.run_cli("outliers", "@Small", "--channels-file", "-", "--ids", stdin="@Big\n")
        self.assertEqual(len(out.split()), 2)                                        # both the argument and the list

    def test_grouping_prints_a_table_per_channel_with_subscribers_or_per_type(self):
        code, out, _ = self.run_cli("outliers", "@Big", "@Small", "--group-by", "channel", "--sort", "channel")
        self.assertEqual(code, 0)
        self.assertIn("== Big Channel (1.5M subs) ==", out)
        self.assertIn("== Small (subs N/A) ==", out)
        self.assertEqual(out.count("Verified"), 2)
        self.assertIn("\n\n== Small", out)                                           # a blank line between groups
        channel(self.backend, "@Big", "Big Channel", 1_500_000, True, videos=vids("a", [1000, 1200, 900, 1100, 4400]),
                shorts=vids("s", [5, 5, 5, 50], "shorts"))
        code, out, _ = self.run_cli("outliers", "@Big", "--type", "all", "--group-by", "type")
        self.assertIn("== videos ==", out)
        self.assertIn("== shorts ==", out)

    def test_resolving_dates_fills_the_days_ago_column_and_reports_progress(self):
        channel(self.backend, "@S", "S", None, None, shorts=vids("s", [10, 10, 10, 50], "shorts", days=[None] * 4))
        self.backend.dates = {v.id: date(2026, 1, 1) for v in self.backend.channels["https://www.youtube.com/@S"]["tabs"]["shorts"]}
        with mock.patch("ytt.ops.research.outliers.utc_now", lambda: datetime(2026, 1, 11, 12, tzinfo=timezone.utc)):
            code, out, err = self.run_cli("outliers", "@S", "--type", "shorts", "--resolve-dates")
        self.assertEqual(code, 0)
        self.assertRegex(out.splitlines()[2], r"\s10\s+shorts$")
        self.assertIn("[1/1] resolving date", err)

    def test_a_missing_channel_is_a_clear_error(self):
        code, out, err = self.run_cli("outliers")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Error: give at least one channel", err)

    def test_it_never_touches_or_creates_a_workspace(self):
        self.run_cli("outliers", "@Big", "--ids")
        self.assertFalse(self.root.exists())

    def test_ctrl_c_says_cancelled_and_exits_130(self):
        with mock.patch.object(research_cli.outliers_mod, "find_outliers", side_effect=KeyboardInterrupt):
            code, out, err = self.run_cli("outliers", "@Big")
        self.assertEqual(code, 130)
        self.assertIn("Cancelled", err)

    def test_a_youtube_problem_is_an_error_line_not_a_crash(self):
        from ytt.sources.errors import SourceError
        with mock.patch.object(research_cli.outliers_mod, "find_outliers", side_effect=SourceError("yt-dlp is not installed")):
            code, out, err = self.run_cli("outliers", "@Big")
        self.assertEqual(code, 1)
        self.assertIn("Error: yt-dlp is not installed", err)

    def test_with_no_tool_named_the_tools_are_listed(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("ytt research outliers", out)


class BackendChoiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytt_research_backend_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def args(self, browser=None, file=None):
        return Namespace(cookies_from_browser=browser, cookies=file)

    def test_cookie_options_on_the_command_line_are_used(self):
        b = research_cli.make_backend(self.args("firefox", "/x/cookies.txt"), self.tmp / "none")
        self.assertEqual(b.cookies, {"cookiesfrombrowser": ("firefox",), "cookiefile": "/x/cookies.txt"})

    def test_no_options_and_no_workspace_means_no_cookies(self):
        self.assertEqual(research_cli.make_backend(self.args(), self.tmp / "none").cookies, {})

    def test_the_workspace_settings_are_used_when_there_are_no_options_and_the_options_win_over_them(self):
        ws = Workspace.init(self.tmp / "ws")
        ws.config["cookies_from_browser"] = "chrome"
        ws.save_config()
        ws.close()
        self.assertEqual(research_cli.make_backend(self.args(), self.tmp / "ws").cookies, {"cookiesfrombrowser": ("chrome",)})
        self.assertEqual(research_cli.make_backend(self.args("firefox"), self.tmp / "ws").cookies, {"cookiesfrombrowser": ("firefox",)})

    def test_a_broken_settings_file_does_not_stop_research(self):
        (self.tmp / "ws").mkdir()
        (self.tmp / "ws" / "workspace.toml").write_text("not [toml")
        self.assertEqual(research_cli.make_backend(self.args(), self.tmp / "ws").cookies, {})


if __name__ == "__main__":
    unittest.main()
