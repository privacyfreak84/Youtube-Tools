"""Turning a Style and a list of clips into a render the engine can run."""
from collections import Counter
from pathlib import Path

from ytt.engine import stitch
from ytt.engine.stitch import RenderSpec

QUALITY = {"fast": ("veryfast", 21), "balanced": ("medium", 18), "best": ("slow", 16)}     # x264 preset, crf


def spec_for(style, clip_paths, output, seed=None, probe=stitch.probe):
    """The output size and frame rate are whatever most of the clips use (not the intro, not an odd first clip).
    Raises EngineError if a clip cannot be read."""
    infos = [probe(p) for p in clip_paths]
    size = Counter((c.width, c.height) for c in infos).most_common(1)[0][0]
    fps = Counter(str(c.fps) for c in infos).most_common(1)[0][0]
    files = ([Path(style.intro)] if style.intro else []) + [Path(p) for p in clip_paths] \
        + ([Path(style.outro)] if style.outro else [])
    preset, crf = QUALITY[style.quality]
    return RenderSpec(files=files, output=Path(output), transition=style.transition,
                      transition_seconds=style.transition_seconds, overlap=style.transition_mode == "overlap",
                      stinger_dir=style.stinger_dir, stinger_key=style.stinger_key, stinger_sim=style.stinger_sim,
                      stinger_blend=style.stinger_blend, stinger_despill=style.stinger_despill,
                      stinger_audio=style.stinger_audio, resolution=size, fps=fps, preset=preset, crf=crf, seed=seed)


def style_problems(style):
    """Why this style cannot be rendered right now (a missing intro, outro or transition-video folder); [] if fine."""
    problems = []
    if style.stinger_dir and not Path(style.stinger_dir).is_dir():
        problems.append(f"the transition-video folder {style.stinger_dir} does not exist")
    if style.transition == "stinger" and not style.stinger_dir:
        problems.append("its transition is 'stinger' but no transition-video folder is set")
    for what, f in (("intro", style.intro), ("outro", style.outro)):
        if f and not Path(f).is_file():
            problems.append(f"the {what} video {f} does not exist")
    return problems
