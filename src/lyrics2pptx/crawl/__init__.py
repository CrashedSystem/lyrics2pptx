"""Collector registry (L3 composition).

Maps a source name to an adapter instance. Only key-free, publicly documented
sources ship enabled (docs/arch/06 §1). ``html_generic`` is intentionally left
out of ``SOURCES``; enabling it is an explicit user edit (see that module's
warning header).

``auto`` (ADR-018) is the default: it falls back from lrclib to ``blog``, which
searches the open web through a robots-allowed backend and cross-validates the
pages it finds.
"""

from __future__ import annotations

from ..errors import ConfigError
from .base import (
    RawSong,
    Source,
    SourceBlockedError,
    SourceError,
    SourceNotFoundError,
    SourceUnavailableError,
)
from .blog import BlogLyricsSource
from .chain import ChainSource
from .lrclib import LrclibSource
from .lyrics_ovh import LyricsOvhSource

__all__ = [
    "SOURCES",
    "get_source",
    "available_sources",
    "RawSong",
    "Source",
    "SourceError",
    "SourceNotFoundError",
    "SourceBlockedError",
    "SourceUnavailableError",
]

# Registry of enabled adapters. Add a module + one line here to extend.
SOURCES: dict[str, type[Source]] = {
    "auto": ChainSource,  # ADR-018: lrclib first, then blog; default
    "lrclib": LrclibSource,  # ADR-017: key-free, robots-allowed, Korean CCM present
    "blog": BlogLyricsSource,  # ADR-018: web search + extraction + majority vote
    "lyrics_ovh": LyricsOvhSource,  # kept for reference; robots.txt is a 404 -> now allowed
    # "html_generic": HtmlGenericSource,  # activate only after reading robots.txt / ToS
}


def available_sources() -> tuple[str, ...]:
    """Return the names of every registered source, sorted."""
    return tuple(sorted(SOURCES))


def get_source(name: str) -> Source:
    """Return an adapter instance for ``name``.

    Raises:
        ConfigError: the name is not registered.
    """
    try:
        adapter_class = SOURCES[name]
    except KeyError as exc:
        known = ", ".join(available_sources()) or "(없음)"
        raise ConfigError(f"알 수 없는 소스입니다: {name!r} (사용 가능: {known})") from exc
    return adapter_class()
