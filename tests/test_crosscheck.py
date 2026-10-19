"""Cross-validation tests (pure; no network).

The fixtures encode the real disagreements observed on Korean worship blogs:
a one-syllable typo (``에스길``/``에스겔``), a spacing difference
(``일꾼되어``/``일꾼 되어``), reordered verses, and an unrelated search hit.
"""

from __future__ import annotations

import unittest

from lyrics2pptx.crawl.crosscheck import (
    MIN_SOURCE_COUNT,
    Transcript,
    cross_check,
    line_key,
)

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


class HonestyTests(unittest.TestCase):
    """Dropping pages is not agreeing: a one-page result is not a 합의 (ADR-020).

    Live, the vote reduced four pages for '정금같이 나오리라' to the single
    bible.com verse page and still reported "1곳 교차검증" — the label the user
    reads must distinguish the two cases.
    """

    OTHER = (
        "완전히 다른 노래입니다",
        "이 가사는 전혀 상관이 없습니다",
        "그래서 투표에서 빠져야 합니다",
        "다른 주제의 문장입니다",
    )

    def test_one_transcript_is_not_cross_checked(self) -> None:
        consensus = cross_check([Transcript("only", SONG)])
        self.assertEqual(consensus.source_count, 1)
        self.assertEqual(consensus.pages_seen, 1)
        self.assertFalse(consensus.cross_checked)
        self.assertFalse(consensus.contested)
        self.assertEqual(consensus.lines, SONG)

    def test_two_agreeing_pages_are_cross_checked(self) -> None:
        consensus = cross_check([Transcript("a", SONG), Transcript("b", SONG)])
        self.assertTrue(consensus.cross_checked)
        self.assertFalse(consensus.contested)

    def test_dropping_a_disagreeing_page_leaves_the_vote_unchecked(self) -> None:
        consensus = cross_check([Transcript("a", SONG), Transcript("b", self.OTHER)])
        self.assertEqual(consensus.source_count, 1)
        self.assertEqual(consensus.pages_seen, 2)
        self.assertFalse(consensus.cross_checked)
        self.assertTrue(consensus.contested)

    def test_the_threshold_is_the_documented_constant(self) -> None:
        self.assertEqual(MIN_SOURCE_COUNT, 2)

    def test_arrival_order_does_not_change_the_consensus(self) -> None:
        typo = tuple(
            "에스길의 환상처럼" if line == "에스겔의 환상처럼" else line for line in SONG
        )
        pages = [Transcript("a", SONG), Transcript("b", SONG), Transcript("c", typo)]
        forward = cross_check(pages)
        backward = cross_check(list(reversed(pages)))
        self.assertEqual(forward.lines, backward.lines)
        self.assertEqual(
            [entry.support for entry in forward.entries],
            [entry.support for entry in backward.entries],
        )
        self.assertEqual(forward.source_count, backward.source_count)

    def test_no_transcript_reports_no_pages_seen(self) -> None:
        consensus = cross_check([])
        self.assertEqual(consensus.pages_seen, 0)
        self.assertFalse(consensus.cross_checked)

    def test_dropped_names_the_page_that_did_not_vote(self) -> None:
        consensus = cross_check(
            [
                Transcript("a", SONG, "https://a.example/1"),
                Transcript("b", self.OTHER, "https://b.example/2"),
            ]
        )
        self.assertEqual(consensus.dropped, ("https://b.example/2",))

    def test_dropped_falls_back_to_the_source_when_there_is_no_url(self) -> None:
        consensus = cross_check([Transcript("a", SONG), Transcript("b", self.OTHER)])
        self.assertEqual(consensus.dropped, ("b",))

    def test_nothing_is_dropped_when_the_pages_agree(self) -> None:
        consensus = cross_check([Transcript("a", SONG), Transcript("b", SONG)])
        self.assertEqual(consensus.dropped, ())


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


#: The twelve lines every publisher prints for "실로암" (지니 · 마커스워십,
#: 신상근 작사·작곡). The *recording* repeats the block; the pages differ in how
#: many times they copy it out.
SILOAM = (
    "어두운 밤에 캄캄한 밤에",
    "새벽을 찾아 떠난다",
    "종이 울리고 닭이 울어도",
    "내 눈에는 오직 밤이었소",
    "우리가 처음 만난 그때는",
    "차가운 새벽이었소",
    "주님 맘속에 사랑 있음을",
    "나는 느낄 수가 있었소",
    "오 주여 당신께 감사하리라",
    "실로암 내게 주심을",
    "나에게 영원한 사랑 속에서",
    "떠나지 않게 하소서",
)

#: The block three times: lyrics.co.kr printed it that way.
REPEATED = SILOAM * 3

#: Two lyric lines to a paragraph, plus the page's own heading: the shape
#: chistopher77.com used, which was the medoid of the five live pages.
MERGED = ("📝실로 찬양 가사",) + tuple(
    " ".join(SILOAM[index : index + 2]) for index in range(0, len(SILOAM), 2)
)


class ReferenceShapeTests(unittest.TestCase):
    """The consensus follows the page that prints the song once (ADR-023).

    Live, the "실로암" vote kept the merged seven-line sheet-music page and threw
    away the four real transcriptions. That page was the medoid, and the length
    guard measured every other page against *its* average line length (26.2
    against 11.5-12.2: ratios 0.44-0.46), so pages whose content similarity to it
    was 0.34-0.92 were dropped. The deck was four slides led by the page's own
    heading ``📝실로 찬양 가사``.
    """

    def test_a_paragraph_merging_page_does_not_anchor_the_vote(self) -> None:
        consensus = cross_check(
            [
                Transcript("sheet", MERGED, "https://sheet.example/1"),
                Transcript("repeated", REPEATED),
                Transcript("once", SILOAM),
            ]
        )
        self.assertEqual(consensus.lines, SILOAM)
        self.assertEqual(consensus.source_count, 2)
        self.assertEqual(consensus.pages_seen, 3)
        self.assertIn("https://sheet.example/1", consensus.dropped)
        self.assertNotIn("📝실로 찬양 가사", consensus.lines)

    def test_the_reference_prints_the_song_once(self) -> None:
        consensus = cross_check([Transcript("a", REPEATED), Transcript("b", SILOAM)])
        self.assertEqual(consensus.lines, SILOAM)
        self.assertTrue(consensus.cross_checked)

    def test_a_chorus_only_fragment_does_not_anchor(self) -> None:
        consensus = cross_check(
            [
                Transcript("fragment", SILOAM[8:]),
                Transcript("once", SILOAM),
                Transcript("twice", SILOAM * 2),
            ]
        )
        self.assertEqual(consensus.lines, SILOAM)

    def test_a_hard_wrapped_lyric_line_does_not_win_the_shape(self) -> None:
        """Report §12.4: one site split a lyric line in two with ``<br>``.

        The complete first line is then carried by the two other pages only, so
        the reference must not be the page that spells the song in more lines —
        that is how the live vote moved the majority's complete line to the end
        of the deck as a stray "extra".
        """
        split = ("어두운 밤에", "캄캄한 밤에") + SILOAM[1:]
        spaced = tuple(
            "주님 맘 속에 사랑 있음을" if line == "주님 맘속에 사랑 있음을" else line
            for line in SILOAM
        )
        consensus = cross_check(
            [
                Transcript("split", split),
                Transcript("once", SILOAM),
                Transcript("other", spaced),
            ]
        )
        self.assertEqual(consensus.lines, SILOAM)
        self.assertEqual(consensus.source_count, 3)


class SamePositionTests(unittest.TestCase):
    """A position written differently is a variant, not a stray line (ADR-024).

    Live, two of the four "실로암" pages sang the seventh position with other
    words ("당신 눈 속에 여명 있음을" for "주님 맘속에 사랑 있음을"). That wording
    scored below :data:`MATCH_RATIO` against the reference, so the vote treated
    it as text the reference lacks and appended it as a thirteenth line — a
    whole slide holding one line no publisher prints. It is the same position's
    other rendering: the deck keeps the wording the pages agree on and reports
    the rest in that position's ``variants``, where the user already looks for
    ``에스길``/``에스겔``.
    """

    def test_a_differently_worded_position_is_reported_not_appended(self) -> None:
        other = tuple(
            "당신 눈 속에 여명 있음을" if line == "주님 맘속에 사랑 있음을" else line
            for line in SILOAM
        )
        other_spaced = tuple(
            "당신 눈 속에 여명있음을" if line == "당신 눈 속에 여명 있음을" else line
            for line in other
        )
        spaced = tuple(
            "주님 맘 속에 사랑 있음을" if line == "주님 맘속에 사랑 있음을" else line
            for line in SILOAM
        )
        consensus = cross_check(
            [
                Transcript("once", SILOAM),
                Transcript("spaced", spaced),
                Transcript("other", other),
                Transcript("other2", other_spaced),
            ]
        )
        self.assertEqual(consensus.lines, SILOAM)
        entry = _entry(consensus, "주님 맘속에 사랑 있음을")
        self.assertEqual((entry.support, entry.total), (2, 4))
        self.assertIn("주님 맘 속에 사랑 있음을", entry.variants)
        self.assertIn("당신 눈 속에 여명 있음을", entry.variants)
        self.assertNotIn("당신 눈 속에 여명 있음을", consensus.lines)

    def test_a_line_inserted_between_neighbours_stays_an_extra(self) -> None:
        """A bridge leaves no open position, so it is still an extra verse."""
        bridge = "우리 함께 주를 찬양하네"
        padded = SILOAM[:3] + (bridge,) + SILOAM[3:]
        padded_spaced = tuple(
            "종이울리고 닭이 울어도" if line == "종이 울리고 닭이 울어도" else line
            for line in padded
        )
        consensus = cross_check(
            [
                Transcript("once", SILOAM),
                Transcript("twice", SILOAM * 2),
                Transcript("padded", padded),
                Transcript("padded2", padded_spaced),
            ]
        )
        self.assertEqual(consensus.lines, SILOAM + (bridge,))
        self.assertEqual(_entry(consensus, bridge).support, 2)


class WrappingTests(unittest.TestCase):
    """One position on one line or on two: a wrapping difference (ADR-029).

    Live, the deck for "물이 바다 덮음같이" carried "여호와의 영광을" /
    "인정하는 것이" *and* the joined phrase the other pages print, so the same
    words ran on three lines and the deck disagreed with the vote it reported
    (§16.4). Two frames of reference are possible (either side can be the
    anchor), so both directions are pinned here.
    """

    SPLIT = ("여호와의 영광을", "인정하는 것이", "땅에 가득하리라", "물이 바다 덮음같이")
    JOINED = ("여호와의 영광을 인정하는 것이", "땅에 가득하리라", "물이 바다 덮음같이")
    PHRASE = "여호와의 영광을 인정하는 것이"

    def test_the_joined_reading_replaces_the_pair_when_pages_prefer_it(self) -> None:
        """Three pages join the phrase, the anchor splits it: keep the phrase."""
        chorus = self.JOINED[-2:]
        consensus = cross_check(
            [
                Transcript("split", self.SPLIT),
                Transcript("joined1", self.JOINED + chorus),
                Transcript("joined2", self.JOINED + chorus),
                Transcript("joined3", self.JOINED + chorus),
            ]
        )
        entry = _entry(consensus, self.PHRASE)
        self.assertEqual(entry.support, 3)
        self.assertEqual(entry.variants, ("여호와의 영광을", "인정하는 것이"))
        self.assertNotIn("여호와의 영광을", consensus.lines)

    def test_a_joined_line_is_a_variant_when_the_pages_split(self) -> None:
        """The anchor and the majority split the phrase: keep the two lines."""
        chorus = self.SPLIT[-2:]
        consensus = cross_check(
            [
                Transcript("split1", self.SPLIT),
                Transcript("split2", self.SPLIT),
                Transcript("split3", self.SPLIT),
                Transcript("joined1", self.JOINED + chorus),
                Transcript("joined2", self.JOINED + chorus),
            ]
        )
        entry = _entry(consensus, "여호와의 영광을")
        self.assertEqual(entry.support, 3)
        self.assertIn(self.PHRASE, entry.variants)
        self.assertNotIn(self.PHRASE, consensus.lines)

    SHORT = ("주 나의 모습 보네", "상한 나의 맘 보시네", "주 나의 눈물 아네", "홀로 울던 맘 아시네")
    SHORT_JOINED = (
        "주 나의 모습 보네 상한 나의 맘 보시네",
        "주 나의 눈물 아네 홀로 울던 맘 아시네",
    )

    def test_a_joined_block_in_the_anchor_loses_to_the_pair(self) -> None:
        """The anchor prints a joined summary *and* the split verses (01, 04).

        「주 은혜임을」 led with "주 나의 모습 보네 상한 나의 맘 보시네" and then
        sang "주 나의 모습 보네" / "상한 나의 맘 보시네" — the same words twice.
        The pair is what the pages agree on, so the joined reading becomes the
        first fragment's variant (ADR-029).
        """
        chorus = self.SHORT + self.SHORT  # the other pages repeat the chorus
        consensus = cross_check(
            [
                Transcript("summary", self.SHORT_JOINED + self.SHORT),
                Transcript("plain1", chorus),
                Transcript("plain2", chorus),
            ]
        )
        self.assertEqual(consensus.lines, self.SHORT)
        entry = _entry(consensus, "주 나의 모습 보네")
        self.assertEqual(entry.support, 3)
        self.assertEqual(entry.variants, ("주 나의 모습 보네 상한 나의 맘 보시네",))

    def test_the_joined_variant_is_reported_once_when_the_anchor_repeats_it(self) -> None:
        chorus = self.SHORT * 3
        consensus = cross_check(
            [
                Transcript("summary", self.SHORT_JOINED + self.SHORT_JOINED + self.SHORT),
                Transcript("plain1", chorus),
                Transcript("plain2", chorus),
            ]
        )
        self.assertEqual(consensus.lines, self.SHORT)
        self.assertEqual(
            _entry(consensus, "주 나의 모습 보네").variants,
            ("주 나의 모습 보네 상한 나의 맘 보시네",),
        )

    APART_SPLIT = ("주님은 나의 산성", "아멘 아멘", "주의 이름 높여", "주님은 나의 요새")
    APART_JOINED = "주님은 나의 산성 주님은 나의 요새"
    APART_EXTRA = ("높이 계신 주께", "영원히 찬양해")

    def test_a_joined_line_is_found_even_when_its_parts_sit_apart(self) -> None:
        """「아무것도 두려워 말라」 joined a verse line with the chorus after it.

        The joined line is not always made of *neighbours*: here "주님은 나의
        산성" and "주님은 나의 요새" are separated by two other lines, and the
        anchor still wrote them as one. The joined line loses (the pair is what
        the pages agree on) and is reported as the first part's variant.
        """
        anchor = (
            self.APART_SPLIT[0],
            self.APART_SPLIT[1],
            self.APART_JOINED,
            self.APART_SPLIT[2],
            self.APART_SPLIT[3],
        )
        pages = (
            Transcript("plain1", self.APART_SPLIT + self.APART_EXTRA),
            Transcript("plain2", self.APART_SPLIT + self.APART_EXTRA),
        )
        consensus = cross_check([Transcript("anchor", anchor), *pages])
        self.assertNotIn(self.APART_JOINED, consensus.lines)
        self.assertEqual(_entry(consensus, "주님은 나의 산성").support, 3)
        self.assertIn(self.APART_JOINED, _entry(consensus, "주님은 나의 산성").variants)

    def test_a_joined_reading_the_pages_agree_on_replaces_the_pair(self) -> None:
        """Both directions are possible: here the pages prefer the joined line.

        The joined line wins and the split pair becomes its variants — leaving
        both would print the same words twice.
        """
        tail = ("아멘 아멘", "주의 이름 높여", "높이 계신 주께", "영원히 찬양해")
        split = ("주님은 나의 산성", "주님은 나의 요새") + tail
        joined = (self.APART_JOINED,) + tail
        consensus = cross_check(
            [
                Transcript("joined1", joined),
                Transcript("joined2", joined),
                Transcript("joined3", joined),
                Transcript("split1", split),
                Transcript("split2", split),
            ]
        )
        entry = _entry(consensus, self.APART_JOINED)
        self.assertEqual(entry.support, 3)
        self.assertEqual(entry.variants, ("주님은 나의 산성", "주님은 나의 요새"))
        self.assertNotIn("주님은 나의 산성", consensus.lines)
        self.assertNotIn("주님은 나의 요새", consensus.lines)

    def test_one_page_that_joins_everything_promotes_nothing(self) -> None:
        """A single page is never enough, however long its lines are."""
        spaced = tuple(
            "땅에 가득 하리라" if line == "땅에 가득하리라" else line
            for line in self.SPLIT
        )
        consensus = cross_check(
            [
                Transcript("once", self.SPLIT),
                Transcript("spaced", spaced),
                Transcript("merger", (self.SPLIT[0] + " " + self.SPLIT[1] + " 땅에 가득하리라", self.SPLIT[-1])),
            ]
        )
        self.assertEqual(consensus.lines, self.SPLIT)
        for entry in consensus.entries:
            self.assertNotIn("여호와의 영광을 인정하는 것이 땅에 가득하리라", entry.variants)


class SplitFragmentTests(unittest.TestCase):
    """A page that splits one position in two prints no new verse (ADR-031).

    「주 하나님 지으신 모든 세계」's anchor joined the hymn's first two rows
    ("주 하나님 지으신 모든 세계 내 마음 속에 그리어볼 때") the way four pages did,
    and a fifth printed the two halves alone. The half arrived as an extra verse
    and the deck ended with a sixth slide that repeated the first half of slide 1
    (조사보고서 §17.6).
    """

    ANCHOR = (
        "주 하나님 지으신 모든 세계 내 마음 속에 그리어볼 때",
        "하늘의 별 울려퍼지는 뇌성 주님의 권능 우주에 찼네",
        "주님의 높고 위대하심을 내 영혼이 찬양하네",
        "나 겸손히 엎드려 경배하며 영원히 주를 찬양하리라",
    )
    # The hymnal's own shape: one row per pair of the publisher's lines.
    SPLIT = (
        "주 하나님 지으신 모든 세계",
        "내 마음 속에 그리어볼 때",
        "하늘의 별 울려퍼지는 뇌성",
        "주님의 권능 우주에 찼네",
        "주님의 높고 위대하심을 내 영혼이 찬양하네",
        "나 겸손히 엎드려 경배하며 영원히 주를 찬양하리라",
    )

    def test_a_split_half_does_not_become_a_last_slide(self) -> None:
        consensus = cross_check(
            [
                Transcript("anchor1", self.ANCHOR),
                Transcript("anchor2", self.ANCHOR),
                Transcript("anchor3", self.ANCHOR),
                Transcript("split1", self.SPLIT),
                Transcript("split2", self.SPLIT),
            ]
        )
        self.assertEqual(consensus.lines, self.ANCHOR)
        entry = _entry(consensus, self.ANCHOR[0])
        self.assertEqual(entry.support, 3)
        self.assertEqual(
            entry.variants,
            ("주 하나님 지으신 모든 세계", "내 마음 속에 그리어볼 때"),
        )
        self.assertEqual(
            _entry(consensus, self.ANCHOR[1]).variants,
            ("하늘의 별 울려퍼지는 뇌성", "주님의 권능 우주에 찼네"),
        )


class AnchorEdgeChromeTests(unittest.TestCase):
    """The anchor's own trailing furniture is trimmed even when extras follow.

    A blog prints its own name (그리심산) in the widget right below the article, so
    한나의 노래's anchor ended with it — and two more verses arrived from the other
    pages, which print the song twice. Appending those after the name made it an
    interior line and the edge trim no longer reached it, so the deck carried the
    blog's own name as slide 6 (조사보고서 §17.6, 08).
    """

    LYRICS = (
        "어제와 다른 괴롬과 슬픔",
        "깊은 탄식에 기도할 수 없고",
        "처연한 내 안의 속사람은",
        "주를 보지만 말하지 못하네",
        "주의 사랑이 목마름 채우고",
        "주의 위로가 눈물을 닦으니",
    )
    EXTRAS = ("주님의 은혜가 필요한 이 땅에서", "이곳에서 주와 함께 노래하리라")

    def test_the_anchor_tail_furniture_is_dropped_when_extras_follow(self) -> None:
        repeated = self.LYRICS + self.EXTRAS + self.LYRICS
        consensus = cross_check(
            [
                Transcript("anchor", self.LYRICS + ("그리심산",)),
                Transcript("plain1", repeated),
                Transcript("plain2", repeated),
                Transcript("plain3", repeated),
            ]
        )
        self.assertEqual(consensus.lines, self.LYRICS + self.EXTRAS)
        self.assertNotIn("그리심산", consensus.lines)

    def test_an_interior_single_page_line_is_still_kept(self) -> None:
        """Interior one-off lines are a genuine extra verse, not furniture (ADR-020)."""
        doubled = self.LYRICS * 2
        consensus = cross_check(
            [
                Transcript("anchor", self.LYRICS[:2] + ("오직 주의 은혜",) + self.LYRICS[2:]),
                Transcript("plain1", doubled),
                Transcript("plain2", doubled),
            ]
        )
        self.assertIn("오직 주의 은혜", consensus.lines)

