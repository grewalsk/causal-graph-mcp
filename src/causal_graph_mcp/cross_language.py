"""Cross-language edge detection via REST route matching and shared contracts."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from causal_graph_mcp.storage import Storage

logger = logging.getLogger(__name__)


def detect_cross_language_edges(storage: Storage) -> list[dict[str, Any]]:
    """Detect cross-language edges by matching REST routes, gRPC services, etc.

    Scans the graph for:
    1. REST route definitions (Flask, FastAPI, Express, Gin, etc.)
    2. HTTP client calls (fetch, axios, requests, http.Get, etc.)
    3. Matches them by URL pattern to create cross-language edges.

    Returns list of cross-language edge dicts.
    """
    edges: list[dict[str, Any]] = []

    # Collect all route definitions and HTTP client calls from the graph
    route_defs = _find_route_definitions(storage)
    http_calls = _find_http_client_calls(storage)

    # Match routes to calls
    for call in http_calls:
        for route in route_defs:
            if _routes_match(call["url_pattern"], route["url_pattern"], call.get("method"), route.get("method")):
                edges.append({
                    "src": call["symbol"],
                    "src_language": call["language"],
                    "dst": route["symbol"],
                    "dst_language": route["language"],
                    "kind": "cross_language_call",
                    "integration": "rest_api",
                    "confidence": _compute_route_confidence(call, route),
                    "contract": f"{route.get('method', 'ANY')} {route['url_pattern']}",
                    "detail": json.dumps({
                        "caller": call["symbol"],
                        "handler": route["symbol"],
                        "route": route["url_pattern"],
                        "method": route.get("method", "ANY"),
                    }),
                })

    return edges


# Route definition patterns by framework
_ROUTE_PATTERNS: dict[str, list[dict[str, Any]]] = {
    # Python frameworks
    "python": [
        # Flask: @app.route("/path"), @app.get("/path")
        {"regex": r'@\w+\.(route|get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']',
         "method_group": 1, "url_group": 2,
         "method_map": {"route": "ANY", "get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE", "patch": "PATCH"}},
        # FastAPI: @app.get("/path"), @router.post("/path")
        {"regex": r'@\w+\.(get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']',
         "method_group": 1, "url_group": 2,
         "method_map": {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE", "patch": "PATCH"}},
    ],
    # JavaScript/TypeScript frameworks
    "javascript": [
        # Express: app.get("/path", handler), router.post("/path", handler)
        {"regex": r'\w+\.(get|post|put|delete|patch|all)\s*\(\s*["\']([^"\']+)["\']',
         "method_group": 1, "url_group": 2,
         "method_map": {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE", "patch": "PATCH", "all": "ANY"}},
    ],
    "typescript": [
        {"regex": r'\w+\.(get|post|put|delete|patch|all)\s*\(\s*["\']([^"\']+)["\']',
         "method_group": 1, "url_group": 2,
         "method_map": {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE", "patch": "PATCH", "all": "ANY"}},
    ],
    # Go frameworks
    "go": [
        # net/http: http.HandleFunc("/path", handler)
        {"regex": r'HandleFunc\s*\(\s*["\']([^"\']+)["\']',
         "method_group": None, "url_group": 1, "method_map": {}},
        # Gin: r.GET("/path", handler)
        {"regex": r'\w+\.(GET|POST|PUT|DELETE|PATCH)\s*\(\s*["\']([^"\']+)["\']',
         "method_group": 1, "url_group": 2, "method_map": {}},
    ],
    "java": [
        # Spring: @GetMapping("/path"), @PostMapping("/path"), @RequestMapping("/path")
        {"regex": r'@(Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']',
         "method_group": 1, "url_group": 2,
         "method_map": {"Get": "GET", "Post": "POST", "Put": "PUT", "Delete": "DELETE", "Patch": "PATCH", "Request": "ANY"}},
    ],
}

# HTTP client call patterns
_CLIENT_PATTERNS: dict[str, list[dict[str, Any]]] = {
    "python": [
        # requests.get("url"), httpx.post("url")
        {"regex": r'(?:requests|httpx)\.(get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']',
         "method_group": 1, "url_group": 2},
        {"regex": r'(?:requests|httpx)\.(get|post|put|delete|patch)\s*\(\s*f["\']([^"\']+)["\']',
         "method_group": 1, "url_group": 2},
    ],
    "javascript": [
        # fetch("url"), fetch("url", { method: "POST" })
        {"regex": r'fetch\s*\(\s*["`\']([^"`\']+)["`\']', "method_group": None, "url_group": 1},
        # axios.get("url"), axios.post("url")
        {"regex": r'axios\.(get|post|put|delete|patch)\s*\(\s*["`\']([^"`\']+)["`\']',
         "method_group": 1, "url_group": 2},
    ],
    "typescript": [
        {"regex": r'fetch\s*\(\s*["`\']([^"`\']+)["`\']', "method_group": None, "url_group": 1},
        {"regex": r'axios\.(get|post|put|delete|patch)\s*\(\s*["`\']([^"`\']+)["`\']',
         "method_group": 1, "url_group": 2},
    ],
    "go": [
        # http.Get("url"), http.Post("url", ...)
        {"regex": r'http\.(Get|Post)\s*\(\s*["\']([^"\']+)["\']',
         "method_group": 1, "url_group": 2},
    ],
    "java": [
        # HttpRequest.newBuilder().uri(URI.create("url"))
        {"regex": r'URI\.create\s*\(\s*["\']([^"\']+)["\']', "method_group": None, "url_group": 1},
        # RestTemplate.getForObject("url", ...)
        {"regex": r'(?:restTemplate|RestTemplate)\.\w+\s*\(\s*["\']([^"\']+)["\']',
         "method_group": None, "url_group": 1},
    ],
}


def _find_route_definitions(storage: Storage) -> list[dict[str, Any]]:
    """Scan all nodes for REST route definitions by reading their source."""
    routes: list[dict[str, Any]] = []
    all_nodes = storage.get_all_nodes()

    for node in all_nodes:
        if node["kind"] not in ("function", "method"):
            continue

        file_path = node.get("file", "")
        # Detect language from file extension
        lang = _detect_lang(file_path)
        if not lang:
            continue

        patterns = _ROUTE_PATTERNS.get(lang, [])
        if not patterns:
            continue

        # Read the source around the function (include decorators/annotations)
        source = _read_source_context(file_path, node.get("line_start", 1), context_lines=5)
        if not source:
            continue

        for pattern in patterns:
            for match in re.finditer(pattern["regex"], source):
                url_group = pattern["url_group"]
                method_group = pattern.get("method_group")

                url = match.group(url_group)
                method = "ANY"
                if method_group is not None:
                    raw_method = match.group(method_group)
                    method = pattern.get("method_map", {}).get(raw_method, raw_method.upper())

                routes.append({
                    "symbol": node["id"],
                    "language": lang,
                    "url_pattern": url,
                    "method": method,
                    "file": file_path,
                })

    return routes


def _find_http_client_calls(storage: Storage) -> list[dict[str, Any]]:
    """Scan all nodes for HTTP client calls."""
    calls: list[dict[str, Any]] = []
    all_nodes = storage.get_all_nodes()

    for node in all_nodes:
        if node["kind"] not in ("function", "method"):
            continue

        file_path = node.get("file", "")
        lang = _detect_lang(file_path)
        if not lang:
            continue

        patterns = _CLIENT_PATTERNS.get(lang, [])
        if not patterns:
            continue

        source = _read_source(file_path, node.get("line_start", 1), node.get("line_end", 1))
        if not source:
            continue

        for pattern in patterns:
            for match in re.finditer(pattern["regex"], source):
                url_group = pattern["url_group"]
                method_group = pattern.get("method_group")

                url = match.group(url_group)
                method = "ANY"
                if method_group is not None:
                    method = match.group(method_group).upper()

                calls.append({
                    "symbol": node["id"],
                    "language": lang,
                    "url_pattern": url,
                    "method": method,
                    "file": file_path,
                })

    return calls


def _routes_match(call_url: str, route_url: str, call_method: str | None, route_method: str | None) -> bool:
    """Check if a client URL matches a route definition.

    Handles path parameters like /users/:id or /users/{id}.
    """
    # Normalize
    call_url = call_url.rstrip("/")
    route_url = route_url.rstrip("/")

    # Strip protocol/host from client URLs
    if "://" in call_url:
        call_url = "/" + call_url.split("://", 1)[1].split("/", 1)[-1]

    # Method matching (ANY matches everything)
    if call_method and route_method and route_method != "ANY" and call_method != "ANY":
        if call_method != route_method:
            return False

    # Convert route params to regex
    # /users/:id → /users/[^/]+
    # /users/{id} → /users/[^/]+
    route_regex = re.sub(r':[a-zA-Z_]\w*', r'[^/]+', route_url)
    route_regex = re.sub(r'\{[a-zA-Z_]\w*\}', r'[^/]+', route_regex)
    route_regex = f"^{route_regex}$"

    # Also convert f-string style params in client URLs
    call_normalized = re.sub(r'\{[^}]+\}', '[^/]+', call_url)

    try:
        return bool(re.match(route_regex, call_normalized))
    except re.error:
        return call_url == route_url


def _compute_route_confidence(call: dict, route: dict) -> float:
    """Compute confidence for a route match."""
    confidence = 0.6  # Base for URL string matching

    # Boost if HTTP methods match explicitly
    if call.get("method") and route.get("method"):
        if call["method"] == route["method"] and call["method"] != "ANY":
            confidence += 0.15

    # Boost if same codebase (more likely correct)
    if call.get("file") and route.get("file"):
        # Different language files = true cross-language
        if call["language"] != route["language"]:
            confidence += 0.1

    return min(confidence, 0.95)


def _detect_lang(file_path: str) -> str | None:
    """Detect language from file extension."""
    from causal_graph_mcp.language import detect_language
    return detect_language(file_path)


def _read_source(file_path: str, line_start: int, line_end: int) -> str | None:
    """Read source lines from a file."""
    try:
        from pathlib import Path
        lines = Path(file_path).read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[max(0, line_start - 1):line_end])
    except Exception:
        return None


def _read_source_context(file_path: str, line_start: int, context_lines: int = 5) -> str | None:
    """Read source lines with context before the function (for decorators)."""
    try:
        from pathlib import Path
        lines = Path(file_path).read_text(encoding="utf-8").splitlines()
        start = max(0, line_start - 1 - context_lines)
        end = min(len(lines), line_start + 3)
        return "\n".join(lines[start:end])
    except Exception:
        return None
