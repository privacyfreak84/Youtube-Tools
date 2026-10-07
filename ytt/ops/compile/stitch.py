"""`ytt stitch`: join any videos into one file (DESIGN.md section 7). A standalone expert utility: no workspace, no
library, no records. The rendering is the engine's; this module decides which videos, in what order, and checks
the request before anything is written (request -> plan -> run)."""
import random
import re
import shlex
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from ytt.engine import stitch as engine
from ytt.engine.stitch import EngineError, RenderSpec
from ytt.ops.errors import OpError
from ytt.ops.plan import Action, Plan

SORTS = ("name", "date", "size", "duration")
FITS = ("pad", "crop")
PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")
OUTPUT_EXTS = (".mp4", ".m4v", ".mov", ".mkv")


@dataclass
class StitchRequest:
    inputs: list                                   # files, folders or wildcards
    output: str = "compilation.mp4"
    transition: str = "fade"
    duration: float = 1.0                          # transition seconds
    overlap: bool = False
    stinger_dir: str = ""
    key: str = "auto"
    key_similarity: float = 0.12
    key_blend: float = 0.05
    despill: bool = False
    stinger_audio: bool = True
    custom: str = None                             # per-junction overrides, "2=circleopen:1.5, 4=cut"
    order: str = None                              # new order as 1-based positions, "3,1,2" (left-out videos are dropped)
    sort: str = None
    shuffle: bool = False
    resolution: str = None                         # "1920x1080" or "1080p"; default: the first video's
    fps: str = None                                # default: the first video's
    fit: str = "pad"
    audio: bool = True
    crf: int = 18
    preset: str = "medium"
    overwrite: bool = False
    seed: int = None                               # makes random order and random transitions repeatable


@dataclass
class StitchPlan:
    plan: Plan
    spec: RenderSpec = None
    render_plan: object = None                     # the engine's RenderPlan; None when the plan has errors

    def command_text(self):
        """The ffmpeg command as one line a person can copy (it writes to the temporary .part file)."""
        return " ".join(shlex.quote(a) for a in self.render_plan.command) if self.render_plan else ""


def apply_order(paths, text):
    """'3,1,2' -> those videos in that order. Left-out videos are dropped, repeats are allowed."""
    try:
        picks = [int(x) for x in re.split(r"[,\s]+", text.strip()) if x]
    except ValueError:
        raise OpError("--order must be positions like 3,1,2")
    if not picks:
        raise OpError("--order is empty; give positions like 3,1,2")
    if any(i < 1 or i > len(paths) for i in picks):
        raise OpError(f"--order positions must be between 1 and {len(paths)}")
    return [paths[i - 1] for i in picks]


def apply_sort(paths, how, probe=engine.probe):
    keys = {
        "name": lambda p: engine.natural_key(p),
        "date": lambda p: p.stat().st_mtime,
        "size": lambda p: p.stat().st_size,
        "duration": lambda p: probe(p).duration,
    }
    return sorted(paths, key=keys[how])


def _check_numbers(r):
    problems = []
    if r.duration < 0:
        problems.append("--duration can't be negative")
    if not 0 <= r.crf <= 51:
        problems.append("--crf must be between 0 and 51")
    if r.preset not in PRESETS:
        problems.append(f"--preset must be one of {', '.join(PRESETS)}")
    if r.fit not in FITS:
        problems.append(f"--fit must be one of {', '.join(FITS)}")
    if r.sort is not None and r.sort not in SORTS:
        problems.append(f"--sort must be one of {', '.join(SORTS)}")
    if r.fps is not None and not re.fullmatch(r"\d+(\.\d+)?(/\d+)?", str(r.fps)):
        problems.append("--fps must be a number like 30 or 29.97 (or 30000/1001)")
    return problems


def _same_file(a, b):
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def plan_stitch(request, probe=engine.probe, make_plan=engine.plan):
    """Work out exactly what would be made. Nothing is written. Problems become plan errors, not exceptions."""
    plan = Plan(title="Join videos")
    r = request
    errors = _check_numbers(r)
    out = Path(r.output)
    if out.suffix.lower() not in OUTPUT_EXTS:
        errors.append(f"the output file needs an extension like {', '.join(OUTPUT_EXTS)} (got {r.output!r})")
    if errors:
        plan.errors += errors
        return StitchPlan(plan)
    try:
        paths = engine.resolve_inputs(r.inputs)
        if len(paths) < 2:
            raise EngineError("need at least two videos to join" + (f" (found {len(paths)})" if paths else ""))
        if r.order:
            paths = apply_order(paths, r.order)
        if r.sort:
            paths = apply_sort(paths, r.sort, probe)
        if r.shuffle:
            random.Random(r.seed).shuffle(paths)
        if len(paths) < 2:
            raise EngineError("need at least two videos after ordering")
        if any(_same_file(p, out) for p in paths):
            raise EngineError(f"the output {out} is one of the videos being joined; choose another name")
        if out.exists() and not r.overwrite:
            raise EngineError(f"{out} already exists (add --overwrite to replace it)")
        spec = RenderSpec(
            files=paths, output=out, transition=r.transition, transition_seconds=r.duration, overlap=r.overlap,
            custom=r.custom, stinger_dir=r.stinger_dir or "", stinger_key=r.key, stinger_sim=r.key_similarity,
            stinger_blend=r.key_blend, stinger_despill=r.despill, stinger_audio=r.stinger_audio,
            resolution=engine.parse_resolution(r.resolution) if r.resolution else None, fps=r.fps, fit=r.fit,
            audio=r.audio, preset=r.preset, crf=r.crf, seed=r.seed)
        rp = make_plan(spec)
    except (EngineError, OpError) as e:
        plan.errors.append(str(e))
        return StitchPlan(plan)
    plan.actions.append(Action(
        "render", f"join {len(rp.clips)} videos into {out}  ({rp.width}x{rp.height}, {_fps_text(rp.fps)} fps, "
                  f"about {engine.fmt_time(rp.total_seconds)})",
        {"lines": _lines(rp)}))
    plan.notes += rp.notes
    if out.exists():
        plan.notes.append(f"{out} will be replaced")
    if not rp.with_audio:
        plan.notes.append("the sound is left out")
    return StitchPlan(plan, spec, rp)


def _fps_text(fps):
    try:
        value = Fraction(str(fps))
    except (ValueError, ZeroDivisionError):
        return str(fps)
    return f"{float(value):.2f}".rstrip("0").rstrip(".")


def _lines(rp):
    lines = []
    for i, clip in enumerate(rp.clips):
        lines.append(f"{i + 1:>2}. {clip.name}  ({clip.duration:.1f}s)")
        if i < len(rp.junctions):
            name, seconds = rp.junctions[i]
            st = engine.STINGERS.get(name)
            label = f"{name} (video: {engine.describe_stinger(st)})" if st else name
            lines.append(f"      -- {label}" + ("" if name == "cut" else f" {seconds:g}s") + " --")
    return lines


def run_stitch(sp, on_progress=None, render=engine.render):
    """Make the file. Problems raise OpError; Ctrl-C leaves no half-written file behind and is passed on."""
    if not sp.plan.ok or sp.render_plan is None:
        raise OpError("; ".join(sp.plan.errors) or "nothing to do")
    try:
        render(sp.spec, on_progress=on_progress, plan_=sp.render_plan)
    except EngineError as e:
        raise OpError(str(e))
    return Path(sp.spec.output)


@dataclass
class TransitionList:
    builtin: list = field(default_factory=list)        # names that need nothing
    videos: list = field(default_factory=list)         # (name, seconds, description) from a --stinger-dir folder


def list_transitions(stinger_dir="", key="auto"):
    """Every transition a person can name. With a folder of transition videos, those are listed too."""
    engine.reset_stingers()
    try:
        out = TransitionList(builtin=["cut", "random", *engine.TRANSITIONS])
        if stinger_dir:
            engine.STINGER_DEFAULTS.update(key=key)
            try:
                engine.load_stingers(stinger_dir)
            except EngineError as e:
                raise OpError(str(e))
            out.videos = [(n, st.duration, engine.describe_stinger(st)) for n, st in engine.STINGERS.items()]
        return out
    finally:
        engine.reset_stingers()
