import hashlib

from helpers import make_torrent

from ratsearch2bitmagnet import cli
from ratsearch2bitmagnet.items import Options
from ratsearch2bitmagnet.model import File
from ratsearch2bitmagnet.sinks import postgres as pg_sink


def test_files_for_bitmagnet():
    opts = Options(files_threshold=3)
    assert pg_sink.files_for_bitmagnet(make_torrent(), opts) == ("no_info", None, [])
    assert pg_sink.files_for_bitmagnet(make_torrent(files=4), opts) == ("no_info", 4, [])
    single = make_torrent(name="x.iso", files=1, file_list=[File("x.iso", 100)])
    assert pg_sink.files_for_bitmagnet(single, opts) == ("single", None, [])
    multi = make_torrent(files=2, file_list=[File("a", 1), File("b", 2), File("a", 1)])
    assert pg_sink.files_for_bitmagnet(multi, opts) == ("multi", 3, [File("a", 1), File("b", 2)])
    big = make_torrent(files=5, file_list=[File(str(i), i) for i in range(5)])
    status, count, files = pg_sink.files_for_bitmagnet(big, opts)
    assert (status, count, len(files)) == ("over_threshold", 5, 3)
    assert pg_sink.files_for_bitmagnet(multi, Options(files=False)) == ("no_info", 3, [])


def test_queue_job_matches_bitmagnet():
    payload, fingerprint = pg_sink.queue_job(["aa" * 20, "bb" * 20])
    assert payload == '{"InfoHashes":["' + "aa" * 20 + '","' + "bb" * 20 + '"]}'
    assert fingerprint == hashlib.sha256(("process_torrent" + payload).encode()).hexdigest()


def test_postgres_sink(seeded, pg, caplog):
    """Against a bitmagnet database (see conftest.pg); skipped without one."""
    dsn, conn = pg
    args = ["127.0.0.1:%d" % seeded["port"], "--postgres", dsn, "--source-name", "rats-test",
            "--files-threshold", "100"]
    assert cli.main(args + ["--import-id", "t1"]) == 0
    assert "content-type hints: off, bitmagnet classifies from names and file lists" in caplog.text
    assert "7 new or completed rows, 134 file rows, 1 classification jobs queued" in caplog.text
    info_hashes = [bytes.fromhex(h) for h in seeded["hashes"]]
    with conn.cursor() as cur:
        # by default only facts are written: no content-type hints, no peer counts
        cur.execute("SELECT count(*) FROM torrent_hints WHERE info_hash = ANY(%s)", (info_hashes,))
        assert cur.fetchone() == (0,)
        cur.execute("SELECT count(*), count(seeders), count(leechers), min(import_id) FROM torrents_torrent_sources "
                    "WHERE source = 'rats-test'")
        assert cur.fetchone() == (7, 0, 0, "t1")
        cur.execute("SELECT name, size, private, files_status::text, files_count, "
                    "(SELECT count(*) FROM torrent_files f WHERE f.info_hash = t.info_hash) "
                    "FROM torrents t WHERE info_hash = ANY(%s) ORDER BY name COLLATE \"C\"", (info_hashes,))
        assert cur.fetchall() == [
            ("Comics", 800, False, "over_threshold", 120, 100),
            ("No files known", 999, False, "no_info", None, 0),
            ("Series S01", 9000, False, "multi", 30, 30),
            ("Some Movie (2019)", 2000, True, "multi", 2, 2),
            ("Umlaut ü 日本", 123, False, "multi", 2, 2),
            ("stray", 50, False, "no_info", 1, 0),
            ("ubuntu.iso", 5000, False, "single", None, 0),
        ]
        cur.execute("SELECT queue, status::text, priority, max_retries, archival_duration::text, payload, fingerprint "
                    "FROM queue_jobs WHERE payload::text LIKE %s", (f"%{seeded['hashes'][0]}%",))
        job = cur.fetchone()
        assert job[:5] == ("process_torrent", "pending", pg_sink.QUEUE_JOB_PRIORITY, 2, "168:00:00")
        assert pg_sink.queue_job(job[5]["InfoHashes"])[1] == job[6]
    # a second run adds no rows and queues nothing; asked to, it fills in hints and peer counts
    caplog.clear()
    assert cli.main(args + ["--import-id", "t2", "--hints", "--peers"]) == 0
    assert "0 new or completed rows, 0 file rows, 0 classification jobs queued" in caplog.text
    with conn.cursor() as cur:
        cur.execute("SELECT t.name, s.seeders, s.leechers, s.import_id FROM torrents_torrent_sources s "
                    "JOIN torrents t USING (info_hash) WHERE s.source = 'rats-test' ORDER BY t.name")
        sources = {name: (se, le, imp) for name, se, le, imp in cur.fetchall()}
        assert sources["ubuntu.iso"] == (120, 3, "t2") and sources["Series S01"] == (None, None, "t2")
        cur.execute("SELECT t.name, h.content_type FROM torrent_hints h JOIN torrents t USING (info_hash) "
                    "WHERE t.info_hash = ANY(%s) ORDER BY t.name", (info_hashes,))
        assert dict(cur.fetchall()) == {"Comics": "comic", "Series S01": "tv_show", "Some Movie (2019)": "movie",
                                        "stray": "software", "ubuntu.iso": "software"}
