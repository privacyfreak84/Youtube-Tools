"""Tests for the rendering engine (ytt/engine/stitch.py): the same guarantees as the old script, plus what a
library must add: errors instead of exits, atomic output, cancellation, and a plan that matches the render."""
import shutil
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
        for style in ("wipeleft", "circleopen", "cut"):
            with self.subTest(style=style):
                s = self.spec(style, transition=style, transition_seconds=0.8)
                stitch.render(s)
                self.assert_nothing_hidden(s.output)

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
        import subprocess
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=0x00ff00:s=96x96:r=25:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(st / "pop.mp4")],
                       check=True)
        p = stitch.plan(self.spec("withst", transition="stinger", stinger_dir=str(st)))
        self.assertEqual([n for n, _ in p.junctions], ["pop"] * 3)
        with self.assertRaises(EngineError):                      # a later render without the folder must not see it
            stitch.plan(self.spec("without", transition="pop"))


if __name__ == "__main__":
    unittest.main()
