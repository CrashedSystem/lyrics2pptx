"""CLI contract tests (``cli.py``, docs/arch/05 §3).

Exit codes are a published contract for shells/automation, so they are pinned
exactly: 0 success, 1 user error, 2 argparse rejection, 3 empty lyrics,
4 template/save failure. ``--json`` must keep stdout parseable, which also
proves no log line leaks into stdout.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lyrics2pptx import cli, config

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def setUpModule() -> None:  # noqa: N802 - unittest hook
    logging.disable(logging.CRITICAL)


def tearDownModule() -> None:  # noqa: N802 - unittest hook
    logging.disable(logging.NOTSET)


def run_cli(argv, *, fixture_mode: bool = False):
    """Invoke ``cli.main`` capturing stdout/stderr and the exit code."""
    stdout, stderr = io.StringIO(), io.StringIO()
    patchers = []
    if fixture_mode:
        patcher = mock.patch.object(config, "FIXTURE_DIR", FIXTURES)
        patcher.start()
        patchers.append(patcher)
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = cli.main(argv)
            except SystemExit as exc:  # argparse rejects with SystemExit(2)
                code = exc.code
    finally:
        for patcher in reversed(patchers):
            patcher.stop()
    return code, stdout.getvalue(), stderr.getvalue()


class ArgumentValidationTests(unittest.TestCase):
    def test_no_input_option_is_a_bad_request(self) -> None:
        code, out, err = run_cli([])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("설정이 올바르지 않습니다", err)

    def test_input_options_are_mutually_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "in.txt"
            path.write_text("a\nb\n", encoding="utf-8")
            code, _, err = run_cli(["--query", "x", "--input-file", str(path)])
        self.assertEqual(code, 1)
        self.assertIn("--input-file", err)

    def test_all_three_input_options_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "in.txt"
            path.write_text("a\nb\n", encoding="utf-8")
            code, _, _ = run_cli(
                ["--query", "x", "--input-file", str(path), "--lines-file", str(path)]
            )
        self.assertEqual(code, 1)

    def test_argparse_rejects_an_unknown_option_with_exit_code_two(self) -> None:
        code, _, _ = run_cli(["--definitely-not-an-option"])
        self.assertEqual(code, 2)

    def test_argparse_rejects_a_bad_strategy_choice_with_exit_code_two(self) -> None:
        code, _, _ = run_cli(["--strategy", "banana", "--query", "x"])
        self.assertEqual(code, 2)

    def test_missing_input_file_is_a_bad_request(self) -> None:
        code, _, err = run_cli(["--input-file", "D:/definitely/missing.txt"])
        self.assertEqual(code, 1)
        self.assertIn("입력 파일", err)

    def test_unknown_source_is_a_bad_request(self) -> None:
        code, _, err = run_cli(["--query", "생명샘", "--source", "nope"], fixture_mode=True)
        self.assertEqual(code, 1)
        self.assertIn("nope", err)

    def test_skip_slide_out_of_range_is_a_bad_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "in.txt"
            path.write_text("a\nb\n", encoding="utf-8")
            code, _, _ = run_cli(["--input-file", str(path), "--skip-slide", "99"])
        self.assertEqual(code, 1)


class EmptyLyricsTests(unittest.TestCase):
    def test_marker_only_input_exits_with_code_three(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "markers.txt"
            path.write_text("[Verse 1]\n[Chorus]\n(x2)\n", encoding="utf-8")
            code, out, err = run_cli(["--input-file", str(path), "-o", str(Path(tmp) / "o.pptx")])
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("유효한 줄", err)


class JsonOutputTests(unittest.TestCase):
    def test_json_without_any_input_is_a_bad_request(self) -> None:
        code, _, err = run_cli(["--json"])
        self.assertEqual(code, 1)
        self.assertIn("--query", err)

    def test_json_with_a_file_instead_of_a_query_is_a_bad_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "in.txt"
            path.write_text("a\nb\n", encoding="utf-8")
            code, _, err = run_cli(["--json", "--input-file", str(path)])
        self.assertEqual(code, 1)
        self.assertIn("--json", err)

    def test_json_is_the_only_thing_on_stdout(self) -> None:
        code, out, _ = run_cli(["--json", "--query", "생명샘"], fixture_mode=True)
        self.assertEqual(code, 0)
        payload = json.loads(out)  # any log line would break this
        self.assertEqual(payload["source"], config.DEFAULT_SOURCE)
        self.assertEqual(payload["query"], "생명샘")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["title"], "생명샘")

    def test_json_reports_not_found_with_exit_code_one(self) -> None:
        code, out, err = run_cli(["--json", "--query", "not found"], fixture_mode=True)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("검색 결과가 없습니다", err)


class DoctorTests(unittest.TestCase):
    def test_doctor_json_is_parseable_and_reports_the_template(self) -> None:
        code, out, _ = run_cli(["--doctor", "--json"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertIn("lyrics2pptx", report)
        self.assertTrue(report["template_exists"])
        self.assertEqual(report["slide_count"], 20)
        self.assertIn(report["clone_writer"], ("parts", "zip"))

    def test_doctor_plain_output_lists_keys(self) -> None:
        code, out, _ = run_cli(["--doctor"])
        self.assertEqual(code, 0)
        self.assertIn("clone_writer:", out)
        self.assertIn("sources:", out)

    def test_doctor_does_not_write_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "OUTPUT_DIR", Path(tmp)):
                code, _, _ = run_cli(["--doctor", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(list(Path(tmp).iterdir()), [])


class BuildSuccessTests(unittest.TestCase):
    def test_lines_file_build_prints_only_the_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lines_file = Path(tmp) / "lines.txt"
            lines_file.write_text("첫째 줄\n둘째 줄\n", encoding="utf-8")
            out_path = Path(tmp) / "result.pptx"
            code, out, _ = run_cli(
                ["--lines-file", str(lines_file), "-o", str(out_path), "--strategy", "parts"]
            )

            self.assertEqual(code, 0)
            self.assertTrue(out_path.is_file())
            # stdout carries the path and nothing else.
            self.assertEqual(out.strip(), str(out_path))
            self.assertNotIn("\n", out.strip())

    def test_input_file_runs_normalization_and_builds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw.txt"
            raw.write_text("[Verse 1]\n가사 한 줄\n\n또 한 줄\n(×2)\n", encoding="utf-8")
            out_path = Path(tmp) / "result.pptx"
            code, out, _ = run_cli(["--input-file", str(raw), "-o", str(out_path)])
            self.assertEqual(code, 0)
            self.assertTrue(out_path.is_file())

    def test_output_directory_is_created_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lines_file = Path(tmp) / "lines.txt"
            lines_file.write_text("가\n나\n", encoding="utf-8")
            out_path = Path(tmp) / "nested" / "deep" / "result.pptx"
            code, _, _ = run_cli(["--lines-file", str(lines_file), "-o", str(out_path)])
            self.assertEqual(code, 0)
            self.assertTrue(out_path.is_file())


if __name__ == "__main__":
    unittest.main()
