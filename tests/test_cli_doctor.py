"""`ytt doctor` through main(), with a pretend machine handed in where the real one would be looked at."""
import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ytt.ui import cli
from ytt.workspace.workspace import Workspace

try:
    from test_doctor import FakeProbes
except ImportError:
    from tests.test_doctor import FakeProbes


class DoctorCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytt_cli_doctor_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "ws"
        self.probes = FakeProbes()
        patcher = mock.patch.object(cli, "make_probes", lambda: self.probes)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--workspace", str(self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_all_well_prints_ok_lines_a_clean_summary_and_exits_0(self):
        Workspace.init(self.root).close()
        code, out, err = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn(f"workspace: {self.root}", out)
        self.assertRegex(out, r"ok\s+Python\s+3\.12\.3")
        self.assertRegex(out, r"ok\s+YouTube")
        self.assertIn("Everything looks fine.", out)
        self.assertNotIn("->", out)

    def test_a_problem_shows_its_fix_under_it_and_makes_the_exit_code_1(self):
        Workspace.init(self.root).close()
        self.probes.programs.pop("ffmpeg")
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertRegex(out, r"FAIL\s+ffmpeg\s+not found\n\s+->\s+install ffmpeg")
        self.assertIn("1 problem, 0 warnings.", out)

    def test_warnings_alone_exit_0(self):
        self.probes.ytdlp = "2026.01.01"                  # old, and there is no workspace: two warnings, no problems
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("0 problems, 2 warnings.", out)

    def test_offline_asks_the_network_nothing(self):
        Workspace.init(self.root).close()
        code, out, _ = self.run_cli("doctor", "--offline")
        self.assertEqual(code, 0)
        self.assertEqual(self.probes.asked, [])
        self.assertNotIn("YouTube", out)

    def test_it_works_with_no_workspace_at_all_and_makes_none(self):
        code, out, _ = self.run_cli("doctor", "--offline")
        self.assertEqual(code, 0)
        self.assertIn("none at", out)
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
