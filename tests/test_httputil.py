"""Unit tests for the HTTP politeness layer (``crawl/httputil.py``).

Every test here is **offline and deterministic**: the transport
(``build_opener``/``_open_and_read``) is replaced, robots verdicts are injected,
and ``time.sleep`` is mocked so exponential backoff is asserted instead of
waited. Redirect hops are driven at the handler level, so M3 (a redirect must
not bypass robots/delay) is verified without opening a socket.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.request import Request

from lyrics2pptx import config
from lyrics2pptx.crawl import httputil
from lyrics2pptx.crawl.base import SourceBlockedError, SourceUnavailableError
from lyrics2pptx.errors import ConfigError

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ROBOTS_ALLOW_ALL = b"User-agent: *\nDisallow:\n"
ROBOTS_DISALLOW_ALL = b"User-agent: *\nDisallow: /\n"


class _FakeResponse:
    """Minimal stand-in for the response returned by ``OpenerDirector.open``."""

    def __init__(self, blob: bytes) -> None:
        self._blob = blob

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self, amount: int = -1) -> bytes:
        return self._blob


class _FakeOpener:
    """Minimal stand-in for the ``OpenerDirector`` returned by ``build_opener``."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.requests: list[Request] = []

    def open(self, request: Request, timeout: float | None = None) -> _FakeResponse:
        self.requests.append(request)
        return self._response


def _http_error(url: str, code: int) -> HTTPError:
    return HTTPError(url, code, f"HTTP {code}", None, None)


class PolitenessBase(unittest.TestCase):
    def setUp(self) -> None:
        httputil.reset_http_state()
        self.addCleanup(httputil.reset_http_state)
        patcher = mock.patch.object(config, "FIXTURE_DIR", None)
        patcher.start()
        self.addCleanup(patcher.stop)


class RobotsTests(PolitenessBase):
    def test_disallow_verdict_blocks_the_request(self) -> None:
        with mock.patch.object(httputil, "_open_and_read", return_value=ROBOTS_DISALLOW_ALL), mock.patch(
            "time.sleep"
        ):
            with self.assertRaises(SourceBlockedError):
                httputil.get_text("https://blocked.example.com/page")

    def test_allow_verdict_lets_the_request_through(self) -> None:
        responses = {"https://ok.example.com/robots.txt": ROBOTS_ALLOW_ALL}

        def fake_open(url: str, *, ua: str, timeout: float) -> bytes:
            if url in responses:
                return responses[url]
            return b"hello"

        with mock.patch.object(httputil, "_open_and_read", side_effect=fake_open), mock.patch(
            "time.sleep"
        ):
            self.assertEqual(httputil.get_text("https://ok.example.com/page"), "hello")

    def test_unreachable_robots_fails_closed(self) -> None:
        with mock.patch.object(httputil, "_open_and_read", side_effect=URLError("no dns")), mock.patch(
            "time.sleep"
        ):
            with self.assertRaises(SourceBlockedError):
                httputil.get_text("https://unreachable.example.com/page")

    def test_absent_robots_404_allows_access(self) -> None:
        # RFC 9309 §2.3.1.3: a 404 means there is no robots file, so access is
        # allowed. This is what makes sites without robots.txt usable.
        with mock.patch.object(
            httputil, "_open_and_read", side_effect=_http_error("https://missing.example.com/robots.txt", 404)
        ):
            self.assertTrue(httputil.allowed_by_robots("https://missing.example.com/page"))

    def test_gone_robots_410_allows_access(self) -> None:
        with mock.patch.object(
            httputil, "_open_and_read", side_effect=_http_error("https://gone.example.com/robots.txt", 410)
        ):
            self.assertTrue(httputil.allowed_by_robots("https://gone.example.com/page"))

    def test_robots_403_stays_blocked(self) -> None:
        with mock.patch.object(
            httputil, "_open_and_read", side_effect=_http_error("https://forbidden.example.com/robots.txt", 403)
        ):
            self.assertFalse(httputil.allowed_by_robots("https://forbidden.example.com/page"))

    def test_robots_500_stays_blocked(self) -> None:
        with mock.patch.object(
            httputil, "_open_and_read", side_effect=_http_error("https://broken.example.com/robots.txt", 500)
        ):
            self.assertFalse(httputil.allowed_by_robots("https://broken.example.com/page"))

    def test_robots_verdict_is_cached_so_robots_txt_is_fetched_once(self) -> None:
        calls: list[str] = []

        def fake_open(url: str, *, ua: str, timeout: float) -> bytes:
            calls.append(url)
            return ROBOTS_ALLOW_ALL

        with mock.patch.object(httputil, "_open_and_read", side_effect=fake_open), mock.patch(
            "time.sleep"
        ):
            self.assertTrue(httputil.allowed_by_robots("https://cache.example.com/a"))
            self.assertTrue(httputil.allowed_by_robots("https://cache.example.com/b"))
        self.assertEqual(sum(1 for url in calls if url.endswith("robots.txt")), 1)

    def test_non_http_url_is_allowed_without_a_lookup(self) -> None:
        with mock.patch.object(httputil, "_open_and_read", side_effect=AssertionError("network")):
            self.assertTrue(httputil.allowed_by_robots("not-a-url"))


class RetryTests(PolitenessBase):
    def test_429_is_retried_with_backoff_then_fails(self) -> None:
        error = _http_error("https://x.example.com/p", 429)
        calls: list[int] = []

        def fake_open(url: str, *, ua: str, timeout: float) -> bytes:
            calls.append(1)
            raise error

        with mock.patch.object(httputil, "_open_and_read", side_effect=fake_open), mock.patch.object(
            httputil, "allowed_by_robots", return_value=True
        ), mock.patch("time.sleep") as sleeper:
            with self.assertRaises(SourceUnavailableError):
                httputil.get_text("https://x.example.com/p")

        self.assertEqual(len(calls), config.MAX_RETRIES + 1)
        self.assertEqual([call.args[0] for call in sleeper.call_args_list], [1, 2, 4])

    def test_5xx_is_retried_then_fails(self) -> None:
        error = _http_error("https://x.example.com/p", 503)
        calls: list[int] = []

        def fake_open(url: str, *, ua: str, timeout: float) -> bytes:
            calls.append(1)
            raise error

        with mock.patch.object(httputil, "_open_and_read", side_effect=fake_open), mock.patch.object(
            httputil, "allowed_by_robots", return_value=True
        ), mock.patch("time.sleep") as sleeper:
            with self.assertRaises(SourceUnavailableError):
                httputil.get_text("https://x.example.com/p")
        self.assertEqual(len(calls), config.MAX_RETRIES + 1)
        self.assertEqual([call.args[0] for call in sleeper.call_args_list], [1, 2, 4])

    def test_403_blocks_immediately_without_retrying(self) -> None:
        self._assert_immediate_block(403)

    def test_404_blocks_immediately_without_retrying(self) -> None:
        self._assert_immediate_block(404)

    def _assert_immediate_block(self, status: int) -> None:
        calls: list[int] = []

        def fake_open(url: str, *, ua: str, timeout: float) -> bytes:
            calls.append(1)
            raise _http_error("https://x.example.com/p", status)

        with mock.patch.object(httputil, "_open_and_read", side_effect=fake_open), mock.patch.object(
            httputil, "allowed_by_robots", return_value=True
        ), mock.patch("time.sleep") as sleeper:
            with self.assertRaises(SourceBlockedError):
                httputil.get_text("https://x.example.com/p")
        self.assertEqual(len(calls), 1)
        sleeper.assert_not_called()

    def test_timeout_is_retried_then_fails(self) -> None:
        calls: list[int] = []

        def fake_open(url: str, *, ua: str, timeout: float) -> bytes:
            calls.append(1)
            raise TimeoutError("timed out")

        with mock.patch.object(httputil, "_open_and_read", side_effect=fake_open), mock.patch.object(
            httputil, "allowed_by_robots", return_value=True
        ), mock.patch("time.sleep") as sleeper:
            with self.assertRaises(SourceUnavailableError):
                httputil.get_text("https://x.example.com/p")
        self.assertEqual(len(calls), config.MAX_RETRIES + 1)
        self.assertEqual([call.args[0] for call in sleeper.call_args_list], [1, 2, 4])

    def test_unexpected_status_fails_without_retrying(self) -> None:
        calls: list[int] = []

        def fake_open(url: str, *, ua: str, timeout: float) -> bytes:
            calls.append(1)
            raise _http_error("https://x.example.com/p", 418)

        with mock.patch.object(httputil, "_open_and_read", side_effect=fake_open), mock.patch.object(
            httputil, "allowed_by_robots", return_value=True
        ), mock.patch("time.sleep"):
            with self.assertRaises(SourceUnavailableError):
                httputil.get_text("https://x.example.com/p")
        self.assertEqual(len(calls), 1)

    def test_transient_429_then_success_returns_the_body(self) -> None:
        sequence: list[object] = [_http_error("https://x.example.com/p", 429), b'{"ok": 1}']

        def fake_open(url: str, *, ua: str, timeout: float) -> bytes:
            item = sequence.pop(0)
            if isinstance(item, Exception):
                raise item
            return item  # type: ignore[return-value]

        with mock.patch.object(httputil, "_open_and_read", side_effect=fake_open), mock.patch.object(
            httputil, "allowed_by_robots", return_value=True
        ), mock.patch("time.sleep") as sleeper:
            self.assertEqual(httputil.get_json("https://x.example.com/p"), {"ok": 1})
        self.assertEqual([call.args[0] for call in sleeper.call_args_list], [1])
        self.assertEqual(sequence, [])

    def test_invalid_json_is_reported_as_unavailable(self) -> None:
        with mock.patch.object(
            httputil, "_open_and_read", return_value=b"<html>not json</html>"
        ), mock.patch.object(httputil, "allowed_by_robots", return_value=True), mock.patch("time.sleep"):
            with self.assertRaises(SourceUnavailableError):
                httputil.get_json("https://x.example.com/p")

    def test_json_array_payload_is_rejected(self) -> None:
        with mock.patch.object(
            httputil, "_open_and_read", return_value=b"[1, 2, 3]"
        ), mock.patch.object(httputil, "allowed_by_robots", return_value=True), mock.patch("time.sleep"):
            with self.assertRaises(SourceUnavailableError):
                httputil.get_json("https://x.example.com/p")


class HeaderTests(PolitenessBase):
    def test_user_agent_is_sent_and_never_spoofs_a_browser(self) -> None:
        opener = _FakeOpener(_FakeResponse(b'{"ok": true}'))

        with mock.patch.object(httputil, "build_opener", return_value=opener), mock.patch.object(
            httputil, "allowed_by_robots", return_value=True
        ), mock.patch("time.sleep"):
            self.assertEqual(httputil.get_json("https://api.example.com/v1/a/b"), {"ok": True})

        request = opener.requests[0]
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["user-agent"], httputil.DEFAULT_UA)
        self.assertNotIn("mozilla", headers["user-agent"].lower())
        self.assertEqual(headers["accept"], "application/json, text/plain, */*")


class DelayTests(PolitenessBase):
    def test_sleep_between_uses_the_configured_delay(self) -> None:
        with mock.patch("time.sleep") as sleeper:
            httputil.sleep_between()
        sleeper.assert_called_once_with(config.HTTP_DELAY)

    def test_sleep_between_honours_an_explicit_duration(self) -> None:
        with mock.patch("time.sleep") as sleeper:
            httputil.sleep_between(0.25)
        sleeper.assert_called_once_with(0.25)

    def test_minimum_delay_is_enforced_between_consecutive_requests(self) -> None:
        self.assertGreater(config.HTTP_DELAY, 0.0, "test precondition: default delay")
        with mock.patch.object(httputil, "_open_and_read", return_value=b"ok"), mock.patch.object(
            httputil, "allowed_by_robots", return_value=True
        ), mock.patch("time.sleep") as sleeper:
            httputil.get_text("https://delay.example.com/1")
            httputil.get_text("https://delay.example.com/2")
        waits = [call.args[0] for call in sleeper.call_args_list]
        self.assertTrue(waits, "no delay was applied to the second request")
        self.assertGreater(waits[0], 0.0)
        self.assertLessEqual(waits[0], config.HTTP_DELAY)


class FixtureModeTests(unittest.TestCase):
    """Offline hook: ``LYRICS2PPTX_FIXTURE_DIR`` -> read files, never the network."""

    def setUp(self) -> None:
        httputil.reset_http_state()
        self.addCleanup(httputil.reset_http_state)
        patcher = mock.patch.object(config, "FIXTURE_DIR", FIXTURES)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fixture_mode_never_touches_the_transport(self) -> None:
        with mock.patch.object(
            httputil, "_open_and_read", side_effect=AssertionError("network access")
        ), mock.patch.object(
            httputil, "allowed_by_robots", side_effect=AssertionError("robots access")
        ):
            self.assertEqual(
                httputil.get_json("https://example.com/api"),
                {"ok": True, "endpoint": "example"},
            )

    def test_explicit_fixture_name_is_read(self) -> None:
        payload = httputil.get_json("https://anything.example/x", fixture="lyrics_ovh_sample.json")
        self.assertIn("생명샘", payload["title"])

    def test_url_derived_fixture_name_is_read(self) -> None:
        # <netloc><path> slug -> example_com_api.json
        self.assertEqual(
            httputil.get_json("https://example.com/api"),
            {"ok": True, "endpoint": "example"},
        )

    def test_text_fixture_is_read_verbatim(self) -> None:
        text = httputil.get_text("https://example.com/x", fixture="lyrics_sample.txt")
        self.assertIn("[Verse 1]", text)

    def test_missing_fixture_raises_source_unavailable(self) -> None:
        with self.assertRaises(SourceUnavailableError):
            httputil.get_json("https://no-such-host.example/missing")

    def test_fixture_dir_none_raises_config_error_when_read_directly(self) -> None:
        # ``_read_fixture`` is guarded by callers; the guard itself is asserted.
        with mock.patch.object(config, "FIXTURE_DIR", None):
            with self.assertRaises(ConfigError):
                httputil._read_fixture("https://x/y", None, parse="json")


class RedirectGuardTests(PolitenessBase):
    """M3 — a redirect hop must pass the robots/delay gate again.

    ``urllib`` follows redirects internally, so a single ``30x`` response used
    to be enough to reach a host that ``robots.txt`` forbids. Every test here
    drives ``_GuardedRedirectHandler.redirect_request`` directly: no socket is
    opened and no real delay is ever waited.
    """

    FRESH = "https://start.example.com/0"

    def _handler(self) -> httputil._GuardedRedirectHandler:
        return httputil._GuardedRedirectHandler(ua=httputil.DEFAULT_UA)

    @staticmethod
    def _request(url: str) -> Request:
        return httputil._build_request(url, ua=httputil.DEFAULT_UA)

    def test_redirect_target_host_robots_disallow_blocks_the_hop(self) -> None:
        handler = self._handler()
        fetched: list[str] = []

        def fake_open(url: str, *, ua: str, timeout: float) -> bytes:
            fetched.append(url)
            if url == "https://blocked.example.com/robots.txt":
                return ROBOTS_DISALLOW_ALL
            return ROBOTS_ALLOW_ALL

        with mock.patch.object(httputil, "_open_and_read", side_effect=fake_open), mock.patch(
            "time.sleep"
        ):
            with self.assertRaises(SourceBlockedError):
                handler.redirect_request(
                    self._request(self.FRESH), None, 302, "Found", {}, "https://blocked.example.com/page"
                )
        # The verdict came from the *redirect target* host, not the original one.
        self.assertIn("https://blocked.example.com/robots.txt", fetched)

    def test_redirect_to_an_allowed_host_returns_the_next_request(self) -> None:
        handler = self._handler()
        with mock.patch.object(httputil, "allowed_by_robots", return_value=True), mock.patch.object(
            httputil, "_respect_delay"
        ), mock.patch("time.sleep"):
            new = handler.redirect_request(
                self._request(self.FRESH), None, 302, "Found", {}, "https://b.example.com/2"
            )
        self.assertIsNotNone(new)
        self.assertEqual(new.full_url, "https://b.example.com/2")  # type: ignore[union-attr]

    def test_every_hop_reapplies_robots_and_delay(self) -> None:
        handler = self._handler()
        hops = [f"https://h{i}.example.com/page" for i in (1, 2, 3)]
        request = self._request(self.FRESH)
        with mock.patch.object(httputil, "allowed_by_robots", return_value=True) as robots, mock.patch.object(
            httputil, "_respect_delay"
        ) as delay, mock.patch("time.sleep"):
            for url in hops:
                request = handler.redirect_request(request, None, 302, "Found", {}, url)  # type: ignore[assignment]
        self.assertEqual([call.args[0] for call in robots.call_args_list], hops)
        self.assertEqual([call.args[0] for call in delay.call_args_list], hops)

    def test_hop_count_above_the_cap_fails_closed(self) -> None:
        handler = self._handler()
        request = self._request(self.FRESH)
        with mock.patch.object(httputil, "allowed_by_robots", return_value=True), mock.patch.object(
            httputil, "_respect_delay"
        ), mock.patch("time.sleep"):
            for hop in range(httputil.MAX_REDIRECTS):
                request = handler.redirect_request(
                    request, None, 302, "Found", {}, f"https://hop{hop}.example.com/x"
                )  # type: ignore[assignment]
            with self.assertRaises(SourceUnavailableError):
                handler.redirect_request(
                    request, None, 302, "Found", {}, "https://over-the-cap.example.com/x"
                )

    def test_hop_cap_is_a_named_constant(self) -> None:
        self.assertIsInstance(httputil.MAX_REDIRECTS, int)
        self.assertGreater(httputil.MAX_REDIRECTS, 0)

    def test_immediate_loop_back_to_the_origin_is_detected(self) -> None:
        handler = self._handler()
        with mock.patch.object(httputil, "allowed_by_robots", return_value=True), mock.patch.object(
            httputil, "_respect_delay"
        ), mock.patch("time.sleep"):
            with self.assertRaises(SourceUnavailableError):
                handler.redirect_request(
                    self._request(self.FRESH), None, 302, "Found", {}, self.FRESH
                )

    def test_two_hop_loop_is_detected(self) -> None:
        handler = self._handler()
        with mock.patch.object(httputil, "allowed_by_robots", return_value=True), mock.patch.object(
            httputil, "_respect_delay"
        ), mock.patch("time.sleep"):
            first = "https://a.example.com/1"
            second = handler.redirect_request(
                self._request(first), None, 302, "Found", {}, "https://b.example.com/2"
            )
            with self.assertRaises(SourceUnavailableError):
                handler.redirect_request(second, None, 302, "Found", {}, first)

    def test_open_chain_routes_the_redirect_through_the_guard(self) -> None:
        """The real ``build_opener`` chain must use our handler, not the default.

        ``OpenerDirector.error('http', ..., 302, ...)`` runs the stdlib
        ``http_error_302`` → ``redirect_request`` path; because the guard raises
        first, ``fp`` is never read and no socket is touched.
        """

        def fake_open(url: str, *, ua: str, timeout: float) -> bytes:
            if url == "https://blocked.example.com/robots.txt":
                return ROBOTS_DISALLOW_ALL
            return ROBOTS_ALLOW_ALL

        opener = httputil.build_opener(httputil._GuardedRedirectHandler(ua=httputil.DEFAULT_UA))
        request = self._request("https://allowed.example.com/start")
        with mock.patch.object(httputil, "_open_and_read", side_effect=fake_open), mock.patch(
            "time.sleep"
        ):
            with self.assertRaises(SourceBlockedError):
                opener.error(
                    "http", request, None, 302, "Found", {"location": "https://blocked.example.com/page"}
                )


if __name__ == "__main__":
    unittest.main()
