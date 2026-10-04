"""Where torrents are written to.

A sink takes one batch at a time together with the source's content-type
numbering (``model.SCHEMA_*``) and the import options; a batch that returns
counts as fully written, which is what the CLI's resume hints rely on.
"""

from __future__ import annotations

from typing import Protocol

from ..items import Options
from ..model import Torrent


class Sink(Protocol):
    def write(self, torrents: list[Torrent], schema: str, opts: Options) -> None: ...

    # Lines for the final report, e.g. how many rows were new.
    def summary(self) -> list[str]: ...

    def close(self) -> None: ...
