"""The torrent as rats-search stores it, and how its classification maps onto bitmagnet's."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

HASH_RE = re.compile(r"^[0-9a-f]{40}$")

# Both rats-search generations store numeric content ids in the same two
# Manticore columns, but they number them differently. 2.x (C++/Qt,
# domain/content.h):
V2_TYPES = {1: "video", 2: "audio", 3: "books", 4: "pictures", 5: "software",
            6: "games", 7: "archive", 100: "bad"}
V2_CATEGORIES = {1: "movie", 2: "series", 3: "documentary", 4: "anime", 5: "music",
                 6: "ebook", 7: "comics", 8: "software", 9: "game", 100: "xxx"}
# 1.x (legacy Electron app, app/content.js): position in the ContentTypes map + 1.
LEGACY_TYPES = {1: "video", 2: "audio", 3: "pictures", 4: "books", 5: "application",
                6: "archive", 7: "disc", 8: "bad"}
LEGACY_CATEGORIES = {1: "xxx"}
# Ids that mean the same thing in both numberings.
COMMON_TYPES = {1: "video", 2: "audio", 5: "software"}

V2_TYPE_IDS = {name: i for i, name in V2_TYPES.items()}
V2_CATEGORY_IDS = {name: i for i, name in V2_CATEGORIES.items()}

# rats-search name -> bitmagnet content type. Only reliable, unambiguous
# mappings: "video" alone could be a movie or a show, "audio" music or an
# audiobook, so those get no hint and bitmagnet's classifier decides.
HINT_BY_CATEGORY = {"movie": "movie", "series": "tv_show", "music": "music",
                    "ebook": "ebook", "comics": "comic", "software": "software",
                    "game": "game", "xxx": "xxx"}
HINT_BY_TYPE = {"books": "ebook", "software": "software", "application": "software",
                "games": "game"}

# Which numbering a source uses; "unknown" restricts hints to COMMON_TYPES.
SCHEMA_V2 = "v2"
SCHEMA_LEGACY = "legacy"
SCHEMA_UNKNOWN = "unknown"


@dataclass(slots=True)
class File:
    path: str
    size: int


@dataclass(slots=True)
class Torrent:
    hash: str
    name: str
    size: int
    files: int
    added: int  # unix seconds, 0 when unknown
    content_type: int = 0
    content_category: int = 0
    seeders: int = 0
    leechers: int = 0
    scraped: bool = False  # seeders/leechers come from an actual tracker scrape
    private: bool = False
    row_id: int = 0
    file_list: list[File] = field(default_factory=list)


def content_names(t: Torrent, schema: str) -> tuple[str | None, str | None]:
    if schema == SCHEMA_V2:
        return V2_TYPES.get(t.content_type), V2_CATEGORIES.get(t.content_category)
    if schema == SCHEMA_LEGACY:
        return LEGACY_TYPES.get(t.content_type), LEGACY_CATEGORIES.get(t.content_category)
    return COMMON_TYPES.get(t.content_type), None


def hint_for(t: Torrent, schema: str) -> str | None:
    """The bitmagnet content type to hint, or None when rats-search's class is ambiguous."""
    type_name, category_name = content_names(t, schema)
    if category_name and category_name in HINT_BY_CATEGORY:
        return HINT_BY_CATEGORY[category_name]
    if type_name:
        return HINT_BY_TYPE.get(type_name)
    return None


def is_bad(t: Torrent) -> bool:
    # "bad" is 8 in 1.x and 100 in 2.x; neither value is used by the other.
    return t.content_type in (8, 100)


def clean_text(value: object) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    elif value is None:
        return ""
    elif not isinstance(value, str):
        value = str(value)
    # PostgreSQL text rejects NUL, and bitmagnet stores names verbatim.
    return value.replace("\x00", "")


def to_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def iso_utc(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_file_blob(paths: str, sizes: str) -> list[File]:
    """Split the "\\n"-joined path/size blobs of a rats-search ``files`` row."""
    if not paths:
        return []
    path_list = paths.split("\n")
    size_list = sizes.split("\n") if sizes else []
    return [File(path_list[i], to_int(size_list[i]) if i < len(size_list) else 0)
            for i in range(len(path_list))]


def private_from_info(info: object) -> bool:
    if isinstance(info, (bytes, str)):
        try:
            info = json.loads(info)
        except ValueError:
            return False
    return isinstance(info, dict) and bool(info.get("private"))
