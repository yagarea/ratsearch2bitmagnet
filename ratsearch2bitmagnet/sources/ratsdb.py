"""Source: a ``.ratsdb`` dump written by rats-search 2.x.

Layout (services/database_dump.h, all integers big-endian):
  "RATSDB\\0", uint32 format version, uint32 length + JSON header,
  repeated [uint32 length + qCompress()'d JSON lines, one torrent each],
  uint32 0, uint32 length + JSON footer.
qCompress output is a uint32 uncompressed size followed by a zlib stream.
"""

from __future__ import annotations

import contextlib
import json
import logging
import struct
import time
import zlib
from collections.abc import Iterator
from datetime import datetime
from typing import IO

from ..model import SCHEMA_V2, V2_CATEGORY_IDS, V2_TYPE_IDS, Torrent, clean_text, private_from_info, to_int

log = logging.getLogger(__name__)

RATSDB_MAGIC = b"RATSDB\0"
RATSDB_MAX_FRAME = 64 * 1024 * 1024
RATSDB_FORMAT_VERSION = 1


def torrent_from_dump_json(obj: dict, fallback_added: int) -> Torrent:
    hash_ = clean_text(obj.get("hash") or obj.get("info_hash") or "").lower()
    added_ms = to_int(obj.get("added"))
    return Torrent(
        hash=hash_,
        name=clean_text(obj.get("name")),
        size=to_int(obj.get("size")),
        added=added_ms // 1000 if added_ms > 0 else fallback_added,
        content_type=V2_TYPE_IDS.get(str(obj.get("contentType") or ""), 0),
        content_category=V2_CATEGORY_IDS.get(str(obj.get("contentCategory") or ""), 0),
        private=private_from_info(obj.get("info")),
    )


class RatsdbSource:
    schema = SCHEMA_V2  # dumps are written by 2.x with 2.x names
    supports_after_id = False

    def __init__(self, path: str):
        self.path = path
        self.file: IO[bytes] = open(path, "rb")
        self.complete = False  # end marker and footer were read
        self.truncated = False  # the file ended before its end marker
        self.footer: dict = {}
        self.torrents_read = 0
        if self.file.read(len(RATSDB_MAGIC)) != RATSDB_MAGIC:
            raise ValueError(f"{path}: not a Rats Search database dump")
        version = self._read_u32()
        if version is None or version > RATSDB_FORMAT_VERSION:
            raise ValueError(f"{path}: dump format version {version} is not supported")
        header = self._read_block()
        if header is None:
            raise ValueError(f"{path}: truncated dump header")
        self.header = json.loads(header.decode("utf-8", errors="replace") or "{}")
        created = str(self.header.get("created") or "")
        self.fallback_added = int(time.time())
        with contextlib.suppress(ValueError):
            if created:
                self.fallback_added = int(datetime.fromisoformat(created).timestamp())

    def _read_u32(self) -> int | None:
        raw = self.file.read(4)
        return struct.unpack(">I", raw)[0] if len(raw) == 4 else None

    def _read_block(self) -> bytes | None:
        length = self._read_u32()
        if length is None or length > RATSDB_MAX_FRAME:
            return None
        data = self.file.read(length)
        return data if len(data) == length else None

    def total_estimate(self) -> int | None:
        return to_int(self.header.get("torrents")) or None

    def describe(self) -> str:
        client = self.header.get("client") or "?"
        created = self.header.get("created") or "?"
        return f"dump {self.path} (client {client}, created {created})"

    def batches(self) -> Iterator[list[Torrent]]:
        while True:
            length = self._read_u32()
            if length is None:
                log.warning("dump ends without an end marker (truncated); keeping what was read")
                self.truncated = True
                return
            if length == 0:
                footer = self._read_block()
                if footer is not None:
                    self.footer = json.loads(footer.decode("utf-8", errors="replace") or "{}")
                    self.complete = True
                return
            if length > RATSDB_MAX_FRAME:
                raise ValueError("corrupt dump: frame length out of range")
            frame = self.file.read(length)
            if len(frame) != length:
                log.warning("dump ends inside a frame (truncated); keeping what was read")
                self.truncated = True
                return
            expected = struct.unpack(">I", frame[:4])[0]
            lines = zlib.decompress(frame[4:])
            if len(lines) != expected:
                log.warning("frame size mismatch: expected %d bytes, got %d", expected, len(lines))
            batch = []
            for line in lines.split(b"\n"):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    log.warning("skipping unparsable dump line")
                    continue
                if isinstance(obj, dict) and obj:
                    batch.append(torrent_from_dump_json(obj, self.fallback_added))
            self.torrents_read += len(batch)
            if batch:
                yield batch

    def summary(self) -> list[str]:
        if self.truncated:
            return ["the dump was truncated; everything readable was imported"]
        return []

    def close(self) -> None:
        self.file.close()
