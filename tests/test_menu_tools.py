"""The guided menu's flows for research, stitch, doctor and transition videos: the exact command line each set of
answers builds, that the real parser accepts it, and that wrong answers are refused with a reason."""
import unittest
from pathlib import Path

try:
    from scripted_prompts import CANCEL
    from test_menu import BACK, GO, DRY, MenuTest
except ImportError:
    from tests.scripted_prompts import CANCEL
    from tests.test_menu import BACK, GO, DRY, MenuTest

ID1, ID2 = "aaaaaaaaaaa", "bbbbbbbbbbb"
NO = False
YES = True


class ResearchFlows(MenuTest):
    def setUp(self):
        super().setUp()
        self.keep = str(self.root / "cache" / "last-research.csv")

    def research(self, *answers):
        """The command the answers build. The tools that find videos also save their rows to cache/last-research.csv
        (see HandoffToMake); that pair is taken off here so these tests are about the questions."""
        self.menu(*answers, BACK).flow_research()
        return self.only_research_call()

    def only_research_call(self):
        argv = self.only_call()
        return argv[:-2] if argv[-2:] == ["--csv", self.keep] else argv

    def test_channels_from_a_search(self):
        argv = self.research("channels", "search", "cute cats", NO, NO)
        self.assertEqual(argv, ["research", "channels", "--search", "cute cats"])
        self.assertEqual(self.parses(argv).search, ["cute cats"])

    def test_channels_from_a_seed_with_subscribers_and_a_csv_file(self):
        argv = self.research("channels", "seed", "@Seed", YES, YES, str(self.tmp / "found.csv"))
        self.assertEqual(argv, ["research", "channels", "--seed", "@Seed", "--with-subs", "--csv", str(self.tmp / "found.csv")])
        args = self.parses(argv)
        self.assertEqual((args.seed, args.with_subs, args.csv), (["@Seed"], True, str(self.tmp / "found.csv")))

    def test_outliers_with_the_usual_choices_add_no_flags(self):
        argv = self.research("outliers", "@A @B", "videos", "3", "", NO)
        self.assertEqual(argv, ["research", "outliers", "@A", "@B"])
        self.assertEqual(self.parses(argv).channels, ["@A", "@B"])

    def test_outliers_with_other_choices(self):
        argv = self.research("outliers", "@A", "all", "5", "10", NO)
        self.assertEqual(argv, ["research", "outliers", "@A", "--type", "all", "--multiplier", "5", "--top", "10"])
        args = self.parses(argv)
        self.assertEqual((args.type, args.multiplier, args.top), ("all", 5.0, 10))

    def test_a_multiplier_written_as_3_point_0_is_still_the_usual(self):
        self.assertEqual(self.research("outliers", "@A", "videos", "3.0", "", NO), ["research", "outliers", "@A"])

    def test_niche(self):
        argv = self.research("niche", "cats", "shorts", "3", "", NO)
        self.assertEqual(argv, ["research", "niche", "--search", "cats", "--type", "shorts"])
        self.assertEqual(self.parses(argv).type, "shorts")

    def test_clip_usual_and_changed(self):
        self.assertEqual(self.research("clip", "cats", "all", "3", "7", "20", NO), ["research", "clip", "--search", "cats"])
        self.calls.clear()
        argv = self.research("clip", "cats", "videos", "2.5", "3", "", NO)
        self.assertEqual(argv, ["research", "clip", "--search", "cats", "--type", "videos", "--multiplier", "2.5",
                                "--max-age-days", "3", "--top", "0"])
        self.calls.clear()
        self.assertEqual(self.research("clip", "cats", "all", "3", "7", "5", NO)[-2:], ["--top", "5"])
        args = self.parses(argv)
        self.assertEqual((args.max_age_days, args.top), (3, 0))

    def test_table_usual_and_changed(self):
        self.assertEqual(self.research("table", "@Chan", "videos", "latest", "", NO, NO), ["research", "table", "@Chan"])
        self.calls.clear()
        argv = self.research("table", "@Chan", "shorts", "popular", "10", YES, NO)
        self.assertEqual(argv, ["research", "table", "@Chan", "--type", "shorts", "--sort", "popular", "--limit", "10", "--full"])
        args = self.parses(argv)
        self.assertEqual((args.type, args.sort, args.limit, args.full), ("shorts", "popular", 10, True))

    def test_live(self):
        argv = self.research("live", "@A @B", NO)
        self.assertEqual(argv, ["research", "live", "@A", "@B"])
        self.assertEqual(self.parses(argv).channels, ["@A", "@B"])

    def test_tags_from_pasted_links_and_ids(self):
        argv = self.research("tags", "paste", f"{ID1} https://youtu.be/{ID2}", NO)
        self.assertEqual(argv, ["research", "tags", ID1, f"https://youtu.be/{ID2}"])
        self.assertEqual(self.parses(argv).videos, [ID1, f"https://youtu.be/{ID2}"])

    def test_tags_from_a_file(self):
        path = self.tmp / "ids.txt"
        path.write_text(f"{ID1}\n{ID2}\n", encoding="utf-8")
        argv = self.research("tags", "file", str(path), NO)
        self.assertEqual(argv, ["research", "tags", "--videos", str(path)])
        self.assertEqual(self.parses(argv).videos_file, str(path))

    def test_wrong_answers_are_refused_with_a_reason_and_asked_again(self):
        m = self.menu("outliers", "  ", "@A", "videos", "0", "3", "x", "4", NO, BACK)
        m.flow_research()
        self.assertEqual(self.only_research_call(), ["research", "outliers", "@A", "--top", "4"])
        reasons = [why for _, _, why in self.ask.refused]
        self.assertIn("Type at least one channel", reasons[0])
        self.assertEqual(reasons[1], "Type a number above 0.")
        self.assertEqual(reasons[2], "Type a whole number, 1 or more.")

    def test_tags_refuses_something_that_is_not_a_video_and_a_file_that_is_not_there(self):
        self.menu("tags", "paste", "not a video", ID1, NO, "tags", "file", str(self.tmp / "none.txt"), CANCEL, BACK).flow_research()
        self.assertEqual(self.only_call(), ["research", "tags", ID1])
        self.assertIn("not a YouTube video link or id: not", self.ask.refused[0][2])
        self.assertEqual(self.ask.refused[1][2], "There is no file there.")

    def test_the_csv_name_must_be_a_csv_in_a_folder_that_exists(self):
        good = str(self.tmp / "ok.csv")
        self.menu("live", "@A", YES, "results.txt", str(self.tmp / "nowhere" / "x.csv"), str(self.tmp), good, BACK).flow_research()
        self.assertEqual(self.only_call()[-2:], ["--csv", good])
        self.assertEqual([why for _, _, why in self.ask.refused],
                         ["Use a file name ending in .csv.", "That folder does not exist.", "Use a file name ending in .csv."])

    def test_cancelling_a_question_returns_to_the_research_menu_and_nothing_runs(self):
        self.menu("outliers", "@A", CANCEL, "live", "@B", NO, BACK).flow_research()
        self.assertEqual(self.only_research_call(), ["research", "live", "@B"])

    def test_every_tool_in_the_research_menu_has_its_questions_and_is_a_real_command(self):
        import contextlib
        import io
        from ytt.ui import menu_tools, research_cli
        keys = [key for _, key in menu_tools.RESEARCH if key != "back"]
        self.assertEqual(set(keys), set(research_cli.TOOLS))                     # the menu offers exactly the tools there are
        for key in keys:
            self.assertTrue(callable(getattr(menu_tools.ToolFlows, "_research_" + key, None)), key)
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                self.parses(["research", key, "--help"])
            self.assertEqual(stopped.exception.code, 0)

    def test_the_command_is_shown_before_it_runs(self):
        self.research("live", "@A", NO)
        self.assertIn("Command line for this:  ytt research live @A", self.shown.getvalue())

    def test_research_is_reached_from_the_main_menu(self):
        self.menu("research", BACK, "quit").start()
        self.assertEqual(self.calls, [])


class HandoffToMake(MenuTest):
    """A research result that is a list of videos can become a compilation (DESIGN.md section 14)."""

    def setUp(self):
        super().setUp()
        self.ids = ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]
        self.keep = self.root / "cache" / "last-research.csv"
        self.exit_code = 0
        self.rows = [*self.ids, self.ids[0], ""]                         # a repeat and an empty id: neither counts

    def run_that_saves(self, argv):
        """Stands in for the real command: records the words and writes the CSV the real one would."""
        self.calls.append(list(argv))
        if "--csv" in argv and self.exit_code == 0:
            path = Path(argv[argv.index("--csv") + 1])
            path.write_text("id,title\n" + "".join(f"{i},T\n" for i in self.rows), encoding="utf-8")
        return self.exit_code

    def flow(self, *answers):
        self.menu(*answers, BACK, run=self.run_that_saves).flow_research()
        return self.calls

    def test_the_tools_that_find_videos_keep_their_rows_and_offer_a_compilation(self):
        calls = self.flow("outliers", "@A", "videos", "3", "", NO, YES, [], GO)
        self.assertEqual(calls[0], ["research", "outliers", "@A", "--csv", str(self.keep)])
        self.assertEqual((self.root / "cache" / "research-ids.txt").read_text(), "\n".join(self.ids) + "\n")
        make = calls[1]
        self.assertEqual(make, ["make", "--videos", str(self.root / "cache" / "research-ids.txt")])
        self.assertEqual(self.request(make).fetch.videos, self.ids)
        self.assertIn("Make a compilation from these 3 video(s)?", self.ask.everything_shown())
        self.assertIn("Command line for this:  ytt make --videos", self.shown.getvalue())

    def test_the_make_questions_that_follow_are_the_usual_ones(self):
        calls = self.flow("live", "@A", NO, YES, ["dups"], GO)
        self.assertEqual(calls[1][-1], "--keep-duplicates")
        self.assertIn("Change anything else?", self.ask.everything_shown())

    def test_saying_no_makes_nothing(self):
        calls = self.flow("niche", "cats", "videos", "3", "", NO, NO)
        self.assertEqual(len(calls), 1)
        self.assertFalse((self.root / "cache" / "research-ids.txt").exists())

    def test_going_back_from_the_make_questions_makes_nothing_and_stays_in_research(self):
        calls = self.flow("clip", "cats", "all", "3", "7", "20", NO, YES, [], BACK)
        self.assertEqual([c[0] for c in calls], ["research"])

    def test_a_csv_the_person_asked_for_is_the_one_used(self):
        mine = str(self.tmp / "mine.csv")
        calls = self.flow("table", "@Chan", "videos", "latest", "", NO, YES, mine, YES, [], GO)
        self.assertEqual(calls[0][-2:], ["--csv", mine])
        self.assertEqual(calls[0].count("--csv"), 1)
        self.assertEqual(self.request(calls[1]).fetch.videos, self.ids)
        self.assertFalse(self.keep.exists())

    def test_nothing_is_offered_when_the_command_failed_or_found_no_videos(self):
        self.exit_code = 1
        self.assertEqual(len(self.flow("outliers", "@A", "videos", "3", "", NO)), 1)
        self.calls.clear()
        self.exit_code, self.rows = 0, []
        self.assertEqual(len(self.flow("outliers", "@A", "videos", "3", "", NO)), 1)
        self.assertNotIn("Make a compilation", self.ask.everything_shown())

    def test_channels_and_tags_never_offer_it_and_never_save_a_csv_unasked(self):
        calls = self.flow("channels", "search", "cats", NO, NO)
        self.assertEqual(calls, [["research", "channels", "--search", "cats"]])
        calls = self.flow("tags", "paste", self.ids[0], NO)
        self.assertEqual(calls[-1], ["research", "tags", self.ids[0]])
        self.assertNotIn("Make a compilation", self.ask.everything_shown())


class StitchFlow(MenuTest):
    def stitch(self, *answers):
        self.menu(*answers).flow_stitch()
        return self.calls

    def test_the_usual_choices_add_no_flags(self):
        calls = self.stitch("a.mp4 b.mp4", "compilation.mp4", "fade", "1", [], GO)
        self.assertEqual(calls, [["stitch", "a.mp4", "b.mp4"]])
        self.assertEqual(self.parses(calls[0]).inputs, ["a.mp4", "b.mp4"])

    def test_a_quoted_path_with_spaces_stays_one_word(self):
        calls = self.stitch('"my clips/a.mp4" b.mp4', "compilation.mp4", "cut", [], GO)
        self.assertEqual(calls[0][:3], ["stitch", "my clips/a.mp4", "b.mp4"])

    def test_every_change(self):
        calls = self.stitch("clips/*.mp4", "out.mkv", "other", "circleopen", "2", ["order", "size", "silent"], "date", "1080p", "30", GO)
        argv = calls[0]
        self.assertEqual(argv, ["stitch", "clips/*.mp4", "-o", "out.mkv", "-t", "circleopen", "-d", "2", "--sort", "date",
                                "--resolution", "1080p", "--fps", "30", "--no-audio"])
        args = self.parses(argv)
        self.assertEqual((args.output, args.transition, args.duration, args.sort, args.resolution, args.fps, args.no_audio),
                         ("out.mkv", "circleopen", 2.0, "date", "1080p", "30", True))

    def test_a_hard_cut_has_no_length_to_ask_about(self):
        self.stitch("a.mp4 b.mp4", "compilation.mp4", "cut", [], GO)
        self.assertNotIn("How long should each one last, in seconds?", self.ask.everything_shown())
        self.assertEqual(self.calls[0], ["stitch", "a.mp4", "b.mp4", "-t", "cut"])

    def test_random_order_and_a_different_transition_each_time(self):
        calls = self.stitch("a.mp4 b.mp4", "compilation.mp4", "random", "1", ["order"], "shuffle", GO)
        self.assertEqual(calls[0], ["stitch", "a.mp4", "b.mp4", "-t", "random", "--shuffle"])

    def test_the_size_questions_can_be_left_empty(self):
        calls = self.stitch("a.mp4 b.mp4", "compilation.mp4", "fade", "1", ["size"], "", "", GO)
        self.assertEqual(calls[0], ["stitch", "a.mp4", "b.mp4"])

    def test_a_dry_run_adds_the_flag_and_going_back_runs_nothing(self):
        calls = self.stitch("a.mp4 b.mp4", "compilation.mp4", "fade", "1", [], DRY)
        self.assertEqual(calls[0][-1], "--dry-run")
        self.calls.clear()
        self.assertEqual(self.stitch("a.mp4 b.mp4", "compilation.mp4", "fade", "1", [], BACK), [])

    def test_wrong_answers_are_refused_with_a_reason_and_asked_again(self):
        self.stitch('"a.mp4 b.mp4', "a.mp4 b.mp4", "result.txt", "result.mp4", "other", "two words", "dissolve", "0", "2",
                    ["size"], "huge", "1080p", "fast", "25", GO)
        reasons = [why for _, _, why in self.ask.refused]
        self.assertEqual(reasons[0], "A quote is not closed.")
        self.assertIn("ending in .mp4", reasons[1])
        self.assertEqual(reasons[2], "Type one name, with no spaces.")
        self.assertEqual(reasons[3], "Type a number above 0.")
        self.assertIn("1920x1080 or 1080p", reasons[4])
        self.assertEqual(reasons[5], "Type a number above 0.")
        self.assertEqual(self.calls[0], ["stitch", "a.mp4", "b.mp4", "-o", "result.mp4", "-t", "dissolve", "-d", "2",
                                         "--resolution", "1080p", "--fps", "25"])

    def test_the_command_is_shown_and_the_menu_reaches_it(self):
        self.menu("stitch", "a.mp4 b.mp4", "compilation.mp4", "fade", "1", [], BACK, "quit").start()
        self.assertIn("Command line for this:  ytt stitch a.mp4 b.mp4", self.shown.getvalue())


class DoctorFlow(MenuTest):
    def test_doctor_runs_with_no_questions(self):
        self.menu().flow_doctor()
        self.assertEqual(self.only_call(), ["doctor"])
        self.assertEqual(self.ask.asked, [])

    def test_it_is_reached_from_the_main_menu(self):
        self.menu("doctor", "quit").start()
        self.assertEqual(self.calls, [["doctor"]])


class StingerFlow(MenuTest):
    def stingers(self, *answers):
        self.menu("stingers", *answers, BACK).flow_style()
        return self.calls

    def test_the_usual_choices_add_no_flags(self):
        calls = self.stingers("stingers", "1080x1920", "all", "CRUNCH!", GO)
        self.assertEqual(calls, [["style", "stingers"]])
        self.assertTrue(self.parses(calls[0]).outdir == "stingers")

    def test_every_change(self):
        calls = self.stingers("mine", "1920x1080", "some", ["burst", "bite"], "POW", GO)
        argv = calls[0]
        self.assertEqual(argv, ["style", "stingers", "-o", "mine", "--size", "1920x1080", "--only", "burst,bite", "--text", "POW"])
        args = self.parses(argv)
        self.assertEqual((args.outdir, args.size, args.only, args.text), ("mine", "1920x1080", "burst,bite", "POW"))

    def test_another_size_is_typed_and_checked(self):
        calls = self.stingers("stingers", "other", "big", "720x1280", "all", "CRUNCH!", GO)
        self.assertEqual(calls[0], ["style", "stingers", "--size", "720x1280"])
        self.assertEqual(self.ask.refused[0][2], "Type a size like 1080x1920.")

    def test_no_word_at_all_is_an_empty_text_flag(self):
        calls = self.stingers("stingers", "1080x1920", "all", "", GO)
        self.assertEqual(calls[0], ["style", "stingers", "--text", ""])
        self.assertEqual(self.parses(calls[0]).text, "")

    def test_the_choices_of_stingers_are_the_ones_the_tool_can_make(self):
        self.stingers("stingers", "1080x1920", "some", ["slats"], "CRUNCH!", BACK)
        shown = self.ask.everything_shown()
        for name in ("crimp_wipe", "slats", "halftone", "burst", "bite"):
            self.assertIn(name + ":", shown)

    def test_a_dry_run_and_going_back(self):
        calls = self.stingers("stingers", "1080x1920", "all", "CRUNCH!", DRY)
        self.assertEqual(calls[0], ["style", "stingers", "--dry-run"])
        self.calls.clear()
        self.assertEqual(self.stingers("stingers", "1080x1920", "all", "CRUNCH!", BACK), [])

    def test_the_other_style_actions_are_still_there(self):
        self.menu("list", BACK).flow_style()
        self.assertEqual(self.only_call(), ["style", "list"])


if __name__ == "__main__":
    unittest.main()
