import pytest

from ratsearch2bitmagnet.model import SCHEMA_V2, is_bad
from ratsearch2bitmagnet.sources.ratsdb import RatsdbSource


def test_reader(dump):
    src = RatsdbSource(str(dump))
    assert src.schema == SCHEMA_V2 and src.total_estimate() == 1200
    assert "client 2.3.1" in src.describe()
    batches = list(src.batches())
    assert [len(b) for b in batches] == [500, 500, 200]
    assert src.complete and not src.truncated and src.footer["torrents"] == 1200
    assert src.summary() == []
    t = batches[0]
    assert t[0].hash == "%040x" % 7920 and t[0].content_category == 1 and t[0].size == 1001
    assert t[1].content_type == 3 and t[1].content_category == 0
    assert is_bad(t[2])
    assert t[4].private and t[4].added == src.fallback_added
    assert t[5].name == "ab"
    src.close()


def test_reader_truncated(dump):
    src = RatsdbSource(str(dump.parent / "truncated.ratsdb"))
    assert sum(len(b) for b in src.batches()) == 1000
    assert src.truncated and not src.complete
    assert src.summary() == ["the dump was truncated; everything readable was imported"]
    src.close()


def test_rejects_other_files(tmp_path):
    other = tmp_path / "x.ratsdb"
    other.write_bytes(b"not a dump at all")
    with pytest.raises(ValueError):
        RatsdbSource(str(other))
