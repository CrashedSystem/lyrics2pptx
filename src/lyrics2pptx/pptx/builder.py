"""Build orchestration — single-background assembly + atomic save (L3).

This module owns exactly one rule (docs/arch/04 §2.1, revised to single-slide
mode):

    every output slide is cloned from ``profile.selected_slide``

The user picks one template slide (1-based, ``config.SLIDE_INDEX`` or
``--slide``) and every generated lyrics slide reuses that exact background.

Everything else is delegation: the writer produces the package bytes, this
module verifies the result reopens, then swaps it into place atomically
(tmp file → ``os.replace``, AD-13).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from pptx import Presentation
from pptx.oxml.ns import qn

from .. import config
from ..errors import (
    ConfigError,
    EmptyLyricsError,
    Lyrics2PptxError,
    OutputWriteError,
    TemplateStructureError,
)
from .template_reader import (
    BuildResult,
    StyleSpec,
    TemplateProfile,
    get_template_profile,
)
from .writer import PptxWriter, select_writer

__all__ = ["SlideSpec", "BuildResult", "build_plan", "build_presentation"]

log = logging.getLogger(__name__)

_FALLBACK_MESSAGE = "슬라이드 복제에 실패했습니다. template.pptx 를 다시 저장해 주세요."


@dataclass(frozen=True, slots=True)
class SlideSpec:
    """Assembly instruction for one output slide (docs/arch/03 §2).

    ``style`` is the ``제목 1`` snapshot of ``profile.selected_slide``; its
    ``slide_index`` is the 0-based template index shared by every output slide.
    """

    order: int  # 0-based output order
    line1: str
    line2: str | None
    style: StyleSpec  # style snapshot of profile.selected_slide


def build_plan(pairs: list[tuple[str, str | None]], profile: TemplateProfile) -> list[SlideSpec]:
    """Turn line pairs into per-slide specs, all sharing one background.

    Every spec carries the style snapshot of ``profile.selected_slide`` so the
    words land on the same template slide each time (single-slide mode).
    """
    if not pairs:
        return []
    style = profile.styles.get(profile.selected_slide, profile.default_style)
    return [
        SlideSpec(order=order, line1=line1, line2=line2, style=style)
        for order, (line1, line2) in enumerate(pairs)
    ]


def _resolve_output_path(output_path: Path | None) -> Path:
    if output_path is None:
        return config.OUTPUT_DIR / config.DEFAULT_FILENAME
    return Path(output_path)


def _ensure_parent(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputWriteError(f"파일을 저장하지 못했습니다: {path}") from exc


def _cleanup(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:  # pragma: no cover - best effort
        log.debug("임시 파일 정리에 실패했습니다: %s", path)


def _count_slides(path: Path) -> int:
    """Read the slide count straight from the XML (never touches ``.slides``).

    Accessing ``Presentation.slides`` would rename slide parts as a side effect;
    the verification pass must stay free of side effects.
    """
    presentation = Presentation(str(path))
    sld_id_lst = presentation.part._element.find(qn("p:sldIdLst"))
    if sld_id_lst is None:
        return 0
    return len(sld_id_lst.findall(qn("p:sldId")))


def _verify_reopenable(path: Path, expected: int) -> None:
    """Confirm the written package reopens and lists exactly ``expected`` slides.

    Raises:
        TemplateStructureError: the file cannot be reopened or the slide count
            differs — the trigger for the F1 fallback (docs/arch/04 §6).
    """
    try:
        actual = _count_slides(path)
    except Exception as exc:  # noqa: BLE001 - any parse error means "unusable"
        log.error("reopen verification failed output=%s", path)
        raise TemplateStructureError(_FALLBACK_MESSAGE) from exc
    if actual != expected:
        log.error(
            "reopen verification failed output=%s slides=%d expected=%d",
            path,
            actual,
            expected,
        )
        raise TemplateStructureError(_FALLBACK_MESSAGE)


def _run_writer(
    writer: PptxWriter,
    plan: list[tuple[str, str | None]],
    profile: TemplateProfile,
    tmp: Path,
    expected: int,
    keep_notes: bool,
) -> BuildResult:
    result = writer.write(plan, profile, tmp, keep_notes=keep_notes)
    _verify_reopenable(tmp, expected)
    return result


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:  # pragma: no cover - file was just written
        return 0


def build_presentation(
    pairs: list[tuple[str, str | None]],
    template_path: Path | None = None,
    output_path: Path | None = None,
    *,
    skip_slide: tuple[int, ...] = (),
    slide_index: int | None = None,
    keep_notes: bool = False,
    strategy: str = "auto",
) -> BuildResult:
    """Assemble ``pairs`` into a presentation and return the build result.

    Args:
        pairs: two-line pairs from ``text.split``.
        template_path: template to read from; defaults to ``config.TEMPLATE_PATH``.
        output_path: destination; defaults to ``output/<DEFAULT_FILENAME>``.
        skip_slide: 1-based template slides excluded from the eligible pool
            (kept for compatibility; single-slide mode always uses ``slide_index``).
        slide_index: 1-based template slide cloned by every output slide;
            ``None`` uses ``config.SLIDE_INDEX``.
        keep_notes: copy notesSlide relationships too (default off, AD-08).
        strategy: ``auto`` / ``parts`` / ``zip`` (docs/arch/04 §5).

    Raises:
        EmptyLyricsError: ``pairs`` is empty.
        ConfigError: ``slide_index`` is out of range or the strategy is unknown.
        TemplateStructureError: no writer could produce a reopenable document.
        OutputWriteError: the output directory/temp file/replace failed.
    """
    if not pairs:
        raise EmptyLyricsError("가사에서 유효한 줄을 찾지 못했습니다.")

    profile = get_template_profile(
        template_path, skip_slide=tuple(skip_slide), slide_index=slide_index
    )
    specs = build_plan(pairs, profile)
    plan: list[tuple[str, str | None]] = [(spec.line1, spec.line2) for spec in specs]
    slide_count = len(plan)

    out = _resolve_output_path(output_path)
    _ensure_parent(out)
    tmp = out.with_name(f".tmp-{uuid4().hex}{out.suffix or '.pptx'}")

    writer = select_writer(strategy)
    try:
        try:
            result = _run_writer(writer, plan, profile, tmp, slide_count, keep_notes)
        except TemplateStructureError as exc:
            # Only ``auto`` falls back; an explicitly forced writer must fail
            # loudly so the user sees why their choice did not work.
            if strategy != "auto" or writer.name != "parts":
                raise
            log.warning("fallback to zip writer reason=%s", exc)
            _cleanup(tmp)
            result = _run_writer(
                select_writer("zip"), plan, profile, tmp, slide_count, keep_notes
            )
        os.replace(str(tmp), str(out))
    except Lyrics2PptxError:
        _cleanup(tmp)
        raise
    except OSError as exc:
        _cleanup(tmp)
        raise OutputWriteError(f"파일을 저장하지 못했습니다: {out}") from exc
    except Exception:
        _cleanup(tmp)
        raise

    log.info(
        "built output=%s slides=%d background=%d bytes=%d",
        out,
        slide_count,
        profile.selected_slide + 1,
        _file_size(out),
    )
    return replace(result, output_path=out)
