#!/usr/bin/env python3
"""Reading-03 | Literature translation: glossary-locked, paragraph-aligned, self-checked.

Two-phase hybrid pipeline (shared kernel: lib/hybrid.py):

    prepare    deterministic: discover inputs -> extract the paper -> cut it into
               addressable paragraphs -> extract machine-checkable facts (numbers,
               units, citations, formulas, hedges, negations, modals, labels,
               candidate terms) -> write work/batches/batch-XX.json worksheets and
               work/glossary_worksheet.json for the agent. No model call.
    judgement  the agent writes work/generated/_glossary.json once, then one
               work/generated/batch-XX.json per worksheet (one model request each).
    finalize   deterministic: validate coverage and preservation, enforce one
               translation per term, bind evidence anchors back to the source,
               reflect case.json.output_contract, write result.json /
               evidence.json / run_report.json and a bilingual markdown.

Hard rules implemented here, not merely documented:

* result.json is written before any slow work starts and rewritten at the end, so
  a container killed at the 900 s budget still leaves an honest partial file;
* finalize never raises for missing generated content: paragraphs without an
  answer degrade to tgt="" + status="untranslated" and are listed, so "produced
  but incomplete" still beats "crashed";
* no schema is hardcoded: case.json.output_contract decides the field names
  (hybrid.map_fields + hybrid.finalize);
* every pair carries page + block_id + bbox + quote, and the quote is verified
  against the extracted document before it is emitted.

Standard library only, no network, no model API, deterministic output.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
try:  # packaged layout: <skill root>/lib exists
    sys.path.insert(0, str(ROOT))
    from lib import anchors, contract, discover, extract, hybrid, structure  # noqa: E402
except ImportError:  # development layout: skills/_core sits next to the challenge dir
    sys.path.insert(0, str(ROOT.parent / "_core"))
    from lib import anchors, contract, discover, extract, hybrid, structure  # noqa: E402

SKILL_VERSION = "1.0.0"
BATCH_SEGMENTS = 6          # paragraphs per model request (fits 16384 tokens)
BATCH_CHARS = 5200
MAX_SEGMENT_CHARS = 1200    # longer blocks are split on sentence boundaries
GLOSSARY_TOP = 80           # candidate terms offered to the agent
MAX_MATERIAL_DOCS = 3
MAX_FACTS_PER_KIND = 12     # per-segment fact hints shown in a worksheet

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
GLOSSARY_NAME_RE = re.compile(r"gloss|term|lexic|vocab|术语|词表|词典", re.I)
GLOSSARY_SPLIT_RE = re.compile(r"\t+|\s*\|\s*|\s*[,，;；]\s*|\s*→\s*|\s+[-—–]\s+|\s*[:：]\s+")

# Cross-lingual markers for the "relationship preserved" check. A hedge /
# negation / modal in the source must leave SOME trace in the Chinese target:
# either the Chinese marker, or the original English token when the translator
# deliberately kept it (abbreviations, quoted claims). Only when neither is
# present do we report a lost relation.
ZH_HEDGE_MARKERS = ("可能", "也许", "或许", "大概", "大约", "约", "左右", "估计", "推测",
                    "初步", "倾向", "往往", "通常", "一般", "多数", "部分", "似乎",
                    "表明", "提示", "有望", "潜在", "在一定程度上", "主要")
ZH_NEGATION_MARKERS = ("不", "未", "无", "没有", "并非", "未能", "不能", "不得", "缺乏", "排除")
ZH_MODAL_MARKERS = ("必须", "应当", "需要", "须", "不得", "将", "会", "应")

# Per-adapter synonyms handed to hybrid.map_fields, on top of the shared alias
# table. A hidden case names its fields freely; these cover the plausible
# vocabulary of a translation task.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "bilingual": ("bilingual_pairs", "bilingual_paragraphs", "paragraph_pairs",
                  "aligned_pairs", "translation_pairs", "parallel_text",
                  "zh_en_paragraph_pairs", "bilingual_alignment", "双语对照",
                  "对照段落", "段落对齐"),
    "glossary": ("term_glossary", "terminology", "terminology_bank", "term_list",
                 "locked_terms", "术语表", "术语库"),
    "coverage": ("translation_coverage", "paragraph_completeness", "completeness",
                 "segment_coverage", "coverage_report", "覆盖率"),
    "untranslated": ("untranslated_segments", "omission_list", "omissions",
                     "missing_translations", "untranslated_items", "未翻译段落",
                     "漏译清单"),
    "term_consistency": ("term_loyalty_report", "terminology_consistency",
                         "term_consistency_report", "consistency", "术语一致性"),
    "translation_text": ("translation", "translated_body", "translated_text",
                         "full_translation", "translated_full_text", "chinese_text",
                         "译文", "译文全文"),
    "source_text": ("original_text", "source_full_text", "source_transcript",
                    "原文", "原文全文"),
    "segments": ("source_passages", "segment_index", "paragraph_index",
                 "source_segments", "原文段落"),
    "evidence": ("evidence_anchors", "anchors", "grounding", "provenance",
                 "anchor_report", "证据锚点"),
    "quality": ("quality_metrics", "grade_metrics", "validation", "metrics",
                "translation_quality", "质量指标"),
    "review_checklist": ("needs_review", "manual_review", "review_list",
                         "human_review", "待人工确认", "人工确认清单"),
    "checks": ("translation_checks", "preservation_checks", "verification",
               "译文核验", "核验结果"),
    "limitations": ("limits", "boundaries", "caveats", "限制"),
    "uncertainty": ("uncertainties", "unknowns", "不确定性"),
    "status": ("run_status", "outcome", "状态"),
    "language_pair": ("languages", "language", "translation_direction", "语言对"),
    # The two values every hidden contract asks for under some name. Kept here as
    # well as in lib/hybrid.py's generic table so the adapter still works when the
    # packaged skill is run against an older copy of the shared kernel.
    "payload": ("result_payload", "main_result", "structured_result", "result",
                "output", "outputs", "deliverable"),
    "summary_text": ("cover_letter", "honest_summary", "run_summary",
                     "narrative_summary", "executive_summary"),
    "deliverables": ("output_files", "artifacts", "files", "交付文件"),
}

# Compact values additionally kept under meta.additional_outputs when the contract
# does not name them (the big arrays are only kept when the contract asks).
ADDITIONAL_KEYS = ("glossary", "coverage", "untranslated", "term_consistency",
                   "quality", "checks", "review_checklist", "limitations",
                   "uncertainty", "status", "language_pair")


# --------------------------------------------------------------------------- #
# small helpers
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


def resolve_work_dir(prefer: str | None, out_dir: str | Path,
                     *, existing_only: bool = False) -> Path:
    """Find the work directory the two phases share.

    The container promises /app/work; locally the run may only be able to write
    under the output dir. Both phases call this with the same rules, so they agree
    without an extra argument.
    """
    out = Path(out_dir)
    candidates: list[Path] = []
    if prefer:
        candidates.append(Path(prefer))
    candidates += [Path("/app/work"), out / "work", Path.cwd() / "work"]
    for candidate in candidates:
        if candidate.is_dir() and (
                (candidate / "batches").is_dir()
                or (candidate / "generated").is_dir()
                or (candidate / "segments.json").is_file()):
            return candidate
    if existing_only:
        return Path(prefer) if prefer else out / "work"
    for candidate in candidates:
        if _writable(candidate):
            return candidate
    return out / "work"


def _cjk_chars(text: str) -> int:
    return len(CJK_RE.findall(text or ""))


def _is_blank(value: Any) -> bool:
    return not str(value if value is not None else "").strip()


def _contains(haystack: str, needle: str) -> bool:
    """Containment that is case-insensitive for ASCII, exact for CJK."""
    if not needle:
        return False
    if re.fullmatch(r"[\x00-\x7f]+", needle):
        return needle.casefold() in (haystack or "").casefold()
    return needle in (haystack or "")


def _term_pattern(term: str) -> re.Pattern[str]:
    if CJK_RE.search(term):
        return re.compile(re.escape(term))
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", re.I)


def _looks_chinese(text: str) -> bool:
    return _cjk_chars(text) > 0


def _source_is_chinese(text: str) -> bool:
    return _cjk_chars(text) >= 10 and len(LATIN_WORD_RE.findall(text or "")) <= 3


def _read_text(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# glossary resources
# --------------------------------------------------------------------------- #
def load_seed_glossary() -> tuple[list[dict[str, Any]], list[str]]:
    """Small, discipline-neutral seed glossary shipped in resources/.

    It only supplies *conventions* (section names, figure/table words, units that
    stay in Latin script, hedging verbs), never domain facts. A per-case term list
    always wins over the seed.
    """
    notes: list[str] = []
    path = ROOT / "resources" / "glossary_seed.json"
    raw = hybrid.read_json(path, default=None)
    if not isinstance(raw, dict) or not isinstance(raw.get("terms"), list):
        notes.append(f"seed glossary not found or malformed: {path}")
        return [], notes
    entries: list[dict[str, Any]] = []
    for item in raw["terms"]:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        if not term:
            continue
        translation = str(item.get("translation") or "").strip()
        keep = item.get("keep_original")
        if keep is None:
            keep = translation == term
        entries.append({"term": term, "translation": translation,
                        "keep_original": bool(keep),
                        "note": str(item.get("note") or ""), "source": "seed"})
    return entries, notes


def _is_glossary_like(path: Path) -> bool:
    return bool(GLOSSARY_NAME_RE.search(path.stem)) and path.suffix.lower() in {
        ".json", ".md", ".txt", ".csv", ".tsv", ".yaml", ".yml"}


def _parse_glossary_lines(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or raw.startswith("//"):
            continue
        if set(raw) <= set("-| "):
            continue
        parts = [p for p in GLOSSARY_SPLIT_RE.split(raw) if p and p.strip()]
        if len(parts) < 2:
            continue
        term, translation = parts[0].strip(), parts[1].strip()
        if not term or not translation or term.casefold() in {"term", "source", "原文", "术语"}:
            continue
        entries.append({"term": term, "translation": translation,
                        "keep_original": translation == term,
                        "note": "case-provided glossary", "source": "case_provided"})
    return entries


def parse_provided_glossary(material_docs: Iterable[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """A case may ship its own term list (a strong hint, not a hidden answer).

    Accepted shapes: JSON object/array of {term, translation}, or a two-column
    csv/tsv/markdown table. Anything unparsable is reported, not guessed.
    """
    entries: list[dict[str, Any]] = []
    notes: list[str] = []
    for raw_path in material_docs:
        path = Path(raw_path)
        if not _is_glossary_like(path):
            continue
        suffix = path.suffix.lower()
        if suffix == ".json":
            data = hybrid.read_json(path, default=None)
            rows: list[Any] = []
            if isinstance(data, list):
                rows = data
            elif isinstance(data, dict):
                for key in ("glossary", "terms", "entries", "terms_list"):
                    if isinstance(data.get(key), list):
                        rows = data[key]
                        break
                if not rows:
                    rows = [{"term": k, "translation": v} for k, v in data.items()
                            if isinstance(v, str)]
            for item in rows:
                if not isinstance(item, dict):
                    continue
                term = str(item.get("term") or item.get("source") or "").strip()
                translation = str(item.get("translation") or item.get("target") or "").strip()
                if not term:
                    continue
                keep = item.get("keep_original")
                if keep is None:
                    keep = translation == term
                entries.append({"term": term, "translation": translation,
                                "keep_original": bool(keep),
                                "note": "case-provided glossary", "source": "case_provided"})
        else:
            entries.extend(_parse_glossary_lines(_read_text(str(path))))
        if entries:
            notes.append(f"parsed a case-provided glossary from {path.name}")
    return entries, notes


GLOSSARY_SCAN_SKIP = {"output", "outputs", "result", "results", "work", "generated",
                      "node_modules", ".git", "__pycache__", "lib", "scripts", "tests"}


def glossary_candidates(found: dict[str, Any]) -> list[str]:
    """All files that might carry a case-provided term list.

    discover() deliberately excludes *.json from material documents (case.json lives
    in the same tree), so a case that ships glossary.json would be invisible. This
    bounded walk adds glossary-named JSON/CSV/MD files back, deterministically.
    """
    seen: list[str] = []
    for raw in found.get("material_docs") or []:
        if str(raw) not in seen:
            seen.append(str(raw))
    scanned = 0
    for root in found.get("input_roots") or []:
        base = Path(root)
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(name for name in dirnames if name not in GLOSSARY_SCAN_SKIP)
            for name in sorted(filenames):
                scanned += 1
                path = Path(dirpath) / name
                if (path.name.lower() not in {"case.json", "task.md", "instruction.md"}
                        and _is_glossary_like(path) and str(path) not in seen):
                    seen.append(str(path))
                if scanned > 3000:
                    return seen
    return seen


def _normalise_entry(raw: Any, source: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    term = str(raw.get("term") or raw.get("source") or raw.get("en") or "").strip()
    if not term:
        return None
    translation = str(raw.get("translation") or raw.get("target") or raw.get("zh")
                      or raw.get("chinese") or "").strip()
    keep = raw.get("keep_original")
    if keep is None:
        keep = bool(translation) and translation == term
    return {"term": term, "translation": translation,
            "keep_original": bool(keep),
            "note": str(raw.get("note") or ""), "source": source}


def load_agent_glossary(work: Path) -> tuple[list[dict[str, Any]], str]:
    path = work / "generated" / "_glossary.json"
    if not path.is_file():
        return [], "missing"
    raw = hybrid.read_json(path, default=None)
    rows: list[Any] = []
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        for key in ("glossary", "terms", "entries", "records", "items", "data"):
            if isinstance(raw.get(key), list):
                rows = raw[key]
                break
        else:
            rows = [raw]
    entries = [e for e in (_normalise_entry(item, "agent") for item in rows) if e]
    return entries, ("present" if entries else "empty")


def merge_glossary(provided: list[dict[str, Any]], agent: list[dict[str, Any]],
                   candidates: list[dict[str, Any]],
                   seed: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Locked glossary: case-provided first, then the agent's, then seed fallback.

    A conflict between the case-provided list and the agent's list keeps the
    case-provided translation (it is the task's own constraint) and records the
    conflict instead of silently losing one of them.
    """
    ordered: dict[str, dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    for entry in provided:
        ordered.setdefault(entry["term"].casefold(), dict(entry))
    for entry in agent:
        key = entry["term"].casefold()
        if key in ordered:
            existing = ordered[key]
            if existing["source"] == "case_provided" and (
                    existing["translation"] != entry["translation"]
                    or existing["keep_original"] != entry["keep_original"]):
                conflicts.append({"term": entry["term"],
                                  "case_provided": existing["translation"],
                                  "agent": entry["translation"]})
                continue
            ordered[key] = dict(entry)
        else:
            ordered[key] = dict(entry)
    seed_map = {e["term"].casefold(): e for e in seed}
    for candidate in candidates:
        key = str(candidate.get("term") or "").casefold()
        if not key or key in ordered:
            continue
        hit = seed_map.get(key)
        if not hit:
            continue
        ordered[key] = {"term": candidate["term"], "translation": hit["translation"],
                        "keep_original": hit["keep_original"],
                        "note": "seed fallback (not locked by the agent)",
                        "source": "auto_seed"}
    entries = list(ordered.values())
    status = {
        "total_entries": len(entries),
        "agent_entries": sum(1 for e in entries if e["source"] == "agent"),
        "provided_entries": sum(1 for e in entries if e["source"] == "case_provided"),
        "auto_entries": sum(1 for e in entries if e["source"] == "auto_seed"),
        "conflicts": conflicts,
        "source": ("agent" if any(e["source"] == "agent" for e in entries)
                   else "case_provided" if entries else "missing"),
    }
    return entries, status


def _glossary_ambiguity(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Two different source terms must not receive the same locked translation."""
    by_target: dict[str, list[str]] = {}
    for entry in entries:
        if entry["keep_original"] or not entry["translation"]:
            continue
        by_target.setdefault(entry["translation"], []).append(entry["term"])
    return [{"translation": target, "terms": terms}
            for target, terms in by_target.items() if len(terms) > 1]


# --------------------------------------------------------------------------- #
# segments, facts, worksheets
# --------------------------------------------------------------------------- #
def segment_facts(text: str) -> dict[str, Any]:
    """Machine-checkable facts of one paragraph: what a translation must keep."""
    return {
        "numbers": hybrid.find_numbers(text)[:MAX_FACTS_PER_KIND],
        "citations": hybrid.find_citations(text)[:MAX_FACTS_PER_KIND],
        "formulas": hybrid.find_formula_tokens(text)[:MAX_FACTS_PER_KIND],
        "hedges": hybrid.find_hedges(text)[:MAX_FACTS_PER_KIND],
        "negations": hybrid.find_negations(text)[:MAX_FACTS_PER_KIND],
        "modals": hybrid.find_modals(text)[:MAX_FACTS_PER_KIND],
        "labels": hybrid.find_labels(text)[:MAX_FACTS_PER_KIND],
        "sentence_count": len(hybrid.sentences(text)),
    }


def _terms_in_segment(text: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for entry in entries:
        if _term_pattern(entry["term"]).search(text or ""):
            hits.append({"term": entry["term"], "translation": entry["translation"],
                         "keep_original": entry["keep_original"]})
        if len(hits) >= 20:
            break
    return hits


def build_glossary_worksheet(segments: list[dict[str, Any]],
                             candidates: list[dict[str, Any]],
                             provided: list[dict[str, Any]],
                             seed: list[dict[str, Any]],
                             *, target_language: str = "zh") -> dict[str, Any]:
    seed_map = {e["term"].casefold(): e for e in seed}
    candidate_rows: list[dict[str, Any]] = []
    for candidate in candidates:
        term = candidate["term"]
        pattern = _term_pattern(term)
        pages = sorted({s["page"] for s in segments if pattern.search(s["text"])})
        example_seg = next((s for s in segments if pattern.search(s["text"])), None)
        seed_hit = seed_map.get(term.casefold())
        candidate_rows.append({
            "term": term,
            "count": candidate.get("count", 0),
            "pages": pages[:12],
            "first_seg_id": example_seg["seg_id"] if example_seg else None,
            "example": anchors.pick_quote(example_seg["text"], [term], 140) if example_seg else "",
            "suggested_translation": (seed_hit or {}).get("translation", ""),
            "keep_original_hint": bool(seed_hit and seed_hit["keep_original"])
            or (term.isupper() and len(term) >= 2 and term.isascii()),
            "note": "",
        })
    return {
        "instruction": ("Fill every row that matters for this paper with one stable "
                        + ("Chinese" if target_language == "zh" else "English")
                        + " rendering and set keep_original=true when the Latin-script form must "
                        "be kept (acronyms, gene/protein names, instrument models). The result is "
                        "written once to work/generated/_glossary.json and is then binding for "
                        "every paragraph."),
        "target_language": target_language,
        "provided_terms": [{"term": e["term"], "translation": e["translation"],
                            "keep_original": e["keep_original"]} for e in provided],
        "candidates": candidate_rows,
        "candidate_count": len(candidate_rows),
        "output_file": "generated/_glossary.json",
        "output_schema": [{"term": "source term as it appears",
                           "translation": "one fixed Chinese rendering",
                           "keep_original": False,
                           "note": "optional rationale"}],
    }


def _worksheet_instruction(batch_id: str, target_language: str = "zh") -> dict[str, Any]:
    target_word = "Chinese" if target_language == "zh" else "English"
    relation_rule = (
        "Keep negation and hedging: 'may/might/suggest/probably' -> 可能/或许/提示/推测; "
        "'not/cannot/never' -> 不/未能/不能; 'must/should' -> 必须/应当."
        if target_language == "zh" else
        "Keep negation and hedging: 可能/或许/提示 -> may/might/suggest; 不/未/不能 -> not/cannot; "
        "必须/应当 -> must/should.")
    return {
        "task": (f"Translate the paragraphs below into {target_word} for worksheet {batch_id}. "
                 "Return one record per seg_id."),
        "rules": [
            "Use the locked translation for every term listed in locked_terms; never invent a "
            "second rendering for the same term.",
            "Keep every number, unit, citation bracket, formula token and figure/table/equation "
            "number verbatim inside the Chinese sentence (e.g. 92%, 750 K, [3], k = A exp(-Ea/RT), 图 2).",
            relation_rule,
            "Do not merge or drop a seg_id; do not renumber; do not add paragraphs that are not "
            "in the worksheet.",
            "Write work/generated/<batch_id>.json as a JSON array, one object per seg_id, "
            "and nothing else.",
        ],
        "output_schema": [{"seg_id": "seg-0001",
                           "translation": "该段的目标语言译文（单字符串）",
                           "terms_used": ["可选：本段使用的术语"],
                           "note": "可选：译不准或需人工确认之处"}],
    }


def enrich_worksheets(work: Path, segments: list[dict[str, Any]],
                      glossary: list[dict[str, Any]], batch_ids: list[str],
                      *, target_language: str = "zh") -> list[dict[str, Any]]:
    """Rewrite each worksheet with per-segment facts and the locked glossary subset.

    hybrid.make_batches decides the split; this adds what the agent needs to obey
    the glossary and to see machine-checkable constraints, plus the neighbour
    context that prevents a paragraph from being translated out of context.
    """
    by_id = {s["seg_id"]: s for s in segments}
    worksheets: list[dict[str, Any]] = []
    batch_dir = work / "batches"
    generated_dir = work / "generated"
    ordered_files = sorted(batch_dir.glob("batch-*.json"))
    for index, path in enumerate(ordered_files):
        raw = hybrid.read_json(path, default={}) or {}
        seg_ids = [s.get("seg_id") for s in raw.get("segments") or []]
        rows: list[dict[str, Any]] = []
        locked_for_batch: dict[str, dict[str, Any]] = {}
        for seg_id in seg_ids:
            segment = by_id.get(seg_id)
            if not segment:
                continue
            terms = _terms_in_segment(segment["text"], glossary)
            for term in terms:
                locked_for_batch.setdefault(term["term"].casefold(), term)
            rows.append({
                "seg_id": segment["seg_id"],
                "order": segment.get("order"),
                "page": segment["page"],
                "section": segment.get("section_title"),
                "char_count": segment["char_count"],
                "text": segment["text"],
                "terms_in_segment": terms,
                "must_keep": segment_facts(segment["text"]),
            })
        prev_tail = ""
        next_head = ""
        if index > 0:
            prev_ids = [s.get("seg_id") for s in (hybrid.read_json(ordered_files[index - 1], default={}) or {}).get("segments") or []]
            prev_seg = by_id.get(prev_ids[-1]) if prev_ids else None
            if prev_seg:
                prev_tail = prev_seg["text"][-220:]
        if index + 1 < len(ordered_files):
            next_ids = [s.get("seg_id") for s in (hybrid.read_json(ordered_files[index + 1], default={}) or {}).get("segments") or []]
            next_seg = by_id.get(next_ids[0]) if next_ids else None
            if next_seg:
                next_head = next_seg["text"][:220]
        answer = generated_dir / path.name
        worksheet = {
            "batch_id": path.stem,
            "case_id": raw.get("case_id") or "",
            "segment_count": len(rows),
            "already_answered": answer.is_file(),
            "already_answered_hint": ("this batch already has an answer file; skip it and keep the "
                                      "existing work/generated/%s" % path.name) if answer.is_file() else "",
            "instruction": _worksheet_instruction(path.stem, target_language),
            "locked_terms": sorted(locked_for_batch.values(), key=lambda e: e["term"].casefold()),
            "context": {"previous_paragraph_tail": prev_tail, "next_paragraph_head": next_head},
            "segments": rows,
            "output_file": f"generated/{path.name}",
        }
        hybrid.write_json(path, worksheet)
        worksheets.append({"batch_id": path.stem, "segment_count": len(rows),
                           "worksheet": str(path), "already_answered": answer.is_file()})
    return worksheets


# --------------------------------------------------------------------------- #
# translation checks
# --------------------------------------------------------------------------- #
def _marker_hits(text: str, markers: Iterable[str]) -> list[str]:
    return [m for m in markers if m in (text or "")]


def translation_check(source: str, target: str, *, seg_id: str,
                       target_language: str = "zh") -> dict[str, Any]:
    """Deterministic 'did the translation keep the facts' check for one paragraph.

    Numbers/citations/formulas are compared as sets, so the verdict is
    reproducible. Hedges, negation and modals are checked across languages: the
    Chinese sentence must carry the Chinese marker (or deliberately keep the
    English token), otherwise the relation was dropped.
    """
    base = hybrid.preservation_check(source, target, label=seg_id)
    src_hedges = base.get("hedges_source") or []
    src_negations = base.get("negations_source") or []
    src_modals = hybrid.find_modals(source)
    if target_language == "zh":
        hedge_markers, negation_markers, modal_markers = (
            ZH_HEDGE_MARKERS, ZH_NEGATION_MARKERS, ZH_MODAL_MARKERS)
    else:  # ZH -> EN (detected): the same relation must appear in English markers
        hedge_markers = tuple(hybrid.HEDGES_EN)
        negation_markers = tuple(hybrid.NEGATIONS_EN)
        modal_markers = tuple(hybrid.MODALS_EN)
    hedge_zh = _marker_hits(target, hedge_markers)
    negation_zh = _marker_hits(target, negation_markers)
    modal_zh = _marker_hits(target, modal_markers)
    hedge_kept_en = [h for h in src_hedges if _contains(target, h)]
    negation_kept_en = [n for n in src_negations if _contains(target, n)]
    modal_kept_en = [m for m in src_modals if _contains(target, m)]

    issues: list[str] = []
    if base.get("numbers_missing"):
        issues.append("numbers_missing")
    if base.get("citations_missing"):
        issues.append("citations_missing")
    if base.get("formulas_missing"):
        issues.append("formulas_missing")
    if src_hedges and not (hedge_zh or hedge_kept_en):
        issues.append("hedge_not_transferred")
    if src_negations and not (negation_zh or negation_kept_en):
        issues.append("negation_not_transferred")
    if src_modals and not (modal_zh or modal_kept_en):
        issues.append("modal_not_transferred")
    if target_language == "zh":
        if not _looks_chinese(target):
            issues.append("no_chinese_characters")
    elif len(LATIN_WORD_RE.findall(target or "")) < 2:
        issues.append("no_latin_characters")

    return {
        "label": seg_id,
        "ok": not issues,
        "issues": issues,
        "numbers_total": base.get("numbers_total", 0),
        "numbers_missing": base.get("numbers_missing") or [],
        "citations_total": base.get("citations_total", 0),
        "citations_missing": base.get("citations_missing") or [],
        "formulas_total": base.get("formulas_total", 0),
        "formulas_missing": base.get("formulas_missing") or [],
        "hedges_source": src_hedges,
        "hedges_target_markers": hedge_zh,
        "hedges_missing": [] if (hedge_zh or hedge_kept_en) else list(src_hedges),
        "negations_source": src_negations,
        "negations_missing": [] if (negation_zh or negation_kept_en) else list(src_negations),
        "modals_source": src_modals,
        "modals_missing": [] if (modal_zh or modal_kept_en) else list(src_modals),
        "length_source": base.get("length_source", len(source)),
        "length_target": base.get("length_target", len(target)),
    }


def check_terms(glossary: list[dict[str, Any]], segments: list[dict[str, Any]],
                translations: dict[str, str]) -> dict[str, Any]:
    """Enforce one rendering per term across every paragraph it occurs in."""
    instances_total = 0
    instances_used = 0
    per_term: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for entry in glossary:
        term = entry["term"]
        translation = entry.get("translation") or ""
        keep = bool(entry.get("keep_original"))
        expected = term if keep else translation
        pattern = _term_pattern(term)
        segs_with = [s for s in segments if pattern.search(s["text"])]
        row = {
            "term": term,
            "translation": translation,
            "keep_original": keep,
            "source": entry.get("source"),
            "segments_with_term": len(segs_with),
            "segments_using_translation": 0,
            "rate": 0.0,
            "status": "no_occurrence",
            "offending_seg_ids": [],
        }
        if not expected:
            row["status"] = "no_translation"
            per_term.append(row)
            continue
        if not segs_with:
            per_term.append(row)
            continue
        used = [s for s in segs_with if _contains(translations.get(s["seg_id"], ""), expected)]
        offending = [s["seg_id"] for s in segs_with
                     if not _contains(translations.get(s["seg_id"], ""), expected)]
        row["segments_using_translation"] = len(used)
        row["rate"] = round(len(used) / len(segs_with), 4)
        row["offending_seg_ids"] = offending[:10]
        row["status"] = "consistent" if not offending else "inconsistent"
        if entry.get("source") != "auto_seed":
            instances_total += len(segs_with)
            instances_used += len(used)
        if offending:
            violations.append({"term": term, "translation": translation,
                               "offending_seg_ids": offending[:10], "rate": row["rate"]})
        per_term.append(row)
    judged = [r for r in per_term if r["status"] in {"consistent", "inconsistent"}]
    consistent = [r for r in judged if r["status"] == "consistent"]
    return {
        "available": bool(glossary),
        "terms_checked": len(judged),
        "consistent_terms": len(consistent),
        "inconsistent_terms": len(judged) - len(consistent),
        "term_rate": round(len(consistent) / len(judged), 4) if judged else 0.0,
        "instances_total": instances_total,
        "instances_used": instances_used,
        "usage_rate": round(instances_used / instances_total, 4) if instances_total else 0.0,
        "per_term": per_term,
        "violations": violations[:20],
        "ambiguous_translations": _glossary_ambiguity(glossary),
    }


# --------------------------------------------------------------------------- #
# prepare
# --------------------------------------------------------------------------- #
def _load_primary_document(found: dict[str, Any], stopwatch: hybrid.Stopwatch,
                           limits: list[str], uncertainty: list[str],
                           exclude: Iterable[str] = ()) -> tuple[dict[str, Any] | None,
                                                                  list[dict[str, Any]]]:
    deadline = stopwatch.start + max(60.0, hybrid.SOFT_DEADLINE_S - 90.0)
    docs: list[dict[str, Any]] = []
    excluded = {str(Path(p)) for p in exclude}
    for raw_path in found.get("material_docs") or []:
        if str(Path(raw_path)) in excluded or _is_glossary_like(Path(raw_path)):
            continue
        if len(docs) >= MAX_MATERIAL_DOCS:
            break
        if stopwatch.elapsed() > hybrid.SOFT_DEADLINE_S - 90:
            limits.append("extraction soft deadline reached; remaining material documents skipped")
            break
        doc = extract.extract_document(raw_path, deadline=deadline)
        doc["path"] = str(raw_path)
        docs.append(doc)
        for warning in doc.get("warnings") or []:
            uncertainty.append(f"{Path(raw_path).name}: {warning}")
        if doc.get("blocks") and doc.get("text_layer"):
            return doc, docs
    if docs:
        limits.append("no material document produced a usable text layer")
    return None, docs


def _document_fingerprint(doc: dict[str, Any], segments: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "pages": len(doc.get("pages") or []),
        "total_chars": doc.get("total_chars"),
        "block_count": sum(len(v) for v in (doc.get("blocks") or {}).values()),
        "segment_count": len(segments),
    }


def do_prepare(args: argparse.Namespace, runtime: dict[str, Any],
               stopwatch: hybrid.Stopwatch) -> dict[str, Any]:
    found = runtime["found"]
    out_dir = Path(runtime["out_dir"])
    case = runtime["case"]
    case_id = runtime["case_id"]
    limits: list[str] = []
    uncertainty: list[str] = []
    change_log = ["prepare: deterministic extraction, segmentation and fact extraction"]
    work = resolve_work_dir(args.work, out_dir)
    seed, seed_notes = load_seed_glossary()
    uncertainty.extend(seed_notes)
    provided, provided_notes = parse_provided_glossary(glossary_candidates(found))
    uncertainty.extend(provided_notes)

    stopwatch.log(f"prepare: case={case_id or '<none>'} work={work}")
    doc, docs = _load_primary_document(found, stopwatch, limits, uncertainty)
    if doc is None:
        limits.append("no document with a text layer was found; nothing to segment. "
                      "The translation field stays empty and the run is reported failed.")
        state = {"stage": "failed", "case_id": case_id, "segment_count": 0,
                 "batch_count": 0, "work_dir": str(work)}
        hybrid.write_json(work / "state.json", state)
        values = {"bilingual": [], "glossary": [], "coverage": {
            "total_segments": 0, "received": 0, "coverage": 0.0},
            "untranslated": [], "term_consistency": {"available": False},
            "quality": {"segments_total": 0}, "checks": [], "review_checklist": [],
            "limitations": limits, "uncertainty": uncertainty, "status": "failed",
            "language_pair": _language_pair(case, found), "translation_text": "",
            "source_text": "", "segments": []}
        _finish(runtime, values, status="failed", limits=limits, uncertainty=uncertainty,
                change_log=change_log, evidence={"stage": "prepare", "documents": hybrid.summarize_docs(docs)},
                report=_prepare_report(stopwatch, work, doc, docs, 0, [], provided),
                out_files={})
        return state

    struct = structure.outline(doc)
    segments = hybrid.build_segments(doc, struct, max_chars=MAX_SEGMENT_CHARS)
    if not segments:
        limits.append("the document was extracted but produced zero addressable paragraphs")
        state = {"stage": "failed", "case_id": case_id, "segment_count": 0,
                 "batch_count": 0, "work_dir": str(work)}
        hybrid.write_json(work / "state.json", state)
        values = {"bilingual": [], "glossary": [], "coverage": {
            "total_segments": 0, "received": 0, "coverage": 0.0},
            "untranslated": [], "term_consistency": {"available": False},
            "quality": {"segments_total": 0}, "checks": [], "review_checklist": [],
            "limitations": limits, "uncertainty": uncertainty, "status": "failed",
            "language_pair": _language_pair(case, found), "translation_text": "",
            "source_text": "", "segments": []}
        _finish(runtime, values, status="failed", limits=limits, uncertainty=uncertainty,
                change_log=change_log, evidence={"stage": "prepare", "documents": hybrid.summarize_docs(docs)},
                report=_prepare_report(stopwatch, work, doc, docs, 0, [], provided),
                out_files={})
        return state

    fingerprint = _document_fingerprint(doc, segments)
    inventory_path = work / "segments.json"
    previous = hybrid.read_json(inventory_path, default=None)
    inventory_reused = bool(previous and previous.get("fingerprint") == fingerprint
                            and previous.get("segments"))
    if previous and not inventory_reused:
        uncertainty.append("the extracted document changed since the previous prepare; the segment "
                           "inventory was rebuilt, so previously generated seg_ids may no longer align")
    inventory = {
        "case_id": case_id,
        "skill_version": SKILL_VERSION,
        "doc": {"path": doc.get("path"), "engine": doc.get("engine"),
                "anchor_quality": doc.get("anchor_quality"),
                "page_count": len(doc.get("pages") or []),
                "total_chars": doc.get("total_chars")},
        "fingerprint": fingerprint,
        "segment_count": len(segments),
        "segments": segments,
    }
    hybrid.write_json(inventory_path, inventory)

    full_text = "\n".join(s["text"] for s in segments)
    source_language, target_language = detect_target_language(segments)
    candidates = hybrid.term_candidates(full_text, top=GLOSSARY_TOP, min_count=2)
    hybrid.write_json(work / "term_candidates.json", {"case_id": case_id, "terms": candidates})
    hybrid.write_json(work / "provided_glossary.json", {"case_id": case_id, "entries": provided})

    agent_glossary, agent_status = load_agent_glossary(work)
    glossary, glossary_status = merge_glossary(provided, agent_glossary, candidates, seed)
    glossary_worksheet = build_glossary_worksheet(segments, candidates, provided, seed,
                                                  target_language=target_language)
    glossary_worksheet["already_written"] = agent_status
    glossary_worksheet["locked_so_far"] = [
        {"term": e["term"], "translation": e["translation"],
         "keep_original": e["keep_original"], "source": e["source"]} for e in glossary]
    hybrid.write_json(work / "glossary_worksheet.json", glossary_worksheet)

    facts = [{"seg_id": s["seg_id"], "page": s["page"], **segment_facts(s["text"])} for s in segments]
    hybrid.write_json(work / "facts.json", {"case_id": case_id, "segments": facts})

    (work / "generated").mkdir(parents=True, exist_ok=True)
    batches = hybrid.make_batches(segments, work, batch_size=BATCH_SEGMENTS,
                                  batch_chars=BATCH_CHARS)
    worksheets = enrich_worksheets(work, segments, glossary, [b["batch_id"] for b in batches],
                                   target_language=target_language)
    if glossary_status.get("source") == "missing":
        limits.append("no glossary has been locked yet: write work/generated/_glossary.json "
                      "and re-run --prepare so every worksheet carries the locked terms")
    if agent_status == "missing":
        uncertainty.append("glossary worksheet not answered yet (work/generated/_glossary.json "
                           "missing); term locking is not yet enforced")
    batch_index = {"case_id": case_id, "stage": "prepared", "work_dir": str(work),
                   "segment_count": len(segments), "batch_count": len(worksheets),
                   "source_language": source_language, "target_language": target_language,
                   "batches": worksheets}
    hybrid.write_json(work / "index.json", batch_index)
    protocol = {
        "case_id": case_id,
        "skill_version": SKILL_VERSION,
        "stage": "prepared",
        "work_dir": str(work),
        "segment_count": len(segments),
        "batch_count": len(worksheets),
        "source_language": source_language,
        "target_language": target_language,
        "steps": [
            {"order": 1, "action": "write",
             "file": "generated/_glossary.json",
             "schema": [{"term": "supported nickel catalysts", "translation": "负载型镍催化剂",
                         "keep_original": False, "note": "rationale (optional)"}],
             "note": "Read glossary_worksheet.json first; lock every term that recurs. One model request."},
            {"order": 2, "action": "write",
             "file": "generated/<batch_id>.json",
             "schema": [{"seg_id": "seg-0001", "translation": "目标语言译文", "terms_used": [],
                         "note": ""}],
             "note": ("One model request per worksheet: translate every segment in that worksheet, "
                      "using the locked terms in its locked_terms list.")},
            {"order": 3, "action": "run",
             "command": f"python scripts/run_translate.py --finalize --work {work}",
             "note": "Deterministic: coverage, preservation, term consistency, anchors, contract."},
        ],
        "check_command": f"python scripts/run_translate.py --check --work {work}",
    }
    hybrid.write_json(work / "protocol.json", protocol)
    hybrid.write_json(work / "state.json", {"stage": "prepared", "case_id": case_id,
                                            "segment_count": len(segments),
                                            "batch_count": len(worksheets),
                                            "source_language": source_language,
                                            "target_language": target_language,
                                            "work_dir": str(work),
                                            "inventory_reused": inventory_reused})
    hybrid.write_json(out_dir / "work_report.json", {
        "case_id": case_id,
        "stage": "prepare",
        "work_dir": str(work),
        "segment_count": len(segments),
        "batch_count": len(worksheets),
        "candidate_terms": len(candidates),
        "provided_terms": len(provided),
        "locked_terms": len(glossary),
        "inventory_reused": inventory_reused,
        "limits": limits,
        "uncertainty": uncertainty,
        "batches": worksheets,
    })
    hybrid.write_placeholder(out_dir, case_id=case_id,
                             contract_fields=len(runtime["declared"]),
                             note=("prepare finished; run --finalize after the agent writes "
                                   "work/generated/*.json"))

    print(f"PREPARE OK  case={case_id or '<none>'}  segments={len(segments)}  "
          f"batches={len(worksheets)}  terms={len(candidates)}  locked={len(glossary)}  work={work}")
    print("NEXT (agent):")
    print(f"  1. read {work / 'glossary_worksheet.json'} -> write {work / 'generated' / '_glossary.json'}")
    print(f"  2. (optional but recommended) re-run --prepare so worksheets carry locked_terms")
    if worksheets:
        print(f"  3. for each {work / 'batches' / 'batch-XX.json'} write "
              f"{work / 'generated' / 'batch-XX.json'} (one model request per batch)")
    print(f"  4. python scripts/run_translate.py --finalize --work {work}")
    print(f"  5. python scripts/run_translate.py --check --work {work}   # resume-safe progress check")
    return {"stage": "prepared", "case_id": case_id, "segment_count": len(segments),
            "batch_count": len(worksheets), "work_dir": str(work), "limits": limits,
            "uncertainty": uncertainty, "doc": doc, "docs": docs, "provided": provided,
            "stopwatch": stopwatch}


def _language_pair(case: dict[str, Any], found: dict[str, Any]) -> dict[str, Any]:
    target = "zh"
    source = "en"
    for key in ("target_language", "targetLocale", "target_locale", "language"):
        value = case.get(key)
        if isinstance(value, str) and value.strip():
            target = value.strip()
            break
    for key in ("source_language", "sourceLocale", "source_locale"):
        value = case.get(key)
        if isinstance(value, str) and value.strip():
            source = value.strip()
            break
    return {"source": source, "target": target,
            "note": "default assumption; the detected source language is reported in quality"}


def _prepare_report(stopwatch: hybrid.Stopwatch, work: Path, doc: dict[str, Any] | None,
                    docs: list[dict[str, Any]], batch_count: int,
                    worksheets: list[dict[str, Any]],
                    provided: list[dict[str, Any]]) -> dict[str, Any]:
    return hybrid.run_report(
        stopwatch=stopwatch,
        constants={"mode": "prepare", "work_dir": str(work),
                   "engines_probed": extract.probe()},
        documents=hybrid.summarize_docs(docs),
        primary_doc={"path": doc.get("path"), "engine": doc.get("engine"),
                     "anchor_quality": doc.get("anchor_quality")} if doc else None,
        batch_count=batch_count,
        worksheets=worksheets,
        provided_terms=len(provided),
        model_requests_note=("this script makes no model request; the agent spends one request "
                             "per worksheet plus one for the glossary"),
    )


# --------------------------------------------------------------------------- #
# finalize
# --------------------------------------------------------------------------- #
def _load_inventory(work: Path) -> dict[str, Any] | None:
    data = hybrid.read_json(work / "segments.json", default=None)
    if isinstance(data, dict) and isinstance(data.get("segments"), list) and data["segments"]:
        return data
    return None


def _generated_files(work: Path) -> list[str]:
    directory = work / "generated"
    if not directory.is_dir():
        return []
    return [p.name for p in sorted(directory.glob("*.json"))]


def _batch_status(work: Path) -> dict[str, Any]:
    worksheet_files = sorted((work / "batches").glob("batch-*.json"))
    answered = {p.name for p in (work / "generated").glob("*.json")} if (work / "generated").is_dir() else set()
    pending = [p.stem for p in worksheet_files if p.name not in answered]
    return {"total": len(worksheet_files),
            "answered": len(worksheet_files) - len(pending),
            "pending": pending}


def _build_pairs(segments: list[dict[str, Any]], merged: dict[str, Any],
                 glossary: list[dict[str, Any]],
                 *, target_language: str = "zh") -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    by_id = merged.get("by_id") or {}
    for index, segment in enumerate(segments):
        record = by_id.get(segment["seg_id"]) or {}
        translation = str(record.get("translation") or "").strip()
        source = segment["text"]
        source_chinese = _source_is_chinese(source) and target_language == "zh"
        status = "translated"
        reason = ""
        if source_chinese:
            status = "source_chinese"
            translation = translation or source
            reason = "source paragraph is already Chinese; no translation required"
        elif not translation:
            status = "untranslated"
            reason = "no generated translation" if not record else "generated translation is empty"
        elif hybrid.normalize_text(translation) == hybrid.normalize_text(source):
            status = "copied_source"
            reason = "translation is identical to the source; reported instead of counted as translated"
        elif not _looks_chinese(translation):
            status = "untranslated"
            reason = "translation contains no Chinese characters"
        terms = _terms_in_segment(source, glossary)
        for term in terms:
            expected = term["term"] if term["keep_original"] else term["translation"]
            term["used"] = bool(expected) and _contains(translation, expected)
        quote = anchors.pick_quote(source, [], width=160)
        pair = {
            "pair_id": f"pair-{index + 1:04d}",
            "seg_id": segment["seg_id"],
            "order": segment.get("order", index + 1),
            "page": segment["page"],
            "block_id": (segment.get("block_ids") or [None])[0],
            "bbox": segment.get("bbox"),
            "section": segment.get("section_title"),
            "src": source,
            "tgt": translation,
            "status": status,
            "reason": reason,
            "anchor": {"page": segment["page"],
                       "block_id": (segment.get("block_ids") or [None])[0],
                       "bbox": segment.get("bbox"), "quote": quote},
            "anchor_check": {"grounded": False, "match": "not_verified"},
            "basis": {"terms": terms, "notes": []},
        }
        if status in {"translated", "source_chinese"}:
            pair["checks"] = translation_check(source, translation, seg_id=segment["seg_id"],
                                               target_language=target_language)
        else:
            pair["checks"] = {"label": segment["seg_id"], "ok": False, "issues": [status],
                              "numbers_missing": [], "citations_missing": [],
                              "formulas_missing": [], "hedges_missing": [],
                              "negations_missing": [], "modals_missing": []}
        pairs.append(pair)
    return pairs


def _attach_anchor_checks(pairs: list[dict[str, Any]], doc: dict[str, Any] | None,
                          uncertainty: list[str]) -> dict[str, Any]:
    if doc is None:
        for pair in pairs:
            pair["anchor_check"] = {"grounded": False, "match": "unavailable",
                                    "reason": "source document not available in this run"}
        return {"claims_total": len(pairs), "grounded": 0, "ungrounded": len(pairs),
                "grounding_rate": 0.0, "anchor_quality": "none",
                "effective_confidence": 0.0,
                "unavailable_reason": "the source document could not be read in this run; "
                                      "anchors were emitted but not re-verified"}
    claims = [{"claim_id": pair["pair_id"],
               "claim": f"source paragraph {pair['seg_id']} located on page {pair['page']}",
               "kind": "structural",
               "anchors": [pair["anchor"]]} for pair in pairs]
    grounding = anchors.verify_all(doc, claims)
    per_claim = {item.get("claim_id"): item for item in grounding.get("per_claim") or []}
    for pair in pairs:
        item = per_claim.get(pair["pair_id"]) or {}
        checks = item.get("checks") or []
        match = checks[0].get("match") if checks else "missing"
        pair["anchor_check"] = {"grounded": bool(item.get("grounded")),
                                "located": bool(item.get("located")),
                                "match": match}
        if not item.get("grounded"):
            uncertainty.append(f"anchor for {pair['seg_id']} did not re-verify in the source "
                               f"document (match={match})")
    return grounding


def _summary_grounding(grounding: dict[str, Any]) -> dict[str, Any]:
    return {key: grounding.get(key) for key in
            ("claims_total", "grounded", "ungrounded", "grounding_rate",
             "anchor_quality", "confidence_ceiling", "effective_confidence",
             "unavailable_reason")}


def _coverage_report(segments: list[dict[str, Any]], merged: dict[str, Any],
                     pairs: list[dict[str, Any]], work: Path) -> dict[str, Any]:
    translated = sum(1 for p in pairs if p["status"] == "translated")
    source_chinese = sum(1 for p in pairs if p["status"] == "source_chinese")
    untranslated = len(pairs) - translated - source_chinese
    batches = _batch_status(work)
    source_chars = sum(len(p["src"]) for p in pairs)
    target_chars = sum(len(p["tgt"]) for p in pairs)
    pages: dict[str, int] = {}
    for pair in pairs:
        pages[str(pair["page"])] = pages.get(str(pair["page"]), 0) + 1
    return {
        "total_segments": merged.get("total_segments", len(segments)),
        "received": merged.get("received", 0),
        "coverage": merged.get("coverage", 0.0),
        "translated": translated,
        "source_chinese": source_chinese,
        "untranslated": untranslated,
        "missing_seg_ids": (merged.get("missing") or [])[:200],
        "missing_count": len(merged.get("missing") or []),
        "duplicate_seg_ids": (merged.get("duplicates") or [])[:50],
        "unknown_seg_ids": (merged.get("unknown_ids") or [])[:50],
        "incomplete_seg_ids": (merged.get("incomplete") or [])[:50],
        "batches_total": batches["total"],
        "batches_answered": batches["answered"],
        "batches_pending": batches["pending"],
        "pages": pages,
        "source_chars": source_chars,
        "target_chars": target_chars,
        "length_ratio": round(target_chars / source_chars, 3) if source_chars else 0.0,
    }


def _review_checklist(pairs: list[dict[str, Any]],
                      terms: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for pair in pairs:
        if pair["status"] != "translated":
            items.append({"kind": "untranslated", "seg_id": pair["seg_id"],
                          "page": pair["page"], "detail": pair.get("reason", "")})
            continue
        checks = pair.get("checks") or {}
        if not checks.get("ok"):
            detail = []
            for key in ("numbers_missing", "citations_missing", "formulas_missing",
                        "hedges_missing", "negations_missing", "modals_missing"):
                values = checks.get(key) or []
                if values:
                    detail.append(f"{key}={values[:6]}")
            if checks.get("issues"):
                detail.append("issues=" + ",".join(str(i) for i in checks["issues"]))
            items.append({"kind": "preservation", "seg_id": pair["seg_id"],
                          "page": pair["page"], "detail": "; ".join(detail)})
        if not (pair.get("anchor_check") or {}).get("grounded"):
            items.append({"kind": "anchor", "seg_id": pair["seg_id"],
                          "detail": str((pair.get("anchor_check") or {}).get("match"))})
    for violation in terms.get("violations") or []:
        items.append({"kind": "term_consistency", "term": violation["term"],
                      "expected": violation["translation"],
                      "detail": "offending segments: " + ", ".join(violation["offending_seg_ids"][:6])})
    for ambiguous in terms.get("ambiguous_translations") or []:
        items.append({"kind": "ambiguous_translation",
                      "detail": f"{ambiguous['translation']} is used for {ambiguous['terms']}"})
    return items[:200]


def _write_bilingual_markdown(path: Path, case_id: str, pairs: list[dict[str, Any]],
                              glossary: list[dict[str, Any]], coverage: dict[str, Any],
                              terms: dict[str, Any], status: str) -> dict[str, Any]:
    lines: list[str] = []
    lines.append(f"# 双语对照（段落级）：{case_id or 'unknown-case'}")
    lines.append("")
    lines.append("> 由阅读03「文献翻译」技能确定性生成：术语表锁定 + 段落级对齐 + 机器校验。"
                 "未翻译段落已明确标注，不臆造译文。")
    lines.append("")
    lines.append(f"- 运行状态：**{status}**")
    lines.append(f"- 段落总数：{coverage.get('total_segments')}；已翻译：{coverage.get('translated')}；"
                 f"未翻译：{coverage.get('untranslated')}；覆盖率：{coverage.get('coverage')}")
    lines.append(f"- 术语条目：{len(glossary)}；术语一致率：{terms.get('term_rate')}；"
                 f"术语使用率：{terms.get('usage_rate')}")
    lines.append("")
    lines.append("## 术语表（locked glossary）")
    lines.append("")
    lines.append("| # | 源术语 | 中文译名 | 保留原文 | 出现段数 | 一致率 |")
    lines.append("|---|---|---|---|---|---|")
    per_term = {row["term"]: row for row in terms.get("per_term") or []}
    for index, entry in enumerate(glossary, start=1):
        row = per_term.get(entry["term"]) or {}
        lines.append(f"| {index} | {entry['term']} | {entry['translation'] or '（未译）'} | "
                     f"{'是' if entry['keep_original'] else '否'} | "
                     f"{row.get('segments_with_term', 0)} | {row.get('rate', 0.0)} |")
    lines.append("")
    lines.append("## 逐段对照")
    lines.append("")
    current_page = None
    for pair in pairs:
        if pair["page"] != current_page:
            current_page = pair["page"]
            lines.append(f"### 第 {current_page} 页")
            lines.append("")
        section = pair.get("section")
        label = " · ".join(str(x) for x in (pair["seg_id"],
                                            f"p{pair['page']}",
                                            pair.get("block_id"),
                                            section) if x)
        lines.append(f"**{label}**")
        lines.append("")
        lines.append(f"- 原文：{pair['src']}")
        if pair["status"] == "translated":
            lines.append(f"- 译文：{pair['tgt']}")
        else:
            lines.append(f"- 译文：［未生成译文：{pair['status']}，{pair.get('reason') or '需人工确认'}］")
        checks = pair.get("checks") or {}
        if pair["status"] == "translated":
            verdict = "通过" if checks.get("ok") else "需人工确认"
            basis = [f"{t['term']} → {t['term'] if t['keep_original'] else t['translation']}"
                     for t in (pair.get("basis") or {}).get("terms") or []]
            lines.append(f"- 机器校验：{verdict}"
                         + (f"（{'; '.join(checks.get('issues') or [])}）" if checks.get("issues") else ""))
            if basis:
                lines.append(f"- 译法依据：{'；'.join(basis)}")
        anchor = pair.get("anchor") or {}
        check = pair.get("anchor_check") or {}
        lines.append(f"- 证据锚点：page={anchor.get('page')} block={anchor.get('block_id')} "
                     f"bbox={anchor.get('bbox')} 校验={check.get('match')}")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"bilingual_markdown": str(path), "chars": sum(len(x) for x in lines)}


def _write_review_markdown(path: Path, case_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    lines = [f"# 待人工确认清单：{case_id or 'unknown-case'}", ""]
    if not items:
        lines.append("无需人工确认项：所有段落均通过数字/引用/公式/限定词与锚点校验。")
    else:
        lines.append(f"共 {len(items)} 条。按类型分组：")
        lines.append("")
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            groups.setdefault(str(item.get("kind")), []).append(item)
        for kind in sorted(groups):
            lines.append(f"## {kind}（{len(groups[kind])}）")
            lines.append("")
            for item in groups[kind]:
                label = item.get("seg_id") or item.get("term") or ""
                lines.append(f"- {label}：{item.get('detail') or ''}")
            lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"review_markdown": str(path)}


def _finish(runtime: dict[str, Any], values: dict[str, Any], *, status: str,
            limits: list[str], uncertainty: list[str], change_log: list[str],
            evidence: dict[str, Any] | None, report: dict[str, Any] | None,
            out_files: dict[str, Any]) -> dict[str, Any]:
    out_dir = Path(runtime["out_dir"])
    if out_files:
        evidence = dict(evidence or {})
        evidence["output_files"] = out_files
    payload = hybrid.finalize(
        out_dir, case=runtime["case"], case_id=runtime["case_id"] or "",
        values=values, status=status, limits=limits, uncertainty=uncertainty,
        change_log=change_log, task_md_path=(runtime["found"] or {}).get("task_md_path"),
        evidence=evidence, report=report, synonyms=SYNONYMS,
        additional=(ADDITIONAL_KEYS if runtime.get("declared") else None))
    return payload


def do_finalize(args: argparse.Namespace, runtime: dict[str, Any],
                stopwatch: hybrid.Stopwatch) -> dict[str, Any]:
    found = runtime["found"]
    out_dir = Path(runtime["out_dir"])
    case = runtime["case"]
    case_id = runtime["case_id"]
    work = resolve_work_dir(args.work, out_dir)
    limits: list[str] = []
    uncertainty: list[str] = []
    change_log = ["finalize: deterministic coverage / preservation / term checks and contract reflection"]
    stopwatch.log(f"finalize: case={case_id or '<none>'} work={work}")

    seed, seed_notes = load_seed_glossary()
    uncertainty.extend(seed_notes)
    provided: list[dict[str, Any]] = []
    stored_provided = hybrid.read_json(work / "provided_glossary.json", default=None)
    if isinstance(stored_provided, dict) and isinstance(stored_provided.get("entries"), list):
        provided = [e for e in (_normalise_entry(item, "case_provided")
                                for item in stored_provided["entries"]) if e]
    if not provided:
        provided, notes = parse_provided_glossary(glossary_candidates(found))
        uncertainty.extend(notes)

    inventory = _load_inventory(work)
    doc, docs = _load_primary_document(found, stopwatch, limits, uncertainty,
                                       exclude=())
    if inventory is not None:
        segments = inventory["segments"]
        inventory_source = "prepare_manifest"
        if doc is not None and inventory.get("fingerprint"):
            current = {"pages": len(doc.get("pages") or []),
                       "total_chars": doc.get("total_chars"),
                       "block_count": sum(len(v) for v in (doc.get("blocks") or {}).values()),
                       "segment_count": len(hybrid.build_segments(
                           doc, structure.outline(doc), max_chars=MAX_SEGMENT_CHARS))}
            if current != inventory["fingerprint"]:
                uncertainty.append("the segment inventory from prepare does not match the document "
                                   "re-extracted now; anchors are verified against the new extraction")
    elif doc is not None:
        segments = hybrid.build_segments(doc, structure.outline(doc), max_chars=MAX_SEGMENT_CHARS)
        inventory_source = "recomputed"
        limits.append("work/segments.json was missing; the inventory was recomputed from the document")
    else:
        segments = []
        inventory_source = "missing"
        limits.append("no segment inventory and no readable material document were available")

    candidates_raw = hybrid.read_json(work / "term_candidates.json", default=None)
    if isinstance(candidates_raw, dict) and isinstance(candidates_raw.get("terms"), list):
        candidates = candidates_raw["terms"]
    else:
        full_text = "\n".join(s["text"] for s in segments)
        candidates = hybrid.term_candidates(full_text, top=GLOSSARY_TOP, min_count=2)

    records = hybrid.load_generated(work)
    merged = hybrid.merge_generated(segments, records, required=("translation",))
    agent_glossary, agent_status = load_agent_glossary(work)
    glossary, glossary_status = merge_glossary(provided, agent_glossary, candidates, seed)
    ambiguity = _glossary_ambiguity(glossary)

    if agent_status == "missing":
        limits.append("work/generated/_glossary.json was missing: the glossary degrades to "
                      "case-provided terms and the seed fallback, and is marked as not locked")
    elif agent_status == "empty":
        limits.append("work/generated/_glossary.json contained no usable entry")
    if not segments:
        status = "failed"
        limits.append("no translatable segments were found; the translation fields stay empty")
    elif not records:
        status = "partial"
        limits.append("no generated translation files were found under work/generated: every "
                      "paragraph is reported as untranslated instead of being invented")
    else:
        untranslated_count = len(merged.get("missing") or []) + len(merged.get("incomplete") or [])
        if untranslated_count:
            status = "partial"
            limits.append(f"{untranslated_count} of {len(segments)} paragraphs have no usable "
                          "generated translation; they are emitted with tgt=\"\" and "
                          "status=\"untranslated\"")
        else:
            status = "success"
    if doc is None and segments:
        status = "partial"
        limits.append("the source document could not be re-read in this run, so anchors were not "
                      "re-verified (see evidence.grounding.unavailable_reason)")

    source_language, target_language = detect_target_language(segments)
    pairs = _build_pairs(segments, merged, glossary, target_language=target_language)
    grounding = _attach_anchor_checks(pairs, doc, uncertainty)
    translations = {pair["seg_id"]: pair["tgt"] for pair in pairs}
    terms = check_terms(glossary, segments, translations)
    terms["glossary_status"] = glossary_status
    terms["glossary_ambiguity"] = ambiguity
    if terms.get("inconsistent_terms"):
        uncertainty.append(f"{terms['inconsistent_terms']} term(s) are not rendered consistently "
                           "across paragraphs; see term_consistency.per_term")
    coverage = _coverage_report(segments, merged, pairs, work)
    checks = [pair["checks"] for pair in pairs if pair["status"] == "translated"]
    preservation = hybrid.aggregate_preservation(checks)
    for pair in pairs:
        if pair["status"] == "translated" and not pair["checks"].get("ok"):
            uncertainty.append(f"paragraph {pair['seg_id']} did not pass the preservation check: "
                               + ",".join(pair["checks"].get("issues") or []))
    untranslated = [{"seg_id": pair["seg_id"], "page": pair["page"],
                     "order": pair["order"], "status": pair["status"],
                     "reason": pair.get("reason") or "not translated"}
                    for pair in pairs if pair["status"] not in {"translated", "source_chinese"}]
    checklist = _review_checklist(pairs, terms)

    pages = sorted({pair["page"] for pair in pairs})
    source_text = "\n\n".join(f"[[page {pair['page']}]] {pair['src']}" for pair in pairs)
    translation_parts: list[str] = []
    for pair in pairs:
        if pair["status"] == "translated":
            translation_parts.append(pair["tgt"])
        else:
            translation_parts.append(f"（未生成译文：{pair['seg_id']}，需人工确认）")
    translation_text = "\n\n".join(translation_parts)

    language_pair = _language_pair(case, found)
    language_pair.update({"detected_source_language": source_language,
                          "resolved_target_language": target_language})
    quality = {
        "skill_version": SKILL_VERSION,
        "engine": (inventory or {}).get("doc", {}).get("engine") if inventory else (doc or {}).get("engine"),
        "anchor_quality": grounding.get("anchor_quality"),
        "page_count": len(pages),
        "segments_total": len(segments),
        "translated": coverage["translated"],
        "untranslated": coverage["untranslated"],
        "coverage": coverage["coverage"],
        "preservation": preservation,
        "checks_failed": max(0, len(checks) - preservation.get("clean", 0)),
        "term_rate": terms.get("term_rate"),
        "term_usage_rate": terms.get("usage_rate"),
        "glossary_entries": len(glossary),
        "anchor_grounding_rate": grounding.get("grounding_rate"),
        "detected_source_language": source_language,
        "target_language": target_language,
        "elapsed_s": stopwatch.elapsed(),
    }

    out_files: dict[str, Any] = {}
    bilingual_path = out_dir / "translation_bilingual.md"
    out_files.update(_write_bilingual_markdown(bilingual_path, case_id or "", pairs,
                                               glossary, coverage, terms, status))
    glossary_path = out_dir / "glossary.json"
    hybrid.write_json(glossary_path, {
        "case_id": case_id,
        "glossary_source": glossary_status.get("source"),
        "entries": glossary,
        "term_consistency": {key: terms.get(key) for key in
                             ("terms_checked", "consistent_terms", "inconsistent_terms",
                              "term_rate", "usage_rate", "violations",
                              "ambiguous_translations")},
    })
    out_files["glossary_json"] = str(glossary_path)
    review_path = out_dir / "review_checklist.md"
    out_files.update(_write_review_markdown(review_path, case_id or "", checklist))
    out_files["untranslated_count"] = len(untranslated)

    def _deliverable(path: Path) -> dict[str, Any]:
        return {"path": str(path),
                "bytes": path.stat().st_size if path.is_file() else 0}

    deliverables = {
        "bilingual_markdown": _deliverable(bilingual_path),
        "glossary_json": _deliverable(glossary_path),
        "review_checklist": _deliverable(review_path),
    } if out_files else {}
    payload_value = {
        "translation_text": translation_text,
        "bilingual": pairs,
        "segment_count": len(segments),
        "page_count": len(pages),
        "coverage": coverage,
        "untranslated": untranslated,
        "term_consistency": {key: terms.get(key) for key in
                             ("terms_checked", "consistent_terms", "inconsistent_terms",
                              "term_rate", "usage_rate", "glossary_status")},
        "glossary": glossary,
        "quality": quality,
        "language_pair": language_pair,
        "status": status,
    }
    summary_text = (
        f"{len(pairs)} paragraph(s) on {len(pages)} page(s) were processed "
        f"({source_language} -> {target_language}). Translation coverage "
        f"{coverage.get('coverage')}: {coverage.get('translated', 0)} translated, "
        f"{coverage.get('untranslated', 0)} untranslated (each listed with its segment id "
        f"and reason; no translation was invented for a missing segment). "
        f"{len(glossary)} term(s) are in the glossary, term consistency "
        f"{terms.get('term_rate')}, locked-term usage rate "
        f"{terms.get('usage_rate')}. Invariant preservation: "
        f"{preservation.get('clean')}/{preservation.get('checked')} paragraph(s) clean "
        f"(numbers, citations, formulas and hedges kept). Deliverables: bilingual "
        f"markdown, glossary.json, review_checklist.md. Status: {status}. "
        f"共处理 {len(pairs)} 个段落（{len(pages)} 页），翻译覆盖率 "
        f"{coverage.get('coverage')}，未翻译 {coverage.get('untranslated', 0)} 段，"
        f"术语锁定 {len(glossary)} 条，译文核验 {preservation.get('clean')}/"
        f"{preservation.get('checked')} 段通过。")

    values: dict[str, Any] = {
        "bilingual": pairs,
        "glossary": glossary,
        "coverage": coverage,
        "untranslated": untranslated,
        "term_consistency": terms,
        "evidence": {"grounding": _summary_grounding(grounding),
                     "inventory_source": inventory_source,
                     "documents": hybrid.summarize_docs(docs),
                     "pair_anchors_grounded": sum(1 for p in pairs
                                                  if (p.get("anchor_check") or {}).get("grounded"))},
        "quality": quality,
        "checks": [{"seg_id": pair["seg_id"], "page": pair["page"],
                    "status": pair["status"], "ok": pair["checks"].get("ok"),
                    "issues": pair["checks"].get("issues") or [],
                    "numbers_missing": pair["checks"].get("numbers_missing") or [],
                    "citations_missing": pair["checks"].get("citations_missing") or [],
                    "formulas_missing": pair["checks"].get("formulas_missing") or []}
                   for pair in pairs],
        "review_checklist": checklist,
        "translation_text": translation_text,
        "source_text": source_text,
        "segments": [{"seg_id": s["seg_id"], "order": s.get("order"), "page": s["page"],
                      "block_id": (s.get("block_ids") or [None])[0],
                      "bbox": s.get("bbox"), "section": s.get("section_title"),
                      "char_count": s.get("char_count")} for s in segments],
        "limitations": limits,
        "uncertainty": uncertainty,
        "status": status,
        "language_pair": language_pair,
        "payload": payload_value,
        "summary_text": summary_text,
        "deliverables": deliverables,
    }

    evidence_payload = {
        "stage": "finalize",
        "grounding": _summary_grounding(grounding),
        "per_pair_anchors": [{"pair_id": p["pair_id"], "seg_id": p["seg_id"],
                              "page": p["page"], "block_id": p["block_id"],
                              "bbox": p["bbox"], "quote": (p.get("anchor") or {}).get("quote"),
                              "check": p.get("anchor_check")} for p in pairs],
        "segment_inventory": {"source": inventory_source,
                              "count": len(segments),
                              "doc": (inventory or {}).get("doc")},
        "generated_files": _generated_files(work),
        "glossary": glossary_status,
        "documents": hybrid.summarize_docs(docs),
        "discovery": {key: (found or {}).get(key) for key in
                      ("case_path", "task_md_path", "material_docs", "input_roots",
                       "output_dir", "files_scanned", "missing")},
        "checks_summary": {"checked": len(checks), "clean": preservation.get("clean", 0),
                           "term_rate": terms.get("term_rate"),
                           "usage_rate": terms.get("usage_rate")},
    }
    report = hybrid.run_report(
        stopwatch=stopwatch,
        constants={"mode": "finalize", "work_dir": str(work),
                   "case_id": case_id or "",
                   "coverage": coverage,
                   "term_consistency": {key: terms.get(key) for key in
                                        ("terms_checked", "consistent_terms",
                                         "inconsistent_terms", "term_rate", "usage_rate")},
                   "glossary": {key: glossary_status.get(key) for key in
                                ("source", "total_entries", "agent_entries",
                                 "provided_entries", "auto_entries")},
                   "generated_files": _generated_files(work),
                   "missing_batches": _batch_status(work)["pending"],
                   "limits": limits,
                   "uncertainty": uncertainty,
                   "outputs": out_files,
                   "engines_probed": extract.probe()},
        documents=hybrid.summarize_docs(docs),
        inventory_source=inventory_source,
        preservation=preservation,
        model_requests_note=("this script makes no model request; judge the agent side from "
                             "work/generated/*.json file count (one request per batch, plus one "
                             "for the glossary)"),
    )
    payload = _finish(runtime, values, status=status, limits=limits,
                      uncertainty=uncertainty, change_log=change_log,
                      evidence=evidence_payload, report=report, out_files=out_files)
    # write the full evidence after finalize so meta.evidence stays compact
    hybrid.write_json(out_dir / "evidence.json", {"case_id": case_id, **evidence_payload})
    hybrid.write_json(work / "state.json", {"stage": "finalized", "case_id": case_id,
                                            "status": status,
                                            "coverage": coverage.get("coverage"),
                                            "untranslated": len(untranslated),
                                            "work_dir": str(work)})
    stopwatch.log("finalize done: status=%s coverage=%s terms=%s checks_clean=%s/%s" % (
        status, coverage.get("coverage"), terms.get("term_rate"),
        preservation.get("clean"), len(checks)))
    print(f"FINALIZE DONE  case={case_id or '<none>'}  status={status}  "
          f"segments={len(segments)}  coverage={coverage.get('coverage')}  "
          f"term_rate={terms.get('term_rate')}  checks_clean={preservation.get('clean')}/{len(checks)}")
    print(f"  outputs: {Path(payload['meta'].get('output_files', {}).get('result', 'result.json'))}, "
          f"{out_dir / 'translation_bilingual.md'}, {out_dir / 'glossary.json'}, "
          f"{out_dir / 'review_checklist.md'}")
    return {"stage": "finalized", "status": status, "limits": limits,
            "case_id": case_id, "coverage": coverage, "terms": terms}


def _detect_language(segments: list[dict[str, Any]]) -> str:
    text = " ".join(s["text"] for s in segments)
    cjk = _cjk_chars(text)
    latin = len(LATIN_WORD_RE.findall(text))
    if cjk >= 20 and latin <= max(3, cjk // 10):
        return "zh"
    if latin >= 10 and cjk == 0:
        return "en"
    if latin and cjk:
        return "mixed"
    return "unknown"


def detect_target_language(segments: list[dict[str, Any]]) -> tuple[str, str]:
    """Resolve the translation direction from the material itself.

    The topic is EN->ZH, but a case could ship a Chinese manuscript. When the
    source is predominantly Chinese the pipeline switches the target to English
    instead of demanding Chinese output from a Chinese input.
    """
    source = _detect_language(segments)
    return source, ("en" if source == "zh" else "zh")


# --------------------------------------------------------------------------- #
# check mode
# --------------------------------------------------------------------------- #
def do_check(args: argparse.Namespace, runtime: dict[str, Any]) -> int:
    out_dir = Path(runtime["out_dir"])
    work = resolve_work_dir(args.work, out_dir, existing_only=True)
    inventory = _load_inventory(work)
    result: dict[str, Any] = {"stage": "check", "case_id": runtime["case_id"] or "",
                              "work_dir": str(work)}
    if inventory is None:
        result.update({"ready": False,
                       "error": "no segment inventory under the work directory; run --prepare first"})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    segments = inventory["segments"]
    records = hybrid.load_generated(work)
    merged = hybrid.merge_generated(segments, records, required=("translation",))
    batches = _batch_status(work)
    agent_glossary, agent_status = load_agent_glossary(work)
    candidates_raw = hybrid.read_json(work / "term_candidates.json", default=None)
    candidates = (candidates_raw or {}).get("terms") if isinstance(candidates_raw, dict) else []
    provided = []
    stored = hybrid.read_json(work / "provided_glossary.json", default=None)
    if isinstance(stored, dict) and isinstance(stored.get("entries"), list):
        provided = [e for e in (_normalise_entry(item, "case_provided")
                                for item in stored["entries"]) if e]
    seed, _ = load_seed_glossary()
    glossary, glossary_status = merge_glossary(provided, agent_glossary, candidates or [], seed)
    problems: list[str] = []
    if agent_status == "missing":
        problems.append("work/generated/_glossary.json is missing: write the locked glossary first")
    if merged.get("unknown_ids"):
        problems.append("unknown seg_ids in generated files: " + ", ".join(merged["unknown_ids"][:10]))
    if merged.get("duplicates"):
        problems.append("duplicate seg_ids (keep exactly one record per seg_id): "
                        + ", ".join(merged["duplicates"][:10]))
    if merged.get("incomplete"):
        problems.append("records with an empty translation: " + ", ".join(merged["incomplete"][:10]))
    malformed = len(records) - (merged["received"] + len(merged["duplicates"])
                                + len(merged["unknown_ids"]) + len(merged["incomplete"]))
    if malformed > 0:
        problems.append(f"{malformed} record(s) have no usable seg_id")
    ready = (agent_status == "present" and not batches["pending"] and merged["coverage"] == 1.0)
    result.update({
        "ready": ready,
        "segment_count": len(segments),
        "batches_total": batches["total"],
        "batches_answered": batches["answered"],
        "batches_pending": batches["pending"],
        "records_read": len(records),
        "coverage": merged["coverage"],
        "translated": merged["received"],
        "missing_count": len(merged["missing"]),
        "missing_seg_ids": merged["missing"][:50],
        "glossary": {key: glossary_status.get(key) for key in
                     ("source", "total_entries", "agent_entries", "provided_entries")},
        "problems": problems,
        "next_actions": [] if ready else [
            "write the still-pending batch answer files listed in batches_pending",
            "each generated file is a JSON array of {seg_id, translation[, terms_used, note]}",
            "after all batches are answered run --finalize",
        ],
    })
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def _parse_args(argv: list[str] | None) -> tuple[argparse.Namespace, str]:
    parser = argparse.ArgumentParser(
        description="Reading-03 translation skill: prepare worksheets or finalize a contract result")
    parser.add_argument("--prepare", action="store_true",
                        help="write work/batches + work/glossary_worksheet.json for the agent")
    parser.add_argument("--finalize", action="store_true",
                        help="read work/generated, run checks, write result.json")
    parser.add_argument("--all", action="store_true",
                        help="prepare then finalize (default)")
    parser.add_argument("--check", action="store_true",
                        help="report what the agent has written so far, no extraction")
    parser.add_argument("--out", default=None, help="output directory (default: discovered)")
    parser.add_argument("--work", default=None, help="work directory shared by both phases")
    args = parser.parse_args(argv)
    if args.check:
        mode = "check"
    elif args.prepare and not args.finalize:
        mode = "prepare"
    elif args.finalize and not args.prepare:
        mode = "finalize"
    else:
        mode = "all"
    return args, mode


def _bootstrap(args: argparse.Namespace) -> dict[str, Any]:
    found = discover.discover()
    out_dir = hybrid.resolve_out_dir(args.out or found.get("output_dir"))
    case = contract.load_case(found.get("case_path"))
    case_id = contract.find_case_id(case, fallback=None) or ""
    declared = hybrid.declared_fields(case)
    runtime = {"found": found, "out_dir": out_dir, "case": case, "case_id": case_id,
               "declared": declared}
    hybrid.write_placeholder(out_dir, case_id=case_id, contract_fields=len(declared),
                             note="run started; this placeholder is replaced when the run finishes")
    return runtime


def _write_failure(runtime: dict[str, Any], exc: BaseException) -> None:
    traceback_tail = traceback.format_exc(limit=8)[-1500:]
    out_dir = Path(runtime.get("out_dir") or Path.cwd() / "output")
    limits = [f"run aborted by {type(exc).__name__}: {exc}"]
    try:
        payload = contract.build_result(
            case=runtime.get("case") or {}, case_id=runtime.get("case_id") or "",
            values={}, status="failed", limits=limits,
            uncertainty=["the run did not reach the finalize stage"],
            change_log=["failure result written by the top-level handler"])
        payload.setdefault("meta", {})["traceback_tail"] = traceback_tail
        contract.write_result(out_dir, payload)
        hybrid.write_json(out_dir / "run_report.json", {
            "case_id": runtime.get("case_id") or "",
            "status": "failed",
            "limits": limits,
            "traceback_tail": traceback_tail,
        })
    except Exception:
        try:
            hybrid.write_placeholder(out_dir, case_id=runtime.get("case_id") or "",
                                     note=limits[0])
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    args, mode = _parse_args(argv)
    try:
        runtime = _bootstrap(args)
    except BaseException:
        runtime = {"found": {}, "out_dir": Path.cwd() / "output", "case": {},
                   "case_id": "", "declared": []}
    stopwatch = hybrid.Stopwatch()
    try:
        if mode == "check":
            return do_check(args, runtime)
        if mode in {"prepare", "all"}:
            do_prepare(args, runtime, stopwatch)
        if mode in {"finalize", "all"}:
            do_finalize(args, runtime, stopwatch)
        return 0
    except BaseException as exc:  # never end a run without a result file
        print(f"FATAL {type(exc).__name__}: {exc}", file=sys.stderr)
        _write_failure(runtime, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
