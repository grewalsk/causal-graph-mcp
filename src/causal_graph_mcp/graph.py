"""Graph traversal: BFS with cycle detection and direction control."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from causal_graph_mcp.storage import Storage


@dataclass
class SubgraphResult:
    """Result of a subgraph traversal."""

    root: str = ""
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    cycles_detected: list[list[str]] = field(default_factory=list)


def get_subgraph(
    storage: Storage,
    root_id: str,
    direction: str = "both",
    max_hops: int = 3,
    min_confidence: float = 0.0,
) -> SubgraphResult:
    """Traverse the graph from a root node using BFS.

    Args:
        storage: Storage instance.
        root_id: Starting node ID.
        direction: "callers" (inbound), "callees" (outbound), or "both".
        max_hops: Maximum traversal depth.
        min_confidence: Minimum edge confidence to follow.

    Returns:
        SubgraphResult with reachable nodes, edges, and detected cycles.
    """
    result = SubgraphResult(root=root_id)
    visited: set[str] = set()
    # Queue of (node_id, current_hop, path_to_here)
    queue: deque[tuple[str, int, list[str]]] = deque()
    queue.append((root_id, 0, [root_id]))
    visited.add(root_id)

    # Add root node
    root_node = storage.get_node(root_id)
    if root_node:
        node_with_hops = dict(root_node)
        node_with_hops["hops"] = 0
        result.nodes.append(node_with_hops)

    while queue:
        current_id, hop, path = queue.popleft()

        if hop >= max_hops:
            continue

        # Get edges based on direction
        edges = _get_directed_edges(storage, current_id, direction, min_confidence)

        for edge in edges:
            # Determine the neighbor (the node on the other end)
            neighbor_id = edge["dst"] if edge["src"] == current_id else edge["src"]

            # Add edge to result (with hop info)
            edge_with_hops = dict(edge)
            edge_with_hops["hops"] = hop + 1
            result.edges.append(edge_with_hops)

            if neighbor_id in visited:
                # Cycle detected
                if neighbor_id in path:
                    cycle_start = path.index(neighbor_id)
                    cycle = path[cycle_start:] + [neighbor_id]
                    result.cycles_detected.append(cycle)
                continue

            visited.add(neighbor_id)
            new_path = path + [neighbor_id]

            # Add neighbor node
            neighbor_node = storage.get_node(neighbor_id)
            if neighbor_node:
                node_with_hops = dict(neighbor_node)
                node_with_hops["hops"] = hop + 1
                result.nodes.append(node_with_hops)

            queue.append((neighbor_id, hop + 1, new_path))

    return result


def _get_directed_edges(
    storage: Storage,
    node_id: str,
    direction: str,
    min_confidence: float,
) -> list[dict[str, Any]]:
    """Get edges for a node filtered by direction and confidence."""
    if direction == "callees":
        edges = storage.get_edges(node_id, direction="out")
    elif direction == "callers":
        edges = storage.get_edges(node_id, direction="in")
    else:
        edges = storage.get_edges(node_id, direction="both")

    if min_confidence > 0:
        edges = [e for e in edges if e.get("confidence", 1.0) >= min_confidence]

    return edges
