"""Two-line pairing (L2).

``list[str]`` in → ``list[LinePair]`` out (docs/plan/04 §2, decision D4).
Pure and exception-free: an empty input yields an empty list (contract E4 in
docs/arch/03 §1).
"""

from __future__ import annotations

import logging

__all__ = ["LinePair", "split_pairs"]

log = logging.getLogger(__name__)

# (first line, second line) — the second element is None on an odd last pair.
LinePair = tuple[str, str | None]

# A line repeated this many times or more is trimmed when dedupe_repeat is on.
_REPEAT_THRESHOLD = 3
# How many of a repeated line survive deduplication (keeps two occurrences).
_KEEP_REPEATS = 2


def _dedupe_repeated_lines(lines: list[str]) -> list[str]:
    """Drop brute-force chorus repeats (non-adjacent), keeping the first pair.

    Only lines appearing ``_REPEAT_THRESHOLD`` times or more are affected, and
    only the first ``_KEEP_REPEATS`` occurrences survive. The relative order of
    everything else is preserved.
    """
    counts: dict[str, int] = {}
    for line in lines:
        counts[line] = counts.get(line, 0) + 1

    seen: dict[str, int] = {}
    result: list[str] = []
    for line in lines:
        if counts[line] >= _REPEAT_THRESHOLD:
            used = seen.get(line, 0)
            if used >= _KEEP_REPEATS:
                continue
            seen[line] = used + 1
        result.append(line)
    return result


def split_pairs(lines: list[str], *, dedupe_repeat: bool = False) -> list[LinePair]:
    """Group ``lines`` in order, two per pair.

    Args:
        lines: normalized, non-empty lines (see ``text.normalize``).
        dedupe_repeat: when True, brute-force repeated lines (>= 3 occurrences)
            are trimmed to their first two occurrences first. Default False —
            the original lyrics are preserved (decision D5).

    Returns:
        Pairs in order. On an odd line count the final pair is
        ``(last_line, None)`` so no empty slide is produced.
    """
    source = list(lines)
    if dedupe_repeat:
        source = _dedupe_repeated_lines(source)

    pairs: list[LinePair] = [
        (source[i], source[i + 1] if i + 1 < len(source) else None)
        for i in range(0, len(source), 2)
    ]
    log.info("split pairs=%d odd=%s", len(pairs), bool(source) and len(source) % 2 == 1)
    return pairs
