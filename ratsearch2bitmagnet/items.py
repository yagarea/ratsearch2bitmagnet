"""The item format of bitmagnet's ``/import`` endpoint, and the import options every sink shares."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from .model import Torrent, hint_for, iso_utc

DEFAULT_SOURCE_NAME = "rats-search"


@dataclass
class Options:
    source_name: str = DEFAULT_SOURCE_NAME
    import_id: str = ""
    # Opt-in: bitmagnet takes a hinted type as final, skipping its own
    # classifier for the torrent now and on every later reprocess.
    hints: bool = False


def import_item(t: Torrent, schema: str, opts: Options) -> dict:
    item = {
        "source": opts.source_name,
        "infoHash": t.hash,
        "name": t.name,
        "size": max(t.size, 0),
        "publishedAt": iso_utc(t.added if t.added > 0 else int(time.time())),
    }
    if t.private:
        item["private"] = True
    if opts.hints:
        hint = hint_for(t, schema)
        if hint:
            item["contentType"] = hint
    return item


def dumps_item(item: dict) -> str:
    return json.dumps(item, ensure_ascii=False, separators=(",", ":"))
