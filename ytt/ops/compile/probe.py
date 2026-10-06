"""A small cache of what ffprobe said about each clip file, so planning a make over hundreds of clips only reads
each file once. The cache lives in the workspace's cache folder and can be deleted at any time; a file that changed
(size or modified time) is read again. Only readable clips are cached: an unreadable one is tried again next time."""
import json
import os
from pathlib import Path

from ytt.engine import stitch
from ytt.engine.stitch import Clip, EngineError


class ProbeCache:
    def __init__(self, path):
        self.path = Path(path) if path else None
        self.entries = {}
        self.dirty = False
        if self.path and self.path.is_file():
            try:
                self.entries = json.loads(self.path.read_text())
            except (OSError, ValueError):
                self.entries = {}                          # a damaged cache is just an empty one

    @staticmethod
    def _stamp(path):
        st = os.stat(path)
        return [st.st_size, int(st.st_mtime)]

    def probe(self, path):
        """Like stitch.probe (raises EngineError when the file cannot be read), but remembers the answer."""
        key = str(path)
        try:
            stamp = self._stamp(path)
        except OSError:
            raise EngineError(f"not found: {path}")
        hit = self.entries.get(key)
        if hit and hit["stamp"] == stamp:
            c = hit["clip"]
            return Clip(Path(path), c["duration"], c["width"], c["height"], c["fps"], c["has_audio"], c["pix_fmt"])
        clip = stitch.probe(path)
        self.entries[key] = {"stamp": stamp, "clip": {
            "duration": clip.duration, "width": clip.width, "height": clip.height, "fps": clip.fps,
            "has_audio": clip.has_audio, "pix_fmt": clip.pix_fmt}}
        self.dirty = True
        return clip

    def duration(self, path):
        """Seconds, or None when the clip cannot be read."""
        try:
            return self.probe(path).duration
        except EngineError:
            return None

    def save(self):
        if not (self.path and self.dirty):
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".part")
            tmp.write_text(json.dumps(self.entries))
            os.replace(tmp, self.path)
            self.dirty = False
        except OSError:
            pass                                           # the cache is a convenience; never fail a run over it
