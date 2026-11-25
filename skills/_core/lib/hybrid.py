#!/usr/bin/env python3
"""Two-phase hybrid pipeline shared by the topic adapters.

Why a hybrid pipeline
---------------------
The evaluation contract allows 64 model requests, 900 seconds and 16384 tokens per
response. Reading, slicing, moving and re-checking text must therefore NOT
consume model requests; only judgement (translate / polish / write / assert /
extract) should. The workload is split in three:

1. prepare  - deterministic: discover inputs, extract text+coordinates, cut the
   document into addressable segments, extract the machine-checkable facts (numbers,
   units, citations, formulas, hedges, negation, candidate terms) and write small
   *batch worksheets* for the agent.
2. judgement - the agent writes generated records (one small JSON file per batch).
3. finalize - deterministic again: validate coverage and preservation, bind
   evidence anchors back to the source blocks, reflect case.json.output_contract
   and write result.json / evidence.json / run_report.json.

Two hard rules are implemented here, not left to the adapter:

* a result.json is written BEFORE any slow work starts (the container can be
  killed at the 900 s budget; an empty output directory scores zero);
* finalize never fails for missing generated content - it substitutes an honest
  deterministic baseline, records the gap in limits/uncertainty and keeps the
  declared shape. "Produced but incomplete" beats "crashed".

No network, no third-party packages, deterministic ordering throughout.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# budget
# --------------------------------------------------------------------------- #
TIME_BUDGET_S = 900
SOFT_DEADLINE_S = 720


class Stopwatch:
    """Elapsed-time guard. Cheap enough to call before every engine/tool step."""

    def __init__(self) -> None:
        self.start = time.time()

    def elapsed(self) -> float:
        return round(time.time() - self.start, 2)

    def remaining(self) -> float:
        return round(TIME_BUDGET_S - (time.time() - self.start), 1)

    def past_soft(self) -> bool:
        return self.elapsed() > SOFT_DEADLINE_S

    def log(self, message: str) -> None:
        print(f"[{self.elapsed():>7.1f}s] {message}", flush=True)


# --------------------------------------------------------------------------- #
# deterministic json io
# --------------------------------------------------------------------------- #
def write_json(path: str | Path, data: Any) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # no BOM: the platform, jq and every JSON parser read this, and a BOM breaks
    # plain json.loads on the reviewer's machine
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def read_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.is_file():
        return default
    try:
        # utf-8-sig tolerates a BOM written by a Windows shell tool; without it the
        # file silently reads as "declared no contract".
        return json.loads(p.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception:
        return default


def read_records(path: str | Path) -> list[dict[str, Any]]:
    """Read one generated file: a JSON array, an object with 'records', or JSONL."""
    p = Path(path)
    if not p.is_file():
        return []
    raw = p.read_text(encoding="utf-8-sig", errors="replace")
    try:
        data = json.loads(raw)
    except Exception:
        rows: list[dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip().rstrip(",")
            if not line or line in "[]":
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                rows.append(item)
        return rows
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("records", "items", "results", "data", "rows"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        # a single record keyed by segment id
        if any(isinstance(v, dict) for v in data.values()):
            rows = []
            for key, value in data.items():
                if isinstance(value, dict):
                    record = dict(value)
                    record.setdefault("seg_id", key)
                    rows.append(record)
            return rows
        return [data]
    return []


# --------------------------------------------------------------------------- #
# output directories and the early placeholder
# --------------------------------------------------------------------------- #
def _writable_dir(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def resolve_out_dir(prefer: str | Path | None = None) -> Path:
    candidates: list[Path] = []
    if prefer:
        candidates.append(Path(prefer))
    candidates.extend([Path("/app/output"), Path("/output"), Path("output")])
    for candidate in candidates:
        if _writable_dir(candidate):
            return candidate
    fallback = Path.cwd() / "output"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def placeholder_payload(*, case_id: str | None = None, status: str = "partial",
                        note: str = "", contract_fields: int = 0) -> dict[str, Any]:
    return {
        "case_id": case_id or "",
        "meta": {
            "status": status,
            "contract_detected": contract_fields > 0,
            "contract_field_count": contract_fields,
            "contract_fields_not_reported": [],
            "placeholders": {},
            "limits": [note] if note else [],
            "uncertainty": [],
            "change_log": ["placeholder written at start of run"],
        },
    }


def write_placeholder(out_dir: str | Path, *, case_id: str | None = None,
                      note: str = "run started; this file is replaced when the run finishes",
                      contract_fields: int = 0) -> Path:
    target = Path(out_dir) / "result.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(
        placeholder_payload(case_id=case_id, note=note, contract_fields=contract_fields),
        ensure_ascii=False, indent=2), encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# contract field mapping
# --------------------------------------------------------------------------- #
# Field names are decided by the hidden case, so no literal schema can be right.
# Every adapter describes what it produced; this mapper decides which declared
# field receives which value. Exact -> alias -> token overlap -> prefix, longest
# candidate first, and every decision is reported in meta.contract_field_mapping.
GENERIC_ALIASES: dict[str, tuple[str, ...]] = {
    "pages": ("page", "pagination", "parsed_page", "page_block", "layout"),
    "text": ("fulltext", "plaintext", "content", "body", "transcript"),
    "sections": ("section", "outline", "heading", "structure", "chapter"),
    "tables": ("table", "tabulate", "table_candidate"),
    "formulas": ("formula", "equation", "math"),
    "references": ("reference", "bibliography", "citation", "cited"),
    "metadata": ("meta", "frontmatter", "information", "info", "manifest"),
    "summary": ("abstract", "digest", "overview", "tldr", "condensed", "takeaway"),
    "notes": ("note", "card", "bullet", "point", "keypoint"),
    "evidence": ("anchor", "provenance", "grounding", "source", "support",
                 "citation_anchor", "locator"),
    "quality": ("metric", "statistics", "stats", "validation", "audit", "qa"),
    "glossary": ("term", "terminology", "lexicon", "vocabulary", "dictionary"),
    "translation": ("translate", "translated", "target", "target_text", "chinese", "zh", "yiven"),
    "bilingual": ("bilingual", "aligned", "parallel", "pair", "side_by_side", "duizhao"),
    "assertions": ("assertion", "claim", "statement", "triple", "nanopublication", "duanyan"),
    "entities": ("entity", "node", "concept", "object", "shiti"),
    "relations": ("relation", "edge", "link", "triple", "relationship", "guanxi"),
    "queries": ("query", "question", "ask", "retrieval", "chaxun"),
    "changes": ("change", "edit", "diff", "modification", "revision", "xiugai"),
    "revised_text": ("revised", "polished", "edited", "rewritten", "output_text", "final"),
    "source_text": ("source", "original", "input", "raw", "yuanwen"),
    "parameters": ("parameter", "hyperparameter", "setting", "config", "canshu"),
    "datasets": ("dataset", "data", "corpus", "shujuji"),
    "reproducibility": ("reproduce", "reproduction", "environment", "command", "fuxian"),
    "limitations": ("limitation", "boundary", "caveat", "constraint", "xianzhi"),
    "status": ("state", "run_status", "outcome"),
    "uncertainty": ("unknown", "unresolved", "gap", "open_question", "buqueding"),
}

# Suffixes that mark a collection rather than a different concept.
_COLLECTION_MARKERS = ("_index", "_list", "_entries", "_items", "_results", "_data",
                       "_content", "_records", "_set", "_map", "_dict")


def field_tokens(name: str) -> list[str]:
    """Normalise a field name into comparable lowercase tokens."""
    text = unicodedata.normalize("NFKC", name or "").casefold()
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", text)
    tokens = [t for t in text.split() if t]
    out: list[str] = []
    for token in tokens:
        out.append(token)
        for marker in _COLLECTION_MARKERS:
            if token.endswith(marker):
                stem = token[: -len(marker)]
                if stem:
                    out.append(stem)
        if token.endswith("ies") and len(token) > 5:
            out.append(token[:-3] + "y")
        elif token.endswith("es") and len(token) > 4:
            out.append(token[:-2])
        elif token.endswith("s") and len(token) > 3:
            out.append(token[:-1])
    return out


def _flat(text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (text or "").casefold())


def _alias_pool(key: str) -> set[str]:
    pool = {key}
    pool.update(GENERIC_ALIASES.get(key, ()))
    return {_flat(p) for p in pool}


def map_fields(declared: list[dict[str, Any]], values: dict[str, Any],
               synonyms: dict[str, Iterable[str]] | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    """Map produced values onto the declared contract fields.

    Returns (mapped, report) where report is {declared_field: internal_key}
    so the run report can show a reviewer exactly how the contract was satisfied.
    One value may feed several declared fields (a contract sometimes asks for the
    same thing twice, e.g. glossary and terminology).
    """
    synonyms = synonyms or {}
    entries: list[tuple[str, set[str]]] = []
    for key in values:
        aliases = _alias_pool(key)
        for extra in synonyms.get(key, ()):  # per-adapter aliases
            aliases.add(_flat(extra))
        entries.append((key, aliases))

    mapped: dict[str, Any] = {}
    report: dict[str, str] = {}
    claimed: set[str] = set()

    def _score(field_flat: str, field_token_list: list[str], key: str, aliases: set[str]) -> float:
        if field_flat == _flat(key):
            return 100.0
        if field_flat in aliases:
            return 80.0
        best = 0.0
        for alias in aliases:
            if len(alias) < 3:
                continue
            if field_flat.startswith(alias) or alias.startswith(field_flat):
                best = max(best, 40.0 + min(len(alias), len(field_flat)) / 100.0)
            elif alias in field_flat or field_flat in alias:
                best = max(best, 25.0 + min(len(alias), len(field_flat)) / 100.0)
        for token in field_token_list:
            for alias in aliases:
                if len(token) >= 4 and len(alias) >= 4 and token == alias:
                    best = max(best, 30.0)
                elif len(token) >= 5 and token in alias:
                    best = max(best, 20.0)
        return best

    # strongest first; ties resolved by declared order for reproducibility
    pairs: list[tuple[float, int, str, str]] = []
    for index, field in enumerate(declared):
        name = field["name"]
        flat = _flat(name)
        tokens = field_tokens(name)
        for key, aliases in entries:
            score = _score(flat, tokens, key, aliases)
            if score > 0:
                pairs.append((score, index, name, key))
    pairs.sort(key=lambda item: (-item[0], item[1]))
    for score, _index, name, key in pairs:
        if name in mapped or key in claimed:
            continue
        if score < 24:
            continue
        mapped[name] = values[key]
        report[name] = key
        claimed.add(key)
    return mapped, report


def declared_fields(case: dict[str, Any]) -> list[dict[str, Any]]:
    from lib import contract  # local import keeps hybrid importable in isolation
    return contract.normalize_contract(contract._find_contract(case))


# --------------------------------------------------------------------------- #
# segments: the addressable units every adapter talks about
# --------------------------------------------------------------------------- #
_SEG_SPLIT = re.compile(r"(?<=[\.\!\?。！？\;；])\s+")


def _split_long(text: str, max_chars: int) -> list[str]:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return [text] if text else []
    parts = _SEG_SPLIT.split(text)
    chunks: list[str] = []
    current = ""
    for part in parts:
        if not part:
            continue
        if current and len(current) + len(part) + 1 > max_chars:
            chunks.append(current.strip())
            current = part
        else:
            current = f"{current} {part}".strip()
    if current:
        chunks.append(current.strip())
    return [c for c in chunks if c]


def _section_index(doc: dict[str, Any], struct: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Map block_id -> section heading, in reading order."""
    sections = list((struct or {}).get("sections") or [])
    by_block: dict[str, dict[str, Any]] = {}
    counter = 0
    for page_key in sorted((doc.get("blocks") or {}), key=lambda k: int(k)):
        for block in doc["blocks"][page_key]:
            for section in sections:
                if section.get("block_id") == block.get("block_id"):
                    counter += 1
                    by_block[str(block.get("block_id"))] = {
                        "section_id": f"sec-{counter:03d}",
                        "title": section.get("title") or "_preamble",
                        "level": section.get("level") or 1,
                        "page": int(page_key),
                        "block_id": block.get("block_id"),
                    }
    return by_block


def build_segments(doc: dict[str, Any], struct: dict[str, Any] | None = None,
                   *, max_chars: int = 1400, min_chars: int = 0) -> list[dict[str, Any]]:
    """Cut the extracted document into ordered, page-anchored segments.

    One block is normally one segment (so the anchor stays exact). A block longer
    than max_chars is split on sentence boundaries; the split does not create new
    facts, it only keeps each agent batch small enough to fit one response.
    """
    sections = _section_index(doc, struct)
    current_section = {"section_id": "sec-000", "title": "_preamble", "level": 0, "page": None}
    segments: list[dict[str, Any]] = []
    for page_key in sorted((doc.get("blocks") or {}), key=lambda k: int(k)):
        page = int(page_key)
        for block in doc["blocks"][page_key]:
            block_id = str(block.get("block_id"))
            if block_id in sections:
                current_section = sections[block_id]
            text = (block.get("text") or "").strip()
            if not text:
                continue
            for index, chunk in enumerate(_split_long(text, max_chars)):
                segments.append({
                    "seg_id": f"seg-{len(segments) + 1:04d}",
                    "order": len(segments) + 1,
                    "page": page,
                    "block_ids": [block_id],
                    "part": index,
                    "bbox": block.get("bbox"),
                    "section_id": current_section.get("section_id"),
                    "section_title": current_section.get("title"),
                    "char_count": len(chunk),
                    "text": chunk,
                })
    return segments


def document_text(doc: dict[str, Any], *, page_marker: bool = True) -> str:
    """Full text with explicit page markers, for context the agent may need."""
    lines: list[str] = []
    for page_key in sorted((doc.get("blocks") or {}), key=lambda k: int(k)):
        if page_marker:
            lines.append(f"\n[[page {page_key}]]")
        for block in doc["blocks"][page_key]:
            text = (block.get("text") or "").strip()
            if text:
                lines.append(f"[{block.get('block_id')}] {text}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# agent batches
# --------------------------------------------------------------------------- #
def make_batches(segments: list[dict[str, Any]], work_dir: str | Path,
                 *, batch_size: int = 8, batch_chars: int = 6000) -> list[dict[str, Any]]:
    """Write small worksheets the agent can answer in one response each."""
    work = Path(work_dir)
    batch_dir = work / "batches"
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
        item = {
            "batch_id": batch_id,
            "segment_count": len(current),
            "segments": [
                {"seg_id": s["seg_id"], "page": s["page"],
                 "section": s.get("section_title"), "text": s["text"]}
                for s in current
            ],
        }
        write_json(batch_dir / f"{batch_id}.json", item)
        batches.append({"batch_id": batch_id, "segment_count": len(current),
                        "chars": current_chars, "worksheet": str(batch_dir / f"{batch_id}.json")})
        current = []
        current_chars = 0

    for segment in segments:
        if current and (len(current) >= batch_size or current_chars + segment["char_count"] > batch_chars):
            flush()
        current.append(segment)
        current_chars += segment["char_count"]
    flush()
    return batches


def load_generated(work_dir: str | Path) -> list[dict[str, Any]]:
    """Read every generated batch file under work/generated."""
    generated_dir = Path(work_dir) / "generated"
    records: list[dict[str, Any]] = []
    if not generated_dir.is_dir():
        return records
    for path in sorted(generated_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        records.extend(read_records(path))
    return records


def merge_generated(segments: list[dict[str, Any]], records: list[dict[str, Any]],
                    *, key: str = "seg_id", required: tuple[str, ...] = ()) -> dict[str, Any]:
    """Validate generated records against the segment inventory.

    Reports missing / duplicate / unknown / incomplete records instead of silently
    accepting a partial answer - a reviewer has to be able to see coverage.
    """
    known = {s[key] for s in segments}
    by_id: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    unknown: list[str] = []
    incomplete: list[str] = []
    for record in records:
        seg_id = record.get(key)
        if not seg_id:
            continue
        seg_id = str(seg_id)
        if seg_id not in known:
            unknown.append(seg_id)
            continue
        filled = all(str(record.get(field) or "").strip() for field in required) if required else True
        if not filled:
            incomplete.append(seg_id)
            continue
        if seg_id in by_id:
            duplicates.append(seg_id)
            continue
        by_id[seg_id] = record
    missing = [s[key] for s in segments if s[key] not in by_id]
    return {
        "total_segments": len(segments),
        "received": len(by_id),
        "coverage": round(len(by_id) / len(segments), 4) if segments else 0.0,
        "missing": missing,
        "duplicates": duplicates,
        "unknown_ids": unknown,
        "incomplete": incomplete,
        "by_id": by_id,
    }


def missing_batches(work_dir: str | Path) -> list[str]:
    """Which batch worksheets have no answer file yet (drives the agent's loop)."""
    work = Path(work_dir)
    pending: list[str] = []
    for worksheet in sorted((work / "batches").glob("batch-*.json")):
        answer = work / "generated" / worksheet.name
        if not answer.is_file():
            pending.append(worksheet.stem)
    return pending


# --------------------------------------------------------------------------- #
# text facts: numbers, units, citations, formulas, hedges, negation, terms
# --------------------------------------------------------------------------- #
_UNIT = (r"%|permille|degC|K|nm|um|mm|cm|m|km|mg|g|kg|ug|ng|pg|mL|ml|L|uL|nL|M|mM|uM|nM|pM"
         r"|mol|mmol|umol|s|ms|us|min|h|d|yr|eV|keV|MeV|GeV|Hz|kHz|MHz|GHz|Pa|kPa|MPa|GPa"
         r"|Torr|bar|atm|V|mV|mA|A|W|kW|J|kJ|cal|kcal|Da|kDa|bp|kb|rpm|wt")
NUM_RE = re.compile(
    r"(?<![A-Za-z0-9])[-+]?\d+(?:[.,]\d+)*(?:\s?(?:" + _UNIT + r")(?![A-Za-z]))?")
CITATION_PATTERNS = (
    re.compile(r"\[(?:\d{1,3}(?:\s*[-,–]\s*\d{1,3})*)(?:\s*[,;]\s*\d{1,3}(?:\s*[-,–]\s*\d{1,3})*)*\]"),
    re.compile(r"\((?:[A-Z][A-Za-z\-]+(?:\s+et\s+al\.?)?(?:,\s*|\s+)(?:19|20)\d{2}[a-z]?)"
               r"(?:\s*[;,]\s*[A-Z][A-Za-z\-]+(?:\s+et\s+al\.?)?(?:,\s*|\s+)(?:19|20)\d{2}[a-z]?)*\)"),
    re.compile(r"\\cite\{[^}]{1,200}\}"),
    re.compile(r"\\ref\{[^}]{1,120}\}"),
    re.compile(r"图\s*\d+(?:[\.\-]\d+)*|表\s*\d+(?:[\.\-]\d+)*|式\s*\(\d+\)"),
)
FORMULA_TOKEN_RE = re.compile(
    r"(?:\b[A-Za-z]\s*[_^]\s*\{?[A-Za-z0-9]+\}?)|(?:\\[a-zA-Z]+\{[^}]*\})|(?:[=≈≤≥±×÷∑∏∫√∂∇<>])")

HEDGES_EN = ("may", "might", "could", "suggest", "suggests", "suggesting",
             "likely", "unlikely", "possible", "possibly", "probable", "probably",
             "appear", "appears", "indicate", "indicates", "tend", "tends",
             "often", "sometimes", "generally", "typically", "usually",
             "approximately", "about", "around", "roughly", "nearly", "almost",
             "at least", "at most", "estimated", "preliminary", "putative",
             "potential", "potentially", "consistent with", "in part",
             "to some extent", "largely", "mostly")
HEDGES_ZH = ("可能", "也许", "或许", "大概", "大约", "约", "左右", "估计",
             "推测", "初步", "倾向", "往往", "通常", "一般", "多数", "部分",
             "似乎", "表明", "提示", "有望", "潜在", "在一定程度上", "主要")
NEGATIONS_EN = ("not", "no", "never", "none", "neither", "nor", "cannot", "without",
                "fail", "fails", "failed", "unable", "lack", "lacks")
NEGATIONS_ZH = ("不", "未", "无", "没有", "并非", "未能", "不能", "不得", "缺乏", "排除")
MODALS_EN = ("must", "should", "shall", "will", "would", "ought", "need")
LABELS_EN = ("figure", "fig.", "table", "tab.", "scheme", "eq.", "equation",
             "section", "appendix")
LABELS_ZH = ("图", "表", "式", "节", "附录", "补充材料")


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\u00ad", "")
    text = re.sub(r"-\s*\n\s*", "", text)
    return re.sub(r"\s+", " ", text).strip()


def find_numbers(text: str) -> list[str]:
    return [m.group(0).strip() for m in NUM_RE.finditer(normalize_text(text)) if m.group(0).strip()]


def find_citations(text: str) -> list[str]:
    found: list[str] = []
    norm = normalize_text(text)
    for pattern in CITATION_PATTERNS:
        found.extend(m.group(0) for m in pattern.finditer(norm))
    return found


def find_formula_tokens(text: str) -> list[str]:
    return [m.group(0) for m in FORMULA_TOKEN_RE.finditer(normalize_text(text))]


def _find_lexicon(text: str, en: Iterable[str], zh: Iterable[str]) -> list[str]:
    norm = normalize_text(text)
    low = f" {norm.casefold()} "
    hits: list[str] = []
    for word in en:
        if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", low):
            hits.append(word)
    for word in zh:
        if word in norm:
            hits.append(word)
    return sorted(set(hits))


def find_hedges(text: str) -> list[str]:
    return _find_lexicon(text, HEDGES_EN, HEDGES_ZH)


def find_negations(text: str) -> list[str]:
    return _find_lexicon(text, NEGATIONS_EN, NEGATIONS_ZH)


def find_modals(text: str) -> list[str]:
    return _find_lexicon(text, MODALS_EN, ())


def find_labels(text: str) -> list[str]:
    norm = normalize_text(text)
    hits = [w for w in LABELS_EN
            if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", norm.casefold())]
    hits.extend(w for w in LABELS_ZH if w in norm)
    return sorted(set(hits))


SENT_SPLIT_RE = re.compile(r"(?<=[\.\!\?。！？])\s+|(?<=[;；])\s+")


def sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT_SPLIT_RE.split(normalize_text(text)) if s.strip()]


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]{1,}|[\u4e00-\u9fff]{2,}")


def words(text: str) -> list[str]:
    return _WORD_RE.findall(normalize_text(text))


ACRONYM_RE = re.compile(r"\b[A-Z][A-Za-z0-9]*[A-Z][A-Za-z0-9\-]*\b")


def find_acronyms(text: str) -> list[str]:
    return sorted(set(m.group(0) for m in ACRONYM_RE.finditer(normalize_text(text))
                      if 2 <= len(m.group(0)) <= 12))


TERM_STOP = {
    "the", "and", "with", "that", "this", "these", "those", "from", "into", "were",
    "have", "been", "which", "their", "there", "however", "therefore", "although",
    "between", "within", "using", "used", "based", "also", "such", "more", "than",
    "figure", "table", "equation", "section", "respectively", "shown", "results",
    "study", "paper", "method", "methods", "data", "analysis", "conclusion",
}


def term_candidates(text: str, *, top: int = 60, min_count: int = 2) -> list[dict[str, Any]]:
    """Deterministic domain-term candidates: acronyms first, then repeated phrases.

    This is only a *candidate* list. Locking a translation for a term is a judgement
    call that the agent must make; the script's job is to make the inventory
    complete and reproducible so the agent's glossary can be checked against it.
    """
    norm = normalize_text(text)
    counts: dict[str, int] = {}
    display: dict[str, str] = {}

    for acronym in find_acronyms(norm):
        # count every occurrence: find_acronyms returns a set, so a lower-case
        # "count = 1" here silently dropped acronyms that occur twice
        counts[acronym] = len(re.findall(rf"(?<![A-Za-z]){re.escape(acronym)}(?![A-Za-z])", norm))
        display[acronym] = acronym

    tokens = words(norm)
    for size in (3, 2):
        for index in range(len(tokens) - size + 1):
            window = tokens[index:index + size]
            if any(len(t) < 3 for t in window):
                continue
            if window[0].casefold() in TERM_STOP or window[-1].casefold() in TERM_STOP:
                continue
            phrase = " ".join(window)
            key = phrase.casefold()
            counts[key] = counts.get(key, 0) + 1
            display.setdefault(key, phrase)

    ranked = sorted(
        ((count, display.get(key, key)) for key, count in counts.items()
         if count >= min_count),
        key=lambda item: (-item[0], len(item[1]), item[1].casefold()),
    )
    seen_flat: set[str] = set()
    out: list[dict[str, Any]] = []
    for count, phrase in ranked:
        flat = _flat(phrase)
        if not flat or flat in seen_flat:
            continue
        seen_flat.add(flat)
        if len(phrase) > 2:
            occurrences = len(re.findall(re.escape(phrase), norm, flags=re.IGNORECASE))
        else:
            occurrences = count
        out.append({"term": phrase, "count": occurrences})
        if len(out) >= top:
            break
    return out


# --------------------------------------------------------------------------- #
# preservation checks (numbers / citations / hedges must survive a rewrite)
# --------------------------------------------------------------------------- #
def preservation_check(source: str, target: str, *, label: str = "segment") -> dict[str, Any]:
    """Deterministic 'didn't break it' check for polish / translation outputs.

    The topic penalises a rewrite that silently strengthens/weakens a claim or drops
    a number, unit or citation. Everything here is a set comparison, so the verdict
    is reproducible and a reviewer can re-run it.
    """
    src_numbers = find_numbers(source)
    tgt_numbers = find_numbers(target)
    src_norm = {normalize_text(n).replace(" ", "") for n in src_numbers}
    tgt_norm = {normalize_text(n).replace(" ", "") for n in tgt_numbers}
    src_cites = find_citations(source)
    tgt_cites = find_citations(target)
    src_hedges = find_hedges(source)
    tgt_hedges = find_hedges(target)
    src_neg = find_negations(source)
    tgt_neg = find_negations(target)
    src_formulas = find_formula_tokens(source)
    tgt_formulas = find_formula_tokens(target)

    missing_numbers = sorted(n for n in src_norm if n not in tgt_norm and n.strip("+-"))
    missing_cites = [c for c in src_cites if c not in tgt_cites]
    missing_formulas = [f for f in src_formulas if f not in tgt_formulas]
    hedge_loss = [h for h in src_hedges if h not in tgt_hedges]
    hedge_gain = [h for h in tgt_hedges if h not in src_hedges]

    ok = not missing_numbers and not missing_cites and not missing_formulas and not hedge_loss
    return {
        "label": label,
        "ok": ok,
        "numbers_total": len(src_norm),
        "numbers_missing": missing_numbers[:20],
        "citations_total": len(src_cites),
        "citations_missing": missing_cites[:20],
        "formulas_total": len(src_formulas),
        "formulas_missing": missing_formulas[:20],
        "hedges_source": src_hedges,
        "hedges_missing": hedge_loss,
        "hedges_added": hedge_gain,
        "negations_source": src_neg,
        "negations_missing": [n for n in src_neg if n not in tgt_neg],
        "length_source": len(source or ""),
        "length_target": len(target or ""),
    }


def aggregate_preservation(checks: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(checks)
    ok = sum(1 for c in checks if c.get("ok"))
    numbers = sum(len(c.get("numbers_missing") or []) for c in checks)
    citations = sum(len(c.get("citations_missing") or []) for c in checks)
    hedges = sum(len(c.get("hedges_missing") or []) for c in checks)
    return {
        "checked": total,
        "clean": ok,
        "clean_rate": round(ok / total, 4) if total else 0.0,
        "numbers_missing_total": numbers,
        "citations_missing_total": citations,
        "hedges_missing_total": hedges,
        "failures": [c for c in checks if not c.get("ok")][:30],
    }


# --------------------------------------------------------------------------- #
# finalize: contract reflection + deliverables
# --------------------------------------------------------------------------- #
def finalize(out_dir: str | Path, *, case: dict[str, Any] | None,
             case_id: str | None, values: dict[str, Any],
             status: str = "success", limits: Iterable[str] = (),
             uncertainty: Iterable[str] = (), change_log: Iterable[str] = (),
             task_md_path: str | None = None,
             evidence: dict[str, Any] | None = None,
             report: dict[str, Any] | None = None,
             synonyms: dict[str, Iterable[str]] | None = None,
             additional: Iterable[str] | None = None,
             extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Assemble and write every deliverable for one case.

    additional names the produced values that should also be preserved under
    meta.additional_outputs when the contract does not name them (default: all
    values not mapped to a declared field). This keeps the rich content available
    to a reviewer even on a case whose contract asks for something narrower.
    """
    from lib import contract

    case = case or {}
    declared = declared_fields(case)
    mapped, mapping_report = map_fields(declared, values, synonyms)
    limits = list(limits)
    if values and not declared:
        limits = limits + ["case.json declared no output_contract; "
                           "all produced content is under meta.additional_outputs"]

    payload = contract.build_result(
        case=case, case_id=case_id, values=mapped,
        evidence=evidence, status=status,
        limits=limits, uncertainty=list(uncertainty),
        change_log=list(change_log), task_md_path=task_md_path, extra=extra)

    meta = payload.setdefault("meta", {})
    meta["contract_field_mapping"] = mapping_report
    meta["field_fill_ratio"] = (
        round(len(mapping_report) / len(declared), 4) if declared else 0.0)
    if declared:
        meta["declared_fields"] = [field["name"] for field in declared]

    keep = set(additional) if additional is not None else set(values)
    mapped_keys = set(mapping_report.values())
    extra_outputs = {key: values[key] for key in values
                     if key in keep and key not in mapped_keys}
    if extra_outputs:
        meta["additional_outputs"] = extra_outputs

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    result_path = contract.write_result(out, payload)

    if evidence is not None:
        write_json(out / "evidence.json", {"case_id": case_id, **evidence})
    if report is not None:
        write_json(out / "run_report.json", {"case_id": case_id, "status": status, **report})
    payload.setdefault("meta", {})["output_files"] = {
        "result": str(result_path),
        "evidence": str(out / "evidence.json") if evidence is not None else None,
        "run_report": str(out / "run_report.json") if report is not None else None,
    }
    # result.json is rewritten once more so output_files is visible inside it
    contract.write_result(out, payload)
    return payload


def summarize_docs(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for doc in docs:
        out.append({
            "path": doc.get("path"),
            "engine": doc.get("engine"),
            "anchor_quality": doc.get("anchor_quality"),
            "text_layer": doc.get("text_layer"),
            "pages": len(doc.get("pages") or []),
            "total_chars": doc.get("total_chars"),
            "attempts": doc.get("attempts"),
            "warnings": doc.get("warnings"),
        })
    return out


def run_report(*, stopwatch: Stopwatch, constants: dict[str, Any] | None = None,
               **extra: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "elapsed_s": stopwatch.elapsed(),
        "time_budget_s": TIME_BUDGET_S,
        "model_requests_used": 0,
        "generated_by": "deterministic pipeline + agent judgement batches",
    }
    if constants:
        report.update(constants)
    report.update(extra)
    return report
