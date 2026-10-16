# Design

Short notes on how the pieces fit, and why.

## Two halves, one seam

```
Claude Code ──► chronicle mod ──► mirror on disk ──┐
  (hooks)        (TypeScript)     <root>/<day>/    │  chronicle_mirror importer
                       │                           ▼
                       │ archive.command     ┌───────────────┐
                       └──────────────────►  │  resonant-    │ ◄── MCP (stdio): search, around, ...
                         `recall --json`     │  archive      │
                                             │  (Python)     │ ◄── other importers (Claude Code,
                                             └──────┬────────┘      Nexus markdown, notes)
                                                    ▼
                                            Postgres + pgvector
                                             embed server (loopback)
```

- **The mod doesn't need the archive.** It mirrors, compacts, squishes and shows the meter alone, and `recall` searches
  the mirror with ripgrep.
- **The archive doesn't need the mod.** It imports Claude Code transcripts, Markdown notes and Nexus exports by itself.
- **The only links are two documents:** the mirror format ([MIRROR-FORMAT.md](MIRROR-FORMAT.md)) and the recall contract
  ([RECALL-CONTRACT.md](RECALL-CONTRACT.md)). The mod holds no database address; the archive's own configuration does.

## Why Postgres

One store gives exact word search (`tsvector`), fuzzy substring search (`pg_trgm`), vectors (`pgvector`, HNSW), date
filters and joins. The cost is a server. The bundled compose file makes that one command and binds it to loopback.

## Search

`search` runs three signals and fuses them by rank (reciprocal rank fusion, k=60):

1. full text, with the language-neutral `simple` configuration,
2. vector similarity over `nomic-embed-text-v1.5` embeddings,
3. **anchors**: exact tokens (URLs, paths, SHAs, versions, identifiers, quoted phrases) pulled out of visible messages at
   import time. Meaning-based search is bad at "what was that commit hash"; anchors are good at it.

If the embedder is down, search drops to full text and anchors and says so in the answer. Rows of the live source
(default `claude-code`) newer than six hours are left out, because that conversation is already in context; the footer
says how many.

## Import pipeline

`read → redact → truncate → drop empties → drop exact duplicates in a conversation → number → upsert`

- Redaction runs on the whole string before any cut, so a key split by a truncation can't slip through.
- Rows are truncated at 20,000 characters of text (2,000 for tool rows).
- Ids are deterministic, so imports are incremental upserts. An unchanged input is skipped by content hash.
- An unchanged row keeps its embedding; a changed one goes back to `pending`.
- Commands print counts, never message text.

## Writing an importer

An importer reads one kind of input and yields records. It does not redact, truncate, deduplicate or write. (If it must shorten a field itself, it sets `Record.cut` and the pipeline cuts after redaction. It lists only files that pass `self.accept(path, root)`, which refuses symlinks, paths outside the root and oversize files, and gives each `Unit` its `root`; the pipeline checks again before reading.)

```python
from resonant_archive.importers.base import Importer, Record, Unit

class MyImporter(Importer):
    name = "my_format"
    description = "one line"
    default_source = "my-label"

    def detect(self, path): ...                    # True if this importer can read `path`
    def units(self, path, opts): ...               # yield Unit(key, files): the pieces imported (and skipped) whole
    def unit_records(self, unit, opts, stats): ... # yield Record for one unit
```

Message ids must be deterministic. Register it from your own package:

```toml
[project.entry-points."resonant_archive.importers"]
my_format = "my_package.importer:MyImporter"
```

A plugin that fails to load is reported by `resonant-archive importers` and never stops the built-ins. Speakers are
`user`, `agent`, `assistant`, `peer`, `system`, `tool` ([SCHEMA.md](SCHEMA.md)).

## The embedder

Local, fp32 ONNX, loopback-only, model pinned by hash. Why fp32: [EMBEDDER.md](EMBEDDER.md).

## Safety defaults

Read-only sessions with a statement timeout for search and MCP, values always bound as parameters, DSN never in the
repo, redaction before storage. See [PRIVACY.md](PRIVACY.md).
