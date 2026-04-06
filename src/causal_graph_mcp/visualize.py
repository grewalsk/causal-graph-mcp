"""Graph visualization: tree format and mermaid diagram rendering."""

from __future__ import annotations

from typing import Any

from causal_graph_mcp.graph import SubgraphResult
from causal_graph_mcp.risk import ImpactResult


def render_tree(subgraph: SubgraphResult) -> str:
    """Render a subgraph as an indented tree with edge metadata.

    Example output:
        auth.create_token (root)
        ├── ← views.login_handler [calls, 1.0]
        │   └── ← test_views.test_login [asserts_on, 0.8] ⚠ TEST
        ├── → hashlib.sha256 [calls, 0.8]
        └── → Session.token [mutates, 1.0]
    """
    if not subgraph.nodes:
        return f"{subgraph.root} (no connections found)"

    root_id = subgraph.root

    # Build adjacency lists from edges
    # callers: nodes that have edges pointing TO root (or intermediate nodes)
    # callees: nodes that have edges pointing FROM root (or intermediate nodes)
    children_in: dict[str, list[dict[str, Any]]] = {}   # node → [{neighbor, edge}]
    children_out: dict[str, list[dict[str, Any]]] = {}

    for edge in subgraph.edges:
        src, dst = edge["src"], edge["dst"]
        kind = edge.get("kind", "calls")
        conf = edge.get("confidence", 1.0)
        hops = edge.get("hops", 1)

        # Inbound edge: someone calls/depends on this node
        children_in.setdefault(dst, []).append({
            "neighbor": src, "kind": kind, "confidence": conf, "hops": hops,
        })
        # Outbound edge: this node calls/depends on something
        children_out.setdefault(src, []).append({
            "neighbor": dst, "kind": kind, "confidence": conf, "hops": hops,
        })

    # Build node metadata lookup
    node_meta: dict[str, dict[str, Any]] = {}
    for n in subgraph.nodes:
        node_meta[n["id"]] = n

    # Collect direct children of root, deduplicating by (direction, neighbor, kind)
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    # Inbound edges (callers)
    for item in children_in.get(root_id, []):
        key = ("←", item["neighbor"], item["kind"])
        if key not in seen:
            seen.add(key)
            entries.append({"direction": "←", **item})

    # Outbound edges (callees)
    for item in children_out.get(root_id, []):
        key = ("→", item["neighbor"], item["kind"])
        if key not in seen:
            seen.add(key)
            entries.append({"direction": "→", **item})

    # Sort: inbound first, then outbound, then by confidence desc
    entries.sort(key=lambda x: (0 if x["direction"] == "←" else 1, -x["confidence"]))

    lines: list[str] = []
    lines.append(f"{root_id} (root)")

    for i, entry in enumerate(entries):
        is_last = i == len(entries) - 1
        prefix = "└── " if is_last else "├── "
        child_prefix = "    " if is_last else "│   "

        neighbor = entry["neighbor"]
        meta = node_meta.get(neighbor, {})
        flags = _build_flags(meta)

        lines.append(
            f"{prefix}{entry['direction']} {neighbor} "
            f"[{entry['kind']}, {entry['confidence']}]{flags}"
        )

        # Add second-level children (hop 2)
        sub_entries = _get_sub_entries(neighbor, root_id, children_in, children_out)
        for j, sub in enumerate(sub_entries):
            sub_is_last = j == len(sub_entries) - 1
            sub_prefix = "└── " if sub_is_last else "├── "
            sub_meta = node_meta.get(sub["neighbor"], {})
            sub_flags = _build_flags(sub_meta)

            lines.append(
                f"{child_prefix}{sub_prefix}{sub['direction']} {sub['neighbor']} "
                f"[{sub['kind']}, {sub['confidence']}]{sub_flags}"
            )

    if subgraph.cycles_detected:
        lines.append("")
        lines.append(f"⚠ Cycles detected: {len(subgraph.cycles_detected)}")
        for cycle in subgraph.cycles_detected[:3]:
            lines.append(f"  {' → '.join(cycle)}")

    return "\n".join(lines)


def render_impact_tree(impact: ImpactResult) -> str:
    """Render impact analysis results as a risk-annotated tree.

    Example output:
        auth.create_token (changing)
        ┌─ HIGH RISK ──────────────────
        │ ▲ 2.25  test_auth.test_login [asserts_on, mutates:Session.token] ⚠ TEST
        │ ▲ 1.50  views.login_handler [calls] → side_effect:network
        ├─ MEDIUM RISK ────────────────
        │ ▲ 0.45  handlers.handle_request [calls]
        └─ LOW RISK ───────────────────
          ▲ 0.12  cache.set_cached [calls]

        Summary: 2 high, 1 medium, 1 low, 1 test at risk
    """
    if not impact.at_risk:
        return f"{impact.changed_symbol} — no downstream symbols at risk"

    lines: list[str] = []
    lines.append(f"{impact.changed_symbol} (changing)")

    high = [e for e in impact.at_risk if e["risk_score"] > 0.7]
    medium = [e for e in impact.at_risk if 0.3 <= e["risk_score"] <= 0.7]
    low = [e for e in impact.at_risk if e["risk_score"] < 0.3]

    if high:
        lines.append("┌─ HIGH RISK ──────────────────")
        for entry in high:
            flags = " ⚠ TEST" if entry.get("is_test") else ""
            factors = ", ".join(entry.get("risk_factors", [])[:3])
            factor_str = f" [{factors}]" if factors else ""
            lines.append(f"│ ▲ {entry['risk_score']:<5}  {entry['symbol']}{factor_str}{flags}")

    if medium:
        sep = "├" if low else "└"
        lines.append(f"{sep}─ MEDIUM RISK ────────────────")
        prefix = "│" if low else " "
        for entry in medium:
            flags = " ⚠ TEST" if entry.get("is_test") else ""
            factors = ", ".join(entry.get("risk_factors", [])[:3])
            factor_str = f" [{factors}]" if factors else ""
            lines.append(f"{prefix} ▲ {entry['risk_score']:<5}  {entry['symbol']}{factor_str}{flags}")

    if low:
        lines.append("└─ LOW RISK ───────────────────")
        for entry in low:
            flags = " ⚠ TEST" if entry.get("is_test") else ""
            lines.append(f"  ▲ {entry['risk_score']:<5}  {entry['symbol']}{flags}")

    s = impact.summary
    lines.append("")
    lines.append(f"Summary: {s['high_risk']} high, {s['medium_risk']} medium, {s['low_risk']} low, {s['tests_at_risk']} test(s) at risk")

    return "\n".join(lines)


def render_mermaid(subgraph: SubgraphResult) -> str:
    """Render a subgraph as a Mermaid flowchart diagram.

    Example output:
        ```mermaid
        graph LR
            A["auth.create_token"]
            B["views.login_handler"]
            C["test_auth.test_login"]
            B -->|calls 1.0| A
            C -.->|asserts_on 1.0| A
        ```
    """
    if not subgraph.nodes:
        return f"```mermaid\ngraph LR\n    A[\"{subgraph.root}\"]\n```"

    lines: list[str] = []
    lines.append("```mermaid")
    lines.append("graph LR")

    # Assign short IDs to nodes
    node_ids: dict[str, str] = {}
    for i, node in enumerate(subgraph.nodes):
        short_id = f"N{i}"
        node_ids[node["id"]] = short_id

        # Style test nodes differently
        label = node["id"]
        if node.get("is_test"):
            lines.append(f"    {short_id}[\"{label} ⚠\"]:::test")
        else:
            lines.append(f"    {short_id}[\"{label}\"]")

    # Add edges (deduplicated)
    seen_edges: set[tuple[str, str, str]] = set()
    for edge in subgraph.edges:
        src_id = node_ids.get(edge["src"])
        dst_id = node_ids.get(edge["dst"])
        if not src_id or not dst_id:
            continue

        kind = edge.get("kind", "calls")
        edge_key = (src_id, dst_id, kind)
        if edge_key in seen_edges:
            continue
        seen_edges.add(edge_key)

        conf = edge.get("confidence", 1.0)
        label = f"{kind} {conf}"

        # Solid arrow for high confidence, dashed for low
        if conf >= 0.8:
            lines.append(f"    {src_id} -->|\"{label}\"| {dst_id}")
        elif conf >= 0.5:
            lines.append(f"    {src_id} -.->|\"{label}\"| {dst_id}")
        else:
            lines.append(f"    {src_id} -..->|\"{label}\"| {dst_id}")

    # Styling
    lines.append("")
    lines.append("    classDef default fill:#f1f5f9,stroke:#64748b,color:#1e293b")
    lines.append("    classDef test fill:#fbbf24,stroke:#d97706,color:#1e293b")

    lines.append("```")
    return "\n".join(lines)


def render_mermaid_impact(impact: ImpactResult) -> str:
    """Render impact analysis as a Mermaid flowchart with risk coloring.

    Nodes colored by risk level: red=high, orange=medium, green=low.
    """
    if not impact.at_risk:
        return f"```mermaid\ngraph LR\n    ROOT[\"{impact.changed_symbol}\"]\n```"

    lines: list[str] = []
    lines.append("```mermaid")
    lines.append("graph LR")
    lines.append(f"    ROOT[\"{impact.changed_symbol}\"]:::changed")

    for i, entry in enumerate(impact.at_risk):
        node_id = f"R{i}"
        label = entry["symbol"]
        score = entry["risk_score"]

        if entry.get("is_test"):
            label += " ⚠"

        if score > 0.7:
            css_class = "high"
        elif score >= 0.3:
            css_class = "medium"
        else:
            css_class = "low"

        lines.append(f"    {node_id}[\"{label}<br/>risk: {score}\"]:::{css_class}")

        # Connect to root via path
        distance = entry.get("distance", 1)
        if distance == 1:
            lines.append(f"    ROOT --> {node_id}")
        else:
            lines.append(f"    ROOT -.-> {node_id}")

    lines.append("")
    lines.append("    classDef changed fill:#6366f1,stroke:#4338ca,color:#fff")
    lines.append("    classDef high fill:#ef4444,stroke:#b91c1c,color:#fff")
    lines.append("    classDef medium fill:#f59e0b,stroke:#d97706,color:#1e293b")
    lines.append("    classDef low fill:#22c55e,stroke:#15803d,color:#fff")

    s = impact.summary
    lines.append("```")
    lines.append("")
    lines.append(f"**{s['high_risk']}** high, **{s['medium_risk']}** medium, **{s['low_risk']}** low, **{s['tests_at_risk']}** test(s) at risk")

    return "\n".join(lines)


def _build_flags(meta: dict[str, Any]) -> str:
    """Build flag string for a node."""
    flags = []
    if meta.get("is_test"):
        flags.append("⚠ TEST")
    if meta.get("is_public") == 0:
        flags.append("private")
    return f" {' '.join(flags)}" if flags else ""


def _get_sub_entries(
    node_id: str,
    root_id: str,
    children_in: dict[str, list[dict[str, Any]]],
    children_out: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Get second-level children for a node, excluding the root."""
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for item in children_in.get(node_id, []):
        if item["neighbor"] != root_id:
            key = ("←", item["neighbor"], item["kind"])
            if key not in seen:
                seen.add(key)
                entries.append({"direction": "←", **item})

    for item in children_out.get(node_id, []):
        if item["neighbor"] != root_id:
            key = ("→", item["neighbor"], item["kind"])
            if key not in seen:
                seen.add(key)
                entries.append({"direction": "→", **item})

    entries.sort(key=lambda x: (0 if x["direction"] == "←" else 1, -x["confidence"]))
    return entries[:5]  # Cap second-level children
