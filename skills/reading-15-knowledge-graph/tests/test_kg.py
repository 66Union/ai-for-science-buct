#!/usr/bin/env python3
"""Self-test for the reading-15 knowledge-graph skill (standard library only).

Runs with a synthetic two-paper JATS-style pack and a plain-text pack, exercises
prepare -> agent generated files -> finalize, the deterministic fallback when the
agent produced nothing, the unverifiable-evidence path, the contract reflection
and the repeat-run determinism. Temporary files live under the skill directory
because a restricted sandbox cannot write to %TEMP%.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
try:  # packaged layout: <skill-root>/lib
    sys.path.insert(0, str(ROOT))
    from lib import hybrid  # noqa: F401
except ImportError:  # dev layout: skills/<topic>/ + skills/_core/lib
    sys.path.insert(0, str(ROOT.parent / "_core"))
    from lib import hybrid  # noqa: F401

sys.path.insert(0, str(ROOT / "scripts"))
import run_kg  # noqa: E402

TMP = ROOT / "_selftest_kg"
FAILS: list[str] = []


def norm_key(text: str) -> str:
    return "".join(ch for ch in str(text or "").casefold()
                   if ch.isalnum() or "一" <= ch <= "鿿")


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name}  {detail}")


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path, default=None):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def run(args: list[str], cwd: Path) -> int:
    previous = os.getcwd()
    os.chdir(cwd)
    try:
        return run_kg.main(args)
    finally:
        os.chdir(previous)


# --------------------------------------------------------------------------- #
# synthetic fixtures
# --------------------------------------------------------------------------- #
PAPER_A = {
    "doc_id": "paper-a",
    "title": "Nickel Catalysts for CO2 Methanation",
    "doi": "10.1000/kg.a",
    "authors": ["Alice Zhang", "Bo Li"],
    "license": "CC-BY-4.0",
    "paragraphs": [
        {"paragraph_id": "A-p1", "section": "Abstract",
         "text": "We report a Ni/SiO2 catalyst for CO2 methanation that reaches 85% "
                 "CO2 conversion at 350 C and 1 bar. The selectivity to CH4 exceeds 99% "
                 "over 100 h on stream."},
        {"paragraph_id": "A-p2", "section": "Introduction",
         "text": "CO2 methanation is a key reaction for power-to-gas. Supported nickel "
                 "catalysts are widely used because of their high activity and low cost."},
        {"paragraph_id": "A-p3", "section": "Methods",
         "text": "The Ni/SiO2 catalyst was prepared by incipient wetness impregnation with "
                 "a nickel loading of 12 wt%. Samples were characterized by X-ray "
                 "diffraction (XRD) and transmission electron microscopy (TEM)."},
        {"paragraph_id": "A-p4", "section": "Results",
         "text": "XRD patterns confirm the formation of metallic Ni nanoparticles after "
                 "reduction at 500 C. The catalyst shows a CH4 selectivity of 99.2% at 350 C."},
        {"paragraph_id": "A-p5", "section": "Conclusions",
         "text": "We conclude that the Ni/SiO2 catalyst is stable for 100 h and that CO2 "
                 "conversion is limited by the reverse water-gas shift reaction at high "
                 "temperature."},
    ],
}
PAPER_B = {
    "doc_id": "paper-b",
    "title": "Cobalt Catalysts for CO2 Hydrogenation",
    "doi": "10.1000/kg.b",
    "authors": ["Cara Wang", "Dan Zhao"],
    "license": "CC-BY-4.0",
    "paragraphs": [
        {"paragraph_id": "B-p1", "section": "Abstract",
         "text": "A Co/TiO2 catalyst was tested for CO2 hydrogenation at 300 C and 10 bar, "
                 "reaching 60% CO2 conversion with 80% CH4 selectivity."},
        {"paragraph_id": "B-p2", "section": "Introduction",
         "text": "CO2 hydrogenation to methane is a promising route. Cobalt catalysts are "
                 "an alternative to nickel catalysts."},
        {"paragraph_id": "B-p3", "section": "Methods",
         "text": "The Co/TiO2 catalyst was synthesized by co-precipitation. XRD and XPS "
                 "were used to characterize the catalyst. The CO2 hydrogenation reaction "
                 "was evaluated at 300 C."},
        {"paragraph_id": "B-p4", "section": "Results",
         "text": "The Co/TiO2 catalyst shows higher CH4 selectivity than the Ni/SiO2 "
                 "catalyst under 10 bar, but its CO2 conversion is lower."},
        {"paragraph_id": "B-p5", "section": "Conclusions",
         "text": "We conclude that cobalt catalysts are an alternative to nickel catalysts "
                 "for CO2 methanation, although the reaction conditions differ."},
    ],
}
CASE = {
    "case_id": "kg-synthetic-001",
    "task": "Build an evidence-bound knowledge graph from the two papers.",
    "output_contract": {
        "graph_nodes": {"type": "array", "description": "nodes with aliases and evidence"},
        "relationship_edges": {"type": "array", "description": "relation type, direction, condition"},
        "entity_alias_map": {"type": "array", "description": "merge decisions and conflicts"},
        "query_definition_list": {"type": "array"},
        "query_outcome_bundle": {"type": "array"},
        "source_evidence_anchors": {"type": "array"},
        "graph_audit_metrics": {"type": "object"},
        "documents_index": {"type": "array"},
        "conflict_register": {"type": "array"},
        "not_in_material_field": {"type": "string", "description": "never produced"},
    },
}
MANIFEST = {
    "corpus": "synthetic-kg-demo",
    "license": "CC-BY-4.0",
    "papers": [
        {"doc_id": "paper-a", "doi": "10.1000/kg.a",
         "title": "Nickel Catalysts for CO2 Methanation",
         "authors": ["Alice Zhang", "Bo Li"], "year": "2020",
         "license": "CC-BY-4.0", "source_file": "primary-material.json"},
        {"doc_id": "paper-b", "doi": "10.1000/kg.b",
         "title": "Cobalt Catalysts for CO2 Hydrogenation",
         "authors": ["Cara Wang", "Dan Zhao"], "year": "2021",
         "license": "CC-BY-4.0", "source_file": "secondary-material.json"},
    ],
}


def build_json_fixture(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    write_json(base / "case.json", CASE)
    write_json(base / "corpus-manifest.json", MANIFEST)
    write_json(base / "primary-material.json", PAPER_A)
    write_json(base / "secondary-material.json", PAPER_B)
    write_json(base / "material.json", {
        "corpus": "synthetic-kg-demo",
        "papers": [
            {"doc_id": "paper-a", "paragraph_ids": [p["paragraph_id"] for p in PAPER_A["paragraphs"]]},
            {"doc_id": "paper-b", "paragraph_ids": [p["paragraph_id"] for p in PAPER_B["paragraphs"]]},
        ],
    })
    (base / "task.md").write_text(
        "# Synthetic task\n\nBuild an evidence-bound knowledge graph from the two papers.\n",
        encoding="utf-8")
    return base


def build_text_fixture(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    write_json(base / "case.json", {
        "case_id": "kg-text-001",
        "output_contract": {
            "nodes_list": {"type": "array"},
            "edges_list": {"type": "array"},
            "queries_list": {"type": "array"},
            "query_output": {"type": "array"},
            "evidence_records": {"type": "array"},
            "metrics": {"type": "object"},
        },
    })
    (base / "task.md").write_text("# text task\n", encoding="utf-8")
    (base / "first-paper.md").write_text(
        "Nickel catalysts for CO2 methanation\n\n"
        "We used a Ni/SiO2 catalyst for CO2 methanation at 350 C. "
        "The catalyst shows 85% CO2 conversion and 99% CH4 selectivity.\n\n"
        "The catalyst was characterized by X-ray diffraction (XRD). "
        "XRD confirms metallic Ni nanoparticles.\n",
        encoding="utf-8")
    (base / "second-paper.md").write_text(
        "Cobalt catalysts for CO2 hydrogenation\n\n"
        "A Co/TiO2 catalyst was tested for CO2 hydrogenation at 300 C. "
        "The catalyst reaches 60% CO2 conversion.\n\n"
        "XRD and XPS were used to characterize the Co/TiO2 catalyst. "
        "Cobalt catalysts are an alternative to nickel catalysts.\n",
        encoding="utf-8")
    return base


def segment_with(work: Path, needle: str) -> dict:
    data = read_json(work / "segments.json", {})
    for segment in data.get("segments") or []:
        if needle in (segment.get("text") or ""):
            return segment
    raise AssertionError("no segment contains: " + needle)


def write_generated(work: Path) -> None:
    seg_a1 = segment_with(work, "We report a Ni/SiO2 catalyst")
    seg_a2 = segment_with(work, "CO2 methanation is a key reaction")
    seg_a3 = segment_with(work, "X-ray")
    seg_b1 = segment_with(work, "A Co/TiO2 catalyst was tested")
    seg_b2 = segment_with(work, "CO2 hydrogenation to methane")
    seg_b3 = segment_with(work, "XRD and XPS")
    seg_b4 = segment_with(work, "higher CH4 selectivity")
    nodes = [
        {"node_id": "a1", "label": "CO2 methanation", "type": "task",
         "aliases": ["CO2 hydrogenation"], "doc_id": "doc-01",
         "seg_id": seg_a2["seg_id"],
         "quote": "CO2 methanation is a key reaction for power-to-gas."},
        {"node_id": "a2", "label": "CO2 hydrogenation", "type": "task",
         "doc_id": "doc-02", "seg_id": seg_b2["seg_id"],
         "quote": "CO2 hydrogenation to methane is a promising route."},
        {"node_id": "a3", "label": "X-ray diffraction", "type": "method",
         "aliases": ["XRD"], "doc_id": "doc-01", "seg_id": seg_a3["seg_id"],
         "quote": "Samples were characterized by X-ray diffraction (XRD)"},
        {"node_id": "a4", "label": "XRD", "type": "method", "doc_id": "doc-02",
         "seg_id": seg_b3["seg_id"],
         "quote": "XRD and XPS were used to characterize the catalyst."},
        {"node_id": "a5", "label": "Ni/SiO2 catalyst", "type": "material",
         "doc_id": "doc-01", "seg_id": seg_a1["seg_id"],
         "quote": "We report a Ni/SiO2 catalyst for CO2 methanation"},
        {"node_id": "a6", "label": "Co/TiO2 catalyst", "type": "material",
         "doc_id": "doc-02", "seg_id": seg_b1["seg_id"],
         "quote": "A Co/TiO2 catalyst was tested for CO2 hydrogenation"},
        # same surface as node a4 but a different declared type: a homonym that
        # must be kept as a separate node and logged, not force-merged
        {"node_id": "a7", "label": "XRD", "type": "dataset", "doc_id": "doc-02",
         "seg_id": seg_b3["seg_id"],
         "quote": "XRD and XPS were used to characterize the catalyst."},
    ]
    edges = [
        {"edge_id": "x1", "source": "a5", "target": "a1", "relation": "applies_to",
         "direction": "directed", "condition": "at 350 C and 1 bar",
         "doc_id": "doc-01", "seg_id": seg_a1["seg_id"],
         "quote": "We report a Ni/SiO2 catalyst for CO2 methanation that reaches 85% "
                  "CO2 conversion at 350 C and 1 bar.",
         "strength": "strong"},
        {"edge_id": "x2", "source": "a6", "target": "a2", "relation": "evaluates_on",
         "direction": "directed", "condition": "at 300 C and 10 bar",
         "doc_id": "doc-02", "seg_id": seg_b1["seg_id"],
         "quote": "A Co/TiO2 catalyst was tested for CO2 hydrogenation at 300 C and "
                  "10 bar, reaching 60% CO2 conversion with 80% CH4 selectivity.",
         "strength": "strong"},
        {"edge_id": "x3", "source": "a6", "target": "a5", "relation": "outperforms",
         "direction": "directed", "condition": "under 10 bar",
         "doc_id": "doc-02", "seg_id": seg_b4["seg_id"],
         "quote": "The Co/TiO2 catalyst shows higher CH4 selectivity than the Ni/SiO2 "
                  "catalyst under 10 bar, but its CO2 conversion is lower.",
         "strength": "moderate"},
        {"edge_id": "x4", "source": "a3", "target": "a5", "relation": "characterizes",
         "direction": "directed", "condition": "",
         "doc_id": "doc-01", "seg_id": seg_a3["seg_id"],
         "quote": "The Ni/SiO2 catalyst was prepared by incipient wetness impregnation "
                  "with a nickel loading of 12 wt%. Samples were characterized by X-ray "
                  "diffraction (XRD)",
         "strength": "moderate"},
    ]
    queries = [
        {"query_id": "q1", "op": "neighbors", "params": {"entity": "Ni/SiO2 catalyst"},
         "natural_language": "Which entities are connected to the Ni/SiO2 catalyst?"},
        {"query_id": "q2", "op": "cross_doc_shared", "params": {"node_type": "method"},
         "natural_language": "Which methods appear in both papers?"},
        {"query_id": "q3", "op": "relation", "params": {"relation": "outperforms"},
         "natural_language": "Which outperforms relations are supported?"},
        {"query_id": "q4", "op": "cross_doc_consistency", "params": {},
         "natural_language": "Do the papers describe the same sample or conditions?"},
        {"query_id": "q5", "op": "evidence_lookup", "params": {"entity": "Co/TiO2 catalyst"},
         "natural_language": "Where is the Co/TiO2 catalyst named?"},
    ]
    write_json(work / "generated" / "nodes.json", nodes)
    write_json(work / "generated" / "edges.json", edges)
    write_json(work / "generated" / "queries.json", queries)


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
def test_prepare_and_full_graph() -> None:
    print("prepare -> generated -> finalize (JSON pack)")
    base = build_json_fixture(TMP / "fixture-json")
    out = base / "out"
    work = out / "work"
    code = run(["--prepare", "--out", str(out), "--work", str(work)], base)
    check("prepare exit code", code == 0, str(code))
    for name in ("doc_index.json", "segments.json", "entity_candidates.json",
                 "relation_evidence.json", "query_seeds.json", "baseline_graph.json",
                 "agent_protocol.md", "manifest.json"):
        check("prepare wrote " + name, (work / name).is_file())
    batches = sorted((work / "batches").glob("batch-*.json"))
    check("batch worksheets written", len(batches) >= 1, str(len(batches)))
    prepared = read_json(out / "result.json", {})
    check("prepare leaves a usable result", prepared.get("meta", {}).get("status") == "partial",
          str(prepared.get("meta", {}).get("status")))
    check("prepare result already carries contract fields",
          "graph_nodes" in prepared, str(sorted(prepared.keys()))[:200])
    doc_index = read_json(work / "doc_index.json", {})
    docs = doc_index.get("documents") or []
    check("two material documents", len(docs) == 2, str([d.get("doc_id") for d in docs]))
    check("root metadata not copied to the second paper",
          docs[1].get("title") == PAPER_B["title"], str(docs[1].get("title")))
    check("second paper keeps its own DOI", docs[1].get("doi") == "10.1000/kg.b",
          str(docs[1].get("doi")))
    segments = read_json(work / "segments.json", {}).get("segments") or []
    check("paragraph locator used", all(seg.get("page") is None for seg in segments))
    check("paragraph ids preserved",
          {"A-p1", "B-p1"}.issubset({seg.get("paragraph_id") for seg in segments}),
          str(sorted({seg.get("paragraph_id") for seg in segments})[:8]))

    write_generated(work)
    code = run(["--finalize", "--out", str(out), "--work", str(work)], base)
    check("finalize exit code", code == 0, str(code))
    result = read_json(out / "result.json", {})
    contract = CASE["output_contract"]
    check("case_id preserved", result.get("case_id") == CASE["case_id"], str(result.get("case_id")))
    missing = [name for name in contract if name not in result]
    check("every contract field present", not missing, str(missing))
    check("declared order kept",
          [name for name in result if name in contract] == list(contract),
          str([name for name in result if name in contract]))
    check("unproduced field is an empty string placeholder",
          result.get("not_in_material_field") == "", repr(result.get("not_in_material_field")))
    check("unproduced field reported",
          result.get("meta", {}).get("contract_fields_not_reported") == ["not_in_material_field"],
          str(result.get("meta", {}).get("contract_fields_not_reported")))
    mapping = result.get("meta", {}).get("contract_field_mapping") or {}
    expected_mapping = {
        "graph_nodes": "nodes", "relationship_edges": "edges",
        "entity_alias_map": "aliases", "query_definition_list": "queries",
        "query_outcome_bundle": "query_results", "source_evidence_anchors": "evidence",
        "graph_audit_metrics": "quality", "documents_index": "documents",
        "conflict_register": "conflicts",
    }
    mapped_ok = all(mapping.get(key) == value for key, value in expected_mapping.items())
    check("contract fields mapped by stem, not by hardcoded name", mapped_ok, str(mapping))
    check("status success on a complete generated graph",
          result.get("meta", {}).get("status") == "success",
          str(result.get("meta", {}).get("status")) + " " + str(result.get("meta", {}).get("limits")))
    nodes = result.get("graph_nodes") or []
    edges = result.get("relationship_edges") or []
    check("nodes present", len(nodes) >= 6, str(len(nodes)))
    check("edges present", len(edges) >= 4, str(len(edges)))
    labels = {node.get("label") for node in nodes}

    def has_surface(needle: str) -> bool:
        for node in nodes:
            if node.get("label") == needle or needle in (node.get("aliases") or []):
                return True
        return False

    for label in ("Ni/SiO2 catalyst", "Co/TiO2 catalyst", "CO2 methanation"):
        check("generated node surface present: " + label, has_surface(label),
              str(sorted(labels))[:300])
    check("all node evidence is paragraph-anchored, never a fake PDF page",
          all((evidence.get("page") is None and evidence.get("paragraph_id"))
              for node in nodes for evidence in node.get("evidence") or []))
    check("node evidence carries quote and block id",
          all(evidence.get("quote") and evidence.get("block_id")
              for node in nodes for evidence in node.get("evidence") or []))
    merged = result.get("entity_alias_map") or []
    xrd_merge = next((entry for entry in merged
                      if "XRD" in (entry.get("merged_labels") or [])
                      or "xrd" in [label.casefold() for label in entry.get("merged_labels") or []]), None)
    check("acronym/alias merge recorded", xrd_merge is not None, str(merged)[:500])
    if xrd_merge:
        check("merge keeps the full form as label",
              xrd_merge.get("kept_label") == "X-ray diffraction", str(xrd_merge.get("kept_label")))
        check("merge basis recorded", bool(xrd_merge.get("basis")), str(xrd_merge.get("basis")))
        check("merge evidence attached", bool(xrd_merge.get("evidence")))
    outperforms = [edge for edge in edges if edge.get("relation") == "outperforms"
                   and edge.get("source_label") == "Co/TiO2 catalyst"]
    check("generated outperforms edge kept", len(outperforms) == 1, str(len(outperforms)))
    if outperforms:
        edge = outperforms[0]
        check("relation direction recorded", edge.get("direction") == "directed",
              str(edge.get("direction")))
        check("condition recorded", "10 bar" in (edge.get("condition") or ""),
              str(edge.get("condition")))
        check("edge quote contains both endpoints",
              "Co/TiO2" in (edge.get("quote") or "") and "Ni/SiO2" in (edge.get("quote") or ""),
              str(edge.get("quote")))
        check("edge stays doc-scoped", edge.get("doc_id") == "doc-02", str(edge.get("doc_id")))
    conflicts = result.get("conflict_register") or []
    homonyms = [item for item in conflicts if item.get("kind") == "homonym_type_conflict"]
    check("homonym type conflict recorded", bool(homonyms),
          str([item.get("kind") for item in conflicts]))
    xrd_surface = [node for node in nodes
                   if "xrd" in [norm_key(label) for label in
                                [node.get("label")] + list(node.get("aliases") or [])]]
    check("homonym kept as separate nodes", len(xrd_surface) >= 2,
          str([(node.get("label"), node.get("type")) for node in xrd_surface]))
    check("homonym nodes differ by type",
          len({node.get("type") for node in xrd_surface}) >= 2,
          str([(node.get("label"), node.get("type")) for node in xrd_surface]))
    check("homonym conflict names the surface",
          any("XRD" in (item.get("labels") or []) for item in homonyms),
          str([item.get("labels") for item in homonyms]))
    queries = result.get("query_definition_list") or []
    outcomes = result.get("query_outcome_bundle") or []
    check("at least three queries declared", len(queries) >= 3, str(len(queries)))
    check("at least three queries executed", len(outcomes) >= 3, str(len(outcomes)))
    executed = [item for item in outcomes
                if item.get("status") in {"answered", "no_result", "undetermined"}]
    check("executed queries carry answers", all(item.get("answer") for item in executed))
    check("query evidence attached where it exists",
          any(item.get("evidence") for item in executed))
    check("cross-doc identity question answered honestly",
          any(item.get("op") == "cross_doc_consistency"
              and item.get("status") in {"undetermined", "answered", "no_result"}
              for item in outcomes))
    anchors = result.get("source_evidence_anchors") or []
    check("evidence index present", len(anchors) > 0, str(len(anchors)))
    check("evidence index uses paragraph locators",
          all(item.get("locator_kind") == "jats_paragraph_id" for item in anchors),
          str(sorted({item.get("locator_kind") for item in anchors})))
    check("evidence index records grounded verdict",
          all(item.get("grounded") for item in anchors if item.get("kind") == "node"),
          "some node anchor ungrounded")
    metrics = result.get("graph_audit_metrics") or {}
    check("audit metrics contain grounding", "grounding" in metrics, str(list(metrics))[:200])
    check("audit metrics report entity type coverage",
          "entity_type_coverage" in metrics and metrics["entity_type_coverage"].get("present"))
    check("audit metrics report query count", (metrics.get("queries") or {}).get("executed_queries", 0) >= 3)
    doc_index_result = result.get("documents_index") or []
    check("documents index carries both papers", len(doc_index_result) == 2,
          str(len(doc_index_result)))
    check("documents index locator is not a PDF page",
          all(doc.get("locator_kind") == "jats_paragraph_id" for doc in doc_index_result))
    evidence_file = read_json(out / "evidence.json", {})
    check("evidence.json written", isinstance(evidence_file, dict) and bool(evidence_file))
    check("evidence.json has grounding", "grounding" in evidence_file)
    check("evidence.json has node evidence", bool(evidence_file.get("node_evidence")))
    report = read_json(out / "run_report.json", {})
    check("run_report.json written", report.get("status") == "success", str(report.get("status")))
    check("run_report records zero model requests",
          report.get("model_requests_used") == 0)
    graph = read_json(out / "graph.json", {})
    check("graph.json exported", len(graph.get("nodes") or []) >= 6)
    check("graph.json has same node count as result",
          len(graph.get("nodes") or []) == len(nodes),
          str((len(graph.get("nodes") or []), len(nodes))))
    with (out / "nodes.csv").open(encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    check("nodes.csv header + rows", rows[0][0] == "node_id" and len(rows) == len(nodes) + 1,
          str(len(rows)))
    with (out / "edges.csv").open(encoding="utf-8") as handle:
        edge_rows = list(csv.reader(handle))
    check("edges.csv header + rows", edge_rows[0][0] == "edge_id"
          and len(edge_rows) == len(edges) + 1, str(len(edge_rows)))
    tree = ET.parse(out / "graph.graphml")
    graphml_ns = {"g": "http://graphml.graphdrawing.org/xmlns"}
    graphml_nodes = tree.getroot().findall(".//g:node", graphml_ns)
    check("graph.graphml is valid XML with nodes",
          len(graphml_nodes) == len(nodes), str(len(graphml_nodes)))
    check("case.json untouched", read_json(base / "case.json", {}).get("case_id") == CASE["case_id"])


def test_missing_generated_fallback() -> None:
    print("deterministic fallback when the agent produced nothing")
    base = build_json_fixture(TMP / "fixture-fallback")
    out = base / "out"
    code = run(["--all", "--out", str(out)], base)
    check("fallback exit code", code == 0, str(code))
    result = read_json(out / "result.json", {})
    check("fallback status partial", result.get("meta", {}).get("status") == "partial",
          str(result.get("meta", {}).get("status")))
    limits = " ".join(result.get("meta", {}).get("limits") or [])
    check("fallback reason recorded", "no work/generated" in limits, limits[:200])
    nodes = result.get("graph_nodes") or []
    edges = result.get("relationship_edges") or []
    check("baseline nodes still produced", len(nodes) >= 5, str(len(nodes)))
    check("baseline edges still produced", len(edges) >= 1, str(len(edges)))
    check("fallback nodes carry evidence",
          all(node.get("evidence") for node in nodes))
    check("fallback edges are evidence-bound",
          all(edge.get("evidence") and edge.get("quote") for edge in edges))
    check("at least three fallback queries executed",
          len(result.get("query_outcome_bundle") or []) >= 3,
          str(len(result.get("query_outcome_bundle") or [])))
    metrics = result.get("graph_audit_metrics") or {}
    check("fallback recorded in metrics", bool(metrics.get("fallbacks")), str(metrics.get("fallbacks")))


def test_unverifiable_claims_are_dropped() -> None:
    print("unverifiable generated claims are dropped, not silently trusted")
    base = build_json_fixture(TMP / "fixture-unverified")
    out = base / "out"
    work = out / "work"
    code = run(["--prepare", "--out", str(out), "--work", str(work)], base)
    check("prepare exit code", code == 0, str(code))
    seg = segment_with(work, "We report a Ni/SiO2 catalyst")
    write_json(work / "generated" / "nodes.json", [
        {"node_id": "bad1", "label": "Superior Graphene Electrode",
         "type": "material", "doc_id": "doc-01", "seg_id": seg["seg_id"],
         "quote": "Superior Graphene Electrode was measured."},
        {"node_id": "good1", "label": "Ni/SiO2 catalyst", "type": "material",
         "doc_id": "doc-01", "seg_id": seg["seg_id"],
         "quote": "We report a Ni/SiO2 catalyst for CO2 methanation"},
    ])
    write_json(work / "generated" / "edges.json", [
        {"edge_id": "bad-edge", "source": "good1", "target": "bad1",
         "relation": "uses", "doc_id": "doc-01", "seg_id": seg["seg_id"],
         "quote": "We report a Ni/SiO2 catalyst for CO2 methanation"},
    ])
    code = run(["--finalize", "--out", str(out), "--work", str(work)], base)
    check("unverified exit code", code == 0, str(code))
    result = read_json(out / "result.json", {})
    labels = {node.get("label") for node in result.get("graph_nodes") or []}
    check("fabricated entity absent from the graph", "Superior Graphene Electrode" not in labels,
          str(sorted(labels))[:300])
    check("real entity kept", "Ni/SiO2 catalyst" in labels)
    check("status partial after dropping a claim",
          result.get("meta", {}).get("status") == "partial",
          str(result.get("meta", {}).get("status")))
    report = read_json(out / "run_report.json", {})
    check("drop recorded in run report",
          any("unverifiable" in str(item) or "dropped" in str(item)
              for item in (report.get("limits") or []) + (report.get("uncertainty") or [])),
          str(report.get("limits"))[:300])
    evidence_file = read_json(out / "evidence.json", {})
    check("drop listed in evidence.json", bool(evidence_file.get("unverified")),
          str(len(evidence_file.get("unverified") or [])))


def test_plain_text_material() -> None:
    print("plain-text material pack")
    base = build_text_fixture(TMP / "fixture-text")
    out = base / "out"
    code = run(["--all", "--out", str(out)], base)
    check("text run exit code", code == 0, str(code))
    result = read_json(out / "result.json", {})
    nodes = result.get("nodes_list") or []
    check("text run produced nodes", len(nodes) >= 3, str(len(nodes)))
    check("text run kept case_id", result.get("case_id") == "kg-text-001")
    check("text anchors do not claim a PDF page",
          all(evidence.get("page") is None
              for node in nodes for evidence in node.get("evidence") or []),
          "page must be null for non-PDF material")
    check("text locator kind recorded",
          all(evidence.get("locator_kind") in {"block"} for node in nodes
              for evidence in node.get("evidence") or []),
          str({evidence.get("locator_kind") for node in nodes
               for evidence in node.get("evidence") or []}))
    check("text run executed queries",
          len(result.get("query_output") or []) >= 3,
          str(len(result.get("query_output") or [])))


def test_no_material() -> None:
    print("no material at all")
    base = TMP / "fixture-empty"
    base.mkdir(parents=True, exist_ok=True)
    write_json(base / "case.json", {"case_id": "kg-empty-001",
                                    "output_contract": {"nodes": {"type": "array"},
                                                        "notes": {"type": "string"}}})
    out = base / "out"
    code = run(["--all", "--out", str(out)], base)
    check("empty run still writes a result", (out / "result.json").is_file())
    check("empty run is honest about failure", code in (0, 1), str(code))
    result = read_json(out / "result.json", {})
    check("empty run status failed", result.get("meta", {}).get("status") == "failed",
          str(result.get("meta", {}).get("status")))
    check("empty run keeps declared shape", result.get("nodes") == [] and result.get("notes") == "",
          str({key: result.get(key) for key in ("nodes", "notes")}))


def test_determinism() -> None:
    print("determinism of repeated runs")
    base = build_json_fixture(TMP / "fixture-determinism")
    out_a = base / "out-a"
    out_b = base / "out-b"
    run(["--all", "--out", str(out_a)], base)
    run(["--all", "--out", str(out_b)], base)
    for name in ("graph.json", "nodes.csv", "edges.csv"):
        left = (out_a / name).read_bytes()
        right = (out_b / name).read_bytes()
        check("byte-identical " + name, left == right, f"{len(left)} vs {len(right)} bytes")
    result_a = read_json(out_a / "result.json", {})
    result_b = read_json(out_b / "result.json", {})
    for result in (result_a, result_b):
        (result.get("meta") or {}).pop("output_files", None)
    check("result.json content identical across runs",
          json.dumps(result_a, ensure_ascii=False, sort_keys=True)
          == json.dumps(result_b, ensure_ascii=False, sort_keys=True))
    report_a = read_json(out_a / "run_report.json", {})
    check("run report records elapsed time separately",
          "elapsed_s" in report_a and "elapsed_s" not in json.dumps(result_a))


def main() -> int:
    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    try:
        test_prepare_and_full_graph()
        test_missing_generated_fallback()
        test_unverifiable_claims_are_dropped()
        test_plain_text_material()
        test_no_material()
        test_determinism()
    except BaseException:
        traceback.print_exc()
        FAILS.append("unexpected exception")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)} check(s):")
        for name in FAILS:
            print(" -", name)
        return 1
    print("all reading-15 checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
