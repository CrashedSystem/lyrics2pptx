"""Command line entry point (L4).

Owns argv parsing, logging init, and the exit-code contract (docs/arch/05 §3).
Every domain failure is turned into a message by ``pipeline.describe_error`` —
this module never contains error wording of its own.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path

from . import __version__, config, pipeline
from .errors import ConfigError, Lyrics2PptxError

log = logging.getLogger("lyrics2pptx.cli")

__all__ = ["main", "build_parser"]

# Code page names (normalized: lower-case, no separators) that stand for the
# Korean Windows default cp949. When the active stream reports one of these we
# pin the encoding so the replacement fallback is deterministic.
_CP949_ENCODINGS = frozenset({"cp949", "ksc56011987", "euckr", "ms949", "uhc"})


# code -> exit status (docs/arch/05 §3). Unknown codes fall back to 4.
_EXIT_CODES: dict[str, int] = {
    "BAD_REQUEST": 1,
    "SOURCE_NOT_FOUND": 1,
    "SOURCE_BLOCKED": 2,
    "SOURCE_UNAVAILABLE": 2,
    "EMPTY_LYRICS": 3,
    "TEMPLATE_ERROR": 4,
    "OUTPUT_WRITE_ERROR": 4,
    "INTERNAL": 4,
}


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for ``python -m lyrics2pptx.cli``."""
    parser = argparse.ArgumentParser(
        prog="python -m lyrics2pptx.cli",
        description="가사 2줄 PPTX 생성기 - 검색어로 가사를 모아 template.pptx 에 2줄씩 채웁니다.",
    )
    parser.add_argument("--query", help="검색어 (예: \"생명샘\" 또는 \"아티스트 - 곡명\")")
    parser.add_argument("--input-file", help="직접 입력 텍스트 파일 (UTF-8, 정규화 적용)")
    parser.add_argument(
        "--lines-file",
        help="이미 정리된 줄 텍스트 파일 (UTF-8, 한 줄에 한 문장, 정규화 생략)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help=f"출력 경로 (기본: {Path('output') / config.DEFAULT_FILENAME})",
    )
    parser.add_argument("--source", default=config.DEFAULT_SOURCE, help="수집 소스 이름")
    parser.add_argument("--index", type=int, default=0, help="검색 결과 중 선택할 번호 (기본 0)")
    parser.add_argument(
        "--dedupe-chorus", action="store_true", help="후렴 등 반복 줄을 축소합니다"
    )
    parser.add_argument(
        "--skip-slide",
        type=int,
        action="append",
        default=[],
        metavar="N",
        help="배경 풀에서 제외할 템플릿 슬라이드 번호 (1-based, 반복 가능)",
    )
    parser.add_argument(
        "--slide",
        type=int,
        default=None,
        metavar="N",
        help=(
            "모든 슬라이드가 복제할 템플릿 배경 번호 (1-based, 1~20, "
            f"기본 {config.SLIDE_INDEX})"
        ),
    )
    parser.add_argument("--keep-notes", action="store_true", help="템플릿 노트도 복제합니다")
    parser.add_argument(
        "--strategy",
        choices=config.CLONE_STRATEGIES,
        default=config.CLONE_STRATEGY,
        help="슬라이드 복제 전략 (auto/parts/zip)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="빌드하지 않고 수집 결과만 JSON 으로 stdout 에 출력합니다",
    )
    parser.add_argument(
        "--cross-check",
        action="store_true",
        help=(
            "여러 공개 페이지의 가사를 교차검증해 합의 가사와 다른 표기를 "
            "출력합니다 (--query 필요, 빌드하지 않음)"
        ),
    )
    parser.add_argument("--log-level", help=f"로그 수준 (기본: {config.LOG_LEVEL})")
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="템플릿/전략 진단만 출력하고 파일을 만들지 않습니다",
    )
    return parser


def _setup_logging(level_name: str | None) -> None:
    level = (level_name or config.LOG_LEVEL).upper()
    resolved = getattr(logging, level, None)
    if not isinstance(resolved, int):
        resolved = config.LOG_LEVEL_OBJ
    logging.basicConfig(
        level=resolved,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    for noisy in ("pptx", "PIL", "lxml"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _warn_unknown_env() -> None:
    unknown = config.unknown_env_keys()
    if unknown:
        log.warning("알 수 없는 환경변수(오타 가능성): %s", ", ".join(unknown))


def _read_text_file(raw_path: str) -> str:
    path = Path(raw_path)
    if not path.is_file():
        raise ConfigError(f"입력 파일을 찾을 수 없습니다: {path}")
    return path.read_text(encoding="utf-8")


def _resolve_inputs(args: argparse.Namespace) -> tuple[str | None, list[str] | None]:
    """Return ``(raw_text, lines)`` from the mutually exclusive input options."""
    given = [
        name
        for name, present in (
            ("--query", bool(args.query)),
            ("--input-file", bool(args.input_file)),
            ("--lines-file", bool(args.lines_file)),
        )
        if present
    ]
    if not given:
        raise ConfigError(
            "--query, --input-file, --lines-file 중 하나가 필요합니다"
        )
    if len(given) > 1:
        raise ConfigError(f"입력 옵션은 하나만 사용할 수 있습니다: {', '.join(given)}")

    if args.input_file:
        return _read_text_file(args.input_file), None
    if args.lines_file:
        content = _read_text_file(args.lines_file)
        return None, content.splitlines()
    return None, None


def _emit_collection_json(args: argparse.Namespace) -> int:
    if not args.query:
        raise ConfigError("--json 은 --query 와 함께 사용해야 합니다")
    songs = pipeline.search_songs(
        args.query, source=args.source, limit=max(args.index + 1, 5)
    )
    payload = {
        "source": args.source,
        "query": args.query,
        "count": len(songs),
        "results": [asdict(song) for song in songs],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _emit_cross_check(args: argparse.Namespace) -> int:
    """Print the majority-voted lyrics and every disagreement (no build)."""
    if not args.query:
        raise ConfigError("--cross-check 는 --query 와 함께 사용해야 합니다")
    result = pipeline.cross_check(args.query)
    if args.as_json:
        payload = {
            "query": result.query,
            "source_count": result.source_count,
            "count": len(result.lines),
            "lines": list(result.lines),
            "detail": [asdict(item) for item in result.detail],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"# {result.query} — {result.source_count}곳 교차검증, {len(result.lines)}줄")
    disputed = 0
    for number, item in enumerate(result.detail, 1):
        note = ""
        if item.variants:
            disputed += 1
            note = f"   ← 다른 표기: {' / '.join(item.variants)}"
        print(f"{number:3d}. {item.text}{note}")
    print(f"# 불일치 {disputed}줄 / 전체 {len(result.detail)}줄")
    return 0


def _doctor(args: argparse.Namespace) -> int:
    report = pipeline.doctor_report()
    try:
        report["python_pptx"] = package_version("python-pptx")
    except PackageNotFoundError:  # pragma: no cover - dependency is pinned
        report["python_pptx"] = "unknown"
    report = {"lyrics2pptx": __version__, **report}

    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for key, value in report.items():
            print(f"{key}: {value}")
    return 0


def _normalized_encoding(stream: object) -> str:
    """Return the stream encoding name in a comparison-friendly form."""
    name = getattr(stream, "encoding", None)
    if not isinstance(name, str):
        return ""
    return name.lower().replace("-", "").replace("_", "").replace(" ", "")


def _harden_console() -> None:
    """Downgrade unencodable characters instead of aborting the process.

    A Korean Windows console defaults to cp949, which cannot represent em dash
    (U+2014) or the horizontal ellipsis (U+2026); a single such character in a
    help or error message would otherwise raise ``UnicodeEncodeError`` and hide
    the very message the user asked for. Reconfiguring stdout/stderr with
    ``errors="replace"`` keeps well-formed UTF-8 behaviour byte-for-byte
    identical while turning a stray character into ``?`` on a cp949 console.

    Streams without ``reconfigure`` (``io.StringIO`` in tests, redirected
    capture) are left untouched.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        kwargs: dict[str, str] = {"errors": "replace"}
        if _normalized_encoding(stream) in _CP949_ENCODINGS:
            kwargs["encoding"] = "cp949"
        try:
            reconfigure(**kwargs)
        except (ValueError, LookupError, OSError):
            continue


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    _harden_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.log_level)
    _warn_unknown_env()

    try:
        if args.doctor:
            return _doctor(args)
        raw_text, lines = _resolve_inputs(args)
        if args.cross_check:
            return _emit_cross_check(args)
        if args.as_json:
            return _emit_collection_json(args)

        result = pipeline.run(
            query=args.query,
            source=args.source,
            index=args.index,
            raw_text=raw_text,
            lines=lines,
            dedupe_chorus=args.dedupe_chorus,
            skip_slide=tuple(args.skip_slide),
            slide_index=args.slide,
            keep_notes=args.keep_notes,
            strategy=args.strategy,
            output_path=Path(args.output) if args.output else None,
        )
        print(str(result.output_path))
        return 0
    except Lyrics2PptxError as exc:
        facing = pipeline.describe_error(exc)
        log.debug("cli failure", exc_info=True)
        print(facing.message, file=sys.stderr)
        if facing.recoverable:
            print(
                "가사를 직접 붙여넣어 --input-file 로 진행할 수 있습니다.",
                file=sys.stderr,
            )
        return _EXIT_CODES.get(facing.code, 4)
    except Exception:
        log.exception("unhandled cli error")
        print("처리하지 못한 오류가 발생했습니다.", file=sys.stderr)
        return 4


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
