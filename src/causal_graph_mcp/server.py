"""MCP server entry point with all 7 tool handlers."""

import json
import logging
import os
from pathlib import Path
from typing import Any

from mcp.server import FastMCP

from causal_graph_mcp.graph import get_subgraph
from causal_graph_mcp.indexer import IndexResult, index_files, index_project
from causal_graph_mcp.risk import compute_impact
from causal_graph_mcp.storage import Storage
from causal_graph_mcp.watcher import FileWatcher

logger = logging.getLogger(__name__)

# Token budget: ~8000 tokens ≈ 32000 chars
_MAX_RESPONSE_CHARS = 32000

_server_storage: "Storage | None" = None
_server_project_root: str = ""
_server_watcher: "FileWatcher | None" = None


def _get_storage() -> Storage:
    global _server_storage, _server_project_root, _server_watcher
    if _server_storage is None:
        _server_project_root = os.getcwd()
        _server_storage = Storage(Path(_server_project_root))
        # Auto-index on startup
        result = index_project(_server_project_root, _server_storage)
        logger.info(
            "Auto-indexed: %d nodes, %d edges, %d files in %dms",
            result.nodes_indexed,
            result.edges_indexed,
            result.files_parsed,
            result.duration_ms,
        )
        # Start file watcher for incremental re-indexing
        storage_ref = _server_storage
        root_ref = _server_project_root

        def _on_change(files: list[str]) -> None:
            index_files(files, root_ref, storage_ref)

        _server_watcher = FileWatcher(_server_project_root, _on_change)
        _server_watcher.start()
    return _server_storage


def _truncate(data: dict) -> dict:
    """Cap response size at ~8000 tokens. Add truncated flag if exceeded."""
    serialized = json.dumps(data)
    if len(serialized) <= _MAX_RESPONSE_CHARS:
        return data
    data["truncated"] = True
    # Truncate list fields to fit
    for key, value in data.items():
        if isinstance(value, list) and len(value) > 5:
            data[key] = value[:5]
            serialized = json.dumps(data)
            if len(serialized) <= _MAX_RESPONSE_CHARS:
                return data
    return data


def create_server() -> FastMCP:
    """Create and configure the MCP server with all 7 tools."""
    server = FastMCP(name="causal-graph-mcp")

    @server.tool()
    def index_project_tool(project_root: str = "") -> str:
        """Index (or re-index) a Python project. Builds the full causal graph. Safe to call multiple times — incremental on subsequent calls."""
        storage = _get_storage()
        root = project_root or _server_project_root
        result = index_project(root, storage)
        return json.dumps(_truncate({
            "nodes_indexed": result.nodes_indexed,
            "edges_indexed": result.edges_indexed,
            "files_parsed": result.files_parsed,
            "files_skipped": result.files_skipped,
            "duration_ms": result.duration_ms,
        }))

    @server.tool()
    def get_call_graph(
        symbol: str,
        direction: str = "both",
        max_hops: int = 3,
        min_confidence: float = 0.5,
    ) -> str:
        """Trace callers and callees of a function, recursively up to N hops. Returns the subgraph with confidence scores on each edge."""
        storage = _get_storage()
        result = get_subgraph(storage, symbol, direction, max_hops, min_confidence)
        return json.dumps(_truncate({
            "root": result.root,
            "nodes": [
                {"id": n["id"], "kind": n["kind"], "file": n.get("file", ""), "line_start": n.get("line_start", 0), "hops": n.get("hops", 0)}
                for n in result.nodes
            ],
            "edges": [
                {"src": e["src"], "dst": e["dst"], "kind": e["kind"], "confidence": e.get("confidence", 1.0), "hops": e.get("hops", 0)}
                for e in result.edges
            ],
            "cycles_detected": result.cycles_detected,
        }))

    @server.tool()
    def impact_analysis(symbol: str, max_hops: int = 4) -> str:
        """Given a symbol that is about to change, return all downstream symbols ranked by breakage risk. Uses mutation, assertion, and side-effect edges for accurate risk scoring."""
        storage = _get_storage()
        result = compute_impact(storage, symbol, max_hops)
        return json.dumps(_truncate({
            "changed_symbol": result.changed_symbol,
            "at_risk": [
                {
                    "symbol": entry["symbol"],
                    "distance": entry["distance"],
                    "risk_score": entry["risk_score"],
                    "risk_factors": entry["risk_factors"],
                    "path": entry["path"],
                }
                for entry in result.at_risk
            ],
            "summary": result.summary,
        }))

    @server.tool()
    def semantic_search(
        query: str,
        kinds: str = "",
        limit: int = 10,
    ) -> str:
        """Search for symbols by name or concept using BM25 full-text search on names, signatures, and docstrings. Pass kinds as comma-separated string (e.g. 'function,method')."""
        storage = _get_storage()
        kind_list = [k.strip() for k in kinds.split(",") if k.strip()] if kinds else None
        results = storage.search(query, kinds=kind_list, limit=limit)
        return json.dumps(_truncate({
            "results": [
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "signature": r.get("signature"),
                    "docstring": r.get("docstring"),
                    "file": r.get("file", ""),
                    "line_start": r.get("line_start", 0),
                    "score": r.get("rank", 0),
                }
                for r in results
            ],
        }))

    @server.tool()
    def get_symbol(symbol_id: str) -> str:
        """Fetch full details for a specific symbol by ID, including source code and all direct edges."""
        storage = _get_storage()
        node = storage.get_node(symbol_id)
        if not node:
            return json.dumps({"error": f"Symbol not found: {symbol_id}"})

        # Read source from file
        source = ""
        try:
            file_path = node.get("file", "")
            if file_path and Path(file_path).is_file():
                lines = Path(file_path).read_text(encoding="utf-8").splitlines()
                start = node.get("line_start", 1) - 1
                end = node.get("line_end", start + 1)
                source = "\n".join(lines[start:end])
        except Exception:
            source = ""

        edges_out = storage.get_edges(symbol_id, direction="out")
        edges_in = storage.get_edges(symbol_id, direction="in")

        return json.dumps(_truncate({
            "id": node["id"],
            "kind": node["kind"],
            "file": node.get("file", ""),
            "line_start": node.get("line_start", 0),
            "line_end": node.get("line_end", 0),
            "signature": node.get("signature"),
            "docstring": node.get("docstring"),
            "source": source,
            "edges_out": [
                {"dst": e["dst"], "kind": e["kind"], "confidence": e.get("confidence", 1.0)}
                for e in edges_out
            ],
            "edges_in": [
                {"src": e["src"], "kind": e["kind"], "confidence": e.get("confidence", 1.0)}
                for e in edges_in
            ],
        }))

    @server.tool()
    def project_map(project_root: str = "") -> str:
        """High-level overview: modules, entry points, hot symbols, test coverage, and graph stats."""
        storage = _get_storage()

        all_nodes = storage.get_all_nodes()

        # Group by module
        modules_dict: dict[str, dict[str, Any]] = {}
        for node in all_nodes:
            mod = node["module"]
            if mod not in modules_dict:
                modules_dict[mod] = {"name": mod, "file": node.get("file", ""), "functions": 0, "classes": 0}
            if node["kind"] in ("function", "method"):
                modules_dict[mod]["functions"] += 1
            elif node["kind"] == "class":
                modules_dict[mod]["classes"] += 1
        modules = list(modules_dict.values())

        # Entry points: nodes with zero incoming call edges
        entry_points: list[str] = []
        for node in all_nodes:
            if node["kind"] in ("function", "method"):
                in_edges = storage.get_edges(node["id"], direction="in")
                call_edges = [e for e in in_edges if e["kind"] == "calls"]
                if not call_edges:
                    entry_points.append(node["id"])

        # Hot symbols: highest in-degree (top 10)
        in_degree: dict[str, int] = {}
        for node in all_nodes:
            in_edges = storage.get_edges(node["id"], direction="in")
            in_degree[node["id"]] = len(in_edges)
        hot_symbols = sorted(
            [{"id": k, "in_degree": v} for k, v in in_degree.items()],
            key=lambda x: x["in_degree"],
            reverse=True,
        )[:10]

        # Test coverage: public symbols with assertion edges vs total public
        total_public = sum(1 for n in all_nodes if n.get("is_public", 1) == 1 and n["kind"] in ("function", "method"))
        symbols_with_assertions = 0
        for node in all_nodes:
            if node.get("is_public", 1) == 1 and node["kind"] in ("function", "method"):
                in_edges = storage.get_edges(node["id"], direction="in")
                if any(e["kind"] == "asserts_on" for e in in_edges):
                    symbols_with_assertions += 1

        coverage_pct = round(symbols_with_assertions / total_public * 100, 1) if total_public > 0 else 0

        stats = storage.get_stats()

        return json.dumps(_truncate({
            "modules": modules,
            "entry_points": entry_points,
            "hot_symbols": hot_symbols,
            "test_coverage": {
                "total_public_symbols": total_public,
                "symbols_with_assertions": symbols_with_assertions,
                "coverage_pct": coverage_pct,
            },
            "graph_stats": stats,
        }))

    @server.tool()
    def find_mutations(symbol_id: str) -> str:
        """Given a field or variable, find all functions that mutate it."""
        storage = _get_storage()
        in_edges = storage.get_edges(symbol_id, direction="in")
        mutators = [e for e in in_edges if e["kind"] == "mutates"]

        mutated_by: list[dict[str, Any]] = []
        for edge in mutators:
            node = storage.get_node(edge["src"])
            if node:
                mutated_by.append({
                    "symbol": node["id"],
                    "file": node.get("file", ""),
                    "line_start": node.get("line_start", 0),
                    "confidence": edge.get("confidence", 1.0),
                })
            else:
                mutated_by.append({
                    "symbol": edge["src"],
                    "file": "",
                    "line_start": 0,
                    "confidence": edge.get("confidence", 1.0),
                })

        return json.dumps(_truncate({
            "target": symbol_id,
            "mutated_by": mutated_by,
        }))

    return server


def main() -> None:
    """Run the MCP server over stdio."""
    server = create_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
