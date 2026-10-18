"""Domain exception tree root (L0 leaf).

Every domain error in this project derives from ``Lyrics2PptxError`` so that
entrypoints can distinguish "expected, user-facing" failures from unexpected
ones (docs/arch/03 §3, AD-03).

This module touches nothing but the standard library: no network, no file I/O,
no XML. Collector-specific errors (``SourceError`` hierarchy) live in
``crawl/base.py`` and derive from this root.
"""

from __future__ import annotations

__all__ = [
    "Lyrics2PptxError",
    "EmptyLyricsError",
    "TemplateStructureError",
    "OutputWriteError",
    "ConfigError",
]


class Lyrics2PptxError(Exception):
    """Root of every domain exception raised by this project."""


class EmptyLyricsError(Lyrics2PptxError):
    """No usable lyric line remained after normalization/splitting."""


class TemplateStructureError(Lyrics2PptxError):
    """Template structure differs from what the generator expects.

    Raised when the ``제목 1`` shape is missing, when a copied slide contains an
    unmapped relationship reference, or when no slide part can be registered.
    """


class OutputWriteError(Lyrics2PptxError):
    """Creating the output directory, writing the temp file, or the atomic
    replace of the final ``.pptx`` failed."""


class ConfigError(Lyrics2PptxError):
    """A configuration value, environment variable, or path argument is invalid."""
