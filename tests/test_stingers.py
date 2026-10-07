"""Transition videos: the drawing and encoding (ytt/engine/stingers.py) and the plan around it
(ytt/ops/compile/stingers.py). Tiny sizes keep it quick; the real ffmpeg and Pillow are used."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ytt.engine import stingers as eng
from ytt.engine import stitch
from ytt.engine.stitch import EngineError
from ytt.ops.compile import stingers as ops
from ytt.ops.errors import OpError

HAVE_PIL = eng.Image is not None
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
NEEDS = unittest.skipUnless(HAVE_PIL and HAVE_FFMPEG, "needs Pillow and ffmpeg")


def tiny_spec(outdir, **kw):
    args = dict(outdir=Path(outdir), size=(96, 64), fps=10, duration=0.6, supersample=1, sheet=False, mp4=False)
    args.update(kw)
    return eng.StingerSpec(**args)


def tiny_request(outdir, **kw):
    args = dict(outdir=str(outdir), size="96x64", fps=10, duration=0.6, supersample=1)
    args.update(kw)
    return ops.StingerRequest(**args)


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytt_stingers_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out = self.tmp / "stingers"


class ParsingTests(unittest.TestCase):
    def test_size_must_look_like_wxh_and_is_made_even(self):
        self.assertEqual(eng.parse_size("1080x1920"), (1080, 1920))
        self.assertEqual(eng.parse_size("101X65"), (100, 64))
        for bad in ("1080", "big", "10x", "x10", "0x0", "1x1"):
            with self.subTest(bad=bad), self.assertRaises(EngineError):
                eng.parse_size(bad)

    def test_the_palette_can_be_changed_by_name_and_a_wrong_entry_names_the_choices(self):
        pal = eng.parse_palette("orange=#FF6600, blue=003399")
        self.assertEqual((pal["orange"], pal["blue"]), ("#FF6600", "#003399"))
        self.assertEqual(pal["red"], eng.PALETTE["red"])
        self.assertEqual(eng.parse_palette(None), eng.PALETTE)
        self.assertEqual(eng.parse_palette(""), eng.PALETTE)
        for bad in ("pink=#FF6600", "orange=red", "orange", "orange=#FFF"):
            with self.subTest(bad=bad), self.assertRaisesRegex(EngineError, "Names: orange"):
                eng.parse_palette(bad)

    def test_the_key_colour_is_a_hex_colour_written_one_way(self):
        self.assertEqual(eng.parse_key_color("00ff00"), "#00FF00")
        self.assertEqual(eng.parse_key_color("#0000e5"), "#0000E5")
        with self.assertRaises(EngineError):
            eng.parse_key_color("green")

    def test_the_frame_count_follows_duration_and_fps_with_a_floor_of_six(self):
        self.assertEqual(eng.StingerSpec(outdir=".", duration=2, fps=30).frames, 60)
        self.assertEqual(eng.StingerSpec(outdir=".", duration=0.1, fps=10).frames, 6)      # a one-frame video can't animate

    def test_the_names_and_the_renderers_agree(self):
        self.assertEqual(list(eng.STINGERS), ["crimp_wipe", "slats", "halftone", "burst", "bite"])


@NEEDS
class DrawingTests(unittest.TestCase):
    """What a stinger is for: clear at both ends, covering the whole screen at the midpoint, where the cut hides."""

    def test_every_stinger_is_clear_at_the_ends_and_covers_everything_at_the_midpoint(self):
        renderers = eng.build_renderers((96, 64), 1, eng.PALETTE, "auto", "CRUNCH!", eng.find_font())
        self.assertEqual(list(renderers), list(eng.STINGERS))
        for name, draw in renderers.items():
            with self.subTest(name=name):
                self.assertEqual(draw(0.0).getchannel("A").getextrema(), (0, 0), "not clear at the start")
                self.assertEqual(draw(1.0).getchannel("A").getextrema(), (0, 0), "not clear at the end")
                self.assertEqual(draw(0.5).getchannel("A").getextrema(), (255, 255), "the cut would show")

    def test_the_direction_follows_the_shape_of_the_screen_unless_chosen(self):
        self.assertEqual(eng.auto_direction((1080, 1920), "auto"), "up")
        self.assertEqual(eng.auto_direction((1920, 1080), "auto"), "right")
        self.assertEqual(eng.auto_direction((1920, 1080), "left"), "left")

    def test_the_palette_colours_reach_the_picture(self):
        pal = eng.parse_palette("black=#112233,red=#112233,white=#112233,orange=#112233")
        draw = eng.build_renderers((96, 64), 1, pal, "right", "", None)["burst"]
        self.assertEqual(draw(0.5).getpixel((48, 32))[:3], (0x11, 0x22, 0x33))


@NEEDS
class GenerateTests(Tmp):
    def probe(self, path):
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
                              "stream=nb_read_frames,pix_fmt,width,height", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, check=True).stdout.strip().split(",")
        return {"w": int(out[0]), "h": int(out[1]), "pix": out[2], "frames": int(out[3])}

    def test_it_writes_a_transparent_mov_with_the_right_size_and_frame_count(self):
        spec = tiny_spec(self.out, names=["burst"])
        written = eng.generate(spec)
        self.assertEqual(written, [self.out / "burst.mov"])
        info = self.probe(self.out / "burst.mov")
        self.assertEqual((info["w"], info["h"], info["frames"]), (96, 64, spec.frames))
        self.assertTrue(stitch.has_alpha(info["pix"]), info["pix"])
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["burst.mov"])

    def test_the_mp4_has_the_key_colour_as_its_background_and_the_sheet_is_a_picture(self):
        spec = tiny_spec(self.out, names=["bite"], mp4=True, sheet=True, key_color="#00FF00")
        written = eng.generate(spec)
        self.assertEqual([p.name for p in written], ["bite.mov", "bite.mp4", "contact_sheet.png"])
        frame = self.tmp / "first.png"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(self.out / "bite.mp4"), "-frames:v", "1", str(frame)], check=True)
        with eng.Image.open(frame) as img:
            r, g, b = img.convert("RGB").getpixel((2, 2))
        self.assertLess(r, 25)
        self.assertGreater(g, 230)
        self.assertLess(b, 25)
        with eng.Image.open(self.out / "contact_sheet.png") as sheet:
            self.assertGreater(sheet.width, 150)

    def test_the_files_it_would_write_are_the_files_it_writes(self):
        spec = tiny_spec(self.out, names=["slats", "bite"], mp4=True, sheet=True)
        written = eng.generate(spec)
        self.assertEqual(written, eng.output_files(spec))
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), sorted(p.name for p in written))

    def test_the_made_stingers_work_as_transitions_in_the_stitch_engine(self):
        eng.generate(tiny_spec(self.out, names=["burst"], mp4=True))
        stitch.reset_stingers()
        self.addCleanup(stitch.reset_stingers)
        stitch.STINGER_DEFAULTS.update(key="none")
        stitch.load_stingers(self.out)
        self.assertEqual(sorted(stitch.STINGERS), ["burst", "burst.mp4"])
        self.assertTrue(stitch.STINGERS["burst"].alpha)                       # the .mov is the transparent one

    def test_ctrl_c_keeps_the_finished_ones_and_leaves_no_part_file(self):
        def stop_at_the_second(name, n, total):
            if n == 2:
                raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            eng.generate(tiny_spec(self.out, names=["slats", "bite", "burst"]), on_stinger=stop_at_the_second)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["slats.mov"])

    def test_a_failed_encode_names_the_file_keeps_an_older_one_and_leaves_nothing_behind(self):
        eng.generate(tiny_spec(self.out, names=["bite"]))
        before = (self.out / "bite.mov").read_bytes()
        failed = subprocess.CompletedProcess([], 1, "", "Unknown encoder 'png'")
        with mock.patch.object(eng.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(EngineError, r"(?s)bite\.mov.*Unknown encoder"):
                eng.generate(tiny_spec(self.out, names=["bite"]))
        self.assertEqual((self.out / "bite.mov").read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["bite.mov"])

    def test_without_pillow_or_ffmpeg_it_says_what_to_install(self):
        with mock.patch.object(eng, "Image", None):
            with self.assertRaisesRegex(EngineError, "pip install pillow"):
                eng.generate(tiny_spec(self.out))
        with mock.patch.object(eng.shutil, "which", return_value=None):
            with self.assertRaisesRegex(EngineError, "ffmpeg not found"):
                eng.generate(tiny_spec(self.out))
        self.assertFalse(self.out.exists())


class PlanTests(Tmp):
    def errors(self, **kw):
        sp = ops.plan_stingers(tiny_request(self.out, **kw))
        self.assertFalse(sp.plan.ok)
        self.assertIsNone(sp.spec)
        self.assertFalse(self.out.exists())
        return sp.plan.errors

    @NEEDS
    def test_the_default_plan_lists_all_five_and_writes_nothing(self):
        sp = ops.plan_stingers(tiny_request(self.out))
        self.assertTrue(sp.plan.ok)
        self.assertEqual([a.data["name"] for a in sp.plan.actions], list(eng.STINGERS))
        self.assertIn("5 stinger(s), 96x64, 6 frames at 10 fps", sp.plan.notes[0])
        self.assertTrue(any("#00FF00" in n for n in sp.plan.notes))
        self.assertEqual(sp.plan.warnings, [])
        self.assertFalse(self.out.exists())

    @NEEDS
    def test_only_picks_in_the_order_given_without_repeats(self):
        sp = ops.plan_stingers(tiny_request(self.out, only="burst, bite,burst"))
        self.assertEqual(sp.spec.names, ["burst", "bite"])

    @NEEDS
    def test_the_options_become_the_spec(self):
        sp = ops.plan_stingers(tiny_request(self.out, direction="left", text="", colors="red=#010203", mp4=False,
                                            sheet=False, key_color="0000ff"))
        s = sp.spec
        self.assertEqual((s.direction, s.text, s.palette["red"], s.mp4, s.sheet, s.key_color),
                         ("left", "", "#010203", False, False, "#0000FF"))
        self.assertEqual(s.size, (96, 64))

    @NEEDS
    def test_files_that_would_be_replaced_are_a_warning_and_stay_untouched(self):
        self.out.mkdir()
        (self.out / "burst.mov").write_bytes(b"mine")
        (self.out / "unrelated.txt").write_bytes(b"x")
        sp = ops.plan_stingers(tiny_request(self.out, only="burst,bite", mp4=False))
        self.assertTrue(sp.plan.ok)
        self.assertEqual(len(sp.plan.warnings), 1)
        self.assertIn("1 file(s)", sp.plan.warnings[0])
        self.assertIn("burst.mov", sp.plan.warnings[0])
        self.assertEqual((self.out / "burst.mov").read_bytes(), b"mine")

    @NEEDS
    def test_every_bad_input_is_an_error_that_names_it(self):
        cases = [(dict(only="burts"), "unknown transition video(s): burts"), (dict(only=" , "), "--only is empty"),
                 (dict(size="big"), "1080x1920"), (dict(colors="pink=#FFFFFF"), "Names:"),
                 (dict(key_color="green"), "hex colour"), (dict(direction="sideways"), "direction"),
                 (dict(fps=0), "fps"), (dict(duration=0), "duration"), (dict(supersample=0), "supersampling")]
        for kw, expected in cases:
            with self.subTest(kw=kw):
                self.assertIn(expected, " ".join(self.errors(**kw)))

    @NEEDS
    def test_all_the_problems_are_listed_not_only_the_first(self):
        self.assertEqual(len(self.errors(size="big", fps=0, key_color="x")), 3)

    @NEEDS
    def test_an_outdir_that_is_a_file_and_an_unusable_font_are_errors(self):
        self.out.write_text("a file")
        sp = ops.plan_stingers(tiny_request(self.out))
        self.assertIn("is not a folder", sp.plan.errors[0])
        self.out.unlink()
        sp = ops.plan_stingers(tiny_request(self.out, font="/nope/font.ttf"))
        self.assertIn("can't use the font /nope/font.ttf", sp.plan.errors[0])
        sp = ops.plan_stingers(tiny_request(self.out, font="/nope/font.ttf", text=""))
        self.assertTrue(sp.plan.ok)                                          # no word, so the font does not matter

    @NEEDS
    def test_a_font_that_loads_is_accepted(self):
        sp = ops.plan_stingers(tiny_request(self.out, font="anything.ttf"), can_load_font=lambda f: True)
        self.assertTrue(sp.plan.ok)

    def test_missing_pillow_and_missing_ffmpeg_are_plan_errors(self):
        with mock.patch.object(eng, "Image", None):
            self.assertIn("pip install pillow", ops.plan_stingers(tiny_request(self.out)).plan.errors[0])
        if HAVE_PIL:
            with mock.patch.object(eng.shutil, "which", return_value=None):
                self.assertIn("ffmpeg not found", ops.plan_stingers(tiny_request(self.out)).plan.errors[0])


@NEEDS
class RunTests(Tmp):
    def test_run_makes_the_planned_files_and_reports_each_stinger(self):
        sp = ops.plan_stingers(tiny_request(self.out, only="halftone,bite", mp4=False, sheet=False))
        seen = []
        written = ops.run_stingers(sp, on_stinger=lambda *a: seen.append(a))
        self.assertEqual(sorted(p.name for p in written), ["bite.mov", "halftone.mov"])
        self.assertEqual(seen, [("halftone", 1, 2), ("bite", 2, 2)])

    def test_a_plan_with_errors_cannot_be_run(self):
        with self.assertRaises(OpError):
            ops.run_stingers(ops.plan_stingers(tiny_request(self.out, fps=0)))

    def test_an_engine_failure_becomes_an_op_error(self):
        sp = ops.plan_stingers(tiny_request(self.out))
        def failing(spec, on_stinger=None):
            raise EngineError("ffmpeg failed while encoding burst.mov")
        with self.assertRaisesRegex(OpError, "burst.mov"):
            ops.run_stingers(sp, generate=failing)

    def test_the_list_names_every_stinger_with_a_description(self):
        names = [n for n, _ in ops.list_stingers()]
        self.assertEqual(names, list(eng.STINGERS))
        self.assertTrue(all(how for _, how in ops.list_stingers()))


if __name__ == "__main__":
    unittest.main()
