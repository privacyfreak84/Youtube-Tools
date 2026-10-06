"""remake: render recorded compilations again, with changes (DESIGN.md section 10).

It works on the concrete recorded compilation (the same clips in the same order), never on the original
selection query. Stages: resolve -> validate -> plan (plan_remake), then execute (run_remake). The command line
shows the plan and asks; this module never prints or prompts.
"""
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from ytt.engine import stitch
from ytt.engine.stitch import EngineError
from ytt.ops.compile import common
from ytt.ops.compile import fetch as fetch_mod
from ytt.ops.compile import render as render_mod
from ytt.ops.compile.style import Style, StyleError
from ytt.ops.errors import OpError
from ytt.ops.plan import Action, Item, Plan, RunResult
from ytt.sources.ytdlp import YtDlpBackend
from ytt.workspace import store

ORDERS = ("recorded", "reversed")


@dataclass
class RemakeRequest:
    targets: str = "last"            # last | all | 3 | compilation_003 | "1,3"
    style: str = None                # name of a saved style; None = what it was made with, else the default
    order: str = "recorded"          # recorded | reversed
    replace: bool = False            # True: overwrite the old output instead of making a new compilation


@dataclass
class ClipRef:
    position: int
    clip_id: int
    legacy_name: str
    youtube_id: str
    path: Path
    label: str


@dataclass
class Job:
    compilation: object              # the database row being remade
    clips: list                      # ClipRef, in the order they will play
    style: Style
    style_note: str
    name: str                        # name of the compilation this job makes or replaces
    output: Path
    spec: object = None              # the engine's RenderSpec
    engine_plan: object = None
    missing: list = field(default_factory=list)      # ClipRef whose files are gone and will be fetched again first


@dataclass
class RemakePlan:
    plan: Plan
    jobs: list = field(default_factory=list)
    request: RemakeRequest = None


def _num_key(name):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def resolve_targets(conn, spec):
    """'last' | 'all' | '3' | 'compilation_003' | '1,3' -> compilation rows, oldest first."""
    rows = store.list_compilations(conn)
    if not rows:
        raise OpError("Nothing has been made yet, so there is nothing to remake. (`ytt library compilations` lists what exists)")
    by_name = {r["name"]: r for r in rows}
    ordered = sorted(rows, key=lambda r: _num_key(r["name"]))
    chosen = []
    for part in [p.strip() for p in str(spec).split(",") if p.strip()]:
        if part.lower() == "all":
            found = ordered
        elif part.lower() == "last":
            found = [ordered[-1]]
        elif part in by_name:
            found = [by_name[part]]
        else:
            found = [r for r in ordered if re.fullmatch(rf".*_0*{re.escape(part)}", r["name"])][:1]
        if not found:
            raise OpError(f"No compilation matches '{part}'. `ytt library compilations` lists them.")
        chosen += [r for r in found if r["name"] not in [c["name"] for c in chosen]]
    return sorted(chosen, key=lambda r: _num_key(r["name"]))


def choose_style(ws, row, wanted):
    """-> (Style, a short phrase saying where it came from)."""
    conn = ws.conn
    try:
        if wanted:
            data = store.get_style(conn, wanted)
            if data is None:
                names = ", ".join(store.list_styles(conn)) or "none yet"
                raise OpError(f"There is no style called '{wanted}'. Saved styles: {names}.")
            return Style.from_dict(data), f"style '{wanted}'"
        if row["style_snapshot"]:
            return Style.from_dict(json.loads(row["style_snapshot"])), "the style it was made with"
        default = ws.config["default_style"]
        data = store.get_style(conn, default)
        if data is not None:
            return Style.from_dict(data), f"style '{default}' (how it was made was never recorded)"
        return Style(), "the built-in default look (how it was made was never recorded)"
    except StyleError as e:
        raise OpError(f"That style is not valid: {e}")


def _clip_refs(ws, row, reverse):
    refs = []
    for r in store.compilation_clips(ws.conn, row["id"]):
        if r["clip_id"] is not None:
            path = ws.from_stored(r["path"])
            label = r["youtube_id"]
        else:
            path = ws.clips_dir / r["legacy_name"]
            label = r["legacy_name"]
        refs.append(ClipRef(r["position"], r["clip_id"], r["legacy_name"], r["youtube_id"], path, label))
    return list(reversed(refs)) if reverse else refs


def _nearest_existing(path):
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _mmss(seconds):
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


def prepare_job(job):
    """Probe the clips and plan the render. Raises EngineError if a clip cannot be read."""
    job.spec = render_mod.spec_for(job.style, [c.path for c in job.clips], job.output)
    job.engine_plan = stitch.plan(job.spec)


def _plural_clips(n):
    return f"{n} clip" + ("" if n == 1 else "s")


def _needed_clips(jobs):
    """{youtube id: ClipRef} of every clip that has to be fetched again, once even if several compilations use it."""
    needed = {}
    for job in jobs:
        for c in job.missing:
            needed.setdefault(c.youtube_id, c)
    return needed


def plan_remake(ws, request):
    """Resolve and validate, then describe what will happen. Nothing is changed (no folder is even created).
    A compilation that cannot be remade is skipped with a warning; if none can be, that becomes the error."""
    if request.order not in ORDERS:
        raise OpError(f"order must be one of {', '.join(ORDERS)} (got {request.order!r})")
    rows = resolve_targets(ws.conn, request.targets)
    plan = Plan(title="Remake " + ", ".join(r["name"] for r in rows))
    rp = RemakePlan(plan=plan, request=request)

    try:
        stitch.check_tools()
    except EngineError as e:
        plan.errors.append(str(e))
        return rp

    new_names = [] if request.replace else common.next_names(ws, len(rows))
    out_dir = ws.compilations_dir
    skipped = []
    total_seconds = 0.0
    for i, row in enumerate(rows):
        label = row["name"]
        try:
            style, style_note = choose_style(ws, row, request.style)
        except OpError as e:
            skipped.append(f"{label}: {e}")
            continue
        clips = _clip_refs(ws, row, request.order == "reversed")
        gone = [c for c in clips if not c.path.is_file()]
        never_tied = [c for c in gone if c.clip_id is None]
        if never_tied:
            names = ", ".join(c.label for c in never_tied[:4]) + (f" and {len(never_tied) - 4} more" if len(never_tied) > 4 else "")
            skipped.append(f"{label}: {len(never_tied)} of its {len(clips)} clips are no longer on disk and were never tied to "
                           f"a YouTube video ({names}), so they cannot be fetched again; put them back to remake it.")
            continue
        if request.replace:
            if not row["output_path"]:
                skipped.append(f"{label}: it has no recorded output file to replace; remake it without --replace.")
                continue
            name, output = row["name"], ws.from_stored(row["output_path"])
        else:
            name, output = new_names[i], out_dir / f"{new_names[i]}.mp4"
        problems = render_mod.style_problems(style)
        if problems:
            skipped.append(f"{label}: " + "; ".join(problems))
            continue
        job = Job(row, clips, style, style_note, name, output, missing=gone)
        if gone:
            # the files are not here to be probed yet; the render is prepared once they have been fetched again
            length = f"fetches {_plural_clips(len(gone))} again first"
        else:
            try:
                prepare_job(job)
            except EngineError as e:
                skipped.append(f"{label}: {e}")
                continue
            plan.notes += [f"{label}: {n}" for n in job.engine_plan.notes]
            total_seconds += job.engine_plan.total_seconds
            length = f"about {_mmss(job.engine_plan.total_seconds)}"
        verb = "replaces" if request.replace else "makes"
        order_note = ", played backwards" if request.order == "reversed" else ""
        plan.actions.append(Action(
            "render", f"{verb} {name}: {len(clips)} clips{order_note} · {style_note} · {length} · {output}",
            {"compilation": row["name"], "name": name, "output": str(output), "clips": len(clips)}))
        rp.jobs.append(job)

    if not rp.jobs:
        plan.errors += skipped or ["Nothing to remake."]
        return rp
    plan.warnings += [f"Skipped {s}" for s in skipped]
    needed = _needed_clips(rp.jobs)
    if needed:
        plan.actions.insert(0, Action("download", f"fetch {_plural_clips(len(needed))} again (no longer on disk)",
                                      {"videos": sorted(needed)}))
        plan.notes.append("Clips that are no longer on disk are downloaded again from YouTube, under the names the library "
                          "recorded. A compilation whose clips cannot all be fetched is skipped.")

    where = Path(rp.jobs[0].output).parent if request.replace else out_dir
    for job in rp.jobs:
        folder = _nearest_existing(Path(job.output).parent)
        if not os.access(folder, os.W_OK):
            plan.errors.append(f"{Path(job.output).parent} is not writable.")
            break
    else:
        free = shutil.disk_usage(_nearest_existing(where)).free
        needed = total_seconds * 10_000_000 / 8                 # a deliberately rough 10 Mbit/s
        if free < needed:
            plan.warnings.append(f"Free disk space ({free / 1e9:.1f} GB) may be less than the renders need "
                                 f"(roughly {needed / 1e9:.1f} GB).")
    if request.replace:
        plan.warnings.append("The old video file of each replaced compilation will be overwritten.")
    return rp


def run_remake(ws, rp, on_progress=None, backend=None, on_fetch=None):
    """Carry out a plan that has no errors. Clips that are no longer on disk are fetched again first (one batch,
    several at a time); a compilation whose clips could not all be fetched is skipped. Each compilation is then
    rendered and recorded on its own, so one failure does not stop the rest, and Ctrl-C keeps everything already
    finished. on_progress(name, done, total) for renders; on_fetch(outcome) for each clip fetched again.
    `backend` is how YouTube is reached (the real one unless a test hands in a fake)."""
    if not rp.plan.ok:
        raise OpError("The plan has errors; nothing was done.")
    conn = ws.conn
    run_id = store.create_run(conn, "remake", asdict(rp.request), rp.plan.to_dict())
    conn.commit()                                           # a crash from here on still leaves the run on record
    items, made = [], []
    cancelled = False
    index = 0
    phase = "fetch"
    fetched, failed_fetch = set(), {}
    try:
        needed = _needed_clips(rp.jobs)
        if needed:
            backend = backend or YtDlpBackend.from_config(ws.config)
            wanted = [(vid, c.label, store.get_clip(conn, vid)) for vid, c in needed.items()]
            max_height = max(j.style.max_height for j in rp.jobs if j.missing)
            outcome = fetch_mod.fetch_again(ws, backend, wanted, max_height, max(1, int(ws.config["workers"])),
                                            on_event=on_fetch)
            fetched = {v for v, err in outcome.items() if err is None}
            failed_fetch = {v: err for v, err in outcome.items() if err}
        phase = "render"
        for index, job in enumerate(rp.jobs):
            bad = [c for c in job.missing if c.youtube_id in failed_fetch]
            if bad:
                items.append(Item(job.name, "failed", f"{_plural_clips(len(bad))} could not be fetched again "
                                                      f"({failed_fetch[bad[0].youtube_id]}); not remade"))
                continue
            try:
                if job.spec is None:                        # its clips were fetched just now: they may have a new place
                    for c in job.clips:
                        if c.youtube_id in fetched:
                            c.path = ws.from_stored(store.get_clip(conn, c.youtube_id)["path"])
                    prepare_job(job)
                stitch.render(job.spec, on_progress=(lambda d, t, n=job.name: on_progress(n, d, t)) if on_progress else None,
                              plan_=job.engine_plan)
            except EngineError as e:
                items.append(Item(job.name, "failed", str(e)))
                continue
            clip_rows = [(c.clip_id, c.legacy_name) for c in job.clips]
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            request = {"kind": "remake", **asdict(rp.request)}
            if rp.request.replace:
                store.update_compilation(conn, job.compilation["id"], made=stamp, style_snapshot=job.style.to_dict(),
                                         request=request, clips=clip_rows)
            else:
                store.add_compilation(conn, job.name, clips=clip_rows, output_path=ws.to_stored(job.output), made=stamp,
                                      style_snapshot=job.style.to_dict(), request=request,
                                      parent_id=job.compilation["id"])
            conn.commit()                                   # recorded only now that the file is complete
            made.append(job.name)
            items.append(Item(job.name, "completed"))
    except KeyboardInterrupt:
        cancelled = True
        conn.rollback()
        if phase == "fetch":
            items.append(Item("fetching clips again", "cancelled", "stopped by the user"))
            items += [Item(j.name, "skipped", "not started") for j in rp.jobs]
        else:
            items.append(Item(rp.jobs[index].name, "cancelled", "stopped by the user"))
            items += [Item(j.name, "skipped", "not started") for j in rp.jobs[index + 1:]]

    deleted = 0
    if ws.config["delete_used_clips"] and fetched and not cancelled:
        # the clips fetched again were deleted on purpose last time ("delete used clips"): delete them again,
        # but only once every compilation that needed them has been remade
        for vid in sorted(fetched):
            users = [j for j in rp.jobs if any(c.youtube_id == vid for c in j.missing)]
            if users and all(j.name in made for j in users):
                row = store.get_clip(conn, vid)
                try:
                    ws.from_stored(row["path"]).unlink()
                except FileNotFoundError:
                    pass
                store.upsert_clip(conn, vid, row["path"], "missing")
                deleted += 1
        conn.commit()

    result = RunResult(run_id, "", items, made,
                       {"fetched_again": len(fetched), "could_not_fetch": len(failed_fetch), "deleted_again": deleted})
    result.status = "cancelled" if cancelled else result.summary_status()
    store.finish_run(conn, run_id, result.status, [asdict(i) for i in items])
    conn.commit()
    return result
