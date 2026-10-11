"""Helpers the tests share: is ffmpeg here, and small generated test videos with known contents.

Test clip of the colour tests:  0.4 s pure green (its head) + 1 s blue + 0.4 s pure red (its tail) at 25 fps, with a
tone. After joining clips, every clip's head and tail frames must still be on screen in full, so the number of pure
green and red frames in a result must not fall below what the clips contain.
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

HAVE_TOOLS = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
FPS = 25
HEAD = TAIL = 10            # frames of pure green / pure red at each end of a clip
BODY = 25                   # frames of blue in the middle


def _make_clip(path, rate="25"):
    colour = lambda c, d: ["-f", "lavfi", "-i", f"color=c={c}:s=96x96:r={rate}:d={d}"]
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *colour("0x00ff00", 0.4), *colour("0x0000ff", 1.0),
                    *colour("0xff0000", 0.4), "-f", "lavfi", "-i", "sine=f=440:d=1.8",
                    "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]", "-map", "3:a",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)


def _colour_frames(path):
    """How many frames are pure green / blue / red (each frame averaged down to one pixel)."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", "scale=1:1:flags=area",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    n = {"R": 0, "G": 0, "B": 0}
    for i in range(0, len(raw), 3):
        r, g, b = raw[i:i + 3]
        if r > 200 and g < 60 and b < 60:
            n["R"] += 1
        elif g > 200 and r < 60 and b < 60:
            n["G"] += 1
        elif b > 200 and r < 60 and g < 60:
            n["B"] += 1
    return n


def _durations(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, check=True).stdout.split()
    return {line.split(",")[0]: float(line.split(",")[1]) for line in out}


_TEMPLATE = {}


def template_clip():
    """One 1-second test clip, generated once and copied for every 'download'."""
    if "path" not in _TEMPLATE:
        d = Path(tempfile.mkdtemp(prefix="ac_tmpl_"))
        p = d / "t.mp4"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=96x96:rate=25",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-shortest",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(p)], check=True)
        _TEMPLATE["path"] = p
    return _TEMPLATE["path"]
