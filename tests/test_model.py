import pytest
from helpers import make_torrent

from ratsearch2bitmagnet import model as m


@pytest.mark.parametrize("schema,ctype,ccat,expected", [
    (m.SCHEMA_V2, 1, 1, "movie"),
    (m.SCHEMA_V2, 1, 2, "tv_show"),
    (m.SCHEMA_V2, 1, 3, None),          # documentary: ambiguous
    (m.SCHEMA_V2, 1, 0, None),          # plain video: ambiguous
    (m.SCHEMA_V2, 2, 5, "music"),
    (m.SCHEMA_V2, 2, 0, None),          # plain audio: could be an audiobook
    (m.SCHEMA_V2, 3, 0, "ebook"),
    (m.SCHEMA_V2, 3, 7, "comic"),
    (m.SCHEMA_V2, 5, 0, "software"),
    (m.SCHEMA_V2, 6, 0, "game"),
    (m.SCHEMA_V2, 1, 100, "xxx"),
    (m.SCHEMA_LEGACY, 1, 1, "xxx"),     # legacy category 1 is xxx, not movie
    (m.SCHEMA_LEGACY, 4, 0, "ebook"),   # legacy books is 4
    (m.SCHEMA_LEGACY, 3, 0, None),      # legacy pictures
    (m.SCHEMA_LEGACY, 5, 0, "software"),
    (m.SCHEMA_LEGACY, 6, 0, None),      # legacy archive, games in 2.x
    (m.SCHEMA_UNKNOWN, 5, 0, "software"),
    (m.SCHEMA_UNKNOWN, 3, 1, None),     # nothing that differs between the two
    (m.SCHEMA_UNKNOWN, 6, 100, None),
])
def test_hint_for(schema, ctype, ccat, expected):
    assert m.hint_for(make_torrent(content_type=ctype, content_category=ccat), schema) == expected


def test_is_bad_in_both_numberings():
    assert m.is_bad(make_torrent(content_type=100))
    assert m.is_bad(make_torrent(content_type=8))
    assert not m.is_bad(make_torrent(content_type=7))


def test_clean_text_strips_nul_and_decodes_leniently():
    assert m.clean_text(b"ok\xff\x00x") == "ok�x"
    assert m.clean_text(None) == ""
    assert m.clean_text(12) == "12"


def test_private_from_info():
    assert m.private_from_info('{"private": true}')
    assert m.private_from_info(b'{"private": 1}')
    assert not m.private_from_info("{}")
    assert not m.private_from_info("not json")
    assert not m.private_from_info(None)


def test_iso_utc():
    assert m.iso_utc(1_700_000_000) == "2023-11-14T22:13:20Z"
