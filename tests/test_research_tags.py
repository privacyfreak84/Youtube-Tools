"""research tags: counting tags and title words across videos, against a fake YouTube."""
import unittest

from ytt.ops.research import tags as tg
from ytt.ops.research.common import ResearchError
from ytt.sources.models import VideoInfo

try:
    from fakes import FakeBackend
except ImportError:
    from tests.fakes import FakeBackend


def vid(x):
    return x.ljust(11, "_")


def put(b, x, title, tags):
    b.infos[vid(x)] = VideoInfo(id=vid(x), title=title, tags=tags)


def run(b, videos, **kw):
    on_progress = kw.pop("on_progress", None)
    return tg.analyze_tags(tg.TagsRequest(videos=[vid(v) if len(v) < 11 else v for v in videos], **kw), b, on_progress)


class TokenizingTests(unittest.TestCase):
    def test_title_words_are_lowercased_and_stopwords_numbers_and_short_words_are_dropped(self):
        self.assertEqual(tg.tokenize_title("The Cat's BEST Trick: 10 Tricks of 2026 (Official Video)!", 3),
                         ["cat's", "best", "trick", "tricks"])
        self.assertEqual(tg.tokenize_title("A big dog", 3), ["big", "dog"])            # exactly the minimum length counts
        self.assertEqual(tg.tokenize_title("A big dog", 4), [])
        self.assertEqual(tg.tokenize_title("A big dog", 1), ["big", "dog"])
        self.assertEqual(tg.tokenize_title("", 3), [])

    def test_urls_come_from_the_url_column_of_a_research_csv(self):
        text = "title,url,id\nA,https://youtu.be/aaaaaaaaaaa,aaaaaaaaaaa\nB,N/A,\nC,,\nD, https://youtu.be/bbbbbbbbbbb ,b\n"
        self.assertEqual(tg.urls_from_csv(text), ["https://youtu.be/aaaaaaaaaaa", "https://youtu.be/bbbbbbbbbbb"])
        with self.assertRaisesRegex(ResearchError, "no 'url' column"):
            tg.urls_from_csv("title,views\nA,1\n")
        with self.assertRaisesRegex(ResearchError, "no 'url' column"):
            tg.urls_from_csv("")


class AnalyzingTests(unittest.TestCase):
    def setUp(self):
        self.b = FakeBackend()
        put(self.b, "a", "Funny cats compilation", ["Cats", "funny", " cats ", "", "Pets"])
        put(self.b, "b", "Funny dogs", ["funny", "Dogs"])
        put(self.b, "c", "No tags here", [])

    def test_a_tag_counts_once_per_video_ignoring_case_and_spaces(self):
        res = run(self.b, ["a", "b", "c"])
        self.assertEqual(dict(res.tags), {"cats": 1, "funny": 2, "pets": 1, "dogs": 1})

    def test_title_words_are_counted_every_time_they_appear(self):
        put(self.b, "d", "Cats cats CATS", [])
        res = run(self.b, ["a", "b", "c", "d"])
        self.assertEqual(res.words["cats"], 4)
        self.assertEqual(res.words["funny"], 2)
        self.assertEqual(res.words["here"], 1)

    def test_coverage_counts_the_videos_read_and_those_that_had_tags(self):
        res = run(self.b, ["a", "b", "c"])
        self.assertEqual((res.total, res.with_tags), (3, 2))

    def test_a_video_that_cannot_be_read_is_a_note_and_is_not_counted(self):
        res = run(self.b, ["a", "zzz"])
        self.assertEqual((res.total, res.notes), (1, [f"could not read {vid('zzz')}; skipped"]))

    def test_links_and_ids_both_work_and_repeats_are_read_once(self):
        res = tg.analyze_tags(tg.TagsRequest(videos=[f"https://youtu.be/{vid('a')}", vid("a"), f"https://www.youtube.com/watch?v={vid('b')}"]), self.b)
        self.assertEqual(self.b.infos_read, [vid("a"), vid("b")])
        self.assertEqual(res.total, 2)

    def test_the_minimum_word_length_applies(self):
        res = run(self.b, ["a", "b", "c"], min_word_len=6)
        self.assertEqual(dict(res.words), {"compilation": 1})            # "funny" has 5 letters, "here" 4

    def test_rows_are_the_top_tags_then_the_top_words(self):
        res = run(self.b, ["a", "b", "c"])
        rows = res.rows(1)
        self.assertEqual(rows, [{"kind": "tag", "value": "funny", "count": 2}, {"kind": "title_word", "value": "funny", "count": 2}])

    def test_progress_counts_the_videos(self):
        seen = []
        run(self.b, ["a", "b"], on_progress=seen.append)
        self.assertEqual(seen, [f"[1/2] fetching tags: {vid('a')}", f"[2/2] fetching tags: {vid('b')}"])

    def test_bad_requests_say_what_is_wrong_before_anything_is_read(self):
        for kw, expected in [(dict(videos=[]), "give video ids or links"), (dict(videos=["not a video"]), "video 1 is not a YouTube"),
                             (dict(videos=[vid("a")], top=0), "--top"), (dict(videos=[vid("a")], min_word_len=0), "--min-word-len")]:
            with self.subTest(kw=kw), self.assertRaisesRegex(ResearchError, expected):
                tg.analyze_tags(tg.TagsRequest(**kw), self.b)
        self.assertEqual(self.b.infos_read, [])


if __name__ == "__main__":
    unittest.main()
