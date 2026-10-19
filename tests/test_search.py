"""Search-backend tests (DDG parsing + registry), fully offline.

The HTML fixtures mirror the two DuckDuckGo result shapes we rely on: the
``html`` page (``result__a`` + ``result__snippet``) and the ``lite`` page, where
links are still wrapped in ``/l/?uddg=``. No socket is opened.
"""

from __future__ import annotations

import unittest
from unittest import mock

from lyrics2pptx.crawl.base import SourceNotFoundError, SourceUnavailableError
from lyrics2pptx.crawl.websearch import available_backends, get_backend
from lyrics2pptx.crawl.websearch.ddg import DdgSearch, parse_results
from lyrics2pptx.errors import ConfigError

HTML_PAGE = """
<html><body>
<a rel="nofollow" class="result__a"
   href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fsong&amp;rut=abc">
   Example <b>song</b> lyrics</a>
<div class="result__snippet">first &amp; snippet</div>
<a rel="nofollow" class="result__a" href="https://other.kr/lyrics">Other page</a>
<div class="result__snippet">second snippet</div>
<a class="result__a" href="//duckduckgo.com/html/?q=more&amp;s=20">next page</a>
</body></html>
"""

LITE_PAGE = """
<html><body><table>
<a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Flite.example%2Fpost" class="result-link">Lite result</a>
<td class="result-snippet">lite snippet</td>
</table></body></html>
"""


class ParseResultsTests(unittest.TestCase):
    def test_results_are_unwrapped_detagged_and_snippet_paired(self) -> None:
        results = parse_results(HTML_PAGE)
        self.assertEqual([item.url for item in results], ["https://example.com/song", "https://other.kr/lyrics"])
        self.assertEqual(results[0].title, "Example song lyrics")
        self.assertEqual(results[0].snippet, "first & snippet")
        self.assertEqual(results[1].snippet, "second snippet")

    def test_duckduckgo_navigation_links_are_ignored(self) -> None:
        results = parse_results(HTML_PAGE)
        self.assertNotIn("duckduckgo.com", "".join(item.url for item in results))

    def test_lite_markup_is_parsed(self) -> None:
        results = parse_results(LITE_PAGE)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://lite.example/post")
        self.assertEqual(results[0].snippet, "lite snippet")

    def test_page_without_results_yields_empty_list(self) -> None:
        self.assertEqual(parse_results("<html><body>no results</body></html>"), [])


class RegistryTests(unittest.TestCase):
    def test_only_robots_allowed_backends_are_registered(self) -> None:
        self.assertEqual(available_backends(), ("ddg",))

    def test_get_backend_defaults_to_the_configured_backend(self) -> None:
        backend = get_backend()
        self.assertIsInstance(backend, DdgSearch)
        self.assertEqual(backend.name, "ddg")

    def test_unknown_backend_lists_the_known_names(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            get_backend("naver")
        self.assertIn("ddg", str(ctx.exception))


class DdgSearchTests(unittest.TestCase):
    def test_search_fetches_the_html_endpoint_and_limits_results(self) -> None:
        with mock.patch.object(
            DdgSearch, "_fetch", autospec=True, return_value=HTML_PAGE
        ) as fetch:
            results = DdgSearch().search("노래", limit=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(fetch.call_count, 1)
        url = fetch.call_args.args[1]
        self.assertTrue(url.startswith("https://html.duckduckgo.com/html/?q="))

    def test_lite_endpoint_is_used_when_the_first_endpoint_fails(self) -> None:
        with mock.patch.object(
            DdgSearch,
            "_fetch",
            autospec=True,
            side_effect=[SourceUnavailableError("blocked"), LITE_PAGE],
        ):
            results = DdgSearch().search("노래")
        self.assertEqual([item.url for item in results], ["https://lite.example/post"])

    def test_no_results_at_either_endpoint_is_not_found(self) -> None:
        with mock.patch.object(DdgSearch, "_fetch", autospec=True, return_value="<html></html>"):
            with self.assertRaises(SourceNotFoundError):
                DdgSearch().search("없는노래")

    def test_empty_query_is_a_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            DdgSearch().search("   ")


class SearchRequestTests(unittest.TestCase):
    def test_search_performs_http_through_the_politeness_layer(self) -> None:
        with mock.patch(
            "lyrics2pptx.crawl.websearch.ddg.httputil.get_text", return_value=HTML_PAGE
        ) as get_text:
            DdgSearch().search("노래")
        self.assertEqual(get_text.call_count, 1)
        self.assertIn("html.duckduckgo.com", get_text.call_args.args[0])
