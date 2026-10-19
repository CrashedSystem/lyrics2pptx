"""Slide metadata + background thumbnails for the web UI (L2, read-only).

The web UI lets the user pick one of the template's slides as the shared
background before generating. This module exposes, for the whole template:

* per-slide metadata (1-based index, whether it exposes ``제목 1``, and the URL
  of its background thumbnail);
* the raw background image bytes of a slide, whatever form the background takes
  (a ``<p:bg>`` blipFill, a full-bleed ``<p:pic>``, or a full-bleed autoshape
  whose ``<p:spPr>`` carries a picture ``<a:blipFill>``).

The template is a 37 MB resource, so the parsed catalogue (metadata + image
blobs) is computed once and cached on ``(path, size, mtime)``; requests never
re-parse it. The template is opened read-only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from pptx import Presentation
from pptx.oxml.ns import qn

from .. import config
from ..errors import ConfigError
from .template_reader import find_title_shape_element

__all__ = [
    "SlideMeta",
    "SlideThumbnail",
    "list_slide_meta",
    "get_slide_thumbnail",
    "clear_thumbnail_cache",
]

log = logging.getLogger(__name__)

_IMAGE_RELTYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
)
_DEFAULT_IMAGE_TYPE = "application/octet-stream"


@dataclass(frozen=True, slots=True)
class SlideMeta:
    """One template slide as presented to the UI (1-based ``index``)."""

    index: int
    has_title_shape: bool
    thumbnail_url: str


@dataclass(frozen=True, slots=True)
class SlideThumbnail:
    """Raw background image of a template slide."""

    data: bytes
    content_type: str


@dataclass(frozen=True, slots=True)
class _SlideCatalog:
    meta: tuple[SlideMeta, ...]
    thumbnails: Mapping[int, SlideThumbnail]  # 1-based index -> image
    slide_count: int


# (str(path), size, mtime_ns) -> _SlideCatalog
_CATALOG_CACHE: dict[tuple[str, int, int], _SlideCatalog] = {}


def clear_thumbnail_cache() -> None:
    """Drop the cached slide catalogue (used by tests when the template changes)."""
    _CATALOG_CACHE.clear()


def _thumbnail_url(index: int) -> str:
    return f"/api/slides/{index}/thumbnail"


def _is_full_bleed(sp_element, slide_width: int, slide_height: int) -> bool:
    """Whether an autoshape covers the whole slide (``0,0`` at the slide size)."""
    sp_pr = sp_element.find(qn("p:spPr"))
    if sp_pr is None:
        return False
    transform = sp_pr.find(qn("a:xfrm"))
    if transform is None:
        return False
    offset = transform.find(qn("a:off"))
    extent = transform.find(qn("a:ext"))
    if offset is None or extent is None:
        return False
    try:
        return (
            int(offset.get("x")) == 0
            and int(offset.get("y")) == 0
            and int(extent.get("cx")) == slide_width
            and int(extent.get("cy")) == slide_height
        )
    except (TypeError, ValueError):
        return False


def _first_embed_reference(
    sld_element, slide_width: int, slide_height: int
) -> str | None:
    """Return the ``r:embed`` of the slide background, or ``None``.

    Handles every background form found in the template: a ``<p:bg>`` blipFill
    (shared/theme-like background), a full-bleed ``<p:pic>``, and a full-bleed
    autoshape filled with a picture (template slide 11).
    """
    background = sld_element.find(f"{qn('p:cSld')}/{qn('p:bg')}")
    if background is not None:
        for blip in background.iter(qn("a:blip")):
            reference = blip.get(qn("r:embed"))
            if reference:
                return reference

    for picture in sld_element.iter(qn("p:pic")):
        for blip in picture.iter(qn("a:blip")):
            reference = blip.get(qn("r:embed"))
            if reference:
                return reference

    for shape in sld_element.iter(qn("p:sp")):
        if not _is_full_bleed(shape, slide_width, slide_height):
            continue
        sp_pr = shape.find(qn("p:spPr"))
        blip_fill = sp_pr.find(qn("a:blipFill")) if sp_pr is not None else None
        if blip_fill is None:
            continue
        for blip in blip_fill.iter(qn("a:blip")):
            reference = blip.get(qn("r:embed"))
            if reference:
                return reference
    return None


def _thumbnail_for(slide, slide_width: int, slide_height: int) -> SlideThumbnail | None:
    reference = _first_embed_reference(slide._element, slide_width, slide_height)
    if reference is None:
        return None
    rel = slide.part.rels.get(reference)
    if rel is None or rel.reltype != _IMAGE_RELTYPE or rel.is_external:
        return None
    part = rel.target_part
    blob = part.blob
    if not blob:
        return None
    content_type = getattr(part, "content_type", None) or _DEFAULT_IMAGE_TYPE
    return SlideThumbnail(data=blob, content_type=content_type)


def _build_catalog(template_path: Path) -> _SlideCatalog:
    try:
        presentation = Presentation(str(template_path))
    except Exception as exc:  # python-pptx raises a variety of parse errors
        raise ConfigError(f"템플릿을 열 수 없습니다: {template_path}") from exc

    slides = list(presentation.slides)
    slide_width = presentation.slide_width
    slide_height = presentation.slide_height
    meta: list[SlideMeta] = []
    thumbnails: dict[int, SlideThumbnail] = {}
    for offset, slide in enumerate(slides):
        index = offset + 1
        meta.append(
            SlideMeta(
                index=index,
                has_title_shape=find_title_shape_element(slide._element) is not None,
                thumbnail_url=_thumbnail_url(index),
            )
        )
        thumbnail = _thumbnail_for(slide, slide_width, slide_height)
        if thumbnail is not None:
            thumbnails[index] = thumbnail

    log.info("slide catalogue loaded slides=%d thumbnails=%d", len(slides), len(thumbnails))
    return _SlideCatalog(
        meta=tuple(meta), thumbnails=thumbnails, slide_count=len(slides)
    )


def _catalog(template_path: Path | None = None) -> _SlideCatalog:
    path = Path(template_path) if template_path is not None else config.TEMPLATE_PATH
    if not path.is_file():
        raise ConfigError(f"템플릿을 열 수 없습니다: {path}")

    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached

    catalog = _build_catalog(path)
    _CATALOG_CACHE[key] = catalog
    return catalog


def list_slide_meta(template_path: Path | None = None) -> list[SlideMeta]:
    """Return metadata for every template slide, in 1-based slide order.

    Raises:
        ConfigError: the template file is missing or cannot be parsed.
    """
    return list(_catalog(template_path).meta)


def get_slide_thumbnail(
    index: int, template_path: Path | None = None
) -> SlideThumbnail | None:
    """Return the ``index`` (1-based) slide's background image, or ``None``.

    ``None`` means the slide has no image background (for example a solid-colour
    fill). Out-of-range indices also return ``None``; callers validate the range.
    """
    catalog = _catalog(template_path)
    if index < 1 or index > catalog.slide_count:
        raise ConfigError(
            f"슬라이드 번호가 범위를 벗어났습니다: {index} (1..{catalog.slide_count} 허용)"
        )
    return catalog.thumbnails.get(index)
