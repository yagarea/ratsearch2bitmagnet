"""Builders shared by the test modules: torrents, .ratsdb dumps, a fake /import server, searchd seeding."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from ratsearch2bitmagnet.model import Torrent
from ratsearch2bitmagnet.sources.ratsdb import RATSDB_MAGIC

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_torrent(**kw: Any) -> Torrent:
    base: dict[str, Any] = dict(hash="0" * 39 + "1", name="Name", size=100, files=0, added=1_700_000_000)
    base.update(kw)
    return Torrent(**base)


# ---------------------------------------------------------------------------
# .ratsdb dumps, written exactly as rats-search's DumpWriter does


def qcompress(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + zlib.compress(data, 6)


def block(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


def write_dump(path, torrents: list[dict], truncate: bool = False, frame: int = 500) -> None:
    header = {"format": "rats-search-db", "version": 1, "client": "2.3.1", "peerId": "p",
              "created": "2026-10-01T12:00:00", "torrents": len(torrents), "compression": "zlib"}
    out = bytearray(RATSDB_MAGIC + struct.pack(">I", 1) + block(json.dumps(header).encode()))
    for i in range(0, len(torrents), frame):
        lines = b"".join(json.dumps(t, ensure_ascii=False).encode() + b"\n" for t in torrents[i:i + frame])
        out += block(qcompress(lines))
    if truncate:
        del out[-7:]
    else:
        footer = {"torrents": len(torrents), "files": sum(t.get("files", 0) for t in torrents),
                  "totalSize": sum(t.get("size", 0) for t in torrents)}
        out += struct.pack(">I", 0) + block(json.dumps(footer).encode())
    with open(path, "wb") as fh:
        fh.write(out)


def dump_torrent(i: int, **kw: Any) -> dict:
    t = {"hash": "%040x" % (i * 7919 + 1), "name": f"Torrent {i}", "size": 1000 + i, "files": 2,
         "pieceLength": 16384, "added": 1_700_000_000_000 + i * 1000, "contentType": "video",
         "contentCategory": "movie", "seeders": 3, "leechers": 1, "completed": 0,
         "trackersChecked": 1_700_000_000_000, "good": 0, "bad": 0,
         "files_list": [{"path": f"dir/file{i}.mkv", "size": 900 + i}, {"path": "readme.txt", "size": 100}]}
    t.update(kw)
    return t


# ---------------------------------------------------------------------------
# a stand-in for bitmagnet's /import endpoint


class FakeImport:
    """Records every POST; `statuses` scripts the response codes (200 once exhausted)."""

    def __init__(self) -> None:
        self.requests: list[tuple[dict, list[str]]] = []
        self.statuses: list[int] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server API
                body = self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8")
                lines = body.splitlines()
                fake.requests.append((dict(self.headers), lines))
                status = fake.statuses.pop(0) if fake.statuses else 200
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b"boom" if status != 200 else b"%d items imported\nimport complete\n" % len(lines))

            def log_message(self, format, *args):  # noqa: A002 - http.server API
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/import"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


# ---------------------------------------------------------------------------
# a real searchd with rats-search's schema


def searchd_binary() -> str | None:
    explicit = os.environ.get("RATSEARCH2BITMAGNET_TEST_SEARCHD")
    if explicit:
        return os.path.abspath(explicit)
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    system = {"Linux": "linux", "Darwin": "darwin"}.get(platform.system())
    if not arch or not system:
        return None
    for base in (ROOT, os.path.dirname(ROOT)):
        path = os.path.join(base, "rats-search", "imports", system, arch, "searchd")
        if os.access(path, os.X_OK):
            return path
    return shutil.which("searchd")


def row_id(hash_: str) -> int:
    """data/row_id.cpp: the 2.x row id derived from the infohash."""
    return (int(hash_[:16], 16) >> 1) or 1


# name, size, files, type, category, seeders, leechers, trackersChecked, info
SEED = [
    ("ubuntu.iso", 5_000, [("ubuntu.iso", 5_000)], 5, 8, 120, 3, 1_700_000_000, {}),
    ("Some Movie (2019)", 2_000, [("Some.Movie.mkv", 1_900), ("sample/s.mkv", 100)], 1, 1, 10, 2, 1_700_000_001,
     {"private": True}),
    ("Series S01", 9_000, [(f"S01E{i:02d}.mkv", 300) for i in range(1, 31)], 1, 2, 0, 0, 0, {}),
    ("Comics", 800, [("Issue %03d.cbz" % i, 8) for i in range(1, 121)], 3, 7, 1, 0, 1_700_000_003, {}),
    ("Bad thing", 10, [("x.exe", 10)], 100, 0, 0, 0, 0, {}),
    ("Umlaut ü 日本", 123, [("a/b.txt", 100), ("a/c.txt", 23)], 7, 0, 0, 0, 0, {}),
    ("No files known", 999, [], 0, 0, 0, 0, 0, {}),
]


def seed_database(port: int) -> list[str]:
    """Insert SEED the way rats-search 2.x writes rows; returns the infohashes."""
    import pymysql

    conn = pymysql.connect(host="127.0.0.1", port=port, user="", password="", autocommit=True)
    hashes = []
    with conn.cursor() as cur:
        for i, (name, size, files, ct, cc, se, le, checked, info) in enumerate(SEED):
            h = hashlib.sha1(name.encode()).hexdigest()
            hashes.append(h)
            cur.execute(
                "INSERT INTO torrents (id, hash, name, nameIndex, size, files, piecelength, added, ipv4, port, "
                "contentType, contentCategory, seeders, leechers, completed, trackersChecked, good, bad, info) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (row_id(h), h, name, name, size, len(files), 16384, 1_700_000_000 + i, "", 0,
                 ct, cc, se, le, 0, checked, 0, 0, json.dumps(info)))
            if files:
                cur.execute("INSERT INTO files (id, hash, path, size) VALUES (%s,%s,%s,%s)",
                            (row_id(h), h, "\n".join(p for p, _ in files), "\n".join(str(s) for _, s in files)))
        # a files row under a foreign id is unreachable, like in rats-search itself
        h = hashlib.sha1(b"stray").hexdigest()
        hashes.append(h)
        cur.execute("INSERT INTO torrents (id, hash, name, nameIndex, size, files, added, contentType, "
                    "contentCategory, info) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (row_id(h), h, "stray", "stray", 50, 1, 1_700_000_100, 5, 0, "{}"))
        cur.execute("INSERT INTO files (id, hash, path, size) VALUES (%s,%s,%s,%s)", (424242, h, "stray.bin", "50"))
        cur.execute("FLUSH RAMCHUNK torrents")
        cur.execute("FLUSH RAMCHUNK files")
    conn.close()
    return hashes
