"""
Offline tests for auto_compile.py. No network and no YouTube: the channel list and the downloads are faked
(downloads are copies of one tiny generated clip), but ffmpeg really renders the compilations.

Run from the repo folder:   python -m unittest discover -s tests -v
Needs: ffmpeg + ffprobe on PATH, and  pip install yt-dlp tabulate
"""
import builtins
import contextlib
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
MODULES = ("auto_compile", "make_compilations", "yt_toolkit", "stitch_videos")
HAVE_TOOLS = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
try:
    import yt_dlp  # noqa: F401
    HAVE_YTDLP = True
except ImportError:
    HAVE_YTDLP = False

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


def make_rows(n, newest=datetime(2026, 9, 30, 12, tzinfo=timezone.utc), title="Clip {i}", duration=20):
    """n fake channel entries, newest first, one per day going back. ids are vid0000, vid0001, ..."""
    return [{"id": f"vid{i:04d}", "title": title.format(i=i), "views": 1000 + i, "duration": duration,
             "sort_ts": (newest - timedelta(days=i)).timestamp(), "approx": False,
             "upload_date": (newest - timedelta(days=i)).strftime("%Y-%m-%d")} for i in range(n)]


@unittest.skipUnless(HAVE_TOOLS and HAVE_YTDLP, "needs ffmpeg and yt-dlp")
class WorldTest(unittest.TestCase):
    """A scratch copy of the scripts (so state files stay out of the repo) wired to a fake YouTube."""

    CLIPS_PER_VIDEO = 4

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ac_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for name in MODULES:
            shutil.copy(REPO / f"{name}.py", self.tmp / f"{name}.py")
        for name in MODULES:
            sys.modules.pop(name, None)
        sys.path.insert(0, str(self.tmp))
        self.addCleanup(sys.path.remove, str(self.tmp))
        self.addCleanup(lambda: [sys.modules.pop(n, None) for n in MODULES])
        self.ac = importlib.import_module("auto_compile")
        self.rows = make_rows(40)
        self.fail_ids = set()
        self.downloads = []                      # video ids "downloaded", in call order
        self.write_compile_settings()
        self.ac.list_tab = lambda *a, **k: self.rows
        self.ac.download_one = self.fake_download

    # ---- fake world ----
    def fake_download(self, url, outtmpl, max_height, cookies_opts, **kw):
        vid = url.rsplit("=", 1)[-1]
        if vid in self.fail_ids:
            raise RuntimeError("HTTP Error 403: Forbidden")
        self.downloads.append(vid)
        shutil.copy(template_clip(), outtmpl.replace("%(ext)s", "mp4"))

    def write_compile_settings(self, **over):
        s = {"size_mode": "count", "clips_per_video": self.CLIPS_PER_VIDEO, "transition": "cut", "quality": "fast",
             "output_dir": str(self.tmp / "compilations"), "clips_dir": ""}
        s.update(over)
        (self.tmp / "compile_settings.json").write_text(json.dumps(s))

    # ---- helpers ----
    @property
    def dest(self):
        return self.tmp / "fetched"

    def state(self, name):
        p = self.tmp / name
        return json.loads(p.read_text()) if p.exists() else {}

    def clips_on_disk(self):
        return sorted(p.name for p in self.dest.glob("*.mp4")) if self.dest.exists() else []

    def made(self):
        return sorted(self.state("compile_ledger.json").get("compilations", {}))

    def run_ac(self, *argv, inputs=(), tty=False):
        """Run auto_compile.main() with fake input. Returns everything it printed."""
        answers = iter(inputs)
        out = io.StringIO()
        real_run = subprocess.run

        def captured_run(cmd, **kw):                      # make_compilations runs for real; keep its output
            p = real_run(cmd, capture_output=True, text=True)
            out.write(p.stdout + p.stderr)
            return p

        patches = [mock.patch.object(sys, "argv", ["auto_compile.py", *argv]),
                   mock.patch.object(builtins, "input", lambda *_: next(answers, "")),
                   mock.patch.object(self.ac.subprocess, "run", captured_run)]
        if hasattr(self.ac, "is_tty"):
            patches.append(mock.patch.object(self.ac, "is_tty", lambda: tty))
        with contextlib.ExitStack() as st, contextlib.redirect_stdout(out):
            for p in patches:
                st.enter_context(p)
            try:
                self.ac.main()
            except SystemExit as e:                       # sys.exit("message") is how it reports a stop
                if e.code not in (None, 0):
                    out.write(str(e.code) + "\n")
        return out.getvalue()

    def base(self, *extra):
        return ["@Chan", "--type", "shorts", "--sort", "oldest", "--dest", str(self.dest), "--yes", *extra]


class BaselineTests(WorldTest):
    def test_downloads_then_compiles_whole_compilations(self):
        out = self.run_ac(*self.base("--pick", "8"))
        self.assertEqual(len(self.downloads), 8, out)
        self.assertEqual(len(self.made()), 2, out)
        self.assertEqual(len(self.state("fetch_archive.json")), 8)

    def test_stale_date_range_is_not_carried_into_the_next_interactive_run(self):
        # regression: an old FROM date used to come back as the Enter-default and silently shrank the list
        (self.tmp / "auto_settings.json").write_text(json.dumps(
            {"channel": "@Chan", "type": "shorts", "sort": "oldest", "pick": "5", "date_from": "2026-09-30",
             "dest": str(self.dest)}))
        out = self.run_ac("--dry-run", inputs=[])
        self.assertIn("take the first 5 of the 40 in that list", out)

    def test_failed_download_is_replaced_by_next_in_line_and_order_is_kept(self):
        self.fail_ids = {"vid0039"}                       # the oldest one fails (sort is oldest-first)
        out = self.run_ac(*self.base("--pick", "new:8"))
        self.assertEqual(len(self.clips_on_disk()), 8, out)
        self.assertNotIn("vid0039", self.state("fetch_archive.json"))
        ids = [n.rsplit("_", 1)[-1][:-4] for n in self.clips_on_disk()]
        self.assertEqual(ids, [f"vid{i:04d}" for i in range(38, 30, -1)])   # oldest-first order survived

    def test_delete_after_removes_used_clips_but_keeps_unused_ones(self):
        out = self.run_ac(*self.base("--pick", "6", "--delete-after"))   # 4 get compiled, 2 are left over
        self.assertEqual(len(self.made()), 1, out)
        self.assertEqual(len(self.clips_on_disk()), 2, out)

    def test_pick_parsing(self):
        p = self.ac.parse_pick
        self.assertEqual(p("60"), ("first", 60))
        self.assertEqual(p("new:7"), ("new", 7))
        self.assertEqual(p("last:3"), ("last", 3))


if __name__ == "__main__":
    unittest.main()
