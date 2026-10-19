"""Unit tests for ``text.normalize`` — pipeline steps N1..N10 (docs/plan/04 §1).

All expectations are hard-coded strings so a step reordering or a regex change
fails here rather than in production. No network, no files other than the
golden fixture ``tests/fixtures/lyrics_sample.txt``.
"""

from __future__ import annotations

import unicodedata
import unittest
from pathlib import Path

from lyrics2pptx.text.normalize import normalize_lines

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# docs/plan/04 §1 "대표 예시" — the golden result for lyrics_sample.txt.
GOLDEN = [
    "너는 담장 너머로",
    "붉은 나무가지에 푸른 열매처럼",
    "우리 함께 손잡고 가세",
    "조금 느리고 어려울지라도",
]


class NewlineAndUnicodeTests(unittest.TestCase):
    def test_n1_crlf_and_lone_cr_become_lf(self) -> None:
        self.assertEqual(normalize_lines("a\r\nb\rc"), ["a", "b", "c"])

    def test_n2_decomposed_hangul_is_composed_to_nfc(self) -> None:
        decomposed = "\u1100\u1161\ub098"  # ᄀ + ᅡ + 나
        result = normalize_lines(decomposed)
        self.assertEqual(result, ["가나"])
        self.assertTrue(unicodedata.is_normalized("NFC", result[0]))

    def test_n3_zero_width_and_nbsp_are_removed(self) -> None:
        self.assertEqual(
            normalize_lines("\u200b\ubd89\uc740\u00a0\ub098\ubb34\ufeff"),
            ["\ubd89\uc740 \ub098\ubb34"],
        )
        # ZWNJ/ZWNJ spanning three syllables must leave all three behind.
        self.assertEqual(
            normalize_lines("\uac00\u200c\ub098\u200d\ub2e4"),
            ["\uac00\ub098\ub2e4"],
        )

    def test_n3_nbsp_becomes_a_regular_space_not_a_separator(self) -> None:
        self.assertEqual(normalize_lines("a\u00a0b"), ["a b"])


class WhitespaceTests(unittest.TestCase):
    def test_n5_collapses_repeated_spaces_and_trims_ends(self) -> None:
        self.assertEqual(normalize_lines("  붉은  나무가지에   푸른  "), ["붉은 나무가지에 푸른"])

    def test_n5_tabs_collapse_to_a_single_space(self) -> None:
        self.assertEqual(normalize_lines("a\t\tb\tc"), ["a b c"])


class SectionMarkerTests(unittest.TestCase):
    def test_n6_marker_only_lines_are_removed(self) -> None:
        text = "[Verse 1]\n실제 가사 줄\n[Chorus]\n(x2)\n(×2)\n※\n[후렴]\n[Bridge]"
        self.assertEqual(normalize_lines(text), ["실제 가사 줄"])

    def test_n6_long_line_that_contains_a_keyword_is_kept(self) -> None:
        line = "그대를 절대 잊지 못할 것입니다 정말로"
        self.assertEqual(normalize_lines(line), [line])

    def test_n6_short_lyric_line_with_keyword_substring_is_kept(self) -> None:
        """N6 removes *marker-only* lines (docs/plan/04 §1 N6 note: "실제 가사
        줄은 길이가 있으므로 보존된다", "확신이 없으면 지우지 않는 쪽으로 실패").

        "내 절망마저" is a real lyric line whose only relation to the marker list
        is the substring "절" inside "절망". Substring matching on the whole line
        therefore must not drop it.
        """
        line = "내 절망마저"
        self.assertEqual(normalize_lines(line), [line])


class BlankLineTests(unittest.TestCase):
    def test_n7_blank_and_whitespace_only_lines_are_dropped(self) -> None:
        self.assertEqual(normalize_lines("a\n\n\n\n   \n\t\n b "), ["a", "b"])

    def test_n7_three_or_more_consecutive_newlines_do_not_produce_empty_lines(self) -> None:
        self.assertEqual(normalize_lines("a\n\n\nb"), ["a", "b"])


class DuplicateRunTests(unittest.TestCase):
    def test_n8_run_of_three_identical_lines_collapses_to_one(self) -> None:
        self.assertEqual(normalize_lines("same\nsame\nsame\nother"), ["same", "other"])

    def test_n8_run_of_two_identical_lines_is_preserved(self) -> None:
        self.assertEqual(normalize_lines("same\nsame\nother"), ["same", "same", "other"])

    def test_n8_blank_lines_between_repeats_do_not_reset_the_run(self) -> None:
        # N7 removes the blank lines *before* N8 runs, so the three survivors
        # become adjacent and collapse to one.
        self.assertEqual(normalize_lines("same\n\nsame\n\nsame"), ["same"])


class TrailingMarkerTests(unittest.TestCase):
    def test_n9_trailing_pipe_dash_and_tilde_runs_are_stripped(self) -> None:
        self.assertEqual(
            normalize_lines("가사 한 줄 |\n두 번째 줄~~~\n세 번째 -"),
            ["가사 한 줄", "두 번째 줄", "세 번째"],
        )

    def test_n9_marker_only_line_disappears_after_stripping(self) -> None:
        self.assertEqual(normalize_lines("a\n-----\nb"), ["a", "b"])


class AdLibTests(unittest.TestCase):
    """N11: 성능 추임새는 가사가 아니다 (ADR-022).

    lrclib's 「아주 먼 옛날」 starts with its own ad-lib line and hangs more of
    them off real lines: a whole slide was ``(Yeah, woah, yeah)``, and
    ``(Woah)``/``(Ooh)`` were printed on the slides beside the words. Removing
    them also repairs the two-line pairing, because the ad-lib line no longer
    eats one of the two slots.
    """

    def test_a_whole_line_of_ad_lib_is_dropped(self) -> None:
        text = "아주 먼 옛날 하늘에서는\n(Yeah, woah, yeah)\n당신을 향한 계획있었죠"
        self.assertEqual(
            normalize_lines(text), ["아주 먼 옛날 하늘에서는", "당신을 향한 계획있었죠"]
        )

    def test_an_inline_ad_lib_is_removed_from_the_line(self) -> None:
        self.assertEqual(
            normalize_lines("나의 손으로 창조하였노라 (Woah)"), ["나의 손으로 창조하였노라"]
        )
        self.assertEqual(normalize_lines("사랑을 드려요 (Ooh)"), ["사랑을 드려요"])

    def test_a_trailing_interjection_after_hangul_is_removed(self) -> None:
        self.assertEqual(
            normalize_lines("하나님께서 바라보시며 oh"), ["하나님께서 바라보시며"]
        )

    def test_an_english_line_is_never_touched(self) -> None:
        for line in ("Oh yeah", "Oh yeah, hold me now", "Na na na na na na"):
            with self.subTest(line=line):
                self.assertEqual(normalize_lines(line), [line])

    def test_a_bracket_of_real_words_survives(self) -> None:
        for line in ("사랑해요 (사랑해요 영원히)", "귀하게 (hold me now)", "주께 (주여)"):
            with self.subTest(line=line):
                self.assertEqual(normalize_lines(line), [line])

    def test_an_empty_bracket_is_decoration(self) -> None:
        self.assertEqual(normalize_lines("나의 가는 길 (...)"), ["나의 가는 길"])
        self.assertEqual(normalize_lines("(...)"), [])
        self.assertEqual(normalize_lines("나는 ( )"), ["나는"])

    def test_a_marker_is_still_a_marker_not_an_ad_lib(self) -> None:
        # N6 owns these; N11 must not need to see them.
        self.assertEqual(normalize_lines("(x2)\n사랑해요"), ["사랑해요"])

    def test_ad_lib_removal_keeps_the_line_count_for_pairing(self) -> None:
        text = "(Yeah, woah, yeah)\n아주 먼 옛날 하늘에서는\n당신을 향한 계획있었죠"
        self.assertEqual(
            normalize_lines(text), ["아주 먼 옛날 하늘에서는", "당신을 향한 계획있었죠"]
        )


class GoldenAndContractTests(unittest.TestCase):
    def test_golden_fixture_matches_documented_four_lines(self) -> None:
        raw = (FIXTURES / "lyrics_sample.txt").read_text(encoding="utf-8")
        self.assertEqual(normalize_lines(raw), GOLDEN)

    def test_golden_fixture_with_crlf_line_endings_yields_the_same_result(self) -> None:
        raw = (FIXTURES / "lyrics_sample.txt").read_text(encoding="utf-8")
        self.assertEqual(normalize_lines(raw.replace("\n", "\r\n")), GOLDEN)

    def test_empty_input_returns_empty_list_without_raising(self) -> None:
        # Contract E3 (docs/arch/03 §1): normalization never raises.
        self.assertEqual(normalize_lines(""), [])
        self.assertEqual(normalize_lines("   \n\n\t "), [])

    def test_all_markers_only_input_returns_empty_list(self) -> None:
        self.assertEqual(normalize_lines("[Verse]\n[Chorus]\n(x2)"), [])

    def test_line_order_is_preserved(self) -> None:
        self.assertEqual(normalize_lines("3\n1\n2"), ["3", "1", "2"])


class SectionMarkerSuffixRegressionTests(unittest.TestCase):
    """M2 regression: the N6 suffix must be number-centred, not open-ended.

    ``_MARKER_SUFFIX_RE`` used to accept any ASCII suffix, so a real lyric line
    that merely *started* with a keyword ("chorus line") was silently dropped —
    a documented fail-safe violation. These cases pin the narrowed rule.
    """

    def test_real_lyric_lines_starting_with_a_keyword_word_are_kept(self) -> None:
        for line in ("chorus line", "intro to me", "outro music"):
            with self.subTest(line=line):
                self.assertEqual(normalize_lines(line), [line])

    def test_the_same_lines_survive_inside_a_larger_block(self) -> None:
        text = "chorus line\nintro to me\noutro music"
        self.assertEqual(normalize_lines(text), ["chorus line", "intro to me", "outro music"])

    def test_keyword_prefixed_hangul_lyric_lines_are_kept(self) -> None:
        for line in ("내 절망마저", "그 절에서", "이 간주곡"):
            with self.subTest(line=line):
                self.assertEqual(normalize_lines(line), [line])

    def test_marker_only_forms_are_still_removed(self) -> None:
        for line in ("Verse", "verse 1", "[Chorus]", "(후렴)", "후렴 x2", "※", "×2", "간주"):
            with self.subTest(line=line):
                self.assertEqual(normalize_lines(line), [], f"{line!r} must be removed")

    def test_number_centred_suffix_variants_are_removed(self) -> None:
        for line in ("verse2", "chorus:", "후렴 2절", "[Verse 1]", "(x2)", "(×2)"):
            with self.subTest(line=line):
                self.assertEqual(normalize_lines(line), [], f"{line!r} must be removed")

    def test_a_block_of_markers_and_lyrics_keeps_only_the_lyrics(self) -> None:
        text = "[Verse 1]\nchorus line\n[Chorus]\nintro to me\n×2\noutro music"
        self.assertEqual(normalize_lines(text), ["chorus line", "intro to me", "outro music"])


if __name__ == "__main__":
    unittest.main()
