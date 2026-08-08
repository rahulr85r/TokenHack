"""JavaScript adapter for TokenHack (JSX included).

The AST walk lives in `walk_source` so the TypeScript adapter can reuse it —
the grammars share the great majority of their node types, and TS only needs to
add its own declaration forms (interface, type alias, enum) on top.
"""
from ._base import (Symbol, ExtractResult, get_text, declaration_line,
                    collect_doc_comments)

LANGUAGE_NAME = "javascript"
FILE_EXTENSIONS = [".js", ".mjs", ".cjs", ".jsx"]

try:
    from tree_sitter import Parser, Language
    import tree_sitter_javascript
    _PARSER = Parser(Language(tree_sitter_javascript.language()))
    AVAILABLE = True
except Exception:
    _PARSER = None
    AVAILABLE = False

_DEF_TYPES = {
    "function_declaration", "class_declaration", "method_definition",
    "generator_function_declaration",
}

_LAMBDA_VALUE_TYPES = {
    "arrow_function", "function_expression", "function", "generator_function",
}


def walk_source(parser, source: bytes, def_types) -> ExtractResult:
    """Shared JS/TS extraction: definitions, imports, call references, docs."""
    tree = parser.parse(source)
    root = tree.root_node

    symbols = []
    imports = []
    references = set()

    stack = [root]
    while stack:
        node = stack.pop()
        nt = node.type

        if nt in def_types:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = get_text(name_node, source)
                if name:
                    line = name_node.start_point[0] + 1
                    end_line = node.end_point[0] + 1
                    sig = declaration_line(source, name_node)[:200]
                    symbols.append(Symbol(name=name, line=line, end_line=end_line, kind="def", signature=sig))

        elif nt == "variable_declarator":
            # const Foo = (...) => {...} or const Foo = function(...) {...}
            value = node.child_by_field_name("value")
            if value is not None and value.type in _LAMBDA_VALUE_TYPES:
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    name = get_text(name_node, source)
                    if name:
                        line = name_node.start_point[0] + 1
                        end_line = node.end_point[0] + 1
                        sig = declaration_line(source, name_node)[:200]
                        symbols.append(Symbol(name=name, line=line, end_line=end_line, kind="def", signature=sig))

        elif nt == "import_statement":
            src = node.child_by_field_name("source")
            if src is not None:
                imports.append(get_text(src, source).strip("'\""))

        elif nt == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None:
                if fn.type == "identifier":
                    references.add(get_text(fn, source))
                elif fn.type == "member_expression":
                    prop = fn.child_by_field_name("property")
                    if prop is not None:
                        references.add(get_text(prop, source))

        for child in reversed(node.children):
            stack.append(child)

    prose, summary = collect_doc_comments(root, source)

    return ExtractResult(
        symbols=symbols,
        imports=list(dict.fromkeys(imports)),
        references=sorted(r for r in references if r),
        docstring_summary=summary,
        prose_paragraphs=prose,
    )


def extract(source: bytes, filepath: str) -> ExtractResult:
    if not AVAILABLE:
        return ExtractResult()
    return walk_source(_PARSER, source, _DEF_TYPES)
