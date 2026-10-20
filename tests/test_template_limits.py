"""ADR-035: the limits left after ADR-034, pinned with synthetic templates.

ADR-034 made a replaced ``template.pptx`` work. Five real-world cases still
failed, and each has a test here:

1. the background lives on the **layout/master** (the usual PowerPoint habit),
   not on the slide → blank (white) tile;
2. the background colour is a **theme colour** (``schemeClr``), so only literal
   ``srgbClr`` was understood → white tile;
3. the background rectangle is **drawn by hand** and sits a few EMU off the
   slide → it was taken for a text box and the lyrics landed in the background;
4. the slide has several boxes (cover/section layouts), so "the biggest one"
   picked a subtitle → the title placeholder or a lyric-named box must win;
5. the text box is inside a **group** (``p:grpSp``) → its coordinates are in the
   group's child space, so size-based decisions were wrong.

It also covers the two "cannot do it" cases now answered precisely: a table or
SmartArt holding the text, and dark backgrounds where black lyrics disappear.
"""

from __future__ import annotations

import io
import re
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.oxml import parse_xml
from pptx.oxml.ns import nsdecls, qn
from pptx.util import Emu, Inches

from lyrics2pptx.errors import TemplateStructureError
from lyrics2pptx.pptx.builder import build_presentation
from lyrics2pptx.pptx.template_reader import (
    background_of,
    clear_profile_cache,
    get_template_profile,
)
from lyrics2pptx.pptx.thumbnails import clear_thumbnail_cache, get_slide_thumbnail

SLIDE_W = 12_192_000  # 16:9, the bundled template's shape
SLIDE_H = 6_858_000
ACCENT1 = (0x4F, 0x81, 0xBD)  # the default Office theme in python-pptx 1.0.2
DARK_RGB = (0x11, 0x22, 0x33)


def new_presentation() -> Presentation:
    prs = Presentation()
    prs.slide_width = Emu(SLIDE_W)
    prs.slide_height = Emu(SLIDE_H)
    return prs


def add_background_rect(
    slide,
    rgb: tuple[int, int, int] = DARK_RGB,
    *,
    dx: int = 0,
    dy: int = 0,
    shrink: int = 0,
):
    rect = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Emu(dx),
        Emu(dy),
        Emu(SLIDE_W - shrink),
        Emu(SLIDE_H - shrink),
    )
    rect.name = "배경 사각형"
    rect.fill.solid()
    rect.fill.fore_color.rgb = RGBColor(*rgb)
    rect.line.fill.background()
    return rect


def add_box(
    slide,
    name: str,
    *,
    left: float,
    top: float,
    width: float,
    height: float,
    text: str | None = None,
):
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    box.name = name
    if text is not None:
        box.text_frame.paragraphs[0].add_run().text = text
    return box


def set_background(element, fill_xml: str) -> None:
    """Replace a slide/layout/master ``p:bg`` with ``fill_xml``."""
    c_sld = element.find(qn("p:cSld"))
    old = c_sld.find(qn("p:bg"))
    if old is not None:
        c_sld.remove(old)
    c_sld.insert(0, parse_xml(f'<p:bg {nsdecls("p", "a", "r")}>{fill_xml}</p:bg>'))


def solid_background(color_xml: str) -> str:
    return f"<p:bgPr><a:solidFill>{color_xml}</a:solidFill><a:effectLst/></p:bgPr>"


def picture_background(part, image: Path) -> str:
    _image_part, r_id = part.get_or_add_image_part(str(image))
    return (
        '<p:bgPr><a:blipFill rotWithShape="1">'
        f'<a:blip r:embed="{r_id}"/>'
        "<a:stretch><a:fillRect/></a:stretch></a:blipFill><a:effectLst/></p:bgPr>"
    )


def set_group_frame(group, *, off, ext, child_off, child_ext) -> None:
    xfrm = group._element.find(f"{qn('p:grpSpPr')}/{qn('a:xfrm')}")
    for tag, (x, y) in (("a:off", off), ("a:chOff", child_off)):
        node = xfrm.find(qn(tag))
        node.set("x", str(int(x)))
        node.set("y", str(int(y)))
    for tag, (cx, cy) in (("a:ext", ext), ("a:chExt", child_ext)):
        node = xfrm.find(qn(tag))
        node.set("cx", str(int(cx)))
        node.set("cy", str(int(cy)))


def slide_number_placeholder_xml() -> str:
    return (
        '<p:sp xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<p:nvSpPr><p:cNvPr id="99" name="Slide Number Placeholder"/>'
        '<p:cNvSpPr/><p:nvPr><p:ph type="sldNum" sz="quarter" idx="12"/></p:nvPr>'
        "</p:nvSpPr>"
        f'<p:spPr><a:xfrm><a:off x="0" y="0"/>'
        f'<a:ext cx="{SLIDE_W - 200000}" cy="{SLIDE_H - 200000}"/></a:xfrm></p:spPr>'
        '<p:txBody><a:bodyPr/><a:p><a:r><a:rPr lang="en-US"/><a:t>12</a:t></a:r></a:p>'
        "</p:txBody></p:sp>"
    )


def inject_theme_font(path: Path, face: str) -> None:
    """Rewrite ``ppt/theme/theme1.xml`` so ``minorFont`` names ``face``."""
    staging = path.with_name(path.name + ".staging")
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(
        staging, "w", zipfile.ZIP_DEFLATED
    ) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename.startswith("ppt/theme/theme") and item.filename.endswith(
                ".xml"
            ):
                text = data.decode("utf-8")
                match = re.search(r"<a:minorFont>(.*?)</a:minorFont>", text, re.S)
                if match is not None:
                    block = match.group(1)
                    if "<a:ea " in block:
                        block = re.sub(
                            r'<a:ea typeface="[^"]*"\s*/>',
                            f'<a:ea typeface="{face}"/>',
                            block,
                            count=1,
                        )
                    else:
                        block = f'<a:ea typeface="{face}"/>' + block
                    text = text[: match.start(1)] + block + text[match.end(1) :]
                data = text.encode("utf-8")
            target.writestr(item, data)
    staging.replace(path)


class _TemplateCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        clear_profile_cache()
        clear_thumbnail_cache()
        self.addCleanup(clear_profile_cache)
        self.addCleanup(clear_thumbnail_cache)
        self._counter = 0

    def path(self, stem: str) -> Path:
        self._counter += 1
        return self.tmp / f"{stem}-{self._counter}.pptx"

    def pixel(self, template: Path) -> tuple[int, int, int]:
        thumbnail = get_slide_thumbnail(1, template)
        with Image.open(io.BytesIO(thumbnail.data)) as image:
            return image.convert("RGB").getpixel((10, 10))

    def synthesized_run(self, template: Path, out: Path, *, slide_index: int = 1):
        build_presentation([("첫째 줄", "둘째 줄")], template, out, slide_index=slide_index)
        produced = Presentation(str(out))
        shape = produced.slides[0].shapes[0]
        for candidate in produced.slides[0].shapes:
            if candidate.has_text_frame and candidate.text_frame.text.strip():
                shape = candidate
                break
        return shape.text_frame.paragraphs[0].runs[0]


class InheritedBackgroundTests(_TemplateCase):
    def test_a_master_background_image_is_inherited(self) -> None:
        image = self.tmp / "master-bg.png"
        Image.new("RGB", (40, 30), (10, 200, 30)).save(image)
        prs = new_presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        add_box(slide, "가사", left=1, top=2, width=6, height=1.5, text="가사 자리")
        master = prs.slide_masters[0]
        set_background(master._element, picture_background(master.part, image))
        template = self.path("master-image")
        prs.save(str(template))

        thumbnail = get_slide_thumbnail(1, template)

        self.assertEqual(thumbnail.content_type, "image/png")
        self.assertEqual(thumbnail.data, image.read_bytes())

    def test_a_layout_background_colour_is_inherited(self) -> None:
        prs = new_presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        add_box(slide, "가사", left=1, top=2, width=6, height=1.5, text="가사 자리")
        set_background(
            slide.slide_layout._element, solid_background('<a:schemeClr val="accent1"/>')
        )
        template = self.path("layout-colour")
        prs.save(str(template))

        self.assertEqual(self.pixel(template), ACCENT1)

    def test_background_of_reports_the_inherited_source(self) -> None:
        prs = new_presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        add_box(slide, "가사", left=1, top=2, width=6, height=1.5, text="가사 자리")
        set_background(
            slide.slide_layout._element, solid_background('<a:schemeClr val="accent1"/>')
        )

        kind, value = background_of(slide, SLIDE_W, SLIDE_H)

        self.assertEqual((kind, value), ("color", ACCENT1))


class ThemeColorTests(_TemplateCase):
    def template_with_background(self, color_xml: str, stem: str) -> Path:
        prs = new_presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        add_box(slide, "가사", left=1, top=2, width=6, height=1.5, text="가사 자리")
        set_background(slide._element, solid_background(color_xml))
        template = self.path(stem)
        prs.save(str(template))
        return template

    def test_scheme_clr_resolves_against_the_theme(self) -> None:
        template = self.template_with_background(
            '<a:schemeClr val="accent1"/>', "scheme"
        )
        self.assertEqual(self.pixel(template), ACCENT1)

    def test_lum_mod_darkens_the_theme_colour(self) -> None:
        base = self.pixel(
            self.template_with_background('<a:schemeClr val="accent1"/>', "base")
        )
        dark = self.pixel(
            self.template_with_background(
                '<a:schemeClr val="accent1"><a:lumMod val="50000"/></a:schemeClr>',
                "lummod",
            )
        )
        self.assertLess(sum(dark), sum(base))
        self.assertGreater(min(dark), 0)  # a modifier, not a collapse to black

    def test_sys_clr_uses_last_clr(self) -> None:
        template = self.template_with_background(
            '<a:sysClr val="window" lastClr="123456"/>', "sys"
        )
        self.assertEqual(self.pixel(template), (0x12, 0x34, 0x56))

    def test_prst_clr_is_resolved(self) -> None:
        template = self.template_with_background('<a:prstClr val="black"/>', "prst")
        self.assertEqual(self.pixel(template), (0, 0, 0))

    def test_an_unknown_scheme_name_falls_back_to_white(self) -> None:
        template = self.template_with_background(
            '<a:schemeClr val="noSuchColour"/>', "unknown"
        )
        self.assertEqual(self.pixel(template), (255, 255, 255))


class ShapeChoiceTests(_TemplateCase):
    def test_a_title_placeholder_beats_a_larger_plain_box(self) -> None:
        prs = new_presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[5])  # Title Only
        slide.shapes.title.text = "제목"
        add_box(slide, "큰 상자", left=0.5, top=3.0, width=11.0, height=3.5, text="큰 상자")
        template = self.path("title-placeholder")
        prs.save(str(template))

        profile = get_template_profile(template, slide_index=1)

        self.assertTrue(profile.selected_shape_name.startswith("Title"))
        self.assertTrue(profile.shape_detected)

    def test_a_lyric_named_box_beats_a_larger_box(self) -> None:
        prs = new_presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        add_background_rect(slide)
        add_box(slide, "가사 상자", left=1, top=2, width=4, height=1.5, text="가사")
        add_box(slide, "TextBox 9", left=0.5, top=4.0, width=9.0, height=2.6, text="큰 상자")
        template = self.path("lyric-name")
        prs.save(str(template))

        profile = get_template_profile(template, slide_index=1)

        self.assertEqual(profile.selected_shape_name, "가사 상자")

    def test_a_slide_number_placeholder_is_ignored(self) -> None:
        prs = new_presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        add_background_rect(slide)
        add_box(slide, "본문", left=1, top=2, width=6, height=2.6, text="가사")
        slide.shapes._spTree.append(parse_xml(slide_number_placeholder_xml()))
        template = self.path("slide-number")
        prs.save(str(template))

        profile = get_template_profile(template, slide_index=1)

        self.assertEqual(profile.selected_shape_name, "본문")

    def test_a_near_full_bleed_background_is_not_a_lyric_box(self) -> None:
        prs = new_presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        # Drawn by hand: 120 EMU right/80 EMU down and 3,000 EMU short.
        add_background_rect(slide, dx=120, dy=80, shrink=3000)
        add_box(slide, "TextBox 1", left=1, top=2, width=6, height=1.5, text="가사")
        template = self.path("hand-drawn-bg")
        prs.save(str(template))

        profile = get_template_profile(template, slide_index=1)

        self.assertEqual(profile.selected_shape_name, "TextBox 1")


class GroupedShapeTests(_TemplateCase):
    def test_a_grouped_text_box_is_found_and_sized_in_slide_space(self) -> None:
        prs = new_presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        add_background_rect(slide)
        group = slide.shapes.add_group_shape()
        inner = group.shapes.add_textbox(
            Emu(1_000_000), Emu(1_000_000), Emu(2_000_000), Emu(750_000)
        )
        inner.name = "Title A"
        # The group doubles everything it contains (child 4×3 → slide 8×6).
        set_group_frame(
            group,
            off=(0, 0),
            ext=(8_000_000, 6_000_000),
            child_off=(0, 0),
            child_ext=(4_000_000, 3_000_000),
        )
        template = self.path("group")
        prs.save(str(template))

        profile = get_template_profile(template, slide_index=1)

        self.assertEqual(profile.selected_shape_name, "Title A")
        # 750,000 EMU × 2 = 1,500,000 EMU = 118 pt → 50 pt after the 0.42
        # estimate. Unscaled (child space) it would be 59 pt → 25 pt, so the
        # number proves the group transform was applied.
        self.assertEqual(profile.styles[0].sz, 5000)
        self.assertTrue(profile.styles[0].synthesized)

    def test_a_grouped_text_box_receives_the_lyrics(self) -> None:
        prs = new_presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        add_background_rect(slide)
        group = slide.shapes.add_group_shape()
        inner = group.shapes.add_textbox(
            Emu(1_000_000), Emu(1_000_000), Emu(2_000_000), Emu(750_000)
        )
        inner.name = "Title A"
        set_group_frame(
            group,
            off=(0, 0),
            ext=(8_000_000, 6_000_000),
            child_off=(0, 0),
            child_ext=(4_000_000, 3_000_000),
        )
        template = self.path("group-build")
        prs.save(str(template))

        out = self.tmp / "group-out.pptx"
        build_presentation([("첫째 줄", "둘째 줄")], template, out, slide_index=1)

        produced = Presentation(str(out))
        group_out = produced.slides[0].shapes[0]
        if group_out.shape_type is not None and hasattr(group_out, "shapes"):
            target = group_out.shapes[0]
        else:  # pragma: no cover - the background rect is first
            target = [s for s in produced.slides[0].shapes if hasattr(s, "shapes")][0].shapes[0]
        self.assertEqual(
            [p.text for p in target.text_frame.paragraphs], ["첫째 줄", "둘째 줄"]
        )


class UnsupportedContainerTests(_TemplateCase):
    def test_a_table_only_slide_names_the_table(self) -> None:
        prs = new_presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        table = slide.shapes.add_table(
            1, 1, Inches(1), Inches(1), Inches(4), Inches(1.5)
        ).table
        table.cell(0, 0).text = "가사 자리"
        template = self.path("table")
        prs.save(str(template))

        with self.assertRaises(TemplateStructureError) as caught:
            get_template_profile(template, slide_index=1)

        message = str(caught.exception)
        self.assertIn("표", message)
        self.assertIn("LYRICS2PPTX_TITLE_SHAPE", message)


class SynthesizedContrastTests(_TemplateCase):
    def test_a_dark_background_gets_white_lyrics(self) -> None:
        prs = new_presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        add_background_rect(slide)  # 0x112233
        add_box(slide, "Title A", left=1, top=2, width=8, height=1.5)
        template = self.path("dark")
        prs.save(str(template))

        run = self.synthesized_run(template, self.tmp / "dark-out.pptx")

        self.assertEqual(run.font.color.rgb, RGBColor(0xFF, 0xFF, 0xFF))

    def test_a_light_background_gets_black_lyrics(self) -> None:
        prs = new_presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        add_box(slide, "Title A", left=1, top=2, width=8, height=1.5)
        template = self.path("light")
        prs.save(str(template))

        run = self.synthesized_run(template, self.tmp / "light-out.pptx")

        self.assertEqual(run.font.color.rgb, RGBColor(0, 0, 0))

    def test_synthesis_uses_the_theme_east_asian_font(self) -> None:
        prs = new_presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        add_box(slide, "Title A", left=1, top=2, width=8, height=1.5)
        template = self.path("theme-font")
        prs.save(str(template))
        inject_theme_font(template, "나눔고딕")
        clear_profile_cache()

        run = self.synthesized_run(template, self.tmp / "font-out.pptx")

        self.assertEqual(run.font.name, "나눔고딕")


if __name__ == "__main__":
    unittest.main()
