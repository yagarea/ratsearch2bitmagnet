"""Against a real searchd (see conftest.seeded); skipped when none is available."""

import os
import shutil

from ratsearch2bitmagnet import cli
from ratsearch2bitmagnet.model import SCHEMA_V2, File, hint_for
from ratsearch2bitmagnet.sources.manticore import ManticoreSource


def test_manticore_source(seeded):
    src = ManticoreSource("127.0.0.1", seeded["port"], batch_size=3)
    assert src.schema == SCHEMA_V2 and src.total_estimate() == 8
    batches = list(src.batches())
    assert [len(b) for b in batches] == [3, 3, 2]
    by_name = {t.name: t for b in batches for t in b}
    assert set(by_name) == {"ubuntu.iso", "Some Movie (2019)", "Series S01", "Comics", "Bad thing",
                            "Umlaut ü 日本", "No files known", "stray"}
    assert by_name["Some Movie (2019)"].private and by_name["Some Movie (2019)"].scraped
    assert not by_name["Series S01"].scraped and len(by_name["Series S01"].file_list) == 30
    assert by_name["Comics"].file_list[0] == File("Issue 001.cbz", 8)
    assert by_name["stray"].file_list == [] and by_name["stray"].files == 1
    assert by_name["No files known"].file_list == []
    assert hint_for(by_name["Comics"], src.schema) == "comic"
    src.close()


def test_after_id_resumes_by_keyset(seeded):
    src = ManticoreSource("127.0.0.1", seeded["port"], batch_size=10)
    all_ids = [t.row_id for b in src.batches() for t in b]
    src.close()
    src = ManticoreSource("127.0.0.1", seeded["port"], batch_size=10, after_id=all_ids[2])
    assert src.total_estimate() == len(all_ids) - 3
    assert [t.row_id for b in src.batches() for t in b] == all_ids[3:]
    src.close()


def test_data_dir_attaches_to_running_searchd(seeded, tmp_path, caplog):
    with open(os.path.join(seeded["data_dir"], "sphinx.conf"), "w") as fh:
        fh.write(f"searchd {{\n    listen = 127.0.0.1:{seeded['port']}:mysql41\n}}\n")
    out = tmp_path / "attach.jsonl"
    assert cli.main([seeded["data_dir"], "--output", str(out)]) == 0
    assert "attaching to the searchd already serving" in caplog.text
    assert len(out.read_text(encoding="utf-8").splitlines()) == 7  # "Bad thing" skipped


def test_data_dir_starts_its_own_searchd(seeded, tmp_path, caplog):
    copy = tmp_path / "copy"
    shutil.copytree(seeded["data_dir"], copy, ignore=shutil.ignore_patterns("sphinx.conf", "*.lock"))
    out = tmp_path / "spawn.jsonl"
    assert cli.main([str(copy), "--searchd", seeded["searchd"], "--output", str(out), "--keep-bad"]) == 0
    assert "starting" in caplog.text and "stopping searchd" in caplog.text
    assert len(out.read_text(encoding="utf-8").splitlines()) == 8
