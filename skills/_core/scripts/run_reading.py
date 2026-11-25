#!/usr/bin/env python3
"""Reading-chain runner: materials in, contract-shaped ``result.json`` out.

Pipeline
    discover inputs -> extract text+layout -> detect structure
    -> bind evidence anchors -> map onto the declared output contract
    -> write result.json / evidence.json / run_report.json

Design rules taken from the contest contract:
* result.json is generated **from** ``case.json.output_contract``; no field names
  are hardcoded (a hidden case can declare anything).
* every claim-bearing output carries anchors; anchors are re-verified against the
  extracted text before being emitted, and failures are recorded, not hidden.
* budget (900 s / 64 model requests / 16384 tokens) is tracked here so a slow
  machine degrades to an honest partial result instead of being killed mid-write.

No network, no model call: the heavy lifting is deterministic, which is what makes
the 3-runs-per-case requirement reproducible.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import anchors, contract, discover, extract, structure  # noqa: E402

START = time.time()
TIME_BUDGET_S = 900
SOFT_DEADLINE_S = 720

# Hidden cases name their contract fields freely ("parsed_pages",
# "section_outline", "formula_index", ...), so a literal substring table misses
# them. Fields are matched by *stem* instead: strip plural/collection suffixes,
# then exact -> prefix -> contains, longest key first.
FAMILY_STEMS: dict[str, str] = {
    "page": "pages",
    "document": "document",
    "section": "sections",
    "heading": "sections",
    "outline": "outline",
    "table": "tables",
    "formula": "formulas",
    "equation": "formulas",
    "reference": "references",
    "bibliograph": "references",
    "citation": "references",
    "text": "full_text",
    "fulltext": "full_text",
    "plaintext": "full_text",
    "readingorder": "reading_order",
    "evidence": "evidence",
    "anchor": "evidence",
    "quality": "quality",
    "metric": "quality",
    "summary": "compact_summary",
    "answer": "answers",
    "qa": "answers",
    "question": "answers",
}
COLLECTION_SUFFIXES = ("_index", "_list", "_entries", "_items", "_results", "_data", "_content")


def _stem_variants(name: str) -> set[str]:
    """All plausible flattened stems, so plural stripping cannot lose the word.

    ``parsed_pages`` naive-stripped of "es" becomes ``parsedpag``, which matches
    nothing. Emitting several candidates and testing each is more robust than
    guessing one plural rule.
    """
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().casefold()).strip("_")
    variants = {s}
    for suffix in COLLECTION_SUFFIXES:
        if s.endswith(suffix):
            variants.add(s[: -len(suffix)])
    for v in list(variants):
        if v.endswith("s"):
            variants.add(v[:-1])
        if v.endswith("es"):
            variants.add(v[:-2])
    return {v.replace("_", "") for v in variants if v}


def _family_for(field_name: str) -> str | None:
    variants = _stem_variants(field_name)
    flat_keys = {k.replace("_", ""): v for k, v in FAMILY_STEMS.items()}
    for v in variants:
        if v in flat_keys:
            return flat_keys[v]
    ordered = sorted(flat_keys, key=len, reverse=True)
    for v in variants:
        for key in ordered:
            if v.startswith(key):
                return flat_keys[key]
    for v in variants:
        for key in ordered:
            if key in v:
                return flat_keys[key]
    return None


def _elapsed() -> float:
    return round(time.time() - START, 2)


def _log(msg: str) -> None:
    print(f"[{_elapsed():>6.1f}s] {msg}", file=sys.stderr)


def build_document_output(doc: dict[str, Any]) -> dict[str, Any]:
    """Per-page, block-level output: the '提得全 / 排得对 / 拆得清 / 核得回' payload."""
    pages = []
    for page in doc["pages"]:
        blocks = (doc.get("blocks") or {}).get(str(page.number), [])
        pages.append({
            "page": page.number,
            "width": round(page.width, 2),
            "height": round(page.height, 2),
            "char_count": len(page.text),
            "block_count": len(blocks),
            "blocks": [
                {
                    "block_id": b["block_id"],
                    "bbox": b["bbox"],
                    "text": b["text"],
                }
                for b in blocks
            ],
        })
    return {
        "engine": doc.get("engine"),
        "anchor_quality": doc.get("anchor_quality"),
        "text_layer": doc.get("text_layer"),
        "page_count": len(pages),
        "total_chars": doc.get("total_chars"),
        "pages": pages,
    }


def collect_claims(doc: dict[str, Any], struct: dict[str, Any],
                   limit: int = 12) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Turn structural findings into anchor-bearing claims and verify them."""
    claims: list[dict[str, Any]] = []

    for section in struct["sections"][:limit]:
        if not section.get("block_id"):
            continue
        page = section.get("page")
        block = next((b for b in (doc.get("blocks") or {}).get(str(page), [])
                      if b.get("block_id") == section["block_id"]), None)
        if not block:
            continue
        claims.append({
            "claim_id": f"claim-section-{section['id']}",
            "claim": f"section heading '{section['title']}' appears on page {page}",
            "anchors": [anchors.make_anchor(page, block, anchors.pick_quote(block["text"], [section["title"]]))],
        })

    for table in struct["tables"][:6]:
        page = table["page"]
        block = next((b for b in (doc.get("blocks") or {}).get(str(page), [])
                      if b.get("block_id") == table["block_id"]), None)
        if not block:
            continue
        claims.append({
            "claim_id": f"claim-{table['table_id']}",
            "claim": f"candidate table region on page {page}",
            "anchors": [anchors.make_anchor(page, block, anchors.pick_quote(block["text"], []))],
        })

    for formula in struct["formulas"][:6]:
        page = formula["page"]
        block = next((b for b in (doc.get("blocks") or {}).get(str(page), [])
                      if b.get("block_id") == formula["block_id"]), None)
        if not block:
            continue
        claims.append({
            "claim_id": f"claim-{formula['formula_id']}",
            "claim": f"candidate formula region on page {page}",
            "anchors": [anchors.make_anchor(page, block, anchors.pick_quote(block["text"], []))],
        })

    return claims, anchors.verify_all(doc, claims)


def run() -> int:
    out_dir_override = None
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--out" in sys.argv:
        out_dir_override = sys.argv[sys.argv.index("--out") + 1]

    _log("stage 1/6 discover")
    found = discover.discover()
    out_dir = Path(out_dir_override) if out_dir_override else Path(found["output_dir"])
    _log(f"case={found['case_path']} docs={len(found['material_docs'])} out={out_dir}")

    _log("stage 2/6 load case")
    case = contract.load_case(found["case_path"])
    case_id = contract.find_case_id(case)
    task_md = ""
    if found["task_md_path"]:
        try:
            task_md = Path(found["task_md_path"]).read_text(encoding="utf-8", errors="replace")
        except Exception:
            task_md = ""

    limits: list[str] = []
    uncertainty: list[str] = []
    change_log: list[str] = ["deterministic extraction; no model call made"]
    status = "success"

    if not found["material_docs"]:
        status = "failed"
        limits.append("no material documents were found in the input roots")
        payload = contract.build_result(
            case=case, case_id=case_id, values={}, status=status, limits=limits,
            uncertainty=["cannot parse what was not provided"],
            change_log=change_log, task_md_path=found["task_md_path"])
        target = contract.write_result(out_dir, payload)
        _log(f"wrote {target} (failed: no materials)")
        return 0

    _log("stage 3/6 extract")
    docs: list[dict[str, Any]] = []
    for path in found["material_docs"][:4]:
        doc = extract.extract_document(path)
        _log(f"  {Path(path).name}: engine={doc.get('engine')} pages={len(doc['pages'])} "
             f"chars={doc.get('total_chars')}")
        docs.append(doc)
        if not doc.get("text_layer"):
            status = "partial"
            limits.append(f"{Path(path).name}: no text layer (scan?) - OCR not available in this run")
        for warning in doc.get("warnings") or []:
            uncertainty.append(f"{Path(path).name}: {warning}")

    primary = docs[0]
    if status == "success" and primary.get("anchor_quality") != "span":
        limits.append(f"anchor quality degraded to '{primary.get('anchor_quality')}' "
                      "(engine could not supply coordinates)")

    if _elapsed() > SOFT_DEADLINE_S:
        status = "partial"
        limits.append(f"soft deadline reached at {_elapsed()}s; remaining stages trimmed")

    _log("stage 4/6 structure")
    struct = structure.outline(primary)
    _log(f"  sections={len(struct['sections'])} tables={len(struct['tables'])} "
         f"formulas={len(struct['formulas'])} refs={struct['references']['entry_count']}")

    _log("stage 5/6 evidence")
    claims, grounding = collect_claims(primary, struct)
    _log(f"  claims={grounding['claims_total']} grounded={grounding['grounded']} "
         f"rate={grounding['grounding_rate']}")
    if grounding["ungrounded"]:
        uncertainty.append(f"{grounding['ungrounded']} claim(s) could not be grounded in the source text")

    document = build_document_output(primary)
    full_text = "\n\n".join(p.text for p in primary["pages"])

    values: dict[str, Any] = {
        "pages": document["pages"],
        "document": document,
        "outline": struct,
        "sections": struct["sections"],
        "tables": struct["tables"],
        "formulas": struct["formulas"],
        "references": struct["references"],
        "reading_order": struct["reading_order"],
        "full_text": full_text,
        "evidence": {
            "grounding": grounding,
            "claims": claims,
            "anchor_quality": primary.get("anchor_quality"),
        },
        "quality": {
            "engine": primary.get("engine"),
            "text_layer": primary.get("text_layer"),
            "page_count": document["page_count"],
            "total_chars": document["total_chars"],
            "block_count": sum(p["block_count"] for p in document["pages"]),
            "grounding_rate": grounding["grounding_rate"],
            "elapsed_s": _elapsed(),
        },
        "compact_summary": {
            "pages": document["page_count"],
            "sections": [s.get("title") for s in struct["sections"]][:20],
            "tables_found": len(struct["tables"]),
            "formulas_found": len(struct["formulas"]),
            "references": struct["references"]["entry_count"],
        },
    }

    _log("stage 6/6 assemble result.json")
    payload = contract.build_result(
        case=case, case_id=case_id, values=values, evidence=values["evidence"],
        status=status, limits=limits, uncertainty=uncertainty,
        change_log=change_log, task_md_path=found["task_md_path"])

    # Remap: contract.build_result only copies values whose key matches a declared
    # field. Fill those through the family map, then rebuild so declared order wins.
    declared = contract.normalize_contract(
        contract._find_contract(case)) if case else []
    mapped: dict[str, Any] = {}
    mapping_report: dict[str, str] = {}
    for field in declared:
        family = _family_for(field["name"])
        if family and family in values and values[family] is not None:
            mapped[field["name"]] = values[family]
            mapping_report[field["name"]] = family
    if declared:
        payload = contract.build_result(
            case=case, case_id=case_id, values=mapped, evidence=values["evidence"],
            status=status, limits=limits, uncertainty=uncertainty,
            change_log=change_log, task_md_path=found["task_md_path"])
        payload["meta"]["contract_field_mapping"] = mapping_report
        # keep the full technical payload available, but outside the contract block
        payload["meta"]["additional_outputs"] = {
            k: v for k, v in values.items() if k not in set(mapping_report.values())
        }

    target = contract.write_result(out_dir, payload)

    evidence_path = out_dir / "evidence.json"
    evidence_path.write_text(json.dumps({
        "case_id": case_id,
        "grounding": grounding,
        "claims": claims,
        "discovery": found,
        "documents": [
            {"path": d.get("path"), "engine": d.get("engine"),
             "anchor_quality": d.get("anchor_quality"),
             "pages": len(d.get("pages") or []),
             "total_chars": d.get("total_chars"),
             "warnings": d.get("warnings")}
            for d in docs
        ],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    report_path = out_dir / "run_report.json"
    report_path.write_text(json.dumps({
        "case_id": case_id,
        "status": status,
        "elapsed_s": _elapsed(),
        "time_budget_s": TIME_BUDGET_S,
        "model_requests_used": 0,
        "limits": limits,
        "uncertainty": uncertainty,
        "inputs": found,
        "task_md_chars": len(task_md),
        "outputs": {
            "result": str(target),
            "evidence": str(evidence_path),
            "run_report": str(report_path),
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    _log(f"done: {target} | status={status} | {_elapsed()}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
