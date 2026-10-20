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
from pptx.oxml import parse_xml
from pptx.oxml.ns import nsdecls, qn
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
    "resolve_title_shape_element",
    "is_full_bleed_shape",
    "extract_slide_style",
]

log = logging.getLogger(__name__)

# Shared writer plan type: (line 1, line 2 | None). The background is a single
# template slide (``TemplateProfile.selected_slide``) shared by every output
# slide, so it is not part of the plan. Declared here (the lowest pptx module)
# so builder and both writers can import it without introducing a cycle.
Plan = list[tuple[str, str | None]]


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
    shape_name: str = ""  # resolved shape name ("" when no shape was found)
    synthesized: bool = False  # True when this project invented the rPr


@dataclass(frozen=True, slots=True)
class TemplateProfile:
    """Everything the builder needs about the template, computed once."""

    template_path: Path
    template_size: int
    template_mtime_ns: int
    slide_count: int
    selected_slide: int  # 0-based template index cloned by every output slide
    pool: tuple[int, ...]  # eligible slide indices (all minus skip_slide)
    styles: Mapping[int, StyleSpec]
    default_style: StyleSpec
    slide_size: tuple[int, int]  # EMU (width, height)
    selected_shape_name: str = ""  # shape on ``selected_slide`` that holds lyrics
    shape_detected: bool = False  # True when no name matched and we guessed


@dataclass(frozen=True, slots=True)
class BuildResult:
    """Outcome of one build (docs/arch/03 §2).

    Declared here rather than in ``builder.py`` so the writer modules
    (``writer_parts``/``writer_zip``) and the builder can share it without an
    import cycle, which rule P8 forbids. ``builder`` re-exports it, so
    ``lyrics2pptx.pptx.builder.BuildResult`` remains valid.

    ``slide_index`` is the 1-based template slide number every output slide was
    cloned from (single background mode).
    """

    output_path: Path
    slide_count: int  # == len(plan)
    slide_index: int  # 1-based template slide used as the shared background
    dropped_notes_count: int  # notes slides not cloned (== slide_count by default)


# (str(path), size, mtime_ns, skip_slide) -> TemplateProfile
_PROFILE_CACHE: dict[
    tuple[str, int, int, tuple[int, ...], int, bool], TemplateProfile
] = {}


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


def is_full_bleed_shape(
    sp_element: etree._Element, slide_width: int, slide_height: int
) -> bool:
    """True when an autoshape covers the whole slide (a background fill)."""
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


def _shape_name_of(sp_element: etree._Element) -> str:
    c_nv_pr = sp_element.find(f"{qn('p:nvSpPr')}/{qn('p:cNvPr')}")
    if c_nv_pr is None:
        return ""
    return (c_nv_pr.get("name") or "").strip()


def _shape_text_of(sp_element: etree._Element) -> str:
    return "".join((node.text or "") for node in sp_element.iter(qn("a:t"))).strip()


def _shape_extent(sp_element: etree._Element) -> tuple[int, int]:
    sp_pr = sp_element.find(qn("p:spPr"))
    if sp_pr is None:
        return (0, 0)
    extent = sp_pr.find(f"{qn('a:xfrm')}/{qn('a:ext')}")
    if extent is None:
        return (0, 0)
    try:
        return (int(extent.get("cx") or 0), int(extent.get("cy") or 0))
    except (TypeError, ValueError):
        return (0, 0)


def _shape_offset(sp_element: etree._Element) -> tuple[int, int]:
    sp_pr = sp_element.find(qn("p:spPr"))
    if sp_pr is None:
        return (0, 0)
    offset = sp_pr.find(f"{qn('a:xfrm')}/{qn('a:off')}")
    if offset is None:
        return (0, 0)
    try:
        return (int(offset.get("y") or 0), int(offset.get("x") or 0))
    except (TypeError, ValueError):
        return (0, 0)


def resolve_title_shape_element(
    sld_element: etree._Element,
    *,
    slide_width: int,
    slide_height: int,
    name: str | None = None,
) -> tuple[etree._Element | None, str, bool]:
    """Find the shape that should receive the lyrics on this slide (ADR-034).

    Order: explicit name (``LYRICS2PPTX_TITLE_SHAPE`` / argument) → the shape
    literally named ``제목 1`` → a deterministic guess, so a template that was
    made elsewhere still works without editing the file.

    The guess only considers text-bearing autoshapes that are not full-bleed
    backgrounds (a picture-filled or solid rectangle covering the slide) and
    not picture-filled themselves. Among those it prefers shapes that already
    contain text, then the largest area; ties break on (top, left, name) so the
    same template always resolves to the same shape.

    Returns:
        ``(element | None, resolved name, detected)``. ``detected`` is True only
        for the heuristic path (no name matched).
    """
    target = (name or config.TITLE_SHAPE_NAME).strip()
    direct = find_title_shape_element(sld_element, name=target)
    if direct is not None:
        return direct, target, False

    candidates: list[etree._Element] = []
    for sp_element in sld_element.iter(qn("p:sp")):
        if sp_element.find(qn("p:txBody")) is None:
            continue
        if is_full_bleed_shape(sp_element, slide_width, slide_height):
            continue
        sp_pr = sp_element.find(qn("p:spPr"))
        if sp_pr is not None and sp_pr.find(qn("a:blipFill")) is not None:
            continue
        candidates.append(sp_element)

    if not candidates:
        return None, target, False

    def rank(sp_element: etree._Element) -> tuple[int, int, int, int, str]:
        has_text = 0 if _shape_text_of(sp_element) else 1
        width, height = _shape_extent(sp_element)
        top, left = _shape_offset(sp_element)
        return (has_text, -(width * height), top, left, _shape_name_of(sp_element))

    best = min(candidates, key=rank)
    resolved = _shape_name_of(best)
    if len(candidates) > 1:
        names = [_shape_name_of(item) for item in candidates]
        log.warning(
            "제목 도형을 이름으로 찾지 못해 추정했습니다: %r (후보 %d개: %s). "
            "다른 도형을 쓰려면 LYRICS2PPTX_TITLE_SHAPE 로 이름을 지정하세요.",
            resolved,
            len(names),
            ", ".join(repr(item) for item in names),
        )
    return best, resolved, True


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


def _style_from_element(
    shape_element: etree._Element,
    slide_index: int,
    *,
    shape_name: str,
) -> StyleSpec:
    """Snapshot one shape's first-run ``rPr``, ``bodyPr`` and size."""
    tx_body = shape_element.find(qn("p:txBody"))
    if tx_body is None:  # pragma: no cover - a title shape always has one
        return StyleSpec(
            slide_index=slide_index,
            rpr_xml=b"",
            has_rpr=False,
            bodypr_xml=b"",
            sz=None,
            shape_found=True,
            paragraph_count=0,
            shape_name=shape_name,
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
        shape_name=shape_name,
    )


# Synthesized style bounds (ADR-034): two lyric lines must fit the box, and
# PowerPoint's normAutofit absorbs whatever this estimate overshoots.
_SYNTH_MIN_PT = 18
_SYNTH_MAX_PT = 66
_SYNTH_FONT = "맑은 고딕"


def _synthesize_style(
    shape_element: etree._Element,
    slide_index: int,
    *,
    slide_height: int,
) -> StyleSpec:
    """Invent a usable run style for a template that carries none.

    A background-only template (an empty text box, or a shape whose text was
    deleted) has no ``a:rPr`` to inherit. Rather than reject the file, derive a
    size from the box height so two lines fit, pick the Korean UI font and
    black text, and let ``normAutofit`` shrink it if the estimate is generous.
    """
    tx_body = shape_element.find(qn("p:txBody"))
    body_pr = tx_body.find(qn("a:bodyPr")) if tx_body is not None else None
    paragraphs = tx_body.findall(qn("a:p")) if tx_body is not None else []

    _width, height = _shape_extent(shape_element)
    height_emu = height or slide_height
    size_pt = int(round(height_emu / 12700 * 0.42))  # EMU -> pt, ~2 lines + leading
    size_pt = max(_SYNTH_MIN_PT, min(_SYNTH_MAX_PT, size_pt))
    # ``sz`` and ``StyleSpec.sz`` are hundredths of a point (72 pt -> 7200).
    size = size_pt * 100

    rpr = parse_xml(
        f'<a:rPr {nsdecls("a")} lang="ko-KR" sz="{size}">'
        '<a:solidFill><a:srgbClr val="000000"/></a:solidFill>'
        f'<a:latin typeface="{_SYNTH_FONT}"/><a:ea typeface="{_SYNTH_FONT}"/>'
        "</a:rPr>"
    )
    log.warning(
        "템플릿 슬라이드 %d 의 가사 도형에 서식(rPr)이 없어 합성했습니다: %dpt %s",
        slide_index + 1,
        size_pt,
        _SYNTH_FONT,
    )
    return StyleSpec(
        slide_index=slide_index,
        rpr_xml=_serialize(rpr),
        has_rpr=True,
        bodypr_xml=_serialize(body_pr),
        sz=size,
        shape_found=True,
        paragraph_count=len(paragraphs),
        shape_name=_shape_name_of(shape_element),
        synthesized=True,
    )


def extract_slide_style(
    slide,
    slide_index: int,
    *,
    name: str | None = None,
    slide_width: int | None = None,
    slide_height: int | None = None,
) -> StyleSpec:
    """Snapshot the lyric shape's style on one slide.

    Without ``slide_width``/``slide_height`` this keeps the historical strict
    name lookup; ``_analyze`` passes the slide size so a template whose shape is
    named differently is detected instead of rejected (ADR-034).
    """
    if slide_width is None or slide_height is None:
        shape_element = find_title_shape_element(slide._element, name=name)
        shape_name = (name or config.TITLE_SHAPE_NAME) if shape_element is not None else ""
    else:
        shape_element, shape_name, _detected = resolve_title_shape_element(
            slide._element,
            slide_width=slide_width,
            slide_height=slide_height,
            name=name,
        )

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
    return _style_from_element(shape_element, slide_index, shape_name=shape_name)


def _analyze(
    template_path: Path,
    *,
    skip_slide: tuple[int, ...],
    slide_index: int,
    file_size: int,
    mtime_ns: int,
    allow_fallback: bool = False,
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

    # ``slide_index`` is 1-based (template slide number). A user-specified value
    # out of range is a hard error; the *default* (19, tuned for the bundled
    # 20-slide template) falls back to the last slide so a shorter template still
    # works after a plain file swap (ADR-034).
    if not 1 <= slide_index <= slide_count:
        if not allow_fallback:
            raise ConfigError(
                f"슬라이드 번호가 범위를 벗어났습니다: {slide_index} (1..{slide_count} 허용)"
            )
        log.warning(
            "기본 배경 번호 %d 가 이 템플릿의 범위(1..%d)를 벗어나 마지막 슬라이드를 씁니다",
            slide_index,
            slide_count,
        )
        slide_index = slide_count

    slide_width = int(presentation.slide_width)
    slide_height = int(presentation.slide_height)

    styles: dict[int, StyleSpec] = {
        index: extract_slide_style(
            presentation.slides[index],
            index,
            slide_width=slide_width,
            slide_height=slide_height,
        )
        for index in range(slide_count)
    }

    selected_index = slide_index - 1
    selected_style = styles[selected_index]
    if not selected_style.shape_found:
        available = ", ".join(
            repr(getattr(shape, "name", "?"))
            for shape in presentation.slides[selected_index].shapes
        ) or "없음"
        raise TemplateStructureError(
            f"배경 슬라이드 {slide_index} 에 가사를 넣을 텍스트 도형이 없습니다. "
            f"존재하는 도형: {available}. 텍스트 상자를 하나 두거나 "
            f"LYRICS2PPTX_TITLE_SHAPE 로 이름을 지정하세요."
        )

    # A template without any run style (a background plus an empty box) would
    # otherwise be rejected; synthesize one for the selected slide instead.
    if not selected_style.has_rpr:
        selected_element, _name, _detected = resolve_title_shape_element(
            presentation.slides[selected_index]._element,
            slide_width=slide_width,
            slide_height=slide_height,
        )
        if selected_element is not None:
            styles[selected_index] = _synthesize_style(
                selected_element, selected_index, slide_height=slide_height
            )
            selected_style = styles[selected_index]

    default_style: StyleSpec | None = next(
        (styles[index] for index in range(slide_count) if styles[index].has_rpr),
        None,
    )
    if default_style is None:  # pragma: no cover - synthesis above guarantees one
        raise TemplateStructureError(
            f"템플릿에서 가사 서식을 정할 수 없습니다: {template_path}"
        )

    pool = tuple(index for index in range(slide_count) if (index + 1) not in skip_slide)

    return TemplateProfile(
        template_path=template_path,
        template_size=file_size,
        template_mtime_ns=mtime_ns,
        slide_count=slide_count,
        selected_slide=selected_index,
        pool=pool,
        styles=styles,
        default_style=default_style,
        slide_size=(slide_width, slide_height),
        selected_shape_name=selected_style.shape_name,
        # True when the selected slide's shape was guessed, i.e. its name is not
        # the configured target (``제목 1`` by default, or the env override).
        shape_detected=selected_style.shape_name != config.TITLE_SHAPE_NAME,
    )


def get_template_profile(
    template_path: Path | None = None,
    *,
    skip_slide: tuple[int, ...] = (),
    slide_index: int | None = None,
) -> TemplateProfile:
    """Return the (cached) template profile.

    Args:
        template_path: template to read; defaults to ``config.TEMPLATE_PATH``.
        skip_slide: 1-based template slides excluded from ``pool``.
        slide_index: 1-based template slide cloned by every output slide;
            ``None`` uses ``config.SLIDE_INDEX``.

    Raises:
        ConfigError: the template file is missing, cannot be parsed, or
            ``slide_index`` is out of range.
    """
    path = Path(template_path) if template_path is not None else config.TEMPLATE_PATH
    if not path.is_file():
        raise ConfigError(f"템플릿을 열 수 없습니다: {path}")

    selected = config.SLIDE_INDEX if slide_index is None else slide_index
    # Only the *default* may fall back to the last slide; a number the caller (or
    # LYRICS2PPTX_SLIDE_INDEX) asked for is honoured or rejected, never clamped.
    allow_fallback = slide_index is None and not config.SLIDE_INDEX_IS_EXPLICIT

    stat = path.stat()
    key = (
        str(path), stat.st_size, stat.st_mtime_ns,
        tuple(sorted(skip_slide)), selected, allow_fallback,
    )
    cached = _PROFILE_CACHE.get(key)
    if cached is not None:
        log.debug(
            "profile cache hit key=(size=%d,mtime=%d,slide=%d)",
            stat.st_size,
            stat.st_mtime_ns,
            selected,
        )
        return cached

    profile = _analyze(
        path,
        skip_slide=key[3],
        slide_index=key[4],
        file_size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        allow_fallback=allow_fallback,
    )
    _PROFILE_CACHE[key] = profile
    log.info(
        "template profile loaded slides=%d selected=%d pool=%d cached=%s",
        profile.slide_count,
        profile.selected_slide + 1,
        len(profile.pool),
        False,
    )
    return profile
