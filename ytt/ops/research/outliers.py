"""outliers: videos that sit well above their own channel's usual views (the old OutlierFinder).
For each channel and tab the baseline is the median views of the videos scanned; a video is an outlier when its
views are at least `multiplier` times that. Nothing here prints: the result carries the rows and the notes."""
import statistics
from dataclasses import dataclass, field

from ytt.ops.research.common import ResearchError, base_channel_url, days_between, utc_now
from ytt.sources.errors import SourceError

TABS = ("videos", "shorts", "streams")
MIN_SAMPLE = 3                      # below this a median is not a meaningful baseline
SORTS = ("ratio", "views", "subs", "days_ago", "baseline", "channel", "type")
SORT_DEFAULT_DESC = {"ratio": True, "views": True, "subs": True, "days_ago": False, "baseline": True,
                     "channel": False, "type": False}                    # True = highest first
GROUPS = ("none", "channel", "type")


@dataclass
class OutlierRequest:
    channels: list
    content_type: str = "videos"    # videos | shorts | streams | all
    multiplier: float = 3.0
    limit: int = None               # newest N videos per channel and tab (default: all)
    top: int = None                 # keep only the best N overall
    sort: str = "ratio"
    reverse: bool = False
    resolve_dates: bool = False     # look up the real date of outliers that have none (mainly Shorts)


@dataclass
class Outlier:
    id: str
    channel: str
    subs: int
    verified: bool
    title: str
    views: int
    baseline_median: float
    ratio: float
    days_ago: int
    type: str
    approx: bool = False            # True when days_ago is only a rough value from the channel page ("1 month ago")

    @property
    def url(self):
        return f"https://youtu.be/{self.id}"

    def as_dict(self):
        return {"id": self.id, "channel": self.channel, "verified": self.verified, "subs": self.subs,
                "title": self.title, "views": self.views, "baseline_median": self.baseline_median,
                "ratio": self.ratio, "days_ago": self.days_ago, "approx": self.approx, "type": self.type,
                "url": self.url}


@dataclass
class OutlierResult:
    rows: list = field(default_factory=list)
    notes: list = field(default_factory=list)       # channels or tabs that were skipped, and why
    multiplier: float = 3.0
    scanned: int = 0                                # videos looked at (with a view count)


_KEYS = {"ratio": lambda r: r.ratio, "views": lambda r: r.views, "subs": lambda r: r.subs,
         "days_ago": lambda r: r.days_ago, "baseline": lambda r: r.baseline_median,
         "channel": lambda r: r.channel.lower(), "type": lambda r: r.type}


def sort_rows(rows, field_name, reverse):
    """Sort by a field. Rows with no value for it always come last, whichever way it is sorted."""
    key = _KEYS[field_name]
    have = sorted((r for r in rows if key(r) is not None), key=key, reverse=reverse)
    return have + [r for r in rows if key(r) is None]


def grouped(rows, by):
    """[(key, rows)] in the order each key first appears. by is 'channel' or 'type'."""
    groups = {}
    for r in rows:
        groups.setdefault(getattr(r, by), []).append(r)
    return list(groups.items())


def check(request, need_channels=True):
    """Stop a bad request before anything is read. need_channels=False is for tools that find the channels themselves."""
    r = request
    if need_channels and not r.channels:
        raise ResearchError("give at least one channel (or --channels-file)")
    if r.content_type not in (*TABS, "all"):
        raise ResearchError(f"--type must be one of {', '.join((*TABS, 'all'))}")
    if r.sort not in SORTS:
        raise ResearchError(f"--sort must be one of {', '.join(SORTS)}")
    if r.multiplier <= 0:
        raise ResearchError("--multiplier must be more than 0")
    if r.limit is not None and r.limit < 1:
        raise ResearchError("--limit must be 1 or more")
    if r.top is not None and r.top < 1:
        raise ResearchError("--top must be 1 or more")


def _resolve_dates(rows, backend, now, on_progress):
    """Look up the exact date of every row that has none or only a rough one (the channel page says "1 week ago",
    which is 7 days here but can be 13 in fact). A date that cannot be read leaves the row as it was."""
    pending = [r for r in rows if r.days_ago is None or r.approx]
    for i, r in enumerate(pending, 1):
        if on_progress:
            on_progress(f"[{i}/{len(pending)}] resolving date: {r.title[:50]}")
        d = backend.probe_date(r.id)
        if d is not None:
            r.days_ago = (now.date() - d).days
            r.approx = False


def find_outliers(request, backend, now=None, on_progress=None):
    """Scan the channels and return the outliers, sorted. on_progress(text) is told what is being read."""
    check(request)
    r = request
    now = now or utc_now()
    tabs = TABS if r.content_type == "all" else (r.content_type,)
    result = OutlierResult(multiplier=r.multiplier)
    for channel in r.channels:
        base = base_channel_url(channel)
        found_any = False
        for tab in tabs:
            if on_progress:
                on_progress(f"fetching {channel} [{tab}] ...")
            try:
                page = backend.channel_tab(base, tab, r.limit)
            except SourceError as e:
                result.notes.append(f"skipped {channel} [{tab}]: {e}")
                continue
            videos = [v for v in page.videos if v.views is not None]    # no view count: can't baseline or flag it
            if not videos:
                continue
            found_any = True
            result.scanned += len(videos)
            if len(videos) < MIN_SAMPLE:
                result.notes.append(f"{channel} [{tab}]: only {len(videos)} video(s), too few for a reliable baseline; skipped")
                continue
            baseline = statistics.median(v.views for v in videos)
            for v in videos:
                ratio = v.views / baseline if baseline else 0
                if ratio >= r.multiplier:
                    result.rows.append(Outlier(
                        id=v.id, channel=page.name or channel, subs=page.subs, verified=page.verified,
                        title=v.title or "N/A", views=v.views, baseline_median=baseline, ratio=ratio,
                        days_ago=days_between(v.timestamp, now), type=tab, approx=bool(v.approx)))
        if not found_any:
            result.notes.append(f"no videos found for {channel}")

    # Sorting by days_ago needs real dates to rank correctly, so only then are dates resolved before sorting and
    # cutting to --top. For every other key the sort and --top come first, so only the rows that stay are looked up.
    resolve_first = r.resolve_dates and r.sort == "days_ago"
    if resolve_first:
        _resolve_dates(result.rows, backend, now, on_progress)
    result.rows = sort_rows(result.rows, r.sort, SORT_DEFAULT_DESC[r.sort] != r.reverse)
    if r.top:
        result.rows = result.rows[:r.top]
    if r.resolve_dates and not resolve_first:
        _resolve_dates(result.rows, backend, now, on_progress)
    return result
