"""research outliers: the logic, against a fake YouTube. Nothing here can reach the network."""
import unittest
from datetime import date, datetime, timedelta, timezone

from ytt.ops.research import outliers as ol
from ytt.ops.research.common import ResearchError
from ytt.sources.models import VideoInfo

try:
    from fakes import FakeBackend
except ImportError:
    from tests.fakes import FakeBackend

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


def vids(prefix, views, tab="videos", days=None):
    """Videos with the given view counts (None = no count), newest first. ids are 11 characters."""
    out = []
    for i, v in enumerate(views):
        d = days[i] if days else i + 1
        ts = None if d is None else (NOW - timedelta(days=d)).timestamp()
        out.append(VideoInfo(id=f"{prefix}{i:03d}".ljust(11, "_")[:11], title=f"{prefix} {i}", views=v, timestamp=ts, tab=tab))
    return out


def channel(backend, handle, name, subs, verified, **tabs):
    backend.channels[f"https://www.youtube.com/{handle}"] = {"name": name, "subs": subs, "verified": verified, "tabs": tabs}


def run(backend, channels, **kw):
    on_progress = kw.pop("on_progress", None)
    return ol.find_outliers(ol.OutlierRequest(channels=channels, **kw), backend, now=NOW, on_progress=on_progress)


class FindingTests(unittest.TestCase):
    def setUp(self):
        self.b = FakeBackend()

    def test_a_video_far_above_its_channels_median_is_flagged_with_everything_known_about_it(self):
        channel(self.b, "@Chan", "Chan", 5000, True, videos=vids("a", [100, 120, 90, 110, 600]))
        res = run(self.b, ["@Chan"])
        self.assertEqual(len(res.rows), 1)
        r = res.rows[0]
        self.assertEqual((r.channel, r.subs, r.verified, r.views, r.baseline_median, r.type), ("Chan", 5000, True, 600, 110, "videos"))
        self.assertAlmostEqual(r.ratio, 600 / 110)
        self.assertEqual((r.days_ago, r.title, r.url), (5, "a 4", f"https://youtu.be/{r.id}"))
        self.assertEqual(res.scanned, 5)

    def test_the_baseline_is_the_median_not_the_average(self):
        channel(self.b, "@Chan", "Chan", None, None, videos=vids("a", [10, 10, 10, 40]))        # average 17.5, median 10
        self.assertEqual([r.views for r in run(self.b, ["@Chan"]).rows], [40])

    def test_exactly_the_multiplier_counts_and_just_under_does_not(self):
        channel(self.b, "@Chan", "Chan", None, None, videos=vids("a", [100, 100, 100, 300, 299]))
        self.assertEqual([r.views for r in run(self.b, ["@Chan"], multiplier=3).rows], [300])
        self.assertEqual(sorted(r.views for r in run(self.b, ["@Chan"], multiplier=2.9).rows), [299, 300])

    def test_videos_without_a_view_count_are_left_out_of_the_baseline_and_the_results(self):
        channel(self.b, "@Chan", "Chan", None, None, videos=vids("a", [100, None, 100, None, 100, 400]))
        res = run(self.b, ["@Chan"])
        self.assertEqual((res.scanned, [r.views for r in res.rows]), (4, [400]))

    def test_too_few_videos_for_a_baseline_are_skipped_with_a_note(self):
        channel(self.b, "@Tiny", "Tiny", None, None, videos=vids("t", [10, 1000]))
        channel(self.b, "@Three", "Three", None, None, videos=vids("h", [10, 10, 1000]))
        res = run(self.b, ["@Tiny", "@Three"])
        self.assertEqual([r.channel for r in res.rows], ["Three"])
        self.assertEqual(res.notes, ["@Tiny [videos]: only 2 video(s), too few for a reliable baseline; skipped"])

    def test_a_channel_with_nothing_and_a_tab_that_fails_are_notes_and_the_rest_carries_on(self):
        channel(self.b, "@Good", "Good", None, None, videos=vids("g", [10, 10, 10, 90]))
        self.b.tab_errors[("https://www.youtube.com/@Bad", "videos")] = "could not read https://www.youtube.com/@Bad/videos (404)"
        res = run(self.b, ["@Empty", "@Bad", "@Good"])
        self.assertEqual([r.channel for r in res.rows], ["Good"])
        self.assertEqual(res.notes[0], "no videos found for @Empty")
        self.assertIn("skipped @Bad [videos]: could not read", res.notes[1])
        self.assertEqual(res.notes[2], "no videos found for @Bad")

    def test_no_outliers_is_an_empty_result_not_an_error(self):
        channel(self.b, "@Flat", "Flat", None, None, videos=vids("f", [100, 100, 100, 100]))
        res = run(self.b, ["@Flat"])
        self.assertEqual((res.rows, res.notes, res.scanned), ([], [], 4))


class WhatIsReadTests(unittest.TestCase):
    def setUp(self):
        self.b = FakeBackend()
        channel(self.b, "@Chan", "Chan", None, None, videos=vids("v", [10, 10, 10, 99], "videos"),
                shorts=vids("s", [5, 5, 5, 50], "shorts"), streams=vids("l", [7, 7, 7, 70], "streams"))

    def test_channels_may_be_a_handle_a_name_or_a_link_to_any_tab(self):
        run(self.b, ["@Chan", "https://www.youtube.com/@Chan/shorts", "https://www.youtube.com/@Chan/"])
        self.assertEqual({base for base, _, _ in self.b.tabs_read}, {"https://www.youtube.com/@Chan"})
        run(self.b, ["Other"])
        self.assertEqual(self.b.tabs_read[-1][0], "https://www.youtube.com/Other")

    def test_one_tab_by_default_all_three_with_type_all_and_the_limit_is_passed_on(self):
        res = run(self.b, ["@Chan"], limit=30)
        self.assertEqual(self.b.tabs_read, [("https://www.youtube.com/@Chan", "videos", 30)])
        self.b.tabs_read.clear()
        res = run(self.b, ["@Chan"], content_type="all")
        self.assertEqual([t for _, t, _ in self.b.tabs_read], ["videos", "shorts", "streams"])
        self.assertEqual(sorted(r.type for r in res.rows), ["shorts", "streams", "videos"])
        self.assertTrue(all(limit is None for _, _, limit in self.b.tabs_read))

    def test_each_tab_has_its_own_baseline(self):
        channel(self.b, "@Mixed", "Mixed", None, None, videos=vids("m", [1000, 1000, 1000, 1000]),
                shorts=vids("n", [10, 10, 10, 40], "shorts"))
        res = run(self.b, ["@Mixed"], content_type="all")
        self.assertEqual([(r.type, r.views) for r in res.rows], [("shorts", 40)])

    def test_progress_says_what_is_being_read(self):
        seen = []
        run(self.b, ["@Chan"], content_type="all", on_progress=seen.append)
        self.assertEqual(seen, ["fetching @Chan [videos] ...", "fetching @Chan [shorts] ...", "fetching @Chan [streams] ..."])


class SortingTests(unittest.TestCase):
    def setUp(self):
        self.b = FakeBackend()
        channel(self.b, "@B", "bravo", 900, None, videos=vids("b", [10, 10, 10, 50], days=[9, 8, 7, 6]))
        channel(self.b, "@A", "Alpha", None, None, videos=vids("a", [100, 100, 100, 400, 800], days=[9, 8, 7, 6, None]))
        channel(self.b, "@C", "Charlie", 100, None, videos=vids("c", [20, 20, 20, 120], days=[9, 8, 7, 2]))

    def order(self, **kw):
        return [(r.channel, r.views) for r in run(self.b, ["@B", "@A", "@C"], **kw).rows]

    def test_default_is_the_biggest_outlier_first(self):
        # ratios: Alpha 800 is 8x its median, Charlie 120 is 6x, bravo 50 is 5x, Alpha 400 is 4x
        self.assertEqual(self.order(), [("Alpha", 800), ("Charlie", 120), ("bravo", 50), ("Alpha", 400)])

    def test_each_key_has_its_natural_direction_and_reverse_flips_it(self):
        self.assertEqual([v for _, v in self.order(sort="views")], [800, 400, 120, 50])
        self.assertEqual([v for _, v in self.order(sort="views", reverse=True)], [50, 120, 400, 800])
        self.assertEqual([c for c, _ in self.order(sort="channel")], ["Alpha", "Alpha", "bravo", "Charlie"])   # A-Z, ignoring case
        self.assertEqual([v for _, v in self.order(sort="days_ago")][:3], [120, 50, 400])                   # soonest first

    def test_rows_with_no_value_for_the_key_come_last_either_way(self):
        self.assertEqual(self.order(sort="subs")[:2], [("bravo", 50), ("Charlie", 120)])                    # highest first
        self.assertEqual(self.order(sort="subs", reverse=True)[:2], [("Charlie", 120), ("bravo", 50)])
        self.assertEqual([c for c, _ in self.order(sort="subs")][-2:], ["Alpha", "Alpha"])
        self.assertEqual([c for c, _ in self.order(sort="subs", reverse=True)][-2:], ["Alpha", "Alpha"])
        self.assertEqual(self.order(sort="days_ago")[-1], ("Alpha", 800))                                    # no date: last

    def test_top_keeps_the_best_n_after_sorting(self):
        self.assertEqual(self.order(top=2), [("Alpha", 800), ("Charlie", 120)])
        self.assertEqual(self.order(top=2, sort="views", reverse=True), [("bravo", 50), ("Charlie", 120)])

    def test_grouping_keeps_the_order_each_group_first_appears(self):
        rows = run(self.b, ["@B", "@A", "@C"]).rows
        self.assertEqual([(k, [r.views for r in g]) for k, g in ol.grouped(rows, "channel")],
                         [("Alpha", [800, 400]), ("Charlie", [120]), ("bravo", [50])])


class ResolvingDatesTests(unittest.TestCase):
    def setUp(self):
        self.b = FakeBackend()
        channel(self.b, "@S", "S", None, None, shorts=vids("s", [10, 10, 10, 50, 40, 30], "shorts", days=[None] * 6))
        for v in self.b.channels["https://www.youtube.com/@S"]["tabs"]["shorts"]:
            self.b.dates[v.id] = date(2026, 10, 1)

    def test_dates_are_not_looked_up_unless_asked(self):
        res = run(self.b, ["@S"], content_type="shorts", multiplier=1.5)
        self.assertEqual(self.b.probes, [])
        self.assertTrue(all(r.days_ago is None for r in res.rows))

    def test_when_asked_only_the_outliers_that_have_no_date_are_looked_up_after_the_cut_to_top(self):
        res = run(self.b, ["@S"], content_type="shorts", multiplier=1.5, resolve_dates=True, top=2)
        self.assertEqual(len(self.b.probes), 2)                                    # three outliers, but only the 2 kept
        self.assertEqual([r.days_ago for r in res.rows], [6, 6])                   # 2026-10-07 minus 2026-10-01

    def test_sorting_by_age_needs_every_date_first_so_all_outliers_are_looked_up(self):
        run(self.b, ["@S"], content_type="shorts", multiplier=1.5, resolve_dates=True, top=1, sort="days_ago")
        self.assertEqual(len(self.b.probes), 3)

    def test_a_date_that_cannot_be_read_stays_empty(self):
        self.b.dates.clear()
        res = run(self.b, ["@S"], content_type="shorts", multiplier=1.5, resolve_dates=True)
        self.assertTrue(all(r.days_ago is None for r in res.rows))

    def test_videos_that_already_have_a_date_are_not_looked_up(self):
        channel(self.b, "@D", "D", None, None, videos=vids("d", [10, 10, 10, 90]))
        run(self.b, ["@D"], resolve_dates=True)
        self.assertEqual(self.b.probes, [])

    def test_progress_counts_the_lookups(self):
        seen = []
        run(self.b, ["@S"], content_type="shorts", multiplier=1.5, resolve_dates=True, top=2, on_progress=seen.append)
        self.assertEqual([m.split(" resolving")[0] for m in seen if "resolving" in m], ["[1/2]", "[2/2]"])


class RequestTests(unittest.TestCase):
    def test_bad_requests_say_what_is_wrong_before_anything_is_read(self):
        b = FakeBackend()
        cases = [(dict(channels=[]), "at least one channel"), (dict(content_type="reels"), "--type"),
                 (dict(sort="fame"), "--sort"), (dict(multiplier=0), "--multiplier"), (dict(limit=0), "--limit"),
                 (dict(top=0), "--top")]
        for kw, expected in cases:
            with self.subTest(kw=kw):
                kw.setdefault("channels", ["@Chan"])
                with self.assertRaisesRegex(ResearchError, expected):
                    ol.find_outliers(ol.OutlierRequest(**kw), b, now=NOW)
        self.assertEqual(b.tabs_read, [])

    def test_as_dict_has_every_column_the_csv_and_json_use(self):
        o = ol.Outlier("abcdefghijk", "C", 5, True, "T", 9, 3.0, 3.0, 4, "videos")
        self.assertEqual(list(o.as_dict()), ["id", "channel", "verified", "subs", "title", "views", "baseline_median",
                                             "ratio", "days_ago", "type", "url"])


if __name__ == "__main__":
    unittest.main()
