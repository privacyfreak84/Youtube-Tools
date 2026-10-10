"""The guided menu (ytt/ui/menu.py).

Part 1 answers the menu's questions from a script and checks the exact command line it builds, that the real
parser accepts it and turns it into the request the answers describe (the menu and the flags are one thing).
Part 2 runs whole flows against the fake YouTube through the real commands.
"""
import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rich.console import Console

from ytt.ops.compile import groups as grp_mod
from ytt.sources import selection as sel
from ytt.ui import cli, menu
from ytt.ui.menu import Menu
from ytt.workspace import store
from ytt.workspace.workspace import Workspace

try:
    from scripted_prompts import CANCEL, ScriptedPrompter
    from test_cli_make import MakeCliTest, vid
except ImportError:
    from tests.scripted_prompts import CANCEL, ScriptedPrompter
    from tests.test_cli_make import MakeCliTest, vid

GO, DRY, BACK = "go", "dry", "back"
EVERY_EXTRA = ["style", "size", "order", "play", "if_short", "delete_used", "retry", "dups", "refetch", "quality"]


class MenuTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytt_menu_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "ws"
        with contextlib.redirect_stdout(io.StringIO()):
            cli.run_argv(["workspace", "init"], self.root)
        self.calls = []
        self.shown = io.StringIO()

    def record(self, argv):
        self.calls.append(list(argv))
        return 0

    def menu(self, *answers, run=None, root=None, **kw):
        self.ask = ScriptedPrompter(*answers)
        return Menu(root or self.root, run or self.record, self.ask, Console(file=self.shown, width=200, highlight=False), **kw)

    def request(self, argv):
        """What the real command makes of these words: the parser, then the same step `ytt make` takes."""
        args = cli.build_parser().parse_args(["--workspace", str(self.root), *argv])
        with Workspace.open(self.root) as ws:
            return cli.make_request_from_args(ws, args)[0]

    def parses(self, argv):
        return cli.build_parser().parse_args(["--workspace", str(self.root), *argv])

    def only_call(self):
        self.assertEqual(len(self.calls), 1, self.calls)
        return self.calls[0]


class MakeFromAChannel(MenuTest):
    def test_compilations_from_a_channel(self):
        self.menu("channel", "@Chan", "shorts", "oldest", "compilations", "3", [], [], GO).flow_make()
        argv = self.only_call()
        self.assertEqual(argv, ["make", "@Chan", "--type", "shorts", "--sort", "oldest", "-n", "3"])
        r = self.request(argv)
        self.assertEqual((r.compilations, r.fetch.channel, r.fetch.type, r.fetch.sort), (3, "@Chan", "shorts", "oldest"))

    def test_the_usual_choices_add_no_flags(self):
        self.menu("channel", "@Chan", "videos", "popular", "clips", "45", [], [], GO).flow_make()
        self.assertEqual(self.only_call(), ["make", "@Chan", "--clips", "45"])

    def test_a_stretch_of_the_list(self):
        self.menu("channel", "@Chan", "videos", "popular", "range", "25", "70", [], [], GO).flow_make()
        argv = self.only_call()
        self.assertEqual(argv, ["make", "@Chan", "--range", "25-70"])
        self.assertEqual(self.request(argv).fetch.range, "25-70")

    def test_a_stretch_with_no_end_runs_to_the_end_of_the_list(self):
        self.menu("channel", "@Chan", "videos", "popular", "range", "25", "", [], [], GO).flow_make()
        self.assertEqual(self.only_call()[-2:], ["--range", "25-"])

    def test_the_end_of_a_stretch_cannot_be_before_its_start(self):
        m = self.menu("channel", "@Chan", "videos", "popular", "range", "25", "10", "70", [], [], GO)
        m.flow_make()
        self.assertEqual(self.only_call()[-2:], ["--range", "25-70"])
        self.assertIn("before place 25", self.ask.refused[0][2])

    def test_the_limits(self):
        m = self.menu("channel", "@Chan", "videos", "popular", "clips", "10", ["dates", "views", "length"],
                      "2025-01-01", "", "5000", "0.5", "2.0", [], GO)
        m.flow_make()
        argv = self.only_call()
        self.assertEqual(argv[2:], ["--clips", "10", "--from", "2025-01-01", "--min-views", "5000",
                                    "--min-length", "0.5", "--max-length", "2"])
        f = self.request(argv).fetch
        self.assertEqual((f.date_from, f.date_to, f.min_views, f.min_length, f.max_length),
                         ("2025-01-01", "", 5000, 0.5, 2))

    def test_a_date_range_typed_once_is_not_carried_into_the_next_make(self):
        # The old tool kept the previous run's dates as the answers to the next run's questions, so pressing Enter
        # silently cut an 844-video channel down to one video (fixed in the old tool on 2026-10-02).
        first = ["channel", "@Chan", "videos", "popular", "clips", "10", ["dates"], "2025-01-01", "2025-06-30", [], GO]
        second = ["channel", "@Chan", "videos", "popular", "clips", "10", [], [], GO]
        m = self.menu(*first, *second)
        m.flow_make()
        m.flow_make()
        self.assertEqual(self.calls[0][-4:], ["--from", "2025-01-01", "--to", "2025-06-30"])
        self.assertNotIn("--from", self.calls[1])
        self.assertNotIn("--to", self.calls[1])
        date_questions = [a for a in self.ask.asked if "date" in a[1] and a[0] == "text"]
        self.assertEqual([a[3] for a in date_questions], ["", ""])                       # no starting answer, ever
        limit_questions = [a for a in self.ask.asked if a[1].startswith("Narrow it down")]
        self.assertEqual(len(limit_questions), 2)                                         # asked again, nothing pre-ticked

    def test_every_extra_reaches_the_request(self):
        m = self.menu("channel", "@Chan", "videos", "popular", "clips", "10", [], EVERY_EXTRA,
                      "default", "clips", "20", "newest", "each", "fetch", True, "720", GO)
        m.flow_make()
        argv = self.only_call()
        self.assertEqual(argv[4:], ["--style", "default", "--per", "20", "--order", "newest", "--reverse-each",
                                    "--if-short", "fetch", "--delete-used-clips", "--retry-failed",
                                    "--keep-duplicates", "--refetch", "--max-height", "720"])
        r = self.request(argv)
        self.assertEqual((r.style, r.per, r.order, r.play, r.if_short, r.delete_used, r.retry_failed),
                         ("default", 20, "newest", "each", "fetch", True, True))
        self.assertEqual((r.fetch.keep_duplicates, r.fetch.refetch, r.fetch.max_height), (True, True, 720))

    def test_a_size_in_minutes_and_the_whole_sequence_backwards(self):
        m = self.menu("channel", "@Chan", "videos", "popular", "clips", "10", [], ["size", "play"],
                      "minutes", "8", "reverse", GO)
        m.flow_make()
        argv = self.only_call()
        self.assertEqual(argv[4:], ["--per-minutes", "8", "--reverse"])
        r = self.request(argv)
        self.assertEqual((r.per, r.per_minutes, r.play), (0, 8, "reverse"))

    def test_the_usual_size_is_what_the_question_starts_with(self):
        m = self.menu("channel", "@Chan", "videos", "popular", "clips", "10", [], ["size", "order", "if_short"],
                      "clips", "15", "name", "keep", GO)
        m.flow_make()
        defaults = {message: d for kind, message, _, d in self.ask.asked if kind in ("text", "select")}
        self.assertEqual(defaults["How many clips in each?"], "15")
        self.assertEqual(defaults["Which clips go first?"], "name")
        self.assertEqual(defaults["Clips that cannot fill a compilation:"], "keep")

    def test_filling_a_compilation_by_downloading_more_is_only_offered_with_a_channel(self):
        self.menu("library", "all", ["if_short"], "keep", GO).flow_make()
        offered = [labels for kind, message, labels, _ in self.ask.asked if message.startswith("Clips that cannot")]
        self.assertEqual(len(offered[0]), 2)
        self.menu("channel", "@Chan", "videos", "popular", "clips", "10", [], ["if_short"], "keep", GO).flow_make()
        offered = [labels for kind, message, labels, _ in self.ask.asked if message.startswith("Clips that cannot")]
        self.assertEqual(len(offered[0]), 3)

    def test_bad_answers_are_refused_with_a_reason_and_asked_again(self):
        m = self.menu("channel", "", "my channel", "@Chan", "videos", "popular", "clips", "0", "many", "12",
                      ["dates"], "31 Feb", "2025-01-31", "", [], GO)
        m.flow_make()
        argv = self.only_call()
        self.assertEqual(argv[1:3], ["@Chan", "--clips"])
        self.assertEqual(argv[3:], ["12", "--from", "2025-01-31"])
        self.assertEqual([a for _, a, _ in self.ask.refused], ["", "my channel", "0", "many", "31 Feb"])


class MakeFromElsewhere(MenuTest):
    def test_the_clips_already_in_the_library(self):
        self.menu("library", "n", "2", [], GO).flow_make()
        self.assertEqual(self.only_call(), ["make", "-n", "2"])
        self.calls.clear()
        self.menu("library", "all", [], GO).flow_make()
        self.assertEqual(self.only_call(), ["make", "--all"])

    def test_a_list_of_videos_in_a_file(self):
        f = self.tmp / "ids.txt"
        f.write_text("vid00000001\nhttps://youtu.be/vid00000002\n")
        m = self.menu("list", str(self.tmp / "nope.txt"), str(f), ["dups"], GO)
        m.flow_make()
        argv = self.only_call()
        self.assertEqual(argv, ["make", "--videos", str(f), "--keep-duplicates"])
        self.assertEqual(self.request(argv).fetch.videos, ["vid00000001", "vid00000002"])
        self.assertIn("no file", self.ask.refused[0][2])

    def test_a_file_with_something_that_is_not_a_video_is_refused(self):
        f = self.tmp / "bad.txt"
        f.write_text("vid00000001\nnot a video\n")
        g = self.tmp / "empty.txt"
        g.write_text("# nothing\n")
        ok = self.tmp / "ok.txt"
        ok.write_text("vid00000001\n")
        self.menu("list", str(f), str(g), str(ok), [], GO).flow_make()
        self.assertIn("line 2", self.ask.refused[0][2])
        self.assertIn("no videos", self.ask.refused[1][2])

    def test_repeating_an_earlier_make_is_not_offered_when_there_is_nothing_to_repeat(self):
        self.menu("library", "all", [], GO).flow_make()
        offered = self.ask.asked[0][2]
        self.assertFalse([label for label in offered if "earlier make" in label])

    def test_the_picture_options_do_not_apply_to_clips_that_are_already_here(self):
        self.menu("library", "all", [], GO).flow_make()
        extras = next(labels for kind, message, labels, _ in self.ask.asked if message.startswith("Change anything"))
        self.assertTrue(extras)
        self.assertFalse([label for label in extras if "re-upload" in label or "quality" in label])


class GetClipsOnly(MenuTest):
    def test_new_clips_from_a_channel(self):
        self.menu("channel", "@Chan", "shorts", "latest", "clips", "15", [], [], GO).flow_fetch()
        argv = self.only_call()
        self.assertEqual(argv, ["fetch", "@Chan", "--type", "shorts", "--sort", "latest", "--clips", "15"])
        self.assertEqual(self.parses(argv).clips, 15)

    def test_enough_for_some_compilations(self):
        self.menu("channel", "@Chan", "videos", "popular", "compilations", "2", [], [], GO).flow_fetch()
        argv = self.only_call()
        self.assertEqual(argv, ["fetch", "@Chan", "-n", "2"])
        self.assertEqual(self.parses(argv).compilations, 2)

    def test_only_the_download_options_are_offered(self):
        self.menu("channel", "@Chan", "videos", "popular", "clips", "5", [], ["refetch", "quality"], "480", GO).flow_fetch()
        self.assertEqual(self.only_call()[-3:], ["--refetch", "--max-height", "480"])
        extras = next(labels for kind, message, labels, _ in self.ask.asked if message.startswith("Change anything"))
        self.assertEqual(len(extras), 3)

    def test_a_list_of_videos(self):
        f = self.tmp / "ids.txt"
        f.write_text("vid00000001\n")
        self.menu("list", str(f), [], GO).flow_fetch()
        self.assertEqual(self.only_call(), ["fetch", "--videos", str(f)])


class ReadyWhatNext(MenuTest):
    def test_a_dry_run_adds_the_flag(self):
        self.menu("library", "all", [], DRY).flow_make()
        self.assertEqual(self.only_call(), ["make", "--all", "--dry-run"])

    def test_going_back_runs_nothing(self):
        self.menu("library", "all", [], BACK).flow_make()
        self.assertEqual(self.calls, [])

    def test_the_command_is_shown_before_it_runs(self):
        self.menu("channel", "@Chan", "shorts", "popular", "clips", "9", [], [], BACK).flow_make()
        self.assertIn("Command line for this:  ytt make @Chan --type shorts --clips 9", self.shown.getvalue())

    def test_a_workspace_that_was_typed_on_the_command_line_is_part_of_the_shown_command(self):
        self.menu("library", "all", [], BACK, workspace_flag="/data/my ws").flow_make()
        self.assertIn("ytt --workspace '/data/my ws' make --all", self.shown.getvalue())


class LookingAround(MenuTest):
    def test_library_views(self):
        m = self.menu("stats", "clips", "unused", "clips", "missing", "sources", "runs", BACK)
        m.flow_library()
        self.assertEqual(self.calls, [["library", "stats"], ["library", "clips", "--unused"],
                                      ["library", "clips", "--status", "missing"], ["library", "sources"],
                                      ["library", "runs"]])

    def test_cancelling_a_question_inside_the_library_stays_in_the_library(self):
        self.menu("clips", CANCEL, "stats", BACK).flow_library()
        self.assertEqual(self.calls, [["library", "stats"]])

    def test_forgetting_with_nothing_made_says_so(self):
        self.menu("forget", BACK).flow_library()
        self.assertEqual(self.calls, [])
        self.assertIn("no compilations to forget", self.shown.getvalue())

    def test_remaking_with_nothing_made_says_so(self):
        self.menu().flow_remake()
        self.assertEqual(self.calls, [])
        self.assertIn("nothing to remake", self.shown.getvalue())


class Styles(MenuTest):
    def test_list_and_show(self):
        self.menu("list", "show", "default", BACK).flow_style()
        self.assertEqual(self.calls, [["style", "list"], ["style", "show", "default"]])

    def test_the_default_is_marked(self):
        self.menu("show", CANCEL, BACK).flow_style()
        labels = next(labels for kind, message, labels, _ in self.ask.asked if message == "Which style?")
        self.assertEqual(labels, ["default (the default)"])

    def test_a_new_style_needs_a_good_new_name_and_is_edited_with_the_guided_editor(self):
        m = self.menu("edit", menu.NEW_STYLE, "has space", "default", "bright", BACK)
        m.flow_style()
        self.assertEqual(self.calls, [["style", "edit", "bright"]])
        self.assertEqual(len(self.ask.refused), 2)
        self.assertIn("already a style", self.ask.refused[1][2])

    def test_changing_an_existing_style(self):
        self.menu("edit", "default", BACK).flow_style()
        self.assertEqual(self.calls, [["style", "edit", "default"]])

    def test_the_default_style_is_not_offered_for_deleting(self):
        self.menu("delete", BACK).flow_style()
        self.assertEqual(self.calls, [])
        self.assertIn("cannot be deleted", self.shown.getvalue())

    def test_deleting_another_style_goes_through_the_plan(self):
        with Workspace.open(self.root) as ws:
            store.save_style(ws.conn, "spare", {})
            ws.conn.commit()
        self.menu("delete", "spare", GO, BACK).flow_style()
        self.assertEqual(self.calls, [["style", "delete", "spare"]])


class WorkspaceAndSettings(MenuTest):
    def test_show(self):
        self.menu("show", BACK).flow_workspace()
        self.assertEqual(self.calls, [["workspace", "show"]])

    def test_changing_a_number_and_a_yes_or_no_and_a_list(self):
        m = self.menu("set", "workers", "lots", "-1", "5", "set", "delete_used_clips", True,
                      "set", "watch_folders", "/a, /b", BACK)
        m.flow_workspace()
        self.assertEqual(self.calls, [["workspace", "set", "workers", "5"],
                                      ["workspace", "set", "delete_used_clips", "yes"],
                                      ["workspace", "set", "watch_folders", "/a, /b"]])
        self.assertEqual(len(self.ask.refused), 2)

    def test_settings_inside_the_make_table_are_offered_too(self):
        self.menu("set", "make.clips_each", "20", BACK).flow_workspace()
        self.assertEqual(self.calls, [["workspace", "set", "make.clips_each", "20"]])
        labels = next(labels for kind, message, labels, _ in self.ask.asked if message == "Which setting?")
        self.assertIn("make.clips_each   (now: 15)", labels)
        self.assertIn("delete_used_clips   (now: no)", labels)

    def test_bringing_in_the_old_state_previews_it_first_and_only_then_imports(self):
        old = self.tmp / "old"
        old.mkdir()
        self.menu("old", str(self.tmp / "missing"), str(old), True, BACK).flow_workspace()
        self.assertEqual(self.calls, [["workspace", "import", str(old), "--dry-run"], ["workspace", "import", str(old)]])
        self.assertIn("no folder", self.ask.refused[0][2])

    def test_saying_no_after_the_preview_imports_nothing(self):
        old = self.tmp / "old"
        old.mkdir()
        self.menu("old", str(old), False, BACK).flow_workspace()
        self.assertEqual(self.calls, [["workspace", "import", str(old), "--dry-run"]])


class TheMainLoop(MenuTest):
    def test_quit(self):
        self.assertEqual(self.menu("quit").start(), 0)

    def test_ctrl_c_at_the_main_menu_quits(self):
        self.assertEqual(self.menu(CANCEL).start(), 0)

    def test_the_header_says_where_the_workspace_is_and_what_is_in_it(self):
        self.menu("quit").start()
        out = self.shown.getvalue()
        self.assertIn(str(self.root), out)
        self.assertIn("0 clips (0 not used yet) · 0 compilations", out)

    def test_cancelling_in_the_middle_of_a_flow_returns_to_the_main_menu(self):
        self.menu("make", "channel", "@Chan", CANCEL, "quit").start()
        self.assertEqual(self.calls, [])
        self.assertIn("Back at the menu.", self.shown.getvalue())

    def test_an_error_from_a_flow_is_shown_and_the_menu_carries_on(self):
        m = self.menu("remake", "quit")
        with mock.patch.object(Menu, "flow_remake", side_effect=menu.OpError("boom")):
            self.assertEqual(m.start(), 0)
        self.assertIn("Error: boom", self.shown.getvalue())

    def test_every_entry_of_the_main_menu_has_a_flow(self):
        for label, key in menu.MAIN:
            if key != "quit":
                self.assertTrue(callable(getattr(Menu, "flow_" + key, None)), label)

    def test_each_flow_is_reached_from_the_main_menu(self):
        self.menu("make", "library", "all", [], BACK, "library", BACK, "style", BACK, "workspace", BACK, "quit").start()
        self.assertEqual(self.calls, [])
        self.menu("fetch", "list", CANCEL, "remake", "quit").start()

    def test_every_way_of_choosing_has_a_plain_label(self):
        self.assertEqual(set(menu.SORT_LABELS), set(sel.SORTS))
        self.assertEqual(set(menu.ORDER_LABELS), set(grp_mod.ORDERS))
        self.assertEqual(set(menu.IF_SHORT_LABELS), set(grp_mod.IF_SHORT))

    def test_the_menu_never_shows_list_position_syntax(self):
        self.menu("channel", "@Chan", "videos", "popular", "range", "5", "9", ["dates", "views", "length"], "", "", "1", "",
                  "", EVERY_EXTRA, "default", "clips", "20", "newest", "each", "fetch", True, "720", BACK).flow_make()
        shown = self.ask.everything_shown()
        self.assertGreater(len(shown), 500)
        for word in ("last:", "every:", "random:", "new:", "comps:", "--take", "--range", "--clips", "1-10"):
            self.assertNotIn(word, shown)


class FirstRun(MenuTest):
    def test_without_a_workspace_it_offers_to_set_one_up(self):
        fresh = self.tmp / "fresh"
        self.assertEqual(self.menu("quit", root=fresh).start(), 0)
        self.assertIn("no workspace", self.shown.getvalue())
        self.assertFalse(fresh.exists())

    def test_setting_up_an_empty_workspace(self):
        fresh = self.tmp / "fresh"
        calls = []

        def run(argv):
            calls.append(argv)
            return cli.run_argv(argv, fresh)

        m = self.menu("empty", "quit", run=run, root=fresh)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.start(), 0)
        self.assertEqual(calls, [["workspace", "init"]])
        self.assertTrue((fresh / "ytt.db").exists())
        self.assertIn("0 clips", self.shown.getvalue())          # and then it showed the main menu with its header

    def test_setting_up_and_bringing_in_the_old_state(self):
        fresh, old = self.tmp / "fresh", self.tmp / "old"
        old.mkdir()
        m = self.menu("old", str(old), True, "quit", root=fresh,
                      run=lambda argv: (self.calls.append(argv), cli.run_argv(argv, fresh))[1])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            m.start()
        self.assertEqual(self.calls[0][:2], ["workspace", "import"])

    def test_cancelling_the_first_question_quits(self):
        self.assertEqual(self.menu(CANCEL, root=self.tmp / "fresh").start(), 0)


class WholeFlowsAgainstTheFakeYouTube(MakeCliTest):
    """The same flows with the real commands behind them (and the fake YouTube instead of the real one)."""

    def setUp(self):
        super().setUp()
        for patch in (mock.patch.object(cli, "is_tty", lambda: True), mock.patch("builtins.input", return_value="y")):
            patch.start()
            self.addCleanup(patch.stop)
        self.shown = io.StringIO()

    def menu(self, *answers):
        self.ask = ScriptedPrompter(*answers)
        return Menu(self.root, lambda argv: cli.run_argv(argv, self.root), self.ask,
                    Console(file=self.shown, width=200, highlight=False))

    def run_menu(self, flow, *answers):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            getattr(self.menu(*answers), flow)()
        return out.getvalue()

    def make_two(self):
        return self.run_menu("flow_make", "channel", "@Chan", "shorts", "oldest", "compilations", "2", [], [], GO)

    def test_making_compilations_from_a_channel(self):
        out = self.make_two()
        self.assertIn("Completed: 2 compilations made, 4 clips downloaded", out)
        self.assertEqual(self.comps(), {"compilation_001": [vid(39), vid(38)], "compilation_002": [vid(37), vid(36)]})

    def test_the_plan_is_shown_and_a_no_stops_it(self):
        with mock.patch("builtins.input", return_value="n"):
            out = self.make_two()
        self.assertIn("Make 2 compilations", out)
        self.assertIn("Cancelled; nothing was done.", out)
        self.assertEqual((self.backend.started, self.comps()), ([], {}))

    def test_the_every_detail_choice_does_nothing(self):
        out = self.run_menu("flow_make", "channel", "@Chan", "shorts", "oldest", "compilations", "2", [], [], DRY)
        self.assertIn("dry run: nothing was done", out)
        self.assertEqual((self.backend.started, self.comps()), ([], {}))

    def test_getting_clips_only(self):
        out = self.run_menu("flow_fetch", "channel", "@Chan", "shorts", "oldest", "clips", "3", [], [], GO)
        self.assertIn("3 clips downloaded", out)
        self.assertEqual(self.library_ids(), [vid(39), vid(38), vid(37)])
        self.assertEqual(self.comps(), {})

    def test_remaking_the_last_compilation_with_a_new_order(self):
        self.make_two()
        out = self.run_menu("flow_remake", "last", "same", "reversed", "new", GO)
        self.assertIn("1 compilation made", out)
        self.assertEqual(self.comps()["compilation_003"], [vid(36), vid(37)])

    def test_remaking_picked_compilations_from_the_list(self):
        self.make_two()
        out = self.run_menu("flow_remake", "pick", ["compilation_001", "compilation_002"], "same", "recorded", "new", GO)
        self.assertIn("2 compilations made", out)
        self.assertEqual(len(self.comps()), 4)

    def test_repeating_an_earlier_make_on_fresh_clips(self):
        self.make_two()
        out = self.run_menu("flow_make", "like", "compilation_002", [], GO)
        self.assertIn("Repeating how compilation_002 was made", out)
        self.assertEqual(self.comps()["compilation_003"], [vid(35), vid(34)])

    def test_the_list_of_repeatable_makes_is_newest_first_and_leaves_out_what_ytt_did_not_make(self):
        self.make_two()
        self.run_menu("flow_remake", "last", "same", "recorded", "new", GO)       # compilation_003 is a remake
        self.run_menu("flow_make", "like", "compilation_002", [], BACK)
        labels = next(labels for kind, message, labels, _ in self.ask.asked if message.startswith("Which one should"))
        self.assertTrue(labels[0].startswith("compilation_003"))                    # a remake follows its parent back to the make

    def test_forgetting_a_compilation_frees_its_clips(self):
        self.make_two()
        out = self.run_menu("flow_library", "forget", "pick", ["compilation_001"], GO, BACK)
        self.assertIn("Forgot 1 compilation", out)
        self.assertEqual(list(self.comps()), ["compilation_002"])

    def test_the_library_views_print_what_the_commands_print(self):
        self.make_two()
        out = self.run_menu("flow_library", "stats", "compilations", BACK)
        self.assertIn("2 compilations", out)
        self.assertIn("compilation_002", out)

    def test_the_whole_thing_from_the_main_menu(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            self.menu("make", "channel", "@Chan", "shorts", "oldest", "compilations", "1", [], [], GO,
                      "library", "stats", BACK, "quit").start()
        self.assertIn("Completed: 1 compilation made", out.getvalue())
        self.assertIn("1 compilation", self.shown.getvalue())                       # the header on the second round


if __name__ == "__main__":
    unittest.main()
