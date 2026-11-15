"""Thin HydraDB client over the HTTP JSON query API. Standard library only.

HydraDB OSS speaks an OpenCypher subset. The quirks this client hides:
  * every request needs a ``cell_id``;
  * result cells are typed ``{"type": ..., "value": ...}`` and a null has no value;
  * UNWIND batches are capped at 1024 rows, so writes are chunked;
  * pages are capped at 4096 rows and a cursor must be resent with the full request.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit

BATCH_ROWS = 1000
PAGE_ROWS = 4096
POOL_WORKERS = 16


class HydraError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, query: str):
        super().__init__(f"HydraDB {status} {code}: {message}\n  query: {query[:300]}")
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class HydraConfig:
    url: str = "http://127.0.0.1:8443"
    token: str = "local-development-token-32-bytes"
    graph: str = "default"
    cell: str = "cell-0"
    namespace: str = "default"

    @classmethod
    def from_env(cls) -> "HydraConfig":
        d = cls()
        return cls(
            url=os.environ.get("HYDRADB_URL", d.url).rstrip("/"),
            token=os.environ.get("HYDRADB_TOKEN", d.token),
            graph=os.environ.get("HYDRADB_GRAPH", d.graph),
            cell=os.environ.get("HYDRADB_CELL", d.cell),
            namespace=os.environ.get("HYDRADB_NAMESPACE", d.namespace),
        )


def _unwrap_tagged(v: Any) -> Any:
    """Path payloads tag property values as {"String": "x"}; unwrap them."""
    if isinstance(v, dict) and len(v) == 1:
        (k, inner), = v.items()
        if k in ("String", "Integer", "Float", "Boolean", "Bool"):
            return inner
        if k == "Null":
            return None
    return v


def _decode_cell(cell: Any) -> Any:
    if not isinstance(cell, dict) or "type" not in cell:
        return cell
    if "value" not in cell:
        return None
    value = cell["value"]
    if cell["type"] == "path" and isinstance(value, dict):
        nodes = [
            {
                "id": n.get("id"),
                "labels": n.get("labels", []),
                **{k: _unwrap_tagged(v) for k, v in (n.get("properties") or {}).items()},
            }
            for n in value.get("nodes", [])
        ]
        rels = [
            {
                "type": r.get("edge_type"),
                "src": r.get("src"),
                "dst": r.get("dst"),
                **{k: _unwrap_tagged(v) for k, v in (r.get("properties") or {}).items()},
            }
            for r in value.get("relationships", [])
        ]
        return {"nodes": nodes, "rels": rels}
    return value


class _Wire:
    """Minimal HTTP/1.1 keep-alive transport, one socket per thread.

    Why not an HTTP library: the ones tried here write the request head and
    body as two segments, and against a node behind a loopback relay
    (Windows -> WSL) that cost ~44 ms per request. Writing each request with
    a single ``sendall`` measured under 1 ms on the same connection.
    """

    def __init__(self, url: str, headers: dict[str, str], timeout: float):
        parts = urlsplit(url)
        if parts.scheme != "http":
            raise ValueError("only http:// endpoints are supported; terminate TLS in front of a remote node")
        self.host, self.port = parts.hostname or "127.0.0.1", parts.port or 80
        self.timeout = timeout
        merged = {"Host": f"{self.host}:{self.port}", **headers}
        self._head = "".join(f"{k}: {v}\r\n" for k, v in merged.items())
        self._local = threading.local()

    def _sock(self) -> socket.socket:
        sock = getattr(self._local, "sock", None)
        if sock is None:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._local.sock = sock
        return sock

    def _drop(self) -> None:
        sock = getattr(self._local, "sock", None)
        self._local.sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def request(self, method: str, path: str, body: bytes = b"") -> tuple[int, bytes]:
        head = f"{method} {path} HTTP/1.1\r\n{self._head}Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        for attempt in (0, 1):
            reused = getattr(self._local, "sock", None) is not None
            try:
                sock = self._sock()
                sock.sendall(head + body)
                return self._read(sock)
            except (OSError, ConnectionError):
                self._drop()
                # A reused connection may have been closed by the server while
                # idle: retry once on a new one. A fresh connection that fails
                # is a real failure.
                if not reused or attempt:
                    raise
        raise ConnectionError("unreachable")

    def _read(self, sock: socket.socket) -> tuple[int, bytes]:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed before response headers")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split(" ", 2)[1])
        hdr = {k.strip().lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}

        def more() -> bytes:
            chunk = sock.recv(1 << 20)
            if not chunk:
                raise ConnectionError("connection closed mid-response")
            return chunk

        if hdr.get("transfer-encoding", "").lower() == "chunked":
            body = b""
            while True:
                while b"\r\n" not in rest:
                    rest += more()
                size_line, _, rest = rest.partition(b"\r\n")
                size = int(size_line.split(b";")[0], 16)
                while len(rest) < size + 2:
                    rest += more()
                body, rest = body + rest[:size], rest[size + 2 :]
                if size == 0:
                    break
        else:
            need = int(hdr.get("content-length", "0"))
            while len(rest) < need:
                rest += more()
            body = rest[:need]
        if hdr.get("connection", "").lower() == "close":
            self._drop()
        return status, body


class Hydra:
    """Synchronous client. Safe to share across threads."""

    def __init__(self, config: HydraConfig | None = None, timeout: float = 60.0):
        self.config = config or HydraConfig.from_env()
        self._wire = _Wire(
            self.config.url,
            {
                "Authorization": f"Bearer {self.config.token}",
                "Content-Type": "application/json",
                "X-Graph-Namespace": self.config.namespace,
            },
            timeout,
        )
        # One long-lived pool so each worker thread keeps its connection.
        self._pool = ThreadPoolExecutor(max_workers=POOL_WORKERS, thread_name_prefix="hydra")
        self._path = f"/v1/graphs/{self.config.graph}/query"
        self.queries = 0  # counted so callers can report DB round-trips
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._pool.shutdown(wait=False)

    def __enter__(self) -> "Hydra":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def health(self) -> bool:
        try:
            status, body = self._wire.request("GET", "/healthz")
            return status == 200 and json.loads(body).get("status") == "ok"
        except (OSError, ValueError):
            return False

    # -- core --------------------------------------------------------------
    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        status, raw = self._wire.request("POST", self._path, json.dumps(body).encode("utf-8"))
        with self._lock:
            self.queries += 1
        if status != 200:
            code, message = "http_error", raw[:500].decode("utf-8", "replace")
            try:
                err = json.loads(raw).get("error", {})
                code, message = err.get("code", code), err.get("message", message)
            except (ValueError, AttributeError):
                pass
            raise HydraError(status, code, message, body.get("query", ""))
        return json.loads(raw)

    def query(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        """Run a read and return rows as dicts, following pagination."""
        body: dict[str, Any] = {"cell_id": self.config.cell, "query": cypher, "page_size": PAGE_ROWS}
        if params:
            body["parameters"] = params
        out: list[dict[str, Any]] = []
        while True:
            data = self._post(body)
            cols = data.get("columns", [])
            for row in data.get("rows", []):
                out.append({c: _decode_cell(v) for c, v in zip(cols, row)})
            cursor = data.get("next_cursor")
            if not cursor:
                return out
            body["cursor"] = cursor
            body["query_id"] = data.get("query_id")

    def run(self, cypher: str, **params: Any) -> None:
        """Run a write (auto-commits)."""
        body: dict[str, Any] = {"cell_id": self.config.cell, "query": cypher}
        if params:
            body["parameters"] = params
        self._post(body)

    def batch(self, cypher: str, rows: Sequence[dict[str, Any]], chunk: int = BATCH_ROWS) -> int:
        """Run an ``UNWIND $rows AS row ...`` write in chunks under the 1024 cap."""
        n = 0
        for i in range(0, len(rows), chunk):
            part = list(rows[i : i + chunk])
            self.run(cypher, rows=part)
            n += len(part)
        return n

    def run_many(self, cypher: str, param_sets: Iterable[dict[str, Any]], workers: int = POOL_WORKERS) -> int:
        """Run one parameterised write per item, concurrently.

        Needed where HydraDB has no batch form (edge properties cannot be set
        inside an UNWIND batch). ``workers`` is accepted for compatibility; the
        shared pool decides concurrency.
        """
        items = list(param_sets)
        list(self._pool.map(lambda p: self.run(cypher, **p), items))
        return len(items)

    def query_many(self, cypher: str, param_sets: Iterable[dict[str, Any]], workers: int = POOL_WORKERS) -> list[list[dict[str, Any]]]:
        """Run one parameterised read per item, concurrently, preserving order."""
        items = list(param_sets)
        if len(items) == 1:
            return [self.query(cypher, **items[0])]
        return list(self._pool.map(lambda p: self.query(cypher, **p), items))
