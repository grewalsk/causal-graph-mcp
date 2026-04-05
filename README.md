# causal-graph-mcp

A Python MCP server that indexes a Python codebase into a persistent **causal dependency graph** stored in SQLite, exposing it to Claude Code as a set of structural query tools.

The core differentiator over existing tools is the **edge model**. Beyond standard call and import edges, the graph tracks:

- **Mutation edges** — what state a function writes (`self.token = ...`)
- **Assertion edges** — what tests assert on which symbols
- **Side-effect edges** — file I/O, network calls, cache writes, subprocess invocations
- **Inheritance and override edges** — class hierarchies and method overrides

This richer graph makes impact analysis genuinely accurate: not just "what's reachable from this function" but "what will actually break and which tests will fail."

---

## Quick Start

```bash
# Register with Claude Code
claude mcp add causal-graph-mcp -- python -m causal_graph_mcp

# Or add to ~/.claude/mcp.json
{
  "mcpServers": {
    "causal-graph": {
      "command": "python",
      "args": ["-m", "causal_graph_mcp"]
    }
  }
}
```

On startup, the server automatically indexes the working directory and starts a background file watcher. The graph is hot before the first tool call arrives.

### Requirements

- Python 3.11+
- Three runtime dependencies: `mcp`, `jedi`, `watchdog`

```bash
pip install mcp jedi watchdog
```

---

## How It Works

```
 ┌─────────┐     ┌────────┐     ┌──────────┐     ┌─────────┐
 │ watchdog │────▶│ indexer │────▶│  parser  │────▶│ storage │
 │ (300ms)  │     │pipeline│     │ (ast +   │     │(SQLite) │
 └─────────┘     └────────┘     │  jedi)   │     └────┬────┘
                                 └──────────┘          │
                                                       ▼
                               ┌───────────────────────────────┐
                               │     7 MCP Tools (FastMCP)     │
                               │  stdio / JSON-RPC 2.0         │
                               └───────────────────────────────┘
```

1. **File discovery** — walks the project, respects `.gitignore`, skips `__pycache__/`, `.venv/`, `.git/`, etc.
2. **Change detection** — SHA-256 content hashing per file; unchanged files are skipped on re-index.
3. **AST extraction** — parses each `.py` file with `ast.parse()`, extracts nodes (functions, methods, classes, variables) and all 7 edge kinds.
4. **Call resolution** — `jedi.Script.goto()` resolves method receivers and duck-typed interfaces that `ast` alone can't handle. Confidence scores on every edge.
5. **Atomic storage** — each file's nodes and edges are written in a single SQLite transaction. Delete old, insert new, update hash.
6. **Live re-indexing** — `watchdog` monitors for `.py` file changes, debounces 300ms, triggers incremental re-index automatically.

---

## MCP Tools

### `index_project`

Index (or re-index) a Python project at the given path. Safe to call multiple times — incremental on subsequent calls.

```
Input:  { "project_root": "/path/to/project" }   (optional, defaults to cwd)
Output: { "nodes_indexed": 342, "edges_indexed": 891, "files_parsed": 28,
          "files_skipped": 14, "duration_ms": 1240 }
```

### `get_call_graph`

Trace callers and callees of a function, recursively up to N hops. Returns the subgraph with confidence scores on each edge.

```
Input:  { "symbol": "auth.create_token", "direction": "both",
          "max_hops": 3, "min_confidence": 0.5 }
Output: { "root": "auth.create_token",
          "nodes": [{ "id": "...", "kind": "function", "file": "...", "hops": 1 }],
          "edges": [{ "src": "...", "dst": "...", "kind": "calls", "confidence": 1.0 }],
          "cycles_detected": [] }
```

Direction: `"callers"` (who calls this?), `"callees"` (what does this call?), or `"both"`.

### `impact_analysis`

Given a symbol that is about to change, return all downstream symbols ranked by breakage risk. Uses mutation, assertion, and side-effect edges — not just call reachability.

```
Input:  { "symbol": "auth.create_token", "max_hops": 4 }
Output: { "changed_symbol": "auth.create_token",
          "at_risk": [
            { "symbol": "test_auth.test_login", "distance": 2,
              "risk_score": 0.94,
              "risk_factors": ["asserts_on:auth.create_token", "mutates:Session.token"],
              "path": ["auth.create_token", "Session.token", "test_auth.test_login"] }
          ],
          "summary": { "high_risk": 3, "medium_risk": 7, "low_risk": 12, "tests_at_risk": 4 } }
```

**Risk score formula:**
```
risk = (1 / distance) * assertion_weight * side_effect_weight * public_api_weight

assertion_weight  = 1 + 0.5 * count(asserts_on edges to this symbol)
side_effect_weight = 1.5 if symbol has any side_effect edges, else 1.0
public_api_weight  = 1.5 if symbol is public, else 1.0
```

### `semantic_search`

Search for symbols by name or concept using BM25 full-text search on names, signatures, and docstrings. No embeddings — pure SQLite FTS5.

```
Input:  { "query": "handle user authentication", "kinds": "function,method", "limit": 10 }
Output: { "results": [
            { "id": "auth.create_token", "kind": "function",
              "signature": "def create_token(user_id: int) -> str",
              "docstring": "Creates a signed JWT for the given user.",
              "file": "auth.py", "line_start": 42, "score": -2.31 }
          ] }
```

### `get_symbol`

Fetch full details for a specific symbol by ID, including its source code and all direct edges.

```
Input:  { "symbol_id": "auth.create_token" }
Output: { "id": "auth.create_token", "kind": "function",
          "file": "auth.py", "line_start": 42, "line_end": 61,
          "signature": "def create_token(user_id: int) -> str",
          "docstring": "Creates a signed JWT for the given user.",
          "source": "def create_token(user_id: int) -> str:\n    ...",
          "edges_out": [{ "dst": "crypto.sign", "kind": "calls", "confidence": 1.0 }],
          "edges_in": [{ "src": "views.login_handler", "kind": "calls", "confidence": 1.0 }] }
```

### `project_map`

High-level overview: modules, entry points (zero callers), hot symbols (highest in-degree), test coverage ratio, and graph statistics.

```
Input:  { "project_root": "/path/to/project" }   (optional)
Output: { "modules": [{ "name": "auth", "file": "auth.py", "functions": 8, "classes": 2 }],
          "entry_points": ["views.login_handler", "views.logout_handler"],
          "hot_symbols": [{ "id": "auth.create_token", "in_degree": 14 }],
          "test_coverage": { "total_public_symbols": 42, "symbols_with_assertions": 28,
                             "coverage_pct": 66.7 },
          "graph_stats": { "total_nodes": 342, "total_edges": 891,
                           "edge_breakdown": { "calls": 610, "mutates": 180, ... } } }
```

### `find_mutations`

Given a field or variable, find all functions that mutate it.

```
Input:  { "symbol_id": "Session.token" }
Output: { "target": "Session.token",
          "mutated_by": [
            { "symbol": "auth.create_token", "file": "auth.py",
              "line_start": 42, "confidence": 1.0 }
          ] }
```

---

## Graph Schema

### Edge Kinds

| Kind | Meaning | Example |
|------|---------|---------|
| `calls` | Function A calls function B | `login_handler` -> `create_token` |
| `imports` | Module A imports symbol B | `views` -> `auth.create_token` |
| `mutates` | Function A writes to field/variable B | `Session.save` -> `Session.token` |
| `asserts_on` | Test function A asserts on symbol B | `test_login` -> `create_token` |
| `side_effect` | Function A performs external I/O | `read_config` -> `side_effect:file_io:open` |
| `inherits` | Class A inherits from class B | `Admin` -> `User` |
| `overrides` | Method A overrides method B | `Admin.deactivate` -> `User.deactivate` |

### Confidence Tiers

Every call edge carries a confidence score indicating how it was resolved:

| Score | Resolution Method | Meaning |
|-------|------------------|---------|
| 1.0 | Same-file match | Callee defined in the same file |
| 0.8 | Import resolved | Callee traced through import statements |
| 0.5 | jedi resolved | jedi type inference resolved the method receiver |
| 0.3 | Unresolved best-guess | Name matched but couldn't be definitively resolved |

### Side-Effect Categories

| Category | Triggers |
|----------|----------|
| `file_io` | `open()`, `os.path.*`, `pathlib.*` |
| `network` | `requests.*`, `httpx.*`, `urllib.*`, `aiohttp.*` |
| `cache` | `redis.*`, `memcache.*` |
| `process` | `subprocess.*`, `os.system()` |

---

## Architecture

```
causal-graph-mcp/
├── src/
│   └── causal_graph_mcp/
│       ├── __init__.py          # Package init, version
│       ├── __main__.py          # python -m entry point
│       ├── server.py            # FastMCP server, 7 tool handlers, auto-index + watcher
│       ├── indexer.py           # File discovery, .gitignore, SHA-256 change detection
│       ├── parser.py            # AST extraction: nodes + all 7 edge kinds
│       ├── resolver.py          # jedi-based call resolution, confidence upgrades
│       ├── storage.py           # SQLite schema, CRUD, FTS5, atomic transactions
│       ├── graph.py             # BFS traversal, cycle detection, direction control
│       ├── risk.py              # Risk scoring formula for impact_analysis
│       └── watcher.py           # watchdog file observer, 300ms debounce
├── tests/
│   ├── test_storage.py          # 14 tests — schema, CRUD, FTS5, transactions
│   ├── test_parser.py           # 15 tests — all node types and edge kinds
│   ├── test_resolver.py         #  7 tests — confidence rules, jedi resolution
│   ├── test_indexer.py          # 11 tests — discovery, change detection, pipeline
│   ├── test_graph.py            #  6 tests — traversal, hops, cycles
│   ├── test_risk.py             #  5 tests — risk formula, sorting, summary
│   ├── test_server.py           #  9 tests — all 7 tool handlers + truncation
│   ├── test_watcher.py          #  3 tests — file detection, filtering, debounce
│   └── test_integration.py      #  5 tests — end-to-end done criteria
├── pyproject.toml
├── .gitignore
└── README.md
```

### Module Responsibilities

| Module | Lines | Purpose |
|--------|-------|---------|
| `storage.py` | 411 | SQLite schema (nodes, edges, file_hashes), CRUD operations, FTS5 full-text search, atomic file-scoped transactions with rollback |
| `parser.py` | 579 | AST extraction using `ast.parse()`. Extracts function/method/class/variable nodes and all 7 edge kinds. Reconstructs signatures, computes body hashes |
| `indexer.py` | 252 | File discovery with `.gitignore` support, SHA-256 change detection, orchestrates parse -> resolve -> store pipeline |
| `resolver.py` | 174 | Post-processing step using `jedi.Script.goto()` to upgrade unresolved call edges (0.3 -> 0.5). Caches scripts per file, never crashes |
| `graph.py` | 113 | BFS traversal with direction control (callers/callees/both), max_hops limit, min_confidence filtering, cycle detection via visited set |
| `risk.py` | 158 | Risk scoring for impact_analysis. Computes weighted risk using assertion, side-effect, and public API multipliers. Sorts and classifies results |
| `server.py` | 301 | FastMCP server with 7 tool handlers registered via `@server.tool()`. Auto-indexes on startup, starts file watcher, caps responses at ~8K tokens |
| `watcher.py` | 100 | watchdog `FileSystemEventHandler` with 300ms debounce. Collects `.py` changes, batches them, triggers incremental re-index via callback |

---

## Test Suite

**75 tests** across 9 test files, running in ~5 seconds.

### Storage Tests (14 tests) — `test_storage.py`

| Test | What It Verifies |
|------|-----------------|
| `test_tables_exist` | nodes, edges, file_hashes tables created |
| `test_fts_table_exists` | FTS5 virtual table for full-text search |
| `test_indexes_exist` | idx_edges_src, idx_edges_dst, idx_edges_kind |
| `test_upsert_and_get` | Insert 3 nodes, retrieve by ID |
| `test_get_nonexistent` | Returns None for missing nodes |
| `test_upsert_replaces` | INSERT OR REPLACE updates existing nodes |
| `test_insert_and_query_edges` | Insert edges, query by direction (out/in/both) |
| `test_all_edge_kinds` | All 7 edge kinds stored and retrieved |
| `test_update_replaces_file_data` | File-scoped delete + reinsert works |
| `test_update_atomicity` | Rollback on mid-transaction failure preserves data |
| `test_basic_search` | FTS5 BM25 search returns ranked results |
| `test_search_with_kind_filter` | Kind filter restricts results |
| `test_search_limit` | Limit parameter caps result count |
| `test_get_stats` | Correct node/edge counts and edge breakdown |

### Parser Tests (15 tests) — `test_parser.py`

| Test | What It Verifies |
|------|-----------------|
| `test_extract_functions` | Function nodes with signature, docstring, body_hash |
| `test_extract_classes_and_methods` | Class + method nodes with correct IDs |
| `test_extract_module_variables` | Module-level Assign and AnnAssign |
| `test_async_function` | `async def` extracted as kind="function" |
| `test_is_public` | `_private` names get `is_public=0` |
| `test_same_file` | Call edge with confidence 1.0 for same-file callee |
| `test_imported` | Call edge with confidence 0.8 for imported callee |
| `test_unresolved` | Call edge with confidence 0.3 for unknown callee |
| `test_self_mutation` | `self.x = ...` produces mutates edge |
| `test_global_mutation` | Module-level variable reassignment in function |
| `test_assertion_edges` | `assert` and `assertEqual` produce asserts_on edges |
| `test_side_effect_edges` | `open()`, `requests.get()`, `subprocess.run()` detected |
| `test_import_edges` | `import` and `from...import` produce edges |
| `test_inheritance` | `class Child(Parent)` produces inherits edge |
| `test_override` | Child method overriding parent produces overrides edge |

### Resolver Tests (7 tests) — `test_resolver.py`

| Test | What It Verifies |
|------|-----------------|
| `test_same_file_untouched` | Confidence 1.0 edges never modified |
| `test_import_untouched` | Confidence 0.8 edges never modified |
| `test_non_call_edges_untouched` | Mutation/assertion edges never modified |
| `test_method_receiver_resolution` | jedi resolves typed method calls to 0.5 |
| `test_unresolvable_stays_low` | Unknown symbols stay at 0.3 |
| `test_bad_file_no_crash` | Non-existent file doesn't crash |
| `test_empty_edges` | Empty input returns empty output |

### Indexer Tests (11 tests) — `test_indexer.py`

| Test | What It Verifies |
|------|-----------------|
| `test_discover_files` | Finds .py files, skips __pycache__/.venv/.git |
| `test_gitignore_respected` | .gitignore patterns exclude matching files |
| `test_skips_unchanged` | Re-index skips files with same SHA-256 |
| `test_detects_changes` | Modified files are re-parsed |
| `test_indexes_project` | Full pipeline produces correct node/edge counts |
| `test_index_files_incremental` | Indexes only specified files |
| `test_empty_project` | Empty directory produces zero counts |
| `test_simple_file` | `main.py` -> `"main"` |
| `test_nested_file` | `src/auth/utils.py` -> `"src.auth.utils"` |
| `test_init_file` | `src/auth/__init__.py` -> `"src.auth"` |
| `test_top_level_init` | `pkg/__init__.py` -> `"pkg"` |

### Graph Tests (6 tests) — `test_graph.py`

| Test | What It Verifies |
|------|-----------------|
| `test_callees_traversal` | A->B->C chain, callees direction |
| `test_callers_traversal` | Reverse traversal via callers direction |
| `test_both_directions` | Both directions from middle node |
| `test_max_hops_limit` | Traversal stops at max_hops |
| `test_min_confidence_filter` | Low-confidence edges excluded |
| `test_cycle_detection` | A->B->C->A cycle detected, no infinite loop |

### Risk Tests (5 tests) — `test_risk.py`

| Test | What It Verifies |
|------|-----------------|
| `test_basic_risk_score` | Formula: (1/1) * 1.5 * 1.0 * 1.5 = 2.25 |
| `test_side_effect_weight` | Side-effect edges apply 1.5x multiplier |
| `test_public_api_weight` | Private symbols get 1.0x (not 1.5x) |
| `test_risk_sorting` | Results sorted by risk score descending |
| `test_summary_counts` | high/medium/low/tests_at_risk counts correct |

### Server Tests (9 tests) — `test_server.py`

| Test | What It Verifies |
|------|-----------------|
| `test_index_project_tool` | Re-index skips unchanged files |
| `test_get_call_graph_tool` | Returns callers with correct node IDs |
| `test_impact_analysis_tool` | Returns at_risk with summary dict |
| `test_semantic_search_tool` | BM25 search finds token-related functions |
| `test_get_symbol_tool` | Returns node details with source code |
| `test_project_map_tool` | Returns nodes and graph stats |
| `test_find_mutations_tool` | Finds Session.save as mutator of Session.token |
| `test_small_response_unchanged` | Small responses pass through |
| `test_large_response_truncated` | Large responses truncated with flag |

### Watcher Tests (3 tests) — `test_watcher.py`

| Test | What It Verifies |
|------|-----------------|
| `test_detects_py_changes` | .py modifications trigger callback |
| `test_ignores_non_py` | .json files don't trigger callback |
| `test_debounce` | Rapid writes batched into fewer callbacks |

### Integration Tests (5 tests) — `test_integration.py`

These are the **done criteria** — end-to-end tests against a 10-file Python project:

| Test | Done Criterion |
|------|---------------|
| `test_dc1_index_and_call_graph` | Index ~10 files, query call graph, verify callers/callees with confidence scores |
| `test_dc2_incremental_reindex` | Modify a file, wait >300ms, verify new function appears in graph via file watcher |
| `test_dc3_causal_impact_analysis` | Run impact_analysis on function with mutation + assertion edges, verify risk scores weight causal edges |
| `test_dc4_semantic_search` | Search by docstring keyword, verify BM25-ranked results |
| `test_dc5_project_map` | Verify entry points (zero callers), hot symbols, test coverage ratio |

---

## Design Decisions

1. **SQLite over in-memory graph** — Persistence across server restarts. No re-index for unchanged files. FTS5 full-text search included for free. Single `.causal-graph/index.db` file, gitignored.

2. **ast + jedi dual resolution** — `ast` handles the 80% case (same-file definitions, direct imports). `jedi` handles the 20% (method receivers, duck typing, import alias chains). Confidence scores make the boundary explicit rather than silently wrong.

3. **Delete-and-reinsert for incremental updates** — On file change, delete all nodes/edges for that file and re-extract. Simpler than diffing ASTs, and fast enough for single-file updates wrapped in a transaction.

4. **Lazy cleanup for deleted files** — The watcher doesn't re-index on deletion events. Stale nodes are removed when encountered during queries. Reduces watcher complexity.

5. **FTS5 BM25 over embeddings** — No external embedding model, no vector store, no API calls. SQLite FTS5 provides BM25 ranking on symbol names, signatures, and docstrings. Sufficient for symbol discovery.

6. **8K token budget cap** — Every tool response is capped at ~32,000 characters (~8,000 tokens). Truncation is explicit via a `"truncated": true` flag. Prevents blowing up Claude Code's context window.

7. **Confidence scoring tiers** — 1.0 / 0.8 / 0.5 / 0.3. Makes resolution uncertainty visible to consumers. Low-confidence edges are included but clearly marked, rather than silently omitted or silently trusted.

8. **`check_same_thread=False` for SQLite** — The file watcher runs in a background thread and needs to write to the same database. WAL journal mode + disabled thread checking enables safe concurrent access.

9. **No `from __future__ import annotations`** in server.py — FastMCP uses `issubclass()` on parameter annotations at decoration time. Stringified annotations (from the future import) break this introspection.

---

## Configuration

Optional `.causal-graph/config.json` at project root:

```json
{
  "exclude_patterns": ["migrations/", "generated/"],
  "max_hops_default": 3,
  "min_confidence_default": 0.5,
  "debounce_ms": 300,
  "token_budget": 8000
}
```

---

## Limitations

- **Python only** — no support for other languages.
- **Dynamic dispatch** — duck-typed interfaces and metaprogramming-generated callables will be missed. The server annotates symbols with unresolvable callers rather than silently under-reporting.
- **No cross-project resolution** — edges to symbols in external packages (e.g., `requests.get`) are stored but not resolved to their source definitions.
- **No incremental edge cleanup on delete** — deleted files' nodes/edges are cleaned up lazily, not immediately.
