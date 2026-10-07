"""The real YouTube access, through yt-dlp. Only this module imports yt_dlp. Covered by the fake in tests, so it is
kept small; what it does against real YouTube can only be checked on a machine that can reach YouTube."""
from datetime import datetime, timezone

from ytt.sources.errors import DownloadStopped, SourceError
from ytt.sources.models import VideoInfo


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
    def list_tab(self, base_url, tab):
        yt_dlp = _yt_dlp()
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "ignoreerrors": True,
                "extract_flat": True, "extractor_args": {"youtubetab": {"approximate_date": [""]}}, **self.cookies}
        url = f"{base_url}/{tab}"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            raise SourceError(f"could not read {url} ({_first_line(e)})")
        entries = (info or {}).get("entries") or []
        out = []
        for e in entries:
            if not e or not e.get("id"):
                continue
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
            out.append(VideoInfo(id=e["id"], title=e.get("title") or "", views=views, duration=e.get("duration"),
                                 timestamp=exact if exact is not None else ts,
                                 approx=exact is None and ts is not None, tab=tab))
        return out

    def probe_date(self, video_id):
        yt_dlp = _yt_dlp()
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "ignoreerrors": True,
                "socket_timeout": 20, "extract_flat": False,
                "extractor_args": {"youtube": {"player_client": ["android", "ios"], "player_skip": ["js", "configs"]}},
                "youtube_include_dash_manifest": False, "youtube_include_hls_manifest": False, **self.cookies}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://youtu.be/{video_id}", download=False)
            ud = (info or {}).get("upload_date")
            return datetime.strptime(ud, "%Y%m%d").date() if ud else None
        except Exception:
            return None

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
