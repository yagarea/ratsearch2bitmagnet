"""Where torrents are read from.

Every source yields torrents in batches and knows which content-type numbering
(``model.SCHEMA_*``) its data uses. Sources that page through a Manticore table
also accept a row id to resume after.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from ..model import Torrent


class Source(Protocol):
    schema: str
    supports_after_id: bool

    def describe(self) -> str: ...

    def total_estimate(self) -> int | None: ...

    def batches(self) -> Iterator[list[Torrent]]: ...

    # Lines for the final report, e.g. that a dump was truncated.
    def summary(self) -> list[str]: ...

    def close(self) -> None: ...
