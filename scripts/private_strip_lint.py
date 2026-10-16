#!/usr/bin/env python3
"""Fail if the shipped tree mentions anything that belongs to a private setup.

    python scripts/private_strip_lint.py [--root DIR] [--config FILE] [--allowlist FILE] [--terms-file FILE]
                                     [--only PATH_PREFIX]

Scans every text file git knows about (tracked, or untracked and not ignored); outside a git checkout it walks the
folder. Prints ``path:line: term: text`` for each hit and exits 1 if there are any.

Two sources of rules:

* the PUBLIC defaults in private_strip_lint.toml: secret shapes and real-looking home directories. Nothing in it
  names a person or a machine, so it is safe to publish. (Its shape rules skip the test fixtures that hold invented
  credentials on purpose; private terms are checked everywhere.)
* YOUR private terms: a file outside the repository, named by the environment variable PRIVATE_STRIP_TERMS_FILE (or
  ``--terms-file``). It is never committed. Without it the lint checks the public defaults only and says so. If the
  variable is set but the file cannot be read, or yields no rules, or holds an empty rule (``word:`` with nothing after
  it, an empty regular expression: it would match every line), the lint stops with exit code 2 rather than pass
  silently.

When the environment variable ``CI`` is ``true`` the output names only ``path:line`` for each hit: never the matched
term or the line, so a private term cannot reach a public build log.

The private file is plain text, one term per line (``#`` starts a comment)::

    word:alice            a whole word, any case ("alicia" and "alice_x" do not match)
    sub:my-host.example   anywhere in a line, any case
    re:\\b5432\\d\\b         a regular expression, any case
    /home/alice           a bare line is a substring too

A ``.toml`` file may be used instead, with the same ``words``, ``substrings``, ``patterns`` and ``exclude`` keys as
private_strip_lint.toml; its rules are added to the defaults. The reasoned exceptions are in
private_strip_allowlist.txt.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAX_BYTES = 2_000_000
ENV_TERMS = "PRIVATE_STRIP_TERMS_FILE"

#: (name, pattern, path globs this rule skips). Only the shape rules skip fixtures; private terms scan everything.
Rule = tuple[str, "re.Pattern[str]", tuple]


def _word(w: str) -> Rule:
    return w, re.compile(r"(?<![A-Za-z0-9_])" + re.escape(w) + r"(?![A-Za-z0-9_])", re.I), ()


def _sub(s: str) -> Rule:
    return s, re.compile(re.escape(s), re.I), ()


class TermsError(Exception):
    """The private terms file is unusable. The message never holds a term (it may be printed in a public CI log)."""


def in_ci() -> bool:
    return os.environ.get("CI", "").strip().lower() == "true"


def _rules_from_cfg(cfg: dict) -> tuple[list[Rule], list[str]]:
    rules: list[Rule] = [_word(w) for w in cfg.get("words", [])] + [_sub(s) for s in cfg.get("substrings", [])]
    for p in cfg.get("patterns", []):
        skip = tuple(cfg.get("pattern_exclude", [])) + tuple(p.get("exclude", []))
        rules.append((p["name"], re.compile(p["regex"], re.I), skip))
    return rules, list(cfg.get("exclude", []))


def _check_toml_terms(cfg: dict) -> None:
    for key in ("words", "substrings"):
        if any(not str(w).strip() for w in cfg.get(key, [])):
            raise TermsError(f"the terms file has an empty entry under '{key}'")
    for p in cfg.get("patterns", []):
        try:
            ok = bool(str(p.get("regex", "")).strip()) and not re.compile(p["regex"], re.I).search("")
        except (re.error, TypeError):
            raise TermsError("the terms file has a pattern that is not a valid regular expression")
        if not ok:
            raise TermsError("the terms file has an empty pattern, or one that matches every line")


def load_config(path: Path) -> tuple[list[Rule], list[str]]:
    return _rules_from_cfg(tomllib.loads(path.read_text(encoding="utf-8")))


def load_terms(path: Path) -> tuple[list[Rule], list[str]]:
    """Rules from the private terms file (plain text, or TOML when the name ends in .toml). Raises ``TermsError`` when
    the file yields no rule at all or holds an empty one; the error names the file and line, never the term."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".toml":
        cfg = tomllib.loads(text)
        _check_toml_terms(cfg)
        rules, exclude = _rules_from_cfg(cfg)
        if not rules:
            raise TermsError("the terms file yields no rules")
        return rules, exclude
    rules: list[Rule] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kind, _, body = line.partition(":")
        kind = kind if kind in ("word", "sub", "re") else None
        body = (line.split(":", 1)[1] if kind else line).strip()
        if not body:
            raise TermsError(f"{path.name}:{n}: empty rule")
        if kind == "word":
            rules.append(_word(body))
        elif kind == "re":
            try:
                rx = re.compile(body, re.I)
            except re.error:
                raise TermsError(f"{path.name}:{n}: not a valid regular expression")
            if rx.search(""):
                raise TermsError(f"{path.name}:{n}: a regular expression that matches the empty string would match every line")
            rules.append((body, rx, ()))
        else:
            rules.append(_sub(body))
    if not rules:
        raise TermsError(f"{path.name}: the terms file yields no rules (only blank lines and comments?)")
    return rules, []


def load_allowlist(path: Path) -> list[tuple[str, str, re.Pattern | None]]:
    out: list[tuple[str, str, re.Pattern | None]] = []
    if not path.exists():
        return out
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(" | ")]
        if len(parts) < 3:
            raise SystemExit(f"{path}:{n}: expected '<path glob> | <term> | <line regex or *> | <reason>'")
        glob, term, pattern = parts[0], parts[1].lower(), parts[2]
        out.append((glob, term, None if pattern == "*" else re.compile(pattern, re.I)))
    return out


def list_files(root: Path) -> list[str]:
    try:
        res = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             capture_output=True, check=True)
        names = [n for n in res.stdout.decode("utf-8", "surrogateescape").split("\0") if n]
        return sorted(set(n for n in names if (root / n).is_file()))
    except (OSError, subprocess.CalledProcessError):
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def matches_any(rel: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, g) for g in globs)


def scan(root: Path, rules, exclude: list[str], allow, skip: set[Path] = frozenset(),
         only: str | None = None) -> list[tuple[str, int, str, str]]:
    hits: list[tuple[str, int, str, str]] = []
    for rel in list_files(root):
        if matches_any(rel, exclude) or (only and not rel.startswith(only)):
            continue
        path = root / rel
        if path.resolve() in skip:
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue  # binary
        text = data.decode("utf-8", "replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            for term, rx, skip_globs in rules:
                if not rx.search(line) or matches_any(rel, list(skip_globs)):
                    continue
                if any(fnmatch.fnmatch(rel, g) and t == term.lower() and (p is None or p.search(line))
                       for g, t, p in allow):
                    continue
                hits.append((rel, lineno, term, line.strip()[:160]))
    return hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=HERE.parent)
    ap.add_argument("--config", type=Path, default=HERE / "private_strip_lint.toml")
    ap.add_argument("--allowlist", type=Path, default=HERE / "private_strip_allowlist.txt")
    ap.add_argument("--only", default=None, metavar="PREFIX", help="scan only files whose path starts with PREFIX")
    ap.add_argument("--terms-file", type=Path, default=None,
                    help=f"your private terms (default: ${ENV_TERMS}); never commit it")
    args = ap.parse_args(argv)
    rules, exclude = load_config(args.config)
    terms_path = args.terms_file or (Path(os.environ[ENV_TERMS]).expanduser() if os.environ.get(ENV_TERMS) else None)
    skip: set[Path] = set()
    if terms_path is not None:
        try:
            extra, extra_exclude = load_terms(terms_path)
        except TermsError as exc:
            print(f"private-strip lint: unusable private terms file: {exc}", file=sys.stderr)
            return 2
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            reason = "it could not be read" if in_ci() else f"{type(exc).__name__}: {exc}"
            print(f"private-strip lint: cannot read the private terms file ({reason})", file=sys.stderr)
            return 2
        rules += extra
        exclude += extra_exclude
        skip.add(terms_path.resolve())   # a terms file kept inside the tree must not flag itself
    else:
        print(f"private-strip lint: no private terms file (set {ENV_TERMS}); checked the public defaults only",
              file=sys.stderr)
    hits = scan(args.root.resolve(), rules, exclude, load_allowlist(args.allowlist), skip, args.only)
    for rel, lineno, term, line in hits:
        print(f"{rel}:{lineno}" if in_ci() else f"{rel}:{lineno}: {term}: {line}")
    files = len({h[0] for h in hits})
    if hits:
        print(f"private-strip lint: {len(hits)} hit(s) in {files} file(s)", file=sys.stderr)
        return 1
    print("private-strip lint: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
