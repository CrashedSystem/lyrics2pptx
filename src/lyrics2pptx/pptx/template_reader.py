"""Template analysis: background pool + per-slide ``제목 1`` style cache (L2).

Loads ``template.pptx`` **once** per process (cached on path/size/mtime) and
records, for every slide, the formatting snapshot of the ``제목 1`` shape
(docs/arch/03 §2, docs/arch/04 §1/§5).

The template is read-only: this module never opens it for writing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from lxml import etree
from pptx import Presentation
from pptx.oxml.ns import qn
from pptx.shapes.base import BaseShape

from .. import config
from ..errors import ConfigError, TemplateStructureError

__all__ = [
    "StyleSpec",
    "TemplateProfile",
    "BuildResult",
    "Plan",
    "get_template_profile",
    "clear_profile_cache",
    "find_title_shape",
    "find_title_shape_element",
    "extract_slide_style",
]

log = logging.getLogger(__name__)

# Shared writer plan type: (background template index, line 1, line 2 | None).
# Declared here (the lowest pptx module) so builder and both writers can import
# it without introducing a cycle.
Plan = list[tuple[int, str, str | None]]


@dataclass(frozen=True, slots=True)
class StyleSpec:
    """Formatting snapshot of one template slide's ``제목 1`` shape.

    XML is stored as serialized ``bytes`` so multiple slides never share a live
    lxml element (deep-copying a shared element would have side effects).
    """

    slide_index: int  # 0-based template index
    rpr_xml: bytes  # serialized <a:rPr> (empty when absent)
    has_rpr: bool
    bodypr_xml: bytes  # serialized <a:bodyPr> (empty when absent)
    sz: int | None  # first run's size in 1/100 pt, e.g. 7200
    shape_found: bool
    paragraph_count: int


@dataclass(frozen=True, slots=True)
class TemplateProfile:
    """Everything the builder needs about the template, computed once."""

    template_path: Path
    template_size: int
    template_mtime_ns: int
    slide_count: int
    pool: tuple[int, ...]  # background pool in template order (e.g. (0..19))
    styles: Mapping[int, StyleSpec]
    default_style: StyleSpec
    slide_size: tuple[int, int]  # EMU (width, height)


@dataclass(frozen=True, slots=True)
class BuildResult:
    """Outcome of one build (docs/arch/03 §2).

    Declared here rather than in ``builder.py`` so the writer modules
    (``writer_parts``/``writer_zip``) and the builder can share it without an
    import cycle, which rule P8 forbids. ``builder`` re-exports it, so
    ``lyrics2pptx.pptx.builder.BuildResult`` remains valid.
    """

    output_path: Path
    slide_count: int  # == len(pairs)
    pool_size: int  # == len(pool)
    wrap_count: int  # how many times the pool wrapped around
    used_bg_indices: tuple[int, ...]  # background index actually used, in order
    dropped_notes_count: int  # notes slides not cloned (== slide_count by default)


# (str(path), size, mtime_ns, skip_slide) -> TemplateProfile
_PROFILE_CACHE: dict[tuple[str, int, int, tuple[int, ...]], TemplateProfile] = {}


def clear_profile_cache() -> None:
    """Drop the cached template profile (used by tests when the env changes)."""
    _PROFILE_CACHE.clear()


def _serialize(element: etree._Element | None) -> bytes:
    if element is None:
        return b""
    return etree.tostring(element, encoding="utf-8", xml_declaration=False)


def find_title_shape_element(
    sld_element: etree._Element,
    *,
    name: str | None = None,
) -> etree._Element | None:
    """Return the ``<p:sp>`` element whose ``<p:cNvPr name="...">`` matches.

    Matching is by shape *name* (not placeholder): the template's title shape is
    an ordinary shape, verified by measurement (docs/plan/05 §1). Comparison
    strips trailing whitespace.
    """
    target = (name or config.TITLE_SHAPE_NAME).strip()
    for shape_el in sld_element.iter(qn("p:sp")):
        c_nv_pr = shape_el.find(f"{qn('p:nvSpPr')}/{qn('p:cNvPr')}")
        if c_nv_pr is None:
            continue
        if (c_nv_pr.get("name") or "").strip() == target:
            return shape_el
    return None


def find_title_shape(slide, *, name: str | None = None) -> BaseShape:
    """Return the ``제목 1`` shape proxy of ``slide``.

    Iterates ``slide.shapes`` (never ``slide.placeholders``) because the title
    shape is not a ``<p:ph>`` placeholder.

    Raises:
        TemplateStructureError: the shape is missing; the message lists the
            shape names that do exist.
    """
    target = (name or config.TITLE_SHAPE_NAME).strip()
    for shape in slide.shapes:
        if (shape.name or "").strip() == target:
            return shape
    available = ", ".join(repr(getattr(shape, "name", "?")) for shape in slide.shapes)
    raise TemplateStructureError(
        f"도형 {target!r} 을(를) 찾지 못했습니다. 존재하는 도형: {available}"
    )


def extract_slide_style(slide, slide_index: int) -> StyleSpec:
    """Snapshot the ``제목 1`` style of a single slide."""
    shape_element = find_title_shape_element(slide._element)
    if shape_element is None:
        return StyleSpec(
            slide_index=slide_index,
            rpr_xml=b"",
            has_rpr=False,
            bodypr_xml=b"",
            sz=None,
            shape_found=False,
            paragraph_count=0,
        )

    tx_body = shape_element.find(qn("p:txBody"))
    if tx_body is None:  # pragma: no cover - template always has a txBody
        return StyleSpec(
            slide_index=slide_index,
            rpr_xml=b"",
            has_rpr=False,
            bodypr_xml=b"",
            sz=None,
            shape_found=True,
            paragraph_count=0,
        )

    body_pr = tx_body.find(qn("a:bodyPr"))
    paragraphs = tx_body.findall(qn("a:p"))

    rpr_element: etree._Element | None = None
    for paragraph in paragraphs:
        run = paragraph.find(qn("a:r"))
        if run is None:
            continue
        candidate = run.find(qn("a:rPr"))
        if candidate is not None:
            rpr_element = candidate
            break

    size: int | None = None
    if rpr_element is not None:
        raw_size = rpr_element.get("sz")
        if raw_size is not None:
            try:
                size = int(raw_size)
            except ValueError:
                size = None

    return StyleSpec(
        slide_index=slide_index,
        rpr_xml=_serialize(rpr_element),
        has_rpr=rpr_element is not None,
        bodypr_xml=_serialize(body_pr),
        sz=size,
        shape_found=True,
        paragraph_count=len(paragraphs),
    )


def _analyze(
    template_path: Path,
    *,
    skip_slide: tuple[int, ...],
    file_size: int,
    mtime_ns: int,
) -> TemplateProfile:
    try:
        presentation = Presentation(str(template_path))
    except Exception as exc:  # python-pptx raises a variety of parse errors
        raise ConfigError(f"템플릿을 열 수 없습니다: {template_path}") from exc

    slide_count = len(presentation.slides)
    if slide_count == 0:
        raise TemplateStructureError(f"템플릿에 슬라이드가 없습니다: {template_path}")

    for index in skip_slide:
        if not 1 <= index <= slide_count:
            raise ConfigError(
                f"--skip-slide 값이 범위를 벗어났습니다: {index} (1..{slide_count} 허용)"
            )

    styles: dict[int, StyleSpec] = {
        index: extract_slide_style(presentation.slides[index], index)
        for index in range(slide_count)
    }

    default_style: StyleSpec | None = next(
        (styles[index] for index in range(slide_count) if styles[index].has_rpr),
        None,
    )
    if default_style is None:
        raise TemplateStructureError(
            f"템플릿의 어떤 슬라이드에도 {config.TITLE_SHAPE_NAME!r} 서식(rPr)이 없습니다"
        )

    pool = tuple(index for index in range(slide_count) if (index + 1) not in skip_slide)

    return TemplateProfile(
        template_path=template_path,
        template_size=file_size,
        template_mtime_ns=mtime_ns,
        slide_count=slide_count,
        pool=pool,
        styles=styles,
        default_style=default_style,
        slide_size=(int(presentation.slide_width), int(presentation.slide_height)),
    )


def get_template_profile(
    template_path: Path | None = None,
    *,
    skip_slide: tuple[int, ...] = (),
) -> TemplateProfile:
    """Return the (cached) template profile.

    Raises:
        ConfigError: the template file is missing or cannot be parsed.
    """
    path = Path(template_path) if template_path is not None else config.TEMPLATE_PATH
    if not path.is_file():
        raise ConfigError(f"템플릿을 열 수 없습니다: {path}")

    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns, tuple(sorted(skip_slide)))
    cached = _PROFILE_CACHE.get(key)
    if cached is not None:
        log.debug(
            "profile cache hit key=(size=%d,mtime=%d)", stat.st_size, stat.st_mtime_ns
        )
        return cached

    profile = _analyze(
        path,
        skip_slide=key[3],
        file_size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )
    _PROFILE_CACHE[key] = profile
    log.info(
        "template profile loaded slides=%d pool=%d cached=%s",
        profile.slide_count,
        len(profile.pool),
        False,
    )
    return profile
