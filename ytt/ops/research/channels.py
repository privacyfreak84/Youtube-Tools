"""channels: find channels worth looking at, by YouTube search and/or a channel's own Channels tab (the old
ChannelFinder). `discover` is the same step the niche report and the clip finder start from."""
from dataclasses import dataclass, field

from ytt.ops.research.common import ResearchError, base_channel_url
from ytt.sources.errors import SourceError


@dataclass
class ChannelsRequest:
    searches: list = field(default_factory=list)    # keywords; each gives `count` results
    seeds: list = field(default_factory=list)       # channels whose Channels tab is read
    count: int = 30
    with_subs: bool = False                         # subscriber count and verified flag, one request per channel


@dataclass
class Found:
    name: str
    url: str
    source: str                                     # 'search:"cats"', 'featured-by:@Chan', or both joined by ', '
    subs: int = None
    verified: bool = None

    def as_dict(self):
        return {"name": self.name, "verified": self.verified, "subs": self.subs, "url": self.url, "source": self.source}


@dataclass
class ChannelsResult:
    channels: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def _merge(found, refs, source):
    for ref in refs:
        if ref.key in found:
            if source not in found[ref.key].source.split(", "):
                found[ref.key].source += f", {source}"
        else:
            found[ref.key] = Found(ref.name, ref.url, source)


def discover(searches, seeds, count, backend, notes, on_progress=None):
    """Search for each keyword and read each seed's Channels tab; a channel found twice is listed once with both
    sources. A search or seed that fails is a note, not the end."""
    found = {}
    for query in searches:
        if on_progress:
            on_progress(f"searching: {query}")
        try:
            _merge(found, backend.search_channels(query, count), f'search:"{query}"')
        except SourceError as e:
            notes.append(f"search failed for {query!r}: {e}")
    for seed in seeds:
        if on_progress:
            on_progress(f"reading the Channels tab: {seed}")
        try:
            refs = backend.featured_channels(base_channel_url(seed))
        except SourceError as e:
            notes.append(f"no Channels tab for {seed}: {e}")
            continue
        _merge(found, refs, f"featured-by:{seed}")
    return list(found.values())


def find_channels(request, backend, on_progress=None):
    r = request
    if not r.searches and not r.seeds:
        raise ResearchError("give at least one --search keyword or --seed channel")
    if r.count < 1:
        raise ResearchError("--count must be 1 or more")
    result = ChannelsResult()
    result.channels = discover(r.searches, r.seeds, r.count, backend, result.notes, on_progress)
    if r.with_subs:
        for i, c in enumerate(result.channels, 1):
            if on_progress:
                on_progress(f"[{i}/{len(result.channels)}] details for {c.name}")
            try:
                page = backend.channel_tab(base_channel_url(c.url), "videos", 1)
            except SourceError:
                continue
            c.subs, c.verified = page.subs, page.verified
    return result
