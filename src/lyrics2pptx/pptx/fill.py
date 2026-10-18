"""``제목 1`` text replacement + autofit (L2).

The template's title shape stores its text as several fragmented runs (e.g.
``['너는 담장 너머로', ' ', '뻗은 나무', '가지에 훌륭 열매처럼']``). Replacing run
text in place would leave fragments behind, so the whole ``a:txBody`` is
reconstructed: the first run's ``rPr`` is taken as the style template and two
fresh paragraphs are built (docs/arch/04 §4, rules F1..F8).

Guarantees:

* F1 style: comes from the caller's ``StyleSpec`` (the first run's ``rPr`` of
  that template slide), so per-slide sizes (6000..7200) survive;
* F2 paragraphs: exactly one (odd last slide) or two — never three;
* F3/F7 line breaks are never inserted; text is set via ``a:t`` so lxml handles
  escaping;
* F4 ``normAutofit`` is always present after the fill;
* F5/F6 ``bodyPr`` anchor/wrap and the shape's ``a:off``/``a:ext`` are untouched.
"""

from __future__ import annotations

import copy
import logging

from lxml import etree
from pptx.oxml import parse_xml
from pptx.oxml.ns import qn

from .. import config
from ..errors import TemplateStructureError
from .template_reader import (
    StyleSpec,
    find_title_shape,
    find_title_shape_element,
)

__all__ = [
    "find_title1",
    "fill_title1",
    "fill_txBody",
    "ensure_norm_autofit",
    "has_norm_autofit",
    "find_title1_txBody",
]

log = logging.getLogger(__name__)

_fallback_logged: set[int] = set()


def find_title1(slide, *, name: str | None = None):
    """Return the ``제목 1`` shape proxy (by name, never by placeholder).

    Raises:
        TemplateStructureError: the shape does not exist on this slide.
    """
    return find_title_shape(slide, name=name)


def find_title1_txBody(sld_element, *, name: str | None = None):
    """Return the ``a:txBody`` element of the named title shape, if present."""
    shape_element = find_title_shape_element(sld_element, name=name)
    if shape_element is None:
        return None
    return shape_element.find(qn("p:txBody"))


def has_norm_autofit(tx_body) -> bool:
    """Return True when ``txBody``'s ``a:bodyPr`` carries ``a:normAutofit``."""
    body_pr = tx_body.find(qn("a:bodyPr"))
    if body_pr is None:
        return False
    return body_pr.find(qn("a:normAutofit")) is not None


def ensure_norm_autofit(body_pr) -> None:
    """Guarantee ``a:normAutofit`` and remove any conflicting autofit element.

    ``fontScale`` is intentionally left unspecified (= 100%): PowerPoint then
    shrinks the text only when it actually overflows (decision D8). ``anchor``
    and ``wrap`` attributes are preserved.
    """
    for tag in ("a:noAutofit", "a:spAutoFit"):
        for element in body_pr.findall(qn(tag)):
            body_pr.remove(element)

    if body_pr.find(qn("a:normAutofit")) is not None:
        return

    norm = etree.SubElement(body_pr, qn("a:normAutofit"))
    body_pr.remove(norm)
    insert_at = 0
    for position, child in enumerate(body_pr):
        if etree.QName(child).localname == "prstTxWarp":
            insert_at = position + 1
    body_pr.insert(insert_at, norm)


def _resolve_style(
    style: StyleSpec,
    *,
    slide_index: int | None,
    default_style: StyleSpec | None,
) -> StyleSpec:
    if style.has_rpr and style.rpr_xml:
        return style
    if default_style is None or not default_style.rpr_xml:
        raise TemplateStructureError(
            "제목 1 의 run 서식(rPr)을 찾지 못해 텍스트 서식을 상속할 수 없습니다"
        )
    if slide_index is not None and slide_index not in _fallback_logged:
        _fallback_logged.add(slide_index)
        log.warning("title1 style fallback slide=%s → default", slide_index)
    return default_style


def fill_txBody(
    tx_body,
    line1: str,
    line2: str | None,
    *,
    style: StyleSpec,
    slide_index: int | None = None,
    default_style: StyleSpec | None = None,
) -> None:
    """Rebuild ``tx_body`` with ``line1`` and (optionally) ``line2``.

    This is the XML-level core shared by the parts writer (through
    ``fill_title1``) and the zip fallback writer, which only has elements.

    Raises:
        TemplateStructureError: ``line1`` is empty or no usable style exists.
    """
    if not line1:
        raise TemplateStructureError("제목 1 에 채울 첫째 줄이 비어 있습니다")

    effective = _resolve_style(style, slide_index=slide_index, default_style=default_style)

    existing_paragraphs = tx_body.findall(qn("a:p"))
    original_p_pr = None
    for paragraph in existing_paragraphs:
        candidate = paragraph.find(qn("a:pPr"))
        if candidate is not None:
            original_p_pr = candidate
            break

    # F2: drop every original paragraph, including the fragmented runs.
    for paragraph in existing_paragraphs:
        tx_body.remove(paragraph)

    body_pr = tx_body.find(qn("a:bodyPr"))
    if body_pr is None:
        body_pr = etree.SubElement(tx_body, qn("a:bodyPr"))
        tx_body.insert(0, body_pr)
    ensure_norm_autofit(body_pr)

    texts = [line1] if line2 is None else [line1, line2]
    for text in texts:
        paragraph = etree.SubElement(tx_body, qn("a:p"))
        if original_p_pr is not None:
            paragraph.append(copy.deepcopy(original_p_pr))
        run = etree.SubElement(paragraph, qn("a:r"))
        # Fresh parse per run: never share one lxml element across runs/slides.
        run.append(parse_xml(effective.rpr_xml))
        text_element = etree.SubElement(run, qn("a:t"))
        text_element.text = text  # lxml escapes &, <, > (rule F7)


def fill_title1(
    slide,
    line1: str,
    line2: str | None,
    *,
    style: StyleSpec,
    name: str | None = None,
    default_style: StyleSpec | None = None,
    slide_index: int | None = None,
) -> None:
    """Fill the ``제목 1`` shape of ``slide`` with one or two lines.

    Raises:
        TemplateStructureError: the shape is missing or ``line1`` is empty.
    """
    shape = find_title1(slide, name=name)
    fill_txBody(
        shape.text_frame._txBody,
        line1,
        line2,
        style=style,
        slide_index=slide_index,
        default_style=default_style,
    )


# Re-exported for module-level discoverability of the configured shape name.
TITLE_SHAPE_NAME = config.TITLE_SHAPE_NAME
