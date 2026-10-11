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
    def describe(self) -> str: ...

    def write(self, torrents: list[Torrent], schema: str, opts: Options) -> None: ...

    # Running counters for the progress display and the final report, keyed by
    # a label that reads naturally after the number ("12 import requests").
    def stats(self) -> dict[str, int]: ...

    def close(self) -> None: ...
