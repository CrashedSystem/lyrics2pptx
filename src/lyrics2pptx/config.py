"""Configuration single source of truth (L0 leaf).

This is the *only* module in the package allowed to touch ``os.environ``
(rule P9 in docs/arch/02 §4). Every value is evaluated once at import time so
the rest of the code reads frozen module constants.

Environment variables are documented in docs/arch/05 §5 and enumerated in
``환경선언서.md`` §4. There is no required variable: every entry has a default.
The only secret-shaped convention is ``LYRICS2PPTX_<SOURCE>_TOKEN`` for token
based adapters; such values must never be logged.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from .errors import ConfigError

__all__ = [
    "ROOT",
    "TEMPLATE_PATH",
    "OUTPUT_DIR",
    "WEB_DIR",
    "FIXTURE_DIR",
    "DEFAULT_FILENAME",
    "DEFAULT_SOURCE",
    "CHAIN_MEMBERS",
    "SEARCH_BACKEND",
    "BLOG_MAX_PAGES",
    "HTTP_DELAY",
    "HTTP_TIMEOUT",
    "LRCLIB_TIMEOUT",
    "MAX_RETRIES",
    "DEFAULT_UA",
    "BIND_HOST",
    "BIND_PORT",
    "LOG_LEVEL",
    "LOG_LEVEL_OBJ",
    "KEEP_NOTES",
    "DEDUPE_CHORUS",
    "CACHE_ENABLED",
    "CLONE_STRATEGY",
    "TITLE_SHAPE_NAME",
    "SLIDE_INDEX",
    "SLIDE_INDEX_IS_EXPLICIT",
    "NORM_AUTOFIT",
    "REQUEST_MAX_BYTES",
    "ROBOTS_CACHE_TTL",
    "CLONE_STRATEGIES",
    "WEBUI_IDLE",
    "WEBUI_TAB_GRACE",
    "WEBUI_MAX_LIFETIME",
    "WEBUI_AUTOSHUTDOWN",
    "webui_state_path",
    "sanitize_filename",
    "ensure_pptx_suffix",
    "unknown_env_keys",
]

_log = logging.getLogger(__name__)

# The folder the *user* sees: a developer checkout, or — in a packaged run —
# the folder ``lyrics2pptx.exe`` was unzipped into. ``template.pptx`` is read
# from there and ``output/`` is written there, so a packaged user can find the
# result without knowing anything about the program (ADR-033).
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
    # Assets that ship *inside* the exe (``web/``) are unpacked per run here.
    _BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", ROOT))
else:
    # Repository root: <repo>/src/lyrics2pptx/config.py -> parents[2] == <repo>
    ROOT = Path(__file__).resolve().parents[2]
    _BUNDLE_ROOT = ROOT

# Constants that are not environment-driven but are shared across layers.
NORM_AUTOFIT = True
REQUEST_MAX_BYTES = 1_048_576  # 1 MiB request body cap for the local web app
ROBOTS_CACHE_TTL = 600.0  # seconds a robots.txt verdict stays cached
CLONE_STRATEGIES = ("auto", "parts", "zip")

_ENV_PREFIX = "LYRICS2PPTX_"


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip()
    return value if value else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    token = raw.strip().lower()
    if token in ("1", "true", "yes", "on"):
        return True
    if token in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"환경변수 {name} 값이 올바르지 않습니다: 0 또는 1 이어야 합니다")


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"환경변수 {name} 값이 올바르지 않습니다: 정수가 아닙니다") from exc
    if value < minimum or (maximum is not None and value > maximum):
        raise ConfigError(f"환경변수 {name} 값이 허용 범위를 벗어났습니다: {value}")
    return value


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"환경변수 {name} 값이 올바르지 않습니다: 숫자가 아닙니다") from exc
    if value < minimum:
        raise ConfigError(f"환경변수 {name} 값이 허용 범위를 벗어났습니다: {value}")
    return value


def _env_path(name: str, default: Path) -> Path:
    """Return an absolute path, resolving relative values against the repo root."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    candidate = Path(raw.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate


def _env_str_choice(name: str, default: str, choices: tuple[str, ...]) -> str:
    value = _env_str(name, default).lower()
    if value not in choices:
        raise ConfigError(
            f"환경변수 {name} 값이 올바르지 않습니다: {'/'.join(choices)} 중 하나여야 합니다"
        )
    return value


TEMPLATE_PATH = _env_path("LYRICS2PPTX_TEMPLATE", ROOT / "template.pptx")
OUTPUT_DIR = _env_path("LYRICS2PPTX_OUTPUT_DIR", ROOT / "output")
WEB_DIR = _env_path("LYRICS2PPTX_WEB_DIR", _BUNDLE_ROOT / "web")
FIXTURE_DIR = _env_path("LYRICS2PPTX_FIXTURE_DIR", ROOT / "tests" / "fixtures") if (
    os.environ.get("LYRICS2PPTX_FIXTURE_DIR", "").strip()
) else None

DEFAULT_FILENAME = _env_str("LYRICS2PPTX_DEFAULT_FILENAME", "가사.pptx")
# ADR-018/ADR-025: ``auto`` asks these members **concurrently** and then ranks
# the answers (checked blog consensus > lrclib > unchecked blog). The order of
# this tuple no longer decides anything — it only names the members.
DEFAULT_SOURCE = _env_str("LYRICS2PPTX_SOURCE", "auto")
CHAIN_MEMBERS = ("lrclib", "blog")
SEARCH_BACKEND = _env_str_choice("LYRICS2PPTX_SEARCH_BACKEND", "ddg", ("ddg",))
# Upper bound on pages fetched (and votes cast) per web search. Each fetch is
# subject to HTTP_DELAY, so this must stay small.
BLOG_MAX_PAGES = _env_int("LYRICS2PPTX_BLOG_MAX_PAGES", 5, minimum=1, maximum=10)

HTTP_DELAY = _env_float("LYRICS2PPTX_HTTP_DELAY", 2.0, minimum=0.0)
HTTP_TIMEOUT = _env_float("LYRICS2PPTX_HTTP_TIMEOUT", 10.0, minimum=0.001)
# ADR-030: ``auto`` asks blog and lrclib *concurrently* (ADR-025), and lrclib is
# only a fallback behind the checked blog consensus. ``HTTP_TIMEOUT`` bounds a
# single socket read, but ``MAX_RETRIES`` × fallback URLs let a dead lrclib hold
# the whole chain for ~100 s. This caps lrclib's *entire* call instead: one
# deadline for every URL, and no retries inside it.
LRCLIB_TIMEOUT = _env_float("LYRICS2PPTX_LRCLIB_TIMEOUT", 10.0, minimum=0.001)
MAX_RETRIES = _env_int("LYRICS2PPTX_MAX_RETRIES", 3, minimum=0, maximum=10)
DEFAULT_UA = _env_str("LYRICS2PPTX_USER_AGENT", "lyrics2pptx/0.1 (+local single-user tool)")

BIND_HOST = _env_str("LYRICS2PPTX_BIND_HOST", "127.0.0.1")
BIND_PORT = _env_int("LYRICS2PPTX_BIND_PORT", 8765, minimum=1, maximum=65535)

# --- web UI lifetime (ADR-032) -------------------------------------------
# The local web app is a *session* tool: a beginner opens it, builds two or three
# decks and wants it gone. "Close the console window" is the one instruction that
# loses that user, so the server retires itself instead, and the launcher can find
# the instance that is already running instead of starting a second one.
#
#   * WEBUI_TAB_GRACE   - the browser tab told us it is closing (pagehide beacon);
#                         the server waits this long for a reload/return, then stops.
#   * WEBUI_IDLE        - no *user* request (searches, builds, downloads) for this
#                         long: the tab is open but nobody is using it. A heartbeat
#                         does not count as use, so a forgotten tab still retires.
#   * WEBUI_MAX_LIFETIME- hard ceiling, so a forgotten server cannot outlive the day.
#   * WEBUI_AUTOSHUTDOWN- 0 disables all three rules (scripts, long batch sessions)
#                         while the in-page "종료" button still works.
# A value of 0 for one of the three timeouts disables just that rule.
WEBUI_TAB_GRACE = _env_float("LYRICS2PPTX_WEBUI_TAB_GRACE", 180.0, minimum=0.0)
WEBUI_IDLE = _env_float("LYRICS2PPTX_WEBUI_IDLE", 1200.0, minimum=0.0)
WEBUI_MAX_LIFETIME = _env_float("LYRICS2PPTX_WEBUI_MAX_LIFETIME", 10800.0, minimum=0.0)
WEBUI_AUTOSHUTDOWN = _env_bool("LYRICS2PPTX_WEBUI_AUTOSHUTDOWN", True)
# Rendezvous file for ``lyrics2pptx.vbs``: it holds the port and the instance id of
# the live server so a second double-click reuses it. Plain ``key=value`` lines
# because the launcher is VBScript and has no JSON parser. Written only while the
# server runs (webapp.write_state / clear_state) — see ``webui_state_path``.
_WEBUI_STATE_NAME = "webui.state"
_WEBUI_STATE_EXPLICIT = bool(os.environ.get("LYRICS2PPTX_WEBUI_STATE_FILE", "").strip())
WEBUI_STATE_FILE = _env_path("LYRICS2PPTX_WEBUI_STATE_FILE", OUTPUT_DIR / _WEBUI_STATE_NAME)

LOG_LEVEL = _env_str("LYRICS2PPTX_LOG_LEVEL", "INFO").upper()
LOG_LEVEL_OBJ = getattr(logging, LOG_LEVEL, None)
if not isinstance(LOG_LEVEL_OBJ, int):
    _log.warning("알 수 없는 LYRICS2PPTX_LOG_LEVEL=%r, INFO 로 대체합니다", LOG_LEVEL)
    LOG_LEVEL = "INFO"
    LOG_LEVEL_OBJ = logging.INFO

KEEP_NOTES = _env_bool("LYRICS2PPTX_KEEP_NOTES", False)
DEDUPE_CHORUS = _env_bool("LYRICS2PPTX_DEDUPE_CHORUS", False)
CACHE_ENABLED = _env_bool("LYRICS2PPTX_CACHE_ENABLED", True)
CLONE_STRATEGY = _env_str_choice("LYRICS2PPTX_CLONE_STRATEGY", "auto", CLONE_STRATEGIES)

TITLE_SHAPE_NAME = _env_str("LYRICS2PPTX_TITLE_SHAPE", "제목 1")

# 1-based template slide number that every output slide is cloned from (single
# background mode). The default 19 is the only template slide whose ``제목 1``
# sample is exactly two lines ("우리 함께 손잡고 가세" / "조금 느리고 어려울지라도"),
# so it previews the real output shape without extra filling. The upper bound
# (slide_count) is enforced by ``template_reader`` because it needs the template.
SLIDE_INDEX = _env_int("LYRICS2PPTX_SLIDE_INDEX", 19, minimum=1)

# True when the slide number above came from the environment instead of the
# default. ``template_reader`` uses this to tell "the user asked for slide 19"
# (hard error on a shorter template) from "the default no longer fits"
# (fall back to the last slide — ADR-034, a plain template swap must work).
SLIDE_INDEX_IS_EXPLICIT = "LYRICS2PPTX_SLIDE_INDEX" in os.environ

_KNOWN_ENV = frozenset(
    {
        "LYRICS2PPTX_TEMPLATE",
        "LYRICS2PPTX_OUTPUT_DIR",
        "LYRICS2PPTX_WEB_DIR",
        "LYRICS2PPTX_FIXTURE_DIR",
        "LYRICS2PPTX_DEFAULT_FILENAME",
        "LYRICS2PPTX_SOURCE",
        "LYRICS2PPTX_SEARCH_BACKEND",
        "LYRICS2PPTX_BLOG_MAX_PAGES",
        "LYRICS2PPTX_HTTP_DELAY",
        "LYRICS2PPTX_HTTP_TIMEOUT",
        "LYRICS2PPTX_LRCLIB_TIMEOUT",
        "LYRICS2PPTX_MAX_RETRIES",
        "LYRICS2PPTX_USER_AGENT",
        "LYRICS2PPTX_BIND_HOST",
        "LYRICS2PPTX_BIND_PORT",
        "LYRICS2PPTX_WEBUI_TAB_GRACE",
        "LYRICS2PPTX_WEBUI_IDLE",
        "LYRICS2PPTX_WEBUI_MAX_LIFETIME",
        "LYRICS2PPTX_WEBUI_AUTOSHUTDOWN",
        "LYRICS2PPTX_WEBUI_STATE_FILE",
        "LYRICS2PPTX_LOG_LEVEL",
        "LYRICS2PPTX_KEEP_NOTES",
        "LYRICS2PPTX_DEDUPE_CHORUS",
        "LYRICS2PPTX_CACHE_ENABLED",
        "LYRICS2PPTX_CLONE_STRATEGY",
        "LYRICS2PPTX_TITLE_SHAPE",
        "LYRICS2PPTX_SLIDE_INDEX",
    }
)

# Characters that must never appear in an output file name.
_UNSAFE_NAME_CHARS = '/\\:*?"<>|'
_MAX_FILENAME_LEN = 100


def webui_state_path() -> Path:
    """Return the launcher rendezvous file (ADR-032).

    ``WEBUI_STATE_FILE`` is frozen at import like every other value here, but the
    output directory can be redirected *afterwards* (``LYRICS2PPTX_OUTPUT_DIR`` in
    a test or a script). When the state file was not set explicitly, follow the
    current ``OUTPUT_DIR`` so the two can never disagree.
    """
    if _WEBUI_STATE_EXPLICIT:
        return WEBUI_STATE_FILE
    return OUTPUT_DIR / _WEBUI_STATE_NAME


def unknown_env_keys() -> tuple[str, ...]:
    """Return ``LYRICS2PPTX_*`` variables this build does not know about.

    Adapters may define ``LYRICS2PPTX_<SOURCE>_TOKEN``; those are treated as
    known because their suffix is source specific. Entrypoints log the rest as
    a typo hint (docs/arch/07 §6).
    """
    extra = []
    for key in os.environ:
        if not key.startswith(_ENV_PREFIX) or key in _KNOWN_ENV:
            continue
        if key.endswith("_TOKEN"):
            continue
        extra.append(key)
    return tuple(sorted(extra))


def sanitize_filename(name: str, *, default: str | None = None) -> str:
    """Return a filesystem-safe file name (no directory separators).

    Used by ``pipeline.resolve_output_path`` before a path ever reaches the
    builder. An empty result falls back to ``default`` (``DEFAULT_FILENAME``).
    """
    fallback = default if default is not None else DEFAULT_FILENAME
    cleaned = "".join(ch for ch in name if ch not in _UNSAFE_NAME_CHARS)
    cleaned = cleaned.strip().strip(".")
    if not cleaned:
        cleaned = fallback
    if len(cleaned) > _MAX_FILENAME_LEN:
        stem, dot, suffix = cleaned.rpartition(".")
        if dot and len(suffix) <= 5:
            cleaned = stem[: _MAX_FILENAME_LEN - len(suffix) - 1] + "." + suffix
        else:
            cleaned = cleaned[:_MAX_FILENAME_LEN]
    return cleaned


def ensure_pptx_suffix(name: str) -> str:
    """Return ``name`` guaranteed to end with the ``.pptx`` extension."""
    return name if name.lower().endswith(".pptx") else f"{name}.pptx"
