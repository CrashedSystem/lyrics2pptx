"""Cross-validation tests (pure; no network).

The fixtures encode the real disagreements observed on Korean worship blogs:
a one-syllable typo (``에스길``/``에스겔``), a spacing difference
(``일꾼되어``/``일꾼 되어``), reordered verses, and an unrelated search hit.
"""

from __future__ import annotations

import unittest

from lyrics2pptx.crawl.crosscheck import Transcript, cross_check, line_key

SONG = (
    "주 말씀이 선포되고",
    "또 주의 종 모세의 때와 같이",
    "언약이 성취되네",
    "에스겔의 환상처럼",
    "마른 뼈가 살아나며",
    "예배가 회복되네",
)


def _entry(consensus, text: str):
    for item in consensus.entries:
        if item.text == text:
            return item
    raise AssertionError(f"missing line: {text!r} in {consensus.lines!r}")


class KeyTests(unittest.TestCase):
    def test_spacing_and_punctuation_are_ignored(self) -> None:
        self.assertEqual(line_key("환난 날이, 다가와도!"), line_key("환난날이 다가와도"))

    def test_different_syllables_differ(self) -> None:
        self.assertNotEqual(line_key("에스길의 환상처럼"), line_key("에스겔의 환상처럼"))


class VoteTests(unittest.TestCase):
    def test_majority_corrects_a_single_source_typo(self) -> None:
        typo = tuple(
            "에스길의 환상처럼" if line == "에스겔의 환상처럼" else line for line in SONG
        )
        consensus = cross_check(
            [
                Transcript("a", SONG),
                Transcript("b", SONG),
                Transcript("c", typo),
            ]
        )
        self.assertEqual(consensus.source_count, 3)
        entry = _entry(consensus, "에스겔의 환상처럼")
        self.assertEqual(entry.support, 2)
        self.assertEqual(entry.total, 3)
        self.assertIn("에스길의 환상처럼", entry.variants)

    def test_spacing_only_difference_is_reported_but_not_a_lost_vote(self) -> None:
        tight = ("우리는 추수할 일꾼되어", "주 말씀을 선포하리")
        loose = ("우리는 추수할 일꾼 되어", "주 말씀을 선포하리")
        consensus = cross_check([Transcript("a", tight), Transcript("b", loose)])
        entry = _entry(consensus, "우리는 추수할 일꾼되어")
        self.assertEqual(entry.support, 2)
        self.assertIn("우리는 추수할 일꾼 되어", entry.variants)

    def test_reordered_verses_still_vote_line_by_line(self) -> None:
        first = SONG[:3]
        second = SONG[3:]
        swapped = second + first
        consensus = cross_check(
            [
                Transcript("a", SONG),
                Transcript("b", swapped),
                Transcript("c", SONG),
            ]
        )
        self.assertEqual(consensus.source_count, 3)
        for line in SONG:
            self.assertEqual(_entry(consensus, line).support, 3)

    def test_unrelated_page_is_dropped_before_voting(self) -> None:
        other = (
            "완전히 다른 노래입니다",
            "이 가사는 전혀 상관이 없습니다",
            "그래서 투표에서 빠져야 합니다",
            "다른 주제의 문장입니다",
        )
        consensus = cross_check(
            [Transcript("a", SONG), Transcript("b", SONG), Transcript("c", other)]
        )
        self.assertEqual(consensus.source_count, 2)
        for line in other:
            self.assertNotIn(line, consensus.lines)

    def test_single_transcript_is_returned_unchanged(self) -> None:
        consensus = cross_check([Transcript("only", SONG)])
        self.assertEqual(consensus.source_count, 1)
        self.assertEqual(consensus.lines, SONG)

    def test_no_transcript_yields_an_empty_consensus(self) -> None:
        consensus = cross_check([])
        self.assertEqual(consensus.lines, ())
        self.assertEqual(consensus.source_count, 0)

    def test_blank_and_invisible_lines_never_vote(self) -> None:
        padded = Transcript("a", SONG + ("", "  ", "\u200b"))
        consensus = cross_check([padded, Transcript("b", SONG)])
        self.assertEqual(consensus.source_count, 2)
        self.assertEqual(consensus.lines, SONG)


class EvidenceTests(unittest.TestCase):
    def test_disagreements_lists_only_disputed_positions(self) -> None:
        typo = tuple(
            "에스길의 환상처럼" if line == "에스겔의 환상처럼" else line for line in SONG
        )
        consensus = cross_check([Transcript("a", SONG), Transcript("b", SONG), Transcript("c", typo)])
        self.assertEqual(len(consensus.disagreements), 1)
        self.assertAlmostEqual(_entry(consensus, "에스겔의 환상처럼").agreement, 2 / 3)

    def test_line_seen_by_a_single_page_is_not_added_as_an_extra(self) -> None:
        padded = Transcript("c", SONG + ("이 줄은 한 곳에만 있습니다",))
        consensus = cross_check([Transcript("a", SONG), Transcript("b", SONG), padded])
        self.assertNotIn("이 줄은 한 곳에만 있습니다", consensus.lines)
