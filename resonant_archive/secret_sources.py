"""Secret-bearing sources: tool calls whose result must never be stored.

A tool that reads a ``.env``, a private key or the process environment returns secrets in whatever shape they have,
which no pattern can be sure to catch. So the call is recognised from its *input* and its result is withheld whole.
For such a call the *input* is also cut down to the tool name and the file it names (or the program it runs): a
``Write`` to a ``.env``, an ``echo KEY=value >> .env`` and an ``Edit`` of one carry the secret in the input itself.

This is **best effort**, not a guarantee. It recognises the common shapes (files by name, commands that print an
environment or a credential, searches whose pattern or path names a secret). A secret read through a path it does not
know, a renamed file, a script, or a command assembled at run time gets through to the pattern redaction only. Keep the
archive private, and tell the importer what is secret with ``--known-secrets``.

This is the Python twin of ``secretBearing`` in the chronicle mod (claude-code/chronicle/hooks/register.ts): change
both together, and keep the case lists in both test suites the same.
"""

from __future__ import annotations

import re

WITHHELD = "[withheld: secret-bearing source]"
INPUT_WITHHELD = "[input withheld: secret-bearing source]"

# A path or file name that holds secrets. Each alternative is anchored on its own, and every run of characters that can
# repeat is bounded or starts at a fixed place, so a hostile 500 KB path cannot make the match quadratic.
SECRET_FILE_SOURCE = "|".join((
    r"[/.](?:env(?:\.[\w-]{1,40}){0,4}|dev\.vars|mcp\.json|pem|key|netrc|pgpass|environ|p12|pfx|jks|keystore|ppk|kdbx|htpasswd)$",
    r"(?:^|[/\\])\.env[\w.~*?-]*$",                                    # .env  .env.local  .envrc  .env*  .env.bak
    r"(?:^|[/\\])\.(?:npmrc|pypirc|git-credentials|vault-token|bash_history|zsh_history)$",
    r"\.docker/config\.json$",
    r"\.kube/config$",
    r"(?:^|[/.\\])(?:credentials(?:\.json)?|id_[a-z0-9]+)$",           # id_rsa id_ed25519 id_ecdsa id_dsa ...
    r"(?:^|[/\\])secrets?\.[\w.*-]+$",                                 # secrets.yml  secret.json
    r"terraform\.tfstate[\w.-]{0,40}$",
    r"\.tfvars(?:\.json)?$",
    r"/proc/[^/\s]+/(?:environ|cmdline)$",
    r"/etc/g?shadow$",
    r"(?:^|[/\\])[\w.-]{0,100}(?:service[-_]?account|application_default_credentials)[\w.-]{0,100}\.json$",
    r"(?:^|[/\\])client_secrets?\.json$",
    r"/private/",
))
SECRET_FILE = re.compile(SECRET_FILE_SOURCE, re.I)

# A word that says "I am looking for a secret": a search with it in the pattern or path has its result withheld.
SECRET_HINT_SOURCE = (r"pass(?:word|wd|phrase|code)|secret|token|api[_ -]?key|credential|private[_ -]?key|bearer|\.env\b|"
                      r"\benv\b|id_rsa|id_ed25519|\.pem\b|\.pfx|\.p12|\.npmrc|\.netrc|\.pgpass|BEGIN[ \w]{0,40}PRIVATE")
SECRET_HINT = re.compile(SECRET_HINT_SOURCE, re.I)

_SEARCH_TOOLS = ("grep", "glob")
_SEARCH_COMMAND = re.compile(r"^(?:\S*/)?(?:grep|egrep|fgrep|zgrep|rg|ag|ack|find|locate|fd)(?:\s|$)")

# A command segment (after wrappers are peeled off) that prints an environment, argv or a credential.
# `set -e` prints nothing (only a bare `set` lists variables), so flags pass there.
SECRET_PROGRAM_SOURCE = "|".join((
    r"env(?:\s|$)",                                     # `/usr/bin/env python3 run.py` is a launcher, not a listing
    r"\S*/env\s*$",
    r"\S*/env\s+-\S+",
    r"(?:\S*/)?(?:printenv|ps)(?:\s|$)",
    r"(?:\S*/)?pm2\s+(?:env|show|describe|prettylist|jlist)\b",
    r"set\s*$",
    r"(?:export|declare|typeset)(?:\s+-[A-Za-z]+)?\s*$",
    r"compgen\s+-[ve]\b",
    r"history(?:\s|$)",
    r"pgrep\s+-[A-Za-z]*[af]",
    r"(?:docker|podman)(?:\s+compose|-compose)?\s+(?:config|inspect)\b",
    r"(?:docker|podman)\s+(?:exec|run)\b[^\n]*\s(?:env|printenv)(?:\s|$)",
    r"kubectl\s+(?:(?:get|describe)\s+secrets?\b|config\s+view\b)",
    r"systemctl\s+(?:--?\S+\s+)*(?:show|cat)\b",
    r"ssh-add\s+-[A-Za-z]*[Ll]",
    r"gh\s+auth\s+(?:token\b|status\b[^\n]*--show-token)",
    r"gcloud\s+auth\s+(?:print-access-token|print-identity-token|application-default\s+print-access-token)",
    r"aws\s+(?:configure\s+(?:get|list|export-credentials)|secretsmanager\s+get-secret-value|"
    r"ssm\s+get-parameters?(?:-by-path)?|sts\s+get-session-token)",
    r"vault\s+(?:read|kv\s+get|token\s+lookup)",
    r"op\s+(?:read|item\s+get|inject)",
))
SECRET_PROGRAM = re.compile("^(?:" + SECRET_PROGRAM_SOURCE + ")")

# Prefixes that run the next command: peeled off one at a time (sudo is tried both with and without an option argument).
_PEELERS = tuple(re.compile(p) for p in (
    r"^[A-Za-z_]\w*=(?:'[^']*'|\"[^\"]*\"|\S*)\s+",                       # VAR=x env
    r"^(?:exec|time|command|builtin|nohup|setsid)\s+(?:-\S+\s+)*",
    r"^(?:sudo|doas)\s+(?:-\S+\s+)*",                                       # sudo env / sudo -E env
    r"^(?:sudo|doas)\s+(?:-\S+\s+)*\S+\s+",                                 # sudo -u user env
    r"^xargs\s+(?:-\S+\s+)*",
    r"^(?:nice|ionice|stdbuf|timeout)\s+(?:-\S+\s+(?:\d+\s+)?)*(?:\d+[smhd]?\s+)?",
))
_SHELL_C = re.compile(r"^(?:\S*/)?(?:ba|z|da|k|fi|a|c)?sh\s+(?:-\S+\s+)*?-[A-Za-z]*c\s+(.+)$")

_SEGMENTS = re.compile(r"[;&|\n]+|\$\(|`")
_TOKENS = re.compile(r"[\s'\"`;&|()<>=]+")
_ENV_BIN = re.compile(r"/bin/env$")
_ENV_READ = re.compile(r"\b(?:os\.environ|os\.getenv|process\.env|ENV\[)")
_PATH_KEYS = ("file_path", "path", "notebook_path")
_SEARCH_KEYS = ("pattern", "path", "glob", "include", "query")
_ASSIGN_VALUE = re.compile(r"^[A-Za-z_]\w*=")


def _trim(seg: str) -> str:
    # strip(), not a regex ending in `$`: a run of 400,000 quotes followed by one more character made that regex quadratic
    return seg.strip().lstrip("({ \t\n\r\f\v").rstrip(")}\"' \t\n\r\f\v")


def _segment_secret(seg: str, depth: int = 0) -> bool:
    seg = _trim(seg)
    if not seg or depth > 5:
        return False
    if SECRET_PROGRAM.search(seg):
        return True
    for rx in _PEELERS:
        m = rx.match(seg)
        if m and _segment_secret(seg[m.end():], depth + 1):
            return True
    m = _SHELL_C.match(seg)
    if m:
        inner = re.sub(r"^[\"'\s]+|[\"'\s]+$", "", m.group(1))
        return any(_segment_secret(s, depth + 1) for s in _SEGMENTS.split(inner))
    return False


def secret_bearing(name: object, tool_input: object) -> bool:
    """True when a tool call (its ``name`` and ``input``) reads a secret-bearing file, prints the environment or a
    credential, or searches for a secret by name. Best effort (see the module docstring)."""
    o = tool_input if isinstance(tool_input, dict) else {}
    for k in _PATH_KEYS:
        v = o.get(k)
        if isinstance(v, str) and SECRET_FILE.search(v.strip()):
            return True
    if isinstance(name, str) and name.lower() in _SEARCH_TOOLS:
        for k in _SEARCH_KEYS:
            v = o.get(k)
            if isinstance(v, str) and (SECRET_HINT.search(v) or SECRET_FILE.search(v.strip())):
                return True
    cmd = o.get("command") if isinstance(o.get("command"), str) else ""
    if cmd:
        if _ENV_READ.search(cmd):
            return True
        for seg in _SEGMENTS.split(cmd):
            if _segment_secret(seg):
                return True
            t = _trim(seg)
            if _SEARCH_COMMAND.search(t) and SECRET_HINT.search(t):
                return True
        for tok in _TOKENS.split(cmd):
            if tok and not _ENV_BIN.search(tok) and SECRET_FILE.search(tok):
                return True
    return False


def secret_call_summary(name: object, tool_input: object) -> str:
    """What may be kept of a secret-bearing call's *input*: the file it names, or for a command only the program (and
    the names of any secret files it mentions), never arguments, ``content``, ``new_string``, ``old_string`` or a
    heredoc body. The tool name is added by the caller."""
    o = tool_input if isinstance(tool_input, dict) else {}
    for k in _PATH_KEYS:
        v = o.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:300]
    cmd = o.get("command") if isinstance(o.get("command"), str) else ""
    if cmd:
        first = _SEGMENTS.split(cmd.lstrip())[0]
        first = re.split(r"[<>]", first)[0].strip()
        words = [w for w in first.split() if not _ASSIGN_VALUE.match(w)]
        prog = (words[0] if words else "")[:80]
        files = [t for t in _TOKENS.split(cmd) if t and not _ENV_BIN.search(t) and SECRET_FILE.search(t)]
        out = prog + (" [arguments withheld]" if len(words) > 1 or len(cmd.strip()) > len(prog) else "")
        if files:
            out += " " + " ".join(dict.fromkeys(f[:200] for f in files[:5]))
        return out.strip() or INPUT_WITHHELD
    return INPUT_WITHHELD
