"""The one interface between ytt and YouTube. The real thing is ytt.sources.ytdlp.YtDlpBackend; tests use a fake
with the same three methods, so everything above this line can be tested without a network."""
from typing import Protocol


class Backend(Protocol):
    def list_tab(self, base_url, tab):
        """One tab ('videos' or 'shorts') of a channel -> [VideoInfo], newest first. Raises SourceError."""

    def channel_tab(self, base_url, tab, limit=None):
        """One tab ('videos', 'shorts' or 'streams') of a channel, with the channel's name, subscriber count and
        verified flag -> ChannelTab. `limit` keeps only the newest N. A tab the channel doesn't have gives an empty
        ChannelTab. Raises SourceError when the page can't be read."""

    def probe_date(self, video_id):
        """The exact upload date of one video (a date), or None if it can't be read."""

    def download(self, video_id, outtmpl, max_height, stop=None):
        """Download one video to `outtmpl` (a yt-dlp template ending in .%(ext)s). `stop` is a threading.Event; when
        it is set the download ends at once by raising DownloadStopped. Returns a dict of what YouTube said about
        the video (any of: title, views, duration, published as YYYY-MM-DD). Raises on failure."""
