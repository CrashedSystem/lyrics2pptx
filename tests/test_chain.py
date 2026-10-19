"""Fallback-chain tests (``auto``), fully offline."""

from __future__ import annotations

import unittest
from unittest import mock

from lyrics2pptx.crawl.base import (
    RawSong,
    SourceBlockedError,
    SourceNotFoundError,
    SourceUnavailableError,
)
from lyrics2pptx.crawl.chain import ChainSource

HIT = [
    RawSong(
        title="생명샘",
        artist="마커스",
        lyrics="주는 생명샘\n마르지 않는 샘",
        source_name="lrclib",
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
        # config.CHAIN_MEMBERS is ("lrclib", "blog"); mirror it exactly.
        self.lrclib = FakeSource("lrclib")
        self.blog = FakeSource("blog")
        self.sources = {"lrclib": self.lrclib, "blog": self.blog}

    def test_first_non_empty_member_wins(self) -> None:
        self.lrclib = FakeSource("lrclib", songs=HIT)
        self.sources["lrclib"] = self.lrclib
        with _resolve(self.sources):
            songs = ChainSource().search("생명샘")
        self.assertEqual(songs, HIT)
        self.assertEqual(self.blog.calls, [])  # fallback not paid for

    def test_falls_through_to_blog_when_lrclib_has_nothing(self) -> None:
        self.lrclib = FakeSource("lrclib", error=SourceNotFoundError("no"))
        self.blog = FakeSource("blog", songs=HIT)
        self.sources.update({"lrclib": self.lrclib, "blog": self.blog})
        with _resolve(self.sources):
            songs = ChainSource().search("생명샘")
        self.assertEqual(songs, HIT)
        self.assertEqual(self.lrclib.calls, ["생명샘"])
        self.assertEqual(self.blog.calls, ["생명샘"])

    def test_missing_fixture_does_not_prevent_the_fallback(self) -> None:
        self.lrclib = FakeSource("lrclib", error=SourceUnavailableError("no fixture"))
        self.blog = FakeSource("blog", songs=HIT)
        self.sources.update({"lrclib": self.lrclib, "blog": self.blog})
        with _resolve(self.sources):
            songs = ChainSource().search("생명샘")
        self.assertEqual(songs, HIT)

    def test_not_found_wins_over_a_transient_failure(self) -> None:
        # The actionable message is "검색 결과가 없습니다 → 직접 붙여넣기", so a
        # member that definitively found nothing decides the user-facing error.
        self.lrclib = FakeSource("lrclib", error=SourceNotFoundError("no"))
        self.blog = FakeSource("blog", error=SourceUnavailableError("fixture missing"))
        self.sources.update({"lrclib": self.lrclib, "blog": self.blog})
        with _resolve(self.sources):
            with self.assertRaises(SourceNotFoundError):
                ChainSource().search("없는노래")

    def test_all_blocked_reports_blocked(self) -> None:
        self.lrclib = FakeSource("lrclib", error=SourceBlockedError("robots"))
        self.blog = FakeSource("blog", error=SourceBlockedError("robots"))
        self.sources.update({"lrclib": self.lrclib, "blog": self.blog})
        with _resolve(self.sources):
            with self.assertRaises(SourceBlockedError):
                ChainSource().search("노래")

    def test_empty_results_are_treated_as_not_found(self) -> None:
        with _resolve(self.sources):
            with self.assertRaises(SourceNotFoundError):
                ChainSource().search("노래")

    def test_source_name_is_auto(self) -> None:
        self.assertEqual(ChainSource().name, "auto")
