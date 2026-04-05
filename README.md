# causal-graph-mcp

> A Python MCP server that indexes a Python codebase into a persistent causal dependency graph (SQLite), exposing structural queries as MCP tools for Claude Code.

**Type:** Utility Tool
**Skill Loadout:** Light PAUL (1 milestone, 3 phases)
**Quality Gates:** 5 done criteria

---

## Overview

Existing code-graph tools track only call and import edges, so impact analysis answers "what's reachable" but not "what will actually break." causal-graph-mcp adds mutation, assertion, and side-effect edges to produce genuinely accurate breakage predictions before Claude Code makes multi-file edits.

The server runs as a local stdio process with zero external infrastructure. A background file watcher (watchdog, 300ms debounce) triggers incremental re-indexing on every save, so the graph stays current. Call resolution uses both `ast` and `jedi` for type-aware method receiver resolution, with confidence scores on every edge.

---

## Location

Standalone repository, distributed as a pip-installable package. Registered as an MCP server via `claude mcp add` or `mcp.json`.

---

## Interface

**Invocation:** MCP server over stdio (JSON-RPC 2.0)

```bash
claude mcp add causal-graph-mcp -- python -m causal_graph_mcp.server
```

On startup, the server indexes the working directory and starts the file watcher automatically.

### Tools

| Tool | Purpose | Key Input |
|------|---------|-----------|
| `index_project` | Full or incremental index | `project_root` |
| `get_call_graph` | Trace callers/callees with confidence | `symbol`, `direction`, `max_hops` |
| `impact_analysis` | Ranked breakage risk using causal edges | `symbol`, `max_hops` |
| `semantic_search` | BM25 search over symbols/docstrings | `query`, `kinds?`, `limit?` |
| `get_symbol` | Full symbol details + all edges + source | `symbol_id` |
| `project_map` | Entry points, hot symbols, test coverage | `project_root` |
| `find_mutations` | All functions that mutate a field/variable | `symbol_id` |

---

## Stack

- **Language:** Python 3.11+
- **MCP transport:** stdio, JSON-RPC 2.0 (`mcp` Python SDK)
- **AST parsing:** Python stdlib `ast`
- **Call resolution:** `jedi` (type-aware method receiver resolution)
- **Storage:** SQLite via `sqlite3` (single `.causal-graph/index.db` file)
- **File watching:** `watchdog` (background thread, 300ms debounce)
- **Change detection:** SHA-256 content hashing per file
- **Runtime deps:** `mcp`, `jedi`, `watchdog` — nothing else

---

## Architecture

```
src/causal_graph_mcp/
├── server.py         # MCP entry point, tool registry
├── indexer.py        # File discovery, change detection, orchestration
├── parser.py         # AST extraction: nodes + all edge kinds
├── resolver.py       # jedi call resolution, confidence scoring
├── storage.py        # SQLite schema, CRUD, FTS5, transactions
├── graph.py          # BFS/DFS traversal, cycle detection, SCC
├── risk.py           # Risk scoring formula for impact_analysis
└── watcher.py        # watchdog observer, 300ms debounce
```

### Graph Schema

**Nodes:** `id` (module.qualified_name), `kind` (function|method|class|variable|import), `module`, `file`, `line_start`, `line_end`, `signature`, `docstring`, `is_public`, `is_test`, `body_hash`

**Edges:** `src`, `dst`, `kind` (calls|imports|mutates|asserts_on|side_effect|inherits|overrides), `confidence` (0.0–1.0), `weight`, `scope`, `detail` (JSON)

### Edge Kinds

| Kind | Meaning |
|------|---------|
| `calls` | Function A calls function B |
| `imports` | Module A imports symbol B |
| `mutates` | Function A writes to field/variable B |
| `asserts_on` | Test function A asserts on symbol B |
| `side_effect` | Function A performs external I/O (file, network, cache, process) |
| `inherits` | Class A inherits from class B |
| `overrides` | Method A overrides method B |

### Confidence Tiers

| Score | Resolution Method |
|-------|------------------|
| 1.0 | Same-file resolved |
| 0.8 | Import resolved |
| 0.5 | jedi resolved (dynamic dispatch) |
| 0.3 | Unresolved best-guess |

### Risk Score Formula (impact_analysis)

```
risk = (1 / distance) * assertion_weight * side_effect_weight * public_api_weight
assertion_weight  = 1 + 0.5 * len(asserts_on edges)
side_effect_weight = 1.5 if has side_effect edges else 1.0
public_api_weight  = 1.5 if is_public else 1.0
```

---

## Design Decisions

1. **SQLite over in-memory graph** — persistence across restarts, no re-index for unchanged files, FTS5 search for free
2. **ast + jedi dual resolution** — ast handles 80% (same-file, direct imports), jedi handles 20% (method receivers, duck typing), confidence scores make the boundary explicit
3. **Delete-and-reinsert for incremental updates** — simpler than AST diffing, fast enough for single-file transactions
4. **Lazy cleanup for deleted files** — reduces watcher complexity; stale nodes removed when encountered during queries
5. **FTS5 BM25 over embeddings** — zero external model dependency, trivial to maintain, sufficient for symbol/docstring search
6. **8K token budget cap** — prevents blowing up Claude Code context; truncation is explicit via `"truncated": true`
7. **Confidence scoring tiers** — makes uncertainty visible to consumers rather than silently under-reporting

---

## Implementation Phases

### Phase 1: Core Graph Construction
Storage (SQLite schema, CRUD, FTS5) + parser (AST extraction for all edge kinds) + indexer (file discovery, change detection, orchestration)

### Phase 2: Query Tools + MCP Server
Graph traversal (BFS/DFS, cycle detection, SCC) + risk scoring + all 7 MCP tool handlers + server entry point

### Phase 3: File Watcher + Integration
watchdog observer with 300ms debounce + incremental re-indexing + integration tests against all 5 done criteria

---

## Done Criteria

- [ ] Index a ~10-file project, query `get_call_graph` — correct callers/callees with confidence scores
- [ ] Modify a file, wait >300ms, query `get_symbol` — graph reflects change without manual re-index
- [ ] `impact_analysis` on function with mutation + assertion edges — risk scores weight causal edges correctly
- [ ] `semantic_search` by docstring keyword — returns ranked BM25 results
- [ ] `project_map` on project with tests — correct entry points, hot symbols, test coverage ratio

---

## Open Questions

None — specification is fully resolved.

---

## References

- Full system specification: `projects/SPEC.md`
- MCP Python SDK: `mcp` package
- Prior art: `code-graph-mcp` (call/import edges only)
