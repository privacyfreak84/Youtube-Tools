"""research niche and clip: discovery plus the outlier scan, against a fake YouTube."""
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from ytt.ops.research import clips as cl
from ytt.ops.research import niche as ni
from ytt.ops.research.common import ResearchError
from ytt.sources.models import ChannelRef, VideoInfo

try:
    from fakes import FakeBackend
except ImportError:
    from tests.fakes import FakeBackend

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


def url(key):
    return f"https://www.youtube.com/channel/{key}"


def vids(prefix, views, tab="videos", days=None):
    out = []
    for i, v in enumerate(views):
        d = days[i] if days else i + 1
        ts = None if d is None else (NOW - timedelta(days=d)).timestamp()
        out.append(VideoInfo(id=f"{prefix}{i:03d}".ljust(11, "_")[:11], title=f"{prefix} {i}", views=v, timestamp=ts, tab=tab))
    return out


class World(unittest.TestCase):
    """Two searchable channels (one with a big video) and one seed that features a third."""

    def setUp(self):
        self.b = FakeBackend()
        self.b.searches = {"cats": [ChannelRef("uc1", "One", url("uc1")), ChannelRef("uc2", "Two", url("uc2"))]}
        self.b.featured = {"https://www.youtube.com/@Seed": [ChannelRef("uc3", "Three", url("uc3"))]}
        self.add("uc1", "One", videos=vids("a", [10, 10, 10, 90], days=[40, 30, 20, 2]))
        self.add("uc2", "Two", videos=vids("b", [20, 20, 20, 60], days=[40, 30, 20, 3]))
        self.add("uc3", "Three", videos=vids("c", [5, 5, 5, 50], days=[40, 30, 20, 60]))

    def add(self, key, name, **tabs):
        self.b.channels[url(key)] = {"name": name, "subs": 100, "verified": False, "tabs": tabs}

    def scanned(self):
        return sorted({base for base, _, _ in self.b.tabs_read})


class NicheTests(World):
    def run_niche(self, **kw):
        on_progress = kw.pop("on_progress", None)
        kw.setdefault("searches", ["cats"])
        return ni.find_niche(ni.NicheRequest(**kw), self.b, NOW, on_progress)

    def test_it_finds_the_channels_then_scans_every_one_for_outliers(self):
        res = self.run_niche(seeds=["@Seed"])
        self.assertEqual([c.name for c in res.channels], ["One", "Two", "Three"])
        self.assertEqual(self.scanned(), [url("uc1"), url("uc2"), url("uc3")])
        # ratios: Three's 50 is 10x its median, One's 90 is 9x, Two's 60 is 3x
        self.assertEqual([(r.channel, r.views) for r in res.rows], [("Three", 50), ("One", 90), ("Two", 60)])

    def test_the_outlier_options_work_as_in_outliers(self):
        res = self.run_niche(seeds=["@Seed"], sort="views", top=2)
        self.assertEqual([r.views for r in res.rows], [90, 60])
        res = self.run_niche(seeds=["@Seed"], multiplier=9.5)
        self.assertEqual([r.channel for r in res.rows], ["Three"])           # only the 10x video is that far out
        self.b.tabs_read.clear()
        self.run_niche(limit=3, content_type="all")
        self.assertTrue(all(limit == 3 for _, _, limit in self.b.tabs_read))
        self.assertEqual({t for _, t, _ in self.b.tabs_read}, {"videos", "shorts", "streams"})

    def test_dates_are_looked_up_only_when_asked(self):
        self.run_niche(sort="ratio")
        self.assertEqual(self.b.probes, [])

    def test_no_channels_found_means_nothing_is_scanned(self):
        res = self.run_niche(searches=["nothing"])
        self.assertEqual((res.channels, res.outliers, res.rows), ([], None, []))
        self.assertEqual(self.b.tabs_read, [])

    def test_notes_from_discovery_and_from_scanning_are_both_kept(self):
        self.b.searches["bad"] = "429"
        self.b.tab_errors[(url("uc2"), "videos")] = "could not read (404)"
        res = self.run_niche(searches=["bad", "cats"])
        self.assertEqual(res.notes[0], "search failed for 'bad': 429")
        self.assertTrue(any("skipped" in n and "uc2" in n for n in res.notes))

    def test_a_bad_option_stops_before_anything_is_searched(self):
        for kw, expected in [(dict(multiplier=0), "--multiplier"), (dict(sort="fame"), "--sort"), (dict(content_type="reels"), "--type"),
                             (dict(top=0), "--top"), (dict(count=0), "--count"), (dict(searches=[]), "--search keyword or --seed")]:
            with self.subTest(kw=kw), self.assertRaisesRegex(ResearchError, expected):
                self.run_niche(**kw)
        self.assertEqual((self.b.searched, self.b.tabs_read), ([], []))

    def test_progress_reports_the_discovery_count(self):
        seen = []
        self.run_niche(on_progress=seen.append)
        self.assertIn("2 unique channel(s) discovered; scanning for outliers ...", seen)


class ClipTests(World):
    def run_clips(self, **kw):
        on_progress = kw.pop("on_progress", None)
        kw.setdefault("searches", ["cats"])
        return cl.find_clips(cl.ClipsRequest(**kw), self.b, NOW, on_progress)

    def test_only_outliers_within_the_age_limit_are_kept_best_first(self):
        res = self.run_clips(seeds=["@Seed"])
        self.assertEqual([(r.channel, r.views, r.days_ago) for r in res.rows], [("One", 90, 2), ("Two", 60, 3)])
        self.assertEqual((res.candidates, res.dropped), (3, 1))                  # Three's outlier is 60 days old

    def test_the_age_limit_includes_the_limit_day_itself(self):
        self.assertEqual(len(self.run_clips(max_age_days=2).rows), 1)
        self.assertEqual(len(self.run_clips(max_age_days=3).rows), 2)
        self.assertEqual(len(self.run_clips(max_age_days=1).rows), 0)

    def test_every_candidate_without_a_date_is_looked_up_and_one_that_cannot_be_is_dropped(self):
        self.add("uc1", "One", shorts=vids("s", [10, 10, 10, 90, 80], "shorts", days=[None] * 5))
        self.b.dates = {vids("s", [1] * 5, "shorts")[3].id: date(2026, 10, 5)}             # one resolves, one does not
        res = self.run_clips(content_type="shorts")
        self.assertEqual([(r.views, r.days_ago, r.type) for r in res.rows], [(90, 2, "shorts")])
        self.assertEqual(len(self.b.probes), 2)                                           # both outliers were asked about
        self.assertEqual((res.candidates, res.dropped), (2, 1))

    def test_a_date_already_known_from_the_channel_page_is_not_looked_up_again(self):
        self.run_clips()
        self.assertEqual(self.b.probes, [])

    def test_a_rough_date_does_not_count_as_fresh_until_the_exact_date_confirms_it(self):
        # the channel page says "1 week ago" (7 days) for all three; in fact they are 13 days, 4 days and unknown
        listing = [replace(v, approx=True) for v in vids("s", [10] * 6 + [90, 80, 70], "shorts",
                                                         days=[40, 35, 30, 25, 20, 15, 7, 7, 7])]
        self.add("uc1", "One", shorts=listing)
        self.b.dates = {listing[6].id: date(2026, 9, 24), listing[7].id: date(2026, 10, 3)}
        res = self.run_clips(content_type="shorts")
        self.assertEqual([(r.views, r.days_ago, r.approx) for r in res.rows], [(80, 4, False)])
        self.assertEqual(len(self.b.probes), 3)                                           # every candidate was asked about
        self.assertEqual((res.candidates, res.dropped), (3, 2))                           # too old, and not confirmable

    def test_top_caps_the_list_and_zero_or_none_means_no_cap(self):
        self.assertEqual(len(self.run_clips(top=1).rows), 1)
        self.assertEqual(self.run_clips(top=1).rows[0].views, 90)
        self.assertEqual(len(self.run_clips(top=0).rows), 2)
        self.assertEqual(len(self.run_clips(top=None).rows), 2)

    def test_the_default_scans_all_three_tabs(self):
        self.run_clips()
        self.assertEqual({t for _, t, _ in self.b.tabs_read}, {"videos", "shorts", "streams"})

    def test_nothing_flagged_or_no_channels_gives_empty_results(self):
        res = self.run_clips(multiplier=500)
        self.assertEqual((res.rows, res.candidates, res.dropped), ([], 0, 0))
        res = self.run_clips(searches=["nothing"])
        self.assertEqual((res.channels, res.rows), ([], []))

    def test_a_bad_option_stops_before_anything_is_searched(self):
        for kw, expected in [(dict(max_age_days=-1), "--max-age-days"), (dict(top=-1), "--top"), (dict(multiplier=0), "--multiplier"),
                             (dict(searches=[]), "--search keyword or --seed")]:
            with self.subTest(kw=kw), self.assertRaisesRegex(ResearchError, expected):
                self.run_clips(**kw)
        self.assertEqual((self.b.searched, self.b.tabs_read), ([], []))


if __name__ == "__main__":
    unittest.main()
