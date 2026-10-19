"""HTTP politeness layer — the single gateway for all network traffic (L1).

Every rule the project enforces about crawling lives here, so adapters cannot
bypass it: they never import ``urllib`` themselves (rule P7), they only call
``get_json`` / ``get_text``.

Enforced behaviour (docs/arch/06 §2, docs/plan/03 §HTTP 예절 레이어):

* robots.txt is consulted before any request; a ``Disallow`` verdict or an
  unreachable robots.txt blocks the request with ``SourceBlockedError``
  (fail closed). A ``404``/``410`` for ``robots.txt`` means "no robots file"
  and therefore **allows** access (RFC 9309 §2.3.1.3); any other HTTP error
  (401/403/5xx) stays blocked.
* ``User-Agent`` is assembled in exactly one place and never spoofs a browser.
* A host-level minimum delay is kept between consecutive requests.
* Redirects are followed manually, one hop at a time, and the robots verdict
  plus the host delay are re-applied to **every** hop — a single ``30x``
  response cannot be used to reach a host whose robots.txt disallows us
  (docs/arch/06 §2). Chains longer than ``MAX_REDIRECTS`` and redirect loops
  fail closed with ``SourceUnavailableError``.
* 429/5xx/timeout are retried with exponential backoff (1s, 2s, 4s, up to
  ``LYRICS2PPTX_MAX_RETRIES`` retries); 403/404 fail immediately. A caller may
  pass ``retries=0`` to skip the backoff entirely — lrclib does, because a dead
  fallback source must not multiply the delay (ADR-030).
* Requests are strictly sequential — no thread or process pools exist here.

Offline mode: when ``LYRICS2PPTX_FIXTURE_DIR`` is set, ``get_json``/``get_text``
read a saved response instead of performing a request, which is how the whole
collector is tested without network access (docs/plan/03 §오프라인 모드).
Fixture resolution rules:

* ``get_json(url, fixture="name.json")`` reads ``<FIXTURE_DIR>/name.json``.
* Without an explicit ``fixture``, the name is derived from the URL as
  ``<host slug>_<path slug>.json``; if that file is absent the call raises
  ``SourceUnavailableError``.
"""

from __future__ import annotations

import json
import logging
import socket
import time
from pathlib import Path
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .. import config
from ..errors import ConfigError
from .base import SourceBlockedError, SourceUnavailableError

__all__ = [
    "DEFAULT_UA",
    "DEFAULT_DELAY",
    "MAX_REDIRECTS",
    "MAX_RESPONSE_BYTES",
    "allowed_by_robots",
    "get_json",
    "get_json_list",
    "get_text",
    "host_of",
    "is_http_url",
    "sleep_between",
    "reset_http_state",
]

log = logging.getLogger(__name__)

DEFAULT_UA = config.DEFAULT_UA
DEFAULT_DELAY = 2.0
MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # cap hostile / oversized responses
MAX_REDIRECTS = 5  # hop cap: a longer chain fails closed instead of being followed

_QUERY_LOG_LIMIT = 60

# host -> monotonic timestamp of the last request (politeness delay tracking)
_last_request_at: dict[str, float] = {}
# host -> (expires_at, allowed) robots verdict cache
_robots_cache: dict[str, tuple[float, bool]] = {}


def reset_http_state() -> None:
    """Forget delay bookkeeping and robots verdicts (used by tests and fixtures)."""
    _last_request_at.clear()
    _robots_cache.clear()


def _source_slug(text: str) -> str:
    out = [ch.lower() if ch.isalnum() else "_" for ch in text]
    return "".join(out).strip("_")


def _fixture_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    slug = _source_slug(f"{parsed.netloc}{parsed.path}")
    return f"{slug or 'default'}.json"


def _read_fixture(url: str, fixture: str | None, *, parse: str) -> str:
    base = config.FIXTURE_DIR
    if base is None:  # pragma: no cover - guarded by the callers
        raise ConfigError("fixture 디렉터리가 설정되지 않았습니다")
    names: list[str] = []
    if fixture:
        names.append(fixture)
    names.append(_fixture_name_from_url(url) if parse == "json" else _fixture_name_from_url(url))
    for name in names:
        candidate = Path(base) / name
        if candidate.is_file():
            log.debug("fixture hit name=%s", name)
            return candidate.read_text(encoding="utf-8")
    raise SourceUnavailableError(f"저장된 응답(fixture)을 찾지 못했습니다: {names[0]}")


def _log_target(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    path = parsed.path
    if parsed.query:
        fragment = parsed.query[:_QUERY_LOG_LIMIT]
        path = f"{path}?{fragment}"
    return parsed.netloc, path


def _encode_url(url: str) -> str:
    """Percent-encode non-ASCII characters so ``urllib`` can build a request.

    Korean blog permalinks (``https://thinkjesus.kr/지금은-엘리야-때처럼/``) reach
    us as IRIs; ``urllib`` encodes headers as ASCII and raises
    ``UnicodeEncodeError`` unless the path is escaped first. Existing ``%XX``
    escapes are preserved (``%`` is in ``safe``), so this is idempotent.
    """
    return quote(url, safe=":/?#[]@!$&'()*+,;=%")


def _build_request(url: str, *, ua: str) -> Request:
    return Request(
        _encode_url(url),
        headers={
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ko,en;q=0.8",
        },
        method="GET",
    )


class _GuardedRedirectHandler(HTTPRedirectHandler):
    """Follow redirects one hop at a time, re-applying the politeness gate.

    ``urlopen`` follows redirects internally, so guarding only the URL the
    caller passed would let one ``30x`` hop reach a host that ``robots.txt``
    forbids (or that still owes us a delay). Every hop therefore passes through
    ``_guard`` again, which keeps robots/delay un-bypassable from the adapter
    layer (docs/arch/06 §2).
    """

    def __init__(self, *, ua: str) -> None:
        super().__init__()
        self._ua = ua
        self._hops = 0
        self._visited: set[str] = set()

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802 - stdlib hook name
        self._visited.add(req.full_url)
        if newurl in self._visited:
            raise SourceUnavailableError(f"리다이렉트 루프가 감지되었습니다: {newurl}")
        self._hops += 1
        if self._hops > MAX_REDIRECTS:
            raise SourceUnavailableError(
                f"리다이렉트가 최대 허용 횟수({MAX_REDIRECTS})를 초과했습니다: {newurl}"
            )
        _guard(newurl, ua=self._ua)
        return super().redirect_request(req, fp, code, msg, headers, _encode_url(newurl))


def _open_and_read(url: str, *, ua: str, timeout: float) -> bytes:
    # A fresh opener (and hop budget) per attempt; the redirect handler is the
    # only place that decides whether the next hop may be requested at all.
    opener = build_opener(_GuardedRedirectHandler(ua=ua))
    with opener.open(_build_request(url, ua=ua), timeout=timeout) as response:  # noqa: S310
        # Read one byte past the cap so oversized bodies can be detected.
        blob = response.read(MAX_RESPONSE_BYTES + 1)
    if len(blob) > MAX_RESPONSE_BYTES:
        raise SourceUnavailableError("응답이 최대 허용 크기를 초과했습니다")
    return blob


def allowed_by_robots(url: str, *, ua: str = DEFAULT_UA) -> bool:
    """Return True when ``robots.txt`` permits fetching ``url`` with ``ua``.

    A ``404``/``410`` response means "no robots file" and therefore allows
    access (RFC 9309 §2.3.1.3); every other unreachable or unparsable
    robots.txt is treated as a denial (fail closed). Verdicts are cached per
    host for ``config.ROBOTS_CACHE_TTL``.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return True
    host = parsed.netloc
    now = monotonic()
    cached = _robots_cache.get(host)
    if cached is not None and cached[0] > now:
        return cached[1]

    robots_url = f"{parsed.scheme}://{host}/robots.txt"
    try:
        blob = _open_and_read(robots_url, ua=ua, timeout=config.HTTP_TIMEOUT)
        text = blob.decode("utf-8", errors="replace")
        allowed = _robots_allows(text, url, ua)
    except HTTPError as exc:
        # RFC 9309 §2.3.1.3: an unavailable robots file (404/410) means there
        # are no rules, so access is allowed. Every other HTTP status (401,
        # 403, 5xx) is not evidence of permission and stays fail-closed.
        if int(exc.code) in (404, 410):
            log.info("robots absent host=%s status=%s -> allowed", host, exc.code)
            allowed = True
        else:
            log.warning("robots unreachable host=%s status=%s -> blocked", host, exc.code)
            allowed = False
    except (URLError, OSError, SourceUnavailableError, ValueError) as exc:
        log.warning("robots unreachable host=%s reason=%s -> blocked", host, type(exc).__name__)
        allowed = False

    _robots_cache[host] = (now + config.ROBOTS_CACHE_TTL, allowed)
    log.info("robots host=%s allowed=%s", host, allowed)
    return allowed


def _robots_allows(robots_text: str, url: str, ua: str) -> bool:
    """Evaluate robots rules without depending on the private parser API surface."""
    from urllib.robotparser import RobotFileParser

    parser = RobotFileParser()
    parser.parse(robots_text.splitlines())
    try:
        return parser.can_fetch(ua, url)
    except (ValueError, AssertionError):  # pragma: no cover - defensive
        return False


def _respect_delay(url: str) -> None:
    host = urlparse(url).netloc
    previous = _last_request_at.get(host)
    if previous is not None:
        wait = config.HTTP_DELAY - (monotonic() - previous)
        if wait > 0:
            log.debug("delay host=%s wait=%.2fs", host, wait)
            time.sleep(wait)
    _last_request_at[host] = monotonic()


def _guard(url: str, *, ua: str) -> None:
    """The single gate every outbound request must pass through."""
    if config.FIXTURE_DIR is not None:  # offline mode: no network at all
        return
    if not allowed_by_robots(url, ua=ua):
        raise SourceBlockedError(f"robots.txt 가 이 경로를 금지합니다: {url}")
    _respect_delay(url)


def _request_with_retry(
    url: str,
    *,
    ua: str,
    timeout: float,
    parse: str,
    retries: int | None = None,
) -> str:
    host, path = _log_target(url)
    total_retries = config.MAX_RETRIES if retries is None else retries
    attempts = max(1, total_retries + 1)  # 1 initial attempt + N retries
    last_reason = "unknown"
    for attempt in range(1, attempts + 1):
        log.info("GET host=%s path=%s attempt=%d", host, path, attempt)
        try:
            blob = _open_and_read(url, ua=ua, timeout=timeout)
        except HTTPError as exc:
            status = int(exc.code)
            if status in (403, 404):
                raise SourceBlockedError(
                    f"이 소스가 요청을 거부했습니다 (HTTP {status})"
                ) from exc
            if status == 429 or 500 <= status <= 599:
                last_reason = f"HTTP {status}"
                if attempt >= attempts:
                    break
                backoff = 2 ** (attempt - 1)
                log.warning("retry attempt=%d status=%s backoff=%ds", attempt, status, backoff)
                time.sleep(backoff)
                continue
            raise SourceUnavailableError(f"예상치 못한 응답입니다 (HTTP {status})") from exc
        except (URLError, socket.timeout, TimeoutError, OSError) as exc:
            last_reason = type(exc).__name__
            if attempt >= attempts:
                break
            backoff = 2 ** (attempt - 1)
            log.warning("retry attempt=%d status=%s backoff=%ds", attempt, last_reason, backoff)
            time.sleep(backoff)
            continue

        try:
            return blob.decode("utf-8")
        except UnicodeDecodeError:
            return blob.decode("utf-8", errors="replace")

    raise SourceUnavailableError(f"일시적으로 수집에 실패했습니다 (사유: {last_reason})")


def get_json(
    url: str,
    *,
    ua: str = DEFAULT_UA,
    timeout: float | None = None,
    retries: int | None = None,
    fixture: str | None = None,
) -> dict:
    """Return the decoded JSON body of ``url`` (or a saved fixture).

    ``retries`` overrides ``LYRICS2PPTX_MAX_RETRIES`` for this call only
    (``0`` disables the backoff); ``None`` keeps the configured policy.
    """
    effective_timeout = config.HTTP_TIMEOUT if timeout is None else timeout
    if config.FIXTURE_DIR is not None:
        raw = _read_fixture(url, fixture, parse="json")
    else:
        _guard(url, ua=ua)
        raw = _request_with_retry(
            url, ua=ua, timeout=effective_timeout, parse="json", retries=retries
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SourceUnavailableError("응답을 해석하지 못했습니다 (JSON 아님)") from exc
    if not isinstance(payload, dict):
        raise SourceUnavailableError("응답 형식이 예상과 다릅니다 (객체 아님)")
    return payload


def get_json_list(
    url: str,
    *,
    ua: str = DEFAULT_UA,
    timeout: float | None = None,
    retries: int | None = None,
    fixture: str | None = None,
) -> list:
    """Return the decoded JSON **array** body of ``url`` (or a saved fixture).

    ``get_json`` rejects non-object payloads, so an array-returning end-point
    (lrclib ``/api/search``, ADR-017) needs its own accessor. The politeness
    gate, retry policy, and offline fixture hook are identical to ``get_json``;
    only the final ``isinstance`` check differs. ``retries`` is passed through
    unchanged (lrclib sends ``0`` — ADR-030).
    """
    effective_timeout = config.HTTP_TIMEOUT if timeout is None else timeout
    if config.FIXTURE_DIR is not None:
        raw = _read_fixture(url, fixture, parse="json")
    else:
        _guard(url, ua=ua)
        raw = _request_with_retry(
            url, ua=ua, timeout=effective_timeout, parse="json", retries=retries
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SourceUnavailableError("응답을 해석하지 못했습니다 (JSON 아님)") from exc
    if not isinstance(payload, list):
        raise SourceUnavailableError("응답 형식이 예상과 다릅니다 (배열 아님)")
    return payload


def get_text(
    url: str,
    *,
    ua: str = DEFAULT_UA,
    timeout: float | None = None,
    retries: int | None = None,
    fixture: str | None = None,
) -> str:
    """Return the text body of ``url`` (or a saved fixture)."""
    effective_timeout = config.HTTP_TIMEOUT if timeout is None else timeout
    if config.FIXTURE_DIR is not None:
        return _read_fixture(url, fixture, parse="text")
    _guard(url, ua=ua)
    return _request_with_retry(
        url, ua=ua, timeout=effective_timeout, parse="text", retries=retries
    )


def sleep_between(seconds: float | None = None) -> None:
    """Sleep for the configured delay (public helper for sequential fetches)."""
    time.sleep(config.HTTP_DELAY if seconds is None else seconds)


def urlquote(component: str) -> str:
    """Percent-encode a single URL path component (helper for adapters)."""
    return quote(component, safe="")


def host_of(url: str) -> str:
    """Return the lower-cased host of ``url`` without a leading ``www.``.

    Adapters must not parse URLs themselves (rule P7 forbids importing
    ``urllib`` outside this module), so the small bits of URL reasoning they
    need — the display host of a page, and whether a search hit is a fetchable
    ``http(s)`` URL — are exposed here.
    """
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def is_http_url(url: str) -> bool:
    """True when ``url`` is an absolute ``http(s)`` URL that can be fetched."""
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)
