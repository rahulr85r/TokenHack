"""JVM-family adapter (Java + Kotlin) for TokenHack.

Java and Kotlin share enough AST structure that one adapter handles both,
dispatching to the right grammar by file extension.
"""
from ._base import Symbol, ExtractResult, get_text, first_line

LANGUAGE_NAME = "jvm"
FILE_EXTENSIONS = [".java", ".kt", ".kts"]

try:
    from tree_sitter import Parser, Language
    import tree_sitter_java
    _JAVA_PARSER = Parser(Language(tree_sitter_java.language()))
    JAVA_AVAILABLE = True
except Exception:
    _JAVA_PARSER = None
    JAVA_AVAILABLE = False

try:
    from tree_sitter import Parser, Language
    import tree_sitter_kotlin
    _KOTLIN_PARSER = Parser(Language(tree_sitter_kotlin.language()))
    KOTLIN_AVAILABLE = True
except Exception:
    _KOTLIN_PARSER = None
    KOTLIN_AVAILABLE = False

AVAILABLE = JAVA_AVAILABLE or KOTLIN_AVAILABLE

_JAVA_EXTS = (".java",)
_KOTLIN_EXTS = (".kt", ".kts")

_DEF_TYPES = {
    # Java
    "method_declaration", "class_declaration", "interface_declaration",
    "enum_declaration", "record_declaration", "constructor_declaration",
    "annotation_type_declaration",
    # Kotlin
    "function_declaration", "object_declaration", "property_declaration",
    "secondary_constructor",
}

_IMPORT_TYPES = {"import_declaration", "import_header"}
_NAME_FALLBACK_TYPES = ("simple_identifier", "identifier", "type_identifier")
_IMPORT_PATH_TYPES = (
    "scoped_identifier", "identifier", "dotted_name",
    "qualified_identifier", "navigation_expression",
)


def _parser_for(filepath: str):
    low = filepath.lower()
    if low.endswith(_JAVA_EXTS):
        return _JAVA_PARSER if JAVA_AVAILABLE else None
    if low.endswith(_KOTLIN_EXTS):
        return _KOTLIN_PARSER if KOTLIN_AVAILABLE else None
    return None


def _find_name(node):
    n = node.child_by_field_name("name")
    if n is not None:
        return n
    for child in node.named_children:
        if child.type in _NAME_FALLBACK_TYPES:
            return child
    return None


def extract(source: bytes, filepath: str) -> ExtractResult:
    parser = _parser_for(filepath)
    if parser is None:
        return ExtractResult()

    tree = parser.parse(source)
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
                    sig = first_line(get_text(node, source))[:200]
                    symbols.append(Symbol(name=name, line=line, end_line=end_line, kind="def", signature=sig))

        elif nt in _IMPORT_TYPES:
            for child in node.named_children:
                if child.type in _IMPORT_PATH_TYPES:
                    imports.append(get_text(child, source))
                    break

        elif nt == "method_invocation":  # Java
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                references.add(get_text(name_node, source))

        elif nt == "object_creation_expression":  # Java `new Foo()`
            t = node.child_by_field_name("type")
            if t is not None:
                references.add(get_text(t, source).split(".")[-1])

        elif nt == "call_expression":  # Kotlin / general
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
