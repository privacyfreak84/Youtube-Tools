"""ytt.sources.ytdlp against a stand-in for the yt_dlp module. This cannot prove that real YouTube behaves (the only
real check is running it on a machine that can reach YouTube) but it does prove that our code builds the right options,
reads the fields it relies on, and turns every failure into our own errors."""
import sys
import types
import unittest
from datetime import date
from unittest import mock

from ytt.sources.errors import DownloadStopped, SourceError
from ytt.sources.ytdlp import YtDlpBackend


class FakeYoutubeDL:
    """Records how it was built and returns whatever the test set up."""
    instances = []
    info = None                       # what extract_info returns
    error = None                      # what it raises
    hook_calls = 1

    def __init__(self, opts):
        self.opts = opts
        self.calls = []
        FakeYoutubeDL.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True):
        self.calls.append((url, download))
        if FakeYoutubeDL.error:
            raise FakeYoutubeDL.error
        for _ in range(FakeYoutubeDL.hook_calls if download else 0):
            for hook in self.opts.get("progress_hooks", []):
                hook({"status": "downloading"})
        return FakeYoutubeDL.info


class YtDlpTests(unittest.TestCase):
    def setUp(self):
        FakeYoutubeDL.instances, FakeYoutubeDL.info, FakeYoutubeDL.error, FakeYoutubeDL.hook_calls = [], None, None, 1
        module = types.SimpleNamespace(YoutubeDL=FakeYoutubeDL)
        patcher = mock.patch.dict(sys.modules, {"yt_dlp": module})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.backend = YtDlpBackend()

    def test_listing_reads_ids_titles_views_lengths_and_dates(self):
        FakeYoutubeDL.info = {"entries": [
            {"id": "aaaaaaaaaaa", "title": "A", "view_count": 5, "duration": 12, "upload_date": "20260102"},
            {"id": "bbbbbbbbbbb", "title": "B", "view_count": None, "concurrent_view_count": 7, "duration": None,
             "timestamp": 1_700_000_000},
            {"id": "ccccccccccc", "title": None},
            None,
            {"title": "no id"},
        ]}
        rows = self.backend.list_tab("https://www.youtube.com/@Chan", "shorts")
        self.assertEqual(FakeYoutubeDL.instances[0].calls, [("https://www.youtube.com/@Chan/shorts", False)])
        self.assertTrue(FakeYoutubeDL.instances[0].opts["extract_flat"])
        self.assertEqual([r.id for r in rows], ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"])
        a, b, c = rows
        self.assertEqual((a.title, a.views, a.duration, a.approx, a.date()), ("A", 5, 12, False, date(2026, 1, 2)))
        self.assertEqual((b.views, b.approx, b.timestamp), (7, True, 1_700_000_000))        # a rough date is marked rough
        self.assertEqual((c.title, c.views, c.duration, c.timestamp), ("", None, None, None))
        self.assertEqual({r.tab for r in rows}, {"shorts"})

    def test_an_empty_or_missing_listing_is_an_empty_list_not_a_crash(self):
        for info in (None, {}, {"entries": None}):
            FakeYoutubeDL.info = info
            self.assertEqual(self.backend.list_tab("https://www.youtube.com/@Chan", "videos"), [])

    def test_a_listing_failure_becomes_our_error_with_the_address_in_it(self):
        FakeYoutubeDL.error = RuntimeError("HTTP Error 404: Not Found\nmore detail")
        with self.assertRaises(SourceError) as cm:
            self.backend.list_tab("https://www.youtube.com/@Nope", "videos")
        self.assertIn("https://www.youtube.com/@Nope/videos", str(cm.exception))
        self.assertIn("404", str(cm.exception))
        self.assertNotIn("more detail", str(cm.exception))

    def test_a_channel_tab_carries_the_channel_name_followers_and_verified_flag(self):
        FakeYoutubeDL.info = {"channel": "Chan", "uploader": "other", "channel_follower_count": 1234,
                              "channel_is_verified": True,
                              "entries": [{"id": "aaaaaaaaaaa", "title": "A", "view_count": 5, "upload_date": "20260102"},
                                          {"id": "bbbbbbbbbbb", "title": "B"}, None, {"title": "no id"}]}
        tab = self.backend.channel_tab("https://www.youtube.com/@Chan", "streams")
        self.assertEqual((tab.name, tab.subs, tab.verified), ("Chan", 1234, True))
        self.assertEqual([(v.id, v.views, v.tab) for v in tab.videos], [("aaaaaaaaaaa", 5, "streams"), ("bbbbbbbbbbb", None, "streams")])
        self.assertEqual(FakeYoutubeDL.instances[0].calls, [("https://www.youtube.com/@Chan/streams", False)])
        self.assertNotIn("playlist_items", FakeYoutubeDL.instances[0].opts)

    def test_a_channel_tab_limit_asks_yt_dlp_for_only_the_newest_n(self):
        FakeYoutubeDL.info = {"entries": []}
        self.backend.channel_tab("https://www.youtube.com/@Chan", "videos", limit=25)
        self.assertEqual(FakeYoutubeDL.instances[0].opts["playlist_items"], "1:25")

    def test_a_channel_name_falls_back_to_the_uploader_then_the_address(self):
        FakeYoutubeDL.info = {"uploader": "Up", "entries": []}
        self.assertEqual(self.backend.channel_tab("https://www.youtube.com/@Chan", "videos").name, "Up")
        FakeYoutubeDL.info = {"entries": []}
        self.assertEqual(self.backend.channel_tab("https://www.youtube.com/@Chan", "videos").name, "https://www.youtube.com/@Chan")

    def test_a_tab_the_channel_does_not_have_is_an_empty_tab_and_a_failure_is_our_error(self):
        FakeYoutubeDL.info = None
        tab = self.backend.channel_tab("https://www.youtube.com/@Chan", "shorts")
        self.assertEqual((tab.name, tab.subs, tab.verified, tab.videos), (None, None, None, []))
        FakeYoutubeDL.error = RuntimeError("HTTP Error 404: Not Found")
        with self.assertRaisesRegex(SourceError, "@Chan/shorts.*404"):
            self.backend.channel_tab("https://www.youtube.com/@Chan", "shorts")

    def test_cookies_from_the_workspace_settings_reach_every_kind_of_request(self):
        backend = YtDlpBackend.from_config({"cookies_from_browser": "firefox", "cookies_file": "/x/c.txt"})
        FakeYoutubeDL.info = {"entries": []}
        backend.list_tab("https://www.youtube.com/@Chan", "videos")
        backend.probe_date("aaaaaaaaaaa")
        FakeYoutubeDL.info = {}
        backend.download("aaaaaaaaaaa", "/t/%(id)s.%(ext)s", 1080)
        for inst in FakeYoutubeDL.instances:
            self.assertEqual(inst.opts["cookiesfrombrowser"], ("firefox",))
            self.assertEqual(inst.opts["cookiefile"], "/x/c.txt")
        plain = YtDlpBackend.from_config({})
        FakeYoutubeDL.instances.clear()
        plain.probe_date("aaaaaaaaaaa")
        self.assertNotIn("cookiesfrombrowser", FakeYoutubeDL.instances[0].opts)

    def test_probing_a_date(self):
        FakeYoutubeDL.info = {"upload_date": "20250131"}
        self.assertEqual(self.backend.probe_date("aaaaaaaaaaa"), date(2025, 1, 31))
        self.assertEqual(FakeYoutubeDL.instances[0].calls, [("https://youtu.be/aaaaaaaaaaa", False)])
        FakeYoutubeDL.info = {}
        self.assertIsNone(self.backend.probe_date("aaaaaaaaaaa"))
        FakeYoutubeDL.info = {"upload_date": "garbage"}
        self.assertIsNone(self.backend.probe_date("aaaaaaaaaaa"))
        FakeYoutubeDL.error = RuntimeError("blocked")
        self.assertIsNone(self.backend.probe_date("aaaaaaaaaaa"))                  # never raises: the caller falls back

    def test_searching_collects_the_channels_behind_the_results(self):
        FakeYoutubeDL.info = {"entries": [
            {"channel_id": "UC1", "channel": "One", "channel_url": "https://www.youtube.com/channel/UC1"},
            {"channel_id": "UC2", "uploader": "Two"},                                  # no address: built from the id
            {"channel": "NoAddress"}, {"channel_id": "UC3"}, None]}
        found = self.backend.search_channels("cute cats", 5)
        self.assertEqual([(c.key, c.name, c.url) for c in found],
                         [("UC1", "One", "https://www.youtube.com/channel/UC1"), ("UC2", "Two", "https://www.youtube.com/channel/UC2")])
        self.assertEqual(FakeYoutubeDL.instances[0].calls, [("ytsearch5:cute cats", False)])
        FakeYoutubeDL.error = RuntimeError("HTTP Error 429\nmore")
        with self.assertRaisesRegex(SourceError, "ytsearch5:cute cats.*429"):
            self.backend.search_channels("cute cats", 5)

    def test_the_channels_a_channel_features_are_read_from_its_channels_tab(self):
        FakeYoutubeDL.info = {"entries": [{"id": "UC9", "title": "Nine", "url": "https://www.youtube.com/@nine"},
                                          {"channel_id": "UC8", "channel": "Eight"}, {"title": "no id or url"}]}
        found = self.backend.featured_channels("https://www.youtube.com/@Chan")
        self.assertEqual([(c.key, c.name, c.url) for c in found],
                         [("UC9", "Nine", "https://www.youtube.com/@nine"), ("UC8", "Eight", "https://www.youtube.com/channel/UC8")])
        self.assertEqual(FakeYoutubeDL.instances[0].calls, [("https://www.youtube.com/@Chan/channels", False)])
        FakeYoutubeDL.info = None
        self.assertEqual(self.backend.featured_channels("https://www.youtube.com/@Chan"), [])

    def test_any_listing_address_can_be_read_with_a_limit(self):
        FakeYoutubeDL.info = {"entries": [{"id": "aaaaaaaaaaa", "title": "A"}]}
        rows = self.backend.list_url("https://www.youtube.com/@Chan/streams", 15)
        self.assertEqual([r.id for r in rows], ["aaaaaaaaaaa"])
        self.assertEqual(FakeYoutubeDL.instances[0].calls, [("https://www.youtube.com/@Chan/streams", False)])
        self.assertEqual(FakeYoutubeDL.instances[0].opts["playlist_items"], "1:15")
        self.backend.list_url("https://www.youtube.com/playlist?list=PL1")
        self.assertNotIn("playlist_items", FakeYoutubeDL.instances[1].opts)

    def test_a_live_video_gives_its_title_channel_and_viewers_and_anything_else_is_none(self):
        FakeYoutubeDL.info = {"live_status": "is_live", "is_live": True, "title": "Live!", "channel": "Chan", "uploader": "x",
                              "webpage_url": "https://www.youtube.com/watch?v=aaaaaaaaaaa", "concurrent_view_count": 321,
                              "view_count": 400}
        live = self.backend.live_status("aaaaaaaaaaa")
        self.assertEqual((live.id, live.title, live.channel, live.url, live.viewers, live.views),
                         ("aaaaaaaaaaa", "Live!", "Chan", "https://www.youtube.com/watch?v=aaaaaaaaaaa", 321, 400))
        self.assertEqual(FakeYoutubeDL.instances[0].calls, [("https://www.youtube.com/watch?v=aaaaaaaaaaa", False)])
        for info in ({"live_status": "was_live", "is_live": False}, {"live_status": "is_upcoming"},
                     {"live_status": "is_live", "is_live": "yes"}, None, {}):
            FakeYoutubeDL.info = info
            self.assertIsNone(self.backend.live_status("aaaaaaaaaaa"), info)
        FakeYoutubeDL.info = {"live_status": "is_live", "is_live": True}
        live = self.backend.live_status("aaaaaaaaaaa")
        self.assertEqual((live.title, live.channel, live.url), ("(untitled)", "Unknown", "https://www.youtube.com/watch?v=aaaaaaaaaaa"))

    def test_a_live_check_that_cannot_read_the_page_is_our_error(self):
        FakeYoutubeDL.error = RuntimeError("HTTP Error 429\nmore")
        with self.assertRaisesRegex(SourceError, "aaaaaaaaaaa.*429"):
            self.backend.live_status("aaaaaaaaaaa")

    def test_a_videos_own_page_gives_exact_date_length_and_views(self):
        FakeYoutubeDL.info = {"id": "other", "title": "T", "view_count": 9, "duration": 15.5, "upload_date": "20260301"}
        v = self.backend.video_info("aaaaaaaaaaa")
        self.assertEqual((v.id, v.title, v.views, v.duration, v.approx, v.date()), ("aaaaaaaaaaa", "T", 9, 15.5, False, date(2026, 3, 1)))
        self.assertEqual(FakeYoutubeDL.instances[0].calls, [("https://youtu.be/aaaaaaaaaaa", False)])
        FakeYoutubeDL.info = {"timestamp": 1_700_000_000}                       # no exact date: a rough one is marked rough
        v = self.backend.video_info("aaaaaaaaaaa")
        self.assertEqual((v.approx, v.timestamp), (True, 1_700_000_000))
        self.assertIsNone(self.backend.probe_date("aaaaaaaaaaa"))                # probe_date only ever answers with exact dates

    def test_a_video_that_cannot_be_read_is_none_and_never_an_error(self):
        FakeYoutubeDL.info = None
        self.assertIsNone(self.backend.video_info("aaaaaaaaaaa"))
        FakeYoutubeDL.error = RuntimeError("blocked")
        self.assertIsNone(self.backend.video_info("aaaaaaaaaaa"))

    def test_download_options_cap_the_height_and_write_to_the_template(self):
        FakeYoutubeDL.info = {"title": "T", "view_count": 9, "duration": 15.5, "upload_date": "20260301"}
        info = self.backend.download("aaaaaaaaaaa", "/t/%(id)s.%(ext)s", 720)
        opts = FakeYoutubeDL.instances[0].opts
        self.assertEqual(opts["outtmpl"], "/t/%(id)s.%(ext)s")
        self.assertIn("height<=720", opts["format"])
        self.assertEqual(opts["merge_output_format"], "mp4")
        self.assertTrue(opts["noplaylist"])
        self.assertEqual(FakeYoutubeDL.instances[0].calls, [("https://www.youtube.com/watch?v=aaaaaaaaaaa", True)])
        self.assertEqual(info, {"title": "T", "views": 9, "duration": 15.5, "published": "2026-03-01"})

    def test_what_youtube_did_not_say_is_left_out_not_made_up(self):
        FakeYoutubeDL.info = {"title": "T"}
        self.assertEqual(self.backend.download("aaaaaaaaaaa", "/t/%(id)s.%(ext)s", 1080), {"title": "T"})
        FakeYoutubeDL.info = None
        self.assertEqual(self.backend.download("aaaaaaaaaaa", "/t/%(id)s.%(ext)s", 1080), {})

    def test_stopping_is_noticed_at_the_next_progress_report(self):
        import threading
        stop = threading.Event()
        FakeYoutubeDL.info = {"title": "T"}
        self.backend.download("aaaaaaaaaaa", "/t/%(id)s.%(ext)s", 1080, stop)           # not set: finishes
        stop.set()
        with self.assertRaises(DownloadStopped):
            self.backend.download("aaaaaaaaaaa", "/t/%(id)s.%(ext)s", 1080, stop)

    def test_a_download_failure_passes_through_for_the_file_layer_to_wrap(self):
        FakeYoutubeDL.error = RuntimeError("HTTP Error 403")
        with self.assertRaises(RuntimeError):
            self.backend.download("aaaaaaaaaaa", "/t/%(id)s.%(ext)s", 1080)

    def test_a_missing_yt_dlp_is_a_clear_install_message(self):
        with mock.patch.dict(sys.modules, {"yt_dlp": None}):
            with self.assertRaises(SourceError) as cm:
                YtDlpBackend().list_tab("https://www.youtube.com/@Chan", "videos")
        self.assertIn("pip install yt-dlp", str(cm.exception))


try:
    import yt_dlp as _real_yt_dlp
except ImportError:
    _real_yt_dlp = None


@unittest.skipUnless(_real_yt_dlp, "needs yt-dlp installed")
class FormatChoiceWithTheRealYtDlp(unittest.TestCase):
    """The one part of the real yt-dlp that can be checked without a network: that the format string we build really
    picks the best video at or under the height cap (run through yt-dlp's own processing of a made-up video)."""

    @staticmethod
    def formats():
        out = [{"format_id": f"v{h}", "vcodec": "avc1.64001f", "acodec": "none", "height": h, "width": h * 16 // 9,
                "ext": "mp4", "protocol": "https", "url": f"http://x/{h}", "tbr": h / 2, "fps": 30}
               for h in (2160, 1080, 720, 480, 360)]
        out.append({"format_id": "audio", "vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a", "protocol": "https",
                    "url": "http://x/a", "abr": 128, "tbr": 128})
        return out

    def pick(self, cap):
        captured = {}

        class Spy(FakeYoutubeDL):
            def extract_info(self, url, download=True):
                captured["format"] = self.opts["format"]
                return {}
        with mock.patch.dict(sys.modules, {"yt_dlp": types.SimpleNamespace(YoutubeDL=Spy)}):
            YtDlpBackend().download("aaaaaaaaaaa", "/t/%(id)s.%(ext)s", cap)
        ydl = _real_yt_dlp.YoutubeDL({"quiet": True, "format": captured["format"], "simulate": True})
        info = {"id": "x", "title": "t", "formats": self.formats(), "extractor": "generic", "extractor_key": "Generic",
                "webpage_url": "http://x/", "original_url": "http://x/"}
        got = ydl.process_video_result(info, download=False)
        return [f["format_id"] for f in (got.get("requested_formats") or [got])]

    def test_the_height_cap_picks_the_best_video_that_fits_plus_audio(self):
        self.assertEqual(self.pick(1080), ["v1080", "audio"])
        self.assertEqual(self.pick(720), ["v720", "audio"])
        self.assertEqual(self.pick(480), ["v480", "audio"])
        self.assertEqual(self.pick(9999), ["v2160", "audio"])


if __name__ == "__main__":
    unittest.main()
