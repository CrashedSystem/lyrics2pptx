"""Local single-user web app (L4).

An ``http.server`` front end for ``pipeline`` (docs/arch/05 §2, docs/plan/06
§2): ``/``, ``/static/*``, ``/api/search``, ``/api/search/start``,
``/api/search/job``, ``/api/slides``, ``/api/slides/{index}/thumbnail``,
``/api/preview`` and ``/api/generate``.
Nothing here contains domain logic: requests are validated, handed to
``pipeline.preview``/``pipeline.run``/``search_hits``, and the result is streamed
back.

``/api/search/start`` + ``/api/search/job`` exist because a collect can take ten
seconds or more (a blog search fetches several pages politely). The single-shot
``/api/search`` is kept for scripts and tests; the browser starts a job and polls
it so it can show *which page* is being read instead of a frozen "검색 중…".

Security boundary (docs/arch/06 §3): bound to ``127.0.0.1``, static serving is
containment-checked against ``web/``, and request bodies are capped at
``config.REQUEST_MAX_BYTES``. Builds are serialized by one global lock because
the template is a 37 MB in-process resource (AD-14).
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from uuid import uuid4

from . import __version__, config, pipeline
from .errors import ConfigError, Lyrics2PptxError, OutputWriteError
from .pptx import thumbnails

log = logging.getLogger("lyrics2pptx.webapp")

__all__ = ["Lyrics2PptxHandler", "build_server", "main"]

# One build at a time: the template and output directory are process-wide.
_BUILD_LOCK = threading.Lock()

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
    ``/api/search/start``, ``/api/search/job``, ``/api/slides``,
    ``/api/slides/{index}/thumbnail``, ``/api/preview``, and ``/api/generate``."""

    server_version = f"lyrics2pptx/{__version__}"
    protocol_version = "HTTP/1.1"

    # --- plumbing ---------------------------------------------------------

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
        """Silence the default per-request line; we log access ourselves."""
        log.debug("http %s", fmt % args)

    def _begin(self) -> tuple[str, float]:
        self._started = time.monotonic()
        self._status = 500
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
        content_type = _CONTENT_TYPES.get(candidate.suffix.lower(), _DEFAULT_CONTENT_TYPE)
        self._send(200, body, content_type, head_only=head_only)

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


def build_server(
    host: str | None = None,
    port: int | None = None,
) -> ThreadingHTTPServer:
    """Create the HTTP server bound to ``host:port`` (defaults from config)."""
    bind_host = host if host is not None else config.BIND_HOST
    bind_port = port if port is not None else config.BIND_PORT
    return ThreadingHTTPServer((bind_host, bind_port), Lyrics2PptxHandler)


def _setup_logging() -> None:
    logging.basicConfig(
        level=config.LOG_LEVEL_OBJ,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    for noisy in ("pptx", "PIL", "lxml"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    """Start the local web server (blocks until Ctrl+C)."""
    _setup_logging()
    unknown = config.unknown_env_keys()
    if unknown:
        log.warning("알 수 없는 환경변수(오타 가능성): %s", ", ".join(unknown))
    log.info("started version=%s python=%s", __version__, sys.version.split()[0])

    server = build_server()
    host, port = server.server_address[0], server.server_address[1]
    log.info("serving http://%s:%d (Ctrl+C 로 종료)", host, port)
    print(f"lyrics2pptx {__version__} 실행 중 → http://{host}:{port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutdown requested")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
