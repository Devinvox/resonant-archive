"""Exact-recall anchors pulled from visible archive text: the strings a person may later search for verbatim.

Kinds: url, path, sha, pr, snowflake, handle, quote, version, ident.
Nothing secret-shaped is ever anchored: the text is already redacted, and every candidate is checked again
against the redaction patterns, '[redacted:' markers, credential-ish query strings and random-looking tokens.
"""
import re

from .redact import SECRET_REGEXES

MAX_TOKEN = 300

URL = re.compile(r'https?://[^\s<>"\'`)\]}|]+')
PATH = re.compile(r'(?<![\w.:/-])(?:~|[A-Za-z]:)?(?:/[\w.@+%-]+){2,}/?')
WIN_PATH = re.compile(r'(?<![\w])[A-Za-z]:[\\/](?:[\w.@+ -]+[\\/])*[\w.@+-]+')
HOME_PATH = re.compile(r'(?<![\w/])~/[\w.@+%/-]+')
HEX = re.compile(r'\b[0-9a-f]{7,40}\b')
GIT_WORDS = re.compile(r'(?i)\b(git|commit(?:ted|s)?|sha|merged?|push(?:ed)?|HEAD|branch|cherry-pick|rebase|revert(?:ed)?|deployed)\b')
PR = re.compile(r'(?i)\b(?:PR|pull(?:\s+request)?|issue|ticket)s?\s*(?:#|no\.?\s*|number\s*)?(\d{1,6})\b|(?<![\w&])#(\d{1,6})\b')
PR_NEAR = re.compile(r'(?i)\b(PR|pull|issue|issues|github|gh)\b')
SNOWFLAKE = re.compile(r'(?<![\d.])\d{17,20}(?![\d.])')
HANDLE = re.compile(r'(?<![\w.@/])@[A-Za-z0-9_][A-Za-z0-9_.-]{1,38}')
QUOTES = [
    re.compile(r'(?<![\w\'])\'([^\'\n]{3,60})\'(?![\w\'])'),
    re.compile(r'(?<![\w"])"([^"\n]{3,60})"(?![\w"])'),
    re.compile(r'“([^”\n]{3,60})”'),
    re.compile(r'‘([^’\n]{3,60})’'),
    re.compile(r'`([^`\n]{3,60})`'),
]
VERSION = re.compile(r'(?<![\w.])(?:v\d+\.\d+(?:\.\d+)*|\d+\.\d+\.\d+)(?:-[0-9A-Za-z.]+)?(?![\w]|\.\d)')
CAMEL = re.compile(r'\b(?:[a-z][a-z0-9]*(?:[A-Z][a-z0-9]+)+|[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*){2,}|[A-Z][a-z0-9]+[A-Z][a-z0-9]+)\b')
SNAKE = re.compile(r'\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b')
RANDOMISH = re.compile(r'[A-Za-z0-9+=]{20,}')   # unseparated runs: keys look like this, words and paths don't
CRED_QUERY = re.compile(r'(?i)[?&#;](?:[\w-]*(?:token|key|secret|sig|signature|password|auth|code|session)[\w-]*)=')


def secret_shaped(tok):
    if '[redacted' in tok or CRED_QUERY.search(tok):
        return True
    for rx in SECRET_REGEXES:
        if rx.search(tok):
            return True
    for m in RANDOMISH.finditer(tok):
        s = m.group(0)
        if any(c.isdigit() for c in s) and any(c.isupper() for c in s) and any(c.islower() for c in s):
            return True
        if len(s) >= 41 and re.fullmatch(r'[0-9a-f]+', s):
            return True   # bare hex longer than a commit id: a key
    return False


def extract(text):
    """-> set of (token, kind) from one visible record's text."""
    out = set()
    if not text:
        return out

    def add(tok, kind):
        tok = tok.strip()
        if 2 <= len(tok) <= MAX_TOKEN and not secret_shaped(tok):
            out.add((tok, kind))

    rest = text
    for m in URL.finditer(text):
        add(m.group(0).rstrip('.,;:!?\'"*_'), 'url')
    rest = URL.sub(' ', text)
    for rx in (PATH, WIN_PATH, HOME_PATH):
        for m in rx.finditer(rest):
            tok = m.group(0).rstrip('.,;:!?\'"*_)')
            if tok.count('/') + tok.count('\\') >= 2 or tok.startswith('~/'):
                add(tok, 'path')
    for m in HEX.finditer(rest):
        h = m.group(0)
        if any(c.isdigit() for c in h) and any(c.isalpha() for c in h):
            if rest[m.start() - 1:m.start()] == '-' or rest[m.end():m.end() + 1] == '-':
                continue   # a UUID segment
            window = rest[max(0, m.start() - 80):m.end() + 80]
            if GIT_WORDS.search(window):
                out.add((h, 'sha'))
    for m in PR.finditer(rest):
        num = m.group(1) or m.group(2)
        if m.group(2) and not PR_NEAR.search(rest[max(0, m.start() - 40):m.end() + 40]):
            continue
        out.add((f'#{num}', 'pr'))
    for m in SNOWFLAKE.finditer(rest):
        out.add((m.group(0), 'snowflake'))
    for m in HANDLE.finditer(rest):
        add(m.group(0).rstrip('.-'), 'handle')
    for rx in QUOTES:
        for m in rx.finditer(text):
            q = m.group(1).strip()
            if len(q) >= 3 and not q.isdigit():
                add(q, 'quote')
    for m in VERSION.finditer(rest):
        add(m.group(0), 'version')
    for rx in (CAMEL, SNAKE):
        for m in rx.finditer(rest):
            tok = m.group(0)
            if len(tok) > 6:
                add(tok, 'ident')
    return out
