# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 2.0.x   | Yes       |
| 0.1.x   | No        |

## Reporting a vulnerability

**Do not open a public issue for security vulnerabilities.**

Report it privately through GitHub: the **Security** tab of [nekyialabs/resonant-archive](https://github.com/nekyialabs/resonant-archive) ("Report a vulnerability"), with:

- Description of the vulnerability
- Steps to reproduce
- Impact assessment (what an attacker could do)

We'll acknowledge within 48 hours and aim to patch critical issues within 7 days.

## Security model

`resonant-archive` is **local-first** software that runs on your machine. The full account is in [docs/PRIVACY.md](docs/PRIVACY.md). In short:

- **No cloud backend.** Your conversations live in a Postgres you run, and in the chronicle mirror on your disk.
- **No telemetry.** The only network use is the one-time embedding model download from Hugging Face, checked against pinned SHA-256 sums.
- **Loopback only.** The bundled Postgres is published on `127.0.0.1`. The embed server refuses to bind any other address, any `Host` header that is not loopback, any POST that is not `application/json`, and a stalled client. The MCP server uses stdio.
- **Read-only search.** `init` creates a login that can only SELECT (`archive_reader`) and saves it as `readonly_dsn`; the MCP server and `recall` connect with it, open the session read-only with a 10 second statement timeout, and pass every value as a bound parameter. `doctor` warns when it is missing.
- **Secrets are redacted** before storage, by pattern, in every stored string and before any cut to a length limit; results of tool calls that read secret-bearing sources are withheld, and so is the content of such a call's input (only the tool name and the file or program are kept). The withholding is **best effort**: it knows the common shapes (a `.env`, key files, commands that print an environment, searches for passwords), not every way to reach a secret. This is a safety net, not a guarantee (see [docs/PRIVACY.md](docs/PRIVACY.md)).
- **Importers read only regular files inside the folder you name:** no symlinks, nothing resolving outside it, size caps per file and per line.
- **The embedding model is verified** (all four files, pinned SHA-256) before it is loaded.
- **The database address** is kept in a mode 0600 file or an environment variable, never inside the repository. `--dsn` on a command line still works, but prints a warning: the password sits in the process list (`ps`) while the command runs, so prefer the file or the variable. The password in the bundled Postgres' `.env` is random, and a placeholder is replaced.
- **CI** pins its actions by commit and the Postgres image by digest. The Claude Code release used by the mod's job is pinned by version number only (a version pin, not a digest), and was at least three days old when chosen. Every job has a time limit.

### What to watch for

- **Do not expose Postgres or the embed server to the internet.** Neither is designed for remote access.
- **Be careful what you import.** Redaction is pattern-based and can miss odd formats. Anything that gets through is searchable.
- **Keep the mirror and the archive private.** Do not commit `~/.chronicle/archive` or a database dump to a public repository.
- **If your owner role cannot create roles,** `init` cannot make the read-only login; set one up by hand ([docs/SCHEMA.md](docs/SCHEMA.md)) or `doctor` will keep warning.

### What `resonant-archive` does NOT protect you from

- **Leaking your source files.** If you import a file with secrets, they may be in the archive.
- **MCP clients with broad permissions.** If your AI can be prompt-injected, a hostile prompt could pull archive content out through tool output. That is a general MCP concern.
- **Filesystem compromise.** An attacker with shell access can read the mirror and the database without this tool.

## Reporting scope

We accept reports for issues in:

- The `resonant_archive` Python package (importers, redaction, embedder, search, MCP server, CLI) and the chronicle mod
- Documentation that misleads users about security behavior
- The default configurations (listen addresses, permissions, paths)

We don't control and can't fix:

- Bugs in Postgres, pgvector, ONNX Runtime, or other upstream dependencies (report those to their respective projects, but feel free to mention them to us so we can track)
- Issues in Claude Desktop, Claude Code, or other MCP clients
- Issues in `Nexus AI Chat Importer` (report upstream)
