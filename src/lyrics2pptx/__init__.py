"""lyrics2pptx — 검색어 기반 가사 수집 → template.pptx 2줄 채우기 (로컬 단일 사용자 도구).

Package entry points:

* ``python -m lyrics2pptx.cli`` — command line interface;
* ``python -m lyrics2pptx.webapp`` — local web UI on ``127.0.0.1:8765``.

The pipeline functions (``run``/``search_hits``/``describe_error``) are
re-exported lazily so that ``import lyrics2pptx`` stays cheap and free of
import-order cycles.
"""

from __future__ import annotations

from .errors import (
    ConfigError,
    EmptyLyricsError,
    Lyrics2PptxError,
    OutputWriteError,
    TemplateStructureError,
)

__version__ = "0.1.0b3"

__all__ = [
    "__version__",
    "ConfigError",
    "EmptyLyricsError",
    "Lyrics2PptxError",
    "OutputWriteError",
    "TemplateStructureError",
    "run",
    "search_hits",
    "describe_error",
]

_LAZY = ("run", "search_hits", "describe_error")


def __getattr__(name: str):
    """Resolve pipeline helpers on first attribute access (PEP 562)."""
    if name in _LAZY:
        from . import pipeline

        return getattr(pipeline, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
