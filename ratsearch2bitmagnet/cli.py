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


def run(args: argparse.Namespace) -> int:
    opts = Options(
        source_name=args.source_name.strip(),
        import_id=args.import_id or f"rats-search-{int(time.time())}",
        hints=not args.no_hints,
        files=not args.no_files,
        files_threshold=args.files_threshold,
        peers=not args.no_peers,
        queue=not args.no_queue,
    )
    try:
        source = open_source(args)
    except Exception as exc:  # noqa: BLE001 - reported, not propagated
        log.error("cannot open the source: %s", exc)
        return 1
    try:
        if args.after_id and not source.supports_after_id:
            raise SystemExit("--after-id only applies to Manticore and data directory sources")
        schema = args.schema if args.schema != "auto" else source.schema
        log.info("reading %s", source.describe())
        total = source.total_estimate()
        if total:
            log.info("about %s torrents in the source", f"{total:,}")
        if opts.hints:
            if schema == SCHEMA_UNKNOWN:
                log.info("content-type numbering could not be determined; only hints valid for both "
                         "rats-search 1.x and 2.x are passed on (use --schema to override)")
            else:
                log.info("content-type numbering: rats-search %s", "2.x" if schema == SCHEMA_V2 else "1.x")
        sink = open_sink(args)
    except SystemExit:
        source.close()
        raise
    except Exception as exc:  # noqa: BLE001 - reported, not propagated
        source.close()
        log.error("cannot open the destination: %s", exc)
        return 1

    # `consumed` counts source torrents of fully written batches, so the
    # resume hints after a failure name exactly where to continue.
    processed = consumed = invalid = bad = 0
    to_skip = args.skip
    last_id = args.after_id
    started = time.monotonic()
    last_report = started
    try:
        for batch in source.batches():
            dropped = min(to_skip, len(batch))
            to_skip -= dropped
            items = []
            for t in batch[dropped:]:
                if not HASH_RE.match(t.hash):
                    invalid += 1
                    continue
                if not args.keep_bad and is_bad(t):
                    bad += 1
                    continue
                items.append(t)
                if args.limit and processed + len(items) >= args.limit:
                    break
            # Dump frames are 500 torrents regardless of --batch-size; split
            # (never merge) so a page is fully written before it counts as consumed.
            for start in range(0, len(items), args.batch_size):
                sink.write(items[start:start + args.batch_size], schema, opts)
            processed += len(items)
            consumed += len(batch)
            if batch[-1].row_id:
                last_id = batch[-1].row_id
            now = time.monotonic()
            if now - last_report >= 5 or log.isEnabledFor(logging.DEBUG):
                last_report = now
                rate = processed / max(now - started, 1e-6)
                position = f"{consumed:,}" + (f" / {total:,}" if total else "")
                log.info("%s torrents, %.0f/s%s", position, rate, f", last row id {last_id}" if last_id else "")
            if args.limit and processed >= args.limit:
                break
    except KeyboardInterrupt:
        log.error("interrupted after %s torrents%s", f"{consumed:,}", resume_hint(consumed, last_id, source))
        sink.close()
        return 130
    except Exception as exc:
        log.error("%s", exc)
        log.error("failed after %s torrents%s", f"{consumed:,}", resume_hint(consumed, last_id, source))
        sink.close()
        return 1
    finally:
        source.close()
    sink.close()

    elapsed = time.monotonic() - started
    summary = [f"{processed:,} torrents written in {elapsed:.0f}s"]
    if invalid:
        summary.append(f"{invalid:,} without a valid infohash skipped")
    if bad:
        summary.append(f"{bad:,} classified 'bad' skipped")
    summary += sink.summary() + source.summary()
    log.info("; ".join(summary))
    return 0


def resume_hint(count: int, last_id: int, source: Source) -> str:
    if last_id and source.supports_after_id:
        return f"; resume with --after-id {last_id} or --skip {count}"
    return f"; resume with --skip {count}"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stderr)
    logging.getLogger(__package__).setLevel(level)
    validate(args)

    def interrupt(*_: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    return run(args)
