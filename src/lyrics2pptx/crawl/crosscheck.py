"""Cross-validation of independent lyric transcriptions (L2, pure).

ADR-018 records the reason this module exists: no authoritative public feed
carries Korean worship/CCM lyrics, and a single blog transcription can carry
typos ("에스길" for "에스겔", "환란" for "환난"). When the same song is
transcribed by several *independent* pages, voting line by line removes most
one-off typos and surfaces the remaining disagreements so a human can confirm
them.

The module is pure: it takes plain strings, performs no I/O and no network, and
is deterministic. It lives in the crawl layer because only the crawl adapter
consumes it, and crawl must not import :mod:`lyrics2pptx.text` (rule P4); lines
are already trimmed and separated by the extractor, so only whitespace and
zero-width clean-up is repeated here.

Why not sequence alignment?
---------------------------
Real transcriptions differ in *verse order* (some blogs put the chorus before
the second verse), repeat the chorus a different number of times, and join two
lyric lines into one paragraph. A global alignment then pairs unrelated lines
and reports the wrong "variant". This module instead matches each reference
line independently against every other transcript:

* exact match on the normalized key (alphanumeric, case-folded) wins;
* otherwise the most similar line above ``MATCH_RATIO`` is accepted, which is
  what makes "에스길"↔"에스겔" and "환난날이"↔"환란 날이" comparable;
* order is irrelevant, so reordered verses still vote correctly.

The reference is the transcript that prints the song once, wrapped the way the
majority wraps it (see :func:`_reference_index`), so the consensus keeps the
line shape most publishers use instead of one page's layout. A transcript that
is not the same song (an unrelated search hit, or a page whose "lyrics" are an
image and whose menu was scraped) is dropped before voting.

A line the reference does not have is not always an extra verse: pages
transcribe the same position with different words ("주님 맘속에 사랑 있음을"
against "당신 눈 속에 여명 있음을" in 실로암). Such a line is found by looking at
where it sits between the positions its own page *did* fill — one position left
open there means the page rendered that position differently, so the wording is
reported as a variant of it instead of being appended as a stray last line
(ADR-024).

Dropping pages is not the same thing as agreement, and the two are easy to
confuse: when it kept only the medoid the vote still reported a "합의" — which is
how a bible.com verse page became the lyrics for "정금같이 나오리라" (ADR-020).
The result is still returned when fewer than :data:`MIN_SOURCE_COUNT`
transcriptions survive (the user needs something to look at), but
:attr:`Consensus.cross_checked` is then False and every caller must label it
"교차검증 안 됨" rather than "합의".
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Sequence

__all__ = [
    "Transcript",
    "LineConsensus",
    "Consensus",
    "MIN_SOURCE_COUNT",
    "line_key",
    "cross_check",
]

#: Minimum number of *independent* transcriptions that must take part before the
#: result may be called a cross-check. One page is one person's typing: calling
#: it "합의" is the lie that made a Bible-verse page the lyrics for
#: "정금같이 나오리라" (ADR-020).
MIN_SOURCE_COUNT = 2

#: Minimum character similarity for two *different* lines to count as the same
#: lyric position. One substitution in an eight-syllable line scores ~0.875;
#: unrelated lines score far below this.
MATCH_RATIO = 0.75

_SHINGLE = 5

#: How far a page's average line length may sit from the majority's (the median
#: across the offered transcriptions) before it is a different *shape*: a page
#: that merges lyric lines into paragraphs, or breaks them one word per line,
#: writes positions nobody else votes for.
_LENGTH_WINDOW = (0.6, 1.7)

#: The reference must carry at least this share of the richest page's text
#: (character shingles) before it may define the shape of the deck: a
#: chorus-only fragment must not become the lyrics.
_MIN_COVERAGE = 0.5

# Zero-width characters and exotic spaces would otherwise split a key that is
# the same word (a mirror that HTML-escaped a soft hyphen, say).
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\ufeff\u0000-\u0008\u000b\u000c\u000e-\u001f]")
_SPACE_RE = re.compile(r"[ \t\u00a0]+")


def _clean_lines(lines: Iterable[str]) -> tuple[str, ...]:
    """Drop empty lines and normalise whitespace before the vote.

    The extractor already removed section markers and page chrome; this only
    guards the public entry point against blank or invisible-only lines, which
    would otherwise create phantom positions every transcript disagrees on.
    """
    cleaned: list[str] = []
    for raw in lines:
        line = _SPACE_RE.sub(" ", _INVISIBLE_RE.sub("", raw)).strip()
        if line:
            cleaned.append(line)
    return tuple(cleaned)


def line_key(line: str) -> str:
    """Return the comparison key of ``line`` (alphanumeric, case-folded).

    Korean syllables, Latin letters and digits survive; spaces, punctuation and
    zero-width characters are dropped, so ``"환난 날이"`` and ``"환난날이"``
    compare equal while ``"환란"`` does not.
    """
    return "".join(ch for ch in line if ch.isalnum()).lower()


@dataclass(frozen=True, slots=True)
class Transcript:
    """One page's transcription of the song.

    ``source`` is a short, stable label (a host name); ``url`` is optional and
    only used for reporting.
    """

    source: str
    lines: tuple[str, ...]
    url: str | None = None


@dataclass(frozen=True, slots=True)
class LineConsensus:
    """The vote result for one lyric position."""

    text: str
    support: int
    total: int
    variants: tuple[str, ...] = ()

    @property
    def agreement(self) -> float:
        """Fraction of transcripts that carried the winning rendering."""
        return self.support / self.total if self.total else 0.0

    @property
    def disputed(self) -> bool:
        """True when at least one transcript rendered this position differently."""
        return bool(self.variants)


@dataclass(frozen=True, slots=True)
class Consensus:
    """A voted lyric plus the per-position evidence that produced it.

    ``source_count`` is how many transcriptions actually voted; ``pages_seen`` is
    how many were offered. They differ when the consistency filter dropped pages
    that disagree (a different song, a list page), which is exactly the case a
    caller must not present as agreement (ADR-020).

    ``dropped`` names those filtered-out pages (``url`` when it has one, else
    ``source``). Without it a caller can only offer the pages as if they had
    voted: clicking the tistory page whose "lyrics" were the sidebar notice
    ``메일로 (0) / 요청해도 (0) ...`` produced eleven lines of page furniture
    (ADR-021).
    """

    entries: tuple[LineConsensus, ...]
    source_count: int
    pages_seen: int = 0
    dropped: tuple[str, ...] = ()

    @property
    def lines(self) -> tuple[str, ...]:
        """The winning rendering of every position (ready for normalization)."""
        return tuple(entry.text for entry in self.entries)

    @property
    def disagreements(self) -> tuple[LineConsensus, ...]:
        """Positions whose transcripts did not all agree."""
        return tuple(entry for entry in self.entries if entry.disputed)

    @property
    def cross_checked(self) -> bool:
        """True when at least two independent transcriptions voted."""
        return self.source_count >= MIN_SOURCE_COUNT

    @property
    def contested(self) -> bool:
        """True when transcriptions that disagreed were dropped before voting."""
        return self.pages_seen > self.source_count


def _mode(values: Iterable[str]) -> tuple[str, int]:
    """Return the most common value and its count (ties: first seen)."""
    counts = Counter(values)
    best = max(counts.values())
    for value in counts:  # dict/Counter preserves insertion order
        if counts[value] == best:
            return value, best
    raise AssertionError("unreachable: Counter is never empty here")


def _shingles(lines: Sequence[str]) -> set[str]:
    """Character 5-grams of a transcript, insensitive to line breaks/spacing.

    Line sets would be defeated by hard-wrapping and by sites that put two
    lyric lines in one paragraph; character shingles compare *content*, so two
    transcriptions of the same song stay similar however they are wrapped.
    """
    text = "".join(line_key(line) for line in lines)
    if not text:
        return set()
    if len(text) <= _SHINGLE:
        return {text}
    return {text[index : index + _SHINGLE] for index in range(len(text) - _SHINGLE + 1)}


def _similarity_matrix(transcripts: Sequence[Transcript]) -> list[list[float]]:
    """Pairwise Jaccard similarity over character shingles."""
    shingles = [_shingles(item.lines) for item in transcripts]
    size = len(transcripts)
    matrix = [[0.0] * size for _ in range(size)]
    for left in range(size):
        for right in range(left + 1, size):
            union = len(shingles[left] | shingles[right])
            value = len(shingles[left] & shingles[right]) / union if union else 0.0
            matrix[left][right] = matrix[right][left] = value
    return matrix


def _medoid_index(transcripts: Sequence[Transcript]) -> int:
    """Index of the transcript most similar to all the others."""
    if len(transcripts) <= 1:
        return 0
    matrix = _similarity_matrix(transcripts)
    return max(range(len(transcripts)), key=lambda index: sum(matrix[index]))


def _average_line_length(transcript: Transcript) -> float:
    if not transcript.lines:
        return 0.0
    return sum(len(line) for line in transcript.lines) / len(transcript.lines)


def _median(values: Sequence[float]) -> float:
    """Median of ``values`` (0.0 when empty); even counts average the halves."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _reference_index(
    transcripts: Sequence[Transcript], matrix: Sequence[Sequence[float]]
) -> int:
    """Index of the transcript whose line shape the consensus should follow.

    The medoid alone is a poor anchor (ADR-023). Live, the "실로암" vote put a
    seven-line sheet-music page first: it merged two lyric lines into one
    paragraph, so it overlapped with everybody and won the centrality tie by a
    hair. The consensus then inherited its shape, and the length guard — which
    compared every other page to *that* page's average line length — threw the
    four real transcriptions away (similarity 0.34-0.92 to the anchor, average
    line length 0.44 of it). The deck was four slides led by the page's own
    heading ``📝실로 찬양 가사``.

    The anchor is the page that

    * **carries the song**: at least :data:`_MIN_COVERAGE` of the richest
      page's shingles, when at least one page qualifies — otherwise a page that
      transcribed only the chorus could anchor;
    * **wraps like the majority**: average line length inside
      :data:`_LENGTH_WINDOW` of the median, when at least one page qualifies —
      otherwise a page that merges lyric lines into paragraphs could anchor;
    * **spells the song in the fewest lines**: both ways a page inflates the
      deck add lines — printing the verse+chorus block five times, and
      hard-wrapping one lyric line into two. The page that prints the song once
      and in the publishers' own wrapping wins (report §12.4, ADR-023).

    Ties go to the page closest to the centre of the vote, so the shape does not
    depend on the order the pages arrived in.
    """
    if len(transcripts) <= 1:
        return 0
    eligible = list(range(len(transcripts)))
    richest = max(len(_shingles(item.lines)) for item in transcripts)
    if richest:
        covering = [
            index
            for index in eligible
            if len(_shingles(transcripts[index].lines)) >= _MIN_COVERAGE * richest
        ]
        if covering:
            eligible = covering
    lengths = [_average_line_length(item) for item in transcripts]
    typical = _median([length for length in lengths if length])
    if typical:
        wrapped = [
            index
            for index in eligible
            if _LENGTH_WINDOW[0] <= lengths[index] / typical <= _LENGTH_WINDOW[1]
        ]
        if wrapped:
            eligible = wrapped
    centre = {
        index: sum(matrix[index][other] for other in eligible) for index in eligible
    }
    return min(
        eligible, key=lambda index: (len(transcripts[index].lines), -centre[index])
    )


def _keep_consistent(
    transcripts: Sequence[Transcript], *, min_similarity: float
) -> list[Transcript]:
    """Keep the coherent cluster; drop pages that are not this song.

    A search result can be an unrelated song, a menu-only page (lyrics published
    as an image) or the same song wrapped in a layout nobody else uses. Such a
    page would otherwise inject positions the majority never saw.

    The cluster is anchored by the medoid *among the pages that wrap like the
    majority* (average line length within :data:`_LENGTH_WINDOW` of the median),
    so a paragraph-merging page can neither anchor the cluster nor, by anchoring
    it, disqualify everybody else: every page below ``min_similarity`` (Jaccard
    over shingles) to that anchor is dropped, and so is every page whose average
    line length is outside the window. Anchoring on the raw medoid instead is
    what made the "실로암" vote keep one page and drop four (ADR-023).
    """
    if len(transcripts) < 2 or min_similarity <= 0:
        return list(transcripts)

    matrix = _similarity_matrix(transcripts)
    lengths = [_average_line_length(item) for item in transcripts]
    typical = _median([length for length in lengths if length])
    wrapped = [
        index
        for index, length in enumerate(lengths)
        if not typical or _LENGTH_WINDOW[0] <= length / typical <= _LENGTH_WINDOW[1]
    ]
    if not wrapped:  # every page wraps unlike every other: keep them all
        wrapped = list(range(len(transcripts)))
    anchor = wrapped[_medoid_index([transcripts[index] for index in wrapped])]
    kept: list[Transcript] = []
    for index, item in enumerate(transcripts):
        if index == anchor:
            kept.append(item)
            continue
        if index not in wrapped:
            continue
        if matrix[index][anchor] < min_similarity:
            continue
        kept.append(item)
    return kept


def _find_match(key: str, lines: Sequence[str]) -> tuple[str, str, int] | None:
    """Find the rendering of ``key`` inside ``lines`` (order-independent).

    Returns ``(key, spelling, index)`` where ``index`` is the line that carries
    it, so a caller can also ask "which position does this page line fill?".
    Exact key matches win; among several identical keys the most common spelling
    is returned (a chorus repeats). When no key matches, the most similar line
    above :data:`MATCH_RATIO` is accepted, which is what lets a one-syllable
    typo still participate in the vote.
    """
    exact = [index for index, line in enumerate(lines) if line_key(line) == key]
    if exact:
        text, _ = _mode(lines[index] for index in exact)
        return key, text, exact[0]

    best: tuple[float, str, str, int] | None = None
    for index, line in enumerate(lines):
        other = line_key(line)
        if not other:
            continue
        ratio = SequenceMatcher(None, key, other, autojunk=False).ratio()
        if ratio >= MATCH_RATIO and (best is None or ratio > best[0]):
            best = (ratio, other, line, index)
    if best is None:
        return None
    return best[1], best[2], best[3]


def _best_match(key: str, lines: Sequence[str]) -> tuple[str, str] | None:
    """Find the rendering of ``key`` inside ``lines`` (see :func:`_find_match`)."""
    found = _find_match(key, lines)
    return None if found is None else (found[0], found[1])


def _gap_position(positions: Sequence[int | None], index: int, size: int) -> int | None:
    """The single reference position left between a page's matched neighbours.

    ``positions[i]`` is the reference position that page line ``i`` fills (``None``
    = it fills none). A line that fills none sits between two positions it *did*
    fill: if exactly one reference position lies between them, the line is a
    rendering *of that position* (the page wrote it differently); zero or several
    positions mean an insertion (an extra verse). ``None`` covers both of those,
    and with ``size`` as the sentinel for "nothing after it" this boundary logic
    decides which of the two an unmatched line is (ADR-024).
    """
    previous = -1
    for other in range(index - 1, -1, -1):
        if positions[other] is not None:
            previous = positions[other]
            break
    following = size
    for other in range(index + 1, len(positions)):
        if positions[other] is not None:
            following = positions[other]
            break
    gap = list(range(previous + 1, following))
    return gap[0] if len(gap) == 1 else None


def _vote(pairs: Sequence[tuple[str, str]], *, ref_key: str, total: int) -> LineConsensus:
    """Turn ``(key, spelling)`` pairs for one position into a consensus entry."""
    counts = Counter(key for key, _ in pairs)
    top = max(counts.values())
    winners = [key for key, count in counts.items() if count == top]
    best_key = ref_key if ref_key in winners else winners[0]

    text, _ = _mode(line for key, line in pairs if key == best_key)
    variants: list[str] = []
    seen_keys: set[str] = set()
    seen_spellings: set[str] = set()
    for key, line in pairs:
        if key == best_key:
            # Same lyric, different spacing ("일꾼되어" vs "일꾼 되어"): not a
            # lost vote, but exactly the kind of detail the user wants to see.
            if line != text and line not in seen_spellings:
                seen_spellings.add(line)
                variants.append(line)
            continue
        if key in seen_keys:
            continue
        seen_keys.add(key)
        variant, _ = _mode(candidate for other, candidate in pairs if other == key)
        if variant not in seen_spellings:
            seen_spellings.add(variant)
            variants.append(variant)
    return LineConsensus(text=text, support=counts[best_key], total=total, variants=tuple(variants))


def cross_check(
    transcripts: Sequence[Transcript],
    *,
    min_support: int = 2,
    min_similarity: float = 0.2,
) -> Consensus:
    """Vote ``transcripts`` line by line and return the consensus.

    Args:
        transcripts: one entry per *independent* page. Callers must deduplicate
            mirrored pages before calling; identical copies would double a vote.
        min_support: minimum number of distinct transcripts required to keep a
            line that exists only outside the reference transcript.
        min_similarity: Jaccard threshold (character 5-grams) below which a
            transcript is treated as an unrelated page and dropped before the
            vote; ``0`` disables the filter.

    Returns:
        A :class:`Consensus`; ``source_count`` is the number of non-empty
        transcripts that took part (0 when every transcript was empty) and
        ``pages_seen`` is how many were offered. A caller must check
        :attr:`Consensus.cross_checked` before calling the result a consensus.
    """
    cleaned: list[Transcript] = []
    for transcript in transcripts:
        lines = _clean_lines(transcript.lines)
        if lines:
            cleaned.append(Transcript(transcript.source, lines, transcript.url))
    if not cleaned:
        return Consensus(entries=(), source_count=0, pages_seen=0)

    # The anchor is picked by similarity, and ties fall to the first transcript,
    # so the input order must not decide the vote: two runs over the same pages
    # otherwise return different lyrics (ADR-020).
    cleaned.sort(key=lambda item: (item.source, item.url or ""))
    offered = cleaned
    pages_seen = len(offered)
    cleaned = _keep_consistent(offered, min_similarity=min_similarity)
    total = len(cleaned)
    kept = {(item.source, item.url) for item in cleaned}
    dropped = tuple(
        item.url or item.source
        for item in offered
        if (item.source, item.url) not in kept
    )
    reference = cleaned[_reference_index(cleaned, _similarity_matrix(cleaned))]
    reference_keys = [line_key(line) for line in reference.lines]

    entries: list[LineConsensus] = []
    for line, key in zip(reference.lines, reference_keys):
        pairs: list[tuple[str, str]] = [(key, line)]
        for transcript in cleaned:
            if transcript is reference:
                continue
            match = _best_match(key, transcript.lines)
            if match is not None:
                pairs.append(match)
        entries.append(_vote(pairs, ref_key=key, total=total))

    # Lines no reference position matched. Most are extra verses, but a page that
    # writes one position differently instead of copying it ("주님 맘속에 사랑
    # 있음을" → "당신 눈 속에 여명 있음을" in 실로암) leaves such a line in a gap of
    # exactly one reference position: it is that position's other rendering, which
    # belongs in the position's variants, not as a stray line at the end of the
    # deck (ADR-024). An insertion between two neighbouring positions leaves no
    # gap and stays an extra verse.
    unmatched: dict[str, list[tuple[str, str, int | None]]] = {}
    for transcript in cleaned:
        if transcript is reference:
            continue
        positions = [
            found[2] if (found := _find_match(line_key(line), reference.lines)) else None
            for line in transcript.lines
        ]
        for index, line in enumerate(transcript.lines):
            key = line_key(line)
            if not key or positions[index] is not None:
                continue
            gap = _gap_position(positions, index, len(reference.lines))
            unmatched.setdefault(key, []).append((transcript.source, line, gap))

    substituted: dict[int, list[str]] = {}
    extras: dict[str, list[tuple[str, str]]] = {}
    extra_order: list[str] = []
    for key, occurrences in unmatched.items():
        distinct = {source for source, _line, _gap in occurrences}
        gaps = {gap for _source, _line, gap in occurrences}
        if len(distinct) < max(1, min_support):
            continue
        if len(gaps) == 1 and None not in gaps:
            position = next(iter(gaps))
            text, _ = _mode(line for _source, line, _gap in occurrences)
            substituted.setdefault(position, []).append(text)
            continue
        extras[key] = [(source, line) for source, line, _gap in occurrences]
        extra_order.append(key)

    if substituted:
        entries = [
            LineConsensus(
                text=entry.text,
                support=entry.support,
                total=entry.total,
                variants=entry.variants
                + tuple(
                    spelling
                    for spelling in substituted.get(index, ())
                    if spelling not in entry.variants
                ),
            )
            for index, entry in enumerate(entries)
        ]

    for key in extra_order:
        occurrences = extras[key]
        distinct = {source for source, _ in occurrences}
        if len(distinct) < max(1, min_support):
            continue
        text, _ = _mode(line for _source, line in occurrences)
        entries.append(
            LineConsensus(text=text, support=len(distinct), total=total, variants=())
        )

    # Trim chrome that only the reference carried at the very edges ("by 작성자",
    # "공유하기", a blog tagline). Interior one-off lines are kept: they are
    # usually a genuine extra verse rather than page furniture.
    if total >= 3:
        while entries and entries[0].support < 2:
            entries.pop(0)
        while entries and entries[-1].support < 2:
            entries.pop()

    return Consensus(
        entries=tuple(entries),
        source_count=total,
        pages_seen=pages_seen,
        dropped=dropped,
    )
