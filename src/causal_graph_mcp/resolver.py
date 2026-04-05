"""Jedi-based call resolution for upgrading low-confidence call edges."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

try:
    import jedi
except ImportError:
    jedi = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def resolve_calls(
    edges: list[dict[str, Any]],
    project_root: str,
) -> list[dict[str, Any]]:
    """Resolve low-confidence call edges using jedi type inference.

    Takes a list of edge dicts (from parser), attempts jedi resolution on
    call edges with confidence < 1.0, and returns the full list with
    upgraded confidences where resolved.

    Args:
        edges: List of edge dicts from the parser.
        project_root: Absolute path to the project root.

    Returns:
        The edge list with upgraded confidences where jedi resolved.
    """
    if not edges:
        return edges

    if jedi is None:
        logger.warning("jedi not installed — skipping call resolution")
        return edges

    scripts_cache: dict[str, Any] = {}
    result: list[dict[str, Any]] = []

    for edge in edges:
        if edge.get("kind") == "calls" and edge.get("confidence", 1.0) < 1.0:
            resolved = _resolve_edge(edge, scripts_cache, project_root)
            result.append(resolved)
        else:
            result.append(edge)

    return result


def _resolve_edge(
    edge: dict[str, Any],
    scripts_cache: dict[str, Any],
    project_root: str,
) -> dict[str, Any]:
    """Attempt to resolve a single call edge using jedi.

    Only upgrades confidence — never downgrades. Returns edge unchanged
    if resolution fails.
    """
    try:
        src_id = edge.get("src", "")
        dst_name = edge.get("dst", "")

        # We need to find the source file and location for jedi
        # The src_id is like "module.class.method" or "module.function"
        # We need to find the actual file
        source_file = _find_source_file(src_id, project_root)
        if not source_file:
            return edge

        script = _get_script(source_file, scripts_cache, project_root)
        if script is None:
            return edge

        # Try to resolve the destination using jedi goto
        resolved_name = _jedi_resolve(script, dst_name, source_file)
        if resolved_name and resolved_name != dst_name:
            # Successfully resolved — upgrade to 0.5
            resolved_edge = dict(edge)
            resolved_edge["dst"] = resolved_name
            resolved_edge["confidence"] = 0.5
            return resolved_edge

        return edge

    except Exception as exc:
        logger.warning("jedi resolution failed for %s -> %s: %s", edge.get("src"), edge.get("dst"), exc)
        return edge


def _find_source_file(symbol_id: str, project_root: str) -> str | None:
    """Find the source file for a symbol based on its module path."""
    root = Path(project_root)
    # Extract module from symbol_id (first part before the symbol name)
    parts = symbol_id.split(".")
    if not parts:
        return None

    # Try progressively shorter module paths
    for i in range(len(parts), 0, -1):
        module_path = parts[:i]
        # Try as a file
        candidate = root / "/".join(module_path[:-1]) / f"{module_path[-1]}.py" if len(module_path) > 1 else root / f"{module_path[0]}.py"
        if candidate.is_file():
            return str(candidate)
        # Try as package
        candidate = root / "/".join(module_path) / "__init__.py"
        if candidate.is_file():
            return str(candidate)

    return None


def _get_script(
    file_path: str,
    cache: dict[str, Any],
    project_root: str,
) -> Any | None:
    """Get or create a cached jedi.Script for a file."""
    if file_path in cache:
        return cache[file_path]

    try:
        source = Path(file_path).read_text(encoding="utf-8")
        script = jedi.Script(source, path=file_path, project=jedi.Project(path=project_root))
        cache[file_path] = script
        return script
    except Exception as exc:
        logger.warning("Failed to create jedi.Script for %s: %s", file_path, exc)
        cache[file_path] = None
        return None


def _jedi_resolve(
    script: Any,
    name: str,
    source_file: str,
) -> str | None:
    """Use jedi to resolve a name to its fully qualified definition.

    Searches the source file for occurrences of the name and tries
    jedi.Script.goto() to find the definition.
    """
    try:
        source = Path(source_file).read_text(encoding="utf-8")
        lines = source.splitlines()

        # Find the name in the source
        simple_name = name.split(".")[-1]

        for line_no, line in enumerate(lines, 1):
            col = line.find(simple_name)
            if col == -1:
                continue

            try:
                definitions = script.goto(line_no, col)
                if definitions:
                    defn = definitions[0]
                    full_name = defn.full_name
                    if full_name:
                        return full_name
            except Exception:
                continue

        return None

    except Exception as exc:
        logger.warning("jedi goto failed for %s in %s: %s", name, source_file, exc)
        return None
