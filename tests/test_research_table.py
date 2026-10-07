"""research table: reading a channel's videos into rows, against a fake YouTube."""
import unittest
from datetime import date, datetime, timedelta, timezone

from ytt.ops.research import table as tb
from ytt.ops.research.common import ResearchError
from ytt.sources.errors import SourceError
from ytt.sources.models import VideoInfo

try:
    from fakes import FakeBackend
except ImportError:
    from tests.fakes import FakeBackend

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
BASE = "https://www.youtube.com/@Chan"


def video(vid, title, views, days, duration=30, tab="videos", approx=False):
    ts = None if days is None else (NOW - timedelta(days=days)).timestamp()
    return VideoInfo(id=vid.ljust(11, "_"), title=title, views=views, duration=duration, timestamp=ts, approx=approx, tab=tab)


def setup_channel(b, **tabs):
    b.channels[BASE] = {"name": "Chan", "subs": 1, "verified": False, "tabs": tabs}


def run(b, **kw):
    kw.setdefault("channel", "@Chan")
    on_progress = kw.pop("on_progress", None)
    return tb.channel_table(tb.TableRequest(**kw), b, now=NOW, on_progress=on_progress)


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.b = FakeBackend()
        setup_channel(self.b,
                      videos=[video("v1", "New", 100, 1), video("v2", "Mid", 900, 10), video("v3", "Old", 50, 30),
                              video("v4", "Undated", 70, None)],
                      shorts=[video("s1", "Short", 5000, 5, 12, "shorts"), video("v1", "New", 100, 1)],
                      streams=[video("l1", "Live", 300, 20, 3600, "streams")])

    def test_each_row_has_title_views_date_days_length_and_type(self):
        res = run(self.b)
        top = res.rows[0]
        self.assertEqual((top.title, top.views, top.date, top.days_ago(NOW), top.duration, top.type),
                         ("New", 100, date(2026, 10, 6), 1, 30, "videos"))
        self.assertEqual(top.as_dict(NOW)["url"], "https://youtu.be/v1_________")
        self.assertEqual(list(top.as_dict(NOW)), ["id", "title", "type", "views", "upload_date", "days_ago", "approx", "duration", "url"])

    def test_latest_is_default_and_puts_undated_videos_last(self):
        self.assertEqual([r.title for r in run(self.b).rows], ["New", "Mid", "Old", "Undated"])

    def test_oldest_and_popular(self):
        self.assertEqual([r.title for r in run(self.b, sort="oldest").rows], ["Old", "Mid", "New", "Undated"])
        self.assertEqual([r.title for r in run(self.b, sort="popular").rows], ["Mid", "New", "Undated", "Old"])

    def test_popular_puts_videos_without_a_view_count_last(self):
        self.b.channels[BASE]["tabs"]["videos"].append(video("v5", "NoViews", None, 2))
        self.assertEqual(run(self.b, sort="popular").rows[-1].title, "NoViews")

    def test_all_reads_the_three_tabs_and_a_video_in_two_tabs_appears_once(self):
        res = run(self.b, content_type="all")
        self.assertEqual([t for _, t, _ in self.b.tabs_read], ["videos", "shorts", "streams"])
        self.assertEqual(sorted(r.title for r in res.rows), ["Live", "Mid", "New", "Old", "Short", "Undated"])
        self.assertEqual([r.type for r in res.rows if r.title == "New"], ["videos"])

    def test_limit_with_latest_is_passed_to_the_read_and_cuts_the_result(self):
        res = run(self.b, limit=2)
        self.assertEqual(self.b.tabs_read, [(BASE, "videos", 2)])
        self.assertEqual([r.title for r in res.rows], ["New", "Mid"])
        self.assertEqual(res.notes, [])

    def test_limit_with_popular_reads_everything_first_so_the_top_n_is_really_the_top_n(self):
        res = run(self.b, sort="popular", limit=1)
        self.assertEqual(self.b.tabs_read, [(BASE, "videos", None)])
        self.assertEqual([r.title for r in res.rows], ["Mid"])
        self.assertEqual(len(res.notes), 1)
        self.assertIn("reads the whole videos list before it can pick the 1", res.notes[0])

    def test_a_tab_that_fails_is_a_note_and_the_others_still_count(self):
        self.b.tab_errors[(BASE, "shorts")] = "could not read (404)"
        res = run(self.b, content_type="all")
        self.assertEqual(res.notes, ["skipped shorts: could not read (404)"])
        self.assertEqual(len(res.rows), 5)

    def test_an_unknown_channel_gives_no_rows(self):
        self.assertEqual(run(self.b, channel="@Nobody").rows, [])

    def test_a_rough_date_is_marked_rough(self):
        self.b.channels[BASE]["tabs"]["videos"] = [video("v9", "Rough", 1, 3, approx=True)]
        row = run(self.b).rows[0]
        self.assertTrue(row.approx)
        self.assertTrue(row.as_dict(NOW)["approx"])

    def test_progress_says_what_is_read(self):
        seen = []
        run(self.b, content_type="all", on_progress=seen.append)
        self.assertEqual(seen, [f"fetching videos: {BASE}/videos", f"fetching shorts: {BASE}/shorts", f"fetching streams: {BASE}/streams"])


class FullTests(unittest.TestCase):
    def setUp(self):
        self.b = FakeBackend()
        setup_channel(self.b, shorts=[video("s1", "A", 10, 5, None, "shorts", True), video("s2", "B", 20, None, None, "shorts"),
                                      video("s3", "C", 30, 7, 9, "shorts", True)])
        self.b.infos = {"s1".ljust(11, "_"): VideoInfo(id="s1".ljust(11, "_"), views=11, duration=14, timestamp=datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()),
                        "s2".ljust(11, "_"): VideoInfo(id="s2".ljust(11, "_"), duration=22, timestamp=datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())}

    def test_full_fills_in_exact_dates_lengths_and_views_from_each_videos_own_page(self):
        res = run(self.b, content_type="shorts", full=True, sort="oldest")
        by = {r.title: r for r in res.rows}
        self.assertEqual((by["A"].date, by["A"].approx, by["A"].duration, by["A"].views), (date(2026, 10, 1), False, 14, 11))
        self.assertEqual((by["B"].date, by["B"].duration, by["B"].views), (date(2026, 9, 1), 22, 20))      # views kept when not given
        self.assertEqual((by["C"].approx, by["C"].duration, by["C"].date), (True, 9, date(2026, 9, 30)))     # unreadable: left as it was

    def test_without_full_no_video_page_is_read(self):
        run(self.b, content_type="shorts")
        self.assertEqual(self.b.infos_read, [])

    def test_full_only_reads_the_rows_that_remain_after_the_cut_and_reports_progress(self):
        seen = []
        run(self.b, content_type="shorts", full=True, limit=2, on_progress=seen.append)
        self.assertEqual(len(self.b.infos_read), 2)
        self.assertEqual([m.split(" ")[0] for m in seen if m.startswith("[")], ["[1/2]", "[2/2]"])


class RequestTests(unittest.TestCase):
    def test_bad_requests_are_errors_before_anything_is_read(self):
        b = FakeBackend()
        for kw, expected in [(dict(channel=" "), "give a channel"), (dict(content_type="reels"), "--type"),
                             (dict(sort="best"), "--sort"), (dict(limit=0), "--limit")]:
            with self.subTest(kw=kw), self.assertRaisesRegex(ResearchError, expected):
                run(b, **kw)
        self.assertEqual(b.tabs_read, [])


if __name__ == "__main__":
    unittest.main()
