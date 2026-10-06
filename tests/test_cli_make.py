"""`ytt make` through main(), the way the real command runs, with the fake YouTube handed in."""
import unittest

from ytt.ops.compile.style import Style
from ytt.ui import cli
from ytt.workspace import store
from ytt.workspace.workspace import Workspace

try:
    from test_cli_fetch import FetchCliTest, vid
    from test_auto_compile import HAVE_TOOLS
    from fakes import FakeBackend, make_videos
except ImportError:
    from tests.test_cli_fetch import FetchCliTest, vid
    from tests.test_auto_compile import HAVE_TOOLS
    from tests.fakes import FakeBackend, make_videos


@unittest.skipUnless(HAVE_TOOLS, "needs ffmpeg")
class MakeCliTest(FetchCliTest):
    def setUp(self):
        super().setUp()
        self.run_cli("workspace", "set", "make.clips_each", "2")
        self.run_cli("workspace", "set", "workers", "1")
        with Workspace.open(self.root) as ws:
            store.save_style(ws.conn, "default", Style(transition="cut", quality="fast").to_dict())
            ws.conn.commit()

    def make(self, *argv, **kw):
        return self.run_cli("make", "@Chan", "--type", "shorts", "--sort", "oldest", *argv, **kw)

    def comps(self):
        with Workspace.open(self.root) as ws:
            return {r["name"]: [c["youtube_id"] for c in store.compilation_clips(ws.conn, r["id"])]
                    for r in store.list_compilations(ws.conn)}


class MakeCommand(MakeCliTest):
    def test_a_dry_run_shows_the_plan_lists_every_clip_and_does_nothing(self):
        code, out, err = self.make("-n", "2", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("Make 2 compilations", out)
        self.assertIn("download 4 clips", out)
        self.assertIn("makes compilation_001: 2 clips", out)
        self.assertIn("- Clip 39", out)                  # the dry run lists the clips of each compilation
        self.assertIn("dry run: nothing was done", out)
        self.assertEqual((self.library_ids(), self.comps()), ([], {}))

    def test_it_fetches_and_makes_and_says_what_happened(self):
        code, out, err = self.make("-n", "2", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("made compilation_001", out)
        self.assertIn("made compilation_002", out)
        self.assertIn("Completed: 2 compilations made, 4 clips downloaded (run 1)", out)
        self.assertEqual(self.comps(), {"compilation_001": [vid(39), vid(38)], "compilation_002": [vid(37), vid(36)]})

    def test_without_a_terminal_it_refuses_instead_of_hanging(self):
        code, out, err = self.make("-n", "1")
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)
        self.assertEqual((self.library_ids(), self.comps()), ([], {}))

    def test_in_the_library_it_must_say_how_many(self):
        code, out, err = self.run_cli("make", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("How many compilations?", err)

    def test_nothing_waiting_is_not_an_error(self):
        code, out, err = self.run_cli("make", "--all", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("Nothing to do.", out)

    def test_all_makes_everything_the_library_allows_and_reports_the_leftover(self):
        self.make("--clips", "5", "--yes")                      # 2 compilations and 1 clip left over
        code, out, err = self.run_cli("make", "--all", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("Nothing to do.", out)
        self.assertEqual(len(self.comps()), 2)

    def test_the_leftover_is_reported(self):
        code, out, err = self.make("--clips", "3", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("1 clip left over, kept for the next make", out)

    def test_like_repeats_the_request_on_fresh_clips_and_typed_flags_override_it(self):
        self.make("--clips", "2", "--yes")
        code, out, err = self.run_cli("make", "--like", "1", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("Repeating how compilation 1 was made", out)
        self.assertEqual(self.comps()["compilation_002"], [vid(37), vid(36)])
        code, out, err = self.run_cli("make", "--like", "last", "--clips", "4", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(self.comps()), ["compilation_001", "compilation_002", "compilation_003", "compilation_004"])

    def test_like_something_that_was_not_made_by_make_is_refused(self):
        self.make("--clips", "2", "--yes")
        code, out, err = self.run_cli("make", "--like", "7", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("No compilation matches '7'", err)

    def test_when_every_download_fails_it_says_failed_and_exits_1(self):
        self.backend.fail = {vid(i) for i in range(40)}
        code, out, err = self.make("-n", "1", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("Failed: 0 compilations made", out)

    def test_ctrl_c_exits_130_and_keeps_what_was_downloaded(self):
        self.backend.interrupt = {vid(38)}
        code, out, err = self.make("-n", "2", "--yes")
        self.assertEqual(code, 130)
        self.assertIn("Cancelled", out)
        self.assertEqual(self.library_ids(), [vid(39)])


class MakeFlags(MakeCliTest):
    """What the flags turn into (no downloads, no renders)."""

    def request(self, *argv):
        args = cli.build_parser().parse_args(["make", *argv])
        with Workspace.open(self.root) as ws:
            return cli.make_request_from_args(ws, args)[0]

    def test_every_flag_lands_in_the_request(self):
        r = self.request("@Chan", "--type", "shorts", "-n", "3", "--per", "4", "--order", "newest", "--reverse-each",
                         "--if-short", "fetch", "--delete-used-clips", "--retry-failed", "--style", "calm",
                         "--min-views", "100", "--sort", "latest")
        self.assertEqual((r.compilations, r.per, r.order, r.play, r.if_short, r.delete_used, r.retry_failed, r.style),
                         (3, 4, "newest", "each", "fetch", True, True, "calm"))
        self.assertEqual((r.fetch.channel, r.fetch.type, r.fetch.min_views, r.fetch.sort, r.fetch.clips),
                         ("@Chan", "shorts", 100, "latest", 0))

    def test_nothing_typed_is_the_plain_defaults(self):
        r = self.request("--all")
        self.assertEqual((r.fetch, r.everything, r.compilations, r.order, r.play, r.delete_used),
                         (None, True, 0, "", "asis", None))

    def test_keep_used_clips_is_an_explicit_no(self):
        self.assertIs(self.request("--all", "--keep-used-clips").delete_used, False)

    def test_per_minutes_replaces_per(self):
        r = self.request("--all", "--per-minutes", "5")
        self.assertEqual((r.per, r.per_minutes), (0, 5))

    def test_a_list_of_videos_replaces_the_channel(self):
        import io
        from unittest import mock
        with mock.patch("sys.stdin", io.StringIO(f"{vid(1)}\n{vid(2)}\n")):
            r = self.request("--videos", "-")
        self.assertEqual((r.fetch.channel, r.fetch.videos), ("", [vid(1), vid(2)]))

    def test_conflicting_flags_are_refused(self):
        for argv in (["--all", "--reverse", "--reverse-each"], ["--all", "--per", "3", "--per-minutes", "2"],
                     ["@Chan", "--clips", "3", "--range", "1-5"], ["--all", "--delete-used-clips", "--keep-used-clips"]):
            with self.assertRaises(SystemExit):
                cli.build_parser().parse_args(["make", *argv])


if __name__ == "__main__":
    unittest.main()
