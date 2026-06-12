"""Base types and utilities shared by all TokenHack language adapters."""
from dataclasses import dataclass, field, asdict
from typing import List


@dataclass
class Symbol:
    name: str
    line: int
    kind: str  # "def" (definition) or "ref" (reference) — v1 only emits "def"
    # Last line of the definition's span (1-based, inclusive). Enables the
    # router to stage a precise `path:line-end_line` read instead of pointing
    # at the whole file. Defaults to 0 (unknown) so an older index — or an
    # adapter that hasn't been updated — still loads and the router falls back
    # to a from-`line` read.
    end_line: int = 0
    signature: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass
class ExtractResult:
    symbols: List[Symbol] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)
    docstring_summary: str = ""
    prose_paragraphs: List[str] = field(default_factory=list)
    references: List[str] = field(default_factory=list)


def get_text(node, source: bytes) -> str:
    """Return the UTF-8 text of a tree-sitter node, replacing invalid bytes."""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def first_line(text: str) -> str:
    return text.strip().split("\n", 1)[0].strip()


def strip_string_quotes(s: str) -> str:
    """Strip leading/trailing string quote markers (single, double, triple)."""
    s = s.strip()
    for q in ('"""', "'''", '"', "'"):
        if s.startswith(q):
            s = s[len(q):]
            break
    for q in ('"""', "'''", '"', "'"):
        if s.endswith(q):
            s = s[:-len(q)]
            break
    return s.strip()
