"""Unit tests for background-image detection in ``pptx/template_reader``.

The real-template behaviour is pinned by ``test_webapp.SlidesEndpointTests``;
these tests cover the XML shapes that heuristic accepts or rejects. The lookup
moved from ``thumbnails`` to ``template_reader`` in ADR-035, when the background
became inherited (slide → layout → master).
"""

from __future__ import annotations

import unittest

from pptx.oxml import parse_xml
from pptx.oxml.ns import nsdecls

from lyrics2pptx.pptx.template_reader import background_image_reference

SLIDE_WIDTH = 12_192_000
SLIDE_HEIGHT = 6_858_000


def _slide(shapes: str = "", background: str = "") -> object:
    return parse_xml(
        f"<p:sld {nsdecls('p', 'a', 'r')}>"
        f"<p:cSld>{background}<p:spTree>{shapes}</p:spTree></p:cSld>"
        f"</p:sld>"
    )


def _shape(x: int, y: int, cx: int, cy: int, fill: str, name: str = "shape") -> str:
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="2" name="{name}"/>'
        f"<p:cNvSpPr/><p:nvPr/></p:nvSpPr>"
        f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/>'
        f'<a:ext cx="{cx}" cy="{cy}"/></a:xfrm>{fill}</p:spPr>'
        f"<p:txBody><a:bodyPr/><a:p/></p:txBody></p:sp>"
    )


def _blip_fill(reference: str) -> str:
    return f'<a:blipFill><a:blip r:embed="{reference}"/></a:blipFill>'


def _solid_fill() -> str:
    return '<a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill>'


def _picture(reference: str) -> str:
    return (
        f'<p:pic><p:nvPicPr><p:cNvPr id="3" name="picture"/>'
        f"<p:cNvPicPr/><p:nvPr/></p:nvPicPr>"
        f'<p:blipFill><a:blip r:embed="{reference}"/>'
        f"<a:stretch><a:fillRect/></a:stretch></p:blipFill>"
        f"<p:spPr/></p:pic>"
    )


class BackgroundImageReferenceTests(unittest.TestCase):
    def detect(self, slide) -> str | None:
        return background_image_reference(slide, SLIDE_WIDTH, SLIDE_HEIGHT)

    def test_background_blip_fill_wins(self) -> None:
        background = f"<p:bg><p:bgPr>{_blip_fill('rId8')}<a:effectLst/></p:bgPr></p:bg>"
        slide = _slide(
            shapes=_shape(0, 0, SLIDE_WIDTH, SLIDE_HEIGHT, _blip_fill("rId9")),
            background=background,
        )
        self.assertEqual(self.detect(slide), "rId8")

    def test_full_bleed_picture_is_found(self) -> None:
        slide = _slide(shapes=_picture("rId7"))
        self.assertEqual(self.detect(slide), "rId7")

    def test_full_bleed_picture_filled_shape_is_found(self) -> None:
        slide = _slide(shapes=_shape(0, 0, SLIDE_WIDTH, SLIDE_HEIGHT, _blip_fill("rId3")))
        self.assertEqual(self.detect(slide), "rId3")

    def test_shape_that_does_not_cover_the_slide_is_ignored(self) -> None:
        slide = _slide(
            shapes=_shape(0, 0, SLIDE_WIDTH // 2, SLIDE_HEIGHT, _blip_fill("rId3"))
        )
        self.assertIsNone(self.detect(slide))

    def test_a_hand_drawn_background_within_the_tolerance_is_found(self) -> None:
        # ADR-035: a background drawn by hand sits a few EMU off the origin and
        # may be a hair smaller than the slide. That is still a background.
        slide = _slide(
            shapes=_shape(120, 80, SLIDE_WIDTH - 3000, SLIDE_HEIGHT - 2000, _blip_fill("rId3"))
        )
        self.assertEqual(self.detect(slide), "rId3")

    def test_a_clearly_inset_shape_is_ignored(self) -> None:
        slide = _slide(
            shapes=_shape(
                int(SLIDE_WIDTH * 0.05),  # 5% in — well past the 1% tolerance
                0,
                SLIDE_WIDTH,
                SLIDE_HEIGHT,
                _blip_fill("rId3"),
            )
        )
        self.assertIsNone(self.detect(slide))

    def test_the_largest_picture_wins_when_none_is_full_bleed(self) -> None:
        small = _picture("rId1")
        large = _picture("rId2")
        # Both are pictures, so the size comes from the spPr transform.
        small = small.replace("<p:spPr/>", "<p:spPr><a:xfrm><a:off x='0' y='0'/><a:ext cx='100' cy='100'/></a:xfrm></p:spPr>")
        large = large.replace("<p:spPr/>", "<p:spPr><a:xfrm><a:off x='0' y='0'/><a:ext cx='900' cy='900'/></a:xfrm></p:spPr>")
        self.assertEqual(self.detect(_slide(shapes=small + large)), "rId2")

    def test_slide_without_any_image_returns_none(self) -> None:
        slide = _slide(shapes=_shape(0, 0, SLIDE_WIDTH, SLIDE_HEIGHT, _solid_fill()))
        self.assertIsNone(self.detect(slide))


if __name__ == "__main__":
    unittest.main()
