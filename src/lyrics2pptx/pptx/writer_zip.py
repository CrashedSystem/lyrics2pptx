"""Fallback F1 writer — rewrite the OPC package with ``zipfile`` + ``lxml``.

Used when method A is impossible (capability probe failed) or when its output
cannot be reopened (docs/arch/04 §6). The external contract is identical: the
produced ``.pptx`` opens and keeps every background.

Strategy, chosen for correctness over cleverness:

* every template member is copied **verbatim** into the output zip, so media,
  layouts, themes and the original slides are byte-for-byte preserved and the
  ``ppt/media/*`` count cannot change (AD-06);
* for each planned slide ``k`` a fresh part ``ppt/slides/slide{1000+k}.xml`` is
  written from the source slide ``pool[k % P]`` with ``제목 1`` refilled;
* the new slide's rels file is a copy of the source's with ``notesSlide``
  entries dropped — rIds stay valid because the rels file is per-part;
* ``[Content_Types].xml`` gains an ``Override`` per new part, and
  ``ppt/presentation.xml(.rels)`` gains one relationship per new part;
* ``<p:sldIdLst>`` is rebuilt to list only the new slides.

The writer only *writes* the given path; the builder owns the temp→``os.replace``
step (AD-13), so F1 and method A share the exact same atomicity guarantee.
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

from lxml import etree

from .. import config
from ..errors import ConfigError, TemplateStructureError
from .clone import RT_NOTES_SLIDE, RT_SLIDE
from .fill import fill_txBody, find_title1_txBody
from .template_reader import BuildResult, Plan, TemplateProfile

__all__ = ["write_via_zip"]

log = logging.getLogger(__name__)

_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_CONTENT_TYPES = "[Content_Types].xml"
_PRESENTATION = "ppt/presentation.xml"
_PRESENTATION_RELS = "ppt/_rels/presentation.xml.rels"

_SLIDE_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"
)

# New slide members live far above the template's numbering to avoid clashes.
_NEW_SLIDE_BASE = 1000
_MIN_SLD_ID = 255


def _serialize(element: etree._Element) -> bytes:
    return etree.tostring(
        element, xml_declaration=True, encoding="UTF-8", standalone=True
    )


def _slide_member(index: int) -> str:
    return f"ppt/slides/slide{index + 1}.xml"


def _slide_rels_member(index: int) -> str:
    return f"ppt/slides/_rels/slide{index + 1}.xml.rels"


def _max_sld_id(presentation_root: etree._Element) -> int:
    ids = [
        int(raw)
        for element in presentation_root.iter(f"{{{_P_NS}}}sldId")
        for raw in [(element.get("id") or "")]
        if raw.isdigit()
    ]
    return max(ids, default=_MIN_SLD_ID)


def _max_r_id(rels_root: etree._Element) -> int:
    ids: list[int] = []
    for element in rels_root.findall(f"{{{_REL_NS}}}Relationship"):
        raw = element.get("Id") or ""
        if raw.startswith("rId") and raw[3:].isdigit():
            ids.append(int(raw[3:]))
    return max(ids, default=0)


def _insert_sld_id_lst(presentation_root: etree._Element) -> etree._Element:
    """Return ``<p:sldIdLst>``, creating it in the schema-correct position."""
    existing = presentation_root.find(f"{{{_P_NS}}}sldIdLst")
    if existing is not None:
        return existing

    created = etree.Element(f"{{{_P_NS}}}sldIdLst")
    slide_size = presentation_root.find(f"{{{_P_NS}}}sldSz")
    if slide_size is not None:
        slide_size.addprevious(created)
    else:
        presentation_root.append(created)
    return created


def write_via_zip(
    plan: Plan,
    profile: TemplateProfile,
    out: Path,
    *,
    keep_notes: bool = False,
) -> BuildResult:
    """Write ``plan`` into a rebuilt OPC package at ``out``.

    Raises:
        ConfigError: the plan or the background pool is empty.
        TemplateStructureError: the template is missing an expected member or a
            source slide has no ``제목 1`` shape.
    """
    if not plan:
        raise ConfigError("출력할 슬라이드가 없습니다")
    if not profile.pool:
        raise ConfigError("배경 풀에 사용할 슬라이드가 없습니다")

    with zipfile.ZipFile(profile.template_path, "r") as archive:
        names = archive.namelist()
        data: dict[str, bytes] = {name: archive.read(name) for name in names}

    for required in (_CONTENT_TYPES, _PRESENTATION, _PRESENTATION_RELS):
        if required not in data:
            raise TemplateStructureError(f"템플릿 패키지에 {required} 가 없습니다")

    presentation_root = etree.fromstring(data[_PRESENTATION])
    rels_root = etree.fromstring(data[_PRESENTATION_RELS])
    content_types_root = etree.fromstring(data[_CONTENT_TYPES])

    sld_id_lst = _insert_sld_id_lst(presentation_root)
    next_sld_id = _max_sld_id(presentation_root) + 1
    next_r_id = _max_r_id(rels_root) + 1

    for element in list(sld_id_lst):
        sld_id_lst.remove(element)

    new_members: list[str] = []
    for order, (bg_index, line1, line2) in enumerate(plan):
        source_name = _slide_member(bg_index)
        source_rels = _slide_rels_member(bg_index)
        if source_name not in data:
            raise TemplateStructureError(f"템플릿에 {source_name} 가 없습니다")

        new_index = _NEW_SLIDE_BASE + order
        target_name = f"ppt/slides/slide{new_index}.xml"
        target_rels = f"ppt/slides/_rels/slide{new_index}.xml.rels"

        slide_root = etree.fromstring(data[source_name])
        tx_body = find_title1_txBody(slide_root)
        if tx_body is None:
            raise TemplateStructureError(
                f"템플릿 {source_name} 에 {config.TITLE_SHAPE_NAME!r} 도형이 없습니다"
            )
        fill_txBody(
            tx_body,
            line1,
            line2,
            style=profile.styles.get(bg_index, profile.default_style),
            slide_index=bg_index,
            default_style=profile.default_style,
        )
        data[target_name] = _serialize(slide_root)
        new_members.append(target_name)

        if source_rels in data:
            slide_rels_root = etree.fromstring(data[source_rels])
            if not keep_notes:
                for relation in list(slide_rels_root):
                    if relation.get("Type") == RT_NOTES_SLIDE:
                        slide_rels_root.remove(relation)
            data[target_rels] = _serialize(slide_rels_root)
            new_members.append(target_rels)

        override = etree.SubElement(content_types_root, f"{{{_CT_NS}}}Override")
        override.set("PartName", f"/{target_name}")
        override.set("ContentType", _SLIDE_CONTENT_TYPE)

        r_id = f"rId{next_r_id}"
        next_r_id += 1
        relation = etree.SubElement(rels_root, f"{{{_REL_NS}}}Relationship")
        relation.set("Id", r_id)
        relation.set("Type", RT_SLIDE)
        relation.set("Target", f"slides/slide{new_index}.xml")

        sld_id = etree.SubElement(sld_id_lst, f"{{{_P_NS}}}sldId")
        sld_id.set("id", str(next_sld_id))
        sld_id.set(f"{{{_R_NS}}}id", r_id)
        next_sld_id += 1

    data[_PRESENTATION] = _serialize(presentation_root)
    data[_PRESENTATION_RELS] = _serialize(rels_root)
    data[_CONTENT_TYPES] = _serialize(content_types_root)

    written: set[str] = set()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            if name in written:
                continue
            written.add(name)
            archive.writestr(name, data[name])
        for name in new_members:
            if name in written:
                continue
            written.add(name)
            archive.writestr(name, data[name])

    used = tuple(bg for bg, _, _ in plan)
    slide_count = len(plan)
    pool_size = len(profile.pool)
    return BuildResult(
        output_path=Path(out),
        slide_count=slide_count,
        pool_size=pool_size,
        wrap_count=(slide_count - 1) // pool_size if slide_count else 0,
        used_bg_indices=used,
        dropped_notes_count=0 if keep_notes else slide_count,
    )
