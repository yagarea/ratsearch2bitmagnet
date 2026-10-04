"""Sink: bitmagnet's HTTP ``/import`` endpoint (internal/importer/httpserver)."""

from __future__ import annotations

import http.client
import logging
import time
import urllib.parse

from ..items import Options, dumps_item, import_item
from ..model import Torrent

log = logging.getLogger(__name__)

DEFAULT_BITMAGNET_URL = "http://localhost:3333/import"


class HttpSink:
    def __init__(self, url: str, timeout: float, retries: int):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise SystemExit(f"--bitmagnet needs an http(s) URL, got {url!r}")
        self.parsed = parsed
        self.path = parsed.path if parsed.path and parsed.path != "/" else "/import"
        if parsed.query:
            self.path += "?" + parsed.query
        self.timeout = timeout
        self.retries = retries
        self.requests = 0
        self.retried = 0

    def _connection(self) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self.parsed.scheme == "https" else http.client.HTTPConnection
        return cls(self.parsed.hostname or "localhost", self.parsed.port, timeout=self.timeout)

    def post(self, body: bytes, import_id: str) -> str:
        headers = {"Content-Type": "application/json", "Connection": "close",
                   "Content-Length": str(len(body)), "X-Import-Id": import_id}
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                delay = min(2 ** attempt, 30)
                log.warning("import request failed (%s); retrying in %ds", last_error, delay)
                self.retried += 1
                time.sleep(delay)
            conn = self._connection()
            try:
                conn.request("POST", self.path, body=body, headers=headers)
                resp = conn.getresponse()
                text = resp.read().decode("utf-8", errors="replace")
                if resp.status == 200:
                    return text
                last_error = RuntimeError(f"HTTP {resp.status}: {text.strip()[:500]}")
                if 400 <= resp.status < 500:
                    break  # bitmagnet rejected an item; retrying would not help
            except (OSError, http.client.HTTPException) as exc:
                last_error = exc
            finally:
                conn.close()
        raise RuntimeError(f"bitmagnet import failed: {last_error}")

    def write(self, torrents: list[Torrent], schema: str, opts: Options) -> None:
        if not torrents:
            return
        body = "".join(dumps_item(import_item(t, schema, opts)) + "\n" for t in torrents).encode("utf-8")
        reply = self.post(body, opts.import_id)
        if "import complete" not in reply:
            log.warning("unexpected reply from bitmagnet: %s", reply.strip()[:200])
        self.requests += 1

    def describe(self) -> str:
        # Rebuilt from its parts so credentials in the URL are not echoed.
        port = f":{self.parsed.port}" if self.parsed.port else ""
        return f"bitmagnet import endpoint {self.parsed.scheme}://{self.parsed.hostname}{port}{self.path}"

    def stats(self) -> dict[str, int]:
        return {"import requests": self.requests, "retries": self.retried}

    def close(self) -> None:
        pass
