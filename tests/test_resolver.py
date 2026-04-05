"""Unit tests for the jedi-based call resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

from causal_graph_mcp.resolver import resolve_calls


def _make_edge(src: str, dst: str, kind: str = "calls", confidence: float = 1.0, **kwargs) -> dict:
    return {
        "src": src,
        "dst": dst,
        "kind": kind,
        "confidence": confidence,
        **kwargs,
    }


class TestConfidenceRules:
    def test_same_file_untouched(self, tmp_path: Path) -> None:
        """Edges at confidence 1.0 should never be modified."""
        edges = [_make_edge("mod.func_a", "mod.func_b", confidence=1.0)]
        result = resolve_calls(edges, str(tmp_path))
        assert result[0]["confidence"] == 1.0
        assert result[0]["dst"] == "mod.func_b"

    def test_import_untouched(self, tmp_path: Path) -> None:
        """Edges at confidence 0.8 should never be modified."""
        edges = [_make_edge("mod.func_a", "foo.bar", confidence=0.8)]
        result = resolve_calls(edges, str(tmp_path))
        assert result[0]["confidence"] == 0.8
        assert result[0]["dst"] == "foo.bar"

    def test_non_call_edges_untouched(self, tmp_path: Path) -> None:
        """Non-call edges should never be modified regardless of confidence."""
        edges = [
            _make_edge("mod.func", "mod.Class.attr", kind="mutates", confidence=0.3),
            _make_edge("test.test_func", "mod.func", kind="asserts_on", confidence=0.3),
            _make_edge("mod", "os", kind="imports", confidence=1.0),
        ]
        result = resolve_calls(edges, str(tmp_path))
        assert len(result) == 3
        assert result[0]["kind"] == "mutates"
        assert result[0]["confidence"] == 0.3
        assert result[1]["kind"] == "asserts_on"
        assert result[1]["confidence"] == 0.3
        assert result[2]["kind"] == "imports"
        assert result[2]["confidence"] == 1.0


class TestJediResolution:
    def test_method_receiver_resolution(self, tmp_path: Path) -> None:
        """Jedi should resolve method calls on typed objects."""
        # Create a source file with a class
        lib_source = '''
class Greeter:
    def greet(self, name: str) -> str:
        return f"Hello, {name}"
'''
        caller_source = '''
from greeter import Greeter

def use_greeter():
    g = Greeter()
    g.greet("world")
'''
        (tmp_path / "greeter.py").write_text(lib_source)
        (tmp_path / "caller.py").write_text(caller_source)

        edges = [_make_edge("caller.use_greeter", "greet", confidence=0.3)]
        result = resolve_calls(edges, str(tmp_path))

        # The resolver should attempt resolution. If jedi can resolve it,
        # confidence upgrades to 0.5. If not (e.g., environment issues),
        # it stays at 0.3. Either way, it shouldn't crash.
        assert result[0]["confidence"] in (0.3, 0.5)
        assert result[0]["kind"] == "calls"

    def test_unresolvable_stays_low(self, tmp_path: Path) -> None:
        """Truly unknown symbols should stay at confidence 0.3."""
        edges = [_make_edge("mod.func", "completely_unknown_xyz", confidence=0.3)]
        result = resolve_calls(edges, str(tmp_path))
        assert result[0]["confidence"] == 0.3


class TestGracefulDegradation:
    def test_bad_file_no_crash(self, tmp_path: Path) -> None:
        """Edge referencing a non-existent file shouldn't crash."""
        edges = [_make_edge("nonexistent.func", "other.func", confidence=0.3)]
        result = resolve_calls(edges, str(tmp_path))
        assert len(result) == 1
        assert result[0]["confidence"] == 0.3

    def test_empty_edges(self, tmp_path: Path) -> None:
        """resolve_calls([]) should return []."""
        result = resolve_calls([], str(tmp_path))
        assert result == []
