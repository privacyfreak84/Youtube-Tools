"""clip: the same discovery and outlier scan as niche, tuned for finding fresh clips to use: every outlier gets its
real date looked up, anything older than `max_age_days` (or with no readable date) is dropped, and the rest come
best first (the old ClipFinder). Freshness is the point of a repost or remix workflow."""
from dataclasses import dataclass, field

from ytt.ops.research import channels as channels_mod
from ytt.ops.research import outliers as outliers_mod
from ytt.ops.research.common import ResearchError, utc_now
from ytt.ops.research.niche import require_sources


@dataclass
class ClipsRequest:
    searches: list = field(default_factory=list)
    seeds: list = field(default_factory=list)
    count: int = 30
    content_type: str = "all"                   # clips can be Shorts or long videos
    multiplier: float = 3.0
    max_age_days: int = 7
    limit: int = None
    top: int = 20                               # None or 0 = no cap


@dataclass
class ClipsResult:
    channels: list = field(default_factory=list)
    rows: list = field(default_factory=list)    # fresh outliers, best ratio first
    candidates: int = 0                         # outliers before the freshness filter
    dropped: int = 0                            # too old, or no readable date
    notes: list = field(default_factory=list)


def find_clips(request, backend, now=None, on_progress=None):
    r = request
    require_sources(r.searches, r.seeds, r.count)
    if r.max_age_days < 0:
        raise ResearchError("--max-age-days can't be negative")
    if r.top is not None and r.top < 0:
        raise ResearchError("--top can't be negative")
    outlier_request = outliers_mod.OutlierRequest(
        channels=[], content_type=r.content_type, multiplier=r.multiplier, limit=r.limit, top=None, sort="ratio",
        resolve_dates=True)                     # no cut before the dates are known: every candidate is looked up
    outliers_mod.check(outlier_request, need_channels=False)
    result = ClipsResult()
    result.channels = channels_mod.discover(r.searches, r.seeds, r.count, backend, result.notes, on_progress)
    if not result.channels:
        return result
    if on_progress:
        on_progress(f"{len(result.channels)} unique channel(s) discovered; scanning for outliers ...")
    outlier_request.channels = [c.url for c in result.channels]
    found = outliers_mod.find_outliers(outlier_request, backend, now or utc_now(), on_progress)
    result.notes += found.notes
    result.candidates = len(found.rows)
    result.rows = [o for o in found.rows if o.days_ago is not None and o.days_ago <= r.max_age_days]
    result.dropped = result.candidates - len(result.rows)
    if r.top:
        result.rows = result.rows[:r.top]
    return result
