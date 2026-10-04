"""
End-to-end tests for `remake` and the library views. The starting point is state written by the real old scripts
(fake YouTube, real ffmpeg), imported into a fresh workspace; then compilations are really remade.
"""
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from ytt.engine import stitch
from ytt.engine.stitch import EngineError
from ytt.ops.compile import remake as rm
from ytt.ops.compile.style import Style
from ytt.ops.errors import OpError
from ytt.ops.legacy_import import import_legacy
from ytt.ops.library import views
from ytt.ui import cli
from ytt.workspace import store
from ytt.workspace.workspace import Workspace

try:
    from test_auto_compile import WorldTest as _World
    from test_stitch_videos import _durations
except ImportError:
    from tests.test_auto_compile import WorldTest as _World
    from tests.test_stitch_videos import _durations


class RemakeWorld(_World):
    """Two compilations of 4 one-second clips each, made by the old tool, then imported."""

    def setUp(self):
        super().setUp()
        self.run_ac(*self.base("--pick", "comps:2"))
        self.assertEqual(len(self.made()), 2)
        self.ws = Workspace.init(self.tmp / "new_ws")
        self.addCleanup(self.ws.close)
        import_legacy(self.ws, self.tmp)
        self.ws.conn.commit()

    # ---- helpers
    def plan(self, **kw):
        return rm.plan_remake(self.ws, rm.RemakeRequest(**kw))

    def remake(self, **kw):
        rp = self.plan(**kw)
        self.assertTrue(rp.plan.ok, rp.plan.errors)
        return rm.run_remake(self.ws, rp)

    def clip_ids(self, name):
        comp = store.get_compilation(self.ws.conn, name)
        return [r["youtube_id"] for r in store.compilation_clips(self.ws.conn, comp["id"])]

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--workspace", str(self.ws.root), *argv])
        return code, out.getvalue(), err.getvalue()


class RemakeTests(RemakeWorld):
    def test_remake_last_makes_a_new_compilation_from_the_same_clips_and_leaves_the_old_one_alone(self):
        old_file = self.tmp / "compilations" / "compilation_002.mp4"
        before = old_file.stat().st_mtime_ns
        result = self.remake(targets="last")
        self.assertEqual((result.status, result.made), ("completed", ["compilation_003"]))
        new = store.get_compilation(self.ws.conn, "compilation_003")
        self.assertEqual(self.clip_ids("compilation_003"), self.clip_ids("compilation_002"))
        self.assertEqual(new["parent_id"], store.get_compilation(self.ws.conn, "compilation_002")["id"])
        self.assertFalse(new["imported"])
        self.assertTrue(self.ws.from_stored(new["output_path"]).is_file())
        self.assertEqual(self.ws.from_stored(new["output_path"]).name, "compilation_003.mp4")
        self.assertEqual(old_file.stat().st_mtime_ns, before)
        self.assertEqual(len(self.made()), 2)                          # the old ledger file is not touched either

    def test_what_was_done_is_recorded_in_the_compilation_and_in_a_run(self):
        self.remake(targets="last", style=None)
        new = store.get_compilation(self.ws.conn, "compilation_003")
        self.assertEqual(json.loads(new["style_snapshot"])["transition"], "cut")      # the imported default style
        self.assertEqual(json.loads(new["request"])["targets"], "last")
        run = store.list_runs(self.ws.conn)[0]
        self.assertEqual((run["kind"], run["status"]), ("remake", "completed"))
        self.assertEqual(json.loads(run["items"]), [{"what": "compilation_003", "status": "completed", "detail": ""}])
        self.assertTrue(run["finished"])

    def test_reversed_order_plays_the_same_clips_backwards(self):
        self.remake(targets="1", order="reversed")
        self.assertEqual(self.clip_ids("compilation_003"), list(reversed(self.clip_ids("compilation_001"))))

    def test_a_chosen_style_changes_the_result_and_is_snapshotted(self):
        store.save_style(self.ws.conn, "soft", Style(transition="fade", transition_seconds=0.3).to_dict())
        self.ws.conn.commit()
        plain = _durations(self.tmp / "compilations" / "compilation_001.mp4")["video"]
        self.remake(targets="1", style="soft")
        out = self.ws.from_stored(store.get_compilation(self.ws.conn, "compilation_003")["output_path"])
        self.assertAlmostEqual(_durations(out)["video"], plain + 3 * 0.3, delta=0.15)     # hold mode adds the transition time
        self.assertEqual(json.loads(store.get_compilation(self.ws.conn, "compilation_003")["style_snapshot"])["transition"], "fade")

    def test_remaking_a_remake_uses_the_style_it_was_made_with(self):
        store.save_style(self.ws.conn, "soft", Style(transition="fade", transition_seconds=0.3).to_dict())
        self.ws.conn.commit()
        self.remake(targets="1", style="soft")
        self.remake(targets="compilation_003")                         # no --style: keeps "soft", not the default
        self.assertEqual(json.loads(store.get_compilation(self.ws.conn, "compilation_004")["style_snapshot"])["transition"], "fade")
        self.assertEqual(store.get_compilation(self.ws.conn, "compilation_004")["parent_id"],
                         store.get_compilation(self.ws.conn, "compilation_003")["id"])

    def test_replace_overwrites_in_place_without_adding_a_compilation(self):
        store.save_style(self.ws.conn, "soft", Style(transition="fade", transition_seconds=0.3).to_dict())
        self.ws.conn.commit()
        out = self.tmp / "compilations" / "compilation_001.mp4"
        before = _durations(out)["video"]
        count = len(store.list_compilations(self.ws.conn))
        original = self.clip_ids("compilation_001")
        result = self.remake(targets="1", style="soft", replace=True, order="reversed")
        self.assertEqual(self.clip_ids("compilation_001"), list(reversed(original)))
        self.assertEqual(result.made, ["compilation_001"])
        self.assertEqual(len(store.list_compilations(self.ws.conn)), count)
        self.assertGreater(_durations(out)["video"], before)
        row = store.get_compilation(self.ws.conn, "compilation_001")
        self.assertEqual(json.loads(row["style_snapshot"])["transition"], "fade")
        self.assertEqual(len(self.clip_ids("compilation_001")), 4)                     # clips replaced by the reversed list
        self.assertFalse(stitch.part_path_for(out).exists())

    def test_all_remakes_every_compilation_with_fresh_numbers(self):
        result = self.remake(targets="all")
        self.assertEqual(result.made, ["compilation_003", "compilation_004"])
        self.assertEqual(self.clip_ids("compilation_003"), self.clip_ids("compilation_001"))
        self.assertEqual(self.clip_ids("compilation_004"), self.clip_ids("compilation_002"))

    def test_new_numbers_never_collide_with_files_already_in_the_folder(self):
        (self.tmp / "compilations" / "compilation_009.mp4").write_bytes(b"someone's file")
        self.assertEqual(self.remake(targets="last").made, ["compilation_010"])


class ResolveTargetsTests(RemakeWorld):
    def names(self, spec):
        return [r["name"] for r in rm.resolve_targets(self.ws.conn, spec)]

    def test_the_ways_to_say_which_compilation(self):
        self.assertEqual(self.names("last"), ["compilation_002"])
        self.assertEqual(self.names("all"), ["compilation_001", "compilation_002"])
        self.assertEqual(self.names("1"), ["compilation_001"])
        self.assertEqual(self.names("compilation_002"), ["compilation_002"])
        self.assertEqual(self.names("2,1"), ["compilation_001", "compilation_002"])
        self.assertEqual(self.names("1,1,compilation_001"), ["compilation_001"])

    def test_an_unknown_compilation_is_an_error_that_says_where_to_look(self):
        with self.assertRaises(OpError) as cm:
            self.names("7")
        self.assertIn("library compilations", str(cm.exception))

    def test_nothing_made_yet(self):
        with Workspace.init(self.tmp / "empty_ws") as ws, self.assertRaises(OpError) as cm:
            rm.resolve_targets(ws.conn, "last")
        self.assertIn("nothing to remake", str(cm.exception))


class PlanTests(RemakeWorld):
    def test_planning_changes_nothing_not_even_creating_the_output_folder(self):
        self.ws.config["compilations_dir"] = str(self.tmp / "brand_new_folder")
        rp = self.plan(targets="last")
        self.assertTrue(rp.plan.ok)
        self.assertFalse((self.tmp / "brand_new_folder").exists())
        self.assertEqual(store.list_runs(self.ws.conn), [])
        self.assertEqual(len(store.list_compilations(self.ws.conn)), 2)
        self.assertIn("compilation_003", rp.plan.actions[0].text)
        self.assertIn("4 clips", rp.plan.actions[0].text)

    def test_the_plan_is_a_plain_object_that_can_be_stored(self):
        d = self.plan(targets="all").plan.to_dict()
        json.dumps(d)
        self.assertEqual(len(d["actions"]), 2)

    def test_an_unknown_style_is_an_error_listing_the_styles_there_are(self):
        rp = self.plan(targets="last", style="nonsense")
        self.assertFalse(rp.plan.ok)
        self.assertIn("nonsense", rp.plan.errors[0])
        self.assertIn("default", rp.plan.errors[0])

    def test_a_bad_order_is_refused(self):
        with self.assertRaises(OpError):
            self.plan(targets="last", order="sideways")

    def test_missing_clips_block_a_single_target_and_name_the_clips(self):
        comp = store.get_compilation(self.ws.conn, "compilation_002")
        victim = store.compilation_clips(self.ws.conn, comp["id"])[0]
        os.remove(victim["path"])
        rp = self.plan(targets="last")
        self.assertFalse(rp.plan.ok)
        self.assertIn(victim["youtube_id"], rp.plan.errors[0])
        self.assertIn("ytt fetch", rp.plan.errors[0])

    def test_with_several_targets_the_unremakable_one_is_skipped_with_a_warning_and_the_rest_go_ahead(self):
        comp = store.get_compilation(self.ws.conn, "compilation_002")
        os.remove(store.compilation_clips(self.ws.conn, comp["id"])[0]["path"])
        rp = self.plan(targets="all")
        self.assertTrue(rp.plan.ok)
        self.assertEqual(len(rp.jobs), 1)
        self.assertTrue(any("Skipped compilation_002" in w for w in rp.plan.warnings))
        self.assertEqual(rm.run_remake(self.ws, rp).made, ["compilation_003"])

    def test_missing_intro_or_stinger_folder_is_caught_before_any_work(self):
        store.save_style(self.ws.conn, "broken", Style(intro="/no/such/intro.mp4", stinger_dir="/no/such/dir").to_dict())
        rp = self.plan(targets="last", style="broken")
        self.assertFalse(rp.plan.ok)
        self.assertIn("intro", rp.plan.errors[0])
        self.assertIn("transition-video folder", rp.plan.errors[0])

    def test_replace_warns_that_the_old_video_will_be_overwritten(self):
        self.assertTrue(any("overwritten" in w for w in self.plan(targets="1", replace=True).plan.warnings))

    def test_replace_needs_a_recorded_output_file(self):
        self.ws.conn.execute("UPDATE compilations SET output_path = NULL WHERE name = 'compilation_001'")
        rp = self.plan(targets="1", replace=True)
        self.assertFalse(rp.plan.ok)
        self.assertIn("no recorded output file", rp.plan.errors[0])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can write anywhere")
    def test_an_unwritable_output_folder_is_an_error(self):
        locked = self.tmp / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        self.ws.config["compilations_dir"] = str(locked)
        self.assertIn("not writable", " ".join(self.plan(targets="last").plan.errors))

    def test_missing_ffmpeg_is_an_error_not_a_crash(self):
        with mock.patch.object(stitch.shutil, "which", lambda name, *a, **k: None):
            rp = self.plan(targets="last")
        self.assertFalse(rp.plan.ok)
        self.assertIn("ffmpeg", rp.plan.errors[0])

    def test_running_a_plan_with_errors_is_refused(self):
        rp = self.plan(targets="last", style="nonsense")
        with self.assertRaises(OpError):
            rm.run_remake(self.ws, rp)
        self.assertEqual(store.list_runs(self.ws.conn), [])


class FailureTests(RemakeWorld):
    def test_one_failed_render_is_recorded_and_the_others_still_happen(self):
        real = stitch.render
        calls = []

        def flaky(spec, **kw):
            calls.append(spec.output.name)
            if len(calls) == 1:
                raise EngineError("ffmpeg failed:\nsomething broke")
            return real(spec, **kw)

        with mock.patch.object(stitch, "render", flaky):
            result = self.remake(targets="all")
        self.assertEqual(result.status, "partial")
        self.assertEqual([(i.what, i.status) for i in result.items], [("compilation_003", "failed"), ("compilation_004", "completed")])
        self.assertIn("something broke", result.items[0].detail)
        self.assertIsNone(store.get_compilation(self.ws.conn, "compilation_003"))          # nothing recorded for the failure
        self.assertIsNotNone(store.get_compilation(self.ws.conn, "compilation_004"))
        self.assertEqual(store.list_runs(self.ws.conn)[0]["status"], "partial")

    def test_everything_failing_is_a_failed_run(self):
        with mock.patch.object(stitch, "render", side_effect=EngineError("nope")):
            result = self.remake(targets="all")
        self.assertEqual(result.status, "failed")
        self.assertEqual(len(store.list_compilations(self.ws.conn)), 2)

    def test_ctrl_c_keeps_what_finished_and_records_the_rest_as_not_done(self):
        real = stitch.render
        calls = []

        def interrupted(spec, **kw):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(spec, **kw)

        with mock.patch.object(stitch, "render", interrupted):
            self.ws.config["make"]["prefix"] = "compilation"
            rp = self.plan(targets="all")
            result = rm.run_remake(self.ws, rp)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual([(i.what, i.status) for i in result.items],
                         [("compilation_003", "completed"), ("compilation_004", "cancelled")])
        self.assertIsNotNone(store.get_compilation(self.ws.conn, "compilation_003"))      # finished work is kept
        self.assertIsNone(store.get_compilation(self.ws.conn, "compilation_004"))
        self.assertEqual(store.list_runs(self.ws.conn)[0]["status"], "cancelled")


class LibraryViewTests(RemakeWorld):
    def test_stats_clips_compilations_sources_and_runs(self):
        self.remake(targets="last")
        s = views.stats(self.ws)
        self.assertEqual((s["clips"], s["ready"], s["missing"], s["compilations"], s["sources"]), (8, 8, 0, 3, 1))
        self.assertEqual((s["used"], s["unused"]), (8, 0))
        self.assertGreater(s["clips_bytes"], 0)
        self.assertGreater(s["compilations_bytes"], 0)
        comps = {c["name"]: c for c in views.compilations(self.ws)}
        self.assertEqual(comps["compilation_003"]["parent"], "compilation_002")
        self.assertTrue(comps["compilation_003"]["style_recorded"])
        self.assertFalse(comps["compilation_001"]["style_recorded"])
        self.assertTrue(comps["compilation_001"]["imported"])
        self.assertEqual(views.sources(self.ws), [{"handle": "@Chan", "videos": 8, "clips": 8}])
        self.assertEqual(views.runs(self.ws)[0]["kind"], "remake")

    def test_filters_on_clips(self):
        os.remove(views.clips(self.ws)[0]["path"])
        self.ws.conn.execute("UPDATE clips SET status = 'missing' WHERE id = (SELECT MIN(id) FROM clips)")
        self.assertEqual(len(views.clips(self.ws, status="missing")), 1)
        self.assertEqual(views.clips(self.ws, unused=True), [])

    def test_a_compilation_whose_file_is_gone_is_noticed(self):
        os.remove(self.tmp / "compilations" / "compilation_001.mp4")
        self.assertEqual(views.stats(self.ws)["compilations_missing_file"], 1)


class CliTests(RemakeWorld):
    def test_remake_through_the_command_line(self):
        code, out, err = self.cli("remake", "last", "--yes")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("makes compilation_003", out)
        self.assertIn("made compilation_003", out)
        self.assertIn("Completed: 1 compilation made", out)

    def test_dry_run_shows_the_plan_and_changes_nothing(self):
        code, out, _ = self.cli("remake", "all", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("compilation_004", out)
        self.assertIn("dry run", out)
        self.assertEqual(len(store.list_compilations(Workspace.open(self.ws.root).conn)), 2)

    def test_without_a_terminal_and_without_yes_it_refuses_instead_of_hanging(self):
        with mock.patch.object(cli, "is_tty", lambda: False):
            code, out, err = self.cli("remake", "last")
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)
        self.assertEqual(len(store.list_compilations(Workspace.open(self.ws.root).conn)), 2)

    def test_answering_no_does_nothing_and_answering_yes_proceeds(self):
        with mock.patch.object(cli, "is_tty", lambda: True), mock.patch("builtins.input", lambda *_: "n"):
            code, out, _ = self.cli("remake", "last")
        self.assertEqual(code, 0)
        self.assertIn("nothing was done", out)
        with mock.patch.object(cli, "is_tty", lambda: True), mock.patch("builtins.input", lambda *_: ""):
            code, out, _ = self.cli("remake", "last")
        self.assertIn("Completed", out)

    def test_a_plan_with_errors_exits_1_and_says_why(self):
        code, out, _ = self.cli("remake", "last", "--style", "nonsense", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("Errors", out)
        self.assertIn("nonsense", out)

    def test_an_unknown_target_is_a_plain_error_message(self):
        code, _, err = self.cli("remake", "99", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("No compilation matches", err)

    def test_ctrl_c_exits_130_after_recording_the_run(self):
        with mock.patch.object(stitch, "render", side_effect=KeyboardInterrupt):
            code, out, _ = self.cli("remake", "last", "--yes")
        self.assertEqual(code, 130)
        self.assertEqual(store.list_runs(Workspace.open(self.ws.root).conn)[0]["status"], "cancelled")

    def test_library_summary_lists_and_json(self):
        code, out, _ = self.cli("library")
        self.assertEqual(code, 0)
        self.assertIn("8 clips (8 ready, 0 missing)", out)
        self.assertIn("2 compilations", out)
        code, out, _ = self.cli("library", "compilations")
        self.assertIn("compilation_001", out)
        self.assertIn("unknown", out)
        code, out, _ = self.cli("library", "clips", "--json")
        self.assertEqual(len(json.loads(out)), 8)
        code, out, _ = self.cli("library", "stats", "--json")
        self.assertEqual(json.loads(out)["compilations"], 2)
        code, out, _ = self.cli("library", "clips", "--unused")
        self.assertIn("Nothing here yet", out)
        code, out, _ = self.cli("library", "sources")
        self.assertIn("@Chan", out)
        code, out, _ = self.cli("library", "runs")
        self.assertIn("Nothing here yet", out)


if __name__ == "__main__":
    unittest.main()
