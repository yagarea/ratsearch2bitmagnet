# ratsearch2bitmagnet (Experimental)

Moves the torrent index of [Rats Search](https://github.com/DEgITx/rats-search)
into [bitmagnet](https://bitmagnet.io).

| It reads | It writes |
|---|---|
| a `.ratsdb` dump exported by rats-search 2.x | bitmagnet's HTTP `/import` endpoint |
| the Manticore Search behind a running rats-search (`host:port`) | bitmagnet's PostgreSQL database directly, file lists included |
| a rats-search data directory (attaches to the running instance, or starts its own `searchd`) | JSON lines in the `/import` format, for `curl` |

## Running it

With [uv](https://docs.astral.sh/uv/), from a checkout:

```sh
uv run ratsearch2bitmagnet --help
```

Or install it (`pip install .`, Python 3.10 or newer) and run `ratsearch2bitmagnet`, or
`python -m ratsearch2bitmagnet`. Its dependencies come with it and each is only loaded for the
feature that needs it: `pymysql` for Manticore, `psycopg` for PostgreSQL, `rich` for the progress
display.

## Quick start

```sh
# A running rats-search, straight into bitmagnet's database (keeps the file lists):
ratsearch2bitmagnet 127.0.0.1:9306 --postgres postgresql://postgres:postgres@localhost:5432/bitmagnet

# A dump, through bitmagnet's import endpoint:
ratsearch2bitmagnet index.ratsdb --bitmagnet http://localhost:3333/import

# The data directory of a rats-search that is not running:
ratsearch2bitmagnet "~/.local/share/Rats Search" --searchd /path/to/rats-search/searchd --postgres postgresql://...

# JSON lines, the way the bitmagnet import guide pipes them:
ratsearch2bitmagnet index.ratsdb | curl -H 'Content-Type: application/json' -H 'Connection: close' \
    --data-binary @- http://localhost:3333/import
```

## Sources

**`.ratsdb` dump.** Export one from rats-search 2.x (the database export in the UI, or
`GET http://localhost:8095/api/database.export?path=/some/where/index.ratsdb` with the REST API
enabled). A dump cut short by an interrupted export is still read up to the cut.

**`HOST[:PORT]`.** The Manticore Search that rats-search runs; it listens on `127.0.0.1:9306`
unless that port was busy, in which case the port is in `sphinx.conf` of the data directory. Works
for rats-search 2.x and the legacy 1.x Electron version alike. Only `SELECT`s are issued.

**Data directory.** The default locations are `~/.local/share/Rats Search` (Linux),
`~/Library/Application Support/Rats Search` (macOS) and `%APPDATA%\Rats Search` (Windows); run
without a `SOURCE` to use it. If a `searchd` is serving the directory, the script attaches to it.
Otherwise it starts one with the same configuration rats-search generates, reads, and stops it.
That needs the Manticore `searchd` binary; use the one that came with your rats-search (in the
source tree: `imports/linux/x64/searchd`; the release packages bundle it) and pass it with
`--searchd`, or have it on `PATH`. Starting `searchd` on the directory is exactly what rats-search
does at start-up, so it has the same effects: the write-ahead log is replayed and flushed into the
tables.

## Destinations

**`--bitmagnet URL`** posts batches to the `/import` endpoint. Each line carries what the endpoint
accepts: infohash, name, size, published date, the `private` flag and a content-type hint. The
endpoint has no way to take file lists, so those are lost on this path. Batches share one
`X-Import-Id`, requests are retried on connection errors and server errors, and the run stops on
the first batch bitmagnet rejects.

**`--postgres DSN`** writes into bitmagnet's database the way bitmagnet's own importer and DHT
crawler do, so the result is indistinguishable from torrents bitmagnet found itself:

| bitmagnet table | what is written |
|---|---|
| `torrents` | infohash, name, size, private, `files_status`, `files_count`; existing rows are kept, except that a row without file information gains it |
| `torrent_files` | the file list, up to `--files-threshold` files (100, bitmagnet's own default); longer lists are marked `over_threshold` like the crawler does |
| `torrent_sources`, `torrents_torrent_sources` | the source (`--source-name`, default `rats-search`), the rats-search "added" time as `published_at`, the import id, and seeder/leecher counts when rats-search had scraped a tracker (`--no-peers` leaves them out) |
| `torrent_hints` | the content-type hint, never overwriting a hint that already points at a matched title |
| `queue_jobs` | one `process_torrent` job per 100 new torrents, so bitmagnet classifies them and they show up in the UI (`--no-queue` skips this; `bitmagnet reprocess` does the same later) |

bitmagnet must have run at least once against the database so its migrations exist. Rerunning an
import is safe: nothing is duplicated and nothing is queued twice.

**`--output FILE`** (or no destination at all, for stdout) writes the `/import` JSON lines;
`--split-size N` starts a new file every N torrents.

## What becomes what

| rats-search | bitmagnet |
|---|---|
| `hash` | `infoHash` (lower-case hex; rows without a valid v1 infohash are skipped) |
| `name` | `name` (NUL bytes removed) |
| `size` | `size` |
| `added` | `publishedAt` / `published_at` |
| `info.private` | `private` |
| `contentType` / `contentCategory` | content-type hint, see below |
| `seeders`, `leechers` (when `trackersChecked` is set) | source seeders/leechers (`--postgres` only) |
| file list | `torrent_files` (`--postgres` only) |

Hints are passed only where the rats-search classification maps onto one bitmagnet content type
without guessing. Plain "video" or "audio" gets no hint, because it could be a movie or a show, an
album or an audiobook, and bitmagnet's classifier is better placed to decide. `--no-hints` turns
hints off altogether.

| rats-search 2.x | bitmagnet |
|---|---|
| category movie / series / music / ebook / comics / software / game / xxx | `movie` / `tv_show` / `music` / `ebook` / `comic` / `software` / `game` / `xxx` |
| type books / software / games (no category) | `ebook` / `software` / `game` |
| type video / audio / pictures / archive, category documentary / anime | no hint |

rats-search 1.x numbered its types differently (and knew only the `xxx` category); it maps to
`ebook` (books), `software` (application) and `xxx`. The script tells the two numberings apart from
the data, falls back to the ids both generations agree on when it cannot, and `--schema v2` /
`--schema legacy` overrides the detection. Torrents rats-search classified as `bad` (its junk
filter) are skipped unless `--keep-bad` is given.

## Progress

On a terminal the run is shown live: what is read and where it goes, a bar with the share done, the
rate and the time left, and running counters for what was written, what was skipped and what the
destination did with it. A summary closes the run, and names the option to resume with if it failed
or was interrupted.

```
╭─ ratsearch2bitmagnet ─────────────────────────────────────────╮
│        source  Manticore at 127.0.0.1:9306                    │
│   destination  bitmagnet database bitmagnet on localhost:5432 │
│ content hints  rats-search 2.x content types                  │
│      torrents  9,375,298                                      │
╰───────────────────────────────────────────────────────────────╯
Importing ━━━━━━━━━━━━━━━━━╸━━━━━━━━━━━━━━━━━━━━━━  44% 4,127,000 / 9,375,298 5,214/s 0:13:11 eta 0:16:46
written 4,126,120   skipped as bad 880   invalid infohash 0   new or completed rows 4,126,120
file rows 9,481,007   classification jobs queued 41,262   last row id 4051120833716349511
```

The display is drawn on stderr, so JSON lines on stdout can still be piped. Log lines replace it
when stderr is not a terminal (cron, a log file), when the JSON lines go to the same terminal, with
`-q`, and with `--no-progress`.

## Large imports

Torrents are read in pages of `--batch-size` (500, at most 1000) and written per page. A run that
stops names where to continue: `--after-id ID` for Manticore sources (keyset paging, no rescan)
or `--skip N` for a dump. `--limit N` stops after N torrents, which is handy for a trial run.
Expect bitmagnet's classification of millions of torrents to take a long time afterwards; the import
itself is bound by how fast bitmagnet's database takes the rows.

## Development

The package is laid out by responsibility:

| module | holds |
|---|---|
| `model.py` | the torrent entity, the two content-type numberings and how they map onto bitmagnet's types |
| `items.py` | the `/import` item format and the import options every sink shares |
| `sources/` | one module per input: `ratsdb.py` (dump), `manticore.py` (live index), `datadir.py` (data directory and the private `searchd`) |
| `sinks/` | one module per output: `http_import.py`, `postgres.py`, `jsonl.py` |
| `progress.py` | what a run reports (counters, outcome) and the plain reporter that logs it |
| `progress_rich.py` | the live terminal display |
| `cli.py` | argument parsing, picking a source, a sink and a reporter, the batch loop and the resume point |

Sources yield torrent batches and know their content-type numbering; sinks take a batch, the
numbering and the options, and expose running counters; a reporter is told the counters after every
written batch. Adding an input or output means one new module and one line in `cli.py`.

```sh
uv sync
uv run pytest
```

Tests mirror the modules. The integration tests start a real `searchd` (found in a `rats-search`
checkout next to the repo, or named by `RATSEARCH2BITMAGNET_TEST_SEARCHD`) and, when
`RATSEARCH2BITMAGNET_TEST_PG` holds the DSN of a database bitmagnet has migrated, write to it and
clean up after themselves. Without those they are skipped.

## License

GPL-3.0-or-later, see `LICENSE`.
