"""Fallback F1 writer — rewrite the OPC package with ``zipfile`` + ``lxml``.

Used when method A is impossible (capability probe failed) or when its output
cannot be reopened (docs/arch/04 §6). The external contract is identical to
method A: single-slide mode, orphan pruning, and a document that opens.

Strategy, chosen for correctness over cleverness:

* for each planned slide ``k`` a fresh part ``ppt/slides/slide{1000+k}.xml`` is
  written from the **same** source slide (``profile.selected_slide``) with
  ``제목 1`` refilled;
* the new slide's rels file is a copy of the source's with ``notesSlide``
  entries dropped — rIds stay valid because the rels file is per-part;
* the presentation→original-slide relationships are removed and the
  ``<p:sldIdLst>`` is rebuilt to list only the new slides;
* the package is then **pruned by reachability**: only members reachable from
  ``_rels/.rels`` through the (rewritten) relationship graph are written, so the
  unused original slides, their notes, their media and every layout/master/theme
  no kept slide needs are omitted;
* ``[Content_Types].xml`` loses the ``Override`` entries of pruned parts and
  gains one per new part; every kept relationship resolves to a written part
  (no dangling relationships).

The writer only *writes* the given path; the builder owns the temp→``os.replace``
step (AD-13), so F1 and method A share the exact same atomicity guarantee.
"""

from __future__ import annotations

import logging
import posixpath
import zipfile
from pathlib import Path

from lxml import etree

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
_PACKAGE_RELS = "_rels/.rels"
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


def _rels_member(member: str) -> str:
    """Return the rels member name that belongs to an OPC part member."""
    directory, _, base = member.rpartition("/")
    if directory:
        return f"{directory}/_rels/{base}.rels"
    return f"_rels/{base}.rels"


def _rel_targets(data: dict[str, bytes], member: str | None) -> list[str]:
    """Return internal relationship targets declared by ``member``.

    ``member`` is ``None`` for the package-level ``_rels/.rels``. External
    relationships are ignored (they point outside the package).
    """
    rels_name = _PACKAGE_RELS if member is None else _rels_member(member)
    blob = data.get(rels_name)
    if blob is None:
        return []
    root = etree.fromstring(blob)
    base_dir = "" if member is None else member.rpartition("/")[0]
    targets: list[str] = []
    for rel in root.findall(f"{{{_REL_NS}}}Relationship"):
        if (rel.get("TargetMode") or "").lower() == "external":
            continue
        target = rel.get("Target") or ""
        if not target:
            continue
        if target.startswith("/"):
            resolved = target.lstrip("/")
        else:
            resolved = posixpath.normpath(posixpath.join(base_dir, target))
        targets.append(resolved)
    return targets


def _compute_kept(data: dict[str, bytes]) -> set[str]:
    """Return every member reachable from the package graph (plus rels files).

    Reachability starts at the package-level ``_rels/.rels`` targets and follows
    each part's relationships depth-first, so a member is kept only when some
    relationship chain leads to it. Every kept part also keeps its ``.rels``
    sidecar so its own relationships stay resolvable.
    """
    kept: set[str] = {"[Content_Types].xml", _PACKAGE_RELS}
    stack: list[str] = list(_rel_targets(data, None))

    while stack:
        name = stack.pop()
        if name in kept:
            continue
        if name not in data:
            continue
        kept.add(name)
        for target in _rel_targets(data, name):
            if target not in kept:
                stack.append(target)

    for name in list(kept):
        rels_name = _rels_member(name)
        if rels_name in data:
            kept.add(rels_name)

    return kept


def _filter_content_types(content_types_root: etree._Element, kept: set[str]) -> None:
    """Drop ``Override`` entries whose part was pruned (Defaults are kept)."""
    kept_partnames = {f"/{name}" for name in kept}
    for override in list(content_types_root.findall(f"{{{_CT_NS}}}Override")):
        if override.get("PartName") not in kept_partnames:
            content_types_root.remove(override)


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
        ConfigError: the plan is empty.
        TemplateStructureError: the template is missing an expected member or the
            source slide has no ``제목 1`` shape.
    """
    if not plan:
        raise ConfigError("출력할 슬라이드가 없습니다")

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

    original_rids = {element.get(f"{{{_R_NS}}}id") for element in sld_id_lst}
    for element in list(sld_id_lst):
        sld_id_lst.remove(element)

    source_index = profile.selected_slide
    source_name = _slide_member(source_index)
    source_rels = _slide_rels_member(source_index)
    if source_name not in data:
        raise TemplateStructureError(f"템플릿에 {source_name} 가 없습니다")

    style = profile.styles.get(source_index, profile.default_style)

    new_members: list[str] = []
    for order, (line1, line2) in enumerate(plan):
        new_index = _NEW_SLIDE_BASE + order
        target_name = f"ppt/slides/slide{new_index}.xml"
        target_rels = f"ppt/slides/_rels/slide{new_index}.xml.rels"

        slide_root = etree.fromstring(data[source_name])
        tx_body = find_title1_txBody(slide_root, name=profile.selected_shape_name)
        if tx_body is None:
            raise TemplateStructureError(
                f"템플릿 {source_name} 에 {profile.selected_shape_name!r} 도형이 없습니다"
            )
        fill_txBody(
            tx_body,
            line1,
            line2,
            style=style,
            slide_index=source_index,
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

    # Sever the presentation→original-slide relationships so the original slides
    # (and everything only they reach: notes, media, unused masters) are pruned.
    for relation in list(rels_root):
        if relation.get("Id") in original_rids:
            rels_root.remove(relation)

    data[_PRESENTATION] = _serialize(presentation_root)
    data[_PRESENTATION_RELS] = _serialize(rels_root)

    kept = _compute_kept(data)
    _filter_content_types(content_types_root, kept)
    data[_CONTENT_TYPES] = _serialize(content_types_root)

    ordered = [*names, *(m for m in new_members if m not in names)]
    written: set[str] = set()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ordered:
            if name in written or name not in kept:
                continue
            written.add(name)
            archive.writestr(name, data[name])

    slide_count = len(plan)
    return BuildResult(
        output_path=Path(out),
        slide_count=slide_count,
        slide_index=profile.selected_slide + 1,
        dropped_notes_count=0 if keep_notes else slide_count,
    )
