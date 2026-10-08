"""The real YouTube access, through yt-dlp. Only this module imports yt_dlp. Covered by the fake in tests, so it is
kept small; what it does against real YouTube can only be checked on a machine that can reach YouTube."""
from datetime import datetime, timezone

from ytt.sources.errors import DownloadStopped, SourceError
from ytt.sources.models import ChannelRef, ChannelTab, LiveInfo, VideoInfo


def _yt_dlp():
    try:
        import yt_dlp
    except ImportError:
        raise SourceError("yt-dlp is not installed. Install it with:  pip install yt-dlp")
    return yt_dlp


def yt_dlp_version():
    """The installed yt-dlp's version text (like '2025.10.22'), or None when it is not installed."""
    try:
        import yt_dlp
        return str(yt_dlp.version.__version__)
    except (ImportError, AttributeError):
        return None


def _first_line(e):
    return (str(e).splitlines() or [""])[0][:200]


class YtDlpBackend:
    def __init__(self, cookies_from_browser="", cookies_file=""):
        self.cookies = {}
        if cookies_from_browser:
            self.cookies["cookiesfrombrowser"] = (cookies_from_browser,)
        if cookies_file:
            self.cookies["cookiefile"] = cookies_file

    @classmethod
    def from_config(cls, config):
        return cls(config.get("cookies_from_browser", ""), config.get("cookies_file", ""))

    # ---- reading
    def _flat(self, url, limit=None):
        """The channel page as yt-dlp's quick 'flat' listing: one request, no per-video detail."""
        yt_dlp = _yt_dlp()
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "ignoreerrors": True,
                "extract_flat": True, "extractor_args": {"youtubetab": {"approximate_date": [""]}}, **self.cookies}
        if limit:
            opts["playlist_items"] = f"1:{int(limit)}"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            raise SourceError(f"could not read {url} ({_first_line(e)})")

    @staticmethod
    def _video(e, tab):
        ts = e.get("timestamp") or e.get("release_timestamp")
        exact = None
        if e.get("upload_date"):
            try:
                exact = datetime.strptime(e["upload_date"], "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                pass
        views = e.get("view_count")
        if views is None:
            views = e.get("concurrent_view_count")
        return VideoInfo(id=e["id"], title=e.get("title") or "", views=views, duration=e.get("duration"),
                         timestamp=exact if exact is not None else ts, approx=exact is None and ts is not None, tab=tab)

    def _videos(self, info, tab):
        return [self._video(e, tab) for e in (info or {}).get("entries") or [] if e and e.get("id")]

    def list_tab(self, base_url, tab):
        return self._videos(self._flat(f"{base_url}/{tab}"), tab)

    def channel_tab(self, base_url, tab, limit=None):
        info = self._flat(f"{base_url}/{tab}", limit)
        if not info:
            return ChannelTab()
        return ChannelTab(name=info.get("channel") or info.get("uploader") or base_url,
                          subs=info.get("channel_follower_count"), verified=info.get("channel_is_verified"),
                          videos=self._videos(info, tab))

    def search_channels(self, query, count):
        info = self._flat(f"ytsearch{int(count)}:{query}")
        out = []
        for e in (info or {}).get("entries") or []:
            if not e:
                continue
            cid = e.get("channel_id")
            name = e.get("channel") or e.get("uploader")
            url = e.get("channel_url") or (f"https://www.youtube.com/channel/{cid}" if cid else None)
            if url and name:
                out.append(ChannelRef(key=cid or url, name=name, url=url))
        return out

    def featured_channels(self, base_url):
        info = self._flat(f"{base_url}/channels")
        out = []
        for e in (info or {}).get("entries") or []:
            if not e:
                continue
            cid = e.get("channel_id") or e.get("id")
            name = e.get("channel") or e.get("title") or e.get("uploader")
            url = e.get("url") or (f"https://www.youtube.com/channel/{cid}" if cid else None)
            if url and name:
                out.append(ChannelRef(key=cid or url, name=name, url=url))
        return out

    def list_url(self, url, limit=None):
        return self._videos(self._flat(url, limit), "videos")

    def live_status(self, video_id):
        yt_dlp = _yt_dlp()
        opts = {"quiet": True, "no_warnings": True, "ignoreerrors": True, "skip_download": True,
                "extractor_args": {"youtube": {"player_client": ["android", "ios"], "player_skip": ["js", "configs"]}},
                "youtube_include_dash_manifest": False, "youtube_include_hls_manifest": False, **self.cookies}
        url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            raise SourceError(f"could not read {url} ({_first_line(e)})")
        if not isinstance(info, dict) or info.get("live_status") != "is_live" or info.get("is_live") is not True:
            return None
        return LiveInfo(id=video_id, title=info.get("title") or "(untitled)",
                        channel=info.get("channel") or info.get("uploader") or "Unknown",
                        url=info.get("webpage_url") or url, viewers=info.get("concurrent_view_count"),
                        views=info.get("view_count"))

    def video_info(self, video_id):
        yt_dlp = _yt_dlp()
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "ignoreerrors": True,
                "socket_timeout": 20, "extract_flat": False,
                "extractor_args": {"youtube": {"player_client": ["android", "ios"], "player_skip": ["js", "configs"]}},
                "youtube_include_dash_manifest": False, "youtube_include_hls_manifest": False, **self.cookies}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://youtu.be/{video_id}", download=False)
        except Exception:
            return None
        if not info:
            return None
        video = self._video({**info, "id": video_id}, "videos")
        video.tags = [t for t in (info.get("tags") or []) if isinstance(t, str)]
        return video

    def probe_date(self, video_id):
        info = self.video_info(video_id)
        return info.date() if info and info.timestamp is not None and not info.approx else None

    # ---- downloading
    def download(self, video_id, outtmpl, max_height, stop=None):
        yt_dlp = _yt_dlp()

        def hook(d):
            if stop is not None and stop.is_set():
                raise DownloadStopped()

        h = int(max_height)
        opts = {"format": f"bv*[height<={h}]+ba/b[height<={h}]/b", "merge_output_format": "mp4",
                "outtmpl": outtmpl, "quiet": True, "no_warnings": True, "noplaylist": True,
                "concurrent_fragment_downloads": 4, "progress_hooks": [hook], **self.cookies}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True) or {}
        meta = {"title": info.get("title"), "views": info.get("view_count"), "duration": info.get("duration")}
        ud = info.get("upload_date")
        if ud:
            try:
                meta["published"] = datetime.strptime(ud, "%Y%m%d").date().isoformat()
            except ValueError:
                pass
        return {k: v for k, v in meta.items() if v is not None}
