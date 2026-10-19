"""Console encoding safety: the cp949 crash regression (docs/arch/05 §3).

A Korean Windows console defaults to cp949 (ks_c_5601-1987), which cannot
represent characters such as em dash (U+2014), the horizontal ellipsis
(U+2026), or the middle dot (U+00B7). When such a character reaches
stdout/stderr the interpreter raises ``UnicodeEncodeError`` and the user sees a
stack trace instead of the ``--help`` text or the error message.

That crash cannot be reproduced on a GitHub runner, because runners use a UTF-8
locale. These tests therefore assert the *locale-independent* property instead:
every console-facing string is encodable as cp949, and a real ``--help`` run
succeeds even when the console streams are cp949.

The one end-to-end test that reads the child's raw bytes asks the child what its
console encoding actually is (``sys.stdout.encoding``) instead of assuming
cp949: on a Korean Windows console that is cp949 and the hardening rule is
asserted, on a UTF-8 locale (including the ubuntu runner) the same bytes must
still decode cleanly and the hardening must not have touched them. Nothing here
depends on the host console actually being cp949, so the suite is green on
ubuntu too.

Run standalone::

    python -m unittest tests.test_cli_encoding -v
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from lyrics2pptx import cli, config, errors, pipeline

REPO_ROOT = Path(__file__).resolve().parents[1]

# cp949 is the Korean Windows default; euc-kr/ks_c_5601-1987 are aliases.
CONSOLE_ENCODING = "cp949"

# 코드페이지 949 계열 이름들. 자식 프로세스가 보고한 인코딩이 이 중 하나면
# "한국어 Windows 콘솔" 경로이며, 하드닝이 스트림을 변형한다.
_CP949_ALIASES = {
    "cp949",
    "ms949",
    "uhc",
    "euc-kr",
    "euckr",
    "ks_c_5601-1987",
    "ksc5601",
}


def assert_cp949_encodable(test: unittest.TestCase, text: object, label: str) -> None:
    """Assert ``text`` survives a strict cp949 encode (the console path)."""
    test.assertIsInstance(text, str, f"{label}: 문자열이 아님 ({type(text).__name__})")
    try:
        text.encode(CONSOLE_ENCODING)
    except UnicodeEncodeError as exc:
        offenders = sorted(
            {
                f"U+{ord(ch):04X} {ch!r}"
                for ch in text
                if not _char_encodable(ch)
            }
        )
        test.fail(
            f"{label}: cp949로 인코딩할 수 없는 문자가 있습니다 {offenders} ({exc})"
        )


def _char_encodable(char: str) -> bool:
    try:
        char.encode(CONSOLE_ENCODING)
    except UnicodeEncodeError:
        return False
    return True


def _module_strings(module: object):
    """Yield ``(name, value)`` for every module-level string-ish constant.

    Walks the real module namespace so a newly added constant is covered
    automatically instead of being missed by a hardcoded list.
    """
    for name, value in vars(module).items():
        if name.startswith("__"):
            continue
        if isinstance(value, str):
            yield name, value
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                if isinstance(item, str):
                    yield f"{name}[]", item
        elif isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str):
                    yield f"{name}<key>", key
                if isinstance(item, str):
                    yield f"{name}<value>", item


class ParserHelpEncodingTests(unittest.TestCase):
    """Every string argparse can emit for ``--help`` must be cp949-safe."""

    def setUp(self) -> None:
        self.parser = cli.build_parser()

    def test_description_and_epilog_are_cp949_encodable(self) -> None:
        # Both are optional in argparse; only check them when present.
        for label, value in (
            ("parser.description", self.parser.description),
            ("parser.epilog", self.parser.epilog),
            ("parser.prog", self.parser.prog),
        ):
            if value is None:
                continue
            assert_cp949_encodable(self, value, label)

    def test_every_action_help_metavar_dest_is_cp949_encodable(self) -> None:
        # Iterate the real parser (no hardcoded option list) so a future flag is
        # covered the moment it is added.
        for action in self.parser._actions:
            for attr in ("help", "metavar", "dest"):
                value = getattr(action, attr, None)
                if value is None:
                    continue
                assert_cp949_encodable(
                    self, value, f"action[{action.dest}].{attr}"
                )
            for choice in getattr(action, "choices", None) or ():
                assert_cp949_encodable(
                    self, str(choice), f"action[{action.dest}].choices"
                )

    def test_format_help_is_cp949_encodable(self) -> None:
        rendered = self.parser.format_help()
        assert_cp949_encodable(self, rendered, "format_help()")
        rendered.encode(CONSOLE_ENCODING)  # the exact call that used to crash


class UserFacingMessageEncodingTests(unittest.TestCase):
    """The error/doctor wording must be cp949-safe on every path."""

    def test_message_templates_are_cp949_encodable(self) -> None:
        # Walk the real table so a new code is covered without edits here.
        self.assertTrue(pipeline._MESSAGE_TEMPLATE, "메시지 템플릿이 비어 있음")
        for code, template in pipeline._MESSAGE_TEMPLATE.items():
            assert_cp949_encodable(self, template, f"_MESSAGE_TEMPLATE[{code}]")

    def test_describe_error_output_is_cp949_encodable(self) -> None:
        # Every mapped exception type, discovered from the mapping itself.
        self.assertTrue(pipeline._MESSAGE_TABLE, "메시지 테이블이 비어 있음")
        sample_detail = "상세: 인코딩 검사"
        for exc_type in list(pipeline._MESSAGE_TABLE) + [ValueError]:
            with self.subTest(exc_type=exc_type.__name__):
                exc = exc_type(sample_detail)
                facing = pipeline.describe_error(exc)
                assert_cp949_encodable(
                    self, facing.message, f"describe_error({exc_type.__name__})"
                )
                assert_cp949_encodable(self, facing.code, "UserFacingError.code")

    def test_describe_error_without_detail_is_cp949_encodable(self) -> None:
        # ``{detail}`` fallback path (empty str(exc)).
        for exc_type in pipeline._MESSAGE_TABLE:
            with self.subTest(exc_type=exc_type.__name__):
                facing = pipeline.describe_error(exc_type())
                assert_cp949_encodable(
                    self, facing.message, f"describe_error({exc_type.__name__}) empty"
                )

    def test_errors_module_has_no_non_cp949_text(self) -> None:
        for name, value in _module_strings(errors):
            assert_cp949_encodable(self, value, f"errors.{name}")

    def test_config_user_facing_constants_are_cp949_encodable(self) -> None:
        for name, value in _module_strings(config):
            assert_cp949_encodable(self, value, f"config.{name}")

    def test_cli_error_wording_is_cp949_encodable(self) -> None:
        for name, value in _module_strings(cli):
            assert_cp949_encodable(self, value, f"cli.{name}")

    def test_doctor_report_is_cp949_encodable(self) -> None:
        report = pipeline.doctor_report()
        for key, value in report.items():
            assert_cp949_encodable(self, key, f"doctor[{key}].key")
            assert_cp949_encodable(self, f"{key}: {value}", f"doctor[{key}]")


class HardenedConsoleTests(unittest.TestCase):
    """``main`` must survive a cp949 console stream."""

    def test_help_exits_zero_through_a_cp949_stdout(self) -> None:
        buffer = io.BytesIO()
        wrapper = io.TextIOWrapper(
            buffer, encoding=CONSOLE_ENCODING, errors="strict", newline="\n"
        )
        with mock.patch.object(sys, "stdout", wrapper):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["--help"])
            code = ctx.exception.code
            wrapper.flush()
        self.assertEqual(code, 0)
        # The help text really made it out, decodable as cp949.
        text = buffer.getvalue().decode(CONSOLE_ENCODING)
        self.assertIn("가사 2줄 PPTX 생성기", text)
        self.assertNotIn("\u2014", text)

    def test_unknown_option_exits_two_through_a_cp949_stderr(self) -> None:
        out, err = io.BytesIO(), io.BytesIO()
        with mock.patch.object(
            sys, "stdout", io.TextIOWrapper(out, encoding=CONSOLE_ENCODING)
        ), mock.patch.object(
            sys, "stderr", io.TextIOWrapper(err, encoding=CONSOLE_ENCODING)
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["--definitely-not-an-option"])
            code = ctx.exception.code
        self.assertEqual(code, 2)

    def test_harden_console_keeps_a_utf8_stream_byte_identical(self) -> None:
        # A UTF-8 console must keep emitting the same bytes (only the error
        # handler changes, so valid text is unaffected).
        buffer = io.BytesIO()
        wrapper = io.TextIOWrapper(buffer, encoding="utf-8", errors="strict")
        with mock.patch.object(sys, "stdout", wrapper), mock.patch.object(
            sys, "stderr", wrapper
        ):
            cli._harden_console()
            wrapper.write("—가사…")
            wrapper.flush()
        self.assertEqual(buffer.getvalue().decode("utf-8"), "—가사…")

    def test_harden_console_tolerates_streams_without_reconfigure(self) -> None:
        # ``io.StringIO`` (used by the redirect helpers) has no reconfigure.
        with mock.patch.object(sys, "stdout", io.StringIO()), mock.patch.object(
            sys, "stderr", io.StringIO()
        ):
            cli._harden_console()  # must not raise

    def test_harden_console_pins_cp949_and_replaces(self) -> None:
        buffer = io.BytesIO()
        wrapper = io.TextIOWrapper(buffer, encoding=CONSOLE_ENCODING, errors="strict")
        with mock.patch.object(sys, "stdout", wrapper), mock.patch.object(
            sys, "stderr", wrapper
        ):
            cli._harden_console()
            wrapper.write("bad\u2014char")
            wrapper.flush()
        decoded = buffer.getvalue().decode(CONSOLE_ENCODING)
        self.assertTrue(decoded.startswith("bad"))
        self.assertTrue(decoded.endswith("char"))


class HelpSubprocessTests(unittest.TestCase):
    """End-to-end: a real process, console encoding left to the locale."""

    def _clean_env(self) -> dict[str, str]:
        env = dict(os.environ)
        # Reproduce the user's shell: no UTF-8 forcing at all.
        env.pop("PYTHONIOENCODING", None)
        env.pop("PYTHONUTF8", None)
        src = str(REPO_ROOT / "src")
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = src if not existing else os.pathsep.join([src, existing])
        return env

    def test_help_exits_zero_without_utf8_env(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "lyrics2pptx.cli", "--help"],
            cwd=str(REPO_ROOT),
            env=self._clean_env(),
            capture_output=True,
            timeout=120,
            check=False,
        )
        stderr = proc.stderr.decode("utf-8", "replace")
        self.assertEqual(
            proc.returncode, 0, f"--help 이 0 이 아님. stderr:\n{stderr}"
        )
        self.assertNotIn("Traceback", stderr)
        self.assertNotIn("UnicodeEncodeError", stderr)
        self.assertIn(b"PPTX", proc.stdout)

    def _child_stdout_encoding(self) -> str:
        """자식 프로세스의 콘솔 인코딩을 직접 물어본다(호스트 추측 금지)."""
        probe = subprocess.run(
            [sys.executable, "-c", "import sys; print(sys.stdout.encoding)"],
            cwd=str(REPO_ROOT),
            env=self._clean_env(),
            capture_output=True,
            timeout=120,
            check=True,
        )
        return probe.stdout.decode("ascii", "replace").strip()

    def test_help_stdout_is_decodable_on_the_child_console(self) -> None:
        """``--help`` 의 바이트가 **그 콘솔의 인코딩**으로 온전히 읽히는지 본다.

        한국어 Windows 콘솔이면 cp949 가 나오고, 그때만 "인코딩할 수 없는 문자를
        미리 바꿔 내보낸다"는 하드닝 규칙(em dash 부재)을 함께 단언한다.
        UTF-8 로케일(우분투 러너 포함)에서는 같은 규칙이 스트림을 건드리지 않으므로
        여기서는 "깨지지 않고 읽힌다"만 본다 — 이 단언이 실패했다면 그것은
        회귀가 아니라 "플랫폼 가정이 틀렸다"는 신호다.
        """
        encoding = self._child_stdout_encoding()
        proc = subprocess.run(
            [sys.executable, "-m", "lyrics2pptx.cli", "--help"],
            cwd=str(REPO_ROOT),
            env=self._clean_env(),
            capture_output=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(
            proc.returncode, 0, proc.stderr.decode("utf-8", "replace")
        )
        try:
            text = proc.stdout.decode(encoding)
        except UnicodeDecodeError as exc:
            self.fail(f"--help 출력이 콘솔 인코딩({encoding})으로 디코딩되지 않음: {exc}")
        self.assertIn("PPTX", text)
        if encoding.lower() in _CP949_ALIASES:
            self.assertNotIn("\u2014", text)


if __name__ == "__main__":
    unittest.main()
