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

The reference is the *medoid* transcript (the one most similar to all others),
so the consensus keeps the line shape most publishers use. A transcript that is
not the same song (an unrelated search hit, or a page whose "lyrics" are an
image and whose menu was scraped) is dropped before voting.

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


def _keep_consistent(
    transcripts: Sequence[Transcript], *, min_similarity: float
) -> list[Transcript]:
    """Keep the coherent cluster; drop pages that are not this song.

    A search result can be an unrelated song, a menu-only page (lyrics published
    as an image) or the same song under a wildly different arrangement. Such a
    page would otherwise inject lines the majority never saw. The medoid
    transcript anchors the cluster and every transcript below ``min_similarity``
    (Jaccard over shingles) is dropped.

    A transcript whose average line length is far from the medoid's is dropped
    too: a site that puts two lyric lines in one paragraph otherwise contributes
    nothing but noise.
    """
    if len(transcripts) < 2 or min_similarity <= 0:
        return list(transcripts)

    matrix = _similarity_matrix(transcripts)
    medoid = max(range(len(transcripts)), key=lambda index: sum(matrix[index]))
    reference_length = _average_line_length(transcripts[medoid])
    kept: list[Transcript] = []
    for index, item in enumerate(transcripts):
        if index == medoid:
            kept.append(item)
            continue
        if matrix[index][medoid] < min_similarity:
            continue
        length = _average_line_length(item)
        if reference_length and not 0.6 <= length / reference_length <= 1.7:
            continue
        kept.append(item)
    return kept


def _best_match(key: str, lines: Sequence[str]) -> tuple[str, str] | None:
    """Find the rendering of ``key`` inside ``lines`` (order-independent).

    Exact key matches win; among several identical keys the most common spelling
    is returned (a chorus repeats). When no key matches, the most similar line
    above :data:`MATCH_RATIO` is accepted, which is what lets a one-syllable
    typo still participate in the vote.
    """
    exact = [line for line in lines if line_key(line) == key]
    if exact:
        text, _ = _mode(exact)
        return key, text

    best: tuple[float, str, str] | None = None
    for line in lines:
        other = line_key(line)
        if not other:
            continue
        ratio = SequenceMatcher(None, key, other, autojunk=False).ratio()
        if ratio >= MATCH_RATIO and (best is None or ratio > best[0]):
            best = (ratio, other, line)
    if best is None:
        return None
    return best[1], best[2]


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

    # The medoid is picked by similarity, and ties fall to the first transcript,
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
    reference = cleaned[_medoid_index(cleaned)]
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

    # Lines that exist only outside the reference (an extra verse) are appended,
    # but a line carried by a single page is treated as that page's noise.
    extras: dict[str, list[tuple[str, str]]] = {}
    extra_order: list[str] = []
    for transcript in cleaned:
        if transcript is reference:
            continue
        for line in transcript.lines:
            key = line_key(line)
            if not key or _best_match(key, reference.lines) is not None:
                continue
            if key not in extras:
                extras[key] = []
                extra_order.append(key)
            extras[key].append((transcript.source, line))

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
