#!/usr/bin/env python3
"""
stitch_videos.py - join many videos into one compilation, with transitions.

Requires: Python 3.8+ and ffmpeg/ffprobe (ffmpeg 4.3+ for the xfade filter) on PATH.

Every clip plays in full. A transition gets its own time between two clips, played over the last frame of the
clip before it and the first frame of the clip after it, so nothing is cut off: the result is one transition
length longer per junction. (--overlap brings back the old behaviour, where transitions eat into the clips.)

Examples
--------
  # Everything in a folder (natural sort: clip2 before clip10), 1s fade between all
  python stitch_videos.py clips/ -o out.mp4

  # Explicit files, different global transition and duration
  python stitch_videos.py a.mp4 b.mp4 c.mp4 -t wipeleft -d 0.8

  # Reorder from the command line (1-based, in terms of the input list)
  python stitch_videos.py a.mp4 b.mp4 c.mp4 --order 3,1,2

  # Reorder interactively
  python stitch_videos.py clips/ --reorder

  # Override specific junctions (1 = between 1st and 2nd clip of the FINAL order)
  python stitch_videos.py clips/ -t fade --custom "2=circleopen:1.5, 4=cut, 5=slideleft"

  # Be asked for the transition at every junction
  python stitch_videos.py clips/ --pick-transitions

  # Use a folder of transition videos (the .mov/.mp4 files from make_transitions.py, or your own)
  python stitch_videos.py clips/ --stinger-dir stingers -t stinger              # random one per junction
  python stitch_videos.py clips/ --stinger-dir stingers -t burst --custom "3=halftone, 5=fade"

  # Use ANY video as a transition, on one junction or all of them: @file[|option=value...]
  python stitch_videos.py clips/ --custom "2=@fx/glitch.mp4"                    # opaque video plays over the cut
  python stitch_videos.py clips/ -t "@stingers/burst.mp4"                       # green background detected + keyed out
  python stitch_videos.py clips/ --custom "1=@fx/smoke.mp4|key=black|sim=0.2"    # black background keyed out
  python stitch_videos.py clips/ --custom "4=@fx/whoosh.mp4|cover=0.3|audio=off" # cut at 30% of the video, mute it

  Options after the file (separate with |):
    key=auto|none|green|blue|black|white|RRGGBB   background colour to make transparent (default auto)
    sim=0.12  blend=0.05   key tolerance / edge softness      despill=on   remove green tint from edges
    cover=0.5  or  cut=1.2   where the cut happens: fraction of the video, or seconds into it
    fit=stretch|pad|crop   audio=on|off      (the video's own sound is mixed in at the cut)
  Files with real transparency (.mov from make_transitions.py) need no key. A plain video with no key plays
  on top of the cut as-is, so make its midpoint fully cover the screen, or set cover/cut to where it does.
  The video plays over a held frame of each clip, so it never hides any of the clips' own footage.

  # Preview the plan without rendering
  python stitch_videos.py clips/ --custom "1=random" --dry-run
"""

import argparse
from fractions import Fraction
import glob
import json
import random
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

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
        sys.exit(f"Error: --stinger-dir '{folder}' is not a folder")
    by_stem = {}
    for f in sorted((f for f in d.iterdir() if f.suffix.lower() in VIDEO_EXTS), key=natural_key):
        try:
            by_stem.setdefault(f.stem.lower(), []).append(make_stinger(None, f, {}))
        except ValueError as e:
            sys.exit(f"Error: {e}")
    for stem, items in by_stem.items():
        items.sort(key=lambda st: not st.alpha)              # transparent version gets the bare name
        for j, st in enumerate(items):
            st.name = stem if j == 0 else f"{stem}{st.path.suffix.lower()}"
            STINGERS[st.name] = st
            CHOICES.add(st.name)
            if j == 0:
                STINGER_POOL.append(st.name)
    if not STINGERS:
        sys.exit(f"Error: no videos found in {folder}")
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
            sys.exit(f"Error: not found: {item}")
    return out


def probe(path):
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit(f"Error: ffprobe could not read {path}\n{res.stderr.strip()}")
    data = json.loads(res.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams
                  if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    if video is None:
        sys.exit(f"Error: no video stream in {path}")

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
        sys.exit(f"Error: could not determine duration of {path}")

    fps = video.get("avg_frame_rate") or video.get("r_frame_rate") or "30"
    if fps in ("0/0", "0"):
        fps = "30"
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return Clip(Path(path), float(dur), w, h, fps, has_audio, video.get("pix_fmt", ""))


# --------------------------------------------------------------------------- ordering

def apply_order(clips, order_spec):
    try:
        idx = [int(x) for x in re.split(r"[,\s]+", order_spec.strip()) if x]
    except ValueError:
        sys.exit("Error: --order must be numbers like 3,1,2")
    if any(i < 1 or i > len(clips) for i in idx):
        sys.exit(f"Error: --order values must be between 1 and {len(clips)}")
    return [clips[i - 1] for i in idx]      # omitted clips are dropped, repeats are allowed


def apply_sort(clips, how):
    keys = {
        "name": lambda c: natural_key(c.path),
        "date": lambda c: c.path.stat().st_mtime,
        "size": lambda c: c.path.stat().st_size,
        "duration": lambda c: c.duration,
    }
    return sorted(clips, key=keys[how])


def show_clips(clips):
    for i, c in enumerate(clips, 1):
        print(f"  {i:>2}. {c.name}  ({c.duration:.1f}s, {c.width}x{c.height})")


def interactive_reorder(clips):
    clips = list(clips)
    print("\nInteractive reorder. Commands:  move A B | swap A B | drop N | done")
    while True:
        print()
        show_clips(clips)
        try:
            parts = input("reorder> ").strip().lower().split()
        except EOFError:
            return clips
        if not parts or parts[0] in ("done", "d", "ok"):
            return clips
        try:
            cmd, args = parts[0], [int(x) - 1 for x in parts[1:]]
            if cmd == "move" and len(args) == 2:
                clips.insert(args[1], clips.pop(args[0]))
            elif cmd == "swap" and len(args) == 2:
                clips[args[0]], clips[args[1]] = clips[args[1]], clips[args[0]]
            elif cmd == "drop" and len(args) == 1:
                clips.pop(args[0])
            else:
                print("  ?  try: move 5 1   |   swap 2 3   |   drop 4   |   done")
        except (ValueError, IndexError):
            print("  ?  invalid number")


# --------------------------------------------------------------------------- transitions

def parse_transition(text, default_dur):
    """'fade', 'wipeleft:0.5', 'cut', '@video.mp4|key=green' -> (name, duration)"""
    text = text.strip()
    if text.startswith("@"):
        return register_external(text[1:]), default_dur
    name, _, dur = text.partition(":")
    name = name.strip().lower()
    if name not in CHOICES:
        hint = " - it needs --stinger-dir FOLDER" if name == "stinger" else " (see --list-transitions)"
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
            sys.exit(f"Error: bad --custom entry '{entry}' (expected N=transition[:seconds])")
        try:
            i = int(idx)
            overrides[i] = parse_transition(val, default_dur)
        except ValueError as e:
            sys.exit(f"Error in --custom entry '{entry}': {e}")
        if not 1 <= i <= n_junctions:
            sys.exit(f"Error: junction {i} doesn't exist (there are {n_junctions}: 1..{n_junctions})")
    return overrides


def plan_junctions(clips, default, default_dur, custom_spec, pick, overlap=False):
    n = len(clips) - 1
    plan = [parse_transition(default, default_dur) for _ in range(n)]
    for i, tr in parse_custom(custom_spec or "", n, default_dur).items():
        plan[i - 1] = tr

    if pick:
        print("\nPick a transition per junction: name, name:seconds, or Enter for the default. '?' lists names.")
        for i in range(n):
            while True:
                cur = f"{plan[i][0]}:{plan[i][1]:g}s"
                ans = input(f"  {i + 1}. {clips[i].name} -> {clips[i + 1].name} [{cur}]: ").strip()
                if ans == "?":
                    print("     " + ", ".join(sorted(CHOICES)))
                    continue
                if not ans:
                    break
                try:
                    plan[i] = parse_transition(ans, default_dur)
                    break
                except ValueError as e:
                    print(f"     {e}")

    final = []
    for i, (name, d) in enumerate(plan):
        if name == "random":
            name = random.choice(RANDOM_POOL)
        if name == "stinger":
            name = random.choice(STINGER_POOL)
        if name in STINGERS:
            st = STINGERS[name]
            need_before, need_after = st.cover * st.duration, (1 - st.cover) * st.duration
            if overlap and (clips[i].duration < need_before or clips[i + 1].duration < need_after):
                print(f"  note: junction {i + 1}: a clip is shorter than stinger '{name}' ({st.duration:.1f}s); it will overlap")
            final.append((name, st.duration))
            continue
        if name != "cut":
            # A transition can't be longer than the clips it overlaps; keep headroom for both ends of a clip.
            limit = 0.45 * min(clips[i].duration, clips[i + 1].duration)
            if overlap and d > limit:
                print(f"  note: junction {i + 1} shortened {d:g}s -> {limit:.2f}s (clip too short)")
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
                sys.exit(f"Error: your ffmpeg has no '{filt}' filter, needed to key out the background")
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


# --------------------------------------------------------------------------- main

def parse_resolution(text):
    text = text.lower()
    if text in RESOLUTIONS:
        return RESOLUTIONS[text]
    m = re.fullmatch(r"(\d+)x(\d+)", text)
    if not m:
        sys.exit("Error: --resolution must look like 1920x1080 or 1080p")
    return int(m.group(1)), int(m.group(2))


def fmt_time(s):
    m, s = divmod(s, 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:05.2f}" if h else f"{m}:{s:05.2f}"


def main():
    ap = argparse.ArgumentParser(description="Stitch videos into one compilation with transitions.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("inputs", nargs="*", help="video files, folders, or wildcards")
    ap.add_argument("-o", "--output", default="compilation.mp4")
    ap.add_argument("-t", "--transition", default="fade", help="default transition (or 'cut'/'random'). Default: fade")
    ap.add_argument("--overlap", action="store_true",
                    help="old behaviour: transitions eat into the end of one clip and the start of the next "
                         "(shorter result, but clip content is hidden). Default: every clip plays in full")
    ap.add_argument("-d", "--duration", type=float, default=1.0, help="default transition length in seconds. Default: 1.0")
    ap.add_argument("--stinger-dir", metavar="DIR", help="folder of transition videos (from make_transitions.py, or your own)")
    ap.add_argument("--key", default="auto", metavar="COLOR",
                    help="background to make transparent in transition videos without alpha: auto (detect green/blue "
                         "screens), none, green, blue, black, white or RRGGBB. Default: auto")
    ap.add_argument("--key-similarity", type=float, default=0.12, help="chroma-key tolerance (default 0.12)")
    ap.add_argument("--key-blend", type=float, default=0.05, help="chroma-key edge softness (default 0.05)")
    ap.add_argument("--despill", action="store_true", help="remove green/blue tint from keyed edges (can shift orange/yellow/cyan slightly)")
    ap.add_argument("--no-stinger-audio", action="store_true", help="don't mix in the transition videos' own sound")
    ap.add_argument("--custom", metavar="SPEC", help='per-junction overrides, e.g. "2=circleopen:1.5, 4=cut"')
    ap.add_argument("--pick-transitions", action="store_true", help="choose the transition for each junction interactively")
    ap.add_argument("--order", metavar="LIST", help="new order as 1-based indices, e.g. 3,1,2 (omitted clips are dropped)")
    ap.add_argument("--sort", choices=["name", "date", "size", "duration"], help="sort clips before stitching")
    ap.add_argument("--shuffle", action="store_true", help="randomise clip order")
    ap.add_argument("--reorder", action="store_true", help="reorder interactively")
    ap.add_argument("--resolution", help="output size, e.g. 1920x1080 or 1080p (default: first clip)")
    ap.add_argument("--fps", help="output frame rate (default: first clip)")
    ap.add_argument("--fit", choices=["pad", "crop"], default="pad",
                    help="how to fit clips of other aspect ratios: letterbox (pad) or fill (crop). Default: pad")
    ap.add_argument("--no-audio", action="store_true", help="drop audio")
    ap.add_argument("--crf", type=int, default=18, help="x264 quality, lower = better (default 18)")
    ap.add_argument("--preset", default="medium", help="x264 preset (default medium)")
    ap.add_argument("--overwrite", action="store_true", help="overwrite the output file if it exists")
    ap.add_argument("--dry-run", action="store_true", help="show the plan and ffmpeg command, don't render")
    ap.add_argument("--list-transitions", action="store_true")
    args = ap.parse_args()

    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        sys.exit("Error: ffmpeg/ffprobe not found on PATH")
    STINGER_DEFAULTS.update(key=args.key, sim=args.key_similarity, blend=args.key_blend,
                            audio=not args.no_stinger_audio, despill=args.despill)
    if args.stinger_dir:
        load_stingers(args.stinger_dir)
    if args.list_transitions:
        print("cut (hard cut), random, " + ", ".join(TRANSITIONS))
        if STINGERS:
            print("\nvideo transitions ('stinger' = random pick):")
            for name, st in STINGERS.items():
                print(f"  {name:<16} {st.duration:.1f}s  {describe_stinger(st)}")
        print("\nAny video: @path/to/video.mp4|key=green|cover=0.5  (see --help)")
        return
    if not args.inputs:
        ap.error("no input videos given")
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        sys.exit("Error: ffmpeg/ffprobe not found on PATH")

    paths = resolve_inputs(args.inputs)
    if len(paths) < 2:
        sys.exit("Error: need at least two videos")
    print(f"Probing {len(paths)} videos...")
    clips = [probe(p) for p in paths]

    # ---- ordering: --order / --sort / --shuffle / --reorder can be combined, applied in that order
    if args.order:
        clips = apply_order(clips, args.order)
    if args.sort:
        clips = apply_sort(clips, args.sort)
    if args.shuffle:
        random.shuffle(clips)
    if args.reorder:
        clips = interactive_reorder(clips)
    if len(clips) < 2:
        sys.exit("Error: need at least two videos after reordering")

    # ---- transitions
    try:
        junctions = plan_junctions(clips, args.transition, args.duration, args.custom, args.pick_transitions,
                                   overlap=args.overlap)
    except ValueError as e:
        sys.exit(f"Error: {e}")

    # ---- output format
    width, height = parse_resolution(args.resolution) if args.resolution else (clips[0].width, clips[0].height)
    width, height = width - width % 2, height - height % 2          # x264 needs even dimensions
    fps = args.fps or clips[0].fps
    with_audio = not args.no_audio

    graph, v_label, a_label, total, extra = build_filter_graph(clips, junctions, width, height, fps, args.fit, with_audio,
                                                                   overlap=args.overlap)

    print(f"\nFinal plan  ({width}x{height}, fps {fps}, ~{fmt_time(total)} total)")
    for i, c in enumerate(clips):
        print(f"  {i + 1:>2}. {c.name}  ({c.duration:.1f}s)")
        if i < len(junctions):
            name, d = junctions[i]
            tag = f" (video: {describe_stinger(STINGERS[name])})" if name in STINGERS else ""
            print(f"        -- {name}{tag}{f' {d:g}s' if name != 'cut' else ''} --")

    out = Path(args.output)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats", "-y"]
    for c in clips:
        cmd += ["-i", str(c.path)]
    for path in extra:
        cmd += ["-i", str(path)]
    cmd += ["-filter_complex", graph, "-map", f"[{v_label}]"]
    if with_audio:
        cmd += ["-map", f"[{a_label}]", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]

    if args.dry_run:
        print("\nffmpeg command:\n  " + " ".join(f'"{a}"' if " " in a or ";" in a else a for a in cmd))
        return
    if out.exists() and not args.overwrite:
        sys.exit(f"Error: {out} exists (use --overwrite)")
    if len(graph) > 28000:
        print("warning: very large filter graph; may fail on Windows (command-line length limit)")

    print("\nRendering...")
    if subprocess.run(cmd).returncode != 0:
        sys.exit("ffmpeg failed")
    print(f"\nDone: {out}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled")
