"""Unit tests for graph traversal."""

from __future__ import annotations

from pathlib import Path

import pytest

from causal_graph_mcp.graph import get_subgraph
from causal_graph_mcp.storage import Storage


def _node(node_id: str, **kwargs) -> dict:
    defaults = {
        "id": node_id,
        "kind": "function",
        "module": "mod",
        "file": "mod.py",
        "line_start": 1,
        "line_end": 10,
        "signature": f"def {node_id}()",
        "docstring": None,
        "is_public": 1,
        "is_test": 0,
        "body_hash": f"hash_{node_id}",
    }
    defaults.update(kwargs)
    return defaults


def _edge(src: str, dst: str, kind: str = "calls", confidence: float = 1.0) -> dict:
    return {"src": src, "dst": dst, "kind": kind, "confidence": confidence}


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    s = Storage(tmp_path)
    yield s
    s.close()


def _setup_chain(storage: Storage) -> None:
    """A → B → C → D chain."""
    storage.upsert_nodes([_node("A"), _node("B"), _node("C"), _node("D")])
    storage.insert_edges([
        _edge("A", "B"),
        _edge("B", "C"),
        _edge("C", "D"),
    ])


class TestTraversal:
    def test_callees_traversal(self, storage: Storage) -> None:
        _setup_chain(storage)
        result = get_subgraph(storage, "A", direction="callees", max_hops=3)
        node_ids = {n["id"] for n in result.nodes}
        assert "A" in node_ids
        assert "B" in node_ids
        assert "C" in node_ids

    def test_callers_traversal(self, storage: Storage) -> None:
        _setup_chain(storage)
        result = get_subgraph(storage, "C", direction="callers", max_hops=3)
        node_ids = {n["id"] for n in result.nodes}
        assert "C" in node_ids
        assert "B" in node_ids
        assert "A" in node_ids

    def test_both_directions(self, storage: Storage) -> None:
        _setup_chain(storage)
        result = get_subgraph(storage, "B", direction="both", max_hops=2)
        node_ids = {n["id"] for n in result.nodes}
        assert "A" in node_ids
        assert "B" in node_ids
        assert "C" in node_ids

    def test_max_hops_limit(self, storage: Storage) -> None:
        _setup_chain(storage)
        result = get_subgraph(storage, "A", direction="callees", max_hops=2)
        node_ids = {n["id"] for n in result.nodes}
        assert "A" in node_ids
        assert "B" in node_ids
        assert "C" in node_ids
        assert "D" not in node_ids

    def test_min_confidence_filter(self, storage: Storage) -> None:
        storage.upsert_nodes([_node("A"), _node("B"), _node("C")])
        storage.insert_edges([
            _edge("A", "B", confidence=1.0),
            _edge("A", "C", confidence=0.3),
        ])
        result = get_subgraph(storage, "A", direction="callees", max_hops=2, min_confidence=0.5)
        node_ids = {n["id"] for n in result.nodes}
        assert "B" in node_ids
        assert "C" not in node_ids

    def test_cycle_detection(self, storage: Storage) -> None:
        storage.upsert_nodes([_node("A"), _node("B"), _node("C")])
        storage.insert_edges([
            _edge("A", "B"),
            _edge("B", "C"),
            _edge("C", "A"),
        ])
        result = get_subgraph(storage, "A", direction="callees", max_hops=10)
        # Should not infinite loop
        node_ids = {n["id"] for n in result.nodes}
        assert "A" in node_ids
        assert "B" in node_ids
        assert "C" in node_ids
        # Cycle should be detected
        assert len(result.cycles_detected) > 0
