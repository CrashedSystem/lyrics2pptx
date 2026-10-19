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
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from pptx import Presentation

from lyrics2pptx import config, pipeline
from lyrics2pptx.webapp import build_server

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TEMPLATE = Path(__file__).resolve().parents[1] / "template.pptx"
PPTX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)


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


if __name__ == "__main__":
    unittest.main()
