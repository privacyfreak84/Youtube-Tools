"""table: a channel's videos as rows: title, views, upload date, days since, length (the old ChannelTable).
Dates read off the channel page are rough; `full` asks each video's own page for the exact date and length."""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ytt.ops.research.common import ResearchError, base_channel_url, days_between, utc_now
from ytt.sources.errors import SourceError

TABS = ("videos", "shorts", "streams")
SORTS = ("latest", "oldest", "popular")


@dataclass
class TableRequest:
    channel: str
    content_type: str = "videos"    # videos | shorts | streams | all
    sort: str = "latest"
    limit: int = None               # keep only N rows after sorting
    full: bool = False              # one request per video for exact date, length and views


@dataclass
class Row:
    id: str
    title: str
    views: int
    timestamp: float                # UTC seconds, or None
    approx: bool                    # True when the date is only a rough value from the channel page
    duration: float
    type: str

    @property
    def date(self):
        return None if self.timestamp is None else datetime.fromtimestamp(self.timestamp, tz=timezone.utc).date()

    @property
    def url(self):
        return f"https://youtu.be/{self.id}"

    def days_ago(self, now):
        return days_between(self.timestamp, now)

    def as_dict(self, now):
        return {"id": self.id, "title": self.title, "type": self.type, "views": self.views,
                "upload_date": self.date.isoformat() if self.date else None, "days_ago": self.days_ago(now),
                "approx": self.approx, "duration": self.duration, "url": self.url}


@dataclass
class TableResult:
    rows: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def sort_rows(rows, how):
    """Anything without a date (latest, oldest) or views (popular) goes last."""
    if how == "popular":
        have = sorted((r for r in rows if r.views is not None), key=lambda r: r.views, reverse=True)
        return have + [r for r in rows if r.views is None]
    have = sorted((r for r in rows if r.timestamp is not None), key=lambda r: r.timestamp, reverse=(how == "latest"))
    return have + [r for r in rows if r.timestamp is None]


def _check(r):
    if not r.channel.strip():
        raise ResearchError("give a channel")
    if r.content_type not in (*TABS, "all"):
        raise ResearchError(f"--type must be one of {', '.join((*TABS, 'all'))}")
    if r.sort not in SORTS:
        raise ResearchError(f"--sort must be one of {', '.join(SORTS)}")
    if r.limit is not None and r.limit < 1:
        raise ResearchError("--limit must be 1 or more")


def channel_table(request, backend, now=None, on_progress=None):
    _check(request)
    r = request
    now = now or utc_now()
    base = base_channel_url(r.channel)
    result = TableResult()
    # "popular" and "oldest" have no pre-sorted feed on YouTube: the whole list has to be read and sorted here, and only
    # then cut to --limit. Cutting the read would sort an arbitrary recent window and give the wrong top N.
    read_limit = r.limit if r.sort == "latest" else None
    if r.limit and read_limit is None:
        result.notes.append(f"--sort {r.sort} reads the whole {r.content_type} list before it can pick the {r.limit}; "
                            f"that may take a while on a big channel")
    seen = set()
    for tab in TABS if r.content_type == "all" else (r.content_type,):
        if on_progress:
            on_progress(f"fetching {tab}: {base}/{tab}")
        try:
            page = backend.channel_tab(base, tab, read_limit)
        except SourceError as e:
            result.notes.append(f"skipped {tab}: {e}")
            continue
        for v in page.videos:
            if v.id in seen:
                continue
            seen.add(v.id)
            result.rows.append(Row(v.id, v.title or "N/A", v.views, v.timestamp, v.approx, v.duration, tab))
    result.rows = sort_rows(result.rows, r.sort)
    if r.limit:
        result.rows = result.rows[:r.limit]
    if r.full:
        for i, row in enumerate(result.rows, 1):
            if on_progress:
                on_progress(f"[{i}/{len(result.rows)}] {row.title[:55]}")
            info = backend.video_info(row.id)
            if not info:
                continue
            if info.timestamp is not None:
                row.timestamp, row.approx = info.timestamp, info.approx
            if info.duration is not None:
                row.duration = info.duration
            if info.views is not None:
                row.views = info.views
    return result
