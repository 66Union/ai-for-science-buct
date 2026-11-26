#!/usr/bin/env python3
"""Entity alignment, relation assembly, query execution and graph export.

This is the finalize-side companion of kg_core.py: it takes the agent's generated
records (or the deterministic draft when the agent produced nothing), merges
aliases with an explicit reason for every merge, refuses to merge homonyms,
attaches doc-scoped evidence, executes the declared queries and writes the
readable graph files.

No network, no third-party package, deterministic ordering throughout.
"""

from __future__ import annotations

import csv
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
try:  # packaged layout: <skill-root>/lib
    sys.path.insert(0, str(ROOT))
    from lib import anchors, hybrid
except ImportError:  # dev layout: skills/<topic>/scripts + skills/_core/lib
    sys.path.insert(0, str(ROOT.parent / "_core"))
    from lib import anchors, hybrid

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from kg_core import (  # noqa: E402  (same directory, both layouts)
    DEFAULT_SCHEMA, TYPE_ORDER, _best_quote, _candidate_occurrences, _contains_label,
    _match_trigger, anchor_ceiling, clip, norm_key, norm_label, public_anchor,
    relation_spec, seg_anchor, variant_key,
)

TYPE_SYNONYMS = {
    "person": "researcher", "author": "researcher", "people": "researcher",
    "researcher": "researcher", "scientist": "researcher",
    "sample": "material", "compound": "material", "chemical": "material",
    "material": "material", "reagent": "material", "catalyst": "material",
    "technique": "method", "tool": "method", "instrument": "method",
    "procedure": "method", "method": "method", "model": "method",
    "benchmark": "task", "problem": "task", "application": "task",
    "task": "task", "dataset": "dataset", "corpus": "dataset",
    "measure": "metric", "indicator": "metric", "performance": "metric",
    "metric": "metric", "result": "metric", "value": "metric",
    "finding": "conclusion", "claim": "conclusion", "conclusion": "conclusion",
    "concept": "concept", "topic": "concept", "other": "other", "entity": "other",
}
STRENGTH_SCORES = {"strong": 0.9, "moderate": 0.6, "weak": 0.35,
                   "direct": 0.8, "co_mention": 0.5, "co_occurrence": 0.2}
GENERATED_PATHS = ("nodes.json", "edges.json", "queries.json")


# --------------------------------------------------------------------------- #
# record normalisation
# --------------------------------------------------------------------------- #
def normalize_type(raw: Any) -> str:
    text = norm_label(str(raw or "")).replace(" ", "_")
    if text in TYPE_SYNONYMS:
        return TYPE_SYNONYMS[text]
    for key, value in TYPE_SYNONYMS.items():
        if key in text:
            return value
    return "other"


def _strength(raw: Any) -> tuple[str, float]:
    if isinstance(raw, (int, float)):
        score = max(0.0, min(1.0, float(raw)))
        label = "strong" if score >= 0.8 else ("moderate" if score >= 0.5 else "weak")
        return label, score
    text = norm_label(str(raw or "")).replace(" ", "_")
    if text in STRENGTH_SCORES:
        return text, STRENGTH_SCORES[text]
    for key, value in STRENGTH_SCORES.items():
        if key in text:
            return key, value
    return "moderate", 0.6


def _find_segment(record: dict[str, Any], segments_by: dict[str, dict[str, Any]],
                  label: str, labels: Iterable[str] = ()) -> tuple[dict[str, Any] | None, str]:
    seg_id = record.get("seg_id") or record.get("segment_id") or record.get("segment")
    seg = segments_by.get(str(seg_id)) if seg_id else None
    quote = str(record.get("quote") or record.get("evidence_quote") or "").strip()
    if seg:
        text = seg.get("text") or ""
        if quote and not (_contains_label(text, quote) or quote.casefold() in text.casefold()):
            quote = _best_quote(text, [label, *labels])
        elif not quote:
            quote = _best_quote(text, [label, *labels])
        return seg, quote
    return None, quote


def _node_entry(record: dict[str, Any], docs: list[dict[str, Any]],
                segments: list[dict[str, Any]], segments_by: dict[str, dict[str, Any]],
                docs_by: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    label = str(record.get("label") or record.get("name") or record.get("entity")
                or record.get("node") or "").strip()
    if not label:
        return None
    aliases = [str(item).strip() for item in (record.get("aliases") or record.get("alias") or [])
               if isinstance(item, str) and item.strip()]
    node_type = normalize_type(record.get("type") or record.get("node_type")
                               or record.get("entity_type"))
    seg, quote = _find_segment(record, segments_by, label)
    occurrences: list[dict[str, Any]] = []
    if seg:
        doc = docs_by.get(seg.get("doc_id") or "")
        if doc:
            occurrences.append(seg_anchor(doc, seg, quote))
    if not occurrences:
        occurrences = _candidate_occurrences(label, docs, segments)
    if not occurrences and aliases:
        for alias in aliases:
            occurrences = _candidate_occurrences(alias, docs, segments)
            if occurrences:
                break
    keys = {norm_key(label)} | {norm_key(alias) for alias in aliases}
    keys.discard("")
    return {
        "label": label,
        "type": node_type,
        "aliases": aliases,
        "keys": keys,
        "occurrences": occurrences,
        "origin": "agent",
        "agent_id": str(record.get("node_id") or record.get("id") or "").strip() or None,
        "is_conclusion": node_type == "conclusion",
        "doc_ids": sorted({anchor.get("doc_id") for anchor in occurrences
                           if anchor.get("doc_id")}),
    }


def _edge_record(record: dict[str, Any], segments_by: dict[str, dict[str, Any]],
                 docs_by: dict[str, dict[str, Any]], schema: dict[str, Any],
                 node_index: dict[str, str], agent_index: dict[str, str]) -> dict[str, Any] | None:
    source_raw = str(record.get("source") or record.get("source_id")
                     or record.get("from") or record.get("subject") or "").strip()
    target_raw = str(record.get("target") or record.get("target_id")
                     or record.get("to") or record.get("object") or "").strip()
    relation_raw = str(record.get("relation") or record.get("predicate")
                       or record.get("type") or "").strip()
    if not (source_raw and target_raw and relation_raw):
        return None
    source_id = node_index.get(norm_key(source_raw)) or agent_index.get(source_raw)
    target_id = node_index.get(norm_key(target_raw)) or agent_index.get(target_raw)
    if not source_id or not target_id:
        return {"_reject": "endpoint_not_in_graph", "source": source_raw, "target": target_raw}
    relation, direction, known = relation_spec(schema, relation_raw)
    seg, quote = _find_segment(record, segments_by, source_raw, [target_raw])
    label_s = str(record.get("source_label") or source_raw)
    label_t = str(record.get("target_label") or target_raw)
    seg_obj = seg or {}
    doc = docs_by.get(seg_obj.get("doc_id") or "") or {}
    anchor_list: list[dict[str, Any]] = []
    if seg and doc:
        anchor_list.append(seg_anchor(doc, seg, quote))
    condition = clip(str(record.get("condition") or record.get("conditions") or ""), 220)
    raw_direction = norm_label(str(record.get("direction") or ""))
    if raw_direction in {"undirected", "bidirectional", "none"}:
        direction = "undirected"
    elif raw_direction in {"directed", "forward"}:
        direction = "directed"
    strength_label, strength_score = _strength(record.get("strength"))
    return {
        "source_id": source_id,
        "target_id": target_id,
        "source_label": label_s,
        "target_label": label_t,
        "relation": relation,
        "relation_raw": relation_raw,
        "relation_known": known,
        "direction": direction,
        "condition": condition,
        "strength": strength_label,
        "strength_score": strength_score,
        "doc_id": doc.get("doc_id") or record.get("doc_id"),
        "paragraph_id": seg_obj.get("paragraph_id") or record.get("paragraph_id"),
        "seg_id": seg_obj.get("seg_id") or record.get("seg_id"),
        "quote": quote,
        "anchors": anchor_list,
        "origin": "agent",
        "trigger": None,
    }


# --------------------------------------------------------------------------- #
# alias / homonym merge
# --------------------------------------------------------------------------- #
def _types_compatible(group: dict[str, Any], node_type: str) -> bool:
    if not node_type or node_type in {"concept", "other"}:
        return True
    if not group["types"]:
        return True
    return node_type in group["types"]


def _group_basis(group: dict[str, Any], acronym_defs: list[dict[str, Any]]) -> list[str]:
    members = group["members"]
    basis: list[str] = []
    surfaced: dict[str, int] = {}
    for member in members:
        surfaced[norm_key(member["label"])] = surfaced.get(norm_key(member["label"]), 0) + 1
    if any(count > 1 for count in surfaced.values()):
        basis.append("same_surface")
    variant_labels: dict[str, set[str]] = {}
    for member in members:
        variant = variant_key(member["label"])
        if variant:
            variant_labels.setdefault(variant, set()).add(norm_key(member["label"]))
    if any(len(labels) > 1 for labels in variant_labels.values()):
        basis.append("surface_variant")
    for member in members:
        if member["origin"] != "agent" or not member.get("aliases"):
            continue
        for alias in member["aliases"]:
            if norm_key(alias) in surfaced:
                basis.append("agent_alias")
                break
    for definition in acronym_defs:
        a = norm_key(definition.get("acronym") or "")
        e = norm_key(definition.get("expansion") or "")
        if a and e and (a in group["keys"] or e in group["keys"]):
            basis.append("acronym_definition")
            break
    return sorted(set(basis)) or ["label_variant"]


def _merge_group_into(primary: dict[str, Any], secondary: dict[str, Any],
                      groups: list[dict[str, Any]]) -> None:
    primary["members"].extend(secondary["members"])
    primary["keys"] |= secondary["keys"]
    primary["types"] |= secondary["types"]
    primary["has_generated"] = primary["has_generated"] or secondary["has_generated"]
    if secondary in groups:
        groups.remove(secondary)


def merge_entities(entries: list[dict[str, Any]], acronym_defs: list[dict[str, Any]]
                   ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Union entries into entity groups; every decision leaves a trace."""
    acronym_map: dict[str, set[str]] = {}
    for definition in acronym_defs:
        a = norm_key(definition.get("acronym") or "")
        e = norm_key(definition.get("expansion") or "")
        if a and e:
            acronym_map.setdefault(a, set()).add(e)
            acronym_map.setdefault(e, set()).add(a)
    groups: list[dict[str, Any]] = []
    key_to_group: dict[str, dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []

    def new_group(entry: dict[str, Any]) -> dict[str, Any]:
        group = {
            "members": [entry],
            "keys": set(entry["keys"]),
            "types": set(),
            "has_generated": entry["origin"] == "agent",
        }
        if entry["type"] not in {"concept", "other"}:
            group["types"].add(entry["type"])
        groups.append(group)
        return group

    for entry in entries:
        keys = set(entry["keys"])
        for key in list(keys):
            keys |= acronym_map.get(key, set())
        variant = variant_key(entry["label"])
        if variant:
            # A trailing generic head noun does not change which object is named,
            # so the variant key participates in alignment like an alias would.
            keys.add(variant)
        entry["keys"] = keys
        matches = []
        for key in keys:
            group = key_to_group.get(key)
            if group is not None and group not in matches:
                matches.append(group)
        compatible = [group for group in matches if _types_compatible(group, entry["type"])]
        if not compatible:
            group = new_group(entry)
            if matches:
                conflicts.append({
                    "kind": "homonym_type_conflict",
                    "labels": sorted({entry["label"]} | {m["label"] for g in matches for m in g["members"]}),
                    "types": sorted({entry["type"]} | {t for g in matches for t in g["types"]}),
                    "detail": ("same normalised surface but declared types differ: kept as "
                               "separate nodes instead of forcing a merge"),
                    "resolution": "kept_separate",
                    "doc_ids": sorted({d for m in entry["occurrences"] for d in [m.get("doc_id")] if d}),
                })
            for key in keys:
                key_to_group.setdefault(key, group)
            continue
        primary = sorted(compatible, key=lambda g: (0 if g["has_generated"] else 1,
                                                    -len(g["members"]), groups.index(g)))[0]
        primary["members"].append(entry)
        primary["has_generated"] = primary["has_generated"] or entry["origin"] == "agent"
        if entry["type"] not in {"concept", "other"}:
            primary["types"].add(entry["type"])
        primary["keys"] |= keys
        for group in compatible:
            if group is not primary:
                _merge_group_into(primary, group, groups)
        for key in primary["keys"]:
            key_to_group[key] = primary
    return groups, conflicts


def _canonical_label(group: dict[str, Any]) -> str:
    agent_labels = [m["label"] for m in group["members"] if m["origin"] == "agent"]
    pool = agent_labels or [m["label"] for m in group["members"]]
    return sorted(pool, key=lambda label: (-len(label), label.casefold()))[0]


def _build_nodes(groups: list[dict[str, Any]], acronym_defs: list[dict[str, Any]],
                 previous: dict[str, Any], schema: dict[str, Any]
                 ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    def _group_type(group: dict[str, Any]) -> str:
        if not group["types"]:
            return "concept"
        return sorted(group["types"],
                      key=lambda t: TYPE_ORDER.index(t) if t in TYPE_ORDER else 9)[0]

    groups.sort(key=lambda g: (
        TYPE_ORDER.index(_group_type(g)) if _group_type(g) in TYPE_ORDER else 9,
        _canonical_label(g).casefold(), norm_key(_canonical_label(g))))
    prev_index: dict[str, str] = {}
    used_ids: set[str] = set()
    for node in (previous or {}).get("nodes") or []:
        node_id = str(node.get("node_id") or "")
        if not node_id:
            continue
        used_ids.add(node_id)
        for key in [norm_key(node.get("label") or "")] + [
                norm_key(alias) for alias in node.get("aliases") or []]:
            if key:
                prev_index.setdefault(key, node_id)
    assigned: dict[int, str] = {}
    preserved = 0
    for index, group in enumerate(groups):
        keys = set(group["keys"]) | {norm_key(m["label"]) for m in group["members"]}
        candidate = next((prev_index[key] for key in sorted(keys)
                          if key in prev_index and prev_index[key] not in assigned.values()), None)
        if candidate:
            assigned[index] = candidate
            preserved += 1
    next_number = 1
    for index in range(len(groups)):
        if index in assigned:
            continue
        while f"n-{next_number:04d}" in used_ids:
            next_number += 1
        assigned[index] = f"n-{next_number:04d}"
        used_ids.add(assigned[index])
        next_number += 1

    nodes: list[dict[str, Any]] = []
    merge_log: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        node_id = assigned[index]
        label = _canonical_label(group)
        node_type = sorted(group["types"] or {"concept"},
                           key=lambda t: TYPE_ORDER.index(t) if t in TYPE_ORDER else 9)[0]
        aliases = sorted({m["label"] for m in group["members"]} |
                         {alias for m in group["members"] for alias in m.get("aliases") or []},
                         key=lambda item: (item.casefold(), item))
        aliases = [alias for alias in aliases if norm_key(alias) != norm_key(label)]
        occurrences: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for member in group["members"]:
            for occurrence in member["occurrences"]:
                marker = (occurrence.get("doc_id") or "", occurrence.get("paragraph_id") or "",
                          (occurrence.get("quote") or "")[:80])
                if marker in seen:
                    continue
                seen.add(marker)
                occurrences.append(occurrence)
        occurrences.sort(key=lambda a: (a.get("doc_id") or "", str(a.get("paragraph_id") or "")))
        origin = "agent" if group["has_generated"] else "baseline"
        node = {
            "node_id": node_id,
            "label": label,
            "type": node_type,
            "aliases": aliases,
            "doc_ids": sorted({occurrence.get("doc_id")
                               for member in group["members"]
                               for occurrence in member["occurrences"]
                               if occurrence.get("doc_id")}),
            "occurrences": occurrences[:4],
            "origin": origin,
            "type_source": "agent" if group["has_generated"] else "heuristic",
            "is_conclusion": any(m.get("is_conclusion") for m in group["members"]),
            "cross_doc_merged": bool(len({occurrence.get("doc_id") for occurrence
                                          in occurrences}) > 1),
        }
        node["scope"] = "concept"
        for member in group["members"]:
            if member.get("agent_id"):
                member["resolved_node_id"] = node_id
        nodes.append(node)
        if len(group["members"]) > 1:
            basis = _group_basis(group, acronym_defs)
            evidence = [public_anchor(a) for a in occurrences[:1]]
            merged_labels = sorted({m["label"] for m in group["members"]
                                    if norm_key(m["label"]) != norm_key(label)})
            merge_log.append({
                "node_id": node_id,
                "kept_label": label,
                "merged_labels": merged_labels,
                "merged_from": [
                    {"label": m["label"], "type": m.get("type"),
                     "doc_ids": sorted({o.get("doc_id") for o in m.get("occurrences") or []
                                        if o.get("doc_id")}),
                     "quote": ((m.get("occurrences") or [{}])[0].get("quote"))}
                    for m in sorted(group["members"], key=lambda item: item["label"].casefold())
                    if norm_key(m["label"]) != norm_key(label)
                ],
                "basis": basis,
                "rule": (schema.get("merge_rules") or {}).get(basis[0], basis[0]),
                "confidence": (0.95 if "same_surface" in basis else
                               0.9 if "acronym_definition" in basis else 0.85),
                "doc_ids": node["doc_ids"],
                "evidence": evidence,
                "not_merged_note": ("different declared types on the same surface stay "
                                    "separate and are logged in the conflict register"),
            })
    for index, entry in enumerate(merge_log, start=1):
        entry["merge_id"] = f"m-{index:04d}"
    incremental = {"preserved_node_ids": preserved,
                   "new_node_ids": len(nodes) - preserved,
                   "previous_graph_found": bool((previous or {}).get("nodes"))}
    return nodes, merge_log, incremental, groups


def _node_index(nodes: list[dict[str, Any]]) -> dict[str, str]:
    index: dict[str, str] = {}
    for node in nodes:
        for label in [node["label"]] + list(node.get("aliases") or []):
            key = norm_key(label)
            if key:
                index.setdefault(key, node["node_id"])
    return index


def _agent_node_index(entries: list[dict[str, Any]]) -> dict[str, str]:
    index: dict[str, str] = {}
    for entry in entries:
        if entry.get("agent_id"):
            index[entry["agent_id"]] = entry.get("resolved_node_id") or ""
    return {key: value for key, value in index.items() if value}


def _collect_edges(records: list[dict[str, Any]], node_index: dict[str, str],
                   agent_index: dict[str, str], segments_by: dict[str, dict[str, Any]],
                   docs_by: dict[str, dict[str, Any]], schema: dict[str, Any],
                   rejected: list[dict[str, Any]],
                   node_labels: dict[str, str] | None = None) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for record in records:
        edge = _edge_record(record, segments_by, docs_by, schema, node_index, agent_index)
        if edge is None:
            continue
        if edge.get("_reject"):
            rejected.append({"record": record, "reason": edge["_reject"]})
            continue
        relation = edge["relation"]
        if edge.get("direction") == "undirected":
            pair = tuple(sorted([edge["source_id"], edge["target_id"]]))
            key = (pair[0], pair[1], relation, edge.get("doc_id") or "")
        else:
            key = (edge["source_id"], edge["target_id"], relation, edge.get("doc_id") or "")
        if key in seen:
            continue
        seen.add(key)
        labels = node_labels or {}
        edge["source_label"] = labels.get(edge["source_id"]) or edge.get("source_label")
        edge["target_label"] = labels.get(edge["target_id"]) or edge.get("target_label")
        edges.append(edge)
    return edges


def _add_baseline_edges(edges: list[dict[str, Any]], baseline_edges: list[dict[str, Any]],
                        node_index: dict[str, str], rejected: list[dict[str, Any]],
                        max_add: int = 200,
                        node_labels: dict[str, str] | None = None) -> int:
    added = 0
    existing = set()
    for edge in edges:
        if edge.get("direction") == "undirected":
            pair = tuple(sorted([edge["source_id"], edge["target_id"]]))
        else:
            pair = (edge["source_id"], edge["target_id"])
        existing.add((pair, edge["relation"], edge.get("doc_id") or ""))
    for record in baseline_edges:
        if added >= max_add:
            break
        source_id = node_index.get(norm_key(record.get("source_label") or ""))
        target_id = node_index.get(norm_key(record.get("target_label") or ""))
        if not source_id or not target_id or source_id == target_id:
            continue
        relation = record.get("relation") or "other"
        direction = record.get("direction") or "directed"
        pair = tuple(sorted([source_id, target_id])) if direction == "undirected" \
            else (source_id, target_id)
        key = (pair, relation, record.get("doc_id") or "")
        if key in existing:
            continue
        existing.add(key)
        edges.append({
            "source_id": source_id,
            "target_id": target_id,
            "source_label": (node_labels or {}).get(source_id) or record.get("source_label"),
            "target_label": (node_labels or {}).get(target_id) or record.get("target_label"),
            "relation": relation,
            "relation_raw": relation,
            "relation_known": True,
            "direction": direction,
            "condition": record.get("condition") or "",
            "strength": record.get("strength") or "moderate",
            "strength_score": record.get("strength_score") or 0.6,
            "doc_id": record.get("doc_id"),
            "paragraph_id": record.get("paragraph_id"),
            "seg_id": record.get("seg_id"),
            "quote": record.get("quote") or "",
            "anchors": record.get("anchors") or [],
            "origin": "baseline",
            "trigger": record.get("trigger"),
        })
        added += 1
    return added


def _add_co_occurrence_edges(edges: list[dict[str, Any]], pairs: list[dict[str, Any]],
                             node_index: dict[str, str], docs_by: dict[str, dict[str, Any]],
                             segments_by: dict[str, dict[str, Any]], max_add: int = 4) -> int:
    added = 0
    existing = set()
    for edge in edges:
        pair = tuple(sorted([edge["source_id"], edge["target_id"]]))
        existing.add((pair, edge.get("doc_id") or ""))
    for pair in pairs:
        if added >= max_add:
            break
        source_id = node_index.get(norm_key(pair.get("source_label") or ""))
        target_id = node_index.get(norm_key(pair.get("target_label") or ""))
        if not source_id or not target_id or source_id == target_id:
            continue
        doc_id = (pair.get("doc_ids") or [None])[0]
        key = (tuple(sorted([source_id, target_id])), doc_id or "")
        if key in existing:
            continue
        existing.add(key)
        seg_id = (pair.get("seg_ids") or [None])[0]
        seg = segments_by.get(seg_id or "")
        doc = docs_by.get(doc_id or "")
        anchor_list = []
        quote = pair.get("quote") or ""
        if seg and doc:
            if not quote or not _contains_label(quote, pair.get("source_label") or "") \
                    or not _contains_label(quote, pair.get("target_label") or ""):
                quote = _best_quote(seg.get("text") or "", [pair.get("source_label"),
                                                             pair.get("target_label")])
            if not _contains_label(quote, pair.get("source_label") or "") \
                    or not _contains_label(quote, pair.get("target_label") or ""):
                continue  # a co-occurrence edge without both labels is not verifiable
            anchor_list.append(seg_anchor(doc, seg, quote))
        edges.append({
            "source_id": source_id,
            "target_id": target_id,
            "source_label": pair.get("source_label"),
            "target_label": pair.get("target_label"),
            "relation": "co_occurs",
            "relation_raw": "co_occurs",
            "relation_known": True,
            "direction": "undirected",
            "condition": "same paragraph; co-occurrence only, relation NOT asserted",
            "strength": "weak",
            "strength_score": 0.2,
            "doc_id": doc_id,
            "paragraph_id": seg.get("paragraph_id") if seg else None,
            "seg_id": seg_id,
            "quote": quote,
            "anchors": anchor_list,
            "origin": "baseline",
            "trigger": None,
        })
        added += 1
    return added


def _assign_edge_ids(edges: list[dict[str, Any]]) -> None:
    edges.sort(key=lambda edge: (edge["source_id"], edge["relation"], edge["target_id"],
                                 edge.get("doc_id") or ""))
    for index, edge in enumerate(edges, start=1):
        edge["edge_id"] = f"e-{index:04d}"


def _condition_conflicts(edges: list[dict[str, Any]], nodes_by_id: dict[str, dict[str, Any]]
                         ) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for edge in edges:
        pair = tuple(sorted([edge["source_id"], edge["target_id"]]))
        buckets.setdefault((pair[0], pair[1], edge["relation"]), []).append(edge)
    conflicts: list[dict[str, Any]] = []
    for (source, target, relation), group in sorted(buckets.items()):
        doc_ids = {edge.get("doc_id") for edge in group}
        conditions = {edge.get("condition") for edge in group if edge.get("condition")}
        if len(doc_ids) < 2 or len(conditions) < 2:
            continue
        conflicts.append({
            "kind": "cross_doc_condition_conflict",
            "labels": [nodes_by_id.get(source, {}).get("label"),
                       nodes_by_id.get(target, {}).get("label")],
            "relation": relation,
            "doc_ids": sorted(doc_id for doc_id in doc_ids if doc_id),
            "conditions": sorted(conditions),
            "detail": ("the same relation appears under different conditions in different "
                       "papers; the edges are kept doc-scoped and must not be merged into "
                       "one conclusion"),
            "resolution": "kept_doc_scoped",
        })
    for index, conflict in enumerate(conflicts, start=1):
        conflict["conflict_id"] = f"x-{index:04d}"
    return conflicts


# Numbers with a unit inside one quote: the doc-scoped measurements behind a
# metric node. Used by the metric-reporting query and by the cross-paper value
# difference register (the two papers' values are never averaged into one).
# Only measurement units count as a metric value: pressure and temperature belong
# to the condition of an experiment, not to the metric (a selectivity is not "10 bar").
METRIC_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:-?\d+(?:\.\d+)?)\s*"
    r"(?:wt%|mol%|%|mAh/g|mg/g|mL/min|h-1|h⁻¹|eV|nm|kPa)(?![A-Za-z])",
    re.I)


def metric_values(quote: str, label: str = "") -> list[str]:
    """Values that sit next to the metric label (before or after it), closest first."""
    text = quote or ""
    if not label:
        return [match.group(0).strip() for match in METRIC_VALUE_RE.finditer(text)][:4]
    pos = text.casefold().find(label.casefold())
    if pos < 0:
        return [match.group(0).strip() for match in METRIC_VALUE_RE.finditer(text)][:4]
    scored: list[tuple[int, str]] = []
    for match in METRIC_VALUE_RE.finditer(text):
        if match.end() <= pos:
            distance = pos - match.end()
        elif match.start() >= pos + len(label):
            distance = match.start() - (pos + len(label))
        else:
            distance = 0
        if distance <= 20:
            scored.append((distance, match.group(0).strip()))
    scored.sort()
    found: list[str] = []
    for _distance, value in scored:
        if value not in found:
            found.append(value)
        if len(found) >= 4:
            break
    return found


def metric_value_conflicts(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
                           ) -> list[dict[str, Any]]:
    """The same metric reported with different numbers in different papers.

    This is NOT a contradiction to resolve: it is the record that the numbers
    are doc-scoped and must not be merged into one conclusion.
    """
    metrics = {node["node_id"]: node for node in nodes if node["type"] == "metric"}
    buckets: dict[str, dict[str, set[str]]] = {}
    for edge in edges:
        doc_id = edge.get("doc_id") or ""
        if not doc_id:
            continue
        for node_id in (edge["source_id"], edge["target_id"]):
            node = metrics.get(node_id)
            if not node:
                continue
            for value in metric_values(edge.get("quote") or "", node["label"]):
                buckets.setdefault(node_id, {}).setdefault(doc_id, set()).add(value)
    conflicts: list[dict[str, Any]] = []
    for node_id in sorted(buckets):
        per_doc = {doc_id: sorted(values) for doc_id, values in sorted(buckets[node_id].items())
                   if values}
        if len(per_doc) < 2:
            continue
        if len({tuple(values) for values in per_doc.values()}) < 2:
            continue
        conflicts.append({
            "kind": "cross_doc_value_difference",
            "labels": [metrics[node_id]["label"]],
            "doc_ids": sorted(per_doc),
            "values_by_doc": per_doc,
            "detail": ("the same metric reports different doc-scoped values; the numbers are "
                       "kept per paper and are NOT averaged or merged into one conclusion"),
            "resolution": "kept_doc_scoped",
        })
    return conflicts


# --------------------------------------------------------------------------- #
# orchestrator: generated records + deterministic draft -> final graph
# --------------------------------------------------------------------------- #
def merge_graphs(generated: dict[str, Any], baseline: dict[str, Any],
                 acronym_defs: list[dict[str, Any]], docs: list[dict[str, Any]],
                 segments: list[dict[str, Any]], schema: dict[str, Any],
                 previous: dict[str, Any]) -> dict[str, Any]:
    from kg_core import documents_by_id  # local import keeps the module import light
    docs_by = documents_by_id(docs)
    segments_by = {seg["seg_id"]: seg for seg in segments}
    rejected: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    for record in generated.get("nodes") or []:
        entry = _node_entry(record, docs, segments, segments_by, docs_by)
        if entry is None:
            rejected.append({"kind": "node", "reason": "missing_label", "record": record})
            continue
        entries.append(entry)
    baseline_entries: list[dict[str, Any]] = []
    for node in baseline.get("nodes") or []:
        baseline_entries.append({
            "label": node.get("label") or "",
            "type": node.get("type") or "other",
            "aliases": [],
            "keys": {norm_key(node.get("label") or "")},
            "occurrences": node.get("occurrences") or [],
            "origin": "baseline",
            "agent_id": None,
            "is_conclusion": bool(node.get("is_conclusion")),
            "doc_ids": node.get("doc_ids") or [],
        })
    groups, conflicts = merge_entities(entries + baseline_entries, acronym_defs)
    nodes, merge_log, incremental, _groups = _build_nodes(groups, acronym_defs, previous, schema)
    node_index = _node_index(nodes)
    agent_index = _agent_node_index(entries)
    node_labels = {node["node_id"]: node["label"] for node in nodes}
    edges = _collect_edges(generated.get("edges") or [], node_index, agent_index,
                           segments_by, docs_by, schema, rejected, node_labels)
    generated_edges_used = len(edges)
    baseline_edges_added = _add_baseline_edges(edges, baseline.get("edges") or [],
                                               node_index, rejected, node_labels=node_labels)
    co_occurrence_added = 0
    if generated_edges_used == 0:
        co_occurrence_added = _add_co_occurrence_edges(
            edges, baseline.get("co_occurrence_candidates") or [], node_index,
            docs_by, segments_by, max_add=0)  # the baseline already carries labelled co-occurrence
    _assign_edge_ids(edges)
    nodes_by_id = {node["node_id"]: node for node in nodes}
    condition_conflicts = _condition_conflicts(edges, nodes_by_id)
    value_differences = metric_value_conflicts(nodes, edges)
    conflicts = conflicts + condition_conflicts + value_differences
    for index, conflict in enumerate(conflicts, start=1):
        conflict["conflict_id"] = f"x-{index:04d}"
    type_counts: dict[str, int] = {}
    for node in nodes:
        type_counts[node["type"]] = type_counts.get(node["type"], 0) + 1
    relation_counts: dict[str, int] = {}
    for edge in edges:
        relation_counts[edge["relation"]] = relation_counts.get(edge["relation"], 0) + 1
    generated_nodes_used = sum(1 for node in nodes if node["origin"] == "agent")
    generated_usage = {
        "nodes_declared": len(generated.get("nodes") or []),
        "nodes_used": generated_nodes_used,
        "edges_declared": len(generated.get("edges") or []),
        "edges_used": generated_edges_used,
        "baseline_edges_added": baseline_edges_added,
        "co_occurrence_edges_added": co_occurrence_added,
        "source": ("agent_generated + deterministic baseline" if generated_nodes_used
                   or generated_edges_used else "deterministic baseline only"),
    }
    stats = {
        "nodes": len(nodes),
        "edges": len(edges),
        "node_type_counts": dict(sorted(type_counts.items())),
        "relation_type_counts": dict(sorted(relation_counts.items())),
        "cross_doc_nodes": sum(1 for node in nodes if len(node.get("doc_ids") or []) > 1),
        "doc_scoped_edges": len({edge.get("doc_id") for edge in edges if edge.get("doc_id")}),
        "merge_groups": len(merge_log),
        "conflicts": len(conflicts),
        "rejected_records": len(rejected),
    }
    return {
        "nodes": nodes,
        "edges": edges,
        "merge_log": merge_log,
        "conflicts": conflicts,
        "incremental": incremental,
        "rejected": rejected,
        "generated_usage": generated_usage,
        "stats": stats,
    }


# --------------------------------------------------------------------------- #
# graph queries: real, executed, with evidence and honest no-result states
# --------------------------------------------------------------------------- #
QUERY_OPS = ("neighbors", "relation", "type_list", "evidence_lookup",
             "cross_doc_shared", "cross_doc_consistency", "path", "unresolved",
             "material_methods", "metric_reporters", "conclusion_evidence")
PARAM_KEYS = ("entity", "entity_id", "node", "node_id", "label", "relation",
              "relation_type", "node_type", "ref", "ref_id", "edge", "edge_id",
              "source", "target", "from", "to", "max_hops", "depth", "type",
              "material", "metric", "conclusion", "ref_label")


def normalize_queries(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Accept the plausible generated query shapes and normalise them."""
    definitions: list[dict[str, Any]] = []
    for index, record in enumerate(records or []):
        if not isinstance(record, dict):
            continue
        natural = str(record.get("natural_language") or record.get("question")
                      or record.get("nl") or record.get("query") or "").strip()
        op = norm_label(str(record.get("op") or record.get("type")
                            or record.get("operation") or "")).replace(" ", "_")
        params = record.get("params") or record.get("arguments")             or record.get("definition") or {}
        if not isinstance(params, dict):
            params = {}
        for key in PARAM_KEYS:
            if key in record and key not in params:
                params[key] = record[key]
        if not op:
            if params.get("relation") or params.get("relation_type"):
                op = "relation"
            elif params.get("ref_id") or params.get("edge_id"):
                op = "evidence_lookup"
            elif params.get("entity") or params.get("node_id") or params.get("label"):
                op = "neighbors"
            elif params.get("node_type") or params.get("type"):
                op = "type_list"
        definitions.append({
            "query_id": str(record.get("query_id") or record.get("id") or f"q-{index + 1:03d}"),
            "natural_language": natural,
            "op": op,
            "params": params,
        })
    return definitions


def _graph_index(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {node["node_id"]: node for node in nodes}
    by_key: dict[str, str] = {}
    for node in nodes:
        for label in [node["label"]] + list(node.get("aliases") or []):
            key = norm_key(label)
            if key:
                by_key.setdefault(key, node["node_id"])
    adjacency: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        adjacency.setdefault(edge["source_id"], []).append(edge)
        if edge["target_id"] != edge["source_id"]:
            adjacency.setdefault(edge["target_id"], []).append(edge)
    edges_by_id = {edge["edge_id"]: edge for edge in edges}
    return {"by_id": by_id, "by_key": by_key, "adjacency": adjacency,
            "edges_by_id": edges_by_id}


def _resolve_node(index: dict[str, Any], raw: Any) -> str | None:
    if raw in (None, ""):
        return None
    text = str(raw)
    if text in index["by_id"]:
        return text
    return index["by_key"].get(norm_key(text))


def _node_brief(node: dict[str, Any]) -> dict[str, Any]:
    return {"node_id": node["node_id"], "label": node["label"], "type": node["type"],
            "doc_ids": node.get("doc_ids") or []}


def _edge_evidence(edge: dict[str, Any], limit: int = 2) -> list[dict[str, Any]]:
    return [public_anchor(anchor) for anchor in (edge.get("anchors") or [])[:limit]]


def _result(definition: dict[str, Any], *, status: str, answer: str = "",
            nodes: list[dict[str, Any]] | None = None,
            edges: list[dict[str, Any]] | None = None,
            evidence: list[dict[str, Any]] | None = None,
            limitations: list[str] | None = None) -> dict[str, Any]:
    nodes = nodes or []
    edges = edges or []
    evidence = evidence if evidence is not None else [
        anchor for edge in edges for anchor in _edge_evidence(edge)]
    return {
        "query_id": definition.get("query_id"),
        "natural_language": definition.get("natural_language"),
        "op": definition.get("op"),
        "params": definition.get("params") or {},
        "status": status,
        "answer": answer,
        "result_nodes": [_node_brief(node) for node in nodes[:20]],
        "result_edges": [public_edge(edge) for edge in edges[:20]],
        "evidence": evidence[:24],
        "limitations": limitations or [],
    }


def execute_queries(nodes: list[dict[str, Any]], edges: list[dict[str, Any]],
                    definitions: list[dict[str, Any]], *,
                    conflicts: list[dict[str, Any]], unverified: list[dict[str, Any]],
                    documents: list[dict[str, Any]], stats: dict[str, Any],
                    templates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    index = _graph_index(nodes, edges)
    results: list[dict[str, Any]] = []
    unsupported = 0
    for definition in definitions:
        op = definition.get("op") or ""
        params = definition.get("params") or {}
        if op not in QUERY_OPS:
            unsupported += 1
            results.append(_result(definition, status="unsupported_op",
                                   answer=f"query operation '{op}' is not implemented",
                                   limitations=["supported ops: " + ", ".join(QUERY_OPS)]))
            continue
        results.append(_run_query(definition, op, params, index, nodes, edges,
                                  conflicts, unverified, documents, stats))
    executed = sum(1 for result in results if result["status"] in {"answered", "no_result", "undetermined"})
    auto_limit = max(0, min(12, 9 - len(results)))
    if executed < 3:
        template_map = {str(item.get("op")): item for item in (templates or [])
                        if isinstance(item, dict) and item.get("op")}
        for definition in auto_query_defs(index, nodes, edges, stats, conflicts, documents)[:auto_limit]:
            template = template_map.get(definition["op"])
            if template and template.get("natural_language"):
                definition["natural_language"] = _fill_template(
                    template["natural_language"], definition, index)
            result = _run_query(definition, definition["op"], definition["params"], index,
                                nodes, edges, conflicts, unverified, documents, stats)
            if result["status"] in {"answered", "no_result", "undetermined"}:
                result["auto_generated"] = True
                results.append(result)
    executed = sum(1 for result in results if result["status"] in {"answered", "no_result", "undetermined"})
    summary = {
        "declared_queries": len(definitions),
        "executed_queries": executed,
        "answered": sum(1 for result in results if result["status"] == "answered"),
        "no_result": sum(1 for result in results if result["status"] == "no_result"),
        "undetermined": sum(1 for result in results if result["status"] == "undetermined"),
        "unsupported": unsupported,
        "minimum_three_queries_satisfied": executed >= 3,
    }
    return results, summary


def _run_query(definition: dict[str, Any], op: str, params: dict[str, Any],
               index: dict[str, Any], nodes: list[dict[str, Any]],
               edges: list[dict[str, Any]], conflicts: list[dict[str, Any]],
               unverified: list[dict[str, Any]], documents: list[dict[str, Any]],
               stats: dict[str, Any]) -> dict[str, Any]:
    by_id = index["by_id"]
    if op == "neighbors":
        node_id = _resolve_node(index, params.get("entity") or params.get("node_id")
                                or params.get("label") or params.get("node"))
        if not node_id:
            return _result(definition, status="no_result",
                           answer="the material never names the requested entity")
        incident = index["adjacency"].get(node_id, [])
        neighbours: list[dict[str, Any]] = []
        seen: set[str] = set()
        for edge in incident:
            other = edge["target_id"] if edge["source_id"] == node_id else edge["source_id"]
            if other in seen or other == node_id:
                continue
            seen.add(other)
            if other in by_id:
                neighbours.append(by_id[other])
        if not incident:
            return _result(definition, status="no_result",
                           answer=f"'{by_id[node_id]['label']}' has no documented relation in this graph",
                           nodes=[by_id[node_id]])
        return _result(definition, status="answered",
                       answer=(f"{len(incident)} relation(s) touch '{by_id[node_id]['label']}'; "
                               f"each carries a paragraph anchor and quote"),
                       nodes=[by_id[node_id]] + neighbours, edges=incident)
    if op == "relation":
        raw = params.get("relation") or params.get("relation_type")
        relation = str(raw or "").strip()
        if not relation:
            return _result(definition, status="no_result",
                           answer="no relation type was requested")
        from kg_core import relation_spec as _relation_spec
        canonical, _direction, _known = _relation_spec({}, relation)
        matched = [edge for edge in edges if edge["relation"] in {canonical, relation}]
        if not matched:
            return _result(definition, status="no_result",
                           answer=(f"no edge of type '{relation}' is supported by the material; "
                                   f"the absence is reported instead of being invented"))
        node_ids = sorted({edge["source_id"] for edge in matched} |
                          {edge["target_id"] for edge in matched})
        return _result(definition, status="answered",
                       answer=f"{len(matched)} '{relation}' edge(s) with doc-scoped evidence",
                       nodes=[by_id[node_id] for node_id in node_ids if node_id in by_id],
                       edges=matched)
    if op == "type_list":
        wanted = normalize_type(params.get("node_type") or params.get("type"))
        matched = [node for node in nodes if node["type"] == wanted]
        if not matched:
            return _result(definition, status="no_result",
                           answer=f"no node of type '{wanted}' was found in the material")
        return _result(definition, status="answered",
                       answer=f"{len(matched)} node(s) of type '{wanted}'",
                       nodes=matched)
    if op == "evidence_lookup":
        ref = params.get("ref_id") or params.get("node_id") or params.get("edge_id") \
            or params.get("ref") or params.get("node") or params.get("edge")
        node_id = _resolve_node(index, ref)
        if node_id:
            node = by_id[node_id]
            anchors_list = [public_anchor(anchor) for anchor in node.get("occurrences") or []]
            return _result(definition, status="answered",
                           answer=f"evidence for '{node['label']}' ({len(anchors_list)} anchor(s))",
                           nodes=[node], evidence=anchors_list)
        edge = index["edges_by_id"].get(str(ref or ""))
        if edge:
            return _result(definition, status="answered",
                           answer=f"evidence for edge '{edge['edge_id']}' ({edge['relation']})",
                           edges=[edge], evidence=_edge_evidence(edge, limit=2))
        return _result(definition, status="no_result",
                       answer="the referenced entity or edge is not in the graph")
    if op == "cross_doc_shared":
        wanted = params.get("node_type") or params.get("type")
        wanted_type = normalize_type(wanted) if wanted else None
        shared = [node for node in nodes if len(node.get("doc_ids") or []) > 1
                  and (wanted_type is None or node["type"] == wanted_type)]
        if not shared:
            shared = [node for node in nodes if len(node.get("doc_ids") or []) > 1]
            if shared:
                return _result(definition, status="answered",
                               answer=(f"no node of type '{wanted_type}' spans both papers; "
                                       f"{len(shared)} node(s) do appear in more than one paper "
                                       f"at concept level: "
                                       + ", ".join(node["label"] for node in shared[:8])),
                               nodes=shared,
                               limitations=["cross-paper identity is asserted at concept level "
                                            "only; per-paper samples and conditions stay separate"])
            return _result(definition, status="no_result",
                           answer=("no concept of this type is confirmed in more than one paper; "
                                   "cross-paper identity is not assumed"))
        return _result(definition, status="answered",
                       answer=(f"{len(shared)} concept(s) appear in both papers at concept level; "
                               f"per-paper samples, conditions and numbers stay doc-scoped"),
                       nodes=shared)
    if op == "cross_doc_consistency":
        material_nodes = [node for node in nodes if node["type"] == "material"]
        shared_material = [node for node in material_nodes if len(node.get("doc_ids") or []) > 1]
        docs_label = ", ".join(str(doc.get("doc_id")) for doc in documents) or "the materials"
        if shared_material:
            answer = (
                f"{len(shared_material)} material term(s) occur in more than one paper, but the "
                f"materials are merged only at concept level: the package reports no shared "
                f"physical sample, batch, dataset instance or experimental condition, so the "
                f"papers' conditions and numbers are kept in separate doc-scoped edges")
            return _result(definition, status="undetermined", answer=answer,
                           nodes=shared_material,
                           limitations=["shared terminology is not evidence of a shared sample"])
        return _result(definition, status="undetermined",
                       answer=(f"the materials ({docs_label}) do not report a shared physical "
                               f"sample or dataset instance; identity across papers cannot be "
                               f"determined from the package"),
                       nodes=material_nodes)
    if op == "material_methods":
        node_id = _resolve_node(index, params.get("material") or params.get("entity")
                                or params.get("label") or params.get("node_id"))
        if not node_id:
            return _result(definition, status="no_result",
                           answer="the material never names the requested material")
        node = by_id[node_id]
        linked = [edge for edge in index["adjacency"].get(node_id, [])
                  if edge["relation"] in {"characterizes", "uses", "applies_to",
                                          "evaluates_on"}]
        methods: dict[str, dict[str, Any]] = {}
        for edge in linked:
            other = edge["target_id"] if edge["source_id"] == node_id else edge["source_id"]
            other_node = by_id.get(other)
            if other_node and other_node["type"] == "method":
                methods.setdefault(other, other_node)
        if not methods:
            return _result(definition, status="no_result",
                           answer=(f"no method is linked to '{node['label']}' by an evidenced "
                                   f"relation in this graph"),
                           nodes=[node])
        ordered = [methods[key] for key in sorted(methods)]
        return _result(definition, status="answered",
                       answer=(f"{len(ordered)} method(s) are evidenced for '{node['label']}': "
                               + ", ".join(item["label"] for item in ordered[:8])),
                       nodes=[node] + ordered, edges=linked)
    if op == "metric_reporters":
        node_id = _resolve_node(index, params.get("metric") or params.get("entity")
                                or params.get("label") or params.get("node_id"))
        if not node_id:
            metrics = sorted((node for node in nodes if node["type"] == "metric"),
                             key=lambda node: node["node_id"])
            if not metrics:
                return _result(definition, status="no_result",
                               answer="the material reports no measurable metric")
            node_id = metrics[0]["node_id"]
        node = by_id[node_id]
        incident = index["adjacency"].get(node_id, [])
        per_doc: dict[str, list[str]] = {}
        for edge in incident:
            doc_id = edge.get("doc_id") or "unknown"
            for value in metric_values(edge.get("quote") or "", node["label"]):
                if value not in per_doc.setdefault(doc_id, []):
                    per_doc[doc_id].append(value)
        if not incident:
            return _result(definition, status="no_result",
                           answer=f"no relation in the graph reports '{node['label']}'",
                           nodes=[node])
        parts = [f"{doc_id}: " + (", ".join(values) if values else "no numeric value in the "
                                  "cited quote") for doc_id, values in sorted(per_doc.items())]
        return _result(definition, status="answered",
                       answer=(f"'{node['label']}' is reported by {len(per_doc) or 0} paper(s) — "
                               + "; ".join(parts)
                               + ". Values stay doc-scoped and are not merged into one number"),
                       nodes=[node], edges=incident,
                       limitations=["a value reported in one paper is NOT evidence for the "
                                    "other paper's sample or conditions"])
    if op == "conclusion_evidence":
        conclusions = sorted((node for node in nodes if node["type"] == "conclusion"
                              or node.get("is_conclusion")), key=lambda node: node["node_id"])
        if not conclusions:
            return _result(definition, status="no_result",
                           answer="the material states no claim that was extracted as a conclusion")
        wanted = _resolve_node(index, params.get("conclusion") or params.get("entity")
                               or params.get("label") or params.get("node_id"))
        node = by_id.get(wanted) if wanted else conclusions[0]
        if not node or node["type"] != "conclusion":
            node = conclusions[0]
        anchors_list = [public_anchor(anchor) for anchor in node.get("occurrences") or []]
        quote = (anchors_list[0].get("quote") if anchors_list else "") or ""
        return _result(definition, status="answered",
                       answer=(f"the claim is stated in paragraph "
                               f"{anchors_list[0].get('paragraph_id') if anchors_list else '?'} "
                               f"of {anchors_list[0].get('doc_id') if anchors_list else '?'}: "
                               f"\"{quote[:180]}\"") if anchors_list else
                              f"the claim '{node['label']}' has no located quote",
                       nodes=[node], evidence=anchors_list)
    if op == "path":
        source = _resolve_node(index, params.get("source") or params.get("from"))
        target = _resolve_node(index, params.get("target") or params.get("to"))
        if not source or not target:
            return _result(definition, status="no_result",
                           answer="both path endpoints must name nodes present in the graph")
        max_hops = int(params.get("max_hops") or 3)
        path_edges = _shortest_path(index, source, target, max_hops)
        if path_edges is None:
            return _result(definition, status="no_result",
                           answer=(f"no path within {max_hops} hop(s); the material does not "
                                   f"connect these nodes"))
        node_ids = {source, target}
        for edge in path_edges:
            node_ids.add(edge["source_id"])
            node_ids.add(edge["target_id"])
        return _result(definition, status="answered",
                       answer=f"shortest path has {len(path_edges)} edge(s), each with evidence",
                       nodes=[by_id[node_id] for node_id in sorted(node_ids) if node_id in by_id],
                       edges=path_edges)
    if op == "unresolved":
        items = [{"kind": "merge_conflict", **conflict} for conflict in conflicts]
        items += [{"kind": "unverified_evidence", **item} for item in unverified]
        if not items:
            return _result(definition, status="answered",
                           answer="no unresolved merge conflict and no dropped unverified claim")
        return _result(definition, status="answered",
                       answer=(f"{len(items)} unresolved item(s): merge conflicts and claims "
                               f"whose evidence could not be located are listed rather than "
                               f"silently resolved"),
                       evidence=[])
    return _result(definition, status="no_result", answer=f"unhandled op {op}")


def _fill_template(template: str, definition: dict[str, Any],
                   index: dict[str, Any]) -> str:
    params = definition.get("params") or {}
    node = index["by_id"].get(str(params.get("entity") or ""))
    relation = params.get("relation") or "relation"
    node_type = params.get("node_type") or "实体"
    ref_label = params.get("ref_label") or (node or {}).get("label") or ""
    try:
        return template.format(entity=(node or {}).get("label") or params.get("entity") or "",
                               relation=relation, node_type=node_type,
                               ref_label=ref_label)
    except (KeyError, IndexError, ValueError):
        return template


def _shortest_path(index: dict[str, Any], source: str, target: str,
                   max_hops: int) -> list[dict[str, Any]] | None:
    if source == target:
        return []
    frontier: list[tuple[str, list[dict[str, Any]]]] = [(source, [])]
    visited = {source}
    for _hop in range(max(1, max_hops)):
        next_frontier: list[tuple[str, list[dict[str, Any]]]] = []
        for node_id, trail in frontier:
            for edge in index["adjacency"].get(node_id, []):
                other = edge["target_id"] if edge["source_id"] == node_id else edge["source_id"]
                if other in visited:
                    continue
                new_trail = trail + [edge]
                if other == target:
                    return new_trail
                visited.add(other)
                next_frontier.append((other, new_trail))
        frontier = next_frontier
        if not frontier:
            break
    return None


def auto_query_defs(index: dict[str, Any], nodes: list[dict[str, Any]],
                    edges: list[dict[str, Any]], stats: dict[str, Any],
                    conflicts: list[dict[str, Any]],
                    documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic query set, used when the agent declared fewer than three.

    The set answers the questions the topic statement grades: what is connected
    to a node, which methods are evidenced for a material, which paper reports a
    metric and with which doc-scoped numbers, where a claim is stated, whether the
    two papers share a physical sample, and what could not be determined.
    """
    by_id = index["by_id"]
    definitions: list[dict[str, Any]] = []
    degree: dict[str, int] = {}
    for edge in edges:
        degree[edge["source_id"]] = degree.get(edge["source_id"], 0) + 1
        degree[edge["target_id"]] = degree.get(edge["target_id"], 0) + 1

    def ranked(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(candidates, key=lambda node: (-degree.get(node["node_id"], 0),
                                                    node["node_id"]))

    entities = [node for node in nodes if node["type"] != "conclusion"] or list(nodes)
    if entities:
        top = ranked(entities)[0]
        definitions.append({
            "query_id": "auto-q1",
            "natural_language": (f"与「{top['label']}」直接相连的实体有哪些？"
                                 f"它们的关系依据是什么？"),
            "op": "neighbors", "params": {"entity": top["node_id"]},
        })
    asserted_edges = [edge for edge in edges if edge["relation"] != "co_occurs"]
    if asserted_edges:
        relation_counts: dict[str, int] = {}
        for edge in asserted_edges:
            relation_counts[edge["relation"]] = relation_counts.get(edge["relation"], 0) + 1
        top_relation = sorted(relation_counts, key=lambda rel: (-relation_counts[rel], rel))[0]
        definitions.append({
            "query_id": "auto-q2",
            "natural_language": (f"图谱中所有「{top_relation}」关系分别有哪些，"
                                 f"证据在哪一段？"),
            "op": "relation", "params": {"relation": top_relation},
        })
        top_edge = sorted(asserted_edges, key=lambda edge: edge["edge_id"])[0]
        definitions.append({
            "query_id": "auto-q3",
            "natural_language": (f"「{top_edge.get('source_label')}」与"
                                 f"「{top_edge.get('target_label')}」这条 "
                                 f"{top_edge['relation']} 关系的原文依据是什么？"),
            "op": "evidence_lookup",
            "params": {"edge_id": top_edge["edge_id"],
                       "ref_label": (f"{top_edge.get('source_label')} -"
                                     f"{top_edge['relation']}-> "
                                     f"{top_edge.get('target_label')}")},
        })
    elif nodes:
        top_node = sorted(nodes, key=lambda node: node["node_id"])[0]
        definitions.append({
            "query_id": "auto-q2",
            "natural_language": f"材料在哪里提到「{top_node['label']}」？",
            "op": "evidence_lookup", "params": {"node_id": top_node["node_id"],
                                                "ref_label": top_node["label"]},
        })
    materials = ranked([node for node in nodes if node["type"] == "material"])
    if materials:
        material = materials[0]
        definitions.append({
            "query_id": "auto-q4",
            "natural_language": (f"材料「{material['label']}」与哪些方法有证据关系？"
                                 f"依据在哪一段？"),
            "op": "material_methods", "params": {"material": material["node_id"]},
        })
    metrics = ranked([node for node in nodes if node["type"] == "metric"])
    if metrics:
        metric = metrics[0]
        definitions.append({
            "query_id": "auto-q5",
            "natural_language": (f"哪些论文报告了指标「{metric['label']}」，"
                                 f"各自的数值是多少？"),
            "op": "metric_reporters", "params": {"metric": metric["node_id"]},
        })
    conclusions = sorted((node for node in nodes if node["type"] == "conclusion"),
                         key=lambda node: node["node_id"])
    if conclusions:
        definitions.append({
            "query_id": "auto-q6",
            "natural_language": (f"哪一段原文支持结论「{conclusions[0]['label'][:60]}」，"
                                 f"原句是什么？"),
            "op": "conclusion_evidence",
            "params": {"conclusion": conclusions[0]["node_id"]},
        })
    definitions.append({
        "query_id": "auto-q7",
        "natural_language": ("两篇材料共同出现的概念或方法有哪些？"
                             "（只在概念层对齐，不假设同一物理样品）"),
        "op": "cross_doc_shared", "params": {"node_type": "method"},
    })
    definitions.append({
        "query_id": "auto-q8",
        "natural_language": "两篇论文是否使用了相同的物理样品、数据集或实验条件？",
        "op": "cross_doc_consistency", "params": {},
    })
    definitions.append({
        "query_id": "auto-q9",
        "natural_language": "哪些关系或合并存在冲突、证据不足，无法确定？",
        "op": "unresolved", "params": {},
    })
    return definitions


# --------------------------------------------------------------------------- #
# public payloads, quality metrics, exports
# --------------------------------------------------------------------------- #
def public_node(node: dict[str, Any]) -> dict[str, Any]:
    occurrences = node.get("occurrences") or []
    return {
        "node_id": node["node_id"],
        "label": node["label"],
        "type": node["type"],
        "aliases": node.get("aliases") or [],
        "doc_ids": node.get("doc_ids") or [],
        "paragraph_ids": sorted({str(anchor.get("paragraph_id"))
                                 for anchor in occurrences if anchor.get("paragraph_id")}),
        "evidence": [public_anchor(anchor) for anchor in occurrences[:3]],
        "evidence_count": len(occurrences),
        "evidence_status": node.get("evidence_status", "unverified"),
        "origin": node.get("origin"),
        "type_source": node.get("type_source"),
        "is_conclusion": bool(node.get("is_conclusion")),
        "cross_doc_merged": bool(node.get("cross_doc_merged")),
        "scope": node.get("scope") or "concept",
    }


def public_edge(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "edge_id": edge["edge_id"],
        "source": edge["source_id"],
        "target": edge["target_id"],
        "source_label": edge.get("source_label"),
        "target_label": edge.get("target_label"),
        "relation": edge["relation"],
        "relation_raw": edge.get("relation_raw"),
        "relation_known": bool(edge.get("relation_known")),
        "direction": edge["direction"],
        "condition": edge.get("condition") or "",
        "strength": edge.get("strength"),
        "strength_score": round(float(edge.get("strength_score") or 0.6), 2),
        "doc_id": edge.get("doc_id"),
        "paragraph_id": edge.get("paragraph_id"),
        "seg_id": edge.get("seg_id"),
        "quote": edge.get("quote") or "",
        "evidence": [public_anchor(anchor) for anchor in (edge.get("anchors") or [])[:2]],
        "evidence_status": edge.get("evidence_status", "unverified"),
        "origin": edge.get("origin"),
    }


def evidence_index(nodes: list[dict[str, Any]], edges: list[dict[str, Any]],
                   node_verdicts: dict[str, Any], edge_verdicts: dict[str, Any]
                   ) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for node in nodes:
        verdict = node_verdicts.get(node["node_id"]) or {}
        checks = verdict.get("checks") or [{}]
        match = checks[0].get("match") if checks else None
        for anchor in node.get("occurrences") or []:
            entries.append({
                "kind": "node",
                "ref_id": node["node_id"],
                "label": node["label"],
                **public_anchor(anchor),
                "match": match,
                "supported_by_quote": bool(verdict.get("supported_by_quote")),
                "grounded": bool(verdict.get("grounded")),
            })
    for edge in edges:
        verdict = edge_verdicts.get(edge["edge_id"]) or {}
        checks = verdict.get("checks") or [{}]
        match = checks[0].get("match") if checks else None
        for anchor in (edge.get("anchors") or [])[:2]:
            entries.append({
                "kind": "edge",
                "ref_id": edge["edge_id"],
                "label": f"{edge.get('source_label')} {edge['relation']} {edge.get('target_label')}",
                **public_anchor(anchor),
                "match": match,
                "supported_by_quote": bool(verdict.get("supported_by_quote")),
                "grounded": bool(verdict.get("grounded")),
            })
    entries.sort(key=lambda item: (item["kind"], item["ref_id"], item.get("doc_id") or "",
                                   str(item.get("paragraph_id") or "")))
    for index, entry in enumerate(entries, start=1):
        entry["evidence_id"] = f"ev-{index:04d}"
    return entries


def quality_metrics(nodes: list[dict[str, Any]], edges: list[dict[str, Any]],
                    documents: list[dict[str, Any]], segments: list[dict[str, Any]],
                    grounding: dict[str, Any], generated_usage: dict[str, Any],
                    incremental: dict[str, Any], merge_log: list[dict[str, Any]],
                    conflicts: list[dict[str, Any]], query_summary: dict[str, Any],
                    schema: dict[str, Any], fallbacks: list[str]) -> dict[str, Any]:
    type_counts: dict[str, int] = {}
    for node in nodes:
        type_counts[node["type"]] = type_counts.get(node["type"], 0) + 1
    relation_counts: dict[str, int] = {}
    for edge in edges:
        relation_counts[edge["relation"]] = relation_counts.get(edge["relation"], 0) + 1
    required_types = [node_type for node_type in TYPE_ORDER
                      if node_type not in {"other"}]
    present_types = sorted(type_counts)
    required_relations = sorted((schema.get("relation_types") or {}).keys())
    present_relations = sorted(relation_counts)
    nodes_with_evidence = sum(1 for node in nodes if node.get("occurrences"))
    edges_with_evidence = sum(1 for edge in edges if edge.get("anchors"))
    return {
        "documents": len(documents),
        "paragraphs": sum(int(doc.get("paragraph_count") or 0) for doc in documents),
        "segments": len(segments),
        "nodes": len(nodes),
        "edges": len(edges),
        "node_type_counts": dict(sorted(type_counts.items())),
        "relation_type_counts": dict(sorted(relation_counts.items())),
        "entity_type_coverage": {
            "required": required_types,
            "present": present_types,
            "ratio": round(len([t for t in present_types if t in required_types]) /
                           len(required_types), 4) if required_types else 0.0,
        },
        "relation_type_coverage": {
            "schema_types": required_relations,
            "present": present_relations,
            "ratio": round(len([r for r in present_relations if r in required_relations]) /
                           len(required_relations), 4) if required_relations else 0.0,
            "off_schema_relations": sorted(r for r in present_relations
                                           if r not in required_relations),
        },
        "cross_doc": {
            "documents": len(documents),
            "shared_nodes": sum(1 for node in nodes if len(node.get("doc_ids") or []) > 1),
            "shared_node_types": sorted({node["type"] for node in nodes
                                         if len(node.get("doc_ids") or []) > 1}),
        },
        "grounding": grounding,
        "alias_merge": {
            "merge_groups": len(merge_log),
            "merged_labels": sum(len(entry["merged_labels"]) for entry in merge_log),
            "conflicts": len(conflicts),
        },
        "coverage": {
            "nodes_with_evidence": nodes_with_evidence,
            "edges_with_evidence": edges_with_evidence,
            "node_evidence_ratio": round(nodes_with_evidence / len(nodes), 4) if nodes else 0.0,
            "edge_evidence_ratio": round(edges_with_evidence / len(edges), 4) if edges else 0.0,
        },
        "generated": generated_usage,
        "queries": query_summary,
        "incremental": incremental,
        "fallbacks": fallbacks,
        "deterministic": True,
        "schema_version": schema.get("version"),
    }


def _xml(value: Any) -> str:
    text = "" if value is None else str(value)
    import xml.sax.saxutils as saxutils
    return saxutils.escape(text, {'"': "&quot;"})


def export_graph(out_dir: str | Path, case_id: str | None, nodes: list[dict[str, Any]],
                 edges: list[dict[str, Any]], merge_log: list[dict[str, Any]],
                 conflicts: list[dict[str, Any]], documents: list[dict[str, Any]],
                 stats: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Write graph.json / nodes.csv / edges.csv / graph.graphml (hand-written XML)."""
    out = Path(out_dir)
    public_nodes = [public_node(node) for node in nodes]
    public_edges = [public_edge(edge) for edge in edges]
    hybrid.write_json(out / "graph.json", {
        "case_id": case_id,
        "schema_version": schema.get("version"),
        "nodes": public_nodes,
        "edges": public_edges,
        "merge_log": merge_log,
        "conflicts": conflicts,
        "documents": documents,
        "stats": stats,
        "locator_note": ("anchors use the publisher paragraph id; page is only reported "
                         "when the material really is a PDF"),
    })
    nodes_csv = out / "nodes.csv"
    with nodes_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["node_id", "label", "type", "aliases", "doc_ids",
                         "evidence_count", "origin", "is_conclusion"])
        for node in public_nodes:
            writer.writerow([node["node_id"], node["label"], node["type"],
                             "|".join(node["aliases"]), "|".join(node["doc_ids"]),
                             node["evidence_count"], node["origin"],
                             "1" if node["is_conclusion"] else "0"])
    edges_csv = out / "edges.csv"
    with edges_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["edge_id", "source", "source_label", "target", "target_label",
                         "relation", "direction", "condition", "strength",
                         "strength_score", "doc_id", "paragraph_id", "quote"])
        for edge in public_edges:
            writer.writerow([edge["edge_id"], edge["source"], edge["source_label"],
                             edge["target"], edge["target_label"], edge["relation"],
                             edge["direction"], edge["condition"], edge["strength"],
                             edge["strength_score"], edge["doc_id"],
                             edge["paragraph_id"], edge["quote"]])
    node_keys = [("label", "string"), ("type", "string"), ("aliases", "string"),
                 ("doc_ids", "string"), ("origin", "string"), ("paragraph_ids", "string")]
    edge_keys = [("relation", "string"), ("direction", "string"), ("condition", "string"),
                 ("strength", "string"), ("doc_id", "string"), ("paragraph_id", "string"),
                 ("quote", "string")]
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<graphml xmlns="http://graphml.graphdrawing.org/xmlns" '
             'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
             'xsi:schemaLocation="http://graphml.graphdrawing.org/xmlns '
             'http://graphml.graphdrawing.org/xmlns/1.0/graphml.xsd">']
    for index, (name, kind) in enumerate(node_keys, start=1):
        lines.append(f'  <key id="n{index}" for="node" attr.name="{name}" attr.type="{kind}"/>')
    for index, (name, kind) in enumerate(edge_keys, start=1):
        lines.append(f'  <key id="e{index}" for="edge" attr.name="{name}" attr.type="{kind}"/>')
    lines.append('  <graph id="knowledge-graph" edgedefault="directed">')
    for node in public_nodes:
        lines.append(f'    <node id="{_xml(node["node_id"])}">')
        values = [node["label"], node["type"], "|".join(node["aliases"]),
                  "|".join(node["doc_ids"]), node["origin"],
                  "|".join(node["paragraph_ids"])]
        for index, value in enumerate(values, start=1):
            lines.append(f'      <data key="n{index}">{_xml(value)}</data>')
        lines.append('    </node>')
    for edge in public_edges:
        lines.append(f'    <edge id="{_xml(edge["edge_id"])}" source="{_xml(edge["source"])}" '
                     f'target="{_xml(edge["target"])}">')
        values = [edge["relation"], edge["direction"], edge["condition"], edge["strength"],
                  edge["doc_id"], edge["paragraph_id"], edge["quote"]]
        for index, value in enumerate(values, start=1):
            lines.append(f'      <data key="e{index}">{_xml(value)}</data>')
        lines.append('    </edge>')
    lines.append("  </graph>")
    lines.append("</graphml>")
    graphml = out / "graph.graphml"
    graphml.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "graph_json": "graph.json",
        "nodes_csv": "nodes.csv",
        "edges_csv": "edges.csv",
        "graphml": "graph.graphml",
        "node_count": len(public_nodes),
        "edge_count": len(public_edges),
    }
