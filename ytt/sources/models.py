"""What ytt knows about a video on YouTube (not about any local file). The YouTube id is its one identity."""
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class VideoInfo:
    id: str
    title: str = ""
    views: int = None
    duration: float = None          # seconds
    timestamp: float = None         # upload time, UTC seconds since 1970
    approx: bool = False            # True when the timestamp is only a rough value from the channel page
    tab: str = "videos"

    def date(self):
        if self.timestamp is None:
            return None
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc).date()


@dataclass
class ChannelTab:
    """One tab of a channel as research sees it: who the channel is, plus its videos (newest first, any of which
    may lack a view count or a date)."""
    name: str = None
    subs: int = None
    verified: bool = None
    videos: list = field(default_factory=list)


@dataclass
class ChannelRef:
    """A channel found by a search or on another channel's Channels tab. `key` is how two finds of the same channel
    are recognised (the channel id when YouTube gave one, otherwise the address)."""
    key: str
    name: str
    url: str


@dataclass
class LiveInfo:
    """A video that is live right now."""
    id: str
    title: str
    channel: str
    url: str
    viewers: int = None             # watching right now
    views: int = None
