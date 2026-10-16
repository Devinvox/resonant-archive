"""The importer interface.

An importer turns one kind of input into ``Record`` rows (and ``Memory`` rows for the chronicle mod). It does not
redact, truncate, deduplicate or write to the database (one exception: a tool call's input is redacted by structure
before it is shortened, see ``Record.cut``): the pipeline (``resonant_archive.pipeline``) does that,
the same way for every importer. An importer reads, never writes, and never prints message text.

Implement::

    class MyImporter(Importer):
        name = "my_format"
        description = "one line"
        default_source = "my-label"

        def detect(self, path): ...                  # True if this importer can read ``path``
        def units(self, path, opts): ...             # yield Unit(key, files): the pieces that are imported (and skipped) whole
        def unit_records(self, unit, opts, stats):   # yield Record / Memory for one unit

A *unit* is the smallest piece that is re-read as a whole when it changes: a file, or a session spread over several
files. A unit whose files are byte-identical to the last import is skipped (see ``pipeline``). Message ids must be
deterministic: the same input must produce the same ids, or re-imports will duplicate.

Third-party packages register importers through the ``resonant_archive.importers`` entry-point group.
"""

from __future__ import annotations

import collections
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..timeutil import as_utc

#: Allowed values of messages.speaker. 'agent' is the AI the person is talking to. 'peer' is another mind or
#: another agent speaking in the same conversation.
SPEAKERS = ("user", "agent", "assistant", "peer", "system", "tool")

#: A tag in this set keeps an otherwise empty row (a message that was only an image, say).
MEDIA_TAGS = frozenset({"image", "audio", "file"})


@dataclass
class Record:
    """One message, as an importer sees it (before redaction and truncation)."""

    id: str
    source: str
    conv_id: str
    ts: dt.datetime
    speaker: str
    text: str
    conv_title: str | None = None
    context: str = "default"
    platform: str | None = None
    model: str | None = None
    hidden: bool = False
    tags: Iterable[str] = ()
    src: str = ""
    order: int = 0  # tie-break for rows with the same timestamp inside a conversation
    #: Keep only this many characters of ``text``, cut AFTER redaction (a secret a cut would split is already gone).
    #: Use it instead of slicing in the importer.
    cut: int | None = None

    def __post_init__(self) -> None:
        if self.speaker not in SPEAKERS:
            raise ValueError(f"unknown speaker {self.speaker!r}")
        if not self.id or not self.conv_id or not self.source:
            raise ValueError("id, conv_id and source are required")
        self.ts = as_utc(self.ts)
        self.conv_id = str(self.conv_id)
        self.text = "" if self.text is None else self.text
        self.tags = sorted(set(self.tags))
        self.hidden = bool(self.hidden)
        self.model = self.model or None


@dataclass
class Memory:
    """One chronicle L1 memory. Goes to the ``memories`` table."""

    id: str
    source: str
    sid: str
    ts: dt.datetime
    text: str
    level: int = 1
    ordinal: int | None = None
    t_from: dt.datetime | None = None
    t_to: dt.datetime | None = None
    from_uuid: str | None = None
    to_uuid: str | None = None
    est_tokens: int | None = None
    rows: int | None = None
    source_hash: str | None = None

    def __post_init__(self) -> None:
        self.ts = as_utc(self.ts)


@dataclass
class Unit:
    """The smallest piece of input that is imported (or skipped as unchanged) as a whole."""

    key: str  # stable across runs and machines for the same logical piece (not an absolute path)
    files: list[Path]
    label: str = ""  # shown in reports; defaults to the key
    root: Path | None = None  # the folder the person named; every file must resolve inside it (see importers.safety)

    def __post_init__(self) -> None:
        self.files = [Path(f) for f in self.files]
        self.label = self.label or self.key
        self.root = Path(self.root) if self.root is not None else None


@dataclass
class Options:
    """Settings an importer may read. Unknown fields are ignored by importers that do not use them."""

    source: str | None = None  # label written to messages.source (importer default when None)
    timezone: str | None = None  # zone of timestamps that carry none (None = this machine's zone)
    date_order: str = "dmy"  # nexus claude.ai headings: 'dmy' or 'mdy'
    projects: list[str] | None = None  # claude_code: only these project folder names
    extra: dict[str, Any] = field(default_factory=dict)


class Importer:
    name: str = ""
    description: str = ""
    default_source: str = ""
    #: Bump when a parser change should make every unit re-import.
    version: int = 1

    def detect(self, path: Path) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def units(self, path: Path, opts: Options) -> Iterator[Unit]:  # pragma: no cover - interface
        raise NotImplementedError

    def unit_records(self, unit: Unit, opts: Options, stats: collections.Counter) -> Iterator[Record | Memory]:
        raise NotImplementedError  # pragma: no cover - interface

    # ---- provided

    #: Files an importer declined to read, by reason (symlink, too large, outside root, ...). Reset by ``units``.
    skipped: collections.Counter

    def accept(self, path: Path, root: Path | None) -> bool:
        """True if ``path`` may be read (a regular file inside ``root``, not too large); otherwise counted in
        ``skipped`` and False. Importers call this while listing; the pipeline checks again before reading."""
        from . import safety

        if "skipped" not in self.__dict__:
            self.skipped = collections.Counter()
        why = safety.problem(path, root)
        if why:
            self.skipped[why] += 1
        return why is None

    def label(self, opts: Options) -> str:
        return opts.source or self.default_source

    def records(self, path: Path, opts: Options | None = None, stats: collections.Counter | None = None) -> Iterator[Record | Memory]:
        """Every record in ``path``, unit by unit. Useful for tests and one-off scripts; the pipeline uses units."""
        opts = opts or Options()
        stats = stats if stats is not None else collections.Counter()
        for unit in self.units(Path(path), opts):
            yield from self.unit_records(unit, opts, stats)
