"""Unit tests for ``config`` — the single environment access point (rule P9).

Only the ``_env_*`` parsers are exercised through ``os.environ`` (with
``patch.dict``); module-level constants are asserted for their documented
defaults. Tests are independent of any ``LYRICS2PPTX_*`` variable the runner
happens to export, except ``LYRICS2PPTX_FIXTURE_DIR`` which is never asserted.
"""

from __future__ import annotations

import logging
import os
import unittest
from pathlib import Path
from unittest import mock

from lyrics2pptx import config
from lyrics2pptx.errors import ConfigError


class DefaultsTests(unittest.TestCase):
    def test_root_is_the_repository_directory(self) -> None:
        self.assertTrue((config.ROOT / "src" / "lyrics2pptx").is_dir())

    def test_template_path_defaults_next_to_the_repository_root(self) -> None:
        self.assertEqual(config.TEMPLATE_PATH, config.ROOT / "template.pptx")

    def test_output_dir_defaults_under_the_repository_root(self) -> None:
        self.assertEqual(config.OUTPUT_DIR, config.ROOT / "output")

    def test_web_dir_defaults_under_the_repository_root(self) -> None:
        self.assertEqual(config.WEB_DIR, config.ROOT / "web")

    def test_scalar_defaults_match_the_environment_manifest(self) -> None:
        self.assertEqual(config.DEFAULT_FILENAME, "가사.pptx")
        # ADR-018: the default is the fallback chain, not a single adapter.
        self.assertEqual(config.DEFAULT_SOURCE, "auto")
        self.assertEqual(config.CHAIN_MEMBERS, ("lrclib", "blog"))
        self.assertEqual(config.SEARCH_BACKEND, "ddg")
        self.assertEqual(config.BLOG_MAX_PAGES, 5)
        self.assertEqual(config.TITLE_SHAPE_NAME, "제목 1")
        self.assertEqual(config.BIND_HOST, "127.0.0.1")
        self.assertEqual(config.BIND_PORT, 8765)
        self.assertEqual(config.HTTP_DELAY, 2.0)
        self.assertEqual(config.HTTP_TIMEOUT, 10.0)
        # ADR-030: lrclib's whole call is capped to 10 s by default.
        self.assertEqual(config.LRCLIB_TIMEOUT, 10.0)
        self.assertEqual(config.MAX_RETRIES, 3)

    def test_clone_strategies_are_exactly_auto_parts_zip(self) -> None:
        self.assertEqual(config.CLONE_STRATEGIES, ("auto", "parts", "zip"))
        self.assertIn(config.CLONE_STRATEGY, config.CLONE_STRATEGIES)

    def test_slide_index_defaults_to_nineteen(self) -> None:
        # The single-slide default: template slide 19, the two-line sample.
        self.assertEqual(config.SLIDE_INDEX, 19)
        self.assertIn("SLIDE_INDEX", config.__all__)

    def test_request_cap_is_one_mib(self) -> None:
        self.assertEqual(config.REQUEST_MAX_BYTES, 1024 * 1024)

    def test_web_ui_lifetime_defaults_match_the_environment_manifest(self) -> None:
        # ADR-032: 탭이 닫히면 3분, 사용이 없으면 20분, 최대 3시간.
        self.assertEqual(config.WEBUI_TAB_GRACE, 180.0)
        self.assertEqual(config.WEBUI_IDLE, 1200.0)
        self.assertEqual(config.WEBUI_MAX_LIFETIME, 10800.0)
        self.assertTrue(config.WEBUI_AUTOSHUTDOWN)

    def test_state_file_follows_a_redirected_output_directory(self) -> None:
        # 기본값은 import 시점의 OUTPUT_DIR 에 묶이지 않고 *현재* 값을 따른다
        # (테스트, LYRICS2PPTX_OUTPUT_DIR 로 출력 위치를 바꾼 실행).
        with mock.patch.object(config, "OUTPUT_DIR", config.ROOT / "tmp-output"):
            self.assertEqual(
                config.webui_state_path(), config.ROOT / "tmp-output" / "webui.state"
            )

    def test_an_explicit_state_file_wins_over_the_output_directory(self) -> None:
        with (
            mock.patch.object(config, "_WEBUI_STATE_EXPLICIT", True),
            mock.patch.object(config, "WEBUI_STATE_FILE", Path("D:/somewhere/webui.state")),
            mock.patch.object(config, "OUTPUT_DIR", Path("D:/elsewhere")),
        ):
            self.assertEqual(config.webui_state_path(), Path("D:/somewhere/webui.state"))

    def test_log_level_object_is_a_logging_level(self) -> None:
        self.assertIsInstance(config.LOG_LEVEL_OBJ, int)
        self.assertEqual(config.LOG_LEVEL_OBJ, getattr(logging, config.LOG_LEVEL))


class EnvBoolTests(unittest.TestCase):
    TRUE_TOKENS = ("1", "true", "TRUE", "yes", "on")
    FALSE_TOKENS = ("0", "false", "no", "off")

    def test_truthy_tokens(self) -> None:
        for token in self.TRUE_TOKENS:
            with self.subTest(token=token), mock.patch.dict(
                os.environ, {"LYRICS2PPTX_TEST_FLAG": token}
            ):
                self.assertIs(config._env_bool("LYRICS2PPTX_TEST_FLAG", False), True)

    def test_falsy_tokens(self) -> None:
        for token in self.FALSE_TOKENS:
            with self.subTest(token=token), mock.patch.dict(
                os.environ, {"LYRICS2PPTX_TEST_FLAG": token}
            ):
                self.assertIs(config._env_bool("LYRICS2PPTX_TEST_FLAG", True), False)

    def test_absent_variable_returns_the_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIs(config._env_bool("LYRICS2PPTX_TEST_FLAG", True), True)

    def test_invalid_value_raises_config_error(self) -> None:
        with mock.patch.dict(os.environ, {"LYRICS2PPTX_TEST_FLAG": "maybe"}):
            with self.assertRaises(ConfigError) as ctx:
                config._env_bool("LYRICS2PPTX_TEST_FLAG", False)
        self.assertIn("LYRICS2PPTX_TEST_FLAG", str(ctx.exception))


class EnvNumberTests(unittest.TestCase):
    def test_int_parses_and_trims(self) -> None:
        with mock.patch.dict(os.environ, {"LYRICS2PPTX_N": " 7 "}):
            self.assertEqual(config._env_int("LYRICS2PPTX_N", 0), 7)

    def test_int_rejects_non_numeric(self) -> None:
        with mock.patch.dict(os.environ, {"LYRICS2PPTX_N": "abc"}):
            with self.assertRaises(ConfigError):
                config._env_int("LYRICS2PPTX_N", 0)

    def test_int_rejects_out_of_range(self) -> None:
        with mock.patch.dict(os.environ, {"LYRICS2PPTX_N": "99"}):
            with self.assertRaises(ConfigError):
                config._env_int("LYRICS2PPTX_N", 0, minimum=0, maximum=10)
        with mock.patch.dict(os.environ, {"LYRICS2PPTX_N": "-1"}):
            with self.assertRaises(ConfigError):
                config._env_int("LYRICS2PPTX_N", 0, minimum=0)

    def test_slide_index_parser_requires_at_least_one(self) -> None:
        # The module-level SLIDE_INDEX is parsed with minimum=1; 0 is rejected.
        with mock.patch.dict(
            os.environ, {"LYRICS2PPTX_SLIDE_INDEX": "0"}
        ), self.assertRaises(ConfigError):
            config._env_int("LYRICS2PPTX_SLIDE_INDEX", 19, minimum=1)
        with mock.patch.dict(os.environ, {"LYRICS2PPTX_SLIDE_INDEX": "7"}):
            self.assertEqual(
                config._env_int("LYRICS2PPTX_SLIDE_INDEX", 19, minimum=1), 7
            )

    def test_slide_index_parser_rejects_non_numeric(self) -> None:
        with mock.patch.dict(
            os.environ, {"LYRICS2PPTX_SLIDE_INDEX": "nineteen"}
        ), self.assertRaises(ConfigError):
            config._env_int("LYRICS2PPTX_SLIDE_INDEX", 19, minimum=1)

    def test_float_parses_and_rejects(self) -> None:
        with mock.patch.dict(os.environ, {"LYRICS2PPTX_F": " 0.5 "}):
            self.assertEqual(config._env_float("LYRICS2PPTX_F", 1.0), 0.5)
        with mock.patch.dict(os.environ, {"LYRICS2PPTX_F": "nope"}):
            with self.assertRaises(ConfigError):
                config._env_float("LYRICS2PPTX_F", 1.0)
        with mock.patch.dict(os.environ, {"LYRICS2PPTX_F": "-2"}):
            with self.assertRaises(ConfigError):
                config._env_float("LYRICS2PPTX_F", 1.0, minimum=0.0)

    def test_lrclib_timeout_is_parsed_like_any_float_with_a_positive_floor(self) -> None:
        # ADR-030: the budget is a normal env float and must stay positive.
        with mock.patch.dict(os.environ, {"LYRICS2PPTX_LRCLIB_TIMEOUT": " 3.5 "}):
            self.assertEqual(
                config._env_float("LYRICS2PPTX_LRCLIB_TIMEOUT", 10.0, minimum=0.001),
                3.5,
            )
        with mock.patch.dict(os.environ, {"LYRICS2PPTX_LRCLIB_TIMEOUT": "0"}):
            with self.assertRaises(ConfigError):
                config._env_float("LYRICS2PPTX_LRCLIB_TIMEOUT", 10.0, minimum=0.001)


class EnvPathAndChoiceTests(unittest.TestCase):
    def test_relative_path_resolves_against_the_repository_root(self) -> None:
        with mock.patch.dict(os.environ, {"LYRICS2PPTX_P": "sub/dir"}):
            self.assertEqual(config._env_path("LYRICS2PPTX_P", config.ROOT), config.ROOT / "sub" / "dir")

    def test_absolute_path_is_kept(self) -> None:
        absolute = config.ROOT / "absolute" / "dir"
        with mock.patch.dict(os.environ, {"LYRICS2PPTX_P": str(absolute)}):
            self.assertEqual(config._env_path("LYRICS2PPTX_P", config.ROOT), absolute)

    def test_blank_value_falls_back_to_the_default(self) -> None:
        with mock.patch.dict(os.environ, {"LYRICS2PPTX_P": "   "}):
            self.assertEqual(config._env_path("LYRICS2PPTX_P", config.ROOT), config.ROOT)

    def test_choice_is_case_insensitive(self) -> None:
        with mock.patch.dict(os.environ, {"LYRICS2PPTX_C": "PARTS"}):
            self.assertEqual(
                config._env_str_choice("LYRICS2PPTX_C", "auto", config.CLONE_STRATEGIES), "parts"
            )

    def test_choice_rejects_unknown_value(self) -> None:
        with mock.patch.dict(os.environ, {"LYRICS2PPTX_C": "banana"}):
            with self.assertRaises(ConfigError):
                config._env_str_choice("LYRICS2PPTX_C", "auto", config.CLONE_STRATEGIES)

    def test_str_blank_falls_back_to_default(self) -> None:
        with mock.patch.dict(os.environ, {"LYRICS2PPTX_S": "  "}):
            self.assertEqual(config._env_str("LYRICS2PPTX_S", "fallback"), "fallback")


class FilenameTests(unittest.TestCase):
    def test_unsafe_characters_are_removed(self) -> None:
        self.assertEqual(config.sanitize_filename('a/b\\c:d*e?f"g<h>i|j.pptx'), "abcdefghij.pptx")

    def test_empty_result_falls_back_to_default_filename(self) -> None:
        self.assertEqual(config.sanitize_filename("..."), config.DEFAULT_FILENAME)

    def test_explicit_default_is_used_when_the_name_is_empty(self) -> None:
        self.assertEqual(config.sanitize_filename("///", default="x.pptx"), "x.pptx")

    def test_long_names_are_truncated_but_keep_the_suffix(self) -> None:
        name = config.sanitize_filename("a" * 200 + ".pptx")
        self.assertLessEqual(len(name), 100)
        self.assertTrue(name.endswith(".pptx"))

    def test_leading_and_trailing_dots_are_removed(self) -> None:
        self.assertEqual(config.sanitize_filename(".hidden.pptx"), "hidden.pptx")

    def test_ensure_pptx_suffix_adds_only_when_missing(self) -> None:
        self.assertEqual(config.ensure_pptx_suffix("song"), "song.pptx")
        self.assertEqual(config.ensure_pptx_suffix("song.pptx"), "song.pptx")
        self.assertEqual(config.ensure_pptx_suffix("SONG.PPTX"), "SONG.PPTX")


class UnknownEnvTests(unittest.TestCase):
    def test_unknown_prefixed_variables_are_reported_but_tokens_are_not(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"LYRICS2PPTX_BOGUS_KEY": "1", "LYRICS2PPTX_MYSOURCE_TOKEN": "secret"},
        ):
            unknown = config.unknown_env_keys()
        self.assertIn("LYRICS2PPTX_BOGUS_KEY", unknown)
        self.assertFalse([key for key in unknown if key.endswith("_TOKEN")])

    def test_known_variables_are_not_reported(self) -> None:
        with mock.patch.dict(os.environ, {"LYRICS2PPTX_TEMPLATE": "x.pptx"}):
            self.assertNotIn("LYRICS2PPTX_TEMPLATE", config.unknown_env_keys())

    def test_lrclib_timeout_is_a_known_variable(self) -> None:
        with mock.patch.dict(os.environ, {"LYRICS2PPTX_LRCLIB_TIMEOUT": "5"}):
            self.assertNotIn("LYRICS2PPTX_LRCLIB_TIMEOUT", config.unknown_env_keys())


if __name__ == "__main__":
    unittest.main()
