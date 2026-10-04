"""What ytt knows about a video on YouTube (not about any local file). The YouTube id is its one identity."""
from dataclasses import dataclass
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
