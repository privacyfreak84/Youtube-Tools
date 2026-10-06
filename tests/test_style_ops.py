"""Managing styles: parsing what a person types, the plan for a change, creating, copying, deleting."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from ytt.ops.compile import style_ops as so
from ytt.ops.compile.style import Style, ensure_default
from ytt.ops.errors import OpError
from ytt.workspace import store
from ytt.workspace.workspace import Workspace


class StyleOpsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytt_style_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ws = Workspace.init(self.tmp / "ws")
        self.addCleanup(self.ws.close)
        ensure_default(self.ws.conn)
        self.ws.conn.commit()

    def saved(self, name):
        return store.get_style(self.ws.conn, name)

    def change(self, name, changes, **kw):
        sp = so.plan_save(self.ws, name, changes, **kw)
        self.assertTrue(sp.plan.ok, sp.plan.errors)
        so.run_save(self.ws, sp)
        return sp


class ParsingValues(StyleOpsTest):
    def test_each_kind_of_setting_is_converted(self):
        self.assertEqual(so.parse_value("transition_seconds", "0.5"), 0.5)
        self.assertEqual(so.parse_value("max_height", "720"), 720)
        self.assertIs(so.parse_value("stinger_audio", "No"), False)
        self.assertIs(so.parse_value("stinger_despill", "yes"), True)
        self.assertEqual(so.parse_value("quality", " best "), "best")

    def test_wrong_kinds_say_what_is_needed(self):
        for key, text, word in (("transition_seconds", "soon", "a number"), ("max_height", "tall", "a whole number"),
                                ("stinger_audio", "maybe", "yes or no")):
            with self.subTest(key=key), self.assertRaises(OpError) as cm:
                so.parse_value(key, text)
            self.assertIn(word, str(cm.exception))

    def test_a_transition_must_be_one_the_engine_knows(self):
        for ok in ("fade", "CUT", "random", "stinger", "wipeleft:2", "@clip.mp4|key=green"):
            so.parse_value("transition", ok)
        with self.assertRaises(OpError) as cm:
            so.parse_value("transition", "fadee")
        self.assertIn("not a transition", str(cm.exception))

    def test_file_settings_become_absolute_paths_and_can_be_cleared(self):
        value = so.parse_value("intro", "clips/intro.mp4")
        self.assertTrue(Path(value).is_absolute())
        self.assertTrue(value.endswith("intro.mp4"))
        self.assertEqual(so.parse_value("intro", ""), "")

    def test_keys_may_be_typed_with_dashes_in_any_case(self):
        self.assertEqual(so.normalize_key("Transition-Seconds"), "transition_seconds")
        with self.assertRaises(OpError) as cm:
            so.normalize_key("colour")
        self.assertIn("The settings are", str(cm.exception))


class ChangingAStyle(StyleOpsTest):
    def test_the_plan_shows_old_and_new_and_nothing_is_saved_until_it_runs(self):
        sp = so.plan_save(self.ws, "default", {"quality": "best", "transition-seconds": "0.5"})
        self.assertEqual([a.text for a in sp.plan.actions], ["transition_seconds: 1.0 → 0.5", "quality: balanced → best"])
        self.assertTrue(sp.changed)
        self.assertEqual(self.saved("default")["quality"], "balanced")
        so.run_save(self.ws, sp)
        self.assertEqual((self.saved("default")["quality"], self.saved("default")["transition_seconds"]), ("best", 0.5))

    def test_setting_what_is_already_there_changes_nothing(self):
        sp = so.plan_save(self.ws, "default", {"quality": "balanced"})
        self.assertFalse(sp.changed)
        self.assertTrue(any("Nothing changes" in n for n in sp.plan.notes))

    def test_a_bad_value_is_refused_before_anything_is_saved(self):
        with self.assertRaises(OpError) as cm:
            so.plan_save(self.ws, "default", {"quality": "ultra"})
        self.assertIn("fast, balanced, best", str(cm.exception))
        self.assertEqual(self.saved("default")["quality"], "balanced")

    def test_a_style_that_does_not_exist_is_not_created_by_a_typo(self):
        with self.assertRaises(OpError) as cm:
            so.plan_save(self.ws, "defualt", {"quality": "best"})
        self.assertIn("--new", str(cm.exception))
        self.assertIsNone(self.saved("defualt"))

    def test_new_starts_from_the_built_in_look_and_from_copies_another_style(self):
        self.change("default", {"quality": "best"})
        self.change("plain", {"transition": "cut"}, new=True)
        self.assertEqual((self.saved("plain")["transition"], self.saved("plain")["quality"]), ("cut", "balanced"))
        self.change("copy", {"max_height": "720"}, source="default")
        self.assertEqual((self.saved("copy")["quality"], self.saved("copy")["max_height"]), ("best", 720))

    def test_a_copy_with_no_changes_is_still_created(self):
        sp = self.change("twin", {}, source="default")
        self.assertTrue(sp.changed)
        self.assertEqual(self.saved("twin"), self.saved("default"))

    def test_new_or_from_on_a_name_that_exists_and_both_together_are_refused(self):
        for kw in (dict(new=True), dict(source="default")):
            with self.assertRaises(OpError) as cm:
                so.plan_save(self.ws, "default", {}, **kw)
            self.assertIn("already exists", str(cm.exception))
        with self.assertRaises(OpError):
            so.plan_save(self.ws, "x", {}, new=True, source="default")
        with self.assertRaises(OpError):
            so.plan_save(self.ws, "x", {}, source="ghost")

    def test_names_are_kept_simple(self):
        for bad in ("", "has space", "-lead", "a/b"):
            with self.subTest(name=bad), self.assertRaises(OpError):
                so.plan_save(self.ws, bad, {}, new=True)

    def test_a_file_that_is_not_there_yet_is_a_warning_not_an_error(self):
        sp = so.plan_save(self.ws, "default", {"intro": str(self.tmp / "intro.mp4")})
        self.assertTrue(sp.plan.ok)
        self.assertTrue(any("does not exist yet" in w for w in sp.plan.warnings))
        (self.tmp / "intro.mp4").write_bytes(b"x")
        self.assertEqual(so.plan_save(self.ws, "default", {"intro": str(self.tmp / "intro.mp4")}).plan.warnings, [])

    def test_changing_the_default_style_says_what_that_affects(self):
        sp = so.plan_save(self.ws, "default", {"quality": "fast"})
        self.assertTrue(any("default style" in n for n in sp.plan.notes))


class ListingAndDeleting(StyleOpsTest):
    def test_list_marks_the_default_and_survives_a_style_that_is_not_valid(self):
        self.change("calm", {"transition": "cut"}, new=True)
        store.save_style(self.ws.conn, "broken", {"quality": "ultra"})
        self.ws.conn.commit()
        rows = {name: (is_default, style, problem) for name, is_default, style, problem in so.list_styles(self.ws)}
        self.assertEqual(sorted(rows), ["broken", "calm", "default"])
        self.assertTrue(rows["default"][0])
        self.assertIsNone(rows["broken"][1])
        self.assertIn("quality", rows["broken"][2])
        self.assertEqual(rows["calm"][1].transition, "cut")

    def test_load_names_the_saved_styles_when_there_is_no_such_one(self):
        with self.assertRaises(OpError) as cm:
            so.load(self.ws, "nope")
        self.assertIn("Saved styles: default", str(cm.exception))

    def test_delete_removes_the_style_and_says_how_many_compilations_keep_a_copy(self):
        self.change("calm", {"transition": "cut"}, new=True)
        store.add_compilation(self.ws.conn, "compilation_001", clips=[], request={"kind": "make", "style": "calm"},
                              style_snapshot=Style(transition="cut").to_dict())
        self.ws.conn.commit()
        plan = so.plan_delete(self.ws, "calm")
        self.assertTrue(plan.ok)
        self.assertTrue(any("1 compilation was made with it" in n for n in plan.notes))
        so.run_delete(self.ws, "calm", plan)
        self.assertIsNone(self.saved("calm"))
        comp = store.get_compilation(self.ws.conn, "compilation_001")
        self.assertEqual(json.loads(comp["style_snapshot"])["transition"], "cut")      # the compilation is untouched

    def test_the_workspace_default_cannot_be_deleted(self):
        plan = so.plan_delete(self.ws, "default")
        self.assertFalse(plan.ok)
        self.assertIn("default_style", plan.errors[0])
        with self.assertRaises(OpError):
            so.run_delete(self.ws, "default", plan)
        self.assertIsNotNone(self.saved("default"))

    def test_deleting_a_style_that_does_not_exist_is_an_error(self):
        with self.assertRaises(OpError):
            so.plan_delete(self.ws, "ghost")


if __name__ == "__main__":
    unittest.main()
