"""Integration + boundary tests for ``pptx/builder.py`` (single-slide mode).

Covers the revised contract from the user's correction:

* every output slide is cloned from the **same** template slide
  (``profile.selected_slide`` / ``BuildResult.slide_index``);
* the default background is template slide 19;
* an out-of-range ``slide_index`` raises ``ConfigError`` (never clamped);
* orphan pruning: only the selected background's media remains, the unused
  original slides/notes are gone, no template lyrics text survives, and the
  file stays well under 10 MB.

Real builds use the read-only ``template.pptx``; every produced file is written
to a temporary directory and reopened with python-pptx.
"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from pptx import Presentation
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml.ns import qn

from lyrics2pptx import config
from lyrics2pptx.errors import ConfigError, EmptyLyricsError
from lyrics2pptx.pptx.builder import build_plan, build_presentation
from lyrics2pptx.pptx.clone import RT_NOTES_SLIDE
from lyrics2pptx.pptx.template_reader import (
    get_template_profile,
    find_title_shape,
)
from lyrics2pptx.text.split import split_pairs

TEMPLATE = Path(__file__).resolve().parents[1] / "template.pptx"

# The template's own lyrics text — none of it may survive into an output file.
TEMPLATE_LYRICS = ("생명샘", "담장 너머로", "손잡고 가세")
TEN_MEGABYTES = 10 * 1024 * 1024


def media_count(path: Path) -> int:
    with zipfile.ZipFile(path) as archive:
        return sum(1 for name in archive.namelist() if name.startswith("ppt/media/"))


def slide_part_count(path: Path) -> int:
    with zipfile.ZipFile(path) as archive:
        return sum(
            1
            for name in archive.namelist()
            if name.startswith("ppt/slides/slide") and name.endswith(".xml")
        )


def notes_part_count(path: Path) -> int:
    with zipfile.ZipFile(path) as archive:
        return sum(1 for name in archive.namelist() if name.startswith("ppt/notesSlides/"))


def background_image_targets(path: Path) -> set[str]:
    """Return the image parts referenced by every slide (all should be identical)."""
    presentation = Presentation(str(path))
    targets: set[str] = set()
    for slide in presentation.slides:
        for rel in slide.part.rels.values():
            if rel.reltype == RT.IMAGE and not rel.is_external:
                targets.add(str(rel.target_part.partname))
    return targets


def slide_member_targets(path: Path) -> list[str]:
    """Return the slide part targets referenced by ``<p:sldIdLst>``, in order."""
    from lxml import etree

    p_ns = "http://schemas.openxmlformats.org/presentationml/2006/main"
    r_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"

    with zipfile.ZipFile(path) as archive:
        presentation = etree.fromstring(archive.read("ppt/presentation.xml"))
        rels = etree.fromstring(archive.read("ppt/_rels/presentation.xml.rels"))
    id_to_target = {
        rel.get("Id"): rel.get("Target")
        for rel in rels.findall(f"{{{rel_ns}}}Relationship")
    }
    targets: list[str] = []
    for sld_id in presentation.iter(f"{{{p_ns}}}sldId"):
        targets.append(id_to_target[sld_id.get(f"{{{r_ns}}}id")])
    return targets


def slide_xml(path: Path, index: int) -> str:
    """Return the raw XML of the ``index``-th slide in ``sldIdLst`` order."""
    target = slide_member_targets(path)[index]
    with zipfile.ZipFile(path) as archive:
        return archive.read("ppt/" + target.lstrip("/")).decode("utf-8")


def paragraph_texts(path: Path) -> list[list[str]]:
    presentation = Presentation(str(path))
    texts: list[list[str]] = []
    for slide in presentation.slides:
        shape = find_title_shape(slide)
        texts.append([p.text for p in shape.text_frame.paragraphs])
    return texts


def slide_reltypes(path: Path) -> list[set[str]]:
    presentation = Presentation(str(path))
    return [{rel.reltype for rel in slide.part.rels.values()} for slide in presentation.slides]


def contains_template_lyrics(path: Path) -> bool:
    """True when any template lyrics string appears anywhere in the package."""
    needles = [needle.encode("utf-8") for needle in TEMPLATE_LYRICS]
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            blob = archive.read(name)
            if any(needle in blob for needle in needles):
                return True
    return False


class PlanBoundaryTests(unittest.TestCase):
    """The single-background rule, exercised without touching the disk."""

    @classmethod
    def setUpClass(cls) -> None:
        if not TEMPLATE.is_file():
            raise unittest.SkipTest(f"template not found: {TEMPLATE}")
        cls.profile = get_template_profile(TEMPLATE)

    @staticmethod
    def _pairs(line_count: int) -> list[tuple[str, str | None]]:
        return split_pairs([f"line{k:02d}" for k in range(line_count)])

    def test_slide_count_equals_ceil_of_half_the_line_count(self) -> None:
        expected = {0: 0, 1: 1, 2: 1, 3: 2, 20: 10, 21: 11, 40: 20, 41: 21, 42: 21}
        for line_count, slides in expected.items():
            with self.subTest(lines=line_count):
                self.assertEqual(len(self._pairs(line_count)), slides)

    def test_every_slide_uses_the_same_selected_background(self) -> None:
        for line_count in (1, 2, 3, 20, 21, 40, 41, 42):
            with self.subTest(lines=line_count):
                specs = build_plan(self._pairs(line_count), self.profile)
                indices = {spec.style.slide_index for spec in specs}
                self.assertEqual(indices, {self.profile.selected_slide})

    def test_default_selected_slide_is_nineteen(self) -> None:
        # slide19 (1-based) is the only template slide with a two-line sample.
        self.assertEqual(config.SLIDE_INDEX, 19)
        self.assertEqual(self.profile.selected_slide, 18)

    def test_style_comes_from_the_selected_template_slide(self) -> None:
        specs = build_plan(self._pairs(42), self.profile)
        style = self.profile.styles[self.profile.selected_slide]
        for spec in specs:
            self.assertIs(spec.style, style)

    def test_empty_plan_is_returned_for_no_pairs(self) -> None:
        self.assertEqual(build_plan([], self.profile), [])

    def test_out_of_range_slide_index_raises_config_error(self) -> None:
        for bad in (0, 21, -1, 999):
            with self.subTest(slide=bad):
                with self.assertRaises(ConfigError):
                    get_template_profile(TEMPLATE, slide_index=bad)

    def test_slide_index_selects_the_background(self) -> None:
        profile = get_template_profile(TEMPLATE, slide_index=5)
        specs = build_plan(self._pairs(4), profile)
        self.assertEqual({spec.style.slide_index for spec in specs}, {4})


class BuilderErrorTests(unittest.TestCase):
    def test_zero_lines_raise_empty_lyrics_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "empty.pptx"
            with self.assertRaises(EmptyLyricsError):
                build_presentation([], TEMPLATE, out)
            self.assertFalse(out.exists())

    def test_unknown_strategy_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bad.pptx"
            with self.assertRaises(ConfigError):
                build_presentation([("a", "b")], TEMPLATE, out, strategy="banana")
            self.assertFalse(out.exists())

    def test_missing_template_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "x.pptx"
            with self.assertRaises(ConfigError):
                build_presentation([("a", "b")], Path(tmp) / "nope.pptx", out)

    def test_out_of_range_slide_index_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bad.pptx"
            with self.assertRaises(ConfigError):
                build_presentation([("a", "b")], TEMPLATE, out, slide_index=21)
            self.assertFalse(out.exists())

    def test_atomic_replace_leaves_no_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "ok.pptx"
            build_presentation([("a", "b")], TEMPLATE, out)
            leftovers = [p.name for p in Path(tmp).iterdir() if p.name.startswith(".tmp-")]
            self.assertEqual(leftovers, [])


class BuilderIntegrationTests(unittest.TestCase):
    """Real builds for the boundary line counts, each reopened afterwards."""

    CASES = {"42": 42, "40": 40, "3": 3}

    @classmethod
    def setUpClass(cls) -> None:
        if not TEMPLATE.is_file():
            raise unittest.SkipTest(f"template not found: {TEMPLATE}")
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.built: dict[str, dict] = {}
        for key, line_count in cls.CASES.items():
            lines = [f"line{k:02d}" for k in range(line_count)]
            pairs = split_pairs(lines)
            out = root / f"{key}.pptx"
            result = build_presentation(pairs, TEMPLATE, out, strategy="auto")
            cls.built[key] = {"lines": lines, "pairs": pairs, "out": out, "result": result}

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_42_lines_produce_21_slides_on_the_default_background(self) -> None:
        result = self.built["42"]["result"]
        self.assertEqual(result.slide_count, 21)
        self.assertEqual(result.slide_index, 19)
        self.assertEqual(result.dropped_notes_count, 21)

    def test_40_lines_produce_20_slides(self) -> None:
        result = self.built["40"]["result"]
        self.assertEqual(result.slide_count, 20)
        self.assertEqual(result.slide_index, 19)
        self.assertEqual(result.dropped_notes_count, 20)

    def test_3_lines_produce_2_slides_with_a_one_line_tail(self) -> None:
        result = self.built["3"]["result"]
        self.assertEqual(result.slide_count, 2)

    def test_output_reopens_and_lists_exactly_the_built_slides(self) -> None:
        for key, case in self.built.items():
            with self.subTest(case=key):
                presentation = Presentation(str(case["out"]))
                self.assertEqual(len(presentation.slides), len(case["pairs"]))

    def test_each_slide_carries_its_two_lines(self) -> None:
        case = self.built["42"]
        texts = paragraph_texts(case["out"])
        self.assertEqual(len(texts), 21)
        for index, (first, second) in enumerate(case["pairs"]):
            expected = [first] if second is None else [first, second]
            self.assertEqual(texts[index], expected)

    def test_the_odd_tail_slide_carries_exactly_one_line(self) -> None:
        texts = paragraph_texts(self.built["3"]["out"])
        self.assertEqual(texts[-1], ["line02"])

    def test_every_slide_uses_the_same_background_image(self) -> None:
        result = self.built["42"]["result"]
        for key, case in self.built.items():
            with self.subTest(case=key):
                targets = background_image_targets(case["out"])
                self.assertEqual(len(targets), 1, targets)
        # And it is the image the selected slide (template slide 19) uses.
        template = Presentation(str(TEMPLATE))
        expected = {
            str(rel.target_part.partname)
            for rel in template.slides[result.slide_index - 1].part.rels.values()
            if rel.reltype == RT.IMAGE
        }
        self.assertEqual(background_image_targets(self.built["42"]["out"]), expected)

    def test_slide_part_count_equals_the_output_slide_count(self) -> None:
        for key, case in self.built.items():
            with self.subTest(case=key):
                self.assertEqual(slide_part_count(case["out"]), len(case["pairs"]))

    def test_only_the_selected_background_media_remains(self) -> None:
        for key, case in self.built.items():
            with self.subTest(case=key):
                self.assertEqual(media_count(case["out"]), 1)

    def test_notes_slides_are_removed(self) -> None:
        for key, case in self.built.items():
            with self.subTest(case=key):
                self.assertEqual(notes_part_count(case["out"]), 0)
                for reltypes in slide_reltypes(case["out"]):
                    self.assertNotIn(RT_NOTES_SLIDE, reltypes)

    def test_template_lyrics_text_is_nowhere_in_the_output(self) -> None:
        for key, case in self.built.items():
            with self.subTest(case=key):
                self.assertFalse(contains_template_lyrics(case["out"]))

    def test_output_stays_below_ten_megabytes(self) -> None:
        for key, case in self.built.items():
            with self.subTest(case=key):
                self.assertLess(case["out"].stat().st_size, TEN_MEGABYTES)

    def test_every_slide_keeps_a_background_reference(self) -> None:
        case = self.built["42"]
        for index in range(case["result"].slide_count):
            with self.subTest(slide=index):
                xml = slide_xml(case["out"], index)
                self.assertTrue(
                    "<p:bg" in xml or "r:embed" in xml,
                    f"slide {index} lost its background reference",
                )

    def test_sld_id_list_length_equals_the_slide_count(self) -> None:
        case = self.built["42"]
        presentation = Presentation(str(case["out"]))
        sld_id_lst = presentation.part._element.find(qn("p:sldIdLst"))
        self.assertIsNotNone(sld_id_lst)
        self.assertEqual(len(sld_id_lst.findall(qn("p:sldId"))), 21)

    def test_default_output_path_honours_config_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "OUTPUT_DIR", Path(tmp)):
                result = build_presentation([("a", "b")], TEMPLATE, None)
            self.assertEqual(result.output_path.parent, Path(tmp))
            self.assertTrue(result.output_path.is_file())
            self.assertEqual(result.output_path.name, config.DEFAULT_FILENAME)


class SlideSelectionBuildTests(unittest.TestCase):
    """``slide_index`` drives which single background every slide copies."""

    @classmethod
    def setUpClass(cls) -> None:
        if not TEMPLATE.is_file():
            raise unittest.SkipTest(f"template not found: {TEMPLATE}")

    def test_slide_1_and_slide_20_build_on_that_background(self) -> None:
        pairs = split_pairs([f"line{k:02d}" for k in range(6)])
        for slide in (1, 20):
            with self.subTest(slide=slide), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / f"slide{slide}.pptx"
                result = build_presentation(pairs, TEMPLATE, out, slide_index=slide)
                self.assertEqual(result.slide_index, slide)
                self.assertEqual(len(background_image_targets(out)), 1)
                self.assertEqual(slide_part_count(out), len(pairs))
                self.assertEqual(media_count(out), 1)


if __name__ == "__main__":
    unittest.main()
