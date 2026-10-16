"""The import pipeline: the same steps for every importer.

    unit changed? -> read records -> redact -> truncate -> drop empties -> drop duplicates -> number (seq)
    -> upsert messages / memories -> refresh anchors and conversations -> remember the unit's hash

Idempotent: a unit whose files, importer version and options are byte-for-byte what they were at the last import is
skipped; a changed unit is upserted by deterministic id (rows are never deleted by a plain import, so what a source
later cleans up stays in the archive). A row is only ever updated by the importer and under the source label that wrote
it: an id that already belongs to another source or importer is skipped and counted, never overwritten.

``rebuild=True`` re-reads every unit and then removes, from the units it actually re-read, the rows the re-read no
longer produces. It never touches a unit it did not read (a symlink or oversize file that was skipped, a file since
purged, rows imported before migration 0003), and it removes nothing at all when any file was skipped, any unit failed
or a line was lost. It says how many rows would go and removes them only with ``yes=True`` (or a ``confirm`` callback
that agrees); ``dry_run=True`` only reports. Embeddings survive any re-import whose text is unchanged.

Reports hold counts only. Message text is never printed or put in an error.
"""

from __future__ import annotations

import collections
import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import psycopg

from . import anchors as anchors_mod
from . import db, importers
from .importers import safety
from .importers.base import MEDIA_TAGS, Memory, Options, Record, Unit
from .redact import REDACTION_VERSION, Redactor, load_known

TEXT_LIMIT = 20_000
TOOL_LIMIT = 2_000
#: Importer stats that mean "a line of the input was not turned into a row": a rebuild must not read that as "gone".
LOSSY_STATS = ("long_lines", "bad_lines", "no_timestamp")

MSG_COLS = ("importer", "id", "source", "conv_id", "conv_title", "ts", "speaker", "context", "text", "platform", "model",
            "hidden", "tags", "src", "seq", "embed_status", "unit_key")


@dataclass
class ImportReport:
    importer: str = ""
    labels: set[str] = field(default_factory=set)
    units_total: int = 0
    units_skipped: int = 0
    units_imported: int = 0
    units_failed: int = 0
    parsed: int = 0
    kept: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    dropped_empty: int = 0
    dropped_duplicate: int = 0
    truncated: int = 0
    memories: int = 0
    anchors: int = 0
    removed: int = 0
    started: object = None   # DB clock at the start of the run: a rebuild never removes a row imported after it
    collisions: int = 0   # rows whose id already belongs to another source or importer: skipped, never overwritten
    redactions: collections.Counter = field(default_factory=collections.Counter)
    importer_stats: collections.Counter = field(default_factory=collections.Counter)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    rebuild: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        lines = [
            f"{self.importer}: {self.units_total} units ({self.units_imported} imported, {self.units_skipped} unchanged, "
            f"{self.units_failed} failed); labels: {', '.join(sorted(self.labels)) or '-'}",
            f"messages: {self.parsed} read, {self.kept} kept ({self.inserted} new, {self.updated} updated, "
            f"{self.unchanged} unchanged); dropped {self.dropped_empty} empty, {self.dropped_duplicate} duplicate; "
            f"{self.truncated} truncated; {self.memories} memories; {self.anchors} anchors"
            + (f"; {self.removed} removed (rebuild)" if self.removed else "")
            + (f"; {self.collisions} skipped (id already belongs to another source or importer)" if self.collisions else ""),
        ]
        if self.redactions:
            lines.append("redactions: " + ", ".join(f"{k} {v}" for k, v in sorted(self.redactions.items())))
        if self.importer_stats:
            lines.append("importer: " + ", ".join(f"{k} {v}" for k, v in sorted(self.importer_stats.items())))
        lines += [f"note: {n}" for n in self.notes]
        lines += [f"ERROR: {e}" for e in self.errors]
        return "\n".join(lines)


# ---------------------------------------------------------------- hashing

def unit_hash(importer: importers.Importer, unit: Unit, opts: Options) -> str:
    h = hashlib.sha256()
    o = dataclasses.asdict(opts)
    o.pop("projects", None)  # selects units, does not change what a unit contains
    h.update(json.dumps([importer.name, importer.version, REDACTION_VERSION, o], sort_keys=True, default=str).encode())
    for f in sorted(unit.files, key=lambda p: (p.parent.name, p.name)):
        h.update(b"\0" + f.parent.name.encode() + b"/" + f.name.encode() + b"\0")
        with open(f, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- cleaning

def _scrub_id(redactor: Redactor, value: str | None) -> str | None:
    """An identifier with no secret in it. Ids are matched by value across imports, so one that changes under
    redaction is replaced by a stable hash of the original (never of anything else), not by a marker that two
    different ids would share. The long-token rule is off here: ids are long random tokens by nature."""
    if not value:
        return value
    out = redactor(value, ident=True)
    if out == value:
        return value
    return "redacted-" + hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:24]


def _scrub_label(redactor: Redactor, value: str | None) -> str | None:
    return redactor(value.replace("\x00", "")) if value else value


def clean(records: list[Record | Memory], redactor: Redactor, report: ImportReport) -> tuple[list[Record], list[Memory]]:
    """Redact, truncate, drop empty rows and duplicates, number each conversation. Pure apart from the counters.

    Every string that is stored is redacted, not only the text: ids, titles, labels, file names, model names, tags.
    Redaction comes first and the cut after it, so a secret a cut would split is gone before the cut."""
    msgs: list[Record] = []
    mems: list[Memory] = []
    seen_ids: set[str] = set()
    seen_keys: set[tuple] = set()
    for r in records:
        if isinstance(r, Memory):
            r.id = _scrub_id(redactor, r.id) or r.id
            if r.id in seen_ids:
                report.dropped_duplicate += 1
                continue
            seen_ids.add(r.id)
            r.source = _scrub_label(redactor, r.source) or r.source
            r.sid = _scrub_id(redactor, r.sid) or r.sid
            r.from_uuid = _scrub_id(redactor, r.from_uuid)
            r.to_uuid = _scrub_id(redactor, r.to_uuid)
            r.source_hash = _scrub_id(redactor, r.source_hash)
            r.text = _cut(redactor(r.text), TEXT_LIMIT, report)
            mems.append(r)
            continue
        report.parsed += 1
        r.id = _scrub_id(redactor, r.id) or r.id
        if r.id in seen_ids:
            report.dropped_duplicate += 1
            continue
        seen_ids.add(r.id)
        r.conv_id = _scrub_id(redactor, r.conv_id) or r.conv_id
        r.source = _scrub_label(redactor, r.source) or r.source
        r.text = redactor((r.text or "").replace("\x00", ""))
        if r.cut is not None:
            r.text = r.text[: r.cut]
        r.conv_title = _scrub_label(redactor, r.conv_title)
        r.src = _scrub_label(redactor, r.src) or ""
        r.platform = _scrub_label(redactor, r.platform)
        r.model = _scrub_label(redactor, r.model)
        r.context = _scrub_label(redactor, r.context) or "default"
        r.tags = sorted({_scrub_label(redactor, t) or "" for t in r.tags} - {""})
        r.text = _cut(r.text, TOOL_LIMIT if r.speaker == "tool" else TEXT_LIMIT, report)
        if not r.text.strip() and not (set(r.tags) & MEDIA_TAGS):
            report.dropped_empty += 1
            continue
        key = (r.source, r.conv_id, r.speaker, r.ts, r.text)
        if key in seen_keys:
            report.dropped_duplicate += 1
            continue
        seen_keys.add(key)
        msgs.append(r)
    msgs.sort(key=lambda r: (r.source, r.conv_id, r.ts, r.order, r.id))
    return msgs, mems


def _cut(s: str, limit: int, report: ImportReport) -> str:
    if len(s) <= limit:
        return s
    report.truncated += 1
    return s[:limit] + f"…[truncated {len(s) - limit} chars]"


# ---------------------------------------------------------------- storing

_UPSERT = """
WITH up AS (
  INSERT INTO messages (importer, id, source, conv_id, conv_title, ts, speaker, context, text, platform, model, hidden,
                        tags, src, seq, embed_status, unit_key)
  SELECT importer, id, source, conv_id, conv_title, ts, speaker, context, text, platform, model, hidden, tags, src, seq,
         embed_status, unit_key
    FROM stage_messages
  ON CONFLICT (id) DO UPDATE SET
    importer = EXCLUDED.importer, source = EXCLUDED.source, conv_id = EXCLUDED.conv_id, conv_title = EXCLUDED.conv_title, ts = EXCLUDED.ts,
    speaker = EXCLUDED.speaker, context = EXCLUDED.context, text = EXCLUDED.text, platform = EXCLUDED.platform,
    model = EXCLUDED.model, hidden = EXCLUDED.hidden, tags = EXCLUDED.tags, src = EXCLUDED.src, seq = EXCLUDED.seq,
    unit_key = EXCLUDED.unit_key,
    embedding     = CASE WHEN messages.text = EXCLUDED.text THEN messages.embedding END,
    embedded_hash = CASE WHEN messages.text = EXCLUDED.text THEN messages.embedded_hash END,
    embedded_at   = CASE WHEN messages.text = EXCLUDED.text THEN messages.embedded_at END,
    embed_status  = CASE WHEN messages.text = EXCLUDED.text AND messages.embed_status = 'done' THEN 'done'
                         ELSE EXCLUDED.embed_status END,
    imported_at = now()
  WHERE messages.source = EXCLUDED.source AND (messages.importer IS NULL OR messages.importer = EXCLUDED.importer)
    AND (messages.importer, messages.source, messages.conv_id, messages.conv_title, messages.ts, messages.speaker, messages.context,
         messages.text, messages.platform, messages.model, messages.hidden, messages.tags, messages.src, messages.seq,
         messages.unit_key)
        IS DISTINCT FROM
        (EXCLUDED.importer, EXCLUDED.source, EXCLUDED.conv_id, EXCLUDED.conv_title, EXCLUDED.ts, EXCLUDED.speaker, EXCLUDED.context,
         EXCLUDED.text, EXCLUDED.platform, EXCLUDED.model, EXCLUDED.hidden, EXCLUDED.tags, EXCLUDED.src, EXCLUDED.seq,
         EXCLUDED.unit_key)
  RETURNING (xmax = 0) AS inserted
)
SELECT count(*) FILTER (WHERE inserted), count(*) FILTER (WHERE NOT inserted) FROM up
"""

_CONVERSATIONS = """
INSERT INTO conversations (source, conv_id, title, platform, first_ts, last_ts, n_messages, n_visible, updated_at)
SELECT source, conv_id,
       (array_agg(conv_title ORDER BY seq) FILTER (WHERE conv_title IS NOT NULL))[1],
       (array_agg(platform ORDER BY seq) FILTER (WHERE platform IS NOT NULL))[1],
       min(ts), max(ts), count(*), count(*) FILTER (WHERE NOT hidden), now()
  FROM messages
 WHERE source = ANY(%(sources)s) AND conv_id = ANY(%(convs)s)
 GROUP BY source, conv_id
ON CONFLICT (source, conv_id) DO UPDATE SET
  title = EXCLUDED.title, platform = EXCLUDED.platform, first_ts = EXCLUDED.first_ts, last_ts = EXCLUDED.last_ts,
  n_messages = EXCLUDED.n_messages, n_visible = EXCLUDED.n_visible, updated_at = now()
"""


_CLASH_MESSAGES = """
SELECT s.id FROM stage_messages s JOIN messages m ON m.id = s.id
 WHERE m.source <> s.source OR (m.importer IS NOT NULL AND m.importer <> s.importer)
"""

_UPSERT_MEMORY = (
    "INSERT INTO memories (id, source, sid, level, ordinal, ts, t_from, t_to, from_uuid, to_uuid, text, "
    "est_tokens, rows, source_hash, importer, unit_key) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON CONFLICT (id) DO UPDATE SET importer = EXCLUDED.importer, text = EXCLUDED.text, level = EXCLUDED.level, "
    "ts = EXCLUDED.ts, t_from = EXCLUDED.t_from, t_to = EXCLUDED.t_to, est_tokens = EXCLUDED.est_tokens, "
    "rows = EXCLUDED.rows, unit_key = EXCLUDED.unit_key, imported_at = now() "
    "WHERE memories.source = EXCLUDED.source AND (memories.importer IS NULL OR memories.importer = EXCLUDED.importer)")


def store_unit(conn: psycopg.Connection, msgs: list[Record], mems: list[Memory], report: ImportReport,
               importer: str | None = None, unit_key: str | None = None) -> None:
    """Write one cleaned unit in the caller's transaction. ``importer`` and ``unit_key`` are recorded on every row (see
    migrations 0002 and 0003). A row is updated only when the stored row has the same source and importer; an id that
    already belongs to another source or importer is skipped and counted in ``report.collisions``."""
    with conn.cursor() as cur:
        if msgs:
            seq: dict[tuple[str, str], int] = collections.defaultdict(int)
            cur.execute(
                "CREATE TEMP TABLE stage_messages (importer text, id text, source text, conv_id text, conv_title text, ts timestamptz, "
                "speaker text, context text, text text, platform text, model text, hidden boolean, tags text[], "
                "src text, seq integer, embed_status text, unit_key text) ON COMMIT DROP")
            with cur.copy(f"COPY stage_messages ({', '.join(MSG_COLS)}) FROM STDIN") as cp:
                for r in msgs:
                    k = (r.source, r.conv_id)
                    n = seq[k]
                    seq[k] += 1
                    cp.write_row((importer, r.id, r.source, r.conv_id, r.conv_title, r.ts, r.speaker, r.context, r.text,
                                  r.platform, r.model, r.hidden, list(r.tags), r.src, n,
                                  "skipped" if r.hidden else "pending", unit_key))
            cur.execute(_CLASH_MESSAGES)
            clash = {row[0] for row in cur.fetchall()}
            if clash:
                cur.execute("DELETE FROM stage_messages WHERE id = ANY(%s)", (sorted(clash),))
                msgs = [r for r in msgs if r.id not in clash]
                report.collisions += len(clash)
                if report.rebuild:   # a skipped clash still counts as produced: the re-read did produce it
                    with cur.copy("COPY run_ids (id) FROM STDIN") as cp:
                        for i in sorted(clash):
                            cp.write_row((i,))
            cur.execute(_UPSERT)
            ins, upd = cur.fetchone()
            report.inserted += ins
            report.updated += upd
            report.unchanged += len(msgs) - ins - upd
        if msgs:
            ids = [r.id for r in msgs]
            cur.execute("DELETE FROM anchors WHERE msg_id = ANY(%s)", (ids,))
            rows = []
            for r in msgs:
                if not r.hidden:
                    rows.extend((tok, kind, r.id, r.ts) for tok, kind in anchors_mod.extract(r.text))
            if rows:
                with cur.copy("COPY anchors (token, kind, msg_id, ts) FROM STDIN") as cp:
                    for row in rows:
                        cp.write_row(row)
            report.anchors += len(rows)
            cur.execute(_CONVERSATIONS, {"sources": sorted({r.source for r in msgs}),
                                         "convs": sorted({r.conv_id for r in msgs})})
            if report.rebuild:
                with cur.copy("COPY run_ids (id) FROM STDIN") as cp:
                    for i in ids:
                        cp.write_row((i,))
        kept_mems = 0
        for m in mems:
            cur.execute(_UPSERT_MEMORY,
                        (m.id, m.source, m.sid, m.level, m.ordinal, m.ts, m.t_from, m.t_to, m.from_uuid, m.to_uuid, m.text,
                         m.est_tokens, m.rows, m.source_hash, importer, unit_key))
            cur.execute("SELECT source = %s AND (importer IS NULL OR importer = %s) FROM memories WHERE id = %s",
                        (m.source, importer, m.id))
            if not cur.fetchone()[0]:
                report.collisions += 1
                if report.rebuild:
                    cur.execute("INSERT INTO run_memory_ids (id) VALUES (%s)", (m.id,))
                continue
            kept_mems += 1
            if report.rebuild:
                cur.execute("INSERT INTO run_memory_ids (id) VALUES (%s)", (m.id,))
        report.memories += kept_mems
        report.labels.update({r.source for r in msgs} | {m.source for m in mems})


# ---------------------------------------------------------------- the run

def run_import(
    importer: str | importers.Importer,
    path: str | Path,
    *,
    dsn: str | None = None,
    conn: psycopg.Connection | None = None,
    options: Options | None = None,
    known_secrets: list[str] | tuple[str, ...] = (),
    json_configs: list[str] | tuple[str, ...] = (),
    force: bool = False,
    rebuild: bool = False,
    yes: bool = False,
    dry_run: bool = False,
    confirm: Callable[[int, int], bool] | None = None,
    progress: Callable[[ImportReport, str], None] | None = None,
) -> ImportReport:
    """Import ``path`` with ``importer``. Returns a report; per-unit failures are in ``report.errors`` and do not stop
    the other units. ``known_secrets`` are files or folders whose values are redacted wherever they appear.

    ``rebuild`` also removes rows (see the module docstring); that needs ``yes=True`` or a ``confirm(messages, memories)``
    callback that returns True, and ``dry_run=True`` only reports what would go. It cannot be combined with a project
    filter: a filtered run has not read most of the input and must not judge what is missing from it."""
    imp = importers.get(importer) if isinstance(importer, str) else importer
    opts = options or Options()
    path = Path(path).expanduser()
    report = ImportReport(importer=imp.name)
    report.rebuild = rebuild
    if rebuild and opts.projects:
        report.errors.append("--rebuild cannot be combined with a project filter: the run would not have read the rest "
                             "of the input, and a rebuild only removes what a complete read no longer produces")
        return report
    if not path.exists():
        report.errors.append(f"path does not exist: {path}")
        return report
    redactor = Redactor(load_known(known_secrets, json_configs))
    own = conn is None
    conn = conn or db.connect(dsn)
    locked = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext('import:' || %s)), clock_timestamp()", (imp.name,))
            locked, started = cur.fetchone()
        conn.commit()
        if not locked:
            report.errors.append(f"another {imp.name} import is already running on this database; nothing was done "
                                 "(wait for it to finish, then run again)")
            return report
        report.started = started
        if rebuild:
            with conn.cursor() as cur:
                cur.execute("CREATE TEMP TABLE run_ids (id text)")
                cur.execute("CREATE TEMP TABLE run_memory_ids (id text)")
            conn.commit()
        seen_units: list[str] = []
        rebuilt_units: list[str] = []    # units re-read in this run whose rows may be judged against the re-read
        partial_units = 0                # re-read, but lines were lost on the way: their rows are left alone
        for unit in imp.units(path, opts):
            report.units_total += 1
            ukey = _scrub_id(redactor, unit.key) or unit.key      # what is stored (and looked up) for this unit
            shown = redactor(unit.label)                          # what is printed
            seen_units.append(ukey)
            try:
                bad = next((why for f in unit.files if (why := safety.problem(f, unit.root))), None)
                if bad:  # swapped since the folder was listed, or a plugin that did not check
                    report.units_skipped += 1
                    report.importer_stats["skipped " + bad] += 1
                    continue
                h = unit_hash(imp, unit, opts)
                with conn.cursor() as cur:
                    cur.execute("SELECT content_hash FROM sources WHERE importer = %s AND unit_key = %s",
                                (imp.name, ukey))
                    row = cur.fetchone()
                conn.commit()
                if row and row[0] == h and not (force or rebuild):
                    report.units_skipped += 1
                    continue
                ustats: collections.Counter = collections.Counter()
                records = list(imp.unit_records(unit, opts, ustats))
                msgs, mems = clean(records, redactor, report)
                store_unit(conn, msgs, mems, report, imp.name, ukey)
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO sources (importer, unit_key, label, content_hash, n_files, n_messages, n_memories, path) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (importer, unit_key) DO UPDATE SET "
                        "label = EXCLUDED.label, content_hash = EXCLUDED.content_hash, n_files = EXCLUDED.n_files, "
                        "n_messages = EXCLUDED.n_messages, n_memories = EXCLUDED.n_memories, path = EXCLUDED.path, "
                        "imported_at = now()",
                        (imp.name, ukey, _scrub_label(redactor, imp.label(opts)) if not msgs else msgs[0].source, h,
                         len(unit.files), len(msgs), len(mems),
                         _scrub_label(redactor, str(unit.files[0].parent)) if unit.files else None))
                conn.commit()
                report.importer_stats.update(ustats)
                if rebuild:
                    if any(ustats.get(k) for k in LOSSY_STATS):
                        partial_units += 1
                    else:
                        rebuilt_units.append(ukey)
                report.kept += len(msgs)
                report.units_imported += 1
            except Exception as exc:
                conn.rollback()
                report.units_failed += 1
                report.errors.append(f"unit {shown}: {type(exc).__name__}: {redactor(str(exc)[:160])}")
            if progress:
                progress(report, shown)
        report.redactions.update(redactor.counts)
        report.importer_stats.update({"skipped " + why: n for why, n in getattr(imp, "skipped", {}).items()})
        if rebuild:
            _finish_rebuild(conn, imp, report, seen_units, rebuilt_units, partial_units,
                            yes=yes, dry_run=dry_run, confirm=confirm)
    finally:
        if locked:
            try:
                conn.rollback()
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(hashtext('import:' || %s))", (imp.name,))
                conn.commit()
            except Exception:
                pass
        if own:
            conn.close()
    return report


def _finish_rebuild(conn: psycopg.Connection, imp, report: ImportReport, seen_units: list[str],
                    rebuilt_units: list[str], partial_units: int, *, yes: bool = False, dry_run: bool = False,
                    confirm: Callable[[int, int], bool] | None = None) -> None:
    """Remove, from the units this run re-read, the rows the re-read no longer produced. Nothing else is touched."""
    if report.errors:
        report.notes.append("rebuild cleanup skipped: some units failed, nothing was removed")
        return
    if report.collisions:
        report.notes.append(f"rebuild cleanup skipped: {report.collisions} row(s) were skipped because their id already "
                            "belongs to another source or importer, so the re-read is not trustworthy; nothing was removed")
        return
    if not report.units_total or not report.labels:
        report.notes.append("rebuild cleanup skipped: the input produced no rows, nothing was removed")
        return
    skipped = sum(n for k, n in report.importer_stats.items() if k.startswith("skipped "))
    if skipped:
        report.notes.append(f"rebuild cleanup skipped: {skipped} file(s) were not read (symlink, too large, outside the "
                            "folder, ...), so the input is not complete; nothing was removed")
        return
    if partial_units:
        report.notes.append(f"rebuild: {partial_units} unit(s) lost lines while being read (too long, not JSON, no "
                            "timestamp); their rows are left alone")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM sources WHERE importer = %s AND NOT (unit_key = ANY(%s))",
                    (imp.name, seen_units))
        unseen = cur.fetchone()[0]
    if unseen:
        report.notes.append(f"rebuild: {unseen} unit(s) imported earlier are not in this input; their rows are left alone")
    if not rebuilt_units:
        return
    with conn.cursor() as cur:
        cur.execute("CREATE INDEX ON run_ids (id)")
        cur.execute("CREATE INDEX ON run_memory_ids (id)")
        doomed_m = ("FROM messages m WHERE m.importer = %s AND m.unit_key = ANY(%s) AND m.imported_at < %s AND NOT EXISTS "
                    "(SELECT 1 FROM run_ids r WHERE r.id = m.id)")
        doomed_k = ("FROM memories m WHERE m.importer = %s AND m.unit_key = ANY(%s) AND m.imported_at < %s AND NOT EXISTS "
                    "(SELECT 1 FROM run_memory_ids r WHERE r.id = m.id)")
        cur.execute("SELECT count(*) " + doomed_m, (imp.name, rebuilt_units, report.started))
        n_msg = cur.fetchone()[0]
        cur.execute("SELECT count(*) " + doomed_k, (imp.name, rebuilt_units, report.started))
        n_mem = cur.fetchone()[0]
        if not (n_msg or n_mem):
            return
        what = f"{n_msg} message row(s) and {n_mem} memory row(s)"
        if dry_run:
            report.notes.append(f"rebuild dry run: would remove {what} that the re-read no longer produces; nothing was removed")
            return
        if not (yes or (confirm is not None and confirm(n_msg, n_mem))):
            report.notes.append(f"rebuild: {what} are no longer produced by the input and were NOT removed; "
                                "run again with --yes to remove them (or --dry-run to only look)")
            return
        report.notes.append(f"rebuild: removing {what}")
        cur.execute("DELETE " + doomed_m, (imp.name, rebuilt_units, report.started))
        report.removed = cur.rowcount
        cur.execute("DELETE " + doomed_k, (imp.name, rebuilt_units, report.started))
        labels = sorted(report.labels)
        cur.execute("DELETE FROM conversations c WHERE c.source = ANY(%s) AND NOT EXISTS "
                    "(SELECT 1 FROM messages m WHERE m.source = c.source AND m.conv_id = c.conv_id)", (labels,))
        cur.execute(_CONVERSATIONS.replace("AND conv_id = ANY(%(convs)s)", ""), {"sources": labels, "convs": []})
    conn.commit()
