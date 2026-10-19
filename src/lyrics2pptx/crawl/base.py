"""Collector data contract and error hierarchy (L0 leaf).

Owns the boundary types shared between the registry, the adapters, and the
pipeline (docs/arch/03 §2). It performs **no** network access: every HTTP call
must go through ``crawl/httputil.py`` (rule P7 in docs/arch/02 §4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

from ..errors import Lyrics2PptxError

__all__ = [
    "RawSong",
    "SourceError",
    "SourceNotFoundError",
    "SourceBlockedError",
    "SourceUnavailableError",
    "Source",
]


@dataclass(frozen=True, slots=True)
class RawSong:
    """One collected song: industry raw text, before normalization."""

    title: str
    artist: str | None
    lyrics: str  # as returned by the source, not normalized; may be empty
    source_name: str  # e.g. "lyrics_ovh"
    source_url: str | None = None
    #: ``True`` when a blog consensus passed cross-validation, ``False`` when it
    #: did not, ``None`` for candidates without a vote (ADR-025). The chain uses
    #: it to put a checked consensus ahead of an unchecked one.
    cross_checked: bool | None = None


class SourceError(Lyrics2PptxError):
    """Upper bound of every collection failure (collector sub-tree)."""


class SourceNotFoundError(SourceError):
    """No result for the query, or a required field was absent."""


class SourceBlockedError(SourceError):
    """robots.txt disallows the path, or the server answered 403/404."""


class SourceUnavailableError(SourceError):
    """Retries were exhausted, the response could not be parsed, or the
    offline fixture was missing."""


@runtime_checkable
class Source(Protocol):
    """Adapter contract. Implementations live in ``crawl/<site>.py``."""

    name: str

    def search(self, query: str, *, limit: int = 5) -> Sequence[RawSong]:
        """Return candidate songs for ``query``.

        Raises:
            SourceNotFoundError: the query produced no candidates.
            SourceBlockedError: the source refused the request.
            SourceUnavailableError: the source could not be reached or parsed.
        """
        ...
