"""
Offline tests for stitch_videos.py: a transition must never hide any part of a clip.

Each test clip is  0.4s pure green (its head) + 1s blue + 0.4s pure red (its tail)  at 25 fps. After stitching, every
clip's head and tail frames must still be on screen in full, so the number of pure-green / pure-red frames in the
result must not fall below what the clips contain.

Run from the repo folder:   python -m unittest discover -s tests -v
Needs: ffmpeg + ffprobe on PATH.
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STITCH = REPO / "stitch_videos.py"
HAVE_TOOLS = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
FPS = 25
HEAD = TAIL = 10            # frames of pure green / pure red at each end of a clip
BODY = 25                   # frames of blue in the middle


def _make_clip(path, rate="25"):
    colour = lambda c, d: ["-f", "lavfi", "-i", f"color=c={c}:s=96x96:r={rate}:d={d}"]
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *colour("0x00ff00", 0.4), *colour("0x0000ff", 1.0),
                    *colour("0xff0000", 0.4), "-f", "lavfi", "-i", "sine=f=440:d=1.8",
                    "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]", "-map", "3:a",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)


def _colour_frames(path):
    """How many frames are pure green / blue / red (each frame averaged down to one pixel)."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", "scale=1:1:flags=area",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    n = {"R": 0, "G": 0, "B": 0}
    for i in range(0, len(raw), 3):
        r, g, b = raw[i:i + 3]
        if r > 200 and g < 60 and b < 60:
            n["R"] += 1
        elif g > 200 and r < 60 and b < 60:
            n["G"] += 1
        elif b > 200 and r < 60 and g < 60:
            n["B"] += 1
    return n


def _durations(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, check=True).stdout.split()
    return {line.split(",")[0]: float(line.split(",")[1]) for line in out}


@unittest.skipUnless(HAVE_TOOLS, "needs ffmpeg")
class TransitionsKeepEveryClipInFullTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="stitch_test_"))
        cls.clips = []
        for n in "abcd":
            _make_clip(cls.tmp / f"{n}.mp4")
            cls.clips.append(str(cls.tmp / f"{n}.mp4"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def stitch(self, name, *args, clips=None):
        out = self.tmp / f"{name}.mp4"
        r = subprocess.run([sys.executable, str(STITCH), *(clips or self.clips), "-o", str(out), "--overwrite", *args],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return out

    def assert_nothing_hidden(self, out, n_clips):
        c = _colour_frames(out)
        self.assertGreaterEqual(c["R"], n_clips * TAIL, f"a clip's ending is hidden: {c}")
        self.assertGreaterEqual(c["G"], n_clips * HEAD, f"a clip's start is hidden: {c}")
        self.assertEqual(c["B"], n_clips * BODY, f"a clip's middle was damaged: {c}")

    def test_fade_shows_every_clip_in_full(self):
        self.assert_nothing_hidden(self.stitch("fade", "-t", "fade", "-d", "1"), 4)

    def test_other_transition_styles_too(self):
        for style in ("wipeleft", "slideup", "circleopen", "dissolve"):
            with self.subTest(style=style):
                self.assert_nothing_hidden(self.stitch(style, "-t", style, "-d", "0.8"), 4)

    def test_random_transitions_too(self):
        self.assert_nothing_hidden(self.stitch("random", "-t", "random", "-d", "0.6"), 4)

    def test_the_transition_gets_its_own_time_so_the_result_is_longer_by_one_duration_per_junction(self):
        out = self.stitch("len", "-t", "fade", "-d", "0.6")
        d = _durations(out)
        expected = 4 * 1.8 + 3 * 0.6
        self.assertAlmostEqual(d["video"], expected, delta=0.1)
        self.assertAlmostEqual(d["audio"], expected, delta=0.1)       # picture and sound stay in step

    def test_a_hard_cut_adds_nothing(self):
        out = self.stitch("cut", "-t", "cut")
        self.assertAlmostEqual(_durations(out)["video"], 4 * 1.8, delta=0.1)
        self.assert_nothing_hidden(out, 4)

    def test_a_transition_longer_than_the_clips_is_not_shortened(self):
        out = self.stitch("long", "-t", "fade", "-d", "3")
        self.assertAlmostEqual(_durations(out)["video"], 4 * 1.8 + 3 * 3, delta=0.15)
        self.assert_nothing_hidden(out, 4)

    def test_works_with_ntsc_frame_rates_over_many_junctions(self):
        clips = []
        for n in "pqrstu":
            _make_clip(self.tmp / f"{n}.mp4", rate="30000/1001")
            clips.append(str(self.tmp / f"{n}.mp4"))
        out = self.stitch("ntsc", "-t", "fade", "-d", "0.7", clips=clips)
        c = _colour_frames(out)
        self.assertGreaterEqual(c["R"], 6 * 12)           # 0.4s at 29.97fps is 12 frames per clip tail
        self.assertGreaterEqual(c["G"], 6 * 12)
        self.assertAlmostEqual(_durations(out)["video"], 6 * 1.8 + 5 * 0.7, delta=0.25)

    def test_overlap_flag_keeps_the_old_shorter_behaviour(self):
        out = self.stitch("overlap", "-t", "fade", "-d", "0.5", "--overlap")
        self.assertAlmostEqual(_durations(out)["video"], 4 * 1.8 - 3 * 0.5, delta=0.1)
        self.assertLess(_colour_frames(out)["R"], 4 * TAIL)     # the old way blends the endings away


if __name__ == "__main__":
    unittest.main()
