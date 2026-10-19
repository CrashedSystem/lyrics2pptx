"""Unit tests for background-image detection in ``pptx/thumbnails``.

The real-template behaviour is pinned by ``test_webapp.SlidesEndpointTests``;
these tests cover the XML shapes that heuristic accepts or rejects.
"""

from __future__ import annotations

import unittest

from pptx.oxml import parse_xml
from pptx.oxml.ns import nsdecls

from lyrics2pptx.pptx.thumbnails import _first_embed_reference

SLIDE_WIDTH = 12_192_000
SLIDE_HEIGHT = 6_858_000


def _slide(shapes: str = "", background: str = "") -> object:
    return parse_xml(
        f"<p:sld {nsdecls('p', 'a', 'r')}>"
        f"<p:cSld>{background}<p:spTree>{shapes}</p:spTree></p:cSld>"
        f"</p:sld>"
    )


def _shape(x: int, y: int, cx: int, cy: int, fill: str) -> str:
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="2" name="shape"/>'
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
        f"<p:blipFill><a:blip r:embed=\"{reference}\"/>"
        f"<a:stretch><a:fillRect/></a:stretch></p:blipFill>"
        f"<p:spPr/></p:pic>"
    )


class FirstEmbedReferenceTests(unittest.TestCase):
    def detect(self, slide) -> str | None:
        return _first_embed_reference(slide, SLIDE_WIDTH, SLIDE_HEIGHT)

    def test_background_blip_fill_wins(self) -> None:
        background = (
            f"<p:bg><p:bgPr>{_blip_fill('rId8')}"
            f"<a:effectLst/></p:bgPr></p:bg>"
        )
        slide = _slide(
            shapes=_shape(
                0, 0, SLIDE_WIDTH, SLIDE_HEIGHT, _blip_fill("rId9")
            ),
            background=background,
        )
        self.assertEqual(self.detect(slide), "rId8")

    def test_full_bleed_picture_is_found(self) -> None:
        slide = _slide(shapes=_picture("rId7"))
        self.assertEqual(self.detect(slide), "rId7")

    def test_full_bleed_picture_filled_shape_is_found(self) -> None:
        slide = _slide(
            shapes=_shape(
                0, 0, SLIDE_WIDTH, SLIDE_HEIGHT, _blip_fill("rId3")
            )
        )
        self.assertEqual(self.detect(slide), "rId3")

    def test_shape_that_does_not_cover_the_slide_is_ignored(self) -> None:
        slide = _slide(
            shapes=_shape(
                0,
                0,
                SLIDE_WIDTH // 2,
                SLIDE_HEIGHT,
                _blip_fill("rId3"),
            )
        )
        self.assertIsNone(self.detect(slide))

    def test_shape_offset_from_the_origin_is_ignored(self) -> None:
        slide = _slide(
            shapes=_shape(
                10, 0, SLIDE_WIDTH, SLIDE_HEIGHT, _blip_fill("rId3")
            )
        )
        self.assertIsNone(self.detect(slide))

    def test_slide_without_any_image_returns_none(self) -> None:
        slide = _slide(
            shapes=_shape(
                0, 0, SLIDE_WIDTH, SLIDE_HEIGHT, _solid_fill()
            )
        )
        self.assertIsNone(self.detect(slide))


if __name__ == "__main__":
    unittest.main()
