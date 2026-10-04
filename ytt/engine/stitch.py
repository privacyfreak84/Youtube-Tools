"""The rendering engine: join video files into one compilation with ffmpeg, with transitions.

Ported from stitch_videos.py as a library: no printing, no prompting, no sys.exit, no subprocess of its own
script. Problems raise EngineError; things worth showing the user are returned as notes. It imports nothing
from the rest of ytt (see tests/test_architecture.py).

Every clip plays in full by default: a transition gets its own time over the held last/first frame
(RenderSpec.overlap=True brings back the old way, where transitions eat into the clips).

Transition videos ("stingers") are kept in module-level registries, so render()/plan() reset and fill them
from the spec each time. That is fine for one render at a time, which is how ytt uses it.
"""
import glob
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path


class EngineError(Exception):
    """Something went wrong that the user can understand: a missing tool, an unreadable clip, a bad option."""


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".wmv", ".mpg", ".mpeg", ".ts"}

# Transitions supported by ffmpeg's xfade filter, plus "cut" (no transition).
TRANSITIONS = [
    "fade", "fadeblack", "fadewhite", "fadegrays", "dissolve", "pixelize", "radial", "distance",
    "wipeleft", "wiperight", "wipeup", "wipedown", "wipetl", "wipetr", "wipebl", "wipebr",
    "slideleft", "slideright", "slideup", "slidedown",
    "smoothleft", "smoothright", "smoothup", "smoothdown",
    "coverleft", "coverright", "coverup", "coverdown",
    "revealleft", "revealright", "revealup", "revealdown",
    "circlecrop", "rectcrop", "circleopen", "circleclose",
    "horzopen", "horzclose", "vertopen", "vertclose",
    "diagtl", "diagtr", "diagbl", "diagbr",
    "hlslice", "hrslice", "vuslice", "vdslice",
    "hblur", "squeezeh", "squeezev", "zoomin",
    "hlwind", "hrwind", "vuwind", "vdwind",
]
CHOICES = set(TRANSITIONS) | {"cut", "random"}
# "random" picks from this calmer subset so a compilation doesn't look chaotic.
RANDOM_POOL = ["fade", "fadeblack", "dissolve", "wipeleft", "wiperight", "slideleft", "slideright",
               "smoothleft", "smoothright", "circleopen", "circleclose", "horzopen", "vertopen",
               "zoomin", "radial", "coverleft", "revealright"]

RESOLUTIONS = {"480p": (854, 480), "720p": (1280, 720), "1080p": (1920, 1080),
               "1440p": (2560, 1440), "4k": (3840, 2160)}

SAMPLE_RATE = 48000


# --------------------------------------------------------------------------- clips

@dataclass
class Clip:
    path: Path
    duration: float
    width: int
    height: int
    fps: str          # ffmpeg-style, e.g. "30" or "30000/1001"
    has_audio: bool
    pix_fmt: str = ""

    @property
    def name(self):
        return self.path.name


def natural_key(p):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", Path(p).name)]


@dataclass
class Stinger:
    """A video played over the cut between two clips."""
    name: str
    path: Path
    duration: float
    width: int
    height: int
    has_audio: bool
    alpha: bool                 # file carries real transparency (e.g. the .mov from make_transitions.py)
    key: str = None             # hex colour made transparent (files without alpha), or None
    sim: float = 0.12
    blend: float = 0.05
    cover: float = 0.5          # fraction of the video at which the cut happens
    fit: str = None             # None = follow --fit
    audio: bool = True
    despill: bool = False


STINGERS = {}        # name -> Stinger
STINGER_POOL = []    # names picked by '-t stinger' (only the --stinger-dir ones)
STINGER_DEFAULTS = dict(key="auto", sim=0.12, blend=0.05, audio=True, despill=False)
KEY_NAMES = {"green": "00FF00", "blue": "0000FF", "black": "000000", "white": "FFFFFF", "magenta": "FF00FF"}
_EXTERNAL = {}       # (path, options) -> registered name
_FILTERS = None


def have_filter(name):
    global _FILTERS
    if _FILTERS is None:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        _FILTERS = set(re.findall(r"^\s*[A-Z.]{3}\s+(\S+)", out, re.M))
    return name in _FILTERS


def has_alpha(pix_fmt):
    return bool(re.match(r"(yuva|gbrap|argb|rgba|bgra|abgr|ya\d)", pix_fmt or ""))


def detect_key_color(path):
    """Green/blue-screen detection: if the four corners of the first frame are the same saturated colour,
    that colour is the background. Plain black/white backgrounds are never guessed (use key=black)."""
    n = 64
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1",
           "-vf", f"scale={n}:{n}:flags=area,format=rgb24", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    if len(raw) != n * n * 3:
        return None

    def px(x, y):
        i = (y * n + x) * 3
        return tuple(raw[i:i + 3])

    corners = [px(1, 1), px(n - 2, 1), px(1, n - 2), px(n - 2, n - 2)]
    avg = tuple(sum(c[i] for c in corners) // 4 for i in range(3))
    if max(abs(c[i] - avg[i]) for c in corners for i in range(3)) > 30:
        return None
    if max(avg) < 110 or max(avg) - min(avg) < 90:
        return None
    return "%02X%02X%02X" % avg


def make_stinger(name, path, opts):
    c = probe(path)
    o = {**STINGER_DEFAULTS, **opts}
    alpha = has_alpha(c.pix_fmt)
    key = None
    if not alpha:
        k = str(o["key"]).lower().lstrip("#")
        if k == "auto":
            key = detect_key_color(path)
        elif k in ("none", "off"):
            key = None
        elif k in KEY_NAMES:
            key = KEY_NAMES[k]
        elif re.fullmatch(r"[0-9a-f]{6}", k):
            key = k.upper()
        else:
            raise ValueError(f"bad key '{o['key']}' (use auto, none, green, blue, black, white or RRGGBB)")
    cover = o["cut"] / c.duration if "cut" in o else o.get("cover", 0.5)
    return Stinger(name, Path(path), c.duration, c.width, c.height, c.has_audio, alpha, key,
                   o["sim"], o["blend"], min(max(cover, 0.0), 1.0), o.get("fit"), o["audio"], o["despill"])


def register_external(spec):
    """'path|key=green|cover=0.4|...' -> registered stinger name."""
    path, *rest = [x.strip() for x in spec.split("|")]
    path = path.strip('"')
    if not Path(path).is_file():
        raise ValueError(f"transition video not found: {path}")
    opts = {}
    for item in rest:
        k, _, v = item.partition("=")
        k, v = k.strip().lower(), v.strip()
        try:
            if k in ("sim", "blend", "cover", "cut"):
                opts[k] = float(v)
            elif k == "key":
                opts[k] = v
            elif k == "fit" and v in ("stretch", "pad", "crop"):
                opts[k] = v
            elif k in ("audio", "despill"):
                opts[k] = v.lower() in ("on", "1", "yes", "true")
            else:
                raise ValueError
        except ValueError:
            raise ValueError(f"bad option '{item}' in @{path}")
    ident = (str(Path(path).resolve()), tuple(sorted(opts.items())))
    if ident not in _EXTERNAL:
        base, name, n = "@" + Path(path).stem, "@" + Path(path).stem, 2
        while name in STINGERS:
            name, n = f"{base}#{n}", n + 1
        STINGERS[name] = make_stinger(name, path, opts)
        _EXTERNAL[ident] = name
    return _EXTERNAL[ident]


def load_stingers(folder):
    """Register every video in a folder as a transition, named after the file (without extension).
    If 'burst.mov' (transparent) and 'burst.mp4' (green screen) both exist, 'burst' is the transparent one
    and the other is 'burst.mp4'."""
    d = Path(folder)
    if not d.is_dir():
        raise EngineError(f"the transition-video folder '{folder}' is not a folder")
    by_stem = {}
    for f in sorted((f for f in d.iterdir() if f.suffix.lower() in VIDEO_EXTS), key=natural_key):
        try:
            by_stem.setdefault(f.stem.lower(), []).append(make_stinger(None, f, {}))
        except ValueError as e:
            raise EngineError(f"{e}")
    for stem, items in by_stem.items():
        items.sort(key=lambda st: not st.alpha)              # transparent version gets the bare name
        for j, st in enumerate(items):
            st.name = stem if j == 0 else f"{stem}{st.path.suffix.lower()}"
            STINGERS[st.name] = st
            CHOICES.add(st.name)
            if j == 0:
                STINGER_POOL.append(st.name)
    if not STINGERS:
        raise EngineError(f"no videos found in {folder}")
    CHOICES.add("stinger")          # 'stinger' = random pick from the folder


def describe_stinger(st):
    how = "transparent" if st.alpha else (f"keyed #{st.key}" if st.key else "opaque")
    return f"{how}{', with sound' if st.has_audio and st.audio else ''}"


def resolve_inputs(items):
    """Expand folders and wildcards (useful on Windows, where the shell doesn't glob)."""
    out = []
    for item in items:
        p = Path(item)
        if p.is_dir():
            out += sorted((f for f in p.iterdir() if f.suffix.lower() in VIDEO_EXTS), key=natural_key)
        elif any(ch in item for ch in "*?["):
            out += sorted((Path(f) for f in glob.glob(item)), key=natural_key)
        elif p.is_file():
            out.append(p)
        else:
            raise EngineError(f"not found: {item}")
    return out


def probe(path):
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise EngineError(f"ffprobe could not read {path}\n{res.stderr.strip()}")
    data = json.loads(res.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams
                  if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    if video is None:
        raise EngineError(f"no video stream in {path}")

    w, h = int(video["width"]), int(video["height"])
    rotation = 0
    for sd in video.get("side_data_list", []):
        if "rotation" in sd:
            rotation = int(sd["rotation"])
    rotation = rotation or int(video.get("tags", {}).get("rotate", 0))
    if abs(rotation) % 180 == 90:          # phone videos: ffmpeg auto-rotates, so swap dims
        w, h = h, w

    dur = data.get("format", {}).get("duration") or video.get("duration")
    if not dur:
        raise EngineError(f"could not determine duration of {path}")

    fps = video.get("avg_frame_rate") or video.get("r_frame_rate") or "30"
    if fps in ("0/0", "0"):
        fps = "30"
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return Clip(Path(path), float(dur), w, h, fps, has_audio, video.get("pix_fmt", ""))

def parse_transition(text, default_dur):
    """'fade', 'wipeleft:0.5', 'cut', '@video.mp4|key=green' -> (name, duration)"""
    text = text.strip()
    if text.startswith("@"):
        return register_external(text[1:]), default_dur
    name, _, dur = text.partition(":")
    name = name.strip().lower()
    if name not in CHOICES:
        hint = " - it needs a folder of transition videos" if name == "stinger" else ""
        raise ValueError(f"unknown transition '{name}'{hint}")
    return name, float(dur) if dur.strip() else default_dur


def parse_custom(spec, n_junctions, default_dur):
    overrides = {}
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        idx, sep, val = entry.partition("=")
        if not sep:
            raise EngineError(f"bad --custom entry '{entry}' (expected N=transition[:seconds])")
        try:
            i = int(idx)
            overrides[i] = parse_transition(val, default_dur)
        except ValueError as e:
            raise EngineError(f"bad --custom entry '{entry}': {e}")
        if not 1 <= i <= n_junctions:
            raise EngineError(f"junction {i} doesn't exist (there are {n_junctions}: 1..{n_junctions})")
    return overrides


def plan_junctions(clips, default, default_dur, custom_spec=None, overlap=False, rng=None, notes=None):
    """One (transition name, seconds) per junction. 'random' and 'stinger' are resolved to concrete picks here,
    so the plan says exactly what will be used. Messages worth showing go into `notes` (a list), never printed."""
    rng = rng or random.Random()
    notes = notes if notes is not None else []
    n = len(clips) - 1
    plan = [parse_transition(default, default_dur) for _ in range(n)]
    for i, tr in parse_custom(custom_spec or "", n, default_dur).items():
        plan[i - 1] = tr

    final = []
    for i, (name, d) in enumerate(plan):
        if name == "random":
            name = rng.choice(RANDOM_POOL)
        if name == "stinger":
            name = rng.choice(STINGER_POOL)
        if name in STINGERS:
            st = STINGERS[name]
            need_before, need_after = st.cover * st.duration, (1 - st.cover) * st.duration
            if overlap and (clips[i].duration < need_before or clips[i + 1].duration < need_after):
                notes.append(f"junction {i + 1}: a clip is shorter than stinger '{name}' ({st.duration:.1f}s); it will overlap")
            final.append((name, st.duration))
            continue
        if name != "cut":
            # In overlap mode a transition can't be longer than the clips it overlaps.
            limit = 0.45 * min(clips[i].duration, clips[i + 1].duration)
            if overlap and d > limit:
                notes.append(f"junction {i + 1} shortened {d:g}s -> {limit:.2f}s (clip too short)")
                d = limit
            if d < 0.05:
                name, d = "cut", 0.0
        else:
            d = 0.0
        final.append((name, d))
    return final


# --------------------------------------------------------------------------- ffmpeg graph

def build_filter_graph(clips, junctions, width, height, fps, fit, with_audio, overlap=False):
    g = []
    if fit == "crop":
        fit_f = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
    else:
        fit_f = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                 f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black")

    for i, c in enumerate(clips):
        g.append(f"[{i}:v:0]{fit_f},setsar=1,fps={fps},format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS[v{i}]")
        if with_audio:
            d = f"{c.duration:.3f}"
            if c.has_audio:
                # Pad/trim audio to exactly the clip length so sync can't drift across many clips.
                g.append(f"[{i}:a:0]aresample={SAMPLE_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,"
                         f"apad=whole_dur={d},atrim=0:{d},asetpts=PTS-STARTPTS[a{i}]")
            else:
                g.append(f"anullsrc=r={SAMPLE_RATE}:cl=stereo:d={d},aformat=sample_fmts=fltp[a{i}]")

    fpsf = float(Fraction(str(fps)))
    v_cur, a_cur, total = "v0", "a0", clips[0].duration
    stinger_uses = []      # (absolute cut time, Stinger) - overlaid after the main chain is built
    for i, (name, d) in enumerate(junctions, start=1):
        v_out, a_out = f"vx{i}", f"ax{i}"
        is_stinger = name in STINGERS
        if overlap:
            # Legacy: the transition eats into the end of one clip and the start of the next.
            if is_stinger:
                stinger_uses.append((total, STINGERS[name]))
            if name == "cut" or is_stinger:            # stingers sit on a hard cut
                g.append(f"[{v_cur}][v{i}]concat=n=2:v=1:a=0,settb=AVTB[{v_out}]")
                if with_audio:
                    g.append(f"[{a_cur}][a{i}]concat=n=2:v=0:a=1[{a_out}]")
                total += clips[i].duration
            else:
                g.append(f"[{v_cur}][v{i}]xfade=transition={name}:duration={d:.3f}:offset={total - d:.3f},"
                         f"settb=AVTB[{v_out}]")
                if with_audio:
                    g.append(f"[{a_cur}][a{i}]acrossfade=d={d:.3f}:c1=tri:c2=tri[{a_out}]")
                total += clips[i].duration - d
            v_cur, a_cur = v_out, a_out
            continue

        # Default: nothing is lost. The transition gets its own time, played over the last frame of the
        # clip before it and the first frame of the clip after it, so both clips play in full.
        # Holds are whole frames, so picture and sound stay in step however many junctions there are.
        if name == "cut":
            na = nb = 0
        elif is_stinger:                               # the video's cover point lands on the cut
            st = STINGERS[name]
            n = max(1, round(st.duration * fpsf))
            na = round(st.cover * n)
            nb = n - na                                # the two holds add up to exactly the video's length
        else:
            na = nb = max(1, round(d * fpsf))
        hold_a, hold_b = na / fpsf, nb / fpsf
        d = hold_a                                     # crossfade length, frame-exact
        if is_stinger:
            stinger_uses.append((total + hold_a, STINGERS[name]))

        v_tail, v_head = v_cur, f"v{i}"
        if na:
            # fps first: the previous junction's output has timestamps tpad can't pad reliably (a later clip vanished)
            g.append(f"[{v_cur}]fps={fps},tpad=stop_mode=clone:stop={na},settb=AVTB[vt{i}]")
            v_tail = f"vt{i}"
        if nb:
            g.append(f"[v{i}]tpad=start_mode=clone:start={nb},settb=AVTB[vh{i}]")
            v_head = f"vh{i}"
        if name == "cut" or is_stinger:                # stingers sit on top of a hard cut
            g.append(f"[{v_tail}][{v_head}]concat=n=2:v=1:a=0,settb=AVTB[{v_out}]")
        else:
            g.append(f"[{v_tail}][{v_head}]xfade=transition={name}:duration={d:.3f}:offset={total:.3f},"
                     f"settb=AVTB[{v_out}]")

        if with_audio:
            gap = (hold_a + hold_b) if (name == "cut" or is_stinger) else d
            if gap > 0.001:                            # a beat of silence while the picture transitions
                g.append(f"anullsrc=r={SAMPLE_RATE}:cl=stereo:d={gap:.3f},aformat=sample_fmts=fltp[ag{i}]")
                g.append(f"[{a_cur}][ag{i}][a{i}]concat=n=3:v=0:a=1[{a_out}]")
            else:
                g.append(f"[{a_cur}][a{i}]concat=n=2:v=0:a=1[{a_out}]")
        total += (hold_a + hold_b if (name == "cut" or is_stinger) else d) + clips[i].duration
        v_cur, a_cur = v_out, a_out

    extra_inputs = []
    for k, (cut_at, st) in enumerate(stinger_uses):
        idx = len(clips) + k
        extra_inputs.append(st.path)
        start = cut_at - st.cover * st.duration          # so the video's cover point lands on the cut
        pre = ""
        if start < 0:                                    # cut is near the very start: drop the video's head
            pre, start = f"trim=start={-start:.3f},setpts=PTS-STARTPTS,", 0.0

        keyf = despill = ""
        if st.key and not st.alpha:
            filt = "colorkey" if st.key in ("000000", "FFFFFF") else "chromakey"
            if not have_filter(filt):
                raise EngineError(f"your ffmpeg has no '{filt}' filter, needed to key out the background")
            keyf = f"{filt}=color=0x{st.key}:similarity={st.sim}:blend={st.blend},"
            r, gr, b = int(st.key[0:2], 16), int(st.key[2:4], 16), int(st.key[4:6], 16)
            if st.despill and have_filter("despill") and max(r, gr, b) - min(r, gr, b) > 60:
                despill = f"despill=type={'green' if gr >= b and gr >= r else 'blue'},"

        how = st.fit or fit
        if how == "stretch" or abs(st.width / st.height - width / height) < 0.03:
            fit_f = f"scale={width}:{height}"
        elif how == "crop":
            fit_f = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
        else:                                            # letterbox with transparent bars
            fit_f = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                     f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black@0")
        g.append(f"[{idx}:v:0]{pre}{keyf}format=rgba,{despill}{fit_f},setsar=1,fps={fps},"
                 f"setpts=PTS-STARTPTS+{start:.3f}/TB[st{k}]")
        g.append(f"[{v_cur}][st{k}]overlay=eof_action=pass:format=auto[vs{k}]")
        v_cur = f"vs{k}"

    if with_audio:                                       # mix each transition video's own sound in at its cut
        mixed = False
        for k, (cut_at, st) in enumerate(stinger_uses):
            if not (st.has_audio and st.audio):
                continue
            start, pre = cut_at - st.cover * st.duration, ""
            if start < 0:
                pre, start = f"atrim=start={-start:.3f},asetpts=PTS-STARTPTS,", 0.0
            ms = int(round(start * 1000))
            g.append(f"[{len(clips) + k}:a:0]{pre}aresample={SAMPLE_RATE},"
                     f"aformat=sample_fmts=fltp:channel_layouts=stereo,adelay={ms}|{ms},"
                     f"apad=whole_dur={total:.3f},atrim=0:{total:.3f},asetpts=PTS-STARTPTS[sa{k}]")
            g.append(f"[{a_cur}][sa{k}]join=inputs=2:channel_layout=quad:map=0.0-FL|0.1-FR|1.0-BL|1.1-BR,"
                     f"pan=stereo|FL=FL+BL|FR=FR+BR[am{k}]")
            a_cur, mixed = f"am{k}", True
        if mixed:
            g.append(f"[{a_cur}]alimiter=limit=0.97[amixed]")
            a_cur = "amixed"
    return ";".join(g), v_cur, a_cur, total, extra_inputs

def parse_resolution(text):
    text = text.lower()
    if text in RESOLUTIONS:
        return RESOLUTIONS[text]
    m = re.fullmatch(r"(\d+)x(\d+)", text)
    if not m:
        raise EngineError("--resolution must look like 1920x1080 or 1080p")
    return int(m.group(1)), int(m.group(2))


def fmt_time(s):
    m, s = divmod(s, 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:05.2f}" if h else f"{m}:{s:05.2f}"


# --------------------------------------------------------------------------- registries
def reset_stingers():
    """Forget all transition videos and put the options back to their defaults."""
    for name in list(STINGERS):
        CHOICES.discard(name)
    CHOICES.discard("stinger")
    STINGERS.clear()
    STINGER_POOL.clear()
    _EXTERNAL.clear()
    STINGER_DEFAULTS.update(key="auto", sim=0.12, blend=0.05, audio=True, despill=False)


def check_tools():
    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        raise EngineError(f"{' and '.join(missing)} not found. Install ffmpeg and make sure it is on your PATH.")


# --------------------------------------------------------------------------- render API
@dataclass
class RenderSpec:
    files: list                                  # clips in play order (intro and outro included by the caller)
    output: Path
    transition: str = "fade"                     # a name, "cut", "random", "stinger" or "@video.mp4|key=green"
    transition_seconds: float = 1.0
    overlap: bool = False                        # True: the old way (transitions hide the clip ends)
    custom: str = None                           # per-junction overrides, e.g. "2=circleopen:1.5, 4=cut"
    stinger_dir: str = ""
    stinger_key: str = "auto"
    stinger_sim: float = 0.12
    stinger_blend: float = 0.05
    stinger_despill: bool = False
    stinger_audio: bool = True
    resolution: tuple = None                     # (width, height); default: the first clip's
    fps: str = None                              # default: the first clip's
    fit: str = "pad"                             # pad | crop | stretch
    audio: bool = True
    preset: str = "medium"
    crf: int = 18
    seed: int = None                             # makes "random" transitions repeatable


@dataclass
class RenderPlan:
    clips: list
    junctions: list                              # [(transition name, seconds)] as actually chosen
    width: int
    height: int
    fps: str
    total_seconds: float
    with_audio: bool
    graph: str
    command: list                                # the ffmpeg command, writing to the .part file
    part_path: Path
    notes: list = field(default_factory=list)


def part_path_for(output):
    output = Path(output)
    return output.with_name(output.stem + ".part" + output.suffix)


def plan(spec):
    """Probe the clips and work out exactly what render() would do. Nothing is written."""
    check_tools()
    if len(spec.files) < 2:
        raise EngineError("need at least two videos to join")
    reset_stingers()
    STINGER_DEFAULTS.update(key=spec.stinger_key, sim=spec.stinger_sim, blend=spec.stinger_blend,
                            audio=spec.stinger_audio, despill=spec.stinger_despill)
    if spec.stinger_dir:
        load_stingers(spec.stinger_dir)
    for f in spec.files:
        if not Path(f).is_file():
            raise EngineError(f"not found: {f}")
    clips = [probe(f) for f in spec.files]
    notes = []
    try:
        junctions = plan_junctions(clips, spec.transition, spec.transition_seconds, spec.custom,
                                   overlap=spec.overlap, rng=random.Random(spec.seed), notes=notes)
    except ValueError as e:
        raise EngineError(str(e))
    width, height = spec.resolution or (clips[0].width, clips[0].height)
    width, height = width - width % 2, height - height % 2          # x264 needs even dimensions
    fps = spec.fps or clips[0].fps
    graph, v_label, a_label, total, extra = build_filter_graph(clips, junctions, width, height, fps, spec.fit,
                                                               spec.audio, overlap=spec.overlap)
    part = part_path_for(spec.output)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-y"]
    for c in clips:
        cmd += ["-i", str(c.path)]
    for path in extra:
        cmd += ["-i", str(path)]
    cmd += ["-filter_complex", graph, "-map", f"[{v_label}]"]
    if spec.audio:
        cmd += ["-map", f"[{a_label}]", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-c:v", "libx264", "-preset", spec.preset, "-crf", str(spec.crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(part)]
    if len(graph) > 28000:
        notes.append("very large filter graph; this may fail on Windows (command-line length limit)")
    return RenderPlan(clips, junctions, width, height, fps, total, spec.audio, graph, cmd, part, notes)


def render(spec, on_progress=None, plan_=None):
    """Render to a temporary .part file and rename it over the output only when ffmpeg succeeded, so a crash or
    Ctrl-C can never leave a half-written file that looks finished. on_progress(seconds_done, total_seconds) is
    called as ffmpeg advances. Returns the RenderPlan that was used."""
    p = plan_ or plan(spec)
    output = Path(spec.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    p.part_path.unlink(missing_ok=True)
    err = tempfile.TemporaryFile()
    with subprocess.Popen(p.command, stdout=subprocess.PIPE, stderr=err, text=True) as proc:   # closes the pipe too
        try:
            for line in proc.stdout:
                if on_progress and line.startswith("out_time_us="):
                    try:
                        on_progress(max(0.0, int(line.split("=", 1)[1]) / 1_000_000), p.total_seconds)
                    except ValueError:
                        pass                                 # ffmpeg prints N/A before the first frame
            rc = proc.wait()
        except BaseException:                                # Ctrl-C, or an error raised by on_progress
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            p.part_path.unlink(missing_ok=True)
            err.close()
            raise
    err.seek(0)
    message = err.read().decode("utf-8", "replace").strip()
    err.close()
    if rc != 0 or not p.part_path.exists():
        p.part_path.unlink(missing_ok=True)
        raise EngineError("ffmpeg failed" + (f":\n{message[-1500:]}" if message else ""))
    os.replace(p.part_path, output)
    return p
