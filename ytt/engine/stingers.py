"""Transition videos ("stingers"): short animations with a transparent background that sweep over the cut between
two clips (DESIGN.md section 9). Ported from make_transitions.py as a library: no printing, no sys.exit; problems
raise EngineError. Pillow draws the frames and ffmpeg encodes them; Pillow is only needed when frames are drawn,
so this module imports without it. It imports nothing from the rest of ytt (see tests/test_architecture.py).

A stinger covers the screen completely at its midpoint, which is where the cut is hidden, then uncovers the next
clip. Style: pop-art / comic / candy-wrapper, from a colour palette (amber, wrapper blue, letter red, black
outlines, chocolate brown).

Output per stinger:
  name.mov  transparent (PNG codec, lossless), used directly by the stitch engine
  name.mp4  the same animation over a solid key colour for chroma-keying (MP4/H.264 cannot store transparency)
Plus contact_sheet.png as a preview.
"""
import math
import os
import random
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ytt.engine.stitch import EngineError

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:                       # only an error once something is drawn: see require_pillow()
    Image = ImageDraw = ImageFont = None

PALETTE = {
    "orange": "#FFB41D",
    "blue": "#0000E5",
    "navy": "#070578",
    "red": "#FB1430",
    "white": "#FEFDFF",
    "black": "#000000",
    "choc": "#BB8360",
    "choc_dark": "#6F4527",
    "cyan": "#58E5EF",
    "yellow": "#E9EE57",
}

HOLD = 0.10          # fraction of the stinger where the screen is fully covered (the cut hides here)
FONT_CANDIDATES = [
    "impact.ttf", "Impact.ttf", "/System/Library/Fonts/Supplemental/Impact.ttf",
    "Anton-Regular.ttf", "Bangers-Regular.ttf", "arialbd.ttf", "Arial Black.ttf",
    "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


# --------------------------------------------------------------------------- helpers

def hex_to_rgba(h, a=255):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a)


def clamp01(x):
    return max(0.0, min(1.0, x))


def ease_io(x):                      # smooth in/out
    x = clamp01(x)
    return x * x * (3 - 2 * x)


def ease_out_back(x, k=1.9):         # overshoot "pop"
    x = clamp01(x) - 1
    return 1 + (k + 1) * x ** 3 + k * x ** 2


class Geo:
    """Maps a (u, v) sweep space onto the screen: u runs along the sweep, v across it."""

    def __init__(self, w, h, s, direction):
        self.W, self.H, self.S, self.dir = w, h, s, direction
        horiz = direction in ("left", "right")
        self.U = w if horiz else h
        self.V = h if horiz else w

    def pt(self, u, v):
        d = self.dir
        if d == "right":
            x, y = u, v
        elif d == "left":
            x, y = self.W - u, v
        elif d == "down":
            x, y = v, u
        else:  # up
            x, y = v, self.H - u
        return (x * self.S, y * self.S)


def phase_progress(t, k, n, stagger):
    """Lead (cover) and trail (uncover) progress for layer k of n, bottom->top.
    Bottom layer arrives first and leaves last; all layers cover fully during the HOLD window."""
    win = 0.5 - HOLD / 2 - (n - 1) * stagger
    lead = ease_io((t - k * stagger) / win)
    trail = ease_io((t - (0.5 + HOLD / 2) - (n - 1 - k) * stagger) / win)
    return lead, trail


# --------------------------------------------------------------------------- shapes
# A shape draws the region "covered up to progress p" (p=0 nothing, p=1 whole screen).

def zigzag_wipe(g):
    m = min(g.W, g.H)
    amp, period = 0.022 * m, 0.038 * m          # candy-wrapper crimp

    def shape(d, p, fill):
        edge = -amp + p * (g.U + 2 * amp)
        pts = [g.pt(-3 * amp, -period), g.pt(-3 * amp, g.V + period)]
        n = int((g.V + 2 * period) / (period / 2)) + 1
        for i in range(n):
            pts.append(g.pt(edge + (amp if i % 2 else 0), g.V + period - i * period / 2))
        d.polygon(pts, fill=fill)
    return shape


def burst(g, spikes=22, inner=0.72, seed=7):
    rng = random.Random(seed)
    jitter = [rng.uniform(0.86, 1.12) for _ in range(spikes)]
    rmax = math.hypot(g.W, g.H) / 2 / inner * 1.12

    def shape(d, p, fill):
        r, rot = p * rmax, (1 - p) * 0.5
        pts = []
        for i in range(spikes * 2):
            ang = rot + math.pi * i / spikes
            rr = r * (jitter[i // 2] if i % 2 == 0 else inner)
            pts.append(((g.W / 2 + rr * math.cos(ang)) * g.S, (g.H / 2 + rr * math.sin(ang)) * g.S))
        d.polygon(pts, fill=fill)
    return shape


def bite_wipe(g):
    r0 = 0.06 * min(g.W, g.H)

    def shape(d, p, fill):
        edge = p * (g.U + r0)
        pts = [g.pt(-4 * r0, -2 * r0), g.pt(-4 * r0, g.V + 2 * r0)]
        v, k = g.V + 2 * r0, 0
        while v > -2 * r0:
            r = r0 * (1.0 if k % 2 == 0 else 0.7)
            for j in range(13):                      # semicircular bite taken out of the edge
                a = math.pi * j / 12
                pts.append(g.pt(edge - 0.95 * r * math.sin(a), v - r * (1 - math.cos(a))))
            v -= 2 * r
            k += 1
        d.polygon(pts, fill=fill)
    return shape


# --------------------------------------------------------------------------- transitions

def render_stack(size, s, t, shape, colors, stagger=0.06):
    """Layered wipe: each colour is a layer; bottom colour leads in and trails out (outline look)."""
    w, h = size[0] * s, size[1] * s
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    n = len(colors)
    for k, col in enumerate(colors):
        lead, trail = phase_progress(t, k, n, stagger)
        if lead <= 0 or trail >= 1:
            continue
        mask = Image.new("L", (w, h), 0)
        md = ImageDraw.Draw(mask)
        shape(md, lead, 255)
        if trail > 0:
            shape(md, trail, 0)
        layer = Image.new("RGBA", (w, h), col)
        layer.putalpha(mask)
        img.alpha_composite(layer)
    return img


def render_slats(size, s, t, pal, direction="right", strips=9, stagger=0.30):
    """Slanted comic bars, alternate bars enter from opposite sides, each outlined in black."""
    W, H = size
    w, h = W * s, H * s
    g = Geo(W, H, s, direction)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    skew = 0.05 * min(W, H)
    outline = 0.008 * min(W, H)
    cols = [pal["orange"], pal["blue"], pal["red"], pal["white"]]
    bar = g.V / strips
    win = 0.5 - HOLD / 2
    for i in range(strips):
        rank = i / max(1, strips - 1)
        lead = ease_io((t - rank * stagger * win) / (win * (1 - stagger)))
        trail = ease_io((t - 0.5 - HOLD / 2 - rank * stagger * win) / (win * (1 - stagger)))
        if lead <= 0 or trail >= 1:
            continue
        span = g.U + 2 * skew + 6
        ua, ub = -skew + trail * span, -skew + lead * span
        v0, v1 = i * bar, (i + 1) * bar
        mirror = i % 2 == 1

        def poly(a, b, va, vb):
            uv = [(a + skew, va), (b + skew, va), (b - skew, vb), (a - skew, vb)]
            if mirror:
                uv = [(g.U - u, v) for u, v in uv]
            return [g.pt(u, v) for u, v in uv]

        d.polygon(poly(ua - outline * 2, ub + outline * 2, v0 - 1, v1 + 1), fill=hex_to_rgba(pal["black"]))
        d.polygon(poly(ua, ub, v0 + outline, v1 - outline), fill=hex_to_rgba(cols[i % len(cols)]))
    return img


def render_halftone(size, s, t, pal):
    """Amber halftone dots (45-degree screen, black rims) swell out from the centre, then shrink away."""
    W, H = size
    w, h = W * s, H * s
    cell = 0.05 * min(W, H)
    cx, cy = W / 2, H / 2
    dmax = math.hypot(W, H) / 2 + cell
    win = 0.5 - HOLD / 2
    lead = ease_io(t / win)
    trail = ease_io((t - 0.5 - HOLD / 2) / win)
    spread = 0.55
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    black, orange = hex_to_rgba(pal["black"]), hex_to_rgba(pal["orange"])
    c45 = math.cos(math.pi / 4)
    n = int(dmax / cell) + 2
    dots = []
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            x = cx + (i - j) * cell * c45
            y = cy + (i + j) * cell * c45
            if -cell < x < W + cell and -cell < y < H + cell:
                dots.append((x, y, math.hypot(x - cx, y - cy) / dmax))
    for rim, col, rmax in ((True, black, 0.80), (False, orange, 0.74)):
        for x, y, dist in dots:
            lv = clamp01(lead * (1 + spread) - dist * spread)
            tv = clamp01(trail * (1 + spread) - dist * spread)
            r = rmax * cell * (1 - tv) * lv
            if r <= 0.4:
                continue
            d.ellipse([(x - r) * s, (y - r) * s, (x + r) * s, (y + r) * s], fill=col)
    return img


def find_font(user_font=None):
    for cand in ([user_font] if user_font else []) + FONT_CANDIDATES:
        try:
            ImageFont.truetype(cand, 40)
            return cand
        except OSError:
            continue
    return None


def add_text(img, size, s, t, text, font_path, pal):
    """Pop the logo-style word (red fill, thick black outline) over the fully-covered screen."""
    a_in = ease_out_back((t - 0.28) / 0.12)
    a_out = 1 - ease_io((t - 0.60) / 0.12)
    scale = a_in * a_out
    if scale <= 0.02 or not font_path:
        return img
    W, H = size
    probe = ImageFont.truetype(font_path, 100)
    tw = probe.getbbox(text, stroke_width=9)[2]
    px = max(8, int(100 * (0.84 * W * s) / tw * scale))
    font = ImageFont.truetype(font_path, px)
    sw = max(2, int(px * 0.09))
    layer = Image.new("RGBA", (W * s, H * s), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    pos = (W * s // 2, H * s // 2)
    ld.text((pos[0] + sw, pos[1] + sw), text, font=font, anchor="mm", fill=hex_to_rgba(pal["black"]),
            stroke_width=sw, stroke_fill=hex_to_rgba(pal["black"]))
    ld.text(pos, text, font=font, anchor="mm", fill=hex_to_rgba(pal["red"]),
            stroke_width=sw, stroke_fill=hex_to_rgba(pal["black"]))
    layer = layer.rotate(6, resample=Image.BICUBIC, center=pos)
    img.alpha_composite(layer)
    return img


# --------------------------------------------------------------------------- registry

def auto_direction(size, wanted):
    if wanted != "auto":
        return wanted
    return "up" if size[1] > size[0] else "right"


def build_renderers(size, s, pal, direction, text, font_path):
    dirn = auto_direction(size, direction)
    col = lambda name: hex_to_rgba(pal[name])
    g = Geo(size[0], size[1], s, dirn)

    zig, bur, bit = zigzag_wipe(g), burst(g), bite_wipe(g)

    def r_crimp(t):
        return render_stack(size, s, t, zig, [col(c) for c in ("black", "red", "white", "blue", "orange")])

    def r_slats(t):
        return render_slats(size, s, t, pal, "right" if direction == "auto" else direction)

    def r_halftone(t):
        return render_halftone(size, s, t, pal)

    def r_burst(t):
        img = render_stack(size, s, t, bur, [col(c) for c in ("black", "red", "white", "orange")], stagger=0.05)
        return add_text(img, size, s, t, text, font_path, pal) if text else img

    def r_bite(t):
        return render_stack(size, s, t, bit, [col(c) for c in ("black", "choc_dark", "choc")], stagger=0.07)

    return {
        "crimp_wipe": r_crimp,     # wrapper-crimp zigzag band: black / red / white / blue / amber
        "slats": r_slats,          # slanted comic bars in the four wrapper colours
        "halftone": r_halftone,    # amber halftone dots swell out from the centre
        "burst": r_burst,          # comic starburst with logo-style word
        "bite": r_bite,            # chocolate edge with bites taken out of it
    }


# --------------------------------------------------------------------------- output


# --------------------------------------------------------------------------- preview sheet

def backdrop(w, h):
    """Neutral two-tone stand-in for 'the video' so the preview shows what the stinger covers."""
    img = Image.new("RGBA", (w, h), (90, 96, 110, 255))
    ImageDraw.Draw(img).rectangle([0, h // 2, w, h], fill=(150, 160, 175, 255))
    return img


def contact_sheet(renders, size, out_path, s):
    cols_t = [0.08, 0.22, 0.36, 0.5, 0.64, 0.8]
    tw = 150
    th = int(tw * size[1] / size[0])
    label_h = 18
    sheet = Image.new("RGB", (tw * len(cols_t) + 5 * (len(cols_t) + 1), (th + label_h + 5) * len(renders) + 5), (24, 24, 24))
    sd = ImageDraw.Draw(sheet)
    for row, (name, fn) in enumerate(renders.items()):
        y = 5 + row * (th + label_h + 5)
        sd.text((6, y), name, fill=(230, 230, 230))
        for c, t in enumerate(cols_t):
            frame = fn(t).resize(size, Image.LANCZOS)
            base = backdrop(*size)
            base.alpha_composite(frame)
            sheet.paste(base.convert("RGB").resize((tw, th), Image.LANCZOS), (5 + c * (tw + 5), y + label_h))
    sheet.save(out_path)


# --------------------------------------------------------------------------- the library side
STINGERS = {
    "crimp_wipe": "wrapper-crimp zigzag band: black / red / white / blue / amber",
    "slats": "slanted comic bars in the four wrapper colours",
    "halftone": "amber halftone dots swell out from the centre",
    "burst": "comic starburst with a logo-style word",
    "bite": "chocolate edge with bites taken out of it",
}
DIRECTIONS = ("auto", "left", "right", "up", "down")
_HEX = r"#?[0-9a-fA-F]{6}"


def require_pillow():
    if Image is None:
        raise EngineError("Pillow is needed to make transition videos. Install it with:  pip install pillow")


def require_ffmpeg():
    if not shutil.which("ffmpeg"):
        raise EngineError("ffmpeg not found. Install ffmpeg and make sure it is on your PATH.")


def parse_size(text):
    m = re.fullmatch(r"(\d+)[xX](\d+)", str(text).strip())
    if not m:
        raise EngineError("the size must look like 1080x1920")
    w, h = int(m.group(1)), int(m.group(2))
    w, h = w - w % 2, h - h % 2                          # x264 needs even dimensions
    if w < 2 or h < 2:
        raise EngineError("the size is too small")
    return w, h


def parse_palette(text):
    """'orange=#FF6600,blue=#003399' on top of the built-in palette."""
    pal = dict(PALETTE)
    for pair in filter(None, (x.strip() for x in (text or "").split(","))):
        key, _, value = pair.partition("=")
        key, value = key.strip(), value.strip()
        if key not in pal or not re.fullmatch(_HEX, value):
            raise EngineError(f"bad colour entry '{pair}'. Names: {', '.join(pal)}; values like #FF6600")
        pal[key] = "#" + value.lstrip("#")
    return pal


def parse_key_color(text):
    if not re.fullmatch(_HEX, str(text).strip()):
        raise EngineError("the key colour must be a hex colour like 00FF00")
    return "#" + str(text).strip().lstrip("#").upper()


def can_load_font(path):
    require_pillow()
    try:
        ImageFont.truetype(str(path), 40)
        return True
    except OSError:
        return False


@dataclass
class StingerSpec:
    outdir: Path
    size: tuple = (1080, 1920)
    fps: int = 30
    duration: float = 1.0
    names: list = field(default_factory=lambda: list(STINGERS))
    direction: str = "auto"
    text: str = "CRUNCH!"                      # the word on the burst; "" for none
    font: str = None
    palette: dict = field(default_factory=lambda: dict(PALETTE))
    mp4: bool = True
    key_color: str = "#00FF00"
    supersample: int = 2
    sheet: bool = True

    @property
    def frames(self):
        return max(6, round(self.duration * self.fps))


def output_files(spec):
    """Every file generate() would write, in the order it writes them."""
    out = []
    for name in spec.names:
        out.append(Path(spec.outdir) / f"{name}.mov")
        if spec.mp4:
            out.append(Path(spec.outdir) / f"{name}.mp4")
    if spec.sheet:
        out.append(Path(spec.outdir) / "contact_sheet.png")
    return out


def _part(path):
    return path.with_name(path.stem + ".part" + path.suffix)


def _encode(frames_dir, fps, final, video_args):
    """Encode to a temporary name and rename when ffmpeg succeeded, so a failure never leaves a broken file."""
    part = _part(final)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-framerate", str(fps),
           "-i", str(Path(frames_dir) / "%04d.png"), *video_args, str(part)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    if res.returncode != 0 or not part.exists():
        part.unlink(missing_ok=True)
        raise EngineError(f"ffmpeg failed while encoding {final.name}" + (f":\n{res.stderr.strip()[-600:]}" if res.stderr.strip() else ""))
    os.replace(part, final)


def generate(spec, on_stinger=None):
    """Draw and encode every stinger in the spec. on_stinger(name, number, total) is called before each one.
    Returns the files written. Ctrl-C leaves finished files and no half-written ones."""
    require_pillow()
    require_ffmpeg()
    font_path = find_font(spec.font) if spec.text else None
    renderers = build_renderers(spec.size, spec.supersample, spec.palette, spec.direction, spec.text, font_path)
    out = Path(spec.outdir)
    out.mkdir(parents=True, exist_ok=True)
    frames, key = spec.frames, hex_to_rgba(spec.key_color)
    written = []
    for n, name in enumerate(spec.names, start=1):
        if on_stinger:
            on_stinger(name, n, len(spec.names))
        fn = renderers[name]
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as tmp_key:
            for f in range(frames):
                frame = fn(f / (frames - 1)).resize(spec.size, Image.LANCZOS)
                frame.save(Path(tmp) / f"{f + 1:04d}.png")
                if spec.mp4:
                    flat = Image.new("RGBA", spec.size, key)
                    flat.alpha_composite(frame)
                    flat.convert("RGB").save(Path(tmp_key) / f"{f + 1:04d}.png")
            _encode(tmp, spec.fps, out / f"{name}.mov", ["-c:v", "png", "-pix_fmt", "rgba"])
            written.append(out / f"{name}.mov")
            if spec.mp4:
                _encode(tmp_key, spec.fps, out / f"{name}.mp4",
                        ["-c:v", "libx264", "-preset", "slow", "-crf", "10", "-pix_fmt", "yuv420p", "-movflags", "+faststart"])
                written.append(out / f"{name}.mp4")
    if spec.sheet:
        sheet = out / "contact_sheet.png"
        part = _part(sheet)
        try:
            contact_sheet({n: renderers[n] for n in spec.names}, spec.size, part, spec.supersample)
            os.replace(part, sheet)
        finally:
            part.unlink(missing_ok=True)
        written.append(sheet)
    return written
