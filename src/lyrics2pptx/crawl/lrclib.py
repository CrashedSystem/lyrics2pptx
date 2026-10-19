"""lrclib.net collector adapter — the default, key-free public JSON API (L2).

Chosen as the default by ADR-017 (``결정문서.md``) and its research report
(``조사보고서.md``): it is the only key-free source whose ``robots.txt`` allows
our User-Agent **and** that actually returns Korean worship/CCM songs.
``lyrics_ovh`` is kept as a module but is no longer the default — its
``robots.txt`` answers 404, so the fail-closed politeness layer blocks every
request (도달률 0%).

End-points (조사보고서 §3.1):

* ``GET /api/search?q=<text>``                 → JSON **array** of candidates
* ``GET /api/search?track_name=&artist_name=`` → JSON **array** (field filter)
* ``GET /api/get/<id>``                        → JSON **object** (one exact match)

Query interpretation (조사보고서 §5):

1. ``"artist - title"`` → free search ``q`` first, then the field filter
   ``track_name``/``artist_name`` when the first pass is empty.
2. A bare string → ``q``; when empty, retry with the word tokens reordered
   longest-first and then dropping the shortest tokens, so
   ``"우리 함께 손잡고 가세"`` still reaches ``"손잡고 함께 가세"``.
3. Items whose ``plainLyrics`` is empty (instrumental) are dropped, and the
   result is filled up to ``limit``.
4. Ranking is *not* trusted: every surviving candidate is returned in source
   order so the user picks (no silent auto-selection, 조사보고서 §5.4).

Time budget (ADR-030): ``auto`` queries this adapter and ``blog``
**concurrently** and only prefers the checked blog consensus (ADR-025), so a
dead lrclib must not hold the chain. A live ``search`` therefore takes one
deadline (``LYRICS2PPTX_LRCLIB_TIMEOUT``, default 10 s) that covers every URL
—free query, field filter, and token fallbacks—and disables the transport's
retry backoff (``retries=0``). One slow URL costs at most the remaining budget
instead of ``MAX_RETRIES × HTTP_TIMEOUT``, and the loop stops as soon as the
budget is spent.

Offline mode (``LYRICS2PPTX_FIXTURE_DIR`` set) reads a saved response. The
fixture is ``lrclib_<slug>.json`` when present, otherwise ``lrclib_sample.json``;
its body must be the JSON array the live search returns.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from time import monotonic

from .. import config
from . import httputil
from .base import RawSong, SourceNotFoundError

__all__ = ["LrclibSource", "BASE_URL", "SEARCH_URL", "GET_URL", "SAMPLE_FIXTURE"]

log = logging.getLogger(__name__)

BASE_URL = "https://lrclib.net"
SEARCH_URL = f"{BASE_URL}/api/search"
GET_URL = f"{BASE_URL}/api/get"
SAMPLE_FIXTURE = "lrclib_sample.json"

_SEPARATOR = re.compile(r"\s+-\s+")
_SLUG = re.compile(r"[^a-z0-9]+")


def _parse_query(query: str) -> tuple[str, str]:
    """Split ``"artist - title"``; a bare query yields ``("", query)``."""
    parts = _SEPARATOR.split(query, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", query.strip()


def _fixture_name(query: str) -> str:
    """Return the saved-response name driving a single offline lookup."""
    base = config.FIXTURE_DIR
    slug = _SLUG.sub("_", query.lower()).strip("_")
    if base is not None and slug and (Path(base) / f"lrclib_{slug}.json").is_file():
        return f"lrclib_{slug}.json"
    return SAMPLE_FIXTURE


def _search_url_q(query: str) -> str:
    """Free-text search URL (``/api/search?q=``)."""
    return f"{SEARCH_URL}?q={httputil.urlquote(query)}"


def _search_fields_url(title: str, artist: str) -> str:
    """Field-filter search URL (``track_name`` + optional ``artist_name``)."""
    url = f"{SEARCH_URL}?track_name={httputil.urlquote(title)}"
    if artist:
        url += f"&artist_name={httputil.urlquote(artist)}"
    return url


def _fallback_queries(text: str) -> list[str]:
    """Reordered/shortened variants of ``text`` for a second-chance search.

    Tokens are reordered longest-first and the shortest tokens are dropped one
    at a time (ties broken by original position); e.g.
    ``"우리 함께 손잡고 가세"`` → ``"손잡고 함께 가세"`` (조사보고서 §5.2).
    """
    tokens = text.split()
    if len(tokens) < 2:
        return []
    order = sorted(range(len(tokens)), key=lambda index: (len(tokens[index]), index))
    queries: list[str] = []
    seen: set[str] = set()
    for drop in range(1, len(tokens)):
        dropped = set(order[:drop])
        kept = [token for index, token in enumerate(tokens) if index not in dropped]
        kept.sort(key=len, reverse=True)
        candidate = " ".join(kept)
        if candidate and candidate not in seen:
            seen.add(candidate)
            queries.append(candidate)
    return queries


def _raw_from_item(item: object) -> RawSong | None:
    """Map one ``/api/search`` array element to ``RawSong`` (or ``None``).

    ``plainLyrics`` empty/absent means an instrumental entry, which is dropped
    because there is nothing to put on a slide.
    """
    if not isinstance(item, dict):
        return None
    lyrics = item.get("plainLyrics")
    if not isinstance(lyrics, str) or not lyrics.strip():
        return None
    title = str(item.get("trackName") or "").strip()
    if not title:
        return None
    artist = str(item.get("artistName") or "").strip() or None
    item_id = item.get("id")
    source_url = f"{GET_URL}/{item_id}" if isinstance(item_id, (int, str)) else None
    return RawSong(
        title=title,
        artist=artist,
        lyrics=lyrics,
        source_name="lrclib",
        source_url=source_url,
    )


class LrclibSource:
    """``Source`` implementation for ``https://lrclib.net``."""

    name = "lrclib"

    def search(self, query: str, *, limit: int = 5) -> list[RawSong]:
        text = (query or "").strip()
        if not text:
            raise SourceNotFoundError("검색어가 비어 있습니다")
        log.debug("query=%s", text[:60])

        if config.FIXTURE_DIR is not None:
            # Offline mode performs exactly one lookup so a single saved
            # response drives the whole adapter (docs/plan/03 §오프라인 모드).
            songs = self._collect(
                _search_url_q(text), limit, fixture=_fixture_name(text)
            )
        else:
            # ADR-030: one deadline for every live URL; it replaces the
            # per-request retry storm that used to reach ~100 s.
            deadline = monotonic() + config.LRCLIB_TIMEOUT
            songs = self._search_live(text, limit, deadline)

        if not songs:
            raise SourceNotFoundError("검색 결과가 없습니다")
        log.info("source=%s hits=%d", self.name, len(songs))
        return songs

    def _search_live(self, text: str, limit: int, deadline: float) -> list[RawSong]:
        artist, title = _parse_query(text)

        songs = self._collect(_search_url_q(text), limit, deadline=deadline)
        if songs:
            return songs

        if artist:
            songs = self._collect(
                _search_fields_url(title or text, artist), limit, deadline=deadline
            )
            if songs:
                return songs

        for fallback in _fallback_queries(text):
            songs = self._collect(_search_url_q(fallback), limit, deadline=deadline)
            if songs:
                return songs
        return []

    def _collect(
        self,
        url: str,
        limit: int,
        *,
        fixture: str | None = None,
        deadline: float | None = None,
    ) -> list[RawSong]:
        """Fetch a search URL and map its array items, dropping unusable ones.

        When ``deadline`` (monotonic seconds) is given the *remaining* budget
        becomes the socket timeout and retries are turned off (ADR-030): a
        spent budget returns no candidates without touching the network, so the
        caller cannot spend more than ``LYRICS2PPTX_LRCLIB_TIMEOUT`` overall.
        """
        retries: int | None = None
        timeout: float | None = None
        if deadline is not None:
            remaining = deadline - monotonic()
            if remaining <= 0:
                log.debug("deadline exhausted url=%s", url)
                return []
            timeout = min(config.HTTP_TIMEOUT, remaining)
            retries = 0
        items = httputil.get_json_list(
            url, timeout=timeout, retries=retries, fixture=fixture
        )
        songs: list[RawSong] = []
        for item in items:
            if len(songs) >= limit:
                break
            raw = _raw_from_item(item)
            if raw is None:
                log.debug("skip unusable item title=%r", item.get("trackName") if isinstance(item, dict) else item)
                continue
            songs.append(raw)
        return songs
