import json
import urllib.error
import urllib.request

import numpy as np
import pytest

from resonant_archive.embed import client as client_mod
from resonant_archive.embed.client import EmbedClient, EmbedderUnavailable, EmbedServerError
from resonant_archive.embed.server import NonLoopbackHost, make_server, require_loopback, EmbedApp


def post(url, body, raw=False, headers=None):
    data = body if raw else json.dumps(body).encode()
    req = urllib.request.Request(url + "/embed", data=data, method="POST",
                                 headers={"Content-Type": "application/json"} if headers is None else headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com", "", "::"])
def test_non_loopback_refused(host, fake_embedder):
    with pytest.raises(NonLoopbackHost):
        require_loopback(host)
    with pytest.raises(NonLoopbackHost):
        make_server(EmbedApp(fake_embedder), host, 0)


def test_bound_to_loopback(running_server):
    assert running_server.srv.server_address[0] == "127.0.0.1"


def test_health(running_server):
    with urllib.request.urlopen(running_server.url + "/health", timeout=10) as r:
        h = json.loads(r.read())
    assert h["status"] == "ok" and h["dim"] == 768 and h["model_sha256"] == "deadbeef"


def test_embed_roundtrip_and_task_passed(running_server, fake_embedder):
    code, out = post(running_server.url, {"texts": ["a", "b"], "task": "query", "dim": 256})
    assert code == 200 and out["dim"] == 256 and len(out["vectors"]) == 2 and len(out["vectors"][0]) == 256
    assert fake_embedder.calls[-1] == (["a", "b"], "query", 256)


@pytest.mark.parametrize("body", [
    {"texts": "nope"}, {"texts": [1]}, {"texts": ["a"], "task": "x"}, {"texts": ["a"], "dim": 100}, [1, 2],
])
def test_bad_requests_are_400(running_server, body):
    assert post(running_server.url, body)[0] == 400


def test_invalid_json_and_404(running_server):
    assert post(running_server.url, b"{not json", raw=True)[0] == 400
    req = urllib.request.Request(running_server.url + "/nope")
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=10)
    assert ei.value.code == 404


def test_too_many_texts_413(running_server, monkeypatch):
    from resonant_archive.embed import server
    monkeypatch.setattr(server, "MAX_TEXTS", 2)
    assert post(running_server.url, {"texts": ["a", "b", "c"]})[0] == 413


def test_server_never_echoes_text_on_failure(running_server):
    def boom(*a, **k):
        raise RuntimeError("SECRET-TEXT")
    running_server.app.embedder.embed = boom
    code, out = post(running_server.url, {"texts": ["SECRET-TEXT"]})
    assert code == 500 and "SECRET" not in json.dumps(out)


def test_client_uses_server(running_server):
    c = EmbedClient(running_server.url, fallback=False)
    v = c.embed(["x", "y", "z"], "document")
    assert v.shape == (3, 768) and c.mode == "server"
    assert c.embed_query("q").shape == (768,)


def test_client_batches_large_requests(running_server, fake_embedder, monkeypatch):
    monkeypatch.setattr(client_mod, "SERVER_BATCH", 4)
    c = EmbedClient(running_server.url, fallback=False)
    v = c.embed([f"t{i}" for i in range(10)], "document", 64)
    assert v.shape == (10, 64)
    assert [len(call[0]) for call in fake_embedder.calls] == [4, 4, 2]


def closed_url():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


def test_client_falls_back_in_process_when_no_server(fake_embedder):
    c = EmbedClient(closed_url())
    c._local = fake_embedder  # stands in for the lazily loaded model
    v = c.embed(["x"], "query")
    assert v.shape == (1, 768) and c.mode == "in-process"
    assert fake_embedder.calls[-1][1] == "query"


def test_client_no_fallback_raises(fake_embedder):
    with pytest.raises(EmbedderUnavailable):
        EmbedClient(closed_url(), fallback=False).embed(["x"])


def test_fallback_without_model_is_actionable(tmp_path):
    c = EmbedClient(closed_url(), model_dir=str(tmp_path))
    with pytest.raises(EmbedderUnavailable):
        c.embed(["x"])


def test_server_error_is_not_masked_by_fallback(running_server, fake_embedder):
    running_server.app.embedder.embed = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    c = EmbedClient(running_server.url)
    c._local = fake_embedder  # would succeed if (wrongly) used
    with pytest.raises(EmbedServerError):
        c.embed(["x"])


@pytest.mark.parametrize("url", ["http://example.com:8899", "https://127.0.0.1:8899", "http://10.0.0.5:8899"])
def test_client_refuses_non_loopback_url(url):
    with pytest.raises(ValueError):
        EmbedClient(url)


def test_empty_input_needs_no_server():
    assert EmbedClient(closed_url(), fallback=False).embed([]).shape == (0, 768)


def test_space_check_roundtrip(running_server, tmp_path):
    app = running_server.app
    app.space_ref = tmp_path / "space-reference.json"
    app.write_reference()
    ok, cos = app.check_space()
    assert ok and cos > 0.9999
    with pytest.raises(FileExistsError):
        app.write_reference()
    with urllib.request.urlopen(running_server.url + "/health?space=1", timeout=10) as r:
        assert json.loads(r.read())["same_space"] is True
    # move the space: reference now disagrees -> 503
    app.embedder.embed = lambda texts, task="document", dim=768: type(
        "R", (), {"vectors": -np.ones((len(texts), dim), dtype=np.float32) / np.sqrt(dim), "tokens": [1] * len(texts),
                  "truncated": [False] * len(texts)})()
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(running_server.url + "/health?space=1", timeout=10)
    assert ei.value.code == 503


# ---------------------------------------------------------------- LOW-5: Host, Content-Type, timeout

def raw_request(running_server, request: bytes) -> tuple[int, bytes]:
    import socket
    host, port = running_server.srv.server_address[:2]
    with socket.create_connection((host, port), timeout=10) as s:
        s.sendall(request)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    return int(data.split(b" ", 2)[1]), data


@pytest.mark.parametrize("hostname,ok", [
    ("127.0.0.1:8899", True), ("localhost:8899", True), ("LOCALHOST", True), ("[::1]:8899", True), ("127.0.0.1", True),
    ("evil.example:8899", False), ("evil.example", False), ("127.0.0.1.evil.example:80", False),
    ("192.168.1.5:8899", False), ("0.0.0.0:8899", False), ("", False), ("localhost.evil.example", False),
])
def test_host_header(hostname, ok):
    from resonant_archive.embed.server import host_allowed
    assert host_allowed(hostname) is ok
    assert host_allowed(None) is False


def test_a_rebound_hostname_is_refused_on_get_and_post(running_server, fake_embedder):
    get = b"GET /health HTTP/1.1\r\nHost: rebound.example:8899\r\nConnection: close\r\n\r\n"
    assert raw_request(running_server, get)[0] == 403
    body = b'{"texts":["a"]}'
    post_ = (b"POST /embed HTTP/1.1\r\nHost: rebound.example\r\nContent-Type: application/json\r\nContent-Length: "
             + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
    assert raw_request(running_server, post_)[0] == 403
    assert fake_embedder.calls == []                    # nothing was embedded for the refused request


def test_missing_host_is_refused(running_server):
    assert raw_request(running_server, b"GET /health HTTP/1.0\r\n\r\n")[0] == 403


def test_loopback_host_works(running_server):
    get = b"GET /health HTTP/1.1\r\nHost: localhost:1\r\nConnection: close\r\n\r\n"
    assert raw_request(running_server, get)[0] == 200


@pytest.mark.parametrize("ctype", [None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data"])
def test_post_needs_json_content_type(running_server, ctype, fake_embedder):
    headers = {"Content-Type": ctype} if ctype else {}
    code, out = post(running_server.url, {"texts": ["a"]}, headers=headers)
    # urllib adds a form content type when none is given, which must be refused as well
    assert code == 415 and "application/json" in out["error"]
    assert (["a"], "document", 768) not in fake_embedder.calls


def test_json_content_type_with_charset_is_fine(running_server):
    code, _ = post(running_server.url, {"texts": ["a"]}, headers={"Content-Type": "Application/JSON; charset=utf-8"})
    assert code == 200


def test_a_stalled_client_is_dropped(fake_embedder, monkeypatch):
    import socket
    import threading
    import time

    from resonant_archive.embed import server
    monkeypatch.setattr(server, "SOCKET_TIMEOUT", 0.5)
    srv = server.make_server(EmbedApp(fake_embedder), "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        s = socket.create_connection(srv.server_address[:2], timeout=10)
        s.sendall(b"POST /embed HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: 500\r\n\r\n{")
        t0 = time.time()
        got = s.recv(4096)                      # the server gives up and closes: EOF (or an error status), not a hang
        assert time.time() - t0 < 5
        assert got == b"" or b"HTTP/1.1 4" in got
        s.close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_handler_has_a_socket_timeout(running_server):
    from resonant_archive.embed import server
    handler = running_server.srv.RequestHandlerClass
    assert handler.timeout == server.SOCKET_TIMEOUT and 0 < handler.timeout <= 120
