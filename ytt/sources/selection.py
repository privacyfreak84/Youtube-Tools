"""Choosing videos from a channel's list: dates, limits, order, positions, look-alike re-uploads.
Ported from auto_compile.py (same behaviour, same tests' expectations); nothing here touches the network
except through the `probe` function a caller hands to DateResolver, and nothing prints."""
import random
import re
from datetime import date, datetime, timezone

from ytt.sources.errors import SourceError

SORTS = ("popular", "unpopular", "oldest", "latest", "longest", "shortest", "title", "random")
SORT_WORDS = {"popular": "most viewed", "unpopular": "least viewed", "oldest": "oldest", "latest": "newest",
              "longest": "longest", "shortest": "shortest", "title": "title A-Z", "random": "random"}


# ---------------------------------------------------------------- dates
def parse_date(text):
    """2024-01-31, 20240131 or 31-01-2024 -> date. Raises ValueError."""
    t = text.strip().replace("/", "-").replace(".", "-")
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t) or re.fullmatch(r"(\d{4})(\d{2})(\d{2})", t)
    try:
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.fullmatch(r"(\d{1,2})-(\d{1,2})-(\d{4})", t)
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        pass
    raise ValueError(f"can't read the date '{text}' (use something like 2024-01-31)")


class DateResolver:
    """Looks up exact upload dates only when asked, and remembers them. `probe(video_id)` returns a date or None;
    on_probe(n) is called before each lookup so a caller can show that something is happening."""

    def __init__(self, probe, on_probe=None):
        self.probe = probe
        self.on_probe = on_probe
        self.cache = {}
        self.probes = 0

    def at(self, video):
        if video.id in self.cache:
            return self.cache[video.id]
        d = None
        if video.timestamp is not None and not video.approx:
            d = video.date()
        if d is None:
            self.probes += 1
            if self.on_probe:
                self.on_probe(self.probes)
            d = self.probe(video.id)
        if d is None and video.timestamp is not None:           # last resort: the channel page's rough date
            d = video.date()
        self.cache[video.id] = d
        return d

    def nearby(self, rows, mid, lo, hi):
        """First readable date at or around index mid (staying inside lo..hi-1)."""
        order = list(range(mid, min(hi, mid + 4))) + list(range(mid - 1, max(lo, mid - 4) - 1, -1))
        for k in order:
            d = self.at(rows[k])
            if d is not None:
                return k, d
        return None, None


def _first_index(rows, pred, resolver, lo=0):
    """rows are newest-first, so dates only go down the list. Find the first row whose date satisfies pred
    (False for the newer rows, True from some point on). Needs only about log2(n) lookups."""
    hi = len(rows)
    while lo < hi:
        mid = (lo + hi) // 2
        k, d = resolver.nearby(rows, mid, lo, hi)
        if d is None:
            raise SourceError("couldn't read upload dates around one part of the list "
                              "(try again later, or update yt-dlp:  pip install -U yt-dlp)")
        if pred(d):
            hi = k
        else:
            lo = k + 1
    return lo


def within_dates(rows, d_from, d_to, resolver):
    """rows newest-first -> the ones uploaded between the two dates (either may be None = no limit)."""
    start = 0 if d_to is None else _first_index(rows, lambda d: d <= d_to, resolver)
    end = len(rows) if d_from is None else _first_index(rows, lambda d: d < d_from, resolver, lo=start)
    return rows[start:end]


# ---------------------------------------------------------------- limits and order
def within_limits(rows, min_views=0, min_minutes=0, max_minutes=0):
    """Drop videos outside the limits. A video whose length or views are unknown is kept."""
    out = []
    for v in rows:
        if v.duration is not None and max_minutes and v.duration > max_minutes * 60:
            continue
        if v.duration is not None and min_minutes and v.duration < min_minutes * 60:
            continue
        if v.views is not None and min_views and v.views < min_views:
            continue
        out.append(v)
    return out


def _by(rows, attr, descending):
    have = [r for r in rows if getattr(r, attr) is not None]
    missing = [r for r in rows if getattr(r, attr) is None]          # unknown values always go last
    return sorted(have, key=lambda r: getattr(r, attr), reverse=descending) + missing


def rank(rows, how, rng=None):
    """rows come newest-first (YouTube's own order). Ties keep that order. rng is for tests (random order)."""
    if how == "popular":
        return _by(rows, "views", True)
    if how == "unpopular":
        return _by(rows, "views", False)
    if how == "oldest":
        return list(reversed(rows))
    if how == "longest":
        return _by(rows, "duration", True)
    if how == "shortest":
        return _by(rows, "duration", False)
    if how == "title":
        return sorted(rows, key=lambda r: (r.title or "").casefold())
    if how == "random":
        shuffled = list(rows)
        (rng or random).shuffle(shuffled)
        return shuffled
    return list(rows)                                                # "latest"


# ---------------------------------------------------------------- picking positions (--range, --take)
def parse_take(spec):
    """'60' | 'last:20' | 'new:60' | 'comps:5' | 'random:30' | 'every:5' | '25-70' | '1-10,25,40-' ...
    -> ('first'|'last'|'new'|'comps'|'random', N)  or  ('positions', [(start, end_or_None, step), ...])"""
    t = re.sub(r"\s+", "", str(spec).lower())
    if not t:
        raise ValueError("nothing to take - try something like 60, 25-70 or last:20")
    if t.isdigit():
        if int(t) < 1:
            raise ValueError("the number must be at least 1")
        return ("first", int(t))
    m = re.fullmatch(r"(first|last|new|random|every|comps):(\d+)", t)
    if m:
        n = int(m.group(2))
        if n < 1:
            raise ValueError("the number must be at least 1")
        return ("positions", [(1, None, n)]) if m.group(1) == "every" else (m.group(1), n)
    parts = []
    for tok in t.split(","):
        if not tok:
            continue
        m = re.fullmatch(r"(\d*)(-(\d*))?(?:/(\d+))?", tok)
        if not m or (not m.group(1) and not m.group(2)):
            raise ValueError(f"can't read '{tok}' - use things like 25, 25-70, 25-, -30 or 1-100/10")
        a = int(m.group(1)) if m.group(1) else 1
        if not m.group(2):
            b = a                                                   # a single position
        else:
            b = int(m.group(3)) if m.group(3) else None             # open-ended: to the last video
        step = int(m.group(4)) if m.group(4) else 1
        if a < 1 or step < 1 or (b is not None and b < a):
            raise ValueError(f"'{tok}' doesn't make sense (positions start at 1 and must go upwards)")
        parts.append((a, b, step))
    if not parts:
        raise ValueError("nothing to take")
    return ("positions", parts)


def parse_range(text):
    """--range: exactly one stretch of positions, written A-B, A- or -B. Returns the same as parse_take."""
    t = re.sub(r"\s+", "", str(text))
    if not re.fullmatch(r"\d*-\d*", t) or t == "-":
        raise ValueError(f"--range needs a stretch of positions like 25-70, 25- or -30 (got '{text}'). "
                         f"For a number of clips use --clips N; for anything fancier use --take.")
    return parse_take(t)


def apply_take(rows, take, skip=(), rng=None):
    """rows: the filtered, sorted list; take: what parse_take returned. Returns [(position, video), ...] in list
    order (positions are 1-based). 'new' skips the ids in `skip` and moves further down the list."""
    kind, val = take
    if kind == "comps":
        raise ValueError("a 'comps:N' take has to be turned into 'new:N' first")
    n = len(rows)
    if kind == "first":
        idx = list(range(min(val, n)))
    elif kind == "last":
        idx = list(range(max(0, n - val), n))
    elif kind == "random":
        idx = sorted((rng or random).sample(range(n), min(val, n)))
    elif kind == "new":
        idx = [i for i, r in enumerate(rows) if r.id not in skip][:val]
    else:
        chosen = set()
        for a, b, step in val:
            for p in range(a, (n if b is None else min(b, n)) + 1, step):
                chosen.add(p - 1)
        idx = sorted(chosen)
    return [(i + 1, rows[i]) for i in idx]


def describe_take(take):
    kind, val = take
    if kind == "first":
        return f"the first {val}"
    if kind == "last":
        return f"the last {val}"
    if kind == "random":
        return f"{val} random ones (shown in list order)"
    if kind == "new":
        return f"the next {val} not in your library yet"
    if kind == "comps":
        return f"enough new videos for {val} compilation{'' if val == 1 else 's'}"
    if len(val) == 1 and val[0][2] > 1 and val[0][0] == 1 and val[0][1] is None:
        return f"every {val[0][2]}th video"
    bits = []
    for a, b, step in val:
        s = f"{a}" if b == a else f"{a}-{b if b is not None else 'end'}"
        bits.append(s + (f" (every {step}th)" if step > 1 else ""))
    return "positions " + ", ".join(bits)


# ---------------------------------------------------------------- re-uploads
def dup_key(title, duration):
    """Same title (ignoring case and punctuation) and same length to the second = probably the same clip.
    Only the first 70 characters of a title count: the old tool kept titles cut to 70, and library entries
    imported from it must still match a fresh listing."""
    t = re.sub(r"[\W_]+", " ", (title or "")[:70].casefold()).strip()
    if not t or duration is None:
        return None
    return (t, int(round(duration)))


def find_likely_duplicates(ranked, have_ids, have_keys):
    """Ids in `ranked` that look like re-uploads: same title and length as a clip already in the library
    (have_ids / have_keys) or as one that ranks higher in this list. A guess, so callers always say so."""
    seen = set(have_keys)
    for v in ranked:
        if v.id in have_ids:
            k = dup_key(v.title, v.duration)
            if k:
                seen.add(k)
    dups = set()
    for v in ranked:
        if v.id in have_ids:
            continue
        k = dup_key(v.title, v.duration)
        if not k:
            continue
        if k in seen:
            dups.add(v.id)
        else:
            seen.add(k)
    return dups
