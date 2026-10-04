"""Sink: JSON lines in the ``/import`` format, to stdout or to (split) files."""

from __future__ import annotations

import os
import sys
from typing import IO

from ..items import Options, dumps_item, import_item
from ..model import Torrent


class JsonlSink:
    def __init__(self, path: str | None, split_size: int | None):
        self.path = path
        self.split_size = split_size
        self.part = 0
        self.in_part = 0
        self.fh: IO[str] | None = None
        if path:
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)
        else:
            self.fh = sys.stdout

    def _open_part(self) -> IO[str]:
        assert self.path
        if self.part == 0 or not self.split_size:
            name = self.path
        else:
            stem, ext = os.path.splitext(self.path)
            name = f"{stem}-{self.part * self.split_size + 1}{ext}"
        self.part += 1
        self.in_part = 0
        return open(name, "w", encoding="utf-8", newline="\n")

    def write(self, torrents: list[Torrent], schema: str, opts: Options) -> None:
        for t in torrents:
            if self.path and (self.fh is None or (self.split_size and self.in_part >= self.split_size)):
                if self.fh:
                    self.fh.close()
                self.fh = self._open_part()
            assert self.fh
            self.fh.write(dumps_item(import_item(t, schema, opts)) + "\n")
            self.in_part += 1

    def summary(self) -> list[str]:
        return []

    def close(self) -> None:
        if self.fh and self.fh is not sys.stdout:
            self.fh.close()
        elif self.fh:
            self.fh.flush()
