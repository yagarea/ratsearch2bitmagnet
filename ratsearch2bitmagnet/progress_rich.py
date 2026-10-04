"""The live terminal display: a progress bar, running counters and a closing summary.

Everything is drawn on stderr, so JSON lines on stdout stay pipeable.
"""

from __future__ import annotations

import contextlib
import logging
import time

from rich.columns import Columns
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    ProgressColumn,
    Task,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from .progress import DONE, FAILED, INTERRUPTED, Counters, Outcome, RunInfo, format_duration

TITLES = {
    DONE: ("Import finished", "green"),
    INTERRUPTED: ("Import interrupted", "yellow"),
    FAILED: ("Import failed", "red"),
}


class CountColumn(ProgressColumn):
    def render(self, task: Task) -> Text:
        total = f" / {int(task.total):,}" if task.total else ""
        return Text(f"{int(task.completed):,}{total}", style="progress.download")


class RateColumn(ProgressColumn):
    def render(self, task: Task) -> Text:
        speed = task.finished_speed or task.speed
        return Text(f"{speed:,.0f}/s" if speed else "-/s", style="progress.data.speed")


def stat(label: str, value: int, style: str) -> Text:
    return Text.assemble((label + " ", "dim"), (f"{value:,}", style if value else "dim"))


class RichReporter:
    def __init__(self, console: Console | None = None):
        self.console = console or Console(stderr=True)
        self.progress = Progress(
            TextColumn("[bold]{task.description}"),
            BarColumn(bar_width=None),
            TaskProgressColumn(),
            CountColumn(),
            RateColumn(),
            TimeElapsedColumn(),
            TextColumn("[dim]eta"),
            TimeRemainingColumn(),
            console=self.console,
        )
        self.task = self.progress.add_task("Importing", total=None, start=False)
        self.live: Live | None = None
        self.counters = Counters()
        self.sink_stats: dict[str, int] = {}
        self.limited = False
        self.started = time.monotonic()

    def log_handler(self) -> logging.Handler | None:
        # Records printed through the display's console appear above it
        # instead of tearing through the bar.
        handler = RichHandler(console=self.console, show_path=False, markup=False,
                              omit_repeated_times=False, log_time_format="%H:%M:%S")
        # RichHandler draws time and level itself; without an explicit formatter
        # logging.basicConfig would add its own "LEVEL:name:" prefix.
        handler.setFormatter(logging.Formatter("%(message)s"))
        return handler

    def _live(self, renderable: RenderableType, refresh_per_second: float) -> Live:
        # stdout carries the JSON lines; rich must never swap it for its proxy,
        # which would print them on the terminal and redraw after every line.
        return Live(renderable, console=self.console, refresh_per_second=refresh_per_second,
                    transient=True, redirect_stdout=False)

    def status(self, message: str) -> contextlib.AbstractContextManager[object]:
        return self._live(Spinner("dots", text=message), 12.5)

    def begin(self, info: RunInfo) -> None:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="dim", justify="right")
        grid.add_column(overflow="fold")
        grid.add_row("source", info.source)
        grid.add_row("destination", info.destination)
        grid.add_row("content hints", info.hints)
        torrents = f"{info.total:,}" if info.total else "unknown"
        if info.limit:
            torrents += f", stopping after {info.limit:,} written"
        grid.add_row("torrents", torrents)
        self.console.print(Panel(grid, title="ratsearch2bitmagnet", title_align="left", border_style="cyan",
                                 expand=False))

        self.started = time.monotonic()
        self.limited = bool(info.limit)
        self.progress.update(self.task, total=info.limit or info.total)
        self.progress.start_task(self.task)
        self.live = self._live(self, 8)
        self.live.start()

    def __rich__(self) -> RenderableType:
        c = self.counters
        stats = [stat("written", c.written, "green"),
                 stat("skipped as bad", c.bad, "yellow"),
                 stat("invalid infohash", c.invalid, "yellow")]
        stats += [stat(label, value, "cyan") for label, value in self.sink_stats.items()]
        if c.last_id:
            stats.append(Text.assemble(("last row id ", "dim"), str(c.last_id)))
        return Group(self.progress, Columns(stats, padding=(0, 3)))

    def update(self, counters: Counters, sink_stats: dict[str, int]) -> None:
        self.counters = counters
        self.sink_stats = sink_stats
        # --limit counts written torrents, so that is what the bar follows then.
        done = counters.written if self.limited else counters.consumed
        total = self.progress.tasks[self.task].total
        # The total is an estimate (rows arrive while a live index is read).
        self.progress.update(self.task, completed=min(done, total) if total else done)

    def end(self, outcome: Outcome, counters: Counters, sink_stats: dict[str, int]) -> None:
        self.close()
        elapsed = time.monotonic() - self.started
        title, color = TITLES[outcome.status]

        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim", justify="right")
        table.add_column(overflow="fold")
        table.add_row("torrents read", f"{counters.consumed:,}")
        table.add_row("written", Text(f"{counters.written:,}", style="bold green"))
        if counters.bad:
            table.add_row("skipped as bad", Text(f"{counters.bad:,}", style="yellow"))
        if counters.invalid:
            table.add_row("invalid infohash", Text(f"{counters.invalid:,}", style="yellow"))
        for label, value in sink_stats.items():
            table.add_row(label, f"{value:,}")
        table.add_row("elapsed", format_duration(elapsed))
        table.add_row("average rate", f"{counters.consumed / max(elapsed, 1e-6):,.0f} torrents/s")
        for note in outcome.notes:
            table.add_row("note", Text(note, style="yellow"))
        if outcome.error:
            table.add_row("error", Text(outcome.error, style="red"))
        if outcome.resume:
            table.add_row("resume with", Text(outcome.resume, style="bold"))
        self.console.print(Panel(table, title=title, title_align="left", border_style=color, expand=False))

    def close(self) -> None:
        if self.live is not None:
            self.live.stop()
            self.live = None
