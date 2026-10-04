"""
Tests for the one-time import of the old scripts' state (ytt/ops/legacy_import.py).

Part 1 builds old-format state by hand (fast). Part 2 runs the real old scripts against the fake YouTube from
test_auto_compile.py, then imports what they really wrote: the check that matters most, because it cannot
drift from the actual file formats.
"""
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from ytt.ops.legacy_import import extract_id, import_legacy
from ytt.workspace import store
from ytt.workspace.errors import WorkspaceError
from ytt.workspace.workspace import Workspace

try:
    from test_auto_compile import WorldTest as _World
except ImportError:                                           # run as `python -m unittest tests....`
    from tests.test_auto_compile import WorldTest as _World

STAMP = "20260101-1200"


def own(i, vid):
    return f"{STAMP}_{i:04d}_{vid}.mp4"


def vid(n):
    return f"vid{n:08d}"


class LegacyDir:
    """Builds a folder the way the old scripts left it."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.fetched = self.root / "fetched"
        self.fetched.mkdir(exist_ok=True)

    def write(self, name, data):
        (self.root / name).write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def clip(self, name, folder=None):
        p = (folder or self.fetched) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"not really a video")
        return p

    def snapshot(self):
        """Every file with a hash: to prove the old folder is left exactly as it was."""
        return {str(p.relative_to(self.root)): hashlib.sha1(p.read_bytes()).hexdigest()
                for p in sorted(self.root.rglob("*")) if p.is_file()}


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytt_imp_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.old = LegacyDir(self.tmp / "old")
        self.ws = Workspace.init(self.tmp / "ws")
        self.addCleanup(self.ws.close)

    def archive_entry(self, n, file, channel="@Chan"):
        return {"file": file, "title": f"Clip {n}", "views": 1000 + n, "duration": 20, "channel": channel, "run": STAMP}

    def basic_state(self):
        files = {n: own(n, vid(n)) for n in (1, 2, 3)}
        for f in files.values():
            self.old.clip(f)
        self.old.write("fetch_archive.json", {vid(n): self.archive_entry(n, f) for n, f in files.items()})
        self.old.write("compile_ledger.json", {"compilations": {
            "compilation_001": {"clips": [files[2], files[1]], "file": str(self.old.root / "compilations" / "compilation_001.mp4"),
                                "made": "2026-01-01 12:30"}}, "skipped": {}})
        self.old.write("auto_settings.json", {"channel": "@Chan", "dest": str(self.old.fetched), "max_height": 720,
                                              "delete_after": True, "check_folders": ["/some/other/folder"]})
        self.old.write("compile_settings.json", {"clips_dir": "", "output_dir": str(self.old.root / "compilations"),
                                                 "clips_per_video": 12, "transition": "wipeleft", "transition_seconds": 0.5,
                                                 "quality": "best", "prefix": "mix"})
        return files


class BasicImportTests(ImportTest):
    def test_videos_clips_sources_and_compilation_come_across_with_order_kept(self):
        self.basic_state()
        r = import_legacy(self.ws, self.old.root)
        self.assertEqual(r.added, {"sources": 1, "videos": 3, "clips": 3, "compilations": 1, "styles": 1})
        self.assertEqual((r.clips_ready, r.clips_missing), (3, 0))
        rows = store.compilation_clips(self.ws.conn, store.get_compilation(self.ws.conn, "compilation_001")["id"])
        self.assertEqual([x["youtube_id"] for x in rows], [vid(2), vid(1)])        # the old order, not sorted
        self.assertEqual(store.get_video(self.ws.conn, vid(1))["views"], 1001)
        self.assertTrue(store.get_compilation(self.ws.conn, "compilation_001")["imported"])
        used = {x["youtube_id"]: x["used"] for x in store.list_clips(self.ws.conn)}
        self.assertEqual(used, {vid(1): 1, vid(2): 1, vid(3): 0})                   # clip 3 was never used

    def test_clips_remember_whether_the_old_tool_downloaded_them_or_they_were_already_there(self):
        files = self.basic_state()
        archive = json.loads((self.old.root / "fetch_archive.json").read_text())
        archive[vid(2)]["adopted"] = True                                          # the old tool only adopted this one
        self.old.write("fetch_archive.json", archive)
        self.old.clip(own(9, vid(9)))                                              # on disk, unknown to the archive
        import_legacy(self.ws, self.old.root)
        origin = {r["youtube_id"]: r["origin"] for r in store.list_clips(self.ws.conn)}
        self.assertEqual(origin, {vid(1): "fetched", vid(2): "found", vid(3): "fetched", vid(9): "found"})

    def test_old_folder_is_left_exactly_as_it_was(self):
        self.basic_state()
        before = self.old.snapshot()
        import_legacy(self.ws, self.old.root)
        self.assertEqual(self.old.snapshot(), before)

    def test_running_it_twice_changes_nothing_the_second_time(self):
        self.basic_state()
        import_legacy(self.ws, self.old.root)
        first = self.ws.counts()
        r2 = import_legacy(self.ws, self.old.root)
        self.assertEqual(self.ws.counts(), first)
        self.assertEqual(r2.added, {"sources": 0, "videos": 0, "clips": 0, "compilations": 0, "styles": 0})
        self.assertEqual(r2.compilations_already_present, 1)

    def test_dry_run_reports_what_a_real_run_does_but_leaves_no_trace(self):
        self.basic_state()
        cfg_before = (self.ws.root / "workspace.toml").read_text()
        dry = import_legacy(self.ws, self.old.root, dry_run=True)
        self.assertEqual(self.ws.counts(), {"sources": 0, "videos": 0, "clips": 0, "compilations": 0, "styles": 0,
                                            "missing_clips": 0})
        self.assertEqual((self.ws.root / "workspace.toml").read_text(), cfg_before)
        self.assertEqual(self.ws.config["workers"], 3)
        real = import_legacy(self.ws, self.old.root)
        self.assertEqual(dry.added, real.added)
        self.assertEqual(dry.settings_applied, real.settings_applied)
        self.assertTrue(dry.dry_run and not real.dry_run)

    def test_sources_come_from_the_archive_and_the_last_run(self):
        self.old.clip(own(1, vid(1)))
        self.old.write("fetch_archive.json", {vid(1): self.archive_entry(1, own(1, vid(1)), channel="@A")})
        self.old.write("auto_settings.json", {"channel": "@B"})
        import_legacy(self.ws, self.old.root)
        self.assertEqual(store.list_sources(self.ws.conn), ["@A", "@B"])

    def test_relative_archive_paths_are_resolved_against_the_download_folder(self):
        self.old.clip("sub/" + own(1, vid(1)))
        self.old.write("fetch_archive.json", {vid(1): self.archive_entry(1, "sub/" + own(1, vid(1)))})
        self.old.write("auto_settings.json", {"dest": str(self.old.fetched)})
        r = import_legacy(self.ws, self.old.root)
        self.assertEqual(r.clips_ready, 1)
        self.assertEqual(store.get_clip(self.ws.conn, vid(1))["path"], str(self.old.fetched / "sub" / own(1, vid(1))))

    def test_files_inside_the_workspace_are_stored_relative(self):
        inside = self.ws.clips_dir
        (inside).mkdir(exist_ok=True)
        (inside / own(1, vid(1))).write_bytes(b"x")
        self.old.write("fetch_archive.json", {vid(1): self.archive_entry(1, own(1, vid(1)))})
        self.old.write("auto_settings.json", {"dest": str(inside)})
        import_legacy(self.ws, self.old.root)
        self.assertEqual(store.get_clip(self.ws.conn, vid(1))["path"], f"clips/{own(1, vid(1))}")

    def test_the_cache_file_is_ignored_without_complaint(self):
        self.basic_state()
        self.old.write("compile_cache.json", {"anything": 1})
        r = import_legacy(self.ws, self.old.root)
        self.assertEqual(r.warnings, [])
        self.assertNotIn("compile_cache.json", r.files_found)


class MessyStateTests(ImportTest):
    def test_clips_deleted_after_use_are_known_but_marked_missing(self):
        files = self.basic_state()
        (self.old.fetched / files[1]).unlink()
        r = import_legacy(self.ws, self.old.root)
        self.assertEqual((r.clips_ready, r.clips_missing), (2, 1))
        self.assertEqual(store.get_clip(self.ws.conn, vid(1))["status"], "missing")
        self.assertEqual(self.ws.counts()["missing_clips"], 1)
        self.assertEqual(r.compilations_with_unresolved_clips, 0)       # missing is not unresolved: the id is known

    def test_a_clip_on_disk_that_the_archive_forgot_is_recovered_by_its_name(self):
        self.old.clip(own(7, vid(7)))
        self.old.clip(f"Some Title [{vid(8)}].mp4")
        self.old.write("auto_settings.json", {"dest": str(self.old.fetched)})
        r = import_legacy(self.ws, self.old.root)
        self.assertEqual((r.added["videos"], r.added["clips"], r.clips_recovered), (2, 2, 2))
        self.assertEqual(store.get_clip(self.ws.conn, vid(8))["status"], "ready")

    def test_files_with_no_id_in_the_name_are_counted_not_guessed(self):
        self.old.clip("holiday_footage.mp4")
        self.old.clip("clip_01234567890.mp4")                           # 11 characters, but not a trusted shape
        self.old.write("auto_settings.json", {"dest": str(self.old.fetched)})
        r = import_legacy(self.ws, self.old.root)
        self.assertEqual((r.added["clips"], r.unidentified_files), (0, 2))

    def test_unfinished_downloads_and_the_output_folder_are_not_mistaken_for_clips(self):
        self.old.clip(f"{STAMP}_0001_{vid(1)}.f137.mp4")
        self.old.clip(f"{STAMP}_0002_{vid(2)}.part.mp4")
        out = self.old.root / "fetched" / "compilations"
        self.old.clip(own(3, vid(3)), folder=out)
        self.old.write("auto_settings.json", {"dest": str(self.old.fetched)})
        self.old.write("compile_settings.json", {"output_dir": str(out)})
        r = import_legacy(self.ws, self.old.root)
        self.assertEqual(r.added["clips"], 0)

    def test_ledger_clips_unknown_to_the_archive_are_recovered_or_reported_as_unresolved(self):
        self.old.clip(own(1, vid(1)))
        self.old.clip("my_own_edit.mp4")
        self.old.write("compile_settings.json", {"clips_dir": str(self.old.fetched)})
        self.old.write("compile_ledger.json", {"compilations": {"compilation_001": {
            "clips": [own(1, vid(1)), "my_own_edit.mp4", own(9, vid(9))], "file": "x.mp4", "made": "2026-01-01 00:00"}},
            "skipped": {"bad.mp4": "unreadable"}})
        r = import_legacy(self.ws, self.old.root)
        rows = store.compilation_clips(self.ws.conn, store.get_compilation(self.ws.conn, "compilation_001")["id"])
        self.assertEqual([x["youtube_id"] for x in rows], [vid(1), None, vid(9)])
        self.assertEqual(rows[1]["legacy_name"], "my_own_edit.mp4")
        self.assertEqual((r.compilations_with_unresolved_clips, r.unresolved_clips), (1, 1))
        self.assertEqual(r.unreadable_in_old_ledger, 1)
        self.assertEqual(store.get_clip(self.ws.conn, vid(9))["status"], "missing")      # known id, file gone

    def test_a_broken_state_file_is_reported_and_the_rest_still_imports(self):
        self.basic_state()
        self.old.write("compile_ledger.json", "{ this is not json")
        before = (self.old.root / "compile_ledger.json").read_text()
        r = import_legacy(self.ws, self.old.root)
        self.assertEqual(r.added["videos"], 3)
        self.assertEqual(r.added["compilations"], 0)
        self.assertTrue(any("compile_ledger.json" in w for w in r.warnings))
        self.assertEqual((self.old.root / "compile_ledger.json").read_text(), before)
        self.assertFalse((self.old.root / "compile_ledger.json.broken").exists())

    def test_a_file_in_the_wrong_shape_is_skipped_with_a_warning(self):
        self.old.write("fetch_archive.json", "[1, 2, 3]")
        self.old.write("auto_settings.json", {"channel": "@Chan"})
        r = import_legacy(self.ws, self.old.root)
        self.assertTrue(any("does not look like" in w for w in r.warnings))

    def test_nothing_to_import_is_an_error_that_names_what_was_looked_for(self):
        with self.assertRaises(WorkspaceError) as cm:
            import_legacy(self.ws, self.old.root)
        self.assertIn("fetch_archive.json", str(cm.exception))

    def test_a_path_that_is_not_a_folder_is_an_error(self):
        with self.assertRaises(WorkspaceError):
            import_legacy(self.ws, self.tmp / "does-not-exist")

    def test_an_existing_clip_is_not_downgraded_to_missing_by_a_second_source_of_truth(self):
        self.old.clip(own(1, vid(1)))
        self.old.write("fetch_archive.json", {vid(1): self.archive_entry(1, own(1, vid(1)))})
        self.old.write("auto_settings.json", {"dest": str(self.old.fetched)})
        import_legacy(self.ws, self.old.root)
        self.old.write("fetch_archive.json", {vid(1): self.archive_entry(1, "gone/elsewhere.mp4")})
        import_legacy(self.ws, self.old.root)
        self.assertEqual(store.get_clip(self.ws.conn, vid(1))["status"], "ready")


class SettingsImportTests(ImportTest):
    def test_old_setup_becomes_workspace_settings_and_the_default_style(self):
        self.basic_state()
        r = import_legacy(self.ws, self.old.root)
        cfg = self.ws.config
        self.assertEqual(cfg["clips_dir"], str(self.old.fetched))
        self.assertEqual(cfg["compilations_dir"], str(self.old.root / "compilations"))
        self.assertTrue(cfg["delete_used_clips"])
        self.assertEqual(cfg["watch_folders"], ["/some/other/folder"])
        self.assertEqual((cfg["make"]["clips_each"], cfg["make"]["prefix"]), (12, "mix"))
        style = store.get_style(self.ws.conn, "default")
        self.assertEqual((style["transition"], style["transition_seconds"], style["quality"], style["max_height"]),
                         ("wipeleft", 0.5, "best", 720))
        self.assertEqual(style["transition_mode"], "hold")             # the old tool had no such setting: new default
        self.assertTrue(r.style_created)
        saved = Workspace.open(self.ws.root).config                    # and it reached the file on disk
        self.assertEqual(saved["make"]["clips_each"], 12)

    def test_settings_you_already_changed_in_the_new_workspace_are_kept(self):
        self.basic_state()
        self.ws.config["delete_used_clips"] = False
        self.ws.config["workers"] = 8
        self.ws.config["make"]["clips_each"] = 30
        self.ws.save_config()
        store.save_style(self.ws.conn, "default", {"transition": "cut"})
        self.ws.conn.commit()
        self.ws.config["clips_dir"] = "/my/choice"
        r = import_legacy(self.ws, self.old.root)
        self.assertEqual(self.ws.config["clips_dir"], "/my/choice")
        self.assertEqual(self.ws.config["make"]["clips_each"], 30)
        self.assertEqual(self.ws.config["workers"], 8)
        self.assertEqual(store.get_style(self.ws.conn, "default"), {"transition": "cut"})
        self.assertFalse(r.style_created)
        self.assertIn("clips_dir", r.settings_kept)
        self.assertIn("make.clips_each", r.settings_kept)
        self.assertIn("style 'default'", r.settings_kept)

    def test_an_invalid_old_value_becomes_a_warning_and_a_default_style_not_a_crash(self):
        self.old.write("compile_settings.json", {"quality": "ultra-mega"})
        r = import_legacy(self.ws, self.old.root)
        self.assertTrue(any("could not be imported as a style" in w for w in r.warnings))
        self.assertEqual(store.get_style(self.ws.conn, "default")["quality"], "balanced")

    def test_no_old_settings_means_no_invented_style(self):
        self.old.clip(own(1, vid(1)))
        self.old.write("fetch_archive.json", {vid(1): self.archive_entry(1, own(1, vid(1)))})
        r = import_legacy(self.ws, self.old.root)
        self.assertEqual(r.added["styles"], 0)
        self.assertFalse(r.style_created)


class ExtractIdTests(unittest.TestCase):
    def test_trusted_shapes_only(self):
        self.assertEqual(extract_id("20260101-1200_0001_abcDEF_-123.mp4"), "abcDEF_-123")
        self.assertEqual(extract_id("My Short [abcDEF_-123].mp4"), "abcDEF_-123")
        self.assertEqual(extract_id("deep/er/My Short [abcDEF_-123].webm"), "abcDEF_-123")
        for name in ("holiday_footage.mp4", "clip_01234567890.mp4", "20260101-1200_0002_aaaaaaaaaaa.f137.mp4",
                     "[short].mp4", "x [abcDEF_-12].mp4"):
            self.assertIsNone(extract_id(name), name)


# ---------------------------------------------------------------------- part 2: the real old scripts
class RealOldStateTests(_World):
    """Run the real old tool against the fake YouTube, then import the state it actually wrote."""

    def test_state_written_by_the_real_old_scripts_imports_completely_and_correctly(self):
        out = self.run_ac(*self.base("--pick", "comps:2"))
        old_archive = self.state("fetch_archive.json")
        old_ledger = self.state("compile_ledger.json")
        self.assertEqual(len(old_ledger["compilations"]), 2, out)
        self.assertTrue(old_archive)

        ws_root = self.tmp / "new_ws"
        with Workspace.init(ws_root) as ws:
            r = import_legacy(ws, self.tmp)
            self.assertEqual(r.warnings, [])
            self.assertEqual(ws.counts()["videos"], len(old_archive))
            self.assertEqual(ws.counts()["clips"], len(old_archive))
            self.assertEqual(ws.counts()["compilations"], 2)
            self.assertEqual(r.unresolved_clips, 0)
            self.assertEqual(r.clips_missing, 0)
            by_file = {Path(e["file"]).name: v for v, e in old_archive.items()}
            for name, entry in old_ledger["compilations"].items():
                rows = store.compilation_clips(ws.conn, store.get_compilation(ws.conn, name)["id"])
                self.assertEqual([x["youtube_id"] for x in rows], [by_file[Path(k).name] for k in entry["clips"]], name)
            for v, e in old_archive.items():
                self.assertEqual(store.get_video(ws.conn, v)["title"], e["title"])
                self.assertTrue(ws.from_stored(store.get_clip(ws.conn, v)["path"]).is_file())
            self.assertEqual(ws.config["make"]["clips_each"], self.CLIPS_PER_VIDEO)
            self.assertEqual(store.get_style(ws.conn, "default")["transition"], "cut")
            self.assertEqual(store.list_sources(ws.conn), ["@Chan"])

    def test_clips_the_old_tool_deleted_after_use_import_as_missing_but_known(self):
        self.run_ac(*self.base("--pick", "new:6", "--delete-after"))     # 6 fetched, 4 used and deleted, 2 kept
        old_archive = self.state("fetch_archive.json")
        self.assertEqual((len(old_archive), len(self.clips_on_disk())), (6, 2))
        with Workspace.init(self.tmp / "new_ws") as ws:
            r = import_legacy(ws, self.tmp)
            self.assertEqual((r.clips_ready, r.clips_missing), (2, 4))
            self.assertEqual(ws.counts()["videos"], 6)
            self.assertEqual(r.unresolved_clips, 0)                      # deleted files still have known ids
            statuses = sorted(c["status"] for c in store.list_clips(ws.conn))
            self.assertEqual(statuses, ["missing"] * 4 + ["ready"] * 2)
            used = [c for c in store.list_clips(ws.conn) if c["used"]]
            self.assertEqual(len(used), 4)


if __name__ == "__main__":
    unittest.main()
