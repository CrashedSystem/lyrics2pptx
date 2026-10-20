"""Method A writer — deep-copy one slide into new parts, then let python-pptx save.

Algorithm (docs/arch/04 §3.2–§3.5, §2.1; single-slide mode):

1. open the template once (this package already holds theme/layout/media);
2. materialize ``prs.slides`` **exactly once** — that access renames slide
   parts in ``sldIdLst`` order, and doing it again *after* cloning would
   collide part names on save;
3. for each planned slide, clone the **same** template slide
   (``profile.selected_slide``) repeatedly, register the clone in ``sldIdLst``
   with a fresh unique ``@id``, then fill ``제목 1``;
4. drop every original ``sldId`` reference and the presentation→original-slide
   relationships. python-pptx serializes only parts reachable from the package
   relationship graph, so the 19 unused original slides, their ``notesSlide``
   parts, their media and every layout/master/theme that no kept slide needs
   are simply not written (orphan pruning, privacy + size).

The template is opened read-only; this module never writes to it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pptx import Presentation
from pptx.oxml.ns import qn

from ..errors import ConfigError, TemplateStructureError
from .clone import RT_SLIDE, clone_slide
from .fill import fill_txBody, find_title1_txBody
from .template_reader import BuildResult, Plan, TemplateProfile

__all__ = ["write_via_parts"]

log = logging.getLogger(__name__)

# First ``sldId/@id`` when no original id is usable (matches python-pptx's 256).
_MIN_SLD_ID = 255


def write_via_parts(
    plan: Plan,
    profile: TemplateProfile,
    out: Path,
    *,
    keep_notes: bool = False,
) -> BuildResult:
    """Write ``plan`` using method A and return the build result.

    Raises:
        ConfigError: the plan is empty.
        TemplateStructureError: a cloned slide lost its ``제목 1`` shape.
        OutputWriteError: (propagated from the caller) — saving is the builder's
            concern; here python-pptx errors surface as their own exceptions.
    """
    if not plan:
        raise ConfigError("출력할 슬라이드가 없습니다")

    presentation = Presentation(str(profile.template_path))

    # (1) The one and only access to ``prs.slides``: it renames slide parts and
    #     we must keep the very same ``Slides`` proxy afterwards.
    slides_proxy = presentation.slides
    originals = list(slides_proxy)
    sld_id_lst = slides_proxy._sldIdLst

    # r:ids of the presentation→original-slide relationships. Dropped at the end
    # so their parts (and media/notes) become unreachable and are not serialized.
    original_slide_rids = [element.get(qn("r:id")) for element in sld_id_lst]

    original_ids = [element.get("id") for element in sld_id_lst]
    next_id = max((int(i) for i in original_ids if i and i.isdigit()), default=_MIN_SLD_ID) + 1

    # (2) Remove the original 20 references (references only — parts are pruned
    #     later through the relationship graph).
    for element in list(sld_id_lst):
        sld_id_lst.remove(element)

    source_slide = originals[profile.selected_slide]
    style = profile.styles.get(profile.selected_slide, profile.default_style)
    presentation_part = presentation.part

    for order, (line1, line2) in enumerate(plan):
        new_part = clone_slide(source_slide, presentation, keep_notes=keep_notes)

        # get-or-add: clone_slide already registered it, so this returns its rId.
        r_id = presentation_part.relate_to(new_part, RT_SLIDE)

        sld_id = sld_id_lst.add_sldId(r_id)
        sld_id.set("id", str(next_id))
        next_id += 1

        tx_body = find_title1_txBody(
            new_part._element, name=profile.selected_shape_name
        )
        if tx_body is None:
            raise TemplateStructureError(
                f"복제 슬라이드에서 {profile.selected_shape_name!r} 도형을 찾지 못했습니다 "
                f"(order={order}, slide_index={profile.selected_slide + 1})"
            )
        fill_txBody(
            tx_body,
            line1,
            line2,
            style=style,
            slide_index=profile.selected_slide,
            default_style=profile.default_style,
        )

    # (3) Orphan pruning: sever presentation→original-slide relationships. Every
    #     part still reachable from the package graph (kept layout/master/theme,
    #     the one background image, docProps) is written; the rest is not.
    for r_id in original_slide_rids:
        if r_id and r_id in presentation_part.rels:
            presentation_part.drop_rel(r_id)

    presentation.save(str(out))

    slide_count = len(plan)
    return BuildResult(
        output_path=Path(out),
        slide_count=slide_count,
        slide_index=profile.selected_slide + 1,
        dropped_notes_count=0 if keep_notes else slide_count,
    )
