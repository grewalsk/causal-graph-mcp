"""Unit tests for risk scoring."""

from __future__ import annotations

from pathlib import Path

import pytest

from causal_graph_mcp.risk import compute_impact
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


def _edge(src: str, dst: str, kind: str = "calls", confidence: float = 1.0, **kw) -> dict:
    return {"src": src, "dst": dst, "kind": kind, "confidence": confidence, **kw}


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    s = Storage(tmp_path)
    yield s
    s.close()


class TestRiskScoring:
    def test_basic_risk_score(self, storage: Storage) -> None:
        """Symbol with assertion edge at distance 1 should have high risk."""
        storage.upsert_nodes([
            _node("target"),
            _node("caller"),
        ])
        storage.insert_edges([
            _edge("caller", "target", kind="calls"),
            _edge("caller", "target", kind="asserts_on"),
        ])

        result = compute_impact(storage, "target", max_hops=3)
        assert len(result.at_risk) == 1

        entry = result.at_risk[0]
        assert entry["symbol"] == "caller"
        # distance=1, assertion_weight=1+0.5*1=1.5, side_effect=1.0, public=1.5
        # risk = (1/1) * 1.5 * 1.0 * 1.5 = 2.25
        assert entry["risk_score"] == 2.25

    def test_side_effect_weight(self, storage: Storage) -> None:
        """Symbol with side-effect edges should get 1.5x multiplier."""
        storage.upsert_nodes([
            _node("target"),
            _node("caller"),
        ])
        storage.insert_edges([
            _edge("caller", "target", kind="calls"),
            _edge("caller", "side_effect:file_io:open", kind="side_effect",
                  detail='{"kind": "file_io", "call": "open"}'),
        ])

        result = compute_impact(storage, "target", max_hops=3)
        entry = result.at_risk[0]
        # distance=1, assertion=1.0, side_effect=1.5, public=1.5
        # risk = 1 * 1.0 * 1.5 * 1.5 = 2.25
        assert entry["risk_score"] == 2.25

    def test_public_api_weight(self, storage: Storage) -> None:
        """Private symbol should get 1.0x, not 1.5x."""
        storage.upsert_nodes([
            _node("target"),
            _node("_private_caller", is_public=0),
        ])
        storage.insert_edges([
            _edge("_private_caller", "target", kind="calls"),
        ])

        result = compute_impact(storage, "target", max_hops=3)
        entry = result.at_risk[0]
        # distance=1, assertion=1.0, side_effect=1.0, public=1.0
        # risk = 1 * 1.0 * 1.0 * 1.0 = 1.0
        assert entry["risk_score"] == 1.0

    def test_risk_sorting(self, storage: Storage) -> None:
        """Multiple at-risk nodes should be sorted by score descending."""
        storage.upsert_nodes([
            _node("target"),
            _node("close_caller"),
            _node("far_caller"),
        ])
        storage.insert_edges([
            _edge("close_caller", "target", kind="calls"),
            _edge("far_caller", "close_caller", kind="calls"),
        ])

        result = compute_impact(storage, "target", max_hops=3)
        assert len(result.at_risk) == 2
        # close_caller is at distance 1, far_caller at distance 2
        assert result.at_risk[0]["symbol"] == "close_caller"
        assert result.at_risk[1]["symbol"] == "far_caller"
        assert result.at_risk[0]["risk_score"] > result.at_risk[1]["risk_score"]

    def test_summary_counts(self, storage: Storage) -> None:
        """Summary should correctly count risk categories and tests."""
        storage.upsert_nodes([
            _node("target"),
            _node("caller_a"),
            _node("test_caller", is_test=1, file="test_mod.py"),
        ])
        storage.insert_edges([
            _edge("caller_a", "target", kind="calls"),
            _edge("test_caller", "target", kind="calls"),
            _edge("test_caller", "target", kind="asserts_on"),
        ])

        result = compute_impact(storage, "target", max_hops=3)
        assert result.summary["tests_at_risk"] == 1
        total = result.summary["high_risk"] + result.summary["medium_risk"] + result.summary["low_risk"]
        assert total == len(result.at_risk)
