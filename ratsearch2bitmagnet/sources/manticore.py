"""Source: the Manticore Search instance behind rats-search.

Schema shared by 1.x and 2.x (data/manticore.cpp, legacy/background/sphinx.js):
  torrents: id, hash, name, size, files, piecelength, added, contentType,
            contentCategory, seeders, leechers, completed, trackersChecked,
            good, bad, info (JSON)
  files:    id (same as the torrent row), hash, path, size -- the last two are
            "\\n"-joined blobs holding the whole file list
Manticore lower-cases column names in result sets.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator

from ..model import (
    SCHEMA_LEGACY,
    SCHEMA_UNKNOWN,
    SCHEMA_V2,
    Torrent,
    clean_text,
    parse_file_blob,
    private_from_info,
    to_int,
)

log = logging.getLogger(__name__)

DEFAULT_MANTICORE_PORT = 9306
# Manticore refuses LIMIT above max_matches (1000 by default).
MAX_BATCH_SIZE = 1000


def load_pymysql():
    try:
        import pymysql  # type: ignore
        import pymysql.cursors  # type: ignore
    except ImportError as exc:
        raise SystemExit("reading a Manticore database needs the 'pymysql' package "
                         "(pip install pymysql, or run this tool with 'uv run')") from exc
    return pymysql


class ManticoreSource:
    supports_after_id = True

    def __init__(self, host: str, port: int, batch_size: int, after_id: int = 0, timeout: float = 30.0):
        pymysql = load_pymysql()
        self.host, self.port = host, port
        self.batch_size = min(batch_size, MAX_BATCH_SIZE)
        self.after_id = after_id
        # Manticore accepts any credentials. Values come back as bytes and are
        # decoded leniently here: names from the DHT are not always valid UTF-8.
        self.conn = pymysql.connect(host=host, port=port, user="", password="",
                                    use_unicode=False, autocommit=True,
                                    connect_timeout=timeout, read_timeout=max(timeout, 600.0),
                                    cursorclass=pymysql.cursors.DictCursor)
        tables = {clean_text(next(iter(r.values()))).lower() for r in self.query("SHOW TABLES")}
        if "torrents" not in tables:
            raise ValueError(f"Manticore at {host}:{port} has no 'torrents' table; not a rats-search database")
        self.has_files = "files" in tables
        self.schema = self.detect_schema()

    def query(self, sql: str) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(sql)
            return list(cur.fetchall())

    def describe(self) -> str:
        return f"Manticore at {self.host}:{self.port}"

    def total_estimate(self) -> int | None:
        rows = self.query("SELECT COUNT(*) AS cnt FROM torrents")
        return to_int(rows[0]["cnt"]) if rows else None

    def detect_schema(self) -> str:
        """Tell the 1.x and 2.x content-type numberings apart from the data.

        Categories above 1 and type 100 exist only in 2.x; type 8 only in 1.x.
        A database showing neither (no classified rows yet) stays "unknown" and
        only the ids both generations agree on are used for hints.
        """
        try:
            rows = self.query("SELECT MAX(contenttype) AS mt, MAX(contentcategory) AS mc FROM torrents")
        except Exception as exc:  # noqa: BLE001 - any SQL failure just disables detection
            log.debug("schema detection query failed: %s", exc)
            return SCHEMA_UNKNOWN
        if not rows:
            return SCHEMA_UNKNOWN
        max_type, max_category = to_int(rows[0].get("mt")), to_int(rows[0].get("mc"))
        if max_category > 1 or max_type == 100:
            return SCHEMA_V2
        if max_type == 8:
            return SCHEMA_LEGACY
        return SCHEMA_UNKNOWN

    @staticmethod
    def row_to_torrent(row: dict) -> Torrent:
        return Torrent(
            hash=clean_text(row.get("hash")).lower(),
            name=clean_text(row.get("name")),
            size=to_int(row.get("size")),
            files=to_int(row.get("files")),
            added=to_int(row.get("added")),
            content_type=to_int(row.get("contenttype")),
            content_category=to_int(row.get("contentcategory")),
            seeders=to_int(row.get("seeders")),
            leechers=to_int(row.get("leechers")),
            scraped=to_int(row.get("trackerschecked")) > 0,
            private=private_from_info(row.get("info")),
            row_id=to_int(row.get("id")),
        )

    def batches(self) -> Iterator[list[Torrent]]:
        after = self.after_id
        columns = ("id, hash, name, size, files, added, contenttype, contentcategory, "
                   "seeders, leechers, trackerschecked, info")
        while True:
            rows = self.query(f"SELECT {columns} FROM torrents WHERE id > {after} "
                              f"ORDER BY id ASC LIMIT {self.batch_size}")
            if not rows:
                return
            after = to_int(rows[-1]["id"])
            batch = [self.row_to_torrent(r) for r in rows]
            if self.has_files:
                self.attach_files(batch)
            yield batch

    def attach_files(self, batch: list[Torrent]) -> None:
        # A files row carries its torrent's row id in both rats-search
        # generations, and that is the only indexed way to reach it: a filter
        # on `hash` scans the whole table. rats-search's own exporter looks the
        # rows up the same way.
        by_id = {t.row_id: t for t in batch if t.row_id}
        if not by_id:
            return
        ids = ",".join(str(i) for i in by_id)
        for row in self.query(f"SELECT id, hash, path, size FROM files WHERE id IN ({ids}) LIMIT {len(by_id)}"):
            t = by_id.get(to_int(row.get("id")))
            if t is not None and clean_text(row.get("hash")).lower() == t.hash:
                t.file_list = parse_file_blob(clean_text(row.get("path")), clean_text(row.get("size")))

    def summary(self) -> list[str]:
        return []

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.conn.close()
