"""Tests for the `ytt workspace ...` commands, run through main() the way the real command runs them."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from ytt.ui.cli import main
from ytt.workspace import config as cfgmod
from ytt.workspace import store
from ytt.workspace.errors import WorkspaceError
from ytt.workspace.workspace import Workspace


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytt_cli_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "ws"

    def run_cli(self, *argv, environ=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--workspace", str(self.root), *argv] if environ is None else list(argv), environ=environ)
        return code, out.getvalue(), err.getvalue()

    def old_state(self):
        old = self.tmp / "old"
        (old / "fetched").mkdir(parents=True)
        f = old / "fetched" / "20260101-1200_0001_vid00000001.mp4"
        f.write_bytes(b"x")
        (old / "fetch_archive.json").write_text(json.dumps(
            {"vid00000001": {"file": f.name, "title": "T", "views": 5, "duration": 9, "channel": "@Chan", "run": "r"}}))
        (old / "auto_settings.json").write_text(json.dumps({"dest": str(old / "fetched"), "channel": "@Chan"}))
        return old


class WorkspaceCommandTests(CliTest):
    def test_init_creates_the_workspace_with_a_default_style_and_is_repeatable(self):
        code, out, _ = self.run_cli("workspace", "init")
        self.assertEqual(code, 0)
        self.assertIn("Created workspace", out)
        code, out, _ = self.run_cli("workspace", "init")
        self.assertIn("already set up", out)
        with Workspace.open(self.root) as ws:
            self.assertEqual(store.list_styles(ws.conn), ["default"])

    def test_show_before_init_says_how_to_start_and_fails(self):
        code, out, err = self.run_cli("workspace", "show")
        self.assertEqual(code, 1)
        self.assertIn("ytt workspace init", err)
        self.assertEqual(out, "")

    def test_show_lists_location_library_and_settings(self):
        self.run_cli("workspace", "init")
        code, out, _ = self.run_cli("workspace", "show")
        self.assertEqual(code, 0)
        for text in (str(self.root), "0 clips", "workers = 3", "[make]", "clips_each = 15"):
            self.assertIn(text, out)

    def test_set_changes_a_setting_and_it_sticks(self):
        self.run_cli("workspace", "init")
        code, out, _ = self.run_cli("workspace", "set", "make.clips_each", "20")
        self.assertEqual((code, out.strip()), (0, "make.clips_each = 20"))
        self.assertEqual(Workspace.open(self.root).config["make"]["clips_each"], 20)

    def test_set_refuses_unknown_keys_and_wrong_kinds_and_changes_nothing(self):
        self.run_cli("workspace", "init")
        before = (self.root / "workspace.toml").read_text()
        for key, value in (("nonsense", "1"), ("make.nonsense", "1"), ("workers", "lots"), ("workers", "-2"),
                           ("delete_used_clips", "maybe"), ("make", "x")):
            with self.subTest(key=key, value=value):
                code, _, err = self.run_cli("workspace", "set", key, value)
                self.assertEqual(code, 1)
                self.assertIn("Error:", err)
        self.assertEqual((self.root / "workspace.toml").read_text(), before)

    def test_set_understands_yes_no_and_lists(self):
        self.run_cli("workspace", "init")
        self.run_cli("workspace", "set", "delete_used_clips", "yes")
        self.run_cli("workspace", "set", "watch_folders", "/a, /b c")
        cfg = Workspace.open(self.root).config
        self.assertEqual((cfg["delete_used_clips"], cfg["watch_folders"]), (True, ["/a", "/b c"]))

    def test_the_environment_variable_picks_the_workspace_when_no_flag_is_given(self):
        code, out, _ = self.run_cli("workspace", "init", environ={"YTT_WORKSPACE": str(self.root)})
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "ytt.db").exists())

    def test_no_command_prints_help_and_succeeds(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("workspace", out)

    def test_broken_settings_file_is_an_error_message_not_a_traceback(self):
        self.run_cli("workspace", "init")
        (self.root / "workspace.toml").write_text("workers = = 3\n")
        code, _, err = self.run_cli("workspace", "show")
        self.assertEqual(code, 1)
        self.assertIn("not valid TOML", err)


class ImportCommandTests(CliTest):
    def test_import_creates_the_workspace_if_needed_and_prints_a_report(self):
        old = self.old_state()
        code, out, _ = self.run_cli("workspace", "import", str(old))
        self.assertEqual(code, 0)
        self.assertIn("Created workspace", out)
        self.assertIn("1 clip", out)
        with Workspace.open(self.root) as ws:
            self.assertEqual(ws.counts()["clips"], 1)
            self.assertIn("default", store.list_styles(ws.conn))

    def test_dry_run_with_no_workspace_shows_a_preview_and_creates_nothing(self):
        old = self.old_state()
        code, out, _ = self.run_cli("workspace", "import", str(old), "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Preview", out)
        self.assertIn("nothing was written", out)
        self.assertIn("1 clip", out)
        self.assertFalse(self.root.exists())

    def test_dry_run_on_an_existing_workspace_changes_nothing(self):
        self.run_cli("workspace", "init")
        old = self.old_state()
        toml = (self.root / "workspace.toml").read_text()
        self.run_cli("workspace", "import", str(old), "--dry-run")
        with Workspace.open(self.root) as ws:
            self.assertEqual(ws.counts()["clips"], 0)
        self.assertEqual((self.root / "workspace.toml").read_text(), toml)

    def test_importing_twice_says_nothing_new_was_added(self):
        old = self.old_state()
        self.run_cli("workspace", "import", str(old))
        code, out, _ = self.run_cli("workspace", "import", str(old))
        self.assertEqual(code, 0)
        self.assertIn("0 clips", out)

    def test_a_folder_with_no_old_state_is_a_clear_error_and_leaves_no_workspace_behind_on_preview(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        code, _, err = self.run_cli("workspace", "import", str(empty), "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("No old ytt state", err)
        self.assertFalse(self.root.exists())

    def test_a_failed_first_import_does_not_leave_an_empty_workspace_behind(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        code, _, err = self.run_cli("workspace", "import", str(empty))
        self.assertEqual(code, 1)
        self.assertFalse(self.root.exists())

    def test_a_failed_import_never_deletes_a_workspace_that_was_already_there(self):
        self.run_cli("workspace", "init")
        empty = self.tmp / "empty"
        empty.mkdir()
        self.run_cli("workspace", "import", str(empty))
        self.assertTrue((self.root / "ytt.db").exists())


if __name__ == "__main__":
    unittest.main()
