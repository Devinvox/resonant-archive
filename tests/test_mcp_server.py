"""The MCP server over real stdio (newline-delimited JSON-RPC), against a real Postgres.

The subprocess has no embedding model and nothing on the embed port, so searches run on the embedder-down path."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys

import psycopg
import pytest

pytest.importorskip("mcp")

from . import synth  # noqa: E402


class Client:
    def __init__(self, env):
        self.p = subprocess.Popen([sys.executable, "-m", "resonant_archive.cli", "mcp"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, env=env)
        self._id = 0

    def _send(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def _recv(self, timeout=60):
        ready, _, _ = select.select([self.p.stdout], [], [], timeout)
        if not ready:
            self.p.kill()
            raise AssertionError("no reply from the MCP server; stderr: " + self.p.stderr.read()[-1500:])
        return json.loads(self.p.stdout.readline())

    def request(self, method, params=None):
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        return self._recv()

    def start(self):
        r = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "test", "version": "0"}})
        assert "result" in r, r
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return r["result"]

    def tools(self):
        return {t["name"]: t for t in self.request("tools/list")["result"]["tools"]}

    def call(self, name, **args):
        res = self.request("tools/call", {"name": name, "arguments": args})["result"]
        return res.get("isError", False), "".join(c.get("text", "") for c in res["content"])

    def close(self):
        self.p.stdin.close()
        try:
            self.p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.p.kill()


def _env(dsn, tmp_path):
    env = dict(os.environ)
    env.update(RESONANT_ARCHIVE_DSN=dsn, RESONANT_EMBEDDER_MODEL_DIR=str(tmp_path / "no-model"),
               RESONANT_EMBEDDER_PORT="9", HOME=str(tmp_path / "home"))
    env.pop("RESONANT_ARCHIVE_READONLY_DSN", None)
    return env


@pytest.fixture
def loaded(dsn, tmp_path):
    from resonant_archive import pipeline
    from resonant_archive.importers.base import Options

    synth.claude_code_session(tmp_path / "cc")
    pipeline.run_import("claude_code", tmp_path / "cc", dsn=dsn, options=Options())
    pipeline.run_import("chronicle_mirror", synth.mirror_tree(tmp_path / "mirror"), dsn=dsn, options=Options())
    return dsn


@pytest.fixture
def client(loaded, tmp_path):
    c = Client(_env(loaded, tmp_path))
    c.start()
    yield c
    c.close()


def test_tools_listed_with_drill_because_memories_exist(client):
    tools = client.tools()
    assert set(tools) == {"search", "around", "overview", "associate", "stats", "drill", "archive_search", "archive_stats"}
    assert {"query", "source", "speaker", "context", "date_from", "date_to", "hidden", "mode", "limit"} <= set(
        tools["search"]["inputSchema"]["properties"])
    assert tools["search"]["inputSchema"]["required"] == ["query"]


def test_search_falls_back_to_text_when_the_embedder_is_down(client):
    err, text = client.call("search", query="herbs")
    assert not err and "also add herbs" in text
    assert "skipped" in text.lower() or "vec" in text            # the answer says vectors were not used
    err, text = client.call("search", query="/srv/app/widget.py", mode="anchor")
    assert not err and "widget.py" in text


def test_around_overview_stats_drill(client):
    err, text = client.call("around", id="chronicle:mirror-sess-1:m4", before=1, after=1)
    assert not err and "also add herbs" in text
    err, text = client.call("overview", date_from="2026-03-01", date_to="2026-04-30")
    assert not err and "garden" in text
    err, text = client.call("stats")
    assert not err and "claude-code" in text and "chronicle" in text
    err, text = client.call("drill", memory="0")
    assert not err and "garden" in text


def test_bad_input_is_a_tool_error_with_a_plain_message(client):
    err, text = client.call("search", query="x", source="nope")
    assert err and "not one of chronicle, claude-code" in text
    err, text = client.call("around", id="does-not-exist")
    assert err and "no message with id" in text
    err, text = client.call("associate", id="chronicle:mirror-sess-1:m4")
    assert err and "no embedding" in text                        # nothing embedded in this test


def test_v01_aliases_still_work(client):
    err, text = client.call("archive_search", query="herbs", namespace="chronicle", n_results=1)
    assert not err and "also add herbs" in text and "shown 1" in text
    err, text = client.call("archive_stats")
    assert not err and "conversations" in text


def test_drill_is_absent_without_memories(dsn, tmp_path):
    from resonant_archive import pipeline
    from resonant_archive.importers.base import Options

    synth.claude_code_session(tmp_path / "cc")
    pipeline.run_import("claude_code", tmp_path / "cc", dsn=dsn, options=Options())
    c = Client(_env(dsn, tmp_path))
    try:
        c.start()
        assert "drill" not in c.tools()
        assert "search" in c.tools()
    finally:
        c.close()


def test_connection_cannot_write(loaded, tmp_path):
    """The server's session is read-only even on the owner's DSN."""
    from resonant_archive.mcp_server import Backend

    be = Backend(dsn=loaded)
    try:
        conn = be._connection()
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("DELETE FROM messages")
    finally:
        be.close()
    assert psycopg.connect(loaded).execute("SELECT count(*) FROM messages").fetchone()[0] == 16


def test_reconnects_after_the_connection_drops(loaded):
    from resonant_archive.mcp_server import Backend

    be = Backend(dsn=loaded)
    try:
        assert "claude-code" in be.call(lambda s: ",".join(s.sources()))
        be._conn.close()
        assert "claude-code" in be.call(lambda s: ",".join(s.sources()))
    finally:
        be.close()
