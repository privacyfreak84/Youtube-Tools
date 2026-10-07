"""research channels: discovery by search and by a channel's Channels tab, against a fake YouTube."""
import unittest

from ytt.ops.research import channels as ch
from ytt.ops.research.common import ResearchError
from ytt.sources.models import ChannelRef

try:
    from fakes import FakeBackend
except ImportError:
    from tests.fakes import FakeBackend


def ref(key, name=None):
    return ChannelRef(key, name or key.title(), f"https://www.youtube.com/channel/{key}")


def run(b, **kw):
    on_progress = kw.pop("on_progress", None)
    return ch.find_channels(ch.ChannelsRequest(**kw), b, on_progress)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.b = FakeBackend()
        self.b.searches = {"cats": [ref("uc1"), ref("uc2")], "dogs": [ref("uc2"), ref("uc3")]}
        self.b.featured = {"https://www.youtube.com/@Seed": [ref("uc3"), ref("uc4")]}

    def test_a_search_gives_the_channels_with_how_they_were_found(self):
        res = run(self.b, searches=["cats"], count=7)
        self.assertEqual([(c.name, c.source) for c in res.channels], [("Uc1", 'search:"cats"'), ("Uc2", 'search:"cats"')])
        self.assertEqual(self.b.searched, [("cats", 7)])
        self.assertEqual(res.notes, [])

    def test_a_seed_gives_the_channels_it_features(self):
        res = run(self.b, seeds=["@Seed"])
        self.assertEqual([(c.name, c.source) for c in res.channels], [("Uc3", "featured-by:@Seed"), ("Uc4", "featured-by:@Seed")])

    def test_a_seed_may_be_given_as_a_link_to_any_of_its_tabs(self):
        res = run(self.b, seeds=["https://www.youtube.com/@Seed/channels"])
        self.assertEqual(len(res.channels), 2)

    def test_a_channel_found_twice_is_listed_once_with_every_way_it_was_found_in_order(self):
        res = run(self.b, searches=["cats", "dogs"], seeds=["@Seed"])
        self.assertEqual([c.name for c in res.channels], ["Uc1", "Uc2", "Uc3", "Uc4"])
        by = {c.name: c.source for c in res.channels}
        self.assertEqual(by["Uc2"], 'search:"cats", search:"dogs"')
        self.assertEqual(by["Uc3"], 'search:"dogs", featured-by:@Seed')
        self.assertEqual(by["Uc4"], "featured-by:@Seed")

    def test_finding_the_same_channel_by_the_same_way_twice_does_not_repeat_the_source(self):
        res = run(self.b, searches=["cats", "cats"])
        self.assertEqual([c.source for c in res.channels], ['search:"cats"', 'search:"cats"'])

    def test_a_search_or_a_seed_that_fails_is_a_note_and_the_rest_carries_on(self):
        self.b.searches["bad"] = "could not read ytsearch30:bad (429)"
        self.b.featured["https://www.youtube.com/@NoTab"] = "could not read (404)"
        res = run(self.b, searches=["bad", "cats"], seeds=["@NoTab"])
        self.assertEqual(len(res.channels), 2)
        self.assertEqual(res.notes, ["search failed for 'bad': could not read ytsearch30:bad (429)",
                                     "no Channels tab for @NoTab: could not read (404)"])

    def test_nothing_found_is_an_empty_result(self):
        self.assertEqual(run(self.b, searches=["nothing"]).channels, [])

    def test_progress_says_what_is_being_done(self):
        seen = []
        run(self.b, searches=["cats"], seeds=["@Seed"], on_progress=seen.append)
        self.assertEqual(seen, ["searching: cats", "reading the Channels tab: @Seed"])


class DetailsTests(unittest.TestCase):
    def setUp(self):
        self.b = FakeBackend()
        self.b.searches = {"cats": [ref("uc1"), ref("uc2")]}
        self.b.channels["https://www.youtube.com/channel/uc1"] = {"name": "Uc1", "subs": 1500, "verified": True, "tabs": {}}

    def test_without_with_subs_no_extra_request_is_made(self):
        res = run(self.b, searches=["cats"])
        self.assertEqual(self.b.tabs_read, [])
        self.assertEqual((res.channels[0].subs, res.channels[0].verified), (None, None))

    def test_with_subs_reads_one_video_page_per_channel_for_the_numbers(self):
        seen = []
        res = run(self.b, searches=["cats"], with_subs=True, on_progress=seen.append)
        self.assertEqual(self.b.tabs_read, [("https://www.youtube.com/channel/uc1", "videos", 1),
                                            ("https://www.youtube.com/channel/uc2", "videos", 1)])
        self.assertEqual([(c.subs, c.verified) for c in res.channels], [(1500, True), (None, None)])
        self.assertEqual(seen[-2:], ["[1/2] details for Uc1", "[2/2] details for Uc2"])

    def test_a_channel_whose_details_cannot_be_read_stays_without_numbers(self):
        self.b.tab_errors[("https://www.youtube.com/channel/uc1", "videos")] = "boom"
        res = run(self.b, searches=["cats"], with_subs=True)
        self.assertEqual([(c.subs, c.verified) for c in res.channels], [(None, None), (None, None)])

    def test_as_dict_is_what_csv_and_json_use(self):
        c = ch.Found("N", "http://u", 'search:"x"', 5, False)
        self.assertEqual(list(c.as_dict()), ["name", "verified", "subs", "url", "source"])


class RequestTests(unittest.TestCase):
    def test_bad_requests_are_errors_before_anything_is_read(self):
        b = FakeBackend()
        with self.assertRaisesRegex(ResearchError, "--search keyword or --seed"):
            run(b)
        with self.assertRaisesRegex(ResearchError, "--count"):
            run(b, searches=["x"], count=0)
        self.assertEqual(b.searched, [])


if __name__ == "__main__":
    unittest.main()
