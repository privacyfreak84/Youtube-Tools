#!/usr/bin/env python3
"""
make_transitions.py - generate "stinger" transitions from a colour palette + style.

A stinger is a short transparent-background animation that sweeps over the cut between two clips:
it covers the screen completely at its midpoint (that's where stitch_videos.py cuts), then
uncovers the next clip.  Style here: pop-art / comic / candy-wrapper, built from the Nestle Crunch
palette (amber, wrapper blue, letter red, black outlines, chocolate brown).

Requires: Python 3.8+, Pillow, ffmpeg on PATH.   pip install pillow

Examples
--------
  python make_transitions.py                                  # 5 transitions, 1080x1920, into ./stingers
  python make_transitions.py --size 1920x1080 -o stingers_wide
  python make_transitions.py --only burst,crimp_wipe --text "CRUNCH!"
  python make_transitions.py --colors orange=#FF6600,blue=#003399 --duration 0.8

Output per transition:
  name.mov  transparent (PNG codec, lossless) - used by stitch_videos.py
  name.mp4  same animation over a solid key colour (default green) for chroma-keying in an editor,
            since MP4/H.264 cannot store transparency.  Skip with --no-mp4, change with --key-color.
Plus contact_sheet.png as a preview.
"""

import argparse
import math
import random
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Pillow is required:  pip install pillow")

# Sampled from the screenshots (wrapper blue/red/white, amber panel, chocolate, cyan highlight).
# navy / choc_dark are darker shades derived for outlines and shading, not sampled directly.
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

def encode(frames_dir, fps, out_path):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-framerate", str(fps),
           "-i", str(frames_dir / "%04d.png"), "-c:v", "png", "-pix_fmt", "rgba", str(out_path)]
    if subprocess.run(cmd).returncode != 0:
        sys.exit("ffmpeg failed while encoding " + str(out_path))


def encode_mp4(frames_dir, fps, out_path):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-framerate", str(fps),
           "-i", str(frames_dir / "%04d.png"), "-c:v", "libx264", "-preset", "slow", "-crf", "10",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_path)]
    if subprocess.run(cmd).returncode != 0:
        sys.exit("ffmpeg failed while encoding " + str(out_path))


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


def parse_size(txt):
    m = re.fullmatch(r"(\d+)[xX](\d+)", txt)
    if not m:
        sys.exit("Error: --size must look like 1080x1920")
    w, h = int(m.group(1)), int(m.group(2))
    return w - w % 2, h - h % 2


def main():
    ap = argparse.ArgumentParser(description="Generate palette-driven stinger transitions.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("-o", "--outdir", default="stingers")
    ap.add_argument("--size", default="1080x1920", help="WxH, match your compilation (default 1080x1920, vertical)")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--duration", type=float, default=1.0, help="seconds per stinger (default 1.0)")
    ap.add_argument("--only", help="comma list of transitions to make (default: all)")
    ap.add_argument("--direction", default="auto", choices=["auto", "left", "right", "up", "down"])
    ap.add_argument("--text", default="CRUNCH!", help="word on the burst transition; '' to disable")
    ap.add_argument("--font", help="path to a .ttf for the burst text (default: Impact/Arial Black/DejaVu)")
    ap.add_argument("--colors", help="override palette, e.g. orange=#FF6600,blue=#003399")
    ap.add_argument("--no-mp4", action="store_true", help="only write the transparent .mov files")
    ap.add_argument("--key-color", default="00FF00",
                    help="background colour of the .mp4 versions, for chroma-keying (default 00FF00, green)")
    ap.add_argument("--ss", type=int, default=2, help="supersampling for smooth edges; 1 is faster (default 2)")
    ap.add_argument("--no-sheet", action="store_true", help="skip contact_sheet.png")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    pal = dict(PALETTE)
    for pair in filter(None, (args.colors or "").split(",")):
        k, _, v = pair.partition("=")
        if k.strip() not in pal or not re.fullmatch(r"#?[0-9a-fA-F]{6}", v.strip()):
            sys.exit(f"Error: bad --colors entry '{pair}'. Names: {', '.join(pal)}")
        pal[k.strip()] = "#" + v.strip().lstrip("#")

    size = parse_size(args.size)
    font_path = find_font(args.font) if args.text else None
    if args.text and not font_path:
        print("note: no usable font found, burst will have no text (use --font path/to/font.ttf)")
    renderers = build_renderers(size, args.ss, pal, args.direction, args.text, font_path)

    if args.list:
        print("\n".join(renderers))
        return
    if not shutil.which("ffmpeg"):
        sys.exit("Error: ffmpeg not found on PATH")

    names = [n.strip() for n in args.only.split(",")] if args.only else list(renderers)
    bad = [n for n in names if n not in renderers]
    if bad:
        sys.exit(f"Error: unknown transition(s): {', '.join(bad)}. Available: {', '.join(renderers)}")

    if not re.fullmatch(r"#?[0-9a-fA-F]{6}", args.key_color):
        sys.exit("Error: --key-color must be a hex colour like 00FF00")
    key = hex_to_rgba(args.key_color)
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    frames = max(6, round(args.duration * args.fps))
    chosen = {n: renderers[n] for n in names}

    for name, fn in chosen.items():
        print(f"Rendering {name} ({frames} frames)...", flush=True)
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as tmp_key:
            tmp, tmp_key = Path(tmp), Path(tmp_key)
            for f in range(frames):
                t = f / (frames - 1)
                frame = fn(t).resize(size, Image.LANCZOS)
                frame.save(tmp / f"{f + 1:04d}.png")
                if not args.no_mp4:
                    flat = Image.new("RGBA", size, key)
                    flat.alpha_composite(frame)
                    flat.convert("RGB").save(tmp_key / f"{f + 1:04d}.png")
            encode(tmp, args.fps, out / f"{name}.mov")
            if not args.no_mp4:
                encode_mp4(tmp_key, args.fps, out / f"{name}.mp4")

    if not args.no_sheet:
        contact_sheet(chosen, size, out / "contact_sheet.png", args.ss)
    print(f"\nDone: {len(chosen)} stinger(s) in {out}/  ({size[0]}x{size[1]}, {frames} frames @ {args.fps}fps)")
    if not args.no_mp4:
        print(f"MP4s use a #{args.key_color.lstrip('#').upper()} background: apply a chroma key in your editor to make it transparent.")
    print("Use them with:  python stitch_videos.py clips/ --stinger-dir " + str(out) + " -t stinger")


if __name__ == "__main__":
    main()
