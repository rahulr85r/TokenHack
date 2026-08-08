"""Python adapter for TokenHack.

Extracts function/class definitions, imports, references (call targets),
and docstring summaries via tree-sitter-python.
"""
from ._base import Symbol, ExtractResult, get_text, first_line, declaration_line, strip_string_quotes

LANGUAGE_NAME = "python"
FILE_EXTENSIONS = [".py"]

try:
    from tree_sitter import Parser, Language
    import tree_sitter_python
    _PARSER = Parser(Language(tree_sitter_python.language()))
    AVAILABLE = True
except Exception:
    _PARSER = None
    AVAILABLE = False


def _docstring_of(body_node, source: bytes) -> str:
    if not body_node:
        return ""
    for child in body_node.named_children:
        if child.type != "expression_statement":
            return ""
        for sub in child.named_children:
            if sub.type == "string":
                return strip_string_quotes(get_text(sub, source))
        return ""
    return ""


def extract(source: bytes, filepath: str) -> ExtractResult:
    if not AVAILABLE:
        return ExtractResult()

    tree = _PARSER.parse(source)
    root = tree.root_node

    symbols = []
    imports = []
    prose = []
    references = set()

    # Module-level docstring (first string literal at module top)
    module_doc = ""
    for child in root.named_children:
        if child.type == "expression_statement":
            for sub in child.named_children:
                if sub.type == "string":
                    module_doc = strip_string_quotes(get_text(sub, source))
                    break
        break
    if module_doc:
        prose.append(first_line(module_doc))

    stack = [root]
    while stack:
        node = stack.pop()
        nt = node.type

        if nt in ("function_definition", "class_definition"):
            name_node = node.child_by_field_name("name")
            body_node = node.child_by_field_name("body")
            if name_node is not None:
                name = get_text(name_node, source)
                line = name_node.start_point[0] + 1
                end_line = node.end_point[0] + 1
                sig = declaration_line(source, name_node)[:200]
                symbols.append(Symbol(name=name, line=line, end_line=end_line, kind="def", signature=sig))
                doc = _docstring_of(body_node, source)
                if doc:
                    prose.append(first_line(doc))

        elif nt == "import_statement":
            for child in node.named_children:
                if child.type == "dotted_name":
                    imports.append(get_text(child, source))
                elif child.type == "aliased_import":
                    n = child.child_by_field_name("name")
                    if n is not None:
                        imports.append(get_text(n, source))

        elif nt == "import_from_statement":
            m = node.child_by_field_name("module_name")
            if m is not None:
                imports.append(get_text(m, source))

        elif nt == "call":
            fn = node.child_by_field_name("function")
            if fn is not None:
                if fn.type == "identifier":
                    references.add(get_text(fn, source))
                elif fn.type == "attribute":
                    attr = fn.child_by_field_name("attribute")
                    if attr is not None:
                        references.add(get_text(attr, source))

        for child in reversed(node.children):
            stack.append(child)

    return ExtractResult(
        symbols=symbols,
        imports=list(dict.fromkeys(imports)),
        docstring_summary=first_line(module_doc),
        prose_paragraphs=prose[:20],
        references=sorted(references),
    )
