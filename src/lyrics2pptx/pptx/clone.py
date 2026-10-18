"""Slide cloning engine — method A (L2).

python-pptx has no slide-copy API, and "add a blank slide then move shapes"
loses the ``slideLayout`` relationship and ``<p:bg>`` (method B, rejected). So
the whole ``<p:sld>`` element is deep-copied into a **new slide part** and the
relationship ids inside it are remapped (docs/arch/04 §3).

Invariants enforced here:

* media parts are never created — every image relationship reuses the source's
  target part (rules R-M1..R-M5);
* ``notesSlide`` relationships are skipped so a cloned slide has no notes;
* an ``r:embed``/``r:link``/``r:id`` value that cannot be mapped is an error, so
  a missing background can never pass silently.
"""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING

from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml.ns import qn
from pptx.parts.slide import SlidePart

from .. import config
from ..errors import TemplateStructureError
from .template_reader import get_template_profile

if TYPE_CHECKING:
    from pptx.presentation import Presentation

__all__ = [
    "RT_SLIDE",
    "RT_SLIDE_LAYOUT",
    "RT_NOTES_SLIDE",
    "clone_slide",
    "remap_relationships",
    "pptx_can_register_slide_part",
    "reset_capability_probe",
]

log = logging.getLogger(__name__)

RT_SLIDE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
RT_SLIDE_LAYOUT = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"
)
RT_NOTES_SLIDE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"
)

_REL_REF_ATTRS = ("r:embed", "r:link", "r:id")

_probe_result: bool | None = None


def reset_capability_probe() -> None:
    """Forget the cached capability probe result (used by tests)."""
    global _probe_result
    _probe_result = None


def _make_slide_part(partname, content_type: str, package, element) -> SlidePart:
    """Thin wrapper isolating the python-pptx internal constructor.

    Kept in a single function so a python-pptx version bump only has to be
    absorbed here (docs/arch/04 §3.2 "구현 메모").
    """
    return SlidePart(partname, content_type, package, element)


def remap_relationships(src_part, new_part, *, keep_notes: bool = False) -> dict[str, str]:
    """Re-point every relationship of the copied XML at the new slide part.

    Internal relationships (``slideLayout``, ``image``) reuse the **same target
    part** so no media part is ever duplicated (AD-06). External relationships
    keep their target URL. ``notesSlide`` is skipped unless ``keep_notes``.

    Raises:
        TemplateStructureError: the copied XML references a relationship id that
            the source part does not define.
    """
    id_map: dict[str, str] = {}
    for rel in src_part.rels.values():
        if rel.reltype == RT.NOTES_SLIDE and not keep_notes:
            continue
        if rel.is_external:
            id_map[rel.rId] = new_part.relate_to(rel.target_ref, rel.reltype, is_external=True)
        else:
            id_map[rel.rId] = new_part.relate_to(rel.target_part, rel.reltype)

    for attr in _REL_REF_ATTRS:
        qualified = qn(attr)
        for element in new_part._element.iter():
            value = element.get(qualified)
            if value is None:
                continue
            replacement = id_map.get(value)
            if replacement is None:
                raise TemplateStructureError(
                    f"복제 슬라이드에 매핑되지 않은 관계 참조가 있습니다: {attr}={value}"
                )
            element.set(qualified, replacement)
    return id_map


def clone_slide(src_slide, target_prs: "Presentation", *, keep_notes: bool = False) -> SlidePart:
    """Deep-copy ``src_slide`` into a brand new slide part of ``target_prs``.

    Args:
        src_slide: a template ``Slide`` proxy (read-only input).
        target_prs: the ``Presentation`` the clone is attached to. It must be
            the presentation opened from the template so theme/layout/media
            parts already exist in the same package.
        keep_notes: when True the notesSlide relationship is copied too.

    Returns:
        The registered ``SlidePart``. Its ``sldId`` is not yet in
        ``sldIdLst`` — ``writer_parts`` owns that step.
    """
    package = target_prs.part.package
    new_element = copy.deepcopy(src_slide._element)  # <p:sld> whole element
    partname = package.next_partname("/ppt/slides/slide%d.xml")
    new_part = _make_slide_part(partname, src_slide.part.content_type, package, new_element)

    # Restore the lxml back-pointer when the runtime supports it. python-pptx
    # 1.0 resolves `.part` through the proxy parent chain, so this is a
    # best-effort convenience rather than a requirement.
    try:
        new_element.part = new_part  # type: ignore[attr-defined]
    except (AttributeError, TypeError):  # pragma: no cover - version dependent
        log.debug("lxml part back-pointer unsupported; continuing")

    remap_relationships(src_slide.part, new_part, keep_notes=keep_notes)

    # Register immediately so the part becomes reachable for next_partname()
    # and for the package writer. relate_to is a get-or-add, so a later call
    # with the same (reltype, target) returns this same rId.
    target_prs.part.relate_to(new_part, RT_SLIDE)
    return new_part


def pptx_can_register_slide_part() -> bool:
    """Return True when python-pptx internals allow method A part registration.

    The verdict is computed once per process. A failure is not a user error: it
    simply switches the builder to the zip fallback (docs/arch/04 §5).
    """
    global _probe_result
    if _probe_result is None:
        _probe_result = _run_capability_probe()
    return _probe_result


def _run_capability_probe() -> bool:
    from pptx import Presentation

    try:
        profile = get_template_profile(config.TEMPLATE_PATH)
        presentation = Presentation(str(profile.template_path))
        package = presentation.part.package
        index = 1 if profile.slide_count > 1 else 0
        source_part = presentation.slides[index].part
        partname = package.next_partname("/ppt/slides/slide%d.xml")
        candidate = _make_slide_part(partname, source_part.content_type, package, None)
        if candidate is None:  # pragma: no cover - constructor never returns None
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - probe failure means "use fallback"
        log.warning("방식 A 불가: %s → 폴백 F1 사용", exc)
        return False
