"""Unit tests for the exception tree (``errors.py``, ``crawl/base.py``).

docs/arch/03 §3 fixes a single root (``Lyrics2PptxError``) with one collector
sub-tree (``SourceError``). Entrypoint logic depends on that shape, so it is
pinned here.
"""

from __future__ import annotations

import dataclasses
import unittest

from lyrics2pptx import errors
from lyrics2pptx.crawl.base import (
    RawSong,
    SourceBlockedError,
    SourceError,
    SourceNotFoundError,
    SourceUnavailableError,
)
from lyrics2pptx.errors import (
    ConfigError,
    EmptyLyricsError,
    Lyrics2PptxError,
    OutputWriteError,
    TemplateStructureError,
)


class HierarchyTests(unittest.TestCase):
    def test_every_domain_error_derives_from_the_single_root(self) -> None:
        for error_type in (
            EmptyLyricsError,
            TemplateStructureError,
            OutputWriteError,
            ConfigError,
            SourceError,
            SourceNotFoundError,
            SourceBlockedError,
            SourceUnavailableError,
        ):
            with self.subTest(error=error_type.__name__):
                self.assertTrue(issubclass(error_type, Lyrics2PptxError))

    def test_collector_errors_share_one_upper_bound(self) -> None:
        for error_type in (SourceNotFoundError, SourceBlockedError, SourceUnavailableError):
            with self.subTest(error=error_type.__name__):
                self.assertTrue(issubclass(error_type, SourceError))
                # They sit below SourceError, not directly below the root.
                self.assertNotIn(Lyrics2PptxError, error_type.__bases__)

    def test_error_types_are_distinct_so_isinstance_dispatch_is_unambiguous(self) -> None:
        types = [
            EmptyLyricsError,
            TemplateStructureError,
            OutputWriteError,
            ConfigError,
            SourceNotFoundError,
            SourceBlockedError,
            SourceUnavailableError,
        ]
        for left in types:
            for right in types:
                if left is right:
                    continue
                with self.subTest(left=left.__name__, right=right.__name__):
                    self.assertFalse(issubclass(left, right))

    def test_messages_survive_the_exception_round_trip(self) -> None:
        for error_type in (EmptyLyricsError, ConfigError, SourceBlockedError):
            with self.subTest(error=error_type.__name__):
                exc = error_type("한국어 메시지")
                self.assertEqual(str(exc), "한국어 메시지")

    def test_module_exports_every_documented_error_name(self) -> None:
        expected = {
            "Lyrics2PptxError",
            "EmptyLyricsError",
            "TemplateStructureError",
            "OutputWriteError",
            "ConfigError",
        }
        self.assertTrue(expected.issubset(set(errors.__all__)))


class RawSongContractTests(unittest.TestCase):
    def test_raw_song_is_frozen(self) -> None:
        song = RawSong(title="t", artist=None, lyrics="l", source_name="s")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            song.title = "other"  # type: ignore[misc]

    def test_raw_song_positional_field_order_matches_the_contract(self) -> None:
        song = RawSong("제목", "가수", "가사", "lyrics_ovh", "https://example.com")
        self.assertEqual(
            (song.title, song.artist, song.lyrics, song.source_name, song.source_url),
            ("제목", "가수", "가사", "lyrics_ovh", "https://example.com"),
        )

    def test_source_url_defaults_to_none(self) -> None:
        self.assertIsNone(RawSong("t", None, "l", "s").source_url)


if __name__ == "__main__":
    unittest.main()
