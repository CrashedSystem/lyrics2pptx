"""pptx — template reading, slide cloning, filling, and package writing.

This axis knows nothing about crawlers or lyric text (rule P3); it receives
already-split line pairs and produces a ``.pptx`` file.

Public surface:

* ``get_template_profile`` / ``StyleSpec`` / ``TemplateProfile`` — template analysis;
* ``clone_slide`` / ``remap_relationships`` — method A clone engine;
* ``find_title1`` / ``fill_title1`` / ``fill_txBody`` — ``제목 1`` text replacement;
* ``build_presentation`` / ``build_plan`` / ``SlideSpec`` / ``BuildResult`` — orchestration;
* ``select_writer`` / ``PptxWriter`` — writer strategy selection.
"""

from __future__ import annotations

from .builder import BuildResult, SlideSpec, build_plan, build_presentation
from .clone import (
    RT_NOTES_SLIDE,
    RT_SLIDE,
    RT_SLIDE_LAYOUT,
    clone_slide,
    pptx_can_register_slide_part,
    remap_relationships,
)
from .fill import (
    ensure_norm_autofit,
    fill_title1,
    fill_txBody,
    find_title1,
    find_title1_txBody,
    has_norm_autofit,
)
from .template_reader import (
    StyleSpec,
    TemplateProfile,
    clear_profile_cache,
    extract_slide_style,
    find_title_shape,
    get_template_profile,
)
from .thumbnails import (
    SlideMeta,
    SlideThumbnail,
    clear_thumbnail_cache,
    get_slide_thumbnail,
    list_slide_meta,
)
from .writer import PartsWriter, PptxWriter, ZipWriter, select_writer
from .writer_parts import write_via_parts
from .writer_zip import write_via_zip

__all__ = [
    # template reading
    "StyleSpec",
    "TemplateProfile",
    "get_template_profile",
    "clear_profile_cache",
    "extract_slide_style",
    "find_title_shape",
    # thumbnails
    "SlideMeta",
    "SlideThumbnail",
    "list_slide_meta",
    "get_slide_thumbnail",
    "clear_thumbnail_cache",
    # clone
    "clone_slide",
    "remap_relationships",
    "pptx_can_register_slide_part",
    "RT_SLIDE",
    "RT_SLIDE_LAYOUT",
    "RT_NOTES_SLIDE",
    # fill
    "find_title1",
    "fill_title1",
    "fill_txBody",
    "find_title1_txBody",
    "ensure_norm_autofit",
    "has_norm_autofit",
    # writer selection
    "PptxWriter",
    "PartsWriter",
    "ZipWriter",
    "select_writer",
    "write_via_parts",
    "write_via_zip",
    # builder
    "SlideSpec",
    "BuildResult",
    "build_plan",
    "build_presentation",
]
