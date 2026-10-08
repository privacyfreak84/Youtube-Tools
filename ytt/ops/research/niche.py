"""niche: find the channels behind a search or a seed channel, then look for outliers across all of them in one
pass (the old NicheReport). A thin layer over `channels.discover` and `outliers.find_outliers`."""
from dataclasses import dataclass, field

from ytt.ops.research import channels as channels_mod
from ytt.ops.research import outliers as outliers_mod
from ytt.ops.research.common import ResearchError, utc_now


@dataclass
class NicheRequest:
    searches: list = field(default_factory=list)
    seeds: list = field(default_factory=list)
    count: int = 30
    content_type: str = "videos"
    multiplier: float = 3.0
    limit: int = None
    top: int = None
    sort: str = "ratio"
    reverse: bool = False
    resolve_dates: bool = False


@dataclass
class NicheResult:
    channels: list = field(default_factory=list)                   # [channels.Found]
    outliers: outliers_mod.OutlierResult = None                    # None when no channels were found
    notes: list = field(default_factory=list)

    @property
    def rows(self):
        return self.outliers.rows if self.outliers else []


def require_sources(searches, seeds, count):
    if not searches and not seeds:
        raise ResearchError("give at least one --search keyword or --seed channel")
    if count < 1:
        raise ResearchError("--count must be 1 or more")


def find_niche(request, backend, now=None, on_progress=None):
    r = request
    require_sources(r.searches, r.seeds, r.count)
    outlier_request = outliers_mod.OutlierRequest(
        channels=[], content_type=r.content_type, multiplier=r.multiplier, limit=r.limit, top=r.top, sort=r.sort,
        reverse=r.reverse, resolve_dates=r.resolve_dates)
    outliers_mod.check(outlier_request, need_channels=False)           # a bad option stops here, before any searching
    result = NicheResult()
    result.channels = channels_mod.discover(r.searches, r.seeds, r.count, backend, result.notes, on_progress)
    if not result.channels:
        return result
    if on_progress:
        on_progress(f"{len(result.channels)} unique channel(s) discovered; scanning for outliers ...")
    outlier_request.channels = [c.url for c in result.channels]
    result.outliers = outliers_mod.find_outliers(outlier_request, backend, now or utc_now(), on_progress)
    result.notes += result.outliers.notes
    return result
