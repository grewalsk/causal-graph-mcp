"""Risk scoring for impact analysis using causal edge weights."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from causal_graph_mcp.graph import get_subgraph
from causal_graph_mcp.storage import Storage


@dataclass
class ImpactResult:
    """Result of an impact analysis."""

    changed_symbol: str = ""
    at_risk: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


def compute_impact(
    storage: Storage,
    symbol_id: str,
    max_hops: int = 4,
) -> ImpactResult:
    """Compute impact analysis for a symbol that is about to change.

    Uses mutation, assertion, and side-effect edges in addition to call
    edges to compute risk — not just reachability.

    Args:
        storage: Storage instance.
        symbol_id: The symbol being changed.
        max_hops: Maximum traversal depth.

    Returns:
        ImpactResult with ranked at-risk symbols and summary.
    """
    # Get all nodes reachable via callers (who depends on this symbol?)
    subgraph = get_subgraph(
        storage, symbol_id, direction="callers", max_hops=max_hops, min_confidence=0.0
    )

    result = ImpactResult(changed_symbol=symbol_id)
    high = 0
    medium = 0
    low = 0
    tests_at_risk = 0

    for node in subgraph.nodes:
        if node["id"] == symbol_id:
            continue  # Skip the changed symbol itself

        distance = node.get("hops", 1)
        if distance == 0:
            distance = 1  # Prevent division by zero

        # Count assertion edges pointing to this node
        all_edges = storage.get_edges(node["id"], direction="both")
        assertion_count = sum(
            1 for e in all_edges if e["kind"] == "asserts_on"
        )

        # Check for side-effect edges from this node
        has_side_effects = any(
            e["kind"] == "side_effect" for e in all_edges
        )

        # Check public API status
        is_public = node.get("is_public", 1) == 1

        # Compute risk score
        assertion_weight = 1 + 0.5 * assertion_count
        side_effect_weight = 1.5 if has_side_effects else 1.0
        public_api_weight = 1.5 if is_public else 1.0
        risk_score = (1 / distance) * assertion_weight * side_effect_weight * public_api_weight

        # Build risk factors
        risk_factors: list[str] = []
        for e in all_edges:
            if e["kind"] == "asserts_on":
                risk_factors.append(f"asserts_on:{e['dst']}")
            elif e["kind"] == "mutates":
                risk_factors.append(f"mutates:{e['dst']}")
            elif e["kind"] == "side_effect":
                risk_factors.append(f"side_effect:{e.get('dst', 'unknown')}")

        # Build path from changed symbol to this node
        path = _find_path(subgraph, symbol_id, node["id"])

        at_risk_entry = {
            "symbol": node["id"],
            "distance": distance,
            "risk_score": round(risk_score, 4),
            "risk_factors": risk_factors,
            "path": path,
            "is_test": node.get("is_test", 0) == 1,
        }
        result.at_risk.append(at_risk_entry)

        # Classify
        if risk_score > 0.7:
            high += 1
        elif risk_score >= 0.3:
            medium += 1
        else:
            low += 1

        if node.get("is_test", 0) == 1:
            tests_at_risk += 1

    # Sort by risk score descending
    result.at_risk.sort(key=lambda x: x["risk_score"], reverse=True)

    result.summary = {
        "high_risk": high,
        "medium_risk": medium,
        "low_risk": low,
        "tests_at_risk": tests_at_risk,
    }

    return result


def _find_path(
    subgraph: Any,
    from_id: str,
    to_id: str,
) -> list[str]:
    """Find the path between two nodes in the subgraph using edge data."""
    # Build adjacency from subgraph edges (reverse direction since we traversed callers)
    adj: dict[str, list[str]] = {}
    for edge in subgraph.edges:
        src, dst = edge["src"], edge["dst"]
        adj.setdefault(dst, []).append(src)
        adj.setdefault(src, []).append(dst)

    # BFS to find path
    from collections import deque

    visited: set[str] = set()
    queue: deque[list[str]] = deque()
    queue.append([from_id])
    visited.add(from_id)

    while queue:
        path = queue.popleft()
        current = path[-1]

        if current == to_id:
            return path

        for neighbor in adj.get(current, []):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(path + [neighbor])

    return [from_id, to_id]  # Fallback: direct path
