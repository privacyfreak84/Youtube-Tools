"""`ytt research ...` through main(), with a fake YouTube handed in where the real one would be."""
import contextlib
import csv
import io
import json
import shutil
import tempfile
import unittest
from argparse import Namespace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from ytt.sources import channel as chan
from ytt.sources.models import VideoInfo
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


class TableCliTest(ResearchCliTest):
    def setUp(self):
        super().setUp()
        now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        patcher = mock.patch.object(research_cli, "utc_now", lambda: now)
        patcher.start()
        self.addCleanup(patcher.stop)

        def v(vid, title, views, days, duration, tab="videos", approx=False):
            ts = None if days is None else (now - timedelta(days=days)).timestamp()
            return VideoInfo(id=vid.ljust(11, "_"), title=title, views=views, duration=duration, timestamp=ts, approx=approx, tab=tab)
        self.v = v
        channel(self.backend, "@Chan", "Chan", 10, False,
                videos=[v("a1", "First video", 1234, 2, 3725), v("a2", "Second", 2_500_000, 40, 59), v("a3", "No date", None, None, None)],
                shorts=[v("s1", "A short", 77, 3, 15, "shorts")])

    def test_the_table_has_readable_views_dates_days_and_lengths(self):
        code, out, err = self.run_cli("table", "@Chan")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), ["Title", "Views", "Uploaded", "Days", "ago", "Duration"])
        self.assertRegex(lines[2], r"^First video\s+1\.2K\s+2026-10-05\s+2\s+1:02:05$")
        self.assertRegex(lines[3], r"^Second\s+2\.5M\s+2026-08-28\s+40\s+0:59$")
        self.assertRegex(lines[4], r"^No date\s+N/A\s+N/A\s+N/A\s+N/A$")
        self.assertNotIn("Type", out)
        self.assertNotIn("approximate", err)

    def test_all_adds_a_type_column(self):
        code, out, _ = self.run_cli("table", "@Chan", "--type", "all")
        self.assertIn("Type", out.splitlines()[0])
        self.assertRegex(out, r"A short\s+77\s+2026-10-04\s+3\s+0:15\s+shorts")

    def test_a_rough_date_gets_a_tilde_and_an_explanation_on_stderr(self):
        channel(self.backend, "@Rough", "Rough", None, None, videos=[self.v("r1", "Rough one", 5, 9, 20, approx=True)])
        code, out, err = self.run_cli("table", "@Rough")
        self.assertRegex(out, r"2026-09-28~")
        self.assertIn("~ = approximate date", err)
        self.assertIn("--full", err)

    def test_json_gives_iso_dates_the_approx_flag_and_ids_and_csv_has_every_column(self):
        path = self.tmp / "t.csv"
        code, out, _ = self.run_cli("table", "@Chan", "--json", "--csv", str(path))
        data = json.loads(out)
        first = data["results"][0]
        self.assertEqual((first["upload_date"], first["days_ago"], first["approx"], first["duration"], first["views"]),
                         ("2026-10-05", 2, False, 3725, 1234))
        self.assertEqual(data["results"][2]["upload_date"], None)
        self.assertEqual(len(data["ids"]), 3)
        with path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(list(rows[0]), ["title", "type", "views", "upload_date", "days_ago", "approx", "duration", "url", "id"])
        self.assertEqual(rows[1]["views"], "2500000")

    def test_ids_are_the_videos_in_table_order(self):
        code, out, _ = self.run_cli("table", "@Chan", "--ids", "--sort", "popular")
        self.assertEqual(out.split(), [vid.ljust(11, "_") for vid in ("a2", "a1", "a3")])         # most viewed first, no count last

    def test_no_videos_is_a_message_and_exit_1_and_empty_json_when_asked(self):
        code, out, err = self.run_cli("table", "@Nobody")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("No videos found", err)
        code, out, _ = self.run_cli("table", "@Nobody", "--json")
        self.assertEqual(json.loads(out)["results"], [])

    def test_a_slow_sort_with_a_limit_warns_on_stderr(self):
        code, out, err = self.run_cli("table", "@Chan", "--sort", "popular", "--limit", "1")
        self.assertIn("(--sort popular reads the whole videos list", err)
        self.assertEqual(len(out.splitlines()), 3)

    def test_full_asks_each_video_and_shows_progress(self):
        self.backend.infos = {"a1".ljust(11, "_"): VideoInfo(id="a1".ljust(11, "_"), duration=100, timestamp=datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())}
        code, out, err = self.run_cli("table", "@Chan", "--limit", "1", "--full")
        self.assertRegex(out.splitlines()[2], r"2026-10-01\s+6\s+1:40$")
        self.assertIn("[1/1] First video", err)


class ChannelsCliTest(ResearchCliTest):
    def setUp(self):
        super().setUp()
        from ytt.sources.models import ChannelRef
        self.ref = lambda key, name: ChannelRef(key, name, f"https://www.youtube.com/channel/{key}")
        self.backend.searches = {"cats": [self.ref("UC1", "Cat One"), self.ref("UC2", "Cat Two")], "dogs": [self.ref("UC2", "Cat Two")]}
        self.backend.featured = {"https://www.youtube.com/@Seed": [self.ref("UC3", "Seeded")]}
        self.backend.channels["https://www.youtube.com/channel/UC1"] = {"name": "Cat One", "subs": 2_300_000, "verified": True, "tabs": {}}

    def test_the_table_lists_name_link_and_how_each_was_found(self):
        code, out, err = self.run_cli("channels", "--search", "cats", "--search", "dogs", "--seed", "@Seed")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), ["Name", "URL", "Found", "via"])
        self.assertRegex(lines[2], r'^Cat One\s+https://www.youtube.com/channel/UC1\s+search:"cats"$')
        self.assertRegex(lines[3], r'^Cat Two\s+https://www.youtube.com/channel/UC2\s+search:"cats", search:"dogs"$')
        self.assertRegex(lines[4], r"^Seeded\s+https://www.youtube.com/channel/UC3\s+featured-by:@Seed$")
        self.assertIn("# 3 unique channel(s) found", err)

    def test_with_subs_adds_verified_and_subscribers_columns(self):
        code, out, _ = self.run_cli("channels", "--search", "cats", "--with-subs")
        self.assertEqual(out.splitlines()[0].split(), ["Name", "Verified", "Subscribers", "URL", "Found", "via"])
        self.assertRegex(out.splitlines()[2], r"^Cat One\s+Yes\s+2\.3M\s+https://")
        self.assertRegex(out.splitlines()[3], r"^Cat Two\s+N/A\s+N/A\s+https://")

    def test_urls_only_is_one_link_per_line_that_outliers_reads_back_from_stdin(self):
        code, out, _ = self.run_cli("channels", "--search", "cats", "--urls-only")
        self.assertEqual(out.split(), ["https://www.youtube.com/channel/UC1", "https://www.youtube.com/channel/UC2"])
        from ytt.ops.research.common import parse_channel_list
        self.assertEqual(parse_channel_list(out), out.split())

    def test_json_has_results_and_notes_but_no_ids_and_csv_has_the_old_columns(self):
        path = self.tmp / "c.csv"
        code, out, _ = self.run_cli("channels", "--search", "cats", "--json", "--csv", str(path))
        data = json.loads(out)
        self.assertEqual(list(data), ["results", "notes"])
        self.assertEqual(data["results"][0], {"name": "Cat One", "verified": None, "subs": None,
                                             "url": "https://www.youtube.com/channel/UC1", "source": 'search:"cats"'})
        with path.open(encoding="utf-8") as f:
            self.assertEqual(list(csv.DictReader(f))[1]["name"], "Cat Two")

    def test_json_and_urls_only_cannot_be_asked_for_together(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            cli.main(["research", "channels", "--search", "x", "--json", "--urls-only"])
        self.assertIn("not allowed with argument", err.getvalue())

    def test_nothing_found_and_nothing_asked(self):
        code, out, err = self.run_cli("channels", "--search", "nothing")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("No channels found.", err)
        code, out, err = self.run_cli("channels")
        self.assertEqual(code, 1)
        self.assertIn("Error: give at least one --search keyword or --seed channel", err)

    def test_a_failing_search_is_shown_as_a_note_on_stderr(self):
        self.backend.searches["bad"] = "could not read ytsearch30:bad (429)"
        code, out, err = self.run_cli("channels", "--search", "bad", "--search", "cats")
        self.assertEqual(code, 0)
        self.assertIn("(search failed for 'bad': could not read ytsearch30:bad (429))", err)


class LiveCliTest(ResearchCliTest):
    def setUp(self):
        super().setUp()
        from ytt.sources.models import LiveInfo
        self.streams = "https://www.youtube.com/@Chan/streams"
        vid = lambda x: x.ljust(11, "_")
        self.backend.listings[self.streams] = [VideoInfo(id=vid("l1"), title="Now"), VideoInfo(id=vid("old"), title="Past")]
        self.backend.lives = {vid("l1"): LiveInfo(vid("l1"), "Big stream", "Chan", f"https://www.youtube.com/watch?v={vid('l1')}", 12_345, 99_000)}

    def test_the_table_shows_title_channel_watching_and_link(self):
        code, out, err = self.run_cli("live", "@Chan")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[0].split(), ["Title", "Channel", "Watching", "URL"])
        self.assertRegex(out.splitlines()[2], r"^Big stream\s+Chan\s+12\.3K\s+https://www\.youtube\.com/watch\?v=l1_+$")
        self.assertIn("checking https://www.youtube.com/@Chan/streams", err)

    def test_nobody_live_is_a_message_and_exit_1_with_empty_json_when_asked(self):
        self.backend.lives = {}
        code, out, err = self.run_cli("live", "@Chan")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("No live streams right now.", err)
        code, out, _ = self.run_cli("live", "@Chan", "--json")
        self.assertEqual(json.loads(out), {"results": [], "ids": [], "notes": []})

    def test_json_and_ids_and_csv(self):
        path = self.tmp / "l.csv"
        code, out, _ = self.run_cli("live", "@Chan", "--json", "--csv", str(path))
        data = json.loads(out)
        self.assertEqual(data["results"][0]["concurrent_view_count"], 12_345)
        self.assertEqual(data["ids"], ["l1".ljust(11, "_")])
        with path.open(encoding="utf-8") as f:
            self.assertEqual(list(csv.DictReader(f))[0]["title"], "Big stream")
        code, out, _ = self.run_cli("live", "@Chan", "--ids")
        self.assertEqual(out.split(), ["l1".ljust(11, "_")])

    def test_out_dir_keeps_one_file_per_channel_in_the_old_format_even_when_nobody_is_live(self):
        self.backend.listings["https://www.youtube.com/@Quiet/streams"] = []
        folder = self.tmp / "keep"
        code, out, err = self.run_cli("live", "@Chan", "@Quiet", "--out-dir", str(folder))
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["Chan.json", "Quiet.json"])
        chan = json.loads((folder / "Chan.json").read_text(encoding="utf-8"))
        self.assertEqual(chan["channel_url"], self.streams)
        self.assertEqual(chan["live_count"], 1)
        self.assertEqual(list(chan["streams"][0]), ["title", "channel", "url", "concurrent_view_count", "view_count"])
        self.assertEqual(json.loads((folder / "Quiet.json").read_text(encoding="utf-8")), {"channel_url": "https://www.youtube.com/@Quiet/streams", "live_count": 0, "streams": []})
        self.assertIn(f"Wrote {folder / 'Chan.json'}", err)

    def test_without_out_dir_no_file_is_written_anywhere(self):
        import os
        before = set(os.listdir("."))
        self.run_cli("live", "@Chan")
        self.assertEqual(set(os.listdir(".")), before)

    def test_channel_is_required(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            cli.main(["research", "live"])
        self.assertIn("required", err.getvalue())


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
