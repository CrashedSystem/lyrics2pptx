"""Template analysis: background pool + per-slide ``제목 1`` style cache (L2).

Loads ``template.pptx`` **once** per process (cached on path/size/mtime) and
records, for every slide, the formatting snapshot of the ``제목 1`` shape
(docs/arch/03 §2, docs/arch/04 §1/§5).

The template is read-only: this module never opens it for writing.
"""

from __future__ import annotations

import colorsys
import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Mapping

from lxml import etree
from PIL import Image, ImageColor, ImageStat
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
    "background_of",
    "background_image_reference",
    "theme_colors",
    "resolve_color",
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


# A hand-drawn background rectangle rarely lands on the exact EMU: PowerPoint
# keeps a few EMU of offset, or the shape is a hair smaller than the slide. Only
# accepting an exact match made such a rectangle a *text* candidate and put the
# lyrics inside the background (ADR-035).
_FULL_BLEED_TOLERANCE = 0.01  # of each slide edge


def _tolerance(edge: int) -> int:
    return max(1270, int(edge * _FULL_BLEED_TOLERANCE))  # ≥ 0.1 pt


def _covers_slide(
    left: int, top: int, width: int, height: int, slide_width: int, slide_height: int
) -> bool:
    """True when a rectangle covers the slide within the tolerance above."""
    tol_x = _tolerance(slide_width)
    tol_y = _tolerance(slide_height)
    return (
        abs(left) <= tol_x
        and abs(top) <= tol_y
        and width >= slide_width - tol_x
        and height >= slide_height - tol_y
    )


def is_full_bleed_shape(
    sp_element: etree._Element, slide_width: int, slide_height: int
) -> bool:
    """True when an autoshape covers (almost) the whole slide (a background)."""
    left, top, width, height = _raw_rect(sp_element)
    if width <= 0 or height <= 0:
        return False
    return _covers_slide(left, top, width, height, slide_width, slide_height)


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


# ---------------------------------------------------------------- geometry

def _raw_rect(sp_element: etree._Element) -> tuple[int, int, int, int]:
    """``(left, top, width, height)`` in the shape's own coordinate system."""
    sp_pr = sp_element.find(qn("p:spPr"))
    if sp_pr is None:
        return (0, 0, 0, 0)
    transform = sp_pr.find(qn("a:xfrm"))
    if transform is None:
        return (0, 0, 0, 0)
    offset = transform.find(qn("a:off"))
    extent = transform.find(qn("a:ext"))
    if offset is None or extent is None:
        return (0, 0, 0, 0)
    try:
        return (
            int(offset.get("x")),
            int(offset.get("y")),
            int(extent.get("cx")),
            int(extent.get("cy")),
        )
    except (TypeError, ValueError):
        return (0, 0, 0, 0)


def _group_frame(group: etree._Element) -> tuple[int, ...] | None:
    """``(off, ext, chOff, chExt)`` of a group, as eight integers."""
    transform = group.find(f"{qn('p:grpSpPr')}/{qn('a:xfrm')}")
    if transform is None:
        return None
    offset = transform.find(qn("a:off"))
    extent = transform.find(qn("a:ext"))
    child_offset = transform.find(qn("a:chOff"))
    child_extent = transform.find(qn("a:chExt"))
    if None in (offset, extent, child_offset, child_extent):
        return None
    try:
        return (
            int(offset.get("x")),
            int(offset.get("y")),
            int(extent.get("cx")),
            int(extent.get("cy")),
            int(child_offset.get("x")),
            int(child_offset.get("y")),
            int(child_extent.get("cx")),
            int(child_extent.get("cy")),
        )
    except (TypeError, ValueError):
        return None


class _Geometry:
    """Slide-space rectangles, undoing nested group coordinate systems.

    A text box inside a ``p:grpSp`` stores its ``a:xfrm`` in the group's child
    space, so comparing it with the slide size (ranking, full-bleed test,
    synthesized font size) was wrong before ADR-035.
    """

    def __init__(
        self,
        sld_element: etree._Element,
        inherited_from: tuple[etree._Element, ...] = (),
    ) -> None:
        self._parents = {
            child: parent for parent in sld_element.iter() for child in parent
        }
        self._inherited_from = inherited_from
        self._cache: dict[etree._Element, tuple[int, int, int, int]] = {}

    def rect(self, sp_element: etree._Element) -> tuple[int, int, int, int]:
        cached = self._cache.get(sp_element)
        if cached is not None:
            return cached
        left, top, width, height = self._rect_or_inherited(sp_element)
        node: etree._Element = sp_element
        while True:
            group = self._group_of(node)
            if group is None:
                break
            frame = _group_frame(group)
            if frame is None:
                break
            gx, gy, gw, gh, cx, cy, cw, ch = frame
            if cw <= 0 or ch <= 0:
                break
            left = gx + int(round((left - cx) * gw / cw))
            top = gy + int(round((top - cy) * gh / ch))
            width = int(round(width * gw / cw))
            height = int(round(height * gh / ch))
            node = group
        rect = (left, top, width, height)
        self._cache[sp_element] = rect
        return rect

    def _rect_or_inherited(
        self, sp_element: etree._Element
    ) -> tuple[int, int, int, int]:
        """A placeholder without its own ``a:xfrm`` borrows the layout's box.

        Real decks keep the title/body geometry on the layout or master, so the
        slide's ``p:sp`` often has no ``a:xfrm`` at all. Treating that as a 0x0
        rectangle made the placeholder look like an empty caption and dropped it
        from the candidates (ADR-035).
        """
        rect = _raw_rect(sp_element)
        if rect[2] > 0 and rect[3] > 0:
            return rect
        placeholder = sp_element.find(
            f"{qn('p:nvSpPr')}/{qn('p:nvPr')}/{qn('p:ph')}"
        )
        if placeholder is None:
            return rect
        inherited = _inherited_placeholder_rect(
            self._inherited_from,
            placeholder.get("idx"),
            (placeholder.get("type") or "obj").strip(),
        )
        return inherited if inherited is not None else rect

    def _group_of(self, node: etree._Element) -> etree._Element | None:
        parent = self._parents.get(node)
        while parent is not None:
            if parent.tag == qn("p:grpSp"):
                return parent
            parent = self._parents.get(parent)
        return None


def _inherited_placeholder_rect(
    elements: tuple[etree._Element, ...], index: str | None, ph_type: str
) -> tuple[int, int, int, int] | None:
    """Box of the placeholder with the same ``idx`` (else type) in a parent part."""
    for element in elements:
        for candidate in element.iter(qn("p:sp")):
            placeholder = candidate.find(
                f"{qn('p:nvSpPr')}/{qn('p:nvPr')}/{qn('p:ph')}"
            )
            if placeholder is None:
                continue
            candidate_index = placeholder.get("idx")
            if index is not None and candidate_index is not None:
                same_slot = candidate_index == index
            else:
                same_slot = (placeholder.get("type") or "obj").strip() == ph_type
            if not same_slot:
                continue
            rect = _raw_rect(candidate)
            if rect[2] > 0 and rect[3] > 0:
                return rect
    return None


def _geometry_for(slide) -> _Geometry:
    """Slide geometry that can borrow a placeholder box from layout/master."""
    specs = background_specs(slide)
    elements = tuple(element for _part, element in specs)
    return _Geometry(elements[0], elements[1:])


# ---------------------------------------------------------------- theme colours

_COLOR_TAG_NAMES = frozenset({"srgbClr", "schemeClr", "sysClr", "prstClr"})
_THEME_RELTYPE_SUFFIX = "/theme"
_DIAGRAM_NS = "{http://schemas.openxmlformats.org/drawingml/2006/diagram}relIds"


def _hex_rgb(value: str | None) -> tuple[int, int, int] | None:
    text = (value or "").strip()
    if len(text) != 6:
        return None
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return None


def _theme_element(slide) -> etree._Element | None:
    """The theme part of the slide's master (``a:theme``), parsed once per call."""
    try:
        master_part = slide.slide_layout.slide_master.part
    except AttributeError:  # pragma: no cover - python-pptx always exposes it
        return None
    for rel in master_part.rels.values():
        if rel.reltype.endswith(_THEME_RELTYPE_SUFFIX) and not rel.is_external:
            try:
                return etree.fromstring(rel.target_part.blob)
            except etree.XMLSyntaxError:  # pragma: no cover - OPC validates XML
                return None
    return None


def theme_colors(slide) -> dict[str, tuple[int, int, int]]:
    """``a:clrScheme`` of the slide's theme, as ``name -> RGB``.

    ``tx1``/``bg1``/``tx2``/``bg2`` are Office aliases of ``dk1``/``lt1``/``dk2``/
    ``lt2``; both spellings are returned so a ``schemeClr`` resolves either way.
    """
    theme = _theme_element(slide)
    if theme is None:  # pragma: no cover - every presentation has a theme
        return {}
    scheme = theme.find(f".//{qn('a:clrScheme')}")
    if scheme is None:  # pragma: no cover
        return {}
    colors: dict[str, tuple[int, int, int]] = {}
    for entry in scheme:
        color = entry[0] if len(entry) else None
        if color is None:
            continue
        name = etree.QName(color).localname
        if name == "srgbClr":
            rgb = _hex_rgb(color.get("val"))
        elif name == "sysClr":
            rgb = _hex_rgb(color.get("lastClr"))
        else:  # pragma: no cover - clrScheme stores srgbClr or sysClr
            rgb = None
        if rgb is not None:
            colors[etree.QName(entry).localname] = rgb
    for alias, target in (("tx1", "dk1"), ("bg1", "lt1"), ("tx2", "dk2"), ("bg2", "lt2")):
        if target in colors:
            colors.setdefault(alias, colors[target])
    return colors


def _luminance(rgb: tuple[int, int, int]) -> float:
    """Relative luminance (WCAG), 0.0 = black, 1.0 = white."""

    def channel(value: int) -> float:
        ratio = value / 255
        return ratio / 12.92 if ratio <= 0.03928 else ((ratio + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(value) for value in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _apply_color_transforms(
    rgb: tuple[int, int, int], color_element: etree._Element
) -> tuple[int, int, int]:
    """Apply ``tint``/``shade``/``lumMod``/``lumOff``/``alpha`` children in order."""
    red, green, blue = rgb
    for modifier in color_element:
        tag = etree.QName(modifier).localname
        raw = modifier.get("val")
        if raw is None:
            continue
        try:
            ratio = int(raw) / 100000.0
        except ValueError:
            continue
        if tag == "tint":  # towards white; 100000 = unchanged
            red = round(red * ratio + 255 * (1 - ratio))
            green = round(green * ratio + 255 * (1 - ratio))
            blue = round(blue * ratio + 255 * (1 - ratio))
        elif tag == "shade":  # towards black; 100000 = unchanged
            red, green, blue = round(red * ratio), round(green * ratio), round(blue * ratio)
        elif tag in ("lumMod", "lumOff"):
            hue, lightness, saturation = colorsys.rgb_to_hls(
                red / 255, green / 255, blue / 255
            )
            lightness = (
                lightness * ratio if tag == "lumMod" else lightness + ratio
            )
            red, green, blue = (
                round(value * 255)
                for value in colorsys.hls_to_rgb(
                    hue, min(max(lightness, 0.0), 1.0), saturation
                )
            )
        elif tag == "alpha":  # a background is composited over white
            red = round(red * ratio + 255 * (1 - ratio))
            green = round(green * ratio + 255 * (1 - ratio))
            blue = round(blue * ratio + 255 * (1 - ratio))
        red, green, blue = (min(max(value, 0), 255) for value in (red, green, blue))
    return (red, green, blue)


def resolve_color(
    color_element: etree._Element | None,
    theme: Mapping[str, tuple[int, int, int]],
) -> tuple[int, int, int] | None:
    """Resolve one ``a:*Clr`` element (with its modifiers) to RGB, or ``None``."""
    if color_element is None:
        return None
    name = etree.QName(color_element).localname
    if name == "srgbClr":
        rgb = _hex_rgb(color_element.get("val"))
    elif name == "sysClr":
        rgb = _hex_rgb(color_element.get("lastClr"))
        if rgb is None:
            rgb = {
                "window": (255, 255, 255),
                "windowText": (0, 0, 0),
            }.get((color_element.get("val") or "").strip())
    elif name == "schemeClr":
        rgb = theme.get((color_element.get("val") or "").strip())
    elif name == "prstClr":
        try:
            rgb = tuple(
                ImageColor.getrgb((color_element.get("val") or "").strip())[:3]
            )
        except (ValueError, TypeError):
            rgb = None
    else:  # pragma: no cover - callers only pass known colour elements
        rgb = None
    if rgb is None:
        return None
    return _apply_color_transforms(rgb, color_element)


# ---------------------------------------------------------------- background

def _background_element(element: etree._Element) -> etree._Element | None:
    return element.find(f"{qn('p:cSld')}/{qn('p:bg')}")


def _first_color_node(element: etree._Element) -> etree._Element | None:
    for node in element.iter():
        if etree.QName(node).localname in _COLOR_TAG_NAMES:
            return node
    return None


def _background_color_node(element: etree._Element) -> etree._Element | None:
    background = _background_element(element)
    if background is None:
        return None
    for tag in (qn("p:bgPr"), qn("p:bgRef")):
        node = background.find(tag)
        if node is None:
            continue
        color = _first_color_node(node)
        if color is not None:
            return color
    return None


def _background_image_reference(element: etree._Element) -> str | None:
    background = _background_element(element)
    if background is None:
        return None
    for blip in background.iter(qn("a:blip")):
        reference = blip.get(qn("r:embed")) or blip.get(qn("r:link"))
        if reference:
            return reference
    return None


def _full_bleed_image_reference(
    element: etree._Element, slide_width: int, slide_height: int
) -> str | None:
    """Picture that acts as the background of this part.

    A full-bleed picture wins; if none is exactly full-bleed (a hand-drawn
    design often is not), the largest picture is used, which is what the UI
    showed before ADR-035.
    """
    best: tuple[int, int, str] | None = None  # (is_full_bleed, area, rel)
    for picture in element.iter(qn("p:pic")):
        left, top, width, height = _raw_rect(picture)
        full_bleed = 1 if _covers_slide(
            left, top, width, height, slide_width, slide_height
        ) else 0
        for blip in picture.iter(qn("a:blip")):
            reference = blip.get(qn("r:embed")) or blip.get(qn("r:link"))
            if not reference:
                continue
            candidate = (full_bleed, width * height, reference)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
            break
    for shape in element.iter(qn("p:sp")):
        if not is_full_bleed_shape(shape, slide_width, slide_height):
            continue
        sp_pr = shape.find(qn("p:spPr"))
        if sp_pr is None or sp_pr.find(qn("a:blipFill")) is None:
            continue
        for blip in sp_pr.iter(qn("a:blip")):
            reference = blip.get(qn("r:embed")) or blip.get(qn("r:link"))
            if reference:
                return reference
    return best[2] if best is not None else None


def background_image_reference(
    element: etree._Element, slide_width: int, slide_height: int
) -> str | None:
    """Image ``r:embed``/``r:link`` that paints this part's background, if any."""
    return _background_image_reference(element) or _full_bleed_image_reference(
        element, slide_width, slide_height
    )


def _full_bleed_shape_color(
    element: etree._Element,
    theme: Mapping[str, tuple[int, int, int]],
    slide_width: int,
    slide_height: int,
) -> tuple[int, int, int] | None:
    for shape in element.iter(qn("p:sp")):
        if not is_full_bleed_shape(shape, slide_width, slide_height):
            continue
        sp_pr = shape.find(qn("p:spPr"))
        if sp_pr is None or sp_pr.find(qn("a:blipFill")) is not None:
            continue
        solid = sp_pr.find(qn("a:solidFill"))
        if solid is None:
            continue
        rgb = resolve_color(_first_color_node(solid), theme)
        if rgb is not None:
            return rgb
    return None


def background_specs(slide) -> tuple[tuple[object, etree._Element], ...]:
    """``(part, element)`` in OOXML inheritance order: slide → layout → master."""
    specs: list[tuple[object, etree._Element]] = [(slide.part, slide._element)]
    layout = getattr(slide, "slide_layout", None)
    if layout is None:  # pragma: no cover - every slide has a layout
        return tuple(specs)
    specs.append((layout.part, layout._element))
    master = getattr(layout, "slide_master", None)
    if master is not None:
        specs.append((master.part, master._element))
    return tuple(specs)


def background_of(
    slide, slide_width: int, slide_height: int
) -> tuple[str, object]:
    """The slide's *effective* background, inherited like PowerPoint does.

    A slide inherits its background from its layout, and the layout from its
    master. Reading only the slide's own XML left every master-based template
    with a blank (white) tile and black lyrics on a dark deck — the two most
    common real-world cases after a swap (ADR-035).

    Returns:
        ``("image", (part, rel_id))``, ``("color", (r, g, b))`` or
        ``("none", None)``. The image's relationship lives on ``part``, so the
        caller must resolve ``rel_id`` against that part, not the slide part.
    """
    theme = theme_colors(slide)
    for part, element in background_specs(slide):
        reference = background_image_reference(element, slide_width, slide_height)
        if reference is not None:
            return ("image", (part, reference))
        rgb = resolve_color(_background_color_node(element), theme)
        if rgb is None:
            rgb = _full_bleed_shape_color(element, theme, slide_width, slide_height)
        if rgb is not None:
            return ("color", rgb)
    return ("none", None)


def _image_luminance(part: object, reference: str) -> float | None:
    rel = part.rels.get(reference)  # type: ignore[attr-defined]
    if rel is None or rel.is_external:
        return None
    try:
        with Image.open(BytesIO(rel.target_part.blob)) as image:
            return ImageStat.Stat(image.convert("L")).mean[0] / 255.0
    except (OSError, ValueError):
        return None


def _background_luminance(slide, slide_width: int, slide_height: int) -> float | None:
    kind, value = background_of(slide, slide_width, slide_height)
    if kind == "color":
        return _luminance(value)  # type: ignore[arg-type]
    if kind == "image":
        part, reference = value  # type: ignore[misc]
        return _image_luminance(part, reference)
    return None


def _theme_font(slide) -> str:
    """East Asian typeface of the theme's minor font (lyrics use the body font)."""
    theme = _theme_element(slide)
    if theme is None:
        return ""
    for family in ("a:minorFont", "a:majorFont"):
        node = theme.find(f".//{qn('a:fontScheme')}/{qn(family)}/{qn('a:ea')}")
        if node is not None:
            face = (node.get("typeface") or "").strip()
            if face:
                return face
    return ""


# ---------------------------------------------------------------- shape choice

_TITLE_PLACEHOLDERS = frozenset({"title", "ctrTitle"})
_META_PLACEHOLDERS = frozenset({"sldNum", "dt", "ftr"})
_TITLE_NAME_HINTS = ("제목", "title", "heading", "headline")
_LYRIC_NAME_HINTS = ("가사", "lyrics", "lyric", "본문", "내용", "message")


def _placeholder_type(sp_element: etree._Element) -> str:
    placeholder = sp_element.find(
        f"{qn('p:nvSpPr')}/{qn('p:nvPr')}/{qn('p:ph')}"
    )
    if placeholder is None:
        return ""
    return (placeholder.get("type") or "").strip()


def _shape_score(
    sp_element: etree._Element, geometry: _Geometry, slide_area: int
) -> int:
    """Prefer real title placeholders, lyric-sounding names and big boxes."""
    placeholder = _placeholder_type(sp_element)
    name = _shape_name_of(sp_element).lower()
    _left, _top, width, height = geometry.rect(sp_element)
    area_ratio = (width * height) / slide_area if slide_area else 0.0

    score = 0
    if placeholder in _TITLE_PLACEHOLDERS:
        score += 100
    if any(hint in name for hint in _TITLE_NAME_HINTS):
        score += 60
    if any(hint in name for hint in _LYRIC_NAME_HINTS):
        score += 80
    if _shape_text_of(sp_element):
        score += 25
    score += int(min(area_ratio, 1.0) * 30)
    if area_ratio < 0.02:  # a caption or a stray label
        score -= 40
    return score


def _unsupported_text_containers(sld_element: etree._Element) -> list[str]:
    """Name the containers we cannot fill (a table cell, a SmartArt diagram)."""
    found: list[str] = []
    for frame in sld_element.iter(qn("p:graphicFrame")):
        if not _shape_text_of(frame):
            continue
        if frame.find(f".//{qn('a:tbl')}") is not None:
            found.append("표")
        elif frame.find(f".//{_DIAGRAM_NS}") is not None:
            found.append("스마트아트")
    return found


def resolve_title_shape_element(
    sld_element: etree._Element,
    *,
    slide_width: int,
    slide_height: int,
    name: str | None = None,
    slide=None,
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

    geometry = _geometry_for(slide) if slide is not None else _Geometry(sld_element)
    slide_area = slide_width * slide_height
    candidates: list[etree._Element] = []
    for sp_element in sld_element.iter(qn("p:sp")):
        if sp_element.find(qn("p:txBody")) is None:
            continue
        if _placeholder_type(sp_element) in _META_PLACEHOLDERS:
            continue  # slide number / date / footer: never the lyric box
        left, top, width, height = geometry.rect(sp_element)
        if width <= 0 or height <= 0:
            continue
        if _covers_slide(left, top, width, height, slide_width, slide_height):
            continue  # the background itself
        sp_pr = sp_element.find(qn("p:spPr"))
        if sp_pr is not None and sp_pr.find(qn("a:blipFill")) is not None:
            continue
        candidates.append(sp_element)

    if not candidates:
        return None, target, False

    def sort_key(sp_element: etree._Element) -> tuple[int, int, int]:
        score = _shape_score(sp_element, geometry, slide_area)
        _left, top, _width, _height = geometry.rect(sp_element)
        return (-score, top, _left)

    ranked = sorted(candidates, key=sort_key)
    best = ranked[0]
    resolved = _shape_name_of(best)
    if len(candidates) > 1:
        detail = ", ".join(
            f"{_shape_name_of(item)!r}={_shape_score(item, geometry, slide_area)}점"
            for item in ranked[:5]
        )
        log.warning(
            "제목 도형을 이름으로 찾지 못해 추정했습니다: %r (후보 %d개 — %s). "
            "다른 도형을 쓰려면 그 도형 이름을 %r 로 바꾸거나 "
            "LYRICS2PPTX_TITLE_SHAPE 로 지정하세요.",
            resolved,
            len(candidates),
            detail,
            config.TITLE_SHAPE_NAME,
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
_SYNTH_FONT = "맑은 고딕"  # fallback when the theme names no East Asian font
_SYNTH_DARK_TEXT = (0, 0, 0)
_SYNTH_LIGHT_TEXT = (255, 255, 255)
# Below this relative luminance the background is "dark" and black lyrics would
# disappear, so the synthesized colour flips to white (ADR-035).
_SYNTH_TEXT_CONTRAST_THRESHOLD = 0.45


def _synthesize_style(
    shape_element: etree._Element,
    slide_index: int,
    *,
    slide_height: int,
    slide=None,
    slide_width: int = 0,
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

    if slide is not None:
        # A box inside a group stores child-space coordinates; use the slide-space
        # rectangle so the synthesized size matches what the user sees.
        _left, _top, _width, height = _geometry_for(slide).rect(shape_element)
    else:
        _width, height = _shape_extent(shape_element)
    height_emu = height or slide_height
    size_pt = int(round(height_emu / 12700 * 0.42))  # EMU -> pt, ~2 lines + leading
    size_pt = max(_SYNTH_MIN_PT, min(_SYNTH_MAX_PT, size_pt))
    # ``sz`` and ``StyleSpec.sz`` are hundredths of a point (72 pt -> 7200).
    size = size_pt * 100

    font = (_theme_font(slide) if slide is not None else "") or _SYNTH_FONT
    luminance = (
        _background_luminance(slide, slide_width, slide_height)
        if slide is not None and slide_width > 0
        else None
    )
    text_rgb = (
        _SYNTH_LIGHT_TEXT
        if luminance is not None and luminance < _SYNTH_TEXT_CONTRAST_THRESHOLD
        else _SYNTH_DARK_TEXT
    )
    color_hex = "%02X%02X%02X" % text_rgb

    rpr = parse_xml(
        f'<a:rPr {nsdecls("a")} lang="ko-KR" sz="{size}">'
        f'<a:solidFill><a:srgbClr val="{color_hex}"/></a:solidFill>'
        f'<a:latin typeface="{font}"/><a:ea typeface="{font}"/>'
        "</a:rPr>"
    )
    log.warning(
        "템플릿 슬라이드 %d 의 가사 도형에 서식(rPr)이 없어 합성했습니다: "
        "%dpt %s %s 글자 (배경 명도 %s)",
        slide_index + 1,
        size_pt,
        font,
        "흰" if text_rgb == _SYNTH_LIGHT_TEXT else "검정",
        "?" if luminance is None else f"{luminance:.2f}",
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
            slide=slide,
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
        blocked = _unsupported_text_containers(
            presentation.slides[selected_index]._element
        )
        if blocked:
            reason = (
                f" 이 슬라이드의 글자는 {'/'.join(sorted(set(blocked)))} 안에 있습니다 — "
                "표·스마트아트는 지원하지 않습니다. 일반 텍스트 상자를 하나 추가하세요."
            )
        else:
            reason = " 텍스트 상자를 하나 두세요."
        raise TemplateStructureError(
            f"배경 슬라이드 {slide_index} 에 가사를 넣을 텍스트 도형이 없습니다. "
            f"존재하는 도형: {available}.{reason} "
            f"다른 도형을 쓰려면 그 이름을 {config.TITLE_SHAPE_NAME!r} 로 바꾸거나 "
            f"LYRICS2PPTX_TITLE_SHAPE 로 지정하세요."
        )

    # A template without any run style (a background plus an empty box) would
    # otherwise be rejected; synthesize one for the selected slide instead.
    if not selected_style.has_rpr:
        selected_element, _name, _detected = resolve_title_shape_element(
            presentation.slides[selected_index]._element,
            slide_width=slide_width,
            slide_height=slide_height,
            slide=presentation.slides[selected_index],
        )
        if selected_element is not None:
            styles[selected_index] = _synthesize_style(
                selected_element,
                selected_index,
                slide_height=slide_height,
                slide=presentation.slides[selected_index],
                slide_width=slide_width,
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
