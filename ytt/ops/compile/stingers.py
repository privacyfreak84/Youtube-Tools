"""`ytt style stingers`: make a folder of transition videos (the old make_transitions.py). Stingers are style
assets (DESIGN.md section 7): the folder is what a style's stinger_dir points to. Works without a workspace.
Request -> plan -> run; replacing files that are already there is a warning that needs a yes."""
from dataclasses import dataclass
from pathlib import Path

from ytt.engine import stingers as engine
from ytt.engine.stitch import EngineError
from ytt.ops.errors import OpError
from ytt.ops.plan import Action, Plan


@dataclass
class StingerRequest:
    outdir: str = "stingers"
    size: str = "1080x1920"
    fps: int = 30
    duration: float = 1.0
    only: str = None                    # "burst,crimp_wipe"; default: all of them
    direction: str = "auto"
    text: str = "CRUNCH!"               # the word on the burst; "" for none
    font: str = None
    colors: str = None                  # "orange=#FF6600,blue=#003399"
    mp4: bool = True
    key_color: str = "00FF00"
    supersample: int = 2
    sheet: bool = True


@dataclass
class StingerPlan:
    plan: Plan
    spec: engine.StingerSpec = None


def list_stingers():
    """[(name, what it looks like)] for every transition video ytt can make."""
    return list(engine.STINGERS.items())


def _names(only):
    if not only:
        return list(engine.STINGERS), None
    names = [n.strip() for n in only.split(",") if n.strip()]
    bad = [n for n in names if n not in engine.STINGERS]
    if bad or not names:
        return names, (f"unknown transition video(s): {', '.join(bad)}. Available: {', '.join(engine.STINGERS)}"
                       if bad else "--only is empty")
    return list(dict.fromkeys(names)), None


def plan_stingers(request, can_load_font=engine.can_load_font):
    """Check the request and say exactly which files would be made. Nothing is written."""
    plan = Plan(title="Make transition videos")
    r = request
    problems = []
    size = palette = key = None
    names, bad = _names(r.only)
    if bad:
        problems.append(bad)
    try:
        size = engine.parse_size(r.size)
    except EngineError as e:
        problems.append(str(e))
    try:
        palette = engine.parse_palette(r.colors)
    except EngineError as e:
        problems.append(str(e))
    try:
        key = engine.parse_key_color(r.key_color)
    except EngineError as e:
        problems.append(str(e))
    if r.direction not in engine.DIRECTIONS:
        problems.append(f"the direction must be one of {', '.join(engine.DIRECTIONS)}")
    if r.fps < 1:
        problems.append("fps must be 1 or more")
    if r.duration <= 0:
        problems.append("the duration must be more than 0 seconds")
    if r.supersample < 1:
        problems.append("supersampling must be 1 or more")
    out = Path(r.outdir)
    if out.exists() and not out.is_dir():
        problems.append(f"{out} exists and is not a folder")
    try:
        engine.require_pillow()
        engine.require_ffmpeg()
    except EngineError as e:
        problems.append(str(e))
    else:
        if r.text and r.font and not can_load_font(r.font):
            problems.append(f"can't use the font {r.font} (it must be a .ttf or .otf file)")
    if problems:
        plan.errors += problems
        return StingerPlan(plan)

    spec = engine.StingerSpec(outdir=out, size=size, fps=r.fps, duration=r.duration, names=names, direction=r.direction,
                              text=r.text, font=r.font, palette=palette, mp4=r.mp4, key_color=key,
                              supersample=r.supersample, sheet=r.sheet)
    for name in names:
        plan.actions.append(Action("render", f"{name}: {engine.STINGERS[name]}", {"name": name}))
    files = engine.output_files(spec)
    plan.notes.append(f"{len(names)} stinger(s), {size[0]}x{size[1]}, {spec.frames} frames at {r.fps} fps, into {out}")
    exists = [f for f in files if f.exists()]
    if exists:
        shown = ", ".join(f.name for f in exists[:4]) + (f" (+{len(exists) - 4} more)" if len(exists) > 4 else "")
        plan.warnings.append(f"{len(exists)} file(s) in {out} will be replaced: {shown}")
    if r.mp4:
        plan.notes.append(f"the .mp4 versions have a #{key.lstrip('#')} background, for chroma-keying in an editor")
    if r.text and "burst" in names and not engine.find_font(r.font):
        plan.notes.append("no usable font was found, so the burst will have no text (use --font path/to/font.ttf)")
    return StingerPlan(plan, spec)


def run_stingers(sp, on_stinger=None, generate=engine.generate):
    """Make the files. Returns the paths written. Problems raise OpError; Ctrl-C is passed on."""
    if not sp.plan.ok or sp.spec is None:
        raise OpError("; ".join(sp.plan.errors) or "nothing to do")
    try:
        return generate(sp.spec, on_stinger=on_stinger)
    except EngineError as e:
        raise OpError(str(e))
