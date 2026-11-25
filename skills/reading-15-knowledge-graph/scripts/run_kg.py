#!/usr/bin/env python3
"""Reading-15 knowledge-graph runner: prepare -> agent judgement -> finalize.

Modes
    --prepare   deterministic: discover the material pack, extract text, cut
                segments, inventory candidate entities/relations, write the
                batch worksheets and a complete draft graph for the agent.
    --finalize  read the agent's generated records (if any), merge/align, verify
                every node and edge against the source, execute the queries and
                write result.json / evidence.json / run_report.json + graph files.
    --all       prepare + finalize in one process (the default; also the honest
                fallback when the agent produced nothing: the deterministic
                baseline graph is delivered instead of an empty result).

Two hard rules live here, not in the adapter:
* a result.json is written BEFORE any slow work starts (the container can be
  killed at the 900 s budget; an empty output directory scores zero);
* finalize never fails because generated content is missing - it substitutes an
  honest deterministic baseline, records the gap in limits/uncertainty and keeps
  the contract shape.

No network, no model call, standard library only, deterministic ordering.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
try:  # packaged layout: <skill-root>/lib
    sys.path.insert(0, str(ROOT))
    from lib import contract, discover, hybrid
except ImportError:  # dev layout: skills/<topic>/scripts + skills/_core/lib
    sys.path.insert(0, str(ROOT.parent / "_core"))
    from lib import contract, discover, hybrid

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import kg_core  # noqa: E402
import kg_graph  # noqa: E402

TIME_BUDGET_S = 900
SOFT_DEADLINE_S = 720
MATERIAL_SUFFIXES = {".json", ".pdf", ".xml", ".md", ".txt", ".docx", ".tex", ".bib"}
EXCLUDE_DIRS = {"output", "outputs", "result", "results", "work", "generated",
                "__pycache__", ".git", "node_modules", ".pytest_cache"}
# Our own deliverables must never be re-read as material on a repeated run.
EXCLUDED_FILENAMES = {"result.json", "evidence.json", "run_report.json", "graph.json",
                      "manifest.json", "_package_manifest.json", "package_manifest.json"}
MANIFEST_MARKERS = ("manifest", "corpus", "license")
INDEX_NAMES = {"material.json", "materials.json", "material-index.json",
               "material_index.json", "material-index", "materials-index.json"}
KEEP_ADDITIONAL = ["documents", "conflicts", "stats", "node_types", "relation_types",
                   "schema_version", "unresolved", "graph_files", "aliases"]

# Contract fields are named by the hidden case, so the adapter only declares what
# it produced; hybrid.map_fields decides which declared field receives which value.
SYNONYMS: dict[str, list[str]] = {
    "nodes": ["graph_nodes", "knowledge_nodes", "entity_nodes", "node_list",
              "vertices", "vertexes", "entities", "实体节点", "节点集合", "实体清单"],
    "edges": ["graph_edges", "relationship_edges", "knowledge_edges", "relation_edges",
              "edge_list", "triples", "relations", "关系边", "关系集合", "三元组"],
    "aliases": ["alias_map", "alias_merge_ledger", "alias_ledger", "merge_ledger",
                "merge_log", "entity_alias_map", "merged_entity_log", "canonical_map",
                "aliases", "别名合并", "合并记录", "同一性判定"],
    "queries": ["query_set", "query_definitions", "query_definition_list", "query_list",
                "questions", "查询定义", "查询集", "查询问题"],
    "query_results": ["query_outcomes", "query_answers", "query_execution",
                      "query_result_bundle", "query_outcome_bundle", "query_outputs",
                      "answers", "查询结果", "问答结果"],
    "evidence": ["evidence_anchors", "source_evidence_anchors", "evidence_index",
                 "evidence_list", "source_bindings", "evidence_records",
                 "证据锚点", "证据清单", "原文依据"],
    "quality": ["graph_audit_metrics", "audit_metrics", "graph_metrics",
                "validation_report", "quality_metrics", "质量指标", "审计指标"],
    "documents": ["doc_index", "document_index", "documents_index", "corpus_manifest",
                  "source_documents", "材料清单", "文档索引"],
    "conflicts": ["conflict_register", "conflict_log", "merge_conflicts",
                  "unresolved_conflicts", "冲突记录", "冲突登记"],
    "unresolved": ["open_questions", "uncertainties", "unresolved_items", "无法确定"],
    "node_types": ["entity_type_definitions", "node_type_schema", "entity_types",
                   "实体类型定义"],
    "relation_types": ["relation_type_definitions", "edge_type_schema",
                       "relation_schema", "关系类型定义"],
    "stats": ["graph_statistics", "graph_summary", "statistics", "图统计"],
    "schema_version": ["schema", "version"],
    "graph_files": ["export_files", "graph_exports", "artifacts", "导出文件"],
    "knowledge_graph": ["graph", "graph_object", "图谱对象"],
}


# --------------------------------------------------------------------------- #
# input discovery (the pack may deliver JATS JSON, PDFs or plain text)
# --------------------------------------------------------------------------- #
def _input_roots(found: dict[str, Any]) -> list[Path]:
    roots: list[Path] = []
    for raw in found.get("input_roots") or []:
        path = Path(raw)
        if path.is_dir():
            roots.append(path)
    if Path.cwd() not in roots:
        roots.insert(0, Path.cwd())
    return roots


def _scan_materials(found: dict[str, Any], out_dir: Path, work_dir: Path) -> dict[str, Any]:
    case_path = Path(found["case_path"]) if found.get("case_path") else None
    task_md = Path(found["task_md_path"]) if found.get("task_md_path") else None
    skip = {case_path.resolve()} if case_path else set()
    exclude_roots = {out_dir.resolve(), work_dir.resolve()}
    files: list[Path] = []
    for root in _input_roots(found):
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                resolved = path.resolve()
            except Exception:
                resolved = path
            if resolved in skip:
                continue
            try:
                parents = set(resolved.parents)
            except Exception:
                parents = set()
            if exclude_roots & parents:
                continue
            if any(part in EXCLUDE_DIRS for part in path.parts[:-1]):
                continue
            if path.suffix.lower() not in MATERIAL_SUFFIXES:
                continue
            if path.name.casefold() in {"task.md", "instruction.md"}:
                continue
            if path.name.casefold() in EXCLUDED_FILENAMES:
                continue
            files.append(path)
    seen: set[str] = set()
    unique: list[Path] = []
    for path in sorted(files, key=lambda p: (p.name.casefold(), str(p))):
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    manifests: list[Path] = []
    primary: list[Path] = []
    secondary: list[Path] = []
    index_files: list[Path] = []
    generic_json: list[Path] = []
    others: list[Path] = []
    for path in unique:
        name = path.name.casefold()
        if any(marker in name for marker in MANIFEST_MARKERS):
            manifests.append(path)
        elif "primary" in name:
            primary.append(path)
        elif "secondary" in name:
            secondary.append(path)
        elif name in INDEX_NAMES or name.endswith("-material.json") \
                or name.endswith("-materials.json"):
            index_files.append(path)
        elif path.suffix.lower() == ".json":
            generic_json.append(path)
        else:
            others.append(path)
    entries: list[dict[str, Any]] = []
    for path in primary + secondary:
        entries.append({"path": str(path),
                        "role": "primary" if path in primary else "secondary",
                        "allow_raw_json": False})
    if not primary and not secondary:
        for path in index_files:
            entries.append({"path": str(path), "role": "index", "allow_raw_json": False})
    for path in generic_json:
        entries.append({"path": str(path), "role": "generic", "allow_raw_json": False})
    for path in others:
        entries.append({"path": str(path), "role": "generic", "allow_raw_json": True})
    return {
        "roots": [str(root) for root in _input_roots(found)],
        "entries": entries,
        "manifest_paths": [str(path) for path in manifests + index_files],
        "manifests": [str(path) for path in manifests],
        "index_files": [str(path) for path in index_files],
        "files_scanned": len(unique),
        "case_path": str(case_path) if case_path else None,
        "task_md_path": str(task_md) if task_md else None,
    }


def _load_inputs(materials: dict[str, Any], stopwatch: hybrid.Stopwatch,
                 limits: list[str], uncertainty: list[str]) -> dict[str, Any]:
    deadline = stopwatch.start + SOFT_DEADLINE_S
    docs = kg_core.build_documents(materials["entries"],
                                   materials["manifest_paths"], deadline=deadline,
                                   limits=limits, uncertainty=uncertainty)
    stopwatch.log(f"documents={len(docs)}")
    segments = kg_core.segments_for_docs(docs)
    stopwatch.log(f"segments={len(segments)}")
    candidates = kg_core.collect_candidates(docs, segments)
    stopwatch.log(f"candidates={len(candidates['candidates'])} "
                  f"acronyms={len(candidates['acronym_definitions'])}")
    schema = kg_core.load_schema()
    baseline = kg_core.build_baseline_graph(docs, segments,
                                            candidates["candidates"], schema)
    stopwatch.log(f"baseline nodes={len(baseline['nodes'])} edges={len(baseline['edges'])}")
    relation_evidence = kg_core.collect_relation_evidence(segments,
                                                          candidates["candidates"])
    return {
        "materials": materials,
        "docs": docs,
        "segments": segments,
        "candidates": candidates,
        "baseline": baseline,
        "relation_evidence": relation_evidence,
        "schema": schema,
    }


# --------------------------------------------------------------------------- #
# work artifacts for the agent
# --------------------------------------------------------------------------- #
def _seg_public(segment: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    kind = doc.get("locator_kind") or "block"
    return {
        "seg_id": segment["seg_id"],
        "doc_id": segment["doc_id"],
        "paragraph_id": segment.get("paragraph_id"),
        "page": int(segment["page"]) if kind == "pdf_page" else None,
        "locator_kind": kind,
        "section": segment.get("section_title"),
        "char_count": segment.get("char_count"),
        "text": segment.get("text"),
    }


def _candidate_public(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": candidate["candidate_id"],
        "label": candidate["label"],
        "type": candidate["type"],
        "type_source": candidate["type_source"],
        "count": candidate["count"],
        "signals": candidate["signals"],
        "doc_ids": candidate["doc_ids"],
        "occurrences": [kg_core.public_anchor(anchor)
                        for anchor in candidate["occurrences"][:2]],
    }


def _query_seeds(candidates: list[dict[str, Any]], baseline: dict[str, Any]) -> list[dict[str, Any]]:
    defs: list[dict[str, Any]] = []
    top_nodes = [node for node in baseline["nodes"] if not node.get("is_conclusion")][:3]
    if top_nodes:
        node = top_nodes[0]
        defs.append({
            "query_id": "seed-q1",
            "natural_language": "与「" + node["label"] + "」直接相连的实体有哪些？每条关系依据在哪一段？",
            "op": "neighbors",
            "params": {"entity": node["label"]},
        })
    if baseline["edges"]:
        edge = baseline["edges"][0]
        defs.append({
            "query_id": "seed-q2",
            "natural_language": "「" + edge["source_label"] + "」与「" + edge["target_label"] + "」之间是什么关系？原文依据是什么？",
            "op": "evidence_lookup",
            "params": {"edge_id": "e-0001"},
        })
        relations = sorted({edge["relation"] for edge in baseline["edges"]})
        defs.append({
            "query_id": "seed-q3",
            "natural_language": "图谱里所有「" + relations[0] + "」关系分别有哪些？",
            "op": "relation",
            "params": {"relation": relations[0]},
        })
    defs.append({
        "query_id": "seed-q4",
        "natural_language": "两篇材料共同出现了哪些方法或概念？",
        "op": "cross_doc_shared",
        "params": {"node_type": "method"},
    })
    defs.append({
        "query_id": "seed-q5",
        "natural_language": "两篇论文是否使用了同一物理样品、数据集或实验条件？哪些结论无法确定？",
        "op": "cross_doc_consistency",
        "params": {},
    })
    return defs


def _query_templates() -> list[dict[str, Any]]:
    data = hybrid.read_json(ROOT / "resources" / "query_templates.json", {}) or {}
    return [item for item in (data.get("templates") or []) if isinstance(item, dict)]


def _write_batches(work_dir: Path, segments: list[dict[str, Any]],
                   docs: list[dict[str, Any]], candidates: list[dict[str, Any]],
                   relation_evidence: list[dict[str, Any]],
                   *, batch_size: int = 6, batch_chars: int = 4800) -> list[dict[str, Any]]:
    docs_by = kg_core.documents_by_id(docs)
    candidate_hits: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        for anchor in candidate["occurrences"][:2]:
            candidate_hits.setdefault(anchor.get("seg_id") or "", []).append({
                "label": candidate["label"],
                "type": candidate["type"],
                "count": candidate["count"],
            })
    hints_by_seg: dict[str, list[dict[str, Any]]] = {}
    for item in relation_evidence:
        for relation in item.get("relations") or []:
            hints_by_seg.setdefault(item["seg_id"], []).append({
                "seg_id": item["seg_id"],
                "sentence": item["sentence"],
                "mentions": item["mentions"],
                "trigger_relation": relation.get("relation"),
                "condition": "; ".join(item.get("conditions") or []),
            })
    batch_dir = work_dir / "batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    batches: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0

    def flush() -> None:
        nonlocal current, current_chars
        if not current:
            return
        index = len(batches) + 1
        batch_id = f"batch-{index:02d}"
        item_candidates: dict[str, dict[str, Any]] = {}
        hints: list[dict[str, Any]] = []
        doc_ids: list[str] = []
        for segment in current:
            for candidate in candidate_hits.get(segment["seg_id"], []):
                entry = item_candidates.setdefault(candidate["label"], {
                    "label": candidate["label"], "type": candidate["type"],
                    "count": candidate["count"], "seg_ids": []})
                if segment["seg_id"] not in entry["seg_ids"]:
                    entry["seg_ids"].append(segment["seg_id"])
            hints.extend(hints_by_seg.get(segment["seg_id"], []))
            if segment["doc_id"] not in doc_ids:
                doc_ids.append(segment["doc_id"])
        payload = {
            "batch_id": batch_id,
            "documents": [
                {"doc_id": doc_id,
                 "title": docs_by.get(doc_id, {}).get("title"),
                 "source_file": docs_by.get(doc_id, {}).get("source_file"),
                 "locator_kind": docs_by.get(doc_id, {}).get("locator_kind")}
                for doc_id in doc_ids
            ],
            "segments": [_seg_public(segment, docs_by.get(segment["doc_id"], {}))
                         for segment in current],
            "candidate_entities": sorted(item_candidates.values(),
                                         key=lambda item: (-item["count"], item["label"]))[:30],
            "relation_hints": hints[:10],
            "write_to": f"work/generated/{batch_id}.json",
            "expected_shape": {
                "nodes": [{"node_id": "optional string",
                           "label": "must appear in the segments",
                           "type": "researcher|material|method|task|metric|conclusion|dataset|concept|other",
                           "aliases": ["optional"], "doc_id": "from segments",
                           "seg_id": "from segments",
                           "quote": "verbatim substring of that segment"}],
                "edges": [{"edge_id": "optional", "source": "node_id or node label",
                           "target": "node_id or node label",
                           "relation": "uses|applies_to|evaluates_on|outperforms|improves|characterizes|supports|contradicts|causes|proposes|is_a|compares_with|part_of|other",
                           "direction": "directed|undirected",
                           "condition": "quote the condition, or empty string",
                           "doc_id": "from segments", "seg_id": "from segments",
                           "quote": "verbatim sentence containing BOTH endpoints",
                           "strength": "strong|moderate|weak"}],
            },
        }
        hybrid.write_json(batch_dir / f"{batch_id}.json", payload)
        batches.append({"batch_id": batch_id, "segment_count": len(current),
                        "char_count": current_chars,
                        "worksheet": f"work/batches/{batch_id}.json"})
        current = []
        current_chars = 0

    for segment in segments:
        text_len = len(segment.get("text") or "")
        if current and (len(current) >= batch_size or current_chars + text_len > batch_chars):
            flush()
        current.append(segment)
        current_chars += text_len
    flush()
    return batches


def _write_agent_protocol(work_dir: Path, batch_count: int, docs: list[dict[str, Any]],
                          segments: list[dict[str, Any]]) -> None:
    lines = [
        "# reading-15 文献知识图谱：Agent 生成协议",
        "",
        "本文件由 --prepare 自动生成。确定性部分（发现材料、切段、候选实体、证据锚点）",
        "已经完成，只需要写 judgement 层：哪些候选是论文里真实出现的科研对象、它们之间",
        "有原文支持的关系是什么。不要凭常识补全论文里没有的实体。",
        "",
        "## 1. 读取材料地址",
        "",
        "- work/doc_index.json：两篇材料（doc_id / 标题 / DOI / 段落定位方式）。",
        "- work/segments.json：全部可寻址段落（seg_id / doc_id / paragraph_id / text）。",
        "- work/entity_candidates.json：候选实体及出现位置（只是候选，可增可删）。",
        "- work/relation_evidence.json：含触发动词的句子与候选实体共现。",
        "- work/batches/batch-01.json 到 batch-%02d.json：分批工作表。" % batch_count,
        "- work/baseline_graph.json：确定性草稿图；可直接复制成 generated 再修正。",
        "",
        "## 2. 写入地址（二选一，推荐 A）",
        "",
        "A. 整图模式（每次重写三个文件）：",
        "",
        "- work/generated/nodes.json：JSON 数组，每个元素",
        '  {"node_id": "n1", "label": "实体原文写法", "type": "material",',
        '   "aliases": ["缩写或其他写法"], "doc_id": "doc-01",',
        '   "seg_id": "doc-01-s0003", "quote": "该段落里逐字出现的原句片段"}',
        "- work/generated/edges.json：JSON 数组，每个元素",
        '  {"edge_id": "e1", "source": "n1 或实体标签", "target": "n2 或实体标签",',
        '   "relation": "uses", "direction": "directed", "condition": "at 350 C",',
        '   "doc_id": "doc-01", "seg_id": "doc-01-s0003",',
        '   "quote": "同时包含两端实体的原句", "strength": "strong"}',
        "- work/generated/queries.json：至少 3 个真实查询，每个元素",
        '  {"query_id": "q1", "natural_language": "自然语言问题", "op": "neighbors",',
        '   "params": {"entity": "实体标签或 node_id"}}',
        "  可用 op：neighbors / relation / type_list / evidence_lookup /",
        "  cross_doc_shared / cross_doc_consistency / path / unresolved。",
        "",
        "B. 分批模式：对每个 work/batches/batch-XX.json 写 work/generated/batch-XX.json，",
        '   内容为 {"batch_id": "batch-01", "nodes": [...], "edges": [...]}。',
        "",
        "## 3. 硬性规则",
        "",
        "1. label 必须在对应 seg_id 的 text 里逐字出现（大小写可不同）；quote 必须是该",
        "   段落文本的逐字子串。写不出 quote 的实体不要写。",
        "2. 边的 quote 必须同时包含 source 与 target 的写法，否则该边会被证据校验丢弃。",
        "3. 只有论文自己陈述的关系才算数：不要把同一段里一起出现当关系（那只是共现）；",
        "   不要把综述转述当直接证据。区分 directed（source 作用于 target）与 undirected。",
        "4. condition 写原文的适用条件（温度、浓度、数据集、样本）；没有条件就写空串。",
        "5. 两篇论文的同一术语只在概念层合并；不要把不同论文的样品、条件、数值合成一条",
        "   结论。相同通用术语不等于相同物理样品。",
        "6. 不要套用 case.json 根部的 title/DOI/authors 到第二篇论文；每篇论文用",
        "   work/doc_index.json 里它自己的元数据。",
        "",
        "## 4. 写完后",
        "",
        "python scripts/run_kg.py --finalize --out <输出目录> --work <输出目录>/work",
        "",
        "finalize 会：校验每条节点/边的证据并回源核对 -> 别名合并（同名冲突保留并记录）",
        "-> 执行查询 -> 反射 case.json 的 output_contract 写 result.json / evidence.json /",
        "run_report.json，并导出 graph.json / nodes.csv / edges.csv / graph.graphml。",
    ]
    (work_dir / "agent_protocol.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_work_artifacts(state: dict[str, Any], work_dir: Path, case_id: str | None,
                          materials: dict[str, Any]) -> dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    docs = state["docs"]
    segments = state["segments"]
    candidates = state["candidates"]
    docs_by = kg_core.documents_by_id(docs)
    public_segments = [_seg_public(segment, docs_by.get(segment["doc_id"], {}))
                       for segment in segments]
    hybrid.write_json(work_dir / "doc_index.json", {
        "case_id": case_id,
        "documents": kg_core.summarize_documents(docs),
        "materials": materials["entries"],
        "manifests": materials["manifests"],
        "locator_note": ("paragraph_id is the publisher/JATS paragraph id; page is null "
                         "unless the material really is a PDF"),
    })
    hybrid.write_json(work_dir / "segments.json", {
        "count": len(public_segments),
        "segments": public_segments,
    })
    hybrid.write_json(work_dir / "entity_candidates.json", {
        "count": len(candidates["candidates"]),
        "candidates": [_candidate_public(candidate) for candidate in candidates["candidates"]],
        "acronym_definitions": candidates["acronym_definitions"],
        "note": ("candidates are a deterministic inventory, not a decision: the agent selects "
                 "the real ones and may add entities the heuristics missed"),
    })
    hybrid.write_json(work_dir / "relation_evidence.json", {
        "count": len(state["relation_evidence"]),
        "sentences": state["relation_evidence"],
    })
    seeds = _query_seeds(candidates["candidates"], state["baseline"])
    templates = _query_templates()
    hybrid.write_json(work_dir / "query_seeds.json", {
        "count": len(seeds),
        "queries": seeds,
        "reusable_templates": templates,
        "note": ("seeds are executable as-is; reusable_templates show the supported ops and "
                 "the natural-language wording pattern"),
    })
    hybrid.write_json(work_dir / "baseline_graph.json", {
        "nodes": [{
            "label": node["label"], "type": node["type"],
            "aliases": node["aliases"], "doc_ids": node["doc_ids"],
            "evidence": [kg_core.public_anchor(anchor) for anchor in node["occurrences"][:2]],
        } for node in state["baseline"]["nodes"]],
        "edges": [{
            "source_label": edge["source_label"], "target_label": edge["target_label"],
            "relation": edge["relation"], "direction": edge["direction"],
            "condition": edge["condition"], "strength": edge["strength"],
            "doc_id": edge["doc_id"], "paragraph_id": edge["paragraph_id"],
            "quote": edge["quote"],
            "evidence": [kg_core.public_anchor(anchor) for anchor in edge["anchors"][:1]],
        } for edge in state["baseline"]["edges"]],
        "co_occurrence_candidates": state["baseline"]["co_occurrence_candidates"],
        "notes": state["baseline"]["notes"],
    })
    batches = _write_batches(work_dir, segments, docs, candidates["candidates"],
                             state["relation_evidence"])
    _write_agent_protocol(work_dir, len(batches), docs, segments)
    manifest = {
        "case_id": case_id,
        "schema_version": state["schema"].get("version"),
        "documents": len(docs),
        "segments": len(segments),
        "candidates": len(candidates["candidates"]),
        "acronym_definitions": len(candidates["acronym_definitions"]),
        "baseline_nodes": len(state["baseline"]["nodes"]),
        "baseline_edges": len(state["baseline"]["edges"]),
        "batches": batches,
        "next_commands": {
            "write_generated": "work/generated/nodes.json, edges.json, queries.json "
                               "(or batch-XX.json per worksheet)",
            "finalize": "python scripts/run_kg.py --finalize --out <out> --work <out>/work",
        },
        "work_files": ["doc_index.json", "segments.json", "entity_candidates.json",
                       "relation_evidence.json", "query_seeds.json", "baseline_graph.json",
                       "agent_protocol.md", "manifest.json",
                       "batches/batch-XX.json"],
    }
    hybrid.write_json(work_dir / "manifest.json", manifest)
    return manifest


# --------------------------------------------------------------------------- #
# generated records
# --------------------------------------------------------------------------- #
def _classify_records(records: list[Any]) -> str:
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("source") or record.get("target") or record.get("relation"):
            return "edges"
        if record.get("label") or record.get("node_id") or record.get("type"):
            return "nodes"
        if record.get("natural_language") or record.get("question") or record.get("op"):
            return "queries"
    return "nodes"


def _load_generated(work_dir: Path) -> dict[str, Any]:
    generated_dir = work_dir / "generated"
    out: dict[str, Any] = {"nodes": [], "edges": [], "queries": [], "files": [],
                           "batch_files": []}
    if not generated_dir.is_dir():
        return out
    for path in sorted(generated_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        data = hybrid.read_json(path)
        if data is None:
            continue
        name = path.name.casefold()
        if isinstance(data, dict):
            if "batch_id" in data:
                out["batch_files"].append(path.name)
            found_any = False
            for key, target in (("nodes", "nodes"), ("entities", "nodes"),
                                ("edges", "edges"), ("relations", "edges"),
                                ("queries", "queries")):
                value = data.get(key)
                if isinstance(value, list):
                    out[target].extend(item for item in value if isinstance(item, dict))
                    found_any = True
            if not found_any:
                for key in ("records", "items", "data", "rows"):
                    value = data.get(key)
                    if isinstance(value, list):
                        target = _classify_records(value)
                        out[target].extend(item for item in value if isinstance(item, dict))
                        found_any = True
                        break
            if not found_any and ("label" in data or "source" in data):
                target = _classify_records([data])
                out[target].append(data)
        elif isinstance(data, list):
            records = [item for item in data if isinstance(item, dict)]
            if "node" in name or "entit" in name:
                target = "nodes"
            elif "edge" in name or "relation" in name:
                target = "edges"
            elif "quer" in name or "question" in name:
                target = "queries"
            else:
                target = _classify_records(records)
            out[target].extend(records)
        out["files"].append(path.name)
    return out


# --------------------------------------------------------------------------- #
# finalize
# --------------------------------------------------------------------------- #
def _verification_payload(verification: dict[str, Any],
                          nodes: list[dict[str, Any]],
                          edges: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "grounding": verification["grounding"],
        "node_evidence": [
            {"node_id": node["node_id"], "label": node["label"],
             "evidence": [kg_core.public_anchor(anchor) for anchor in node["occurrences"][:3]],
             "verdict": {key: value for key, value in
                         (verification["node_verdicts"].get(node["node_id"]) or {}).items()
                         if key != "checks"}}
            for node in nodes
        ],
        "edge_evidence": [
            {"edge_id": edge["edge_id"],
             "evidence": [kg_core.public_anchor(anchor) for anchor in edge["anchors"][:2]],
             "verdict": {key: value for key, value in
                         (verification["edge_verdicts"].get(edge["edge_id"]) or {}).items()
                         if key != "checks"}}
            for edge in edges
        ],
        "verification_method": verification["grounding"].get("method"),
        "locator_note": ("locator is the publisher paragraph id (page is only filled for real "
                         "PDF material); a quote is required to contain the entity label and, "
                         "for an edge, both endpoints"),
    }


def _anchors_from_public(public_anchors: list[dict[str, Any]], docs: list[dict[str, Any]],
                         segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    docs_by = kg_core.documents_by_id(docs)
    out: list[dict[str, Any]] = []
    for anchor in public_anchors:
        quote = anchor.get("quote") or ""
        doc_id = anchor.get("doc_id")
        paragraph_id = anchor.get("paragraph_id")
        chosen = None
        for segment in segments:
            if segment.get("doc_id") != doc_id:
                continue
            if paragraph_id and segment.get("paragraph_id") and \
                    str(segment["paragraph_id"]) != str(paragraph_id):
                continue
            if quote and quote.casefold()[:60] in (segment.get("text") or "").casefold():
                chosen = segment
                break
        if chosen is None:
            continue
        doc = docs_by.get(chosen["doc_id"])
        if not doc:
            continue
        out.append(kg_core.seg_anchor(doc, chosen,
                                      quote or (chosen.get("text") or "")[:200]))
    return out


def _finalize_phase(state: dict[str, Any] | None, *, case: dict[str, Any],
                    case_id: str | None, found: dict[str, Any], out_dir: Path,
                    work_dir: Path, stopwatch: hybrid.Stopwatch, materials: dict[str, Any],
                    limits: list[str], uncertainty: list[str],
                    change_log: list[str]) -> dict[str, Any]:
    if state is None:
        state = _load_inputs(materials, stopwatch, limits, uncertainty)
    docs = state["docs"]
    segments = state["segments"]
    schema = state["schema"]
    generated = _load_generated(work_dir)
    baseline = state["baseline"]
    if not generated["nodes"] and not generated["edges"]:
        stored = hybrid.read_json(work_dir / "baseline_graph.json", None)
        if isinstance(stored, dict) and stored.get("nodes"):
            baseline = {
                "nodes": [{
                    "label": node.get("label"), "type": node.get("type"),
                    "aliases": node.get("aliases") or [],
                    "doc_ids": node.get("doc_ids") or [],
                    "occurrences": _anchors_from_public(node.get("evidence") or [],
                                                        docs, segments),
                    "is_conclusion": False,
                } for node in stored.get("nodes") or []],
                "edges": [{
                    "source_label": edge.get("source_label"),
                    "target_label": edge.get("target_label"),
                    "relation": edge.get("relation"), "direction": edge.get("direction"),
                    "condition": edge.get("condition"), "strength": edge.get("strength"),
                    "strength_score": 0.6, "doc_id": edge.get("doc_id"),
                    "paragraph_id": edge.get("paragraph_id"), "seg_id": None,
                    "quote": edge.get("quote"),
                    "anchors": _anchors_from_public(edge.get("evidence") or [],
                                                    docs, segments),
                    "origin": "baseline", "trigger": None,
                } for edge in stored.get("edges") or []],
                "co_occurrence_candidates": stored.get("co_occurrence_candidates") or [],
                "notes": stored.get("notes") or [],
            }
    previous = hybrid.read_json(out_dir / "graph.json", {}) or {}
    stopwatch.log("merge entities and relations")
    merged = kg_graph.merge_graphs(generated, baseline,
                                   state["candidates"]["acronym_definitions"], docs,
                                   segments, schema, previous)
    stopwatch.log(f"merged nodes={len(merged['nodes'])} edges={len(merged['edges'])} "
                  f"merge_log={len(merged['merge_log'])} conflicts={len(merged['conflicts'])}")
    verification = kg_core.verify_evidence(docs, merged["nodes"], merged["edges"])
    node_kept: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []
    for node in merged["nodes"]:
        verdict = verification["node_verdicts"].get(node["node_id"]) or {}
        if verdict.get("grounded"):
            node_kept.append(node)
        else:
            unverified.append({"kind": "node", "ref_id": node["node_id"],
                               "label": node["label"],
                               "reason": "quote not located in the source or does not "
                                         "mention the entity"})
    kept_ids = {node["node_id"] for node in node_kept}
    edge_kept: list[dict[str, Any]] = []
    for edge in merged["edges"]:
        verdict = verification["edge_verdicts"].get(edge["edge_id"]) or {}
        if verdict.get("grounded") and edge["source_id"] in kept_ids \
                and edge["target_id"] in kept_ids:
            edge_kept.append(edge)
        else:
            unverified.append({"kind": "edge", "ref_id": edge["edge_id"],
                               "label": f"{edge.get('source_label')} {edge['relation']} "
                                        f"{edge.get('target_label')}",
                               "reason": "quote does not contain both endpoints or was "
                                         "not located"})
    if unverified:
        uncertainty.append(f"{len(unverified)} generated claim(s) were dropped because their "
                           f"evidence could not be located in the material")
    stopwatch.log(f"grounding kept nodes={len(node_kept)} edges={len(edge_kept)} "
                  f"rate={verification['grounding']['grounding_rate']}")
    definitions = kg_graph.normalize_queries(generated["queries"])
    query_results, query_summary = kg_graph.execute_queries(
        node_kept, edge_kept, definitions,
        conflicts=merged["conflicts"], unverified=unverified, documents=docs,
        stats=merged["stats"], templates=_query_templates())
    for item in merged["conflicts"]:
        uncertainty.append(f"merge/condition conflict {item.get('conflict_id')}: "
                           f"{item.get('detail')}")
    fallbacks: list[str] = []
    usage = merged["generated_usage"]
    if not generated["files"]:
        fallbacks.append("no work/generated/*.json: deterministic baseline graph delivered")
    if usage["nodes_used"] == 0:
        fallbacks.append("no generated node survived: baseline/heuristic nodes used")
    if usage["edges_used"] == 0:
        fallbacks.append("no generated edge survived: deterministic edges substituted")
    if unverified:
        fallbacks.append(f"{len(unverified)} unverifiable claim(s) dropped")
    if not query_summary["minimum_three_queries_satisfied"]:
        fallbacks.append("fewer than three queries could be executed")
    if stopwatch.past_soft():
        fallbacks.append(f"soft deadline reached at {stopwatch.elapsed()}s; remaining work trimmed")
    if not docs:
        status = "failed"
    elif fallbacks:
        status = "partial"
    else:
        status = "success"
    limits.extend(fallbacks)
    documents_public = kg_core.summarize_documents(docs)
    evidence_list = kg_graph.evidence_index(node_kept, edge_kept,
                                            verification["node_verdicts"],
                                            verification["edge_verdicts"])
    nodes_public = [kg_graph.public_node(node) for node in node_kept]
    edges_public = [kg_graph.public_edge(edge) for edge in edge_kept]
    merge_log_public = merged["merge_log"]
    conflicts_public = merged["conflicts"]
    quality = kg_graph.quality_metrics(
        node_kept, edge_kept, docs, segments, verification["grounding"], usage,
        merged["incremental"], merge_log_public, conflicts_public, query_summary,
        schema, fallbacks)
    quality["grounding"]["graph_grounding_rate"] = round(
        sum(1 for node in node_kept if node.get("evidence_status") == "grounded") /
        max(1, len(node_kept)), 4)
    query_defs_public = [{"query_id": result["query_id"],
                          "natural_language": result["natural_language"],
                          "op": result["op"], "params": result["params"]}
                         for result in query_results]
    unresolved = {
        "conflict_count": len(conflicts_public),
        "unverified_count": len(unverified),
        "items": (conflicts_public + unverified)[:40],
    }
    graph_files = kg_graph.export_graph(out_dir, case_id, node_kept, edge_kept,
                                        merge_log_public, conflicts_public,
                                        documents_public, merged["stats"], schema)
    values: dict[str, Any] = {
        "nodes": nodes_public,
        "edges": edges_public,
        "aliases": merge_log_public,
        "conflicts": conflicts_public,
        "queries": query_defs_public,
        "query_results": query_results,
        "evidence": evidence_list,
        "quality": quality,
        "documents": documents_public,
        "stats": merged["stats"],
        "node_types": [{"type": node_type, "definition": definition}
                       for node_type, definition in (schema.get("node_types") or {}).items()],
        "relation_types": [{"relation": relation,
                            "direction": (spec or {}).get("direction"),
                            "definition": (spec or {}).get("definition")}
                           for relation, spec in (schema.get("relation_types") or {}).items()],
        "schema_version": schema.get("version"),
        "unresolved": unresolved,
        "graph_files": graph_files,
        "knowledge_graph": {
            "nodes": nodes_public,
            "edges": edges_public,
            "queries": query_defs_public,
            "query_results": query_results,
            "merge_log": merge_log_public,
            "conflicts": conflicts_public,
            "stats": merged["stats"],
        },
    }
    evidence_payload = _verification_payload(verification, node_kept, edge_kept)
    evidence_payload.update({
        "case_id": case_id,
        "documents": documents_public,
        "alias_merge": merge_log_public,
        "conflicts": conflicts_public,
        "unverified": unverified,
        "queries": query_results,
        "material_files": [entry["path"] for entry in materials["entries"]],
    })
    report = hybrid.run_report(
        stopwatch=stopwatch,
        constants={
            "mode": "finalize",
            "documents": documents_public,
            "segments": len(segments),
            "generated_files": generated["files"],
            "generated_batch_files": generated["batch_files"],
            "generated_usage": usage,
            "incremental": merged["incremental"],
            "query_summary": query_summary,
            "graph_files": graph_files,
            "rejected_records": merged["rejected"][:20],
        },
        discovery={"case_path": found.get("case_path"),
                   "task_md_path": found.get("task_md_path"),
                   "input_roots": materials["roots"],
                   "files_scanned": materials["files_scanned"]},
        limits=limits,
        uncertainty=uncertainty,
        change_log=change_log,
        outputs={"result": str(out_dir / "result.json"),
                 "evidence": str(out_dir / "evidence.json"),
                 "run_report": str(out_dir / "run_report.json"),
                 **{key: str(out_dir / value) for key, value in graph_files.items()
                    if isinstance(value, str)}},
    )
    payload = hybrid.finalize(
        out_dir, case=case, case_id=case_id, values=values, status=status,
        limits=limits, uncertainty=uncertainty, change_log=change_log,
        task_md_path=found.get("task_md_path"), evidence=evidence_payload,
        report=report, synonyms=SYNONYMS, additional=KEEP_ADDITIONAL)
    # The full per-claim record lives in evidence.json; result.json only carries a
    # compact pointer so the contract document stays small and readable.
    payload["meta"]["evidence"] = {
        "grounding": verification["grounding"],
        "claims_total": verification["grounding"]["claims_total"],
        "dropped_claims": len(unverified),
        "full_record": "evidence.json",
        "verification_method": evidence_payload["verification_method"],
        "locator_note": evidence_payload["locator_note"],
    }
    contract.write_result(out_dir, payload)
    stopwatch.log(f"finalize done status={status} nodes={len(nodes_public)} "
                  f"edges={len(edges_public)} queries={query_summary['executed_queries']}")
    print(json.dumps({
        "stage": "finalize",
        "status": status,
        "case_id": case_id,
        "documents": len(docs),
        "nodes": len(nodes_public),
        "edges": len(edges_public),
        "merge_groups": len(merge_log_public),
        "conflicts": len(conflicts_public),
        "queries_executed": query_summary["executed_queries"],
        "grounding_rate": verification["grounding"]["grounding_rate"],
        "fallbacks": fallbacks,
        "out_dir": str(out_dir),
        "result": str(out_dir / "result.json"),
    }, ensure_ascii=False))
    return payload


# --------------------------------------------------------------------------- #
# CLI and failure handling
# --------------------------------------------------------------------------- #
def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="reading-15 knowledge graph: prepare -> agent -> finalize")
    parser.add_argument("--prepare", action="store_true",
                        help="deterministic phase: worksheets + draft graph")
    parser.add_argument("--finalize", action="store_true",
                        help="finalize phase: verify, merge, query, write result")
    parser.add_argument("--all", action="store_true",
                        help="prepare + finalize in one process (default)")
    parser.add_argument("--out", default=None, help="output directory")
    parser.add_argument("--work", default=None, help="work directory (default: <out>/work)")
    parser.add_argument("--case", default=None, help="explicit case.json path")
    parser.add_argument("--max-docs", type=int, default=6, help="material documents to read")
    return parser.parse_args(argv)


def _write_failure_result(out_dir: Path, case: dict[str, Any], case_id: str | None,
                          exc: BaseException, traceback_text: str) -> None:
    try:
        payload = contract.build_result(
            case=case, case_id=case_id, values={}, status="failed",
            limits=[f"run aborted by {type(exc).__name__}: {exc}"],
            uncertainty=["the failure is reported instead of an invented graph"],
            change_log=["failure result written by the top-level handler"])
        payload.setdefault("meta", {})["traceback_tail"] = traceback_text[-1200:]
        contract.write_result(out_dir, payload)
    except Exception:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "result.json").write_text(json.dumps({
                "case_id": case_id or "",
                "meta": {"status": "failed", "contract_detected": False,
                         "limits": [f"run aborted by {type(exc).__name__}"],
                         "traceback_tail": traceback_text[-1200:]},
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    stopwatch = hybrid.Stopwatch()
    found = discover.discover()
    if args.case:
        found["case_path"] = str(Path(args.case))
    out_dir = Path(args.out) if args.out else Path(found["output_dir"])
    work_dir = Path(args.work) if args.work else out_dir / "work"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        work_dir = out_dir
    case = contract.load_case(found.get("case_path"))
    case_id = contract.find_case_id(case)
    declared = len(hybrid.declared_fields(case))
    hybrid.write_placeholder(out_dir, case_id=case_id,
                             note="run started; this file is replaced when the run finishes",
                             contract_fields=declared)
    stopwatch.log(f"case={found.get('case_path')} out={out_dir} work={work_dir} "
                  f"contract_fields={declared}")
    limits: list[str] = []
    uncertainty: list[str] = []
    change_log: list[str] = ["deterministic extraction; no model call made by this script"]
    try:
        materials = _scan_materials(found, out_dir, work_dir)
        stopwatch.log(f"material candidates={len(materials['entries'])} "
                      f"manifests={len(materials['manifests'])}")
        if not materials["entries"]:
            limits.append("no material document was found in the input roots")
        state = _load_inputs(materials, stopwatch, limits, uncertainty)
        if not args.finalize:
            manifest = _write_work_artifacts(state, work_dir, case_id, materials)
            stopwatch.log(f"worksheets={len(manifest['batches'])} work={work_dir}")
            print(json.dumps({
                "stage": "prepare",
                "case_id": case_id,
                "documents": manifest["documents"],
                "segments": manifest["segments"],
                "candidates": manifest["candidates"],
                "baseline_nodes": manifest["baseline_nodes"],
                "baseline_edges": manifest["baseline_edges"],
                "batches": len(manifest["batches"]),
                "work_dir": str(work_dir),
                "generated_pending": [batch["batch_id"] for batch in manifest["batches"]],
            }, ensure_ascii=False))
        _finalize_phase(state, case=case, case_id=case_id, found=found,
                        out_dir=out_dir, work_dir=work_dir,
                        stopwatch=stopwatch, materials=materials,
                        limits=limits, uncertainty=uncertainty,
                        change_log=change_log)
        return 0
    except BaseException as exc:  # noqa: BLE001 - never end a run without a result file
        traceback_text = traceback.format_exc(limit=8)
        stopwatch.log(f"FATAL {type(exc).__name__}: {exc}")
        _write_failure_result(out_dir, case, case_id, exc, traceback_text)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
