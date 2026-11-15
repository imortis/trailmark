import socket
import threading
from types import SimpleNamespace

from trailmark.curve import paired_ci
from trailmark.hydra import _decode_cell, _Wire
from trailmark.present import snippet


def test_paired_ci_brackets_the_mean_difference():
    a = [False] * 50 + [True] * 50
    b = [True] * 60 + [False] * 40
    diff, lo, hi = paired_ci(a, b)
    assert abs(diff - (60 - 50) / 100) < 1e-9
    assert lo <= diff <= hi
    # identical inputs give a zero-width interval at zero
    assert paired_ci(a, a) == (0.0, 0.0, 0.0)


def test_snippet_cuts_long_functions_and_says_how_much_is_left(tmp_path):
    (tmp_path / "m.py").write_text("\n".join(f"line {i}" for i in range(1, 101)), encoding="utf-8")
    ws = SimpleNamespace(root=tmp_path)
    short = snippet(ws, "m.py", "3-5")
    assert short == "line 3\nline 4\nline 5"
    long = snippet(ws, "m.py", "10-80")
    assert long.startswith("line 10\n") and long.endswith("... (41 more lines)")
    assert snippet(ws, "missing.py", "1-2") == ""


def _serve_once(responses: list[bytes]) -> tuple[str, threading.Thread]:
    """A one-connection HTTP server that answers each request with the next canned response."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def run():
        conn, _ = srv.accept()
        with conn:
            for resp in responses:
                buf = b""
                while b"\r\n\r\n" not in buf:
                    buf += conn.recv(65536)
                head, _, body = buf.partition(b"\r\n\r\n")
                length = next((int(l.split(b":")[1]) for l in head.split(b"\r\n") if l.lower().startswith(b"content-length")), 0)
                while len(body) < length:
                    body += conn.recv(65536)
                conn.sendall(resp)
        srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return f"http://127.0.0.1:{port}", t


def test_wire_reuses_one_connection_and_reads_both_body_framings():
    plain = b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 15\r\n\r\n{"status":"ok"}'
    chunked = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
               b"5\r\n{\"a\":\r\n3\r\n12}\r\n0\r\n\r\n")
    url, t = _serve_once([plain, chunked])
    wire = _Wire(url, {"Authorization": "Bearer x"}, timeout=5)
    assert wire.request("GET", "/healthz") == (200, b'{"status":"ok"}')
    assert wire.request("POST", "/q", b'{"q":1}') == (200, b'{"a":12}')  # second request, same socket
    t.join(timeout=5)


def test_decode_cell_handles_nulls_scalars_and_paths():
    assert _decode_cell({"type": "integer"}) is None
    assert _decode_cell({"type": "vertex_id", "value": 7}) == 7
    path = _decode_cell({"type": "path", "value": {
        "nodes": [{"id": 1, "labels": ["File"], "properties": {"path": {"String": "a.py"}}},
                  {"id": 2, "labels": ["File"], "properties": {"path": {"String": "b.py"}}}],
        "relationships": [{"edge_type": "IMPORTS", "src": 1, "dst": 2, "properties": {"w": {"Float": 0.5}}}],
    }})
    assert path["nodes"][1]["path"] == "b.py"
    assert path["rels"][0] == {"type": "IMPORTS", "src": 1, "dst": 2, "w": 0.5}
