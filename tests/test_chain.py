"""Chain tests (``auto``): priority tiers (ADR-025) and Korean-first (ADR-026).

Every test is offline; members are fakes resolved through ``crawl.get_source``.
"""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from lyrics2pptx import config
from lyrics2pptx.crawl import progress
from lyrics2pptx.crawl.base import (
    RawSong,
    SourceBlockedError,
    SourceNotFoundError,
    SourceUnavailableError,
)
from lyrics2pptx.crawl.chain import ChainSource, is_korean_text

KOREAN = "주는 생명샘\n마르지 않는 샘"
ENGLISH = "Amazing Grace\nHow sweet the sound"

HIT = [RawSong(title="생명샘", artist="마커스", lyrics=KOREAN, source_name="lrclib")]
EN_HIT = [
    RawSong(title="Amazing Grace", artist="Dave Fenley", lyrics=ENGLISH, source_name="lrclib")
]


def _blog(lyrics: str = KOREAN, *, cross_checked: bool) -> list[RawSong]:
    return [
        RawSong(
            title="생명샘",
            artist="교차검증 3곳" if cross_checked else "교차검증 안 됨 — 페이지 1장",
            lyrics=lyrics,
            source_name="blog",
            cross_checked=cross_checked,
        )
    ]


class FakeSource:
    def __init__(self, name: str, *, songs=None, error=None) -> None:
        self.name = name
        self._songs = songs
        self._error = error
        self.calls: list[str] = []

    def search(self, query: str, *, limit: int = 5):
        self.calls.append(query)
        if self._error is not None:
            raise self._error
        return list(self._songs or [])


def _resolve(sources):
    return mock.patch(
        "lyrics2pptx.crawl.get_source", side_effect=lambda name: sources[name]
    )


class ChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lrclib = FakeSource("lrclib")
        self.blog = FakeSource("blog")
        self.sources = {"lrclib": self.lrclib, "blog": self.blog}

    def _search(self, *, lrclib=None, blog=None) -> list[RawSong]:
        if lrclib is not None:
            self.lrclib = FakeSource("lrclib", **lrclib)
        if blog is not None:
            self.blog = FakeSource("blog", **blog)
        self.sources.update({"lrclib": self.lrclib, "blog": self.blog})
        with _resolve(self.sources):
            return ChainSource().search("생명샘")

    def test_lrclib_wins_when_the_blog_has_nothing(self) -> None:
        songs = self._search(lrclib={"songs": HIT})
        self.assertEqual(songs, HIT)
        # ADR-025: both members are asked up front; the loser is not "skipped".
        self.assertEqual(self.blog.calls, ["생명샘"])

    def test_falls_through_to_blog_when_lrclib_has_nothing(self) -> None:
        songs = self._search(
            lrclib={"error": SourceNotFoundError("no")},
            blog={"songs": _blog(cross_checked=False)},
        )
        self.assertEqual(len(songs), 1)
        self.assertEqual(self.lrclib.calls, ["생명샘"])
        self.assertEqual(self.blog.calls, ["생명샘"])

    def test_missing_fixture_does_not_prevent_the_fallback(self) -> None:
        songs = self._search(
            lrclib={"error": SourceUnavailableError("no fixture")},
            blog={"songs": _blog(cross_checked=True)},
        )
        self.assertEqual(len(songs), 1)

    def test_not_found_wins_over_a_transient_failure(self) -> None:
        # The actionable message is "검색 결과가 없습니다 → 직접 붙여넣기", so a
        # member that definitively found nothing decides the user-facing error.
        with self.assertRaises(SourceNotFoundError):
            self._search(
                lrclib={"error": SourceNotFoundError("no")},
                blog={"error": SourceUnavailableError("fixture missing")},
            )

    def test_all_blocked_reports_blocked(self) -> None:
        with self.assertRaises(SourceBlockedError):
            self._search(
                lrclib={"error": SourceBlockedError("robots")},
                blog={"error": SourceBlockedError("robots")},
            )

    def test_empty_results_are_treated_as_not_found(self) -> None:
        with self.assertRaises(SourceNotFoundError):
            self._search()

    def test_source_name_is_auto(self) -> None:
        self.assertEqual(ChainSource().name, "auto")

    def test_chain_reports_which_tier_was_picked(self) -> None:
        # The web UI reads these lines; a silent chain means a frozen bar.
        seen: list = []
        progress.set_sink(seen.append)
        self.addCleanup(progress.set_sink, None)
        self._search(
            lrclib={"songs": HIT},
            blog={"songs": _blog(cross_checked=True)},
        )
        self.assertEqual(
            [update.message for update in seen],
            ["lrclib·blog 소스 동시 조회 중…", "blog 교차검증 — 후보 1건"],
        )
        # 체인은 퍼센트를 지어내지 않는다: 실제 총량은 수집기만 안다.
        self.assertEqual([update.percent for update in seen], [None, None])

    def test_a_single_member_chain_does_not_announce_itself(self) -> None:
        # ``blog``/``lrclib`` used directly must not say "동시 조회"; only the
        # collector's own lines are interesting there.
        self.sources = {"lrclib": FakeSource("lrclib", songs=HIT)}
        seen: list = []
        progress.set_sink(seen.append)
        self.addCleanup(progress.set_sink, None)
        with _resolve(self.sources), mock.patch.object(
            config, "CHAIN_MEMBERS", ("lrclib",)
        ):
            ChainSource().search("생명샘")
        self.assertEqual([update.message for update in seen], ["lrclib — 후보 1건"])


class PriorityTierTests(unittest.TestCase):
    """ADR-025: checked blog consensus > lrclib > unchecked blog consensus."""

    def _search(self, *, lrclib, blog) -> list[RawSong]:
        sources = {
            "lrclib": FakeSource("lrclib", **lrclib),
            "blog": FakeSource("blog", **blog),
        }
        with _resolve(sources):
            return ChainSource().search("생명샘")

    def test_cross_checked_blog_wins_over_lrclib(self) -> None:
        blog = _blog(cross_checked=True)
        songs = self._search(lrclib={"songs": HIT}, blog={"songs": blog})
        self.assertEqual(songs, blog)

    def test_unchecked_blog_loses_to_lrclib(self) -> None:
        songs = self._search(
            lrclib={"songs": HIT}, blog={"songs": _blog(cross_checked=False)}
        )
        self.assertEqual(songs, HIT)

    def test_unchecked_blog_wins_when_lrclib_has_nothing(self) -> None:
        blog = _blog(cross_checked=False)
        songs = self._search(
            lrclib={"error": SourceNotFoundError("no")}, blog={"songs": blog}
        )
        self.assertEqual(songs, blog)

    def test_members_are_queried_concurrently(self) -> None:
        # A sequential chain would block on the first member and break the
        # barrier; the pool is what makes "always ask the blog" affordable.
        barrier = threading.Barrier(2, timeout=2.0)

        class BarrierSource(FakeSource):
            def search(self, query: str, *, limit: int = 5):
                barrier.wait()
                return list(self._songs or [])

        sources = {
            "lrclib": BarrierSource("lrclib", songs=HIT),
            "blog": BarrierSource("blog", songs=_blog(cross_checked=True)),
        }
        with _resolve(sources):
            songs = ChainSource().search("생명샘")
        self.assertEqual(len(songs), 1)


class KoreanFirstTests(unittest.TestCase):
    """ADR-026: a Korean candidate is ranked before a foreign-language one."""

    def _search(self, *, lrclib, blog) -> list[RawSong]:
        sources = {
            "lrclib": FakeSource("lrclib", **lrclib),
            "blog": FakeSource("blog", **blog),
        }
        with _resolve(sources):
            return ChainSource().search("Amazing Grace")

    def test_korean_version_wins_over_an_english_lrclib_hit(self) -> None:
        blog = _blog(KOREAN, cross_checked=True)
        songs = self._search(lrclib={"songs": EN_HIT}, blog={"songs": blog})
        self.assertEqual(songs, blog)

    def test_unchecked_korean_blog_still_beats_an_english_hit(self) -> None:
        # "한글 우선, 없으면 원어" (ADR-026): the Korean text is the point, even
        # when only one page carried it.
        blog = _blog(KOREAN, cross_checked=False)
        songs = self._search(lrclib={"songs": EN_HIT}, blog={"songs": blog})
        self.assertEqual(songs, blog)

    def test_english_only_candidates_use_the_same_tiers(self) -> None:
        blog = _blog(ENGLISH, cross_checked=True)
        songs = self._search(lrclib={"songs": EN_HIT}, blog={"songs": blog})
        self.assertEqual(songs, blog)

    def test_no_korean_candidate_falls_back_to_a_foreign_one(self) -> None:
        songs = self._search(
            lrclib={"songs": EN_HIT}, blog={"error": SourceNotFoundError("no")}
        )
        self.assertEqual(songs, EN_HIT)


class LanguageTests(unittest.TestCase):
    """``is_korean_text`` is the ADR-026 rule, and callers report it too."""

    def test_a_hangul_majority_is_korean(self) -> None:
        self.assertTrue(is_korean_text(KOREAN))
        self.assertTrue(is_korean_text("주님 사랑해요\n주님 사랑해요\nLord I love you"))

    def test_a_hangul_minority_is_not_korean(self) -> None:
        self.assertFalse(is_korean_text(ENGLISH))
        self.assertFalse(is_korean_text("Amazing grace\nHow sweet the sound\n주님"))

    def test_empty_and_blank_text_is_not_korean(self) -> None:
        self.assertFalse(is_korean_text(""))
        self.assertFalse(is_korean_text("\n   \n"))


if __name__ == "__main__":
    unittest.main()
