"""HTTP integration tests for ``webapp.py`` (docs/arch/05 §2).

The real ``ThreadingHTTPServer`` is started on an ephemeral port and driven over
a real socket with ``http.client`` — no handler is called directly, so routing,
headers, status codes, and the static containment check are all exercised. Every
server is stopped and its thread joined in ``tearDown``.
"""

from __future__ import annotations

import http.client
import json
import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from pptx import Presentation

from lyrics2pptx import config, pipeline, webapp
from lyrics2pptx.webapp import build_server

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TEMPLATE = Path(__file__).resolve().parents[1] / "template.pptx"
PPTX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)
TOKEN_HEADER = "X-Lyrics2Pptx-Token"


def setUpModule() -> None:  # noqa: N802 - unittest hook
    logging.disable(logging.CRITICAL)


def tearDownModule() -> None:  # noqa: N802 - unittest hook
    logging.disable(logging.NOTSET)


class WebAppTestCase(unittest.TestCase):
    """Boots the server on 127.0.0.1 with an ephemeral port."""

    FIXTURE_MODE = False

    def setUp(self) -> None:
        self._patchers = []
        if self.FIXTURE_MODE:
            patcher = mock.patch.object(config, "FIXTURE_DIR", FIXTURES)
            patcher.start()
            self._patchers.append(patcher)
        self._tmp = tempfile.TemporaryDirectory()
        out_patcher = mock.patch.object(config, "OUTPUT_DIR", Path(self._tmp.name))
        out_patcher.start()
        self._patchers.append(out_patcher)
        pipeline.clear_search_cache()

        self.server = build_server("127.0.0.1", 0)
        self.host = self.server.server_address[0]
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)

    def _shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        self.assertFalse(self.thread.is_alive(), "server thread did not stop")
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmp.cleanup()
        pipeline.clear_search_cache()

    # -- helpers ----------------------------------------------------------

    def request(self, method: str, path: str, *, body=None, headers=None):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=60)
        try:
            merged = {"Connection": "close"}
            merged.update(headers or {})
            connection.request(method, path, body=body, headers=merged)
            response = connection.getresponse()
            payload = response.read()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, payload
        finally:
            connection.close()

    def get(self, path: str):
        return self.request("GET", path)

    def post_json(self, path: str, payload) -> tuple:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self.request(
            "POST", path, body=body, headers={"Content-Type": "application/json"}
        )


class StaticAssetTests(WebAppTestCase):
    def test_root_serves_index_html(self) -> None:
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/html"))
        self.assertIn("charset=utf-8", headers["content-type"])
        self.assertTrue(body)

    def test_static_assets_are_served_with_their_content_type(self) -> None:
        status, headers, body = self.get("/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["content-type"])
        self.assertTrue(body)

        status, headers, _ = self.get("/static/style.css")
        self.assertEqual(status, 200)
        self.assertIn("css", headers["content-type"])

    def test_static_responses_are_not_cached(self) -> None:
        _, headers, _ = self.get("/")
        self.assertEqual(headers["cache-control"], "no-store")

    def test_head_request_returns_headers_without_a_body(self) -> None:
        status, headers, body = self.request("HEAD", "/")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertIn("content-length", headers)

    def test_path_traversal_outside_the_web_directory_is_404(self) -> None:
        for path in (
            "/static/../template.pptx",
            "/static/../../template.pptx",
            "/static/..%2f..%2f..%2ftemplate.pptx",
        ):
            with self.subTest(path=path):
                status, _, _ = self.get(path)
                self.assertEqual(status, 404)

    def test_unknown_path_is_404_json(self) -> None:
        status, headers, body = self.get("/definitely/not/a/route")
        self.assertEqual(status, 404)
        self.assertIn("application/json", headers["content-type"])
        self.assertEqual(json.loads(body)["error"]["code"], "NOT_FOUND")


class SearchEndpointTests(WebAppTestCase):
    FIXTURE_MODE = True

    def test_search_returns_fixture_hits(self) -> None:
        status, headers, body = self.get(f"/api/search?q={quote('생명샘')}")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["content-type"])
        payload = json.loads(body)
        self.assertEqual(len(payload["results"]), 1)
        hit = payload["results"][0]
        self.assertEqual(hit["title"], "생명샘")
        self.assertEqual(hit["source_name"], "lrclib")
        self.assertLessEqual(len(hit["preview_lines"]), 3)

    def test_empty_query_is_a_bad_request(self) -> None:
        for path in ("/api/search?q=", "/api/search"):
            with self.subTest(path=path):
                status, _, body = self.get(path)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"]["code"], "BAD_REQUEST")

    def test_unknown_source_is_a_bad_request(self) -> None:
        status, _, body = self.get("/api/search?q=abc&source=nope")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "BAD_REQUEST")

    def test_no_results_maps_to_404_source_not_found(self) -> None:
        status, _, body = self.get(f"/api/search?q={quote('not found')}")
        self.assertEqual(status, 404)
        error = json.loads(body)["error"]
        self.assertEqual(error["code"], "SOURCE_NOT_FOUND")
        self.assertIn("검색 결과가 없습니다", error["message"])
        self.assertTrue(error["recoverable"])

    def test_error_bodies_never_leak_a_stack_trace(self) -> None:
        _, _, body = self.get("/api/search?q=")
        text = body.decode("utf-8")
        self.assertNotIn("Traceback", text)
        self.assertNotIn("lyrics2pptx/", text)


class GenerateEndpointTests(WebAppTestCase):
    def test_raw_text_generates_a_pptx_attachment(self) -> None:
        status, headers, body = self.post_json(
            "/api/generate", {"raw_text": "첫 줄\n둘째 줄\n셋 줄"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], PPTX_CONTENT_TYPE)
        self.assertTrue(headers["content-disposition"].startswith("attachment;"))
        self.assertIn("filename*=UTF-8''", headers["content-disposition"])
        self.assertIn(quote(config.DEFAULT_FILENAME, safe=""), headers["content-disposition"])
        self.assertEqual(int(headers["content-length"]), len(body))
        self.assertTrue(body.startswith(b"PK"), "response is not a zip/pptx payload")

    def test_lines_input_generates_and_sanitizes_the_filename(self) -> None:
        status, headers, body = self.post_json(
            "/api/generate",
            {"lines": ["가", "나", "다", "라"], "filename": "..\\evil/name.pptx"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"PK"))
        disposition = headers["content-disposition"]
        self.assertIn("evilname.pptx", disposition)
        self.assertNotIn("/", disposition)
        self.assertNotIn("\\", disposition)

    def test_missing_input_is_a_bad_request(self) -> None:
        status, _, body = self.post_json("/api/generate", {})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "BAD_REQUEST")

    def test_wrong_field_type_is_a_bad_request(self) -> None:
        for payload in ({"raw_text": 123}, {"lines": "not-a-list"}, {"lines": [1, 2]}):
            with self.subTest(payload=payload):
                status, _, body = self.post_json("/api/generate", payload)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"]["code"], "BAD_REQUEST")

    def test_invalid_json_is_a_bad_request(self) -> None:
        status, _, body = self.request(
            "POST",
            "/api/generate",
            body=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "BAD_REQUEST")

    def test_non_object_json_body_is_a_bad_request(self) -> None:
        status, _, body = self.request(
            "POST", "/api/generate", body=b"[1, 2, 3]", headers={"Content-Type": "application/json"}
        )
        self.assertEqual(status, 400)

    def test_post_to_another_path_is_404(self) -> None:
        status, _, body = self.post_json("/api/other", {"raw_text": "a\nb"})
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "NOT_FOUND")

    def test_oversized_body_is_rejected_with_413(self) -> None:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=60)
        try:
            connection.putrequest("POST", "/api/generate")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(2 * 1024 * 1024))
            connection.putheader("Connection", "close")
            connection.endheaders()
            response = connection.getresponse()
            payload = response.read()
            status = response.status
        finally:
            connection.close()
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(payload)["error"]["code"], "PAYLOAD_TOO_LARGE")


class SlidesEndpointTests(WebAppTestCase):
    """``GET /api/slides`` and ``GET /api/slides/{index}/thumbnail``."""

    def setUp(self) -> None:
        super().setUp()
        if not TEMPLATE.is_file():
            self.skipTest(f"template not found: {TEMPLATE}")

    def test_slides_returns_twenty_metas_in_order(self) -> None:
        status, headers, body = self.get("/api/slides")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["content-type"])
        payload = json.loads(body)
        self.assertIsInstance(payload, list)
        self.assertEqual(len(payload), 20)
        self.assertEqual([meta["index"] for meta in payload], list(range(1, 21)))

    def test_each_meta_carries_the_documented_fields(self) -> None:
        _, _, body = self.get("/api/slides")
        first = json.loads(body)[0]
        self.assertEqual(first["index"], 1)
        self.assertIsInstance(first["has_title_shape"], bool)
        self.assertEqual(first["thumbnail_url"], "/api/slides/1/thumbnail")

    def test_thumbnail_returns_raw_image_bytes(self) -> None:
        # Slide 19 is the default background and has an image blipFill.
        status, headers, body = self.get("/api/slides/19/thumbnail")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("image/"), headers["content-type"])
        self.assertTrue(body, "thumbnail body is empty")
        self.assertEqual(int(headers["content-length"]), len(body))

    def test_thumbnail_for_a_full_bleed_picture_slide(self) -> None:
        # Slide 20 uses a full-bleed <p:pic> rather than a <p:bg> blipFill.
        status, headers, body = self.get("/api/slides/20/thumbnail")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("image/"))
        self.assertTrue(body)

    def test_thumbnail_out_of_range_is_a_bad_request(self) -> None:
        for path in ("/api/slides/0/thumbnail", "/api/slides/21/thumbnail"):
            with self.subTest(path=path):
                status, _, body = self.get(path)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"]["code"], "BAD_REQUEST")

    def test_thumbnail_non_integer_index_is_a_bad_request(self) -> None:
        status, _, body = self.get("/api/slides/abc/thumbnail")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "BAD_REQUEST")

    def test_thumbnail_for_a_picture_filled_full_bleed_shape(self) -> None:
        # Slide 11 paints its background with a full-bleed autoshape whose
        # spPr carries a blipFill, rather than a <p:bg> or <p:pic>.
        status, headers, body = self.get("/api/slides/11/thumbnail")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("image/"))
        self.assertTrue(body)

    def test_malformed_slides_path_is_404(self) -> None:
        for path in ("/api/slides/19", "/api/slides/19/other"):
            with self.subTest(path=path):
                status, _, _ = self.get(path)
                self.assertEqual(status, 404)


class SlideIndexEndpointTests(WebAppTestCase):
    """The chosen background slide reaches preview and generate."""

    def setUp(self) -> None:
        super().setUp()
        if not TEMPLATE.is_file():
            self.skipTest(f"template not found: {TEMPLATE}")

    def test_preview_defaults_slide_index_to_nineteen(self) -> None:
        status, _, body = self.post_json("/api/preview", {"raw_text": "가\n나"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["slide_index"], 19)

    def test_preview_echoes_the_requested_slide_index(self) -> None:
        status, _, body = self.post_json(
            "/api/preview", {"raw_text": "가\n나", "slide_index": 3}
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["slide_index"], 3)

    def test_generate_honours_the_requested_slide_index(self) -> None:
        status, _, body = self.post_json(
            "/api/generate",
            {"raw_text": "가\n나", "slide_index": 20, "filename": "s20.pptx"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"PK"))
        out = Path(self._tmp.name) / "s20.pptx"
        self.assertTrue(out.is_file())
        presentation = Presentation(str(out))
        self.assertEqual(len(presentation.slides), 1)

    def test_generate_out_of_range_slide_index_is_a_bad_request(self) -> None:
        for slide in (0, 21):
            with self.subTest(slide_index=slide):
                status, _, body = self.post_json(
                    "/api/generate", {"raw_text": "가\n나", "slide_index": slide}
                )
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"]["code"], "BAD_REQUEST")

    def test_generate_non_integer_slide_index_is_a_bad_request(self) -> None:
        status, _, body = self.post_json(
            "/api/generate", {"raw_text": "가\n나", "slide_index": "x"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "BAD_REQUEST")


class SearchJobHelpers:
    """Shared polling helper for the job endpoints."""

    def wait_for_job(self, job_id: str, *, tries: int = 200) -> dict:
        for _ in range(tries):
            status, _, body = self.get(f"/api/search/job?id={job_id}")
            self.assertEqual(status, 200)
            snapshot = json.loads(body)
            if snapshot["state"] != "running":
                return snapshot
            time.sleep(0.05)
        self.fail("job did not finish in time")


class SearchJobEndpointTests(SearchJobHelpers, WebAppTestCase):
    """/api/search/start + /api/search/job against the fixture corpus."""

    FIXTURE_MODE = True

    def test_start_returns_a_job_snapshot(self) -> None:
        status, headers, body = self.get(f"/api/search/start?q={quote('생명샘')}")
        self.assertEqual(status, 202)
        self.assertIn("application/json", headers["content-type"])
        payload = json.loads(body)
        self.assertTrue(payload["job_id"])
        self.assertIn(payload["state"], {"running", "done"})
        self.assertIsInstance(payload["steps"], list)
        self.assertIsInstance(payload["elapsed_ms"], int)
        self.assertGreaterEqual(payload["elapsed_ms"], 0)

    def test_job_publishes_the_same_results_as_the_one_shot_endpoint(self) -> None:
        _, _, body = self.get(f"/api/search/start?q={quote('생명샘')}")
        snapshot = self.wait_for_job(json.loads(body)["job_id"])
        self.assertEqual(snapshot["state"], "done")
        self.assertTrue(snapshot["results"])
        self.assertIn("title", snapshot["results"][0])

    def test_empty_query_is_rejected_before_any_work_starts(self) -> None:
        for path in (
            "/api/search/start?q=",
            "/api/search/start",
            "/api/search/start?q=%20%20",
        ):
            with self.subTest(path=path):
                status, _, body = self.get(path)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"]["code"], "BAD_REQUEST")

    def test_unknown_job_id_is_404(self) -> None:
        status, _, body = self.get("/api/search/job?id=deadbeef")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "NOT_FOUND")

    def test_unknown_source_surfaces_as_the_job_error(self) -> None:
        _, _, body = self.get("/api/search/start?q=abc&source=nope")
        snapshot = self.wait_for_job(json.loads(body)["job_id"])
        self.assertEqual(snapshot["state"], "error")
        self.assertEqual(snapshot["error"]["code"], "BAD_REQUEST")
        self.assertNotIn("results", snapshot)


class SearchJobProgressTests(SearchJobHelpers, WebAppTestCase):
    """The collectors' progress lines reach the polled snapshot."""

    def _run_job(self, patcher) -> dict:
        """Start a job and poll it to completion with ``patcher`` still active.

        The patch must outlive the poll: ``/api/search/start`` returns as soon as
        the worker thread is spawned, so restoring the real collector early would
        let the worker call it instead of the fake.
        """
        with patcher:
            status, _, body = self.get("/api/search/start?q=x")
            self.assertEqual(status, 202)
            return self.wait_for_job(json.loads(body)["job_id"])

    def test_reported_lines_and_numbers_are_published(self) -> None:
        from lyrics2pptx.crawl import progress as crawl_progress

        def fake_search_hits(query, *, source=None, limit=5):
            crawl_progress.report("첫 단계")
            crawl_progress.report("가사 페이지 확인 중…", done=2, total=4)
            return []

        snapshot = self._run_job(
            mock.patch.object(pipeline, "search_hits", side_effect=fake_search_hits)
        )
        self.assertEqual(snapshot["state"], "done")
        self.assertEqual(
            [step["message"] for step in snapshot["steps"]],
            ["첫 단계", "가사 페이지 확인 중…"],
        )
        self.assertEqual(snapshot["progress"]["percent"], 50)
        self.assertEqual(snapshot["progress"]["done"], 2)
        self.assertEqual(snapshot["progress"]["total"], 4)

    def test_a_line_without_numbers_stays_indeterminate(self) -> None:
        from lyrics2pptx.crawl import progress as crawl_progress

        def fake_search_hits(query, *, source=None, limit=5):
            crawl_progress.report("블로그 검색 중…")
            return []

        snapshot = self._run_job(
            mock.patch.object(pipeline, "search_hits", side_effect=fake_search_hits)
        )
        self.assertEqual([step["message"] for step in snapshot["steps"]], ["블로그 검색 중…"])
        self.assertNotIn("percent", snapshot["steps"][0])

    def test_repeated_identical_lines_do_not_pile_up(self) -> None:
        from lyrics2pptx.crawl import progress as crawl_progress

        def fake_search_hits(query, *, source=None, limit=5):
            for _ in range(5):
                crawl_progress.report("같은 줄")
            crawl_progress.report("같은 줄", done=1, total=2)
            return []

        snapshot = self._run_job(
            mock.patch.object(pipeline, "search_hits", side_effect=fake_search_hits)
        )
        self.assertEqual([step["message"] for step in snapshot["steps"]], ["같은 줄"])
        self.assertEqual(snapshot["progress"]["percent"], 50)

    def test_the_worker_thread_gets_its_own_sink(self) -> None:
        from lyrics2pptx.crawl import progress as crawl_progress

        seen: list = []

        def fake_search_hits(query, *, source=None, limit=5):
            # The web app installs the sink for the worker thread before calling.
            seen.append(crawl_progress.current_sink())
            return []

        self._run_job(mock.patch.object(pipeline, "search_hits", side_effect=fake_search_hits))
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0])

    def test_a_failing_collect_becomes_an_error_without_leaking_details(self) -> None:
        snapshot = self._run_job(
            mock.patch.object(pipeline, "search_hits", side_effect=RuntimeError("boom"))
        )
        self.assertEqual(snapshot["state"], "error")
        self.assertEqual(snapshot["error"]["code"], "INTERNAL")
        self.assertNotIn("boom", json.dumps(snapshot))


class SessionRuleTests(unittest.TestCase):
    """ADR-032: the three retirement rules and what defers them (pure logic)."""

    def _session(self, **kwargs) -> webapp.Session:
        base = dict(
            token="t0ken",
            tab_grace=0.0,
            idle=0.0,
            max_lifetime=0.0,
            started=100.0,
            last_activity=100.0,
        )
        base.update(kwargs)
        return webapp.Session(**base)

    def test_a_quiet_session_keeps_serving_until_the_idle_rule_fires(self) -> None:
        session = self._session(idle=60.0)
        self.assertIsNone(session.stop_reason(now=159.9))
        self.assertEqual(session.stop_reason(now=160.0), "오랫동안 사용이 없어서")

    def test_zero_disables_a_rule(self) -> None:
        session = self._session(idle=0.0, tab_grace=0.0, max_lifetime=0.0)
        session.closed_at = 0.0
        self.assertIsNone(session.stop_reason(now=10_000.0))

    def test_the_tab_grace_only_starts_when_the_page_says_it_is_closing(self) -> None:
        session = self._session(tab_grace=180.0)
        self.assertIsNone(session.stop_reason(now=10_000.0))
        session.closed_at = 100.0
        self.assertIsNone(session.stop_reason(now=279.9))
        self.assertEqual(session.stop_reason(now=280.0), "탭을 닫아서")

    def test_a_heartbeat_cancels_the_closing_decision(self) -> None:
        session = self._session(tab_grace=1.0)
        session.closed_at = 100.0
        self.assertEqual(session.stop_reason(now=102.0), "탭을 닫아서")
        session.heartbeat()
        self.assertIsNone(session.stop_reason(now=102.0))

    def test_keep_marks_use_but_a_plain_heartbeat_does_not(self) -> None:
        session = self._session(idle=60.0, last_activity=0.0)
        session.heartbeat()
        self.assertEqual(session.last_activity, 0.0)
        session.heartbeat(keep=True)
        self.assertGreater(session.last_activity, 0.0)

    def test_the_hard_ceiling_applies_even_to_an_open_tab(self) -> None:
        session = self._session(max_lifetime=3600.0, started=0.0)
        self.assertEqual(session.stop_reason(now=3600.0), "최대 가동 시간을 넘어서")

    def test_work_in_progress_defers_every_rule(self) -> None:
        session = self._session(idle=1.0, last_activity=0.0)
        session.enter()
        self.assertIsNone(session.stop_reason(now=999.0))
        self.assertIsNone(session.stop_reason(now=999.0, jobs=1))
        session.leave()
        self.assertIsNotNone(session.stop_reason(now=999.0))

    def test_an_explicit_request_is_remembered_and_waits_for_the_work(self) -> None:
        session = self._session(idle=0.0)
        session.request_stop("사용자 요청")
        self.assertEqual(session.stop_reason(now=1.0), "사용자 요청")
        session.enter()
        self.assertIsNone(session.stop_reason(now=1.0))
        session.leave()
        self.assertEqual(session.stop_reason(now=1.0), "사용자 요청")

    def test_snapshot_is_json_ready(self) -> None:
        now = time.monotonic()
        session = self._session(idle=60.0, started=now, last_activity=now)
        payload = session.snapshot()
        self.assertEqual(payload["instance"], session.instance)
        self.assertEqual(payload["idle_seconds_left"], 60)
        self.assertIsNone(payload["lifetime_seconds_left"])
        self.assertFalse(payload["busy"])
        self.assertFalse(payload["closing"])
        self.assertTrue(payload["auto_shutdown"])
        json.dumps(payload)


class SessionEndpointTests(WebAppTestCase):
    """ADR-032: heartbeat, closing beacon, and the guarded shutdown button."""

    def test_ping_reports_the_session(self) -> None:
        status, _, body = self.get("/api/ping")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["instance"], self.server.session.instance)
        self.assertTrue(payload["auto_shutdown"])
        self.assertGreaterEqual(payload["idle_seconds_left"], 0)
        self.assertFalse(payload["closing"])
        self.assertEqual(payload["tab_grace_s"], self.server.session.tab_grace)
        # 하트비트 자신은 "작업 진행 중"이 아니다(아니면 busy 가 항상 true 다).
        self.assertFalse(payload["busy"])

    def test_the_closing_beacon_starts_the_grace_and_a_ping_cancels_it(self) -> None:
        # 페이지는 keepalive fetch 로 토큰을 헤더에 실어 보낸다.
        status, _, body = self.request(
            "POST",
            "/api/ping?closing=1",
            body=b"{}",
            headers={
                "Content-Type": "application/json",
                TOKEN_HEADER: self.server.session.token,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["closing"])
        self.assertIsNotNone(self.server.session.closed_at)
        self.get("/api/ping")
        self.assertIsNone(self.server.session.closed_at)

    def test_another_site_cannot_pretend_the_tab_is_closing(self) -> None:
        status, _, _ = self.request(
            "POST",
            "/api/ping?closing=1",
            body=b"{}",
            headers={"Content-Type": "application/json", "Origin": "http://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertIsNone(self.server.session.closed_at)

    def test_a_heartbeat_never_counts_as_use(self) -> None:
        session = self.server.session
        session.last_activity -= 120.0
        stale = session.last_activity
        self.get("/api/ping")
        self.assertEqual(session.last_activity, stale)
        self.get("/api/ping?keep=1")
        self.assertGreater(session.last_activity, stale)

    def test_real_requests_count_as_use(self) -> None:
        session = self.server.session
        session.last_activity -= 120.0
        stale = session.last_activity
        self.get("/api/slides")
        self.assertGreater(session.last_activity, stale)

    def test_shutdown_without_the_token_is_refused(self) -> None:
        status, _, body = self.post_json("/api/shutdown", {})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "FORBIDDEN")
        self.assertEqual(self.get("/api/ping")[0], 200)  # 서버는 그대로 살아 있다

    def test_shutdown_from_another_site_is_refused(self) -> None:
        status, _, _ = self.request(
            "POST",
            "/api/shutdown",
            body=b"{}",
            headers={
                "Content-Type": "application/json",
                TOKEN_HEADER: self.server.session.token,
                "Origin": "http://evil.example",
            },
        )
        self.assertEqual(status, 403)

    def test_the_page_can_stop_the_server(self) -> None:
        status, _, body = self.request(
            "POST",
            "/api/shutdown",
            body=b"{}",
            headers={
                "Content-Type": "application/json",
                TOKEN_HEADER: self.server.session.token,
                "Origin": f"http://{self.host}:{self.port}",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.thread.join(timeout=10)
        self.assertFalse(self.thread.is_alive(), "server did not stop after /api/shutdown")
        self.assertEqual(self.server.session.reason, "사용자 요청")

    def test_the_watchdog_retires_the_server_after_the_tab_closed(self) -> None:
        session = self.server.session
        session.tab_grace = 0.2
        session.note_tab_closed()
        self.assertIsNotNone(webapp.start_watchdog(self.server, interval=0.05))
        self.thread.join(timeout=10)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(session.reason, "탭을 닫아서")

    def test_the_watchdog_waits_for_work_in_progress(self) -> None:
        session = self.server.session
        session.tab_grace = 0.1
        session.note_tab_closed()
        session.enter()
        webapp.start_watchdog(self.server, interval=0.05)
        time.sleep(0.5)
        self.assertTrue(self.thread.is_alive(), "server stopped while work was in flight")
        session.leave()
        self.thread.join(timeout=10)
        self.assertFalse(self.thread.is_alive())

    def test_the_watchdog_can_be_disabled(self) -> None:
        self.server.session.auto_shutdown = False
        self.assertIsNone(webapp.start_watchdog(self.server, interval=0.05))


class SessionPageTests(WebAppTestCase):
    """The shutdown token reaches the page it was served to, and nowhere else."""

    def test_index_html_carries_the_token_and_no_placeholder(self) -> None:
        status, _, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(self.server.session.token.encode("utf-8"), body)
        self.assertNotIn(b"<!-- LYRICS2PPTX_SESSION -->", body)

    def test_the_scripts_do_not_carry_the_token(self) -> None:
        _, _, body = self.get("/static/app.js")
        self.assertNotIn(self.server.session.token.encode("utf-8"), body)


class StateFileTests(WebAppTestCase):
    """ADR-032: the launcher finds a running server through ``output/webui.state``."""

    def test_state_file_describes_the_running_server(self) -> None:
        target = webapp.write_state(self.server)
        self.assertIsNotNone(target)
        self.assertEqual(target, config.webui_state_path())
        lines = target.read_text(encoding="utf-8").split("\n")
        self.assertIn(f"instance={self.server.session.instance}", lines)
        self.assertIn(f"port={self.port}", lines)
        self.assertIn(f"url=http://127.0.0.1:{self.port}/", lines)

    def test_only_the_owning_instance_removes_the_file(self) -> None:
        webapp.write_state(self.server)
        webapp.clear_state("deadbeefdeadbeef")
        self.assertTrue(config.webui_state_path().exists())
        webapp.clear_state(self.server.session.instance)
        self.assertFalse(config.webui_state_path().exists())

    def test_a_missing_state_file_is_not_an_error(self) -> None:
        webapp.clear_state(self.server.session.instance)


class RevealTests(WebAppTestCase):
    """``/api/reveal`` opens the built file — and nothing outside ``output/``."""

    def _post_reveal(self, filename: str, *, token: bool = True, origin: str | None = None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers[TOKEN_HEADER] = self.server.session.token
        if origin:
            headers["Origin"] = origin
        body = json.dumps({"filename": filename}, ensure_ascii=False).encode("utf-8")
        return self.request("POST", "/api/reveal", body=body, headers=headers)

    def test_a_built_file_is_revealed(self) -> None:
        target = config.OUTPUT_DIR / "가사.pptx"
        target.write_bytes(b"pptx")
        with mock.patch.object(webapp, "_reveal_in_explorer") as reveal:
            status, _, body = self._post_reveal("가사.pptx")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(Path(reveal.call_args.args[0]), target.resolve())

    def test_a_name_outside_the_output_directory_is_sanitized_away(self) -> None:
        # sanitize_filename 은 구분자를 지우므로 ../../template.pptx 는 output 안의
        # 평범한 이름이 되고, 그 파일이 없으니 404 다(경로 탈출 자체가 불가능).
        status, _, _ = self._post_reveal("../../template.pptx")
        self.assertEqual(status, 404)
        status, _, _ = self._post_reveal("..\\..\\template.pptx")
        self.assertEqual(status, 404)

    def test_a_missing_file_is_a_404(self) -> None:
        status, _, _ = self._post_reveal("없는파일.pptx")
        self.assertEqual(status, 404)

    def test_only_the_served_page_may_open_a_folder(self) -> None:
        target = config.OUTPUT_DIR / "가사.pptx"
        target.write_bytes(b"pptx")
        with mock.patch.object(webapp, "_reveal_in_explorer") as reveal:
            self.assertEqual(self._post_reveal("가사.pptx", token=False)[0], 403)
            self.assertEqual(
                self._post_reveal("가사.pptx", origin="http://evil.example")[0], 403
            )
        reveal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
