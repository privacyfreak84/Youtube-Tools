"""
make end to end against the fake YouTube and real ffmpeg: what the plan says, what a run records, and what
survives a failure or Ctrl-C. Compilations are kept tiny (2 clips of about a second) so the renders are quick.
"""
import json
import unittest
from pathlib import Path
from unittest import mock

from ytt.engine import stitch
from ytt.engine.stitch import EngineError
from ytt.ops.compile import make as mk
from ytt.ops.compile import remake as rm
from ytt.ops.compile.make import MakeRequest
from ytt.ops.compile.style import Style
from ytt.ops.errors import OpError
from ytt.workspace import store

try:
    from fakes import FakeBackend, make_videos, template_clip
    from test_fetch import FetchWorld, vid
    from test_auto_compile import HAVE_TOOLS
except ImportError:
    from tests.fakes import FakeBackend, make_videos, template_clip
    from tests.test_fetch import FetchWorld, vid
    from tests.test_auto_compile import HAVE_TOOLS


@unittest.skipUnless(HAVE_TOOLS, "needs ffmpeg")
class MakeWorld(FetchWorld):
    """FetchWorld (a fresh workspace and a fake channel of 40 shorts, oldest first = vid00000039) with 2 clips per
    compilation, one download at a time (so the library order is the queue order) and a quick default style."""

    def setUp(self):
        super().setUp()
        self.ws.config["make"]["clips_each"] = 2
        self.ws.config["workers"] = 1
        store.save_style(self.ws.conn, "default", Style(transition="cut", quality="fast").to_dict())
        self.ws.conn.commit()

    # ---- helpers
    def request(self, fetch=None, **kw):
        return MakeRequest(fetch=self.req(**fetch) if fetch is not None else None, **kw)

    def plan(self, **kw):
        return mk.plan_make(self.ws, self.request(**kw), self.backend)

    def make(self, **kw):
        mp = self.plan(**kw)
        self.assertTrue(mp.plan.ok, mp.plan.errors)
        return mk.run_make(self.ws, mp, backend=self.backend), mp

    def ids(self, groups):
        return [[c.youtube_id for c in g] for g in groups]

    def comp_ids(self, name):
        comp = store.get_compilation(self.ws.conn, name)
        return [r["youtube_id"] for r in store.compilation_clips(self.ws.conn, comp["id"])]

    def clip_status(self, n):
        return store.get_clip(self.ws.conn, vid(n))["status"]

    def used(self):
        return sorted(c["youtube_id"] for c in store.list_clips(self.ws.conn) if c["used"])

    def dated(self, n, day):
        store.upsert_video(self.ws.conn, vid(n), published=f"2026-0{day[0]}-0{day[1]}")
        self.ws.conn.commit()


class RequestChecks(MakeWorld):
    def check(self, text, **kw):
        with self.assertRaises(OpError) as cm:
            self.plan(**kw)
        self.assertIn(text, str(cm.exception))

    def test_without_a_channel_it_must_say_how_many(self):
        self.check("How many compilations?")

    def test_n_and_all_exclude_each_other(self):
        self.check("not both", compilations=2, everything=True)

    def test_per_and_per_minutes_exclude_each_other(self):
        self.check("not both", everything=True, per=3, per_minutes=2)

    def test_a_compilation_needs_two_clips(self):
        self.check("at least 2", everything=True, per=1)

    def test_unknown_words_are_refused(self):
        self.check("order must be", everything=True, order="shuffle")
        self.check("play must be", everything=True, play="backwards")
        self.check("if-short must be", everything=True, if_short="later")

    def test_if_short_fetch_needs_a_channel(self):
        self.check("needs a channel", everything=True, if_short="fetch")
        self.check("needs a channel", fetch=dict(channel="", videos=[vid(1)]), if_short="fetch")

    def test_a_channel_still_needs_its_own_count(self):
        self.check("How many?", fetch={})

    def test_an_unknown_style_is_refused_and_lists_the_saved_ones(self):
        self.check("no style called 'nope'", everything=True, style="nope")

    def test_a_style_whose_files_are_gone_is_a_plan_error(self):
        store.save_style(self.ws.conn, "intro", Style(intro=str(self.tmp / "nope.mp4")).to_dict())
        mp = self.plan(everything=True, style="intro")
        self.assertFalse(mp.plan.ok)
        self.assertIn("intro video", mp.plan.errors[0])

    def test_the_workspace_settings_fill_in_what_the_request_leaves_out(self):
        self.ws.config["make"]["order"] = "newest"
        self.ws.config["make"]["if_short"] = "short"
        self.ws.config["delete_used_clips"] = True
        r = self.plan(everything=True).request
        self.assertEqual((r.order, r.if_short, r.per, r.delete_used, r.style), ("newest", "short", 2, True, "default"))
        self.assertEqual(self.plan(everything=True, order="random").request.order, "random")
        self.assertTrue(self.plan(everything=True, order="random").request.seed)

    def test_by_minutes_comes_from_the_workspace_when_it_is_the_size_mode(self):
        self.ws.config["make"]["size_mode"] = "minutes"
        self.ws.config["make"]["minutes_each"] = 3
        r = self.plan(everything=True).request
        self.assertEqual((r.per, r.per_minutes), (0, 3))


class PlanFromTheLibrary(MakeWorld):
    def test_everything_is_cut_in_library_order_and_the_leftover_is_kept(self):
        for n in range(1, 12):
            self.add_to_library(n)
        mp = self.plan(everything=True)
        self.assertTrue(mp.plan.ok, mp.plan.errors)
        self.assertEqual(self.ids(mp.groups), [[vid(1), vid(2)], [vid(3), vid(4)], [vid(5), vid(6)],
                                                [vid(7), vid(8)], [vid(9), vid(10)]])
        self.assertEqual(self.ids([mp.held]), [[vid(11)]])
        self.assertEqual(mp.names[0], "compilation_001")
        self.assertEqual([a.kind for a in mp.plan.actions], ["render"] * 5)
        self.assertTrue(any("kept for next time" in n for n in mp.plan.notes))

    def test_n_makes_at_most_that_many_and_what_is_left_is_spare_not_leftover(self):
        for n in range(1, 12):
            self.add_to_library(n)
        mp = self.plan(compilations=2)
        self.assertEqual(len(mp.groups), 2)
        self.assertEqual(mp.held, [])
        self.assertFalse(any("left over" in n for n in mp.plan.notes))

    def test_nothing_waiting_is_nothing_to_do_not_an_error(self):
        mp = self.plan(everything=True)
        self.assertTrue(mp.plan.ok)
        self.assertTrue(mp.nothing_to_do)
        self.assertTrue(any("Nothing to make" in n for n in mp.plan.notes))

    def test_used_clips_and_clips_without_a_file_are_not_candidates(self):
        for n in range(1, 7):
            self.add_to_library(n)
        self.add_to_library(7, status="missing")
        self.make(compilations=1)                                  # uses clips 1 and 2
        mp = self.plan(everything=True)
        self.assertEqual(self.ids(mp.groups), [[vid(3), vid(4)], [vid(5), vid(6)]])

    def test_order_oldest_and_newest_go_by_upload_date(self):
        for n in range(1, 5):
            self.add_to_library(n)
        for n, day in ((1, (3, 1)), (2, (2, 1)), (3, (1, 1)), (4, (4, 1))):
            self.dated(n, day)
        self.assertEqual(self.ids(self.plan(everything=True, order="oldest").groups),
                         [[vid(3), vid(2)], [vid(1), vid(4)]])
        self.assertEqual(self.ids(self.plan(everything=True, order="newest").groups),
                         [[vid(4), vid(1)], [vid(2), vid(3)]])

    def test_random_order_uses_the_seed_so_the_plan_and_the_run_agree(self):
        for n in range(1, 9):
            self.add_to_library(n)
        a = self.plan(everything=True, order="random", seed=5)
        b = self.plan(everything=True, order="random", seed=5)
        c = self.plan(everything=True, order="random", seed=6)
        self.assertEqual(self.ids(a.groups), self.ids(b.groups))
        self.assertNotEqual(self.ids(a.groups), self.ids(c.groups))

    def test_reverse_and_reverse_each(self):
        for n in range(1, 5):
            self.add_to_library(n)
        self.assertEqual(self.ids(self.plan(everything=True, play="reverse").groups),
                         [[vid(4), vid(3)], [vid(2), vid(1)]])
        self.assertEqual(self.ids(self.plan(everything=True, play="each").groups),
                         [[vid(2), vid(1)], [vid(4), vid(3)]])

    def test_if_short_short_makes_a_last_shorter_compilation_but_not_from_one_clip(self):
        for n in range(1, 6):
            self.add_to_library(n)
        self.assertEqual([len(g) for g in self.plan(everything=True, per=3).groups], [3])
        mp = self.plan(everything=True, per=3, if_short="short")
        self.assertEqual([len(g) for g in mp.groups], [3, 2])
        self.assertTrue(any("shorter" in n for n in mp.plan.notes))
        self.add_to_library(6)
        self.assertEqual([len(g) for g in self.plan(everything=True, per=3, if_short="short").groups], [3, 3])

    def test_by_minutes_a_compilation_closes_when_it_is_long_enough(self):
        for n in range(1, 8):
            self.add_to_library(n)
        one = stitch.probe(template_clip()).duration
        mp = self.plan(everything=True, per_minutes=2.5 * one / 60)
        self.assertEqual([len(g) for g in mp.groups], [3, 3])
        self.assertEqual(len(mp.held), 1)

    def test_a_clip_that_cannot_be_read_is_reported_and_left_out(self):
        for n in range(1, 5):
            self.add_to_library(n)
        (self.ws.clips_dir / f"{vid(2)}.mp4").write_bytes(b"not a video")
        mp = self.plan(everything=True)
        self.assertTrue(mp.plan.ok)
        self.assertTrue(any("cannot be read" in w for w in mp.plan.warnings))
        self.assertEqual(self.ids(mp.groups), [[vid(1), vid(3)]])
        self.assertEqual(self.ids([mp.held]), [[vid(4)]])

    def test_the_plan_changes_nothing(self):
        for n in range(1, 5):
            self.add_to_library(n)
        before = (store.counts(self.ws.conn), sorted(p.name for p in self.ws.root.rglob("*")
                                                       if "cache" not in p.parts))
        self.plan(everything=True)
        self.plan(fetch=dict(clips=3), everything=False)
        after = (store.counts(self.ws.conn), sorted(p.name for p in self.ws.root.rglob("*") if "cache" not in p.parts))
        self.assertEqual(before, after)
        self.assertEqual(store.list_runs(self.ws.conn), [])

    def test_delete_used_is_a_warning_that_needs_a_yes(self):
        for n in range(1, 5):
            self.add_to_library(n)
        mp = self.plan(everything=True, delete_used=True)
        self.assertTrue(any("deleted from the disk" in w for w in mp.plan.warnings))
        self.assertFalse(any("deleted from the disk" in w for w in self.plan(everything=True).plan.warnings))


class PlanWithAChannel(MakeWorld):
    def test_the_downloads_are_one_action_and_the_compilations_count_clips_already_waiting(self):
        self.add_to_library(100)
        self.add_to_library(101)
        mp = self.plan(fetch={}, compilations=2)
        self.assertTrue(mp.plan.ok, mp.plan.errors)
        self.assertEqual([a.kind for a in mp.plan.actions], ["download", "render", "render"])
        self.assertEqual(len(mp.plan.actions[0].data["videos"]), 2)          # 2 waiting + 2 new = 2 compilations
        self.assertEqual(self.ids(mp.groups), [[vid(100), vid(101)], [vid(39), vid(38)]])
        self.assertIn("still to be downloaded", mp.plan.actions[2].text)
        self.assertNotIn("still to be downloaded", mp.plan.actions[1].text)
        self.assertTrue(any("cut again" in n for n in mp.plan.notes))
        self.assertEqual(self.backend.downloads, [])

    def test_clips_gives_a_number_of_new_clips_and_everything_that_fills_a_compilation_is_made(self):
        self.add_to_library(100)
        mp = self.plan(fetch=dict(clips=4))
        self.assertEqual(len(mp.plan.actions[0].data["videos"]), 4)
        self.assertEqual(len(mp.groups), 2)                                   # 5 clips: 2 compilations, 1 left over
        self.assertEqual(len(mp.held), 1)

    def test_n_with_clips_limits_how_many_are_made(self):
        mp = self.plan(fetch=dict(clips=6), compilations=2)
        self.assertEqual(len(mp.groups), 2)
        self.assertEqual(len(mp.plan.actions[0].data["videos"]), 6)

    def test_if_short_fetch_says_how_many_more_will_be_fetched(self):
        mp = self.plan(fetch=dict(clips=3), if_short="fetch")
        self.assertEqual(len(mp.held), 1)
        self.assertTrue(any("1 more clip will be fetched" in n for n in mp.plan.notes), mp.plan.notes)

    def test_per_changes_how_many_n_fetches(self):
        mp = self.plan(fetch={}, compilations=2, per=3)
        self.assertEqual(len(mp.plan.actions[0].data["videos"]), 6)
        self.assertEqual([len(g) for g in mp.groups], [3, 3])

    def test_a_channel_problem_is_the_plans_error(self):
        self.backend.list_error = "HTTP Error 404"
        mp = self.plan(fetch=dict(clips=2))
        self.assertFalse(mp.plan.ok)
        self.assertIn("404", mp.plan.errors[0])
        with self.assertRaises(OpError):
            mk.run_make(self.ws, mp, backend=self.backend)
        self.assertEqual(store.list_runs(self.ws.conn), [])

    def test_a_list_of_videos_is_fetched_and_made(self):
        mp = self.plan(fetch=dict(channel="", videos=[vid(1), vid(2), vid(3), vid(4)]))
        self.assertEqual(self.ids(mp.groups), [[vid(1), vid(2)], [vid(3), vid(4)]])


class RunFromTheLibrary(MakeWorld):
    def test_compilations_are_rendered_recorded_and_their_clips_are_used(self):
        for n in range(1, 5):
            self.add_to_library(n)
        result, mp = self.make(everything=True)
        self.assertEqual((result.status, result.made), ("completed", ["compilation_001", "compilation_002"]))
        for name in result.made:
            self.assertGreater((self.ws.compilations_dir / f"{name}.mp4").stat().st_size, 1000)
        self.assertEqual(self.comp_ids("compilation_001"), [vid(1), vid(2)])
        self.assertEqual(self.comp_ids("compilation_002"), [vid(3), vid(4)])
        self.assertEqual(self.used(), [vid(n) for n in range(1, 5)])
        row = store.get_compilation(self.ws.conn, "compilation_001")
        request = json.loads(row["request"])
        self.assertEqual((request["kind"], request["order"], request["per"]), ("make", "name", 2))
        self.assertEqual(json.loads(row["style_snapshot"])["transition"], "cut")
        run = store.get_run(self.ws.conn, result.run_id)
        self.assertEqual((run["kind"], run["status"]), ("make", "completed"))
        self.assertEqual(self.plan(everything=True).groups, [])             # nothing is made twice

    def test_the_style_it_was_asked_for_is_the_one_recorded(self):
        for n in range(1, 3):
            self.add_to_library(n)
        store.save_style(self.ws.conn, "calm", Style(transition="cut", quality="balanced").to_dict())
        self.ws.conn.commit()
        result, mp = self.make(everything=True, style="calm")
        snap = json.loads(store.get_compilation(self.ws.conn, result.made[0])["style_snapshot"])
        self.assertEqual(snap["quality"], "balanced")
        self.assertEqual(mp.style_name, "calm")

    def test_leftover_is_kept_for_the_next_make(self):
        for n in range(1, 4):
            self.add_to_library(n)
        result, _ = self.make(everything=True)
        self.assertEqual((result.made, result.counts["left_over"]), (["compilation_001"], 1))
        self.add_to_library(4)
        result, _ = self.make(everything=True)
        self.assertEqual(self.comp_ids(result.made[0]), [vid(3), vid(4)])

    def test_if_short_short_makes_the_shorter_last_compilation(self):
        for n in range(1, 6):
            self.add_to_library(n)
        result, _ = self.make(everything=True, per=3, if_short="short")
        self.assertEqual([len(self.comp_ids(n)) for n in result.made], [3, 2])
        self.assertEqual(result.counts["left_over"], 0)

    def test_an_unreadable_clip_is_marked_failed_and_the_rest_is_made(self):
        for n in range(1, 6):
            self.add_to_library(n)
        (self.ws.clips_dir / f"{vid(2)}.mp4").write_bytes(b"not a video")
        result, _ = self.make(everything=True)
        self.assertEqual(self.comp_ids(result.made[0]), [vid(1), vid(3)])
        self.assertEqual(self.clip_status(2), "failed")
        self.assertEqual(result.counts["unreadable"], 1)
        self.assertTrue(any(i.status == "skipped" and "cannot be read" in i.detail for i in result.items))
        self.assertTrue(any("could not be read earlier" in n for n in self.plan(everything=True).plan.notes))

    def test_retry_failed_tries_a_failed_clip_again_and_clears_the_mark_when_it_works(self):
        self.add_to_library(1)
        self.add_to_library(2)
        store.upsert_clip(self.ws.conn, vid(1), store.get_clip(self.ws.conn, vid(1))["path"], "failed")
        self.ws.conn.commit()
        self.assertTrue(self.plan(everything=True).nothing_to_do)
        result, _ = self.make(everything=True, retry_failed=True)
        self.assertEqual(self.comp_ids(result.made[0]), [vid(1), vid(2)])
        self.assertEqual(self.clip_status(1), "ready")

    def test_one_failed_render_does_not_stop_the_rest_and_its_clips_stay_unused(self):
        for n in range(1, 5):
            self.add_to_library(n)
        real = stitch.render
        calls = []

        def flaky(spec, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise EngineError("ffmpeg failed: disk full")
            return real(spec, **kw)

        with mock.patch.object(mk.stitch, "render", flaky):
            result, _ = self.make(everything=True)
        self.assertEqual(result.status, "partial")
        self.assertEqual(self.comp_ids(result.made[0]), [vid(3), vid(4)])
        self.assertEqual(self.used(), [vid(3), vid(4)])
        self.assertTrue(any(i.status == "failed" and "disk full" in i.detail for i in result.items))

    def test_ctrl_c_during_a_render_keeps_what_was_finished_and_deletes_nothing(self):
        for n in range(1, 5):
            self.add_to_library(n)
        real = stitch.render
        calls = []

        def interrupted(spec, **kw):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(spec, **kw)

        with mock.patch.object(mk.stitch, "render", interrupted):
            result, _ = self.make(everything=True, delete_used=True)
        self.assertEqual((result.status, result.made), ("cancelled", ["compilation_001"]))
        self.assertEqual(self.used(), [vid(1), vid(2)])
        self.assertTrue((self.ws.clips_dir / f"{vid(1)}.mp4").exists())       # not deleted: the run was cancelled
        self.assertEqual([i.status for i in result.items if i.what.startswith("compilation ")], ["cancelled"])
        self.assertEqual(store.get_run(self.ws.conn, result.run_id)["status"], "cancelled")

    def test_the_recorded_request_can_be_repeated_with_like(self):
        for n in range(1, 5):
            self.add_to_library(n)
        self.make(compilations=1, order="newest", per=2)
        like = mk.request_like(self.ws, "1")
        self.assertEqual((like.compilations, like.order, like.per, like.seed, like.style),
                         (1, "newest", 2, 0, "default"))
        self.assertEqual(self.ids(mk.plan_make(self.ws, like, self.backend).groups), [[vid(3), vid(4)]])
        self.assertEqual(mk.request_like(self.ws, "last").compilations, 1)

    def test_like_refuses_what_has_no_make_request(self):
        store.add_compilation(self.ws.conn, "compilation_900", clips=[], imported=True)
        store.add_compilation(self.ws.conn, "compilation_901", clips=[], request={"kind": "remake"})
        self.ws.conn.commit()
        with self.assertRaises(OpError) as cm:
            mk.request_like(self.ws, "900")
        self.assertIn("imported", str(cm.exception))
        with self.assertRaises(OpError) as cm:
            mk.request_like(self.ws, "901")
        self.assertIn("not made by `ytt make`", str(cm.exception))
        with self.assertRaises(OpError):
            mk.request_like(self.ws, "all")


class RunWithAChannel(MakeWorld):
    def test_fetch_then_make_in_one_run(self):
        result, mp = self.make(fetch={}, compilations=2)
        self.assertEqual((result.status, result.counts["downloaded"]), ("completed", 4))
        self.assertEqual(result.made, ["compilation_001", "compilation_002"])
        self.assertEqual(self.comp_ids("compilation_001"), [vid(39), vid(38)])
        self.assertEqual(self.comp_ids("compilation_002"), [vid(37), vid(36)])
        self.assertEqual(store.get_clip(self.ws.conn, vid(39))["origin"], "fetched")
        self.assertEqual([r["kind"] for r in store.list_runs(self.ws.conn)], ["make"])         # one run, not two

    def test_a_failed_download_is_replaced_and_the_make_still_completes(self):
        self.backend.fail = {vid(38)}
        result, _ = self.make(fetch={}, compilations=2)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.counts["failed"], 1)
        self.assertEqual(self.comp_ids("compilation_001"), [vid(39), vid(37)])
        self.assertEqual(self.comp_ids("compilation_002"), [vid(36), vid(35)])

    def test_when_every_download_fails_nothing_is_made_and_the_run_failed(self):
        self.backend.fail = {vid(i) for i in range(40)}
        result, _ = self.make(fetch={}, compilations=1)
        self.assertEqual((result.status, result.made), ("failed", []))

    def test_fewer_downloads_than_promised_is_a_partial_run_and_the_rest_is_still_made(self):
        self.backend = FakeBackend(make_videos(3))
        self.backend.fail = {vid(1)}
        result, _ = self.make(fetch={}, compilations=2)
        self.assertEqual((result.status, result.counts["downloaded"]), ("partial", 2))
        self.assertEqual(self.comp_ids(result.made[0]), [vid(2), vid(0)])

    def test_only_a_few_new_videos_is_not_a_failure_the_plan_said_so(self):
        self.backend = FakeBackend(make_videos(3))
        result, _ = self.make(fetch={}, compilations=2)
        self.assertEqual((result.status, len(result.made), result.counts["left_over"]), ("completed", 1, 1))

    def test_if_short_fetch_fetches_just_enough_more_to_fill_one(self):
        self.add_to_library(100)
        self.add_to_library(101)
        result, _ = self.make(fetch=dict(clips=1), if_short="fetch")
        self.assertEqual((result.status, result.counts["downloaded"], result.counts["left_over"]), ("completed", 2, 0))
        self.assertEqual([self.comp_ids(n) for n in result.made], [[vid(100), vid(101)], [vid(39), vid(38)]])

    def test_if_short_fetch_with_nothing_more_to_fetch_keeps_the_leftover(self):
        self.backend = FakeBackend(make_videos(3))
        result, _ = self.make(fetch=dict(clips=3), if_short="fetch")
        self.assertEqual((len(result.made), result.counts["left_over"]), (1, 1))
        self.assertTrue(any(i.what == "filling the last compilation" for i in result.items))

    def test_ctrl_c_while_downloading_keeps_the_finished_downloads_and_makes_nothing(self):
        self.backend.interrupt = {vid(38)}
        result, _ = self.make(fetch={}, compilations=2)
        self.assertEqual((result.status, result.made), ("cancelled", []))
        self.assertEqual(self.clips(), [vid(39)])
        self.assertEqual(store.get_run(self.ws.conn, result.run_id)["status"], "cancelled")

    def test_delete_used_removes_only_what_ytt_downloaded(self):
        self.add_to_library(100, origin="found")
        self.add_to_library(101, origin="fetched")
        result, _ = self.make(everything=True, delete_used=True)
        self.assertEqual((len(result.made), result.counts["deleted"]), (1, 1))
        self.assertTrue((self.ws.clips_dir / f"{vid(100)}.mp4").exists())
        self.assertFalse((self.ws.clips_dir / f"{vid(101)}.mp4").exists())
        self.assertEqual((self.clip_status(100), self.clip_status(101)), ("ready", "missing"))

    def test_like_repeats_the_request_on_fresh_clips_and_works_through_a_remake(self):
        first, _ = self.make(fetch=dict(clips=2))
        self.assertEqual(self.comp_ids(first.made[0]), [vid(39), vid(38)])
        like = mk.request_like(self.ws, "1")
        self.assertEqual((like.fetch.channel, like.fetch.clips, like.fetch.sort), ("@Chan", 2, "oldest"))
        mp = mk.plan_make(self.ws, like, self.backend)
        again = mk.run_make(self.ws, mp, backend=self.backend)
        self.assertEqual(self.comp_ids(again.made[0]), [vid(37), vid(36)])
        remade = rm.run_remake(self.ws, rm.plan_remake(self.ws, rm.RemakeRequest(targets="1")))
        child = remade.made[0]
        self.assertEqual(mk.request_like(self.ws, child).fetch.clips, 2)


if __name__ == "__main__":
    unittest.main()
