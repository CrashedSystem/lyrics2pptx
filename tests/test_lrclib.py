"""Tests for the ``lrclib`` collector adapter (``crawl/lrclib.py``).

Three layers, all offline:

* **fixture mode** — ``LYRICS2PPTX_FIXTURE_DIR`` points at ``tests/fixtures`` and
  a saved JSON *array* drives the whole adapter (no socket is opened);
* **injected transport** — ``httputil.get_json_list`` is replaced with a
  deterministic fake so URL construction and candidate handling are asserted;
* **pure helpers** — query parsing and the token fallback.

The ``Source`` contract itself (``SourceNotFoundError`` on an empty query, etc.)
is exercised here for lrclib; the registry is covered in ``test_crawl``.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from lyrics2pptx import config
from lyrics2pptx.crawl import get_source
from lyrics2pptx.crawl.base import RawSong, Source, SourceNotFoundError
from lyrics2pptx.crawl.lrclib import (
    GET_URL,
    SAMPLE_FIXTURE,
    SEARCH_URL,
    LrclibSource,
    _fallback_queries,
    _parse_query,
    _raw_from_item,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class QueryHelperTests(unittest.TestCase):
    def test_parse_query_splits_artist_dash_title(self) -> None:
        self.assertEqual(_parse_query("어노인팅 - 손잡고 함께 가세"), ("어노인팅", "손잡고 함께 가세"))

    def test_parse_query_without_separator_is_a_bare_search(self) -> None:
        self.assertEqual(_parse_query("생명샘"), ("", "생명샘"))

    def test_fallback_reorders_longest_first_for_the_research_example(self) -> None:
        queries = _fallback_queries("우리 함께 손잡고 가세")
        self.assertEqual(queries[0], "손잡고 함께 가세")

    def test_fallback_is_empty_for_a_single_token(self) -> None:
        self.assertEqual(_fallback_queries("생명샘"), [])


class RawFromItemTests(unittest.TestCase):
    def _item(self, **overrides):
        base = {
            "id": 7,
            "trackName": "제목",
            "artistName": "가수",
            "plainLyrics": "한 줄\n두 줄",
        }
        base.update(overrides)
        return base

    def test_maps_fields_and_builds_the_get_url(self) -> None:
        song = _raw_from_item(self._item())
        self.assertIsInstance(song, RawSong)
        self.assertEqual(song.title, "제목")
        self.assertEqual(song.artist, "가수")
        self.assertEqual(song.source_name, "lrclib")
        self.assertEqual(song.source_url, f"{GET_URL}/7")

    def test_instrumental_without_plain_lyrics_is_dropped(self) -> None:
        self.assertIsNone(_raw_from_item(self._item(plainLyrics="")))
        self.assertIsNone(_raw_from_item(self._item(plainLyrics=None)))

    def test_missing_track_name_is_dropped(self) -> None:
        self.assertIsNone(_raw_from_item(self._item(trackName="  ")))

    def test_missing_artist_becomes_none(self) -> None:
        song = _raw_from_item(self._item(artistName=""))
        assert song is not None
        self.assertIsNone(song.artist)

    def test_non_mapping_item_is_dropped(self) -> None:
        self.assertIsNone(_raw_from_item("not a dict"))
        self.assertIsNone(_raw_from_item(None))


class FixtureModeTests(unittest.TestCase):
    """One saved response drives ``search`` (docs/plan/03 §오프라인 모드)."""

    def setUp(self) -> None:
        patcher = mock.patch.object(config, "FIXTURE_DIR", FIXTURES)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source = LrclibSource()

    def test_search_reads_the_saved_array(self) -> None:
        songs = self.source.search("생명샘")
        self.assertEqual(len(songs), 1)
        song = songs[0]
        self.assertEqual(song.title, "생명샘")
        self.assertEqual(song.artist, "테스트가수")
        self.assertEqual(song.source_name, "lrclib")
        self.assertIn("\r\n", song.lyrics)  # raw text is returned un-normalized

    def test_empty_saved_array_raises_not_found(self) -> None:
        with self.assertRaises(SourceNotFoundError):
            self.source.search("not found")

    def test_sample_fixture_name_is_the_default(self) -> None:
        self.assertEqual(SAMPLE_FIXTURE, "lrclib_sample.json")

    def test_empty_query_raises_not_found(self) -> None:
        with self.assertRaises(SourceNotFoundError):
            self.source.search("   ")

    def test_fixture_mode_never_touches_the_transport(self) -> None:
        with mock.patch(
            "lyrics2pptx.crawl.lrclib.httputil._open_and_read",
            side_effect=AssertionError("network access"),
        ):
            songs = self.source.search("생명샘")
        self.assertEqual(len(songs), 1)


class InjectedTransportTests(unittest.TestCase):
    """URL construction and candidate handling with a fake ``get_json_list``."""

    def setUp(self) -> None:
        patcher = mock.patch.object(config, "FIXTURE_DIR", None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source = LrclibSource()

    def _patch(self, fake):
        return mock.patch(
            "lyrics2pptx.crawl.lrclib.httputil.get_json_list", side_effect=fake
        )

    def test_search_uses_the_free_query_first(self) -> None:
        urls: list[str] = []

        def fake(url, *, fixture=None, **kwargs):
            urls.append(url)
            return [{"id": 1, "trackName": "T", "artistName": "A", "plainLyrics": "line"}]

        with self._patch(fake):
            songs = self.source.search("생명샘")

        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0].startswith(f"{SEARCH_URL}?q="))
        self.assertIn("%EC%83%9D%EB%AA%85%EC%83%98", urls[0])  # percent-encoded 생명샘
        self.assertEqual(songs[0].title, "T")

    def test_artist_dash_title_falls_back_to_the_field_filter(self) -> None:
        urls: list[str] = []

        def fake(url, *, fixture=None, **kwargs):
            urls.append(url)
            if "track_name=" in url:
                return [{"id": 2, "trackName": "Song Title", "artistName": "Artist", "plainLyrics": "x"}]
            return []

        with self._patch(fake):
            songs = self.source.search("Artist - Song Title")

        self.assertEqual(len(urls), 2)
        self.assertTrue(urls[0].startswith(f"{SEARCH_URL}?q="))
        self.assertIn("track_name=Song%20Title", urls[1])
        self.assertIn("artist_name=Artist", urls[1])
        self.assertEqual(songs[0].artist, "Artist")

    def test_bare_query_falls_back_to_reordered_tokens(self) -> None:
        urls: list[str] = []

        def fake(url, *, fixture=None, **kwargs):
            urls.append(url)
            if "q=%EC%86%90%EC%9E%A1%EA%B3%A0" in url:  # q=손잡고...
                return [{"id": 3, "trackName": "손잡고 함께 가세", "artistName": "어노인팅", "plainLyrics": "y"}]
            return []

        with self._patch(fake):
            songs = self.source.search("우리 함께 손잡고 가세")

        self.assertGreaterEqual(len(urls), 2)
        self.assertEqual(songs[0].title, "손잡고 함께 가세")

    def test_instrumental_entries_are_skipped(self) -> None:
        def fake(url, *, fixture=None, **kwargs):
            return [
                {"id": 1, "trackName": "Inst", "artistName": "A", "plainLyrics": ""},
                {"id": 2, "trackName": "Song", "artistName": "A", "plainLyrics": "line"},
            ]

        with self._patch(fake):
            songs = self.source.search("q")
        self.assertEqual([song.title for song in songs], ["Song"])

    def test_limit_caps_the_result(self) -> None:
        def fake(url, *, fixture=None, **kwargs):
            return [
                {"id": index, "trackName": f"T{index}", "artistName": "A", "plainLyrics": "l"}
                for index in range(5)
            ]

        with self._patch(fake):
            songs = self.source.search("q", limit=2)
        self.assertEqual(len(songs), 2)

    def test_no_results_raises_not_found(self) -> None:
        with self._patch(lambda url, **kwargs: []):
            with self.assertRaises(SourceNotFoundError):
                self.source.search("nothing at all here")

    def test_non_dict_items_are_skipped(self) -> None:
        def fake(url, *, fixture=None, **kwargs):
            return [42, {"id": 1, "trackName": "T", "artistName": "A", "plainLyrics": "l"}]

        with self._patch(fake):
            songs = self.source.search("q")
        self.assertEqual(len(songs), 1)


class RegistryTests(unittest.TestCase):
    def test_lrclib_is_registered_and_satisfies_the_protocol(self) -> None:
        source = get_source("lrclib")
        self.assertIsInstance(source, LrclibSource)
        self.assertEqual(source.name, "lrclib")
        self.assertIsInstance(source, Source)

    def test_auto_chain_is_the_default_source(self) -> None:
        # ADR-018: lrclib stays first in the chain, but ``blog`` may answer when
        # lrclib has no entry, so the default is the chain, not lrclib alone.
        self.assertEqual(config.DEFAULT_SOURCE, "auto")
        self.assertEqual(config.CHAIN_MEMBERS[0], "lrclib")


if __name__ == "__main__":
    unittest.main()
