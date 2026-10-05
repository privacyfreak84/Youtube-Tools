"""The grouping rules, with plain numbers (no files, no ffmpeg)."""
import unittest

from ytt.ops.compile import groups as g
from ytt.ops.compile.groups import Sizing


def lengths(*secs):
    """clips named c1, c2, ... with the given lengths; None = unreadable. Returns (pool, duration_of)."""
    table = {f"c{i}": s for i, s in enumerate(secs, 1)}
    return list(table), table.get


class BuildGroups(unittest.TestCase):
    def test_full_groups_and_what_is_left_over(self):
        pool, dur = lengths(*[10] * 11)
        groups, held, bad = g.build_groups(pool, Sizing("count", 4), dur)
        self.assertEqual([len(x) for x in groups], [4, 4])
        self.assertEqual(held, ["c9", "c10", "c11"])
        self.assertEqual(bad, [])

    def test_a_group_never_has_fewer_than_two_clips(self):
        pool, dur = lengths(10, 10, 10)
        groups, held, _ = g.build_groups(pool, Sizing("count", 1), dur)
        self.assertEqual([len(x) for x in groups], [2])            # 1 would close at once, but 2 is the least
        self.assertEqual(held, ["c3"])

    def test_max_groups_stops_early_and_leaves_the_rest_as_spare_not_leftover(self):
        pool, dur = lengths(*[10] * 11)
        groups, held, _ = g.build_groups(pool, Sizing("count", 4), dur, max_groups=1)
        self.assertEqual([len(x) for x in groups], [4])
        self.assertEqual(held, [])

    def test_a_shorter_last_compilation_from_the_leftover(self):
        pool, dur = lengths(*[10] * 11)
        groups, held, _ = g.build_groups(pool, Sizing("count", 4), dur, include_leftover=True)
        self.assertEqual([len(x) for x in groups], [4, 4, 3])
        self.assertEqual(held, [])

    def test_one_leftover_clip_is_not_enough_for_a_shorter_compilation(self):
        pool, dur = lengths(*[10] * 9)
        groups, held, _ = g.build_groups(pool, Sizing("count", 4), dur, include_leftover=True)
        self.assertEqual([len(x) for x in groups], [4, 4])
        self.assertEqual(held, ["c9"])

    def test_the_leftover_never_goes_past_max_groups(self):
        pool, dur = lengths(*[10] * 11)
        groups, held, _ = g.build_groups(pool, Sizing("count", 4), dur, max_groups=2, include_leftover=True)
        self.assertEqual([len(x) for x in groups], [4, 4])
        self.assertEqual(held, [])

    def test_unreadable_clips_are_left_out_and_listed(self):
        pool, dur = lengths(10, None, 10, 10, None, 10)
        groups, held, bad = g.build_groups(pool, Sizing("count", 2), dur)
        self.assertEqual(groups, [["c1", "c3"], ["c4", "c6"]])
        self.assertEqual(bad, ["c2", "c5"])

    def test_by_minutes_a_group_closes_once_it_is_long_enough(self):
        pool, dur = lengths(30, 30, 30, 30, 30, 30, 30)
        groups, held, _ = g.build_groups(pool, Sizing("minutes", 0, 60 * 1.5), dur)      # 90 seconds
        self.assertEqual([len(x) for x in groups], [3, 3])
        self.assertEqual(held, ["c7"])

    def test_by_minutes_one_long_clip_still_needs_a_partner(self):
        pool, dur = lengths(500, 5, 500)
        groups, _, _ = g.build_groups(pool, Sizing("minutes", 0, 60), dur)
        self.assertEqual(groups, [["c1", "c2"]])

    def test_durations_are_only_asked_for_as_far_as_needed(self):
        asked = []
        pool = [f"c{i}" for i in range(100)]
        g.build_groups(pool, Sizing("count", 4), lambda c: asked.append(c) or 10.0, max_groups=1)
        self.assertEqual(len(asked), 4)

    def test_an_empty_pool(self):
        self.assertEqual(g.build_groups([], Sizing("count", 4), lambda c: 1), ([], [], []))


class Ordering(unittest.TestCase):
    DATES = {"a": "2026-03-01", "b": "2026-01-01", "c": None, "d": "2026-02-01", "e": "2026-01-01"}

    def order(self, mode, **kw):
        return g.order_pool(list(self.DATES), mode, date_of=self.DATES.get, **kw)

    def test_name_keeps_the_library_order(self):
        self.assertEqual(self.order("name"), ["a", "b", "c", "d", "e"])

    def test_oldest_is_by_upload_date_equal_dates_keep_library_order_unknown_dates_go_last(self):
        self.assertEqual(self.order("oldest"), ["b", "e", "d", "a", "c"])

    def test_newest_is_by_upload_date_equal_dates_keep_library_order_unknown_dates_go_last(self):
        self.assertEqual(self.order("newest"), ["a", "d", "b", "e", "c"])

    def test_random_is_the_same_for_the_same_seed_and_different_for_another(self):
        pool = [f"c{i}" for i in range(30)]
        one = g.order_pool(pool, "random", seed=7)
        self.assertEqual(one, g.order_pool(pool, "random", seed=7))
        self.assertNotEqual(one, g.order_pool(pool, "random", seed=8))
        self.assertEqual(sorted(one), sorted(pool))

    def test_the_input_is_not_changed(self):
        pool = ["b", "a"]
        g.order_pool(pool, "random", seed=1)
        self.assertEqual(pool, ["b", "a"])


class PlayAndSizing(unittest.TestCase):
    def test_reverse_flips_the_whole_pool_and_reverse_each_flips_each_group(self):
        self.assertEqual(g.reverse_pool([1, 2, 3, 4], "reverse"), [4, 3, 2, 1])
        self.assertEqual(g.reverse_pool([1, 2, 3, 4], "asis"), [1, 2, 3, 4])
        self.assertEqual(g.play_order([[1, 2], [3, 4]], "each"), [[2, 1], [4, 3]])
        self.assertEqual(g.play_order([[1, 2], [3, 4]], "asis"), [[1, 2], [3, 4]])

    def test_sizing_comes_from_the_workspace_unless_the_request_names_a_size(self):
        cfg = {"size_mode": "count", "clips_each": 15, "minutes_each": 10}
        self.assertEqual(g.sizing_from(cfg), Sizing("count", 15, 0.0))
        self.assertEqual(g.sizing_from({**cfg, "size_mode": "minutes"}), Sizing("minutes", 0, 600.0))
        self.assertEqual(g.sizing_from(cfg, clips_each=6), Sizing("count", 6, 0.0))
        self.assertEqual(g.sizing_from(cfg, minutes_each=2.5), Sizing("minutes", 0, 150.0))

    def test_the_label(self):
        self.assertEqual(Sizing("count", 4).label(), "4 clips")
        self.assertEqual(Sizing("minutes", 0, 150.0).label(), "about 2.5 min")


if __name__ == "__main__":
    unittest.main()
