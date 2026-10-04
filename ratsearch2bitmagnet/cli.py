"""Command line: pick a source and a sink, stream batches between them, report."""

from __future__ import annotations

import argparse
import logging
import os
import re
import signal
import sys
import time

from . import __version__
from .items import DEFAULT_FILES_THRESHOLD, DEFAULT_SOURCE_NAME, Options
from .model import HASH_RE, SCHEMA_LEGACY, SCHEMA_UNKNOWN, SCHEMA_V2, is_bad
from .progress import DONE, FAILED, INTERRUPTED, Counters, Outcome, Reporter, RunInfo, create_reporter
from .sinks import Sink
from .sinks.http_import import DEFAULT_BITMAGNET_URL, HttpSink
from .sinks.jsonl import JsonlSink
from .sinks.postgres import PostgresSink
from .sources import Source
from .sources.datadir import DataDirSource, default_data_dir
from .sources.manticore import DEFAULT_MANTICORE_PORT, MAX_BATCH_SIZE, ManticoreSource
from .sources.ratsdb import RatsdbSource

log = logging.getLogger(__name__)


def detect_source_kind(source: str) -> str:
    if os.path.isdir(source):
        return "data-dir"
    if os.path.isfile(source):
        return "ratsdb"
    if source.endswith(".ratsdb") or os.sep in source:
        raise SystemExit(f"{source}: no such file or directory")
    if re.match(r"^\[?[\w.\-:]+\]?(:\d+)?$", source):
        return "manticore"
    raise SystemExit(f"{source}: not a .ratsdb file, a data directory or a host:port")


def split_host_port(value: str) -> tuple[str, int]:
    match = re.match(r"^\[(.+)\]:(\d+)$", value) or (re.match(r"^([^:]+):(\d+)$", value))
    if match:
        return match.group(1), int(match.group(2))
    return value.strip("[]") or "127.0.0.1", DEFAULT_MANTICORE_PORT


def open_source(args: argparse.Namespace) -> Source:
    kind = args.source_type
    source = args.source
    if source is None:
        source = default_data_dir()
        if not os.path.isdir(source):
            raise SystemExit(f"no SOURCE given and the default data directory {source} does not exist")
    if kind == "auto":
        kind = detect_source_kind(source)
    if kind == "ratsdb":
        return RatsdbSource(source)
    if kind == "manticore":
        host, port = split_host_port(source)
        return ManticoreSource(host, port, args.batch_size, args.after_id, args.manticore_timeout)
    return DataDirSource(source, args.searchd, args.batch_size, args.after_id,
                         args.manticore_timeout, args.searchd_timeout)


def open_sink(args: argparse.Namespace) -> Sink:
    chosen = [name for name in ("bitmagnet", "postgres", "output") if getattr(args, name)]
    if len(chosen) > 1:
        raise SystemExit("choose one of --bitmagnet, --postgres or --output")
    if args.bitmagnet:
        return HttpSink(args.bitmagnet, args.http_timeout, args.retries)
    if args.postgres:
        return PostgresSink(args.postgres)
    return JsonlSink(args.output, args.split_size)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ratsearch2bitmagnet",
        description="Import a Rats Search (rats-search) torrent index into bitmagnet.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""\
SOURCE is one of:
  index.ratsdb        a dump exported by rats-search 2.x
  HOST[:PORT]         the Manticore Search of a running rats-search (port {DEFAULT_MANTICORE_PORT})
  /path/to/data-dir   a rats-search data directory (default: {default_data_dir()})

examples:
  %(prog)s index.ratsdb --bitmagnet http://localhost:3333/import
  %(prog)s 127.0.0.1:9306 --postgres postgresql://postgres:postgres@localhost:5432/bitmagnet
  %(prog)s "~/.local/share/Rats Search" --searchd ./searchd --output rats.jsonl --split-size 100000
  %(prog)s index.ratsdb | curl -H 'Content-Type: application/json' --data-binary @- http://localhost:3333/import
""")
    parser.add_argument("source", nargs="?", help="what to read (see below); defaults to the data directory")
    parser.add_argument("--source-type", choices=("auto", "ratsdb", "manticore", "data-dir"), default="auto",
                        help="how to interpret SOURCE (default: detect)")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")

    sink = parser.add_argument_group("destination (default: JSON lines on stdout)")
    sink.add_argument("--bitmagnet", metavar="URL", nargs="?", const=DEFAULT_BITMAGNET_URL,
                      help=f"POST to bitmagnet's /import endpoint (default URL: {DEFAULT_BITMAGNET_URL})")
    sink.add_argument("--postgres", metavar="DSN",
                      help="write straight into bitmagnet's PostgreSQL database, file lists included "
                           "(e.g. postgresql://postgres:postgres@localhost:5432/bitmagnet)")
    sink.add_argument("-o", "--output", metavar="FILE", help="write JSON lines to FILE instead of stdout")
    sink.add_argument("--split-size", type=int, metavar="N", help="with --output: start a new file every N torrents")

    content = parser.add_argument_group("what is imported")
    content.add_argument("--source-name", default=DEFAULT_SOURCE_NAME,
                         help="torrent source key shown in bitmagnet (default: %(default)s)")
    content.add_argument("--import-id", help="import id recorded with each torrent (default: rats-search-<timestamp>)")
    content.add_argument("--schema", choices=("auto", SCHEMA_V2, SCHEMA_LEGACY), default="auto",
                         help="content-type numbering of the source: rats-search 2.x or 1.x (default: detect)")
    content.add_argument("--no-hints", action="store_true", help="do not pass rats-search content types to bitmagnet")
    content.add_argument("--keep-bad", action="store_true", help="also import torrents rats-search classified as 'bad'")
    content.add_argument("--no-files", action="store_true", help="--postgres: do not store file lists")
    content.add_argument("--files-threshold", type=int, default=DEFAULT_FILES_THRESHOLD, metavar="N",
                         help="--postgres: keep at most N files per torrent, like bitmagnet's crawler "
                              "(default: %(default)s)")
    content.add_argument("--no-peers", action="store_true", help="--postgres: do not copy seeder/leecher counts")
    content.add_argument("--no-queue", action="store_true",
                         help="--postgres: do not queue classification jobs (run 'bitmagnet reprocess' yourself)")

    flow = parser.add_argument_group("batching and resuming")
    flow.add_argument("--batch-size", type=int, default=500, metavar="N",
                      help=f"torrents per read page and per write batch, at most {MAX_BATCH_SIZE} "
                           "(default: %(default)s)")
    flow.add_argument("--skip", type=int, default=0, metavar="N", help="skip the first N torrents")
    flow.add_argument("--after-id", type=int, default=0, metavar="ID",
                      help="Manticore sources: continue after this row id (printed on failure)")
    flow.add_argument("--limit", type=int, metavar="N", help="stop after N torrents")
    flow.add_argument("--retries", type=int, default=5, metavar="N",
                      help="--bitmagnet: retries per request (default: %(default)s)")
    flow.add_argument("--http-timeout", type=float, default=600.0, metavar="SEC",
                      help="--bitmagnet: request timeout (default: %(default)s)")
    flow.add_argument("--manticore-timeout", type=float, default=30.0, metavar="SEC",
                      help="Manticore connect timeout (default: %(default)s)")

    searchd = parser.add_argument_group("data directory sources")
    searchd.add_argument("--searchd", metavar="PATH", help="Manticore 'searchd' binary to start on the data directory")
    searchd.add_argument("--searchd-timeout", type=float, default=600.0, metavar="SEC",
                         help="how long searchd may take to load the tables (default: %(default)s)")

    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="only warnings and errors")
    parser.add_argument("--no-progress", action="store_true",
                        help="plain log lines instead of the live progress display")
    return parser


def validate(args: argparse.Namespace) -> None:
    if args.batch_size < 1 or args.batch_size > MAX_BATCH_SIZE:
        raise SystemExit(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}")
    if args.split_size is not None and args.split_size < 1:
        raise SystemExit("--split-size must be positive")
    if args.split_size and not args.output:
        raise SystemExit("--split-size needs --output")
    if args.files_threshold < 0:
        raise SystemExit("--files-threshold must not be negative")
    if args.skip < 0 or args.after_id < 0 or (args.limit is not None and args.limit < 1):
        raise SystemExit("--skip, --after-id must not be negative and --limit must be positive")
    if not args.source_name.strip():
        raise SystemExit("--source-name must not be empty")



HINT_TEXT = {
    SCHEMA_V2: "rats-search 2.x content types",
    SCHEMA_LEGACY: "rats-search 1.x content types",
    SCHEMA_UNKNOWN: "numbering undetermined; only content types that rats-search 1.x and 2.x "
                    "number alike (--schema overrides)",
}


def wants_live_display(args: argparse.Namespace) -> bool:
    if args.no_progress or args.quiet or not sys.stderr.isatty():
        return False
    # JSON lines printed to the same terminal would tear through the display.
    to_stdout = not (args.bitmagnet or args.postgres or args.output)
    return not (to_stdout and sys.stdout.isatty())


def resume_options(counters: Counters, source: Source) -> str:
    """Options that continue a stopped run; empty when nothing was written yet."""
    if not counters.consumed:
        return ""
    if counters.last_id and source.supports_after_id:
        return f"--after-id {counters.last_id}"
    return f"--skip {counters.consumed}"


def run(args: argparse.Namespace, reporter: Reporter) -> int:
    opts = Options(
        source_name=args.source_name.strip(),
        import_id=args.import_id or f"rats-search-{int(time.time())}",
        hints=not args.no_hints,
        files=not args.no_files,
        files_threshold=args.files_threshold,
        peers=not args.no_peers,
        queue=not args.no_queue,
    )
    source: Source | None = None
    try:
        with reporter.status("Opening the source"):
            source = open_source(args)
            total = source.total_estimate()
    except Exception as exc:  # noqa: BLE001 - reported, not propagated
        if source is not None:
            source.close()
        log.error("cannot open the source: %s", exc)
        return 1
    try:
        if args.after_id and not source.supports_after_id:
            raise SystemExit("--after-id only applies to Manticore and data directory sources")
        schema = args.schema if args.schema != "auto" else source.schema
        with reporter.status("Opening the destination"):
            sink = open_sink(args)
    except SystemExit:
        source.close()
        raise
    except Exception as exc:  # noqa: BLE001 - reported, not propagated
        source.close()
        log.error("cannot open the destination: %s", exc)
        return 1

    counters = Counters(last_id=args.after_id)
    outcome = Outcome()
    to_skip = args.skip
    hints = HINT_TEXT[schema] if opts.hints else "off"
    reporter.begin(RunInfo(source.describe(), sink.describe(), hints, total, args.limit))
    try:
        try:
            for batch in source.batches():
                dropped = min(to_skip, len(batch))
                to_skip -= dropped
                items = []
                invalid = bad = 0
                for t in batch[dropped:]:
                    if not HASH_RE.match(t.hash):
                        invalid += 1
                        continue
                    if not args.keep_bad and is_bad(t):
                        bad += 1
                        continue
                    items.append(t)
                    if args.limit and counters.written + len(items) >= args.limit:
                        break
                # Dump frames are 500 torrents regardless of --batch-size; split
                # (never merge) so a page is fully written before it is counted.
                for start in range(0, len(items), args.batch_size):
                    sink.write(items[start:start + args.batch_size], schema, opts)
                # Counted only now: after a failure the counters describe the
                # last fully written batch, which is what resuming needs.
                counters.written += len(items)
                counters.consumed += len(batch)
                counters.invalid += invalid
                counters.bad += bad
                if batch[-1].row_id:
                    counters.last_id = batch[-1].row_id
                reporter.update(counters, sink.stats())
                if args.limit and counters.written >= args.limit:
                    break
        except KeyboardInterrupt:
            outcome = Outcome(INTERRUPTED, resume=resume_options(counters, source))
        except Exception as exc:  # noqa: BLE001 - reported with the resume point
            outcome = Outcome(FAILED, error=str(exc), resume=resume_options(counters, source))
        finally:
            source.close()
        sink.close()
        if outcome.status == DONE:
            outcome.notes = source.summary()
        reporter.end(outcome, counters, sink.stats())
    finally:
        reporter.close()
    return {DONE: 0, INTERRUPTED: 130, FAILED: 1}[outcome.status]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate(args)
    level = logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO
    reporter = create_reporter(live=wants_live_display(args))
    handler = reporter.log_handler()
    if handler is None:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    logging.basicConfig(level=level, handlers=[handler])
    logging.getLogger(__package__).setLevel(level)

    def interrupt(*_: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        return run(args, reporter)
    except KeyboardInterrupt:
        log.error("interrupted")
        return 130
