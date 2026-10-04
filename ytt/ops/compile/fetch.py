"""fetch: get clips into the library (DESIGN.md sections 5, 8, 11, 13).

Stages: resolve -> validate -> plan (plan_fetch), then execute and record (run_fetch). The command line shows the
plan and asks; this module never prints or prompts. Reading a channel and downloading go through a Backend
(ytt.sources.backend), so tests run against a fake YouTube.

What "how many" means (DESIGN.md section 8):
  clips N        N new clips: videos not in the library, after dropping look-alike re-uploads. If a download
                 fails, the next video in line takes its place.
  compilations N enough new clips for N full compilations; clips already waiting in the library count.
  range A-B      exactly those positions of the filtered, sorted list; anything already in the library is skipped.
  take EXPR      the advanced old grammar (last:20, every:5, random:30, new:60, comps:5, ...).
  videos [ids]   an explicit list of YouTube ids instead of a channel query.
"""
import math
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from ytt.ops.compile.style import Style, StyleError
from ytt.ops.errors import OpError
from ytt.ops.plan import CANCELLED, COMPLETED, FAILED, PARTIAL, Action, Item, Plan, RunResult
from ytt.sources import channel as chan
from ytt.sources import files
from ytt.sources import selection as sel
from ytt.sources.downloader import Job, download_all
from ytt.sources.errors import SourceError
from ytt.sources.models import VideoInfo
from ytt.workspace import store

BYTES_PER_SECOND = 1_000_000          # a deliberately generous 8 Mbit/s, only for the free-space warning


@dataclass
class FetchRequest:
    channel: str = ""                 # @handle or link; empty when `videos` is given
    type: str = "videos"              # videos | shorts
    date_from: str = ""
    date_to: str = ""
    min_views: int = 0
    min_length: float = 0             # minutes
    max_length: float = 0
    sort: str = "popular"
    clips: int = 0                    # N new clips
    compilations: int = 0             # enough new clips for N compilations
    range: str = ""                   # "25-70"
    take: str = ""                    # advanced: "last:20", "every:5", ...
    videos: list = None               # explicit YouTube ids instead of a channel query
    keep_duplicates: bool = False
    refetch: bool = False             # download again even what the library already has
    max_height: int = 0               # 0 = the default style's
    workers: int = 0                  # 0 = the workspace setting


@dataclass
class FetchPlan:
    plan: Plan
    request: FetchRequest
    jobs: list = field(default_factory=list)       # Job, in queue order; the first `target` are what the plan promises
    target: int = 0                                # how many downloads must succeed
    adopt: list = field(default_factory=list)      # [(VideoInfo, Path)]: already on the disk, to be added to the library
    meta: dict = field(default_factory=dict)       # youtube id -> VideoInfo (what the channel list said)
    source: str = ""
    max_height: int = 1080
    workers: int = 3


# ---------------------------------------------------------------- small helpers
def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def _k(n):
    if n is None:
        return "? views"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M views"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K views"
    return f"{n} views"


def _mmss(seconds):
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _nearest_existing(path):
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def canonical_handle(text):
    """How a channel is written in the library: @Name when that is what the link says, else as given."""
    t = text.strip().rstrip("/")
    for tab in (*chan.TABS, "streams", "featured"):
        if t.endswith("/" + tab):
            t = t[: -len(tab) - 1]
    if "/@" in t:
        return "@" + t.split("/@", 1)[1].split("/")[0]
    return t


def waiting_clips(ws):
    """Ready clips whose file is there and that no compilation has used yet."""
    return [c for c in store.list_clips(ws.conn)
            if c["status"] == "ready" and not c["used"] and ws.from_stored(c["path"]).is_file()]


def clips_per_compilation(make_cfg, videos):
    """How many clips make one compilation -> (number, exact?). By count it is exact; by minutes it is estimated
    from the average clip length in the list."""
    if make_cfg["size_mode"] == "count":
        return int(make_cfg["clips_each"]), True
    lens = [v.duration for v in videos if v.duration]
    avg = sum(lens) / len(lens) if lens else 30
    return max(2, math.ceil(make_cfg["minutes_each"] * 60 / avg)), False


def default_max_height(ws):
    """The download height cap: whatever the workspace's default style says."""
    data = store.get_style(ws.conn, ws.config["default_style"])
    return Style.from_dict(data).max_height if data is not None else Style().max_height


def restore_path(ws, clip_row):
    """Where a clip that is being fetched again goes: its recorded name if that is an .mp4 inside the clips folder
    (so earlier compilations can be remade exactly), otherwise the default <id>.mp4 there (None)."""
    p = ws.from_stored(clip_row["path"])
    return p if not _outside(ws.clips_dir, p) and p.suffix.lower() == ".mp4" else None


def _check_request(r):
    """Problems with the request itself, before anything is read. Raises OpError."""
    if r.type not in chan.TABS:
        raise OpError(f"type must be one of {', '.join(chan.TABS)} (got {r.type!r})")
    if r.sort not in sel.SORTS:
        raise OpError(f"sort must be one of {', '.join(sel.SORTS)} (got {r.sort!r})")
    for name in ("clips", "compilations", "min_views", "min_length", "max_length", "max_height", "workers"):
        if getattr(r, name) < 0:
            raise OpError(f"{name.replace('_', '-')} cannot be negative")
    chosen = [n for n, v in (("--clips", r.clips), ("-n", r.compilations), ("--range", r.range), ("--take", r.take)) if v]
    query_things = [n for n, v in (("--from", r.date_from), ("--to", r.date_to), ("--min-views", r.min_views),
                                   ("--min-length", r.min_length), ("--max-length", r.max_length)) if v]
    if r.videos is not None:
        if r.channel:
            raise OpError("A list of videos already says which videos, so leave the channel out.")
        if chosen or query_things:
            raise OpError(f"A list of videos already says which videos, so {', '.join(chosen + query_things)} "
                          f"does not apply.")
        if not r.videos:
            raise OpError("The list of videos is empty.")
        return
    if not r.channel.strip():
        raise OpError("Say which channel to fetch from (for example @Name), or give a list with --videos FILE.")
    if not chosen:
        raise OpError("How many? Give --clips N (N new clips), -n N (enough for N compilations), "
                      "--range A-B (exact positions) or --take EXPR (advanced).")
    if len(chosen) > 1:
        raise OpError(f"Use only one of --clips, -n, --range and --take (got {', '.join(chosen)}).")
    try:
        d_from = sel.parse_date(r.date_from) if r.date_from else None
        d_to = sel.parse_date(r.date_to) if r.date_to else None
        if r.range:
            sel.parse_range(r.range)
        if r.take:
            sel.parse_take(r.take)
    except ValueError as e:
        raise OpError(str(e))
    if d_from and d_to and d_from > d_to:
        raise OpError("The --from date is after the --to date.")


# ---------------------------------------------------------------- plan
def plan_fetch(ws, request, backend, progress=None):
    """Resolve and validate, then describe what will happen. Nothing is changed. `progress(text)` is told what is
    being read, because a big channel takes a while."""
    say = progress or (lambda text: None)
    _check_request(request)
    conn = ws.conn
    listed = request.videos is None
    handle = canonical_handle(request.channel) if listed else ""
    plan = Plan(title=f"Fetch from {handle}" if listed else f"Fetch {_plural(len(request.videos), 'video')} from your list")
    fp = FetchPlan(plan=plan, request=request, source=handle)
    try:
        fp.max_height = request.max_height or default_max_height(ws)
    except StyleError as e:
        fp.max_height = Style().max_height
        plan.warnings.append(f"The default style is not valid ({e}); downloading at up to {fp.max_height}p.")
    fp.workers = max(1, request.workers or int(ws.config["workers"]))

    clip_rows = store.list_clips(conn)
    have_ids = set() if request.refetch else {c["youtube_id"] for c in clip_rows}
    have_keys = set() if request.refetch else {k for k in (sel.dup_key(c["title"], c["duration"]) for c in clip_rows) if k}
    folders = [ws.clips_dir] + [Path(f) for f in ws.config["watch_folders"]]
    range_note = ""
    candidates_all = []                                    # for the "enough clips" arithmetic

    if listed:
        # ---- read the channel
        base_url = chan.base_channel_url(request.channel)
        say(f"Reading the {request.type} list of {handle} ... (a big channel can take a minute)")
        try:
            rows = [v for v in backend.list_tab(base_url, request.type) if v.id]
        except SourceError as e:
            plan.errors.append(f"Could not read the channel: {e}")
            return fp
        if not rows:
            plan.errors.append("No videos found - check the channel link/@handle (cookies for age-restricted or "
                               "members-only channels: ytt workspace set cookies_from_browser firefox).")
            return fp
        fp.meta = {v.id: v for v in rows}

        # ---- 1. dates, only looking up exact ones where it matters
        rows_in = rows
        d_from = sel.parse_date(request.date_from) if request.date_from else None
        d_to = sel.parse_date(request.date_to) if request.date_to else None
        if d_from or d_to:
            resolver = sel.DateResolver(backend.probe_date,
                                        on_probe=lambda n: say(f"Checking upload dates ... ({n} looked up)"))
            try:
                rows_in = sel.within_dates(rows, d_from, d_to, resolver)
            except SourceError as e:
                plan.errors.append(str(e))
                return fp
            plan.notes.append(f"Dates: {len(rows_in)} of the {len(rows)} {request.type} were uploaded "
                              f"{d_from or 'at the start'} to {d_to or 'today'}.")
            if len(rows_in) < len(rows):
                range_note = (f" Your date range ({d_from or 'start'} to {d_to or 'today'}) narrowed the "
                              f"{len(rows)} {request.type} down to {len(rows_in)}; clear or widen it to reach more.")
        # ---- length / views limits
        before = len(rows_in)
        rows_in = sel.within_limits(rows_in, request.min_views, request.min_length, request.max_length)
        if len(rows_in) < before:
            plan.notes.append(f"Limits: {before - len(rows_in)} left out by your length/views limits.")
        if not rows_in:
            plan.errors.append("No videos match those limits." + range_note)
            return fp
        ranked = sel.rank(rows_in, request.sort)

        # ---- what is on the disk already but not in the library? It counts as yours.
        if not request.refetch:
            found = files.find_on_disk(folders, {v.id for v in rows})
            for vid, path in sorted(found.items()):
                if vid not in have_ids:
                    fp.adopt.append((fp.meta[vid], path))
            have_ids |= {v.id for v, _ in fp.adopt}
        # ---- look-alike re-uploads
        dups = set() if request.keep_duplicates else sel.find_likely_duplicates(ranked, have_ids, have_keys)
        skip = have_ids | dups
        # ---- which ones
        mode, count, take = "new", 0, None
        if request.clips:
            count = request.clips
            what = f"{_plural(count, 'new clip')}"
        elif request.compilations:
            per, exact = clips_per_compilation(ws.config["make"], ranked)
            waiting = len(waiting_clips(ws))
            count = max(0, request.compilations * per - waiting)
            what = (f"enough new clips for {_plural(request.compilations, 'compilation')} of "
                    f"{'' if exact else 'about '}{_plural(per, 'clip')} each ({waiting} already waiting)")
        else:
            take = sel.parse_range(request.range) if request.range else sel.parse_take(request.take)
            if take[0] == "comps":
                per, exact = clips_per_compilation(ws.config["make"], ranked)
                waiting = len(waiting_clips(ws))
                count = max(0, take[1] * per - waiting)
                what = (f"enough new clips for {_plural(take[1], 'compilation')} of "
                        f"{'' if exact else 'about '}{_plural(per, 'clip')} each ({waiting} already waiting)")
            elif take[0] == "new":
                count = take[1]
                what = f"{_plural(count, 'new clip')}"
            else:
                mode = "exact"
                what = sel.describe_take(take)
        plan.notes.insert(0, f"Source: {handle} · {request.type} · sorted by {sel.SORT_WORDS[request.sort]} · "
                             f"{len(ranked)} to choose from")
        plan.notes.insert(1, f"Selection: {what}")

        if mode == "new":
            candidates = [(i + 1, v) for i, v in enumerate(ranked) if v.id not in skip]
            picked = candidates[:count]
            queue = candidates if picked else []
            fp.target = len(picked)
            if count == 0:
                plan.notes.append("Already enough clips are waiting in your library, so nothing needs downloading.")
            elif not candidates:
                plan.notes.append("Nothing new: every video in that list is already in your library"
                                  + (" or looks like a re-upload" if dups else "") + "." + range_note)
            elif len(candidates) < count:
                full = ""
                if request.compilations or (take and take[0] == "comps"):
                    full = (f", which makes {_plural((len(waiting_clips(ws)) + len(candidates)) // per, 'full compilation')} "
                            f"at most")
                plan.notes.append(f"Only {_plural(len(candidates), 'new video')} {'is' if len(candidates) == 1 else 'are'} "
                                  f"available in that list{full}." + range_note)
            already_n = sum(1 for v in ranked if v.id in have_ids)
            plan.notes.append(f"Library: {already_n} of the {len(ranked)} already yours (skipped)"
                              + (f" · {_plural(len(dups), 'look-alike re-upload')} skipped" if dups else ""))
        else:
            picked_all = sel.apply_take(ranked, take)
            if not picked_all:
                plan.errors.append(f"Your selection ({what}) doesn't match anything - the list only has "
                                   f"{_plural(len(ranked), 'video')}." + range_note)
                return fp
            picked = [(p, v) for p, v in picked_all if v.id not in skip]
            already_n = sum(1 for _, v in picked_all if v.id in have_ids)
            look_n = len(picked_all) - len(picked) - already_n
            queue = picked
            fp.target = len(picked)
            plan.notes.append(f"Library: of the {_plural(len(picked_all), 'video')} selected, {already_n} are already yours "
                              f"(skipped)" + (f" and {look_n} look like re-uploads (skipped)" if look_n else ""))
            if not picked:
                plan.notes.append("Nothing new in that selection. For the next batch pick other positions, or use --clips N."
                                  + range_note)
        if dups:
            plan.notes.append("Look-alikes are a guess (same title and length); --keep-duplicates downloads them too.")
        fp.jobs = [Job(v.id, v.title, position=p, path=_refetch_path(ws, v.id, request)) for p, v in queue]
    else:
        # ---- an explicit list of videos
        ids = list(request.videos)
        fp.meta = {i: VideoInfo(id=i, title=_known_title(conn, i)) for i in ids}
        found = {} if request.refetch else files.find_on_disk(folders, {i for i in ids if i not in have_ids})
        fp.adopt = [(fp.meta[i], found[i]) for i in ids if i in found]
        have_ids |= {v.id for v, _ in fp.adopt}
        picked = [(n, fp.meta[i]) for n, i in enumerate(ids, 1) if i not in have_ids]
        fp.jobs = [Job(v.id, v.title or v.id, position=p, path=_refetch_path(ws, v.id, request)) for p, v in picked]
        fp.target = len(fp.jobs)
        plan.notes.append(f"Selection: your list of {_plural(len(ids), 'video')}")
        plan.notes.append(f"Library: {len(ids) - len(picked) - len(fp.adopt)} already yours (skipped)")
        if not picked and not fp.adopt:
            plan.notes.append("Nothing new: every video on your list is already in your library.")

    # ---- the actions
    if fp.adopt:
        elsewhere = sum(1 for _, p in fp.adopt if _outside(ws.clips_dir, p))
        plan.actions.append(Action("adopt", f"add {_plural(len(fp.adopt), 'clip')} already on your disk to the library",
                                   {"videos": [v.id for v, _ in fp.adopt]}))
        if elsewhere:
            plan.warnings.append(f"{_plural(elsewhere, 'video')} you already have in other folders will be added to the "
                                 f"library where they are (they are never moved or deleted by ytt).")
    for job in fp.jobs[:fp.target]:
        v = fp.meta.get(job.video_id)
        where = f"#{job.position}  " if listed else ""
        dur = f" · {_mmss(v.duration)}" if v and v.duration else ""
        when = f" · {v.date()}" if v and v.date() else ""
        views = f" · {_k(v.views)}" if v and v.views is not None else ""
        plan.actions.append(Action("download", f"{where}{job.label or job.video_id}{views}{when}{dur}",
                                   {"video": job.video_id, "position": job.position}))
    if fp.target:
        known = sum(fp.meta[j.video_id].duration or 0 for j in fp.jobs[:fp.target] if j.video_id in fp.meta)
        plan.title += f": {_plural(fp.target, 'clip')} to download" + (f" (about {_mmss(known)} of video)" if known else "")
    else:
        plan.title += ": nothing to download"

    # ---- validate what can be known without doing the work
    if fp.target or fp.adopt:
        folder = _nearest_existing(ws.clips_dir)
        if not os.access(folder, os.W_OK):
            plan.errors.append(f"{ws.clips_dir} is not writable.")
        elif fp.target:
            free = shutil.disk_usage(folder).free
            needed = sum(fp.meta[j.video_id].duration or 0 for j in fp.jobs[:fp.target] if j.video_id in fp.meta) \
                * BYTES_PER_SECOND
            if needed and free < needed:
                plan.warnings.append(f"Free disk space ({free / 1e9:.1f} GB) may be less than the downloads need "
                                     f"(roughly {needed / 1e9:.1f} GB).")
    if request.refetch and fp.target:
        plan.warnings.append("--refetch downloads videos again even if the library already has them; "
                             "an existing file in the clips folder is replaced.")
    return fp


def _outside(folder, path):
    try:
        Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(folder)))
        return False
    except ValueError:
        return True


def _known_title(conn, vid):
    row = store.get_video(conn, vid)
    return (row["title"] if row and row["title"] else "")


def _refetch_path(ws, vid, request):
    if not request.refetch:
        return None
    row = store.get_clip(ws.conn, vid)
    return restore_path(ws, row) if row else None


# ---------------------------------------------------------------- execute
def _record(ws, source_id, meta, outcome, origin="fetched"):
    """Write one finished download to the library: the video (what we know about it) and its clip."""
    info = outcome.info or {}
    published = info.get("published") or (meta.date().isoformat() if meta and meta.date() else None)
    store.upsert_video(ws.conn, outcome.job.video_id,
                       title=info.get("title") or (meta.title if meta and meta.title else None),
                       views=info.get("views") if info.get("views") is not None else (meta.views if meta else None),
                       duration=info.get("duration") if info.get("duration") is not None else (meta.duration if meta else None),
                       published=published, source_id=source_id)
    store.upsert_clip(ws.conn, outcome.job.video_id, ws.to_stored(outcome.path), "ready", origin)
    ws.conn.commit()


def run_fetch(ws, fp, backend, on_event=None):
    """Carry out a plan that has no errors. Each clip is recorded as soon as its file is complete, so a crash or
    Ctrl-C keeps everything that finished. on_event(outcome, done, target) is told about every download."""
    if not fp.plan.ok:
        raise OpError("The plan has errors; nothing was done.")
    conn = ws.conn
    run_id = store.create_run(conn, "fetch", asdict(fp.request), fp.plan.to_dict())
    conn.commit()                                           # a crash from here on still leaves the run on record
    items, counts = [], {"downloaded": 0, "adopted": 0, "failed": 0}
    cancelled = False
    source_id = None
    try:
        if fp.source and (fp.jobs or fp.adopt):
            source_id = store.add_source(conn, fp.source)
        for video, path in fp.adopt:
            store.upsert_video(conn, video.id, title=video.title or None, views=video.views, duration=video.duration,
                               published=video.date().isoformat() if video.date() else None, source_id=source_id)
            store.upsert_clip(conn, video.id, ws.to_stored(path), "ready", "found")
            counts["adopted"] += 1
        if fp.adopt:
            conn.commit()
            items.append(Item(f"{_plural(len(fp.adopt), 'clip')} already on your disk", "completed", "added to the library"))

        done = 0

        def got(outcome):
            nonlocal done
            if outcome.status == "completed":
                _record(ws, source_id, fp.meta.get(outcome.job.video_id), outcome)
                counts["downloaded"] += 1
                done += 1
                items.append(Item(outcome.job.video_id, "completed", outcome.job.label))
            else:
                counts["failed"] += 1
                items.append(Item(outcome.job.video_id, "failed", f"{outcome.job.label}: {outcome.detail}"))
            if on_event:
                on_event(outcome, done, fp.target)

        if fp.target:
            ws.clips_dir.mkdir(parents=True, exist_ok=True)
            download_all(backend, fp.jobs, target=fp.target, folder=ws.clips_dir, max_height=fp.max_height,
                         workers=fp.workers, on_result=got)
    except KeyboardInterrupt:
        cancelled = True
        conn.rollback()
        left = fp.target - counts["downloaded"]
        items.append(Item("the rest", "cancelled", f"stopped by the user; {left} of {fp.target} downloads not finished"))
    if cancelled:
        status = CANCELLED
    elif counts["downloaded"] >= fp.target:
        status = COMPLETED
    else:
        status = PARTIAL if counts["downloaded"] else FAILED
    store.finish_run(conn, run_id, status, [asdict(i) for i in items])
    conn.commit()
    return RunResult(run_id, status, items, [], counts)


def fetch_again(ws, backend, wanted, max_height, workers, on_event=None):
    """Download clips whose files are gone, under their recorded names where possible (used by remake).
    wanted: list of (youtube_id, label, clip row). Returns {youtube_id: error text or None}. Recorded as ready when
    the file is complete. Raises KeyboardInterrupt after recording what finished."""
    jobs = [Job(vid, label, path=restore_path(ws, row)) for vid, label, row in wanted]
    result = {vid: "not started" for vid, _, _ in wanted}

    def got(outcome):
        vid = outcome.job.video_id
        if outcome.status == "completed":
            store.upsert_clip(ws.conn, vid, ws.to_stored(outcome.path), "ready", "fetched")
            ws.conn.commit()
            result[vid] = None
        else:
            result[vid] = outcome.detail
        if on_event:
            on_event(outcome)

    ws.clips_dir.mkdir(parents=True, exist_ok=True)
    download_all(backend, jobs, target=len(jobs), folder=ws.clips_dir, max_height=max_height, workers=workers,
                 on_result=got)
    return result
