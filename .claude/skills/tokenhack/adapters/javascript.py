"""JavaScript adapter for TokenHack (JSX included).

Note: TypeScript is intentionally NOT covered in v1 — see CONTRIBUTING
section of the top-level README for the planned first-PR scope.
"""
from ._base import Symbol, ExtractResult, get_text, first_line

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


def extract(source: bytes, filepath: str) -> ExtractResult:
    if not AVAILABLE:
        return ExtractResult()

    tree = _PARSER.parse(source)
    root = tree.root_node

    symbols = []
    imports = []
    references = set()

    stack = [root]
    while stack:
        node = stack.pop()
        nt = node.type

        if nt in _DEF_TYPES:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = get_text(name_node, source)
                if name:
                    line = name_node.start_point[0] + 1
                    sig = first_line(get_text(node, source))[:200]
                    symbols.append(Symbol(name=name, line=line, kind="def", signature=sig))

        elif nt == "variable_declarator":
            # const Foo = (...) => {...} or const Foo = function(...) {...}
            value = node.child_by_field_name("value")
            if value is not None and value.type in _LAMBDA_VALUE_TYPES:
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    name = get_text(name_node, source)
                    if name:
                        line = name_node.start_point[0] + 1
                        sig = first_line(get_text(node, source))[:200]
                        symbols.append(Symbol(name=name, line=line, kind="def", signature=sig))

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

    return ExtractResult(
        symbols=symbols,
        imports=list(dict.fromkeys(imports)),
        references=sorted(r for r in references if r),
    )
