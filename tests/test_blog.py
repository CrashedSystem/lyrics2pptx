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

# gasazip.com put eleven furniture lines (a search box, a login form, share
# buttons) directly above the song; every one of them used to be transcribed as a
# lyric line (ADR-020).
FURNITURE_PAGE = """
<html><body>
<p>가사 검색</p>
<p>검색</p>
<p>회원아이디 필수</p>
<p>비밀번호 필수</p>
<p>가사 등록</p>
<p>싱크 가사 등록</p>
<h1><a href="/p">지금은 엘리야 때처럼</a></h1>
<p>AI 해석</p>
<p>가사 카드</p>
<p>이 가수 듣기</p>
<p>공유</p>
<p>보관</p>
<div class="lyrics">
주 말씀이 선포되고<br>또 주의 종 모세의 때와 같이<br>언약이 성취되네<br>
에스겔의 환상처럼<br>마른 뼈가 살아나며<br>예배가 회복되네
</div>
<p>관련 가사</p>
<p>다른 노래 제목</p>
<p>또 다른 노래</p>
</body></html>
"""

# The naver post that reached the vote for '정금같이 나오리라': Job 23:1-9, i.e. a
# Bible chapter with the verse numbers kept inside the line.
VERSE_NUMBERED_PAGE = """
<html><body><div>
1 욥이 대답하여 이르되<br>
2 오늘도 내게 반항하는 마음과 근심이 있나니<br>
3 내가 어찌하면 하나님을 발견하고 그의 처소에 나아가랴<br>
4 어찌하면 그 앞에서 내가 호소하며 변론할 말을 내 입에 채우고<br>
5 내게 대답하시는 말씀을 내가 알며<br>
6 그가 큰 권능을 가지시고 나와 더불어 다투시겠느냐
</div></body></html>
"""

# bible.com's verse page: four chrome lines around the single line the song
# happens to quote, which is why it looked like a match.
SCRIPTURE_CHROME_PAGE = """
<html><body>
<p>한국어</p>
<p>공유</p>
<p>나의 가는 길을 오직 그가 아시나니 그가 나를 단련하신 후에는 내가 정금 같이 나오리라</p>
<p>모든 번역본 비교 : 욥기 23:10</p>
<p>욥기 23:10: 관련 무료 묵상 계획</p>
<p>고난은 있어도 절망은 없다 (김상복 목사)</p>
</body></html>
"""

# Sync-format notation: the site prints each line twice. Collapsing it must not
# touch a short lyric repeat. The two halves below differ, so the *block* rule
# does not apply and only the phrase duplication is collapsed.
DOUBLED_PHRASE_PAGE = """
<html><body><div>
내가 정금같이 나오리라 내가 정금같이 나오리라<br>
나의 가는 길은 하나님이 아시나니<br>
그가 나를 연단하신 후에는 그가 나를 연단하신 후에는<br>
사랑해 사랑해<br>
주를 찬양하리라 주를 찬양하리라<br>
주의 은혜라<br>
너의 삶에 고난 있어도<br>
너는 담대하라<br>
나의 가는 길을 오직 그가 아시나니<br>
때가 되면 높이리라<br>
주님이 함께 하시니<br>
두려울 것이 없네
</div></body></html>
"""

# gasazip.com's layout: the same block above and below a "영상 보기" button.
DOUBLED_BLOCK_PAGE = """
<html><body><div>
나의 가는 길은 하나님이 아시나니<br>
나의 가는 길을 오직 그가 아시나니<br>
그가 나를 연단하신 후에는<br>
내가 정금같이 나오리라<br>
내가 정금같이 나오리라<br>
정금같이 나오리라<br>
영상 보기<br>
나의 가는 길은 하나님이 아시나니<br>
나의 가는 길을 오직 그가 아시나니<br>
그가 나를 연단하신 후에는<br>
내가 정금같이 나오리라<br>
내가 정금같이 나오리라<br>
정금같이 나오리라
</div></body></html>
"""

OTHER_SONG_PAGE = """
<html><body><div>
봄바람이 불어오는 언덕에서<br>
우리는 서로 손을 잡았네<br>
여름밤 별이 쏟아지는 곳에<br>
다시 만나 노래를 부르리<br>
가을비 내리는 골목길에서<br>
겨울 눈이 쌓인 창가에서
</div></body></html>
"""

# tistory's notice/category sidebar, which the extractor read as that page's
# lyrics because every line ends with the site's own counter in brackets. It was
# offered as candidate [1] for '아주 먼 옛날' and clicking it produced eleven
# slides of page furniture (ADR-021).
SIDEBAR_NOTICE_PAGE = """
<html><body><div>
<p>분류 전체보기 (8015)</p>
<p>메일로 (0)</p>
<p>요청해도 (0)</p>
<p>보내지 (0)</p>
<p>않아요 (0)</p>
<p>찬송가는 (0)</p>
<p>문제로 (0)</p>
<p>더이상 (0)</p>
<p>공유하지 (0)</p>
<p>않습니다 (0)</p>
<p>어린이찬송가 (478)</p>
</div></body></html>
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


class PagePlausibilityTests(unittest.TestCase):
    """A page must look like a transcription before its lines may vote (ADR-020).

    Each fixture is the shape that actually reached the vote for
    '정금같이 나오리라' (or would have, had the extractor been this strict).
    """

    def test_page_furniture_is_not_transcribed_as_lyrics(self) -> None:
        self.assertEqual(
            blog.extract_lyrics(FURNITURE_PAGE),
            (
                "주 말씀이 선포되고",
                "또 주의 종 모세의 때와 같이",
                "언약이 성취되네",
                "에스겔의 환상처럼",
                "마른 뼈가 살아나며",
                "예배가 회복되네",
            ),
        )

    def test_related_songs_after_the_heading_are_dropped(self) -> None:
        lines = blog.extract_lyrics(FURNITURE_PAGE)
        for junk in ("관련 가사", "다른 노래 제목", "또 다른 노래", "가사 카드", "공유"):
            self.assertNotIn(junk, lines)

    def test_numbered_verses_are_a_passage_not_a_song(self) -> None:
        self.assertEqual(blog.extract_lyrics(VERSE_NUMBERED_PAGE), ())

    def test_verse_page_chrome_leaves_nothing_to_vote(self) -> None:
        self.assertEqual(blog.extract_lyrics(SCRIPTURE_CHROME_PAGE), ())

    def test_catalogue_page_is_rejected(self) -> None:
        # allklyrics.com listed 353 other songs; 120 is enough to prove the cap.
        rows = "".join(f"<p>{index}번째 다른 노래 제목입니다</p>" for index in range(120))
        self.assertEqual(blog.extract_lyrics(f"<html><body>{rows}</body></html>"), ())

    def test_sync_duplicated_phrase_is_collapsed(self) -> None:
        lines = blog.extract_lyrics(DOUBLED_PHRASE_PAGE)
        self.assertIn("내가 정금같이 나오리라", lines)
        self.assertNotIn("내가 정금같이 나오리라 내가 정금같이 나오리라", lines)

    def test_short_lyric_repeat_is_not_collapsed(self) -> None:
        self.assertIn("사랑해 사랑해", blog.extract_lyrics(DOUBLED_PHRASE_PAGE))

    def test_block_printed_twice_is_collapsed_to_one_copy(self) -> None:
        lines = blog.extract_lyrics(DOUBLED_BLOCK_PAGE)
        self.assertEqual(len(lines), 6)
        self.assertEqual(lines.count("나의 가는 길은 하나님이 아시나니"), 1)

    def test_a_site_sidebar_is_not_a_transcription(self) -> None:
        self.assertEqual(blog.extract_lyrics(SIDEBAR_NOTICE_PAGE), ())

    def test_a_bracketed_count_is_not_a_lyric_but_an_adlib_is(self) -> None:
        self.assertFalse(blog._is_lyric_line("메일로 (0)"))
        self.assertFalse(blog._is_lyric_line("어린이찬송가 (478)"))
        self.assertTrue(blog._is_lyric_line("사랑해요 (사랑해요 영원히)"))


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

    def test_collect_reports_page_progress(self) -> None:
        # The web UI turns these into the progress bar; a silent collect is a
        # frozen bar, so the lines are part of the collector's contract.
        from lyrics2pptx.crawl import progress

        backend = FakeBackend(
            [
                SearchResult("t", "https://a.example/1"),
                SearchResult("t", "https://c.example/3"),
            ]
        )
        seen: list = []
        progress.set_sink(seen.append)
        self.addCleanup(progress.set_sink, None)
        with mock.patch("lyrics2pptx.crawl.blog.get_backend", return_value=backend), mock.patch.object(
            blog.httputil, "get_text", side_effect=lambda url: PAGES[url]
        ):
            blog.collect_and_vote("지금은 엘리야 때처럼", max_pages=2)

        messages = [update.message for update in seen]
        self.assertEqual(messages[0], "블로그 검색 중…")
        self.assertIn("후보 2건에서 가사를 확인합니다", messages)
        self.assertTrue(any(m.endswith("a.example 페이지 확인 중…") for m in messages))
        self.assertTrue(any(m.startswith("1/2 ") for m in messages))
        self.assertTrue(any("교차검증 완료" in m for m in messages))
        # Only the steps whose total is actually known carry a percentage.
        self.assertIsNone(seen[0].percent)
        page_updates = [u for u in seen if u.message.endswith("페이지 확인 중…")]
        self.assertTrue(page_updates)
        self.assertTrue(all(u.percent is not None for u in page_updates))


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


class CandidateLabelTests(unittest.TestCase):
    """The list must say which page is which, and whether it really voted."""

    def _search(self, results: list[SearchResult], pages: dict[str, str]):
        backend = FakeBackend(results)
        with mock.patch(
            "lyrics2pptx.crawl.blog.get_backend", return_value=backend
        ), mock.patch.object(blog.httputil, "get_text", side_effect=lambda url: pages[url]):
            return blog.BlogLyricsSource().search("지금은 엘리야 때처럼", limit=10)

    def test_page_candidate_carries_host_and_size(self) -> None:
        songs = self._search(
            [SearchResult("t", "https://a.example/1")],
            {"https://a.example/1": LYRIC_PAGE},
        )
        self.assertEqual(songs[1].artist, "a.example · 9줄")

    def test_agreeing_pages_are_labeled_as_cross_checked(self) -> None:
        songs = self._search(
            [
                SearchResult("t", "https://a.example/1"),
                SearchResult("t", "https://c.example/3"),
            ],
            {
                "https://a.example/1": LYRIC_PAGE,
                "https://c.example/3": DOUBLE_ESCAPED_PAGE,
            },
        )
        self.assertEqual(songs[0].artist, "교차검증 2곳")

    def test_disagreeing_pages_are_labeled_as_not_cross_checked(self) -> None:
        songs = self._search(
            [
                SearchResult("t", "https://a.example/1"),
                SearchResult("t", "https://b.example/2"),
            ],
            {
                "https://a.example/1": LYRIC_PAGE,
                "https://b.example/2": OTHER_SONG_PAGE,
            },
        )
        self.assertEqual(songs[0].artist, "교차검증 안 됨 — 후보 2개가 서로 다름")

    def test_a_page_the_vote_refused_is_labeled_and_sorted_last(self) -> None:
        songs = self._search(
            [
                SearchResult("t", "https://a.example/1"),
                SearchResult("t", "https://b.example/2"),
            ],
            {
                "https://a.example/1": LYRIC_PAGE,
                "https://b.example/2": OTHER_SONG_PAGE,
            },
        )
        # index 1 is a page that really voted, so the first thing a user clicks is
        # a transcription; the refused page is still offered, but it says so.
        self.assertEqual(songs[0].source_url, "https://a.example/1")
        self.assertEqual(songs[1].artist, "a.example · 9줄")
        self.assertEqual(songs[2].artist, "b.example · 6줄 (투표 제외)")

    def test_the_refused_page_stays_last_whatever_the_arrival_order(self) -> None:
        pages = {
            "https://a.example/1": LYRIC_PAGE,
            "https://b.example/2": OTHER_SONG_PAGE,
        }
        forward = self._search(
            [SearchResult("t", "https://a.example/1"), SearchResult("t", "https://b.example/2")],
            pages,
        )
        backward = self._search(
            [SearchResult("t", "https://b.example/2"), SearchResult("t", "https://a.example/1")],
            pages,
        )
        self.assertEqual([song.artist for song in forward], [song.artist for song in backward])
        self.assertEqual([song.source_url for song in forward], [song.source_url for song in backward])

    def test_not_cross_checked_is_announced_while_collecting(self) -> None:
        from lyrics2pptx.crawl import progress

        backend = FakeBackend(
            [
                SearchResult("t", "https://a.example/1"),
                SearchResult("t", "https://b.example/2"),
            ]
        )
        pages = {
            "https://a.example/1": LYRIC_PAGE,
            "https://b.example/2": OTHER_SONG_PAGE,
        }
        seen: list = []
        progress.set_sink(seen.append)
        self.addCleanup(progress.set_sink, None)
        with mock.patch(
            "lyrics2pptx.crawl.blog.get_backend", return_value=backend
        ), mock.patch.object(blog.httputil, "get_text", side_effect=lambda url: pages[url]):
            blog.collect_and_vote("지금은 엘리야 때처럼", max_pages=2)

        messages = [update.message for update in seen]
        self.assertTrue(any("교차검증 안 됨" in message for message in messages))
        self.assertFalse(any("교차검증 완료" in message for message in messages))

    def test_candidate_order_does_not_follow_arrival_order(self) -> None:
        pages = {
            "https://a.example/1": LYRIC_PAGE,
            "https://b.example/2": OTHER_SONG_PAGE,
        }
        forward = self._search(
            [
                SearchResult("t", "https://a.example/1"),
                SearchResult("t", "https://b.example/2"),
            ],
            pages,
        )
        backward = self._search(
            [
                SearchResult("t", "https://b.example/2"),
                SearchResult("t", "https://a.example/1"),
            ],
            pages,
        )
        self.assertEqual(
            [song.source_url for song in forward],
            [song.source_url for song in backward],
        )


class ConfigDefaultTests(unittest.TestCase):
    def test_blog_page_budget_is_small(self) -> None:
        # Every page costs an HTTP_DELAY pause, so the default must stay modest.
        self.assertLessEqual(config.BLOG_MAX_PAGES, 5)
