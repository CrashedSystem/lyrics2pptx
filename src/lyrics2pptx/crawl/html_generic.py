"""Generic HTML adapter skeleton — DISABLED by default (L2).

!!! WARNING !!!
    This adapter is NOT registered in ``crawl/__init__.SOURCES`` and therefore
    is not reachable unless you deliberately edit that registry. Scraping a
    site may violate its terms of service and the lyrics' copyright. Before
    enabling it you must:

      1. read the target site's ``robots.txt`` (this module still enforces it
         through ``crawl.httputil``, which fails closed), and
      2. read the target site's terms of service and confirm that automated
         retrieval and personal use are permitted.

    Nothing here is legal advice; the responsibility is yours.

Implementation notes (docs/plan/03 §HTML 어댑터 스켈레톤 규칙):

* Selectors are module constants so a user can retarget the adapter without
  touching logic.
* Parsing uses ``re`` only — no BeautifulSoup, to keep the dependency count at
  zero. Tags are stripped and whitespace collapsed.
* Every request goes through ``crawl.httputil`` (robots + UA + delay).
"""

from __future__ import annotations

import html as html_module
import logging
import re

from . import httputil
from .base import RawSong, SourceNotFoundError, SourceUnavailableError

__all__ = ["HtmlGenericSource", "SEARCH_URL", "SELECTORS", "RESULT_PATTERN"]

log = logging.getLogger(__name__)

# --- user-editable configuration -------------------------------------------
# Fill these in for the site you are allowed to use.
SEARCH_URL = ""  # e.g. "https://example.com/search?q={query}"
SELECTORS = {
    "result_link": "",  # regex matching one result row -> captures the detail URL
    "title": "",  # regex capturing the song title from a detail page
    "artist": "",  # regex capturing the artist name from a detail page
    "lyrics_container": "",  # regex capturing the lyrics block from a detail page
}
# Fallback result-row pattern used when ``SELECTORS["result_link"]`` is empty.
RESULT_PATTERN = r"<a[^>]+href=\"([^\"]+)\""

_TAG = re.compile(r"<[^>]+>")
_BREAK = re.compile(r"(?i)<br\s*/?>|</p>")
_WS = re.compile(r"[ \t]+")


def _strip_tags(fragment: str) -> str:
    text = _BREAK.sub("\n", fragment)
    text = _TAG.sub("", text)
    text = html_module.unescape(text)
    lines = [_WS.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _first_group(pattern: str, text: str) -> str | None:
    if not pattern:
        return None
    match = re.search(pattern, text, re.DOTALL)
    if match is None:
        return None
    if match.groups():
        return _strip_tags(match.group(1)).strip() or None
    return _strip_tags(match.group(0)).strip() or None


class HtmlGenericSource:
    """Disabled-by-default HTML ``Source`` skeleton."""

    name = "html_generic"

    def search(self, query: str, *, limit: int = 5) -> list[RawSong]:
        log.warning(
            "html_generic 은 기본 비활성 어댑터입니다. "
            "대상 사이트의 robots.txt 와 이용약관을 확인한 뒤 활성화하세요."
        )
        if not SEARCH_URL:
            raise SourceUnavailableError(
                "html_generic 어댑터가 설정되지 않았습니다 (SEARCH_URL 비어 있음)"
            )

        search_url = SEARCH_URL.format(query=httputil.urlquote(query))
        page = httputil.get_text(search_url)
        link_pattern = SELECTORS.get("result_link") or RESULT_PATTERN
        links = re.findall(link_pattern, page, re.DOTALL)
        if not links:
            raise SourceNotFoundError("검색 결과가 없습니다")

        songs: list[RawSong] = []
        for link in links[:limit]:
            detail_url = link if str(link).startswith("http") else _join_url(search_url, str(link))
            songs.append(self._parse_detail(detail_url))
        return songs

    def _parse_detail(self, url: str) -> RawSong:
        page = httputil.get_text(url)
        lyrics_raw = _first_group(SELECTORS.get("lyrics_container", ""), page)
        if not lyrics_raw:
            raise SourceNotFoundError("가사를 찾지 못했습니다")
        title = _first_group(SELECTORS.get("title", ""), page) or url.rsplit("/", 1)[-1]
        artist = _first_group(SELECTORS.get("artist", ""), page)
        return RawSong(
            title=title,
            artist=artist,
            lyrics=lyrics_raw,
            source_name=self.name,
            source_url=url,
        )


def _join_url(base: str, target: str) -> str:
    if target.startswith("//"):
        scheme = base.split("://", 1)[0]
        return f"{scheme}:{target}"
    if target.startswith("/"):
        root = base.split("://", 1)
        if len(root) == 2:
            host = root[1].split("/", 1)[0]
            return f"{root[0]}://{host}{target}"
    return target
