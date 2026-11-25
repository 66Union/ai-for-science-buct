#!/usr/bin/env python3
"""Reference & citation management skill: two-phase runner.

Why two phases
--------------
The evaluation budget is 64 model requests / 900 s / 16384 tokens per response,
so only genuine judgement may cost a model call. prepare does all deterministic
work (discover -> extract -> parse bibliography -> index citation positions ->
detect duplicates -> cut agent worksheets) and writes a complete baseline
result.json; the agent then fills a single small judgement file; finalize merges
that judgement, renumbers, formats, re-verifies every anchor and rewrites
result.json / evidence.json / run_report.json.

Invariants implemented here (not left to the prompt)
----------------------------------------------------
* a result.json exists before slow work starts (container may be killed at 900 s);
* finalize never fails because generated content is missing - it falls back to the
  deterministic lexical baseline and records the gap in limits;
* no field name from a hidden contract is hardcoded: contract reflection happens
  in lib/hybrid.finalize via lib/contract;
* every conclusion carries page + block_id + bbox + quote and is re-verified
  against the extracted source before being emitted.

Usage
    python scripts/run_reference_management.py --prepare  [--input DIR] [--work DIR] [--out DIR]
    python scripts/run_reference_management.py --finalize [--input DIR] [--work DIR] [--out DIR]
    python scripts/run_reference_management.py --all      [--input DIR] [--work DIR] [--out DIR]

--all (the default) runs prepare then finalize without agent judgement, which is
the honest deterministic baseline: still a fully contract-shaped result.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
try:  # packaged layout: <skill>/lib and <skill>/scripts
    sys.path.insert(0, str(ROOT))
    from lib import anchors, contract, discover, docwrite, extract, hybrid, render
except ImportError:  # dev layout: skills/<challenge> with skills/_core next to it
    sys.path.insert(0, str(ROOT.parent / "_core"))
    from lib import anchors, contract, discover, docwrite, extract, hybrid, render

sys.path.insert(0, str(Path(__file__).resolve().parent))
import refmanage as M  # noqa: E402
import refparse as R  # noqa: E402

STATE_FILE = "citation_state.json"
CASE_SNAPSHOT = "case_snapshot.json"
JUDGEMENT_FILE = "claim_support.json"
MAX_CLAIM_ANCHORS = 2
CLAIM_BATCH_SIZE = 6
CLAIM_BATCH_CHARS = 6000

BIB_SUFFIXES = {".bib", ".bibtex", ".ris", ".nbib", ".enw", ".endnote"}
TEXT_READ_SUFFIXES = {".md", ".markdown", ".txt", ".tex", ".latex", ".csv", ".bib",
                      ".bibtex", ".ris", ".nbib", ".enw", ".endnote", ".json", ".xml",
                      ".yaml", ".yml"}
BINARY_SUFFIXES = {".pdf", ".docx", ".pptx"}
MATERIAL_SUFFIXES = TEXT_READ_SUFFIXES | BINARY_SUFFIXES

MANUSCRIPT_NAME_HINTS = ("manuscript", "paper", "draft", "article", "thesis", "main",
                         "chapter", "text", "document", "\u7a3f\u4ef6", "\u6b63\u6587", "\u8bba\u6587")
STYLE_HINTS = (
    (re.compile(r"GB\s*/?\s*T\s*7714(?:[-\u2014]\s*2015)?|GB7714"), M.STYLE_GBT, "GB/T 7714"),
    (re.compile(r"APA(?:\s*(?:\u7b2c\s*)?7|\s*7th|\s*(?:\u7b2c)?\u4e03\u7248)?"), M.STYLE_APA, "APA"),
    (re.compile(r"\u987a\u5e8f\u7f16\u7801\u5236|numeric(?:al)?\s+(?:citation\s+)?style"), M.STYLE_GBT,
     "\u987a\u5e8f\u7f16\u7801\u5236"),
    (re.compile(r"\u8457\u8005\s*[-\u2014\u2013\uff0d]\s*\u51fa\u7248\u5e74|author\s*[-\u2014\u2013]\s*date"),
     M.STYLE_APA, "author-date"),
    (re.compile(r"Vancouver|\u6e29\u54e5\u534e"), M.STYLE_GBT, "Vancouver-style numeric"),
)

SYNONYMS = {
    "revised_text": ["manuscript", "manuscript_text", "final_manuscript", "revised_manuscript",
                     "updated_manuscript", "cleaned_manuscript", "renumbered_manuscript",
                     "output_manuscript", "edited_manuscript", "revised_text", "final_text"],
    "renumbered_text": ["renumbered", "renumbered_document", "updated_text", "numbered_text",
                        "text_after_renumbering", "manuscript_after_renumbering"],
    "references": ["reference_list", "reference_entries", "formatted_references", "works_cited",
                   "bibliography", "literature_cited", "cited_references", "reference_table",
                   "clean_references", "reference_items", "references_formatted"],
    "citation_map": ["intext_marker_trace", "citation_trace", "marker_trace", "citation_index",
                     "in_text_citations", "citation_locations", "citation_occurrences",
                     "citation_positions", "intext_citations"],
    "duplicates": ["duplicate_report", "dedup_report", "deduplication_report",
                   "merge_report", "duplicates"],
    "duplicate_groups": ["duplicate_groups", "merged_groups", "merged_bibliography_groups",
                         "merge_groups", "dedup_groups", "duplicate_clusters"],
    "merges": ["merge_records", "merges", "merged_entries", "merge_log"],
    "resolution": ["resolution_report", "citation_resolution", "resolution"],
    "metadata_issues": ["field_problems", "metadata_problems", "metadata_gaps", "field_issues",
                        "quality_issues", "data_issues", "record_issues", "metadata_errors",
                        "missing_fields_report"],
    "claim_support": ["sentence_level_support", "claim_matches", "claim_evidence",
                      "assertion_support", "sentence_support", "claim_support_map",
                      "claim_reference_matches", "claim_matching"],
    "insertion_suggestions": ["citation_insertions", "insertion_points", "suggested_citations",
                              "citation_suggestions", "insertion_suggestions", "citation_placement"],
    "consistency": ["consistency_report", "consistency_check", "cross_check", "check_results",
                    "validation_report", "consistency_checks"],
    "quality": ["quality_metrics", "metrics", "statistics", "counts", "audit", "quality_report"],
    "evidence": ["evidence_anchors", "anchors", "provenance_map", "source_map", "grounding",
                 "traceability", "source_evidence"],
    "changes": ["change_records", "change_log", "modifications", "diff", "trace", "edit_records"],
    "uncertainty": ["uncertainties", "open_questions", "needs_review", "manual_checks",
                    "unresolved", "unresolved_items"],
    "limits": ["limitations", "gaps", "boundaries", "constraints"],
    "style": ["target_style", "format", "citation_style", "formatting_standard", "style_spec",
              "chosen_style"],
    "summary": ["overview", "abstract", "summary_statistics", "brief", "executive_summary"],
    "status": ["run_status", "state", "outcome"],
}
ADDITIONAL_KEYS = ["summary", "quality", "changes", "uncertainty", "limits", "style",
                   "citation_map", "duplicates", "duplicate_groups", "merges",
                   "metadata_issues", "insertion_suggestions"]


def log(message: str) -> None:
    print(message, flush=True)


def _deadline(soft_seconds: int = 720) -> float:
    import time
    return time.time() + soft_seconds


# --------------------------------------------------------------------------- #
# input discovery / material classification
# --------------------------------------------------------------------------- #
def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def resolve_work_dir(prefer: str | None) -> Path:
    candidates = [Path(prefer)] if prefer else []
    candidates.extend([Path("/app/work"), Path("work")])
    for candidate in candidates:
        if _writable(candidate):
            return candidate
    fallback = Path.cwd() / "work"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return ""


def _looks_like_case(path: Path) -> bool:
    if path.suffix.lower() != ".json":
        return False
    try:
        data = json.loads(_read_text(path))
    except Exception:
        return False
    return isinstance(data, dict) and any(key in data for key in
                                          ("case_id", "output_contract", "caseId", "outputContract"))


def discover_inputs(input_dir: str | None,
                    exclude_dirs: tuple[Path, ...] = ()) -> dict[str, Any]:
    """Discover case.json, task.md and material files without hardcoding paths."""
    if not input_dir:
        found = discover.discover()
        found["materials"] = list(found.get("material_docs") or [])
        return found
    root = Path(input_dir)
    files = sorted((path for path in root.rglob("*") if path.is_file()),
                   key=lambda path: str(path)) if root.is_dir() else []
    case_path = next((path for path in files if path.name == "case.json"), None)
    if case_path is None:
        case_path = next((path for path in files if _looks_like_case(path)
                          and ".tmp" not in str(path)), None)
    task_md = next((path for path in files if path.name.lower() in {"task.md", "instruction.md"}), None)
    excluded = tuple(str(path.resolve()) for path in exclude_dirs if path)

    def _is_excluded(path: Path) -> bool:
        try:
            resolved = str(path.resolve())
        except Exception:
            return False
        return any(resolved == prefix or resolved.startswith(prefix + os.sep)
                   for prefix in excluded)

    materials = [path for path in files
                 if path.suffix.lower() in MATERIAL_SUFFIXES
                 and path not in {case_path, task_md}
                 and not path.name.startswith(".")
                 and not _is_excluded(path)]
    return {
        "input_roots": [str(root)],
        "case_path": str(case_path) if case_path else None,
        "task_md_path": str(task_md) if task_md else None,
        "material_docs": [str(path) for path in materials],
        "materials": [str(path) for path in materials],
        "output_dir": str(hybrid.resolve_out_dir()),
        "files_scanned": len(files),
        "missing": ([] if case_path else ["case.json"]) + ([] if task_md else ["task.md"])
                   + ([] if materials else ["material files"]),
    }


def classify_materials(material_paths: list[str]) -> dict[str, Any]:
    """Split materials into bibliography / manuscript / supporting text."""
    bibliography: list[Path] = []
    manuscripts: list[Path] = []
    supporting: list[Path] = []
    for raw in material_paths:
        path = Path(raw)
        suffix = path.suffix.lower()
        if suffix in BIB_SUFFIXES:
            bibliography.append(path)
            continue
        if suffix == ".json":
            try:
                data = json.loads(_read_text(path))
            except Exception:
                supporting.append(path)
                continue
            rows = data if isinstance(data, list) else (data.get("references") if isinstance(data, dict) else None)
            if isinstance(rows, list) and rows and all(isinstance(row, dict) for row in rows) and any(
                    ("title" in row) or ("doi" in row) or ("author" in row) for row in rows):
                bibliography.append(path)
            else:
                supporting.append(path)
            continue
        if suffix in TEXT_READ_SUFFIXES and suffix not in {".csv", ".xml", ".yaml", ".yml"}:
            head = _read_text(path)[:20000]
            fmt = R.sniff_format(head, suffix)
            if fmt in {"bibtex", "ris", "endnote"}:
                bibliography.append(path)
                continue
            manuscripts.append(path)
            continue
        manuscripts.append(path)

    def rank_manuscript(path: Path) -> tuple:
        name = path.name.lower()
        hinted = any(hint in name for hint in MANUSCRIPT_NAME_HINTS)
        size = 0
        try:
            size = path.stat().st_size
        except Exception:
            pass
        return (0 if hinted else 1,
                0 if path.suffix.lower() in {".docx", ".pdf", ".tex", ".md"} else 1,
                -size, str(path))

    ordered = sorted(manuscripts, key=rank_manuscript)
    return {"bibliography": bibliography, "manuscripts": ordered, "supporting": supporting}


# --------------------------------------------------------------------------- #
# style decision
# --------------------------------------------------------------------------- #
def detect_style(texts: list[tuple[str, str]], markers: list[dict[str, Any]],
                 manuscript_language: str) -> dict[str, Any]:
    """Pick the target style from explicit instructions, else from the text itself."""
    evidence = []
    chosen = None
    for label, text in texts:
        if not text:
            continue
        for pattern, style, name in STYLE_HINTS:
            match = pattern.search(text)
            if match and chosen is None:
                chosen = style
                position = match.start()
                evidence.append({"source": label, "hint": name, "style": style,
                                 "offset": position, "matched": match.group(0)})
    numeric = sum(1 for marker in markers if marker.get("style") in {"numeric", "numeric_superscript"})
    author_year = sum(1 for marker in markers if marker.get("style") in {"author_year", "author_year_narrative"})
    latex = sum(1 for marker in markers if marker.get("style") == "latex")
    if chosen:
        basis = "explicit instruction in the task material"
    elif author_year > max(numeric, latex):
        chosen = M.STYLE_APA
        basis = "majority of in-text markers are author-year"
    elif numeric > 0:
        chosen = M.STYLE_GBT if manuscript_language == "zh" else M.STYLE_APA
        basis = ("numeric markers and a Chinese manuscript -> GB/T 7714"
                 if manuscript_language == "zh"
                 else "numeric markers; no template given -> APA-style numeric-safe default")
        if manuscript_language != "zh":
            chosen = M.STYLE_APA
            basis = "author-year output is the safer default for a non-Chinese manuscript"
    else:
        chosen = M.STYLE_GBT if manuscript_language == "zh" else M.STYLE_APA
        basis = "no in-text citation markers found; default by manuscript language"
    return {"style": chosen, "label": M._STYLE_LABELS.get(chosen, chosen), "basis": basis,
            "hint_evidence": evidence,
            "marker_counts": {"numeric": numeric, "author_year": author_year, "latex": latex},
            "manuscript_language": manuscript_language}


def apply_style_override(style_info: dict[str, Any], work_dir: Path) -> list[str]:
    """An optional agent style override lives next to the judgement records."""
    notes = []
    path = work_dir / "generated" / "style.json"
    data = hybrid.read_json(path)
    if isinstance(data, dict) and data.get("style"):
        wanted = str(data["style"]).strip().casefold()
        mapping = {"gbt7714-2015": M.STYLE_GBT, "gbt7714": M.STYLE_GBT,
                   "gb/t 7714-2015": M.STYLE_GBT, "numeric": M.STYLE_GBT,
                   "apa7": M.STYLE_APA, "apa": M.STYLE_APA, "author-year": M.STYLE_APA,
                   "author_year": M.STYLE_APA}
        if wanted in mapping:
            style_info = dict(style_info)
            style_info["style"] = mapping[wanted]
            style_info["label"] = M._STYLE_LABELS.get(mapping[wanted], mapping[wanted])
            style_info["basis"] = "agent override from work/generated/style.json"
            notes.append("style overridden by work/generated/style.json -> " + mapping[wanted])
            return [style_info, notes]
    return [style_info, notes]


# --------------------------------------------------------------------------- #
# extraction helpers
# --------------------------------------------------------------------------- #
def _page_text(blocks: dict[str, Any]) -> str:
    parts = []
    for page_key in sorted((blocks or {}), key=lambda key: int(key)):
        for block in blocks[page_key]:
            text = str(block.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts)


def _raw_blocks(text: str) -> dict[str, Any]:
    """Paragraph blocks built from the file's own text.

    The generic plain-text extraction path clusters lines with synthetic
    coordinates, which can merge two physical lines without the space between
    them ("with" + "activity" -> "withactivity"). That silently corrupts the
    block text used for anchor verification, so text-like materials are re-cut
    here from the original characters: blank lines separate blocks, and blocks
    longer than 1500 characters are split on line boundaries.
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    has_blank_separator = re.search(r"\n[ \t]*\n", raw) is not None
    if has_blank_separator:
        paragraphs = [part for part in re.split(r"\n\s*\n", raw) if part.strip()]
    else:
        # docx/RIS/EndNote exports put one logical paragraph per line and use no
        # blank lines; cutting per paragraph would merge the whole document into a
        # single block and hide the "References" heading from section detection.
        paragraphs = [line for line in raw.splitlines() if line.strip()]
    if not paragraphs:
        paragraphs = [raw] if raw.strip() else []
    blocks: list[dict[str, Any]] = []
    for paragraph in paragraphs:
        lines = [line for line in paragraph.split("\n") if line.strip()]
        chunks: list[str] = []
        current = ""
        for line in lines:
            if current and len(current) + len(line) + 1 > 1500:
                chunks.append(current)
                current = line
            else:
                current = (current + " " + line).strip() if current else line
        if current:
            chunks.append(current)
        for chunk in chunks:
            text_value = R.normalize_space(chunk)
            if not text_value:
                continue
            index = len(blocks)
            blocks.append({
                "block_id": "raw-b%03d" % (index + 1),
                "page": 1,
                "bbox": [0.0, float(-index * 12), float(max(1, len(text_value))), float(-index * 12 + 10)],
                "text": text_value,
                "lines": max(1, chunk.count("\n") + 1),
                "size": 10.0,
                "x_starts": [0.0],
                "col_gaps": 0,
                "likely_heading": False,
            })
    return {"1": blocks} if blocks else {}


def extract_material(path: Path, deadline: float) -> dict[str, Any]:
    doc = extract.extract_document(path, deadline=deadline)
    blocks = doc.get("blocks") or {}
    suffix = path.suffix.lower()
    if suffix != ".pdf" and doc.get("pages"):
        # text-like material (md/txt/tex/docx/pptx/bib/ris/json): rebuild blocks
        # from the original characters so anchors are not verified against a
        # whitespace-corrupted copy.
        if suffix in TEXT_READ_SUFFIXES:
            raw_text = _read_text(path)
        else:
            raw_text = "\n".join(getattr(page, "text", "") for page in doc.get("pages") or [])
        rebuilt = _raw_blocks(raw_text)
        if rebuilt:
            blocks = rebuilt
    return {
        "path": str(path),
        "suffix": path.suffix.lower(),
        "engine": doc.get("engine"),
        "anchor_quality": doc.get("anchor_quality"),
        "text_layer": bool(doc.get("text_layer")),
        "total_chars": doc.get("total_chars") or 0,
        "pages": [{"page": page.number, "width": round(getattr(page, "width", 0), 2),
                   "height": round(getattr(page, "height", 0), 2),
                   "char_count": len(getattr(page, "text", ""))}
                  for page in (doc.get("pages") or [])],
        "blocks": blocks,
        "warnings": list(doc.get("warnings") or []),
        "attempts": list(doc.get("attempts") or []),
    }


def manuscript_text(path: Path, material: dict[str, Any]) -> tuple[str, str]:
    """Raw text when the file is plain text, else the extracted block text."""
    if path.suffix.lower() in {".md", ".markdown", ".txt", ".tex", ".latex"}:
        raw = _read_text(path)
        if raw.strip():
            return raw, "raw"
    return _page_text(material["blocks"]), "blocks"


def _walk_blocks(material: dict[str, Any]):
    for page_key in sorted((material.get("blocks") or {}), key=lambda key: int(key)):
        for block in material["blocks"][page_key]:
            yield int(page_key), block


def anchor_for_text(materials: dict[str, Any], file_hint: str | None, text: str,
                    limit: int = 240) -> dict[str, Any] | None:
    """Find a page/block for a piece of source text; None when it cannot be located."""
    flat_source = R.normalize_space(text or "")
    if not flat_source:
        return None
    ordered = [file_hint] if file_hint in materials else []
    ordered += [key for key in materials if key not in ordered]
    # longest probe first, then shorter prefixes: extraction normalises whitespace
    # and line breaks differently from the raw file, so an exact prefix may fail
    # while an 80-character prefix still matches.
    for length in (limit, 160, 80, 40):
        probe = flat_source[:length]
        if not probe:
            continue
        flat = probe.casefold()
        for key in ordered:
            material = materials.get(key)
            if not material:
                continue
            for page, block in _walk_blocks(material):
                haystack = R.normalize_space(block.get("text") or "").casefold()
                if flat and flat in haystack:
                    return anchors.make_anchor(page, block, probe)
    return None


def block_of(materials: dict[str, Any], file_key: str, page: int, block_id: str) -> dict[str, Any] | None:
    material = materials.get(file_key) or {}
    for block in (material.get("blocks") or {}).get(str(page), []):
        if block.get("block_id") == block_id:
            return block
    return None


def locate_text(materials: dict[str, Any], file_hint: str, text: str) -> dict[str, Any]:
    """Locate text and return {page, block_id, bbox, quote}; never raises."""
    anchor = anchor_for_text(materials, file_hint, text)
    if anchor:
        return {"page": anchor["page"], "block_id": anchor["block_id"],
                "bbox": anchor.get("bbox"), "quote": anchor.get("quote") or ""}
    first_key = file_hint if file_hint in materials else next(iter(materials), None)
    if first_key:
        for page, block in _walk_blocks(materials[first_key]):
            return {"page": page, "block_id": block.get("block_id"),
                    "bbox": block.get("bbox"),
                    "quote": anchors.pick_quote(block.get("text") or "", [])}
    return {"page": 1, "block_id": None, "bbox": None, "quote": ""}


def _section_map(body_text: str, sentences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    heading_re = re.compile(r"(?m)^[ \t]*(?:#{1,6}\s+|\d+(?:\.\d+)*\.?\s+)([^\n]{2,80})$")

    headings = [(match.start(), R.normalize_space(match.group(0))[:80])
                for match in heading_re.finditer(body_text)]
    current = "_preamble"
    index = 0
    for sentence in sentences:
        while index < len(headings) and headings[index][0] <= sentence["start"]:
            current = headings[index][1]
            index += 1
        sentence["section_title"] = current
    return sentences


# --------------------------------------------------------------------------- #
# state construction (prepare)
# --------------------------------------------------------------------------- #
def build_state(found: dict[str, Any], out_dir: Path, work_dir: Path,
                stopwatch: "hybrid.Stopwatch") -> dict[str, Any]:
    limits: list[str] = []
    uncertainty: list[str] = []
    warnings: list[str] = []
    deadline = _deadline()

    case_path = found.get("case_path")
    case = contract.load_case(case_path)
    case_id = contract.find_case_id(case)
    declared = hybrid.declared_fields(case)
    task_text = _read_text(Path(found["task_md_path"])) if found.get("task_md_path") else ""

    classes = classify_materials(found.get("material_docs") or found.get("materials") or [])
    log("[prepare] materials: manuscript=%d bibliography=%d supporting=%d"
        % (len(classes["manuscripts"]), len(classes["bibliography"]), len(classes["supporting"])))

    materials: dict[str, Any] = {}
    material_summaries: list[dict[str, Any]] = []
    primary_path: Path | None = None
    for path in classes["manuscripts"][:3]:
        if stopwatch.past_soft():
            limits.append("soft deadline reached before reading every manuscript")
            break
        material = extract_material(path, deadline)
        materials[str(path)] = material
        material_summaries.append({"path": str(path), "role": "manuscript",
                                   "engine": material["engine"],
                                   "anchor_quality": material["anchor_quality"],
                                   "pages": len(material["pages"]),
                                   "total_chars": material["total_chars"],
                                   "warnings": material["warnings"]})
        if primary_path is None and material["blocks"]:
            primary_path = path
        for warning in material["warnings"]:
            uncertainty.append("%s: %s" % (path.name, warning))
    if primary_path is None:
        limits.append("no readable manuscript material was found; every content field is a "
                      "declared-shape placeholder")
        raw_primary, text_kind = "", "none"
        section = {"found": False, "heading": "", "heading_start": None,
                   "body": "", "reference_text": ""}
        manuscript_language = "en"
    else:
        raw_primary, text_kind = manuscript_text(primary_path, materials[str(primary_path)])
        section = R.find_reference_section(raw_primary)
        manuscript_language = R.detect_language(raw_primary[:20000])
    body_text = section["body"] if section["found"] else raw_primary
    if primary_path and not raw_primary.strip():
        limits.append("the primary manuscript produced no text (scanned PDF without a text layer?)")
    if section["found"]:
        log("[prepare] embedded reference section '%s' found" % section["heading"])

    # ---- bibliography entries -------------------------------------------------
    raw_items: list[dict[str, Any]] = []
    bibliography_sources: list[dict[str, Any]] = []
    if section["found"] and section["reference_text"].strip():
        embedded = R.parse_plain_reference_list(section["reference_text"], str(primary_path))
        for row in embedded:
            raw_items.append({"raw": row, "source": {"file": str(primary_path),
                                                     "format": "embedded_plain",
                                                     "section": section["heading"]}})
        bibliography_sources.append({"file": str(primary_path), "format": "embedded_plain",
                                     "entry_count": len(embedded)})
        log("[prepare] embedded reference list: %d entries" % len(embedded))
    for path in classes["bibliography"]:
        if stopwatch.past_soft():
            limits.append("soft deadline reached before reading every bibliography file")
            break
        text = _read_text(path)
        parsed_format, rows = R.parse_bibliography_text(text, path.suffix, str(path))
        material = extract_material(path, deadline)
        materials[str(path)] = material
        material_summaries.append({"path": str(path), "role": "bibliography",
                                   "engine": material["engine"],
                                   "anchor_quality": material["anchor_quality"],
                                   "pages": len(material["pages"]),
                                   "total_chars": material["total_chars"],
                                   "warnings": material["warnings"]})
        for row in rows:
            raw_items.append({"raw": row, "source": {"file": str(path), "format": parsed_format}})
        bibliography_sources.append({"file": str(path), "format": parsed_format,
                                     "entry_count": len(rows)})
        log("[prepare] bibliography %s: format=%s entries=%d" % (path.name, parsed_format, len(rows)))
    if not raw_items:
        limits.append("no bibliography entries could be parsed from any material file")
    for source in bibliography_sources:
        if source["entry_count"] == 0:
            warnings.append("bibliography source %s produced no entries" % source["file"])

    used_keys: set[str] = set()
    parsed_entries = []
    for index, item in enumerate(raw_items):
        parsed_entries.append(R.normalize_entry(item["raw"], item["source"], index, used_keys))
    unparsed = sum(1 for entry in parsed_entries if entry.get("parse_status") != "parsed")
    if unparsed:
        uncertainty.append("%d bibliography entr%s could not be fully parsed and will be passed "
                           "through verbatim" % (unparsed, "y" if unparsed == 1 else "ies"))

    pre_dedupe = [dict(entry) for entry in parsed_entries]
    duplicate_report = M.find_duplicates(parsed_entries)
    applied = M.apply_duplicates(parsed_entries, duplicate_report)
    entries = applied["entries"]
    canonical_by_key = {entry["ref_key"]: entry for entry in entries}
    alias_to_key = applied["alias_to_key"]
    duplicate_report["merges"] = applied["merges"]
    duplicate_report["removed_keys"] = applied["removed"]
    duplicate_report["merge_count"] = len(applied["merges"])
    # edge_criteria is keyed by (index, index) tuples; keep it out of the JSON state
    duplicate_report.pop("aliases", None)

    # ---- number map (old in-text number -> entry) -----------------------------
    number_map: dict[int, dict[str, Any]] = {}
    for entry in pre_dedupe:
        old_number = entry.get("old_number")
        if not old_number:
            continue
        canonical_key = alias_to_key.get(R.norm_key(entry["ref_key"]), entry["ref_key"])
        canonical = canonical_by_key.get(canonical_key)
        if canonical:
            number_map[int(old_number)] = canonical
    assumption_note = None
    if not number_map and entries:
        for index, entry in enumerate(entries, start=1):
            number_map[index] = entry
        assumption_note = ("no numbered in-text reference list was found; the bibliography file "
                           "order is assumed to equal the original citation numbering")
        uncertainty.append(assumption_note)
        if M.is_preprint(entries[0]) is False and len(entries) > 2:
            surnames = [R.norm_key(R.first_surname(entry)) for entry in entries]
            if surnames == sorted(surnames):
                uncertainty.append("the bibliography is alphabetically ordered, so assuming its "
                                   "order equals the citation numbering is risky; the renumbered "
                                   "text keeps every marker's resolved key traceable in "
                                   "citation_map")

    # ---- citation markers and sentences --------------------------------------
    markers = R.find_citation_markers(body_text)
    resolution = R.resolve_markers(markers, entries, number_map)
    unresolved = list(resolution["unresolved"])
    sentences = _section_map(body_text, R.split_sentences(body_text))
    for sentence in sentences:
        located = locate_text(materials, str(primary_path) if primary_path else "", sentence["text"])
        sentence.update({"page": located["page"], "block_id": located["block_id"],
                         "bbox": located["bbox"]})
    by_offset = sorted(sentences, key=lambda item: item["start"])
    for marker in markers:
        owner = None
        for sentence in by_offset:
            if sentence["start"] <= marker["start"] < sentence["end"]:
                owner = sentence
                break
        if owner is None:
            continue
        marker["sentence_id"] = owner["sentence_id"]
        marker["sentence"] = owner["text"]
        marker["page"] = owner["page"]
        marker["block_id"] = owner["block_id"]
        marker["bbox"] = owner["bbox"]
        marker["section_title"] = owner.get("section_title")
        marker["anchors"] = [anchors.make_anchor(owner["page"],
                                                 {"block_id": owner["block_id"], "bbox": owner["bbox"]},
                                                 owner["text"][:240])]
    numeric_markers = sum(1 for marker in markers if marker.get("style") in {"numeric", "numeric_superscript"})
    if markers and numeric_markers == 0 and not any(
            marker.get("style") == "latex" for marker in markers):
        uncertainty.append("no numeric citation markers found; numbering is assigned from the "
                           "bibliography order")

    # ---- style ---------------------------------------------------------------
    hint_texts = [("task.md", task_text)]
    if case:
        hint_texts.append(("case.json", json.dumps(case, ensure_ascii=False)))
    for path in classes["supporting"][:4]:
        hint_texts.append((path.name, _read_text(path)[:60000]))
    if primary_path:
        hint_texts.append((primary_path.name, raw_primary[:40000]))
    style_info = detect_style(hint_texts, markers, manuscript_language)
    log("[prepare] style=%s (%s) markers=%d entries=%d"
        % (style_info["style"], style_info["basis"], len(markers), len(entries)))
    if style_info["hint_evidence"]:
        log("[prepare] style hint: %s" % style_info["hint_evidence"][0])

    # ---- claim records (deterministic baseline) ------------------------------
    entries_by_key = {entry["ref_key"]: entry for entry in entries}
    all_keys = [entry["ref_key"] for entry in entries]
    cited_by_sentence: dict[str, list[str]] = {}
    for marker in markers:
        if marker.get("nocite") or not marker.get("sentence_id"):
            continue
        bucket = cited_by_sentence.setdefault(marker["sentence_id"], [])
        for target in marker.get("targets") or []:
            if target["ref_key"] not in bucket:
                bucket.append(target["ref_key"])
    claim_records = []
    for sentence in sentences:
        cited_keys = cited_by_sentence.get(sentence["sentence_id"], [])
        needs = R.needs_citation(sentence["text"])
        if not cited_keys and not needs:
            continue
        baseline = M.match_claim(sentence["text"], cited_keys, entries_by_key, all_keys)
        block = block_of(materials, str(primary_path) if primary_path else "",
                         sentence["page"], sentence["block_id"]) if primary_path else None
        quote = anchors.pick_quote((block or {}).get("text") or sentence["text"],
                                   baseline["claim_terms"], width=200)
        record = {
            "seg_id": sentence["sentence_id"],
            "sentence": sentence["text"],
            "page": sentence["page"],
            "block_id": sentence["block_id"],
            "bbox": sentence["bbox"],
            "section": sentence.get("section_title"),
            "has_citation": bool(cited_keys),
            "cited_refs": cited_keys,
            "claim_terms": baseline["claim_terms"],
            "matched_refs": baseline["matched_refs"],
            "candidate_alternatives": baseline["candidate_alternatives"],
            "possible_misattribution": baseline["possible_misattribution"],
            "insertion": {"needed": not cited_keys,
                          "suggested_refs": [alt["ref_key"] for alt in baseline["candidate_alternatives"]],
                          "note": "" if cited_keys else "sentence states a claim but carries no citation"},
            "confidence": round(sum(item["confidence"] for item in baseline["matched_refs"])
                                / len(baseline["matched_refs"]), 2) if baseline["matched_refs"] else 0.2,
            "source": "baseline",
            "note": "deterministic lexical baseline (agent judgement not merged)",
            "anchors": [anchors.make_anchor(sentence["page"],
                                            {"block_id": sentence["block_id"], "bbox": sentence["bbox"]},
                                            quote)],
        }
        claim_records.append(record)

    # ---- evidence anchors for bibliography entries --------------------------
    entry_anchors = []
    for entry in entries:
        source = entry.get("source") or {}
        file_hint = str(source.get("file") or "")
        probes = [str(source.get("raw") or ""), str(entry.get("title") or ""),
                  str(entry.get("doi") or ""),
                  (str(entry.get("title") or "") + " " + str(entry.get("year") or "")).strip()]
        located = None
        for probe in probes:
            if not probe.strip():
                continue
            located = anchor_for_text(materials, file_hint, probe)
            if located:
                break
        entry_anchors.append({
            "ref_key": entry["ref_key"],
            "file": str(source.get("file") or ""),
            "page": located["page"] if located else None,
            "block_id": located["block_id"] if located else None,
            "bbox": located.get("bbox") if located else None,
            "quote": (located.get("quote") if located else "") or "",
            "matched_from": located.get("note") if located else "",
        })

    state = {
        "version": 1,
        "created_at_elapsed_s": stopwatch.elapsed(),
        "case": {"path": str(case_path) if case_path else None,
                 "case_id": case_id,
                 "raw": case,
                 "contract_fields": [field["name"] for field in declared],
                 "contract_detected": bool(declared)},
        "inputs": found,
        "output_dir": str(out_dir),
        "work_dir": str(work_dir),
        "manuscript": {
            "path": str(primary_path) if primary_path else None,
            "text_kind": text_kind,
            "language": manuscript_language,
            "total_chars": len(raw_primary),
            "body_chars": len(body_text),
            "reference_section": {"found": section["found"], "heading": section["heading"],
                                  "heading_start": section["heading_start"]},
            "raw_text": raw_primary,
            "body_text": body_text,
        },
        "materials": materials,
        "material_summaries": material_summaries,
        "bibliography_sources": bibliography_sources,
        "entries": entries,
        "pre_dedupe_entries": pre_dedupe,
        "duplicates": duplicate_report,
        "alias_to_key": alias_to_key,
        "number_map": [{"old_number": number, "ref_key": entry["ref_key"]}
                       for number, entry in sorted(number_map.items())],
        "markers": markers,
        "sentences": sentences,
        "claim_records": claim_records,
        "entry_anchors": entry_anchors,
        "style": style_info,
        "limits": limits,
        "uncertainty": uncertainty,
        "warnings": warnings,
    }
    return state


def save_state(work_dir: Path, state: dict[str, Any]) -> None:
    hybrid.write_json(work_dir / STATE_FILE, state)
    hybrid.write_json(work_dir / CASE_SNAPSHOT, state.get("case") or {})


def load_state(work_dir: Path) -> dict[str, Any] | None:
    state = hybrid.read_json(work_dir / STATE_FILE)
    return state if isinstance(state, dict) and state.get("version") else None


def write_worksheets(work_dir: Path, state: dict[str, Any]) -> list[dict[str, Any]]:
    """Small per-batch worksheets; the agent answers them in one judgement file."""
    batch_dir = work_dir / "batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    records = state.get("claim_records") or []
    entries_by_key = {entry["ref_key"]: entry for entry in state.get("entries") or []}
    batches = []
    current: list[dict[str, Any]] = []
    current_chars = 0

    def flush() -> None:
        nonlocal current, current_chars
        if not current:
            return
        batch_id = "batch-%02d" % (len(batches) + 1)
        payload = {
            "batch_id": batch_id,
            "instructions": [
                "Fill work/generated/claim_support.json with one record per seg_id below.",
                "Record schema: {seg_id, claim_terms[], matched_refs[{ref_key, relation, "
                "confidence, reason}], insertion{needed, suggested_refs[], note}, confidence, note}.",
                "relation is one of supported / partial / unrelated / contradicts.",
                "Only use ref_key values listed for that segment or in work/bib_entries.json.",
                "Leave a segment out if you have no judgement; finalize falls back to the "
                "deterministic baseline and reports the gap.",
            ],
            "style": state.get("style", {}).get("style"),
            "segments": current,
        }
        hybrid.write_json(batch_dir / (batch_id + ".json"), payload)
        batches.append({"batch_id": batch_id, "segment_count": len(current),
                        "worksheet": str(batch_dir / (batch_id + ".json"))})
        current = []
        current_chars = 0

    for record in records:
        candidates = []
        for key in record.get("cited_refs") or []:
            entry = entries_by_key.get(key)
            if entry:
                candidates.append(_candidate_view(entry, "cited"))
        for alternative in record.get("candidate_alternatives") or []:
            entry = entries_by_key.get(alternative["ref_key"])
            if entry and entry["ref_key"] not in [item["ref_key"] for item in candidates]:
                candidates.append(_candidate_view(entry, "lexical_alternative"))
        view = {
            "seg_id": record["seg_id"],
            "page": record.get("page"),
            "section": record.get("section"),
            "sentence": record.get("sentence"),
            "has_citation": record.get("has_citation"),
            "cited_refs": record.get("cited_refs"),
            "baseline_claim_terms": record.get("claim_terms"),
            "candidate_refs": candidates[:8],
        }
        chars = len(json.dumps(view, ensure_ascii=False))
        if current and (len(current) >= CLAIM_BATCH_SIZE or current_chars + chars > CLAIM_BATCH_CHARS):
            flush()
        current.append(view)
        current_chars += chars
    flush()

    manifest = {
        "style": state.get("style", {}),
        "entry_count": len(state.get("entries") or []),
        "claim_segments": len(records),
        "batches": batches,
        "generated_file": str(work_dir / "generated" / JUDGEMENT_FILE),
        "fallback": "if this file is missing or partial, finalize keeps the deterministic "
                    "lexical baseline and records the gap in limits",
    }
    hybrid.write_json(work_dir / "claim_worksheets.json", manifest)
    return batches


def _candidate_view(entry: dict[str, Any], role: str) -> dict[str, Any]:
    authors = [author.get("family", "") for author in (entry.get("authors") or [])][:4]
    return {"ref_key": entry["ref_key"], "role": role,
            "title": entry.get("title"), "authors": authors, "year": entry.get("year"),
            "venue": entry.get("journal") or entry.get("booktitle"),
            "doi": entry.get("doi"), "type": entry.get("type"),
            "terms": M.ref_terms(entry)[:10]}


def write_work_outputs(work_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Human-auditable snapshots: citation index, bibliography, style, state."""
    markers = state.get("markers") or []
    entries = state.get("entries") or []
    citation_index = {
        "manuscript": state["manuscript"]["path"],
        "language": state["manuscript"]["language"],
        "marker_count": len(markers),
        "by_style": {},
        "unresolved": [item for item in markers if not item.get("targets")],
        "markers": [{"cite_id": marker.get("cite_id"), "style": marker.get("style"),
                     "raw": marker.get("raw"), "page": marker.get("page"),
                     "block_id": marker.get("block_id"), "sentence_id": marker.get("sentence_id"),
                     "resolved_refs": [target["ref_key"] for target in marker.get("targets") or []],
                     "via": [target.get("via") for target in marker.get("targets") or []],
                     "narrative": bool(marker.get("narrative"))}
                    for marker in markers],
    }
    for marker in markers:
        key = str(marker.get("style"))
        citation_index["by_style"][key] = citation_index["by_style"].get(key, 0) + 1
    duplicate_report = dict(state.get("duplicates") or {})
    duplicate_report.pop("aliases", None)
    bibliography = {
        "style": state.get("style"),
        "sources": state.get("bibliography_sources"),
        "entry_count": len(entries),
        "number_map": state.get("number_map"),
        "entries": entries,
        "duplicate_report": duplicate_report,
    }
    hybrid.write_json(work_dir / "citation_index.json", citation_index)
    hybrid.write_json(work_dir / "bib_entries.json", bibliography)
    hybrid.write_json(work_dir / "style.json", state.get("style") or {})
    return {"citation_index": str(work_dir / "citation_index.json"),
            "bib_entries": str(work_dir / "bib_entries.json"),
            "style": str(work_dir / "style.json")}


# --------------------------------------------------------------------------- #
# agent judgement: load, validate, merge
# --------------------------------------------------------------------------- #
ALLOWED_RELATIONS = {"supported", "partial", "unrelated", "contradicts"}


def load_judgement(work_dir: Path, known_seg_ids: set[str],
                   entries_by_key: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Read work/generated/claim_support.json (or per-batch files) and validate it.

    The model's own words are never taken at face value: unknown seg_ids, unknown
    ref_keys, out-of-range confidences and unknown relation labels are rejected
    and recorded, so a hallucinated citation cannot silently reach result.json.
    """
    generated_dir = work_dir / "generated"
    candidates = []
    main_file = generated_dir / JUDGEMENT_FILE
    if main_file.is_file():
        candidates.append(main_file)
    if generated_dir.is_dir():
        for path in sorted(generated_dir.glob("batch-*.json")):
            if path.name != JUDGEMENT_FILE:
                candidates.append(path)
    records: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    files: list[str] = []
    for path in candidates:
        files.append(path.name)
        for row in hybrid.read_records(path):
            seg_id = str(row.get("seg_id") or "").strip()
            if not seg_id:
                invalid.append({"file": path.name, "reason": "record has no seg_id"})
                continue
            if seg_id not in known_seg_ids:
                invalid.append({"file": path.name, "seg_id": seg_id,
                                "reason": "seg_id is not in the worksheet"})
                continue
            matched = []
            for item in row.get("matched_refs") or []:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("ref_key") or "").strip()
                if key not in entries_by_key:
                    invalid.append({"file": path.name, "seg_id": seg_id,
                                    "reason": "ref_key not in the bibliography", "ref_key": key})
                    continue
                relation = str(item.get("relation") or "").strip().casefold()
                if relation not in ALLOWED_RELATIONS:
                    invalid.append({"file": path.name, "seg_id": seg_id,
                                    "reason": "unknown relation label", "relation": relation})
                    relation = "partial"
                try:
                    confidence = float(item.get("confidence"))
                except (TypeError, ValueError):
                    confidence = 0.5
                confidence = round(min(1.0, max(0.0, confidence)), 2)
                matched.append({"ref_key": key, "relation": relation,
                                "confidence": confidence,
                                "reason": str(item.get("reason") or "")[:300]})
            insertion = row.get("insertion") if isinstance(row.get("insertion"), dict) else {}
            suggested = [str(key) for key in (insertion.get("suggested_refs") or [])
                         if str(key) in entries_by_key]
            records.append({
                "seg_id": seg_id,
                "claim_terms": [str(term) for term in (row.get("claim_terms") or [])][:20],
                "matched_refs": matched,
                "insertion": {"needed": bool(insertion.get("needed")),
                              "suggested_refs": suggested,
                              "note": str(insertion.get("note") or "")[:300]},
                "confidence": row.get("confidence"),
                "note": str(row.get("note") or "")[:300],
                "source_file": path.name,
            })
    by_seg: dict[str, dict[str, Any]] = {}
    duplicates = []
    for record in records:
        if record["seg_id"] in by_seg:
            duplicates.append(record["seg_id"])
            continue
        by_seg[record["seg_id"]] = record
    coverage = round(len(by_seg) / len(known_seg_ids), 4) if known_seg_ids else 0.0
    return {"by_seg": by_seg, "records": list(by_seg.values()),
            "coverage": coverage, "expected": len(known_seg_ids),
            "received": len(by_seg), "invalid": invalid[:60],
            "duplicate_segments": sorted(set(duplicates)),
            "files": sorted(set(files))}


def merge_claim_records(baseline: list[dict[str, Any]],
                        judgement: dict[str, Any],
                        entries_by_key: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Merge agent judgement over the deterministic baseline, keeping both trails."""
    merged = []
    stats = {"agent_records": 0, "baseline_records": 0, "agent_new_refs": 0,
             "flagged_assertions": 0}
    for record in baseline:
        output = json.loads(json.dumps(record, ensure_ascii=False))
        agent = judgement.get("by_seg", {}).get(record["seg_id"])
        if not agent:
            output["source"] = "baseline"
            stats["baseline_records"] += 1
            merged.append(output)
            continue
        stats["agent_records"] += 1
        output["source"] = "agent"
        output["agent_note"] = agent.get("note") or ""
        output["note"] = agent.get("note") or "agent judgement merged over the lexical baseline"
        if agent.get("claim_terms"):
            output["claim_terms"] = agent["claim_terms"]
        baseline_by_key = {item["ref_key"]: item for item in record.get("matched_refs") or []}
        matched = []
        for item in agent.get("matched_refs") or []:
            entry = entries_by_key.get(item["ref_key"])
            baseline_item = baseline_by_key.get(item["ref_key"])
            claim = dict(item)
            claim["entry_title"] = (entry or {}).get("title")
            claim["baseline_relation"] = (baseline_item or {}).get("relation", "not_cited")
            claim["baseline_term_coverage"] = (baseline_item or {}).get("term_coverage", 0.0)
            if baseline_item is None:
                stats["agent_new_refs"] += 1
                claim["flag"] = "added_by_agent_not_in_baseline_match"
            elif baseline_item.get("relation") == "unrelated" and item["relation"] in {"supported", "partial"}:
                claim["flag"] = "agent_asserts_support_but_lexical_overlap_is_low"
                stats["flagged_assertions"] += 1
            matched.append(claim)
        if matched:
            output["matched_refs"] = matched
        if agent.get("insertion") and (agent["insertion"].get("needed")
                                       or agent["insertion"].get("suggested_refs")):
            output["insertion"] = {**output.get("insertion", {}),
                                   **{key: value for key, value in agent["insertion"].items() if value}}
        if agent.get("confidence") is not None:
            try:
                output["confidence"] = round(min(1.0, max(0.0, float(agent["confidence"]))), 2)
            except (TypeError, ValueError):
                pass
        merged.append(output)
    return {"records": merged, "stats": stats}


# --------------------------------------------------------------------------- #
# artifacts
# --------------------------------------------------------------------------- #
def _text_blocks(text: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        numbered = re.match(r"^(\d+(?:\.\d+)*)\.?\s+([^\n]{2,80})$", line.strip())
        if heading:
            level = min(len(heading.group(1)), 3)
            blocks.append({"type": "heading", "level": level, "text": heading.group(2).strip()})
        elif numbered and len(line.strip()) <= 90:
            blocks.append({"type": "heading", "level": 2, "text": line.strip()})
        else:
            blocks.append({"type": "para", "text": line.strip()})
    return blocks


def write_artifacts(out_dir: Path, revised_text: str, formatted: list[dict[str, Any]],
                    entries: list[dict[str, Any]], style: str,
                    change_records: list[dict[str, Any]]) -> dict[str, Any]:
    """Editable manuscript + synchronised bibliography, plus render probe."""
    artifacts: dict[str, Any] = {}
    try:
        blocks = _text_blocks(revised_text)
        written = docwrite.write_editable(out_dir, "revised_manuscript", blocks,
                                          title="Revised manuscript")
        artifacts["revised_manuscript"] = written
        refs_md = out_dir / "references.md"
        refs_md.write_text("# Reference list\n\n" + "\n\n".join(
            item["formatted"] for item in formatted) + "\n", encoding="utf-8")
        artifacts["references_md"] = str(refs_md)
        bib_path = out_dir / "references.bib"
        bib_path.write_text("\n".join(M.to_bibtex(entry) for entry in entries), encoding="utf-8")
        artifacts["references_bib"] = str(bib_path)
        ris_path = out_dir / "references.ris"
        ris_path.write_text(M.to_ris(entries), encoding="utf-8")
        artifacts["references_ris"] = str(ris_path)
        log_path = out_dir / "change_log.md"
        lines = ["# Change log", ""]
        for change in change_records[:200]:
            lines.append("- [%s] %s -> %s (%s)" % (change.get("type"), change.get("before"),
                                                   change.get("after"), change.get("note") or ""))
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        artifacts["change_log_md"] = str(log_path)
        render_report = render.render(written["docx"], out_dir)
        artifacts["render"] = {"ok": render_report.get("ok"), "pdf": render_report.get("pdf"),
                               "png": render_report.get("png"),
                               "reason": render_report.get("reason")}
        artifacts["files"] = {key: value for key, value in artifacts.items()
                              if isinstance(value, str)}
    except Exception as exc:  # artifacts are a bonus; never lose result.json over them
        artifacts["error"] = "%s: %s" % (type(exc).__name__, exc)
    return artifacts


# --------------------------------------------------------------------------- #
# finalize
# --------------------------------------------------------------------------- #
def _enriched_duplicate_groups(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Duplicate groups joined with their merge decision, so either shape is useful."""
    duplicates = state.get("duplicates") or {}
    merge_by_group = {merge.get("group_id"): merge
                      for merge in duplicates.get("merges") or []}
    enriched = []
    for group in duplicates.get("groups") or []:
        item = dict(group)
        merge = merge_by_group.get(group.get("group_id")) or {}
        item["merged_into"] = merge.get("merged_into")
        item["merged_keys"] = merge.get("merged_keys") or []
        item["field_fills"] = merge.get("field_fills") or {}
        enriched.append(item)
    return enriched


def _number_map_from_state(state: dict[str, Any],
                           entries_by_key: dict[str, dict[str, Any]]) -> dict[int, dict[str, Any]]:
    number_map: dict[int, dict[str, Any]] = {}
    for item in state.get("number_map") or []:
        entry = entries_by_key.get(str(item.get("ref_key")))
        if entry is not None:
            number_map[int(item["old_number"])] = entry
    if not number_map:
        for index, entry in enumerate(state.get("entries") or [], start=1):
            number_map[index] = entry
    return number_map


def _reference_section_text(heading: str, formatted: list[dict[str, Any]],
                            language: str) -> str:
    label = re.sub(r"^[#\s]*", "", heading or "").strip()
    label = re.sub(r"^\d+(?:\.\d+)*\.?\s*", "", label)
    if not label:
        label = "\u53c2\u8003\u6587\u732e" if language == "zh" else "References"
    if not formatted:
        return label + "\n\n(no bibliography entries were available to format)"
    return label + "\n\n" + "\n\n".join(item["formatted"] for item in formatted)


def finalize_core(out_dir: Path, work_dir: Path, stopwatch: "hybrid.Stopwatch",
                  *, allow_generated: bool,
                  style_override: str | None = None) -> dict[str, Any]:
    state = load_state(work_dir)
    if state is None:
        raise RuntimeError("no prepare state in " + str(work_dir))
    case = state.get("case", {}).get("raw") or {}
    case_path = state.get("case", {}).get("path")
    if case_path and Path(case_path).is_file():
        refreshed = contract.load_case(case_path)
        if refreshed:
            case = refreshed
    case_id = contract.find_case_id(case, state.get("case", {}).get("case_id"))
    task_md_path = (state.get("inputs") or {}).get("task_md_path")

    entries = state.get("entries") or []
    entries_by_key = {entry["ref_key"]: entry for entry in entries}
    markers = state.get("markers") or []
    manuscript = state.get("manuscript") or {}
    materials = state.get("materials") or {}
    manuscript_key = str(manuscript.get("path") or "")
    manuscript_doc = materials.get(manuscript_key) or {"blocks": {}, "anchor_quality": "text"}
    body_text = manuscript.get("body_text") or ""
    raw_text = manuscript.get("raw_text") or ""
    section = manuscript.get("reference_section") or {}
    language = manuscript.get("language") or "en"

    limits = list(state.get("limits") or [])
    uncertainty = list(state.get("uncertainty") or [])
    warnings = list(state.get("warnings") or [])
    change_log: list[str] = []

    style_info = dict(state.get("style") or {})
    if allow_generated:
        style_info, override_notes = apply_style_override(style_info, work_dir)
        limits.extend(override_notes)
    style = style_override if style_override in (M.STYLE_GBT, M.STYLE_APA) else style_info.get("style")
    if style not in (M.STYLE_GBT, M.STYLE_APA):
        style = M.STYLE_GBT
    style_info["style"] = style
    style_info["label"] = M._STYLE_LABELS.get(style, style)
    change_log.append("style: %s (%s)" % (style_info["label"], style_info.get("basis") or ""))

    known_seg_ids = {record["seg_id"] for record in (state.get("claim_records") or [])}
    if allow_generated:
        judgement = load_judgement(work_dir, known_seg_ids, entries_by_key)
    else:
        judgement = {"by_seg": {}, "records": [], "coverage": 0.0,
                     "expected": len(known_seg_ids), "received": 0, "invalid": [],
                     "duplicate_segments": [], "files": []}
    if not allow_generated:
        limits.append("deterministic baseline only: agent judgement file was not merged "
                      "(run --finalize after writing work/generated/claim_support.json)")
    elif judgement["received"] == 0 and known_seg_ids:
        limits.append("work/generated/claim_support.json was missing or empty; claim-level "
                      "conclusions use the deterministic lexical baseline")
    elif judgement["coverage"] < 1.0 and known_seg_ids:
        limits.append("%d of %d claim segments were answered by the agent; the rest use the "
                      "deterministic baseline" % (judgement["received"], judgement["expected"]))
    if judgement["invalid"]:
        uncertainty.append("%d agent judgement item(s) were rejected during validation "
                           "(unknown segment/ref_key/relation)" % len(judgement["invalid"]))
    if judgement["duplicate_segments"]:
        uncertainty.append("duplicate judgement records for segments: "
                           + ", ".join(judgement["duplicate_segments"][:10]))
    merge_result = merge_claim_records(state.get("claim_records") or [], judgement, entries_by_key)
    claim_records = merge_result["records"]
    merge_stats = merge_result["stats"]

    number_map = _number_map_from_state(state, entries_by_key)
    resolution = R.resolve_markers(markers, entries, number_map)
    unresolved = list(resolution["unresolved"])

    plan = M.plan_renumbering(markers, entries, style)
    ordered_entries = []
    for index, key in enumerate(plan["entry_order"], start=1):
        entry = dict(entries_by_key[key])
        entry["number"] = index
        ordered_entries.append(entry)
    formatted = M.format_reference_list(ordered_entries, style)

    keep_latex = (manuscript.get("text_kind") == "raw"
                  and str(manuscript.get("path") or "").lower().endswith((".tex", ".latex")))
    rewrite = M.rewrite_citations(body_text, markers, plan, keep_latex=keep_latex)
    if keep_latex:
        limits.append("LaTeX manuscript: \\cite keys were kept (the tex bibliography handles "
                      "ordering); the renumbered order is reported in citation_map")
    rebuilt_body = rewrite["text"].rstrip()
    ref_text = _reference_section_text(str(section.get("heading") or ""), formatted, language)
    if not manuscript.get("path"):
        # nothing was read: keep the contract-shaped empty value instead of
        # inventing a reference section for an empty manuscript
        revised_text = ""
    elif rebuilt_body:
        revised_text = rebuilt_body + "\n\n" + ref_text + "\n"
    else:
        revised_text = ref_text + "\n"
    if not section.get("found"):
        uncertainty.append("the manuscript had no reference section heading; the formatted list "
                           "was appended under a default heading")

    # ---- evidence verification ---------------------------------------------
    claims = [{"claim_id": "claim-" + record["seg_id"], "claim": record["sentence"],
               "kind": "content", "source": record.get("source"),
               "anchors": record.get("anchors") or []} for record in claim_records]
    grounding = anchors.verify_all(manuscript_doc, claims)
    marker_claims = [{"claim_id": "marker-" + str(marker.get("cite_id")),
                      "claim": marker.get("sentence") or str(marker.get("raw")),
                      "kind": "content", "anchors": marker.get("anchors") or []}
                     for marker in markers if not marker.get("nocite")]
    marker_grounding = anchors.verify_all(manuscript_doc, marker_claims)

    entry_checks = []
    entry_grounded = 0
    for item in state.get("entry_anchors") or []:
        source_file = str(item.get("file") or manuscript_key)
        doc = materials.get(source_file) or manuscript_doc
        anchor = {"page": item.get("page"), "block_id": item.get("block_id"),
                  "bbox": item.get("bbox"), "quote": item.get("quote") or ""}
        result = anchors.verify(doc, anchor) if item.get("page") is not None else {"match": "missing"}
        ok = result.get("match") in anchors.GROUNDED
        if ok:
            entry_grounded += 1
        entry_checks.append({"ref_key": item.get("ref_key"), "file": source_file,
                             "page": item.get("page"), "block_id": item.get("block_id"),
                             "quote": (item.get("quote") or "")[:220],
                             "match": result.get("match"), "grounded": ok})
    entry_grounding = {
        "entries_total": len(entry_checks), "grounded": entry_grounded,
        "grounding_rate": round(entry_grounded / len(entry_checks), 4) if entry_checks else 0.0,
    }
    if entry_checks and entry_grounding["grounding_rate"] < 0.6:
        uncertainty.append("only %.0f%% of bibliography entries could be located verbatim in the "
                           "source material" % (100 * entry_grounding["grounding_rate"]))
    if grounding["ungrounded"]:
        uncertainty.append("%d claim segment(s) could not be grounded in the source text"
                           % grounding["ungrounded"])

    # ---- consistency / metadata issues -------------------------------------
    consistency = M.check_consistency(markers=markers, entries=entries, plan=plan,
                                      formatted=formatted,
                                      reference_section_found=bool(section.get("found")),
                                      unresolved=unresolved + rewrite["unresolved"],
                                      style=style)
    issues = M.metadata_issues(entries)
    for item in unresolved:
        issues.append({"issue_id": "meta-%04d" % (len(issues) + 1),
                       "ref_key": None, "field": "citation", "severity": "error",
                       "message": "in-text citation could not be resolved: " + str(item.get("reason")),
                       "value": str(item.get("raw") or "")[:120]})
    for item in rewrite["unresolved"]:
        issues.append({"issue_id": "meta-%04d" % (len(issues) + 1),
                       "ref_key": None, "field": "citation", "severity": "error",
                       "message": "citation marker was left unchanged: " + str(item.get("reason")),
                       "value": str(item.get("raw") or "")[:120]})
    for key in plan["uncited"]:
        issues.append({"issue_id": "meta-%04d" % (len(issues) + 1), "ref_key": key,
                       "field": "citation", "severity": "info",
                       "message": "bibliography entry is never cited in the manuscript", "value": ""})
    for record in claim_records:
        if record.get("possible_misattribution"):
            issues.append({"issue_id": "meta-%04d" % (len(issues) + 1),
                           "ref_key": (record.get("matched_refs") or [{}])[0].get("ref_key"),
                           "field": "claim_support", "severity": "warning",
                           "message": "a non-cited reference matches this sentence better than "
                                      "the cited one; check for a mis-attributed citation",
                           "value": record["sentence"][:160]})
        if record.get("insertion", {}).get("needed"):
            issues.append({"issue_id": "meta-%04d" % (len(issues) + 1), "ref_key": None,
                           "field": "citation_position", "severity": "info",
                           "message": "sentence states a factual claim but carries no citation; "
                                      "insertion candidates are in insertion_suggestions",
                           "value": record["sentence"][:160]})
    metadata_errors = sum(1 for issue in issues if issue["severity"] == "error")
    metadata_warnings = sum(1 for issue in issues if issue["severity"] == "warning")

    # ---- citation map / insertion suggestions ------------------------------
    change_by_cite = {change.get("cite_id"): change for change in rewrite["changes"]
                      if change.get("cite_id")}
    citation_map = []
    for marker in markers:
        if marker.get("nocite"):
            continue
        change = change_by_cite.get(marker.get("cite_id")) or {}
        citation_map.append({
            "cite_id": marker.get("cite_id"),
            "marker": marker.get("raw"),
            "marker_style": marker.get("style"),
            "new_marker": change.get("after", marker.get("raw")),
            "ref_keys": [target["ref_key"] for target in marker.get("targets") or []],
            "resolved_via": [target.get("via") for target in marker.get("targets") or []],
            "resolution": "resolved" if marker.get("targets") else "unresolved",
            "page": marker.get("page"),
            "block_id": marker.get("block_id"),
            "sentence_id": marker.get("sentence_id"),
            "section": marker.get("section_title"),
        })
    insertion_suggestions = [{
        "seg_id": record["seg_id"], "sentence": record["sentence"],
        "page": record.get("page"), "block_id": record.get("block_id"),
        "suggested_refs": record.get("insertion", {}).get("suggested_refs") or [],
        "reason": record.get("insertion", {}).get("note") or
                  "sentence makes a factual claim without a citation",
        "confidence": record.get("confidence"),
        "source": record.get("source"),
    } for record in claim_records if record.get("insertion", {}).get("needed")]

    # ---- change records ----------------------------------------------------
    changes = []
    for change in rewrite["changes"]:
        changes.append({"change_id": "chg-%04d" % (len(changes) + 1),
                        "type": "citation_marker",
                        "cite_id": change.get("cite_id"),
                        "before": change.get("before"), "after": change.get("after"),
                        "ref_keys": change.get("ref_keys"),
                        "page": change.get("page"), "block_id": change.get("block_id"),
                        "reason": change.get("note") or ""})
    for merge in (state.get("duplicates") or {}).get("merges") or []:
        changes.append({"change_id": "chg-%04d" % (len(changes) + 1),
                        "type": "duplicate_merge",
                        "before": ", ".join(merge.get("merged_keys") or []),
                        "after": merge.get("merged_into"),
                        "reason": "criteria=" + "+".join(merge.get("criteria") or [])
                                  + "; " + str(merge.get("rationale") or ""),
                        "confidence": merge.get("confidence")})
    for item in formatted[:120]:
        changes.append({"change_id": "chg-%04d" % (len(changes) + 1),
                        "type": "format_conversion",
                        "ref_key": item["ref_key"],
                        "before": (item.get("title") or "")[:120],
                        "after": item.get("formatted"),
                        "reason": "reformatted to " + style_info["label"]})
    changes.append({"change_id": "chg-%04d" % (len(changes) + 1),
                    "type": "reference_list_rebuild",
                    "before": "original reference section (%d entries)" % len(entries),
                    "after": "formatted list (%d cited entries)" % len(formatted),
                    "reason": style_info.get("basis") or "target style"})
    change_log.append("citation markers rewritten: %d" % len(rewrite["changes"]))
    change_log.append("duplicate merges applied: %d" % len((state.get("duplicates") or {}).get("merges") or []))

    # ---- quality -----------------------------------------------------------
    marker_total = sum(1 for marker in markers if not marker.get("nocite"))
    marker_resolved = sum(1 for marker in markers if not marker.get("nocite") and marker.get("targets"))
    quality = {
        "manuscript_chars": len(raw_text),
        "body_chars": len(body_text),
        "bibliography_entries": len(entries),
        "cited_entries": len(plan["entry_order"]),
        "uncited_entries": len(plan["uncited"]),
        "citation_markers": marker_total,
        "markers_resolved": marker_resolved,
        "marker_resolution_rate": round(marker_resolved / marker_total, 4) if marker_total else 0.0,
        "duplicate_groups": len((state.get("duplicates") or {}).get("groups") or []),
        "duplicates_merged": len((state.get("duplicates") or {}).get("merges") or []),
        "duplicates_need_review": len((state.get("duplicates") or {}).get("needs_review") or []),
        "metadata_issues": len(issues),
        "metadata_errors": metadata_errors,
        "metadata_warnings": metadata_warnings,
        "consistency_failures": consistency["failures"],
        "consistency_warnings": consistency["warnings"],
        "claim_segments": len(claim_records),
        "agent_judged_segments": merge_stats["agent_records"],
        "claim_grounding_rate": grounding["grounding_rate"],
        "entry_grounding_rate": entry_grounding["grounding_rate"],
        "marker_grounding_rate": marker_grounding["grounding_rate"],
        "insertion_suggestions": len(insertion_suggestions),
        "judgement_coverage": judgement.get("coverage", 0.0),
        "elapsed_s": stopwatch.elapsed(),
    }

    # ---- artifacts ---------------------------------------------------------
    artifacts = write_artifacts(out_dir, revised_text, formatted, entries, style, changes)
    if isinstance(artifacts.get("render"), dict) and not artifacts["render"].get("ok"):
        limits.append("no office/TeX renderer worked in this image; the .docx/.md/.html "
                      "deliverables are the editable artifacts and the HTML file is the preview")
    if artifacts.get("error"):
        limits.append("artifact writing partially failed: " + str(artifacts["error"]))

    # ---- status ------------------------------------------------------------
    status = "success"
    if not allow_generated:
        status = "partial"
    if not manuscript.get("path"):
        status = "partial"
        limits.append("no manuscript material was read; content fields are declared-shape baseline")
    if not entries:
        status = "partial"
        limits.append("no bibliography entries were parsed; the reference list is empty")
    if judgement.get("received", 0) < judgement.get("expected", 0) or not allow_generated:
        status = "partial"
    if consistency["failures"]:
        status = "partial"

    # ---- evidence block ----------------------------------------------------
    evidence = {
        "grounding": grounding,
        "marker_grounding": {"claims_total": marker_grounding["claims_total"],
                             "grounded": marker_grounding["grounded"],
                             "grounding_rate": marker_grounding["grounding_rate"]},
        "entry_grounding": entry_grounding,
        "entry_checks": entry_checks,
        "claims": claims,
        "citation_markers": [{"cite_id": marker.get("cite_id"), "raw": marker.get("raw"),
                              "page": marker.get("page"), "block_id": marker.get("block_id"),
                              "sentence_id": marker.get("sentence_id"),
                              "anchors": marker.get("anchors") or [],
                              "grounded": (item.get("grounded") if item else None),
                              "anchor_match": [check.get("match") for check in
                                               (item.get("checks") if item else [])]}
                             for marker, item in zip(
                                 [marker for marker in markers if not marker.get("nocite")],
                                 marker_grounding["per_claim"])],
        "sources": {
            "manuscript": {"path": manuscript.get("path"), "engine": manuscript.get("engine"),
                           "anchor_quality": manuscript.get("anchor_quality"),
                           "language": language, "text_kind": manuscript.get("text_kind")},
            "bibliography": state.get("bibliography_sources"),
            "supporting_materials": [summary["path"] for summary in
                                     (state.get("material_summaries") or [])
                                     if summary.get("role") == "manuscript"],
            "style_evidence": style_info.get("hint_evidence") or [],
        },
        "duplicates": {key: value for key, value in (state.get("duplicates") or {}).items()
                       if key != "aliases"},
        "judgement": {"received": judgement.get("received", 0),
                      "expected": judgement.get("expected", 0),
                      "coverage": judgement.get("coverage", 0.0),
                      "invalid": judgement.get("invalid") or [],
                      "files": judgement.get("files") or [],
                      "merge_stats": merge_stats},
        "consistency": consistency,
        "verification_policy": "every claim and every citation position is re-read from the "
                               "extracted source before being emitted; failed anchors are "
                               "flagged, never upgraded",
    }

    # ---- contract values ---------------------------------------------------
    values: dict[str, Any] = {
        "revised_text": revised_text,
        "renumbered_text": revised_text,
        "manuscript": revised_text,
        "references": formatted,
        "reference_list": [item["formatted"] for item in formatted],
        "bibliography": formatted,
        "citation_map": citation_map,
        "intext_citations": citation_map,
        "duplicates": {key: value for key, value in (state.get("duplicates") or {}).items()
                       if key != "aliases"},
        "duplicate_groups": _enriched_duplicate_groups(state),
        "merges": list((state.get("duplicates") or {}).get("merges") or []),
        "resolution": {"markers_total": marker_total, "markers_resolved": marker_resolved,
                       "unresolved": unresolved[:40],
                       "marker_resolution_rate": quality["marker_resolution_rate"]},
        "metadata_issues": issues,
        "claim_support": claim_records,
        "insertion_suggestions": insertion_suggestions,
        "consistency": consistency,
        "quality": quality,
        "evidence": [{"claim_id": item.get("claim_id"), "kind": item.get("kind"),
                      "grounded": item.get("grounded"),
                      "anchors": [check for check in item.get("checks") or []][:2]}
                     for item in grounding["per_claim"]],
        "changes": changes,
        "uncertainty": uncertainty,
        "limits": limits,
        "style": style_info,
        "summary": {
            "case_id": case_id,
            "target_style": style_info.get("label"),
            "bibliography_entries": len(entries),
            "cited_entries": len(plan["entry_order"]),
            "citation_markers": marker_total,
            "markers_resolved": marker_resolved,
            "duplicates_merged": quality["duplicates_merged"],
            "metadata_issues": len(issues),
            "claim_grounding_rate": grounding["grounding_rate"],
            "judgement_coverage": judgement.get("coverage", 0.0),
            "status": status,
        },
    }
    return hybrid.finalize(
        out_dir, case=case, case_id=case_id, values=values, status=status,
        limits=limits, uncertainty=uncertainty, change_log=change_log,
        task_md_path=task_md_path, evidence=evidence,
        report=hybrid.run_report(
            stopwatch=stopwatch,
            constants={
                "stage": "finalize" if allow_generated else "prepare-baseline",
                "case_id": case_id,
                "style": style_info,
                "manuscript": {key: manuscript.get(key) for key in
                               ("path", "engine", "anchor_quality", "text_kind", "language",
                                "total_chars", "body_chars")},
                "bibliography_sources": state.get("bibliography_sources"),
                "documents": state.get("material_summaries"),
                "counts": quality,
                "judgement": {"received": judgement.get("received", 0),
                              "expected": judgement.get("expected", 0),
                              "coverage": judgement.get("coverage", 0.0),
                              "invalid": judgement.get("invalid") or []},
                "consistency": {"passed": consistency["passed"],
                                "warnings": consistency["warnings"],
                                "failures": consistency["failures"]},
                "artifacts": artifacts,
                "limits": limits,
                "uncertainty": uncertainty,
                "warnings": warnings,
                "inputs": state.get("inputs"),
                "work_dir": str(work_dir),
                "duplicate_report": {key: value for key, value in
                                     (state.get("duplicates") or {}).items() if key != "aliases"},
                "engines_probed": extract.probe(),
                "render_probe": render.probe(),
            }),
        synonyms=SYNONYMS,
        additional=ADDITIONAL_KEYS,
    )


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def _placeholder(out_dir: Path, case: dict[str, Any], note: str) -> None:
    case_id = contract.find_case_id(case)
    declared = hybrid.declared_fields(case)
    hybrid.write_placeholder(out_dir, case_id=case_id, note=note,
                             contract_fields=len(declared))


def _apply_cli_style(state: dict[str, Any], args: Any) -> None:
    wanted = getattr(args, "style", None)
    if wanted not in (M.STYLE_GBT, M.STYLE_APA):
        return
    style_info = dict(state.get("style") or {})
    style_info["style"] = wanted
    style_info["label"] = M._STYLE_LABELS.get(wanted, wanted)
    style_info["basis"] = "command line --style override"
    state["style"] = style_info


def _log_work_files(work_dir: Path) -> None:
    for name in ("citation_index.json", "bib_entries.json", "style.json", STATE_FILE):
        path = work_dir / name
        log("[prepare]   work/%s %s" % (name, "written" if path.is_file() else "MISSING"))


def run_prepare(args: Any) -> int:
    stopwatch = hybrid.Stopwatch()
    out_dir = hybrid.resolve_out_dir(getattr(args, "out", None))
    hybrid.write_placeholder(out_dir, note="prepare started; placeholder result.json written")
    work_dir = resolve_work_dir(getattr(args, "work", None))
    found = discover_inputs(getattr(args, "input", None), exclude_dirs=(out_dir, work_dir))
    case = contract.load_case(found.get("case_path"))
    _placeholder(out_dir, case, "prepare in progress (baseline placeholder)")
    case_id = contract.find_case_id(case)
    log("[prepare] case=%s case_id=%s materials=%d" % (found.get("case_path"), case_id,
                                                       len(found.get("material_docs") or [])))
    log("[prepare] out=%s work=%s" % (out_dir, work_dir))
    if found.get("missing"):
        log("[prepare] missing inputs: %s" % ", ".join(str(item) for item in found["missing"]))
    state = build_state(found, out_dir, work_dir, stopwatch)
    _apply_cli_style(state, args)
    save_state(work_dir, state)
    write_work_outputs(work_dir, state)
    batches = write_worksheets(work_dir, state)
    _log_work_files(work_dir)
    log("[prepare] batches=%d claim_segments=%d entries=%d duplicates=%d"
        % (len(batches), len(state.get("claim_records") or []),
           len(state.get("entries") or []),
           len((state.get("duplicates") or {}).get("groups") or [])))
    if getattr(args, "no_baseline", False):
        log("[prepare] --no-baseline: skipping the baseline result.json")
        return 0
    finalize_core(out_dir, work_dir, stopwatch, allow_generated=False,
                  style_override=getattr(args, "style", None))
    log("[prepare] baseline result.json written; run --finalize after writing the judgement file")
    return 0


def run_finalize(args: Any) -> int:
    stopwatch = hybrid.Stopwatch()
    out_dir = hybrid.resolve_out_dir(getattr(args, "out", None))
    hybrid.write_placeholder(out_dir, note="finalize started; placeholder result.json written")
    work_dir = resolve_work_dir(getattr(args, "work", None))
    state = load_state(work_dir)
    if state is None:
        found = discover_inputs(getattr(args, "input", None), exclude_dirs=(out_dir, work_dir))
        case = contract.load_case(found.get("case_path"))
        _placeholder(out_dir, case, "finalize had no prepare state; rebuilding it now")
        log("[finalize] no prepare state found; rebuilding the deterministic state first")
        state = build_state(found, out_dir, work_dir, stopwatch)
        _apply_cli_style(state, args)
        save_state(work_dir, state)
        write_work_outputs(work_dir, state)
        write_worksheets(work_dir, state)
    else:
        _apply_cli_style(state, args)
        save_state(work_dir, state)
        _placeholder(out_dir, state.get("case", {}).get("raw") or {},
                     "finalize in progress")
    log("[finalize] work=%s out=%s" % (work_dir, out_dir))
    finalize_core(out_dir, work_dir, stopwatch, allow_generated=True,
                  style_override=getattr(args, "style", None))
    return 0


def write_failure(out_dir: Path, args: Any, exc: BaseException) -> None:
    """Last-resort writer: a failed run still names the case and the reason."""
    trace = traceback.format_exc(limit=8)
    case: dict[str, Any] = {}
    try:
        found = discover_inputs(getattr(args, "input", None))
        case = contract.load_case(found.get("case_path"))
    except Exception:
        case = {}
    case_id = contract.find_case_id(case)
    payload = contract.build_result(
        case=case, case_id=case_id, values={}, status="failed",
        limits=["run aborted by %s: %s" % (type(exc).__name__, exc)],
        uncertainty=["no citation-management content could be produced in this run"],
        change_log=["failure result written by the top-level handler"])
    payload.setdefault("meta", {})["traceback_tail"] = trace[-1500:]
    contract.write_result(out_dir, payload)
    hybrid.write_json(out_dir / "evidence.json",
                      {"case_id": case_id, "status": "failed",
                       "error": "%s: %s" % (type(exc).__name__, exc),
                       "traceback_tail": trace[-1500:]})
    hybrid.write_json(out_dir / "run_report.json",
                      {"case_id": case_id, "status": "failed",
                       "error": "%s: %s" % (type(exc).__name__, exc),
                       "traceback_tail": trace[-1500:]})
    log("[fatal] %s: %s -> failed result.json written" % (type(exc).__name__, exc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reference & citation management: prepare -> agent -> finalize")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--prepare", action="store_true",
                       help="deterministic phase: read inputs, parse, write worksheets + baseline result")
    group.add_argument("--finalize", action="store_true",
                       help="merge the agent judgement file and write the final deliverables")
    group.add_argument("--all", action="store_true",
                       help="prepare then finalize without agent judgement (default)")
    parser.add_argument("--input", default=None,
                        help="input directory to read instead of the runtime discovery roots")
    parser.add_argument("--work", default=None, help="work directory (default /app/work)")
    parser.add_argument("--out", default=None, help="output directory (default /app/output)")
    parser.add_argument("--style", default=None, choices=[M.STYLE_GBT, M.STYLE_APA],
                        help="force the target reference style")
    parser.add_argument("--no-baseline", action="store_true",
                        help="prepare only: do not write the baseline result.json")
    ns = parser.parse_args(argv)
    out_dir = hybrid.resolve_out_dir(ns.out)
    try:
        hybrid.write_placeholder(out_dir,
                                 note="run started; this placeholder is replaced when the run finishes")
        if ns.prepare:
            return run_prepare(ns)
        if ns.finalize:
            return run_finalize(ns)
        code = run_prepare(ns)
        if code != 0:
            return code
        return run_finalize(ns)
    except BaseException as exc:  # noqa: BLE001 - a run must always leave a result.json
        traceback.print_exc()
        try:
            write_failure(out_dir, ns, exc)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
