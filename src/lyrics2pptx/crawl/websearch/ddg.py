"""DuckDuckGo HTML search backend (L2).

ADR-018 chose this backend because it is the only general web search whose
``robots.txt`` allows an automated client: both ``html.duckduckgo.com`` and
``lite.duckduckgo.com`` answer ``User-agent: * / Allow: /`` (the mainstream
alternatives disallow ``/search``). The backend is used as an *index* only —
result snippets are never treated as lyrics; the collector follows each result
URL and reads the page itself.

Two end-points are tried in order. The ``lite`` page is a different, simpler
markup, so parsing here accepts any anchor and only unwraps DuckDuckGo's
``/l/?uddg=<target>`` redirect, which works for both shapes.
"""

from __future__ import annotations

import html as html_module
import logging
import re
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from .. import httputil
from ..base import SourceError, SourceNotFoundError
from ...errors import ConfigError
from .base import SearchResult

__all__ = ["DdgSearch", "parse_results"]

log = logging.getLogger(__name__)

HTML_ENDPOINT = "https://html.duckduckgo.com/html/"
LITE_ENDPOINT = "https://lite.duckduckgo.com/lite/"

_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_SNIPPET_RE = re.compile(
    r"""<[^>]*class\s*=\s*["'][^"']*result(?:__snippet|-snippet)[^"']*["'][^>]*>(.*?)</(?:a|div|td)>""",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_DDG_HOST_SUFFIX = "duckduckgo.com"


def _text(fragment: str) -> str:
    """Strip tags/entities from an HTML fragment and collapse whitespace."""
    return _SPACE_RE.sub(" ", html_module.unescape(_TAG_RE.sub(" ", fragment))).strip()


def _unwrap(href: str) -> str:
    """Resolve a DuckDuckGo result link to its real target URL."""
    value = html_module.unescape(href).strip()
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlparse(value)
    if parsed.netloc.endswith(_DDG_HOST_SUFFIX) and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return unquote(target)
    return value


def parse_results(page: str) -> list[SearchResult]:
    """Extract result links from a DuckDuckGo HTML or lite results page.

    Only links that resolve to a third-party ``http(s)`` URL are kept, so the
    page's own navigation, settings and pagination links are ignored.
    """
    snippets = [_text(fragment) for fragment in _SNIPPET_RE.findall(page)]
    results: list[SearchResult] = []
    seen: set[str] = set()
    for attributes, inner in _ANCHOR_RE.findall(page):
        match = _HREF_RE.search(attributes)
        if match is None:
            continue
        url = _unwrap(match.group(1))
        if not url.startswith(("http://", "https://")):
            continue
        host = urlparse(url).netloc
        if host == _DDG_HOST_SUFFIX or host.endswith("." + _DDG_HOST_SUFFIX):
            continue
        if url in seen:
            continue
        title = _text(inner)
        if not title:
            continue
        seen.add(url)
        index = len(results)
        snippet = snippets[index] if index < len(snippets) else ""
        results.append(SearchResult(title=title, url=url, snippet=snippet))
    return results


class DdgSearch:
    """Key-free search backend over DuckDuckGo's robots-allowed HTML pages."""

    name = "ddg"
    endpoints: tuple[str, ...] = (HTML_ENDPOINT, LITE_ENDPOINT)

    def _fetch(self, url: str) -> str:
        """Fetch one results page through the politeness layer (test seam)."""
        return httputil.get_text(url)

    def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        text = (query or "").strip()
        if not text:
            raise ConfigError("검색어가 비어 있습니다")
        wanted = max(1, limit)

        last_error: SourceError | None = None
        for endpoint in self.endpoints:
            url = f"{endpoint}?q={quote_plus(text)}"
            try:
                page = self._fetch(url)
            except SourceError as exc:
                log.info("ddg endpoint failed endpoint=%s reason=%s", endpoint, type(exc).__name__)
                last_error = exc
                continue
            results = parse_results(page)
            if results:
                log.info("ddg results=%d endpoint=%s", len(results), endpoint)
                return results[:wanted]
            last_error = SourceNotFoundError(f"검색 결과가 없습니다: {text}")

        raise last_error or SourceNotFoundError(f"검색 결과가 없습니다: {text}")
