"""Blog collector tests (extraction + collection), fully offline.

The HTML fixtures reproduce the shapes the live reconnaissance found on tistory
and Naver blogs: verse numbers between stanzas, ``&amp;nbsp;`` escaping, link
lists of other posts, and a page whose lyrics are an image (menu only).
"""

from __future__ import annotations

import unittest
from unittest import mock

from lyrics2pptx import config
from lyrics2pptx.crawl import blog
from lyrics2pptx.crawl.base import RawSong, SourceNotFoundError
from lyrics2pptx.crawl.websearch.base import SearchResult

LYRIC_PAGE = """
<html><body>
<h1><a href="/post/1">지금은 엘리야 때처럼</a></h1>
<div class="entry">
  1.
  <p>주 말씀이 선포되고<br>또 주의 종 모세의 때와 같이<br>언약이 성취되네</p>
  2.
  <p>환난날이 다가와도<br>우리는 광야의 외치는 소리<br>주의 길을 예비하라</p>
  3.
  <p>에스겔의 환상처럼<br>마른 뼈가 살아나며<br>예배가 회복되네</p>
</div>
<div class="related">
  <a href="/x">다른 노래 제목</a>
  <a href="/y">또 다른 노래</a>
</div>
<p>공유하기</p>
</body></html>
"""

DOUBLE_ESCAPED_PAGE = """
<html><body><div>
주 말씀이 선포되고<br>&amp;nbsp;<br>또 주의 종 모세의 때와 같이<br>
언약이 성취되네<br>에스겔의 환상처럼<br>마른 뼈가 살아나며<br>예배가 회복되네
</div></body></html>
"""

IMAGE_ONLY_PAGE = """
<html><body><div class="menu">
""" + "".join(
    f'<a href="/p{i}">가사</a><a href="/q{i}">악보</a>' for i in range(12)
) + """
</div><img src="lyrics.png"/></body></html>
"""

PAGES = {
    "https://a.example/1": LYRIC_PAGE,
    "https://b.example/2": LYRIC_PAGE,
    "https://c.example/3": DOUBLE_ESCAPED_PAGE,
}


class FakeBackend:
    name = "fake"

    def __init__(self, results: list[SearchResult]) -> None:
        self.results = results
        self.queries: list[str] = []

    def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        self.queries.append(query)
        return self.results[:limit]


class ExtractLyricsTests(unittest.TestCase):
    def test_verse_numbers_do_not_split_the_run(self) -> None:
        lines = blog.extract_lyrics(LYRIC_PAGE)
        self.assertEqual(len(lines), 9)
        self.assertEqual(lines[0], "주 말씀이 선포되고")
        self.assertEqual(lines[-1], "예배가 회복되네")

    def test_related_post_links_end_the_run(self) -> None:
        lines = blog.extract_lyrics(LYRIC_PAGE)
        self.assertNotIn("다른 노래 제목", lines)

    def test_double_escaped_nbsp_does_not_truncate_the_run(self) -> None:
        lines = blog.extract_lyrics(DOUBLE_ESCAPED_PAGE)
        self.assertEqual(len(lines), 6)
        self.assertEqual(lines[-1], "예배가 회복되네")

    def test_image_only_page_yields_no_lyrics(self) -> None:
        self.assertEqual(blog.extract_lyrics(IMAGE_ONLY_PAGE), ())

    def test_short_page_is_rejected(self) -> None:
        self.assertEqual(blog.extract_lyrics("<p>한 줄뿐</p>"), ())


class StripHeadingsTests(unittest.TestCase):
    def test_leading_title_and_heading_are_removed(self) -> None:
        lines = ("가사", "지금은 엘리야 때처럼", "주 말씀이 선포되고")
        stripped = blog._strip_headings(lines, title_key="지금은엘리야때처럼")
        self.assertEqual(stripped, ("주 말씀이 선포되고",))

    def test_trailing_title_is_removed(self) -> None:
        lines = ("주 말씀이 선포되고", "지금은 엘리야 때처럼")
        stripped = blog._strip_headings(lines, title_key="지금은엘리야때처럼")
        self.assertEqual(stripped, ("주 말씀이 선포되고",))


class CollectAndVoteTests(unittest.TestCase):
    def _run(self, results: list[SearchResult], pages: dict[str, str] = None):
        pages = PAGES if pages is None else pages
        backend = FakeBackend(results)

        def fake_get_text(url: str) -> str:
            if url not in pages:
                raise SourceNotFoundError(f"no fixture for {url}")
            return pages[url]

        with mock.patch("lyrics2pptx.crawl.blog.get_backend", return_value=backend), mock.patch.object(
            blog.httputil, "get_text", side_effect=fake_get_text
        ):
            consensus, _pages = blog.collect_and_vote("지금은 엘리야 때처럼")
        return consensus, backend

    def test_query_gets_a_lyrics_suffix(self) -> None:
        _result, backend = self._run([SearchResult("t", "https://a.example/1")])
        self.assertEqual(backend.queries, ["지금은 엘리야 때처럼 가사"])

    def test_identical_pages_count_as_one_vote(self) -> None:
        consensus, _backend = self._run(
            [
                SearchResult("t", "https://a.example/1"),
                SearchResult("t", "https://b.example/2"),
            ]
        )
        self.assertEqual(consensus.source_count, 1)

    def test_consensus_keeps_the_majority_spelling(self) -> None:
        consensus, _backend = self._run(
            [
                SearchResult("t", "https://a.example/1"),
                SearchResult("t", "https://b.example/2"),
                SearchResult("t", "https://c.example/3"),
            ]
        )
        self.assertEqual(consensus.source_count, 2)
        self.assertIn("에스겔의 환상처럼", consensus.lines)

    def test_no_usable_page_raises_not_found(self) -> None:
        backend = FakeBackend([SearchResult("t", "https://missing.example/1")])
        with mock.patch("lyrics2pptx.crawl.blog.get_backend", return_value=backend), mock.patch.object(
            blog.httputil,
            "get_text",
            side_effect=SourceNotFoundError("no fixture"),
        ):
            with self.assertRaises(SourceNotFoundError):
                blog.collect_and_vote("없는노래")

    def test_empty_query_is_a_config_error(self) -> None:
        from lyrics2pptx.errors import ConfigError

        with self.assertRaises(ConfigError):
            blog.collect_and_vote("   ")


class BlogLyricsSourceTests(unittest.TestCase):
    def test_search_returns_the_consensus_first(self) -> None:
        backend = FakeBackend(
            [
                SearchResult("t", "https://a.example/1"),
                SearchResult("t", "https://c.example/3"),
            ]
        )

        def fake_get_text(url: str) -> str:
            return PAGES[url]

        with mock.patch("lyrics2pptx.crawl.blog.get_backend", return_value=backend), mock.patch.object(
            blog.httputil, "get_text", side_effect=fake_get_text
        ):
            songs = blog.BlogLyricsSource().search("지금은 엘리야 때처럼")

        self.assertIsInstance(songs[0], RawSong)
        self.assertEqual(songs[0].source_name, "blog")
        self.assertTrue(songs[0].artist.startswith("교차검증"))
        self.assertIn("에스겔의 환상처럼", songs[0].lyrics)
        self.assertEqual(len(songs), 3)  # consensus + two pages

    def test_search_honours_the_limit(self) -> None:
        backend = FakeBackend(
            [
                SearchResult("t", "https://a.example/1"),
                SearchResult("t", "https://c.example/3"),
            ]
        )
        with mock.patch("lyrics2pptx.crawl.blog.get_backend", return_value=backend), mock.patch.object(
            blog.httputil, "get_text", side_effect=lambda url: PAGES[url]
        ):
            songs = blog.BlogLyricsSource().search("지금은 엘리야 때처럼", limit=1)
        self.assertEqual(len(songs), 1)


class ConfigDefaultTests(unittest.TestCase):
    def test_blog_page_budget_is_small(self) -> None:
        # Every page costs an HTTP_DELAY pause, so the default must stay modest.
        self.assertLessEqual(config.BLOG_MAX_PAGES, 5)
