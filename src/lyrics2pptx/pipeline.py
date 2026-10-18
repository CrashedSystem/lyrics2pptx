"""Pipeline composition and the single error→message mapping (L3).

Entrypoints (``cli`` / ``webapp``) call only this module, so the error wording
and HTTP status live in exactly one place (``describe_error``, AD-09), and the
"no usable line" verdict is made exactly once (``run``, contract E3/E4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import config
from .crawl import (
    RawSong,
    SourceBlockedError,
    SourceNotFoundError,
    SourceUnavailableError,
    available_sources,
    get_source,
)
from .errors import (
    ConfigError,
    EmptyLyricsError,
    Lyrics2PptxError,
    OutputWriteError,
    TemplateStructureError,
)
from .pptx import builder as pptx_builder
from .pptx.builder import BuildResult, build_presentation
from .pptx.template_reader import get_template_profile
from .text.normalize import normalize_lines, normalize_with_stats
from .text.split import LinePair, split_pairs

__all__ = [
    "UserFacingError",
    "SearchHit",
    "PreviewResult",
    "run",
    "preview",
    "search_hits",
    "search_songs",
    "resolve_slide_index",
    "describe_error",
    "resolve_output_path",
    "doctor_report",
    "clear_search_cache",
]

log = logging.getLogger(__name__)

_PREVIEW_LINES = 3


@dataclass(frozen=True, slots=True)
class UserFacingError:
    """A user-visible error: code, Korean message, HTTP status, recoverability."""

    code: str
    message: str
    http_status: int
    recoverable: bool


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One search candidate as shown to the user."""

    index: int
    title: str
    artist: str | None
    source_name: str
    preview_lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreviewResult:
    """Normalized lines and their two-line pairing, without touching a file.

    Produced by ``preview`` for the web preview so the browser shows exactly
    what a build would put on the slides (M1: single normalization source).
    """

    lines: tuple[str, ...]
    pairs: tuple[LinePair, ...]
    removed_marker_count: int


# Exception type → (code, http_status, recoverable). Order matters: the first
# isinstance() match wins, and no entry here is a subclass of another.
_MESSAGE_TABLE: dict[type[Lyrics2PptxError], tuple[str, int, bool]] = {
    SourceNotFoundError: ("SOURCE_NOT_FOUND", 404, True),
    SourceBlockedError: ("SOURCE_BLOCKED", 423, True),
    SourceUnavailableError: ("SOURCE_UNAVAILABLE", 502, True),
    EmptyLyricsError: ("EMPTY_LYRICS", 422, False),
    TemplateStructureError: ("TEMPLATE_ERROR", 500, False),
    OutputWriteError: ("OUTPUT_WRITE_ERROR", 500, False),
    ConfigError: ("BAD_REQUEST", 400, False),
}

# Canonical Korean wording for each code. ``{detail}`` is filled with str(exc)
# for the codes that benefit from the concrete cause/path.
_MESSAGE_TEMPLATE: dict[str, str] = {
    "SOURCE_NOT_FOUND": "검색 결과가 없습니다. 다른 검색어를 시도하거나 가사를 직접 붙여넣으세요.",
    "SOURCE_BLOCKED": "이 소스는 자동 수집이 차단되었습니다. 직접 입력을 사용하세요.",
    "SOURCE_UNAVAILABLE": "일시적으로 수집에 실패했습니다. 잠시 후 재시도하세요.",
    "EMPTY_LYRICS": "가사에서 유효한 줄을 찾지 못했습니다.",
    "TEMPLATE_ERROR": "템플릿 구조를 읽을 수 없습니다: {detail}",
    "OUTPUT_WRITE_ERROR": "파일을 저장하지 못했습니다: {detail}",
    "BAD_REQUEST": "설정이 올바르지 않습니다: {detail}",
    "INTERNAL": "처리하지 못한 오류가 발생했습니다.",
}

# (source, query, limit) -> candidates. process-lifetime cache (docs/arch/03 §5).
_SEARCH_CACHE: dict[tuple[str, str, int], tuple[RawSong, ...]] = {}


def clear_search_cache() -> None:
    """Drop cached search results (used by tests and by ``--doctor``)."""
    _SEARCH_CACHE.clear()


def _message_for(code: str, exc: BaseException) -> str:
    template = _MESSAGE_TEMPLATE.get(code, _MESSAGE_TEMPLATE["INTERNAL"])
    if "{detail}" not in template:
        return template
    detail = str(exc).strip() or "원인을 알 수 없습니다"
    return template.replace("{detail}", detail)


def describe_error(exc: BaseException) -> UserFacingError:
    """Map any exception to a user-facing error (the only such mapping)."""
    for exc_type, (code, status, recoverable) in _MESSAGE_TABLE.items():
        if isinstance(exc, exc_type):
            return UserFacingError(
                code=code,
                message=_message_for(code, exc),
                http_status=status,
                recoverable=recoverable,
            )
    return UserFacingError(
        code="INTERNAL",
        message=_MESSAGE_TEMPLATE["INTERNAL"],
        http_status=500,
        recoverable=False,
    )


def search_songs(
    query: str,
    *,
    source: str = config.DEFAULT_SOURCE,
    limit: int = 5,
) -> list[RawSong]:
    """Return raw candidates for ``query`` (cached when enabled).

    Raises:
        ConfigError: the query is empty.
        SourceNotFoundError / SourceBlockedError / SourceUnavailableError: from
            the adapter.
    """
    text = (query or "").strip()
    if not text:
        raise ConfigError("검색어가 비어 있습니다")

    key = (source, text, max(1, limit))
    if config.CACHE_ENABLED:
        cached = _SEARCH_CACHE.get(key)
        if cached is not None:
            log.debug("search cache hit source=%s query=%s", source, text[:60])
            return list(cached)

    songs = list(get_source(source).search(text, limit=key[2]))
    if config.CACHE_ENABLED:
        _SEARCH_CACHE[key] = tuple(songs)
    return songs


def search_hits(
    query: str,
    *,
    source: str = config.DEFAULT_SOURCE,
    limit: int = 5,
) -> list[SearchHit]:
    """Return presentable candidates (title/artist/preview) for ``query``."""
    songs = search_songs(query, source=source, limit=limit)
    hits: list[SearchHit] = []
    for index, song in enumerate(songs):
        preview = tuple(normalize_lines(song.lyrics)[:_PREVIEW_LINES])
        hits.append(
            SearchHit(
                index=index,
                title=song.title,
                artist=song.artist,
                source_name=song.source_name,
                preview_lines=preview,
            )
        )
    return hits


def _clean_lines(lines: list[str]) -> list[str]:
    """Minimal guard for pre-normalized input: trim and drop blank entries.

    The caller asserts these lines are already normalized, so no N1..N10 pass
    runs here; we only ensure no empty line reaches ``split_pairs``.
    """
    cleaned: list[str] = []
    for line in lines:
        text = str(line).strip()
        if text:
            cleaned.append(text)
    return cleaned


def resolve_output_path(output_path: Path | None, *, title: str | None = None) -> Path:
    """Resolve where the ``.pptx`` is written, sanitizing the file name.

    An explicit ``output_path`` is honoured (relative paths resolve against the
    current directory); otherwise ``output/<title|DEFAULT_FILENAME>`` is used.
    The final name always ends in ``.pptx`` and carries no path characters.
    """
    if output_path is not None:
        path = Path(output_path).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
    else:
        stem = config.sanitize_filename(title) if title else config.DEFAULT_FILENAME
        path = config.OUTPUT_DIR / config.ensure_pptx_suffix(stem)

    safe_name = config.ensure_pptx_suffix(config.sanitize_filename(path.name))
    return path.with_name(safe_name)


def preview(raw_text: str, *, dedupe_chorus: bool = False) -> PreviewResult:
    """Normalize and pair ``raw_text`` without building a presentation.

    Used by the ``/api/preview`` route so the client never re-implements
    N1..N10/2-line pairing (M1). The normalization is the *same*
    ``text.normalize`` pass ``run`` uses, so a preview can never disagree with
    the generated slides.

    Raises:
        ConfigError: ``raw_text`` is not a string.
    """
    if not isinstance(raw_text, str):
        raise ConfigError("raw_text 는 문자열이어야 합니다")
    lines, removed_markers = normalize_with_stats(raw_text)
    pairs = split_pairs(lines, dedupe_repeat=dedupe_chorus)
    return PreviewResult(
        lines=tuple(lines),
        pairs=tuple(pairs),
        removed_marker_count=removed_markers,
    )


def _coerce_slide_index(value: object) -> int:
    """Coerce a user-supplied slide index to an int, or raise ``ConfigError``.

    Booleans and non-integer floats are rejected (``True`` must not become 1).
    """
    if value is None:
        return config.SLIDE_INDEX
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("slide_index 는 정수여야 합니다")
    if isinstance(value, float):
        if not value.is_integer():
            raise ConfigError("slide_index 는 정수여야 합니다")
        value = int(value)
    return int(value)


def resolve_slide_index(value: object = None) -> int:
    """Return a validated 1-based slide index (default ``config.SLIDE_INDEX``).

    Raises:
        ConfigError: ``value`` is not an integer or falls outside 1..slide_count.
    """
    index = _coerce_slide_index(value)
    get_template_profile(config.TEMPLATE_PATH, slide_index=index)
    return index


def run(
    query: str | None = None,
    *,
    source: str = config.DEFAULT_SOURCE,
    index: int = 0,
    raw_text: str | None = None,
    lines: list[str] | None = None,
    dedupe_chorus: bool = False,
    skip_slide: tuple[int, ...] = (),
    slide_index: int | None = None,
    keep_notes: bool = False,
    strategy: str = "auto",
    output_path: Path | None = None,
) -> BuildResult:
    """Collect (or accept) lyrics, split them, and build the presentation.

    Input priority is ``lines`` > ``raw_text`` > ``query`` (docs/arch/05 §4).
    When ``raw_text``/``lines`` are given, no network call happens.

    ``slide_index`` (1-based) selects the single template slide every output
    slide is cloned from; ``None`` uses ``config.SLIDE_INDEX``.

    Returns:
        ``BuildResult`` for the written file.

    Raises:
        ConfigError: no input was provided or ``slide_index`` is out of range.
        EmptyLyricsError: nothing usable remained after normalization/splitting.
        (plus every collector/template/write error, unmodified)
    """
    title: str | None = None

    if lines is not None:
        if raw_text is not None or query:
            log.warning("lines 우선: raw_text/query 를 무시합니다")
        clean = _clean_lines(lines)
    elif raw_text is not None:
        if query:
            log.warning("raw_text 우선: query 를 무시합니다 (네트워크 호출 없음)")
        clean = normalize_lines(raw_text)
    else:
        if not query or not str(query).strip():
            raise ConfigError("검색어(query)나 직접 입력(raw_text/lines) 중 하나가 필요합니다")
        candidates = search_songs(query, source=source, limit=max(index + 1, 5))
        if index < 0 or index >= len(candidates):
            raise SourceNotFoundError(f"선택한 검색 결과가 없습니다 (index={index})")
        song = candidates[index]
        title = song.title
        clean = normalize_lines(song.lyrics)

    pairs = split_pairs(clean, dedupe_repeat=dedupe_chorus)
    if not pairs:
        raise EmptyLyricsError("가사에서 유효한 줄을 찾지 못했습니다.")

    out = resolve_output_path(output_path, title=title)
    return pptx_builder.build_presentation(
        pairs,
        config.TEMPLATE_PATH,
        out,
        skip_slide=tuple(skip_slide),
        slide_index=slide_index,
        keep_notes=keep_notes,
        strategy=strategy,
    )


def doctor_report() -> dict[str, object]:
    """Read-only diagnostics for ``--doctor`` (docs/arch/07 §7)."""
    template = config.TEMPLATE_PATH
    report: dict[str, object] = {
        "template_path": str(template),
        "template_exists": template.is_file(),
        "fixture_mode": config.FIXTURE_DIR is not None,
        "configured_strategy": config.CLONE_STRATEGY,
        "configured_slide_index": config.SLIDE_INDEX,
        "sources": list(available_sources()),
        "output_dir": str(config.OUTPUT_DIR),
    }
    if template.is_file():
        try:
            profile = get_template_profile(template)
            report.update(
                {
                    "template_size": profile.template_size,
                    "slide_count": profile.slide_count,
                    "selected_slide": profile.selected_slide + 1,
                    "selected_slide_has_title_shape": (
                        profile.styles[profile.selected_slide].shape_found
                    ),
                    "pool": list(profile.pool),
                    "pool_size": len(profile.pool),
                    "slide_size": list(profile.slide_size),
                    "default_style_slide": profile.default_style.slide_index + 1,
                    "style_fallback_slides": [
                        index + 1
                        for index, style in sorted(profile.styles.items())
                        if not style.has_rpr
                    ],
                }
            )
        except Lyrics2PptxError as exc:
            report["profile_error"] = str(exc)
    try:
        report["clone_writer"] = pptx_builder.select_writer("auto").name
    except Lyrics2PptxError as exc:  # pragma: no cover - defensive
        report["clone_writer"] = f"error: {exc}"
    return report
