# causal-graph-mcp

## The Problem

Claude Code is powerful at editing code, but it's flying blind when it comes to **understanding what will break**. When you ask it to change a function, it doesn't know:

- What other functions call it
- Which tests will fail
- Whether it mutates shared state that other code reads
- Whether callers have side effects like network calls or file writes that make breakage more costly

Existing code-graph tools (like `code-graph-mcp`) only track **call and import edges** — they can tell you "what's reachable from this function" but not "what will actually break." A function might be reachable via 3 call hops, but if nothing along that path asserts on it or mutates shared state, it's probably fine. Reachability is not risk.

Without this context, Claude Code either makes changes conservatively (touching less than it should) or confidently (breaking things it didn't know depended on the change). Both waste your time.

## What This Solves

causal-graph-mcp gives Claude Code a **pre-computed causal dependency graph** of your Python codebase before it makes multi-file edits.

**A causal dependency graph** maps not just *what calls what*, but *what causes what to change*. In a regular call graph, an edge from A to B means "A calls B." In a causal graph, edges also capture:

- A **writes to** a field that B **reads** (mutation edge) — changing A's write logic breaks B's assumptions
- A **test asserts on** B's output (assertion edge) — changing B will fail that test
- A performs **I/O as a consequence** of calling B (side-effect edge) — a breakage here isn't just a failed test, it's a failed API call or corrupted file

The "causal" part means: if you change node X, you can trace forward through these edges to find everything that will *actually be affected* — not just everything that's syntactically connected. A function 4 hops away via call edges might have zero risk if nothing along that path asserts on it or shares state with it. A function 1 hop away with an assertion edge and a side-effect is critical.

The graph tracks these specific edge types:

- **Mutation edges** — what state a function writes (`self.token = ...`), so you know who's reading that state downstream
- **Assertion edges** — what tests assert on which symbols, so you know exactly which tests will fail
- **Side-effect edges** — file I/O, network calls, cache writes, subprocess invocations — so you know which breakages have real-world consequences
- **Inheritance and override edges** — class hierarchies and method overrides, so polymorphic dispatch is visible

This means `impact_analysis` can answer: *"If I change `auth.create_token`, then `test_auth.test_login` will fail (it asserts on the return value), `Session.save` is at risk (it mutates `Session.token` using the output), and `views.login_handler` has a network side-effect in its caller chain (higher cost if broken)."*

That's not reachability. That's a ranked risk assessment with causal evidence.

The server runs locally over stdio with zero infrastructure — a single SQLite file, three pip dependencies, and a file watcher that keeps the graph current on every save.

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

## Usage Guide

Once registered, start a new Claude Code session in any Python project. The server auto-indexes on first use and keeps the graph current via file watcher — you don't need to think about it after setup.

### Onboarding to an unfamiliar codebase

When you first open a project you've never seen before:

```
> "Use project_map to give me an overview of this codebase"
```

This returns the module structure, entry points (functions nobody calls — likely CLI commands, API handlers, or main functions), the hottest symbols (most depended-on), and what percentage of public functions have test assertions covering them. It's the fastest way to understand what a project does and where to start reading.

```
> "Search for functions related to payment processing"
```

Semantic search uses BM25 over function names, signatures, and docstrings. When you know *what* you're looking for but not *where* it lives, this is faster than grep because it ranks by relevance across all three fields.

### Before making a change

This is the primary use case. Before you edit a function that other code depends on:

```
> "Run impact analysis on billing.charge_customer"
```

This traces all callers, but unlike a simple call graph, it also checks:
- Which tests assert on `charge_customer` (those will fail)
- Whether `charge_customer` mutates shared state (other readers of that state are at risk)
- Whether callers have side effects like network calls or file writes (those are higher-risk breakages)

Each downstream symbol gets a risk score. High-risk items are the ones you need to check before committing.

```
> "Get the call graph for billing.charge_customer, callers only, 4 hops"
```

When you want the raw dependency tree without risk scoring — useful for understanding the call chain structure before deciding what to refactor.

### During a refactoring

When you're renaming a method, changing a return type, or restructuring a class:

```
> "Show me the symbol details for models.Order.calculate_total"
```

This returns the full source code of the function, its signature, docstring, and every edge — who calls it, what it calls, what state it mutates, which tests assert on it, and any side effects. All in one response.

```
> "Find all mutations of Order.total_amount"
```

When you're changing a field and need to know every function that writes to it. Mutation tracking catches `self.total_amount = ...` assignments across the entire codebase, so you don't miss a setter hiding in a different module.

### Investigating a bug

When a test fails and you need to trace the cause:

```
> "Get the call graph for test_checkout.test_discount_applied, callees, 5 hops"
```

Follow the callees of a failing test to see the full execution path — what does the test call, what do those functions call, and where might the breakage be? Confidence scores help you distinguish between definitely-called functions (1.0) and dynamically-dispatched ones (0.5) that might not be on the actual execution path.

```
> "Search for functions related to discount"
> "Run impact analysis on pricing.apply_discount"
```

Combine search to find the suspect function, then impact analysis to see everything downstream. If the bug is in `apply_discount`, impact analysis tells you which other tests *should* also be failing — and if they're not, that's a clue about what's different.

### Reviewing a PR

When reviewing someone else's changes, ask about the files they touched:

```
> "Run impact analysis on auth.refresh_token"
> "Find all mutations of Session.expires_at"
```

Impact analysis on the changed functions tells you what the reviewer should be checking. Mutation tracking on modified fields surfaces hidden coupling the PR author might not have considered.

### Keeping the graph current

The file watcher handles this automatically — every time you save a `.py` file, the graph re-indexes that file within 300ms. You only need manual re-indexing after:

- First-time setup (happens automatically on first tool call)
- Pulling a large upstream diff with many changed files
- Adding a new directory that wasn't being watched

```
> "Re-index the project"
```

### Tool reference

| When you need to... | Use this tool | Example prompt |
|---|---|---|
| Orient yourself in a new codebase | `project_map` | *"Give me a project overview"* |
| Find a function by concept | `semantic_search` | *"Search for error handling functions"* |
| Understand one function in depth | `get_symbol` | *"Show me details for auth.create_token"* |
| See the dependency tree | `get_call_graph` | *"Call graph for login_handler, callers, 3 hops"* |
| Know what will break before editing | `impact_analysis` | *"Impact analysis on charge_customer"* |
| Track who writes to a field | `find_mutations` | *"Find mutations of User.email"* |
| Force a full re-index | `index_project` | *"Re-index the project"* |
| Find cross-language API dependencies | `cross_language_edges` | *"Detect cross-language edges"* |

---

## Multi-Language Support

The graph indexes 6 languages, not just Python. Each language gets full structural analysis — functions, classes, call edges, mutation edges, side-effect edges, imports, and inheritance.

| Language | Parser | Call Resolution |
|----------|--------|----------------|
| Python | `ast` + `jedi` | Type-aware (jedi resolves method receivers) |
| JavaScript | tree-sitter | AST-only (same-file + import resolution) |
| TypeScript | tree-sitter | AST-only |
| Go | tree-sitter | AST-only |
| Rust | tree-sitter | AST-only |
| Java | tree-sitter | AST-only |

Python gets the highest-fidelity resolution because `jedi` does type inference. The other languages use tree-sitter for parsing and resolve calls against same-file definitions and import statements. Unresolved calls get confidence 0.3 — they're included in the graph but flagged as uncertain.

All the tools work the same across languages. `impact_analysis` on a Go function works exactly like it does on a Python function — same risk formula, same edge types, same output.

### Cross-Language Edge Detection

In multi-language codebases, the most common dependency between languages is **one service calling another's API**. The `cross_language_edges` tool detects these by matching REST route definitions against HTTP client calls.

```
> "Detect cross-language edges"
```

It works by scanning the indexed graph for two patterns:

**Route definitions** — framework-specific decorators and method calls that register HTTP endpoints:

| Framework | Pattern |
|-----------|---------|
| Flask / FastAPI | `@app.get("/api/users")` |
| Express | `app.get("/api/users", handler)` |
| Gin (Go) | `r.GET("/api/users", handler)` |
| Spring (Java) | `@GetMapping("/api/users")` |

**HTTP client calls** — fetch, axios, requests, or language-specific HTTP libraries:

| Client | Pattern |
|--------|---------|
| JS/TS fetch | `fetch("/api/users")` |
| JS/TS axios | `axios.get("/api/users")` |
| Python requests | `requests.get("https://host/api/users")` |
| Go net/http | `http.Get("https://host/api/users")` |

When a client URL matches a route definition (handling path parameters like `/users/:id` and stripping hostnames), it creates a cross-language edge with confidence ~0.7.

This means if you change a Python Flask handler, `impact_analysis` can trace across the language boundary and tell you which JavaScript functions call that endpoint.

**What it doesn't catch:**
- Dynamic URLs built from variables (`fetch(baseUrl + path)`)
- gRPC/protobuf service calls (planned)
- Database-level dependencies (Go writes a table, Python reads it — planned)
- Message queue pub/sub connections (planned)

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

- **Dynamic dispatch** — duck-typed interfaces and metaprogramming-generated callables will be missed. The server annotates symbols with unresolvable callers rather than silently under-reporting.
- **No cross-project resolution** — edges to symbols in external packages (e.g., `requests.get`) are stored but not resolved to their source definitions.
- **No incremental edge cleanup on delete** — deleted files' nodes/edges are cleaned up lazily, not immediately.
- **Tree-sitter languages lack type-aware resolution** — JS/TS/Go/Rust/Java use AST-only call resolution (no equivalent of jedi). Unresolved calls get confidence 0.3. Adding language servers (tsserver, gopls, rust-analyzer) would upgrade these.
- **Cross-language detection is URL-pattern only** — only catches REST API calls via string matching. Dynamic URLs, gRPC, database sharing, and message queues aren't detected yet.

---

## Roadmap

This is where the project is headed. Contributions welcome — each item is a self-contained piece of work.

### More languages

| Language | Status | What's needed |
|----------|--------|---------------|
| Python | Full support | ast + jedi, all edge types |
| JavaScript | Parsing done | Add tsserver for type-aware call resolution |
| TypeScript | Parsing done | Add tsserver for type-aware call resolution |
| Go | Parsing done | Add gopls for cross-package call resolution |
| Rust | Parsing done | Add rust-analyzer for trait method resolution |
| Java | Parsing done | Add Eclipse JDT or java LSP for type resolution |
| C# | Not started | tree-sitter-c-sharp grammar exists, needs parser config |
| Ruby | Not started | tree-sitter-ruby grammar exists |
| PHP | Not started | tree-sitter-php grammar exists |
| Kotlin | Not started | tree-sitter-kotlin grammar exists |
| Swift | Not started | tree-sitter-swift grammar exists |

Adding a new language is mostly configuration — define the node types, side-effect patterns, test patterns, and import parsing for the `TreeSitterParser`. The tree-sitter grammar does the heavy lifting.

### Better cross-language detection

The current REST route matching covers the most common case (frontend calls backend), but real multi-language codebases have more integration patterns:

| Integration | Approach | Confidence | Status |
|-------------|----------|------------|--------|
| REST APIs | URL pattern matching | ~0.7 | Done |
| gRPC / Protobuf | Parse `.proto` files, match service methods to generated stubs | ~0.9 | Planned |
| GraphQL | Parse `.graphql` schemas, match query/mutation names to resolvers | ~0.85 | Planned |
| Database tables | Parse SQL migrations + ORM models, match table read/write across languages | ~0.7 | Planned |
| Message queues | Match Kafka topic names, RabbitMQ exchanges, Celery task names across languages | ~0.65 | Planned |
| Shared config | Trace env var references across docker-compose, k8s manifests, and source | ~0.5 | Planned |
| FFI / bindings | Detect `ctypes`, `extern "C"`, cgo `import "C"`, match symbol names | ~0.75 | Planned |
| OpenTelemetry | Runtime trace collection to validate static edges with ground truth | ~1.0 | Planned |

**gRPC/Protobuf** is the highest-value next step — `.proto` files are explicit contracts with typed service definitions, so matching is nearly exact. A Python gRPC client calling a Go gRPC server through a shared `.proto` would produce a confidence 0.9 edge.

**OpenTelemetry** is the long-term play — instrument services with OTel, run integration tests, collect distributed traces, and use the trace data to validate and improve confidence scores on statically-detected edges. A static edge confirmed by a runtime trace gets upgraded to confidence 1.0.

### Type-aware resolution for non-Python languages

Right now, only Python gets type-aware call resolution via jedi. The other languages resolve calls against same-file definitions and imports, leaving cross-file method calls at confidence 0.3. Adding language server integration would fix this:

| Language | Language Server | What it unlocks |
|----------|----------------|-----------------|
| JS/TS | tsserver | Resolve `obj.method()` to the correct class definition across files |
| Go | gopls | Resolve interface method calls, cross-package imports |
| Rust | rust-analyzer | Resolve trait method implementations, generic type calls |
| Java | Eclipse JDT LS | Resolve inheritance hierarchies, interface implementations |

The architecture is ready for this — `resolver.py` is a post-processing step that takes edges and upgrades confidence. Each language would get its own resolver that follows the same pattern: take 0.3-confidence edges, ask the language server, upgrade to 0.5 if resolved.

### Other ideas

- **Monorepo support** — detect service boundaries automatically from build configs (Bazel, Nx, Lerna) and scope indexing per service
- **Git-aware impact analysis** — combine the causal graph with `git diff` to automatically run impact analysis on every changed function in a PR
- **Embedding-based search** — augment FTS5 BM25 with vector embeddings for semantic concept search ("find the rate limiter" when no function mentions "rate limit" in its name)
- **Visualization** — export the graph as DOT or D3 JSON for interactive dependency visualization
- **CI integration** — run as a GitHub Action that comments on PRs with impact analysis results
