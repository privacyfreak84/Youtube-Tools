"""research live: finding the streams that are live now, against a fake YouTube."""
import unittest

from ytt.ops.research import live as lv
from ytt.ops.research.common import ResearchError
from ytt.sources.models import LiveInfo, VideoInfo

try:
    from fakes import FakeBackend
except ImportError:
    from tests.fakes import FakeBackend

STREAMS = "https://www.youtube.com/@Chan/streams"


def entry(vid, title="t"):
    return VideoInfo(id=vid.ljust(11, "_"), title=title, tab="streams")


def live(vid, title="Live", channel="Chan", viewers=10):
    vid = vid.ljust(11, "_")
    return LiveInfo(id=vid, title=title, channel=channel, url=f"https://www.youtube.com/watch?v={vid}", viewers=viewers)


def run(b, channels, **kw):
    on_progress = kw.pop("on_progress", None)
    return lv.find_live(lv.LiveRequest(channels=channels, **kw), b, on_progress)


class AddressTests(unittest.TestCase):
    def test_a_channel_is_read_on_its_streams_tab_unless_the_link_ends_in_live(self):
        self.assertEqual(lv.listing_url("@Chan"), STREAMS)
        self.assertEqual(lv.listing_url("https://www.youtube.com/@Chan/videos"), STREAMS)
        self.assertEqual(lv.listing_url("https://www.youtube.com/@Chan/streams/"), STREAMS)
        self.assertEqual(lv.listing_url("https://www.youtube.com/@Chan/live"), "https://www.youtube.com/@Chan/live")
        self.assertEqual(lv.listing_url("https://www.youtube.com/playlist?list=PL1"), "https://www.youtube.com/playlist?list=PL1")
        self.assertEqual(lv.listing_url("https://www.youtube.com/watch?v=aaaaaaaaaaa"), "https://www.youtube.com/watch?v=aaaaaaaaaaa")

    def test_the_file_name_for_a_channel(self):
        self.assertEqual(lv.file_name(STREAMS), "Chan.json")
        self.assertEqual(lv.file_name("https://www.youtube.com/@Chan/live"), "Chan.json")
        self.assertEqual(lv.file_name("https://www.youtube.com/channel/UCabc/streams"), "UCabc.json")
        self.assertEqual(lv.file_name("https://www.youtube.com/playlist?list=PL1"), "playlist_list=PL1.json")
        self.assertEqual(lv.file_name("https://x.com/a:b"), "a_b.json")
        self.assertEqual(lv.file_name("///"), "channel.json")


class FindingTests(unittest.TestCase):
    def setUp(self):
        self.b = FakeBackend()
        self.b.listings[STREAMS] = [entry("up1", "Upcoming"), entry("l1", "Now"), entry("old1", "Past"), entry("l2", "Also now")]
        self.b.lives = {"l1".ljust(11, "_"): live("l1", "Now", viewers=5), "l2".ljust(11, "_"): live("l2", "Also now", viewers=7)}

    def test_only_the_streams_that_are_live_now_are_kept_in_order(self):
        res = run(self.b, ["@Chan"])
        self.assertEqual([s.title for s in res.streams], ["Now", "Also now"])
        self.assertEqual([s.viewers for s in res.channels[0].streams], [5, 7])
        self.assertEqual(res.channels[0].channel_url, STREAMS)
        self.assertEqual(res.notes, [])

    def test_the_default_checks_15_entries_and_zero_means_the_whole_tab(self):
        run(self.b, ["@Chan"])
        self.assertEqual(self.b.listed_urls[-1], (STREAMS, 15))
        run(self.b, ["@Chan"], limit=0)
        self.assertEqual(self.b.listed_urls[-1], (STREAMS, None))
        run(self.b, ["@Chan"], limit=2)
        self.assertEqual(self.b.listed_urls[-1], (STREAMS, 2))

    def test_the_limit_bounds_how_many_pages_are_checked(self):
        run(self.b, ["@Chan"], limit=2)
        self.assertEqual(self.b.live_checked, ["up1".ljust(11, "_"), "l1".ljust(11, "_")])

    def test_each_channel_keeps_its_own_streams_and_a_channel_with_none_is_still_listed(self):
        self.b.listings["https://www.youtube.com/@Quiet/streams"] = [entry("q1")]
        res = run(self.b, ["@Quiet", "@Chan"])
        self.assertEqual([(c.channel_url.split("/")[-2], len(c.streams)) for c in res.channels], [("@Quiet", 0), ("@Chan", 2)])

    def test_the_same_live_stream_found_twice_in_one_channel_is_kept_once(self):
        self.b.listings[STREAMS] = [entry("l1"), entry("l1")]
        self.assertEqual(len(run(self.b, ["@Chan"]).streams), 1)

    def test_a_channel_that_cannot_be_read_and_a_video_that_cannot_be_checked_are_notes(self):
        self.b.listings["https://www.youtube.com/@Bad/streams"] = "could not read (404)"
        self.b.lives["l1".ljust(11, "_")] = "could not read https://www.youtube.com/watch?v=l1 (429)"
        res = run(self.b, ["@Bad", "@Chan"])
        self.assertEqual([s.title for s in res.streams], ["Also now"])
        self.assertEqual(res.notes[0], "skipped https://www.youtube.com/@Bad/streams: could not read (404)")
        self.assertIn("skipped l1_________: could not read", res.notes[1])

    def test_progress_names_the_channel_and_counts_the_entries(self):
        seen = []
        run(self.b, ["@Chan"], limit=2, on_progress=seen.append)
        self.assertEqual(seen, [f"checking {STREAMS} ...", "[1/2] checking: Upcoming", "[2/2] checking: Now"])

    def test_bad_requests_are_errors_before_anything_is_read(self):
        with self.assertRaisesRegex(ResearchError, "at least one channel"):
            run(self.b, [])
        with self.assertRaisesRegex(ResearchError, "--limit"):
            run(self.b, ["@Chan"], limit=-1)
        self.assertEqual(self.b.listed_urls, [])


if __name__ == "__main__":
    unittest.main()
