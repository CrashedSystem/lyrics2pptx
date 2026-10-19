"""Priority chain collector (L3): blog consensus > lrclib > unconfirmed blog.

``auto`` is the default source. Its priority is a three-tier rule, not a member
order (ADR-025):

1. a blog consensus that passed cross-validation — two or more independent
   pages agreed, so the text has been checked against something;
2. ``lrclib`` — structured and fast, but its ranking is not trustworthy: a
   query shortened to one token can return an unrelated song, and the adapter
   auto-selects hit 0 (조사보고서 §16.5, five of twenty-one songs);
3. a blog consensus nobody could confirm (one page).

Language comes before the tiers (ADR-026): candidates whose lyrics are Korean
are considered first, so an English original cannot win over an available
Korean version. Only when no Korean candidate exists does the same tier rule
apply to foreign-language candidates.

Both members are queried *concurrently*. Cross-validation needs the blog crawl
even when lrclib could answer, so a sequential chain would add the whole crawl
to every query; in parallel the query costs the slower of the two.

Error policy (unchanged, ADR-018): a member that *definitively* found nothing
decides the user-facing error, because "검색 결과가 없습니다 → 직접 붙여넣기" is
the actionable message. A transient failure is only propagated when nobody
found anything.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor

from .. import config
from . import progress
from .base import (
    RawSong,
    SourceBlockedError,
    SourceError,
    SourceNotFoundError,
    SourceUnavailableError,
)

__all__ = ["ChainSource"]

log = logging.getLogger(__name__)

_HANGUL_RE = re.compile(r"[가-힣]")

#: Tier labels (ADR-025). ``blog`` appears twice on purpose: a cross-checked
#: consensus and an unconfirmed one are different evidence, not different pages.
_TIER_CHECKED = "blog 교차검증"
_TIER_LRCLIB = "lrclib"
_TIER_UNCHECKED = "blog 미검증"


def is_korean_text(lyrics: str) -> bool:
    """True when most non-empty lines carry Hangul (ADR-026).

    The rule is the policy itself — the language of the lyrics decides which
    candidates are tried first — so callers also use it to *report* the language
    of what they are about to show (``--cross-check``, ``--json``).
    """
    lines = [line for line in lyrics.splitlines() if line.strip()]
    if not lines:
        return False
    korean = sum(1 for line in lines if _HANGUL_RE.search(line))
    return korean * 2 > len(lines)


class ChainSource:
    """Ask the configured members concurrently, then pick by ADR-025's tiers."""

    name = "auto"

    def search(self, query: str, *, limit: int = 5) -> list[RawSong]:
        # Deferred import: the registry imports this module.
        from . import get_source

        members = tuple(config.CHAIN_MEMBERS)
        # The UI shows which members are being asked; a chain of one (the
        # explicit ``lrclib``/``blog`` sources) does not announce itself, and the
        # chain never publishes a percentage — only the collector that knows the
        # real page count may do that (an invented number is worse than a
        # sweeping bar).
        if len(members) > 1:
            progress.report(f"{'·'.join(members)} 소스 동시 조회 중…")
        answers = self._ask(members, get_source, query, limit)

        chosen = self._choose(answers)
        if chosen is not None:
            label, member, songs = chosen
            for other, (candidate, _error) in answers.items():
                if other != member and candidate:
                    log.info(
                        "chain skipped member=%s hits=%d picked=%s",
                        other,
                        len(candidate),
                        label,
                    )
            log.info(
                "chain picked=%s tier=%s hits=%d korean=%s",
                member,
                label,
                len(songs),
                is_korean_text(songs[0].lyrics),
            )
            progress.report(f"{label} — 후보 {len(songs)}건")
            return list(songs)

        errors = [error for _songs, error in answers.values() if error is not None]
        for exc_type in (
            SourceNotFoundError,
            SourceBlockedError,
            SourceUnavailableError,
        ):
            for exc in errors:
                if isinstance(exc, exc_type):
                    raise exc
        raise SourceNotFoundError(f"'{query}' 검색 결과가 없습니다")

    def _ask(
        self,
        members: tuple[str, ...],
        get_source,
        query: str,
        limit: int,
    ) -> dict[str, tuple[list[RawSong], SourceError | None]]:
        """Query every member in parallel; return ``{name: (songs, error)}``."""
        answers: dict[str, tuple[list[RawSong], SourceError | None]] = {}

        def ask(member: str) -> None:
            try:
                songs = list(get_source(member).search(query, limit=limit))
            except SourceError as exc:
                answers[member] = ([], exc)
                return
            answers[member] = (songs, None)

        # One worker per member: the collectors are network-bound and know
        # nothing about each other, so a pool this small is the whole change.
        with ThreadPoolExecutor(max_workers=max(1, len(members))) as pool:
            list(pool.map(ask, members))
        return answers

    def _choose(
        self,
        answers: dict[str, tuple[list[RawSong], SourceError | None]],
    ) -> tuple[str, str, list[RawSong]] | None:
        """Return ``(label, member, songs)`` for the winning tier, or ``None``."""
        for korean in (True, False):
            group = {
                name: songs
                for name, (songs, _error) in answers.items()
                if songs and is_korean_text(songs[0].lyrics) is korean
            }
            if not group:
                continue
            blog = group.get("blog")
            if blog and blog[0].cross_checked:
                return _TIER_CHECKED, "blog", blog
            if group.get("lrclib"):
                return _TIER_LRCLIB, "lrclib", group["lrclib"]
            if blog:
                return _TIER_UNCHECKED, "blog", blog
            # A member this file does not know about (config-driven): keep it
            # usable instead of hiding a hit.
            for name in answers:
                if name in group:
                    return name, name, group[name]
        return None
