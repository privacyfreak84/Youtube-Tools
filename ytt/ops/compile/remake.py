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
from ytt.ops.compile import render as render_mod
from ytt.ops.compile.style import Style, StyleError
from ytt.ops.errors import OpError
from ytt.ops.plan import Action, Item, Plan, RunResult
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


def _next_numbers(ws, count):
    prefix = ws.config["make"]["prefix"]
    pattern = re.compile(rf"^{re.escape(prefix)}_(\d+)$")
    used = [int(m.group(1)) for n in store.compilation_names(ws.conn) if (m := pattern.match(n))]
    folder = ws.compilations_dir
    if folder.is_dir():
        used += [int(m.group(1)) for p in folder.iterdir() if (m := pattern.match(p.stem))]
    start = max(used, default=0) + 1
    return [f"{prefix}_{n:03d}" for n in range(start, start + count)]


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

    new_names = [] if request.replace else _next_numbers(ws, len(rows))
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
        if gone:
            names = ", ".join(c.label for c in gone[:4]) + (f" and {len(gone) - 4} more" if len(gone) > 4 else "")
            skipped.append(f"{label}: {len(gone)} of its {len(clips)} clips are no longer on disk ({names}). "
                           f"Fetching them again arrives with `ytt fetch`; until then they have to be put back.")
            continue
        if request.replace:
            if not row["output_path"]:
                skipped.append(f"{label}: it has no recorded output file to replace; remake it without --replace.")
                continue
            name, output = row["name"], ws.from_stored(row["output_path"])
        else:
            name, output = new_names[i], out_dir / f"{new_names[i]}.mp4"
        problems = []
        if style.stinger_dir and not Path(style.stinger_dir).is_dir():
            problems.append(f"the transition-video folder {style.stinger_dir} does not exist")
        if style.transition == "stinger" and not style.stinger_dir:
            problems.append("its transition is 'stinger' but no transition-video folder is set")
        for what, f in (("intro", style.intro), ("outro", style.outro)):
            if f and not Path(f).is_file():
                problems.append(f"the {what} video {f} does not exist")
        if problems:
            skipped.append(f"{label}: " + "; ".join(problems))
            continue
        job = Job(row, clips, style, style_note, name, output)
        try:
            job.spec = render_mod.spec_for(style, [c.path for c in clips], output)
            job.engine_plan = stitch.plan(job.spec)
        except EngineError as e:
            skipped.append(f"{label}: {e}")
            continue
        plan.notes += [f"{label}: {n}" for n in job.engine_plan.notes]
        total_seconds += job.engine_plan.total_seconds
        verb = "replaces" if request.replace else "makes"
        order_note = ", played backwards" if request.order == "reversed" else ""
        plan.actions.append(Action(
            "render", f"{verb} {name}: {len(clips)} clips{order_note} · {style_note} · "
                      f"about {_mmss(job.engine_plan.total_seconds)} · {output}",
            {"compilation": row["name"], "name": name, "output": str(output), "clips": len(clips)}))
        rp.jobs.append(job)

    if not rp.jobs:
        plan.errors += skipped or ["Nothing to remake."]
        return rp
    plan.warnings += [f"Skipped {s}" for s in skipped]

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


def run_remake(ws, rp, on_progress=None):
    """Carry out a plan that has no errors. Each compilation is rendered and recorded on its own, so one failure
    does not stop the rest, and Ctrl-C keeps everything already finished. on_progress(name, done, total)."""
    if not rp.plan.ok:
        raise OpError("The plan has errors; nothing was done.")
    conn = ws.conn
    run_id = store.create_run(conn, "remake", asdict(rp.request), rp.plan.to_dict())
    conn.commit()                                           # a crash from here on still leaves the run on record
    items, made = [], []
    cancelled = False
    index = 0
    try:
        for index, job in enumerate(rp.jobs):
            try:
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
        items.append(Item(rp.jobs[index].name, "cancelled", "stopped by the user"))
        items += [Item(j.name, "skipped", "not started") for j in rp.jobs[index + 1:]]
    result = RunResult(run_id, "", items, made)
    result.status = "cancelled" if cancelled else result.summary_status()
    store.finish_run(conn, run_id, result.status, [asdict(i) for i in items])
    conn.commit()
    return result
