"""`ytt stitch` through main(), the way the real command runs it. Needs no workspace."""
import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ytt.ui import cli

try:
    from test_stitch_videos import HAVE_TOOLS, _durations, _make_clip
except ImportError:
    from tests.test_stitch_videos import HAVE_TOOLS, _durations, _make_clip


@unittest.skipUnless(HAVE_TOOLS, "needs ffmpeg")
class StitchCliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = Path(tempfile.mkdtemp(prefix="stitch_cli_base_"))
        _make_clip(cls.base / "base.mp4")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="stitch_cli_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "no-workspace-here"
        self.out = self.tmp / "out.mp4"
        self.files = []
        for n in "abc":
            shutil.copy(self.base / "base.mp4", self.tmp / f"{n}.mp4")
            self.files.append(str(self.tmp / f"{n}.mp4"))

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--workspace", str(self.root), "stitch", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_it_joins_the_videos_and_says_where_the_result_is(self):
        code, out, err = self.run_cli(*self.files, "-o", str(self.out), "-t", "fade", "-d", "0.5")
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"Done: {self.out}", out)
        self.assertIn("join 3 videos", out)
        self.assertRegex(out, r"rendering: 9\d%")                 # it reported how far it got, up to nearly the end
        self.assertAlmostEqual(_durations(self.out)["video"], 3 * 1.8 + 2 * 0.5, delta=0.15)
        self.assertEqual(sorted(p.name for p in self.tmp.glob("out*")), ["out.mp4"])

    def test_it_never_touches_or_creates_a_workspace(self):
        self.run_cli(*self.files, "-o", str(self.out), "-t", "cut")
        self.assertFalse(self.root.exists())

    def test_a_folder_works_as_input(self):
        folder = self.tmp / "clips"
        folder.mkdir()
        for f in self.files:
            shutil.move(f, folder)
        code, out, _ = self.run_cli(str(folder), "-o", str(self.out), "-t", "cut")
        self.assertEqual(code, 0)
        self.assertIn("join 3 videos", out)

    def test_dry_run_shows_the_plan_and_the_command_and_makes_nothing(self):
        code, out, _ = self.run_cli(*self.files, "-o", str(self.out), "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("-- fade 1s --", out)
        self.assertIn("ffmpeg command", out)
        self.assertIn("-filter_complex", out)
        self.assertIn("(dry run: nothing was made)", out)
        self.assertEqual(list(self.tmp.glob("out*")), [])

    def test_the_options_reach_the_engine(self):
        code, out, _ = self.run_cli(*self.files, "-o", str(self.out), "--order", "3,1", "--resolution", "128x72",
                                    "--fps", "20", "--no-audio", "-t", "cut", "--crf", "30", "--preset", "ultrafast")
        self.assertEqual(code, 0, out)
        self.assertIn("join 2 videos", out)
        self.assertIn("128x72, 20 fps", out)
        self.assertIn("the sound is left out", out)
        self.assertAlmostEqual(_durations(self.out)["video"], 2 * 1.8, delta=0.15)
        self.assertNotIn("audio", _durations(self.out))

    def test_overwrite_is_required_to_replace_a_file(self):
        self.out.write_bytes(b"old")
        code, out, err = self.run_cli(*self.files, "-o", str(self.out), "-t", "cut")
        self.assertEqual(code, 1)
        self.assertIn("Errors", out)
        self.assertIn("--overwrite", out)
        self.assertEqual(self.out.read_bytes(), b"old")
        code, out, _ = self.run_cli(*self.files, "-o", str(self.out), "-t", "cut", "--overwrite")
        self.assertEqual(code, 0)
        self.assertGreater(self.out.stat().st_size, 1000)

    def test_no_videos_given_is_a_plain_error_with_an_example(self):
        code, out, err = self.run_cli("-o", str(self.out))
        self.assertEqual(code, 1)
        self.assertIn("Error: no videos given", err)
        self.assertIn("ytt stitch a.mp4 b.mp4", err)

    def test_one_video_is_an_error_not_a_crash(self):
        code, out, _ = self.run_cli(self.files[0], "-o", str(self.out))
        self.assertEqual(code, 1)
        self.assertIn("at least two videos", out)

    def test_a_bad_choice_is_stopped_by_the_parser_before_anything_runs(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stopped:
            cli.main(["--workspace", str(self.root), "stitch", *self.files, "--sort", "colour", "-o", str(self.out)])
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("invalid choice", err.getvalue())
        self.assertEqual(list(self.tmp.glob("out*")), [])

    def test_ctrl_c_says_cancelled_and_exits_130(self):
        with mock.patch.object(cli.stitch_mod, "run_stitch", side_effect=KeyboardInterrupt):
            code, out, err = self.run_cli(*self.files, "-o", str(self.out))
        self.assertEqual(code, 130)
        self.assertIn("Cancelled", err)

    def test_list_transitions_needs_no_videos_and_no_workspace(self):
        code, out, _ = self.run_cli("--list-transitions")
        self.assertEqual(code, 0)
        self.assertIn("cut, random, fade", out)
        self.assertIn("@path/to/video.mp4", out)
        self.assertFalse(self.root.exists())

    def test_list_transitions_shows_the_videos_of_a_stinger_folder(self):
        folder = self.tmp / "stingers"
        folder.mkdir()
        shutil.copy(self.base / "base.mp4", folder / "burst.mp4")
        code, out, _ = self.run_cli("--list-transitions", "--stinger-dir", str(folder))
        self.assertEqual(code, 0)
        self.assertRegex(out, r"burst\s+1\.8s")

    def test_a_stinger_folder_is_used_as_a_transition(self):
        folder = self.tmp / "stingers"
        folder.mkdir()
        shutil.copy(self.base / "base.mp4", folder / "burst.mp4")
        code, out, err = self.run_cli(*self.files, "-o", str(self.out), "--stinger-dir", str(folder), "-t", "burst",
                                      "--key", "none", "--dry-run")
        self.assertEqual(code, 0, out + err)
        self.assertIn("-- burst (video:", out)


if __name__ == "__main__":
    unittest.main()
