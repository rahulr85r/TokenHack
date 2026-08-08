"""Swift adapter for TokenHack."""
from ._base import Symbol, ExtractResult, get_text, declaration_line

LANGUAGE_NAME = "swift"
FILE_EXTENSIONS = [".swift"]

try:
    from tree_sitter import Parser, Language
    import tree_sitter_swift
    _PARSER = Parser(Language(tree_sitter_swift.language()))
    AVAILABLE = True
except Exception:
    _PARSER = None
    AVAILABLE = False

_DEF_TYPES = {
    "function_declaration", "class_declaration", "struct_declaration",
    "protocol_declaration", "enum_declaration", "extension_declaration",
    "init_declaration", "deinit_declaration", "subscript_declaration",
    "property_declaration",
}

_NAME_FALLBACK_TYPES = ("simple_identifier", "type_identifier", "identifier")


def _find_name(node):
    n = node.child_by_field_name("name")
    if n is not None:
        return n
    for child in node.named_children:
        if child.type in _NAME_FALLBACK_TYPES:
            return child
    return None


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
            name_node = _find_name(node)
            if name_node is not None:
                name = get_text(name_node, source)
                if name:
                    line = name_node.start_point[0] + 1
                    end_line = node.end_point[0] + 1
                    sig = declaration_line(source, name_node)[:200]
                    symbols.append(Symbol(name=name, line=line, end_line=end_line, kind="def", signature=sig))

        elif nt == "import_declaration":
            for child in node.named_children:
                if child.type in _NAME_FALLBACK_TYPES:
                    imports.append(get_text(child, source))
                    break

        elif nt == "call_expression":
            for child in node.named_children:
                if child.type in _NAME_FALLBACK_TYPES + ("navigation_expression",):
                    references.add(get_text(child, source).split(".")[-1])
                    break

        for child in reversed(node.children):
            stack.append(child)

    return ExtractResult(
        symbols=symbols,
        imports=list(dict.fromkeys(imports)),
        references=sorted(r for r in references if r),
    )
