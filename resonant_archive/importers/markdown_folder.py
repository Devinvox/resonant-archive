"""markdown_folder: a folder of ``.md`` / ``.markdown`` / ``.mdx`` / ``.txt`` notes (the v0.1 behaviour).

Each file becomes one conversation (``conv_id`` = its path relative to the folder). The body is split into chunks
with the v0.1 chunker (paragraph or window strategy, frontmatter aware) and each chunk becomes one row, in order.
Rows are speaker ``user``, context ``notes``, tagged ``document`` plus the file's frontmatter tags. The timestamp is
the frontmatter ``create_time`` / ``date`` / ``timestamp`` / ``created`` when present, else the file's modification
time. Hidden files and folders (leading ``.``) are skipped.

Options (``Options.extra``): ``strategy`` (``paragraph`` | ``window``), ``max_chars``, ``min_chars``,
``overlap_chars``, ``extensions``.

Row ids include the source label, so importing the same folder under two labels keeps two copies on purpose
(v0.1 namespaces).
"""

from __future__ import annotations

import collections
from pathlib import Path
from typing import Iterator

from .. import chunker
from ..timeutil import parse_iso
from .base import Importer, Options, Record, Unit


class MarkdownFolder(Importer):
    name = "markdown_folder"
    description = "A folder of markdown/text notes, chunked (the v0.1 behaviour)"
    default_source = "notes"

    def _exts(self, opts: Options):
        return tuple(opts.extra.get("extensions") or chunker.DEFAULT_EXTENSIONS)

    def detect(self, path: Path) -> bool:
        path = Path(path)
        if path.is_file():
            return path.suffix.lower() in chunker.DEFAULT_EXTENSIONS
        return path.is_dir() and bool(chunker.walk_text_files(path, chunker.DEFAULT_EXTENSIONS))

    def units(self, path: Path, opts: Options) -> Iterator[Unit]:
        path = Path(path)
        root = path.parent if path.is_file() else path
        files = [path] if path.is_file() else chunker.walk_text_files(path, self._exts(opts))
        self.skipped = collections.Counter()
        for f in files:
            if not self.accept(f, root):
                continue
            rel = f.relative_to(root).as_posix()
            yield Unit(key=rel, files=[f], root=root)

    def unit_records(self, unit: Unit, opts: Options, stats: collections.Counter) -> Iterator[Record]:
        label = self.label(opts)
        f = unit.files[0]
        rel = unit.key
        ex = opts.extra
        chunks = chunker.chunk_file(
            f, rel,
            strategy=ex.get("strategy", "paragraph"),
            max_chars=int(ex.get("max_chars", chunker.DEFAULT_MAX_CHARS)),
            min_chars=int(ex.get("min_chars", chunker.DEFAULT_MIN_CHARS)),
            overlap_chars=int(ex.get("overlap_chars", chunker.DEFAULT_OVERLAP_CHARS)),
        )
        stats["files"] += 1
        for c in chunks:
            try:
                ts = parse_iso(c.timestamp) if c.timestamp else None
            except ValueError:
                ts = None
            if ts is None:
                stats["no_timestamp"] += 1
                ts = parse_iso("1970-01-01T00:00:00Z")
            yield Record(
                id=f"md:{label}:{c.chunk_id}", source=label, conv_id=rel, ts=ts, speaker="user", text=c.text,
                conv_title=c.title, context="notes", tags={"document", *(c.tags or [])}, src=rel, order=c.chunk_index,
            )
