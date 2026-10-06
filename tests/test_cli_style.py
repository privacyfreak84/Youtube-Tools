"""`ytt style ...` through main()."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ytt.ui import cli
from ytt.workspace import store
from ytt.workspace.workspace import Workspace


class StyleCli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytt_clis_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "ws"
        self.tty = False
        for patch in (mock.patch.object(cli, "is_tty", lambda: self.tty),):
            patch.start()
            self.addCleanup(patch.stop)
        self.run_cli("workspace", "init")

    def run_cli(self, *argv, answers=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if answers is not None:
                with mock.patch("builtins.input", side_effect=answers):
                    code = cli.main(["--workspace", str(self.root), *argv])
            else:
                code = cli.main(["--workspace", str(self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def saved(self, name):
        with Workspace.open(self.root) as ws:
            return store.get_style(ws.conn, name)


class Viewing(StyleCli):
    def test_the_default_style_exists_from_the_start_and_is_marked(self):
        code, out, err = self.run_cli("style")
        self.assertEqual(code, 0, err)
        self.assertIn("default *", out)
        self.assertIn("fade", out)
        self.assertIn("* = the workspace's default style", out)

    def test_show_lists_every_setting(self):
        code, out, err = self.run_cli("style", "show", "default")
        self.assertEqual(code, 0, err)
        for key in ("transition", "transition_seconds", "quality", "max_height", "intro", "stinger_dir"):
            self.assertIn(key, out)
        self.assertIn("(the workspace's default)", out)
        self.assertIn("(none)", out)

    def test_json_for_scripts(self):
        data = json.loads(self.run_cli("style", "list", "--json")[1])
        self.assertEqual((data[0]["name"], data[0]["default"], data[0]["settings"]["quality"]),
                         ("default", True, "balanced"))
        shown = json.loads(self.run_cli("style", "show", "default", "--json")[1])
        self.assertEqual(shown["settings"]["max_height"], 1080)

    def test_an_unknown_style_is_an_error_that_lists_the_saved_ones(self):
        code, out, err = self.run_cli("style", "show", "nope")
        self.assertEqual(code, 1)
        self.assertIn("Saved styles: default", err)


class Setting(StyleCli):
    def test_a_dry_run_shows_the_change_and_saves_nothing(self):
        code, out, err = self.run_cli("style", "set", "default", "quality", "best", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("quality: balanced → best", out)
        self.assertIn("dry run", out)
        self.assertEqual(self.saved("default")["quality"], "balanced")

    def test_without_a_terminal_it_refuses_instead_of_hanging(self):
        code, out, err = self.run_cli("style", "set", "default", "quality", "best")
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)
        self.assertEqual(self.saved("default")["quality"], "balanced")

    def test_yes_saves_several_settings_at_once(self):
        code, out, err = self.run_cli("style", "set", "default", "quality", "best", "transition-seconds", "0.5",
                                      "transition", "wipeleft", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("Saved style 'default'", out)
        s = self.saved("default")
        self.assertEqual((s["quality"], s["transition_seconds"], s["transition"]), ("best", 0.5, "wipeleft"))

    def test_asking_on_a_terminal_and_saying_no_changes_nothing(self):
        self.tty = True
        with mock.patch("builtins.input", return_value="n"):
            code, out, err = self.run_cli("style", "set", "default", "quality", "fast")
        self.assertEqual(code, 0, err)
        self.assertIn("Cancelled", out)
        self.assertEqual(self.saved("default")["quality"], "balanced")

    def test_a_typo_in_the_style_name_does_not_create_one(self):
        code, out, err = self.run_cli("style", "set", "defualt", "quality", "best", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("--new", err)
        self.assertIsNone(self.saved("defualt"))

    def test_new_and_from_create_styles(self):
        self.run_cli("style", "set", "default", "quality", "best", "--yes")
        self.assertEqual(self.run_cli("style", "set", "calm", "transition", "cut", "--new", "--yes")[0], 0)
        self.assertEqual((self.saved("calm")["transition"], self.saved("calm")["quality"]), ("cut", "balanced"))
        self.assertEqual(self.run_cli("style", "set", "small", "max_height", "720", "--from", "default", "--yes")[0], 0)
        self.assertEqual((self.saved("small")["quality"], self.saved("small")["max_height"]), ("best", 720))

    def test_bad_input_is_explained(self):
        for argv, word in ((["default", "quality", "ultra"], "fast, balanced, best"),
                           (["default", "colour", "red"], "The settings are"),
                           (["default", "quality"], "in pairs"),
                           (["default", "transition", "fadee"], "not a transition")):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli("style", "set", *argv, "--yes")
                self.assertEqual(code, 1)
                self.assertIn(word, err)

    def test_setting_what_is_already_there_is_nothing_to_do(self):
        code, out, err = self.run_cli("style", "set", "default", "quality", "balanced", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("Nothing to do.", out)

    def test_new_and_from_exclude_each_other(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["style", "set", "x", "quality", "best", "--new", "--from", "default"])


class Editing(StyleCli):
    ORDER = ["transition", "transition_seconds", "transition_mode", "stinger_dir", "stinger_key", "stinger_sim",
             "stinger_blend", "stinger_despill", "stinger_audio", "intro", "outro", "quality", "max_height"]

    def answers(self, **given):
        return [given.get(k, "") for k in self.ORDER] + ["y"]

    def test_edit_needs_a_terminal(self):
        code, out, err = self.run_cli("style", "edit", "default")
        self.assertEqual(code, 1)
        self.assertIn("needs a terminal", err)

    def test_enter_keeps_and_typed_answers_change(self):
        self.tty = True
        code, out, err = self.run_cli("style", "edit", "default", answers=self.answers(quality="best", transition="cut"))
        self.assertEqual(code, 0, err)
        self.assertIn("quality: balanced → best", out)
        self.assertIn("Saved style 'default'", out)
        s = self.saved("default")
        self.assertEqual((s["quality"], s["transition"], s["transition_seconds"]), ("best", "cut", 1.0))

    def test_a_bad_answer_is_explained_and_asked_again(self):
        self.tty = True
        order = list(self.ORDER)
        answers = []
        for k in order:
            if k == "quality":
                answers += ["ultra", "fast"]
            else:
                answers.append("")
        code, out, err = self.run_cli("style", "edit", "default", answers=answers + ["y"])
        self.assertEqual(code, 0, err)
        self.assertIn("fast, balanced, best", out)
        self.assertEqual(self.saved("default")["quality"], "fast")

    def test_a_new_name_creates_a_style_and_a_dash_clears_a_text_setting(self):
        self.tty = True
        code, out, err = self.run_cli("style", "edit", "fresh", answers=self.answers(intro=str(self.tmp / "i.mp4")))
        self.assertEqual(code, 0, err)
        self.assertIn("New style 'fresh'", out)
        self.assertTrue(self.saved("fresh")["intro"].endswith("i.mp4"))
        code, out, err = self.run_cli("style", "edit", "fresh", answers=self.answers(intro="-"))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.saved("fresh")["intro"], "")

    def test_saying_no_at_the_end_keeps_the_style_as_it_was(self):
        self.tty = True
        answers = self.answers(quality="best")[:-1] + ["n"]
        code, out, err = self.run_cli("style", "edit", "default", answers=answers)
        self.assertIn("Cancelled", out)
        self.assertEqual(self.saved("default")["quality"], "balanced")


class Deleting(StyleCli):
    def test_a_style_can_be_deleted_with_yes_and_a_dry_run_deletes_nothing(self):
        self.run_cli("style", "set", "calm", "quality", "fast", "--new", "--yes")
        code, out, err = self.run_cli("style", "delete", "calm", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIsNotNone(self.saved("calm"))
        code, out, err = self.run_cli("style", "delete", "calm", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("Deleted style 'calm'", out)
        self.assertIsNone(self.saved("calm"))

    def test_without_a_terminal_it_refuses_instead_of_hanging(self):
        self.run_cli("style", "set", "calm", "quality", "fast", "--new", "--yes")
        code, out, err = self.run_cli("style", "delete", "calm")
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)
        self.assertIsNotNone(self.saved("calm"))

    def test_the_default_style_cannot_be_deleted(self):
        code, out, err = self.run_cli("style", "delete", "default", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("default_style", out)
        self.assertIsNotNone(self.saved("default"))


if __name__ == "__main__":
    unittest.main()
