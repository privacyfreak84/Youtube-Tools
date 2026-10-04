"""Tests for the style definition (ytt/ops/compile/style.py)."""
import unittest

from ytt.ops.compile.style import DEFAULT_NAME, Style, StyleError, ensure_default
from ytt.workspace import db, store


class StyleTests(unittest.TestCase):
    def test_defaults_match_the_design(self):
        s = Style()
        self.assertEqual((s.transition, s.transition_seconds, s.transition_mode, s.quality, s.max_height),
                         ("fade", 1.0, "hold", "balanced", 1080))

    def test_roundtrip_through_a_dict(self):
        s = Style(transition="wipeleft", transition_seconds=0.4, quality="best", stinger_key="00FF00")
        self.assertEqual(Style.from_dict(s.to_dict()), s)

    def test_unknown_keys_from_a_newer_version_are_ignored(self):
        self.assertEqual(Style.from_dict({"quality": "fast", "blurred_background": True}).quality, "fast")

    def test_bad_values_are_refused_with_a_message_that_says_what_is_allowed(self):
        cases = [({"quality": "ultra"}, "quality"), ({"transition_mode": "x"}, "hold, overlap"),
                 ({"transition_seconds": -1}, "transition_seconds"), ({"transition_seconds": True}, "transition_seconds"),
                 ({"max_height": 0}, "max_height"), ({"max_height": 7.5}, "max_height"),
                 ({"stinger_key": "purple"}, "stinger_key")]
        for data, word in cases:
            with self.subTest(data=data):
                with self.assertRaises(StyleError) as cm:
                    Style.from_dict(data)
                self.assertIn(word, str(cm.exception))

    def test_a_hex_colour_and_the_named_keys_are_accepted(self):
        for key in ("auto", "none", "green", "blue", "black", "white", "0a1B2c"):
            Style(stinger_key=key)

    def test_ensure_default_creates_once_and_never_overwrites(self):
        conn = db.connect(":memory:")
        db.migrate(conn)
        self.assertTrue(ensure_default(conn))
        store.save_style(conn, DEFAULT_NAME, {"transition": "cut"})
        self.assertFalse(ensure_default(conn))
        self.assertEqual(store.get_style(conn, DEFAULT_NAME), {"transition": "cut"})


if __name__ == "__main__":
    unittest.main()
