"""Cutting a pool of clips into compilations (DESIGN.md sections 9-11). Ported from make_compilations.py
(`order_pool`, `build_groups`). Pure functions: no files, no database, no printing, so they can be tested with
plain numbers.

A "clip" here is any object; what it needs is described by the small accessors passed in. A clip whose length
cannot be found (an unreadable file) is reported as bad and left out, never guessed at.
"""
import random
from dataclasses import dataclass

ORDERS = ("name", "oldest", "newest", "random")      # name = the order they were fetched in (library order)
PLAYS = ("asis", "reverse", "each")                  # reverse: the whole sequence backwards; each: every compilation backwards
IF_SHORT = ("keep", "short", "fetch")                # what to do with clips that cannot fill a whole compilation


@dataclass(frozen=True)
class Sizing:
    """How big one compilation is: a number of clips, or about so many seconds."""
    mode: str = "count"             # count | minutes
    clips: int = 15
    seconds: float = 600.0

    def label(self, exact=True):
        if self.mode == "count":
            return f"{self.clips} clips"
        return f"about {self.seconds / 60:g} min"


def sizing_from(make_cfg, clips_each=0, minutes_each=0):
    """The workspace's make defaults, overridden by a request that names a size (clips_each wins over minutes_each
    only because both being given is refused earlier)."""
    if clips_each:
        return Sizing("count", int(clips_each), 0.0)
    if minutes_each:
        return Sizing("minutes", 0, float(minutes_each) * 60)
    if make_cfg["size_mode"] == "count":
        return Sizing("count", int(make_cfg["clips_each"]), 0.0)
    return Sizing("minutes", 0, float(make_cfg["minutes_each"]) * 60)


def order_pool(pool, order, seed=0, n_used=0, date_of=None):
    """pool: clips in library order -> the same clips in the order they should be used.
    name: as given (the order they were fetched in). oldest/newest: by upload date; clips with no known date keep
    their library place after the dated ones. random: shuffled with a seed, so a dry run and the real run agree."""
    pool = list(pool)
    if order == "random":
        random.Random((seed or 0) + n_used).shuffle(pool)
    elif order in ("oldest", "newest"):
        dates = [(date_of(c) if date_of else None) for c in pool]
        dated = [(d, c) for d, c in zip(dates, pool) if d]
        undated = [c for d, c in zip(dates, pool) if not d]
        dated.sort(key=lambda t: t[0], reverse=order == "newest")      # a sort keeps equal dates in library order
        pool = [c for _, c in dated] + undated
    return pool


def build_groups(pool, sizing, duration_of, max_groups=None, include_leftover=False):
    """Walk the ordered pool and cut it into compilations -> (groups, held, bad).
    duration_of(clip) -> seconds, or None when the clip cannot be read (it is left out and listed in `bad`).
    A compilation is closed when it has the number of clips (or the length) asked for, and has at least 2 clips.
    held: the clips left over that did not fill one. include_leftover turns a held group of 2+ into a last, shorter
    compilation. max_groups stops as soon as that many are made (what is left is spare, not leftover)."""
    groups, cur, cur_secs, bad = [], [], 0.0, []
    by_count = sizing.mode == "count"
    for clip in pool:
        if max_groups is not None and len(groups) >= max_groups:
            break
        seconds = duration_of(clip)
        if seconds is None:
            bad.append(clip)
            continue
        cur.append(clip)
        cur_secs += seconds
        full = len(cur) >= sizing.clips if by_count else cur_secs >= sizing.seconds
        if full and len(cur) >= 2:
            groups.append(cur)
            cur, cur_secs = [], 0.0
    held = cur
    if include_leftover and len(held) >= 2 and (max_groups is None or len(groups) < max_groups):
        groups.append(held)
        held = []
    return groups, held, bad


def play_order(groups, play):
    """--reverse is applied to the pool before grouping (see `reverse_pool`); --reverse-each flips each group."""
    if play == "each":
        return [g[::-1] for g in groups]
    return groups


def reverse_pool(pool, play):
    return list(reversed(pool)) if play == "reverse" else list(pool)
