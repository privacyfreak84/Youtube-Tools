"""live: which streams are live right now on these channels (the old LiveExtractor). A channel's streams tab lists
live and upcoming streams before past ones, so only the first `limit` entries are checked, each by its own page."""
from dataclasses import dataclass, field

from ytt.ops.research.common import ResearchError, base_channel_url
from ytt.sources.errors import SourceError

DEFAULT_LIMIT = 15


@dataclass
class LiveRequest:
    channels: list
    limit: int = DEFAULT_LIMIT      # entries to check per channel; 0 = the whole tab


@dataclass
class ChannelLive:
    channel_url: str                # the address that was read
    streams: list = field(default_factory=list)       # [LiveInfo]


@dataclass
class LiveResult:
    channels: list = field(default_factory=list)      # [ChannelLive], in the order asked
    notes: list = field(default_factory=list)

    @property
    def streams(self):
        return [s for c in self.channels for s in c.streams]


def listing_url(channel):
    """What to read for a channel: its streams tab, or its live tab when the link ends in /live. A playlist or
    watch link is used as it is."""
    raw = channel.strip().rstrip("/")
    if "/watch" in raw or "/playlist" in raw:
        return raw
    return f"{base_channel_url(raw)}/{'live' if raw.endswith('/live') else 'streams'}"


def file_name(channel_url):
    """The name of the JSON file kept for a channel: @Name/streams -> Name.json."""
    parts = channel_url.rstrip("/").split("/")
    name = parts[-2] if parts[-1] in ("streams", "live") else parts[-1]
    name = name[1:] if name.startswith("@") else name
    name = name or "channel"
    return "".join("_" if c in '<>:"/\\|?*' else c for c in name) + ".json"


def find_live(request, backend, on_progress=None):
    if not request.channels:
        raise ResearchError("give at least one channel")
    if request.limit < 0:
        raise ResearchError("--limit must be 0 (the whole tab) or more")
    result = LiveResult()
    for channel in request.channels:
        url = listing_url(channel)
        entry = ChannelLive(url)
        result.channels.append(entry)
        if on_progress:
            on_progress(f"checking {url} ...")
        try:
            listed = backend.list_url(url, request.limit or None)
        except SourceError as e:
            result.notes.append(f"skipped {url}: {e}")
            continue
        seen = set()
        for i, video in enumerate(listed, 1):
            if on_progress:
                on_progress(f"[{i}/{len(listed)}] checking: {(video.title or video.id)[:50]}")
            try:
                live = backend.live_status(video.id)
            except SourceError as e:
                result.notes.append(f"skipped {video.id}: {e}")
                continue
            if live is None or live.url in seen:
                continue
            seen.add(live.url)
            entry.streams.append(live)
    return result
