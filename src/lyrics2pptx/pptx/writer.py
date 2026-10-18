"""Writer strategy selection (L3 composition).

``PptxWriter`` is the single seam through which the builder writes a package
(docs/arch/04 §6). Two implementations exist:

* ``PartsWriter`` — method A: deep-copy ``<p:sld>`` into a new slide part and
  let python-pptx serialize the package (``writer_parts.py``).
* ``ZipWriter`` — fallback F1: rewrite the OPC zip with ``zipfile`` + ``lxml``
  (``writer_zip.py``).

``select_writer`` resolves ``config.CLONE_STRATEGY``; only ``auto`` consults the
capability probe, so the probe's result is computed at most once per process.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

from .. import config
from ..errors import ConfigError
from .clone import pptx_can_register_slide_part
from .template_reader import BuildResult, Plan, TemplateProfile
from .writer_parts import write_via_parts
from .writer_zip import write_via_zip

__all__ = ["Plan", "PptxWriter", "PartsWriter", "ZipWriter", "select_writer"]

log = logging.getLogger(__name__)


@runtime_checkable
class PptxWriter(Protocol):
    """Contract every writer honours (docs/arch/04 §6)."""

    name: str

    def write(
        self,
        plan: Plan,
        profile: TemplateProfile,
        out: Path,
        *,
        keep_notes: bool = False,
    ) -> BuildResult:
        """Write ``plan`` to ``out`` and report what was built.

        ``keep_notes`` is an addition to the documented signature: it forwards
        the ``--keep-notes`` / ``LYRICS2PPTX_KEEP_NOTES`` decision so both
        writers honour AD-08 without the builder reaching into their internals.
        """
        ...


class PartsWriter:
    """Method A writer: python-pptx part registration."""

    name = "parts"

    def write(
        self,
        plan: Plan,
        profile: TemplateProfile,
        out: Path,
        *,
        keep_notes: bool = False,
    ) -> BuildResult:
        return write_via_parts(plan, profile, out, keep_notes=keep_notes)


class ZipWriter:
    """Fallback F1 writer: direct OPC zip rewrite."""

    name = "zip"

    def write(
        self,
        plan: Plan,
        profile: TemplateProfile,
        out: Path,
        *,
        keep_notes: bool = False,
    ) -> BuildResult:
        return write_via_zip(plan, profile, out, keep_notes=keep_notes)


def select_writer(strategy: str = "auto") -> PptxWriter:
    """Return the writer for ``strategy``.

    ``auto`` returns the parts writer when the capability probe succeeds and the
    zip writer otherwise. ``parts``/``zip`` force a writer without probing.

    Raises:
        ConfigError: ``strategy`` is not one of ``config.CLONE_STRATEGIES``.
    """
    if strategy not in config.CLONE_STRATEGIES:
        allowed = "/".join(config.CLONE_STRATEGIES)
        raise ConfigError(f"알 수 없는 복제 전략입니다: {strategy!r} ({allowed} 중 하나)")

    if strategy == "parts":
        return PartsWriter()
    if strategy == "zip":
        return ZipWriter()

    can_register = pptx_can_register_slide_part()
    log.info("clone strategy=auto (probe=%s)", can_register)
    return PartsWriter() if can_register else ZipWriter()
