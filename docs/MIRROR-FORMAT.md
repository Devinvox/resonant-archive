# Mirror format (version 1)

The contract between the chronicle mod, which writes the mirror, and the `chronicle_mirror` importer, which reads it.
The mod has no Python dependency and the package has no dependency on the mod: this page is the only link.

## Layout

```
<root>/<day>/<session id>.jsonl            rows and event lines, appended as they happen
<root>/<day>/<session id>.memories.jsonl   chronicle L1 memories
```

- `<root>` defaults to `~/.chronicle/archive` and is set by the mod's `archiveRoot`.
- `<day>` is `YYYY-MM-DD` in the mod's configured timezone (or `undated` when a row has no time). A session that crosses
  midnight has files in more than one day folder.
- File names use the session id with every character outside `A-Za-z0-9._-` replaced by `_`. The importer takes the
  session id from each line's `sid`, and falls back to the file name.
- Folders are created 0700 and files 0600. Files are append-only JSON Lines, UTF-8, one object per line. A line cut
  short by a crash is not valid JSON; readers skip it.
- Every string was redacted before it was written. A tool result read from a secret-bearing source is replaced by
  `[withheld: secret-bearing source]`. A recall tool's own output is replaced by a short note so a search does not find
  its own previous results.

## Row lines (no `event` field)

| field | type | meaning |
|---|---|---|
| `t` | number | Time, milliseconds since the Unix epoch. |
| `sid` | string | Session id. |
| `uuid` | string | Row id, unique in the session. A row written twice (a retried flush) repeats its uuid. |
| `door` | string | How the row entered: `prompt`, `command`, `tool-result`, `tool-message`, `note`, ... |
| `origin` | string, optional | `composer`, `bridge`, `peer`, `plugin`, `task-notification`, ... |
| `role` | string | `user` or `assistant`. |
| `queued` | string, optional | `prompt` for a message typed while the assistant was working; `other` for any other queued input. |
| `chronicle` | string, optional | `memories`, `ack`, `fork`: the mod's own injected rows. |
| `meta` | boolean, optional | Harness-generated row. |
| `text` | string, optional | The words of the row. |
| `tools` | string[], optional | One `name(json input)` string per tool call, input capped. |
| `results` | string[], optional | One string per tool result, each capped. |

## Event lines (an `event` field)

| `event` | fields | importer |
|---|---|---|
| `compaction` | `t`, `trigger`, `session`, `summary` (the summary text), counts | A hidden `system` message tagged `summary`, `compaction`, when `summary` is present. |
| `memory` | `t`, `sid`, `level`, `ordinal`, `text`, `from_uuid`, `to_uuid`, `t_from`, `t_to`, `est_tokens`, `rows`, `source_hash`, `mac` | A row in `memories` (the `mac` signature is ignored: it protects the mod, not the archive). |
| `session_fresh`, `memory_formed`, `memory_skipped`, `mirror_dropped`, others | various | Bookkeeping; counted, not imported. Unknown events are ignored, so the format can grow. |

## How rows become messages

| mirror row | messages row |
|---|---|
| `role: assistant`, `text` | speaker `agent`, visible |
| `role: user`, `door: prompt`, origin composer/bridge/none | speaker `user`, visible |
| the same with `queued: prompt` | speaker `user`, tag `mid-turn` |
| `origin: peer` | speaker `peer`, context `peer` |
| `meta`, `queued: other`, other doors, origin plugin or task notification | speaker `system`, hidden |
| `chronicle: memories/ack/fork` | speaker `system`, hidden, tags `chronicle` + kind |
| each `tools[k]` | speaker `tool`, hidden, tag `tool-call`, id `<row id>:tool:<k>` |
| each `results[k]` | speaker `tool`, hidden, tag `tool-result`, text `[result] ...`, id `<row id>:result:<k>` |

Message id: `chronicle:<sid>:<uuid>`. Default source label: `chronicle` (`--source` changes it). Platform `cli`.

One session (all its day files) is one import unit: if any of its files changed since the last import, the session is
re-read, and rows already stored are updated in place rather than duplicated.

## Compatibility

Version 1 is what the mod writes today. Readers must ignore unknown fields and unknown events. A change that breaks a
reader gets a new version number here and in the mod.
