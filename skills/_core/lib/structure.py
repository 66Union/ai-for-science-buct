#!/usr/bin/env python3
"""Document structure detection: sections, tables, formulas, references.

Everything here is heuristic and **reports its own confidence**, because the
contest penalises confident nonsense more than it penalises an honest "could not
determine". Each detector returns candidates with a ``confidence`` in 0..1 and a
``why`` string naming the evidence that produced it.

Deliberate limits
- Table detection works from text lines only (no ruled-line geometry unless an
  engine supplies it), so it finds *candidate* tables and never claims cell-level
  correctness it cannot verify.
- Formula detection flags structure (math symbols, numbering), not meaning; the
  task explicitly says formulas are structural elements, not semantics.
"""

from __future__ import annotations

import re
from typing import Any

NUMBERED_HEADING = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+(\S.{0,80})$")
KNOWN_HEADINGS = {
    "abstract", "summary", "introduction", "background", "related work",
    "methods", "method", "methodology", "materials and methods", "experimental",
    "experiments", "results", "discussion", "conclusion", "conclusions",
    "acknowledgments", "acknowledgements", "references", "bibliography",
    "appendix", "supplementary", "摘要", "引言", "方法", "结果", "讨论",
    "结论", "参考文献", "致谢",
}
MATH_SYMBOLS = set("=≈±×÷∑∏∫√∂∇≤≥≠→←↔∞αβγδεζηθικλμνξπρστυφχψωΓΔΘΛΞΠΣΦΨΩ")
# Operators that are evidence of an equation rather than of prose. Counted
# separately because a bare "=" is already strong signal when the line is also
# numbered, while prose rarely carries two of these together.
MATH_OPERATORS = ("=", "≈", "≤", "≥", "<", ">", "±", "/", "^", "·")


def _is_heading(block: dict[str, Any]) -> tuple[bool, str, int]:
    """Return (is_heading, title, level)."""
    text = (block.get("text") or "").strip()
    if not text or len(text) > 110:
        return False, "", 0
    low = text.casefold().strip(" .:：")
    if low in KNOWN_HEADINGS:
        return True, text, 1
    m = NUMBERED_HEADING.match(text)
    if m and not text.endswith("."):
        depth = m.group(1).count(".") + 1
        # a numbered line that reads like a sentence is body text, not a heading
        body = m.group(2)
        if len(body.split()) <= 12:
            return True, text, min(depth, 4)
    # ALL-CAPS short line, e.g. "METHODS"
    letters = [c for c in text if c.isalpha()]
    if letters and all(c.isupper() for c in letters) and len(text) < 60:
        return True, text, 1
    # typographic hint from the layout pass: single line set larger than the page median
    if block.get("likely_heading") and not text.endswith((".", ";", ",")):
        return True, text, 2
    return False, "", 0


def detect_sections(doc: dict[str, Any]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    current = {"id": "sec-0", "level": 0, "title": "_preamble", "page": None, "blocks": []}
    sections.append(current)
    for page_key in sorted((doc.get("blocks") or {}), key=lambda k: int(k)):
        page = int(page_key)
        for block in doc["blocks"][page_key]:
            is_head, title, level = _is_heading(block)
            if is_head:
                current = {"id": f"sec-{len(sections)}", "level": level,
                           "title": title, "page": page,
                           "block_id": block.get("block_id"), "blocks": []}
                sections.append(current)
            else:
                current["blocks"].append(block.get("block_id"))
    for section in sections:
        section["block_count"] = len(section["blocks"])
    return [s for s in sections if s["block_count"] or s["level"]]


TABLE_HINT = re.compile(r"\S\s{3,}\S")
NUMERIC_ROW = re.compile(r"(?:^|\s)[-+]?\d+(?:\.\d+)?(?:\s|$)")


def detect_tables(doc: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for page_key in sorted((doc.get("blocks") or {}), key=lambda k: int(k)):
        page = int(page_key)
        for block in doc["blocks"][page_key]:
            text = block.get("text") or ""
            if len(text) < 20:
                continue
            columns = len(TABLE_HINT.findall(text))
            numbers = len(NUMERIC_ROW.findall(text))
            if columns >= 2 or (numbers >= 3 and len(text) < 400):
                confidence = min(0.9, 0.3 + 0.15 * columns + 0.05 * numbers)
                candidates.append({
                    "table_id": f"tbl-{page}-{len(candidates) + 1}",
                    "page": page,
                    "block_id": block.get("block_id"),
                    "bbox": block.get("bbox"),
                    "confidence": round(confidence, 2),
                    "why": f"{columns} aligned column gaps, {numbers} numeric tokens",
                    "text_preview": text[:300],
                    "note": "candidate from text layout only; cell boundaries not verified",
                })
    return candidates


def detect_formulas(doc: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for page_key in sorted((doc.get("blocks") or {}), key=lambda k: int(k)):
        page = int(page_key)
        for block in doc["blocks"][page_key]:
            text = block.get("text") or ""
            if len(text) < 4 or len(text) > 600:
                continue
            math_chars = sum(1 for c in text if c in MATH_SYMBOLS)
            operators = sum(text.count(op) for op in MATH_OPERATORS)
            numbered = bool(re.search(r"\(\d{1,3}[a-z]?\)\s*$", text))
            # an equation needs an operator *and* either equation numbering,
            # several math symbols, or a dense operator count
            if operators and (numbered or math_chars >= 2 or operators >= 3):
                conf = min(0.85, 0.25 + 0.08 * math_chars + 0.06 * operators
                           + (0.2 if numbered else 0))
                found.append({
                    "formula_id": f"eq-{page}-{len(found) + 1}",
                    "page": page,
                    "block_id": block.get("block_id"),
                    "bbox": block.get("bbox"),
                    "numbered": numbered,
                    "confidence": round(conf, 2),
                    "why": (f"{math_chars} math symbols, {operators} operators"
                            + (", trailing equation number" if numbered else "")),
                    "raw": text[:300],
                })
    return found


REF_ENTRY = re.compile(r"^\s*(?:\[(\d{1,3})\]|(\d{1,3})\.)\s+")
# Entry boundary inside one block. Extraction often joins the whole bibliography
# into a single block, so entries must be recovered from the joined text.
REF_SPLIT = re.compile(r"(?=\[\d{1,3}\]\s|\b\d{1,3}\.\s)")


def detect_references(doc: dict[str, Any]) -> dict[str, Any]:
    in_refs = False
    entries: list[dict[str, Any]] = []
    started_at = None
    for page_key in sorted((doc.get("blocks") or {}), key=lambda k: int(k)):
        page = int(page_key)
        for block in doc["blocks"][page_key]:
            text = (block.get("text") or "").strip()
            low = text.casefold().strip(" .:：")
            if low in {"references", "bibliography", "参考文献"} and len(text) < 40:
                in_refs = True
                started_at = page
                continue
            if not in_refs:
                continue
            # one block frequently holds every entry; split on the markers
            for part in REF_SPLIT.split(text):
                part = part.strip()
                if not part:
                    continue
                m = REF_ENTRY.match(part)
                if m:
                    entries.append({
                        "ref_id": m.group(1) or m.group(2),
                        "page": page,
                        "block_id": block.get("block_id"),
                        "text": part[:400],
                    })
    return {
        "found_heading": started_at is not None,
        "starts_on_page": started_at,
        "entry_count": len(entries),
        "entries": entries,
        "confidence": 0.8 if entries else (0.4 if started_at else 0.0),
    }


def infer_reading_order(doc: dict[str, Any]) -> dict[str, Any]:
    """Report whether the document looks multi-column (affects order confidence)."""
    spreads = []
    for page_key, blocks in (doc.get("blocks") or {}).items():
        lefts = [b["bbox"][0] for b in blocks if b.get("bbox")]
        if len(lefts) < 6:
            continue
        lefts.sort()
        gaps = [lefts[i + 1] - lefts[i] for i in range(len(lefts) - 1)]
        if gaps and max(gaps) > 60:
            spreads.append(int(page_key))
    return {
        "multi_column_suspected_pages": spreads,
        "confidence": 0.6 if spreads else 0.2,
        "note": ("column spread detected; reading order follows top-down then left-right, "
                 "which can interleave columns" if spreads
                 else "no clear column gap detected"),
    }


def outline(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "sections": detect_sections(doc),
        "tables": detect_tables(doc),
        "formulas": detect_formulas(doc),
        "references": detect_references(doc),
        "reading_order": infer_reading_order(doc),
    }
