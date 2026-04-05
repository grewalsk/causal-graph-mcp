"""Tree-sitter based parser for JavaScript, TypeScript, Go, Rust, and Java."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter import Language, Node, Parser

from causal_graph_mcp.parser import ParseResult

logger = logging.getLogger(__name__)

# Side-effect patterns per language
_SIDE_EFFECTS: dict[str, dict[str, str]] = {
    "javascript": {
        "fetch": "network", "axios": "network", "http": "network", "https": "network",
        "XMLHttpRequest": "network", "request": "network",
        "fs.": "file_io", "readFile": "file_io", "writeFile": "file_io",
        "child_process": "process", "exec": "process", "spawn": "process",
        "redis": "cache", "memcached": "cache",
    },
    "typescript": {
        "fetch": "network", "axios": "network", "http": "network", "https": "network",
        "XMLHttpRequest": "network", "request": "network",
        "fs.": "file_io", "readFile": "file_io", "writeFile": "file_io",
        "child_process": "process", "exec": "process", "spawn": "process",
        "redis": "cache", "memcached": "cache",
    },
    "go": {
        "http.": "network", "net.": "network", "grpc.": "network",
        "os.Open": "file_io", "os.Create": "file_io", "os.ReadFile": "file_io",
        "ioutil.": "file_io", "bufio.": "file_io",
        "exec.Command": "process", "os.StartProcess": "process",
        "redis.": "cache", "memcache.": "cache",
    },
    "rust": {
        "reqwest": "network", "hyper": "network", "surf": "network",
        "std::fs": "file_io", "tokio::fs": "file_io", "File::": "file_io",
        "Command::": "process", "std::process": "process",
        "redis": "cache",
    },
    "java": {
        "HttpClient": "network", "HttpURLConnection": "network",
        "RestTemplate": "network", "WebClient": "network", "OkHttp": "network",
        "FileInputStream": "file_io", "FileOutputStream": "file_io",
        "BufferedReader": "file_io", "Files.": "file_io",
        "Runtime.exec": "process", "ProcessBuilder": "process",
        "Jedis": "cache", "RedisTemplate": "cache",
    },
}

# Test file detection patterns per language
_TEST_PATTERNS: dict[str, dict[str, Any]] = {
    "javascript": {
        "file_patterns": ["test_", "_test", ".test.", ".spec.", "__tests__"],
        "assert_calls": ["expect", "assert", "should", "toBe", "toEqual",
                         "toHaveBeenCalled", "toThrow", "toContain"],
    },
    "typescript": {
        "file_patterns": ["test_", "_test", ".test.", ".spec.", "__tests__"],
        "assert_calls": ["expect", "assert", "should", "toBe", "toEqual",
                         "toHaveBeenCalled", "toThrow", "toContain"],
    },
    "go": {
        "file_patterns": ["_test.go"],
        "assert_calls": ["t.Fatal", "t.Error", "t.Fail", "assert.", "require.",
                         "t.Run", "testing."],
    },
    "rust": {
        "file_patterns": [],  # Rust tests are inline with #[test]
        "assert_calls": ["assert!", "assert_eq!", "assert_ne!", "panic!"],
        "test_annotations": ["#[test]", "#[cfg(test)]"],
    },
    "java": {
        "file_patterns": ["Test", "test_", "_test"],
        "assert_calls": ["assertEquals", "assertTrue", "assertFalse",
                         "assertNotNull", "assertThrows", "assertThat",
                         "verify", "when"],
        "test_annotations": ["@Test", "@ParameterizedTest"],
    },
}


def _load_language(lang_name: str) -> Language | None:
    """Load a tree-sitter language grammar."""
    try:
        if lang_name == "javascript":
            import tree_sitter_javascript as ts
            return Language(ts.language())
        elif lang_name == "typescript":
            import tree_sitter_typescript as ts
            return Language(ts.language_typescript())
        elif lang_name == "go":
            import tree_sitter_go as ts
            return Language(ts.language())
        elif lang_name == "rust":
            import tree_sitter_rust as ts
            return Language(ts.language())
        elif lang_name == "java":
            import tree_sitter_java as ts
            return Language(ts.language())
    except ImportError:
        logger.warning("tree-sitter grammar for %s not installed", lang_name)
    return None


# Cache loaded languages
_LANGUAGES: dict[str, Language] = {}


def _get_language(lang_name: str) -> Language | None:
    if lang_name not in _LANGUAGES:
        lang = _load_language(lang_name)
        if lang:
            _LANGUAGES[lang_name] = lang
    return _LANGUAGES.get(lang_name)


def _node_text(node: Node, source: bytes) -> str:
    """Extract the text of a tree-sitter node."""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _compute_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _is_test_file(file_path: str, lang_name: str) -> bool:
    patterns = _TEST_PATTERNS.get(lang_name, {}).get("file_patterns", [])
    name = Path(file_path).name
    return any(p in name for p in patterns)


def _find_children_by_type(node: Node, type_name: str) -> list[Node]:
    """Recursively find all descendants of a given type."""
    results: list[Node] = []
    for child in node.children:
        if child.type == type_name:
            results.append(child)
        results.extend(_find_children_by_type(child, type_name))
    return results


def _get_name(node: Node, source: bytes) -> str:
    """Extract the name from a named node (function, class, etc.)."""
    for child in node.children:
        if child.type in ("identifier", "property_identifier", "type_identifier"):
            return _node_text(child, source)
    return ""


def _get_call_name(node: Node, source: bytes) -> str:
    """Extract the full callee name from a call expression."""
    func = node.child_by_field_name("function")
    if func is None:
        # Try first child for languages where call_expression structure varies
        for child in node.children:
            if child.type in ("identifier", "member_expression", "field_expression",
                              "scoped_identifier", "method_invocation"):
                return _node_text(child, source)
        return ""
    return _node_text(func, source)


# Language-specific node type mappings
_NODE_TYPES: dict[str, dict[str, list[str]]] = {
    "javascript": {
        "function": ["function_declaration", "arrow_function", "method_definition"],
        "class": ["class_declaration"],
        "variable": ["variable_declaration", "lexical_declaration"],
        "call": ["call_expression"],
        "assignment": ["assignment_expression", "augmented_assignment_expression"],
        "import": ["import_statement"],
    },
    "typescript": {
        "function": ["function_declaration", "arrow_function", "method_definition"],
        "class": ["class_declaration", "interface_declaration"],
        "variable": ["variable_declaration", "lexical_declaration"],
        "call": ["call_expression"],
        "assignment": ["assignment_expression", "augmented_assignment_expression"],
        "import": ["import_statement"],
    },
    "go": {
        "function": ["function_declaration", "method_declaration"],
        "class": ["type_declaration"],  # struct types
        "variable": ["var_declaration", "const_declaration", "short_var_declaration"],
        "call": ["call_expression"],
        "assignment": ["assignment_statement"],
        "import": ["import_declaration"],
    },
    "rust": {
        "function": ["function_item"],
        "class": ["struct_item", "enum_item", "trait_item", "impl_item"],
        "variable": ["let_declaration", "static_item", "const_item"],
        "call": ["call_expression"],
        "assignment": ["assignment_expression"],
        "import": ["use_declaration"],
    },
    "java": {
        "function": ["method_declaration", "constructor_declaration"],
        "class": ["class_declaration", "interface_declaration", "enum_declaration"],
        "variable": ["field_declaration", "local_variable_declaration"],
        "call": ["method_invocation"],
        "assignment": ["assignment_expression"],
        "import": ["import_declaration"],
    },
}


class TreeSitterParser:
    """Tree-sitter based parser for a specific language."""

    def __init__(self, lang_name: str, extensions: list[str]) -> None:
        self._lang_name = lang_name
        self._extensions = extensions
        self._ts_language = _get_language(lang_name)
        if self._ts_language:
            self._parser = Parser(self._ts_language)
        else:
            self._parser = None

    @property
    def file_extensions(self) -> list[str]:
        return self._extensions

    @property
    def language_name(self) -> str:
        return self._lang_name

    def parse(self, file_path: str, module_name: str) -> ParseResult:
        if self._parser is None:
            return ParseResult()

        try:
            source = Path(file_path).read_bytes()
        except (OSError, IOError):
            return ParseResult()

        try:
            tree = self._parser.parse(source)
        except Exception:
            return ParseResult()

        is_test = _is_test_file(file_path, self._lang_name)
        node_types = _NODE_TYPES.get(self._lang_name, {})
        result = ParseResult()

        # Extract nodes
        self._extract_nodes(tree.root_node, source, module_name, file_path, is_test, node_types, result)

        # Extract edges
        self._extract_edges(tree.root_node, source, module_name, file_path, is_test, node_types, result)

        return result

    def _extract_nodes(
        self, root: Node, source: bytes, module: str, file_path: str,
        is_test: bool, node_types: dict, result: ParseResult,
    ) -> None:
        """Extract function, class, and variable nodes."""
        # Functions/methods
        for type_name in node_types.get("function", []):
            for node in _find_children_by_type(root, type_name):
                name = _get_name(node, source)
                if not name:
                    continue

                # Determine if this is a method (inside a class)
                parent = node.parent
                kind = "function"
                node_id = f"{module}.{name}"

                while parent:
                    if parent.type in node_types.get("class", []):
                        parent_name = _get_name(parent, source)
                        if parent_name:
                            kind = "method"
                            node_id = f"{module}.{parent_name}.{name}"
                        break
                    parent = parent.parent

                result.nodes.append({
                    "id": node_id,
                    "kind": kind,
                    "module": module,
                    "file": file_path,
                    "line_start": node.start_point[0] + 1,
                    "line_end": node.end_point[0] + 1,
                    "signature": _node_text(node, source).split("{")[0].strip()[:200],
                    "docstring": self._extract_docstring(node, source),
                    "is_public": 0 if name.startswith("_") else 1,
                    "is_test": 1 if is_test else 0,
                    "body_hash": _compute_hash(_node_text(node, source)),
                })

        # Classes
        for type_name in node_types.get("class", []):
            for node in _find_children_by_type(root, type_name):
                name = _get_name(node, source)
                if not name:
                    continue
                result.nodes.append({
                    "id": f"{module}.{name}",
                    "kind": "class",
                    "module": module,
                    "file": file_path,
                    "line_start": node.start_point[0] + 1,
                    "line_end": node.end_point[0] + 1,
                    "signature": f"class {name}",
                    "docstring": self._extract_docstring(node, source),
                    "is_public": 0 if name.startswith("_") else 1,
                    "is_test": 1 if is_test else 0,
                    "body_hash": _compute_hash(_node_text(node, source)),
                })

                # Inheritance edges
                self._extract_inheritance(node, source, module, name, result)

    def _extract_edges(
        self, root: Node, source: bytes, module: str, file_path: str,
        is_test: bool, node_types: dict, result: ParseResult,
    ) -> None:
        """Extract call, mutation, assertion, side-effect, and import edges."""
        # Build scope map for resolution
        scope_map: dict[str, str] = {}
        for n in result.nodes:
            short_name = n["id"].split(".")[-1]
            scope_map[short_name] = n["id"]

        # Walk all function bodies for edges
        for type_name in node_types.get("function", []):
            for func_node in _find_children_by_type(root, type_name):
                func_name = _get_name(func_node, source)
                if not func_name:
                    continue

                # Determine qualified ID
                parent = func_node.parent
                func_id = f"{module}.{func_name}"
                while parent:
                    if parent.type in node_types.get("class", []):
                        parent_name = _get_name(parent, source)
                        if parent_name:
                            func_id = f"{module}.{parent_name}.{func_name}"
                        break
                    parent = parent.parent

                # Call edges
                for call_type in node_types.get("call", []):
                    for call_node in _find_children_by_type(func_node, call_type):
                        call_name = _get_call_name(call_node, source)
                        if not call_name:
                            continue

                        # Resolve
                        simple = call_name.split(".")[-1] if "." in call_name else call_name
                        if simple in scope_map:
                            dst = scope_map[simple]
                            confidence = 1.0
                        else:
                            dst = call_name
                            confidence = 0.3

                        result.edges.append({
                            "src": func_id, "dst": dst,
                            "kind": "calls", "confidence": confidence,
                        })

                        # Side-effect detection
                        se_kind = self._match_side_effect(call_name)
                        if se_kind:
                            result.edges.append({
                                "src": func_id,
                                "dst": f"side_effect:{se_kind}:{call_name}",
                                "kind": "side_effect",
                                "confidence": 1.0,
                                "detail": f'{{"kind": "{se_kind}", "call": "{call_name}"}}',
                            })

                # Mutation edges (this.x = ... / self.x = ...)
                for assign_type in node_types.get("assignment", []):
                    for assign_node in _find_children_by_type(func_node, assign_type):
                        text = _node_text(assign_node, source)
                        if "this." in text or "self." in text:
                            # Extract field name
                            parts = text.split("=")[0].strip()
                            if "this." in parts:
                                field = parts.split("this.")[-1].strip()
                            elif "self." in parts:
                                field = parts.split("self.")[-1].strip()
                            else:
                                continue

                            # Find enclosing class
                            p = func_node.parent
                            while p:
                                if p.type in node_types.get("class", []):
                                    cls_name = _get_name(p, source)
                                    if cls_name:
                                        result.edges.append({
                                            "src": func_id,
                                            "dst": f"{module}.{cls_name}.{field}",
                                            "kind": "mutates",
                                            "confidence": 1.0,
                                        })
                                    break
                                p = p.parent

                # Assertion edges (test files)
                if is_test:
                    assert_calls = _TEST_PATTERNS.get(self._lang_name, {}).get("assert_calls", [])
                    for call_type in node_types.get("call", []):
                        for call_node in _find_children_by_type(func_node, call_type):
                            call_name = _get_call_name(call_node, source)
                            if not call_name:
                                continue
                            # Check if this is an assertion call
                            simple_name = call_name.split(".")[-1] if "." in call_name else call_name
                            if simple_name in assert_calls:
                                # Extract the first argument as the symbol being tested
                                call_text = _node_text(call_node, source)
                                # Try to find what's being asserted on from arguments
                                for arg_name, arg_id in scope_map.items():
                                    if arg_name in call_text:
                                        result.edges.append({
                                            "src": func_id,
                                            "dst": arg_id,
                                            "kind": "asserts_on",
                                            "confidence": 0.8,
                                        })

        # Import edges
        for import_type in node_types.get("import", []):
            for imp_node in _find_children_by_type(root, import_type):
                imp_text = _node_text(imp_node, source)
                # Extract imported module/symbol names
                imported = self._parse_import(imp_text)
                for imp_name in imported:
                    result.edges.append({
                        "src": module, "dst": imp_name,
                        "kind": "imports", "confidence": 1.0,
                    })

    def _extract_inheritance(
        self, class_node: Node, source: bytes, module: str,
        class_name: str, result: ParseResult,
    ) -> None:
        """Extract inheritance edges from class definitions."""
        text = _node_text(class_node, source)

        if self._lang_name in ("javascript", "typescript"):
            if "extends" in text:
                parts = text.split("extends")
                if len(parts) > 1:
                    parent = parts[1].split("{")[0].split("implements")[0].strip()
                    if parent:
                        result.edges.append({
                            "src": f"{module}.{class_name}",
                            "dst": parent,
                            "kind": "inherits",
                            "confidence": 0.8,
                        })
        elif self._lang_name == "java":
            if "extends" in text:
                parts = text.split("extends")
                if len(parts) > 1:
                    parent = parts[1].split("{")[0].split("implements")[0].strip()
                    if parent:
                        result.edges.append({
                            "src": f"{module}.{class_name}",
                            "dst": parent,
                            "kind": "inherits",
                            "confidence": 0.8,
                        })
            if "implements" in text:
                parts = text.split("implements")
                if len(parts) > 1:
                    interfaces = parts[1].split("{")[0].strip()
                    for iface in interfaces.split(","):
                        iface = iface.strip()
                        if iface:
                            result.edges.append({
                                "src": f"{module}.{class_name}",
                                "dst": iface,
                                "kind": "inherits",
                                "confidence": 0.8,
                            })

    def _extract_docstring(self, node: Node, source: bytes) -> str | None:
        """Extract a docstring/comment from a node."""
        # Check for preceding comment
        prev = node.prev_named_sibling
        if prev and prev.type in ("comment", "block_comment", "line_comment"):
            text = _node_text(prev, source)
            return text.strip("/* \n/")

        # Check for JSDoc or leading comment inside body
        for child in node.children:
            if child.type in ("comment", "block_comment"):
                text = _node_text(child, source)
                return text.strip("/* \n/")
        return None

    def _match_side_effect(self, call_name: str) -> str | None:
        """Match a call against known side-effect patterns."""
        patterns = _SIDE_EFFECTS.get(self._lang_name, {})
        if call_name in patterns:
            return patterns[call_name]
        for prefix, kind in patterns.items():
            if prefix.endswith(".") and call_name.startswith(prefix):
                return kind
            if call_name.startswith(prefix):
                return kind
        return None

    def _parse_import(self, text: str) -> list[str]:
        """Parse an import statement and return imported names."""
        # Strip common prefixes
        text = text.strip().rstrip(";")
        names: list[str] = []

        if self._lang_name in ("javascript", "typescript"):
            # import X from 'Y' or import { X } from 'Y' or require('Y')
            if "from" in text:
                parts = text.split("from")
                module_part = parts[-1].strip().strip("'\"` ;")
                names.append(module_part)
            elif "require" in text:
                start = text.find("(")
                end = text.find(")")
                if start != -1 and end != -1:
                    names.append(text[start+1:end].strip("'\"` "))
        elif self._lang_name == "go":
            # import "pkg" or import ( "pkg" )
            text = text.replace("import", "").strip().strip("()")
            for line in text.split("\n"):
                line = line.strip().strip('"')
                if line:
                    names.append(line)
        elif self._lang_name == "rust":
            # use std::collections::HashMap;
            text = text.replace("use ", "").strip().rstrip(";")
            names.append(text)
        elif self._lang_name == "java":
            # import com.example.Foo;
            text = text.replace("import ", "").replace("static ", "").strip().rstrip(";")
            names.append(text)

        return names
