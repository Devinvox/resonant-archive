"""What an importer may read: regular files inside the folder it was pointed at, of a sane size.

A symlink in an archive folder can name any file the importing user can read (``~/.ssh/id_rsa``, ``/etc/passwd``), and
a FIFO or a device would hang the import. So an input file must be a regular file (not a symlink), must resolve to a
path inside the root the person named, and must not exceed ``MAX_FILE_BYTES``. Inside a JSON-lines file a line longer
than ``MAX_LINE_CHARS`` is skipped (and counted). The pipeline checks again just before it reads a unit, so a file
swapped after the folder was listed is caught too.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import IO, Iterator

#: Per-file ceiling. A bigger file is skipped whole (and counted), not truncated.
MAX_FILE_BYTES = 256 * 1024 * 1024
#: Per-line ceiling for JSON-lines input, in characters.
MAX_LINE_CHARS = 16 * 1024 * 1024


def problem(path: Path, root: Path | None = None) -> str | None:
    """Why ``path`` must not be read (``symlink``, ``not a regular file``, ``too large``, ``outside root``,
    ``unreadable``), or None when it may."""
    path = Path(path)
    try:
        if path.is_symlink():
            return "symlink"
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode):
            return "not a regular file"
        if st.st_size > MAX_FILE_BYTES:
            return "too large"
        if root is not None:
            real, base = path.resolve(), Path(root).resolve()
            if real != base and base not in real.parents:
                return "outside root"
    except (OSError, RuntimeError):
        return "unreadable"
    return None


def iter_lines(fh: IO[str], stats=None, limit: int | None = None) -> Iterator[tuple[int, str]]:
    """``(line number, line)`` for a text file, skipping lines longer than ``limit`` characters (counted in
    ``stats['long_lines']``). Memory use is bounded by ``limit`` however long a line is."""
    limit = MAX_LINE_CHARS if limit is None else limit
    ln = -1
    while True:
        line = fh.readline(limit + 1)
        if not line:
            return
        ln += 1
        if len(line) > limit and not line.endswith("\n"):
            while True:  # drain the rest of the line in pieces
                rest = fh.readline(limit)
                if not rest or rest.endswith("\n"):
                    break
            if stats is not None:
                stats["long_lines"] += 1
            continue
        yield ln, line
