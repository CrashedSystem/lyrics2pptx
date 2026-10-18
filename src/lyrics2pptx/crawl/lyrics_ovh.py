"""Reference collector adapter — a key-free public JSON API (L2).

Chosen as the default only because it needs no credential and gives a working
minimum example (docs/plan/03 §결정 2). Korean coverage may be poor; that is an
honest property of the source, not a design defect — swap the adapter module.

Query interpretation (docs/plan/03 §참조 어댑터):

* ``"artist - title"`` → a single direct lookup of that artist/title.
* A bare string      → discovery via the public ``/suggest`` end-point, then a
  lyrics lookup for each candidate (sequential; the politeness layer enforces
  delays and backoff).

Offline mode (``LYRICS2PPTX_FIXTURE_DIR`` set) always performs exactly one
lookup so a single saved response drives the whole adapter. The fixture name is
``<slug(query)>.json`` when that file exists, otherwise
``lyrics_ovh_sample.json``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .. import config
from . import httputil
from .base import RawSong, SourceNotFoundError

__all__ = ["LyricsOvhSource", "BASE_URL", "SAMPLE_FIXTURE"]

log = logging.getLogger(__name__)

BASE_URL = "https://api.lyrics.ovh"
SAMPLE_FIXTURE = "lyrics_ovh_sample.json"

_SEPARATOR = re.compile(r"\s+-\s+")
_SLUG = re.compile(r"[^a-z0-9]+")


def _parse_query(query: str) -> tuple[str, str]:
    """Split ``"artist - title"``; a bare query yields ``("", query)``."""
    parts = _SEPARATOR.split(query, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", query.strip()


def _fixture_name(query: str) -> str:
    base = config.FIXTURE_DIR
    slug = _SLUG.sub("_", query.lower()).strip("_")
    if base is not None and slug and (Path(base) / f"{slug}.json").is_file():
        return f"{slug}.json"
    return SAMPLE_FIXTURE


class LyricsOvhSource:
    """``Source`` implementation for ``https://api.lyrics.ovh``."""

    name = "lyrics_ovh"

    def search(self, query: str, *, limit: int = 5) -> list[RawSong]:
        text = (query or "").strip()
        if not text:
            raise SourceNotFoundError("검색어가 비어 있습니다")
        artist, title = _parse_query(text)
        log.debug("query=%s artist=%s", text[:60], artist[:30])

        if config.FIXTURE_DIR is not None:
            song = self._fetch_lyrics(
                artist=artist,
                title=title or text,
                fallback_title=title or text,
                fixture=_fixture_name(text),
            )
            log.info("source=%s hits=%d chosen_index=%d", self.name, 1, 0)
            return [song]

        if artist:
            songs = [
                self._fetch_lyrics(artist=artist, title=title, fallback_title=title, fixture=None)
            ]
            log.info("source=%s hits=%d chosen_index=%d", self.name, len(songs), 0)
            return songs

        songs = self._discover(text, limit=limit)
        if not songs:
            raise SourceNotFoundError("검색 결과가 없습니다")
        log.info("source=%s hits=%d chosen_index=%d", self.name, len(songs), 0)
        return songs

    def _discover(self, query: str, *, limit: int) -> list[RawSong]:
        """Use the suggestion end-point, then fetch lyrics for each candidate."""
        try:
            payload = httputil.get_json(f"{BASE_URL}/suggest/{httputil.urlquote(query)}")
        except SourceNotFoundError:
            return []
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            return []

        songs: list[RawSong] = []
        for item in data:
            if len(songs) >= limit:
                break
            if not isinstance(item, dict):
                continue
            candidate_title = str(item.get("title") or "").strip()
            artist_field = item.get("artist")
            if isinstance(artist_field, dict):
                candidate_artist = str(artist_field.get("name") or "").strip()
            else:
                candidate_artist = str(artist_field or "").strip()
            if not candidate_title:
                continue
            try:
                songs.append(
                    self._fetch_lyrics(
                        artist=candidate_artist,
                        title=candidate_title,
                        fallback_title=candidate_title,
                        fixture=None,
                    )
                )
            except (SourceNotFoundError,) as exc:
                log.debug("skip candidate title=%s reason=%s", candidate_title, type(exc).__name__)
        return songs

    def _fetch_lyrics(
        self,
        *,
        artist: str,
        title: str,
        fallback_title: str,
        fixture: str | None,
    ) -> RawSong:
        url = f"{BASE_URL}/v1/{httputil.urlquote(artist)}/{httputil.urlquote(title)}"
        payload = httputil.get_json(url, fixture=fixture)
        lyrics = payload.get("lyrics")
        if not isinstance(lyrics, str) or not lyrics.strip():
            raise SourceNotFoundError("가사를 찾지 못했습니다")
        resolved_title = payload.get("title") if isinstance(payload.get("title"), str) else None
        resolved_artist = payload.get("artist") if isinstance(payload.get("artist"), str) else None
        return RawSong(
            title=(resolved_title or fallback_title or title).strip(),
            artist=(resolved_artist or artist).strip() or None,
            lyrics=lyrics,
            source_name=self.name,
            source_url=url,
        )
