"""Tests for the rendering engine (ytt/engine/stitch.py): the same guarantees as the old script, plus what a
library must add: errors instead of exits, atomic output, cancellation, and a plan that matches the render."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ytt.engine import stitch
from ytt.engine.stitch import EngineError, RenderSpec

try:
    from test_stitch_videos import HAVE_TOOLS, TAIL, HEAD, BODY, _colour_frames, _durations, _make_clip
except ImportError:
    from tests.test_stitch_videos import HAVE_TOOLS, TAIL, HEAD, BODY, _colour_frames, _durations, _make_clip


@unittest.skipUnless(HAVE_TOOLS, "needs ffmpeg")
class EngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="engine_test_"))
        cls.clips = []
        for n in "abcd":
            _make_clip(cls.tmp / f"{n}.mp4")
            cls.clips.append(cls.tmp / f"{n}.mp4")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def spec(self, name, **kw):
        return RenderSpec(files=list(self.clips), output=self.tmp / f"{name}.mp4", **kw)

    def assert_nothing_hidden(self, out, n=4):
        c = _colour_frames(out)
        self.assertGreaterEqual(c["R"], n * TAIL, c)
        self.assertGreaterEqual(c["G"], n * HEAD, c)
        self.assertEqual(c["B"], n * BODY, c)


class RenderingTests(EngineTest):
    def test_every_clip_plays_in_full_and_the_transition_adds_its_own_time(self):
        s = self.spec("fade", transition="fade", transition_seconds=0.6)
        p = stitch.render(s)
        self.assert_nothing_hidden(s.output)
        d = _durations(s.output)
        self.assertAlmostEqual(d["video"], 4 * 1.8 + 3 * 0.6, delta=0.1)
        self.assertAlmostEqual(d["audio"], d["video"], delta=0.1)
        self.assertAlmostEqual(p.total_seconds, d["video"], delta=0.1)          # the plan's estimate was right

    def test_other_styles_and_a_hard_cut(self):
        for style in ("wipeleft", "slideup", "circleopen", "dissolve", "cut"):
            with self.subTest(style=style):
                s = self.spec(style, transition=style, transition_seconds=0.8)
                stitch.render(s)
                self.assert_nothing_hidden(s.output)

    def test_a_hard_cut_adds_nothing(self):
        s = self.spec("hardcut", transition="cut")
        stitch.render(s)
        self.assertAlmostEqual(_durations(s.output)["video"], 4 * 1.8, delta=0.1)
        self.assert_nothing_hidden(s.output)

    def test_random_transitions_also_show_every_clip_in_full(self):
        s = self.spec("randomrender", transition="random", transition_seconds=0.6, seed=3)
        stitch.render(s)
        self.assert_nothing_hidden(s.output)

    def test_a_transition_longer_than_the_clips_is_not_shortened(self):
        s = self.spec("longrender", transition="fade", transition_seconds=3)
        stitch.render(s)
        self.assertAlmostEqual(_durations(s.output)["video"], 4 * 1.8 + 3 * 3, delta=0.15)
        self.assert_nothing_hidden(s.output)

    def test_ntsc_frame_rates_work_over_many_junctions(self):
        clips = []
        for n in "pqrstu":
            _make_clip(self.tmp / f"{n}.mp4", rate="30000/1001")
            clips.append(self.tmp / f"{n}.mp4")
        s = RenderSpec(files=clips, output=self.tmp / "ntsc.mp4", transition="fade", transition_seconds=0.7)
        stitch.render(s)
        c = _colour_frames(s.output)
        self.assertGreaterEqual(c["R"], 6 * 12)           # 0.4s at 29.97 fps is 12 frames per clip tail
        self.assertGreaterEqual(c["G"], 6 * 12)
        self.assertAlmostEqual(_durations(s.output)["video"], 6 * 1.8 + 5 * 0.7, delta=0.25)

    def test_overlap_mode_is_the_old_shorter_behaviour(self):
        s = self.spec("overlap", transition="fade", transition_seconds=0.5, overlap=True)
        stitch.render(s)
        self.assertAlmostEqual(_durations(s.output)["video"], 4 * 1.8 - 3 * 0.5, delta=0.1)
        self.assertLess(_colour_frames(s.output)["R"], 4 * TAIL)

    def test_random_transitions_are_repeatable_with_a_seed_and_the_plan_names_them(self):
        a = stitch.plan(self.spec("r1", transition="random", seed=7))
        b = stitch.plan(self.spec("r2", transition="random", seed=7))
        c = stitch.plan(self.spec("r3", transition="random", seed=8))
        self.assertEqual(a.junctions, b.junctions)
        self.assertNotEqual(a.junctions, c.junctions)
        self.assertTrue(all(name in stitch.RANDOM_POOL for name, _ in a.junctions))

    def test_resolution_fps_and_audio_options(self):
        s = self.spec("opts", transition="cut", resolution=(64, 64), fps="10", audio=False)
        stitch.render(s)
        self.assertNotIn("audio", _durations(s.output))
        p = stitch.plan(s)
        self.assertEqual((p.width, p.height, p.fps), (64, 64, "10"))

    def test_progress_is_reported_and_reaches_the_end(self):
        seen = []
        stitch.render(self.spec("prog", transition="cut"), on_progress=lambda done, total: seen.append((done, total)))
        self.assertTrue(seen)
        self.assertAlmostEqual(seen[-1][0], seen[-1][1], delta=0.5)
        self.assertEqual(sorted(x[0] for x in seen), [x[0] for x in seen])


class PlanTests(EngineTest):
    def test_plan_writes_nothing_and_describes_the_work(self):
        s = self.spec("planonly", transition="fade", transition_seconds=1.0)
        p = stitch.plan(s)
        self.assertEqual(len(p.clips), 4)
        self.assertEqual(p.junctions, [("fade", 1.0)] * 3)
        self.assertFalse(s.output.exists())
        self.assertFalse(p.part_path.exists())
        self.assertEqual(p.command[0], "ffmpeg")
        self.assertEqual(p.command[-1], str(p.part_path))

    def test_nothing_is_cut_so_a_transition_longer_than_the_clips_is_kept_in_hold_mode(self):
        p = stitch.plan(self.spec("long", transition="fade", transition_seconds=5.0))
        self.assertEqual(p.junctions, [("fade", 5.0)] * 3)
        self.assertEqual(p.notes, [])

    def test_overlap_mode_shortens_and_says_so(self):
        p = stitch.plan(self.spec("longo", transition="fade", transition_seconds=5.0, overlap=True))
        self.assertTrue(all(d < 5.0 for _, d in p.junctions))
        self.assertEqual(len(p.notes), 3)


class FailureTests(EngineTest):
    def test_unknown_transition_is_an_engine_error_naming_it(self):
        with self.assertRaises(EngineError) as cm:
            stitch.plan(self.spec("bad", transition="spin"))
        self.assertIn("spin", str(cm.exception))

    def test_stinger_without_a_folder_says_what_is_missing(self):
        with self.assertRaises(EngineError) as cm:
            stitch.plan(self.spec("bad", transition="stinger"))
        self.assertIn("folder of transition videos", str(cm.exception))

    def test_missing_input_unreadable_input_and_too_few_inputs(self):
        with self.assertRaises(EngineError) as cm:
            stitch.plan(RenderSpec(files=[self.clips[0], self.tmp / "ghost.mp4"], output=self.tmp / "x.mp4"))
        self.assertIn("ghost.mp4", str(cm.exception))
        junk = self.tmp / "junk.mp4"
        junk.write_bytes(b"this is not a video at all")
        with self.assertRaises(EngineError) as cm:
            stitch.plan(RenderSpec(files=[self.clips[0], junk], output=self.tmp / "x.mp4"))
        self.assertIn("junk.mp4", str(cm.exception))
        with self.assertRaises(EngineError):
            stitch.plan(RenderSpec(files=[self.clips[0]], output=self.tmp / "x.mp4"))

    def test_a_failed_render_leaves_no_output_and_no_part_file_and_keeps_the_old_output(self):
        out = self.tmp / "keep.mp4"
        out.write_bytes(b"previous good render")
        s = RenderSpec(files=list(self.clips), output=out, transition="cut", preset="no-such-preset")
        with self.assertRaises(EngineError) as cm:
            stitch.render(s)
        self.assertIn("ffmpeg failed", str(cm.exception))
        self.assertEqual(out.read_bytes(), b"previous good render")
        self.assertFalse(stitch.part_path_for(out).exists())

    def test_cancelling_midway_stops_ffmpeg_removes_the_part_file_and_leaves_the_old_output(self):
        out = self.tmp / "cancel.mp4"
        out.write_bytes(b"previous good render")

        def cancel(done, total):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            stitch.render(RenderSpec(files=list(self.clips), output=out, transition="cut"), on_progress=cancel)
        self.assertEqual(out.read_bytes(), b"previous good render")
        self.assertFalse(stitch.part_path_for(out).exists())

    def test_missing_ffmpeg_is_a_clear_error(self):
        real = shutil.which
        shutil.which = lambda name, *a, **k: None if name in ("ffmpeg", "ffprobe") else real(name, *a, **k)
        try:
            with self.assertRaises(EngineError) as cm:
                stitch.plan(self.spec("nope"))
        finally:
            shutil.which = real
        self.assertIn("ffmpeg", str(cm.exception))

    def test_stinger_state_does_not_leak_between_renders(self):
        st = self.tmp / "stingers"
        st.mkdir(exist_ok=True)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=0x00ff00:s=96x96:r=25:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(st / "pop.mp4")],
                       check=True)
        p = stitch.plan(self.spec("withst", transition="stinger", stinger_dir=str(st)))
        self.assertEqual([n for n, _ in p.junctions], ["pop"] * 3)
        with self.assertRaises(EngineError):                      # a later render without the folder must not see it
            stitch.plan(self.spec("without", transition="pop"))


class TransitionVideoLookTests(EngineTest):
    """How a transition video (a "stinger") is shown: which background is removed, how, and whether it brings its own
    sound. The graph is what ffmpeg is told; the renders prove it really runs for every choice."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.stingers = cls.tmp / "stingers"
        cls.stingers.mkdir()
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y",              # a green screen with a red box, and a beep
                        "-f", "lavfi", "-i", "color=c=0x00ff00:s=96x96:r=25:d=1,drawbox=x=30:y=30:w=36:h=36:color=red:t=fill",
                        "-f", "lavfi", "-i", "sine=f=880:d=1", "-shortest",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(cls.stingers / "pop.mp4")], check=True)

    def look(self, name, **kw):
        return self.spec(name, transition="stinger", stinger_dir=str(self.stingers), transition_seconds=0.5, **kw)

    def graph(self, name, **kw):
        return stitch.plan(self.look(name, **kw)).graph

    def test_a_named_key_colour_is_removed_with_the_chosen_closeness_and_softness(self):
        g = self.graph("g1", stinger_key="green", stinger_sim=0.3, stinger_blend=0.1)
        self.assertIn("chromakey=color=0x00FF00:similarity=0.3:blend=0.1", g)

    def test_the_defaults_are_the_old_behaviour(self):
        g = self.graph("g2", stinger_key="green")
        self.assertIn("chromakey=color=0x00FF00:similarity=0.12:blend=0.05", g)
        self.assertNotIn("despill", g)

    def test_auto_finds_the_green_background_by_looking_at_the_first_frame(self):
        g = self.graph("g3", stinger_key="auto")                    # the colour as it really is in the file, about 00FF00
        self.assertRegex(g, r"chromakey=color=0x00[EF][0-9A-F]00:similarity=0\.12")

    def test_none_removes_nothing(self):
        g = self.graph("g4", stinger_key="none")
        self.assertNotIn("chromakey", g)
        self.assertNotIn("colorkey", g)

    def test_black_and_white_use_the_colour_key_and_a_typed_colour_is_accepted(self):
        self.assertIn("colorkey=color=0x000000", self.graph("g5", stinger_key="black"))
        self.assertIn("colorkey=color=0xFFFFFF", self.graph("g6", stinger_key="white"))
        self.assertIn("chromakey=color=0x1122FF", self.graph("g7", stinger_key="1122ff"))

    def test_despill_is_only_added_when_asked_for(self):
        if not stitch.have_filter("despill"):
            self.skipTest("this ffmpeg has no despill filter")
        self.assertIn("despill=type=green", self.graph("g8", stinger_key="green", stinger_despill=True))
        self.assertNotIn("despill", self.graph("g9", stinger_key="green", stinger_despill=False))

    def test_its_own_sound_is_mixed_in_unless_switched_off(self):
        self.assertIn("alimiter", self.graph("g10", stinger_key="green", stinger_audio=True))
        self.assertNotIn("alimiter", self.graph("g11", stinger_key="green", stinger_audio=False))

    def test_a_bad_key_colour_is_an_engine_error_naming_it(self):
        with self.assertRaises(EngineError) as cm:
            stitch.plan(self.look("bad", stinger_key="purple"))
        self.assertIn("purple", str(cm.exception))

    def test_a_real_render_works_under_each_choice_and_matches_its_plan(self):
        for key in ("green", "none", "auto"):
            for sound in (True, False):
                with self.subTest(key=key, sound=sound):
                    s = self.look(f"real_{key}_{sound}", stinger_key=key, stinger_audio=sound)
                    p = stitch.render(s)
                    self.assertTrue(s.output.is_file())
                    self.assertFalse(stitch.part_path_for(s.output).exists())
                    self.assertAlmostEqual(_durations(s.output)["video"], p.total_seconds, delta=0.2)

    def test_removing_the_green_lets_the_clip_ends_show_through_and_keeping_it_covers_them(self):
        keyed = self.look("shows", stinger_key="green")
        stitch.render(keyed)
        covered = self.look("covers", stinger_key="none")
        stitch.render(covered)
        shown, hidden = _colour_frames(keyed.output), _colour_frames(covered.output)
        self.assertGreater(shown["R"], hidden["R"])           # the red clip tails are visible only when the green is gone
        self.assertLess(shown["G"], hidden["G"])              # an opaque green screen adds green frames of its own
        self.assertEqual(shown["B"], hidden["B"])             # the middle of every clip is never touched


if __name__ == "__main__":
    unittest.main()
