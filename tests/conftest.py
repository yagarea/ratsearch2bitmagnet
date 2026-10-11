"""Fixtures: a synthetic dump, a fake /import server and a seeded searchd.

The searchd is the binary rats-search ships (found in a rats-search checkout
next to this repo or given through RATSEARCH2BITMAGNET_TEST_SEARCHD); the tests
that need it are skipped without one.
"""

from __future__ import annotations

import pytest
from helpers import FakeImport, dump_torrent, searchd_binary, seed_database, write_dump

from ratsearch2bitmagnet.sources.datadir import SearchdProcess


@pytest.fixture
def dump(tmp_path):
    """test.ratsdb with 1200 torrents (one bad, one with an invalid hash) and a truncated twin."""
    torrents = [dump_torrent(i) for i in range(1, 1201)]
    torrents[1] = dump_torrent(2, contentType="books", contentCategory="")
    torrents[2] = dump_torrent(3, contentType="bad")
    torrents[3] = dump_torrent(4, hash="nope")
    torrents[4] = dump_torrent(5, added=0, info={"private": True}, files=0, files_list=[])
    torrents[5] = dump_torrent(6, name="a\u0000b")
    path = tmp_path / "test.ratsdb"
    write_dump(path, torrents)
    write_dump(tmp_path / "truncated.ratsdb", torrents, truncate=True)
    return path


@pytest.fixture
def import_server():
    server = FakeImport()
    yield server
    server.close()


@pytest.fixture(scope="session")
def seeded(tmp_path_factory):
    """A running searchd on a data directory holding helpers.SEED."""
    searchd = searchd_binary()
    if not searchd:
        pytest.skip("no searchd binary (set RATSEARCH2BITMAGNET_TEST_SEARCHD)")
    pytest.importorskip("pymysql")
    data_dir = tmp_path_factory.mktemp("rats")
    (data_dir / "database").mkdir()
    proc = SearchdProcess(str(data_dir), searchd, 120)
    hashes = seed_database(proc.port)
    yield {"data_dir": str(data_dir), "port": proc.port, "hashes": hashes, "searchd": searchd}
    proc.close()
