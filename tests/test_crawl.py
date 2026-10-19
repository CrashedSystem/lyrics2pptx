"""Collector registry + reference adapter tests (``crawl/``).

Two modes are covered, both offline:

* **fixture mode** — ``LYRICS2PPTX_FIXTURE_DIR`` points at ``tests/fixtures`` and
  the adapter reads saved responses (no socket is ever opened);
* **injected transport** — the adapter's only network entry point
  (``httputil.get_json``) is replaced with a deterministic fake, so URL
  construction and candidate handling can be asserted.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from lyrics2pptx import config
from lyrics2pptx.crawl import SOURCES, available_sources, get_source
from lyrics2pptx.crawl.base import RawSong, Source, SourceNotFoundError, SourceUnavailableError
from lyrics2pptx.crawl.blog import BlogLyricsSource
from lyrics2pptx.crawl.chain import ChainSource
from lyrics2pptx.crawl.html_generic import HtmlGenericSource
from lyrics2pptx.crawl.lyrics_ovh import BASE_URL, LyricsOvhSource
from lyrics2pptx.errors import ConfigError

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class RegistryTests(unittest.TestCase):
    def test_available_sources_is_sorted_and_contains_the_reference_adapter(self) -> None:
        self.assertEqual(available_sources(), ("auto", "blog", "lrclib", "lyrics_ovh"))

    def test_auto_and_blog_are_registered(self) -> None:
        blog = get_source("blog")
        self.assertIsInstance(blog, BlogLyricsSource)
        self.assertEqual(blog.name, "blog")
        chain = get_source("auto")
        self.assertIsInstance(chain, ChainSource)
        self.assertEqual(chain.name, "auto")

    def test_html_generic_is_intentionally_disabled(self) -> None:
        self.assertNotIn("html_generic", SOURCES)

    def test_get_source_returns_a_source_implementation(self) -> None:
        source = get_source("lyrics_ovh")
        self.assertIsInstance(source, LyricsOvhSource)
        self.assertEqual(source.name, "lyrics_ovh")
        self.assertIsInstance(source, Source)  # runtime_checkable protocol

    def test_unknown_source_raises_config_error_listing_known_names(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            get_source("does_not_exist")
        self.assertIn("lyrics_ovh", str(ctx.exception))

    def test_disabled_html_adapter_fails_loudly_when_used_directly(self) -> None:
        with self.assertRaises(SourceUnavailableError):
            HtmlGenericSource().search("anything")


class FixtureModeAdapterTests(unittest.TestCase):
    """``search()`` end-to-end on saved responses (docs/plan/03 §오프라인 모드)."""

    def setUp(self) -> None:
        patcher = mock.patch.object(config, "FIXTURE_DIR", FIXTURES)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source = LyricsOvhSource()

    def test_search_returns_the_saved_song(self) -> None:
        songs = self.source.search("생명샘")
        self.assertEqual(len(songs), 1)
        song = songs[0]
        self.assertIsInstance(song, RawSong)
        self.assertEqual(song.title, "생명샘")
        self.assertEqual(song.artist, "테스트가수")
        self.assertEqual(song.source_name, "lyrics_ovh")
        self.assertTrue(song.lyrics.strip())
        self.assertIn("\r\n", song.lyrics)  # raw text is returned un-normalized

    def test_query_slug_selects_its_own_fixture(self) -> None:
        song = self.source.search("abc")[0]
        self.assertEqual(song.title, "ABC Song")
        self.assertEqual(song.artist, "Alpha")

    def test_saved_response_without_lyrics_raises_not_found(self) -> None:
        with self.assertRaises(SourceNotFoundError):
            self.source.search("not found")

    def test_empty_query_raises_not_found(self) -> None:
        with self.assertRaises(SourceNotFoundError):
            self.source.search("   ")


class InjectedTransportTests(unittest.TestCase):
    """URL construction and candidate handling with a fake ``get_json``."""

    def setUp(self) -> None:
        patcher = mock.patch.object(config, "FIXTURE_DIR", None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source = LyricsOvhSource()

    def _patch_get_json(self, fake):
        return mock.patch("lyrics2pptx.crawl.lyrics_ovh.httputil.get_json", side_effect=fake)

    def test_artist_dash_title_performs_one_direct_lookup(self) -> None:
        urls: list[str] = []

        def fake(url, *, fixture=None, **kwargs):
            urls.append(url)
            return {"lyrics": "가사 한 줄", "title": "Song Title", "artist": "Artist Name"}

        with self._patch_get_json(fake):
            songs = self.source.search("Artist Name - Song Title")

        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0].startswith(f"{BASE_URL}/v1/"))
        self.assertIn("Artist%20Name", urls[0])
        self.assertIn("Song%20Title", urls[0])
        self.assertEqual(songs[0].title, "Song Title")
        self.assertEqual(songs[0].artist, "Artist Name")

    def test_bare_query_discovers_candidates_and_skips_lyricless_ones(self) -> None:
        seen: list[str] = []

        def fake(url, *, fixture=None, **kwargs):
            seen.append(url)
            if "/suggest/" in url:
                return {
                    "data": [
                        {"title": "T1", "artist": {"name": "A1"}},
                        {"title": "T3", "artist": {"name": "A3"}},
                        {"title": "T2", "artist": "A2"},
                    ]
                }
            if url.endswith("/v1/A1/T1"):
                return {"lyrics": "one\ntwo"}
            if url.endswith("/v1/A3/T3"):
                return {"error": "no lyrics"}
            if url.endswith("/v1/A2/T2"):
                return {"lyrics": "three\nfour"}
            raise AssertionError(f"unexpected url {url}")

        with self._patch_get_json(fake):
            songs = self.source.search("plain query")

        self.assertEqual([song.title for song in songs], ["T1", "T2"])
        self.assertTrue(any("/suggest/" in url for url in seen))

    def test_discovery_respects_the_limit(self) -> None:
        def fake(url, *, fixture=None, **kwargs):
            if "/suggest/" in url:
                return {
                    "data": [
                        {"title": "T1", "artist": "A1"},
                        {"title": "T2", "artist": "A2"},
                        {"title": "T3", "artist": "A3"},
                    ]
                }
            return {"lyrics": "line"}

        with self._patch_get_json(fake):
            songs = self.source.search("q", limit=1)
        self.assertEqual(len(songs), 1)

    def test_no_candidates_raises_not_found(self) -> None:
        with self._patch_get_json(lambda url, **kwargs: {"data": []}):
            with self.assertRaises(SourceNotFoundError):
                self.source.search("nothing")

    def test_suggestion_endpoint_not_found_is_treated_as_no_results(self) -> None:
        def fake(url, **kwargs):
            raise SourceNotFoundError("no suggestions")

        with self._patch_get_json(fake):
            with self.assertRaises(SourceNotFoundError):
                self.source.search("nothing")

    def test_empty_lyrics_field_raises_not_found(self) -> None:
        with self._patch_get_json(lambda url, **kwargs: {"lyrics": "   "}):
            with self.assertRaises(SourceNotFoundError):
                self.source.search("q")


if __name__ == "__main__":
    unittest.main()
