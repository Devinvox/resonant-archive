# The `recall` contract

`recall` is how a program (the chronicle mod, a script) asks the archive a question and gets an answer it can use. It is
the same engine as the MCP tools `search`, `around`, `overview`, `associate` (and `drill`), behind one command line.

```
resonant-archive recall <action> [arguments] [--json]
```

The mod runs this as its `archive.command` (for example `["resonant-archive", "recall"]`), appends the arguments, and
reads stdout. Nothing is passed on stdin. The database address comes from the archive's own configuration, never from
the caller.

## Actions

| Action | Arguments | Does |
|---|---|---|
| `search QUERY` | `--source S,..` `--from YYYY-MM-DD` `--to YYYY-MM-DD` `--speaker S,..` `--context C,..` `--hidden` `--limit 1-40` (10) `--mode hybrid\|fts\|vector\|anchor` (hybrid) `--recent` | Full text + vectors + exact anchors, fused by rank (RRF, k=60) |
| `around ID` | `--before 0-30` (4) `--after 0-30` (4) `--hidden` | A message and its neighbours in the same conversation |
| `overview` | `--from` and `--to` (both required) `--source S,..` `--memories-only` `--no-memories` | Counts by source and speaker, busiest conversations, memories in range |
| `associate ID` | `--k 1-20` (8) `--source` `--from` `--to` `--speaker` `--context` `--hidden` `--recent` | Nearest neighbours by meaning in other conversations |
| `drill MEMORY` | `--hidden` | The archive messages a chronicle memory was written from (`#index` from `overview`, or an endpoint id) |

- `drill` exists only when the archive has chronicle memories. Without them it is not an action at all (asking for it is
  a usage error, exit 2), and `overview` says nothing about memories.
- Sources are whatever labels the archive holds (`chatgpt`, `claude-code`, `notes`, ...). They are read from the
  database; an unknown one is an error that lists the real ones. `--era` is accepted as an alias of `--source`.
- Speakers are `user`, `agent`, `assistant`, `peer`, `system`, `tool`.
- Dates are whole days in the configured timezone (`RESONANT_ARCHIVE_TZ`, or `timezone` in the search config; empty
  means the system zone). `--to` is inclusive.
- Hidden rows (tool calls and results, harness input) stay out unless `--hidden`. Hidden rows that are recall's own
  output echoed back stay out always.
- Rows of the live source (default `claude-code`) newer than 6 hours stay out of `search` and `associate` unless
  `--recent`, because that conversation is already in context. The footer says how many were left out.
- `search` needs `QUERY`; a query starting with `-` goes after `--`.

## Plain text (default)

Compact text for a model reader, capped near 6,000 characters. When it caps, it says how many hits or lines were left
out. Exit code `0` on success. On failure the text is `recall failed: <reason>` and the exit code is `2` (bad input, id not
found, capability missing) or `1` (unexpected failure, such as the database being unreachable).

## `--json`

Add `--json` anywhere before a `--` terminator. stdout is then exactly one JSON object, on success and on failure.

Success (exit `0`):

```json
{
  "ok": true,
  "action": "search",
  "text": "recall archive \"castle geese\" (hybrid): top 2 of 5 fused hits. ...",
  "data": { }
}
```

- `text` is the plain-text answer above. A caller that only wants to show something to a model can use `text` alone.
- `data` is the structured form. Its shape depends on `action`.

Failure (exit `2` or `1`):

```json
{ "ok": false, "error": "source: gpt not one of chat-a, code, notes", "kind": "usage" }
```

`kind` is `usage` (bad argument, id not found, missing capability), `db` (cannot reach or read the database),
`embedder` (the embedder was required and failed, which only happens with `--mode vector`; `associate` uses stored
vectors and never calls it) or `internal`.

### `data` for `search`

```json
{
  "query": "castle geese",
  "mode": "hybrid",
  "fused": 5,
  "signals": { "fts": 3, "vec": 50, "anchor": 0 },
  "vectors_skipped": false,
  "vectors_skipped_reason": null,
  "pending_embeddings": 0,
  "excluded_recent": 0,
  "notes": [],
  "elapsed_ms": 12,
  "filters": { "sources": null, "speakers": null, "contexts": null, "date_from": null, "date_to": null,
               "hidden": false, "include_recent": false },
  "hits": [
    { "rank": 1, "id": "...", "source": "notes", "conv_id": "...", "conv_title": "...",
      "ts": "2025-03-02T10:00:00+00:00", "seq": 0, "speaker": "user", "context": "default", "hidden": false,
      "tags": [], "text": "...full text...", "length": 49, "signals": { "fts": 1, "vec": 1 }, "snippet": "..." }
  ]
}
```

- `signals` counts candidates per signal. A skipped signal holds a string: `"vec": "skipped: embedder unavailable (...)"`.
- `hits[].signals` is the rank within each signal that found the hit.
- **Embedder down**: `vectors_skipped` is `true`, the reason is in `vectors_skipped_reason`, `text` carries a
  `[vectors skipped: ...]` line, and the hits come from full text and anchors. The exit code is still `0`. Only
  `--mode vector` fails (`kind: "embedder"`).
- `ts` is ISO 8601 with offset. Timestamps are stored in UTC and rendered in the configured zone in `text`.

### `data` for the other actions

- `around`: `target`, `conv_id`, `seq_from`, `seq_to`, `conv_seq_min`, `conv_seq_max`, `conv_rows`, `rows[]` (same row
  fields as a hit, in conversation order).
- `overview`: `date_from`, `date_to`, `timezone`, `sources`, `conversations`, `by_source_speaker[]`
  (`source, speaker, visible, hidden`), `contexts[]`, `busiest[]` (`source, conv_id, title, first, last, n`),
  `memories` (`null` when the archive has no memories or `--no-memories`; otherwise a list with `index, id, source,
  conv_id, t_from, t_to, level, text`).
- `associate`: `origin`, `candidates`, `skipped_conv`, `skipped_source`, `exact_scan`, `excluded_recent`, `hits[]` (hit
  fields plus `cosine`).
- `drill`: `memory`, `notes`, `rows[]`.

Fields may be added in later versions; callers should ignore keys they do not know. Removing or renaming a field is a
breaking change and bumps the contract.

## Safety

The command opens the database read-only with a 10 second statement timeout. Every user value, query text, source,
speaker and date included, is passed as a bound parameter; none is spliced into SQL.

## Library entry points (for the CLI and the MCP server)

```python
from resonant_archive.search import Searcher, SearchConfig, run_recall, render, to_dict, open_readonly

conn = open_readonly(dsn)
code, stdout = run_recall(argv, conn, embedder, SearchConfig.from_env())     # the whole contract above
s = Searcher(conn, embedder, cfg)                                            # or call the actions directly
res = s.search('castle geese', sources=['notes'], limit=10)
text = render(res, cfg)                                                      # model-ready text
data = to_dict(res, cfg)                                                     # JSON-safe dict
s.available_actions()                                                        # 'drill' only when memories exist
```

`embedder` is anything with `dim` and `embed_query(text) -> sequence of floats`
(`resonant_archive.search.embedder.QueryEmbedder`); `FakeEmbedder` is there for tests. SQL lives in one module,
`resonant_archive/search/sql.py`.
