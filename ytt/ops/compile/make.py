"""make: fetch clips (when a channel is given), then cut the clips waiting in the library into compilations and
render them (DESIGN.md sections 9-11).

Stages: resolve -> validate -> plan (plan_make), then execute and record (run_make). The command line shows the
plan and asks; this module never prints or prompts. Reading a channel and downloading go through a Backend, so tests
run against a fake YouTube.

What gets made: every clip in the library that no compilation has used yet (and that can be read) is a candidate,
the ones just fetched included. `-n N` makes at most N compilations; without it everything that fills a
compilation is made. The clips that cannot fill one are the leftover and `if_short` decides what happens to them:
keep them for next time, make a shorter last compilation, or fetch just enough more to fill one.

The plan is a projection when downloads are involved: the compilations are cut from the library again once the
downloads have finished, so they can differ from the plan if a download fails and the next video takes its place.
"""
import json
import os
import random
import shutil
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime
from pathlib import Path

from ytt.engine import stitch
from ytt.engine.stitch import EngineError
from ytt.ops.compile import common
from ytt.ops.compile import fetch as fetch_mod
from ytt.ops.compile import groups as grp
from ytt.ops.compile import remake as remake_mod
from ytt.ops.compile import render as render_mod
from ytt.ops.compile.probe import ProbeCache
from ytt.ops.compile.style import Style, StyleError
from ytt.ops.errors import OpError
from ytt.ops.plan import CANCELLED, COMPLETED, FAILED, PARTIAL, Action, Item, Plan, RunResult
from ytt.sources.ytdlp import YtDlpBackend
from ytt.workspace import store

ASSUMED_CLIP_SECONDS = 30.0        # for a clip still to be downloaded whose length the channel list did not say


@dataclass
class MakeRequest:
    fetch: object = None             # a FetchRequest (the channel part), or None: use the clips already in the library
    compilations: int = 0            # make at most N compilations (0 = as many as the clips allow)
    everything: bool = False         # --all: no channel, as many as the clips in the library allow
    style: str = None                # name of a saved style; None = the workspace's default style
    per: int = 0                     # clips per compilation (0 = the workspace's size)
    per_minutes: float = 0           # or about this many minutes per compilation
    order: str = ""                  # name | oldest | newest | random ("" = the workspace's)
    play: str = "asis"               # asis | reverse (the whole sequence backwards) | each (every compilation backwards)
    if_short: str = ""               # keep | short | fetch ("" = the workspace's)
    delete_used: bool = None         # delete downloaded clips once they are in a compilation (None = the workspace's)
    retry_failed: bool = False       # try the clips that could not be read earlier once more
    seed: int = 0                    # for order random; 0 = pick one when planning

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        """Tolerant: unknown keys are ignored, missing ones take their defaults."""
        data = dict(data or {})
        known = {f.name for f in fields(cls)}
        out = {k: v for k, v in data.items() if k in known}
        f = out.get("fetch")
        if isinstance(f, dict):
            fknown = {x.name for x in fields(fetch_mod.FetchRequest)}
            out["fetch"] = fetch_mod.FetchRequest(**{k: v for k, v in f.items() if k in fknown})
        return cls(**out)


@dataclass
class PoolClip:
    """A clip that can go into a compilation: one in the library, or one the plan expects to have after fetching."""
    youtube_id: str
    label: str
    path: Path
    clip_id: int = None              # None while it is only expected
    duration: float = None
    published: str = None            # ISO date or None
    projected: bool = False
    status: str = "ready"


@dataclass
class MakePlan:
    plan: Plan
    request: MakeRequest             # with every default filled in: what gets recorded
    fetch: object = None             # the FetchPlan, or None
    style: Style = None
    style_name: str = None
    sizing: grp.Sizing = None
    groups: list = field(default_factory=list)      # projected compilations: lists of PoolClip
    held: list = field(default_factory=list)        # projected leftover
    names: list = field(default_factory=list)

    @property
    def nothing_to_do(self):
        work = self.fetch is not None and (self.fetch.target or self.fetch.adopt)
        return not self.groups and not work


# ---------------------------------------------------------------- the request
def effective_fetch(request):
    """The channel request as it will be planned: `make -n N` with no other count means "enough for N"."""
    f = request.fetch
    if f is None:
        return None
    if f.videos is None and not (f.clips or f.compilations or f.range or f.take) and request.compilations:
        return replace(f, compilations=request.compilations)
    return f


def resolve_request(ws, request):
    """Check the request itself and fill in every default from the workspace -> a new MakeRequest.
    Raises OpError for a request that makes no sense."""
    r = replace(request)
    cfg = ws.config["make"]
    r.order = r.order or cfg["order"]
    r.if_short = r.if_short or cfg["if_short"]
    if r.order not in grp.ORDERS:
        raise OpError(f"order must be one of {', '.join(grp.ORDERS)} (got {r.order!r})")
    if r.play not in grp.PLAYS:
        raise OpError(f"play must be one of {', '.join(grp.PLAYS)} (got {r.play!r})")
    if r.if_short not in grp.IF_SHORT:
        raise OpError(f"if-short must be one of {', '.join(grp.IF_SHORT)} (got {r.if_short!r})")
    for name in ("compilations", "per", "per_minutes"):
        if getattr(r, name) < 0:
            raise OpError(f"{name.replace('_', '-')} cannot be negative")
    if r.per and r.per_minutes:
        raise OpError("Use either --per (clips per compilation) or --per-minutes, not both.")
    if r.per == 1:
        raise OpError("A compilation needs at least 2 clips.")
    if r.compilations and r.everything:
        raise OpError("Use either -n N or --all, not both.")
    if r.fetch is None and not (r.compilations or r.everything):
        raise OpError("How many compilations? Give -n N, or --all for as many as the clips in your library allow "
                      "(or name a channel to fetch from).")
    if r.if_short == "fetch" and (r.fetch is None or r.fetch.videos is not None):
        raise OpError("--if-short fetch needs a channel to fetch more clips from.")
    size = grp.sizing_from(cfg, r.per, r.per_minutes)
    r.per, r.per_minutes = (size.clips, 0) if size.mode == "count" else (0, size.seconds / 60)
    if r.delete_used is None:
        r.delete_used = bool(ws.config["delete_used_clips"])
    if r.order == "random" and not r.seed:
        r.seed = random.randrange(1, 2 ** 31)
    return r


def sizing_of(request):
    return grp.Sizing("count", request.per, 0.0) if request.per else grp.Sizing("minutes", 0, request.per_minutes * 60)


def _size_cfg(sizing):
    """The size in the shape fetch's `-n` arithmetic wants."""
    return {"size_mode": sizing.mode, "clips_each": sizing.clips, "minutes_each": sizing.seconds / 60}


def resolve_style(ws, wanted):
    """-> (Style, name or None). None means the built-in look, because no saved style has the default's name."""
    name = wanted or ws.config["default_style"]
    data = store.get_style(ws.conn, name)
    if data is None:
        if wanted:
            names = ", ".join(store.list_styles(ws.conn)) or "none yet"
            raise OpError(f"There is no style called '{wanted}'. Saved styles: {names}.")
        return Style(), None
    try:
        return Style.from_dict(data), name
    except StyleError as e:
        raise OpError(f"Style '{name}' is not valid: {e}")


def request_like(ws, target):
    """The make request that made compilation `target` (a number, a name or 'last'), found by following 'remade
    from' links back to the compilation that `make` made. The random seed is not repeated: a new one is picked."""
    rows = remake_mod.resolve_targets(ws.conn, target)
    if len(rows) != 1:
        raise OpError("--like takes one compilation, for example --like 5 or --like last.")
    row, seen = rows[0], set()
    first = row["name"]
    while row is not None and row["id"] not in seen:
        seen.add(row["id"])
        data = json.loads(row["request"]) if row["request"] else None
        if data and data.get("kind") == "make":
            req = MakeRequest.from_dict(data)
            req.seed = 0
            return req
        row = ws.conn.execute("SELECT * FROM compilations WHERE id = ?", (row["parent_id"],)).fetchone() \
            if row["parent_id"] else None
    imported = ws.conn.execute("SELECT imported FROM compilations WHERE name = ?", (first,)).fetchone()["imported"]
    why = ("was imported from the old tool, which did not record how it was made"
           if imported else "was not made by `ytt make`, so there is no make request to repeat")
    raise OpError(f"{first} {why}; --like cannot repeat it. Run `ytt make` with the options you want instead.")


# ---------------------------------------------------------------- the pool
def library_pool(ws, retry_failed=False):
    """Clips in the library that no compilation has used, whose file is there, in library order (the order they
    were fetched in). With retry_failed the clips that could not be read earlier are candidates again."""
    out = []
    for c in store.list_clips(ws.conn):
        if c["used"]:
            continue
        if c["status"] != "ready" and not (retry_failed and c["status"] == "failed"):
            continue
        path = ws.from_stored(c["path"])
        if not path.is_file():
            continue
        out.append(PoolClip(c["youtube_id"], c["title"] or c["youtube_id"], path, c["id"], c["duration"],
                            c["published"], False, c["status"]))
    return out


def _projected(fp):
    """The clips a fetch plan expects to add, in the order they will be recorded: found on disk first, then downloads."""
    out = []
    for video, path in fp.adopt:
        out.append(PoolClip(video.id, video.title or video.id, Path(path), None, video.duration,
                            video.date().isoformat() if video.date() else None, True))
    for job in fp.jobs[:fp.target]:
        v = fp.meta.get(job.video_id)
        out.append(PoolClip(job.video_id, job.label or job.video_id, Path(job.path) if job.path else Path(job.video_id),
                            None, v.duration if v else None,
                            v.date().isoformat() if v and v.date() else None, True))
    return out


def cut(request, sizing, pool, duration_of):
    """Order the pool, then cut it -> (groups in play order, held, unreadable)."""
    ordered = grp.order_pool(pool, request.order, seed=request.seed, date_of=lambda c: c.published)
    ordered = grp.reverse_pool(ordered, request.play)
    groups, held, bad = grp.build_groups(ordered, sizing, duration_of, max_groups=request.compilations or None,
                                         include_leftover=request.if_short == "short")
    return grp.play_order(groups, request.play), held, bad


def _how_many_more(sizing, held, duration_of):
    """How many more clips would fill one more compilation from `held`."""
    if sizing.mode == "count":
        return max(1, sizing.clips - len(held))
    have = sum(duration_of(c) or 0 for c in held)
    known = [d for d in (duration_of(c) for c in held) if d]
    avg = sum(known) / len(known) if known else ASSUMED_CLIP_SECONDS
    return max(1, int(-(-(sizing.seconds - have) // avg)))


def _names(clips, limit=3):
    shown = ", ".join(c.label for c in clips[:limit])
    return shown + (f" and {len(clips) - limit} more" if len(clips) > limit else "")


# ---------------------------------------------------------------- plan
def plan_make(ws, request, backend=None, progress=None, probe=None):
    """Resolve and validate, then describe what will happen. Nothing is changed (no folder is even created)."""
    say = progress or (lambda text: None)
    request = resolve_request(ws, request)
    plan = Plan(title="Make compilations")
    mp = MakePlan(plan=plan, request=request)
    try:
        stitch.check_tools()
    except EngineError as e:
        plan.errors.append(str(e))
        return mp
    mp.style, mp.style_name = resolve_style(ws, request.style)
    request.style = mp.style_name
    plan.errors += [f"Style {mp.style_name or '(built-in)'}: {p}" for p in render_mod.style_problems(mp.style)]
    mp.sizing = sizing = sizing_of(request)

    fp = None
    fetch_req = effective_fetch(request)
    if fetch_req is not None:
        backend = backend or YtDlpBackend.from_config(ws.config)
        fp = mp.fetch = fetch_mod.plan_fetch(ws, fetch_req, backend, progress, make_cfg=_size_cfg(sizing))
        plan.errors += fp.plan.errors
        plan.warnings += fp.plan.warnings
        plan.notes += fp.plan.notes
    if not plan.ok:
        return mp

    cache = probe or ProbeCache(ws.cache_dir / "probes.json")
    library = library_pool(ws, request.retry_failed)
    if len(library) > 20:
        say("Checking the clips in your library...")
    projected = _projected(fp) if fp else []

    def duration_of(c):
        return (c.duration or ASSUMED_CLIP_SECONDS) if c.projected else cache.duration(c.path)

    groups, held, bad = cut(request, sizing, library + projected, duration_of)
    cache.save()
    mp.groups, mp.held = groups, held
    mp.names = common.next_names(ws, len(groups))

    # ---- what is in the library
    failed_before = [c for c in store.list_clips(ws.conn) if c["status"] == "failed" and not c["used"]]
    if failed_before and not request.retry_failed:
        plan.notes.append(f"{common.plural(len(failed_before), 'clip')} could not be read earlier and "
                          f"{'is' if len(failed_before) == 1 else 'are'} left out (--retry-failed tries again).")
    if bad:
        plan.warnings.append(f"{common.plural(len(bad), 'clip')} in your library cannot be read and will be left out "
                             f"({_names(bad)}).")
    have = f"{len(library) - len(bad)} waiting in the library"
    plan.notes.append(f"Clips: {have}" + (f" + {len(projected)} coming from this fetch" if projected else ""))
    play_note = {"asis": "", "reverse": ", the whole sequence played backwards", "each": ", each compilation played backwards"}
    plan.notes.append(f"Each compilation: {sizing.label()} · order {request.order}{play_note[request.play]} · "
                      f"style {mp.style_name or '(built-in look)'}")

    # ---- the actions
    if fp:
        for a in fp.plan.actions:
            if a.kind == "adopt":
                plan.actions.append(a)
        downloads = [a for a in fp.plan.actions if a.kind == "download"]
        if downloads:
            known = sum(fp.meta[j.video_id].duration or 0 for j in fp.jobs[:fp.target] if j.video_id in fp.meta)
            plan.actions.append(Action(
                "download", f"download {common.plural(len(downloads), 'clip')}"
                            + (f" (about {common.mmss(known)} of video)" if known else ""),
                {"videos": [a.data["video"] for a in downloads], "lines": [a.text for a in downloads]}))
    total_seconds = 0.0
    for name, group in zip(mp.names, groups):
        secs = sum(duration_of(c) or 0 for c in group)
        total_seconds += secs
        soon = " (some clips still to be downloaded)" if any(c.projected for c in group) else ""
        plan.actions.append(Action(
            "render", f"makes {name}: {len(group)} clips · {common.mmss(secs)} of clips{soon} · "
                      f"{ws.compilations_dir / (name + '.mp4')}",
            {"name": name, "clips": len(group), "lines": [c.label for c in group]}))

    # ---- what is left over
    if held:
        if request.if_short == "keep":
            plan.notes.append(f"{common.plural(len(held), 'clip')} left over, not enough for a compilation "
                              f"({sizing.label()}): kept for next time.")
        elif request.if_short == "short":
            plan.notes.append(f"{common.plural(len(held), 'clip')} left over: too few for a compilation and only "
                              f"one clip is not enough for a shorter one, so it is kept for next time.")
        else:
            need = _how_many_more(sizing, held, duration_of)
            if fp and fp.spare >= need:
                plan.notes.append(f"{common.plural(len(held), 'clip')} left over: {common.plural(need, 'more clip')} "
                                  f"will be fetched to fill one more compilation.")
            else:
                plan.notes.append(f"{common.plural(len(held), 'clip')} left over; filling one more compilation needs "
                                  f"{common.plural(need, 'more clip')} but only {fp.spare if fp else 0} more new "
                                  f"{'video is' if (fp.spare if fp else 0) == 1 else 'videos are'} available, so they "
                                  f"are kept for next time.")
    if request.if_short == "short" and groups and sizing.mode == "count" and len(groups[-1]) < sizing.clips:
        plan.notes.append(f"The last compilation is shorter ({len(groups[-1])} clips) because it is made from the leftover.")

    if not groups and not mp.nothing_to_do:
        plan.notes.append(f"No compilation can be made from these clips yet ({sizing.label()} each); "
                          f"they are fetched and kept.")
    if not groups and mp.nothing_to_do:
        plan.notes.append(f"Nothing to make: not enough clips are waiting for a compilation ({sizing.label()} each).")
    if projected and any(c.projected for g in groups for c in g):
        plan.notes.append("The compilations are cut again from the library once the downloads have finished, so the "
                          "clips can differ from this plan if a download fails and the next video takes its place.")

    # ---- validate what can be known without doing the work
    if groups:
        out_dir = ws.compilations_dir
        folder = common.nearest_existing(out_dir)
        if not os.access(folder, os.W_OK):
            plan.errors.append(f"{out_dir} is not writable.")
        else:
            free = shutil.disk_usage(folder).free
            needed = total_seconds * 10_000_000 / 8                 # a deliberately rough 10 Mbit/s
            if free < needed:
                plan.warnings.append(f"Free disk space ({free / 1e9:.1f} GB) may be less than the renders need "
                                     f"(roughly {needed / 1e9:.1f} GB).")
    if request.delete_used and groups:
        plan.warnings.append("Clips that ytt downloaded will be deleted from the disk once they are inside a compilation "
                             "(`ytt remake` can fetch them again). Clips you already had are never deleted.")
    plan.title = (f"Make {common.plural(len(groups), 'compilation')}" if groups else "Make compilations") + \
        (f" (after fetching {common.plural(fp.target + len(fp.adopt), 'clip')})" if fp and (fp.target or fp.adopt) else "")
    return mp


# ---------------------------------------------------------------- run
def run_make(ws, mp, on_progress=None, backend=None, on_fetch=None, probe=None):
    """Carry out a plan that has no errors: fetch, cut the library into compilations again (what is really there
    now), render each one and record it as soon as its file is complete. One failed render does not stop the rest,
    and Ctrl-C keeps everything already finished. on_progress(name, done, total) for renders; on_fetch(outcome,
    done, target) for downloads."""
    if not mp.plan.ok:
        raise OpError("The plan has errors; nothing was done.")
    conn = ws.conn
    request, sizing = mp.request, mp.sizing
    run_id = store.create_run(conn, "make", request.to_dict(), mp.plan.to_dict())
    conn.commit()                                           # a crash from here on still leaves the run on record
    cache = probe or ProbeCache(ws.cache_dir / "probes.json")
    items, made = [], []
    counts = {"downloaded": 0, "adopted": 0, "failed": 0, "unreadable": 0, "left_over": 0, "deleted": 0}
    cancelled, ok = False, True
    groups, held = [], []
    index = 0
    phase = "fetch"

    def fetch_part(fp):
        nonlocal cancelled, ok
        be = backend or YtDlpBackend.from_config(ws.config)
        its, cts, cancel = fetch_mod.execute_fetch(ws, fp, be, on_fetch)
        items.extend(its)
        for k in ("downloaded", "adopted", "failed"):
            counts[k] += cts[k]
        if cancel:
            cancelled = True
        elif fetch_mod.fetch_status(fp, cts, cancel) != COMPLETED:
            ok = False

    def cut_now():
        pool = library_pool(ws, request.retry_failed)
        g, h, bad = cut(request, sizing, pool, lambda c: cache.duration(c.path))
        for c in bad:
            store.upsert_clip(conn, c.youtube_id, ws.to_stored(c.path), "failed")
            items.append(Item(c.label, "skipped", "the clip cannot be read; left out (--retry-failed tries it again)"))
        counts["unreadable"] += len(bad)
        if bad:
            conn.commit()
        return g, h

    try:
        fp = mp.fetch
        if fp is not None and (fp.target or fp.adopt):
            fetch_part(fp)
        phase = "cut"
        if not cancelled:
            groups, held = cut_now()
            fetch_req = effective_fetch(request)
            if held and request.if_short == "fetch" and fetch_req is not None:
                need = _how_many_more(sizing, held, lambda c: cache.duration(c.path))
                try:
                    more = fetch_mod.plan_fetch(ws, replace(fetch_req, clips=need, compilations=0, range="", take="",
                                                            videos=None),
                                                backend or YtDlpBackend.from_config(ws.config),
                                                make_cfg=_size_cfg(sizing))
                except OpError as e:
                    more = None
                    items.append(Item("filling the last compilation", "skipped", str(e)))
                if more is not None and more.plan.ok and more.target:
                    fetch_part(more)
                    if not cancelled:
                        groups, held = cut_now()
                elif more is not None:
                    items.append(Item("filling the last compilation", "skipped",
                                      "no more new clips were available; the leftover is kept for next time"))
        phase = "render"
        names = common.next_names(ws, len(groups)) if groups and not cancelled else []
        for index, group in enumerate(groups if not cancelled else []):
            name = names[index]
            output = ws.compilations_dir / f"{name}.mp4"
            try:
                spec = render_mod.spec_for(mp.style, [c.path for c in group], output, probe=cache.probe)
                engine_plan = stitch.plan(spec)
                stitch.render(spec, on_progress=(lambda d, t, n=name: on_progress(n, d, t)) if on_progress else None,
                              plan_=engine_plan)
            except EngineError as e:
                ok = False
                items.append(Item(name, "failed", str(e)))
                continue
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            store.add_compilation(conn, name, clips=[(c.clip_id, None) for c in group],
                                  output_path=ws.to_stored(output), made=stamp, style_snapshot=mp.style.to_dict(),
                                  request={"kind": "make", **request.to_dict()})
            for c in group:
                if c.status == "failed":                    # it was tried again and it worked
                    store.upsert_clip(conn, c.youtube_id, ws.to_stored(c.path), "ready")
            conn.commit()                                   # recorded only now that the file is complete
            made.append(name)
            items.append(Item(name, "completed", f"{len(group)} clips"))
    except KeyboardInterrupt:
        cancelled = True
        conn.rollback()
        if phase == "render" and groups:
            items.append(Item(f"compilation {index + 1} of {len(groups)}", "cancelled", "stopped by the user"))
            items += [Item(f"compilation {i + 1} of {len(groups)}", "skipped", "not started")
                      for i in range(index + 1, len(groups))]
        else:
            items.append(Item("making compilations", "cancelled", "stopped by the user"))
    finally:
        cache.save()

    if request.delete_used and made and not cancelled:
        counts["deleted"] = _delete_used(ws, made)
    counts["left_over"] = len(held)

    if cancelled:
        status = CANCELLED
    elif ok:
        status = COMPLETED
    else:
        status = PARTIAL if (made or counts["downloaded"] or counts["adopted"]) else FAILED
    store.finish_run(conn, run_id, status, [asdict(i) for i in items])
    conn.commit()
    return RunResult(run_id, status, items, made, counts)


def _delete_used(ws, made_names):
    """Delete the files of clips that ytt downloaded and that now sit inside a compilation made by this run. Clips
    that were found on the disk or imported are the user's own files and are never deleted. The clip stays in the
    library as 'missing', so `remake` can fetch it again."""
    conn, deleted = ws.conn, 0
    for name in made_names:
        comp = store.get_compilation(conn, name)
        for r in store.compilation_clips(conn, comp["id"]):
            if r["clip_id"] is None:
                continue
            row = store.get_clip(conn, r["youtube_id"])
            if row["origin"] != "fetched" or row["status"] != "ready":
                continue
            try:
                ws.from_stored(row["path"]).unlink()
            except FileNotFoundError:
                pass
            store.upsert_clip(conn, r["youtube_id"], row["path"], "missing")
            deleted += 1
    conn.commit()
    return deleted
