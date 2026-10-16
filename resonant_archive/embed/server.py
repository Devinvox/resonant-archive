"""Local text-embedding HTTP server (loopback only, no egress).

Besides binding loopback only, it refuses a request whose Host header is not a loopback name (DNS rebinding), a POST
that is not ``Content-Type: application/json`` (cross-site form posts), and a client that stalls for more than
``RESONANT_EMBEDDER_SOCKET_TIMEOUT`` seconds (default 30).

  POST /embed  {"texts": [...], "task": "document"|"query", "dim": 768}
           ->  {"vectors": [[...]], "model": "nomic-embed-text-v1.5", "dim": 768,
                "tokens": [n, ...], "truncated": [i, ...]}
  GET  /health -> {"status": "ok", ...}
  GET  /health?space=1 -> also embeds a fixed probe and compares it with the stored
       reference (space-reference.json beside the model): {"same_space": true, "min_cos": ...}.
       503 if the space moved or no reference exists. Fail closed: a model that answers
       but embeds differently would silently mix vector spaces in a store that trusts it.

"tokens" = tokens embedded per text (task prefix + [CLS]/[SEP] included, max 2048).
"truncated" = indices of texts cut to the 2048-token cap.

Never logs request text. Binds loopback only; any other host is refused. The server
makes no outbound connections of any kind.

Run:  python -m resonant_archive.embed.server [--host H] [--port P] [--write-reference]
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import paths
from .nomic import FULL_DIM, MATRYOSHKA_DIMS, MAX_TOKENS, MODEL_NAME, PREFIXES

LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
MAX_TEXTS = int(os.environ.get("RESONANT_EMBEDDER_MAX_TEXTS", "256"))
MAX_BODY = int(os.environ.get("RESONANT_EMBEDDER_MAX_BODY", str(16 * 1024 * 1024)))
SPACE_MIN_COS = float(os.environ.get("RESONANT_EMBEDDER_SPACE_MIN_COS", "0.9999"))
# Fixed probe: one per task, never changed (changing it means writing a new reference).
PROBE = {
    "document": "The lighthouse keeper logs the tide each evening; the harbour remembers what it is told.",
    "query": "who watches the harbour?",
}


class NonLoopbackHost(ValueError):
    """The embed server refuses to bind anything but loopback."""


def require_loopback(host: str) -> str:
    if host not in LOOPBACK_HOSTS:
        raise NonLoopbackHost(f"embed server: refusing to bind non-loopback host {host!r}")
    return host


#: Seconds a connection may sit idle, or a request body may stall, before the socket is closed.
SOCKET_TIMEOUT = float(os.environ.get("RESONANT_EMBEDDER_SOCKET_TIMEOUT", "30"))


def host_allowed(header: str | None) -> bool:
    """Is a Host header one a loopback client would send? A web page on another origin that rebinds its DNS name to
    127.0.0.1 still sends its own name in Host, so refusing any name that is not loopback shuts that door."""
    if not header:
        return False
    h = header.strip().lower()
    if h.startswith("["):                       # [::1]:8899
        h = h[1:].split("]", 1)[0]
    elif h.count(":") == 1:                     # name:port or 1.2.3.4:port
        h = h.split(":", 1)[0]
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}", flush=True)


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb)


class EmbedApp:
    """State shared by all request threads: the encoder, its lock, counters."""

    def __init__(self, embedder: Any, *, threads: int = 4, space_ref: Path | None = None,
                 model_sha256: str | None = None) -> None:
        self.embedder = embedder
        self.threads = threads
        self.space_ref = space_ref
        self.model_sha256 = model_sha256
        self.lock = threading.Lock()  # one forward pass at a time; ORT already uses `threads` cores
        self.started = time.time()
        self.stats = {"requests": 0, "texts": 0, "errors": 0}

    def probe_vectors(self) -> dict[str, list[float]]:
        with self.lock:
            return {t: self.embedder.embed([s], t, FULL_DIM).vectors[0].tolist() for t, s in PROBE.items()}

    def check_space(self) -> tuple[bool, float]:
        if self.space_ref is None:
            raise ValueError("no space reference configured")
        with open(self.space_ref, encoding="utf-8") as f:
            ref = json.load(f)
        if ref.get("probe") != PROBE or ref.get("model") != MODEL_NAME:
            raise ValueError("reference was written for a different probe or model")
        now = self.probe_vectors()
        m = min(_cos(v, ref["vectors"][t]) for t, v in now.items())
        return m >= SPACE_MIN_COS, m

    def write_reference(self) -> Path:
        if self.space_ref is None:
            raise ValueError("no space reference path")
        if self.space_ref.exists():
            raise FileExistsError(f"{self.space_ref} exists; remove it deliberately to rewrite")
        with open(self.space_ref, "w", encoding="utf-8") as f:
            json.dump({"model": MODEL_NAME, "model_sha256": self.model_sha256, "probe": PROBE,
                       "written": time.strftime("%Y-%m-%dT%H:%M:%S"), "vectors": self.probe_vectors()}, f)
        return self.space_ref


def make_handler(app: EmbedApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "resonant-embedder/1"
        protocol_version = "HTTP/1.1"
        timeout = SOCKET_TIMEOUT   # StreamRequestHandler applies it to the socket: a stalled client is dropped

        def _host_ok(self) -> bool:
            if host_allowed(self.headers.get("Host")):
                return True
            self._err(403, "host not allowed")
            return False

        def log_message(self, fmt, *args):  # default access log is replaced by our own lines
            pass

        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _err(self, code: int, msg: str) -> None:
            app.stats["errors"] += 1
            self.close_connection = True  # body may be unread; don't reuse the stream
            log(f"{self.command} {self.path} {code} {msg}")
            self._send(code, {"error": msg})

        def do_GET(self):
            if not self._host_ok():
                return
            path, _, qs = self.path.partition("?")
            if path.rstrip("/") != "/health":
                return self._err(404, "not found")
            if "space=1" in qs.split("&"):
                try:
                    ok, min_cos = app.check_space()
                except Exception as e:
                    return self._err(503, f"space check failed: {type(e).__name__}: {str(e)[:120]}")
                return self._send(200 if ok else 503, {
                    "status": "ok" if ok else "space_moved", "model": MODEL_NAME, "same_space": ok,
                    "min_cos": round(min_cos, 6), "threshold": SPACE_MIN_COS,
                    "model_sha256": app.model_sha256,
                })
            self._send(200, {
                "status": "ok", "model": MODEL_NAME, "dim": FULL_DIM, "dims": list(MATRYOSHKA_DIMS),
                "max_tokens": MAX_TOKENS, "max_texts": MAX_TEXTS, "threads": app.threads,
                "busy": app.lock.locked(), "uptime_s": round(time.time() - app.started),
                "model_sha256": app.model_sha256, **app.stats,
            })

        def do_POST(self):
            if not self._host_ok():
                return
            if self.path.rstrip("/") != "/embed":
                return self._err(404, "not found")
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":     # a cross-site form post cannot send this without a preflight
                return self._err(415, "Content-Type must be application/json")
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._err(400, "bad Content-Length")
            if n <= 0 or n > MAX_BODY:
                return self._err(413 if n > MAX_BODY else 400, f"body must be 1..{MAX_BODY} bytes")
            try:
                req = json.loads(self.rfile.read(n))
            except (ValueError, UnicodeDecodeError):
                return self._err(400, "invalid JSON")
            if not isinstance(req, dict):
                return self._err(400, "body must be a JSON object")
            texts = req.get("texts")
            task = req.get("task", "document")
            dim = req.get("dim", FULL_DIM)
            if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
                return self._err(400, "texts must be a list of strings")
            if len(texts) > MAX_TEXTS:
                return self._err(413, f"at most {MAX_TEXTS} texts per request")
            if task not in PREFIXES:
                return self._err(400, f"task must be one of {sorted(PREFIXES)}")
            if dim not in MATRYOSHKA_DIMS:
                return self._err(400, f"dim must be one of {list(MATRYOSHKA_DIMS)}")
            t0 = time.perf_counter()
            try:
                with app.lock:
                    res = app.embedder.embed(texts, task, dim)
            except Exception as e:  # never echo text back; type only
                return self._err(500, f"embed failed: {type(e).__name__}")
            dt = time.perf_counter() - t0
            trunc = [i for i, t in enumerate(res.truncated) if t]
            app.stats["requests"] += 1
            app.stats["texts"] += len(texts)
            log(f"POST /embed 200 n={len(texts)} task={task} dim={dim} tokens={sum(res.tokens)} "
                f"truncated={len(trunc)} {dt * 1000:.0f}ms")
            self._send(200, {
                "vectors": res.vectors.tolist(), "model": MODEL_NAME, "dim": dim,
                "tokens": res.tokens, "truncated": trunc,
            })

    return Handler


class _V6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def make_server(app: EmbedApp, host: str, port: int) -> ThreadingHTTPServer:
    """Build (but do not start) the HTTP server. Raises NonLoopbackHost for non-loopback hosts."""
    require_loopback(host)
    cls = _V6Server if host == "::1" else ThreadingHTTPServer
    cls.request_queue_size = 64
    srv = cls((host, port), make_handler(app))
    srv.daemon_threads = True
    return srv


def serve(host: str | None = None, port: int | None = None, *, model_dir: str | os.PathLike | None = None,
          threads: int = 4, write_reference: bool = False, verify: bool = True) -> int:
    """Load the model (verified against the pinned sums) and serve until interrupted."""
    from .fetch import verify_cached
    from .nomic import Embedder

    host = require_loopback(host or paths.server_host())
    port = paths.server_port() if port is None else port
    mdir = Path(model_dir) if model_dir else paths.model_dir()
    sums = verify_cached(mdir) if verify else {}
    t0 = time.time()
    emb = Embedder(model_dir=mdir, threads=threads, verify=False)  # just verified above
    emb.embed(["warm up"], "query")
    app = EmbedApp(emb, threads=threads, space_ref=mdir / "space-reference.json",
                   model_sha256=sums.get("model.onnx"))
    if write_reference:
        log(f"wrote space reference {app.write_reference()}")
        return 0
    log(f"model {MODEL_NAME} loaded in {time.time() - t0:.1f}s, threads={threads}")
    srv = make_server(app, host, port)
    log(f"listening on http://{host}:{port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="resonant-embedder", description="Local embed server (loopback only).")
    ap.add_argument("--host", default=None, help="loopback address only (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=None, help="default 8899")
    ap.add_argument("--model-dir", default=None)
    ap.add_argument("--threads", type=int, default=int(os.environ.get("RESONANT_EMBEDDER_THREADS", "4")))
    ap.add_argument("--write-reference", action="store_true", help="write space-reference.json once, then exit")
    a = ap.parse_args(argv)
    try:
        return serve(a.host, a.port, model_dir=a.model_dir, threads=a.threads, write_reference=a.write_reference)
    except (NonLoopbackHost, FileExistsError) as e:
        print(f"resonant-embedder: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # ModelIntegrityError, FileNotFoundError, RuntimeError
        print(f"resonant-embedder: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
