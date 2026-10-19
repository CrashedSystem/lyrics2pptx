"""Fallback chain collector (L3).

``auto`` is the default source: it asks each member in order and returns the
first non-empty result set. Ordering matters — ``lrclib`` is a fast structured
JSON API, so a normal query never pays for the slower web search; only a query
that lrclib cannot serve falls through to ``blog`` (ADR-018).

Error policy: if any member *definitively* found nothing, that verdict wins over
a transient failure of another member, because "검색 결과가 없습니다 → 직접
붙여넣기" is the actionable message. Only when every member failed for a
non-``not found`` reason is that failure propagated.
"""

from __future__ import annotations

import logging

from .. import config
from .base import (
    RawSong,
    SourceBlockedError,
    SourceError,
    SourceNotFoundError,
    SourceUnavailableError,
)

__all__ = ["ChainSource"]

log = logging.getLogger(__name__)


class ChainSource:
    """Try ``config.CHAIN_MEMBERS`` in order; first non-empty answer wins."""

    name = "auto"

    def search(self, query: str, *, limit: int = 5) -> list[RawSong]:
        # Deferred import: the registry imports this module.
        from . import get_source

        errors: list[SourceError] = []
        for member in config.CHAIN_MEMBERS:
            try:
                songs = get_source(member).search(query, limit=limit)
            except SourceError as exc:
                log.info(
                    "chain member=%s failed=%s", member, type(exc).__name__
                )
                errors.append(exc)
                continue
            if songs:
                log.info("chain member=%s hits=%d", member, len(songs))
                return list(songs)
            errors.append(SourceNotFoundError(f"{member}: 검색 결과가 없습니다"))

        for exc_type in (SourceNotFoundError, SourceBlockedError, SourceUnavailableError):
            for exc in errors:
                if isinstance(exc, exc_type):
                    raise exc
        raise SourceNotFoundError(f"'{query}' 검색 결과가 없습니다")
