"""text — normalization and two-line splitting axis.

This axis knows nothing about pptx or crawling (rules P1/P2 in docs/arch/02
§4). It turns raw lyric text into ``list[str]`` and then into line pairs.
"""

from __future__ import annotations

from .normalize import normalize_lines
from .split import LinePair, split_pairs

__all__ = ["normalize_lines", "split_pairs", "LinePair"]
