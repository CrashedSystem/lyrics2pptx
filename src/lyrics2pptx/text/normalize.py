"""Text normalization pipeline N1..N11 (L2).

``str`` in → ``list[str]`` out (docs/plan/04 §1). The step order is fixed; the
test suite pins each step. This module never raises for empty input: an empty
result is returned and the "no usable line" verdict is made exactly once by
``pipeline.run`` (contract E3 in docs/arch/03 §1).

N6 rule (final): a line is removed only when it is a section marker *by itself*
— whole residual equals a keyword, or the keyword is followed by nothing but a
separator, an optional ``x``/``×`` and digits ("verse 1", "chorus x2", "후렴 2절",
"Chorus:") — including bracket decorations like ``[Chorus]``/``(후렴)``/``※``/``×2``.
Any other trailing word ("chorus line", "intro to me") means a real lyric line
and is kept: when in doubt, preserve (fail-safe).

N11 rule (new, ADR-022): performance ad-libs are removed, but only the ones we
can *recognise*. A bracketed group whose whole content is interjections
("(Yeah, woah, yeah)", "(Woah)", "(Ooh)") disappears, and a trailing interjection
hung on the end of a line that still contains Hangul ("하나님께서 바라보시며 oh")
disappears with it. Anything else survives: an English echo "(hold me now)", a
Korean echo "(사랑해요 영원히)" and an all-English line "Oh yeah" are lyric text,
not 추임새. A line the removal empties is dropped exactly like a blank line (N7).
"""

from __future__ import annotations

import logging
import re
import unicodedata

__all__ = ["normalize_lines", "normalize_with_stats", "SECTION_MARKER_RE"]

log = logging.getLogger(__name__)

# N6: a short, bracket-ish line whose *entire* residual text may be a marker.
# The regex captures the text inside the optional wrapping brackets so that
# ``_is_section_marker`` can compare the whole residual against a keyword
# instead of searching for a substring somewhere inside the line.
SECTION_MARKER_RE = re.compile(
    r"^\s*[\[\(（]?(?P<inner>[^\]\)）]{0,12})[\]\)）]?\s*$",
    re.IGNORECASE,
)
SECTION_KEYWORDS = (
    "verse",
    "chorus",
    "bridge",
    "intro",
    "outro",
    "후렴",
    "절",
    "간주",
    "x2",
    "×2",
    "※",
)

_ZERO_WIDTH = ("\u200b", "\ufeff", "\u200c", "\u200d")
_MULTI_SPACE = re.compile(r"[ \t]+")
_TRAILING_MARKER = re.compile(r"[|\-~]+\s*$")
# N6: what may follow a keyword while the line still counts as a marker. The
# suffix is *number-centred*: only separators (space/colon), an optional
# multiplication sign, digits, and an optional trailing "절" are accepted
# ("Verse 1", "verse2", "chorus x2", "후렴 2절", "Chorus:"). Arbitrary words are
# rejected so real lyric lines survive ("chorus line", "intro to me").
_MARKER_SUFFIX_RE = re.compile(r"[ :]*[x×]?[0-9]*절?")
# N8: an identical line repeated three or more times in a row collapses to one.
_RUN_THRESHOLD = 3

# N11: interjections a singer *performs* rather than sings. Deliberately a closed
# list: a bracket is only removed when every word inside it is one of these, so
# "(hold me now)" and "(사랑해요 영원히)" cannot be mistaken for an ad-lib.
_ADLIB_TOKENS = frozenset(
    {
        "yeah",
        "yea",
        "yep",
        "woah",
        "whoa",
        "oh",
        "ooh",
        "oooh",
        "uh",
        "uhh",
        "ah",
        "ahh",
        "hey",
        "ha",
        "haha",
        "hmm",
        "hm",
        "mm",
        "mmm",
        "yo",
        "wow",
        "yay",
        "eh",
        "ay",
        "ayy",
        "uhuh",
        "uhhuh",
        "la",
        "na",
        "da",
    }
)
_ADLIB_TOKEN_ALT = "|".join(sorted(_ADLIB_TOKENS, key=len, reverse=True))
_ADLIB_GROUP_RE = re.compile(r"\s*[\[\(（]([^\]\)）]*)[\]\)）]")
_ADLIB_TRAILING_RE = re.compile(rf"\s+(?:{_ADLIB_TOKEN_ALT})\s*[.!?…]?\s*$", re.IGNORECASE)
_HANGUL_RE = re.compile(r"[가-힣]")


def _step_n1_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _step_n2_unicode(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _step_n3_invisibles(text: str) -> str:
    for ch in _ZERO_WIDTH:
        text = text.replace(ch, "")
    return text.replace("\u00a0", " ")


def _is_section_marker(line: str) -> bool:
    """N6: True only when the *whole* residual line is a marker.

    Rule: after stripping surrounding brackets/space, the residual text must
    either equal a keyword or start with a keyword followed only by a
    number-centred suffix (``[ :]*[x×]?[0-9]*절?`` — "Verse 1", "verse2",
    "chorus x2", "후렴 2절", "Chorus:"). Real lyric lines such as "내 절망마저",
    "chorus line" or "이 간주곡" are kept: any other trailing word fails the
    suffix and the line is preserved (fail-safe).
    """
    match = SECTION_MARKER_RE.match(line)
    if match is None:
        return False
    residual = match.group("inner").strip().lower()
    if not residual:
        return False
    for keyword in SECTION_KEYWORDS:
        if residual == keyword:
            return True
        if residual.startswith(keyword) and _MARKER_SUFFIX_RE.fullmatch(residual[len(keyword) :]):
            return True
    return False


def _is_adlib_group(inner: str) -> bool:
    """N11: True when a bracket holds nothing but interjections (or nothing at all).

    ``"(Yeah, woah, yeah)"`` and ``"(Ooh)"`` are performance; ``"(사랑해요 영원히)"``
    and ``"(hold me now)"`` are words somebody sings, so their words are not in
    :data:`_ADLIB_TOKENS` and the bracket stays.
    """
    words = [word for word in re.split(r"[^0-9A-Za-z가-힣]+", inner) if word]
    if not words:
        return True  # "(...)", "( )" — decoration, carries nothing
    return all(word.lower() in _ADLIB_TOKENS for word in words)


def _step_n11_adlibs(line: str) -> str:
    """N11: strip ad-lib brackets and a trailing interjection; may return ``""``."""

    def replace(match: re.Match[str]) -> str:
        return "" if _is_adlib_group(match.group(1)) else match.group(0)

    stripped = _MULTI_SPACE.sub(" ", _ADLIB_GROUP_RE.sub(replace, line)).strip()
    # Only for a line that still holds Hangul: "하나님께서 바라보시며 oh" is Korean
    # with a performance tail, while "Oh yeah" is an English lyric line.
    if _HANGUL_RE.search(stripped):
        stripped = _ADLIB_TRAILING_RE.sub("", stripped).strip()
    return stripped


def _collapse_consecutive_duplicates(lines: list[str]) -> list[str]:
    """N8: reduce a run of three or more identical lines to a single copy."""
    collapsed: list[str] = []
    index = 0
    total = len(lines)
    while index < total:
        current = lines[index]
        run_length = 1
        while index + run_length < total and lines[index + run_length] == current:
            run_length += 1
        if run_length >= _RUN_THRESHOLD:
            collapsed.append(current)
        else:
            collapsed.extend([current] * run_length)
        index += run_length
    return collapsed


def _run_pipeline(text: str) -> tuple[list[str], int]:
    """Run N1..N11 once and return ``(lines, removed_marker_count)``.

    Single source for both ``normalize_lines`` and ``normalize_with_stats`` so
    the web preview and the real build can never disagree (M1/M2, docs/plan/04
    §1 N6). ``removed_marker_count`` counts the N6 marker-only lines dropped; an
    N11 ad-lib line that is emptied out is counted as a dropped line, exactly
    like a blank one (N7), and is visible in the debug log below.
    """
    if not text:
        return [], 0

    staged = _step_n3_invisibles(_step_n2_unicode(_step_n1_newlines(text)))

    lines: list[str] = []
    removed_markers = 0
    for line in staged.split("\n"):
        cleaned = _MULTI_SPACE.sub(" ", line).strip()  # N5
        if not cleaned:  # N7 (blank / whitespace-only)
            continue
        if _is_section_marker(cleaned):  # N6
            removed_markers += 1
            continue
        cleaned = _TRAILING_MARKER.sub("", cleaned).strip()  # N9
        if not cleaned:
            continue
        cleaned = _step_n11_adlibs(cleaned)  # N11
        if not cleaned:
            continue
        lines.append(cleaned)  # N10 (accumulate)

    lines = _collapse_consecutive_duplicates(lines)  # N8
    return lines, removed_markers


def normalize_lines(text: str) -> list[str]:
    """Run the fixed N1..N11 pipeline and return the remaining lines in order.

    Args:
        text: raw lyric text as returned by a collector (or pasted by a user).

    Returns:
        A list of non-empty, trimmed lines. Empty input yields ``[]``.
    """
    if not text:
        return []

    raw_line_count = len(_step_n1_newlines(text).split("\n"))
    lines, removed_markers = _run_pipeline(text)
    log.debug(
        "normalized lines=%d dropped=%d markers=%d",
        len(lines),
        max(0, raw_line_count - len(lines)),
        removed_markers,
    )
    return lines


def normalize_with_stats(text: str) -> tuple[list[str], int]:
    """Normalize ``text`` and report how many N6 marker lines were removed.

    Used by the web preview (``pipeline.preview``) so the browser never needs a
    second copy of N1..N10: the preview result is produced by this module, the
    same one that feeds the real build.
    """
    return _run_pipeline(text)
