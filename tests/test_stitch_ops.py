"""ytt.ops.compile.stitch: the request -> plan -> run behind `ytt stitch`. Real tiny clips, real ffmpeg."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ytt.engine import stitch as engine
from ytt.engine.stitch import EngineError
from ytt.ops.compile import stitch as st
from ytt.ops.errors import OpError

try:
    from test_stitch_videos import HAVE_TOOLS, _durations, _make_clip
except ImportError:
    from tests.test_stitch_videos import HAVE_TOOLS, _durations, _make_clip


def make_timed_clip(path, seconds):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=c=0x2060a0:s=96x96:r=25:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=f=440:d={seconds}", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(path)], check=True)


@unittest.skipUnless(HAVE_TOOLS, "needs ffmpeg")
class StitchOpsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = Path(tempfile.mkdtemp(prefix="stitch_ops_base_"))
        _make_clip(cls.base / "base.mp4")                       # 1.8 s, 96x96
        make_timed_clip(cls.base / "short.mp4", 0.6)
        make_timed_clip(cls.base / "long.mp4", 1.4)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="stitch_ops_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out = self.tmp / "out.mp4"

    def clip(self, name, source="base.mp4"):
        p = self.tmp / name
        shutil.copy(self.base / source, p)
        return str(p)

    def request(self, inputs, **kw):
        return st.StitchRequest(inputs=inputs, output=str(self.out), **kw)

    def names(self, sp):
        return [c.name for c in sp.render_plan.clips]

    def errors(self, request):
        sp = st.plan_stitch(request)
        self.assertFalse(sp.plan.ok, "expected the plan to have errors")
        self.assertIsNone(sp.render_plan)
        self.assertFalse(self.out.exists())
        return sp.plan.errors


class PlanTests(StitchOpsTest):
    def test_the_plan_lists_the_videos_and_the_transition_between_each_pair_and_writes_nothing(self):
        a, b, c = self.clip("a.mp4"), self.clip("b.mp4"), self.clip("c.mp4")
        sp = st.plan_stitch(self.request([a, b, c], transition="wipeleft", duration=0.5))
        self.assertTrue(sp.plan.ok)
        self.assertEqual(self.names(sp), ["a.mp4", "b.mp4", "c.mp4"])
        action = sp.plan.actions[0]
        self.assertIn("join 3 videos", action.text)
        self.assertIn("96x96, 25 fps", action.text)
        lines = action.data["lines"]
        self.assertEqual(lines[0].strip(), "1. a.mp4  (1.8s)")
        self.assertIn("-- wipeleft 0.5s --", lines[1])
        self.assertEqual(len(lines), 5)
        self.assertEqual(list(self.tmp.glob("out*")), [])                    # nothing written, no .part either
        self.assertIn("ffmpeg", sp.command_text())

    def test_a_hard_cut_is_shown_without_seconds(self):
        sp = st.plan_stitch(self.request([self.clip("a.mp4"), self.clip("b.mp4")], transition="cut"))
        self.assertIn("-- cut --", sp.plan.actions[0].data["lines"][1])

    def test_a_folder_and_a_wildcard_are_expanded_and_sorted_the_way_a_person_counts(self):
        for n in ("clip10.mp4", "clip2.mp4", "clip1.mp4"):
            self.clip(n)
        folder = st.plan_stitch(self.request([str(self.tmp)]))
        self.assertEqual(self.names(folder), ["clip1.mp4", "clip2.mp4", "clip10.mp4"])
        wild = st.plan_stitch(self.request([str(self.tmp / "clip*.mp4")]))
        self.assertEqual(self.names(wild), ["clip1.mp4", "clip2.mp4", "clip10.mp4"])

    def test_the_output_size_follows_the_first_video_unless_told_otherwise(self):
        files = [self.clip("a.mp4"), self.clip("b.mp4")]
        self.assertIn("96x96", st.plan_stitch(self.request(files)).plan.actions[0].text)
        sp = st.plan_stitch(self.request(files, resolution="160x90", fps="30"))
        self.assertIn("160x90, 30 fps", sp.plan.actions[0].text)
        sp = st.plan_stitch(self.request(files, audio=False))
        self.assertIn("the sound is left out", sp.plan.notes)
        self.assertFalse(sp.render_plan.with_audio)

    def test_an_existing_output_is_only_replaced_with_overwrite_and_the_plan_says_so(self):
        files = [self.clip("a.mp4"), self.clip("b.mp4")]
        self.out.write_bytes(b"old")
        refused = st.plan_stitch(self.request(files))
        self.assertFalse(refused.plan.ok)
        self.assertIn("already exists (add --overwrite to replace it)", refused.plan.errors[0])
        sp = st.plan_stitch(self.request(files, overwrite=True))
        self.assertTrue(sp.plan.ok)
        self.assertIn(f"{self.out} will be replaced", sp.plan.notes)
        self.assertEqual(self.out.read_bytes(), b"old")                     # planning replaced nothing


class OrderingTests(StitchOpsTest):
    def test_order_picks_repeats_and_drops(self):
        files = [self.clip("a.mp4"), self.clip("b.mp4"), self.clip("c.mp4")]
        self.assertEqual(self.names(st.plan_stitch(self.request(files, order="3,1,2"))), ["c.mp4", "a.mp4", "b.mp4"])
        self.assertEqual(self.names(st.plan_stitch(self.request(files, order="2 2 1"))), ["b.mp4", "b.mp4", "a.mp4"])
        self.assertEqual(self.names(st.plan_stitch(self.request(files, order="3,1"))), ["c.mp4", "a.mp4"])

    def test_a_bad_order_is_an_error_that_says_what_to_type(self):
        files = [self.clip("a.mp4"), self.clip("b.mp4")]
        self.assertIn("positions like 3,1,2", self.errors(self.request(files, order="one,two"))[0])
        self.assertIn("between 1 and 2", self.errors(self.request(files, order="1,3"))[0])
        self.assertIn("between 1 and 2", self.errors(self.request(files, order="0,1"))[0])
        self.assertIn("at least two videos after ordering", self.errors(self.request(files, order="2"))[0])
        self.assertIn("--order is empty", self.errors(self.request(files, order=" , "))[0])

    def test_sort_by_duration_name_and_size(self):
        long_, short = self.clip("a_long.mp4", "long.mp4"), self.clip("b_short.mp4", "short.mp4")
        mid = self.clip("c_mid.mp4")                                          # 1.8 s: the longest of the three
        files = [mid, long_, short]
        self.assertEqual(self.names(st.plan_stitch(self.request(files, sort="duration"))),
                         ["b_short.mp4", "a_long.mp4", "c_mid.mp4"])
        self.assertEqual(self.names(st.plan_stitch(self.request(files, sort="name"))),
                         ["a_long.mp4", "b_short.mp4", "c_mid.mp4"])
        by_size = sorted(files, key=lambda f: Path(f).stat().st_size)
        self.assertEqual(self.names(st.plan_stitch(self.request(files, sort="size"))), [Path(f).name for f in by_size])

    def test_shuffle_with_a_seed_is_repeatable_and_keeps_every_video(self):
        files = [self.clip(f"{n}.mp4") for n in "abcdef"]
        first = self.names(st.plan_stitch(self.request(files, shuffle=True, seed=7)))
        again = self.names(st.plan_stitch(self.request(files, shuffle=True, seed=7)))
        self.assertEqual(first, again)
        self.assertEqual(sorted(first), sorted(f"{n}.mp4" for n in "abcdef"))
        others = {tuple(self.names(st.plan_stitch(self.request(files, shuffle=True, seed=s)))) for s in range(8)}
        self.assertGreater(len(others), 1)                                    # it really mixes


class ErrorTests(StitchOpsTest):
    def test_too_few_videos_missing_files_and_unreadable_files_are_errors_naming_the_problem(self):
        a = self.clip("a.mp4")
        self.assertIn("at least two videos", self.errors(self.request([a]))[0])
        self.assertIn("at least two videos", self.errors(self.request([str(self.tmp / "none*.mp4")]))[0])
        self.assertIn("not found", self.errors(self.request([a, str(self.tmp / "missing.mp4")]))[0])
        bad = self.tmp / "bad.mp4"
        bad.write_text("not a video")
        self.assertIn("bad.mp4", self.errors(self.request([a, str(bad)]))[0])

    def test_the_output_may_not_be_one_of_the_videos_even_by_another_route(self):
        a, b = self.clip("a.mp4"), self.clip("b.mp4")
        for out in (a, str(Path(self.tmp) / "sub" / ".." / "a.mp4")):
            sp = st.plan_stitch(st.StitchRequest(inputs=[a, b], output=out, overwrite=True))
            self.assertFalse(sp.plan.ok)
            self.assertIn("is one of the videos being joined", sp.plan.errors[0])
        self.assertTrue(Path(a).exists())

    def test_the_output_needs_a_video_extension(self):
        files = [self.clip("a.mp4"), self.clip("b.mp4")]
        for name in (str(self.tmp / "result"), str(self.tmp / "result.txt"), str(self.tmp / "result.webm")):
            sp = st.plan_stitch(st.StitchRequest(inputs=files, output=name))
            self.assertIn("needs an extension", sp.plan.errors[0])
        self.assertTrue(st.plan_stitch(st.StitchRequest(inputs=files, output=str(self.tmp / "r.MKV"))).plan.ok)

    def test_each_bad_number_or_choice_says_which_flag(self):
        files = [self.clip("a.mp4"), self.clip("b.mp4")]
        cases = [({"duration": -1}, "--duration"), ({"crf": 52}, "--crf"), ({"crf": -1}, "--crf"),
                 ({"preset": "turbo"}, "--preset"), ({"fit": "stretch"}, "--fit"), ({"sort": "colour"}, "--sort"),
                 ({"fps": "fast"}, "--fps"), ({"fps": "30,5"}, "--fps")]
        for kw, flag in cases:
            with self.subTest(kw=kw):
                self.assertIn(flag, self.errors(self.request(files, **kw))[0])
        self.assertIn("--resolution", self.errors(self.request(files, resolution="huge"))[0])

    def test_all_the_problems_in_a_request_are_listed_not_just_the_first(self):
        files = [self.clip("a.mp4"), self.clip("b.mp4")]
        errors = self.errors(self.request(files, crf=99, preset="turbo"))
        self.assertEqual(len(errors), 2)

    def test_an_unknown_transition_and_a_bad_custom_entry_name_themselves(self):
        files = [self.clip("a.mp4"), self.clip("b.mp4"), self.clip("c.mp4")]
        self.assertIn("unknown transition 'sparkle'", self.errors(self.request(files, transition="sparkle"))[0])
        self.assertIn("stinger", self.errors(self.request(files, transition="stinger"))[0])
        self.assertIn("junction 5", self.errors(self.request(files, custom="5=cut"))[0])
        self.assertIn("bad --custom entry", self.errors(self.request(files, custom="cut"))[0])

    def test_missing_ffmpeg_is_reported_as_a_plan_error(self):
        files = [self.clip("a.mp4"), self.clip("b.mp4")]
        with mock.patch.object(engine.shutil, "which", return_value=None):
            sp = st.plan_stitch(self.request(files))
        self.assertIn("ffmpeg and ffprobe not found", sp.plan.errors[0])


class RunTests(StitchOpsTest):
    def test_run_makes_the_file_with_the_right_length_reports_progress_and_leaves_no_part_file(self):
        files = [self.clip("a.mp4"), self.clip("b.mp4"), self.clip("c.mp4")]
        sp = st.plan_stitch(self.request(files, transition="fade", duration=0.5))
        seen = []
        out = st.run_stitch(sp, on_progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(out, self.out)
        self.assertAlmostEqual(_durations(out)["video"], 3 * 1.8 + 2 * 0.5, delta=0.15)
        self.assertEqual(sorted(p.name for p in self.tmp.glob("out*")), ["out.mp4"])
        self.assertTrue(seen and seen[-1][0] > 0.9 * seen[-1][1])

    def test_no_audio_and_a_chosen_size_are_what_comes_out(self):
        files = [self.clip("a.mp4"), self.clip("b.mp4")]
        sp = st.plan_stitch(self.request(files, audio=False, resolution="128x72", transition="cut"))
        st.run_stitch(sp)
        probe = engine.probe(self.out)
        self.assertEqual((probe.width, probe.height, probe.has_audio), (128, 72, False))

    def test_overwrite_replaces_the_old_file_only_when_the_new_one_is_complete(self):
        files = [self.clip("a.mp4"), self.clip("b.mp4")]
        self.out.write_bytes(b"old")
        st.run_stitch(st.plan_stitch(self.request(files, overwrite=True, transition="cut")))
        self.assertGreater(self.out.stat().st_size, 1000)

    def test_a_plan_with_errors_cannot_be_run(self):
        sp = st.plan_stitch(self.request([self.clip("a.mp4")]))
        with self.assertRaises(OpError):
            st.run_stitch(sp)

    def test_an_engine_failure_becomes_an_op_error_with_its_message(self):
        sp = st.plan_stitch(self.request([self.clip("a.mp4"), self.clip("b.mp4")]))
        def failing(spec, on_progress=None, plan_=None):
            raise EngineError("ffmpeg failed: disk full")
        with self.assertRaisesRegex(OpError, "disk full"):
            st.run_stitch(sp, render=failing)

    def test_ctrl_c_is_passed_on_and_leaves_nothing_behind(self):
        files = [self.clip("a.mp4"), self.clip("b.mp4")]
        sp = st.plan_stitch(self.request(files))
        def interrupted(done, total):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            st.run_stitch(sp, on_progress=interrupted)
        self.assertEqual(list(self.tmp.glob("out*")), [])


class FpsTextTests(unittest.TestCase):
    def test_frame_rates_read_the_way_people_write_them(self):
        self.assertEqual(st._fps_text("25/1"), "25")
        self.assertEqual(st._fps_text("30000/1001"), "29.97")
        self.assertEqual(st._fps_text("29.97"), "29.97")
        self.assertEqual(st._fps_text("weird"), "weird")
        self.assertEqual(st._fps_text("5/0"), "5/0")


@unittest.skipUnless(HAVE_TOOLS, "needs ffmpeg")
class ListTransitionsTests(StitchOpsTest):
    def test_the_builtin_names_are_listed(self):
        found = st.list_transitions()
        self.assertEqual(found.builtin[:3], ["cut", "random", "fade"])
        self.assertIn("circleopen", found.builtin)
        self.assertEqual(found.videos, [])

    def test_a_folder_of_transition_videos_is_listed_and_forgotten_afterwards(self):
        folder = self.tmp / "stingers"
        folder.mkdir()
        shutil.copy(self.base / "short.mp4", folder / "burst.mp4")
        found = st.list_transitions(str(folder))
        self.assertEqual([v[0] for v in found.videos], ["burst"])
        self.assertEqual(engine.STINGERS, {})
        self.assertNotIn("burst", engine.CHOICES)
        self.assertNotIn("stinger", engine.CHOICES)

    def test_a_folder_that_is_not_one_is_an_op_error(self):
        with self.assertRaises(OpError):
            st.list_transitions(str(self.tmp / "nope"))


if __name__ == "__main__":
    unittest.main()
