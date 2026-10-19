"""In-process progress reporting for long collects (L1, presentation only).

A ``blog`` search fetches several pages with a politeness delay between them, so
it can take ten seconds or more. The web UI shows a progress bar while it waits,
and only the collector knows *how far along* the work is — so the collector
publishes short human-readable lines and the entry point decides what to do with
them.

Two rules keep this module harmless:

* **Reporting is a hint.** :func:`report` never raises, never blocks, and never
  changes a code path. With no sink installed (CLI, tests, library use) it costs
  one attribute lookup.
* **The sink is per thread.** The web server handles each request in its own
  thread (``ThreadingHTTPServer``), so two concurrent searches cannot mix their
  lines. ``set_sink(None)`` clears it again when the work is done.

Optional ``done``/``total`` numbers let the UI turn an indeterminate bar into a
real one ("가사 페이지 3/5") without inventing percentages that do not exist
(docs/arch/07 관측성).
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

__all__ = ["ProgressUpdate", "ProgressSink", "report", "set_sink", "current_sink"]

log = logging.getLogger(__name__)


class ProgressUpdate:
    """One progress line: a message plus an optional ``done``/``total`` pair."""

    __slots__ = ("message", "done", "total")

    def __init__(self, message: str, done: int | None = None, total: int | None = None) -> None:
        self.message = message
        self.done = done
        self.total = total

    @property
    def has_fraction(self) -> bool:
        return self.total is not None and self.total > 0 and self.done is not None

    @property
    def percent(self) -> int | None:
        if not self.has_fraction:
            return None
        assert self.done is not None and self.total is not None  # for type checkers
        return max(0, min(100, round(self.done * 100 / self.total)))

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"ProgressUpdate({self.message!r}, {self.done}/{self.total})"


ProgressSink = Callable[[ProgressUpdate], None]

_local = threading.local()


def set_sink(sink: ProgressSink | None) -> None:
    """Install (or clear) the progress sink for the **current thread**."""
    _local.sink = sink


def current_sink() -> ProgressSink | None:
    """Return the sink installed for the current thread, if any."""
    return getattr(_local, "sink", None)


def report(message: str, *, done: int | None = None, total: int | None = None) -> None:
    """Publish a progress line to the current thread's sink, if one is installed."""
    sink = getattr(_local, "sink", None)
    if sink is None:
        return
    try:
        sink(ProgressUpdate(message, done, total))
    except Exception:  # pragma: no cover - a sink must never break a collect
        log.debug("progress sink raised; ignoring", exc_info=True)
