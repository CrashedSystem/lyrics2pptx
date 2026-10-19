"""Search-backend contract (L0 leaf).

A *search backend* answers "which public pages might carry this song?" — it is
an index, not a lyrics source. Lyrics are always read from the result page
itself, so a backend that returns only titles/snippets (the Naver Open API does)
is still usable in principle.

Backends must perform their HTTP through ``crawl.httputil`` (rule P7); that is
what makes robots.txt, the user agent and the per-host delay non-bypassable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

__all__ = ["SearchResult", "SearchBackend"]


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One hit returned by a search backend."""

    title: str
    url: str
    snippet: str = ""


@runtime_checkable
class SearchBackend(Protocol):
    """Minimal interface every search backend implements."""

    name: str

    def search(self, query: str, *, limit: int = 10) -> Sequence[SearchResult]:
        """Return up to ``limit`` results for ``query`` (best match first)."""
        ...
