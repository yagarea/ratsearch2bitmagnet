"""How a run is shown to the person watching it.

The CLI hands a reporter the run's counters after every written batch. The
plain reporter here turns them into periodic log lines, which is what a log
file or a pipe wants; ``progress_rich`` draws a live display for terminals.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger(__name__)

DONE = "done"
INTERRUPTED = "interrupted"
FAILED = "failed"


@dataclass
class RunInfo:
    source: str
    destination: str
    hints: str  # which content-type numbering the hints are read with, in words
    total: int | None  # torrents in the source, as far as it knows
    limit: int | None = None  # the run stops once this many are written


@dataclass
class Counters:
    consumed: int = 0  # source torrents read, counted once their batch is written
    written: int = 0
    bad: int = 0
    invalid: int = 0
    last_id: int = 0


@dataclass
class Outcome:
    status: str = DONE
    error: str = ""
    resume: str = ""  # command-line options that continue the run where it stopped
    notes: list[str] = field(default_factory=list)


class Reporter(Protocol):
    # The handler log records must go through while this reporter owns the
    # terminal, or None for the default stderr handler.
    def log_handler(self) -> logging.Handler | None: ...

    # Wraps a step that may take long before any torrent is read.
    def status(self, message: str) -> contextlib.AbstractContextManager[object]: ...

    def begin(self, info: RunInfo) -> None: ...

    def update(self, counters: Counters, sink_stats: dict[str, int]) -> None: ...

    def end(self, outcome: Outcome, counters: Counters, sink_stats: dict[str, int]) -> None: ...

    # Releases the terminal; safe to call more than once and without begin().
    def close(self) -> None: ...


def format_duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


class LogReporter:
    """Progress as log lines: one every few seconds and a one-line summary."""

    interval = 5.0

    def __init__(self) -> None:
        self.info: RunInfo | None = None
        self.started = self.last_report = time.monotonic()

    def log_handler(self) -> logging.Handler | None:
        return None

    @contextlib.contextmanager
    def status(self, message: str) -> Generator[None]:
        log.debug("%s", message)
        yield

    def begin(self, info: RunInfo) -> None:
        self.info = info
        self.started = self.last_report = time.monotonic()
        log.info("reading %s", info.source)
        log.info("writing %s", info.destination)
        if info.total:
            log.info("about %s torrents to read", f"{info.total:,}")
        if info.limit:
            log.info("stopping after %s written", f"{info.limit:,}")
        log.info("content-type hints: %s", info.hints)

    def update(self, counters: Counters, sink_stats: dict[str, int]) -> None:
        now = time.monotonic()
        if now - self.last_report < self.interval and not log.isEnabledFor(logging.DEBUG):
            return
        self.last_report = now
        total = self.info.total if self.info else None
        position = f"{counters.consumed:,}" + (f" / {total:,}" if total else "")
        rate = counters.consumed / max(now - self.started, 1e-6)
        last = f", last row id {counters.last_id}" if counters.last_id else ""
        log.info("%s torrents, %.0f/s%s", position, rate, last)

    def end(self, outcome: Outcome, counters: Counters, sink_stats: dict[str, int]) -> None:
        if outcome.status != DONE:
            if outcome.error:
                log.error("%s", outcome.error)
            resume = f"; resume with {outcome.resume}" if outcome.resume else ""
            log.error("%s after %s torrents%s", outcome.status, f"{counters.consumed:,}", resume)
            return
        elapsed = time.monotonic() - self.started
        summary = [f"{counters.written:,} torrents written in {elapsed:.0f}s"]
        if counters.invalid:
            summary.append(f"{counters.invalid:,} without a valid infohash skipped")
        if counters.bad:
            summary.append(f"{counters.bad:,} classified 'bad' skipped")
        if sink_stats:
            summary.append(", ".join(f"{value:,} {label}" for label, value in sink_stats.items()))
        log.info("; ".join(summary + outcome.notes))

    def close(self) -> None:
        pass


def create_reporter(live: bool) -> Reporter:
    """A live terminal display when asked for and rich is importable, log lines otherwise."""
    if live:
        try:
            from .progress_rich import RichReporter
        except ImportError:
            pass
        else:
            return RichReporter()
    return LogReporter()
