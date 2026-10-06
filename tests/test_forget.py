"""library forget: compilations leave the records, their clips can be used again, nothing else is touched."""
import contextlib
import io
import unittest
from unittest import mock

from ytt.ops.compile import make as mk
from ytt.ops.errors import OpError
from ytt.ops.library import forget
from ytt.ui import cli
from ytt.workspace import store

try:
    from test_fetch import FetchWorld, vid
    from test_auto_compile import HAVE_TOOLS
except ImportError:
    from tests.test_fetch import FetchWorld, vid
    from tests.test_auto_compile import HAVE_TOOLS


@unittest.skipUnless(HAVE_TOOLS, "needs ffmpeg")
class ForgetWorld(FetchWorld):
    """Six clips; compilation_001 = clips 1,2 and compilation_002 = clips 3,4 (recorded directly, no rendering)."""

    def setUp(self):
        super().setUp()
        self.ws.config["make"]["clips_each"] = 2
        for n in range(1, 7):
            self.add_to_library(n)
        self.comp("compilation_001", 1, 2)
        self.comp("compilation_002", 3, 4)

    def comp(self, name, *clips, **kw):
        ids = [store.get_clip(self.ws.conn, vid(n))["id"] for n in clips]
        cid = store.add_compilation(self.ws.conn, name, clips=[(i, None) for i in ids], **kw)
        self.ws.conn.commit()
        return cid

    def names(self):
        return [r["name"] for r in store.list_compilations(self.ws.conn)]

    def used(self):
        return sorted(c["youtube_id"] for c in store.list_clips(self.ws.conn) if c["used"])

    def forget(self, targets):
        fp = forget.plan_forget(self.ws, targets)
        self.assertTrue(fp.plan.ok, fp.plan.errors)
        return forget.run_forget(self.ws, fp), fp


class Forgetting(ForgetWorld):
    def test_the_plan_says_how_many_clips_come_back_and_changes_nothing(self):
        fp = forget.plan_forget(self.ws, "1")
        self.assertEqual((fp.available, [r["name"] for r in fp.rows]), (2, ["compilation_001"]))
        self.assertIn("2 can be used again", fp.plan.actions[0].text)
        self.assertEqual(self.names(), ["compilation_001", "compilation_002"])
        self.assertEqual(store.list_runs(self.ws.conn), [])

    def test_a_run_removes_the_record_frees_the_clips_and_leaves_the_rest(self):
        result, _ = self.forget("compilation_001")
        self.assertEqual((result.status, result.counts), ("completed", {"forgotten": 1, "available": 2}))
        self.assertEqual(self.names(), ["compilation_002"])
        self.assertEqual(self.used(), [vid(3), vid(4)])
        self.assertEqual(store.get_run(self.ws.conn, result.run_id)["kind"], "forget")

    def test_the_video_file_is_never_touched_and_the_plan_says_so(self):
        out = self.ws.compilations_dir / "compilation_001.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"video")
        self.ws.conn.execute("UPDATE compilations SET output_path = ? WHERE name = 'compilation_001'",
                             (self.ws.to_stored(out),))
        self.ws.conn.commit()
        result, fp = self.forget("1")
        self.assertTrue(any("stays where it is" in n for n in fp.plan.notes))
        self.assertTrue(out.exists())

    def test_a_clip_another_compilation_still_uses_does_not_come_back(self):
        self.comp("compilation_003", 1, 5)
        fp = forget.plan_forget(self.ws, "1")
        self.assertEqual(fp.available, 1)                                   # clip 2 only
        self.forget("1")
        self.assertEqual(self.used(), [vid(1), vid(3), vid(4), vid(5)])

    def test_a_clip_whose_file_is_gone_does_not_come_back(self):
        (self.ws.clips_dir / f"{vid(2)}.mp4").unlink()
        self.assertEqual(forget.plan_forget(self.ws, "1").available, 1)

    def test_all_counts_each_clip_once(self):
        self.comp("compilation_003", 1, 5)
        result, fp = self.forget("all")
        self.assertEqual((result.counts["forgotten"], result.counts["available"]), (3, 5))
        self.assertEqual(self.names(), [])
        self.assertEqual(self.used(), [])

    def test_compilations_remade_from_it_stay_but_lose_the_link(self):
        parent = store.get_compilation(self.ws.conn, "compilation_001")["id"]
        self.comp("compilation_004", 1, 2, parent_id=parent)
        fp = forget.plan_forget(self.ws, "1")
        self.assertTrue(any("compilation_004 was remade from compilation_001" in n for n in fp.plan.notes))
        self.assertEqual(fp.available, 0)                                   # the child still uses its clips
        forget.run_forget(self.ws, fp)
        child = store.get_compilation(self.ws.conn, "compilation_004")
        self.assertIsNotNone(child)
        self.assertIsNone(child["parent_id"])

    def test_forgotten_clips_are_picked_up_by_the_next_make(self):
        self.forget("1")
        mp = mk.plan_make(self.ws, mk.MakeRequest(everything=True), self.backend)
        self.assertEqual([[c.youtube_id for c in g] for g in mp.groups], [[vid(1), vid(2)], [vid(5), vid(6)]])

    def test_a_name_that_does_not_exist_is_an_error(self):
        with self.assertRaises(OpError):
            forget.plan_forget(self.ws, "9")


@unittest.skipUnless(HAVE_TOOLS, "needs ffmpeg")
class ForgetCommand(ForgetWorld):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(cli, "is_tty", lambda: False):
            code = cli.main(["--workspace", str(self.ws.root), "library", "forget", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_a_dry_run_changes_nothing(self):
        code, out, err = self.run_cli("1", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("forget compilation_001: 2 clips, 2 can be used again", out)
        self.assertIn("dry run", out)
        self.assertEqual(len(self.names()), 2)

    def test_without_a_terminal_it_refuses_instead_of_hanging(self):
        code, out, err = self.run_cli("1")
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)
        self.assertEqual(len(self.names()), 2)

    def test_yes_forgets_and_says_how_many_clips_are_free(self):
        code, out, err = self.run_cli("compilation_002", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("Forgot 1 compilation; 2 clips can be used again", out)
        self.assertEqual(self.names(), ["compilation_001"])

    def test_an_unknown_target_is_explained(self):
        code, out, err = self.run_cli("7", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("No compilation matches '7'", err)


if __name__ == "__main__":
    unittest.main()
