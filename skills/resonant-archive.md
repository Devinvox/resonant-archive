---
name: resonant-archive
description: Search and manage the user's local archive of past AI conversations and notes with resonant-archive. Use when the user asks what was said before, wants to find an exact path, id, decision or quote from an earlier session, or asks to import, index or check their archive. Also covers the chronicle mod's /squish and recall.
---

# resonant-archive skill

`resonant-archive` keeps the user's past conversations (Claude Code sessions, the chronicle mirror, ChatGPT and claude.ai
exports, folders of notes) in a local Postgres and searches them by words, exact strings and meaning. Everything is
local. There is no daemon to start for search.

## Check it is installed

```bash
resonant-archive --version
resonant-archive doctor
```

`doctor` checks the database, the extensions, the embedding model and the embed server, and prints the fix for what it
finds. If the command is missing, the user installs it from a clone of the repository (`pip install -e .`).

## Searching (what you do most)

If the MCP server is connected you have these tools. If not, `resonant-archive recall ...` does the same from a shell.

| Tool | Use it to |
|---|---|
| `search` | Find things. Works on exact words, paths, ids, URLs and quoted phrases as well as meaning. Filters: `source`, `speaker`, `context`, `date_from`, `date_to`, `mode`, `limit`. |
| `around` | Read the messages either side of a hit. Pass an `id` from a search result. |
| `associate` | Find talk in other conversations close in meaning to one message. |
| `overview` | See what happened between two dates (both required, `YYYY-MM-DD`). |
| `stats` | See what the archive holds and how many rows still await embedding. |
| `drill` | Only present when chronicle memories exist: the messages a memory was written from. |

Habits that work:

- Search first, with the specific thing: a filename, an error string, an id. Then widen to meaning.
- Take the id from a good hit to `around` to read the context before you answer from it.
- Quote what you found and say where it came from (source, date). Don't fill gaps from imagination; say when nothing matched.
- Hidden rows (tool calls and results) are left out by default. Pass `hidden` only if you need them.
- The current Claude Code session's last six hours are left out of search, because it is already in context. The answer's footer says so.
- If the footer says vectors were skipped or rows are pending, say so and offer `resonant-archive embed`.

From a shell: `resonant-archive recall search "the retry bug" --source claude-code --from 2026-09-01 --limit 10`.

## Setting up (when the user asks you to)

```bash
resonant-archive init --docker --up      # bundled Postgres on 127.0.0.1; or: init --dsn postgresql://...
resonant-archive models fetch            # embedding model, about 550 MB, once
resonant-archive import PATH             # importer detected; --importer NAME, --source LABEL to force
resonant-archive embed                   # adds meaning-based search; resumable
resonant-archive importers               # list importers
resonant-archive stats
```

Importers: `claude_code` (`~/.claude/projects`), `chronicle_mirror` (the mod's mirror, default
`~/.chronicle/archive`), `nexus_markdown` (ChatGPT and claude.ai exports converted by the Nexus AI Chat Importer
Obsidian plugin), `markdown_folder` (plain notes). Raw `conversations.json` exports are not read directly yet. Imports
are incremental, so running one again is safe and cheap.

Before importing someone's files, tell them the text will be stored in their local Postgres. Credentials are redacted
on the way in, but only by pattern: if they know of secrets, offer `--known-secrets FILE`.

## The chronicle mod (compaction)

If the user has the chronicle mod installed (`claude-code/chronicle/`):

- `/squish [focus]` compacts the conversation when they choose, keeping the recent stretch word for word. `/squish status`
  shows how full the window is.
- A line like `[context · 142k of 1M (14%)]` appears with their prompts. At 75% it adds "squish soon", at 90% "SQUISH NOW".
  If you see it, mention it once, plainly, at a natural pause. Don't nag.
- After a compaction, `recall` brings back the original wording of anything the summary dropped.

## Errors

- *"missing extensions"*: the user runs the `CREATE EXTENSION` line it prints, as a Postgres superuser.
- *"no embedder"*: `resonant-archive models fetch`.
- *"cannot reach the archive database"*: the container may be stopped. `docker compose up -d --wait` in the repo.
- Search finds words but not meaning: rows are pending. `resonant-archive embed`.

More: README.md and docs/ in the repository.
