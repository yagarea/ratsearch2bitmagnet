import json

from helpers import make_torrent

from ratsearch2bitmagnet.items import Options, dumps_item, import_item
from ratsearch2bitmagnet.model import SCHEMA_V2


def test_import_item():
    opts = Options(source_name="rats")
    t = make_torrent(hash="ab" * 20, name="N", size=-5, added=0, private=True, content_type=5)
    item = import_item(t, SCHEMA_V2, opts)
    assert item["source"] == "rats" and item["infoHash"] == "ab" * 20 and item["size"] == 0
    assert item["private"] is True and item["publishedAt"].endswith("Z")
    assert "contentType" not in item  # rats-search's classification is opt-in
    assert import_item(t, SCHEMA_V2, Options(hints=True))["contentType"] == "software"
    assert import_item(make_torrent(added=1_700_000_000), SCHEMA_V2, opts)["publishedAt"] == "2023-11-14T22:13:20Z"


def test_dumps_item_is_one_compact_line():
    line = dumps_item({"name": "ü\n日本", "size": 1})
    assert "\n" not in line and json.loads(line) == {"name": "ü\n日本", "size": 1}
    assert line == '{"name":"ü\\n日本","size":1}'
