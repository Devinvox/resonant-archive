"""Importer registry: the built-in importers plus any registered through the ``resonant_archive.importers``
entry-point group.

A third-party package registers one like this in its ``pyproject.toml``::

    [project.entry-points."resonant_archive.importers"]
    my_format = "my_package.importer:MyImporter"

The entry point may name an ``Importer`` subclass or an instance. A plugin that fails to load is reported by
``plugin_errors()`` and never stops the built-ins from working.
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path

from .base import Importer, Memory, Options, Record, Unit
from .chronicle_mirror import ChronicleMirror
from .claude_code import ClaudeCode
from .markdown_folder import MarkdownFolder
from .nexus_markdown import NexusMarkdown

GROUP = "resonant_archive.importers"

#: Order matters for ``detect``: specific formats first, the catch-all markdown folder last.
BUILTIN: tuple[type[Importer], ...] = (ClaudeCode, ChronicleMirror, NexusMarkdown, MarkdownFolder)

_plugin_errors: list[str] = []


def available() -> dict[str, Importer]:
    """{name: importer instance} for the built-ins and every loadable plugin (built-ins win a name clash)."""
    _plugin_errors.clear()
    found: dict[str, Importer] = {cls.name: cls() for cls in BUILTIN}
    try:
        eps = metadata.entry_points(group=GROUP)
    except Exception as exc:  # a broken environment must not break the built-ins
        _plugin_errors.append(f"could not list plugins: {exc}")
        return found
    for ep in eps:
        try:
            obj = ep.load()
            inst = obj() if isinstance(obj, type) else obj
            if not isinstance(inst, Importer) or not inst.name:
                raise TypeError("not an Importer with a name")
            if inst.name in found:
                raise ValueError(f"name {inst.name!r} is already taken")
            found[inst.name] = inst
        except Exception as exc:
            _plugin_errors.append(f"plugin {ep.name}: {type(exc).__name__}: {exc}")
    return found


def plugin_errors() -> list[str]:
    return list(_plugin_errors)


def get(name: str) -> Importer:
    reg = available()
    if name not in reg:
        raise KeyError(f"unknown importer {name!r}; available: {', '.join(sorted(reg))}")
    return reg[name]


def detect(path: str | Path) -> list[str]:
    """Names of the importers that can read ``path``, most specific first."""
    p = Path(path).expanduser()
    found = []
    for name, imp in available().items():
        try:
            if imp.detect(p):
                found.append(name)
        except Exception:  # hostile or broken input (a deeply nested JSON line, a vanished file) is "not mine"
            continue
    return found


__all__ = ["Importer", "Memory", "Options", "Record", "Unit", "GROUP", "BUILTIN", "available", "get", "detect",
           "plugin_errors"]
