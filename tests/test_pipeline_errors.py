"""Contract tests for ``pipeline.describe_error`` and ``resolve_output_path``.

``describe_error`` is the *only* error→message/status mapping (AD-09, docs/arch
03 §4). The table below is a verbatim copy of that contract, so drift in either
the table or the entrypoints fails here.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from lyrics2pptx import config, pipeline
from lyrics2pptx.crawl.base import (
    SourceBlockedError,
    SourceNotFoundError,
    SourceUnavailableError,
)
from lyrics2pptx.errors import (
    ConfigError,
    EmptyLyricsError,
    OutputWriteError,
    TemplateStructureError,
)


class DescribeErrorTests(unittest.TestCase):
    # (exception factory, code, http_status, recoverable)
    TABLE = (
        (lambda: SourceNotFoundError("없음"), "SOURCE_NOT_FOUND", 404, True),
        (lambda: SourceBlockedError("차단"), "SOURCE_BLOCKED", 423, True),
        (lambda: SourceUnavailableError("불가"), "SOURCE_UNAVAILABLE", 502, True),
        (lambda: EmptyLyricsError("빈 가사"), "EMPTY_LYRICS", 422, False),
        (lambda: TemplateStructureError("도형 없음"), "TEMPLATE_ERROR", 500, False),
        (lambda: OutputWriteError("저장 실패"), "OUTPUT_WRITE_ERROR", 500, False),
        (lambda: ConfigError("bad"), "BAD_REQUEST", 400, False),
    )

    def test_error_mapping_table_is_covered_exactly(self) -> None:
        for factory, code, status, recoverable in self.TABLE:
            with self.subTest(code=code):
                facing = pipeline.describe_error(factory())
                self.assertEqual(facing.code, code)
                self.assertEqual(facing.http_status, status)
                self.assertIs(facing.recoverable, recoverable)

    def test_canonical_messages_are_stable(self) -> None:
        expected = {
            "SOURCE_NOT_FOUND": "검색 결과가 없습니다. '가수' 또는 '가수 - 곡명'으로 다시 시도하거나, 가사를 직접 붙여넣으세요.",
            "SOURCE_BLOCKED": "이 소스는 자동 수집이 차단되었습니다. 직접 입력을 사용하세요.",
            "SOURCE_UNAVAILABLE": "일시적으로 수집에 실패했습니다. 잠시 후 재시도하세요.",
            "EMPTY_LYRICS": "가사에서 유효한 줄을 찾지 못했습니다.",
        }
        for factory, code, _, _ in self.TABLE:
            if code not in expected:
                continue
            with self.subTest(code=code):
                self.assertEqual(pipeline.describe_error(factory()).message, expected[code])

    def test_detail_is_appended_for_codes_that_need_a_cause(self) -> None:
        self.assertIn("도형 없음", pipeline.describe_error(TemplateStructureError("도형 없음")).message)
        self.assertIn("저장 실패", pipeline.describe_error(OutputWriteError("저장 실패")).message)
        self.assertTrue(
            pipeline.describe_error(ConfigError("x=1")).message.startswith("설정이 올바르지 않습니다:")
        )

    def test_unknown_exception_maps_to_internal_without_leaking_details(self) -> None:
        secret = "C:/secret/path/config.ini"
        facing = pipeline.describe_error(ValueError(secret))
        self.assertEqual(facing.code, "INTERNAL")
        self.assertEqual(facing.http_status, 500)
        self.assertIs(facing.recoverable, False)
        self.assertNotIn(secret, facing.message)
        self.assertNotIn("ValueError", facing.message)

    def test_recoverable_errors_stay_recoverable_for_the_entrypoints(self) -> None:
        # cli appends a "paste your lyrics" hint and the web UI shows a retry
        # button exactly when recoverable is True.
        self.assertTrue(pipeline.describe_error(SourceUnavailableError("x")).recoverable)
        self.assertFalse(pipeline.describe_error(EmptyLyricsError("x")).recoverable)


class ResolveOutputPathTests(unittest.TestCase):
    def test_default_path_uses_output_dir_and_default_filename(self) -> None:
        path = pipeline.resolve_output_path(None)
        self.assertEqual(path.parent, config.OUTPUT_DIR)
        self.assertEqual(path.name, config.DEFAULT_FILENAME)

    def test_explicit_path_gains_pptx_suffix(self) -> None:
        path = pipeline.resolve_output_path(Path("D:/tmp/mysong"))
        self.assertTrue(path.name.endswith(".pptx"))

    def test_path_characters_in_the_name_are_sanitized(self) -> None:
        path = pipeline.resolve_output_path(Path("D:/tmp/song:1.pptx"))
        for bad in '/\\:*?"<>|':
            self.assertNotIn(bad, path.name)

    def test_title_is_sanitized_and_suffixed(self) -> None:
        path = pipeline.resolve_output_path(None, title="가사/제목")
        self.assertEqual(path.parent, config.OUTPUT_DIR)
        self.assertNotIn("/", path.name)
        self.assertTrue(path.name.endswith(".pptx"))

    def test_relative_explicit_path_resolves_against_the_working_directory(self) -> None:
        path = pipeline.resolve_output_path(Path("sub/out.pptx"))
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.name, "out.pptx")


if __name__ == "__main__":
    unittest.main()
