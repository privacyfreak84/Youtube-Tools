"""The old tool's state, exactly as the real old scripts wrote it, rebuilt in a scratch folder.

tests/fixtures/legacy_state/<scenario>/ holds the files the real old tool (auto_compile.py with make_compilations.py
and stitch_videos.py) wrote when it was run against a fake YouTube: its archive, ledger, settings and caches, plus
media.json, which lists the video files that were in the folder. They were captured once, from the git tag
`old-scripts`, with machine paths replaced by "{ROOT}", which is put back here as the scratch folder. See
tests/fixtures/legacy_state/README.md for how they were made and how to make them again.

The video files themselves are tiny generated stand-ins with the names the old tool gave them (the importer only needs
to know a file is there, and the remake tests need real video to join). A compilation is rendered from as many clips
as the ledger says it had.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

try:
    from support import HAVE_TOOLS, template_clip
except ImportError:
    from tests.support import HAVE_TOOLS, template_clip

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "legacy_state"
_COMPILATIONS = {}                      # how many clips -> a rendered compilation of that many, made once


def _compilation_of(n_clips):
    if n_clips not in _COMPILATIONS:
        from ytt.engine import stitch
        out = Path(tempfile.mkdtemp(prefix="legacy_comp_")) / "c.mp4"
        stitch.render(stitch.RenderSpec(files=[template_clip()] * n_clips, output=out, transition="cut"))
        _COMPILATIONS[n_clips] = out
    return _COMPILATIONS[n_clips]


@unittest.skipUnless(HAVE_TOOLS, "needs ffmpeg")
class LegacyWorld(unittest.TestCase):
    """A scratch folder holding the old tool's state. Name another scenario in SCENARIO, or call old_state_from()."""

    SCENARIO = "two_compilations"       # 2 compilations of 4 one-second clips each, all clips still on disk
    CLIPS_PER_VIDEO = 4                 # what the old tool's settings said

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="legacy_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.old_state_from(self.SCENARIO)

    def old_state_from(self, scenario):
        src = FIXTURES / scenario
        root = json.dumps(str(self.tmp))[1:-1]                      # as it must be written inside a JSON string
        for f in sorted(src.glob("*.json")):
            if f.name != "media.json":
                (self.tmp / f.name).write_text(f.read_text().replace("{ROOT}", root))
        ledger = self.state("compile_ledger.json").get("compilations", {})
        for rel in json.loads((src / "media.json").read_text())["files"]:
            target = self.tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if rel.startswith("compilations/"):
                n = next((len(e["clips"]) for e in ledger.values() if Path(e["file"]).name == target.name),
                         self.CLIPS_PER_VIDEO)
                shutil.copy(_compilation_of(n), target)
            else:
                shutil.copy(template_clip(), target)

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
