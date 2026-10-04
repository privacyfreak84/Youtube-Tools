"""A fake YouTube for the new code: the same three methods as ytt.sources.backend.Backend, no network.
Downloads are copies of one tiny generated clip (so ffmpeg can really use them)."""
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ytt.sources.errors import DownloadStopped, SourceError
from ytt.sources.models import VideoInfo

try:
    from test_auto_compile import template_clip
except ImportError:
    from tests.test_auto_compile import template_clip


def make_videos(n, newest=datetime(2026, 9, 30, 12, tzinfo=timezone.utc), title="Clip {i}", duration=20, tab="shorts"):
    """n fake channel entries, newest first, one per day going back. ids are vid00000000, vid00000001, ... (11 chars)"""
    return [VideoInfo(id=f"vid{i:08d}", title=title.format(i=i), views=1000 + i, duration=duration,
                      timestamp=(newest - timedelta(days=i)).timestamp(), approx=False, tab=tab) for i in range(n)]


class FakeBackend:
    def __init__(self, videos=None, tab="shorts"):
        self.videos = {tab: list(videos if videos is not None else make_videos(40, tab=tab))}
        self.fail = set()               # ids whose download raises
        self.slow = {}                  # id -> seconds to wait before finishing
        self.interrupt = set()          # ids whose download raises KeyboardInterrupt (Ctrl-C)
        self.downloads = []             # ids that finished, in the order they finished
        self.started = []               # ids whose download began
        self.listed = []                # (base_url, tab) of every list_tab call
        self.probes = []
        self.heights = []
        self.list_error = None
        self._lock = threading.Lock()

    def list_tab(self, base_url, tab):
        self.listed.append((base_url, tab))
        if self.list_error:
            raise SourceError(self.list_error)
        return list(self.videos.get(tab, []))

    def probe_date(self, video_id):
        self.probes.append(video_id)
        for rows in self.videos.values():
            for v in rows:
                if v.id == video_id and v.timestamp is not None:
                    return v.date()
        return None

    def download(self, video_id, outtmpl, max_height, stop=None):
        with self._lock:
            self.started.append(video_id)
            self.heights.append(max_height)
        if video_id in self.interrupt:
            raise KeyboardInterrupt
        delay = self.slow.get(video_id, 0)
        waited = 0.0
        while waited < delay:                       # a real download checks `stop` as it makes progress
            if stop is not None and stop.is_set():
                Path(outtmpl.replace("%(ext)s", "mp4.part")).write_bytes(b"half")
                raise DownloadStopped()
            time.sleep(0.02)
            waited += 0.02
        if video_id in self.fail:
            Path(outtmpl.replace("%(ext)s", "mp4.part")).write_bytes(b"half")      # leaves junk, like a real failure
            raise RuntimeError("HTTP Error 403: Forbidden")
        shutil.copy(template_clip(), outtmpl.replace("%(ext)s", "mp4"))
        with self._lock:
            self.downloads.append(video_id)
        info = {"title": None, "views": None, "duration": 1.0}
        for rows in self.videos.values():
            for v in rows:
                if v.id == video_id:
                    info = {"title": v.title, "views": v.views, "duration": v.duration}
                    if v.timestamp is not None:
                        info["published"] = v.date().isoformat()
        return {k: x for k, x in info.items() if x is not None}
