"""AST-based extraction of nodes and edges from Python source files."""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ParseResult:
    """Result of parsing a single Python file."""

    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)


def parse_file(file_path: str, module_name: str) -> ParseResult:
    """Parse a Python file and extract nodes and edges.

    Args:
        file_path: Absolute path to the Python source file.
        module_name: Dotted module name (e.g. "auth.utils").

    Returns:
        ParseResult with extracted nodes and edges.
    """
    source = Path(file_path).read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError:
        return ParseResult()

    is_test = _is_test_file(file_path)
    scope_map = _build_scope_map(tree, module_name)
    imports_map = _build_imports_map(tree)

    result = ParseResult()

    # Extract module-level nodes and edges
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _extract_function(node, module_name, None, file_path, is_test, source, scope_map, imports_map, result)
        elif isinstance(node, ast.ClassDef):
            _extract_class(node, module_name, file_path, is_test, source, scope_map, imports_map, result)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            _extract_module_variable(node, module_name, file_path, source, result)

    # Extract import edges
    _extract_import_edges(tree, module_name, result)

    return result


def _is_test_file(file_path: str) -> bool:
    """Check if a file is a test file based on its name."""
    name = Path(file_path).stem
    return name.startswith("test_") or name.endswith("_test")


def _compute_body_hash(node: ast.AST) -> str:
    """Compute SHA-256 hash of an AST node's dump."""
    dump = ast.dump(node)
    return hashlib.sha256(dump.encode()).hexdigest()


def _get_call_string(node: ast.Call) -> str:
    """Extract the full callee expression from an ast.Call node."""
    return _get_attr_string(node.func)


def _get_attr_string(node: ast.expr) -> str:
    """Recursively extract a dotted name from an AST expression."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        value = _get_attr_string(node.value)
        if value:
            return f"{value}.{node.attr}"
        return node.attr
    return ""


def _build_scope_map(tree: ast.Module, module: str) -> dict[str, str]:
    """Build a mapping from local names to qualified IDs for same-file resolution."""
    scope: dict[str, str] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope[node.name] = f"{module}.{node.name}"
        elif isinstance(node, ast.ClassDef):
            scope[node.name] = f"{module}.{node.name}"
            # Also add methods
            for item in ast.iter_child_nodes(node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    scope[f"{node.name}.{item.name}"] = f"{module}.{node.name}.{item.name}"
    return scope


def _build_imports_map(tree: ast.Module) -> dict[str, str]:
    """Build a mapping from imported local names to their full import paths."""
    imports: dict[str, str] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname if alias.asname else alias.name
                imports[local_name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                local_name = alias.asname if alias.asname else alias.name
                imports[local_name] = f"{mod}.{alias.name}" if mod else alias.name
    return imports


def _reconstruct_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Reconstruct a function signature string from its AST node."""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    args_parts: list[str] = []

    all_args = node.args

    # Positional args
    num_args = len(all_args.args)
    num_defaults = len(all_args.defaults)
    for i, arg in enumerate(all_args.args):
        part = arg.arg
        if arg.annotation:
            part += f": {ast.unparse(arg.annotation)}"
        # Check if there's a default
        default_idx = i - (num_args - num_defaults)
        if default_idx >= 0:
            part += f" = {ast.unparse(all_args.defaults[default_idx])}"
        args_parts.append(part)

    # *args
    if all_args.vararg:
        part = f"*{all_args.vararg.arg}"
        if all_args.vararg.annotation:
            part += f": {ast.unparse(all_args.vararg.annotation)}"
        args_parts.append(part)

    # **kwargs
    if all_args.kwarg:
        part = f"**{all_args.kwarg.arg}"
        if all_args.kwarg.annotation:
            part += f": {ast.unparse(all_args.kwarg.annotation)}"
        args_parts.append(part)

    args_str = ", ".join(args_parts)
    sig = f"{prefix} {node.name}({args_str})"
    if node.returns:
        sig += f" -> {ast.unparse(node.returns)}"
    return sig


def _extract_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    module: str,
    class_name: str | None,
    file_path: str,
    is_test: bool,
    source: str,
    scope_map: dict[str, str],
    imports_map: dict[str, str],
    result: ParseResult,
) -> None:
    """Extract a function/method node and its edges."""
    if class_name:
        node_id = f"{module}.{class_name}.{node.name}"
        kind = "method"
    else:
        node_id = f"{module}.{node.name}"
        kind = "function"

    result.nodes.append({
        "id": node_id,
        "kind": kind,
        "module": module,
        "file": file_path,
        "line_start": node.lineno,
        "line_end": node.end_lineno or node.lineno,
        "signature": _reconstruct_signature(node),
        "docstring": ast.get_docstring(node),
        "is_public": 0 if node.name.startswith("_") else 1,
        "is_test": 1 if is_test else 0,
        "body_hash": _compute_body_hash(node),
    })

    # Extract edges from function body
    _extract_call_edges(node, node_id, module, scope_map, imports_map, result)
    _extract_mutation_edges(node, node_id, module, class_name, result)
    if is_test:
        _extract_assertion_edges(node, node_id, scope_map, imports_map, result)
    _extract_side_effect_edges(node, node_id, result)


def _extract_class(
    node: ast.ClassDef,
    module: str,
    file_path: str,
    is_test: bool,
    source: str,
    scope_map: dict[str, str],
    imports_map: dict[str, str],
    result: ParseResult,
) -> None:
    """Extract a class node, its methods, and inheritance/override edges."""
    class_id = f"{module}.{node.name}"

    result.nodes.append({
        "id": class_id,
        "kind": "class",
        "module": module,
        "file": file_path,
        "line_start": node.lineno,
        "line_end": node.end_lineno or node.lineno,
        "signature": f"class {node.name}",
        "docstring": ast.get_docstring(node),
        "is_public": 0 if node.name.startswith("_") else 1,
        "is_test": 1 if is_test else 0,
        "body_hash": _compute_body_hash(node),
    })

    # Inheritance edges
    for base in node.bases:
        base_name = _get_attr_string(base)
        if base_name:
            base_id = scope_map.get(base_name, imports_map.get(base_name, base_name))
            result.edges.append({
                "src": class_id,
                "dst": base_id,
                "kind": "inherits",
                "confidence": 1.0 if base_name in scope_map else 0.8,
            })

    # Collect parent method names for override detection
    parent_methods: set[str] = set()
    for base in node.bases:
        base_name = _get_attr_string(base)
        if base_name in scope_map:
            # Same-file parent class — collect its methods
            parent_methods.update(_get_class_method_names(node, module, base_name, scope_map))

    # Extract methods
    for item in ast.iter_child_nodes(node):
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _extract_function(item, module, node.name, file_path, is_test, source, scope_map, imports_map, result)

            # Override edges
            if item.name in parent_methods:
                for base in node.bases:
                    base_name = _get_attr_string(base)
                    parent_method_id = f"{module}.{base_name}.{item.name}"
                    if parent_method_id in scope_map.values() or f"{base_name}.{item.name}" in scope_map:
                        result.edges.append({
                            "src": f"{module}.{node.name}.{item.name}",
                            "dst": parent_method_id,
                            "kind": "overrides",
                            "confidence": 1.0,
                        })


def _get_class_method_names(
    current_class_node: ast.ClassDef,
    module: str,
    parent_class_name: str,
    scope_map: dict[str, str],
) -> set[str]:
    """Get method names of a parent class defined in the same file.

    We need to find the parent class in the same module. Since we have the scope_map,
    we look for entries like "{parent_class_name}.{method}" in the scope_map keys.
    """
    methods: set[str] = set()
    for key in scope_map:
        if key.startswith(f"{parent_class_name}."):
            method_name = key[len(parent_class_name) + 1:]
            methods.add(method_name)
    return methods


def _extract_module_variable(
    node: ast.Assign | ast.AnnAssign,
    module: str,
    file_path: str,
    source: str,
    result: ParseResult,
) -> None:
    """Extract module-level variable assignments as nodes."""
    if isinstance(node, ast.AnnAssign):
        if isinstance(node.target, ast.Name):
            result.nodes.append({
                "id": f"{module}.{node.target.id}",
                "kind": "variable",
                "module": module,
                "file": file_path,
                "line_start": node.lineno,
                "line_end": node.end_lineno or node.lineno,
                "signature": None,
                "docstring": None,
                "is_public": 0 if node.target.id.startswith("_") else 1,
                "is_test": 0,
                "body_hash": _compute_body_hash(node),
            })
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                result.nodes.append({
                    "id": f"{module}.{target.id}",
                    "kind": "variable",
                    "module": module,
                    "file": file_path,
                    "line_start": node.lineno,
                    "line_end": node.end_lineno or node.lineno,
                    "signature": None,
                    "docstring": None,
                    "is_public": 0 if target.id.startswith("_") else 1,
                    "is_test": 0,
                    "body_hash": _compute_body_hash(node),
                })


def _extract_call_edges(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    func_id: str,
    module: str,
    scope_map: dict[str, str],
    imports_map: dict[str, str],
    result: ParseResult,
) -> None:
    """Extract call edges from a function body."""
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            call_str = _get_call_string(node)
            if not call_str:
                continue

            # Resolve the callee
            resolved_id, confidence = _resolve_call(call_str, scope_map, imports_map, module)

            result.edges.append({
                "src": func_id,
                "dst": resolved_id,
                "kind": "calls",
                "confidence": confidence,
            })


def _resolve_call(
    call_str: str,
    scope_map: dict[str, str],
    imports_map: dict[str, str],
    module: str,
) -> tuple[str, float]:
    """Resolve a call string to a qualified ID with confidence score."""
    # Same-file resolution
    if call_str in scope_map:
        return scope_map[call_str], 1.0

    # Check for method calls like ClassName.method
    parts = call_str.split(".")
    if len(parts) == 2:
        cls, method = parts
        key = f"{cls}.{method}"
        if key in scope_map:
            return scope_map[key], 1.0

    # Import resolution
    root = parts[0]
    if root in imports_map:
        import_path = imports_map[root]
        if len(parts) > 1:
            resolved = f"{import_path}.{'.'.join(parts[1:])}"
        else:
            resolved = import_path
        return resolved, 0.8

    # Unresolved — best guess
    return call_str, 0.3


def _extract_mutation_edges(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    func_id: str,
    module: str,
    class_name: str | None,
    result: ParseResult,
) -> None:
    """Extract mutation edges from assignments in function bodies."""
    for node in ast.walk(func_node):
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                    if target.value.id == "self" and class_name:
                        # self.x = ... → mutates {class}.{attr}
                        result.edges.append({
                            "src": func_id,
                            "dst": f"{module}.{class_name}.{target.attr}",
                            "kind": "mutates",
                            "confidence": 1.0,
                        })
                elif isinstance(target, ast.Name):
                    # Could be a module-level variable reassignment
                    # We emit this as a mutation if the variable exists at module scope
                    result.edges.append({
                        "src": func_id,
                        "dst": f"{module}.{target.id}",
                        "kind": "mutates",
                        "confidence": 0.8,
                    })
        elif isinstance(node, ast.AnnAssign) and node.target:
            if isinstance(node.target, ast.Attribute) and isinstance(node.target.value, ast.Name):
                if node.target.value.id == "self" and class_name:
                    result.edges.append({
                        "src": func_id,
                        "dst": f"{module}.{class_name}.{node.target.attr}",
                        "kind": "mutates",
                        "confidence": 1.0,
                    })


# Known assertion method names
_ASSERT_METHODS = frozenset({
    "assertEqual", "assertNotEqual", "assertTrue", "assertFalse",
    "assertIs", "assertIsNot", "assertIsNone", "assertIsNotNone",
    "assertIn", "assertNotIn", "assertIsInstance", "assertNotIsInstance",
    "assertRaises", "assertWarns", "assertAlmostEqual", "assertNotAlmostEqual",
    "assertGreater", "assertGreaterEqual", "assertLess", "assertLessEqual",
    "assertRegex", "assertNotRegex", "assertCountEqual",
    "assert_called_with", "assert_called_once_with",
})


def _extract_assertion_edges(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    func_id: str,
    scope_map: dict[str, str],
    imports_map: dict[str, str],
    result: ParseResult,
) -> None:
    """Extract assertion edges from test function bodies."""
    for node in ast.walk(func_node):
        if isinstance(node, ast.Assert):
            # Extract symbols from the assert expression
            symbols = _extract_symbols_from_expr(node.test)
            for sym in symbols:
                resolved, _ = _resolve_call(sym, scope_map, imports_map, "")
                result.edges.append({
                    "src": func_id,
                    "dst": resolved,
                    "kind": "asserts_on",
                    "confidence": 1.0,
                })
        elif isinstance(node, ast.Call):
            call_str = _get_call_string(node)
            # Check for assert* method calls
            method_name = call_str.split(".")[-1] if "." in call_str else call_str
            if method_name in _ASSERT_METHODS or method_name.startswith("assert_"):
                # The first argument is typically the thing being asserted on
                if node.args:
                    symbols = _extract_symbols_from_expr(node.args[0])
                    for sym in symbols:
                        resolved, _ = _resolve_call(sym, scope_map, imports_map, "")
                        result.edges.append({
                            "src": func_id,
                            "dst": resolved,
                            "kind": "asserts_on",
                            "confidence": 1.0,
                        })


def _extract_symbols_from_expr(expr: ast.expr) -> list[str]:
    """Extract symbol references from an expression (for assertions)."""
    symbols: list[str] = []
    if isinstance(expr, ast.Call):
        call_str = _get_call_string(expr)
        if call_str:
            symbols.append(call_str)
    elif isinstance(expr, ast.Name):
        symbols.append(expr.id)
    elif isinstance(expr, ast.Attribute):
        attr_str = _get_attr_string(expr)
        if attr_str:
            symbols.append(attr_str)
    elif isinstance(expr, ast.Compare):
        symbols.extend(_extract_symbols_from_expr(expr.left))
        for comparator in expr.comparators:
            symbols.extend(_extract_symbols_from_expr(comparator))
    elif isinstance(expr, ast.BoolOp):
        for value in expr.values:
            symbols.extend(_extract_symbols_from_expr(value))
    elif isinstance(expr, ast.UnaryOp):
        symbols.extend(_extract_symbols_from_expr(expr.operand))
    return symbols


# Side-effect pattern matching
_SIDE_EFFECT_PATTERNS: dict[str, str] = {
    "open": "file_io",
    "os.": "file_io",
    "pathlib.": "file_io",
    "requests.": "network",
    "httpx.": "network",
    "urllib.": "network",
    "aiohttp.": "network",
    "redis.": "cache",
    "memcache.": "cache",
    "subprocess.": "process",
    "os.system": "process",
}


def _extract_side_effect_edges(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    func_id: str,
    result: ParseResult,
) -> None:
    """Extract side-effect edges from known I/O patterns."""
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            call_str = _get_call_string(node)
            if not call_str:
                continue

            effect_kind = _match_side_effect(call_str)
            if effect_kind:
                result.edges.append({
                    "src": func_id,
                    "dst": f"side_effect:{effect_kind}:{call_str}",
                    "kind": "side_effect",
                    "confidence": 1.0,
                    "detail": f'{{"kind": "{effect_kind}", "call": "{call_str}"}}',
                })


def _match_side_effect(call_str: str) -> str | None:
    """Match a call string against known side-effect patterns."""
    # Check exact match first (e.g., "open")
    if call_str in _SIDE_EFFECT_PATTERNS:
        return _SIDE_EFFECT_PATTERNS[call_str]

    # Check prefix match (e.g., "requests." matches "requests.get")
    for prefix, kind in _SIDE_EFFECT_PATTERNS.items():
        if prefix.endswith(".") and call_str.startswith(prefix):
            return kind

    # Special case: os.system is both "os." (file_io) and "os.system" (process)
    # The exact match for "os.system" should take priority
    return None


def _extract_import_edges(
    tree: ast.Module,
    module: str,
    result: ParseResult,
) -> None:
    """Extract import edges from module-level import statements."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                result.edges.append({
                    "src": module,
                    "dst": alias.name,
                    "kind": "imports",
                    "confidence": 1.0,
                })
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                dst = f"{mod}.{alias.name}" if mod else alias.name
                result.edges.append({
                    "src": module,
                    "dst": dst,
                    "kind": "imports",
                    "confidence": 1.0,
                })
