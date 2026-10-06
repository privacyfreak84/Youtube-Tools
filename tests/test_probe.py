"""The probe cache: reads each file once, notices changes, never caches failures."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ytt.engine.stitch import Clip, EngineError
from ytt.ops.compile.probe import ProbeCache


def fake_clip(path, duration=5.0):
    return Clip(Path(path), duration, 640, 360, "30", True, "yuv420p")


class ProbeCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.file = self.dir / "a.mp4"
        self.file.write_bytes(b"x" * 10)
        self.cache_file = self.dir / "cache" / "probes.json"

    def test_a_file_is_read_once_and_the_answer_survives_a_new_cache_object(self):
        with mock.patch("ytt.ops.compile.probe.stitch.probe", side_effect=lambda p: fake_clip(p, 7.5)) as probe:
            c = ProbeCache(self.cache_file)
            self.assertEqual(c.probe(self.file).duration, 7.5)
            self.assertEqual(c.duration(self.file), 7.5)
            self.assertEqual(probe.call_count, 1)
            c.save()
            self.assertTrue(self.cache_file.is_file())
            again = ProbeCache(self.cache_file)
            self.assertEqual(again.duration(self.file), 7.5)
            self.assertEqual(probe.call_count, 1)

    def test_a_changed_file_is_read_again(self):
        with mock.patch("ytt.ops.compile.probe.stitch.probe", side_effect=lambda p: fake_clip(p, 1.0)) as probe:
            c = ProbeCache(self.cache_file)
            c.probe(self.file)
            self.file.write_bytes(b"y" * 99)
            c.probe(self.file)
            self.assertEqual(probe.call_count, 2)

    def test_an_unreadable_clip_gives_none_and_is_not_cached(self):
        with mock.patch("ytt.ops.compile.probe.stitch.probe", side_effect=EngineError("bad")) as probe:
            c = ProbeCache(self.cache_file)
            self.assertIsNone(c.duration(self.file))
            self.assertIsNone(c.duration(self.file))
            self.assertEqual(probe.call_count, 2)
            self.assertEqual(c.entries, {})

    def test_a_missing_file_is_an_engine_error_and_a_damaged_cache_is_ignored(self):
        self.cache_file.parent.mkdir()
        self.cache_file.write_text("{not json")
        c = ProbeCache(self.cache_file)
        self.assertEqual(c.entries, {})
        with self.assertRaises(EngineError):
            c.probe(self.dir / "nope.mp4")
        self.assertIsNone(c.duration(self.dir / "nope.mp4"))

    def test_without_a_path_nothing_is_written(self):
        with mock.patch("ytt.ops.compile.probe.stitch.probe", side_effect=lambda p: fake_clip(p)):
            c = ProbeCache(None)
            c.probe(self.file)
            c.save()
        self.assertFalse(self.cache_file.exists())


if __name__ == "__main__":
    unittest.main()
