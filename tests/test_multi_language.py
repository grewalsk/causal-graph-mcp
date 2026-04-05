"""Tests for multi-language parsing via tree-sitter and cross-language edge detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from causal_graph_mcp.cross_language import detect_cross_language_edges, _routes_match
from causal_graph_mcp.indexer import index_project
from causal_graph_mcp.language import register_parser, get_parser
from causal_graph_mcp.python_parser import PythonParser
from causal_graph_mcp.storage import Storage
from causal_graph_mcp.ts_parser import TreeSitterParser


@pytest.fixture(autouse=True)
def _register_all_parsers():
    """Register parsers for all tests."""
    register_parser(PythonParser())
    for lang, exts in [
        ("javascript", [".js", ".jsx"]),
        ("typescript", [".ts", ".tsx"]),
        ("go", [".go"]),
    ]:
        try:
            register_parser(TreeSitterParser(lang, exts))
        except Exception:
            pass


class TestLanguageDetection:
    def test_python_parser(self) -> None:
        parser = get_parser("auth.py")
        assert parser is not None
        assert parser.language_name == "python"

    def test_js_parser(self) -> None:
        parser = get_parser("app.js")
        assert parser is not None
        assert parser.language_name == "javascript"

    def test_ts_parser(self) -> None:
        parser = get_parser("app.ts")
        assert parser is not None
        assert parser.language_name == "typescript"

    def test_go_parser(self) -> None:
        parser = get_parser("main.go")
        assert parser is not None
        assert parser.language_name == "go"

    def test_unknown_extension(self) -> None:
        assert get_parser("file.txt") is None


class TestJavaScriptParsing:
    def test_extract_functions(self, tmp_path: Path) -> None:
        source = '''
function greet(name) {
    return "Hello, " + name;
}

const helper = () => {
    return 42;
};
'''
        (tmp_path / "mod.js").write_text(source)
        parser = get_parser("mod.js")
        result = parser.parse(str(tmp_path / "mod.js"), "mod")

        func_nodes = [n for n in result.nodes if n["kind"] == "function"]
        assert any(n["id"] == "mod.greet" for n in func_nodes)

    def test_extract_class(self, tmp_path: Path) -> None:
        source = '''
class UserService {
    constructor(db) {
        this.db = db;
    }

    getUser(id) {
        return this.db.find(id);
    }
}
'''
        (tmp_path / "service.js").write_text(source)
        parser = get_parser("service.js")
        result = parser.parse(str(tmp_path / "service.js"), "service")

        class_nodes = [n for n in result.nodes if n["kind"] == "class"]
        assert any(n["id"] == "service.UserService" for n in class_nodes)

        method_nodes = [n for n in result.nodes if n["kind"] == "method"]
        assert any(n["id"] == "service.UserService.getUser" for n in method_nodes)

    def test_call_edges(self, tmp_path: Path) -> None:
        source = '''
function validate(x) { return x > 0; }

function process(data) {
    if (validate(data)) {
        return data;
    }
}
'''
        (tmp_path / "mod.js").write_text(source)
        parser = get_parser("mod.js")
        result = parser.parse(str(tmp_path / "mod.js"), "mod")

        call_edges = [e for e in result.edges if e["kind"] == "calls"]
        assert any(
            e["src"] == "mod.process" and e["dst"] == "mod.validate"
            for e in call_edges
        )

    def test_import_edges(self, tmp_path: Path) -> None:
        source = '''
import express from 'express';
import { Router } from 'express';
'''
        (tmp_path / "app.js").write_text(source)
        parser = get_parser("app.js")
        result = parser.parse(str(tmp_path / "app.js"), "app")

        import_edges = [e for e in result.edges if e["kind"] == "imports"]
        dsts = {e["dst"] for e in import_edges}
        assert "express" in dsts

    def test_side_effect_edges(self, tmp_path: Path) -> None:
        source = '''
function fetchData() {
    return fetch("https://api.example.com/data");
}
'''
        (tmp_path / "api.js").write_text(source)
        parser = get_parser("api.js")
        result = parser.parse(str(tmp_path / "api.js"), "api")

        se_edges = [e for e in result.edges if e["kind"] == "side_effect"]
        assert any("network" in (e.get("detail") or "") for e in se_edges)

    def test_inheritance_edges(self, tmp_path: Path) -> None:
        source = '''
class Animal {
    speak() { return "..."; }
}

class Dog extends Animal {
    speak() { return "Woof"; }
}
'''
        (tmp_path / "mod.js").write_text(source)
        parser = get_parser("mod.js")
        result = parser.parse(str(tmp_path / "mod.js"), "mod")

        inherits = [e for e in result.edges if e["kind"] == "inherits"]
        assert any(e["src"] == "mod.Dog" for e in inherits)


class TestGoParsing:
    def test_extract_functions(self, tmp_path: Path) -> None:
        source = '''
package main

func greet(name string) string {
    return "Hello, " + name
}

func main() {
    greet("world")
}
'''
        (tmp_path / "main.go").write_text(source)
        parser = get_parser("main.go")
        result = parser.parse(str(tmp_path / "main.go"), "main")

        func_nodes = [n for n in result.nodes if n["kind"] == "function"]
        func_ids = {n["id"] for n in func_nodes}
        assert "main.greet" in func_ids
        assert "main.main" in func_ids

    def test_call_edges(self, tmp_path: Path) -> None:
        source = '''
package main

func helper() int { return 1 }

func process() int {
    return helper()
}
'''
        (tmp_path / "main.go").write_text(source)
        parser = get_parser("main.go")
        result = parser.parse(str(tmp_path / "main.go"), "main")

        call_edges = [e for e in result.edges if e["kind"] == "calls"]
        assert any(
            e["src"] == "main.process" and e["dst"] == "main.helper"
            for e in call_edges
        )


class TestRouteMatching:
    def test_exact_match(self) -> None:
        assert _routes_match("/api/users", "/api/users", "GET", "GET")

    def test_method_mismatch(self) -> None:
        assert not _routes_match("/api/users", "/api/users", "GET", "POST")

    def test_any_method_matches(self) -> None:
        assert _routes_match("/api/users", "/api/users", "GET", "ANY")

    def test_path_param_colon(self) -> None:
        assert _routes_match("/api/users/123", "/api/users/:id", "GET", "GET")

    def test_path_param_braces(self) -> None:
        assert _routes_match("/api/users/123", "/api/users/{id}", "GET", "GET")

    def test_url_with_host(self) -> None:
        assert _routes_match("https://api.example.com/api/users", "/api/users", "GET", "GET")

    def test_no_match(self) -> None:
        assert not _routes_match("/api/users", "/api/posts", "GET", "GET")


class TestCrossLanguageDetection:
    def test_python_flask_to_js_fetch(self, tmp_path: Path) -> None:
        """Detect JS frontend calling Python Flask backend."""
        # Python Flask backend
        (tmp_path / "app.py").write_text('''
from flask import Flask
app = Flask(__name__)

@app.get("/api/users")
def get_users():
    return {"users": []}

@app.post("/api/users")
def create_user():
    return {"id": 1}
''')
        # JavaScript frontend
        (tmp_path / "frontend.js").write_text('''
async function loadUsers() {
    const resp = await fetch("/api/users");
    return resp.json();
}

async function addUser(name) {
    const resp = await fetch("/api/users", { method: "POST" });
    return resp.json();
}
''')
        storage = Storage(tmp_path)
        try:
            index_project(str(tmp_path), storage)
            edges = detect_cross_language_edges(storage)

            # Should find at least one cross-language edge
            assert len(edges) > 0

            # Verify the edge connects JS caller to Python handler
            for edge in edges:
                assert edge["src_language"] in ("javascript", "python")
                assert edge["dst_language"] in ("javascript", "python")
                assert edge["integration"] == "rest_api"
                assert edge["confidence"] > 0
        finally:
            storage.close()


class TestMultiLanguageIndexing:
    def test_mixed_project(self, tmp_path: Path) -> None:
        """Index a project with Python and JavaScript files."""
        (tmp_path / "backend.py").write_text('''
def process_data(x):
    return x * 2
''')
        (tmp_path / "frontend.js").write_text('''
function renderData(data) {
    return data.toString();
}
''')
        storage = Storage(tmp_path)
        try:
            result = index_project(str(tmp_path), storage)
            assert result.files_parsed == 2
            assert result.nodes_indexed > 0

            # Both languages indexed
            py_node = storage.get_node("backend.process_data")
            assert py_node is not None

            js_node = storage.get_node("frontend.renderData")
            assert js_node is not None
        finally:
            storage.close()
