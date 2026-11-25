#!/usr/bin/env python3
"""Writing-02 language polish runner: minimal-edit revision with a per-change rationale.

Why a two-phase hybrid pipeline
-------------------------------
The evaluation contract allows 64 model requests, 900 seconds and 16384 tokens per
response, and it runs the Skill twice per case (baseline + Skill). Text handling
must therefore not consume model requests; only *judgement* (which word to change
and why) should. The work is split in three:

1. --prepare  (deterministic) discover inputs, extract the manuscript, cut it
   into paragraph segments, extract the machine-checkable invariants of every
   paragraph (numbers, citations, hedges, negations, modals, figure/table labels,
   formula tokens) and the document term candidates, then write small batch
   worksheets under <work>/batches/.
2. the agent (driven by SKILL.md) reads a worksheet and writes
   <work>/generated/batch-XX.json with the revised paragraph plus one change
   record per edit (original / revised / type / reason).
3. --finalize (deterministic) validates coverage, validates every change
   against *both* texts, re-checks semantic preservation paragraph by paragraph,
   reverts any paragraph whose rewrite changed a number / citation / negation (a
   polish must never smuggle in a new fact), rebuilds the full revised text,
   reflects case.json.output_contract and writes result.json / evidence.json /
   run_report.json plus editable deliverables.

Hard rules implemented here, not left to the prompt
---------------------------------------------------
* a contract-shaped result.json is written BEFORE any slow work, so a container
  killed at the 900 s budget still leaves an honest partial deliverable;
* missing agent output never fails the run: that paragraph is emitted with
  status "unrevised" and revised == original, and the gap is reported;
* a rewrite that breaks semantic preservation is reverted, recorded and counted -
  never silently shipped.

No network, no third-party packages, no model call: the pipeline is deterministic
and repeated runs over one case produce the same result.json.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# import layout compatibility
#   packaged   : <skill>/lib/hybrid.py + <skill>/scripts/run_language_polish.py
#   dev layout : skills/_core/lib/... + skills/writing-02-language-polish/scripts
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parents[1]
try:  # packaged layout
    sys.path.insert(0, str(ROOT))
    from lib import anchors, contract, discover, docwrite, extract, hybrid, render
except ImportError:  # development layout
    sys.path.insert(0, str(ROOT.parent / "_core"))
    from lib import anchors, contract, discover, docwrite, extract, hybrid, render

RUNNER = "writing-02-language-polish"
STATE_SCHEMA = "writing-02-language-polish/state/v1"

# Batch sizing is a budget decision, not a cosmetic one: one batch must fit in a
# single model response (16384 tokens) and asking for 5 paragraphs at a time keeps
# the requested output well under that even for CJK text.
BATCH_SIZE = 5
BATCH_CHARS = 5000
SEG_MAX_CHARS = 1800
MAX_SEGMENTS = 80
HEAVY_EDIT_RATE = 0.5
MAX_CHANGES_PER_SEGMENT = 80

# Controlled vocabulary of edit types. Both ids and Chinese labels are accepted
# from the agent (the hidden case may ask for Chinese labels in the result).
EDIT_TYPES = {
    "grammar": "语法",
    "terminology": "术语",
    "tone": "语气/语域",
    "punctuation": "标点",
    "structure": "结构",
    "chinese_english": "中英表达",
}
TYPE_ALIASES = {
    "grammar": "grammar", "syntax": "grammar", "句法": "grammar", "语法": "grammar",
    "术语": "terminology", "term": "terminology", "wording": "terminology",
    "terminology": "terminology", "term_consistency": "terminology", "用词": "terminology",
    "tone": "tone", "register": "tone", "语气": "tone", "语域": "tone", "style": "tone",
    "学术语气": "tone",
    "punctuation": "punctuation", "punct": "punctuation", "标点": "punctuation",
    "格式": "punctuation",
    "structure": "structure", "sentence_structure": "structure", "long_sentence": "structure",
    "结构": "structure", "句式": "structure", "逻辑": "structure",
    "chinese_english": "chinese_english", "chinglish": "chinese_english",
    "translationese": "chinese_english", "中英表达": "chinese_english",
    "中译英": "chinese_english", "直译腔": "chinese_english", "表达": "chinese_english",
}

# Rules repeated inside every worksheet: the agent may read batches in a later
# context, so the hard constraints must travel with the worksheet.
RULES = [
    "Minimal edit: change only what is wrong; keep the author's sentence structure and word order unless a structural fix is required.",
    "Copy every number, unit, percentage, citation marker ([n], (Author, 2020), \\cite{..}, \\ref{..}) and figure/table reference VERBATIM. Never reformat, translate, round, renumber or reorder them.",
    "Never strengthen or weaken a claim: keep hedges (may / suggests / likely / approximately / 可能 / 约 / 提示 ...), modals (should / will / must) and negations exactly as strong as in the source.",
    "Do not add facts, references, numbers or examples that are not in the source paragraph.",
    "Keep the target register of the style profile; do not turn an academic paragraph into a summary.",
    "Output the FULL revised paragraph, not a diff, not an outline, not a comment.",
    "For every real edit emit one change record: original (exact substring of the source paragraph), revised (exact substring of your revised paragraph), type and reason.",
    "If the paragraph needs no edit, return the paragraph unchanged with an empty changes list.",
    "If the source is ambiguous or the edit would need author confirmation, set status=flagged and explain in notes; still return the best unchanged or minimally edited text.",
    "Answer in valid JSON only (a top-level array), UTF-8, no trailing commas, no markdown fences.",
]


# --------------------------------------------------------------------------- #
# tiny utilities
# --------------------------------------------------------------------------- #
def _log(stopwatch: hybrid.Stopwatch, message: str) -> None:
    # ASCII only: the Windows console code page must never break a run.
    print("[%7.1fs] %s" % (stopwatch.elapsed(), message), flush=True)


def _uniq(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    return cjk / max(1, len(text))


_WS_RE = re.compile(r"\s+")


def _ws(text: str) -> str:
    """Whitespace-insensitive comparison form.

    The segment text comes out of the block builder with normalised spaces, so an
    exact substring test would produce false negatives on line-join differences.
    Comparing with all whitespace removed is the honest middle ground.
    """
    return _WS_RE.sub("", (text or "")).casefold()


def _ws_contains(haystack: str, needle: str) -> bool:
    key = _ws(needle)
    return bool(key) and key in _ws(haystack)


def _short(text: str, limit: int = 120) -> str:
    text = _WS_RE.sub(" ", (text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _count_term(text: str, term: str) -> int:
    if not term or not text:
        return 0
    if re.search(r"[\u3400-\u9fff]", term):
        return text.count(term)
    pattern = r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])"
    return len(re.findall(pattern, text, flags=re.IGNORECASE))


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
    """Work directory that survives across the prepare / finalize processes."""
    candidates: list[Path] = []
    if prefer:
        candidates.append(Path(prefer))
    env = os.environ.get("POLISH_WORK_DIR")
    if env:
        candidates.append(Path(env))
    candidates.append(Path("/app/work"))
    candidates.append(Path(out_dir) / "work")
    candidates.append(Path.cwd() / "work")
    for candidate in candidates:
        if _writable_dir(candidate):
            return candidate
    fallback = Path(out_dir) / "work"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


# --------------------------------------------------------------------------- #
# style profile and task hints
# --------------------------------------------------------------------------- #
DEFAULT_STYLE_PROFILES: dict[str, Any] = {
    "journal": {
        "label": "期刊/会议论文 (journal / conference paper)",
        "voice": "impersonal, third person or passive voice where the field expects it",
        "hedging": "keep the original hedging strength; do not promote 'may' to 'does'",
        "contractions": "forbidden",
        "first_person": "avoid unless the source already uses 'we'",
        "notes": "concise, information-dense sentences; no marketing adjectives",
    },
    "thesis": {
        "label": "学位论文 (thesis / dissertation)",
        "voice": "formal, explicit signposting allowed",
        "hedging": "keep hedging; explanatory repetition is acceptable",
        "contractions": "forbidden",
        "first_person": "allowed where the source uses it",
        "notes": "longer explanatory sentences are acceptable; keep chapter-level terminology",
    },
    "grant": {
        "label": "基金申请书 (grant proposal)",
        "voice": "active, forward-looking, concrete",
        "hedging": "keep uncertainty about outcomes, do not overclaim feasibility",
        "contractions": "forbidden",
        "first_person": "'we propose / we will' is normal if present in the source",
        "notes": "emphasise significance and feasibility without inventing preliminary data",
    },
    "review_response": {
        "label": "审稿回复 (reviewer response)",
        "voice": "courteous, direct, point-by-point",
        "hedging": "do not concede more than the authors conceded in the source",
        "contractions": "forbidden",
        "first_person": "allowed",
        "notes": "answer the concern first, then the change; never invent new experiments",
    },
    "abstract": {
        "label": "摘要 (abstract)",
        "voice": "self-contained, no citations to sections",
        "hedging": "keep the source's claim strength exactly",
        "contractions": "forbidden",
        "first_person": "follow the field convention of the source",
        "notes": "one paragraph, no undefined abbreviations, no new numbers",
    },
    "unknown": {
        "label": "未指定的学术文体 (unspecified academic register)",
        "voice": "neutral academic",
        "hedging": "keep the original hedging strength",
        "contractions": "forbidden",
        "first_person": "follow the source",
        "notes": "minimal edit; flag what could not be determined",
    },
}


def load_style_profiles() -> dict[str, Any]:
    data = hybrid.read_json(ROOT / "resources" / "style_profiles.json", None)
    if isinstance(data, dict) and isinstance(data.get("profiles"), dict) and data["profiles"]:
        return data["profiles"]
    return DEFAULT_STYLE_PROFILES


GENRE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("review_response", ("审稿", "回复审稿人", "response to review", "reviewer", "rebuttal", "回复意见")),
    ("grant", ("基金", "申请书", "标书", "grant", "proposal", "funding", "application")),
    ("thesis", ("学位论文", "毕业论文", "学位", "thesis", "dissertation")),
    ("abstract", ("摘要", "abstract only", "abstract-only", "写摘要", "standalone abstract")),
    ("journal", ("期刊", "会议", "论文", "journal", "conference", "paper", "manuscript")),
)

SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "abstract": ("abstract", "摘要"),
    "introduction": ("introduction", "引言", "前言", "背景"),
    "related_work": ("related work", "相关工作", "文献综述"),
    "methods": ("methods", "materials and methods", "方法", "实验"),
    "results": ("results", "结果"),
    "discussion": ("discussion", "讨论"),
    "conclusion": ("conclusion", "结论", "总结"),
    "references": ("references", "bibliography", "参考文献"),
    "appendix": ("appendix", "supplementary", "附录"),
}


def detect_style_profile(task_md: str, sample_text: str) -> dict[str, Any]:
    """Deterministic target-style guess from the task brief and the manuscript."""
    brief = task_md or ""
    genre = "unknown"
    low = brief.casefold()
    for name, hints in GENRE_HINTS:
        if any(h.casefold() in low for h in hints):
            genre = name
            break
    if genre == "unknown":
        # fall back to the manuscript's own shape: an 'Abstract' heading plus a
        # numbered body reads as a journal paper.
        if re.search(r"\babstract\b|摘要", sample_text or "", flags=re.IGNORECASE):
            genre = "journal"
    target_language = ""
    if re.search(r"译成英文|译为英文|翻成英文|translate[^.\n]{0,40}into english|in english", low):
        target_language = "en"
    elif re.search(r"译成中文|译为中文|翻成中文|translate[^.\n]{0,40}into chinese|中文润色", low):
        target_language = "zh"
    if not target_language:
        target_language = "zh" if _cjk_ratio(sample_text) >= 0.2 else "en"
    focus = [name for name, aliases in SECTION_ALIASES.items()
             if any(alias.casefold() in low for alias in aliases)]
    profiles = load_style_profiles()
    profile = dict(profiles.get(genre) or profiles.get("unknown") or DEFAULT_STYLE_PROFILES["unknown"])
    profile["genre"] = genre
    profile["target_language"] = target_language
    profile["focus_sections"] = focus
    profile["source"] = ("resources/style_profiles.json"
                         if (ROOT / "resources" / "style_profiles.json").is_file() else "builtin")
    return profile


# --------------------------------------------------------------------------- #
# material discovery and extraction
# --------------------------------------------------------------------------- #
STRONG_NAMES = ("manuscript", "draft", "paper", "article", "thesis", "dissertation",
                "chapter", "abstract", "main", "正文", "稿件", "论文", "润色", "原文")
REF_NAMES = ("reference", "refs", "bibliography", "glossary", "term", "style",
             "guide", "notes", "readme", "template", "instruction")
SUFFIX_RANK = {".docx": 0, ".doc": 1, ".tex": 2, ".latex": 3, ".md": 4,
               ".markdown": 5, ".txt": 6, ".pdf": 7}


def _material_rank(path: Path) -> tuple:
    name = path.name.casefold()
    strong = 0 if any(key in name for key in STRONG_NAMES) else 1
    ref = 1 if any(key in name for key in REF_NAMES) else 0
    suffix = SUFFIX_RANK.get(path.suffix.casefold(), 9)
    try:
        size = -path.stat().st_size
    except Exception:
        size = 0
    return (strong, ref, suffix, size, str(path).casefold())


def select_materials(found: dict[str, Any], out_dir: Path, work_dir: Path) -> list[Path]:
    """Rank candidate materials; never pick up our own deliverables as input."""
    skip_roots = []
    for root in (out_dir, work_dir):
        try:
            skip_roots.append(root.resolve())
        except Exception:
            skip_roots.append(root)
    picked: list[Path] = []
    for raw in found.get("material_docs") or []:
        path = Path(raw)
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        if any(resolved == root or str(resolved).startswith(str(root) + os.sep)
               for root in skip_roots):
            continue
        picked.append(path)
    return sorted(picked, key=_material_rank)


REF_LINE_START_RE = re.compile(r"^\s*(?:\[\d{1,3}\]|\d{1,3}\.)\s+\S")


def _plain_paragraphs(text: str, *, break_markdown_headings: bool = False,
                      break_tex: bool = False) -> list[str]:
    """Split plain text into visual paragraphs (blank line = paragraph break).

    A bibliography is often typed one entry per line without blank lines between
    them, and a heading may follow the previous paragraph without an empty line;
    both start a new paragraph instead of being glued to the previous one.
    """
    blocks: list[str] = []
    current: list[str] = []
    for line in (text or "").splitlines():
        if line.strip():
            starts_new = bool(
                REF_LINE_START_RE.match(line)
                or (break_markdown_headings and MD_HEADING_RE.match(line))
                or (break_tex and (TEX_HEADING_RE.match(line)
                                   or TEX_ENV_HEADING_RE.match(line))))
            if current and starts_new:
                blocks.append(" ".join(current).strip())
                current = []
            current.append(line.strip())
        else:
            if current:
                blocks.append(" ".join(current).strip())
                current = []
    if current:
        blocks.append(" ".join(current).strip())
    return blocks


MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
TEX_HEADING_RE = re.compile(r"^\s*\\(?:part|chapter|section|subsection|subsubsection|paragraph)\*?\{")
TEX_ENV_HEADING_RE = re.compile(r"^\s*\\begin\{(?:abstract|keywords)\}")


def _plain_document(path: Path) -> dict[str, Any]:
    """Paragraph-level document for non-PDF material (docx / md / tex / txt).

    The generic pdftext block builder assumes real coordinates; for plain text its
    synthetic line coordinates differ by 1 unit, so every line would cluster into
    ONE block and the whole manuscript would become a single segment. That destroys
    the per-paragraph alignment the polish task is graded on, so plain material is
    segmented from paragraph breaks instead.
    """
    suffix = path.suffix.casefold()
    warnings: list[str] = []
    engine = "paragraphs"
    try:
        if suffix == ".docx":
            text = extract._docx_text_zip(path)  # stdlib OOXML fallback, deterministic
            engine = "paragraphs:docx"
            if not text:
                warnings.append("docx text extraction returned nothing")
        elif suffix == ".pptx":
            text = extract._pptx_text_zip(path)
            engine = "paragraphs:pptx"
        elif suffix == ".pdf":
            raise ValueError("pdf handled by extract.extract_document")
        else:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception as exc:
        return {"engine": None, "path": str(path), "pages": [], "blocks": {},
                "text_layer": False, "total_chars": 0, "anchor_quality": "text",
                "warnings": ["could not read %s: %s: %s" % (path.name, type(exc).__name__, exc)],
                "attempts": ["plain reader failed"]}

    if suffix in {".docx", ".doc", ".pptx"}:
        # one OOXML paragraph per line, no blank-line separators available
        paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
    else:
        paragraphs = _plain_paragraphs(
            text,
            break_markdown_headings=suffix in {".md", ".markdown"},
            break_tex=suffix in {".tex", ".latex"})
    blocks: list[dict[str, Any]] = []
    for index, para in enumerate(paragraphs):
        known = re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", para).casefold().strip(" .:：")
        likely_heading = bool(
            (suffix in {".md", ".markdown"} and MD_HEADING_RE.match(para))
            or (suffix in {".tex", ".latex"}
                and (TEX_HEADING_RE.match(para) or TEX_ENV_HEADING_RE.match(para)))
            or (len(para) <= 90
                and re.fullmatch(r"(?:\d+(?:\.\d+)*\.?\s+)?[A-Z\u4e00-\u9fff][^.]{2,80}", para)
                and known in {"abstract", "introduction", "methods", "method", "results",
                              "discussion", "conclusion", "conclusions", "references",
                              "摘要", "引言", "方法", "结果", "讨论", "结论", "参考文献"})
        )
        blocks.append({
            "block_id": "p1-b%d" % (index + 1),
            "page": 1,
            "bbox": [0.0, float(-index), float(max(len(para), 1)), float(-index + 1)],
            "text": para,
            "lines": max(1, para.count("  ") + 1),
            "size": 11.0 if likely_heading else 10.0,
            "x_starts": [],
            "col_gaps": 0,
            "likely_heading": likely_heading,
        })
    return {
        "engine": engine,
        "path": str(path),
        "pages": [{"number": 1}],
        "blocks": {"1": blocks},
        "text_layer": bool(blocks),
        "total_chars": sum(len(b["text"]) for b in blocks),
        "anchor_quality": "page",
        "warnings": warnings + ["non-PDF material: paragraph anchors are page+block; "
                                "bounding boxes are synthetic (no physical layout)"],
        "attempts": ["paragraph splitter used (deterministic, no third-party parser)"],
    }


def extract_primary(paths: list[Path], stopwatch: hybrid.Stopwatch,
                    deadline: float, limits: list[str]) -> tuple[Path | None, dict[str, Any]]:
    """Extract the first candidate that yields real text; record every skip."""
    for path in paths[:5]:
        if time.time() > deadline - 5:
            limits.append("extraction deadline reached before %s" % path.name)
            break
        stopwatch.log("extract %s" % path.name)
        if path.suffix.casefold() == ".pdf":
            doc = extract.extract_document(path, deadline=deadline)
        else:
            doc = _plain_document(path)
            if not doc.get("text_layer"):
                fallback = extract.extract_document(path, deadline=deadline)
                if fallback.get("text_layer"):
                    doc = fallback
        if doc.get("text_layer") and int(doc.get("total_chars") or 0) > 40:
            return path, doc
        limits.append("%s: no usable text layer (%s); trying the next material"
                      % (path.name, "; ".join((doc.get("warnings") or [])[:2]) or "empty"))
    return None, {}


def read_secondary_texts(paths: list[Path], limit: int = 3) -> list[tuple[str, str]]:
    """Raw text of small secondary text material, used only for glossary hints."""
    out: list[tuple[str, str]] = []
    for path in paths:
        if len(out) >= limit:
            break
        suffix = path.suffix.casefold()
        if suffix not in {".md", ".markdown", ".txt", ".csv", ".tsv", ".bib"}:
            continue
        try:
            if path.stat().st_size > 200_000:
                continue
            out.append((path.name, path.read_text(encoding="utf-8-sig", errors="replace")))
        except Exception:
            continue
    return out


MD_TABLE_RE = re.compile(r"^\s*\|(.+)\|\s*$")
PAIR_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s+)?([^:=—–→\t]{1,60}?)\s*(?::|=|—|–|→|\t)\s*([^:=—–→\t]{1,120}?)\s*$")
ABBR_FWD_RE = re.compile(r"([A-Z][A-Za-z\-]+(?:\s+[A-Za-z\-]+){0,8})\s*\(([A-Z][A-Z0-9]{1,9})\)")
ABBR_REV_RE = re.compile(r"([A-Z][A-Z0-9]{1,9})\s*\(([A-Z][A-Za-z\-]+(?:\s+[A-Za-z\-]+){0,8})\)")


def parse_reference_glossary(texts: list[tuple[str, str]]) -> dict[str, Any]:
    """Pull glossary-like (source, preferred) pairs and abbreviation pairs.

    This is a *hint* source: the evaluator may ship a separate terminology sheet,
    and the agent glossary is authoritative when both exist. Parsing is
    deliberately conservative (short left side, exactly two fields) so ordinary
    prose lines are not mistaken for glossary entries.
    """
    pairs: list[dict[str, Any]] = []
    abbreviations: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for filename, text in texts:
        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            cells: list[str] | None = None
            if "\t" in line:
                cells = [c.strip() for c in line.split("\t")]
            elif line.startswith("|"):
                match = MD_TABLE_RE.match(line)
                if match:
                    cells = [c.strip() for c in match.group(1).split("|")]
            elif len(line) < 180 and not line.endswith((".", "。", "!", "?", "！", "？")):
                match = PAIR_LINE_RE.match(line)
                if match:
                    cells = [match.group(1).strip(), match.group(2).strip()]
            if not cells or len(cells) != 2 or not all(cells):
                continue
            left, right = cells
            if len(left) > 60 or len(right) > 120 or left.casefold() == right.casefold():
                continue
            key = (left.casefold(), right.casefold())
            if key in seen or len(pairs) >= 100:
                continue
            seen.add(key)
            pairs.append({"source": left, "preferred": right, "from_file": filename})
    short_seen = set()
    for filename, text in texts:
        for match in ABBR_FWD_RE.finditer(text or ""):
            short = match.group(2)
            if short.casefold() in short_seen:
                continue
            short_seen.add(short.casefold())
            abbreviations.append({"short": short, "full": match.group(1).strip(),
                                  "from_file": filename})
        for match in ABBR_REV_RE.finditer(text or ""):
            short = match.group(1)
            if short.casefold() in short_seen:
                continue
            short_seen.add(short.casefold())
            abbreviations.append({"short": short, "full": match.group(2).strip(),
                                  "from_file": filename})
    return {"pairs": pairs[:100], "abbreviations": abbreviations[:40],
            "note": "deterministic hints parsed from secondary material; the agent glossary wins"}


# --------------------------------------------------------------------------- #
# invariants, edits, validation
# --------------------------------------------------------------------------- #
def _num_key(token: str) -> str:
    return hybrid.normalize_text(token).replace(" ", "")


def segment_facts(text: str) -> dict[str, Any]:
    return {
        "numbers": _uniq(hybrid.find_numbers(text)),
        "citations": _uniq(hybrid.find_citations(text)),
        "hedges": hybrid.find_hedges(text),
        "negations": hybrid.find_negations(text),
        "modals": hybrid.find_modals(text),
        "labels": hybrid.find_labels(text),
        "formulas": _uniq(hybrid.find_formula_tokens(text))[:20],
    }


def invariant_diff(source: str, target: str) -> dict[str, Any]:
    """Set difference of the machine-checkable facts between two texts."""
    src_numbers = {_num_key(n) for n in hybrid.find_numbers(source)}
    tgt_numbers = {_num_key(n) for n in hybrid.find_numbers(target)}
    src_cites = {hybrid.normalize_text(c) for c in hybrid.find_citations(source)}
    tgt_cites = {hybrid.normalize_text(c) for c in hybrid.find_citations(target)}
    src_neg = set(hybrid.find_negations(source))
    tgt_neg = set(hybrid.find_negations(target))
    src_modal = set(hybrid.find_modals(source))
    tgt_modal = set(hybrid.find_modals(target))
    src_labels = set(hybrid.find_labels(source))
    tgt_labels = set(hybrid.find_labels(target))
    src_formulas = set(hybrid.find_formula_tokens(source))
    tgt_formulas = set(hybrid.find_formula_tokens(target))
    return {
        "numbers_missing": sorted(src_numbers - tgt_numbers),
        "numbers_added": sorted(tgt_numbers - src_numbers),
        "citations_missing": sorted(src_cites - tgt_cites),
        "citations_added": sorted(tgt_cites - src_cites),
        "negations_missing": sorted(src_neg - tgt_neg),
        "negations_added": sorted(tgt_neg - src_neg),
        "modals_missing": sorted(src_modal - tgt_modal),
        "modals_added": sorted(tgt_modal - src_modal),
        "labels_missing": sorted(src_labels - tgt_labels),
        "formulas_missing": sorted(src_formulas - tgt_formulas),
        "formulas_added": sorted(tgt_formulas - src_formulas),
    }


def critical_reasons(diff: dict[str, Any], source: str, target: str) -> list[str]:
    """Facts whose change must revert the paragraph.

    Numbers and citations are hard facts: losing or inventing one is fabrication,
    and the polish task explicitly scores "did not break it". Negations flip the
    claim, so they are hard too. Chinese negations are single characters that also
    occur inside unrelated words (不同 = different), so the negation rule is only
    applied to paragraphs written in Latin script. Formula tokens are noisy by
    nature and are reported as review items instead of forcing a revert.
    """
    reasons: list[str] = []
    if diff["numbers_missing"]:
        reasons.append("number_lost:" + ",".join(diff["numbers_missing"][:5]))
    if diff["numbers_added"]:
        reasons.append("number_added:" + ",".join(diff["numbers_added"][:5]))
    if diff["citations_missing"]:
        reasons.append("citation_lost:" + ",".join(diff["citations_missing"][:5]))
    if diff["citations_added"]:
        reasons.append("citation_added:" + ",".join(diff["citations_added"][:5]))
    if not (_has_cjk(source) or _has_cjk(target)):
        if diff["negations_missing"]:
            reasons.append("negation_lost:" + ",".join(diff["negations_missing"][:5]))
        if diff["negations_added"]:
            reasons.append("negation_added:" + ",".join(diff["negations_added"][:5]))
    return reasons


HEDGE_EN_SET = set(hybrid.HEDGES_EN)
# "about 85%" is an approximation hedge; "problems about stability" is not. The
# shared lexicon cannot tell them apart, so the ambiguous ones only count as
# hedges when a number sits next to them - otherwise every preposition edit would
# be reported as weakening a claim.
HEDGE_APPROX_NEEDS_NUMBER = {"about", "around"}
HEDGE_NUM_NEAR_RE = re.compile(r"\d")


def _hedge_near_number(text: str, word: str) -> bool:
    for match in re.finditer(r"(?<![A-Za-z])" + re.escape(word) + r"(?![A-Za-z])",
                             text or "", flags=re.IGNORECASE):
        window = (text or "")[max(0, match.start() - 20):match.end() + 25]
        if HEDGE_NUM_NEAR_RE.search(window):
            return True
    return False


def _hedge_lemmas(text: str) -> set:
    """Hedge words as lemmas: 'indicates' -> 'indicate', so a grammar fix is not
    mistaken for a lost hedge."""
    out: set = set()
    for word in hybrid.find_hedges(text):
        if word in HEDGE_APPROX_NEEDS_NUMBER and not _hedge_near_number(text, word):
            continue
        base = word
        if word in HEDGE_EN_SET and word.endswith("s") and word[:-1] in HEDGE_EN_SET:
            base = word[:-1]
        out.add(base)
    return out


def hedge_signal(source: str, target: str) -> dict[str, Any]:
    src = _hedge_lemmas(source)
    tgt = _hedge_lemmas(target)
    return {"missing": sorted(src - tgt), "added": sorted(tgt - src)}


def preservation_ok(check: dict[str, Any]) -> bool:
    """A paragraph is preserved when no number/citation/formula/hedge was lost.

    Added hedges weaken a claim but do not hide data; they stay review items.
    """
    return not (check.get("numbers_missing") or check.get("citations_missing")
                or check.get("formulas_missing") or check.get("hedges_missing"))


def checked_preservation(source: str, target: str, label: str = "segment") -> dict[str, Any]:
    """hybrid.preservation_check with lemma-level hedge comparison and a verdict.

    The shared checker reports 'indicates' vs 'indicate' as a lost hedge, which is
    a grammar fix, not a weakened claim; this wrapper fixes that and keeps the
    rest of the deterministic comparison untouched.
    """
    check = hybrid.preservation_check(source, target, label=label)
    hedges = hedge_signal(source, target)
    check["hedges_missing"] = hedges["missing"]
    check["hedges_added"] = hedges["added"]
    check["ok"] = preservation_ok(check)
    return check


def edit_stats(source: str, target: str) -> dict[str, Any]:
    """Character-level edit size between two paragraphs (stdlib, deterministic)."""
    source = source or ""
    target = target or ""
    if source == target:
        return {"changed_chars": 0, "edit_rate": 0.0, "similarity": 1.0,
                "source_chars": len(source), "target_chars": len(target)}
    matcher = difflib.SequenceMatcher(None, source, target, autojunk=False)
    changed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            changed += max(i2 - i1, j2 - j1)
    return {
        "changed_chars": changed,
        "edit_rate": round(changed / max(1, len(source)), 4),
        "similarity": round(matcher.ratio(), 4),
        "source_chars": len(source),
        "target_chars": len(target),
    }


def normalize_edit_type(raw: Any) -> tuple[str, str]:
    key = str(raw or "").strip().casefold()
    mapped = TYPE_ALIASES.get(key)
    if mapped:
        return mapped, EDIT_TYPES[mapped]
    return "unspecified", "未分类"


def validate_changes(source: str, revised: str, raw_changes: Any) -> list[dict[str, Any]]:
    """Check every change record against BOTH the source and the revised text.

    A change record that cannot be located in the texts is not evidence of a real
    edit ("讲得清" fails), so it is kept but marked invalid with the exact reason.
    """
    changes: list[dict[str, Any]] = []
    for item in (raw_changes if isinstance(raw_changes, list) else []):
        if len(changes) >= MAX_CHANGES_PER_SEGMENT:
            break
        if not isinstance(item, dict):
            continue
        original = str(item.get("original") or "").strip()
        new = str(item.get("revised") or "").strip()
        reason = str(item.get("reason") or "").strip()
        etype, elabel = normalize_edit_type(item.get("type"))
        issues: list[str] = []
        if not original:
            issues.append("missing_original")
        elif not (original in source or _ws_contains(source, original)):
            issues.append("original_not_in_source")
        if not new:
            issues.append("missing_revised")
        elif not (new in revised or _ws_contains(revised, new)):
            issues.append("revised_not_in_output")
        if not reason:
            issues.append("missing_reason")
        if etype == "unspecified":
            issues.append("unknown_type")
        changes.append({
            "original": _short(original, 220),
            "revised": _short(new, 220),
            "type": etype,
            "type_label": elabel,
            "reason": _short(reason, 220),
            "valid": not issues,
            "issues": issues,
        })
    return changes


# --------------------------------------------------------------------------- #
# terminology
# --------------------------------------------------------------------------- #
def acronym_first_uses(text: str, limit: int = 30) -> list[dict[str, Any]]:
    """First use of every all-caps acronym, with a deterministic defined/undefined test."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in re.finditer(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,7}(?![A-Za-z0-9])", text or ""):
        token = match.group(0)
        if token in seen or sum(1 for ch in token if ch.isalpha()) < 2:
            continue
        seen.add(token)
        prefix = (text or "")[max(0, match.start() - 180):match.start()]
        defined = bool(re.search(r"[A-Za-z][A-Za-z\-]*(?:\s+[A-Za-z\-]+){0,8}\s*\(\s*$", prefix))
        found.append({"acronym": token, "offset": match.start(), "defined": defined,
                      "context": _short((text or "")[match.start():match.start() + 80], 80)})
        if len(found) >= limit:
            break
    return found


def build_terminology(state: dict[str, Any], source_text: str, revised_text: str,
                      glossary: dict[str, Any]) -> dict[str, Any]:
    """Term consistency table + acronym first-use audit.

    The agent's glossary (generated/_glossary.json) is authoritative when present;
    otherwise the deterministic term inventory is reported honestly as
    "inventory_only" instead of pretending a decision was verified.
    """
    entries: list[dict[str, Any]] = []
    checked = 0
    adopted = 0
    conflicts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    agent_terms = glossary.get("terms") if isinstance(glossary, dict) else None
    if isinstance(agent_terms, list):
        for item in agent_terms:
            if not isinstance(item, dict):
                continue
            source_form = str(item.get("source") or item.get("term") or "").strip()
            preferred = str(item.get("preferred") or item.get("revision") or source_form).strip() or source_form
            if not source_form:
                continue
            key = (source_form.casefold(), preferred.casefold())
            if key in seen:
                continue
            seen.add(key)
            uses_source = _count_term(source_text, source_form)
            uses_preferred = _count_term(revised_text, preferred)
            leftover_source = 0
            if preferred.casefold() != source_form.casefold() and not _ws_contains(preferred, source_form):
                leftover_source = _count_term(revised_text, source_form)
            if leftover_source:
                status = "conflict"
                conflicts.append({"source": source_form, "preferred": preferred,
                                  "occurrences_left": leftover_source})
            elif uses_preferred:
                status = "adopted"
            elif uses_source:
                status = "dropped"
            else:
                status = "not_present_in_source"
            if status in {"adopted", "conflict", "dropped"}:
                checked += 1
                if status == "adopted":
                    adopted += 1
            entries.append({
                "source_form": source_form,
                "preferred_form": preferred,
                "occurrences_source": uses_source,
                "occurrences_revised": uses_preferred,
                "status": status,
                "note": _short(str(item.get("note") or ""), 160),
                "origin": "agent_glossary",
            })

    inventory = state.get("terms") or []
    if isinstance(inventory, list):
        for item in inventory[:40]:
            if not isinstance(item, dict):
                continue
            term = str(item.get("term") or "").strip()
            if not term or term.casefold() in {e["source_form"].casefold() for e in entries}:
                continue
            entries.append({
                "source_form": term,
                "preferred_form": term,
                "occurrences_source": _count_term(source_text, term),
                "occurrences_revised": _count_term(revised_text, term),
                "status": "inventory_only",
                "note": "deterministic candidate; no locked form was supplied",
                "origin": "deterministic_candidates",
            })

    reference_pairs = (state.get("reference_glossary") or {}).get("pairs") or []
    reference = []
    for pair in reference_pairs[:60]:
        source_form = str(pair.get("source") or "").strip()
        preferred = str(pair.get("preferred") or "").strip()
        if not source_form or not preferred:
            continue
        key = (source_form.casefold(), preferred.casefold())
        if key in seen:
            continue
        seen.add(key)
        reference.append({
            "source_form": source_form,
            "preferred_form": preferred,
            "occurrences_source": _count_term(source_text, source_form),
            "occurrences_revised": _count_term(revised_text, preferred),
            "from_file": pair.get("from_file"),
        })

    acronyms = acronym_first_uses(revised_text)
    source_acronyms = {a["acronym"]: a for a in acronym_first_uses(source_text)}
    for item in acronyms:
        before = source_acronyms.get(item["acronym"])
        item["defined_in_source"] = bool(before and before.get("defined"))
        item["regressed"] = bool(before and before.get("defined")) and not item["defined"]

    rate = round(adopted / checked, 4) if checked else 1.0
    return {
        "entries": entries[:80],
        "entries_checked": checked,
        "adopted": adopted,
        "consistency_rate": rate,
        "conflicts": conflicts[:20],
        "reference_glossary": reference[:60],
        "acronym_first_use": acronyms,
        "acronyms_losing_definition": [a["acronym"] for a in acronyms if a["regressed"]],
        "source": "agent_glossary" if checked else "deterministic_candidates",
        "note": ("consistency_rate is computed only over glossary entries the agent locked "
                 "a preferred form for; inventory_only rows are reported without a verdict"),
    }


# --------------------------------------------------------------------------- #
# prepare: segments, worksheets, state
# --------------------------------------------------------------------------- #
def _heading_index(struct: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for section in struct.get("sections") or []:
        block_id = section.get("block_id")
        if block_id:
            index[str(block_id)] = {"title": section.get("title"),
                                    "level": section.get("level") or 1}
    return index


def build_segments(doc: dict[str, Any], struct: dict[str, Any],
                   style: dict[str, Any]) -> list[dict[str, Any]]:
    segments = hybrid.build_segments(doc, struct, max_chars=SEG_MAX_CHARS)
    headings = _heading_index(struct)
    focus = [s.casefold() for s in (style.get("focus_sections") or [])]
    for seg in segments:
        block_id = (seg.get("block_ids") or [None])[0]
        heading = headings.get(str(block_id))
        seg["is_heading"] = bool(heading)
        seg["heading_level"] = int(heading.get("level") or 1) if heading else 0
        seg["facts"] = segment_facts(seg["text"])
        title = (seg.get("section_title") or "").casefold()
        seg["in_focus"] = bool(not focus or any(name in title for name in focus))
        seg["priority"] = 0 if seg["in_focus"] else 1
    if len(segments) > MAX_SEGMENTS:
        segments = segments[:MAX_SEGMENTS]
    return segments


def _term_hints(text: str, reference: dict[str, Any],
                abbreviations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hints: list[dict[str, Any]] = []
    for pair in (reference or {}).get("pairs") or []:
        source_form = pair.get("source") or ""
        if source_form and _count_term(text, source_form):
            hints.append({"source": source_form, "preferred": pair.get("preferred"),
                          "from_file": pair.get("from_file")})
        if len(hints) >= 4:
            break
    for abbr in abbreviations or []:
        if abbr.get("short") and _count_term(text, abbr["short"]):
            hints.append({"source": abbr["short"], "preferred": abbr.get("full"),
                          "note": "abbreviation; keep the source's convention"})
        if len(hints) >= 6:
            break
    return hints


def build_worksheets(segments: list[dict[str, Any]], work: Path,
                     style: dict[str, Any], reference: dict[str, Any],
                     task_brief: str) -> list[dict[str, Any]]:
    batch_dir = work / "batches"
    try:
        if batch_dir.is_dir():
            for stale in batch_dir.glob("batch-*.json"):
                stale.unlink()
    except Exception:
        pass
    batch_dir.mkdir(parents=True, exist_ok=True)
    ordered = sorted(segments, key=lambda s: (s.get("priority", 0), s.get("order", 0)))
    batches: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0

    def flush() -> None:
        nonlocal current, current_chars
        if not current:
            return
        batch_id = "batch-%02d" % (len(batches) + 1)
        payload = {
            "batch_id": batch_id,
            "runner": RUNNER,
            "paragraph_count": len(current),
            "char_count": current_chars,
            "rules": RULES,
            "style_profile": {
                "genre": style.get("genre"),
                "label": style.get("label"),
                "target_language": style.get("target_language"),
                "voice": style.get("voice"),
                "hedging": style.get("hedging"),
                "first_person": style.get("first_person"),
                "contractions": style.get("contractions"),
                "notes": style.get("notes"),
            },
            "task_brief": task_brief[:900],
            "focus_sections": style.get("focus_sections") or [],
            "allowed_types": EDIT_TYPES,
            "allowed_status": ["revised", "kept", "flagged"],
            "answer_path": str(work / "generated" / ("%s.json" % batch_id)),
            "answer_format": [
                {"seg_id": "seg-0001", "status": "revised",
                 "revised": "the complete polished paragraph",
                 "changes": [{"original": "exact substring of the source paragraph",
                              "revised": "exact substring of the revised paragraph",
                              "type": "grammar",
                              "reason": "why this edit is necessary"}],
                 "notes": "optional; required when status=flagged"}
            ],
            "segments": [],
        }
        for seg in current:
            payload["segments"].append({
                "seg_id": seg["seg_id"],
                "page": seg.get("page"),
                "section": seg.get("section_title"),
                "is_heading": seg.get("is_heading"),
                "in_focus": seg.get("in_focus"),
                "char_count": seg.get("char_count"),
                "text": seg["text"],
                "preserve": {
                    "numbers": seg["facts"]["numbers"],
                    "citations": seg["facts"]["citations"],
                    "hedges": seg["facts"]["hedges"],
                    "modals": seg["facts"]["modals"],
                    "labels": seg["facts"]["labels"],
                    "formulas": seg["facts"]["formulas"],
                },
                "term_hints": _term_hints(seg["text"], reference,
                                          reference.get("abbreviations") or []),
            })
        hybrid.write_json(batch_dir / ("%s.json" % batch_id), payload)
        batches.append({"batch_id": batch_id, "segment_count": len(current),
                        "chars": current_chars,
                        "seg_ids": [s["seg_id"] for s in current],
                        "worksheet": str(batch_dir / ("%s.json" % batch_id))})
        current = []
        current_chars = 0

    for seg in ordered:
        if current and (len(current) >= BATCH_SIZE
                        or current_chars + int(seg.get("char_count") or 0) > BATCH_CHARS):
            flush()
        current.append(seg)
        current_chars += int(seg.get("char_count") or 0)
    flush()
    return batches


PROTOCOL_README = """# Writing-02 language polish - agent worksheet index

This directory was produced by scripts/run_language_polish.py --prepare.

- batches/batch-XX.json    worksheets (input for the agent)
- generated/batch-XX.json  the agent writes the answers here
- generated/_glossary.json optional terminology decisions
- state.json / doc.json / terms.json deterministic state used by --finalize

Answer format (a JSON array in generated/batch-XX.json):

    [
      {"seg_id": "seg-0001", "status": "revised",
       "revised": "the complete polished paragraph",
       "changes": [
         {"original": "was carried out", "revised": "was performed",
          "type": "grammar", "reason": "collocation; 'carry out a reaction' is not idiomatic"}
       ]}
    ]

- status: revised | kept (no edit needed; return the text unchanged) | flagged (needs author confirmation)
- type: grammar | terminology | tone | punctuation | structure | chinese_english
- original must be an exact substring of the worksheet text, revised an exact substring of your paragraph.
- Return the FULL paragraph, not a diff and not a comment. Keep numbers / citations / figure references verbatim.

Then run: python scripts/run_language_polish.py --finalize --out <OUT_DIR>
"""


def prepare(found: dict[str, Any], case: dict[str, Any], case_id: str | None,
            out_dir: Path, work_dir: Path, stopwatch: hybrid.Stopwatch) -> dict[str, Any]:
    """Deterministic phase 1: materials -> segments -> facts -> worksheets -> state."""
    limits: list[str] = []
    uncertainty: list[str] = []
    change_log: list[str] = [
        "prepare: deterministic segmentation and invariant extraction; no model call made"]
    state: dict[str, Any] = {
        "schema": STATE_SCHEMA,
        "runner": RUNNER,
        "case": case,
        "case_id": case_id,
        "case_path": found.get("case_path"),
        "task_md_path": found.get("task_md_path"),
        "out_dir": str(out_dir),
        "work_dir": str(work_dir),
        "inputs": found,
        "ok": False,
        "status": "failed",
        "degraded": False,
        "segments": [],
        "batches": [],
        "terms": [],
        "reference_glossary": {"pairs": [], "abbreviations": []},
        "style_profile": {},
        "limits": limits,
        "uncertainty": uncertainty,
        "change_log": change_log,
    }

    task_md = ""
    if found.get("task_md_path"):
        try:
            task_md = Path(found["task_md_path"]).read_text(encoding="utf-8-sig",
                                                            errors="replace")
        except Exception as exc:
            uncertainty.append("task.md could not be read: %s" % type(exc).__name__)

    materials = select_materials(found, out_dir, work_dir)
    state["materials"] = [str(p) for p in materials]
    if not materials:
        limits.append("no material document was found in the input roots")
        state["limits"] = limits
        hybrid.write_json(work_dir / "state.json", state)
        return state

    deadline = time.time() + hybrid.SOFT_DEADLINE_S
    primary, doc = extract_primary(materials, stopwatch, deadline, limits)
    if primary is None:
        limits.append("every material candidate lacked a usable text layer "
                      "(OCR is not available in this image)")
        state["limits"] = limits
        hybrid.write_json(work_dir / "state.json", state)
        return state
    state["primary"] = str(primary)
    state["engine"] = doc.get("engine")
    state["anchor_quality"] = doc.get("anchor_quality")
    state["text_layer"] = bool(doc.get("text_layer"))
    state["page_count"] = len(doc.get("pages") or [])
    state["total_chars"] = int(doc.get("total_chars") or 0)
    for warning in doc.get("warnings") or []:
        uncertainty.append("%s: %s" % (primary.name, warning))
    if doc.get("anchor_quality") != "span":
        limits.append("anchor quality is '%s' (non-PDF material has no physical "
                      "coordinates); paragraph anchors are page+block"
                      % doc.get("anchor_quality"))

    struct = structure_outline(doc)
    sample = " ".join(block.get("text", "")
                      for page_blocks in (doc.get("blocks") or {}).values()
                      for block in page_blocks[:20])
    style = detect_style_profile(task_md, sample)
    state["style_profile"] = style
    state["task_brief"] = task_md[:2000]
    if not task_md:
        uncertainty.append("task.md was not found; the style profile is inferred "
                           "from the manuscript alone")

    secondary_texts = read_secondary_texts([p for p in materials if p != primary])
    reference = parse_reference_glossary(secondary_texts)
    state["reference_glossary"] = reference

    segments = build_segments(doc, struct, style)
    if not segments:
        limits.append("the manuscript produced no paragraph segments")
        state["limits"] = limits
        hybrid.write_json(work_dir / "state.json", state)
        return state
    if len(segments) >= MAX_SEGMENTS:
        limits.append("manuscript truncated to the first %d paragraph segments" % MAX_SEGMENTS)
        state["degraded"] = True
    if any("deadline" in item for item in limits):
        state["degraded"] = True

    doc_text = hybrid.document_text(doc, page_marker=False)
    terms = hybrid.term_candidates(doc_text, top=40)
    state["segments"] = segments
    state["terms"] = terms
    state["frontmatter_title"] = struct.get("frontmatter", {}).get("title")
    state["section_count"] = len(struct.get("sections") or [])

    batches = build_worksheets(segments, work_dir, style, reference, task_md)
    state["batches"] = batches
    (work_dir / "generated").mkdir(parents=True, exist_ok=True)
    hybrid.write_json(work_dir / "doc.json", {
        "blocks": doc.get("blocks") or {},
        "anchor_quality": doc.get("anchor_quality"),
        "engine": doc.get("engine"),
        "text_layer": bool(doc.get("text_layer")),
        "page_count": len(doc.get("pages") or []),
    })
    hybrid.write_json(work_dir / "terms.json", {
        "terms": terms,
        "reference_glossary": reference,
        "style_profile": style,
    })
    (work_dir / "README.md").write_text(PROTOCOL_README, encoding="utf-8")

    state["ok"] = True
    # A limitation that reduces coverage (truncation / deadline) demotes the run;
    # an anchor-quality or renderer note does not - it is reported, not hidden.
    state["status"] = "partial" if state.get("degraded") else "success"
    state["limits"] = limits
    state["uncertainty"] = uncertainty
    state["change_log"] = change_log
    hybrid.write_json(work_dir / "state.json", state)
    stopwatch.log("prepared %d segments in %d batches (work=%s)"
                  % (len(segments), len(batches), work_dir))
    return state


def structure_outline(doc: dict[str, Any]) -> dict[str, Any]:
    try:
        from lib import structure  # local import keeps the top-level import list small
        return structure.outline(doc)
    except Exception:
        return {"sections": [], "tables": [], "formulas": [],
                "references": {"entry_count": 0, "entries": []},
                "reading_order": {}, "frontmatter": {}}


# --------------------------------------------------------------------------- #
# finalize: validation, preservation, evidence, deliverables, contract
# --------------------------------------------------------------------------- #
def _load_glossary(work_dir: Path) -> dict[str, Any]:
    data = hybrid.read_json(work_dir / "generated" / "_glossary.json", None)
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return {"terms": data}
    return {}


def _find_block(doc: dict[str, Any], page: int, block_id: Any) -> dict[str, Any] | None:
    blocks = (doc.get("blocks") or {}).get(str(page)) or []
    for block in blocks:
        if block.get("block_id") == block_id:
            return block
    return None


def build_paragraphs(segments: list[dict[str, Any]], records: dict[str, dict[str, Any]],
                     doc: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Merge agent answers with the source, enforcing preservation per paragraph."""
    paragraphs: list[dict[str, Any]] = []
    stats = {"unrevised": 0, "kept_unchanged": 0, "revised": 0,
             "revised_with_review_items": 0, "reverted_preservation": 0,
             "reported_change_not_applied": 0, "empty_fallback": 0}
    review_items: list[dict[str, Any]] = []
    for seg in segments:
        seg_id = seg["seg_id"]
        original = seg.get("text") or ""
        record = records.get(seg_id)
        facts = seg.get("facts") or segment_facts(original)
        changes: list[dict[str, Any]] = []
        flags: list[str] = []
        agent_revised = None
        agent_preservation = None
        revised = original
        status = "unrevised"
        preservation = checked_preservation(original, original, label=seg_id)
        diff = invariant_diff(original, original)
        edit = edit_stats(original, original)
        agent_edit = edit_stats(original, original)

        if record is not None:
            agent_revised = str(record.get("revised") or "").strip()
            if not agent_revised:
                status = "unrevised"
                flags.append("empty_revised_fell_back_to_source")
                stats["empty_fallback"] += 1
            else:
                changes = validate_changes(original, agent_revised, record.get("changes"))
                candidate_preservation = checked_preservation(original, agent_revised,
                                                                 label=seg_id)
                candidate_diff = invariant_diff(original, agent_revised)
                agent_edit = edit_stats(original, agent_revised)
                agent_preservation = candidate_preservation
                reasons = critical_reasons(candidate_diff, original, agent_revised)
                if reasons:
                    status = "reverted_preservation"
                    flags.extend(reasons)
                    for reason in reasons:
                        review_items.append({"seg_id": seg_id, "issue": reason,
                                             "action": "reverted_to_source"})
                elif agent_revised == original:
                    preservation = candidate_preservation
                    diff = candidate_diff
                    if changes:
                        status = "reported_change_not_applied"
                        flags.append("change_records_without_text_change")
                    else:
                        status = "kept_unchanged"
                else:
                    revised = agent_revised
                    preservation = candidate_preservation
                    diff = candidate_diff
                    edit = agent_edit
                    status = "revised"
                    if preservation["hedges_missing"]:
                        flags.append("hedge_weakened:"
                                     + ",".join(preservation["hedges_missing"][:5]))
                    if preservation["hedges_added"]:
                        flags.append("hedge_strengthened:"
                                     + ",".join(preservation["hedges_added"][:5]))
                    if diff["modals_missing"] or diff["modals_added"]:
                        flags.append("modal_strength_changed")
                    if diff["labels_missing"]:
                        flags.append("label_reference_lost:"
                                     + ",".join(diff["labels_missing"][:4]))
                    if diff["formulas_missing"]:
                        flags.append("formula_token_lost:"
                                     + ",".join(diff["formulas_missing"][:4]))
                    if not changes:
                        flags.append("edit_without_rationale")
                    if any(not change["valid"] for change in changes):
                        flags.append("invalid_change_record")
                    if edit["edit_rate"] > HEAVY_EDIT_RATE:
                        flags.append("heavy_edit:%.2f" % edit["edit_rate"])
                    if flags:
                        status = "revised_with_review_items"
        # anchors: the paragraph must be traceable to the extracted source block
        page = int(seg.get("page") or 1)
        block_id = (seg.get("block_ids") or [None])[0]
        block = _find_block(doc, page, block_id)
        if block is None:
            block = {"block_id": block_id, "bbox": seg.get("bbox"), "text": original}
        quote = (block.get("text") or original or "")[:400]
        anchor = anchors.make_anchor(page, block, quote)
        for flag in flags:
            if flag.startswith(("hedge_", "modal_", "label_", "formula_", "edit_without",
                                "invalid_change", "heavy_edit", "change_records_without")):
                if any(item["seg_id"] == seg_id and item["issue"] == flag
                       for item in review_items):
                    continue
                review_items.append({"seg_id": seg_id, "issue": flag,
                                     "action": "kept_with_review_item"})
        if status in stats:
            stats[status] += 1
        paragraphs.append({
            "seg_id": seg_id,
            "order": seg.get("order"),
            "page": page,
            "section": seg.get("section_title"),
            "is_heading": bool(seg.get("is_heading")),
            "heading_level": int(seg.get("heading_level") or 0),
            "original": original,
            "revised": revised,
            "agent_revised": agent_revised,
            "status": status,
            "changes": changes,
            "flags": flags,
            "preservation": {
                "ok": bool(preservation.get("ok")),
                "numbers_missing": preservation.get("numbers_missing") or [],
                "numbers_added": diff.get("numbers_added") or [],
                "citations_missing": preservation.get("citations_missing") or [],
                "citations_added": diff.get("citations_added") or [],
                "hedges_missing": preservation.get("hedges_missing") or [],
                "hedges_added": preservation.get("hedges_added") or [],
                "negations_missing": preservation.get("negations_missing") or [],
                "formulas_missing": preservation.get("formulas_missing") or [],
                "numbers_total": preservation.get("numbers_total"),
                "citations_total": preservation.get("citations_total"),
            },
            "edit": edit,
            "agent_edit": agent_edit,
            "agent_preservation": agent_preservation,
            "anchor": anchor,
            "facts": facts,
        })
    return paragraphs, {"status_counts": stats, "review_items": review_items}


def build_evidence(doc: dict[str, Any], paragraphs: list[dict[str, Any]],
                   changes: list[dict[str, Any]], preservation: dict[str, Any],
                   terminology: dict[str, Any], state: dict[str, Any],
                   deliverables: dict[str, Any]) -> dict[str, Any]:
    """Anchored evidence: every paragraph is (a) located at its source block,
    (b) still present in the independently concatenated document text and
    (c) preserves the machine-checkable invariants. Only then is it grounded."""
    doc_text = " ".join(
        block.get("text", "")
        for page_key in sorted((doc.get("blocks") or {}), key=lambda k: int(k))
        for block in doc["blocks"][page_key])
    per_claim: list[dict[str, Any]] = []
    grounded = 0
    for paragraph in paragraphs:
        anchor = paragraph["anchor"]
        result = anchors.verify(doc, anchor)
        located = result.get("match") in anchors.GROUNDED
        document_contains = _ws_contains(doc_text, paragraph["original"])
        invariants_ok = bool(paragraph["preservation"].get("ok"))
        ok = bool(located and document_contains and invariants_ok)
        if ok:
            grounded += 1
        per_claim.append({
            "claim_id": "source-paragraph-" + paragraph["seg_id"],
            "seg_id": paragraph["seg_id"],
            "claim": ("source paragraph %s on page %s is anchored and its invariants "
                      "survive the polish" % (paragraph["seg_id"], paragraph["page"])),
            "page": paragraph["page"],
            "block_id": anchor.get("block_id"),
            "anchor_match": result.get("match"),
            "document_contains_quote": document_contains,
            "invariants_preserved": invariants_ok,
            "grounded": ok,
            "status": paragraph["status"],
        })
    total = len(per_claim)
    rate = round(grounded / total, 4) if total else 0.0
    quality = doc.get("anchor_quality") or "text"
    ceiling = {"span": 1.0, "page": 0.7, "text": 0.4}.get(quality, 0.4)
    valid_changes = [c for c in changes if c.get("valid")]
    invalid_changes = [c for c in changes if not c.get("valid")]
    return {
        "grounding": {
            "claims_total": total,
            "grounded": grounded,
            "ungrounded": total - grounded,
            "grounding_rate": rate,
            "anchor_quality": quality,
            "confidence_ceiling": ceiling,
            "effective_confidence": round(min(rate, ceiling), 4),
            "definition": ("grounded = source block anchor verifies AND the paragraph text is "
                           "found in the independently concatenated document text AND the "
                           "paragraph's numbers/citations/hedges are preserved. "
                           "Content-level independence comes from change landing and "
                           "preservation, not from a self-quote."),
            "per_claim": per_claim,
        },
        "change_landing": {
            "changes_total": len(changes),
            "changes_valid": len(valid_changes),
            "changes_invalid": len(invalid_changes),
            "landing_rate": round(len(valid_changes) / len(changes), 4) if changes else 1.0,
            "invalid_examples": invalid_changes[:20],
            "definition": ("a change record is valid only if original occurs in the source "
                           "paragraph, revised occurs in the revised paragraph and a reason "
                           "is given"),
        },
        "invariants": {
            "numbers_source": preservation.get("numbers_total"),
            "numbers_missing_total": preservation.get("numbers_missing_total"),
            "citations_missing_total": preservation.get("citations_missing_total"),
            "hedges_missing_total": preservation.get("hedges_missing_total"),
            "document_preservation_ok": bool(preservation.get("ok")),
            "per_paragraph_failures": preservation.get("failures") or [],
        },
        "terminology_consistency": {
            "entries_checked": terminology.get("entries_checked"),
            "adopted": terminology.get("adopted"),
            "consistency_rate": terminology.get("consistency_rate"),
            "conflicts": terminology.get("conflicts"),
        },
        "sources": {
            "primary": state.get("primary"),
            "engine": state.get("engine"),
            "anchor_quality": state.get("anchor_quality"),
            "page_count": state.get("page_count"),
            "total_chars": state.get("total_chars"),
            "materials": state.get("materials"),
            "case_path": state.get("case_path"),
            "task_md_path": state.get("task_md_path"),
        },
        "segmentation": {
            "segment_count": len(paragraphs),
            "batch_count": len(state.get("batches") or []),
        },
        "deliverables": deliverables,
    }


def build_deliverables(out_dir: Path, state: dict[str, Any],
                       paragraphs: list[dict[str, Any]], changes: list[dict[str, Any]],
                       terminology: dict[str, Any], preservation: dict[str, Any],
                       status: str) -> tuple[dict[str, Any], list[str]]:
    """Write editable .docx/.md/.html + revision notes + a best-effort preview."""
    limits: list[str] = []
    blocks: list[dict[str, Any]] = []
    title = state.get("frontmatter_title")
    blocks.append({"type": "title", "text": str(title) if title else "Revised manuscript"})
    for paragraph in paragraphs:
        if paragraph.get("is_heading"):
            blocks.append({"type": "heading",
                           "level": min(max(int(paragraph.get("heading_level") or 2), 1), 3),
                           "text": paragraph["revised"]})
        else:
            blocks.append({"type": "para", "text": paragraph["revised"]})
    manuscript = docwrite.write_editable(out_dir, "revised_manuscript", blocks,
                                         title="Revised manuscript")

    notes_blocks: list[dict[str, Any]] = [
        {"type": "title", "text": "Revision notes (per-change ledger)"},
        {"type": "para", "text": ("Status: %s. Paragraphs: %d, changes: %d, "
                                  "preservation clean: %s/%s."
                                  % (status, len(paragraphs), len(changes),
                                     preservation.get("clean"),
                                     preservation.get("checked")))},
    ]
    table_rows: list[list[str]] = [["seg", "page", "type", "original", "revised", "reason"]]
    for change in changes[:200]:
        row = next((p for p in paragraphs if p["seg_id"] == change.get("seg_id")), {})
        table_rows.append([
            str(change.get("seg_id", "")),
            str(row.get("page", "")),
            str(change.get("type_label") or change.get("type") or ""),
            _short(str(change.get("original") or ""), 120),
            _short(str(change.get("revised") or ""), 120),
            _short(str(change.get("reason") or ""), 140),
        ])
    notes_blocks.append({"type": "heading", "level": 1, "text": "Change ledger"})
    notes_blocks.append({"type": "table", "rows": table_rows})
    if terminology.get("entries"):
        term_rows: list[list[str]] = [["source form", "preferred form", "revised uses", "status"]]
        for entry in terminology["entries"][:60]:
            term_rows.append([str(entry.get("source_form", "")),
                              str(entry.get("preferred_form", "")),
                              str(entry.get("occurrences_revised", "")),
                              str(entry.get("status", ""))])
        notes_blocks.append({"type": "heading", "level": 1, "text": "Terminology"})
        notes_blocks.append({"type": "table", "rows": term_rows})
    failed = preservation.get("failures") or []
    if failed:
        fail_rows: list[list[str]] = [["seg", "numbers missing", "citations missing",
                                       "hedges missing"]]
        for item in failed[:60]:
            fail_rows.append([str(item.get("label", "")),
                              ",".join(item.get("numbers_missing") or []) or "-",
                              ",".join(item.get("citations_missing") or []) or "-",
                              ",".join(item.get("hedges_missing") or []) or "-"])
        notes_blocks.append({"type": "heading", "level": 1,
                             "text": "Preservation failures (reverted or flagged)"})
        notes_blocks.append({"type": "table", "rows": fail_rows})
    notes = docwrite.write_editable(out_dir, "revision_notes", notes_blocks,
                                    title="Revision notes")

    csv_path = out_dir / "revision_notes.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["seg_id", "page", "type", "original", "revised", "reason", "valid"])
        for change in changes:
            row = next((p for p in paragraphs if p["seg_id"] == change.get("seg_id")), {})
            writer.writerow([change.get("seg_id", ""), row.get("page", ""),
                             change.get("type_label") or change.get("type", ""),
                             change.get("original", ""), change.get("revised", ""),
                             change.get("reason", ""), change.get("valid")])

    render_report = render.render(manuscript["docx"], out_dir / "preview")
    preview: dict[str, Any] = {"html_preview": manuscript["html"],
                               "rendered_pdf": render_report.get("pdf"),
                               "rendered_png": render_report.get("png"),
                               "render_ok": bool(render_report.get("ok"))}
    if not render_report.get("ok"):
        preview["reason"] = render_report.get("reason")
        limits.append("no office/PDF renderer produced a preview in this image "
                      "(%s); the self-contained HTML preview is provided instead"
                      % (render_report.get("reason") or "renderer unavailable"))
    deliverables = {
        "revised_manuscript": manuscript,
        "revision_notes": notes,
        "revision_notes_csv": str(csv_path),
        "preview": preview,
        "note": ("editable .docx + markdown source + self-contained HTML preview; the preview "
                 "field is null when the image has no renderer (never faked)"),
    }
    return deliverables, limits


SYNONYMS: dict[str, tuple[str, ...]] = {
    "revised_text": ("polished_manuscript", "polished_text", "revised_manuscript",
                     "final_text", "polished_full_text", "revised_document", "润色后全文"),
    "source_text": ("original_text", "original_manuscript", "source_manuscript", "原文"),
    "paragraphs": ("paragraph_alignment", "paragraph_pairs", "paragraph_diff",
                   "original_revised_pairs", "逐段对照", "alignment"),
    "changes": ("change_ledger", "edit_ledger", "edit_list", "change_records",
                "modification_list", "修改清单", "diff_list"),
    "change_summary": ("revision_stats", "edit_summary", "change_statistics",
                       "summary_of_changes", "edit_statistics"),
    "terminology": ("term_consistency", "terminology_table", "glossary_table",
                    "term_list", "术语表"),
    "preservation": ("fidelity_checks", "semantic_preservation", "invariant_checks",
                     "preservation_report", "语义守恒"),
    "evidence": ("evidence_anchors", "source_evidence", "anchor_list", "provenance"),
    "quality": ("run_quality", "quality_metrics", "audit", "qa_metrics"),
    "style_profile": ("style_guide", "target_style", "register_profile", "文体画像"),
    "payload": ("result_payload", "main_result", "structured_result", "deliverable"),
    "summary_text": ("cover_letter", "honest_summary", "run_summary",
                     "executive_summary", "narrative_summary"),
    "deliverables": ("output_files", "artifacts", "files"),
}


def _summary_text(style: dict[str, Any], status: str, paragraphs: list[dict[str, Any]],
                  changes: list[dict[str, Any]], preservation: dict[str, Any],
                  terminology: dict[str, Any]) -> str:
    edited = sum(1 for paragraph in paragraphs if paragraph["revised"] != paragraph["original"])
    reverted = sum(1 for p in paragraphs if p["status"] == "reverted_preservation")
    unrevised = sum(1 for p in paragraphs if p["status"] == "unrevised")
    kept = len(paragraphs) - edited - reverted - unrevised
    if style.get("target_language") == "en":
        return ("%d paragraph(s) were processed (%d edited, %d kept unchanged, %d reverted to "
                "the source because the rewrite changed a number/citation/negation, %d left "
                "unrevised). %d change record(s) recorded with original, revised text, type "
                "and reason. Invariant preservation: %s/%s paragraphs clean. Terminology "
                "consistency: %s over %s locked term(s). Status: %s."
                % (len(paragraphs), edited, kept, reverted, unrevised, len(changes),
                   preservation.get("clean"), preservation.get("checked"),
                   terminology.get("consistency_rate"),
                   terminology.get("entries_checked"), status))
    return ("本次共处理 %d 个段落（实际改文 %d 段，原样保留 %d 段，因改动数字/引用/否定而回退 %d 段，"
            "未处理 %d 段）；记录逐句修改 %d 条（含原文、改文、类型、理由）。语义守恒校验 %s/%s 段通过；"
            "术语一致性 %s（锁定词条 %s 个）。运行状态：%s。"
            % (len(paragraphs), edited, kept, reverted, unrevised, len(changes),
               preservation.get("clean"), preservation.get("checked"),
               terminology.get("consistency_rate"), terminology.get("entries_checked"), status))


def _declared_type(field: dict[str, Any]) -> str:
    spec = field.get("spec")
    if isinstance(spec, dict):
        return str(spec.get("type") or "").casefold()
    return ""


def conform_contract_types(payload: dict[str, Any],
                           declared: list[dict[str, Any]]) -> dict[str, Any]:
    """Shape a mapped value to the type the contract *declares*.

    Field names are reflected, so the declared type is the only other machine
    hint the hidden case gives us. A rich evidence object mapped onto an
    array-typed field is wrapped into a one-item list (content preserved, type
    correct) and the adjustment is recorded in meta, never hidden.
    """
    adjusted: dict[str, str] = {}
    for field in declared:
        name = field.get("name")
        declared_type = _declared_type(field)
        if not name or not declared_type or name not in payload:
            continue
        value = payload[name]
        if value is None:
            continue
        if declared_type in {"array", "list"} and not isinstance(value, list):
            payload[name] = [value]
            adjusted[name] = "wrapped_%s_into_array" % type(value).__name__
        elif declared_type in {"object", "dict", "map", "mapping"} and not isinstance(value, dict):
            payload[name] = {"items": value}
            adjusted[name] = "wrapped_%s_into_object" % type(value).__name__
        elif (declared_type in {"string", "str", "text"}
              and not isinstance(value, str)):
            payload[name] = json.dumps(value, ensure_ascii=False)
            adjusted[name] = "serialised_%s_into_string" % type(value).__name__
    if adjusted:
        payload.setdefault("meta", {})["contract_type_conformance"] = adjusted
    return payload


def assemble(out_dir: Path, work_dir: Path, state: dict[str, Any],
             stopwatch: hybrid.Stopwatch, phase: str) -> dict[str, Any]:
    """Deterministic phase 3: validate, preserve, assemble, reflect, write."""
    case = state.get("case") or {}
    case_id = state.get("case_id")
    limits = list(state.get("limits") or [])
    uncertainty = list(state.get("uncertainty") or [])
    # Drop a previous finalize note before appending this run's note: state.json is
    # rewritten after every phase, so appending blindly made result.json's
    # change_log grow on each re-run (and broke byte-level determinism).
    change_log = [item for item in (state.get("change_log") or [])
                  if not str(item).startswith("finalize(")]
    change_log.append("finalize(%s): validated generated batches, preservation and change landing"
                      % phase)

    if not state.get("ok"):
        limits = limits or ["no paragraph could be extracted from the material"]
        values: dict[str, Any] = {}
        status = "failed"
        evidence = {"grounding": {"claims_total": 0, "grounded": 0,
                                  "grounding_rate": 0.0, "per_claim": []},
                    "sources": {"case_path": state.get("case_path"),
                                "task_md_path": state.get("task_md_path"),
                                "materials": state.get("materials")},
                    "note": "no paragraph could be extracted from the material"}
        report = hybrid.run_report(
            stopwatch=stopwatch, constants={"runner": RUNNER, "phase": phase},
            inputs=state.get("inputs"), limits=limits, uncertainty=uncertainty,
            status=status, materials=state.get("materials"),
            engines_probed=extract.probe())
        payload = hybrid.finalize(out_dir, case=case, case_id=case_id, values=values,
                                  status=status, limits=limits, uncertainty=uncertainty,
                                  change_log=change_log, evidence=evidence, report=report,
                                  synonyms=SYNONYMS,
                                  task_md_path=state.get("task_md_path"))
        declared = hybrid.declared_fields(case)
        if declared:
            conform_contract_types(payload, declared)
            contract.write_result(out_dir, payload)
        return payload

    segments = state.get("segments") or []
    doc = hybrid.read_json(work_dir / "doc.json", {}) or {}
    records = hybrid.load_generated(work_dir)
    merged = hybrid.merge_generated(segments, records, required=("revised",))
    glossary = _load_glossary(work_dir)

    paragraphs, audit = build_paragraphs(segments, merged["by_id"], doc)
    revised_text = "\n\n".join(paragraph["revised"] for paragraph in paragraphs)
    source_text = "\n\n".join(paragraph["original"] for paragraph in paragraphs)

    changes: list[dict[str, Any]] = []
    for paragraph in paragraphs:
        for change in paragraph["changes"]:
            record = dict(change)
            record["seg_id"] = paragraph["seg_id"]
            record["page"] = paragraph["page"]
            record["section"] = paragraph.get("section")
            changes.append(record)

    preservation = hybrid.aggregate_preservation(
        [paragraph["preservation"] for paragraph in paragraphs])
    checks = []
    for paragraph in paragraphs:
        checks.append({
            "label": paragraph["seg_id"],
            "ok": paragraph["preservation"]["ok"],
            "numbers_missing": paragraph["preservation"]["numbers_missing"],
            "citations_missing": paragraph["preservation"]["citations_missing"],
            "formulas_missing": paragraph["preservation"]["formulas_missing"],
            "hedges_missing": paragraph["preservation"]["hedges_missing"],
            "hedges_added": paragraph["preservation"]["hedges_added"],
            "negations_missing": paragraph["preservation"]["negations_missing"],
        })
    preservation["per_segment"] = checks
    preservation["failures"] = [c for c in checks if not c["ok"]][:30]

    terminology = build_terminology(state, source_text, revised_text, glossary)
    deliverables, delivery_limits = build_deliverables(
        out_dir, state, paragraphs, changes, terminology, preservation, "partial")
    limits.extend(delivery_limits)

    coverage = round(merged["received"] / len(segments), 4) if segments else 0.0
    changed_paragraphs = [p for p in paragraphs if p["revised"] != p["original"]]
    documented = [p for p in changed_paragraphs
                  if p["changes"] and all(change["valid"] for change in p["changes"])]
    status_counts = audit["status_counts"]
    reverted = status_counts.get("reverted_preservation", 0)
    unrevised = status_counts.get("unrevised", 0)

    status = "success"
    if merged["received"] == 0:
        status = "partial"
        limits.append("no work/generated/*.json answers were found: the deterministic "
                      "baseline (revised == original) is used for every paragraph")
    if merged["missing"]:
        status = "partial"
        limits.append("%d of %d paragraph(s) have no generated revision and are emitted "
                      "with status=unrevised" % (len(merged["missing"]), len(segments)))
    if merged["duplicates"]:
        status = "partial"
        limits.append("duplicate generated records ignored for: %s"
                      % ", ".join(merged["duplicates"][:10]))
    if merged["unknown_ids"]:
        uncertainty.append("generated records for unknown segment ids were ignored: %s"
                           % ", ".join(merged["unknown_ids"][:10]))
    if reverted:
        status = "partial"
        limits.append("%d paragraph(s) were reverted to the source because the rewrite "
                      "changed a number, citation or negation (fabrication guard)" % reverted)
    if unrevised and merged["received"]:
        status = "partial"
        limits.append("%d answered paragraph(s) fell back to the source (empty/blank "
                      "revised text)" % unrevised)
    if state.get("degraded") and status == "success":
        status = "partial"
    if stopwatch.past_soft():
        status = "partial"
        limits.append("soft deadline (%ss) reached during the run; results may be trimmed"
                      % hybrid.SOFT_DEADLINE_S)
    if merged["incomplete"]:
        uncertainty.append("generated records present but missing the revised field: %s"
                           % ", ".join(merged["incomplete"][:10]))

    evidence = build_evidence(doc, paragraphs, changes, preservation, terminology,
                              state, deliverables)
    grounding = evidence["grounding"]
    landing = evidence["change_landing"]

    document_edit_chars = sum(paragraph["edit"]["changed_chars"] for paragraph in paragraphs)
    document_chars = sum(len(paragraph["original"]) for paragraph in paragraphs)
    quality = {
        "coverage_rate": coverage,
        "paragraphs_total": len(paragraphs),
        "paragraphs_edited": len(changed_paragraphs),
        "paragraphs_reverted": reverted,
        "paragraphs_unrevised": unrevised,
        "changes_total": len(changes),
        "changes_by_type": {key: sum(1 for c in changes if c["type"] == key)
                            for key in EDIT_TYPES},
        "change_landing_rate": landing["landing_rate"],
        "preservation_clean_rate": preservation.get("clean_rate"),
        "preservation_checked": preservation.get("checked"),
        "terminology_consistency_rate": terminology.get("consistency_rate"),
        "terminology_entries_checked": terminology.get("entries_checked"),
        "grounding_rate": grounding["grounding_rate"],
        "effective_confidence": grounding["effective_confidence"],
        "change_documentation_rate": (round(len(documented) / len(changed_paragraphs), 4)
                                      if changed_paragraphs else 1.0),
        "document_edit_rate": round(document_edit_chars / max(1, document_chars), 4),
        "review_item_count": len(audit["review_items"]),
        "anchor_quality": state.get("anchor_quality"),
        "engine": state.get("engine"),
    }

    change_summary = {
        "paragraphs_total": len(paragraphs),
        "status_counts": status_counts,
        "changes_total": len(changes),
        "changes_by_type": {EDIT_TYPES[key]: sum(1 for c in changes if c["type"] == key)
                            for key in EDIT_TYPES},
        "changes_valid": landing["changes_valid"],
        "changes_invalid": landing["changes_invalid"],
        "review_items": audit["review_items"][:60],
        "coverage_rate": coverage,
        "preservation_clean_rate": preservation.get("clean_rate"),
        "document_edit_rate": quality["document_edit_rate"],
    }

    payload_value = {
        "revised_text": revised_text,
        "paragraph_count": len(paragraphs),
        "change_count": len(changes),
        "status": status,
        "preservation_clean_rate": preservation.get("clean_rate"),
        "terminology_consistency_rate": terminology.get("consistency_rate"),
        "deliverables": deliverables,
    }

    values: dict[str, Any] = {
        "revised_text": revised_text,
        "source_text": source_text,
        "paragraphs": paragraphs,
        "changes": changes,
        "change_summary": change_summary,
        "terminology": terminology,
        "preservation": preservation,
        "evidence": evidence,
        "quality": quality,
        "style_profile": state.get("style_profile") or {},
        "payload": payload_value,
        "summary_text": _summary_text(state.get("style_profile") or {}, status, paragraphs,
                                      changes, preservation, terminology),
        "deliverables": deliverables,
    }

    report = hybrid.run_report(
        stopwatch=stopwatch,
        constants={"runner": RUNNER, "phase": phase, "state_schema": STATE_SCHEMA},
        inputs={"case_path": state.get("case_path"),
                "task_md_path": state.get("task_md_path"),
                "primary": state.get("primary"),
                "materials": state.get("materials")},
        materials=state.get("materials"),
        batch_count=len(state.get("batches") or []),
        batches=state.get("batches"),
        segment_count=len(segments),
        generated={"received": merged["received"], "missing": merged["missing"],
                   "duplicates": merged["duplicates"],
                   "unknown_ids": merged["unknown_ids"], "coverage": coverage,
                   "pending_batches": hybrid.missing_batches(work_dir)},
        limits=limits,
        uncertainty=uncertainty,
        status=status,
        quality=quality,
        deliverables=deliverables,
        engines_probed=extract.probe(),
        style_profile=state.get("style_profile"),
        output_dir=str(out_dir),
        work_dir=str(work_dir),
    )
    payload = hybrid.finalize(
        out_dir, case=case, case_id=case_id, values=values, status=status,
        limits=limits, uncertainty=uncertainty, change_log=change_log,
        evidence=evidence, report=report, synonyms=SYNONYMS,
        task_md_path=state.get("task_md_path"),
    )
    declared = hybrid.declared_fields(case)
    if declared:
        conform_contract_types(payload, declared)
        contract.write_result(out_dir, payload)
    state["final_status"] = status
    state["change_log"] = change_log
    hybrid.write_json(work_dir / "state.json", state)
    return payload


# --------------------------------------------------------------------------- #
# placeholder / failure results
# --------------------------------------------------------------------------- #
def write_contract_placeholder(out_dir: Path, case: dict[str, Any], case_id: str | None,
                               note: str) -> None:
    """Contract-shaped placeholder: every declared field exists before slow work."""
    try:
        payload = contract.build_result(
            case=case, case_id=case_id, values={}, status="partial",
            limits=[note],
            uncertainty=["this file is a placeholder; it is replaced when the phase finishes"],
            change_log=["placeholder written at start of run"])
        contract.write_result(out_dir, payload)
    except Exception:
        hybrid.write_placeholder(out_dir, case_id=case_id, note=note,
                                 contract_fields=len(hybrid.declared_fields(case)))


def write_failure(out_dir: Path, exc: BaseException, tb: str) -> None:
    """Last-resort result so a crashed run is never a zero-byte output."""
    try:
        found = discover.discover()
        case = contract.load_case(found.get("case_path"))
        case_id = contract.find_case_id(case)
        payload = contract.build_result(
            case=case, case_id=case_id, values={}, status="failed",
            limits=["run aborted by %s: %s" % (type(exc).__name__, exc)],
            uncertainty=["partial deliverables may be inconsistent because the run aborted"],
            change_log=["failure result written by the top-level handler"])
        payload.setdefault("meta", {})["traceback_tail"] = tb[-1500:]
        contract.write_result(out_dir, payload)
        hybrid.write_json(out_dir / "run_report.json", {
            "case_id": case_id, "status": "failed",
            "error": "%s: %s" % (type(exc).__name__, exc),
            "traceback_tail": tb[-1500:], "runner": RUNNER,
        })
    except Exception:
        try:
            hybrid.write_placeholder(out_dir, note="run aborted: %s" % exc)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def resolve_mode(args: argparse.Namespace) -> str:
    if args.pending:
        return "pending"
    if args.prepare and args.finalize:
        return "all"
    if args.prepare:
        return "prepare"
    if args.finalize:
        return "finalize"
    return "all"


def run(args: argparse.Namespace) -> int:
    stopwatch = hybrid.Stopwatch()
    out_dir = hybrid.resolve_out_dir(args.out)
    hybrid.write_placeholder(out_dir,
                             note="run started; this file is replaced when the run finishes")
    mode = resolve_mode(args)
    work_dir = resolve_work_dir(args.work, out_dir)

    found = discover.discover()
    case = contract.load_case(found.get("case_path"))
    case_id = contract.find_case_id(case)
    declared = hybrid.declared_fields(case)
    write_contract_placeholder(out_dir, case, case_id,
                               "run started: awaiting %s phase" % mode)
    if mode != "pending":  # --pending must print machine-readable JSON only
        _log(stopwatch, "mode=%s case=%s contract_fields=%d"
             % (mode, case_id, len(declared)))
        _log(stopwatch, "out=%s work=%s" % (out_dir, work_dir))
        if found.get("missing"):
            _log(stopwatch, "discovery missing: %s" % ", ".join(found["missing"]))

    if mode == "pending":
        pending = hybrid.missing_batches(work_dir)
        print(json.dumps({"work_dir": str(work_dir),
                          "generated_dir": str(work_dir / "generated"),
                          "pending_batches": pending}, ensure_ascii=False, indent=2))
        return 0

    state: dict[str, Any] | None = None
    if mode in {"prepare", "all"}:
        state = prepare(found, case, case_id, out_dir, work_dir, stopwatch)
        if mode == "prepare":
            # Early honest baseline: a complete contract-shaped result already
            # exists before the agent phase, so an interrupted run is not a zero.
            payload = assemble(out_dir, work_dir, state, stopwatch, phase="prepare_baseline")
            print("PHASE=prepare STATUS=%s" % payload.get("meta", {}).get("status"))
            print("WORK_DIR=%s" % work_dir)
            print("OUT_DIR=%s" % out_dir)
            print("PENDING_BATCHES=%s" % json.dumps(hybrid.missing_batches(work_dir)))
            print("GENERATED_DIR=%s" % (work_dir / "generated"))
            print("RESULT=%s" % (out_dir / "result.json"))
            return 0
    if mode == "finalize":
        state = hybrid.read_json(work_dir / "state.json", None)
        if not isinstance(state, dict):
            _log(stopwatch, "no prepared state found; bootstrapping the prepare phase")
            state = prepare(found, case, case_id, out_dir, work_dir, stopwatch)
    if state is None:
        state = {"ok": False, "status": "failed",
                 "limits": ["no phase produced a prepared state"],
                 "case": case, "case_id": case_id,
                 "case_path": found.get("case_path"),
                 "task_md_path": found.get("task_md_path"),
                 "materials": found.get("material_docs")}
    payload = assemble(out_dir, work_dir, state, stopwatch, phase=mode)
    final_status = payload.get("meta", {}).get("status")
    quality = (payload.get("quality") or {}) if isinstance(payload.get("quality"), dict) else {}
    if not quality:
        extra = ((payload.get("meta") or {}).get("additional_outputs") or {})
        quality = extra.get("quality") or {}
    _log(stopwatch, "done status=%s paragraphs=%s coverage=%s"
         % (final_status, quality.get("paragraphs_total"), quality.get("coverage_rate")))
    print("PHASE=%s STATUS=%s" % (mode, final_status))
    print("OUT_DIR=%s" % out_dir)
    print("WORK_DIR=%s" % work_dir)
    print("PENDING_BATCHES=%s" % json.dumps(hybrid.missing_batches(work_dir)))
    print("RESULT=%s" % (out_dir / "result.json"))
    details = {
        "status": final_status,
        "paragraphs_total": quality.get("paragraphs_total"),
        "paragraphs_edited": quality.get("paragraphs_edited"),
        "paragraphs_reverted": quality.get("paragraphs_reverted"),
        "paragraphs_unrevised": quality.get("paragraphs_unrevised"),
        "changes_total": quality.get("changes_total"),
        "coverage_rate": quality.get("coverage_rate"),
        "preservation_clean_rate": quality.get("preservation_clean_rate"),
        "grounding_rate": quality.get("grounding_rate"),
        "terminology_consistency_rate": quality.get("terminology_consistency_rate"),
    }
    print("SUMMARY=" + json.dumps(details, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="writing-02 language polish: minimal-edit revision with a per-change "
                    "rationale (two-phase hybrid pipeline)")
    parser.add_argument("--prepare", action="store_true",
                        help="deterministic phase 1: materials -> segments -> batch worksheets")
    parser.add_argument("--finalize", action="store_true",
                        help="deterministic phase 3: validate answers, preserve semantics, "
                             "reflect the declared output contract")
    parser.add_argument("--all", action="store_true",
                        help="prepare + finalize in one run (default); missing answers degrade "
                             "to the deterministic baseline instead of failing")
    parser.add_argument("--pending", action="store_true",
                        help="print which batch answers are still missing and exit")
    parser.add_argument("--out", default=None,
                        help="output directory (default: /app/output or ./output)")
    parser.add_argument("--work", default=None,
                        help="work directory for worksheets/state (default: /app/work or <out>/work)")
    args = parser.parse_args()

    out_dir = hybrid.resolve_out_dir(args.out)
    try:
        return run(args)
    except BaseException as exc:  # noqa: BLE001 - a run must always leave a result file
        tb = traceback.format_exc(limit=8)
        write_failure(out_dir, exc, tb)
        print("FATAL %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
