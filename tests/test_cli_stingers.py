"""`ytt style stingers` through main(). It needs no workspace."""
import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ytt.ui import cli

try:
    from test_stingers import NEEDS
except ImportError:
    from tests.test_stingers import NEEDS


@NEEDS
class StingersCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytt_cli_stingers_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "no-workspace-here"
        self.out = self.tmp / "stingers"

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--workspace", str(self.root), "style", "stingers", *argv])
        return code, out.getvalue(), err.getvalue()

    def small(self, *extra):
        return ["-o", str(self.out), "--size", "96x64", "--fps", "10", "--duration", "0.6", "--smoothing", "1", *extra]

    def test_it_makes_the_files_and_prints_how_to_use_them(self):
        code, out, err = self.run_cli(*self.small("--only", "burst,bite"))
        self.assertEqual(code, 0, out + err)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["bite.mov", "bite.mp4", "burst.mov", "burst.mp4", "contact_sheet.png"])
        self.assertIn("making burst (1/2)", out)
        self.assertIn("Done: 2 transition videos in", out)
        self.assertIn(f"ytt style set default stinger-dir {self.out} transition stinger", out)
        self.assertIn(f"--stinger-dir {self.out} -t stinger", out)

    def test_it_never_touches_or_creates_a_workspace(self):
        self.run_cli(*self.small("--only", "bite", "--no-mp4", "--no-sheet"))
        self.assertFalse(self.root.exists())

    def test_no_mp4_and_no_sheet_leave_only_the_transparent_movies(self):
        self.run_cli(*self.small("--only", "bite", "--no-mp4", "--no-sheet"))
        self.assertEqual([p.name for p in self.out.iterdir()], ["bite.mov"])

    def test_dry_run_shows_the_plan_and_makes_nothing(self):
        code, out, _ = self.run_cli(*self.small("--dry-run"))
        self.assertEqual(code, 0)
        self.assertIn("burst: comic starburst", out)
        self.assertIn("(dry run: nothing was made)", out)
        self.assertFalse(self.out.exists())

    def test_list_needs_nothing_and_names_all_five(self):
        code, out, _ = self.run_cli("--list")
        self.assertEqual(code, 0)
        for name in ("crimp_wipe", "slats", "halftone", "burst", "bite"):
            self.assertIn(name, out)
        self.assertFalse(self.root.exists())

    def test_replacing_files_needs_a_yes_and_without_a_terminal_it_refuses_instead_of_waiting(self):
        self.run_cli(*self.small("--only", "bite", "--no-mp4", "--no-sheet"))
        before = (self.out / "bite.mov").read_bytes()
        code, out, err = self.run_cli(*self.small("--only", "bite", "--no-mp4", "--no-sheet", "--direction", "left"))
        self.assertEqual(code, 1)
        self.assertIn("will be replaced: bite.mov", out)
        self.assertIn("--yes", err)
        self.assertEqual((self.out / "bite.mov").read_bytes(), before)

    def test_yes_replaces_them(self):
        self.run_cli(*self.small("--only", "bite", "--no-mp4", "--no-sheet", "--direction", "up"))
        before = (self.out / "bite.mov").read_bytes()
        code, out, _ = self.run_cli(*self.small("--only", "bite", "--no-mp4", "--no-sheet", "--direction", "left", "--yes"))
        self.assertEqual(code, 0)
        self.assertNotEqual((self.out / "bite.mov").read_bytes(), before)
        self.assertEqual([p.name for p in self.out.iterdir()], ["bite.mov"])

    def test_a_bad_value_is_listed_under_errors_and_nothing_is_made(self):
        code, out, _ = self.run_cli("-o", str(self.out), "--size", "huge", "--only", "nope")
        self.assertEqual(code, 1)
        self.assertIn("Errors", out)
        self.assertIn("1080x1920", out)
        self.assertIn("unknown transition video(s): nope", out)
        self.assertFalse(self.out.exists())

    def test_ctrl_c_says_cancelled_and_exits_130(self):
        with mock.patch.object(cli.stingers_mod, "run_stingers", side_effect=KeyboardInterrupt):
            code, out, err = self.run_cli(*self.small())
        self.assertEqual(code, 130)
        self.assertIn("Cancelled", err)

    def test_the_other_style_commands_still_need_and_use_the_workspace(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--workspace", str(self.root), "style", "list"])
        self.assertEqual(code, 1)                                   # there is no workspace to list styles from


if __name__ == "__main__":
    unittest.main()
