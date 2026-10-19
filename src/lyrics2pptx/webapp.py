"""Local single-user web app (L4).

An ``http.server`` front end for ``pipeline`` (docs/arch/05 §2, docs/plan/06
§2): ``/``, ``/static/*``, ``/api/search``, ``/api/search/start``,
``/api/search/job``, ``/api/slides``, ``/api/slides/{index}/thumbnail``,
``/api/preview``, ``/api/generate``, ``/api/reveal``, and the session endpoints
``/api/ping`` and ``/api/shutdown`` (ADR-032).
Nothing here contains domain logic: requests are validated, handed to
``pipeline.preview``/``pipeline.run``/``search_hits``, and the result is streamed
back.

``/api/search/start`` + ``/api/search/job`` exist because a collect can take ten
seconds or more (a blog search fetches several pages politely). The single-shot
``/api/search`` is kept for scripts and tests; the browser starts a job and polls
it so it can show *which page* is being read instead of a frozen "검색 중…".

Session lifetime (ADR-032, docs/arch/05 §6): the server is meant to *retire
itself* — the page heartbeats ``/api/ping``, tell us when the tab is closing, and
the watchdog stops the process after ``config.WEBUI_TAB_GRACE`` (tab gone),
``config.WEBUI_IDLE`` (nobody using it) or ``config.WEBUI_MAX_LIFETIME``.
``main`` publishes port + instance id in ``config.WEBUI_STATE_FILE`` so
``lyrics2pptx.vbs`` reopens the running instance instead of starting a second one.
``/api/shutdown`` (the in-page "종료" button) is guarded by a per-process token,
so a random web page cannot kill the user's server with a cross-site POST.

``launch`` is the same server seen from the *packaged* side (ADR-033): the
double-clicked ``lyrics2pptx.exe`` has no console and no launcher script, so the
"reuse a live instance, otherwise start one and open the tab" dance the ``.vbs``
performs lives here as ``reuse_url`` + ``launch``.

Security boundary (docs/arch/06 §3): bound to ``127.0.0.1``, static serving is
containment-checked against ``web/``, and request bodies are capped at
``config.REQUEST_MAX_BYTES``. Builds are serialized by one global lock because
the template is a 37 MB in-process resource (AD-14).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from uuid import uuid4

from . import __version__, config, pipeline
from .errors import ConfigError, Lyrics2PptxError, OutputWriteError
from .pptx import thumbnails

log = logging.getLogger("lyrics2pptx.webapp")

__all__ = [
    "Lyrics2PptxHandler",
    "Session",
    "build_server",
    "clear_state",
    "launch",
    "main",
    "read_state",
    "reuse_url",
    "start_watchdog",
    "write_state",
]

# One build at a time: the template and output directory are process-wide.
_BUILD_LOCK = threading.Lock()

# ADR-033: the packaged exe has no console, so ``launch`` sends both streams here
# (next to ``output/``-relative artifacts, where a user can find them).
_LAUNCH_LOG_NAME = "launch.log"
#: Rotate once it passes this size: one run writes a few dozen lines, so a
#: megabyte is months of history — and the file must never grow forever.
_LAUNCH_LOG_MAX_BYTES = 1_048_576

_PPTX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)
_CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".ico": "image/x-icon",
}
_DEFAULT_CONTENT_TYPE = "application/octet-stream"

_INTERNAL_ERROR = {
    "error": {
        "code": "INTERNAL",
        "message": "처리하지 못한 오류가 발생했습니다.",
        "recoverable": False,
    }
}
_NOT_FOUND_ERROR = {
    "error": {
        "code": "NOT_FOUND",
        "message": "요청한 경로를 찾지 못했습니다.",
        "recoverable": False,
    }
}
_TOO_LARGE_ERROR = {
    "error": {
        "code": "PAYLOAD_TOO_LARGE",
        "message": "요청 본문이 너무 큽니다 (최대 1 MiB).",
        "recoverable": False,
    }
}
_FORBIDDEN_ERROR = {
    "error": {
        "code": "FORBIDDEN",
        "message": "이 창에서 보낸 요청이 아닙니다. 서버를 종료하려면 열려 있는 페이지에서 눌러 주세요.",
        "recoverable": False,
    }
}


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _is_within(base: Path, target: Path) -> bool:
    try:
        return target == base or target.is_relative_to(base)
    except AttributeError:  # pragma: no cover - Python < 3.9
        return str(target).startswith(str(base))


def _coerce_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


# --- session lifetime (ADR-032) -------------------------------------------
# The page tells us two different things and they must not be confused:
#
#   * "I am still here"   - ``/api/ping``          -> heartbeat
#   * "I am going away"   - ``/api/ping?closing=1``-> tab closed (pagehide beacon)
#
# A heartbeat therefore *cancels* a pending close (a reload or a return to the
# tab) but is **not** user activity: only real requests (search, slides, preview,
# generate, download) count, so a tab left open on a desk still retires itself.
# We deliberately do not require a periodic heartbeat — browsers throttle and
# eventually freeze timers in background tabs, and a frozen tab must not look
# like a closed one.

_TOKEN_HEADER = "X-Lyrics2Pptx-Token"
_HEARTBEAT_PATH = "/api/ping"
_SESSION_PLACEHOLDER = b"<!-- LYRICS2PPTX_SESSION -->"
_STATE_FILENAME = "webui.state"
_STOP_GRACE_SECONDS = 1.0  # let an in-flight response (a download) reach the browser


def _instance_id(token: str) -> str:
    """Public half of the token: proves identity without leaking the secret."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _is_true(values: list[str] | None) -> bool:
    return bool(values) and values[0].strip().lower() in ("1", "true", "yes", "on")


def _reveal_in_explorer(target: Path) -> None:
    """Show ``target`` in the OS file manager (best effort, never fatal).

    Local tool for one user: opening the folder is the friendliest way to answer
    "결과를 어디서 받지?". A missing desktop integration only costs a log line —
    the file is always in ``output/`` and downloadable from the page.
    """
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", str(target)])
        elif sys.platform == "darwin":  # pragma: no cover - not a target platform
            subprocess.Popen(["open", "-R", str(target)])
        else:  # pragma: no cover - not a target platform
            subprocess.Popen(["xdg-open", str(target.parent)])
    except OSError as exc:  # pragma: no cover - desktop dependent
        log.warning("탐색기를 열지 못했습니다 (%s): %s", target, exc)


@dataclass
class Session:
    """Process-lifetime bookkeeping shared by the page, the launcher, the watchdog.

    Times are ``time.monotonic`` seconds; the defaults come from ``config`` in
    ``build_server`` so a test can shrink a timeout on the instance directly.
    """

    token: str
    tab_grace: float = 180.0
    idle: float = 1200.0
    max_lifetime: float = 10800.0
    auto_shutdown: bool = True
    started: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    closed_at: float | None = None
    reason: str | None = None
    in_flight: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def instance(self) -> str:
        return _instance_id(self.token)

    # --- bookkeeping ------------------------------------------------------

    def enter(self) -> None:
        with self._lock:
            self.in_flight += 1

    def leave(self) -> None:
        with self._lock:
            self.in_flight = max(0, self.in_flight - 1)

    def note_activity(self) -> None:
        """A real request: the user is working, so the idle rule restarts."""
        with self._lock:
            self.last_activity = time.monotonic()

    def heartbeat(self, *, keep: bool = False) -> None:
        """The page is alive; drop any pending "the tab closed" decision."""
        with self._lock:
            self.closed_at = None
            if keep:
                self.last_activity = time.monotonic()

    def note_tab_closed(self) -> None:
        with self._lock:
            self.closed_at = time.monotonic()

    def busy(self, *, jobs: int = 0) -> bool:
        with self._lock:
            return self.in_flight > 0 or jobs > 0

    def request_stop(self, reason: str) -> None:
        with self._lock:
            self.reason = reason

    # --- decisions --------------------------------------------------------

    def stop_reason(self, *, jobs: int = 0, now: float | None = None) -> str | None:
        """Return why the server should stop now, or ``None`` to keep serving.

        An explicit request wins over the timeouts, but never interrupts work in
        progress: a build or a download finishes first (it is retried on the next
        tick of the watchdog).
        """
        moment = time.monotonic() if now is None else now
        with self._lock:
            if self.in_flight > 0 or jobs > 0:
                return None
            if self.reason is not None:
                return self.reason
            if (
                self.closed_at is not None
                and self.tab_grace > 0
                and moment - self.closed_at >= self.tab_grace
            ):
                return "탭을 닫아서"
            if self.idle > 0 and moment - self.last_activity >= self.idle:
                return "오랫동안 사용이 없어서"
            if self.max_lifetime > 0 and moment - self.started >= self.max_lifetime:
                return "최대 가동 시간을 넘어서"
        return None

    def snapshot(self, *, jobs: int = 0) -> dict:
        """JSON-ready view for ``/api/ping`` (also drives the in-page status chip)."""
        moment = time.monotonic()
        with self._lock:
            idle_left = (
                None
                if self.idle <= 0
                else round(max(0.0, self.idle - (moment - self.last_activity)))
            )
            lifetime_left = (
                None
                if self.max_lifetime <= 0
                else round(max(0.0, self.max_lifetime - (moment - self.started)))
            )
            return {
                "instance": self.instance,
                "auto_shutdown": self.auto_shutdown,
                "busy": self.in_flight > 0 or jobs > 0,
                "tab_grace_s": self.tab_grace,
                "idle_timeout_s": self.idle,
                "idle_seconds_left": idle_left,
                "lifetime_seconds_left": lifetime_left,
                "closing": self.closed_at is not None,
                "uptime_s": round(moment - self.started),
                "shutdown_reason": self.reason,
            }


# --- search jobs ----------------------------------------------------------
# ``GET /api/search`` blocks for as long as the collect takes, and a blog collect
# fetches several pages with a politeness delay between them, so the browser
# shows the user "검색 중…" for ten seconds with nothing to look at. The browser
# therefore starts a job and polls it, and the collector's progress lines
# (``crawl.progress``) are recorded per job so the bar can say *which page* is
# being read.
#
# Jobs are process-local and disposable: they are pruned by age and by count, and
# a job the client never polls simply expires. Nothing here is durable state —
# the one-shot ``/api/search`` remains the contract for scripts and tests.

_JOB_TTL_SECONDS = 600.0
_JOB_MAX = 32
_JOB_STEPS_KEPT = 40
_jobs: dict[str, "_SearchJob"] = {}
_jobs_lock = threading.Lock()


@dataclass
class _SearchJob:
    """One in-flight (or just-finished) collect the browser polls for progress."""

    id: str
    query: str
    source: str
    state: str = "running"  # running | done | error
    steps: list[dict] = field(default_factory=list)
    latest: dict | None = None
    results: list[dict] | None = None
    error: dict | None = None
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None

    def snapshot(self) -> dict:
        """Return a JSON-ready view; safe to call from the request thread."""
        with _jobs_lock:
            now = self.finished_at if self.finished_at is not None else time.monotonic()
            payload: dict = {
                "job_id": self.id,
                "state": self.state,
                "elapsed_ms": int((now - self.started_at) * 1000),
                "steps": [dict(step) for step in self.steps[-_JOB_STEPS_KEPT:]],
                "progress": dict(self.latest) if self.latest else None,
            }
            if self.state == "done":
                payload["results"] = list(self.results or [])
            elif self.state == "error":
                payload["error"] = dict(self.error or _INTERNAL_ERROR["error"])
            return payload


def _prune_jobs() -> None:
    """Drop expired jobs, then the oldest ones over the cap. Call with the lock."""
    cutoff = time.monotonic() - _JOB_TTL_SECONDS
    for job_id, job in list(_jobs.items()):
        if job.finished_at is not None and job.finished_at < cutoff:
            del _jobs[job_id]
    overflow = len(_jobs) - _JOB_MAX
    if overflow > 0:
        for job_id, _ in sorted(_jobs.items(), key=lambda item: item[1].started_at)[:overflow]:
            del _jobs[job_id]


def _running_job_count() -> int:
    """How many collects are still working, for the shutdown decision (ADR-032)."""
    with _jobs_lock:
        return sum(1 for job in _jobs.values() if job.state == "running")


def _record_step(job: _SearchJob, update) -> None:
    """Progress sink: append/replace the newest line for ``job``.

    Repeated identical messages replace each other (a page that is re-tried
    should not fill the list), and the numbers are only published when the
    collector actually knows them — an invented percentage is worse than none.
    """
    step: dict = {"message": update.message}
    if update.percent is not None:
        step.update(done=update.done, total=update.total, percent=update.percent)
    with _jobs_lock:
        if job.steps and job.steps[-1]["message"] == step["message"]:
            job.steps[-1] = step
        else:
            job.steps.append(step)
        job.latest = step


def _run_search_job(job: _SearchJob) -> None:
    """Worker thread body: collect with a progress sink installed."""
    pipeline.set_progress_sink(lambda update: _record_step(job, update))
    try:
        hits = pipeline.search_hits(job.query, source=job.source)
    except Lyrics2PptxError as exc:
        facing = pipeline.describe_error(exc)
        with _jobs_lock:
            job.error = {
                "code": facing.code,
                "message": facing.message,
                "recoverable": facing.recoverable,
            }
            job.state = "error"
    except Exception:
        log.exception("search job failed id=%s", job.id)
        with _jobs_lock:
            job.error = dict(_INTERNAL_ERROR["error"])
            job.state = "error"
    else:
        with _jobs_lock:
            job.results = [asdict(hit) for hit in hits]
            job.state = "done"
    finally:
        pipeline.set_progress_sink(None)
        with _jobs_lock:
            job.finished_at = time.monotonic()
        log.info(
            "search job id=%s state=%s steps=%d",
            job.id,
            job.state,
            len(job.steps),
        )


class Lyrics2PptxHandler(BaseHTTPRequestHandler):
    """Route dispatcher for ``/``, ``/static/*``, ``/api/search``,
    ``/api/search/start``, ``/api/search/job``, ``/api/ping``,
    ``/api/shutdown``, ``/api/slides``, ``/api/slides/{index}/thumbnail``,
    ``/api/preview``, and ``/api/generate``."""

    server_version = f"lyrics2pptx/{__version__}"
    protocol_version = "HTTP/1.1"

    # --- plumbing ---------------------------------------------------------

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
        """Silence the default per-request line; we log access ourselves."""
        log.debug("http %s", fmt % args)

    def _begin(self) -> tuple[str, float]:
        self._started = time.monotonic()
        self._status = 500
        # ADR-032: the session lives on the server object. ``in_flight`` keeps the
        # watchdog from stopping the process in the middle of a build or download;
        # anything that is not a heartbeat counts as "the user is working".
        self._session = getattr(self.server, "session", None)
        self._is_heartbeat = urlparse(self.path).path == _HEARTBEAT_PATH
        # 하트비트는 세지 않는다: 순간적인 상태 조회라서 "작업 진행 중"이 아니고,
        # 세면 /api/ping 응답의 busy 가 자기 자신 때문에 항상 true 가 된다.
        if self._session is not None and not self._is_heartbeat:
            self._session.enter()
        return uuid4().hex[:8], self._started

    def _finish(self, method: str, rid: str) -> None:
        elapsed_ms = (time.monotonic() - self._started) * 1000
        log.info(
            "%s %s -> %d %.0fms rid=%s",
            method,
            self.path,
            self._status,
            elapsed_ms,
            rid,
        )
        if self._session is not None and not self._is_heartbeat:
            self._session.leave()
            self._session.note_activity()

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        extra: dict[str, str] | None = None,
        head_only: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if not head_only and body:
            self.wfile.write(body)
        self._status = status

    def _send_json(self, status: int, payload: object) -> None:
        self._send(status, _json_bytes(payload), "application/json; charset=utf-8")

    def _send_error(self, exc: Lyrics2PptxError, rid: str) -> None:
        facing = pipeline.describe_error(exc)
        log.warning("error code=%s status=%d rid=%s", facing.code, facing.http_status, rid)
        self._send_json(
            facing.http_status,
            {
                "error": {
                    "code": facing.code,
                    "message": facing.message,
                    "recoverable": facing.recoverable,
                }
            },
        )

    # --- GET --------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        rid, _ = self._begin()
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._serve_static("index.html")
            elif parsed.path.startswith("/static/"):
                self._serve_static(parsed.path[len("/static/") :])
            elif parsed.path == "/api/search":
                self._handle_search(parse_qs(parsed.query), rid)
            elif parsed.path == "/api/search/start":
                self._handle_search_start(parse_qs(parsed.query))
            elif parsed.path == "/api/search/job":
                self._handle_search_job(parse_qs(parsed.query))
            elif parsed.path == _HEARTBEAT_PATH:
                self._handle_ping(parse_qs(parsed.query))
            elif parsed.path == "/api/slides":
                self._handle_slides()
            elif parsed.path.startswith("/api/slides/"):
                self._handle_thumbnail(parsed.path)
            else:
                self._send_json(404, _NOT_FOUND_ERROR)
        except Lyrics2PptxError as exc:
            self._send_error(exc, rid)
        except Exception:
            log.exception("unhandled error path=%s rid=%s", self.path, rid)
            self._send_json(500, _INTERNAL_ERROR)
        finally:
            self._finish("GET", rid)

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        rid, _ = self._begin()
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._serve_static("index.html", head_only=True)
            elif parsed.path.startswith("/static/"):
                self._serve_static(parsed.path[len("/static/") :], head_only=True)
            else:
                self._send_json(404, _NOT_FOUND_ERROR)
        except Exception:
            log.exception("unhandled error path=%s rid=%s", self.path, rid)
            self._send_json(500, _INTERNAL_ERROR)
        finally:
            self._finish("HEAD", rid)

    def _serve_static(self, relative: str, *, head_only: bool = False) -> None:
        base = Path(config.WEB_DIR).resolve()
        candidate = (base / relative).resolve()
        if not _is_within(base, candidate) or not candidate.is_file():
            self._send(404, b"Not Found", "text/plain; charset=utf-8", head_only=head_only)
            return
        body = candidate.read_bytes()
        if candidate.name == "index.html":
            body = self._inject_session(body)
        content_type = _CONTENT_TYPES.get(candidate.suffix.lower(), _DEFAULT_CONTENT_TYPE)
        self._send(200, body, content_type, head_only=head_only)

    def _inject_session(self, body: bytes) -> bytes:
        """Hand the page its shutdown token (ADR-032).

        The token never appears in a URL and the file on disk keeps its
        placeholder, so the served page is the only place it is readable.
        """
        session = self._session
        if session is None or _SESSION_PLACEHOLDER not in body:  # pragma: no cover
            return body
        meta = (
            f'<meta name="lyrics2pptx-session" content="{session.token}">'
        ).encode("utf-8")
        return body.replace(_SESSION_PLACEHOLDER, meta)

    def _handle_search(self, params: dict[str, list[str]], rid: str) -> None:
        query = (params.get("q") or [""])[0]
        source = (params.get("source") or [config.DEFAULT_SOURCE])[0]
        try:
            hits = pipeline.search_hits(query, source=source)
        except Lyrics2PptxError as exc:
            self._send_error(exc, rid)
            return
        self._send_json(200, {"results": [asdict(hit) for hit in hits]})

    def _handle_search_start(self, params: dict[str, list[str]]) -> None:
        """``GET /api/search/start`` — begin a collect, return its job id.

        Validation that can be decided without the network happens here so the
        browser gets an immediate 4xx; everything else surfaces as the job's
        ``error`` state (the browser renders both the same way).
        """
        query = (params.get("q") or [""])[0].strip()
        source = (params.get("source") or [config.DEFAULT_SOURCE])[0]
        if not query:
            raise ConfigError("검색어가 비어 있습니다")
        job = _SearchJob(id=uuid4().hex[:12], query=query, source=source)
        with _jobs_lock:
            _prune_jobs()
            _jobs[job.id] = job
        threading.Thread(
            target=_run_search_job,
            args=(job,),
            name=f"search-{job.id}",
            daemon=True,
        ).start()
        self._send_json(202, job.snapshot())

    def _handle_search_job(self, params: dict[str, list[str]]) -> None:
        """``GET /api/search/job`` — current state of a started collect."""
        job_id = (params.get("id") or [""])[0]
        with _jobs_lock:
            job = _jobs.get(job_id)
        if job is None:  # expired or never existed: the client just restarts
            self._send_json(404, _NOT_FOUND_ERROR)
            return
        self._send_json(200, job.snapshot())

    # --- session (ADR-032) -------------------------------------------------

    def _handle_ping(self, params: dict[str, list[str]]) -> None:
        """``GET|POST /api/ping`` — heartbeat, tab-closing beacon, status.

        ``?closing=1`` (sent by the page on ``pagehide``) starts the grace timer;
        it must carry the session token, because a random web page must not be
        able to retire the user's server. A plain ping cancels a pending closing
        (a reload or a return to the tab) and needs no token: it reveals nothing
        but "a lyrics2pptx server is listening on this port". ``?keep=1`` is the
        in-page "계속 쓰기" button and counts as use, which the idle rule reads.
        """
        session = self._session
        if session is None:  # pragma: no cover - build_server always sets it
            self._send_json(200, {"ok": True, "version": __version__})
            return
        self._drain_body()
        if _is_true(params.get("closing")):
            if not self._page_may_ask(session):
                log.warning("closing beacon rejected origin=%s", self.headers.get("Origin"))
                self._send_json(403, _FORBIDDEN_ERROR)
                return
            session.note_tab_closed()
        else:
            session.heartbeat(keep=_is_true(params.get("keep")))
        self._send_json(
            200,
            {
                "ok": True,
                "version": __version__,
                **session.snapshot(jobs=_running_job_count()),
            },
        )

    def _handle_shutdown(self, rid: str) -> None:
        """``POST /api/shutdown`` — the in-page "종료" button.

        Only the page we served may stop the server: the request must carry the
        per-process token (injected into ``index.html``) *and*, when a browser
        sends one, an ``Origin`` of this very origin. Without that check any
        website could kill the user's server with a cross-site POST, because the
        loopback port is guessable.
        """
        session = self._session
        self._drain_body()
        if session is None or not self._page_may_ask(session):
            log.warning("shutdown rejected rid=%s origin=%s", rid, self.headers.get("Origin"))
            self._send_json(403, _FORBIDDEN_ERROR)
            return
        session.request_stop("사용자 요청")
        log.info("shutdown requested by the page rid=%s", rid)
        self._send_json(
            200,
            {
                "ok": True,
                "reason": session.reason,
                **session.snapshot(jobs=_running_job_count()),
            },
        )
        # Answer first, stop after: ``shutdown()`` waits for ``serve_forever`` to
        # return and the response has to reach the browser (it paints "종료됨").
        threading.Timer(_STOP_GRACE_SECONDS, self.server.shutdown).start()

    def _handle_reveal(self, payload: dict, rid: str) -> None:
        """``POST /api/reveal`` — show a built file in the OS file manager.

        The "폴더 열기" button after a build. The name is sanitized and resolved
        inside ``config.OUTPUT_DIR``, so the endpoint cannot be talked into
        opening anything else on the machine.
        """
        session = self._session
        # 본문은 라우팅에서 _read_json_body 가 이미 읽었다 — 여기서 또 읽으면
        # keep-alive 연결에서 영원히 기다린다(_drain_body 는 본문을 안 읽는
        # /api/shutdown 과 /api/ping 에만 필요하다).
        if session is None or not self._page_may_ask(session):
            log.warning("reveal rejected rid=%s", rid)
            self._send_json(403, _FORBIDDEN_ERROR)
            return
        name = config.sanitize_filename(str(payload.get("filename") or ""))
        base = Path(config.OUTPUT_DIR).resolve()
        target = (base / name).resolve()
        if not _is_within(base, target) or not target.is_file():
            self._send_json(404, _NOT_FOUND_ERROR)
            return
        _reveal_in_explorer(target)
        self._send_json(200, {"ok": True, "filename": name})

    def _page_may_ask(self, session: Session) -> bool:
        """True when the request comes from the page we served (ADR-032)."""
        supplied = self.headers.get(_TOKEN_HEADER, "")
        if not hmac.compare_digest(supplied, session.token):
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True  # scripts and tests; they already had to read the token
        host = self.headers.get("Host") or ""
        return origin.rstrip("/") in (f"http://{host}", f"https://{host}")

    def _drain_body(self) -> None:
        """Read and discard a small unread body so keep-alive stays in sync."""
        raw = self.headers.get("Content-Length")
        try:
            length = int(raw) if raw else 0
        except ValueError:
            length = 0
        if length <= 0:
            return
        if length > config.REQUEST_MAX_BYTES:
            self.close_connection = True
            return
        self.rfile.read(length)

    def _handle_slides(self) -> None:
        """``GET /api/slides`` — metadata for every selectable template slide."""
        metas = thumbnails.list_slide_meta()
        self._send_json(
            200,
            [
                {
                    "index": meta.index,
                    "has_title_shape": meta.has_title_shape,
                    "thumbnail_url": meta.thumbnail_url,
                }
                for meta in metas
            ],
        )

    def _handle_thumbnail(self, path: str) -> None:
        """``GET /api/slides/{index}/thumbnail`` — raw background image bytes."""
        segments = path.strip("/").split("/")
        if len(segments) != 4 or segments[3] != "thumbnail":
            self._send_json(404, _NOT_FOUND_ERROR)
            return
        try:
            index = int(segments[2])
        except ValueError as exc:
            raise ConfigError("슬라이드 번호는 정수여야 합니다") from exc
        thumbnail = thumbnails.get_slide_thumbnail(index)
        if thumbnail is None:
            self._send_json(404, _NOT_FOUND_ERROR)
            return
        self._send(200, thumbnail.data, thumbnail.content_type)

    # --- POST -------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        rid, _ = self._begin()
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/generate":
                payload = self._read_json_body()
                self._handle_generate(payload, rid)
            elif parsed.path == "/api/preview":
                payload = self._read_json_body()
                self._handle_preview(payload, rid)
            elif parsed.path == _HEARTBEAT_PATH:
                # ``navigator.sendBeacon`` (the "the tab is closing" signal) is a
                # POST, so the heartbeat answers on both methods.
                self._handle_ping(parse_qs(parsed.query))
            elif parsed.path == "/api/shutdown":
                self._handle_shutdown(rid)
            elif parsed.path == "/api/reveal":
                payload = self._read_json_body()
                self._handle_reveal(payload, rid)
            else:
                self._send_json(404, _NOT_FOUND_ERROR)
        except _BodyTooLarge:
            pass  # the 413 response was already written by _read_json_body
        except Lyrics2PptxError as exc:
            self._send_error(exc, rid)
        except Exception:
            log.exception("unhandled error path=%s rid=%s", self.path, rid)
            self._send_json(500, _INTERNAL_ERROR)
        finally:
            self._finish("POST", rid)

    def _read_json_body(self) -> dict:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length else 0
        except ValueError:
            length = 0
        if length > config.REQUEST_MAX_BYTES:
            self.close_connection = True
            self._send_json(413, _TOO_LARGE_ERROR)
            raise _BodyTooLarge()
        body = self.rfile.read(length) if length else b""
        if not body:
            return {}
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ConfigError("요청 본문이 올바른 JSON 이 아닙니다") from exc
        if not isinstance(payload, dict):
            raise ConfigError("요청 본문은 JSON 객체여야 합니다")
        return payload

    def _handle_preview(self, payload: dict, rid: str) -> None:
        """``POST /api/preview`` — normalize + pair, no pptx is written.

        The response is the exact line content a build would fill, so the
        browser preview cannot drift from the generated file (M1).
        """
        raw_text = payload.get("raw_text")
        if raw_text is None:
            raise ConfigError("raw_text 가 필요합니다")
        if not isinstance(raw_text, str):
            raise ConfigError("raw_text 는 문자열이어야 합니다")
        dedupe = bool(payload.get("dedupe_chorus", False))
        slide_index = pipeline.resolve_slide_index(payload.get("slide_index"))
        result = pipeline.preview(raw_text, dedupe_chorus=dedupe)
        self._send_json(
            200,
            {
                "lines": list(result.lines),
                "pairs": [[first, second] for first, second in result.pairs],
                "removed_marker_count": result.removed_marker_count,
                "slide_index": slide_index,
            },
        )

    def _handle_generate(self, payload: dict, rid: str) -> None:
        raw_text = payload.get("raw_text")
        lines = payload.get("lines")
        query = payload.get("query")
        source = payload.get("source") or config.DEFAULT_SOURCE
        index = _coerce_int(payload.get("index", 0), 0)
        dedupe = bool(payload.get("dedupe_chorus", False))
        filename = payload.get("filename") or config.DEFAULT_FILENAME
        slide_index = pipeline.resolve_slide_index(payload.get("slide_index"))

        if raw_text is not None and not isinstance(raw_text, str):
            raise ConfigError("raw_text 는 문자열이어야 합니다")
        if lines is not None and not (
            isinstance(lines, list) and all(isinstance(item, str) for item in lines)
        ):
            raise ConfigError("lines 는 문자열 배열이어야 합니다")
        if raw_text is None and lines is None and not query:
            raise ConfigError("query, raw_text, lines 중 하나가 필요합니다")

        safe_name = config.ensure_pptx_suffix(config.sanitize_filename(str(filename)))
        target = config.OUTPUT_DIR / safe_name

        with _BUILD_LOCK:
            result = pipeline.run(
                query=query,
                source=source,
                index=index,
                raw_text=raw_text,
                lines=lines,
                dedupe_chorus=dedupe,
                slide_index=slide_index,
                output_path=target,
            )
            try:
                blob = Path(result.output_path).read_bytes()
            except OSError as exc:
                raise OutputWriteError(
                    f"생성된 파일을 읽지 못했습니다: {result.output_path}"
                ) from exc

        disposition = f"attachment; filename*=UTF-8''{quote(safe_name, safe='')}"
        self._send(
            200,
            blob,
            _PPTX_CONTENT_TYPE,
            extra={"Content-Disposition": disposition},
        )


class _BodyTooLarge(Exception):
    """Internal signal: the response was already sent, skip further handling."""


class _Server(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` that carries the session metadata (ADR-032)."""

    session: Session


def build_server(
    host: str | None = None,
    port: int | None = None,
) -> _Server:
    """Create the HTTP server bound to ``host:port`` (defaults from config)."""
    bind_host = host if host is not None else config.BIND_HOST
    bind_port = port if port is not None else config.BIND_PORT
    server = _Server((bind_host, bind_port), Lyrics2PptxHandler)
    server.session = Session(
        token=secrets.token_hex(16),
        tab_grace=config.WEBUI_TAB_GRACE,
        idle=config.WEBUI_IDLE,
        max_lifetime=config.WEBUI_MAX_LIFETIME,
        auto_shutdown=config.WEBUI_AUTOSHUTDOWN,
    )
    return server


def start_watchdog(server: _Server, *, interval: float = 1.0) -> threading.Thread | None:
    """Start the thread that retires the server by itself (ADR-032).

    The rules live in ``Session.stop_reason``; this only polls them and calls
    ``shutdown()`` from *another* thread, which is what ``socketserver`` requires.
    Work in progress defers the decision to the next tick.
    """

    def loop() -> None:
        while True:
            time.sleep(interval)
            reason = server.session.stop_reason(jobs=_running_job_count())
            if reason is None:
                continue
            server.session.request_stop(reason)
            log.info("auto shutdown reason=%s", reason)
            server.shutdown()
            return

    if not server.session.auto_shutdown:
        log.info("자동 종료가 꺼져 있습니다 (LYRICS2PPTX_WEBUI_AUTOSHUTDOWN=0)")
        return None
    thread = threading.Thread(target=loop, name="lyrics2pptx-watchdog", daemon=True)
    thread.start()
    return thread


def write_state(server: _Server) -> Path | None:
    """Publish port + instance id so the launcher can reuse a running server.

    Plain ``key=value`` lines: ``lyrics2pptx.vbs`` has no JSON parser. Written
    atomically (temp file + replace) so the launcher never reads half a file.
    """
    session = server.session
    host, port = server.server_address[0], server.server_address[1]
    target = config.webui_state_path()
    lines = (
        "# lyrics2pptx - written while the local web UI is running. Do not edit.",
        f"instance={session.instance}",
        f"host={host}",
        f"port={port}",
        f"pid={os.getpid()}",
        f"started={datetime.now().isoformat(timespec='seconds')}",
        f"url=http://{host}:{port}/",
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_name(target.name + ".tmp")
        staging.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(staging, target)
    except OSError as exc:
        log.warning("실행 상태 파일을 쓰지 못했습니다 (%s): %s", target, exc)
        return None
    return target


def clear_state(instance: str) -> None:
    """Remove the state file — but only if it still describes *this* server."""
    target = config.webui_state_path()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return
    if f"instance={instance}" not in text.split("\n"):
        return  # a newer instance owns the file now
    try:
        target.unlink()
    except OSError as exc:  # pragma: no cover - best effort while exiting
        log.warning("실행 상태 파일을 지우지 못했습니다 (%s): %s", target, exc)


def read_state() -> dict[str, str]:
    """Return ``config.WEBUI_STATE_FILE`` as a dict (``{}`` when unreadable).

    Same ``key=value`` format ``write_state`` writes and ``lyrics2pptx.vbs``
    parses; kept deliberately dumb so all three readers agree.
    """
    try:
        text = config.webui_state_path().read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def reuse_url(timeout: float = 1.5) -> str:
    """URL of a *live* lyrics2pptx server, or ``""`` when there is none.

    The state file alone proves nothing: the process may be gone and the port
    may since have been taken by something else entirely. So the instance id in
    the file has to answer on ``/api/ping`` before we send the user to it. The
    address is rebuilt on ``127.0.0.1`` rather than trusted from the file, so a
    hand-edited state file cannot point the launcher at another host.
    """
    values = read_state()
    port, instance = values.get("port", ""), values.get("instance", "")
    if not port.isdigit() or not instance:
        return ""
    url = f"http://127.0.0.1:{port}/"
    request = urllib.request.Request(
        f"{url}api/ping", headers={"User-Agent": config.DEFAULT_UA}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return ""  # refused, timed out, or not our JSON at all
    if not isinstance(payload, dict) or payload.get("instance") != instance:
        return ""
    return url


def _open_browser(url: str) -> bool:
    """Open the user's default browser; never let this fail the server."""
    try:
        opened = webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001 - webbrowser may raise anything
        log.warning("브라우저를 열지 못했습니다 (%s): %s", type(exc).__name__, exc)
        return False
    if not opened:
        log.warning("브라우저를 열지 못했습니다. 주소를 직접 열어 주세요: %s", url)
    return bool(opened)


def _rotate_launch_log(target: Path) -> None:
    """Keep ``launch.log`` short by moving a grown file aside (best effort)."""
    try:
        if target.is_file() and target.stat().st_size > _LAUNCH_LOG_MAX_BYTES:
            target.replace(target.with_name(target.name + ".old"))
    except OSError:
        pass  # another run holds it — appending is always better than losing it


def _redirect_launch_log() -> None:
    """Send stdout/stderr to ``output/launch.log`` in a packaged run (ADR-033).

    A packaged ``lyrics2pptx.exe`` is built without a console, so a traceback
    would otherwise vanish; this file is the one thing the user can send us when
    something goes wrong. Developer runs (``python -m lyrics2pptx.webapp``) keep
    their console.

    The file is opened in **append** mode on purpose: double-clicking the exe
    while it is already running must not truncate the log of the server that is
    still writing to it (that is exactly the "opened it twice" case).
    """
    if not getattr(sys, "frozen", False):
        return
    target = config.OUTPUT_DIR / _LAUNCH_LOG_NAME
    try:
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        _rotate_launch_log(target)
        stream = target.open("a", encoding="utf-8", buffering=1)
        stream.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} 실행 ===\n")
    except OSError as exc:  # pragma: no cover - unwritable folder
        log.warning("실행 기록을 남기지 못했습니다: %s", exc)
        return
    sys.stdout = stream
    sys.stderr = stream


def launch(argv: list[str] | None = None) -> int:
    """Entry point of the packaged app: one double-click, no console (ADR-033).

    This is the packaged twin of ``lyrics2pptx.vbs``: reuse the running server
    when there is one (the user double-clicked twice, or a window is still
    open), otherwise start the server and open the tab. There is no separate
    "start" step and no separate "stop" step — the rules of ADR-032 retire it.
    """
    _redirect_launch_log()
    running = reuse_url()
    if running:
        log.info("이미 실행 중인 서버를 엽니다 → %s", running)
        _open_browser(running)
        return 0
    return main(argv, open_browser=True)


def _setup_logging() -> None:
    logging.basicConfig(
        level=config.LOG_LEVEL_OBJ,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    for noisy in ("pptx", "PIL", "lxml"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None, *, open_browser: bool = False) -> int:
    """Start the local web server and serve until it retires itself (ADR-032).

    ``open_browser`` is set by ``launch`` (ADR-033) so a double-clicked exe
    reaches the UI without the user typing a URL. It is opened only *after* the
    socket is accepting, and a browser that refuses to open is a warning, never
    a crash.
    """
    _setup_logging()
    unknown = config.unknown_env_keys()
    if unknown:
        log.warning("알 수 없는 환경변수(오타 가능성): %s", ", ".join(unknown))
    log.info("started version=%s python=%s", __version__, sys.version.split()[0])

    server = build_server()
    host, port = server.server_address[0], server.server_address[1]
    write_state(server)
    session = server.session
    log.info(
        "serving http://%s:%d (탭을 닫으면 %.0f초 뒤, 사용이 없으면 %.0f초 뒤 종료)",
        host,
        port,
        session.tab_grace,
        session.idle,
    )
    print(
        f"lyrics2pptx {__version__} 실행 중 → http://{host}:{port}  "
        f"(이 창을 닫으면 자동 종료됩니다)",
        file=sys.stderr,
    )
    # ``serve_forever`` runs in its own thread so the watchdog can call
    # ``shutdown()`` (it must not run in the serving thread) and so Ctrl+C still
    # lands in the main thread.
    serving = threading.Thread(
        target=server.serve_forever, name="lyrics2pptx-serve", daemon=True
    )
    serving.start()
    start_watchdog(server)
    if open_browser:
        # The port is already listening (``build_server`` bound it), so the tab
        # cannot race us into a "connection refused".
        _open_browser(f"http://{host}:{port}/")
    try:
        while serving.is_alive():
            serving.join(timeout=1.0)
    except KeyboardInterrupt:
        log.info("shutdown requested (Ctrl+C)")
        session.request_stop("Ctrl+C")
    finally:
        server.shutdown()
        serving.join(timeout=10)
        server.server_close()
        clear_state(session.instance)
        log.info("stopped reason=%s", session.reason or "외부 신호")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
