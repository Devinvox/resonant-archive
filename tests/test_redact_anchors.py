from __future__ import annotations

from resonant_archive import anchors
from resonant_archive.redact import Redactor, load_known, redact

from .synth import FAKE_DB, FAKE_GH, FAKE_SK


def test_each_pattern_hits_and_near_misses_do_not():
    assert "[redacted:api-key]" in redact(f"key {FAKE_SK}")
    assert "[redacted:github-token]" in redact(FAKE_GH)
    assert "[redacted:aws-key]" in redact("AKIA" + "ABCDEFGH12345678")
    assert "[redacted:jwt]" in redact("eyJhbGciOiJIUzI1" + ".eyJzdWIiOiIxMjM0NTY3" + ".SflKxwRJSMeKKF2QT4")
    assert "[redacted:token]" in redact("Authorization: Bearer abcdefghijklmnop12345")
    assert "[redacted:password]@" in redact(FAKE_DB) and "s3cretpassw0rd" not in redact(FAKE_DB)
    assert "/mcp/[redacted:secret-path]" in redact("https://x.example.test/mcp/abcdefghijklmnop1234")
    assert "[redacted:private-key]" in redact("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----")
    assert "[redacted:private-key]" in redact("-----BEGIN PRIVATE KEY-----\nno end in sight")
    assert "abc123def456" not in redact("https://h.example.test/x?token=abc123def456")
    assert redact("MY_API_KEY=supersecretvalue1").endswith("[redacted:secret]")
    # near misses
    assert redact("sk-short") == "sk-short"
    assert redact("the word token alone, or password reset") == "the word token alone, or password reset"
    assert redact("https://example.test/docs/mcp/overview") == "https://example.test/docs/mcp/overview"


def test_redaction_is_idempotent_and_counts_only_real_changes():
    r = Redactor()
    once = r(f"export API_TOKEN=abcdefghijk123 and {FAKE_SK}")
    first = dict(r.counts)
    assert r(once) == once
    assert dict(r.counts) == first


def test_known_values_are_removed_everywhere(tmp_path):
    f = tmp_path / "secrets.env"
    f.write_text("SERVICE_PASSWORD=Hunter2Hunter2Zz\n# a comment\nSHORT=abc\n")
    known = load_known([str(f)])
    assert "Hunter2Hunter2Zz" in known and "abc" not in known
    r = Redactor(known)
    assert "Hunter2" not in r("my password is Hunter2Hunter2Zz, really Hunter2Hunter2Zz")
    assert r.counts["known"] == 2


def test_known_from_json_config(tmp_path):
    import json
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"servers": {"a": {"url": "https://h.example.test/mcp/ABCDEFGHIJKLMNOPQRST",
                                                  "headers": {"Authorization": "Bearer tokentokentoken1234"}}}}))
    vals = load_known([], [str(cfg)])
    assert "ABCDEFGHIJKLMNOPQRST" in vals and "tokentokentoken1234" in vals


def test_load_known_has_no_default_paths():
    assert load_known([]) == []


def ex(text):
    return anchors.extract(text)


def test_anchor_kinds():
    got = ex("see https://example.test/a/b?x=1 and /srv/app/widget.py, ~/notes/todo.md; commit abc1234f merged. "
             "PR #42, v1.2.3, `some_function_name`, handle @robo_helper")
    kinds = {k for _, k in got}
    assert {"url", "path", "sha", "pr", "version", "quote", "handle"} <= kinds
    assert ("/srv/app/widget.py", "path") in got and ("abc1234f", "sha") in got and ("#42", "pr") in got


def test_secret_shaped_strings_are_never_anchored():
    got = ex(f"url https://example.test/cb?token=abcd1234 and {FAKE_SK} and `Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8`")
    toks = {t for t, _ in got}
    assert not any("token=" in t or t.startswith("sk-") or "Ab1Cd2" in t for t in toks)


def test_empty_and_plain_prose_make_no_anchors():
    assert ex("") == set() and ex("just some friendly words here") == set()
