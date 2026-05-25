"""Language adapters for TokenHack.

Each adapter module exposes a uniform surface:

    LANGUAGE_NAME: str
    FILE_EXTENSIONS: list[str]   # leading-dot extensions handled
    AVAILABLE: bool              # True if the tree-sitter grammar loaded
    extract(source: bytes, filepath: str) -> ExtractResult

Adapters degrade gracefully: if the underlying tree-sitter grammar is
not installed, AVAILABLE is False and extract() returns an empty result
without raising.

To add a new language: create adapters/<language>.py with the surface
above, then import it below and add it to the _MODULES tuple.
"""
from ._base import Symbol, ExtractResult, get_text, first_line, strip_string_quotes

from . import python as _python
from . import jvm as _jvm
from . import swift as _swift
from . import javascript as _javascript

_MODULES = (_python, _jvm, _swift, _javascript)


def _build_ext_map():
    m = {}
    for mod in _MODULES:
        for ext in mod.FILE_EXTENSIONS:
            m[ext.lower()] = mod
    return m


_EXT_MAP = _build_ext_map()


def get_adapter(filepath: str):
    """Return the adapter module for a file path, or None if unsupported."""
    import os
    _, ext = os.path.splitext(filepath)
    return _EXT_MAP.get(ext.lower())


def supported_extensions():
    """Return all file extensions for which an adapter exists."""
    return sorted(_EXT_MAP.keys())


def availability_report():
    """Return {language_name: available_bool} — useful for diagnostics."""
    return {mod.LANGUAGE_NAME: bool(getattr(mod, "AVAILABLE", False)) for mod in _MODULES}
