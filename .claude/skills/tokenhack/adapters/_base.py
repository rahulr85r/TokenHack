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


def declaration_line(source: bytes, name_node) -> str:
    """Return the source line the symbol's *name* sits on, stripped.

    Anchoring on the name rather than on the definition node's first line
    skips leading decorators, annotations and modifiers — `@Override`,
    `@Deprecated(since = "7.1")`, `@objc`, `@pytest.fixture` — which are the
    first line of the node but say nothing about what the definition is. The
    router surfaces this string next to each staged read range, so it needs to
    read like a signature.
    """
    start = name_node.start_byte
    left = source.rfind(b"\n", 0, start) + 1
    right = source.find(b"\n", start)
    if right == -1:
        right = len(source)
    return source[left:right].decode("utf-8", errors="replace").strip()


# ----------------------------------------------------------------------
# Doc-comment extraction (the prose channel)
# ----------------------------------------------------------------------
#
# The router scores a separate "prose" BM25 channel at the highest weight in the
# formula (ETA = 1.2), because docs are written in the words a human question
# uses while identifiers are written in the words a compiler needs. Until now
# only the Python adapter populated it — Java, Kotlin, Swift and JavaScript
# returned nothing — so on netty, Spring, Signal, DuckDuckGo and ownCloud the
# highest-weighted channel in the scoring function was empty for 100% of files.
# Javadoc/KDoc/Swift-doc is exactly the natural-language bridge those repos
# needed.

DOC_PREFIXES = ("/**", "/*!", "///", "//!")

_DOC_LINE_NOISE = ("/**", "/*!", "*/", "///", "//!")
_JAVADOC_TAG = None   # compiled lazily; adapters import this module a lot


def _doc_regexes():
    global _JAVADOC_TAG
    if _JAVADOC_TAG is None:
        import re
        _JAVADOC_TAG = (
            re.compile(r"\{@\w+\s+([^}]*)\}"),       # {@link Foo#bar} -> Foo#bar
            re.compile(r"^\s*@\w+"),                  # @param / @return line
            re.compile(r"<[^>]+>"),                   # <p>, <code>, </pre>
            re.compile(r"\s+"),
        )
    return _JAVADOC_TAG


def clean_doc_comment(text: str) -> str:
    """Turn a raw /** ... */ or /// block into a plain prose paragraph.

    Drops comment syntax, inline javadoc markup ({@link X} keeps X), HTML tags,
    and standalone block-tag lines (@param, @return, @throws), which are
    structure rather than description and mostly repeat identifiers the code
    channel already indexes.
    """
    inline_tag, block_tag, html, ws = _doc_regexes()
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        for noise in _DOC_LINE_NOISE:
            if line.startswith(noise):
                line = line[len(noise):]
        line = line.strip()
        if line.startswith("*"):
            line = line[1:].strip()
        if line.endswith("*/"):
            line = line[:-2].strip()
        if not line or block_tag.match(line):
            continue
        out.append(line)
    joined = " ".join(out)
    joined = inline_tag.sub(r"\1", joined)
    joined = html.sub(" ", joined)
    return ws.sub(" ", joined).strip()


def collect_doc_comments(root, source: bytes, max_paragraphs=8, max_chars=240):
    """Return (paragraphs, summary) from the doc comments in a parsed tree.

    Any node whose type contains "comment" is considered; only blocks opening
    with a doc marker are kept, so ordinary `//` implementation notes and
    license headers don't flood the channel. Language-agnostic on purpose —
    Java, Kotlin, Swift and JavaScript all spell doc comments the same way.
    """
    paragraphs = []
    stack = [root]
    while stack and len(paragraphs) < max_paragraphs:
        node = stack.pop()
        if "comment" in node.type:
            text = get_text(node, source).lstrip()
            if text.startswith(DOC_PREFIXES):
                cleaned = clean_doc_comment(text)
                # Skip license/copyright boilerplate, which every file repeats
                # and which would otherwise dominate the corpus term stats.
                low = cleaned[:120].lower()
                if cleaned and len(cleaned) > 24 and not (
                    "copyright" in low or "licensed under" in low
                    or "license, version" in low or "all rights reserved" in low
                ):
                    paragraphs.append(cleaned[:max_chars])
        for child in reversed(node.children):
            stack.append(child)
    summary = first_line(paragraphs[0]) if paragraphs else ""
    return paragraphs, summary[:max_chars]


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
