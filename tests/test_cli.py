import json

import pytest

from ratsearch2bitmagnet import cli
from ratsearch2bitmagnet.sources.manticore import DEFAULT_MANTICORE_PORT


def test_split_host_port():
    assert cli.split_host_port("localhost") == ("localhost", DEFAULT_MANTICORE_PORT)
    assert cli.split_host_port("10.0.0.2:9307") == ("10.0.0.2", 9307)
    assert cli.split_host_port("[::1]:9307") == ("::1", 9307)
    assert cli.split_host_port("::1") == ("::1", DEFAULT_MANTICORE_PORT)


def test_detect_source_kind(tmp_path):
    (tmp_path / "x.ratsdb").write_bytes(b"")
    assert cli.detect_source_kind(str(tmp_path)) == "data-dir"
    assert cli.detect_source_kind(str(tmp_path / "x.ratsdb")) == "ratsdb"
    assert cli.detect_source_kind("127.0.0.1:9306") == "manticore"
    assert cli.detect_source_kind("rats.example.org") == "manticore"
    with pytest.raises(SystemExit):
        cli.detect_source_kind(str(tmp_path / "missing.ratsdb"))


@pytest.mark.parametrize("argv", [
    ["x", "--batch-size", "0"],
    ["x", "--batch-size", "1001"],
    ["x", "--split-size", "10"],
    ["x", "--limit", "0"],
    ["x", "--source-name", " "],
    ["x", "--bitmagnet", "--postgres", "dsn"],
])
def test_rejected_arguments(argv, dump):
    argv[0] = str(dump)
    with pytest.raises(SystemExit):
        cli.main(argv)


def test_dump_to_split_files(tmp_path, dump, caplog):
    out = tmp_path / "out" / "rats.jsonl"
    assert cli.main([str(dump), "--output", str(out), "--split-size", "500"]) == 0
    parts = sorted(p.name for p in out.parent.iterdir())
    assert parts == ["rats-1001.jsonl", "rats-501.jsonl", "rats.jsonl"]
    lines = [json.loads(line) for p in (out, out.parent / "rats-501.jsonl", out.parent / "rats-1001.jsonl")
             for line in p.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1198  # one bad, one invalid hash
    assert lines[0] == {"source": "rats-search", "infoHash": "%040x" % 7920, "name": "Torrent 1", "size": 1001,
                        "publishedAt": "2023-11-14T22:13:21Z"}
    assert not any("contentType" in line for line in lines)
    assert lines[2]["private"] is True and lines[3]["name"] == "ab"
    assert "1 classified 'bad' skipped" in caplog.text and "1 without a valid infohash" in caplog.text
    assert "truncated" not in caplog.text


def test_hints_are_opt_in(dump, capsys, caplog):
    assert cli.main([str(dump), "--limit", "2"]) == 0
    assert "content-type hints: off, and no file lists reach bitmagnet this way" in caplog.text
    assert cli.main([str(dump), "--limit", "2", "--hints"]) == 0
    assert "content-type hints: rats-search 2.x content types" in caplog.text
    plain, hinted = (json.loads(line) for line in capsys.readouterr().out.splitlines()[::2])
    assert "contentType" not in plain and hinted["contentType"] == "movie"


def test_truncated_dump_is_reported(dump, caplog):
    assert cli.main([str(dump.parent / "truncated.ratsdb"), "--output", str(dump.parent / "t.jsonl")]) == 0
    assert "the dump was truncated" in caplog.text


def test_skip_and_limit(dump, capsys):
    assert cli.main([str(dump), "--skip", "10", "--limit", "3", "-q"]) == 0
    names = [json.loads(line)["name"] for line in capsys.readouterr().out.splitlines()]
    assert names == ["Torrent 11", "Torrent 12", "Torrent 13"]


def test_unreachable_manticore_is_reported(caplog):
    assert cli.main(["127.0.0.1:1", "--manticore-timeout", "2"]) == 1
    assert "cannot open the source" in caplog.text
