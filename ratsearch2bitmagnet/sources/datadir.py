"""Source: a rats-search data directory.

Mirrors what rats-search 2.x does at start-up (data/manticore.cpp): reuse a
searchd that is already serving the directory, otherwise launch one on the
same tables with the same configuration.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator

from ..model import SCHEMA_UNKNOWN, SCHEMA_V2, Torrent
from .manticore import DEFAULT_MANTICORE_PORT, ManticoreSource

log = logging.getLogger(__name__)

SEARCHD_CONFIG = """
index torrents
{{
    type = rt
    path = {db}/torrents

    min_prefix_len = 3
    expand_keywords = 1

    rt_attr_string = hash
    rt_attr_string = name
    rt_field = nameIndex
    rt_attr_bigint = size
    rt_attr_uint = files
    rt_attr_uint = piecelength
    rt_attr_timestamp = added
    rt_field = ipv4
    rt_attr_uint = port
    rt_attr_uint = contentType
    rt_attr_uint = contentCategory
    rt_attr_uint = seeders
    rt_attr_uint = leechers
    rt_attr_uint = completed
    rt_attr_timestamp = trackersChecked
    rt_attr_uint = good
    rt_attr_uint = bad
    rt_attr_json = info

    stored_only_fields = ipv4
}}

index files
{{
    type = rt
    path = {db}/files

    rt_field = path
    rt_attr_string = hash
    rt_field = size

    stored_fields = path
    stored_only_fields = size
}}

index version
{{
    type = rt
    path = {db}/version

    rt_attr_uint = version
    rt_field = versionIndex
}}

index store
{{
    type = rt
    path = {db}/store

    rt_field = storeIndex
    rt_attr_json = data
    rt_attr_string = hash
    rt_attr_string = peerId
}}

index feed
{{
    type = rt
    path = {db}/feed

    rt_field = feedIndex
    rt_attr_json = data
}}

searchd
{{
    listen = 127.0.0.1:{port}:mysql41
    seamless_rotate = 1
    preopen_indexes = 1
    unlink_old = 1
    pid_file = {run}/searchd.pid
    log = {run}/searchd.log
    binlog_path = {binlog}
}}
"""

# A rats-search checkout next to this package ships searchd in imports/.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def default_data_dir() -> str:
    """Where rats-search 2.x keeps its data when started without --data-dir."""
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
    elif system == "Darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "Rats Search")


def port_is_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def free_port(start: int = DEFAULT_MANTICORE_PORT, attempts: int = 50) -> int:
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free TCP port for searchd")


def configured_port(data_dir: str) -> int | None:
    conf = os.path.join(data_dir, "sphinx.conf")
    try:
        with open(conf, encoding="utf-8", errors="replace") as fh:
            match = re.search(r"listen\s*=\s*[\w.]+:(\d+):mysql41", fh.read())
    except OSError:
        return None
    return int(match.group(1)) if match else None


def find_searchd(explicit: str | None) -> str:
    if explicit:
        if os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return os.path.abspath(explicit)
        raise SystemExit(f"--searchd {explicit}: not an executable file")
    candidates = []
    found = shutil.which("searchd")
    if found:
        candidates.append(found)
    arches = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}
    arch = arches.get(platform.machine().lower(), "ia32")
    system = {"Linux": "linux", "Darwin": "darwin", "Windows": "win"}.get(platform.system(), "linux")
    exe = "searchd.exe" if system == "win" else "searchd"
    candidates += [
        "/usr/bin/searchd", "/usr/local/bin/searchd", "/opt/manticore/bin/searchd",
        os.path.join(PROJECT_ROOT, "rats-search", "imports", system, arch, exe),
        os.path.join(os.getcwd(), "imports", system, arch, exe),
    ]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return os.path.abspath(path)
    raise SystemExit("searchd (Manticore Search) not found; pass --searchd PATH. rats-search ships it in "
                     f"imports/{system}/{arch}/{exe} of its source tree and inside its release packages.")


class SearchdProcess:
    """A private searchd serving a rats-search data directory, stopped on close()."""

    def __init__(self, data_dir: str, searchd: str, timeout: float):
        searchd = os.path.abspath(searchd)  # the process starts in its own run directory
        self.data_dir = data_dir
        self.run_dir = tempfile.mkdtemp(prefix="ratsearch2bitmagnet-")
        self.port = free_port()
        config = SEARCHD_CONFIG.format(db=os.path.join(data_dir, "database"), port=self.port,
                                       run=self.run_dir, binlog=data_dir)
        self.config_path = os.path.join(self.run_dir, "sphinx.conf")
        with open(self.config_path, "w", encoding="utf-8") as fh:
            fh.write(config)
        self.log_path = os.path.join(self.run_dir, "searchd.log")
        self.output = open(os.path.join(self.run_dir, "searchd.out"), "wb")
        args = [searchd, "--config", self.config_path]
        if platform.system() != "Windows":
            args.append("--nodetach")
        log.info("starting %s on port %d (log: %s)", searchd, self.port, self.log_path)
        try:
            self.proc = subprocess.Popen(args, stdout=self.output, stderr=subprocess.STDOUT, cwd=self.run_dir)
        except OSError:
            self.output.close()
            shutil.rmtree(self.run_dir, ignore_errors=True)
            raise
        self._wait_ready(timeout)

    def _wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self.close()
                raise RuntimeError(f"searchd exited with code {self.proc.returncode}:\n{self._tail()}")
            if port_is_open("127.0.0.1", self.port, 0.5):
                return
            time.sleep(0.5)
        self.close()
        raise RuntimeError(f"searchd did not start listening within {timeout:.0f}s:\n{self._tail()}")

    def _tail(self, lines: int = 20) -> str:
        chunks = []
        for path in (os.path.join(self.run_dir, "searchd.out"), self.log_path):
            with contextlib.suppress(OSError):
                with open(path, encoding="utf-8", errors="replace") as fh:
                    chunks.append("".join(fh.readlines()[-lines:]))
        return "\n".join(c for c in chunks if c.strip())

    def close(self) -> None:
        if self.proc.poll() is None:
            log.info("stopping searchd")
            self.proc.terminate()
            try:
                self.proc.wait(60)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
        self.output.close()
        shutil.rmtree(self.run_dir, ignore_errors=True)


class DataDirSource:
    """ManticoreSource over a data directory, owning the searchd it may have started."""

    supports_after_id = True

    def __init__(self, data_dir: str, searchd: str | None, batch_size: int, after_id: int,
                 timeout: float, start_timeout: float):
        data_dir = os.path.abspath(data_dir)
        database = os.path.join(data_dir, "database")
        if not any(f.startswith("torrents.") for f in (os.listdir(database) if os.path.isdir(database) else [])):
            raise SystemExit(f"{data_dir}: no database/torrents.* tables; not a rats-search data directory")
        self.process: SearchdProcess | None = None
        port = configured_port(data_dir)
        if port and port_is_open("127.0.0.1", port):
            log.info("attaching to the searchd already serving %s on port %d", data_dir, port)
        else:
            self.process = SearchdProcess(data_dir, find_searchd(searchd), start_timeout)
            port = self.process.port
        try:
            self.inner = ManticoreSource("127.0.0.1", port, batch_size, after_id, timeout)
        except Exception:
            self.close()
            raise
        self.data_dir = data_dir
        self.schema = self.inner.schema
        if self.schema == SCHEMA_UNKNOWN:
            self.schema = self.schema_from_state()

    def schema_from_state(self) -> str:
        # 2.x records its migrations; the one that re-classified everything
        # with the 2.x numbering is the earliest marker of a 2.x database.
        state = os.path.join(self.data_dir, "migrations.json")
        try:
            with open(state, encoding="utf-8") as fh:
                completed = json.load(fh).get("completedMigrations") or []
        except (OSError, ValueError):
            return SCHEMA_UNKNOWN
        return SCHEMA_V2 if "v2.0.12_recategorize_torrents" in completed else SCHEMA_UNKNOWN

    def describe(self) -> str:
        mode = "own searchd" if self.process else "running instance"
        return f"data directory {self.data_dir} ({mode}, port {self.inner.port})"

    def total_estimate(self) -> int | None:
        return self.inner.total_estimate()

    def batches(self) -> Iterator[list[Torrent]]:
        return self.inner.batches()

    def summary(self) -> list[str]:
        return self.inner.summary()

    def close(self) -> None:
        if hasattr(self, "inner"):
            self.inner.close()
        if self.process:
            self.process.close()
