"""Search-backend registry (L3).

A *search backend* turns a query into candidate page URLs; the lyrics are then
read from those pages by :mod:`lyrics2pptx.crawl.blog`. Only backends whose
end-points are robots-allowed are registered — ``ddg`` (ADR-018) is the only
general index that qualifies today. ``naver`` is deliberately **not** shipped:
both ``openapi.naver.com`` and ``search.naver.com`` answer ``Disallow: /`` to
every agent (see ADR-018), and this project never bypasses robots.txt.
"""

from __future__ import annotations

from ... import config
from ...errors import ConfigError
from .base import SearchBackend, SearchResult
from .ddg import DdgSearch

__all__ = [
    "BACKENDS",
    "SearchBackend",
    "SearchResult",
    "available_backends",
    "get_backend",
]

# Registry of enabled backends. Add a module + one line here to extend.
BACKENDS: dict[str, type[SearchBackend]] = {
    "ddg": DdgSearch,
}


def available_backends() -> tuple[str, ...]:
    """Return the names of every registered backend, sorted."""
    return tuple(sorted(BACKENDS))


def get_backend(name: str | None = None) -> SearchBackend:
    """Return a backend instance, defaulting to ``config.SEARCH_BACKEND``.

    Raises:
        ConfigError: the name is not registered.
    """
    chosen = name or config.SEARCH_BACKEND
    try:
        backend_class = BACKENDS[chosen]
    except KeyError as exc:
        known = ", ".join(available_backends()) or "(없음)"
        raise ConfigError(
            f"알 수 없는 검색 백엔드입니다: {chosen!r} (사용 가능: {known})"
        ) from exc
    return backend_class()
