"""
fetch end to end against the fake YouTube (no network): what the plan says, what a run records, what survives a
failure or Ctrl-C. The library state is checked through the same store functions the rest of ytt uses.
"""
import json
import shutil
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

from ytt.ops.compile import fetch
from ytt.ops.compile.fetch import FetchRequest
from ytt.ops.compile.style import Style
from ytt.ops.errors import OpError
from ytt.ops.library import views
from ytt.workspace import store
from ytt.workspace.workspace import Workspace

try:
    from fakes import FakeBackend, make_videos, template_clip
    from test_auto_compile import HAVE_TOOLS
except ImportError:
    from tests.fakes import FakeBackend, make_videos, template_clip
    from tests.test_auto_compile import HAVE_TOOLS


def vid(n):
    return f"vid{n:08d}"


@unittest.skipUnless(HAVE_TOOLS, "needs ffmpeg")
class FetchWorld(unittest.TestCase):
    """A fresh workspace and a fake channel of 40 shorts, one per day, newest = vid00000000. Sorted 'oldest' the
    first in line is vid00000039."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytt_fetch_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ws = Workspace.init(self.tmp / "ws")
        self.addCleanup(self.ws.close)
        self.ws.config["make"]["clips_each"] = 4
        self.backend = FakeBackend()

    # ---- helpers
    def req(self, **kw):
        base = dict(channel="@Chan", type="shorts", sort="oldest")
        base.update(kw)
        return FetchRequest(**base)

    def plan(self, **kw):
        return fetch.plan_fetch(self.ws, self.req(**kw), self.backend)

    def fetch(self, **kw):
        fp = self.plan(**kw)
        self.assertTrue(fp.plan.ok, fp.plan.errors)
        return fetch.run_fetch(self.ws, fp, self.backend)

    def clips(self):
        """youtube ids of the library's clips, in the order they were recorded"""
        return [c["youtube_id"] for c in store.list_clips(self.ws.conn)]

    def files(self, folder=None):
        folder = folder or self.ws.clips_dir
        return sorted(p.name for p in Path(folder).rglob("*") if p.is_file())

    def put_clip(self, folder, name):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy(template_clip(), folder / name)
        return folder / name

    def add_to_library(self, n, status="ready", origin="fetched", title=None, duration=20):
        """a clip the library already has (and its file when ready)"""
        v = vid(n)
        store.upsert_video(self.ws.conn, v, title=title or f"Clip {n}", duration=duration)
        path = self.put_clip(self.ws.clips_dir, f"{v}.mp4") if status == "ready" else self.ws.clips_dir / f"{v}.mp4"
        store.upsert_clip(self.ws.conn, v, self.ws.to_stored(path), status, origin)
        self.ws.conn.commit()


class RequestChecks(FetchWorld):
    def test_a_channel_is_needed_unless_a_list_is_given(self):
        with self.assertRaises(OpError) as cm:
            self.plan(channel="", clips=3)
        self.assertIn("which channel", str(cm.exception))

    def test_a_count_is_needed(self):
        with self.assertRaises(OpError) as cm:
            self.plan()
        self.assertIn("How many?", str(cm.exception))

    def test_only_one_way_of_saying_how_many(self):
        with self.assertRaises(OpError) as cm:
            self.plan(clips=3, range="1-5")
        self.assertIn("only one of", str(cm.exception))

    def test_a_list_of_videos_excludes_channel_and_selection(self):
        for kw in (dict(channel="@Chan"), dict(channel="", clips=3), dict(channel="", date_from="2026-01-01")):
            with self.assertRaises(OpError, msg=str(kw)):
                fetch.plan_fetch(self.ws, FetchRequest(videos=[vid(1)], **kw), self.backend)
        with self.assertRaises(OpError):
            fetch.plan_fetch(self.ws, FetchRequest(videos=[]), self.backend)

    def test_bad_values_are_clear_errors(self):
        for kw in (dict(clips=3, type="live"), dict(clips=3, sort="best"), dict(clips=3, date_from="soon"),
                   dict(clips=3, date_from="2026-09-10", date_to="2026-09-01"), dict(range="60"),
                   dict(take="last:0"), dict(clips=-1), dict(clips=3, workers=-2)):
            with self.assertRaises(OpError, msg=str(kw)):
                self.plan(**kw)

    def test_an_unreadable_channel_is_a_plan_error_not_a_crash(self):
        self.backend.list_error = "HTTP Error 404"
        fp = self.plan(clips=3)
        self.assertFalse(fp.plan.ok)
        self.assertIn("404", fp.plan.errors[0])

    def test_an_empty_channel_is_a_plan_error(self):
        self.backend.videos["shorts"] = []
        fp = self.plan(clips=3)
        self.assertIn("No videos found", fp.plan.errors[0])

    def test_run_refuses_a_plan_with_errors(self):
        self.backend.list_error = "boom"
        with self.assertRaises(OpError):
            fetch.run_fetch(self.ws, self.plan(clips=3), self.backend)
        self.assertEqual(store.list_runs(self.ws.conn), [])


class PlanningChangesNothing(FetchWorld):
    def test_a_plan_writes_nothing_anywhere(self):
        self.add_to_library(5)
        counts, files = self.ws.counts(), self.files()
        fp = self.plan(clips=6)
        self.assertEqual(fp.target, 6)
        self.assertEqual((self.ws.counts(), self.files(), store.list_runs(self.ws.conn)), (counts, files, []))
        self.assertEqual(self.backend.started, [])

    def test_the_plan_lists_each_download_in_the_chosen_order(self):
        fp = self.plan(clips=3)
        downloads = [a for a in fp.plan.actions if a.kind == "download"]
        self.assertEqual([a.data["video"] for a in downloads], [vid(39), vid(38), vid(37)])
        self.assertIn("Clip 39", downloads[0].text)
        self.assertIn("3 clips to download", fp.plan.title)

    def test_the_plan_can_be_turned_into_json(self):
        json.dumps(self.plan(clips=3).plan.to_dict())


class FetchNewClips(FetchWorld):
    def test_clips_n_gets_n_clips_and_records_everything(self):
        result = self.fetch(clips=8)
        self.assertEqual((result.status, result.counts["downloaded"]), ("completed", 8))
        self.assertEqual(self.clips(), [vid(i) for i in range(39, 31, -1)])
        self.assertEqual(self.files(), sorted(f"{vid(i)}.mp4" for i in range(32, 40)))
        v = store.get_video(self.ws.conn, vid(39))
        self.assertEqual((v["title"], v["views"], v["duration"], v["published"]), ("Clip 39", 1039, 20, "2026-08-22"))
        clip = store.get_clip(self.ws.conn, vid(39))
        self.assertEqual((clip["status"], clip["origin"], clip["path"]), ("ready", "fetched", f"clips/{vid(39)}.mp4"))
        self.assertEqual(store.list_sources(self.ws.conn), ["@Chan"])
        run = views.runs(self.ws)[0]
        self.assertEqual((run["kind"], run["status"]), ("fetch", "completed"))

    def test_the_default_order_is_most_viewed_first(self):
        self.fetch(clips=3, sort="popular")
        self.assertEqual(self.clips(), [vid(39), vid(38), vid(37)])             # views grow with the number
        self.fetch(clips=2, sort="latest")
        self.assertEqual(self.clips()[3:], [vid(0), vid(1)])

    def test_fetch_is_idempotent_a_second_run_gets_different_clips(self):
        self.fetch(clips=4)
        self.fetch(clips=4)
        self.assertEqual(len(set(self.clips())), 8)
        self.assertEqual(self.backend.downloads.count(vid(39)), 1)

    def test_clips_already_waiting_in_the_library_are_not_counted_against_clips_n(self):
        for n in (39, 38, 37):
            self.add_to_library(n)
        self.fetch(clips=4)
        self.assertEqual(len(self.backend.downloads), 4)
        self.assertEqual(sorted(self.backend.downloads), [vid(i) for i in (33, 34, 35, 36)])

    def test_a_clip_whose_file_was_deleted_is_still_in_the_library_and_not_new(self):
        self.add_to_library(39, status="missing")
        self.fetch(clips=2)
        self.assertEqual(sorted(self.backend.downloads), [vid(37), vid(38)])

    def test_a_failed_download_is_replaced_by_the_next_in_line_and_reported(self):
        self.backend.fail = {vid(38)}
        result = self.fetch(clips=4)
        self.assertEqual(result.status, "completed")                              # 4 clips arrived
        self.assertEqual(self.clips(), [vid(39), vid(37), vid(36), vid(35)])
        failed = [i for i in result.items if i.status == "failed"]
        self.assertEqual([i.what for i in failed], [vid(38)])
        self.assertIn("403", failed[0].detail)
        self.assertEqual(self.files(), sorted(f"{vid(i)}.mp4" for i in (39, 37, 36, 35)))   # no junk left behind
        self.assertIsNone(store.get_clip(self.ws.conn, vid(38)))

    def test_when_the_channel_has_fewer_new_videos_you_get_what_exists_and_the_plan_says_so(self):
        self.backend.videos["shorts"] = make_videos(3)
        fp = self.plan(clips=10)
        self.assertEqual(fp.target, 3)
        self.assertTrue(any("Only 3 new videos" in n for n in fp.plan.notes), fp.plan.notes)
        self.assertEqual(self.fetch(clips=10).status, "completed")

    def test_everything_failing_is_a_failed_run_and_some_failing_without_enough_replacements_is_partial(self):
        self.backend.videos["shorts"] = make_videos(4)
        self.backend.fail = {vid(0), vid(1), vid(2), vid(3)}
        self.assertEqual(self.fetch(clips=3).status, "failed")
        self.backend.fail = {vid(0), vid(1)}
        self.assertEqual(self.fetch(clips=3).status, "partial")                    # 3 wanted, only 2 could arrive

    def test_lookalike_reuploads_are_dropped_before_counting_so_you_still_get_n(self):
        for n in (39, 38, 37):
            self.backend.videos["shorts"][n].title = "Same Clip!"
        fp = self.plan(clips=4)
        self.assertEqual([j.video_id for j in fp.jobs[:fp.target]], [vid(39), vid(36), vid(35), vid(34)])
        self.assertTrue(any("2 look-alike re-uploads" in n for n in fp.plan.notes))

    def test_a_lookalike_of_a_clip_you_have_is_skipped_unless_keep_duplicates(self):
        store.upsert_video(self.ws.conn, "someOtherId1", title="clip 39", duration=20)       # a re-upload of row 39
        store.upsert_clip(self.ws.conn, "someOtherId1", "clips/gone.mp4", "missing")
        self.ws.conn.commit()
        self.assertNotIn(vid(39), [j.video_id for j in self.plan(clips=2).jobs])
        self.assertIn(vid(39), [j.video_id for j in self.plan(clips=2, keep_duplicates=True).jobs])

    def test_titles_without_a_length_are_never_called_duplicates(self):
        self.backend.videos["shorts"] = make_videos(10, title="Same", duration=None)
        self.assertEqual(self.plan(clips=3).target, 3)

    def test_the_source_is_recorded_as_the_handle_even_when_a_link_was_given(self):
        self.fetch(clips=1, channel="https://www.youtube.com/@Chan/shorts")
        self.assertEqual(store.list_sources(self.ws.conn), ["@Chan"])
        self.assertEqual(self.backend.listed, [("https://www.youtube.com/@Chan", "shorts")])


class FetchEnoughForCompilations(FetchWorld):
    def test_n_is_the_number_of_full_compilations_not_clips(self):
        self.fetch(compilations=2)                                                  # 2 x 4 clips, nothing waiting
        self.assertEqual(len(self.clips()), 8)

    def test_clips_already_waiting_are_counted(self):
        for n in (39, 38, 37):
            self.add_to_library(n)
        fp = self.plan(compilations=2)
        self.assertEqual(fp.target, 5)                                              # 8 needed - 3 waiting
        self.assertTrue(any("3 already waiting" in n for n in fp.plan.notes))

    def test_used_clips_are_not_waiting(self):
        for n in (39, 38, 37, 36):
            self.add_to_library(n)
        ids = [store.get_clip(self.ws.conn, vid(n))["id"] for n in (39, 38)]
        store.add_compilation(self.ws.conn, "compilation_001", clips=[(i, None) for i in ids])
        self.ws.conn.commit()
        self.assertEqual(self.plan(compilations=1).target, 2)                       # 4 needed - 2 still waiting

    def test_a_clip_whose_file_is_gone_is_not_waiting(self):
        self.add_to_library(39)
        self.add_to_library(38, status="missing")
        self.assertEqual(self.plan(compilations=1).target, 3)

    def test_nothing_to_download_when_enough_are_waiting(self):
        for n in range(39, 29, -1):
            self.add_to_library(n)
        fp = self.plan(compilations=2)
        self.assertEqual((fp.target, fp.jobs), (0, []))
        self.assertTrue(any("Already enough" in n for n in fp.plan.notes))
        self.assertIn("nothing to download", fp.plan.title)

    def test_limited_by_what_the_channel_has(self):
        self.backend.videos["shorts"] = make_videos(6)
        fp = self.plan(compilations=2)
        self.assertEqual(fp.target, 6)
        self.assertTrue(any("Only 6 new videos" in n and "1 full compilation at most" in n for n in fp.plan.notes))

    def test_by_minutes_the_size_is_estimated_from_clip_lengths(self):
        self.ws.config["make"].update(size_mode="minutes", minutes_each=1)          # 60 s of 20 s clips = 3 clips
        fp = self.plan(compilations=2)
        self.assertEqual(fp.target, 6)
        self.assertTrue(any("about 3 clips" in n for n in fp.plan.notes))

    def test_take_comps_is_the_same_as_n(self):
        self.assertEqual(self.plan(take="comps:2").target, 8)


class FetchByPosition(FetchWorld):
    def test_range_is_exactly_those_positions_of_the_sorted_list(self):
        fp = self.plan(range="3-5")
        self.assertEqual([j.video_id for j in fp.jobs], [vid(37), vid(36), vid(35)])
        self.assertEqual([j.position for j in fp.jobs], [3, 4, 5])
        self.fetch(range="3-5")
        self.assertEqual(self.clips(), [vid(37), vid(36), vid(35)])

    def test_the_same_range_again_finds_nothing_new_and_a_wider_one_only_the_rest(self):
        self.fetch(range="3-5")
        again = self.plan(range="3-5")
        self.assertEqual((again.target, again.plan.ok), (0, True))
        self.assertTrue(any("Nothing new" in n for n in again.plan.notes))
        wider = self.plan(range="1-6")
        self.assertEqual([j.video_id for j in wider.jobs], [vid(39), vid(38), vid(34)])

    def test_a_failed_download_is_not_replaced_when_you_asked_for_exact_positions(self):
        self.backend.fail = {vid(36)}
        result = self.fetch(range="3-5")
        self.assertEqual(result.status, "partial")
        self.assertEqual(self.clips(), [vid(37), vid(35)])

    def test_a_range_beyond_the_list_is_an_error(self):
        fp = self.plan(range="50-60")
        self.assertFalse(fp.plan.ok)
        self.assertIn("doesn't match anything", fp.plan.errors[0])

    def test_take_last_every_and_new(self):
        self.assertEqual([j.video_id for j in self.plan(take="last:3").jobs], [vid(2), vid(1), vid(0)])
        self.assertEqual([j.video_id for j in self.plan(take="every:10").jobs], [vid(i) for i in (39, 29, 19, 9)])
        self.add_to_library(39)
        self.assertEqual([j.video_id for j in self.plan(take="new:2").jobs[:2]], [vid(38), vid(37)])

    def test_random_take_is_never_more_than_asked(self):
        self.assertEqual(self.plan(take="random:5").target, 5)


class FetchFilters(FetchWorld):
    def test_dates(self):
        fp = self.plan(clips=10, date_from="2026-09-25", date_to="2026-09-27")
        self.assertEqual([j.video_id for j in fp.jobs[:fp.target]], [vid(5), vid(4), vid(3)])
        self.assertTrue(any("Only 3 new videos" in n and "narrowed" in n for n in fp.plan.notes))

    def test_a_date_range_that_cuts_nothing_off_adds_no_misleading_note(self):
        fp = self.plan(clips=100, date_from="2020-01-01")
        self.assertFalse(any("narrowed" in n for n in fp.plan.notes))

    def test_views_and_length_limits(self):
        self.assertEqual(self.plan(clips=10, min_views=1037).target, 3)
        self.backend.videos["shorts"][0].duration = 400
        self.assertEqual(self.plan(clips=100, max_length=2).target, 39)

    def test_nothing_matching_the_limits_is_an_error(self):
        self.assertIn("No videos match", self.plan(clips=3, min_views=10**9).plan.errors[0])

    def test_rough_dates_are_checked_exactly_only_where_needed(self):
        for v in self.backend.videos["shorts"]:
            v.approx = True
        fp = self.plan(clips=100, date_from="2026-09-25", date_to="2026-09-27")
        self.assertEqual(fp.target, 3)
        self.assertGreater(len(self.backend.probes), 0)
        self.assertLess(len(self.backend.probes), 20)


class FoundOnDisk(FetchWorld):
    def test_videos_you_already_have_are_added_to_the_library_not_downloaded_again(self):
        other = self.tmp / "elsewhere"
        self.ws.config["watch_folders"] = [str(other)]
        self.put_clip(self.ws.clips_dir, "20260101-0000_0001_vid00000039.mp4")      # the old tool's naming
        self.put_clip(self.ws.clips_dir, "Some Title [vid00000038].mp4")            # yt-dlp's naming
        self.put_clip(other, "mine_vid00000037.mp4")                                # in a watch folder
        fp = self.plan(clips=5)
        self.assertEqual(len(fp.adopt), 3)
        self.assertEqual([j.video_id for j in fp.jobs[:5]], [vid(i) for i in range(36, 31, -1)])
        self.assertTrue(any("other folders" in w for w in fp.plan.warnings), fp.plan.warnings)
        result = fetch.run_fetch(self.ws, fp, self.backend)
        self.assertEqual((result.counts["adopted"], result.counts["downloaded"]), (3, 5))
        origin = {c["youtube_id"]: c["origin"] for c in store.list_clips(self.ws.conn)}
        self.assertEqual({k: origin[k] for k in (vid(39), vid(38), vid(37))}, {vid(39): "found", vid(38): "found", vid(37): "found"})
        self.assertEqual(origin[vid(36)], "fetched")
        self.assertEqual(store.get_clip(self.ws.conn, vid(38))["path"], "clips/Some Title [vid00000038].mp4")
        self.assertEqual(store.get_clip(self.ws.conn, vid(37))["path"], str(other / "mine_vid00000037.mp4"))
        self.assertNotIn(vid(39), self.backend.started)                              # never downloaded
        self.assertEqual(self.plan(clips=1).adopt, [])                               # and not found a second time

    def test_unfinished_downloads_and_hidden_files_are_not_mistaken_for_clips(self):
        self.put_clip(self.ws.clips_dir, "vid00000039.f137.mp4")
        (self.ws.clips_dir / "vid00000038.mp4.part").write_bytes(b"x")
        self.put_clip(self.ws.clips_dir / ".partial", "vid00000037.mp4")
        self.assertEqual(self.plan(clips=3).adopt, [])

    def test_refetch_ignores_what_you_have_and_replaces_the_file(self):
        self.fetch(range="1-2")
        path = self.ws.clips_dir / f"{vid(39)}.mp4"
        path.write_bytes(b"damaged")
        self.assertEqual(self.plan(range="1-2").target, 0)
        fp = self.plan(range="1-2", refetch=True)
        self.assertEqual(fp.target, 2)
        self.assertTrue(any("--refetch" in w for w in fp.plan.warnings))
        fetch.run_fetch(self.ws, fp, self.backend)
        self.assertEqual(path.stat().st_size, template_clip().stat().st_size)
        self.assertEqual(len(self.clips()), 2)                                       # same two clips, not four

    def test_a_found_file_is_marked_found_and_a_refetch_makes_it_fetched(self):
        self.put_clip(self.ws.clips_dir, f"{vid(39)}.mp4")
        fetch.run_fetch(self.ws, self.plan(clips=1), self.backend)
        self.assertEqual(store.get_clip(self.ws.conn, vid(39))["origin"], "found")
        fetch.run_fetch(self.ws, self.plan(range="1-1", refetch=True), self.backend)
        self.assertEqual(store.get_clip(self.ws.conn, vid(39))["origin"], "fetched")


class FetchFromAList(FetchWorld):
    def test_only_what_is_missing_is_fetched_and_nothing_replaces_a_failure(self):
        self.add_to_library(1)
        self.backend.fail = {vid(3)}
        request = FetchRequest(videos=[vid(1), vid(2), vid(3), vid(4)])
        fp = fetch.plan_fetch(self.ws, request, self.backend)
        self.assertEqual([j.video_id for j in fp.jobs], [vid(2), vid(3), vid(4)])
        self.assertEqual(self.backend.listed, [])                                     # no channel was read
        result = fetch.run_fetch(self.ws, fp, self.backend)
        self.assertEqual(result.status, "partial")
        self.assertEqual(sorted(self.clips()), [vid(1), vid(2), vid(4)])
        self.assertEqual(store.get_video(self.ws.conn, vid(2))["title"], "Clip 2")    # learned from the download
        self.assertEqual(store.list_sources(self.ws.conn), [])

    def test_a_list_where_everything_is_here_already_has_nothing_to_do(self):
        self.add_to_library(1)
        fp = fetch.plan_fetch(self.ws, FetchRequest(videos=[vid(1)]), self.backend)
        self.assertEqual((fp.target, fp.plan.ok), (0, True))


class RunBehaviour(FetchWorld):
    def test_clips_are_recorded_in_the_chosen_order_even_if_the_first_finishes_last(self):
        self.backend.slow = {vid(39): 0.4}
        self.fetch(clips=6, workers=3)
        self.assertEqual(self.clips(), [vid(i) for i in range(39, 33, -1)])

    def test_never_more_than_asked_with_several_workers(self):
        self.fetch(clips=5, workers=4)
        self.assertEqual(len(self.backend.started), 5)

    def test_workers_come_from_the_workspace_unless_the_request_says_otherwise(self):
        self.ws.config["workers"] = 2
        self.assertEqual(self.plan(clips=1).workers, 2)
        self.assertEqual(self.plan(clips=1, workers=5).workers, 5)

    def test_ctrl_c_keeps_what_finished_and_the_run_ends_as_cancelled(self):
        self.backend.interrupt = {vid(36)}
        result = self.fetch(clips=6, workers=1)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(self.clips(), [vid(39), vid(38), vid(37)])
        self.assertEqual(self.files(), sorted(f"{vid(i)}.mp4" for i in (39, 38, 37)))
        self.assertEqual(views.runs(self.ws)[0]["status"], "cancelled")
        self.backend.interrupt = set()
        self.assertEqual(self.fetch(clips=3).status, "completed")                   # and the next run just carries on
        self.assertEqual(len(self.clips()), 6)

    def test_ctrl_c_with_several_workers_leaves_no_half_files(self):
        self.backend.slow = {vid(38): 3, vid(37): 3}
        self.backend.interrupt = {vid(36)}
        result = self.fetch(clips=8, workers=4)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(self.files(), sorted(f"{v}.mp4" for v in self.clips()))     # every file on disk is recorded

    def test_the_download_quality_cap_comes_from_the_default_style(self):
        store.save_style(self.ws.conn, "default", Style(max_height=720).to_dict())
        self.ws.conn.commit()
        self.fetch(clips=2)
        self.assertEqual(self.backend.heights, [720, 720])

    def test_max_height_on_the_request_wins(self):
        store.save_style(self.ws.conn, "default", Style(max_height=720).to_dict())
        self.fetch(clips=1, max_height=480)
        self.assertEqual(self.backend.heights, [480])

    def test_a_low_free_disk_is_a_warning_not_an_error(self):
        usage = namedtuple("usage", "total used free")
        with mock.patch.object(fetch.shutil, "disk_usage", lambda p: usage(100, 90, 10)):
            fp = self.plan(clips=3)
        self.assertTrue(fp.plan.ok)
        self.assertTrue(any("disk space" in w for w in fp.plan.warnings))

    def test_no_run_is_made_by_planning_but_every_real_run_is_recorded(self):
        self.plan(clips=2)
        self.assertEqual(views.runs(self.ws), [])
        self.fetch(clips=2)
        self.fetch(clips=2)
        self.assertEqual([r["kind"] for r in views.runs(self.ws)], ["fetch", "fetch"])


class FetchAgain(FetchWorld):
    """The part `remake` uses: get clips whose files are gone, under their recorded names."""

    def setUp(self):
        super().setUp()
        self.fetch(clips=3)
        self.rows = {c["youtube_id"]: c for c in store.list_clips(self.ws.conn)}

    def wanted(self, ids):
        return [(i, i, store.get_clip(self.ws.conn, i)) for i in ids]

    def test_files_come_back_under_the_same_names(self):
        for i in (vid(39), vid(38)):
            (self.ws.clips_dir / f"{i}.mp4").unlink()
        result = fetch.fetch_again(self.ws, self.backend, self.wanted([vid(39), vid(38)]), 1080, 2)
        self.assertEqual(result, {vid(39): None, vid(38): None})
        self.assertEqual(self.files(), sorted(f"{vid(i)}.mp4" for i in (39, 38, 37)))

    def test_an_old_style_name_is_kept(self):
        old = self.ws.clips_dir / "20260101-0000_0001_vid00000039.mp4"
        (self.ws.clips_dir / f"{vid(39)}.mp4").rename(old)
        store.upsert_clip(self.ws.conn, vid(39), self.ws.to_stored(old), "missing")
        old.unlink()
        fetch.fetch_again(self.ws, self.backend, self.wanted([vid(39)]), 1080, 1)
        self.assertTrue(old.is_file())
        self.assertEqual(store.get_clip(self.ws.conn, vid(39))["status"], "ready")

    def test_a_clip_recorded_outside_the_clips_folder_comes_back_into_the_clips_folder(self):
        elsewhere = self.tmp / "elsewhere" / "mine.mp4"
        store.upsert_clip(self.ws.conn, vid(39), str(elsewhere), "missing")
        fetch.fetch_again(self.ws, self.backend, self.wanted([vid(39)]), 1080, 1)
        self.assertFalse(elsewhere.exists())
        self.assertEqual(store.get_clip(self.ws.conn, vid(39))["path"], f"clips/{vid(39)}.mp4")

    def test_failures_are_reported_per_clip_and_the_others_still_arrive(self):
        for i in (vid(39), vid(38)):
            (self.ws.clips_dir / f"{i}.mp4").unlink()
        self.backend.fail = {vid(38)}
        result = fetch.fetch_again(self.ws, self.backend, self.wanted([vid(39), vid(38)]), 1080, 2)
        self.assertIsNone(result[vid(39)])
        self.assertIn("403", result[vid(38)])
        self.assertEqual(self.files(), sorted(f"{vid(i)}.mp4" for i in (39, 37)))


class Handles(unittest.TestCase):
    def test_canonical_handle(self):
        c = fetch.canonical_handle
        self.assertEqual(c("@Chan"), "@Chan")
        self.assertEqual(c(" https://www.youtube.com/@Chan/shorts/ "), "@Chan")
        self.assertEqual(c("https://www.youtube.com/channel/UCabc/videos"), "https://www.youtube.com/channel/UCabc")


if __name__ == "__main__":
    unittest.main()
