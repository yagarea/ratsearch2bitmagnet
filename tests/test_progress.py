import io
import logging

import pytest
from rich.console import Console

from ratsearch2bitmagnet import cli
from ratsearch2bitmagnet.progress import (
    FAILED,
    Counters,
    LogReporter,
    Outcome,
    RunInfo,
    create_reporter,
    format_duration,
)
from ratsearch2bitmagnet.progress_rich import RichReporter

INFO = RunInfo("dump x.ratsdb", "JSON lines in out.jsonl", "rats-search 2.x content types", 1200)
FINAL = Counters(consumed=1200, written=1198, bad=1, invalid=1, last_id=42)


def recording(terminal: bool = False) -> Console:
    return Console(file=io.StringIO(), width=120, force_terminal=terminal, color_system=None)


def output(reporter: RichReporter) -> str:
    file = reporter.console.file
    assert isinstance(file, io.StringIO)
    return file.getvalue()


class FakeStream:
    def __init__(self, tty: bool):
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def parse(*argv: str):
    return cli.build_parser().parse_args(list(argv))


def test_format_duration():
    assert format_duration(0) == "0:00:00"
    assert format_duration(3725.9) == "1:02:05"


def test_create_reporter():
    assert isinstance(create_reporter(False), LogReporter)
    assert isinstance(create_reporter(True), RichReporter)


# ---------------------------------------------------------------------------
# plain log lines


def test_log_reporter(caplog):
    caplog.set_level(logging.INFO, logger="ratsearch2bitmagnet")
    reporter = LogReporter()
    reporter.interval = 0
    reporter.begin(INFO)
    reporter.update(Counters(consumed=500, written=498, last_id=42), {"file rows": 9})
    reporter.end(Outcome(notes=["a note"]), FINAL, {"file rows": 9})
    assert "reading dump x.ratsdb" in caplog.text and "writing JSON lines in out.jsonl" in caplog.text
    assert "about 1,200 torrents to read" in caplog.text
    assert "500 / 1,200 torrents" in caplog.text and "last row id 42" in caplog.text
    assert "1,198 torrents written" in caplog.text and "9 file rows; a note" in caplog.text


def test_log_reporter_failure(caplog):
    reporter = LogReporter()
    reporter.end(Outcome(FAILED, error="boom", resume="--skip 500"), Counters(consumed=500), {})
    assert "boom" in caplog.text and "failed after 500 torrents; resume with --skip 500" in caplog.text


# ---------------------------------------------------------------------------
# rich display


def test_rich_header_and_summary():
    reporter = RichReporter(recording())
    reporter.begin(INFO)
    reporter.update(Counters(consumed=500, written=498), {"file rows": 4})
    reporter.end(Outcome(notes=["the dump was truncated"]), FINAL, {"file rows": 9})
    text = output(reporter)
    for expected in ("dump x.ratsdb", "JSON lines in out.jsonl", "rats-search 2.x content types", "1,200",
                     "Import finished", "1,198", "skipped as bad", "invalid infohash", "file rows",
                     "average rate", "the dump was truncated"):
        assert expected in text
    assert "resume with" not in text


def test_rich_failure_summary():
    reporter = RichReporter(recording())
    reporter.begin(INFO)
    reporter.end(Outcome(FAILED, error="HTTP 400: nope", resume="--after-id 42"), Counters(consumed=500), {})
    text = output(reporter)
    assert "Import failed" in text and "HTTP 400: nope" in text and "--after-id 42" in text


def test_rich_live_block_and_close():
    reporter = RichReporter(recording(terminal=True))
    reporter.begin(INFO)
    reporter.update(Counters(consumed=600, written=598, bad=1, invalid=1, last_id=42), {"file rows": 9})
    assert reporter.live is not None
    reporter.live.refresh()
    text = output(reporter)
    for expected in ("Importing", "50%", "600 / 1,200", "written 598", "skipped as bad 1", "file rows 9",
                     "last row id 42"):
        assert expected in text
    reporter.close()
    reporter.close()  # releasing the terminal twice is harmless
    assert reporter.live is None


def test_rich_bar_never_overshoots_the_estimate():
    reporter = RichReporter(recording())
    reporter.begin(RunInfo("s", "d", "off", 100))
    reporter.update(Counters(consumed=150), {})
    assert reporter.progress.tasks[0].completed == 100
    reporter.close()


def test_rich_bar_follows_written_torrents_under_a_limit():
    reporter = RichReporter(recording())
    reporter.begin(RunInfo("s", "d", "off", 1000, limit=50))
    reporter.update(Counters(consumed=40, written=30), {})
    task = reporter.progress.tasks[0]
    assert (task.completed, task.total) == (30, 50)
    reporter.close()
    assert "1,000, stopping after 50 written" in output(reporter)


def test_rich_log_handler_writes_to_the_display_console():
    reporter = RichReporter(recording())
    handler = reporter.log_handler()
    assert handler is not None
    handler.handle(logging.LogRecord("x", logging.WARNING, __file__, 1, "retrying [soon]", None, None))
    assert "retrying [soon]" in output(reporter)


# ---------------------------------------------------------------------------
# how the CLI picks and drives a reporter


@pytest.mark.parametrize("argv,stderr_tty,stdout_tty,expected", [
    (["x", "--output", "o.jsonl"], True, True, True),
    (["x", "--postgres", "dsn"], True, False, True),
    (["x"], True, False, True),             # JSON lines piped away, display on stderr
    (["x"], True, True, False),             # JSON lines on the same terminal
    (["x", "--output", "o.jsonl"], False, True, False),
    (["x", "--output", "o.jsonl", "--no-progress"], True, True, False),
    (["x", "--output", "o.jsonl", "-q"], True, True, False),
])
def test_wants_live_display(monkeypatch, argv, stderr_tty, stdout_tty, expected):
    monkeypatch.setattr(cli.sys, "stderr", FakeStream(stderr_tty))
    monkeypatch.setattr(cli.sys, "stdout", FakeStream(stdout_tty))
    assert cli.wants_live_display(parse(*argv)) is expected


def test_run_with_rich_reporter(dump, tmp_path):
    reporter = RichReporter(recording())
    assert cli.run(parse(str(dump), "--output", str(tmp_path / "o.jsonl")), reporter) == 0
    text = output(reporter)
    for expected in ("Import finished", "1,200", "1,198", "skipped as bad", "invalid infohash", "output files"):
        assert expected in text
    assert reporter.live is None


def test_failed_run_with_rich_reporter(dump, import_server):
    import_server.statuses = [400]
    reporter = RichReporter(recording())
    assert cli.run(parse(str(dump), "--bitmagnet", import_server.url), reporter) == 1
    text = output(reporter)
    assert "Import failed" in text and "HTTP 400" in text
    assert "resume with" not in text  # nothing was written, so there is nothing to skip
    assert reporter.live is None


def test_failed_run_names_the_resume_point(dump, import_server):
    import_server.statuses = [200, 400]
    reporter = RichReporter(recording())
    assert cli.run(parse(str(dump), "--bitmagnet", import_server.url), reporter) == 1
    assert "--skip 500" in output(reporter)  # the first dump frame went through, the second did not


def test_json_lines_stay_on_stdout_under_the_live_display(dump, capsys):
    reporter = RichReporter(recording(terminal=True))
    assert cli.run(parse(str(dump)), reporter) == 0
    assert len(capsys.readouterr().out.splitlines()) == 1198
    assert "infoHash" not in output(reporter)


def test_rich_unknown_total():
    reporter = RichReporter(recording(terminal=True))
    reporter.begin(RunInfo("s", "d", "off", None))
    reporter.update(Counters(consumed=700, written=700), {})
    assert reporter.live is not None
    reporter.live.refresh()
    reporter.end(Outcome(), Counters(consumed=700, written=700), {})
    text = output(reporter)
    assert "unknown" in text and "700" in text and "Import finished" in text
