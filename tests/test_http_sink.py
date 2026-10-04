import json

import pytest

from ratsearch2bitmagnet import cli
from ratsearch2bitmagnet.sinks import http_import


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(http_import.time, "sleep", lambda *_: None)


def test_batches_and_headers(import_server, dump):
    assert cli.main([str(dump), "--bitmagnet", import_server.url, "--batch-size", "400", "--import-id", "imp-1",
                     "-q"]) == 0
    # dump frames hold 500 torrents; the first one loses a bad and an invalid entry
    assert [len(lines) for _, lines in import_server.requests] == [400, 98, 400, 100, 200]
    headers, lines = import_server.requests[0]
    assert headers["Content-Type"] == "application/json" and headers["X-Import-Id"] == "imp-1"
    assert json.loads(lines[0])["infoHash"] == "%040x" % 7920


def test_retries_server_errors_only(import_server, dump):
    import_server.statuses = [500, 503]
    assert cli.main([str(dump), "--bitmagnet", import_server.url, "--limit", "5", "-q"]) == 0
    assert len(import_server.requests) == 3
    import_server.requests.clear()
    import_server.statuses = [400]
    assert cli.main([str(dump), "--bitmagnet", import_server.url, "--limit", "5", "-q"]) == 1
    assert len(import_server.requests) == 1


def test_url_handling():
    sink = http_import.HttpSink("http://bitmagnet:3333", 1.0, 0)
    assert sink.path == "/import"
    sink = http_import.HttpSink("https://example.org/prefix/import?x=1", 1.0, 0)
    assert sink.path == "/prefix/import?x=1" and sink.parsed.scheme == "https"
    with pytest.raises(SystemExit):
        http_import.HttpSink("bitmagnet:3333", 1.0, 0)
