"""Unit tests for ``text.split`` — the "2줄씩" pairing rule (docs/plan/04 §2).

``LinePair`` is ``tuple[str, str | None]``; the second element is ``None`` on an
odd last pair so the final slide carries one line instead of an empty one.
"""

from __future__ import annotations

import unittest
from math import ceil

from lyrics2pptx.text import normalize_lines, split_pairs


class PairingTests(unittest.TestCase):
    def test_even_line_count_produces_exact_pairs(self) -> None:
        self.assertEqual(split_pairs(["a", "b", "c", "d"]), [("a", "b"), ("c", "d")])

    def test_odd_line_count_makes_the_last_pair_second_element_none(self) -> None:
        self.assertEqual(split_pairs(["a", "b", "c"]), [("a", "b"), ("c", None)])

    def test_single_line_becomes_one_pair_with_none(self) -> None:
        self.assertEqual(split_pairs(["only"]), [("only", None)])

    def test_empty_input_returns_empty_list(self) -> None:
        # Contract E4 (docs/arch/03 §1): splitting never raises.
        self.assertEqual(split_pairs([]), [])

    def test_return_type_is_list_of_two_tuples(self) -> None:
        pairs = split_pairs(["a", "b", "c"])
        self.assertIsInstance(pairs, list)
        for first, second in pairs:
            self.assertIsInstance(first, str)
            self.assertTrue(second is None or isinstance(second, str))
        self.assertIsNone(pairs[-1][1])

    def test_pair_count_equals_ceil_of_half_the_line_count(self) -> None:
        for line_count in range(1, 45):
            lines = [f"line{k}" for k in range(line_count)]
            self.assertEqual(len(split_pairs(lines)), ceil(line_count / 2), msg=f"{line_count} lines")


class BlankLineTests(unittest.TestCase):
    def test_blank_lines_are_removed_before_pairing_by_normalize(self) -> None:
        self.assertEqual(split_pairs(normalize_lines("a\n\n\n   \nb")), [("a", "b")])

    def test_whitespace_only_input_yields_no_pairs(self) -> None:
        self.assertEqual(split_pairs(normalize_lines("   \n\n\t")), [])

    def test_pairing_does_not_strip_caller_supplied_whitespace(self) -> None:
        # split_pairs is a pure grouper: the normalization/guard step owns
        # whitespace handling (docs/arch/03 §1 E3/E4).
        self.assertEqual(split_pairs(["  a  ", "b"]), [("  a  ", "b")])


class DedupeRepeatTests(unittest.TestCase):
    def test_dedupe_disabled_keeps_every_occurrence(self) -> None:
        lines = ["x", "x", "x", "y"]
        self.assertEqual(split_pairs(lines, dedupe_repeat=False), [("x", "x"), ("x", "y")])

    def test_dedupe_enabled_keeps_only_the_first_two_occurrences(self) -> None:
        lines = ["x", "x", "x", "y"]
        self.assertEqual(split_pairs(lines, dedupe_repeat=True), [("x", "x"), ("y", None)])

    def test_dedupe_only_affects_lines_repeated_three_times_or_more(self) -> None:
        lines = ["x", "x", "y", "y", "z"]
        self.assertEqual(
            split_pairs(lines, dedupe_repeat=True), [("x", "x"), ("y", "y"), ("z", None)]
        )


if __name__ == "__main__":
    unittest.main()
