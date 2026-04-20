"""Indexing pipeline: file discovery, change detection, and orchestration."""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from causal_graph_mcp.language import get_parser, get_source_extensions
from causal_graph_mcp.parser import parse_file
from causal_graph_mcp.resolver import resolve_calls
from causal_graph_mcp.storage import Storage

# Directories to always skip during file discovery
_SKIP_DIRS = frozenset({
    "__pycache__", ".venv", "venv", "env", "node_modules",
    ".git", ".causal-graph", ".tox", ".mypy_cache", ".pytest_cache",
})


@dataclass
class IndexResult:
    """Result of an indexing operation."""

    nodes_indexed: int = 0
    edges_indexed: int = 0
    files_parsed: int = 0
    files_skipped: int = 0
    duration_ms: int = 0


def index_project(project_root: str, storage: Storage) -> IndexResult:
    """Index an entire project: discover files, detect changes, run pipeline.

    Args:
        project_root: Absolute path to the project root.
        storage: Storage instance for the project.

    Returns:
        IndexResult with counts and duration.
    """
    start = time.monotonic()

    file_paths = _discover_files(project_root)
    changed_files, skipped = _detect_changed_files(file_paths, storage)

    result = IndexResult(files_skipped=skipped)

    for file_path in changed_files:
        nodes, edges = _index_file(file_path, project_root, storage)
        result.nodes_indexed += nodes
        result.edges_indexed += edges
        result.files_parsed += 1

    result.duration_ms = int((time.monotonic() - start) * 1000)
    return result


def index_files(
    file_paths: list[str],
    project_root: str,
    storage: Storage,
) -> IndexResult:
    """Index specific files (for incremental re-indexing by file watcher).

    Skips discovery and change detection — indexes all provided files.

    Args:
        file_paths: List of absolute file paths to index.
        project_root: Absolute path to the project root.
        storage: Storage instance for the project.

    Returns:
        IndexResult with counts and duration.
    """
    start = time.monotonic()
    result = IndexResult()

    valid_exts = get_source_extensions() | {".py"}
    for file_path in file_paths:
        if Path(file_path).suffix.lower() not in valid_exts:
            continue
        if not Path(file_path).is_file():
            continue
        nodes, edges = _index_file(file_path, project_root, storage)
        result.nodes_indexed += nodes
        result.edges_indexed += edges
        result.files_parsed += 1

    result.duration_ms = int((time.monotonic() - start) * 1000)
    return result


def _discover_files(project_root: str) -> list[str]:
    """Walk the project directory and collect all .py files.

    Skips excluded directories and respects .gitignore patterns.
    """
    root = Path(project_root)
    gitignore_patterns = _parse_gitignore(project_root)
    source_files: list[str] = []
    # Collect all known source extensions (registered parsers + known types)
    valid_exts = get_source_extensions() | {".py"}

    for dirpath, dirnames, filenames in os.walk(root):
        # Remove excluded directories in-place to prevent os.walk from descending
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS
            and not _matches_gitignore(
                os.path.relpath(os.path.join(dirpath, d), root),
                gitignore_patterns,
            )
        ]

        for filename in filenames:
            ext = Path(filename).suffix.lower()
            if ext not in valid_exts:
                continue
            full_path = os.path.join(dirpath, filename)
            rel_path = os.path.relpath(full_path, root)
            if not _matches_gitignore(rel_path, gitignore_patterns):
                source_files.append(full_path)

    return source_files


def _parse_gitignore(project_root: str) -> list[str]:
    """Parse .gitignore and return a list of patterns."""
    gitignore_path = Path(project_root) / ".gitignore"
    if not gitignore_path.is_file():
        return []

    patterns: list[str] = []
    for line in gitignore_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def _matches_gitignore(rel_path: str, patterns: list[str]) -> bool:
    """Check if a relative path matches any gitignore pattern."""
    # Normalize to forward slashes for matching
    rel_path = rel_path.replace(os.sep, "/")

    for pattern in patterns:
        # Strip trailing slash for directory patterns
        clean_pattern = pattern.rstrip("/")

        # Check if any path component matches
        if fnmatch(rel_path, clean_pattern):
            return True
        if fnmatch(rel_path, f"{clean_pattern}/*"):
            return True
        # Check basename match
        basename = os.path.basename(rel_path)
        if fnmatch(basename, clean_pattern):
            return True
        # Check if any parent directory matches
        parts = rel_path.split("/")
        for part in parts:
            if fnmatch(part, clean_pattern):
                return True

    return False


def _compute_file_hash(file_path: str) -> str:
    """Compute SHA-256 hash of a file's contents."""
    content = Path(file_path).read_bytes()
    return hashlib.sha256(content).hexdigest()


def _detect_changed_files(
    file_paths: list[str],
    storage: Storage,
) -> tuple[list[str], int]:
    """Compare file hashes against storage to find changed files.

    Returns:
        Tuple of (changed_file_paths, skipped_count).
    """
    changed: list[str] = []
    skipped = 0

    for file_path in file_paths:
        current_hash = _compute_file_hash(file_path)
        stored_hash = storage.get_file_hash(file_path)

        if stored_hash == current_hash:
            skipped += 1
        else:
            changed.append(file_path)

    return changed, skipped


def _derive_module_name(file_path: str, project_root: str) -> str:
    """Derive a dotted module name from a file path relative to the project root.

    Examples:
        "src/auth/utils.py" → "src.auth.utils"
        "src/auth/__init__.py" → "src.auth"
        "main.py" → "main"
        "src/Foo.java" → "src.Foo"
    """
    rel_path = os.path.relpath(file_path, project_root)
    # Normalize separators
    rel_path = rel_path.replace(os.sep, "/")

    # Handle __init__.py — use the package name
    if rel_path.endswith("/__init__.py"):
        module = rel_path[: -len("/__init__.py")]
    elif rel_path == "__init__.py":
        module = ""
    else:
        # Strip the file extension regardless of length (.py, .java, .tsx, etc.)
        module = rel_path.rsplit(".", 1)[0] if "." in Path(rel_path).name else rel_path

    return module.replace("/", ".")


def _index_file(
    file_path: str,
    project_root: str,
    storage: Storage,
) -> tuple[int, int]:
    """Run the full indexing pipeline for a single file.

    Pipeline: derive module → parse → resolve → store.

    Returns:
        Tuple of (node_count, edge_count).
    """
    module_name = _derive_module_name(file_path, project_root)

    # Use language-specific parser if available, fall back to Python parser
    parser = get_parser(file_path)
    if parser:
        parse_result = parser.parse(file_path, module_name)
    else:
        # Fall back to Python parser for .py files (always available)
        parse_result = parse_file(file_path, module_name)

    # Resolve call edges (jedi only works for Python, but the function is safe for others)
    if Path(file_path).suffix.lower() == ".py":
        resolved_edges = resolve_calls(parse_result.edges, project_root)
    else:
        resolved_edges = parse_result.edges

    # Compute hash
    file_hash = _compute_file_hash(file_path)

    # Store atomically
    storage.update_file_scope(
        file_path,
        parse_result.nodes,
        resolved_edges,
        file_hash,
    )

    return len(parse_result.nodes), len(resolved_edges)
