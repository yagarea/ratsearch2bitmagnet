"""Sink: bitmagnet's PostgreSQL database.

Writes what bitmagnet's importer and DHT crawler write (internal/importer,
internal/dhtcrawler/persist.go): the torrent, its source row, a content-type
hint, the file list, and a "process_torrent" queue job so the classifier
picks the torrents up and they appear in the UI.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from datetime import datetime, timezone

from ..items import Options
from ..model import File, Torrent, hint_for

# Classification jobs carry this many infohashes, like bitmagnet's importer.
QUEUE_JOB_CHUNK = 100
QUEUE_JOB_PRIORITY = 20

REQUIRED_TABLES = ("torrents", "torrent_files", "torrent_sources", "torrents_torrent_sources",
                   "torrent_hints", "queue_jobs")

SQL_TORRENTS = """
INSERT INTO torrents (info_hash, name, size, private, files_status, files_count, created_at, updated_at)
SELECT t.h, t.n, t.s, t.p, t.fs::"FilesStatus", t.fc, %(now)s, %(now)s
FROM unnest(%(h)s::bytea[], %(n)s::text[], %(s)s::bigint[], %(p)s::boolean[], %(fs)s::text[], %(fc)s::integer[])
     AS t(h, n, s, p, fs, fc)
ON CONFLICT (info_hash) DO UPDATE
    SET files_status = EXCLUDED.files_status, files_count = EXCLUDED.files_count, updated_at = EXCLUDED.updated_at
    WHERE torrents.files_status = 'no_info' AND EXCLUDED.files_status <> 'no_info'
RETURNING info_hash
"""

SQL_FILES = """
INSERT INTO torrent_files (info_hash, "index", path, size, created_at, updated_at)
SELECT f.h, f.i, f.p, f.s, %(now)s, %(now)s
FROM unnest(%(h)s::bytea[], %(i)s::integer[], %(p)s::text[], %(s)s::bigint[]) AS f(h, i, p, s)
ON CONFLICT DO NOTHING
"""

SQL_SOURCES = """
INSERT INTO torrents_torrent_sources
    (source, info_hash, import_id, seeders, leechers, published_at, created_at, updated_at)
SELECT %(source)s, s.h, %(import_id)s, s.se, s.le, s.pa, %(now)s, %(now)s
FROM unnest(%(h)s::bytea[], %(se)s::integer[], %(le)s::integer[], %(pa)s::timestamptz[]) AS s(h, se, le, pa)
ON CONFLICT (source, info_hash) DO UPDATE
    SET import_id = EXCLUDED.import_id, seeders = EXCLUDED.seeders, leechers = EXCLUDED.leechers,
        published_at = EXCLUDED.published_at, updated_at = EXCLUDED.updated_at
"""

SQL_HINTS = """
INSERT INTO torrent_hints (info_hash, content_type, created_at, updated_at)
SELECT x.h, x.ct, %(now)s, %(now)s
FROM unnest(%(h)s::bytea[], %(ct)s::text[]) AS x(h, ct)
ON CONFLICT (info_hash) DO UPDATE
    SET content_type = EXCLUDED.content_type, updated_at = EXCLUDED.updated_at
    WHERE torrent_hints.content_id IS NULL
"""

# bitmagnet reads archival_duration back as "hh:mm:ss", which is how PostgreSQL
# prints an interval held in hours; '7 days' would print as "7 days" and make
# the queue worker fail on every job it fetches alongside this one.
SQL_QUEUE = """
INSERT INTO queue_jobs (fingerprint, queue, status, payload, retries, max_retries, run_after,
                        archival_duration, created_at, priority)
VALUES (%s, 'process_torrent', 'pending', %s::jsonb, 0, 2, %s, interval '168 hours', %s, %s)
ON CONFLICT DO NOTHING
"""


def queue_job(hashes: list[str]) -> tuple[str, str]:
    """Payload and fingerprint of a process_torrent job, as bitmagnet computes them."""
    payload = json.dumps({"InfoHashes": hashes}, separators=(",", ":"))
    fingerprint = hashlib.sha256(("process_torrent" + payload).encode("utf-8")).hexdigest()
    return payload, fingerprint


def files_for_bitmagnet(t: Torrent, opts: Options) -> tuple[str, int | None, list[File]]:
    """files_status, files_count and the files to store, following bitmagnet's crawler."""
    if not t.file_list and t.files <= 0:
        return "no_info", None, []
    if not t.file_list:
        return "no_info", t.files, []
    if len(t.file_list) == 1 and t.file_list[0].path == t.name:
        return "single", None, []
    count = max(t.files, len(t.file_list))
    seen: set[str] = set()
    files = []
    for f in t.file_list:
        if f.path and f.path not in seen:
            seen.add(f.path)
            files.append(f)
    if not opts.files:
        return "no_info", count, []
    if len(files) > opts.files_threshold:
        return "over_threshold", count, files[:opts.files_threshold]
    return "multi", count, files


class PostgresSink:
    def __init__(self, dsn: str):
        try:
            import psycopg  # type: ignore
        except ImportError as exc:
            raise SystemExit("writing to PostgreSQL needs the 'psycopg' package "
                             "(pip install 'psycopg[binary]', or run this tool with 'uv run')") from exc
        self.conn = psycopg.connect(dsn, autocommit=False)
        with self.conn.cursor() as cur:
            missing = []
            for table in REQUIRED_TABLES:
                cur.execute("SELECT to_regclass(%s)", (table,))
                if cur.fetchone()[0] is None:
                    missing.append(table)
        self.conn.rollback()
        if missing:
            raise SystemExit("bitmagnet tables missing in the target database: " + ", ".join(missing)
                             + " (start bitmagnet once so it runs its migrations)")
        self.source_ready = False
        self.inserted = 0
        self.queued = 0

    def ensure_source(self, opts: Options) -> None:
        if self.source_ready:
            return
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO torrent_sources (key, name, created_at, updated_at) "
                        "VALUES (%s, %s, now(), now()) ON CONFLICT DO NOTHING",
                        (opts.source_name, opts.source_name))
        self.conn.commit()
        self.source_ready = True

    def write(self, torrents: list[Torrent], schema: str, opts: Options) -> None:
        if not torrents:
            return
        self.ensure_source(opts)
        now = datetime.now(timezone.utc)
        unique: dict[str, Torrent] = {}
        for t in torrents:
            unique.setdefault(t.hash, t)
        rows = list(unique.values())
        plans = {t.hash: files_for_bitmagnet(t, opts) for t in rows}
        hashes = [bytes.fromhex(t.hash) for t in rows]
        try:
            with self.conn.cursor() as cur:
                cur.execute(SQL_TORRENTS, {
                    "now": now,
                    "h": hashes,
                    "n": [t.name for t in rows],
                    "s": [max(t.size, 0) for t in rows],
                    "p": [t.private for t in rows],
                    "fs": [plans[t.hash][0] for t in rows],
                    "fc": [plans[t.hash][1] for t in rows],
                })
                touched = {bytes(r[0]).hex() for r in cur.fetchall()}
                file_rows = [(bytes.fromhex(t.hash), i, f.path, max(f.size, 0))
                             for t in rows if t.hash in touched
                             for i, f in enumerate(plans[t.hash][2])]
                if file_rows:
                    cur.execute(SQL_FILES, {
                        "now": now,
                        "h": [r[0] for r in file_rows],
                        "i": [r[1] for r in file_rows],
                        "p": [r[2] for r in file_rows],
                        "s": [r[3] for r in file_rows],
                    })
                cur.execute(SQL_SOURCES, {
                    "now": now, "source": opts.source_name, "import_id": opts.import_id or None,
                    "h": hashes,
                    "se": [t.seeders if opts.peers and t.scraped else None for t in rows],
                    "le": [t.leechers if opts.peers and t.scraped else None for t in rows],
                    "pa": [datetime.fromtimestamp(t.added, tz=timezone.utc) if t.added > 0 else now for t in rows],
                })
                if opts.hints:
                    hinted = [(bytes.fromhex(t.hash), hint_for(t, schema)) for t in rows]
                    hinted = [(h, c) for h, c in hinted if c]
                    if hinted:
                        cur.execute(SQL_HINTS, {"now": now, "h": [h for h, _ in hinted], "ct": [c for _, c in hinted]})
                if opts.queue and touched:
                    ordered = [t.hash for t in rows if t.hash in touched]
                    jobs = []
                    for i in range(0, len(ordered), QUEUE_JOB_CHUNK):
                        payload, fingerprint = queue_job(ordered[i:i + QUEUE_JOB_CHUNK])
                        jobs.append((fingerprint, payload, now, now, QUEUE_JOB_PRIORITY))
                    cur.executemany(SQL_QUEUE, jobs)
                    self.queued += len(jobs)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        self.inserted += len(touched)

    def summary(self) -> list[str]:
        return [f"{self.inserted:,} new or completed rows, {self.queued:,} classification jobs queued"]

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.conn.close()
