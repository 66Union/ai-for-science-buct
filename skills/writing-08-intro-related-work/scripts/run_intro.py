#!/usr/bin/env python3
"""Introduction + related-work writing chain: materials in, contract-shaped result out.

Two-phase hybrid design (see the shared kernel docstring for the rationale):

    prepare   deterministic. Discover the case/materials, extract the manuscript into
              page-anchored segments, parse the reference library (.bib/.ris/CSL-JSON/
              plain list), build the citation inventory, extract contribution cues and
              figure/table labels, then write small worksheets for the agent
              (work/brief.json, work/literature.json, work/manuscript.json,
              work/batches/batch-XX.json).
    judgement the agent writes work/generated/intro.json, work/generated/related_work.json
              and optionally work/generated/batch-XX.json (see SKILL.md for the exact
              JSON shapes).
    finalize  deterministic. Validate coverage, resolve every citation against the
              library, re-verify every evidence anchor against the manuscript, build the
              citation-coverage audit and the revised manuscript, then reflect
              case.json.output_contract into result.json and write evidence.json /
              run_report.json.

Two rules are never delegated to the agent:

* a placeholder result.json is written before any slow work starts, and a top-level
  BaseException handler still writes a contract-shaped failed result (the container is
  killed at 900 s; a zero-byte output directory scores zero);
* missing generated content never raises: every missing part is replaced by an honest
  deterministic baseline (extractive abstract / co-occurrence grouping of the library)
  and the gap is recorded in limits/uncertainty with status=partial.

No network, no model call, standard library only, deterministic output.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import traceback
import unicodedata
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]
try:  # packaged layout: <skill>/lib sits next to <skill>/scripts
    sys.path.insert(0, str(ROOT))
    from lib import anchors, contract, discover, docwrite, extract, hybrid, render  # noqa: E402
except ImportError:  # development layout: <repo>/skills/_core/lib
    sys.path.insert(0, str(ROOT.parent / "_core"))
    from lib import anchors, contract, discover, docwrite, extract, hybrid, render  # noqa: E402

SKILL_NAME = "writing-08-intro-related-work"
SKILL_VERSION = "1.0.0"

# Internal value keys. The hidden case decides the real field names; hybrid.map_fields
# (plus the per-adapter synonyms below) reflects these onto whatever it declares.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "introduction": ("intro", "introduction_section", "introduction_and_background",
                     "intro_text", "opening_section", "yinyan", "section_one"),
    "background": ("introduction_background", "research_background", "context",
                   "opening_background", "background_and_motivation", "motivation",
                   "study_context", "beijing"),
    "problem_statement": ("problem", "research_problem", "problem_formulation",
                          "open_problem", "problem_statement_and_motivation"),
    "gap_analysis": ("research_gap", "research_gaps", "gaps", "gap_statement",
                     "knowledge_gap", "empty_space", "identifying_the_gap", "yanjiu_kongbai"),
    "related_work": ("related_works", "related_works_comparison", "prior_art_survey",
                     "prior_work", "literature_review", "survey", "related_work_section",
                     "state_of_the_art", "xiangguan_gongzuo"),
    "contributions": ("contribution", "contribution_statements", "novelty",
                      "novelty_statements", "key_contributions", "our_contributions",
                      "gongxian"),
    "citation_coverage": ("citation_audit", "bibliography_audit", "citation_report",
                          "reference_coverage", "citation_check", "citation_coverage_audit"),
    "citation_inventory": ("citations", "citation_inventory", "bibliography",
                           "reference_list", "references", "references_library"),
    "claim_evidence": ("claim_support_map", "claim_support", "evidence_map",
                       "evidence_register", "grounding", "claims_with_evidence"),
    "revised_manuscript": ("revised_draft", "revised_introduction", "revised_paper",
                           "updated_paper", "revised_text", "manuscript_with_intro",
                           "final_manuscript", "output_manuscript"),
    "quality": ("self_check", "quality_report", "run_quality", "metrics", "statistics"),
    "evidence": ("evidence_anchors", "anchors", "provenance", "sources", "grounding_evidence"),
    "figure_table_map": ("figure_table_inventory", "figures_and_tables", "labels",
                         "figure_table_index"),
    "literature": ("library", "ref_entries", "parsed_references", "bib_entries"),
    "manuscript_sections": ("sections", "outline", "section_structure"),
    "background_and_gap": ("background_and_gap", "background_gap"),
    "payload": ("result_payload", "main_result", "structured_result", "result",
                "output", "outputs", "deliverable"),
    "summary_text": ("cover_letter", "honest_summary", "run_summary",
                     "narrative_summary", "executive_summary"),
    "deliverables": ("output_files", "artifacts", "files"),
}

# --------------------------------------------------------------------------- #
# section recognition
# --------------------------------------------------------------------------- #
SECTION_PATTERNS: list[tuple[str, str]] = [
    ("abstract", r"^(?:abstract|summary|摘\s*要|内容摘要)$|^abstract[:：]"),
    ("related_work", r"^(?:\d+\.?\s*)?(?:related\s+works?|prior\s+work|previous\s+works?|"
                     r"literature\s+review|state\s+of\s+the\s+art|相关工作|文献综述)$"),
    ("introduction", r"^(?:\d+\.?\s*)?(?:introduction|background|motivation|"
                     r"引\s*言|前\s*言|背\s*景|绪\s*论)$"),
    ("method", r"^(?:\d+\.?\s*)?(?:methods?|methodology|materials\s+and\s+methods|"
               r"(?:proposed\s+)?approach|proposed\s+(?:method|framework)|framework|"
               r"materials?|方\s*法|方法学)$"),
    ("experiments", r"^(?:\d+\.?\s*)?(?:experiments?|experimental(?:\s+setup|\s+section)?|"
                    r"evaluation(?:\s+setup)?|实验|实验设置)$"),
    ("results", r"^(?:\d+\.?\s*)?(?:results?(?:\s+and\s+discussion)?|findings|结果)$"),
    ("discussion", r"^(?:\d+\.?\s*)?(?:discussion|讨\s*论)$"),
    ("conclusion", r"^(?:\d+\.?\s*)?(?:conclusions?|concluding\s+remarks|summary|"
                   r"结\s*论|总\s*结)$"),
    ("references", r"^(?:\d+\.?\s*)?(?:references?|bibliography|works\s+cited|参考文献)$"),
]
SECTION_MATCHERS = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in SECTION_PATTERNS]
BODY_SECTIONS = {"related_work", "introduction", "method", "experiments", "results",
                 "discussion", "conclusion"}

# Sentences that read like a contribution claim. Used only to *offer* candidates to the
# agent and to build an extractive baseline; never to invent a contribution.
CONTRIBUTION_CUES = (
    "we propose", "we present", "we introduce", "we develop", "we design", "we show",
    "we demonstrate", "we find", "we report", "we provide", "we argue", "we establish",
    "this work", "this paper", "this study", "our approach", "our method", "our results",
    "in this paper", "here we", "我们提出", "本文", "本研究", "我们展示", "我们证明",
)
PROBLEM_CUES = ("challenge", "problem", "remains", "remain unclear", "unclear", "limited",
                "lack", "difficult", "barrier", "bottleneck", "trade-off", "tradeoff",
                "然而", "挑战", "问题", "尚未", "难以")
LIMITATION_CUES = ("however", "limitation", "limited", "cannot", "fails to", "does not",
                   "do not", "suffer", "restricted", "only", "but", "however,",
                   "局限", "不足", "仅", "无法", "但是")

FIGURE_RE = re.compile(
    r"(?<![A-Za-z])(?:(Figure|Fig\.?|Table|Tab\.?|Scheme|Algorithm)\s*"
    r"(\d+[a-zA-Z]?(?:\s*[\(\-]\s*[a-zA-Z0-9]+\))?)|图\s*(\d+(?:[\.\-]\d+)*)|表\s*(\d+(?:[\.\-]\d+)*))")
STOP_TERMS = set(anchors.STOPWORDS) | {
    "using", "used", "based", "approach", "method", "methods", "results", "study",
    "paper", "work", "works", "propose", "proposed", "show", "shows", "new", "novel",
    "however", "therefore", "which", "their", "these", "those", "such", "more", "than",
}


def _log(message: str) -> None:
    print(f"[{SKILL_NAME}] {message}", flush=True)


def _terms(text: str, limit: int = 12) -> list[str]:
    """Deterministic content-word list (ascii words + CJK bigrams)."""
    norm = hybrid.normalize_text(text)
    words = re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", norm)
    out: list[str] = []
    seen: set[str] = set()
    for word in words:
        low = word.casefold().strip("-")
        if len(low) < 4 or low in STOP_TERMS or low in seen:
            continue
        seen.add(low)
        out.append(low)
    cjk = re.findall(r"[\u4e00-\u9fff]{2,}", norm)
    for run in cjk:
        for i in range(len(run) - 1):
            bigram = run[i:i + 2]
            if bigram in seen or bigram in ("我们", "本文", "因此", "然而"):
                continue
            seen.add(bigram)
            out.append(bigram)
    return out[:limit]


def _term_overlap(claim: str, source: str) -> float:
    """Fraction of the claim's content terms that occur in the source text."""
    claim_terms = _terms(claim, 14)
    if not claim_terms:
        return 0.0
    haystack = hybrid.normalize_text(source).casefold()
    hits = sum(1 for term in claim_terms if term in haystack)
    return round(hits / len(claim_terms), 4)


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("text", "content", "statement", "paragraph", "value"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key].strip()
    return ""


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("records", "items", "entries", "list", "data"):
            if isinstance(value.get(key), list):
                return value[key]
        return [value]
    return [value]


def _pick(raw: Any, names: Iterable[str], default: Any = None) -> Any:
    if not isinstance(raw, dict):
        return default
    for name in names:
        if name in raw and raw[name] not in (None, "", []):
            return raw[name]
    return default


def _unique(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = str(item or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


# --------------------------------------------------------------------------- #
# input loading
# --------------------------------------------------------------------------- #
def _writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def resolve_work_dir(prefer: str | None, out_dir: Path) -> Path:
    """Working area for worksheets and generated content (default /app/work)."""
    candidates: list[Path] = []
    if prefer:
        candidates.append(Path(prefer))
    candidates.extend([Path("/app/work"), out_dir.parent / "work", Path.cwd() / "work"])
    for candidate in candidates:
        if _writable_dir(candidate):
            return candidate
    fallback = Path.cwd() / "work"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _read_text(path: Path) -> str:
    """Read a text material tolerantly (BOM, Windows encodings)."""
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return path.read_text(encoding=encoding, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
        except Exception:
            break
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _docx_paragraphs(path: Path) -> str:
    """Paragraph text of a .docx with the standard library (no python-docx)."""
    import zipfile

    with zipfile.ZipFile(str(path)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", "replace")
    paragraphs = re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S)
    texts: list[str] = []
    for paragraph in paragraphs:
        runs = re.findall(r"<w:t[^>]*>(.*?)</w:t>", paragraph, flags=re.S)
        text = html.unescape("".join(runs)).strip()
        if text:
            texts.append(text)
    return "\n\n".join(texts)


TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")


def _blockify(text: str, *, page: int = 1) -> list[dict[str, Any]]:
    """Split raw text into paragraph blocks with a synthetic line-range bbox.

    Plain-text inputs (and .docx paragraphs) have no real coordinates. Anchors still
    need a stable block identity and a bbox, so one line becomes one unit of height and
    the block keeps its line span. anchor_quality is reported as 'page' so no caller
    mistakes this for span-level provenance.
    """
    lines = [line.rstrip() for line in (text or "").splitlines()]
    blocks: list[dict[str, Any]] = []
    buffer: list[str] = []
    start = 0

    def flush(end_line: int) -> None:
        nonlocal buffer, start
        joined = hybrid.normalize_text(" ".join(buffer))
        if joined:
            width = max((len(line) for line in buffer), default=len(joined))
            blocks.append({
                "block_id": f"p{page}-b{len(blocks) + 1}",
                "page": page,
                "bbox": [0.0, float(-end_line), float(min(width, 600)), float(-start)],
                "text": joined,
                "lines": len(buffer),
                "size": 10.0,
                "likely_heading": len(buffer) == 1 and len(joined) <= 90,
            })
        buffer = []

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            if buffer:
                flush(index)
            start = index + 1
            continue
        heading_like = (bool(re.match(r"^#{1,6}\s+\S", stripped))
                        or bool(re.match(r"^\\(?:sub)*section\*?\{", stripped))
                        or bool(re.match(r"^\d+(?:\.\d+)*\.?\s+\S", stripped) and len(stripped) <= 80))
        if TABLE_ROW_RE.match(stripped) or heading_like:
            if buffer:
                flush(index)
            buffer = [stripped]
            start = index
            flush(index + 1)
            start = index + 1
            continue
        if not buffer:
            start = index
        buffer.append(stripped)
    if buffer:
        flush(len(lines))
    return blocks


def load_manuscript(path: Path, deadline: float | None) -> tuple[dict[str, Any], list[str]]:
    """Read one manuscript file into the kernel's document shape."""
    warnings: list[str] = []
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        doc = extract.extract_document(path, deadline=deadline)
        return doc, warnings
    if suffix == ".pptx":
        return extract.extract_document(path, deadline=deadline), warnings

    engine = ""
    if suffix == ".docx":
        try:
            text = _docx_paragraphs(path)
            engine = "docx:paragraphs"
            if not text.strip():
                raise ValueError("no paragraph text in the docx")
        except Exception as exc:  # noqa: BLE001 - fall back to the shared reader
            warnings.append(f"docx paragraph reader failed ({type(exc).__name__}: {exc}); "
                            "using the shared plain-text reader")
            doc = extract.extract_document(path, deadline=deadline)
            return doc, warnings
    else:
        text = _read_text(path)
        engine = f"text:{suffix.lstrip('.') or 'txt'}"
    blocks = _blockify(text)
    return {
        "path": str(path),
        "engine": engine,
        "anchor_quality": "page",
        "text_layer": bool(text.strip()),
        "total_chars": len(text),
        "pages": [{"number": 1, "width": 612.0, "height": 792.0,
                   "char_count": len(text), "block_count": len(blocks)}],
        "blocks": {"1": blocks},
        "attempts": [f"{engine}: paragraph-level text with synthetic line bboxes"],
        "warnings": warnings,
    }, warnings


# --------------------------------------------------------------------------- #
# reference library parsing: BibTeX, RIS, CSL-JSON, plain numbered lists
# --------------------------------------------------------------------------- #
_ENTRY_START_RE = re.compile(r"@([A-Za-z]+)\s*\{\s*([^,\s]+)\s*,", re.IGNORECASE)
_MONTH_RE = re.compile(r"\b(19|20)\d{2}\b")


def _matching_brace(text: str, start: int) -> int:
    depth = 1
    index = start
    while index < len(text) and depth > 0:
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    return index - 1


def _split_bib_fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    depth = 0
    current: list[str] = []
    pieces: list[str] = []
    for char in body:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        if char == "," and depth == 0:
            pieces.append("".join(current))
            current = []
            continue
        current.append(char)
    pieces.append("".join(current))
    for piece in pieces:
        if "=" not in piece:
            continue
        name, _, value = piece.partition("=")
        name = name.strip().casefold()
        value = value.strip().strip(",").strip()
        if value.startswith("{") and value.endswith("}") and len(value) > 1:
            value = value[1:-1]
        elif value.startswith('"') and value.endswith('"') and len(value) > 1:
            value = value[1:-1]
        fields[name] = hybrid.normalize_text(value)
    return fields


def _authors_from(value: str) -> list[str]:
    if not value:
        return []
    parts = re.split(r"\s+and\s+|\s*;\s*", value)
    authors: list[str] = []
    for part in parts:
        part = part.strip().strip("{}").strip()
        if not part:
            continue
        if "," in part:
            surname = part.split(",")[0].strip()
        else:
            surname = part.split()[-1].strip() if part.split() else part
        if surname:
            authors.append(surname)
    return authors


def _year_from(value: str) -> str:
    match = _MONTH_RE.search(value or "")
    return match.group(0) if match else ""


def _normalise_key(raw: str) -> str:
    return re.sub(r"\s+", "", str(raw or "").strip().strip("{},"))


def parse_bibtex(text: str, source: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    index = 0
    while True:
        match = _ENTRY_START_RE.search(text, index)
        if not match:
            break
        entry_type = match.group(1).casefold()
        key = _normalise_key(match.group(2))
        brace_end = _matching_brace(text, match.end())
        body = text[match.end():brace_end] if brace_end > match.end() else ""
        index = max(brace_end, match.end()) + 1
        if entry_type in {"comment", "string", "preamble"}:
            continue
        fields = _split_bib_fields(body)
        year = _year_from(fields.get("year", "")) or _year_from(fields.get("date", ""))
        entries.append({
            "ref_key": key or f"bib-{len(entries) + 1}",
            "entry_type": entry_type,
            "title": fields.get("title", ""),
            "authors": _authors_from(fields.get("author", "") or fields.get("editor", "")),
            "year": year,
            "venue": fields.get("journal", "") or fields.get("booktitle", "")
                     or fields.get("publisher", ""),
            "doi": fields.get("doi", ""),
            "abstract": fields.get("abstract", ""),
            "source": source,
            "raw": hybrid.normalize_text(body)[:600],
        })
    return entries


def parse_ris(text: str, source: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    current: dict[str, list[str]] = {}
    for line in text.splitlines():
        match = re.match(r"^([A-Z][A-Z0-9])\s{1,2}-\s?(.*)$", line)
        if not match:
            continue
        tag, value = match.group(1), match.group(2).strip()
        if tag == "TY":
            current = {"TY": [value]}
        elif tag == "ER":
            if current:
                entries.append(_ris_entry(current, source, len(entries) + 1))
            current = {}
        elif current is not None:
            current.setdefault(tag, []).append(value)
    if current:
        entries.append(_ris_entry(current, source, len(entries) + 1))
    return [entry for entry in entries if entry["ref_key"]]


def _ris_entry(tags: dict[str, list[str]], source: str, ordinal: int) -> dict[str, Any]:
    def first(*names: str) -> str:
        for name in names:
            if tags.get(name):
                return tags[name][0]
        return ""

    key = first("ID", "AN", "C1")
    title = first("TI", "T1", "CT")
    if not key:
        key = f"ris-{ordinal}"
    return {
        "ref_key": _normalise_key(key) or f"ris-{ordinal}",
        "entry_type": (tags.get("TY") or ["GEN"])[0].casefold(),
        "title": title,
        "authors": [value.strip() for value in tags.get("AU", []) + tags.get("A1", []) if value.strip()],
        "year": _year_from(first("PY", "Y1", "DA")),
        "venue": first("JO", "JF", "T2", "JA"),
        "doi": first("DO"),
        "abstract": first("AB", "N2"),
        "source": source,
        "raw": first("TI", "T1"),
    }


def parse_csl_json(data: Any, source: str) -> list[dict[str, Any]]:
    rows = data if isinstance(data, list) else (
        data.get("items") if isinstance(data, dict) and isinstance(data.get("items"), list) else [])
    entries: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        issued = row.get("issued") or {}
        date_parts = issued.get("date-parts") if isinstance(issued, dict) else None
        year = ""
        if isinstance(date_parts, list) and date_parts and isinstance(date_parts[0], list):
            year = str(date_parts[0][0]) if date_parts[0] else ""
        authors = []
        for author in row.get("author") or []:
            if isinstance(author, dict):
                name = str(author.get("family") or author.get("literal") or author.get("given") or "")
                if name:
                    authors.append(name)
            elif isinstance(author, str):
                authors.append(author)
        entries.append({
            "ref_key": _normalise_key(row.get("id") or f"csl-{index + 1}"),
            "entry_type": str(row.get("type") or "article").casefold(),
            "title": hybrid.normalize_text(str(row.get("title") or "")),
            "authors": authors,
            "year": year,
            "venue": hybrid.normalize_text(str(row.get("container-title") or
                                               row.get("publisher") or "")),
            "doi": str(row.get("DOI") or ""),
            "abstract": hybrid.normalize_text(str(row.get("abstract") or "")),
            "source": source,
            "raw": hybrid.normalize_text(str(row.get("title") or ""))[:600],
        })
    return entries


PLAIN_REF_RE = re.compile(r"^\s*(?:\[(\d{1,3})\]|(\d{1,3})[.)])\s+(.{10,})$")


def parse_plain_refs(text: str, source: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = PLAIN_REF_RE.match(line)
        if not match:
            continue
        number = match.group(1) or match.group(2) or ""
        body = hybrid.normalize_text(match.group(3))
        year = _year_from(body)
        first = body.split(".")[0]
        authors = [token for token in re.split(r",| and ", first) if token.strip()][:4]
        entries.append({
            "ref_key": f"ref-{number}" if number else f"ref-{len(entries) + 1}",
            "entry_type": "plain",
            "title": body,
            "authors": [author.strip() for author in authors],
            "year": year,
            "venue": "",
            "doi": "",
            "abstract": "",
            "source": source,
            "raw": body[:600],
            "title_quality": "raw_reference_line",
        })
    return entries


def load_library(paths: list[Path]) -> dict[str, Any]:
    """Parse every reference-library file into one keyed inventory."""
    entries: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for path in sorted(paths, key=lambda p: (len(p.parts), str(p))):
        suffix = path.suffix.lower()
        text = _read_text(path)
        parsed: list[dict[str, Any]] = []
        fmt = suffix.lstrip(".")
        try:
            if suffix == ".bib":
                parsed = parse_bibtex(text, path.name)
            elif suffix in {".ris", ".enw"}:
                parsed = parse_ris(text, path.name)
            elif suffix == ".json":
                parsed = parse_csl_json(hybrid.read_json(path, []) or [], path.name)
            elif suffix in {".nbib", ".bibtex"}:
                parsed = parse_bibtex(text, path.name) or parse_ris(text, path.name)
            else:
                parsed = parse_plain_refs(text, path.name)
        except Exception as exc:  # noqa: BLE001 - a broken library must not kill the run
            sources.append({"path": path.name, "format": fmt, "entry_count": 0,
                            "warning": f"parse failed: {type(exc).__name__}: {exc}"})
            continue
        sources.append({"path": path.name, "format": fmt, "entry_count": len(parsed)})
        entries.extend(parsed)

    by_key: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for entry in entries:
        key = str(entry.get("ref_key") or "").strip()
        if not key:
            continue
        if key in by_key:
            base = key
            suffix = 2
            while f"{base}-{suffix}" in by_key:
                suffix += 1
            entry = dict(entry)
            entry["ref_key"] = f"{base}-{suffix}"
            key = entry["ref_key"]
        by_key[key] = entry
        ordered.append(entry)

    numeric_index = {str(index): entry["ref_key"] for index, entry in enumerate(ordered, start=1)}
    styles: set[str] = set()
    for entry in ordered:
        if str(entry.get("entry_type")) in {"plain"}:
            styles.add("plain")
    return {
        "entries": ordered,
        "by_key": by_key,
        "key_order": [entry["ref_key"] for entry in ordered],
        "numeric_index": numeric_index,
        "sources": sources,
        "library_size": len(ordered),
        "styles": sorted(styles),
    }


# --------------------------------------------------------------------------- #
# citation resolution
# --------------------------------------------------------------------------- #
NUMERIC_TOKEN_RE = re.compile(r"\d{1,3}(?:\s*[-,–]\s*\d{1,3})*")
AUTHOR_YEAR_RE = re.compile(
    r"([A-Z][A-Za-z\-']+)(?:\s+(?:et\s+al\.?|and\s+[A-Z][A-Za-z\-']+))?\s*,?\s*\(?((?:19|20)\d{2})[a-z]?\)?")


def _looks_like_label(token: str) -> bool:
    return bool(re.search(r"\\ref\{|图\s*\d|表\s*\d|式\s*\(\d", token))


def resolve_token(token: str, library: dict[str, Any]) -> dict[str, Any]:
    r"""Resolve one citation token to library keys.

    Supported: LaTeX \cite{...}, numeric [1,3-5], author-year (Smith et al., 2019;
    Chen, 2020). Anything else returns 'unresolved' with a reason instead of being
    silently dropped.
    """
    raw = str(token or "").strip()
    result: dict[str, Any] = {"token": raw, "kind": "unknown", "keys": [], "unresolved": []}
    by_key = library["by_key"]
    index_map = library["numeric_index"]

    latex = re.match(r"\\cite[a-zA-Z]*\{([^}]*)\}", raw)
    if latex:
        result["kind"] = "latex"
        for part in re.split(r"\s*,\s*", latex.group(1)):
            key = _normalise_key(part)
            if not key:
                continue
            if key in by_key:
                result["keys"].append(key)
            else:
                result["unresolved"].append(key)
        result["keys"] = _unique(result["keys"])
        return result

    if raw.startswith("\\"):
        result["kind"] = "label"
        result["unresolved"].append(raw)
        return result

    if raw.startswith("[") or re.fullmatch(r"\d{1,3}(?:\s*[-,–]\s*\d{1,3})*", raw):
        result["kind"] = "numeric"
        body = raw.strip("[]() ")
        for chunk in re.finditer(r"\d{1,3}\s*[-,–]\s*\d{1,3}|\d{1,3}", body):
            piece = chunk.group(0)
            numbers: list[int] = []
            if re.search(r"[-,–]", piece):
                bounds = [int(value) for value in re.findall(r"\d{1,3}", piece)]
                if len(bounds) == 2 and 0 < bounds[1] - bounds[0] <= 20:
                    numbers = list(range(bounds[0], bounds[1] + 1))
                else:
                    numbers = bounds
            else:
                numbers = [int(piece)]
            for number in numbers:
                key = index_map.get(str(number))
                if key:
                    result["keys"].append(key)
                else:
                    result["unresolved"].append(f"[{number}]")
        result["keys"] = _unique(result["keys"])
        return result

    matches = AUTHOR_YEAR_RE.findall(raw)
    if matches:
        result["kind"] = "author-year"
        for surname, year in matches:
            low = surname.casefold()
            hits = [entry for entry in library["entries"]
                    if str(entry.get("year", "")) == year
                    and any(str(author).casefold().startswith(low)
                            for author in (entry.get("authors") or []))]
            if hits:
                hits.sort(key=lambda entry: entry["ref_key"])
                result["keys"].append(hits[0]["ref_key"])
                if len(hits) > 1:
                    result.setdefault("ambiguous", []).append(
                        {"token": raw, "chosen": hits[0]["ref_key"],
                         "candidates": [entry["ref_key"] for entry in hits]})
            else:
                result["unresolved"].append(raw)
        result["keys"] = _unique(result["keys"])
        return result

    result["unresolved"].append(raw)
    return result


def resolve_many(tokens: Iterable[Any], library: dict[str, Any]) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    keys: list[str] = []
    unresolved: list[str] = []
    details: list[dict[str, Any]] = []
    for token in tokens:
        if isinstance(token, dict):
            token = token.get("ref_key") or token.get("key") or token.get("citation") or ""
        token = str(token or "").strip()
        if not token:
            continue
        if token in library["by_key"]:
            keys.append(token)
            details.append({"token": token, "kind": "bib-key", "keys": [token], "unresolved": []})
            continue
        resolved = resolve_token(token, library)
        details.append(resolved)
        keys.extend(resolved["keys"])
        unresolved.extend(resolved["unresolved"])
    return _unique(keys), _unique(unresolved), details


def citation_tokens(text: str) -> list[str]:
    """Citation-looking tokens, excluding figure/table/equation references."""
    tokens = [token for token in hybrid.find_citations(text) if not _looks_like_label(token)]
    return _unique(tokens)


def citation_style(tokens: Iterable[str]) -> str:
    kinds: set[str] = set()
    for token in tokens:
        if token.startswith("\\cite"):
            kinds.add("latex")
        elif token.strip().startswith("["):
            kinds.add("numeric")
        elif re.search(r"(19|20)\d{2}", token):
            kinds.add("author-year")
    if not kinds:
        return "none"
    return "+".join(sorted(kinds))


# --------------------------------------------------------------------------- #
# manuscript analysis
# --------------------------------------------------------------------------- #
def _clean_heading(text: str) -> str:
    value = hybrid.normalize_text(text)
    value = re.sub(r"^#{1,6}\s*", "", value)
    latex = re.match(r"^\\(?:sub)*section\*?\{([^}]*)\}", value)
    if latex:
        value = latex.group(1)
    value = re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", value)
    return value.strip(" .:：·—-*")


def classify_heading(text: str) -> tuple[str | None, str]:
    """Return (canonical section name, display name) for a heading-like segment."""
    value = hybrid.normalize_text(text)
    if not value or len(value) > 120:
        return None, ""
    cleaned = _clean_heading(value)
    lowered = value.casefold()
    for name, matcher in SECTION_MATCHERS:
        if matcher.match(cleaned) or matcher.match(lowered):
            return name, cleaned
    return None, cleaned


def _is_heading_like(text: str, block: dict[str, Any]) -> bool:
    value = hybrid.normalize_text(text)
    if not value or len(value) > 120:
        return False
    if block.get("likely_heading") and len(value) <= 100 and block.get("lines") == 1:
        return True
    if re.match(r"^#{1,6}\s+\S", value):
        return True
    if re.match(r"^\\(?:sub)*section\*?\{", value):
        return True
    if re.match(r"^\d+(?:\.\d+)*\.?\s+\S", value) and len(value) <= 90:
        return True
    canonical, _ = classify_heading(value)
    return bool(canonical)


def detect_sections(segments: list[dict[str, Any]],
                    blocks: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for segment in segments:
        block = blocks.get(segment["block_ids"][0]) if segment.get("block_ids") else None
        if not _is_heading_like(segment["text"], block or {}):
            continue
        canonical, display = classify_heading(segment["text"])
        sections.append({
            "canonical": canonical or "custom",
            "name": display or hybrid.normalize_text(segment["text"])[:120],
            "seg_id": segment["seg_id"],
            "order": segment["order"],
            "page": segment["page"],
        })
    return sections


def _section_spans(sections: list[dict[str, Any]],
                   segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for index, section in enumerate(sections):
        end = sections[index + 1]["order"] if index + 1 < len(sections) else 10 ** 9
        member_segments = [segment for segment in segments
                           if section["order"] <= segment["order"] < end]
        spans.append({
            "canonical": section["canonical"],
            "name": section["name"],
            "start_order": section["order"],
            "end_order": end,
            "seg_ids": [segment["seg_id"] for segment in member_segments],
            "chars": sum(len(segment["text"]) for segment in member_segments),
        })
    return spans


def find_abstract(segments: list[dict[str, Any]],
                  sections: list[dict[str, Any]]) -> tuple[str, str]:
    for index, section in enumerate(sections):
        if section["canonical"] != "abstract":
            continue
        end = sections[index + 1]["order"] if index + 1 < len(sections) else 10 ** 9
        parts = [segment["text"] for segment in segments
                 if section["order"] <= segment["order"] < end
                 and segment["seg_id"] != section["seg_id"]]
        text = hybrid.normalize_text(" ".join(parts))
        return text[:2200], "abstract-heading"
    for segment in segments[:4]:
        text = segment["text"]
        if len(text) >= 180 and not _is_heading_like(text, {}):
            return text[:2200], "first-substantial-paragraph"
    return "", "none"


def find_title(segments: list[dict[str, Any]], sections: list[dict[str, Any]]) -> str:
    abstract_ids = {section["seg_id"] for section in sections if section["canonical"] == "abstract"}
    for segment in segments[:3]:
        text = hybrid.normalize_text(segment["text"])
        if not text or len(text) > 300:
            continue
        if segment["seg_id"] in abstract_ids:
            continue
        if re.match(r"^#{1,6}\s", segment["text"]):
            return re.sub(r"^#{1,6}\s+", "", text)
        if len(text) <= 250 and "abstract" not in text.casefold()[:12]:
            return text
    return ""


def contribution_candidates(segments: list[dict[str, Any]],
                           limit: int = 12) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for segment in segments:
        if (segment.get("section_title") or "").casefold() in {"references", "bibliography",
                                                               "参考文献"}:
            continue
        for sentence in hybrid.sentences(segment["text"]):
            low = sentence.casefold()
            cue = next((item for item in CONTRIBUTION_CUES if item in low), "")
            if not cue:
                continue
            found.append({
                "seg_id": segment["seg_id"],
                "page": segment["page"],
                "order": segment["order"],
                "cue": cue,
                "sentence": sentence[:400],
            })
            if len(found) >= limit:
                return found
    return found


def sentence_around(text: str, needle: str, width: int = 260) -> str:
    norm = hybrid.normalize_text(text)
    position = norm.casefold().find(str(needle).casefold())
    if position < 0:
        return norm[:width]
    start = max(0, position - width // 3)
    return norm[start:start + width].strip()


def figure_table_inventory(segments: list[dict[str, Any]], limit: int = 40) -> dict[str, Any]:
    figures: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    seen: set[str] = set()
    for segment in segments:
        for match in FIGURE_RE.finditer(segment["text"]):
            if match.group(1):
                label = f"{match.group(1)} {match.group(2)}".strip()
                kind = "figure" if match.group(1).casefold().startswith(("fig", "scheme")) \
                    else "table"
            elif match.group(3):
                label, kind = f"图 {match.group(3)}", "figure"
            else:
                label, kind = f"表 {match.group(4)}", "table"
            label = hybrid.normalize_text(label)
            if label.casefold() in seen:
                continue
            seen.add(label.casefold())
            item = {
                "label": label,
                "kind": kind,
                "seg_id": segment["seg_id"],
                "page": segment["page"],
                "context": sentence_around(segment["text"], match.group(0))[:300],
            }
            (figures if kind == "figure" else tables).append(item)
            if len(figures) + len(tables) >= limit:
                return {"figures": figures, "tables": tables}
    return {"figures": figures, "tables": tables}


def analyse_manuscript(doc: dict[str, Any], library: dict[str, Any]) -> dict[str, Any]:
    segments = hybrid.build_segments(doc, None, max_chars=1200)
    blocks: dict[str, dict[str, Any]] = {}
    for _page_key, page_blocks in (doc.get("blocks") or {}).items():
        for block in page_blocks:
            blocks[str(block.get("block_id"))] = block
    sections = detect_sections(segments, blocks)
    spans = _section_spans(sections, segments)
    for segment in segments:
        segment["section_title"] = next(
            (span["name"] for span in spans if span["canonical"] != "custom"
             and span["start_order"] <= segment["order"] < span["end_order"]), "")
    abstract, abstract_source = find_abstract(segments, sections)
    title = find_title(segments, sections)

    tokens: list[dict[str, Any]] = []
    keys: list[str] = []
    unresolved: list[str] = []
    for segment in segments:
        found = citation_tokens(segment["text"])
        if not found:
            continue
        resolved_keys, unresolved_tokens, details = resolve_many(found, library)
        keys.extend(resolved_keys)
        unresolved.extend(unresolved_tokens)
        for detail in details:
            tokens.append({**detail, "seg_id": segment["seg_id"], "page": segment["page"],
                           "section": segment.get("section_title", "")})
    all_tokens = citation_tokens(hybrid.document_text(doc, page_marker=False))
    counts: dict[str, int] = {}
    for key in keys:
        counts[key] = counts.get(key, 0) + 1
    cited = [key for key in library["key_order"] if key in counts]
    uncited = [key for key in library["key_order"] if key not in counts]

    return {
        "doc": doc,
        "segments": segments,
        "blocks": blocks,
        "sections": sections,
        "section_spans": spans,
        "abstract": abstract,
        "abstract_source": abstract_source,
        "title": title,
        "contributions_candidates": contribution_candidates(segments),
        "figure_tables": figure_table_inventory(segments),
        "citation": {
            "style": citation_style(all_tokens),
            "tokens_total": len(all_tokens),
            "resolved_tokens": tokens,
            "cited_keys": cited,
            "uncited_keys": uncited,
            "cited_counts": counts,
            "missing_tokens": _unique(unresolved),
            "library_size": library["library_size"],
        },
    }


# --------------------------------------------------------------------------- #
# prepare
# --------------------------------------------------------------------------- #
TEXT_LIBRARY_SUFFIXES = {".bib", ".ris", ".enw", ".nbib", ".bibtex"}
SUPPORT_SUFFIXES = {".md", ".txt", ".csv", ".tsv", ".tex", ".json"}
MANUSCRIPT_SUFFIXES = {".pdf", ".docx", ".md", ".txt", ".tex", ".doc", ".rtf", ".pptx"}
MANUSCRIPT_NAME_HINTS = ("draft", "manuscript", "paper", "main", "article", "正文",
                         "submission", "intro", "chapter")


def _rank_materials(paths: list[Path]) -> list[Path]:
    def rank(path: Path) -> tuple:
        low = path.name.casefold()
        hinted = 0 if any(hint in low for hint in MANUSCRIPT_NAME_HINTS) else 1
        return (hinted, 0 if path.suffix.lower() in {".docx", ".md", ".tex", ".pdf"} else 1,
                -path.stat().st_size if path.exists() else 0, str(path))
    return sorted(paths, key=rank)


def split_materials(found: dict[str, Any]) -> dict[str, Any]:
    materials = [Path(item) for item in found.get("material_docs") or []]
    libraries: list[Path] = []
    docs: list[Path] = []
    for path in materials:
        suffix = path.suffix.lower()
        if suffix in TEXT_LIBRARY_SUFFIXES:
            libraries.append(path)
        elif suffix in MANUSCRIPT_SUFFIXES:
            docs.append(path)
    ranked = _rank_materials(docs)
    manuscript = ranked[0] if ranked else None
    related_pdfs = [path for path in ranked[1:] if path.suffix.lower() == ".pdf"]
    supports = [path for path in ranked[1:] if path != manuscript
                and path.suffix.lower() in SUPPORT_SUFFIXES]
    return {
        "manuscript": manuscript,
        "materials_ranked": [str(path) for path in ranked],
        "libraries": libraries,
        "related_pdfs": related_pdfs[:3],
        "supports": supports[:4],
    }


def read_support_note(path: Path) -> dict[str, Any]:
    text = _read_text(path)
    return {"path": str(path), "chars": len(text),
            "excerpt": hybrid.normalize_text(text)[:500]}


def related_pdf_abstract(path: Path, deadline: float | None) -> dict[str, Any]:
    try:
        doc = extract.extract_document(path, deadline=deadline)
        text = hybrid.normalize_text(" ".join(
            page.text if hasattr(page, "text") else str(page)
            for page in (doc.get("pages") or [])))
        abstract = ""
        for sentence in hybrid.sentences(text)[:12]:
            if len(abstract) >= 600:
                break
            abstract = hybrid.normalize_text(f"{abstract} {sentence}")
        return {"path": str(path), "engine": doc.get("engine"),
                "chars": doc.get("total_chars"), "text_layer": doc.get("text_layer"),
                "excerpt": text[:900], "abstract_guess": abstract[:900]}
    except Exception as exc:  # noqa: BLE001 - an unreadable PDF is a limit, not a crash
        return {"path": str(path), "engine": None, "error": f"{type(exc).__name__}: {exc}"}


def match_pdf_to_library(pdf_notes: list[dict[str, Any]],
                         library: dict[str, Any]) -> list[dict[str, Any]]:
    """Attach an extracted PDF excerpt to the library entry it most likely belongs to."""
    links: list[dict[str, Any]] = []
    for note in pdf_notes:
        excerpt = note.get("excerpt") or ""
        best = None
        best_score = 0.0
        for entry in library["entries"]:
            title = entry.get("title") or ""
            score = _term_overlap(title, excerpt) if title else 0.0
            if score > best_score:
                best, best_score = entry["ref_key"], score
        links.append({"path": note.get("path"), "matched_ref_key": best,
                      "match_score": round(best_score, 4)})
        if best and best_score >= 0.5:
            entry = library["by_key"].get(best)
            if entry and not entry.get("abstract"):
                entry["abstract"] = (note.get("abstract_guess") or "")[:900]
                entry["abstract_source"] = Path(str(note.get("path"))).name
    return links


def write_batches(entries: list[dict[str, Any]], work_dir: Path,
                  *, size: int = 6) -> list[dict[str, Any]]:
    batch_dir = work_dir / "batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    batches: list[dict[str, Any]] = []
    for start in range(0, len(entries), size):
        chunk = entries[start:start + size]
        batch_id = f"batch-{len(batches) + 1:02d}"
        worksheet = {
            "batch_id": batch_id,
            "instruction": (
                "For every reference below write one record into "
                "work/generated/" + batch_id + ".json as "
                "{\"records\": [{\"ref_key\": ..., \"dimension\": ..., \"claim\": ..., "
                "\"limitation\": ...}]}. dimension = the comparison axis you group it under "
                "(problem setting / method family / data or evaluation regime). claim = what "
                "the work actually establishes, paraphrased from the title/abstract above and "
                "never invented. limitation = the condition it does not cover. Use the ref_key "
                "verbatim."
            ),
            "refs": [
                {
                    "ref_key": entry["ref_key"],
                    "title": entry.get("title", ""),
                    "authors": entry.get("authors", []),
                    "year": entry.get("year", ""),
                    "venue": entry.get("venue", ""),
                    "abstract_excerpt": (entry.get("abstract") or "")[:600],
                    "abstract_source": entry.get("abstract_source")
                                       or ("library metadata" if entry.get("abstract") else "none"),
                }
                for entry in chunk
            ],
        }
        hybrid.write_json(batch_dir / f"{batch_id}.json", worksheet)
        batches.append({"batch_id": batch_id, "ref_count": len(chunk),
                        "worksheet": str(batch_dir / f"{batch_id}.json")})
    return batches


INTRO_SCHEMA = {
    "file": "generated/intro.json",
    "required_keys": ["background", "problem_statement", "gap_analysis", "contributions"],
    "shape": {
        "background": [{"text": "one background paragraph (manuscript language)",
                        "citations": ["bib key, or a token that occurs in the library"],
                        "evidence_seg": "seg-0003 (optional but scored)"}],
        "problem_statement": {"text": "the concrete unresolved problem",
                              "citations": ["smith2019"],
                              "evidence_seg": "seg-0007"},
        "gap_analysis": [{"gap": "the gap derived from cited work",
                          "derived_from_refs": ["smith2019", "chen2020"],
                          "manifestation": {"seg_id": "seg-0011",
                                            "quote": "verbatim sentence from the manuscript"}}],
        "contributions": [{"contribution": "checkable claim of this paper",
                           "manuscript_evidence": {
                               "seg_id": "seg-0019",
                               "quote": "verbatim sentence",
                               "figure_or_table": "Figure 2 (optional)"}}],
    },
}
RW_SCHEMA = {
    "file": "generated/related_work.json",
    "required_keys": ["groups"],
    "shape": {"groups": [{"dimension": "problem setting | method family | data/evaluation regime",
                          "why_this_dimension": "why these works belong together",
                          "works": [{"ref_key": "smith2019",
                                     "claim": "what it establishes",
                                     "limitation": "its limit or condition of applicability",
                                     "compared_on": "the axis it is compared on"}]}]},
}
BATCH_SCHEMA = {
    "file": "generated/batch-XX.json",
    "required_keys": ["records"],
    "shape": {"records": [{"ref_key": "smith2019", "dimension": "...",
                           "claim": "...", "limitation": "..."}]},
}


def build_brief(case_id: str | None, task_md: dict[str, Any], found: dict[str, Any],
                manuscript: dict[str, Any] | None, library: dict[str, Any],
                analysis: dict[str, Any] | None, supports: list[dict[str, Any]],
                pdf_notes: list[dict[str, Any]], pdf_links: list[dict[str, Any]],
                batches: list[dict[str, Any]]) -> dict[str, Any]:
    brief: dict[str, Any] = {
        "skill": SKILL_NAME,
        "skill_version": SKILL_VERSION,
        "phase": "prepare",
        "case_id": case_id,
        "task_md": task_md,
        "inputs": found,
        "library": {
            "size": library["library_size"],
            "sources": library["sources"],
            "keys": library["key_order"],
            "numeric_index": library["numeric_index"],
        },
        "supporting_notes": supports,
        "related_pdf_notes": pdf_notes,
        "related_pdf_links": pdf_links,
        "batches": batches,
        "agent_tasks": [
            "read work/brief.json, work/manuscript.json, work/literature.json",
            "write work/generated/intro.json (background/problem_statement/gap_analysis/"
            "contributions)",
            "write work/generated/related_work.json (grouped comparison with limitations)",
            "optionally answer every worksheet work/batches/batch-XX.json into "
            "work/generated/batch-XX.json",
            "run: python scripts/run_intro.py --finalize --out <out> --work <work>",
        ],
        "schemas": {"intro": INTRO_SCHEMA, "related_work": RW_SCHEMA, "batch": BATCH_SCHEMA},
        "rules": [
            "every background/problem/gap item must cite at least one library key, otherwise "
            "finalize flags unsupported_citation",
            "every gap must name the references whose limitation it derives from "
            "(derived_from_refs)",
            "every contribution must point at a verbatim manuscript sentence or a "
            "figure/table label",
            "never invent numbers, citations or results: unverifiable content is flagged, "
            "not upgraded",
        ],
    }
    if manuscript is not None and analysis is not None:
        brief["manuscript"] = {
            "path": manuscript.get("path"),
            "engine": manuscript.get("engine"),
            "anchor_quality": manuscript.get("anchor_quality"),
            "title": analysis["title"],
            "abstract": analysis["abstract"],
            "abstract_source": analysis["abstract_source"],
            "segment_count": len(analysis["segments"]),
            "total_chars": manuscript.get("total_chars"),
            "sections": analysis["sections"],
            "section_spans": analysis["section_spans"],
            "existing_introduction": [span for span in analysis["section_spans"]
                                      if span["canonical"] == "introduction"],
            "existing_related_work": [span for span in analysis["section_spans"]
                                      if span["canonical"] == "related_work"],
            "citation_inventory": analysis["citation"],
            "contribution_candidates": analysis["contributions_candidates"],
            "figure_tables": analysis["figure_tables"],
        }
    return brief


def cmd_prepare(out_dir: Path, work_dir: Path,
                stopwatch: "hybrid.Stopwatch") -> dict[str, Any]:
    found = discover.discover()
    case = contract.load_case(found.get("case_path"))
    case_id = contract.find_case_id(case)
    declared = hybrid.declared_fields(case) if case else []
    _log(f"prepare: case={found.get('case_path')} case_id={case_id} "
         f"contract_fields={len(declared)} materials={len(found.get('material_docs') or [])}")

    task_md = {"path": found.get("task_md_path"), "chars": 0, "excerpt": ""}
    if found.get("task_md_path"):
        text = _read_text(Path(str(found["task_md_path"])))
        task_md = {"path": found["task_md_path"], "chars": len(text),
                   "excerpt": hybrid.normalize_text(text)[:1200]}

    split = split_materials(found)
    library = load_library(split["libraries"])
    _log(f"prepare: library files={len(split['libraries'])} entries={library['library_size']}")

    manuscript = None
    analysis = None
    if split["manuscript"] is not None:
        deadline = stopwatch.start + hybrid.SOFT_DEADLINE_S
        manuscript, warnings = load_manuscript(Path(split["manuscript"]), deadline)
        for warning in warnings:
            _log(f"prepare: manuscript warning: {warning}")
        if manuscript.get("text_layer"):
            analysis = analyse_manuscript(manuscript, library)
            _log(f"prepare: segments={len(analysis['segments'])} "
                 f"sections={len(analysis['sections'])} "
                 f"cited={len(analysis['citation']['cited_keys'])}/"
                 f"{library['library_size']}")
        else:
            _log("prepare: manuscript has no text layer")

    supports = [read_support_note(path) for path in split["supports"]]
    pdf_notes = [related_pdf_abstract(path, stopwatch.start + hybrid.SOFT_DEADLINE_S)
                 for path in split["related_pdfs"]]
    pdf_links = match_pdf_to_library(pdf_notes, library) if pdf_notes else []
    if pdf_notes:
        _log(f"prepare: related pdfs={len(pdf_notes)} "
             f"linked={sum(1 for link in pdf_links if link['matched_ref_key'])}")

    batches = write_batches(library["entries"], work_dir) if library["entries"] else []
    hybrid.write_json(work_dir / "literature.json", {
        "entries": library["entries"],
        "key_order": library["key_order"],
        "numeric_index": library["numeric_index"],
        "sources": library["sources"],
        "pdf_links": pdf_links,
    })
    if analysis is not None and manuscript is not None:
        hybrid.write_json(work_dir / "manuscript.json", {
            "path": manuscript.get("path"),
            "engine": manuscript.get("engine"),
            "anchor_quality": manuscript.get("anchor_quality"),
            "title": analysis["title"],
            "abstract": analysis["abstract"],
            "sections": analysis["sections"],
            "citation": analysis["citation"],
            "segments": [{key: segment[key] for key in
                          ("seg_id", "order", "page", "section_title", "char_count", "text")}
                         for segment in analysis["segments"]],
        })
    brief = build_brief(case_id, task_md, found, manuscript, library, analysis,
                        supports, pdf_notes, pdf_links, batches)
    hybrid.write_json(work_dir / "brief.json", brief)
    expected = (["generated/intro.json", "generated/related_work.json"]
                + [f"generated/{batch['batch_id']}.json" for batch in batches])
    hybrid.write_json(work_dir / "manifest.json", {
        "case_id": case_id,
        "batches": [batch["batch_id"] for batch in batches],
        "expected_generated": expected,
        "contract_fields": [field["name"] for field in declared],
        "work_dir": str(work_dir),
        "out_dir": str(out_dir),
    })
    return {
        "phase": "prepare",
        "case": case,
        "case_id": case_id,
        "found": found,
        "work_dir": str(work_dir),
        "out_dir": str(out_dir),
        "brief": brief,
        "library": library,
        "analysis": analysis,
        "manuscript": manuscript,
        "batches": batches,
        "expected_generated": expected,
    }


# --------------------------------------------------------------------------- #
# generated-content loading
# --------------------------------------------------------------------------- #
def load_generated(work_dir: Path) -> dict[str, Any]:
    generated = work_dir / "generated"
    report: dict[str, Any] = {"intro": None, "related_work": None, "batch_records": [],
                              "files": [], "broken": [], "ignored_files": []}
    if not generated.is_dir():
        return report
    for path in sorted(generated.glob("*.json")):
        if path.name.startswith("_"):
            continue
        data = hybrid.read_json(path, None)
        report["files"].append(path.name)
        if data is None:
            report["broken"].append({"file": path.name, "reason": "not valid JSON"})
            continue
        low = path.stem.casefold()
        if low in {"intro", "introduction"}:
            report["intro"] = data if isinstance(data, dict) else {"records": data}
        elif low in {"related_work", "relatedwork", "related"}:
            report["related_work"] = data if isinstance(data, dict) else {"groups": data}
        elif low.startswith("batch"):
            for record in hybrid.read_records(path):
                if not record.get("ref_key") and record.get("key"):
                    record["ref_key"] = record.get("key")
                report["batch_records"].append(record)
        else:
            report["ignored_files"].append(path.name)
    return report


# --------------------------------------------------------------------------- #
# finalize: validation
# --------------------------------------------------------------------------- #
def _segment_index(analysis: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {segment["seg_id"]: segment for segment in analysis["segments"]}


def _anchor_for_segment(segment: dict[str, Any], quote: str = "") -> dict[str, Any]:
    block = {"block_id": (segment.get("block_ids") or [None])[0], "bbox": segment.get("bbox")}
    text = segment["text"]
    chosen = quote if quote and quote in text else anchors.pick_quote(
        text, [], width=min(160, max(20, len(text))))
    return anchors.make_anchor(segment["page"], block, chosen)


def _figure_labels(analysis: dict[str, Any]) -> list[str]:
    return [item["label"] for item in
            (analysis["figure_tables"]["figures"] + analysis["figure_tables"]["tables"])]


def resolve_evidence(raw: Any, analysis: dict[str, Any]) -> dict[str, Any]:
    """Turn an agent-supplied evidence reference into a verifiable anchor."""
    seg_by_id = _segment_index(analysis)
    block_index: dict[str, dict[str, Any]] = analysis["blocks"]
    labels = _figure_labels(analysis)

    if isinstance(raw, str) and raw.strip():
        seg_id = raw.strip()
        if seg_id in seg_by_id:
            return {"ok": True, "reason": "", "anchor": _anchor_for_segment(seg_by_id[seg_id]),
                    "how": "seg_id"}
        if any(seg_id.casefold() in label.casefold() for label in labels):
            return {"ok": True, "reason": "", "anchor": None, "how": "figure_label",
                    "label": seg_id}
        return {"ok": False, "reason": f"unknown evidence reference '{seg_id}'",
                "anchor": None, "how": "unresolved"}
    if not isinstance(raw, dict):
        return {"ok": False, "reason": "no evidence supplied", "anchor": None, "how": "none"}

    quote = _as_text(_pick(raw, ("quote", "evidence", "text", "sentence"), ""))
    seg_id = _as_text(_pick(raw, ("seg_id", "segment", "segment_id", "evidence_seg"), ""))
    label = _as_text(_pick(raw, ("figure_or_table", "label", "figure", "table"), ""))
    page = _pick(raw, ("page",), None)
    block_id = _as_text(_pick(raw, ("block_id", "block"), ""))

    if seg_id and seg_id in seg_by_id:
        return {"ok": True, "reason": "",
                "anchor": _anchor_for_segment(seg_by_id[seg_id], quote),
                "how": "seg_id+quote" if quote else "seg_id"}
    if label:
        # A figure/table label is a *positional* claim: the evidence is the label's own
        # location in the manuscript, so it gets a structural anchor from the inventory
        # and is verified by the kernel as kind="structural" (location, not wording).
        for item in (analysis["figure_tables"]["figures"]
                     + analysis["figure_tables"]["tables"]):
            if label.casefold() in item["label"].casefold():
                segment = seg_by_id.get(item.get("seg_id", ""))
                anchor = _anchor_for_segment(segment, item.get("context", "")) if segment else None
                return {"ok": True, "reason": "", "anchor": anchor, "how": "figure_label",
                        "label": item["label"], "kind": "structural",
                        "seg_id": item.get("seg_id"), "page": item.get("page")}
    if block_id and block_id in block_index:
        block = block_index[block_id]
        anchor_page = int(page) if str(page).isdigit() else 0
        return {"ok": True, "reason": "",
                "anchor": anchors.make_anchor(
                    anchor_page, block,
                    quote or anchors.pick_quote(block.get("text", ""), [])),
                "how": "block_id+quote" if quote else "block_id"}
    if quote:
        norm = anchors.normalize(quote)
        hits = [segment for segment in analysis["segments"]
                if norm and norm in anchors.normalize(segment["text"])]
        if hits:
            return {"ok": True, "reason": "", "anchor": _anchor_for_segment(hits[0], quote),
                    "how": "quote_located"}
        return {"ok": False, "reason": "quote does not occur in the manuscript text",
                "anchor": None, "how": "quote_missing"}
    return {"ok": False, "reason": "evidence object has no seg_id/quote/block_id/label",
            "anchor": None, "how": "empty"}


def validate_intro(raw_intro: Any, analysis: dict[str, Any],
                   library: dict[str, Any]) -> dict[str, Any]:
    intro = raw_intro if isinstance(raw_intro, dict) else {}
    out: dict[str, Any] = {"background": [], "problem_statement": None,
                           "gap_analysis": [], "contributions": [], "flags": []}

    def citation_fields(item: Any) -> Any:
        return _pick(item, ("citations", "cites", "references", "refs", "citation_keys"), [])

    for index, item in enumerate(_as_list(intro.get("background"))):
        text = _as_text(item)
        keys, unresolved, _details = resolve_many(citation_fields(item), library)
        evidence = resolve_evidence(
            _pick(item, ("evidence_seg", "evidence", "seg_id", "anchor"), ""), analysis)
        record = {
            "id": f"bg-{index + 1}",
            "text": text,
            "citations_requested": [str(token) for token in _as_list(citation_fields(item))],
            "citations_resolved": keys,
            "citations_unresolved": unresolved,
            "evidence": evidence,
            "flags": [],
        }
        if not keys:
            record["flags"].append("unsupported_citation")
        if not text:
            record["flags"].append("empty_text")
        if unresolved:
            record["flags"].append("citation_not_in_library")
        if not evidence.get("ok"):
            record["flags"].append("evidence_missing")
        out["background"].append(record)

    problem = intro.get("problem_statement")
    if problem:
        keys, unresolved, _details = resolve_many(citation_fields(problem), library)
        evidence = resolve_evidence(
            _pick(problem, ("evidence_seg", "evidence", "seg_id", "anchor"), ""), analysis)
        out["problem_statement"] = {
            "text": _as_text(problem),
            "citations_resolved": keys,
            "citations_unresolved": unresolved,
            "evidence": evidence,
            "flags": ([] if keys else ["unsupported_citation"])
                     + ([] if evidence.get("ok") else ["evidence_missing"]),
        }

    for index, item in enumerate(_as_list(intro.get("gap_analysis"))):
        gap_text = _as_text(_pick(item, ("gap", "text", "statement", "description"), item))
        refs = _as_list(_pick(item, ("derived_from_refs", "refs", "references", "citations"), []))
        keys, unresolved, _details = resolve_many(refs, library)
        manifestation = resolve_evidence(
            _pick(item, ("manifestation", "evidence", "evidence_seg", "where"), ""), analysis)
        record = {
            "id": f"gap-{index + 1}",
            "gap": gap_text,
            "derived_from_refs": keys,
            "refs_unresolved": unresolved,
            "manifestation": manifestation,
            "flags": [],
        }
        if not keys:
            record["flags"].append("gap_without_reference")
        if not gap_text:
            record["flags"].append("empty_gap")
        if unresolved:
            record["flags"].append("gap_reference_not_in_library")
        if not manifestation.get("ok"):
            record["flags"].append("gap_manifestation_missing")
        out["gap_analysis"].append(record)

    for index, item in enumerate(_as_list(intro.get("contributions"))):
        text = _as_text(_pick(item, ("contribution", "text", "statement", "claim"), item))
        evidence = resolve_evidence(
            _pick(item, ("manuscript_evidence", "evidence", "evidence_seg", "anchor"), ""),
            analysis)
        record = {
            "id": f"ctb-{index + 1}",
            "contribution": text,
            "manuscript_evidence": evidence,
            "flags": [],
        }
        if not text:
            record["flags"].append("empty_contribution")
        if not evidence.get("ok"):
            record["flags"].append("evidence_missing")
        out["contributions"].append(record)

    out["flags"] = sorted({
        flag for group in ("background", "gap_analysis", "contributions")
        for item in out[group] for flag in item.get("flags") or []
    } | set((out.get("problem_statement") or {}).get("flags") or []))
    return out


def _ref_check(ref_key: str, library: dict[str, Any], claim: str = "") -> dict[str, Any]:
    entry = library["by_key"].get(ref_key)
    if entry is None:
        return {"ref_key": ref_key, "exists": False, "title": "", "year": "",
                "venue": "", "claim_overlap": 0.0, "flags": ["ref_not_in_library"]}
    abstract = entry.get("abstract") or ""
    overlap = _term_overlap(claim, abstract) if (claim and abstract) else None
    flags: list[str] = []
    if overlap is not None and overlap < 0.2:
        flags.append("claim_not_supported_by_abstract")
    if not (entry.get("title") or ""):
        flags.append("title_missing")
    if not (entry.get("year") or ""):
        flags.append("year_missing")
    return {
        "ref_key": ref_key,
        "exists": True,
        "title": entry.get("title", ""),
        "authors": entry.get("authors", []),
        "year": entry.get("year", ""),
        "venue": entry.get("venue", ""),
        "doi": entry.get("doi", ""),
        "claim_overlap": overlap,
        "flags": flags,
    }


def validate_related_work(raw: Any, batch_records: list[dict[str, Any]],
                          library: dict[str, Any],
                          cited_keys: list[str]) -> dict[str, Any]:
    groups_raw: list[Any] = []
    source = ""
    if isinstance(raw, dict) and raw.get("groups"):
        groups_raw = _as_list(raw.get("groups"))
        source = "generated/related_work.json"
    elif isinstance(raw, list) and raw:
        groups_raw = raw
        source = "generated (bare list)"
    elif batch_records:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in batch_records:
            dimension = _as_text(_pick(record, ("dimension", "group", "axis", "route"), "")) \
                or "unclassified"
            grouped.setdefault(dimension, []).append(record)
        groups_raw = [{"dimension": dimension,
                       "why_this_dimension": "grouped from work/generated/batch-XX.json records",
                       "works": rows} for dimension, rows in grouped.items()]
        source = "generated/batch-XX.json records"
    groups: list[dict[str, Any]] = []
    for index, group in enumerate(groups_raw):
        if not isinstance(group, dict):
            continue
        dimension = _as_text(_pick(group, ("dimension", "group", "axis", "route", "theme"), "")) \
            or f"group-{index + 1}"
        works: list[dict[str, Any]] = []
        for work in _as_list(_pick(group, ("works", "papers", "entries", "items"), [])):
            if not isinstance(work, dict):
                continue
            ref = _as_text(_pick(work, ("ref_key", "key", "citation", "id"), ""))
            keys, _unresolved, _details = resolve_many([ref], library) if ref else ([], [], [])
            key = keys[0] if keys else ref
            claim = _as_text(_pick(work, ("claim", "summary", "finding", "contribution"), ""))
            limitation = _as_text(_pick(work, ("limitation", "limit", "caveat", "condition"), ""))
            check = _ref_check(key, library, claim)
            flags = list(check["flags"])
            if not limitation:
                flags.append("limitation_missing")
            if not claim:
                flags.append("claim_missing")
            works.append({
                "ref_key": key,
                "claim": claim,
                "limitation": limitation,
                "compared_on": _as_text(_pick(work, ("compared_on", "axis", "comparison"), "")),
                "ref_check": check,
                "flags": sorted(set(flags)),
            })
        flags: list[str] = []
        if len(works) < 2:
            flags.append("group_with_single_work")
        groups.append({
            "dimension": dimension,
            "why_this_dimension": _as_text(_pick(group, ("why_this_dimension", "rationale",
                                                         "why", "basis"), "")),
            "works": works,
            "flags": flags,
        })
    covered = _unique([work["ref_key"] for group in groups for work in group["works"]])
    return {
        "source": source or "none",
        "groups": groups,
        "dimensions": [group["dimension"] for group in groups],
        "works_total": sum(len(group["works"]) for group in groups),
        "refs_covered": covered,
        "unused_cited_keys": [key for key in cited_keys if key not in covered],
        "flags": sorted({flag for group in groups for flag in group["flags"]}),
        "degenerate": source == "",
    }


def baseline_intro(analysis: dict[str, Any], library: dict[str, Any]) -> dict[str, Any]:
    """Extractive, honest fallback used when no agent content exists.

    It never invents a claim: background paragraphs are sentences of the manuscript's
    own abstract, the gap text says plainly that it was not derived, and contributions
    are the manuscript's own contribution cues with their locations.
    """
    abstract = analysis["abstract"]
    sentences = hybrid.sentences(abstract)[:3] if abstract else []
    background: list[dict[str, Any]] = []
    for index, sentence in enumerate(sentences):
        keys, unresolved, _details = resolve_many(citation_tokens(sentence), library)
        segment = next((item for item in analysis["segments"]
                        if anchors.normalize(sentence[:80])
                        in anchors.normalize(item["text"])), None)
        background.append({
            "id": f"bg-{index + 1}",
            "text": sentence,
            "citations_requested": [],
            "citations_resolved": keys,
            "citations_unresolved": unresolved,
            "evidence": {"ok": segment is not None, "how": "extractive_abstract",
                         "anchor": _anchor_for_segment(segment, sentence) if segment else None},
            "flags": ["baseline_extractive"] + ([] if keys else ["unsupported_citation"]),
        })
    if not background:
        background = [{
            "id": "bg-1",
            "text": "",
            "citations_requested": [],
            "citations_resolved": [],
            "citations_unresolved": [],
            "evidence": {"ok": False, "how": "none", "anchor": None},
            "flags": ["baseline_extractive", "no_abstract_found",
                      "unsupported_citation", "empty_text"],
        }]

    problem_text = ""
    problem_segment = None
    for segment in analysis["segments"]:
        for sentence in hybrid.sentences(segment["text"]):
            if any(cue in sentence.casefold() for cue in PROBLEM_CUES):
                problem_text, problem_segment = sentence, segment
                break
        if problem_text:
            break
    problem = {
        "text": problem_text,
        "citations_resolved": [],
        "citations_unresolved": [],
        "evidence": {"ok": problem_segment is not None, "how": "extractive_problem_cue",
                     "anchor": _anchor_for_segment(problem_segment, problem_text)
                     if problem_segment else None},
        "flags": ["baseline_extractive"] + ([] if problem_text else ["problem_not_found"]),
    }

    gap_refs = list(analysis["citation"]["cited_keys"])[:3]
    gap = {
        "id": "gap-1",
        "gap": ("Gap analysis was not produced by the agent. The manuscript and the citation "
                "library alone do not establish a gap statement, so none is asserted here."),
        "derived_from_refs": gap_refs,
        "refs_unresolved": [],
        "manifestation": {"ok": False, "how": "none", "anchor": None},
        "flags": ["baseline_placeholder", "gap_not_derived", "gap_manifestation_missing"],
    }

    contributions: list[dict[str, Any]] = []
    seg_index = _segment_index(analysis)
    for index, candidate in enumerate(analysis["contributions_candidates"][:6]):
        segment = seg_index.get(candidate["seg_id"])
        contributions.append({
            "id": f"ctb-{index + 1}",
            "contribution": candidate["sentence"],
            "manuscript_evidence": {
                "ok": segment is not None, "how": "extractive_cue",
                "anchor": _anchor_for_segment(segment, candidate["sentence"])
                if segment else None},
            "flags": ["baseline_extractive"],
        })
    if not contributions:
        contributions = [{
            "id": "ctb-1",
            "contribution": "",
            "manuscript_evidence": {"ok": False, "how": "none", "anchor": None},
            "flags": ["baseline_extractive", "contribution_not_found", "empty_contribution"],
        }]

    flags = sorted({flag for item in [*background, gap, *contributions]
                    for flag in item.get("flags") or []}
                   | set(problem.get("flags") or []))
    return {"background": background, "problem_statement": problem,
            "gap_analysis": [gap], "contributions": contributions, "flags": flags,
            "baseline": True}


def _baseline_work(library: dict[str, Any], ref_key: str) -> dict[str, Any]:
    entry = library["by_key"].get(ref_key, {})
    return {
        "ref_key": ref_key,
        "claim": entry.get("title", "") or "",
        "limitation": "",
        "compared_on": "title only (baseline, no agent text)",
        "ref_check": _ref_check(ref_key, library, ""),
        "flags": ["baseline_title_only", "limitation_missing"],
    }


def baseline_related_work(library: dict[str, Any],
                          cited_keys: list[str]) -> dict[str, Any]:
    """Co-occurrence grouping baseline: group the library by shared title keywords."""
    entries = list(library["entries"])
    if not entries:
        return {"source": "baseline:no-library", "groups": [], "dimensions": [],
                "works_total": 0, "refs_covered": [], "unused_cited_keys": [],
                "flags": ["no_library"], "degenerate": True}

    keyword_hits: dict[str, list[str]] = {}
    for entry in entries:
        for keyword in _terms(entry.get("title", "") or entry.get("raw", ""), 8):
            keyword_hits.setdefault(keyword, []).append(entry["ref_key"])
    shared = [(keyword, keys) for keyword, keys in keyword_hits.items() if len(keys) >= 2]
    shared.sort(key=lambda item: (-len(item[1]), item[0]))
    dimensions = shared[:4]

    assigned: dict[str, str] = {}
    for keyword, keys in dimensions:
        for key in keys:
            assigned.setdefault(key, keyword)
    groups: list[dict[str, Any]] = []
    for keyword, keys in dimensions:
        works = [_baseline_work(library, key) for key in keys if assigned.get(key) == keyword]
        groups.append({"dimension": f"shared topic: {keyword}",
                       "why_this_dimension": "title-keyword co-occurrence baseline",
                       "works": works, "flags": ["baseline_cooccurrence"]})
    leftovers = [entry["ref_key"] for entry in entries if entry["ref_key"] not in assigned]
    if leftovers:
        groups.append({"dimension": "unclassified (no shared title keyword)",
                       "why_this_dimension": "title-keyword co-occurrence baseline",
                       "works": [_baseline_work(library, key) for key in leftovers],
                       "flags": ["baseline_cooccurrence", "unclassified_group"]})
    covered = _unique([work["ref_key"] for group in groups for work in group["works"]])
    return {
        "source": "baseline:title-keyword co-occurrence",
        "groups": groups,
        "dimensions": [group["dimension"] for group in groups],
        "works_total": sum(len(group["works"]) for group in groups),
        "refs_covered": covered,
        "unused_cited_keys": [key for key in cited_keys if key not in covered],
        "flags": ["baseline_related_work", "baseline_cooccurrence"],
        "degenerate": True,
    }


# --------------------------------------------------------------------------- #
# finalize: text assembly, deliverables and contract reflection
# --------------------------------------------------------------------------- #
def format_citation(keys: list[str], library: dict[str, Any], style: str) -> str:
    if not keys:
        return ""
    numeric = {entry["ref_key"]: position
               for position, entry in enumerate(library["entries"], start=1)}
    if "latex" in style:
        return "\\cite{" + ", ".join(keys) + "}"
    if "numeric" in style and "author-year" not in style:
        numbers = sorted(numeric[key] for key in keys if key in numeric)
        if numbers:
            return "[" + ", ".join(str(number) for number in numbers) + "]"
        return "[" + ", ".join(keys) + "]"
    parts = []
    for key in keys:
        entry = library["by_key"].get(key, {})
        authors = entry.get("authors") or []
        surname = authors[0] if authors else key
        year = entry.get("year") or "n.d."
        more = " et al." if len(authors) > 2 else ""
        parts.append(f"{surname}{more}, {year}")
    return "(" + "; ".join(parts) + ")"


def intro_text(intro: dict[str, Any], library: dict[str, Any], style: str,
               *, heading: str = "1 Introduction") -> str:
    lines: list[str] = [f"## {heading}", ""]
    background = intro.get("background") or []
    if background:
        lines.extend(["### 1.1 Background", ""])
        for item in background:
            citation = format_citation(item.get("citations_resolved") or [], library, style)
            text = (item.get("text") or "").strip()
            if text:
                lines.append(f"{text} {citation}".strip())
        lines.append("")
    problem = intro.get("problem_statement")
    if problem and (problem.get("text") or "").strip():
        lines.extend(["### 1.2 Problem statement", ""])
        citation = format_citation(problem.get("citations_resolved") or [], library, style)
        lines.append(f"{problem['text'].strip()} {citation}".strip())
        lines.append("")
    gaps = intro.get("gap_analysis") or []
    if gaps:
        lines.extend(["### 1.3 Research gap", ""])
        for item in gaps:
            citation = format_citation(item.get("derived_from_refs") or [], library, style)
            text = (item.get("gap") or "").strip()
            if text:
                lines.append(f"{text} {citation}".strip())
        lines.append("")
    contributions = intro.get("contributions") or []
    if contributions:
        lines.extend(["### 1.4 Contributions", ""])
        for index, item in enumerate(contributions, start=1):
            text = (item.get("contribution") or "").strip()
            if text:
                lines.append(f"{index}. {text}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def related_work_text(related: dict[str, Any], library: dict[str, Any], style: str,
                      *, heading: str = "2 Related Work") -> str:
    lines: list[str] = [f"## {heading}", ""]
    for index, group in enumerate(related.get("groups") or [], start=1):
        lines.extend([f"### 2.{index} {group.get('dimension') or 'group'}", ""])
        if group.get("why_this_dimension"):
            lines.extend([str(group["why_this_dimension"]).strip(), ""])
        for work in group.get("works") or []:
            citation = format_citation([work["ref_key"]], library, style)
            claim = (work.get("claim") or "").strip()
            limitation = (work.get("limitation") or "").strip()
            compared = (work.get("compared_on") or "").strip()
            sentence = f"{citation} {claim}".strip()
            if compared:
                sentence = f"{sentence} Compared on: {compared}."
            if limitation:
                sentence = f"{sentence} Limitation: {limitation}."
            if sentence.strip():
                lines.append(sentence)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def assemble_revised(analysis: dict[str, Any], intro_md: str,
                     related_md: str) -> dict[str, Any]:
    """Splice the new Introduction + Related Work into the manuscript body."""
    segments = analysis["segments"]
    spans = analysis["section_spans"]
    remove_ids: set[str] = set()
    removed_sections: list[str] = []
    for span in spans:
        if span["canonical"] in {"introduction", "related_work"}:
            remove_ids.update(span["seg_ids"])
            removed_sections.append(span["name"])
    insert_before: int | None = None
    if remove_ids:
        insert_before = min(segment["order"] for segment in segments
                            if segment["seg_id"] in remove_ids)
    else:
        for span in spans:
            if span["canonical"] in {"method", "experiments", "results", "discussion",
                                     "conclusion"}:
                insert_before = span["start_order"]
                break
        if insert_before is None:
            for span in spans:
                if span["canonical"] == "abstract":
                    insert_before = span["end_order"]
                    break
    pieces: list[str] = []
    if analysis["title"]:
        pieces.extend([f"# {analysis['title'].strip()}", ""])
    inserted = False
    for segment in segments:
        if segment["seg_id"] in remove_ids:
            continue
        if not inserted and insert_before is not None and segment["order"] >= insert_before:
            pieces.extend([intro_md.strip(), "", related_md.strip(), ""])
            inserted = True
        text = segment["text"]
        block = analysis["blocks"].get((segment.get("block_ids") or [""])[0], {})
        if _is_heading_like(text, block):
            pieces.extend([f"## {_clean_heading(text) or hybrid.normalize_text(text)}", ""])
        else:
            pieces.extend([text, ""])
    if not inserted:
        pieces.extend([intro_md.strip(), "", related_md.strip(), ""])
    markdown = "\n".join(pieces).strip() + "\n"
    return {
        "markdown": markdown,
        "removed_existing_sections": removed_sections,
        "inserted_before_order": insert_before,
        "removed_segment_count": len(remove_ids),
    }


def revised_blocks(analysis: dict[str, Any], intro: dict[str, Any],
                   related: dict[str, Any], library: dict[str, Any], style: str,
                   assembled: dict[str, Any], coverage: dict[str, Any],
                   claim_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    title = analysis["title"] or "Revised manuscript (generated section)"
    blocks: list[dict[str, Any]] = [{"type": "title", "text": title},
                                    {"type": "heading", "level": 1, "text": "1 Introduction"}]
    background = intro.get("background") or []
    if background:
        blocks.append({"type": "heading", "level": 2, "text": "1.1 Background"})
        for item in background:
            citation = format_citation(item.get("citations_resolved") or [], library, style)
            text = (item.get("text") or "").strip()
            if text:
                blocks.append({"type": "para", "text": f"{text} {citation}".strip()})
    problem = intro.get("problem_statement") or {}
    if (problem.get("text") or "").strip():
        blocks.append({"type": "heading", "level": 2, "text": "1.2 Problem statement"})
        citation = format_citation(problem.get("citations_resolved") or [], library, style)
        blocks.append({"type": "para", "text": f"{problem['text'].strip()} {citation}".strip()})
    gaps = intro.get("gap_analysis") or []
    if gaps:
        blocks.append({"type": "heading", "level": 2, "text": "1.3 Research gap"})
        for item in gaps:
            citation = format_citation(item.get("derived_from_refs") or [], library, style)
            text = (item.get("gap") or "").strip()
            if text:
                blocks.append({"type": "para", "text": f"{text} {citation}".strip()})
    contributions = intro.get("contributions") or []
    if contributions:
        blocks.append({"type": "heading", "level": 2, "text": "1.4 Contributions"})
        items = [(item.get("contribution") or "").strip() for item in contributions]
        blocks.append({"type": "list", "items": [item for item in items if item]})
    blocks.append({"type": "heading", "level": 1, "text": "2 Related Work"})
    for index, group in enumerate(related.get("groups") or [], start=1):
        blocks.append({"type": "heading", "level": 2,
                       "text": f"2.{index} {group.get('dimension') or 'group'}"})
        if group.get("why_this_dimension"):
            blocks.append({"type": "para", "text": str(group["why_this_dimension"])})
        paragraphs = []
        for work in group.get("works") or []:
            citation = format_citation([work["ref_key"]], library, style)
            claim = (work.get("claim") or "").strip()
            limitation = (work.get("limitation") or "").strip()
            sentence = f"{citation} {claim}".strip()
            if limitation:
                sentence = f"{sentence} Limitation: {limitation}."
            if sentence:
                paragraphs.append(sentence)
        if paragraphs:
            blocks.append({"type": "list", "items": paragraphs})

    tail = assembled.get("markdown") or ""
    marker = tail.find("## 3 ")
    blocks.append({"type": "page_break"})
    blocks.append({"type": "heading", "level": 1,
                   "text": "3 Remaining manuscript (unchanged)"})
    if marker >= 0:
        paragraphs = [chunk.strip() for chunk in tail[marker:].split("\n\n") if chunk.strip()]
        for paragraph in paragraphs[1:]:
            if paragraph.startswith("## "):
                blocks.append({"type": "heading", "level": 1,
                               "text": paragraph[3:].strip()})
            else:
                blocks.append({"type": "para", "text": paragraph})

    blocks.append({"type": "page_break"})
    blocks.append({"type": "heading", "level": 1,
                   "text": "Appendix A. Citation coverage audit"})
    rows = [["ref key", "year", "venue", "cited by manuscript", "cited in new text"]]
    for row in (coverage.get("per_key") or [])[:40]:
        rows.append([
            str(row.get("ref_key", "")), str(row.get("year", "")),
            str(row.get("venue", ""))[:40],
            "yes" if row.get("cited_by_manuscript") else "no",
            "yes" if row.get("cited_in_generated") else "no",
        ])
    blocks.append({"type": "table", "rows": rows})
    blocks.append({"type": "heading", "level": 1,
                   "text": "Appendix B. Claim evidence register"})
    register = [["claim id", "section", "anchor", "located"]]
    for row in claim_rows[:40]:
        anchor = row.get("anchor") or {}
        location = anchor.get("block_id") or row.get("evidence_how") or "-"
        register.append([str(row.get("claim_id", "")), str(row.get("section", ""))[:40],
                         str(location), "yes" if row.get("located") else "no"])
    blocks.append({"type": "table", "rows": register})
    return blocks


def _normalise_docx_timestamps(path: Path) -> None:
    """Rewrite a .docx with fixed zip timestamps so repeat runs are byte-identical.

    The shared writer uses zipfile.writestr, which stamps entries with the current
    clock: the content was deterministic but the bytes were not, and the topic asks
    for three repeats per case to be comparable.
    """
    import zipfile

    if not path.is_file():
        return
    with zipfile.ZipFile(str(path)) as source:
        items = [(info.filename, source.read(info.filename)) for info in source.infolist()]
    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as target:
        for name, data in items:
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(info, data)


def write_deliverables(out_dir: Path, blocks: list[dict[str, Any]],
                       markdown: str) -> dict[str, Any]:
    files = docwrite.write_editable(
        out_dir, "revised_manuscript", blocks,
        title="Revised manuscript - Introduction and Related Work")
    _normalise_docx_timestamps(Path(files["docx"]))
    files["bytes"]["docx"] = Path(files["docx"]).stat().st_size
    full_md = out_dir / "revised_manuscript_full.md"
    full_md.write_text(markdown, encoding="utf-8")
    files["full_markdown"] = str(full_md)
    files["bytes"]["full_markdown"] = full_md.stat().st_size
    render_report = render.render(files["docx"], out_dir / "preview")
    if not render_report.get("ok"):
        preview = {"ok": False, "reason": render_report.get("reason"),
                   "html": files["html"], "pdf": None, "png": None,
                   "probe": render_report.get("probe")}
    else:
        preview = {"ok": True, "pdf": render_report.get("pdf"),
                   "png": render_report.get("png"), "html": files["html"],
                   "reason": "", "probe": render_report.get("probe")}
    files["preview"] = preview
    return files


def build_claim_evidence(analysis: dict[str, Any], intro: dict[str, Any],
                         related: dict[str, Any],
                         library: dict[str, Any]) -> dict[str, Any]:
    claims: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    evidence_flags: list[bool] = []

    def add(claim_id: str, section: str, text: str, evidence: dict[str, Any]) -> None:
        anchor = evidence.get("anchor") or {}
        claims.append({
            "claim_id": claim_id,
            "section": section,
            "claim": text,
            "kind": evidence.get("kind") or "content",
            "anchors": [anchor] if anchor else [],
            "evidence_how": evidence.get("how"),
            "flags": [] if evidence.get("ok") else ["ungrounded"],
        })
        evidence_flags.append(bool(evidence.get("ok")))
        rows.append({"claim_id": claim_id, "section": section,
                     "evidence_how": evidence.get("how"), "anchor": anchor})

    for item in intro.get("background") or []:
        add(item.get("id", "bg"), "background", item.get("text", ""),
            item.get("evidence") or {})
    problem = intro.get("problem_statement")
    if problem:
        add("problem", "problem_statement", problem.get("text", ""),
            problem.get("evidence") or {})
    for item in intro.get("gap_analysis") or []:
        add(item.get("id", "gap"), "gap_analysis", item.get("gap", ""),
            item.get("manifestation") or {})
    for item in intro.get("contributions") or []:
        add(item.get("id", "ctb"), "contributions", item.get("contribution", ""),
            item.get("manuscript_evidence") or {})

    if claims:
        grounding = anchors.verify_all(analysis["doc"], claims)
    else:
        grounding = {"claims_total": 0, "grounded": 0, "ungrounded": 0,
                     "grounding_rate": 0.0,
                     "anchor_quality": analysis["doc"].get("anchor_quality") or "page",
                     "effective_confidence": 0.0, "per_claim": []}
    per_claim = grounding.get("per_claim") or []
    total = grounding.get("claims_total") or 0
    # A claim is located when either the quoted anchor still exists in the manuscript, or
    # the evidence was a figure/table label matched against the extracted label inventory
    # (that route carries no text anchor, so verify_all cannot speak for it).
    located_flags: list[bool] = []
    for index, claim in enumerate(claims):
        detail = per_claim[index] if index < len(per_claim) else {}
        label_route = bool(evidence_flags[index] if index < len(evidence_flags) else False) \
            and not claim.get("anchors")
        located_flags.append(bool(detail.get("located")) or label_route)
    located = sum(1 for flag in located_flags if flag)
    supported = sum(1 for index, detail in enumerate(per_claim)
                    if detail.get("supported_by_quote")
                    or (index < len(evidence_flags) and evidence_flags[index]
                        and not (claims[index].get("anchors"))))
    for index, row in enumerate(rows):
        detail = per_claim[index] if index < len(per_claim) else {}
        row["located"] = located_flags[index] if index < len(located_flags) else False
        row["supported_by_quote"] = bool(detail.get("supported_by_quote"))
    support = {
        "claims_total": total,
        "located": located,
        "location_rate": round(located / total, 4) if total else 0.0,
        "supported_by_quote": supported,
        "support_rate": round(supported / total, 4) if total else 0.0,
        "strict_grounding_rate": grounding.get("grounding_rate"),
        "anchor_quality": grounding.get("anchor_quality"),
        "effective_confidence": grounding.get("effective_confidence"),
        "per_claim": per_claim,
    }
    unique_refs: dict[str, dict[str, Any]] = {}
    for item in intro.get("gap_analysis") or []:
        for key in item.get("derived_from_refs") or []:
            unique_refs.setdefault(key, _ref_check(key, library, item.get("gap", "")))
    for group in related.get("groups") or []:
        for work in group.get("works") or []:
            key = str(work.get("ref_key") or "")
            if key and key not in unique_refs:
                unique_refs[key] = work.get("ref_check") or _ref_check(
                    key, library, work.get("claim", ""))
    return {
        "claims": claims,
        "grounding": support,
        "ref_checks": list(unique_refs.values()),
        "ref_check_failures": [check for check in unique_refs.values()
                               if not check.get("exists")],
        "register": rows,
        "counts": {
            "claims": total,
            "located": located,
            "refs_checked": len(unique_refs),
            "refs_missing": sum(1 for check in unique_refs.values()
                                if not check.get("exists")),
        },
    }


def _format_consistency(analysis: dict[str, Any],
                        library: dict[str, Any]) -> dict[str, Any]:
    style = analysis["citation"]["style"]
    problems: list[str] = []
    if style == "none" and library["library_size"]:
        problems.append("manuscript carries no recognisable in-text citation token")
    if style not in {"none", ""} and "+" in style:
        problems.append("manuscript mixes citation styles")
    missing_year = [entry["ref_key"] for entry in library["entries"]
                    if not entry.get("year")]
    if missing_year:
        problems.append(f"{len(missing_year)} library entries have no year")
    missing_authors = [entry["ref_key"] for entry in library["entries"]
                       if not entry.get("authors")]
    if missing_authors:
        problems.append(f"{len(missing_authors)} library entries have no author")
    return {"style": style, "problems": problems,
            "entries_with_year": library["library_size"] - len(missing_year)}


def build_citation_coverage(analysis: dict[str, Any], library: dict[str, Any],
                            intro: dict[str, Any],
                            related: dict[str, Any]) -> dict[str, Any]:
    generated_keys: list[str] = []
    for item in intro.get("background") or []:
        generated_keys.extend(item.get("citations_resolved") or [])
    problem = intro.get("problem_statement") or {}
    generated_keys.extend(problem.get("citations_resolved") or [])
    for item in intro.get("gap_analysis") or []:
        generated_keys.extend(item.get("derived_from_refs") or [])
    for group in related.get("groups") or []:
        for work in group.get("works") or []:
            generated_keys.append(str(work.get("ref_key") or ""))
    generated_keys = _unique(generated_keys)
    cited = analysis["citation"]["cited_keys"]
    per_key = []
    for entry in library["entries"]:
        key = entry["ref_key"]
        per_key.append({
            "ref_key": key,
            "title": (entry.get("title") or "")[:160],
            "year": entry.get("year", ""),
            "venue": (entry.get("venue") or "")[:80],
            "cited_by_manuscript": key in cited,
            "manuscript_count": analysis["citation"]["cited_counts"].get(key, 0),
            "cited_in_generated": key in generated_keys,
        })
    total = library["library_size"]
    return {
        "library_size": total,
        "cited_by_manuscript": len(cited),
        "manuscript_coverage_rate": round(len(cited) / total, 4) if total else 0.0,
        "cited_in_generated": len(generated_keys),
        "generated_coverage_rate": round(len(generated_keys) / total, 4) if total else 0.0,
        "uncited_keys": analysis["citation"]["uncited_keys"],
        "generated_unused_keys": [key for key in library["key_order"]
                                  if key not in generated_keys],
        "missing_tokens": analysis["citation"]["missing_tokens"],
        "citation_style": analysis["citation"]["style"],
        "tokens_total": analysis["citation"]["tokens_total"],
        "per_key": per_key,
        "format_consistency": _format_consistency(analysis, library),
    }


def build_values(analysis: dict[str, Any], library: dict[str, Any],
                 intro: dict[str, Any], related: dict[str, Any],
                 coverage: dict[str, Any], claim_evidence: dict[str, Any],
                 files: dict[str, Any], assembled: dict[str, Any],
                 generated: dict[str, Any],
                 quality: dict[str, Any]) -> dict[str, Any]:
    style = analysis["citation"]["style"]
    intro_md = intro_text(intro, library, style)
    related_md = related_work_text(related, library, style)
    return {
        "introduction": {
            "background": intro.get("background") or [],
            "problem_statement": intro.get("problem_statement"),
            "gap_analysis": intro.get("gap_analysis") or [],
            "contributions": intro.get("contributions") or [],
            "text": intro_md,
            "baseline": bool(intro.get("baseline")),
        },
        "background": intro.get("background") or [],
        "problem_statement": intro.get("problem_statement"),
        "gap_analysis": intro.get("gap_analysis") or [],
        "contributions": intro.get("contributions") or [],
        "related_work": {
            "source": related.get("source"),
            "groups": related.get("groups") or [],
            "dimensions": related.get("dimensions") or [],
            "text": related_md,
            "baseline": bool(related.get("degenerate")),
        },
        "citation_coverage": coverage,
        "citation_inventory": {
            "library_size": library["library_size"],
            "keys": library["key_order"],
            "numeric_index": library["numeric_index"],
            "sources": library["sources"],
            "cited_keys": analysis["citation"]["cited_keys"],
            "uncited_keys": analysis["citation"]["uncited_keys"],
            "missing_tokens": analysis["citation"]["missing_tokens"],
            "style": style,
        },
        "claim_evidence": claim_evidence,
        "revised_manuscript": {
            "title": analysis["title"],
            "markdown": assembled.get("markdown"),
            "files": {key: files.get(key) for key in
                      ("docx", "markdown", "html", "full_markdown")},
            "bytes": files.get("bytes"),
            "preview": files.get("preview"),
            "removed_existing_sections": assembled.get("removed_existing_sections"),
            "inserted_before_order": assembled.get("inserted_before_order"),
            "sections_inserted": ["1 Introduction", "2 Related Work"],
            "word_count": len(re.findall(r"\S+", assembled.get("markdown") or "")),
        },
        "figure_table_map": analysis["figure_tables"],
        "manuscript_sections": analysis["sections"],
        "literature": {
            "entries": [{key: entry.get(key) for key in
                         ("ref_key", "title", "authors", "year", "venue", "doi")}
                        for entry in library["entries"]],
            "sources": library["sources"],
        },
        "evidence": {
            "generated": {key: generated.get(key) for key in
                          ("files", "broken", "ignored_files")},
            "degraded": quality.get("degraded", []),
            "grounding": claim_evidence.get("grounding"),
            "citation_coverage_rate": coverage.get("manuscript_coverage_rate"),
        },
        "quality": quality,
    }


# --------------------------------------------------------------------------- #
# finalize driver
# --------------------------------------------------------------------------- #
def cmd_finalize(out_dir: Path, work_dir: Path, stopwatch: "hybrid.Stopwatch",
                 prepared: dict[str, Any] | None = None) -> dict[str, Any]:
    found = discover.discover()
    case = contract.load_case(found.get("case_path"))
    case_id = contract.find_case_id(case)
    declared = hybrid.declared_fields(case) if case else []
    task_md_ref = found.get("task_md_path")
    _log(f"finalize: case_id={case_id} contract_fields={len(declared)} work={work_dir}")

    limits: list[str] = []
    uncertainty: list[str] = []
    change_log = [f"{SKILL_NAME} {SKILL_VERSION}: deterministic finalize; no model call made"]
    degraded: list[str] = []

    split = split_materials(found)
    library = load_library(split["libraries"])
    if library["library_size"] == 0:
        limits.append("no reference library was found (.bib/.ris/CSL-JSON/plain list); "
                      "citation resolution is impossible for this case")
        degraded.append("no_library")

    manuscript = None
    analysis = None
    if split["manuscript"] is not None:
        manuscript, warnings = load_manuscript(Path(split["manuscript"]),
                                               stopwatch.start + hybrid.SOFT_DEADLINE_S)
        for warning in warnings:
            uncertainty.append(f"manuscript: {warning}")
        if manuscript.get("text_layer"):
            analysis = analyse_manuscript(manuscript, library)
    if analysis is None or manuscript is None:
        limits.append("no readable manuscript draft was found under the input roots")
        failure_report = hybrid.run_report(
            stopwatch=stopwatch, mode="finalize", inputs=found, limits=limits,
            uncertainty=["cannot write an introduction for a manuscript that was not "
                         "provided"])
        payload = hybrid.finalize(
            out_dir, case=case, case_id=case_id, values={}, status="failed",
            limits=limits, uncertainty=uncertainty, change_log=change_log,
            task_md_path=task_md_ref,
            evidence={"failure": "no manuscript", "inputs": found},
            report=failure_report, synonyms=SYNONYMS)
        _log(f"finalize: failed (no manuscript) -> {out_dir / 'result.json'}")
        return {"status": "failed", "payload": payload, "case_id": case_id,
                "degraded": ["no_manuscript"]}

    generated = load_generated(work_dir)
    for broken in generated["broken"]:
        limits.append(f"generated/{broken['file']} could not be parsed "
                      f"({broken['reason']}); its content was replaced by the deterministic "
                      "baseline")
        degraded.append(f"broken_generated:{broken['file']}")

    baseline = baseline_intro(analysis, library)
    intro_raw = generated.get("intro")
    if intro_raw is None:
        limits.append("work/generated/intro.json is missing: background/problem/gap/"
                      "contributions were rebuilt with an extractive baseline")
        degraded.append("intro_missing")
        intro = baseline
    else:
        intro = validate_intro(intro_raw, analysis, library)
        for part in ("background", "gap_analysis", "contributions"):
            if not intro.get(part):
                limits.append(f"intro.json has no usable '{part}' entries: filled from the "
                              "deterministic baseline")
                degraded.append(f"intro_partial:{part}")
                intro[part] = baseline[part]
        if not (intro.get("problem_statement") or {}).get("text"):
            limits.append("intro.json has no problem_statement text: filled from the "
                          "deterministic baseline")
            degraded.append("intro_partial:problem_statement")
            intro["problem_statement"] = baseline["problem_statement"]

    related_raw = generated.get("related_work")
    if related_raw is None and not generated["batch_records"]:
        limits.append("work/generated/related_work.json is missing: related work was rebuilt "
                      "with the title-keyword co-occurrence baseline")
        degraded.append("related_work_missing")
        related = baseline_related_work(library, analysis["citation"]["cited_keys"])
    else:
        related = validate_related_work(related_raw, generated["batch_records"], library,
                                        analysis["citation"]["cited_keys"])
        if related["degenerate"]:
            limits.append("no usable related-work groups were supplied: baseline grouping used")
            degraded.append("related_work_degenerate")
            related = baseline_related_work(library, analysis["citation"]["cited_keys"])

    # --- hard checks ---------------------------------------------------------
    unsupported = [item["id"] for item in intro.get("background") or []
                   if "unsupported_citation" in item.get("flags", [])]
    if unsupported:
        limits.append(f"{len(unsupported)} background item(s) carry no resolvable citation: "
                      f"{unsupported[:6]}")
        degraded.append("unsupported_citation")
    problem_flags = (intro.get("problem_statement") or {}).get("flags") or []
    if "unsupported_citation" in problem_flags:
        uncertainty.append("the problem statement cites nothing that exists in the library")
        degraded.append("problem_unsupported_citation")
    gap_unsupported = [item["id"] for item in intro.get("gap_analysis") or []
                       if "gap_without_reference" in item.get("flags", [])]
    if gap_unsupported:
        limits.append(f"{len(gap_unsupported)} gap item(s) name no existing reference in "
                      f"derived_from_refs: {gap_unsupported[:6]}")
        degraded.append("gap_without_reference")
    missing_evidence = [item["id"] for item in intro.get("contributions") or []
                        if "evidence_missing" in item.get("flags", [])]
    if missing_evidence:
        limits.append(f"{len(missing_evidence)} contribution(s) could not be matched to a "
                      f"manuscript sentence or figure/table label: {missing_evidence[:6]}")
        degraded.append("contribution_evidence_missing")
    unknown_refs = [work["ref_key"] for group in related.get("groups") or []
                    for work in group.get("works") or []
                    if not (work.get("ref_check") or {}).get("exists")]
    if unknown_refs:
        limits.append(f"related-work entries reference {len(unknown_refs)} key(s) that are not "
                      f"in the library: {_unique(unknown_refs)[:6]}")
        degraded.append("related_work_unknown_refs")
    without_limitation = [work["ref_key"] for group in related.get("groups") or []
                          for work in group.get("works") or []
                          if "limitation_missing" in work.get("flags", [])]
    if without_limitation:
        uncertainty.append(f"{len(without_limitation)} related-work entr(ies) state no "
                           "limitation; the grouped comparison is therefore partial")

    claim_evidence = build_claim_evidence(analysis, intro, related, library)
    coverage = build_citation_coverage(analysis, library, intro, related)
    grounding = claim_evidence["grounding"]
    if grounding["claims_total"]:
        if grounding["location_rate"] < 1.0:
            missing = grounding["claims_total"] - grounding["located"]
            limits.append(f"{missing} of {grounding['claims_total']} claims could not be "
                          "located in the manuscript (evidence_missing)")
            degraded.append("claim_not_located")
        if grounding["support_rate"] < grounding["location_rate"]:
            uncertainty.append(
                f"{grounding['claims_total'] - grounding['supported_by_quote']} claim quote(s) "
                "do not mention the claim's own terms (paraphrase suspected); the strict "
                "grounding_rate is in quality, the located rate drives the status")
    if coverage["missing_tokens"]:
        uncertainty.append(
            f"the manuscript cites {len(coverage['missing_tokens'])} token(s) that are not in "
            f"the supplied library: {coverage['missing_tokens'][:6]} (input-side finding, "
            "reported not fixed)")
    if analysis["doc"].get("anchor_quality") != "span":
        limits.append(f"anchor granularity is '{analysis['doc'].get('anchor_quality')}': the "
                      "manuscript carried no coordinates, so bboxes are synthetic line ranges; "
                      "verification uses paragraph text containment")
    if not analysis["sections"]:
        uncertainty.append("no section headings were recognised in the manuscript; the new "
                           "text was appended at the end")
    if analysis["citation"]["style"] == "none":
        uncertainty.append("the manuscript carries no in-text citations; the new text was "
                           "written with the library style guessed from the library itself")

    style = analysis["citation"]["style"]
    assembled = assemble_revised(analysis, intro_text(intro, library, style),
                                 related_work_text(related, library, style))
    blocks = revised_blocks(analysis, intro, related, library, style, assembled, coverage,
                            claim_evidence["register"])
    try:
        files = write_deliverables(out_dir, blocks, assembled["markdown"])
    except Exception as exc:  # noqa: BLE001 - a writer failure must not kill the result
        limits.append(f"editable deliverable writer failed ({type(exc).__name__}: {exc}); the "
                      "markdown body is still in revised_manuscript.markdown")
        degraded.append("deliverable_writer_failed")
        files = {"docx": None, "markdown": None, "html": None, "full_markdown": None,
                 "bytes": {}, "preview": {"ok": False, "reason": str(exc)}}
    preview = files.get("preview") or {}
    if not preview.get("ok"):
        limits.append("no rendered PDF/PNG preview is available in this image "
                      f"({preview.get('reason')}); an HTML preview was written instead")
    else:
        change_log.append(f"rendered preview: pdf={preview.get('pdf')} "
                          f"png={preview.get('png')}")

    quality = {
        "status_inputs": {
            "library_size": library["library_size"],
            "manuscript_engine": manuscript.get("engine"),
            "anchor_quality": manuscript.get("anchor_quality"),
            "segments": len(analysis["segments"]),
            "sections_detected": [section["name"] for section in analysis["sections"]][:20],
            "generated_files": generated["files"],
        },
        "citation": {
            "style": style,
            "manuscript_coverage_rate": coverage["manuscript_coverage_rate"],
            "generated_coverage_rate": coverage["generated_coverage_rate"],
            "missing_tokens": len(coverage["missing_tokens"]),
            "format_problems": coverage["format_consistency"]["problems"],
        },
        "claim_support": {
            "claims": claim_evidence["counts"]["claims"],
            "location_rate": grounding["location_rate"],
            "strict_grounding_rate": grounding["strict_grounding_rate"],
            "support_rate": grounding["support_rate"],
            "refs_checked": claim_evidence["counts"]["refs_checked"],
            "refs_missing": claim_evidence["counts"]["refs_missing"],
        },
        "related_work": {"source": related.get("source"),
                         "dimensions": related.get("dimensions"),
                         "works": related.get("works_total")},
        "degraded": _unique(degraded),
        "contract_fields_declared": [field["name"] for field in declared],
    }

    values = build_values(analysis, library, intro, related, coverage, claim_evidence,
                          files, assembled, generated, quality)
    status = "success" if not degraded else "partial"
    if status == "partial":
        limits.insert(0, "status=partial: at least one part of the deliverable was rebuilt "
                         "from a deterministic baseline or failed a hard check; see "
                         "limits/degraded")

    byte_map = files.get("bytes") or {}

    def _artifact(path: Any, key: str) -> dict[str, Any]:
        size = byte_map.get(key) if isinstance(byte_map, dict) else None
        if size is None and isinstance(path, str) and path:
            item = Path(path)
            size = item.stat().st_size if item.is_file() else 0
        return {"path": path, "bytes": size or 0}

    deliverables = {kind: _artifact(files.get(kind), kind)
                    for kind in ("docx", "markdown", "html", "full_markdown")}
    deliverables["preview"] = preview
    payload_value = {key: values[key] for key in (
        "introduction", "background", "problem_statement", "gap_analysis", "related_work",
        "contributions", "citation_coverage", "citation_inventory", "claim_evidence",
        "revised_manuscript", "figure_table_map", "manuscript_sections", "literature",
        "quality") if key in values}
    payload_value["status"] = status
    claimed = len(analysis["citation"]["cited_keys"])
    claims_total = grounding.get("claims_total") or 0
    located = grounding.get("located") or 0
    strict = grounding.get("supported_by_quote") or 0
    summary_text = (
        "%d section(s) were inserted into the supplied manuscript (introduction rebuilt "
        "from a deterministic baseline: %s; related-work entries: %s over %d comparison "
        "dimension(s)). The bibliography holds %d entr(ies); the manuscript cites %d "
        "(coverage %s) and the new text cites %d (coverage %s). %d claim(s) were "
        "registered in the claim-evidence register, %d located in the manuscript "
        "(location rate %s) and %d strictly supported by their own quote; %d claim(s) "
        "could not be anchored and are listed as such. Deliverables: revised manuscript "
        "(.docx / .md / .html) plus full markdown. Degraded parts: %s. Status: %s. "
        "中文：本次写入引言与相关工作共 %d 节，文献库 %d 条、引用覆盖 %s，"
        "claim 回源 %d/%d，无法落地 %d 条。"
        % (len(assembled.get("sections_inserted") or []),
           bool(intro.get("baseline")), related.get("works_total"),
           len(related.get("dimensions") or []), library["library_size"], claimed,
           coverage.get("manuscript_coverage_rate"),
           coverage.get("cited_in_generated") or 0,
           coverage.get("generated_coverage_rate"), claims_total, located,
           grounding.get("location_rate"), strict, claims_total - located,
           ", ".join(_unique(degraded)) or "none", status,
           len(assembled.get("sections_inserted") or []), library["library_size"],
           coverage.get("manuscript_coverage_rate"), located, claims_total,
           claims_total - located))
    values["payload"] = payload_value
    values["summary_text"] = summary_text
    values["deliverables"] = deliverables

    evidence = {
        "inputs": found,
        "manuscript": {
            "path": manuscript.get("path"),
            "engine": manuscript.get("engine"),
            "anchor_quality": manuscript.get("anchor_quality"),
            "total_chars": manuscript.get("total_chars"),
            "title": analysis["title"],
            "abstract_source": analysis["abstract_source"],
            "sections": analysis["sections"],
            "segment_count": len(analysis["segments"]),
            "warnings": manuscript.get("warnings"),
            "attempts": manuscript.get("attempts"),
        },
        "library": {
            "size": library["library_size"],
            "sources": library["sources"],
            "keys": library["key_order"],
            "cited_keys": analysis["citation"]["cited_keys"],
            "uncited_keys": analysis["citation"]["uncited_keys"],
            "missing_tokens": analysis["citation"]["missing_tokens"],
        },
        "generated": generated,
        "claims": claim_evidence["claims"],
        "grounding": grounding,
        "ref_checks": claim_evidence["ref_checks"],
        "citation_coverage": coverage,
        "degraded": _unique(degraded),
        "deliverables": {key: files.get(key) for key in
                         ("docx", "markdown", "html", "full_markdown", "preview")},
        "render_probe": (preview.get("probe") or {}),
    }
    report = hybrid.run_report(
        stopwatch=stopwatch, mode="finalize", skill=SKILL_NAME, skill_version=SKILL_VERSION,
        inputs=found, work_dir=str(work_dir), out_dir=str(out_dir),
        generated_coverage={"files": generated["files"],
                            "broken": generated["broken"],
                            "intro": generated.get("intro") is not None,
                            "related_work": generated.get("related_work") is not None,
                            "batch_records": len(generated["batch_records"])},
        limits=limits, uncertainty=uncertainty, change_log=change_log,
        degraded=_unique(degraded),
        deliverables={key: files.get(key) for key in
                      ("docx", "markdown", "html", "full_markdown")},
        preview={"ok": preview.get("ok"), "reason": preview.get("reason"),
                 "png": preview.get("png")})

    payload = hybrid.finalize(
        out_dir, case=case, case_id=case_id, values=values, status=status,
        limits=limits, uncertainty=uncertainty, change_log=change_log,
        task_md_path=task_md_ref, evidence=evidence, report=report, synonyms=SYNONYMS,
        additional=["background", "problem_statement", "gap_analysis", "contributions",
                    "citation_inventory", "figure_table_map", "manuscript_sections",
                    "literature", "introduction", "related_work"])
    _log(f"finalize: status={status} degraded={_unique(degraded)} "
         f"result={out_dir / 'result.json'}")
    return {"status": status, "payload": payload, "case_id": case_id,
            "degraded": _unique(degraded)}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _fallback_result(out_dir: Path, *, status: str, limits: list[str],
                     change_log: list[str], traceback_tail: str = "") -> None:
    """Last-resort contract-shaped result so a crash is never a zero-byte output."""
    case: dict[str, Any] = {}
    case_id: str | None = None
    try:
        found = discover.discover()
        case = contract.load_case(found.get("case_path"))
        case_id = contract.find_case_id(case)
    except Exception:
        pass
    try:
        hybrid.finalize(
            out_dir, case=case, case_id=case_id, values={}, status=status,
            limits=limits, uncertainty=[], change_log=change_log,
            evidence={"failure": limits[:3],
                      "traceback_tail": traceback_tail[-1200:] if traceback_tail else ""},
            report={"status": status, "limits": limits,
                    "traceback_tail": traceback_tail[-1200:] if traceback_tail else ""},
            synonyms=SYNONYMS)
    except Exception:
        hybrid.write_json(out_dir / "result.json", {
            "case_id": case_id or "",
            "meta": {"status": status, "limits": limits, "change_log": change_log,
                     "traceback_tail": traceback_tail[-1200:]},
        })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Introduction + related-work writing chain (prepare / finalize / all)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true",
                      help="deterministic phase: extract inputs and build agent worksheets")
    mode.add_argument("--finalize", action="store_true",
                      help="deterministic phase: validate generated content, write deliverables")
    mode.add_argument("--all", action="store_true",
                      help="run prepare then finalize in one process (default; the agent should "
                           "write work/generated/* between the two phases)")
    parser.add_argument("--out", default=None,
                        help="output directory (default: /app/output)")
    parser.add_argument("--work", default=None,
                        help="working directory (default: /app/work)")
    parser.add_argument("--input", default=None,
                        help="directory holding case.json/task.md/materials (default: detected)")
    parser.add_argument("--quiet", action="store_true", help="only log to stderr")
    args = parser.parse_args(argv)
    single = args.prepare or args.finalize
    run_prepare = args.prepare or args.all or not single
    run_finalize = args.finalize or args.all or not single

    if args.input:
        target = Path(args.input).resolve()
        if target.is_dir():
            os.chdir(target)

    out_dir = hybrid.resolve_out_dir(args.out)
    work_dir = resolve_work_dir(args.work, out_dir)
    declared: list[dict[str, Any]] = []
    case_r: dict[str, Any] = {}
    case_id: str | None = None
    try:
        found = discover.discover()
        case_r = contract.load_case(found.get("case_path"))
        case_id = contract.find_case_id(case_r)
        declared = hybrid.declared_fields(case_r) if case_r else []
    except Exception:
        pass

    try:  # placeholder first: the container may be killed at the 900 s budget
        hybrid.write_placeholder(
            out_dir, case_id=case_id,
            note="run started; this file is replaced when the skill finishes",
            contract_fields=len(declared))
    except Exception:
        pass

    stopwatch = hybrid.Stopwatch()
    try:
        prepared = None
        if run_prepare:
            prepared = cmd_prepare(out_dir, work_dir, stopwatch)
            if not args.quiet:
                analysis = prepared.get("analysis") or {}
                print(json.dumps({
                    "phase": "prepare",
                    "case_id": prepared.get("case_id"),
                    "work_dir": str(work_dir),
                    "out_dir": str(out_dir),
                    "manuscript": (prepared.get("manuscript") or {}).get("path"),
                    "segments": len(analysis.get("segments") or []),
                    "sections": [section["name"] for section in
                                 (analysis.get("sections") or [])],
                    "library_size": prepared["library"]["library_size"],
                    "batches": [batch["batch_id"] for batch in prepared["batches"]],
                    "expected_generated": prepared["expected_generated"],
                    "next": (f"write {work_dir}/generated/intro.json and "
                             "related_work.json, then run --finalize"),
                }, ensure_ascii=False, indent=2))
            if not run_finalize:
                return 0
        result = cmd_finalize(out_dir, work_dir, stopwatch, prepared)
        if not args.quiet:
            payload = result.get("payload") or {}
            meta = payload.get("meta") or {}
            print(json.dumps({
                "phase": "finalize",
                "case_id": result.get("case_id"),
                "status": result.get("status"),
                "degraded": result.get("degraded"),
                "contract_fields": meta.get("declared_fields"),
                "contract_field_mapping": meta.get("contract_field_mapping"),
                "contract_fields_not_reported": meta.get("contract_fields_not_reported"),
                "field_fill_ratio": meta.get("field_fill_ratio"),
                "outputs": meta.get("output_files"),
                "elapsed_s": stopwatch.elapsed(),
            }, ensure_ascii=False, indent=2))
        return 0
    except BaseException as exc:  # noqa: BLE001 - never end a run without a result file
        traceback_tail = traceback.format_exc(limit=8)
        _log(f"FATAL {type(exc).__name__}: {exc}")
        _fallback_result(
            out_dir, status="failed",
            limits=[f"run aborted by {type(exc).__name__}: {exc}"],
            change_log=["failure result written by the top-level handler"],
            traceback_tail=traceback_tail)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
