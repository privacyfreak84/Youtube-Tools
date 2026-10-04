"""The sources layer: channel addresses, selection (dates, limits, order, positions, re-uploads), files on disk and
the parallel downloader. Everything runs against the fake YouTube; ffmpeg is only used to make the one tiny clip."""
import random
import shutil
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path

from ytt.sources import channel as ch
from ytt.sources import files, selection as sel
from ytt.sources.downloader import Job, download_all
from ytt.sources.errors import DownloadError, DownloadStopped
from ytt.sources.models import VideoInfo

try:
    from fakes import FakeBackend, make_videos
    from test_auto_compile import HAVE_TOOLS
except ImportError:
    from tests.fakes import FakeBackend, make_videos
    from tests.test_auto_compile import HAVE_TOOLS


class ChannelTests(unittest.TestCase):
    def test_channel_addresses(self):
        self.assertEqual(ch.base_channel_url("@Chan"), "https://www.youtube.com/@Chan")
        self.assertEqual(ch.base_channel_url(" @Chan/ "), "https://www.youtube.com/@Chan")
        self.assertEqual(ch.base_channel_url("https://www.youtube.com/@Chan/shorts"), "https://www.youtube.com/@Chan")
        self.assertEqual(ch.base_channel_url("https://www.youtube.com/channel/UCabc/videos/"),
                         "https://www.youtube.com/channel/UCabc")

    def test_video_ids_from_ids_and_links(self):
        self.assertEqual(ch.video_id("dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(ch.video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=5"), "dQw4w9WgXcQ")
        self.assertEqual(ch.video_id("https://youtu.be/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(ch.video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertIsNone(ch.video_id("not a video"))
        self.assertIsNone(ch.video_id("https://example.com/"))

    def test_a_list_of_ids_skips_blanks_comments_and_repeats(self):
        text = "# my list\n\ndQw4w9WgXcQ\nhttps://youtu.be/aaaaaaaaaaa\n  dQw4w9WgXcQ  \n"
        self.assertEqual(ch.parse_video_ids(text), ["dQw4w9WgXcQ", "aaaaaaaaaaa"])

    def test_a_bad_line_is_named(self):
        with self.assertRaises(ValueError) as cm:
            ch.parse_video_ids("dQw4w9WgXcQ\nhello there\n")
        self.assertIn("line 2", str(cm.exception))


class DateTests(unittest.TestCase):
    def test_formats(self):
        for text in ("2024-01-31", "20240131", "31-01-2024", "2024/1/31", "31.01.2024"):
            self.assertEqual(sel.parse_date(text), date(2024, 1, 31), text)

    def test_nonsense_is_a_clear_error(self):
        for text in ("yesterday", "2024-13-01", "2024-02-30", ""):
            with self.assertRaises(ValueError, msg=text):
                sel.parse_date(text)

    def test_date_range_needs_few_lookups_and_cuts_in_the_right_place(self):
        rows = make_videos(200)                                  # one per day, newest = 2026-09-30
        resolver = sel.DateResolver(lambda vid: None)
        got = sel.within_dates(rows, date(2026, 6, 1), date(2026, 6, 30), resolver)
        self.assertEqual([v.date() for v in got][0], date(2026, 6, 30))
        self.assertEqual([v.date() for v in got][-1], date(2026, 6, 1))
        self.assertEqual(len(got), 30)
        self.assertEqual(resolver.probes, 0)                     # exact dates were on the list already

    def test_rough_dates_are_looked_up_exactly_only_where_needed(self):
        rows = make_videos(200)
        for v in rows:
            v.approx = True
        by_id = {v.id: v.date() for v in rows}
        resolver = sel.DateResolver(lambda vid: by_id[vid])
        got = sel.within_dates(rows, date(2026, 6, 1), date(2026, 6, 30), resolver)
        self.assertEqual(len(got), 30)
        self.assertGreater(resolver.probes, 0)
        self.assertLess(resolver.probes, 40)                     # about log2(200) per edge, not 200

    def test_open_ended_ranges(self):
        rows = make_videos(10)
        resolver = sel.DateResolver(lambda vid: None)
        self.assertEqual(len(sel.within_dates(rows, None, None, resolver)), 10)
        self.assertEqual(len(sel.within_dates(rows, date(2026, 9, 28), None, resolver)), 3)

    def test_unreadable_dates_stop_with_a_clear_message(self):
        rows = make_videos(10)
        for v in rows:
            v.timestamp, v.approx = None, False
        with self.assertRaises(Exception) as cm:
            sel.within_dates(rows, date(2026, 9, 1), None, sel.DateResolver(lambda vid: None))
        self.assertIn("upload dates", str(cm.exception))


class OrderTests(unittest.TestCase):
    def setUp(self):
        self.rows = make_videos(5)                               # views 1000..1004, newest first

    def ids(self, rows):
        return [v.id[-1] for v in rows]

    def test_each_order(self):
        self.assertEqual(self.ids(sel.rank(self.rows, "latest")), list("01234"))
        self.assertEqual(self.ids(sel.rank(self.rows, "oldest")), list("43210"))
        self.assertEqual(self.ids(sel.rank(self.rows, "popular")), list("43210"))
        self.assertEqual(self.ids(sel.rank(self.rows, "unpopular")), list("01234"))

    def test_unknown_values_always_go_last(self):
        self.rows[0].views = None
        self.assertEqual(self.ids(sel.rank(self.rows, "popular"))[-1], "0")
        self.assertEqual(self.ids(sel.rank(self.rows, "unpopular"))[-1], "0")

    def test_longest_shortest_title(self):
        for i, v in enumerate(self.rows):
            v.duration, v.title = 10 * (i + 1), "bcdea"[i]
        self.assertEqual(self.ids(sel.rank(self.rows, "longest")), list("43210"))
        self.assertEqual(self.ids(sel.rank(self.rows, "shortest")), list("01234"))
        self.assertEqual(self.ids(sel.rank(self.rows, "title")), list("40123"))

    def test_random_is_reproducible_with_a_seed_and_loses_nothing(self):
        a = sel.rank(self.rows, "random", random.Random(3))
        b = sel.rank(self.rows, "random", random.Random(3))
        self.assertEqual(self.ids(a), self.ids(b))
        self.assertEqual(sorted(self.ids(a)), list("01234"))

    def test_limits_keep_what_is_unknown(self):
        rows = make_videos(4)
        rows[0].duration, rows[1].duration, rows[2].duration, rows[3].duration = 10, 100, None, 400
        rows[3].views = None
        got = sel.within_limits(rows, min_minutes=0.5, max_minutes=3)       # 30 s to 180 s
        self.assertEqual(self.ids(got), list("12"))
        self.assertEqual(self.ids(sel.within_limits(rows, min_views=1002)), list("23"))


class TakeTests(unittest.TestCase):
    def setUp(self):
        self.rows = make_videos(30)

    def pos(self, spec, skip=()):
        return [p for p, _ in sel.apply_take(self.rows, sel.parse_take(spec), skip)]

    def test_forms(self):
        self.assertEqual(sel.parse_take("60"), ("first", 60))
        self.assertEqual(sel.parse_take("new:7"), ("new", 7))
        self.assertEqual(sel.parse_take("last:3"), ("last", 3))
        self.assertEqual(sel.parse_take("comps:3"), ("comps", 3))
        self.assertEqual(sel.parse_take("every:5"), ("positions", [(1, None, 5)]))

    def test_positions(self):
        self.assertEqual(self.pos("3"), [1, 2, 3])
        self.assertEqual(self.pos("25-27"), [25, 26, 27])
        self.assertEqual(self.pos("28-"), [28, 29, 30])
        self.assertEqual(self.pos("-2"), [1, 2])
        self.assertEqual(self.pos("1-10,25,28-"), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 25, 28, 29, 30])
        self.assertEqual(self.pos("1-30/10"), [1, 11, 21])
        self.assertEqual(self.pos("every:10"), [1, 11, 21])
        self.assertEqual(self.pos("last:2"), [29, 30])
        self.assertEqual(self.pos("25-99"), [25, 26, 27, 28, 29, 30])         # past the end is cut, not an error

    def test_new_skips_what_you_have_and_goes_further_down(self):
        skip = {self.rows[0].id, self.rows[1].id, self.rows[3].id}
        self.assertEqual(self.pos("new:3", skip), [3, 5, 6])

    def test_random_keeps_list_order(self):
        got = [p for p, _ in sel.apply_take(self.rows, sel.parse_take("random:5"), rng=random.Random(1))]
        self.assertEqual(got, sorted(got))
        self.assertEqual(len(got), 5)

    def test_bad_input_is_a_clear_error(self):
        for bad in ("", "0", "last:0", "abc", "10-5", "0-3", "5/0"):
            with self.assertRaises(ValueError, msg=bad):
                sel.parse_take(bad)
        with self.assertRaises(ValueError):
            sel.apply_take(self.rows, ("comps", 3))

    def test_range_is_exactly_one_stretch(self):
        self.assertEqual(sel.parse_range("25-70"), ("positions", [(25, 70, 1)]))
        self.assertEqual(sel.parse_range("25-"), ("positions", [(25, None, 1)]))
        self.assertEqual(sel.parse_range("-30"), ("positions", [(1, 30, 1)]))
        for bad in ("60", "last:20", "1-10,25", "a-b", "-", "", "1-100/10"):
            with self.assertRaises(ValueError, msg=bad):
                sel.parse_range(bad)

    def test_describing(self):
        d = lambda s: sel.describe_take(sel.parse_take(s))
        self.assertEqual(d("60"), "the first 60")
        self.assertEqual(d("comps:1"), "enough new videos for 1 compilation")
        self.assertEqual(d("comps:2"), "enough new videos for 2 compilations")
        self.assertEqual(d("every:5"), "every 5th video")
        self.assertEqual(d("1-10,25,40-"), "positions 1-10, 25, 40-end")


class DuplicateTests(unittest.TestCase):
    def test_same_title_and_length_is_a_lookalike_but_only_with_a_length(self):
        self.assertEqual(sel.dup_key("Same Clip!", 20.2), sel.dup_key("same   clip", 20))
        self.assertNotEqual(sel.dup_key("Same Clip", 20), sel.dup_key("Same Clip", 21))
        self.assertIsNone(sel.dup_key("Same Clip", None))
        self.assertIsNone(sel.dup_key("", 20))

    def test_long_titles_match_their_70_character_cut(self):
        long = "A very long title " * 6
        self.assertEqual(sel.dup_key(long, 10), sel.dup_key(long[:70], 10))

    def test_lookalike_of_a_library_clip_and_of_a_higher_ranked_one(self):
        rows = make_videos(6)
        rows[1].title = rows[2].title = rows[4].title = "Twin"           # 1, 2 and 4 are look-alikes of each other
        rows[5].title = "Library twin"
        dups = sel.find_likely_duplicates(rows, have_ids=set(), have_keys={sel.dup_key("library twin", 20)})
        self.assertEqual(dups, {rows[2].id, rows[4].id, rows[5].id})       # the first Twin stays; 5 matches the library

    def test_a_video_you_already_have_makes_its_lookalikes_skipped(self):
        rows = make_videos(3)
        rows[0].title = rows[2].title = "Twin"
        dups = sel.find_likely_duplicates(rows, have_ids={rows[0].id}, have_keys=set())
        self.assertEqual(dups, {rows[2].id})

    def test_without_lengths_nothing_is_a_duplicate(self):
        rows = make_videos(4, title="Same", duration=None)
        self.assertEqual(sel.find_likely_duplicates(rows, set(), set()), set())


@unittest.skipUnless(HAVE_TOOLS, "needs ffmpeg")
class DiskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytt_src_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.backend = FakeBackend()

    def test_find_on_disk_in_our_names_old_names_and_ytdlp_names(self):
        a, b = self.tmp / "a", self.tmp / "b"
        a.mkdir(), b.mkdir()
        for folder, name in ((a, "vid00000001.mp4"), (a, "20260101-0000_0001_vid00000002.mp4"),
                             (a, "Some Title [vid00000003].webm"), (b, "mine_vid00000004.mkv"),
                             (a, "notes.txt"), (a, "vid00000005.txt"), (b, ".vid00000006.mp4"),
                             (a, "vid00000007.f137.mp4"), (a, "vid00000008.mp4.part")):
            (folder / name).write_bytes(b"x")
        (a / ".partial").mkdir()
        (a / ".partial" / "vid00000009.mp4").write_bytes(b"x")
        wanted = {f"vid{i:08d}" for i in range(1, 10)}
        found = files.find_on_disk([a, b, self.tmp / "nope"], wanted)
        self.assertEqual(sorted(found), ["vid00000001", "vid00000002", "vid00000003", "vid00000004"])
        self.assertEqual(found["vid00000004"].parent, b)

    def test_a_download_ends_up_as_one_finished_file_and_nothing_else(self):
        final, info = files.download_clip(self.backend, "vid00000001", self.tmp)
        self.assertEqual(final, self.tmp / "vid00000001.mp4")
        self.assertTrue(final.is_file())
        self.assertEqual(info["title"], "Clip 1")
        self.assertEqual([p.name for p in self.tmp.rglob("*") if p.is_file()], ["vid00000001.mp4"])

    def test_a_failure_leaves_nothing_behind_and_says_why(self):
        self.backend.fail = {"vid00000001"}
        with self.assertRaises(DownloadError) as cm:
            files.download_clip(self.backend, "vid00000001", self.tmp)
        self.assertIn("403", str(cm.exception))
        self.assertEqual([p for p in self.tmp.rglob("*") if p.is_file()], [])

    def test_stopping_leaves_nothing_behind(self):
        self.backend.slow = {"vid00000001": 5}
        stop = threading.Event()
        stop.set()
        with self.assertRaises(DownloadStopped):
            files.download_clip(self.backend, "vid00000001", self.tmp, stop=stop)
        self.assertEqual([p for p in self.tmp.rglob("*") if p.is_file()], [])

    def test_a_clip_can_be_put_under_an_exact_name(self):
        exact = self.tmp / "sub" / "20260101-0000_0001_vid00000001.mp4"
        final, _ = files.download_clip(self.backend, "vid00000001", self.tmp, exact_path=exact)
        self.assertEqual(final, exact)
        self.assertTrue(exact.is_file())

    def test_ctrl_c_during_a_download_cleans_up_and_propagates(self):
        self.backend.interrupt = {"vid00000001"}
        with self.assertRaises(KeyboardInterrupt):
            files.download_clip(self.backend, "vid00000001", self.tmp)
        self.assertEqual([p for p in self.tmp.rglob("*") if p.is_file()], [])


@unittest.skipUnless(HAVE_TOOLS, "needs ffmpeg")
class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytt_dl_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.backend = FakeBackend()
        self.got = []

    def jobs(self, ids):
        return [Job(f"vid{i:08d}", f"Clip {i}", position=n) for n, i in enumerate(ids, 1)]

    def run_all(self, ids, target, workers=1):
        return download_all(self.backend, self.jobs(ids), target=target, folder=self.tmp, workers=workers,
                            on_result=lambda o: self.got.append(o.job.video_id[-2:] + ":" + o.status))

    def left_on_disk(self):
        return sorted(p.name for p in self.tmp.rglob("*") if p.is_file())

    def test_never_more_than_the_target(self):
        out = self.run_all(range(10), target=4, workers=3)
        self.assertEqual(len([o for o in out if o.status == "completed"]), 4)
        self.assertEqual(len(self.backend.started), 4)
        self.assertEqual(len(self.left_on_disk()), 4)

    def test_a_failure_is_replaced_by_the_next_in_line(self):
        self.backend.fail = {"vid00000001", "vid00000003"}
        out = self.run_all(range(10), target=4, workers=2)
        done = [o.job.video_id[-2:] for o in out if o.status == "completed"]
        self.assertEqual(done, ["00", "02", "04", "05"])
        self.assertEqual([o.job.video_id[-2:] for o in out if o.status == "failed"], ["01", "03"])
        self.assertEqual(len(self.left_on_disk()), 4)                  # the failures left no junk

    def test_when_the_queue_runs_dry_you_get_what_exists(self):
        out = self.run_all(range(3), target=10)
        self.assertEqual(len([o for o in out if o.status == "completed"]), 3)

    def test_results_arrive_in_queue_order_even_if_the_first_finishes_last(self):
        self.backend.slow = {"vid00000000": 0.4}
        self.run_all(range(5), target=5, workers=3)
        self.assertEqual(self.got, [f"0{i}:completed" for i in range(5)])

    def test_one_worker_still_works(self):
        self.run_all(range(3), target=3, workers=1)
        self.assertEqual(self.got, ["00:completed", "01:completed", "02:completed"])

    def test_ctrl_c_hands_over_what_finished_and_leaves_no_junk(self):
        self.backend.interrupt = {"vid00000003"}
        with self.assertRaises(KeyboardInterrupt):
            self.run_all(range(8), target=8, workers=1)
        self.assertEqual(self.got, ["00:completed", "01:completed", "02:completed"])
        self.assertEqual(self.left_on_disk(), ["vid00000000.mp4", "vid00000001.mp4", "vid00000002.mp4"])

    def test_ctrl_c_with_several_workers_stops_the_slow_ones_and_keeps_the_finished(self):
        self.backend.slow = {"vid00000001": 3, "vid00000002": 3}
        self.backend.interrupt = {"vid00000003"}
        with self.assertRaises(KeyboardInterrupt):
            self.run_all(range(8), target=8, workers=4)
        self.assertEqual(self.got[0], "00:completed")
        self.assertTrue(all(name.endswith(".mp4") and ".part" not in name for name in self.left_on_disk()))
        self.assertEqual(len(self.left_on_disk()), len(self.got))      # everything on disk was handed over


if __name__ == "__main__":
    unittest.main()
