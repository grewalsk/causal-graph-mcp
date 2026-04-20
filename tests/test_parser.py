"""Unit tests for the AST parser module."""

from __future__ import annotations

from pathlib import Path

from causal_graph_mcp.parser import ParseResult, parse_file


def _write_and_parse(
    tmp_path: Path,
    source: str,
    filename: str = "mod.py",
    module: str = "mod",
) -> ParseResult:
    """Write source to a file and parse it."""
    fp = tmp_path / filename
    fp.write_text(source, encoding="utf-8")
    return parse_file(str(fp), module)


class TestNodeExtraction:
    def test_extract_functions(self, tmp_path: Path) -> None:
        source = '''
def greet(name: str) -> str:
    """Say hello."""
    return f"Hello, {name}"

def _helper():
    pass
'''
        result = _write_and_parse(tmp_path, source)
        nodes = {n["id"]: n for n in result.nodes}

        assert "mod.greet" in nodes
        n = nodes["mod.greet"]
        assert n["kind"] == "function"
        assert "greet" in n["signature"]
        assert n["docstring"] == "Say hello."
        assert n["is_public"] == 1
        assert n["body_hash"]  # non-empty

        assert "mod._helper" in nodes
        assert nodes["mod._helper"]["is_public"] == 0

    def test_extract_classes_and_methods(self, tmp_path: Path) -> None:
        source = '''
class MyClass:
    """A class."""
    def method_a(self):
        pass

    def method_b(self, x: int) -> int:
        return x
'''
        result = _write_and_parse(tmp_path, source)
        nodes = {n["id"]: n for n in result.nodes}

        assert "mod.MyClass" in nodes
        assert nodes["mod.MyClass"]["kind"] == "class"
        assert nodes["mod.MyClass"]["docstring"] == "A class."

        assert "mod.MyClass.method_a" in nodes
        assert nodes["mod.MyClass.method_a"]["kind"] == "method"

        assert "mod.MyClass.method_b" in nodes
        assert nodes["mod.MyClass.method_b"]["kind"] == "method"

    def test_extract_module_variables(self, tmp_path: Path) -> None:
        source = '''
MAX_RETRIES = 3
_secret: str = "hidden"
'''
        result = _write_and_parse(tmp_path, source)
        nodes = {n["id"]: n for n in result.nodes}

        assert "mod.MAX_RETRIES" in nodes
        assert nodes["mod.MAX_RETRIES"]["kind"] == "variable"
        assert nodes["mod.MAX_RETRIES"]["is_public"] == 1

        assert "mod._secret" in nodes
        assert nodes["mod._secret"]["is_public"] == 0

    def test_async_function(self, tmp_path: Path) -> None:
        source = '''
async def fetch_data(url: str) -> dict:
    pass
'''
        result = _write_and_parse(tmp_path, source)
        nodes = {n["id"]: n for n in result.nodes}

        assert "mod.fetch_data" in nodes
        assert nodes["mod.fetch_data"]["kind"] == "function"
        assert "async def" in nodes["mod.fetch_data"]["signature"]

    def test_is_public(self, tmp_path: Path) -> None:
        source = '''
def public_func():
    pass

def _private_func():
    pass

class _PrivateClass:
    pass
'''
        result = _write_and_parse(tmp_path, source)
        nodes = {n["id"]: n for n in result.nodes}

        assert nodes["mod.public_func"]["is_public"] == 1
        assert nodes["mod._private_func"]["is_public"] == 0
        assert nodes["mod._PrivateClass"]["is_public"] == 0


class TestCallEdges:
    def test_same_file(self, tmp_path: Path) -> None:
        source = '''
def func_b():
    pass

def func_a():
    func_b()
'''
        result = _write_and_parse(tmp_path, source)
        call_edges = [e for e in result.edges if e["kind"] == "calls"]

        assert any(
            e["src"] == "mod.func_a" and e["dst"] == "mod.func_b" and e["confidence"] == 1.0
            for e in call_edges
        )

    def test_imported(self, tmp_path: Path) -> None:
        source = '''
from foo import bar

def func_a():
    bar()
'''
        result = _write_and_parse(tmp_path, source)
        call_edges = [e for e in result.edges if e["kind"] == "calls"]

        assert any(
            e["src"] == "mod.func_a" and e["dst"] == "foo.bar" and e["confidence"] == 0.8
            for e in call_edges
        )

    def test_unresolved(self, tmp_path: Path) -> None:
        source = '''
def func_a():
    unknown_func()
'''
        result = _write_and_parse(tmp_path, source)
        call_edges = [e for e in result.edges if e["kind"] == "calls"]

        assert any(
            e["src"] == "mod.func_a" and e["dst"] == "unknown_func" and e["confidence"] == 0.3
            for e in call_edges
        )


class TestMutationEdges:
    def test_self_mutation(self, tmp_path: Path) -> None:
        source = '''
class Token:
    def set_value(self, v):
        self.value = v
'''
        result = _write_and_parse(tmp_path, source)
        mut_edges = [e for e in result.edges if e["kind"] == "mutates"]

        assert any(
            e["src"] == "mod.Token.set_value" and e["dst"] == "mod.Token.value"
            for e in mut_edges
        )

    def test_global_mutation(self, tmp_path: Path) -> None:
        source = '''
counter = 0

def increment():
    counter = counter + 1
'''
        result = _write_and_parse(tmp_path, source)
        mut_edges = [e for e in result.edges if e["kind"] == "mutates"]

        assert any(
            e["src"] == "mod.increment" and e["dst"] == "mod.counter"
            for e in mut_edges
        )


class TestAssertionEdges:
    def test_assertion_edges(self, tmp_path: Path) -> None:
        source = '''
from auth import create_token

def test_token_creation():
    result = create_token(1)
    assert result is not None
    self.assertEqual(create_token(2), "expected")
'''
        result = _write_and_parse(tmp_path, source, filename="test_auth.py", module="test_auth")
        assert_edges = [e for e in result.edges if e["kind"] == "asserts_on"]

        # Should have assertion edges
        assert len(assert_edges) > 0

        # Test nodes should have is_test=1
        test_nodes = [n for n in result.nodes if n["kind"] == "function"]
        assert all(n["is_test"] == 1 for n in test_nodes)


class TestSideEffectEdges:
    def test_side_effect_edges(self, tmp_path: Path) -> None:
        source = '''
import requests
import subprocess

def read_file():
    f = open("data.txt")
    return f.read()

def fetch_api():
    requests.get("https://api.example.com")

def run_cmd():
    subprocess.run(["ls"])
'''
        result = _write_and_parse(tmp_path, source)
        se_edges = [e for e in result.edges if e["kind"] == "side_effect"]

        # open → file_io
        file_io = [e for e in se_edges if "file_io" in (e.get("detail") or "")]
        assert len(file_io) >= 1

        # requests.get → network
        network = [e for e in se_edges if "network" in (e.get("detail") or "")]
        assert len(network) >= 1

        # subprocess.run → process
        process = [e for e in se_edges if "process" in (e.get("detail") or "")]
        assert len(process) >= 1


class TestImportEdges:
    def test_import_edges(self, tmp_path: Path) -> None:
        source = '''
import os
from pathlib import Path
from auth.utils import create_token
'''
        result = _write_and_parse(tmp_path, source)
        import_edges = [e for e in result.edges if e["kind"] == "imports"]

        dsts = {e["dst"] for e in import_edges}
        assert "os" in dsts
        assert "pathlib.Path" in dsts
        assert "auth.utils.create_token" in dsts

        # All import edges should come from the module
        assert all(e["src"] == "mod" for e in import_edges)


class TestInheritanceEdges:
    def test_inheritance(self, tmp_path: Path) -> None:
        source = '''
class Animal:
    def speak(self):
        pass

class Dog(Animal):
    pass
'''
        result = _write_and_parse(tmp_path, source)
        inherits = [e for e in result.edges if e["kind"] == "inherits"]

        assert any(
            e["src"] == "mod.Dog" and e["dst"] == "mod.Animal"
            for e in inherits
        )


class TestOverrideEdges:
    def test_override(self, tmp_path: Path) -> None:
        source = '''
class Animal:
    def speak(self):
        return "..."

class Dog(Animal):
    def speak(self):
        return "Woof"
'''
        result = _write_and_parse(tmp_path, source)
        overrides = [e for e in result.edges if e["kind"] == "overrides"]

        assert any(
            e["src"] == "mod.Dog.speak" and e["dst"] == "mod.Animal.speak"
            for e in overrides
        )
