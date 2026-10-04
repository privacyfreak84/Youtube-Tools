"""Tests for the workspace layer: paths, workspace.toml, the database, and the transaction rules."""
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ytt.workspace import config, db, paths, store
from ytt.workspace.errors import WorkspaceError
from ytt.workspace.workspace import Workspace


class TmpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytt_ws_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class PathTests(unittest.TestCase):
    def test_precedence_explicit_then_environment_then_default(self):
        self.assertEqual(paths.resolve("/a/b", {"YTT_WORKSPACE": "/x"}), Path("/a/b"))
        self.assertEqual(paths.resolve(None, {"YTT_WORKSPACE": "/x"}), Path("/x"))
        self.assertEqual(paths.resolve(None, {}), paths.default_workspace().resolve())

    def test_default_is_videos_ytt_in_the_home_folder(self):
        self.assertEqual(paths.default_workspace(), Path.home() / "Videos" / "ytt")

    def test_home_shortcut_is_expanded(self):
        self.assertEqual(paths.resolve("~/somewhere", {}), (Path.home() / "somewhere").resolve())


class ConfigTests(TmpTest):
    def test_missing_file_gives_defaults(self):
        c = config.load(self.tmp / "nope.toml")
        self.assertEqual(c["workers"], 3)
        self.assertEqual(c["make"]["clips_each"], 15)
        self.assertEqual(c["default_style"], "default")

    def test_roundtrip_including_awkward_strings_lists_and_tables(self):
        c = config.load(self.tmp / "x.toml")
        c["cookies_file"] = 'C:\\Users\\me\\"cookies".txt'
        c["watch_folders"] = ["/a b/c", "/d"]
        c["delete_used_clips"] = True
        c["make"]["clips_each"] = 22
        config.save(self.tmp / "x.toml", c)
        self.assertEqual(config.load(self.tmp / "x.toml"), c)

    def test_partial_file_is_filled_with_defaults_and_unknown_keys_survive(self):
        (self.tmp / "p.toml").write_text('workers = 7\nfuture_thing = "keep me"\n[make]\nclips_each = 9\n')
        c = config.load(self.tmp / "p.toml")
        self.assertEqual((c["workers"], c["make"]["clips_each"], c["make"]["prefix"]), (7, 9, "compilation"))
        self.assertEqual(c["future_thing"], "keep me")
        config.save(self.tmp / "p.toml", c)
        self.assertEqual(config.load(self.tmp / "p.toml")["future_thing"], "keep me")

    def test_broken_file_is_a_clear_error_not_a_crash_or_a_silent_reset(self):
        (self.tmp / "bad.toml").write_text("workers = = 3\n")
        with self.assertRaises(WorkspaceError) as cm:
            config.load(self.tmp / "bad.toml")
        self.assertIn("not valid TOML", str(cm.exception))
        self.assertEqual((self.tmp / "bad.toml").read_text(), "workers = = 3\n")      # untouched

    def test_unsupported_values_are_refused_rather_than_written_wrong(self):
        with self.assertRaises(TypeError):
            config.dumps({"x": object()})


class DatabaseTests(TmpTest):
    def test_migrate_creates_the_current_version_and_is_repeatable(self):
        conn = db.connect(self.tmp / "t.db")
        self.assertEqual(db.version(conn), 0)
        self.assertEqual(db.migrate(conn), db.LATEST)
        self.assertEqual(db.migrate(conn), db.LATEST)

    def test_a_database_from_a_newer_ytt_is_refused(self):
        conn = db.connect(self.tmp / "t.db")
        conn.execute(f"PRAGMA user_version = {db.LATEST + 1}")
        with self.assertRaises(WorkspaceError) as cm:
            db.migrate(conn)
        self.assertIn("newer ytt", str(cm.exception))

    def test_a_clip_cannot_exist_without_its_video(self):
        conn = db.connect(self.tmp / "t.db")
        db.migrate(conn)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO clips (youtube_id, path, status, added) VALUES ('nope', 'x', 'ready', 't')")

    def test_a_version_2_database_gets_clip_origins_and_keeps_its_clips(self):
        conn = db.connect(self.tmp / "t.db")
        for v in range(2):                                  # exactly what a ytt from before origins wrote
            conn.executescript(db.MIGRATIONS[v])
            conn.execute(f"PRAGMA user_version = {v + 1}")
        store.upsert_video(conn, "abcdefghijk")
        conn.execute("INSERT INTO clips (youtube_id, path, status, added) VALUES ('abcdefghijk', 'a.mp4', 'ready', 't')")
        conn.commit()
        self.assertEqual(db.migrate(conn), db.LATEST)
        self.assertEqual(store.get_clip(conn, "abcdefghijk")["origin"], "imported")

    def test_clip_origin_is_restricted(self):
        conn = db.connect(self.tmp / "t.db")
        db.migrate(conn)
        store.upsert_video(conn, "abcdefghijk")
        with self.assertRaises(sqlite3.IntegrityError):
            store.upsert_clip(conn, "abcdefghijk", "x.mp4", "ready", "stolen")

    def test_clip_status_is_restricted(self):
        conn = db.connect(self.tmp / "t.db")
        db.migrate(conn)
        store.upsert_video(conn, "abcdefghijk")
        with self.assertRaises(sqlite3.IntegrityError):
            store.upsert_clip(conn, "abcdefghijk", "x.mp4", "downloading")


class StoreTests(TmpTest):
    def setUp(self):
        super().setUp()
        self.ws = Workspace.init(self.tmp / "w")
        self.addCleanup(self.ws.close)
        self.c = self.ws.conn

    def test_video_fill_in_never_replaces_known_values_with_nothing(self):
        store.upsert_video(self.c, "abcdefghijk", title="T", views=5, duration=12.5)
        store.upsert_video(self.c, "abcdefghijk", title=None, views=None, published="2025-01-01")
        v = store.get_video(self.c, "abcdefghijk")
        self.assertEqual((v["title"], v["views"], v["duration"], v["published"]), ("T", 5, 12.5, "2025-01-01"))

    def test_sources_are_unique(self):
        a = store.add_source(self.c, "@Chan")
        self.assertEqual(store.add_source(self.c, "@Chan"), a)
        self.assertEqual(store.list_sources(self.c), ["@Chan"])

    def test_one_clip_per_video_and_updates_in_place(self):
        store.upsert_video(self.c, "abcdefghijk")
        first, created = store.upsert_clip(self.c, "abcdefghijk", "a.mp4", "ready")
        again, created2 = store.upsert_clip(self.c, "abcdefghijk", "b.mp4", "missing")
        self.assertEqual((first, created, created2), (again, True, False))
        self.assertEqual(len(store.list_clips(self.c)), 1)
        self.assertEqual(store.get_clip(self.c, "abcdefghijk")["status"], "missing")

    def test_a_clip_remembers_how_it_arrived_and_a_plain_update_does_not_change_that(self):
        store.upsert_video(self.c, "abcdefghijk")
        store.upsert_clip(self.c, "abcdefghijk", "a.mp4", "ready", "found")
        self.assertEqual(store.get_clip(self.c, "abcdefghijk")["origin"], "found")
        store.upsert_clip(self.c, "abcdefghijk", "b.mp4", "missing")               # no origin given: untouched
        self.assertEqual(store.get_clip(self.c, "abcdefghijk")["origin"], "found")
        store.upsert_clip(self.c, "abcdefghijk", "c.mp4", "ready", "fetched")      # downloaded again by ytt
        self.assertEqual(store.get_clip(self.c, "abcdefghijk")["origin"], "fetched")
        store.upsert_video(self.c, "bbbbbbbbbbb")
        store.upsert_clip(self.c, "bbbbbbbbbbb", "d.mp4", "ready")
        self.assertEqual(store.get_clip(self.c, "bbbbbbbbbbb")["origin"], "imported")

    def test_styles_roundtrip_and_overwrite(self):
        store.save_style(self.c, "clean", {"transition": "fade"})
        store.save_style(self.c, "clean", {"transition": "cut"})
        self.assertEqual(store.get_style(self.c, "clean"), {"transition": "cut"})
        self.assertIsNone(store.get_style(self.c, "ghost"))
        self.assertEqual(store.list_styles(self.c), ["clean"])

    def test_compilation_keeps_clip_order_and_unresolved_entries(self):
        for i, vid in enumerate(["aaaaaaaaaaa", "bbbbbbbbbbb"]):
            store.upsert_video(self.c, vid)
            store.upsert_clip(self.c, vid, f"{i}.mp4", "ready")
        a = store.get_clip(self.c, "aaaaaaaaaaa")["id"]
        b = store.get_clip(self.c, "bbbbbbbbbbb")["id"]
        cid = store.add_compilation(self.c, "compilation_001", clips=[(b, None), (None, "mystery.mp4"), (a, None)],
                                    style_snapshot={"transition": "fade"}, imported=True)
        rows = store.compilation_clips(self.c, cid)
        self.assertEqual([r["position"] for r in rows], [1, 2, 3])
        self.assertEqual([r["youtube_id"] for r in rows], ["bbbbbbbbbbb", None, "aaaaaaaaaaa"])
        self.assertEqual(rows[1]["legacy_name"], "mystery.mp4")
        used = {r["youtube_id"]: r["used"] for r in store.list_clips(self.c)}
        self.assertEqual(used, {"aaaaaaaaaaa": 1, "bbbbbbbbbbb": 1})

    def test_compilation_names_are_unique(self):
        store.add_compilation(self.c, "compilation_001", clips=[])
        with self.assertRaises(sqlite3.IntegrityError):
            store.add_compilation(self.c, "compilation_001", clips=[])


class WorkspaceTests(TmpTest):
    def test_init_creates_the_layout_and_is_safe_to_repeat_without_touching_edits(self):
        with Workspace.init(self.tmp / "w") as ws:
            for sub in paths.SUBFOLDERS:
                self.assertTrue((ws.root / sub).is_dir())
            ws.config["workers"] = 9
            ws.save_config()
            store.add_source(ws.conn, "@Chan")
            ws.conn.commit()
        with Workspace.init(self.tmp / "w") as ws:
            self.assertEqual(ws.config["workers"], 9)
            self.assertEqual(store.list_sources(ws.conn), ["@Chan"])

    def test_open_without_a_workspace_says_how_to_make_one(self):
        with self.assertRaises(WorkspaceError) as cm:
            Workspace.open(self.tmp / "nothing-here")
        self.assertIn("ytt workspace init", str(cm.exception))

    def test_transaction_commits_rolls_back_on_error_and_dry_run_leaves_no_trace(self):
        with Workspace.init(self.tmp / "w") as ws:
            with ws.transaction():
                store.add_source(ws.conn, "@kept")
            with self.assertRaises(RuntimeError):
                with ws.transaction():
                    store.add_source(ws.conn, "@lost")
                    raise RuntimeError("boom")
            with ws.transaction(dry_run=True):
                store.add_source(ws.conn, "@dry")
                self.assertIn("@dry", store.list_sources(ws.conn))        # visible inside...
            self.assertEqual(store.list_sources(ws.conn), ["@kept"])      # ...gone after

    def test_default_folders_follow_the_workspace_and_can_be_pointed_elsewhere(self):
        with Workspace.init(self.tmp / "w") as ws:
            self.assertEqual(ws.clips_dir, ws.root / "clips")
            ws.config["clips_dir"] = "/somewhere/else"
            self.assertEqual(ws.clips_dir, Path("/somewhere/else"))

    def test_paths_inside_the_workspace_are_stored_relative_so_the_folder_can_move(self):
        with Workspace.init(self.tmp / "w") as ws:
            inside = ws.clips_dir / "a.mp4"
            self.assertEqual(ws.to_stored(inside), "clips/a.mp4")
            self.assertEqual(ws.to_stored("/elsewhere/b.mp4"), "/elsewhere/b.mp4")
            self.assertEqual(ws.from_stored("clips/a.mp4"), ws.root / "clips" / "a.mp4")
            self.assertEqual(ws.from_stored("/elsewhere/b.mp4"), Path("/elsewhere/b.mp4"))
        shutil.move(str(self.tmp / "w"), str(self.tmp / "moved"))
        with Workspace.open(self.tmp / "moved") as ws:
            self.assertEqual(ws.from_stored("clips/a.mp4"), self.tmp / "moved" / "clips" / "a.mp4")


if __name__ == "__main__":
    unittest.main()
