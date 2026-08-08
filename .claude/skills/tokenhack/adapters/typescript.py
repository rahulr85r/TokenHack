"""TypeScript / TSX adapter for TokenHack.

The TypeScript grammar is a superset of the JavaScript one, so the AST walk is
imported wholesale from `javascript.py` and only the extra declaration forms
are added here: interfaces, type aliases, enums, abstract classes, and the
class-property / constructor-parameter shapes TS introduces.

`.ts` and `.tsx` need *different* parsers — tree-sitter-typescript ships two
grammars, because `<T>` is a type assertion in .ts and a JSX element in .tsx.
Dispatching on extension is the whole reason this file isn't three lines.
"""
from .javascript import _DEF_TYPES as _JS_DEF_TYPES, walk_source
from ._base import ExtractResult

LANGUAGE_NAME = "typescript"
FILE_EXTENSIONS = [".ts", ".tsx", ".mts", ".cts"]

try:
    from tree_sitter import Parser, Language
    import tree_sitter_typescript
    _TS_PARSER = Parser(Language(tree_sitter_typescript.language_typescript()))
    _TSX_PARSER = Parser(Language(tree_sitter_typescript.language_tsx()))
    AVAILABLE = True
except Exception:
    _TS_PARSER = None
    _TSX_PARSER = None
    AVAILABLE = False

# TS-only declaration forms. `interface_declaration` and `type_alias_declaration`
# matter most: in a typed codebase the domain vocabulary lives in the types, and
# a question about "the order payload" is far likelier to hit `interface Order`
# than any function name.
_TS_EXTRA_DEF_TYPES = {
    "interface_declaration",
    "type_alias_declaration",
    "enum_declaration",
    "abstract_class_declaration",
    "module",                       # `declare module Foo` / namespace
    "internal_module",
    "public_field_definition",      # class properties, incl. arrow-function members
    "abstract_method_signature",
    "method_signature",
    "property_signature",
    "function_signature",
}

_DEF_TYPES = _JS_DEF_TYPES | _TS_EXTRA_DEF_TYPES

_TSX_EXTENSIONS = (".tsx",)


def _parser_for(filepath: str):
    return _TSX_PARSER if filepath.lower().endswith(_TSX_EXTENSIONS) else _TS_PARSER


def extract(source: bytes, filepath: str) -> ExtractResult:
    if not AVAILABLE:
        return ExtractResult()
    parser = _parser_for(filepath)
    if parser is None:
        return ExtractResult()
    return walk_source(parser, source, _DEF_TYPES)
