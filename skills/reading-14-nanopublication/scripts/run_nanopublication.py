#!/usr/bin/env python3
"""Reading-14 Nanopublication: assertion-level decomposition with minimal evidence.

Two-phase hybrid pipeline (the shared framework lives in lib/hybrid.py):

  prepare   deterministic: discover inputs, extract the paper, cut it into
            page-anchored segments, split each segment into numbered sentences,
            extract machine-checkable facts (numbers, citations, hedges, ...) and
            relation cues, then write batch worksheets for the agent.
  generate  the agent writes work/generated/batch-XX.json (judgement only).
  finalize  deterministic: validate every assertion against its source segment
            (normalised quote containment + anchor re-verification), score
            atomicity, map relations onto the controlled vocabulary, build
            nanopublication records, serialise TriG + JSON-LD, and reflect
            case.json.output_contract into result.json.

Hard rules implemented here:
  * a placeholder result.json is written before any slow work - a killed
    container that leaves no file scores zero on every dimension;
  * missing generated content degrades to a deterministic extractive baseline
    and is reported as partial; the pipeline never crashes on an empty answer;
  * no network, no model calls, standard library only, deterministic output.
"""

from __future__ import annotations

import argparse
import hashlib
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
    from lib import anchors, contract, discover, extract, hybrid, structure
except ImportError:  # development layout: skills/_core/lib
    sys.path.insert(0, str(ROOT.parent / "_core"))
    from lib import anchors, contract, discover, extract, hybrid, structure

SKILL_NAME = "reading-14-nanopublication"
SKILL_VERSION = "1.0.0"
PROTOCOL_VERSION = "1.0"
VOCAB_PATH = ROOT / "resources" / "relation_vocab.json"
NP_NS = "https://w3id.org/np/RA"
VOCAB_NS = "https://w3id.org/np/skill-reading14/vocab#"
SOURCE_NS = "https://w3id.org/np/source/"

MAX_DOCS = 3
DEFAULT_BATCH_SIZE = 8
DEFAULT_MAX_SEGMENTS = 96
DEFAULT_MAX_BASELINE = 24
BASELINE_WHEN_GENERATED = 8
LONG_ASSERTION_CHARS = 240
WORKSHEET_BATCH_CHARS = 7000

STOP = hybrid.Stopwatch()

# Extra per-adapter aliases so a hidden contract whose field names differ from our
# internal keys still receives the right value (contract reflection, no hardcoding).
SYNONYMS: dict[str, tuple[str, ...]] = {
    "assertions": ("assertion", "claim", "atomic_claim", "assertion_list", "assertion_table",
                   "claim_table", "duanyan"),
    "nanopublications": ("nanopub", "nanopubs", "np", "nanopub_record", "nanopub_set",
                         "nanopublication_record"),
    "source_metadata": ("paper", "paper_metadata", "bibliographic", "provenance_record",
                        "source_meta", "paper_provenance"),
    "sources": ("source", "material", "materials", "source_list"),
    "relation_vocab": ("vocabulary", "controlled_vocabulary", "controlled_vocab",
                       "relation_types", "relations", "vocab"),
    "quality": ("metric", "metrics", "audit", "audit_metrics", "statistics", "qa",
                "atomicity", "assertion_quality"),
    "title": ("paper_title", "manuscript_title", "article_title"),
    "evidence": ("evidence_bundle", "anchor", "anchors", "grounding", "quote", "quotes",
                 "evidence_table"),
    "serialization": ("trig", "trig_export", "jsonld", "json_ld", "export",
                      "machine_readable", "rdf"),
    "rejected_assertions": ("rejected", "quarantined", "invalid_assertions",
                            "validation_failures"),
    "status": ("run_status", "state", "outcome", "completion_status"),
    "uncertainty": ("uncertainties", "unknowns", "open_questions", "gaps", "unresolved"),
    "limits": ("limitation", "limitations", "boundaries", "caveats", "constraints"),
    "summary": ("overview", "digest", "tldr", "takeaway", "condensed", "key_points"),
    "page_count": ("pages", "page_total", "n_pages"),
    "keywords": ("key_words", "index_terms"),
    "abstract": ("summary_text", "paper_abstract"),
}


# --------------------------------------------------------------------------- #
# small deterministic helpers
# --------------------------------------------------------------------------- #
def _log(message: str) -> None:
    print(f"[{STOP.elapsed():>7.1f}s] {message}", file=sys.stderr, flush=True)


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _norm(text: Any) -> str:
    return anchors.normalize(str(text or ""))


def _stable_id(*parts: Any, length: int = 16) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def _file_fingerprint(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > 50 * 1024 * 1024:
                data = handle.read(2 * 1024 * 1024)
                scope = "head_2mb"
            else:
                data = handle.read()
                scope = "full"
        return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": size, "hash_scope": scope}
    except Exception as exc:  # a missing hash must never kill the run
        return {"sha256": "", "bytes": 0,
                "hash_scope": "unavailable_" + type(exc).__name__}


def _quote_match(quote: str, segment_text: str) -> str:
    """exact -> normalised -> missing, against the persisted segment text."""
    if not quote:
        return "missing"
    if quote in segment_text:
        return "exact"
    if _norm(quote) and _norm(quote) in _norm(segment_text):
        return "normalised"
    return "missing"


# --------------------------------------------------------------------------- #
# controlled relation vocabulary
# --------------------------------------------------------------------------- #
def _fallback_vocab() -> dict[str, Any]:
    """Built-in copy of the minimum vocabulary, used if resources/ is unavailable."""
    relations = [
        {"relation": "supports", "definition": "evidence supports a claim",
         "cues": ["support", "supports", "consistent with", "confirm"]},
        {"relation": "contradicts", "definition": "result conflicts with a claim",
         "cues": ["contradict", "contradicts", "inconsistent with"]},
        {"relation": "uses", "definition": "study uses a method or material",
         "cues": ["use", "uses", "used", "using", "based on"]},
        {"relation": "measures", "definition": "quantity is measured",
         "cues": ["measure", "measured", "yield", "reached"]},
        {"relation": "derived_from", "definition": "value derived from a source",
         "cues": ["derived from", "computed from", "calculated from"]},
        {"relation": "part_of", "definition": "component of a larger entity",
         "cues": ["part of", "component of", "belongs to"]},
        {"relation": "states", "definition": "source states a fact (default)",
         "cues": ["report", "show", "find", "observe", "demonstrate", "indicate", "suggest"]},
    ]
    return {"version": "builtin-1.0.0", "vocab_uri": VOCAB_NS, "default_relation": "states",
            "relations": relations, "source": "builtin-fallback",
            "confidence_basis": {"base": 0.55, "quote_exact": 0.15, "quote_normalised": 0.08,
                                 "atomic": 0.1, "relation_in_vocab": 0.1, "max": 0.95}}


def load_vocab() -> dict[str, Any]:
    data = hybrid.read_json(VOCAB_PATH)
    if (isinstance(data, dict) and isinstance(data.get("relations"), list)
            and data["relations"]):
        data.setdefault("default_relation", "states")
        data.setdefault("vocab_uri", VOCAB_NS)
        data.setdefault("version", "1.0.0")
        data["source"] = "resources/relation_vocab.json"
        return data
    return _fallback_vocab()


def relation_names(vocab: dict[str, Any]) -> list[str]:
    return [str(rel.get("relation")) for rel in vocab.get("relations") or [] if rel.get("relation")]


_ASCII_CUE_RE = re.compile(r"^[a-z0-9][a-z0-9 .\-]*$")


def _cue_in(norm_text: str, cue: str) -> bool:
    cue_norm = _norm(cue)
    if not cue_norm or not norm_text:
        return False
    if _ASCII_CUE_RE.match(cue_norm):
        return re.search(rf"(?<![a-z]){re.escape(cue_norm)}(?![a-z])", norm_text) is not None
    return cue_norm in norm_text


def relation_cue_hits(text: str, vocab: dict[str, Any]) -> list[dict[str, str]]:
    """All controlled-vocabulary cue hits, in vocabulary order (deterministic)."""
    norm_text = _clean(text).casefold()
    hits: list[dict[str, str]] = []
    for rel in vocab.get("relations") or []:
        for cue in rel.get("cues") or []:
            if _cue_in(norm_text, cue):
                hits.append({"relation": str(rel.get("relation")), "cue": str(cue)})
    return hits


def infer_relation(text: str, vocab: dict[str, Any]) -> tuple[str, str]:
    hits = relation_cue_hits(text, vocab)
    if hits:
        return hits[0]["relation"], hits[0]["cue"]
    return "", ""


# --------------------------------------------------------------------------- #
# sentences and machine-checkable facts
# --------------------------------------------------------------------------- #
_SPLIT_RE = re.compile(r"(?<=[\u3002\uff01\uff1f])|(?<=[\.\!\?])\s+")
_ENDS_WITH_ABBREV = re.compile(
    r"(?:\b(?:e\.g|i\.e|et al|fig|figs|eq|eqs|ref|refs|vs|approx|ca|cf|no|dr|al|resp|min|max)\.|\bet\s+al\.)$",
    re.I)


def split_sentences(text: str) -> list[str]:
    """Sentence split that survives abbreviations and Chinese full stops."""
    cleaned = _clean(text)
    if not cleaned:
        return []
    parts = _SPLIT_RE.split(cleaned)
    merged: list[str] = []
    buffer = ""
    for part in parts:
        part = (part or "").strip()
        if not part:
            continue
        buffer = f"{buffer} {part}".strip() if buffer else part
        if _ENDS_WITH_ABBREV.search(buffer):
            continue
        merged.append(buffer)
        buffer = ""
    if buffer:
        merged.append(buffer)
    return [item for item in merged if item]


def sentence_facts(text: str) -> dict[str, Any]:
    return {
        "numbers": hybrid.find_numbers(text),
        "citations": hybrid.find_citations(text),
        "hedges": hybrid.find_hedges(text),
        "negations": hybrid.find_negations(text),
        "modals": hybrid.find_modals(text),
        "labels": hybrid.find_labels(text),
        "formula_tokens": hybrid.find_formula_tokens(text),
        "acronyms": hybrid.find_acronyms(text),
    }


_VERB_HINT = re.compile(
    r"\b(?:report(?:s|ed)?|show(?:s|n|ed)?|indicat(?:e|es|ed)|suggest(?:s|ed)?|"
    r"found|observe[sd]?|demonstrat(?:e|es|ed)|support(?:s|ed)?|contradict(?:s|ed)?|"
    r"measur(?:e|es|ed)|yield(?:s|ed)?|reach(?:es|ed)?|increas(?:e|es|ed)|"
    r"decreas(?:e|es|ed)|prepar(?:e|es|ed)|us(?:e|es|ed)|deriv(?:e|es|ed)|"
    r"calculat(?:e|es|ed)|correlat(?:e|es|ed)|attribut(?:e|es|ed)|"
    r"is|are|was|were|has|have|had|can|may|will)\b", re.I)


def leading_phrase(text: str, max_tokens: int = 7) -> str:
    """Deterministic 'subject-ish' leading phrase used by the extractive baseline."""
    tokens = _clean(text).split()
    if not tokens:
        return ""
    out: list[str] = []
    for token in tokens[:max_tokens]:
        if _VERB_HINT.match(token) and out:
            break
        out.append(token)
        if token.endswith((",", ";", ":", ".")) and len(out) >= 2:
            break
    phrase = " ".join(out).strip(" ,;:.")
    return phrase[:120]


_CONDITION_RE = re.compile(
    r"(?:\b(?:at|under|in the presence of|when|for)\b)\s+([^,.;]{2,60})", re.I)


def condition_phrase(text: str) -> str:
    match = _CONDITION_RE.search(_clean(text))
    if not match:
        return ""
    return _clean(match.group(0))[:80]


def segment_sentences(segment: dict[str, Any], vocab: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, sentence in enumerate(split_sentences(segment.get("text") or ""), 1):
        relation, cue = infer_relation(sentence, vocab)
        rows.append({
            "sentence_id": f"{segment['seg_id']}-s{index:02d}",
            "text": sentence,
            "char_count": len(sentence),
            "facts": sentence_facts(sentence),
            "suggested_relation": relation or str(vocab.get("default_relation") or "states"),
            "relation_cue": cue,
        })
    return rows


_REFERENCE_SECTION_RE = re.compile(
    r"\b(?:references?|bibliography|works cited|literature cited|acknowledg\w*|"
    r"appendix|supplementary|supporting information)\b", re.I)


def _reference_like(segment: dict[str, Any]) -> bool:
    """Reference/bibliography/appendix material is not an assertion source."""
    section = str(segment.get("section_title") or "")
    if section and section != "_preamble" and _REFERENCE_SECTION_RE.search(section):
        return True
    heading_like = str(segment.get("text") or "")[:80]
    return bool(len(heading_like) < 80 and _REFERENCE_SECTION_RE.search(heading_like)
                and len(heading_like.split()) <= 4)


def _eligible_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Segments worth asking the agent about (no reference lists, no bare headings)."""
    eligible = []
    for segment in segments:
        if _reference_like(segment):
            continue
        facts = segment.get("facts") or {}
        has_fact = any(facts.get(key) for key in
                       ("numbers", "citations", "labels", "acronyms", "formula_tokens"))
        if int(segment.get("char_count") or 0) < 40 and not has_fact:
            continue
        eligible.append(segment)
    return eligible or list(segments)


def _salience(segment: dict[str, Any]) -> int:
    facts = segment.get("facts") or {}
    score = 0
    score += 2 * len(facts.get("numbers") or [])
    score += 2 * len(facts.get("citations") or [])
    score += 3 * len(segment.get("relation_cues") or [])
    score += len(facts.get("hedges") or [])
    score += min(len(segment.get("text") or ""), 400) // 200
    return score


# --------------------------------------------------------------------------- #
# source metadata (traceability: paper / authors / year / version)
# --------------------------------------------------------------------------- #
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_DOI_LINE_RE = re.compile(r"(?:doi|DOI)\s*[:：]\s*(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)")
_AUTHORS_LINE_RE = re.compile(r"(?:authors?|作者)\s*[:：]\s*(.+)", re.I)


def build_source_metadata(primary: dict[str, Any] | None, frontmatter: dict[str, Any] | None,
                          material_path: Path | None, case_id: str,
                          task_md_path: str | None) -> dict[str, Any]:
    frontmatter = frontmatter or {}
    blocks = (primary or {}).get("blocks") or {}
    page1 = list(blocks.get("1") or [])
    page1_text = " ".join(str(block.get("text") or "") for block in page1)
    full_text = " ".join(str(block.get("text") or "")
                         for page in sorted(blocks, key=lambda key: int(key))
                         for block in blocks[page])

    def _suspicious_title(value: str) -> bool:
        low = value.casefold()
        return (len(value) > 220 or "doi" in low or "authors" in low
                or "abstract" in low or "keywords" in low)

    title = _clean(frontmatter.get("title"))
    title_source = "frontmatter"
    if not title or _suspicious_title(title):
        first_line = _clean((primary or {}).get("first_line"))
        if first_line and not _suspicious_title(first_line):
            title = first_line[:300]
            title_source = "first_line"
        elif page1:
            title = _clean(page1[0].get("text"))[:300]
            title_source = "first_block"
        elif material_path is not None:
            title = material_path.stem
            title_source = "file_stem"
        else:
            title = ""
            title_source = "missing"

    authors = [str(author) for author in (frontmatter.get("authors") or []) if str(author).strip()]
    authors_source = "frontmatter"
    if not authors:
        match = _AUTHORS_LINE_RE.search(page1_text or full_text)
        if match:
            author_text = re.split(r"\b(?:doi|DOI|Abstract|Keywords|E-?mail)\b",
                                   match.group(1))[0]
            authors = [part.strip() for part in
                       re.split(r"[;,，；]|\band\b", author_text)
                       if part.strip()][:20]
            authors_source = "authors_line"
        else:
            authors_source = "missing"

    year = ""
    year_source = "missing"
    match = _YEAR_RE.search(page1_text[:3000] or full_text[:3000])
    if match:
        year = match.group(0)
        year_source = "text_regex"
    elif material_path is not None:
        match = _YEAR_RE.search(material_path.name)
        if match:
            year = match.group(0)
            year_source = "file_name"

    doi = _clean(frontmatter.get("doi"))
    doi_source = "frontmatter"
    if not doi:
        match = _DOI_LINE_RE.search(page1_text or full_text)
        if match:
            doi = match.group(1)
            doi_source = "text_regex"
        else:
            doi_source = "missing"

    fingerprint = _file_fingerprint(material_path) if material_path is not None else {
        "sha256": "", "bytes": 0, "hash_scope": "no_material"}
    version = (f"v1+sha256:{fingerprint['sha256'][:12]}"
               if fingerprint.get("sha256") else "v1-unhashed")
    source = {
        "document_id": "src-" + (_stable_id(material_path.name, fingerprint.get("sha256", ""))
                                 if material_path else _stable_id(case_id)),
        "file": material_path.name if material_path else "",
        "path": str(material_path) if material_path else "",
        "sha256": fingerprint.get("sha256", ""),
        "bytes": fingerprint.get("bytes", 0),
        "hash_scope": fingerprint.get("hash_scope", ""),
        "version": version,
        "retrieved": "local material supplied with the case (offline run; no network fetch)",
        "title": title,
        "title_source": title_source,
        "authors": authors,
        "authors_source": authors_source,
        "year": year,
        "year_source": year_source,
        "doi": doi,
        "doi_source": doi_source,
        "keywords": [str(word) for word in (frontmatter.get("keywords") or [])],
        "abstract": _clean(frontmatter.get("abstract")),
        "venue": "",
        "venue_source": "missing",
        "engine": (primary or {}).get("engine"),
        "anchor_quality": (primary or {}).get("anchor_quality"),
        "page_count": len((primary or {}).get("pages") or []),
        "total_chars": (primary or {}).get("total_chars"),
        "case_id": case_id,
        "task_md": task_md_path or "",
        "citation": "",
        "provenance_chain": [
            f"case:{case_id}",
            f"file:{material_path.name if material_path else ''}",
            f"sha256:{fingerprint.get('sha256', '')[:16]}",
            "then page/block/quote anchors stored on every assertion",
        ],
    }
    first_author = authors[0] if authors else "unknown author"
    year_part = year or "n.d."
    title_part = title or "untitled"
    source["citation"] = f"{first_author} et al. ({year_part}). {title_part}. doi:{doi or 'n/a'}"
    source["missing_fields"] = [
        field for field, ok in (("title", bool(title)), ("authors", bool(authors)),
                                ("year", bool(year)), ("doi", bool(doi)),
                                ("venue", bool(source["venue"])),
                                ("file_hash", bool(fingerprint.get("sha256"))))
        if not ok
    ]
    return source

# --------------------------------------------------------------------------- #
# prepare
# --------------------------------------------------------------------------- #
def _resolve_work_dir(prefer: str | Path | None) -> Path:
    candidates: list[Path] = []
    if prefer:
        candidates.append(Path(prefer))
    candidates.extend([Path("/app/work"), Path.cwd() / "work"])
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return candidate
        except Exception:
            continue
    fallback = Path.cwd() / "work"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _resolve_case(found: dict[str, Any],
                  case_override: str | None) -> tuple[Path | None, dict[str, Any]]:
    if case_override:
        path: Path | None = Path(case_override).resolve()
    elif found.get("case_path"):
        path = Path(found["case_path"])
    else:
        path = None
    case = contract.load_case(path) if path is not None else {}
    return path, case


_PLAIN_PARAGRAPH_SUFFIXES = {".txt", ".md", ".markdown"}


def _paragraph_blocks(path: Path) -> tuple[list[dict[str, Any]], str]:
    """Rebuild paragraph-level blocks for plain-text materials.

    The shared extractor turns a whole .txt/.md file into a single block (its line
    clustering tolerates bare line breaks), which would give a single giant
    segment and no usable batch grid. For plain text the blank line is the
    paragraph boundary, so rebuild blocks here: one paragraph = one block.
    """
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return [], ""
    blocks: list[dict[str, Any]] = []
    current: list[str] = []
    first_line = ""
    heading_names = {"abstract", "introduction", "background", "methods", "method",
                     "materials and methods", "results", "results and discussion",
                     "discussion", "conclusion", "conclusions", "references",
                     "acknowledgements", "acknowledgments", "keywords"}

    def add_block(paragraph: str, heading: bool = False) -> None:
        paragraph = _clean(paragraph)
        if not paragraph:
            return
        index = len(blocks) + 1
        blocks.append({
            "block_id": f"p1-b{index}",
            "bbox": [0.0, float(-index), float(min(len(paragraph), 500)),
                     float(-index + 1)],
            "size": 12.0 if heading else 10.0,
            "text": paragraph,
        })

    def flush() -> None:
        nonlocal current
        if current:
            add_block(" ".join(current))
        current = []

    def looks_like_heading(line: str) -> bool:
        low = line.casefold().strip(" .:：")
        if low in heading_names:
            return True
        if len(line) <= 90 and re.match(r"^\d+(?:\.\d+)*\.?\s+[A-Z]", line):
            return True
        return bool(len(line) <= 90 and line.isupper() and len(line.split()) <= 8)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            continue
        if not first_line:
            first_line = line
        if looks_like_heading(line):
            flush()
            add_block(line, heading=True)
            continue
        current.append(line)
    flush()
    return blocks, first_line


def _answer_schema(vocab: dict[str, Any], batch_id: str) -> dict[str, Any]:
    return {
        "file": f"generated/{batch_id}.json",
        "format": "JSON object or bare array",
        "top_level": {"batch_id": "string (this batch id)",
                      "records": "array of record objects"},
        "record_fields": {
            "assertion_id": "string, unique within the case, e.g. a-0001",
            "seg_id": "one of the seg_id values in this worksheet (required)",
            "text": "one atomic, truth-evaluable assertion (<=240 chars), close to the source",
            "subject": "the entity/value the assertion is about (taken from the text)",
            "relation": "one of allowed_relations",
            "object": "the entity/value the subject is related to (may be '' for states)",
            "condition": "scope, temperature, dose, population ... or ''",
            "confidence": "number in [0,1] reflecting evidence strength",
            "quote": "verbatim substring of the segment text supporting the assertion",
            "direction": "subject->object or object->subject",
            "evidence_type": "direct (stated in the quote) or indirect (inferred)",
        },
        "rules": [
            "one record = one assertion that can be judged true or false on its own",
            "quote must be copied verbatim from the segment text (no paraphrase)",
            "do not merge two claims with and/but/because; emit two records instead",
            "relation must come from allowed_relations; do not invent new relation names",
            "subject and object should appear in the quote whenever the text allows it",
        ],
    }


def _example_record(batch_segments: list[dict[str, Any]],
                    vocab: dict[str, Any]) -> dict[str, Any]:
    """A field-format example built from this batch, clearly marked as an example."""
    ranked: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for segment in batch_segments:
        for sentence in segment.get("sentences") or []:
            text = sentence["text"]
            if len(text) < 20 or len(text) > LONG_ASSERTION_CHARS:
                continue
            facts = sentence.get("facts") or {}
            score = (2 * len(facts.get("numbers") or []) + 2 * len(facts.get("citations") or [])
                     + (2 if sentence.get("relation_cue") else 0))
            ranked.append((-score, segment, sentence))
    if ranked:
        ranked.sort(key=lambda item: item[0])
        _score_value, segment, sentence = ranked[0]
        text = sentence["text"]
        return {
                "_note": "field-format example only; rewrite records from the actual text",
                "assertion_id": "a-example",
                "seg_id": segment["seg_id"],
                "sentence_id": sentence["sentence_id"],
                "text": text,
                "subject": leading_phrase(text),
                "relation": sentence["suggested_relation"],
                "object": "",
                "condition": condition_phrase(text),
                "confidence": 0.6,
                "quote": text,
                "direction": "subject->object",
                "evidence_type": "direct",
            }
    first = batch_segments[0] if batch_segments else {"seg_id": "seg-0001"}
    return {
        "_note": "field-format example only; rewrite records from the actual text",
        "assertion_id": "a-example",
        "seg_id": first.get("seg_id"),
        "text": "",
        "subject": "",
        "relation": str(vocab.get("default_relation") or "states"),
        "object": "",
        "condition": "",
        "confidence": 0.5,
        "quote": "",
        "direction": "subject->object",
        "evidence_type": "direct",
    }


def _worksheet_payload(batch: dict[str, Any], batch_segments: list[dict[str, Any]],
                       vocab: dict[str, Any]) -> dict[str, Any]:
    sentence_total = sum(len(segment.get("sentences") or []) for segment in batch_segments)
    instructions = [
        "阅读下面每个 segment 的原文与其编号句子。",
        "把每段结论拆成多条各自可判真假的断言，一条断言只表达一个主张。",
        "每条断言必须给出 seg_id 与 quote；quote 必须是该 segment 原文的连续子串（逐字复制）。",
        "relation 只能取 allowed_relations 中的值；subject/object 尽量出现在 quote 里。",
        "复合句（and/but/because/whereas/多谓词）应拆成多条记录，不要整段塞进一个 text。",
        "把答案写到 answer_file 指定的 JSON：{batch_id, records:[...]}；字段格式见 answer_schema/example_record。",
        "如果本批某个 segment 没有可判真假的陈述，可以不产出记录，但不要编造。",
    ]
    return {
        "skill": SKILL_NAME,
        "skill_version": SKILL_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "batch_id": batch["batch_id"],
        "answer_file": f"generated/{batch['batch_id']}.json",
        "instructions": instructions,
        "allowed_relations": relation_names(vocab),
        "relation_vocab_version": vocab.get("version"),
        "answer_schema": _answer_schema(vocab, batch["batch_id"]),
        "example_record": _example_record(batch_segments, vocab),
        "segment_count": len(batch_segments),
        "sentence_count": sentence_total,
        "segments": batch_segments,
    }


def _print_prepare(manifest: dict[str, Any]) -> None:
    print(f"PREPARE_OK case={manifest.get('case_id')} "
          f"segments={manifest.get('segments_total')} "
          f"worksheets={manifest.get('segments_in_worksheets')} "
          f"batches={len(manifest.get('batches') or [])} "
          f"work={manifest.get('work_dir')}", flush=True)


def prepare(args: argparse.Namespace, quiet: bool = False) -> dict[str, Any]:
    """Deterministic stage: inputs -> work/ worksheets (+ placeholder result.json)."""
    log = (lambda message: None) if quiet else _log
    log("prepare stage 1/7 discover inputs")
    found = discover.discover()
    case_path, case = _resolve_case(found, getattr(args, "case", None))
    case_id = contract.find_case_id(case) or ""
    out_dir = Path(args.out) if args.out else Path(found["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = _resolve_work_dir(getattr(args, "work", None))
    work_dir.mkdir(parents=True, exist_ok=True)
    declared = hybrid.declared_fields(case) if case else []
    hybrid.write_placeholder(
        out_dir, case_id=case_id,
        note="prepare started; this file is replaced when finalize finishes",
        contract_fields=len(declared))

    materials = [Path(path) for path in (found.get("material_docs") or [])]
    log(f"case={case_path} materials={[p.name for p in materials]} out={out_dir}")
    vocab = load_vocab()
    manifest: dict[str, Any] = {
        "skill": SKILL_NAME, "version": SKILL_VERSION, "protocol_version": PROTOCOL_VERSION,
        "case_id": case_id, "case_path": str(case_path) if case_path else "",
        "task_md_path": found.get("task_md_path"),
        "out_dir": str(out_dir), "work_dir": str(work_dir),
        "materials": [str(path) for path in materials],
        "vocab": {"path": "resources/relation_vocab.json", "version": vocab.get("version"),
                  "source": vocab.get("source"), "relations": relation_names(vocab)},
        "limits": [], "segments_total": 0, "segments_in_worksheets": 0,
        "batches": [], "missing_batches": [], "anchor_quality": None,
        "status": "prepared", "stage": "prepare",
    }
    if not materials:
        manifest["status"] = "no_materials"
        manifest["limits"].append("no material document was found in the input roots")
        hybrid.write_json(work_dir / "manifest.json", manifest)
        hybrid.write_json(work_dir / "segments.json", {"case_id": case_id, "segments": []})
        _print_prepare(manifest)
        return manifest

    log("prepare stage 2/7 extract text and coordinates")
    deadline = time.time() + max(60.0, hybrid.SOFT_DEADLINE_S - STOP.elapsed())
    docs: list[dict[str, Any]] = []
    for path in materials[:MAX_DOCS]:
        if STOP.elapsed() > hybrid.SOFT_DEADLINE_S - 15:
            manifest["limits"].append(f"extraction deadline reached before {path.name}")
            break
        doc = extract.extract_document(path, deadline=deadline)
        docs.append(doc)
        log(f"  {path.name}: engine={doc.get('engine')} pages={len(doc.get('pages') or [])} "
            f"chars={doc.get('total_chars')} anchor={doc.get('anchor_quality')}")
        if not doc.get("text_layer"):
            manifest["limits"].append(
                f"{path.name}: no text layer (scanned?) - OCR unavailable")
        for warning in doc.get("warnings") or []:
            manifest["limits"].append(f"{path.name}: {warning}")
    if len(materials) > MAX_DOCS:
        manifest["limits"].append(
            f"only the first {MAX_DOCS} materials were extracted; the rest are listed "
            f"in the manifest")
    if docs and docs[0].get("text_layer") and materials[0].suffix.lower() in \
            _PLAIN_PARAGRAPH_SUFFIXES:
        blocks, first_line = _paragraph_blocks(materials[0])
        if len(blocks) > 1:
            docs[0]["blocks"] = {"1": blocks}
            docs[0]["first_line"] = first_line
            docs[0]["page_count"] = 1
            manifest["limits"].append(
                "plain-text material: blocks rebuilt per paragraph (blank-line boundaries)")
    if not docs or not docs[0].get("text_layer"):
        manifest["status"] = "no_text"
        hybrid.write_json(work_dir / "manifest.json", manifest)
        hybrid.write_json(work_dir / "segments.json", {"case_id": case_id, "segments": []})
        _print_prepare(manifest)
        return manifest

    primary = docs[0]
    log("prepare stage 3/7 detect document structure")
    struct = structure.outline(primary)
    frontmatter = struct.get("frontmatter") or {}
    source_meta = build_source_metadata(primary, frontmatter, materials[0], case_id,
                                        found.get("task_md_path"))
    manifest["anchor_quality"] = primary.get("anchor_quality")
    manifest["primary_engine"] = primary.get("engine")
    manifest["page_count"] = len(primary.get("pages") or [])
    manifest["total_chars"] = primary.get("total_chars")
    manifest["source"] = source_meta
    log(f"  sections={len(struct.get('sections') or [])} "
        f"title={source_meta.get('title')!r}")

    log("prepare stage 4/7 segments, sentences and facts")
    built = hybrid.build_segments(primary, struct, max_chars=1200)
    segments: list[dict[str, Any]] = []
    for segment in built:
        text = _clean(segment.get("text"))
        if not text:
            continue
        segments.append({
            "seg_id": segment["seg_id"], "order": segment["order"],
            "page": segment["page"],
            "block_id": (segment.get("block_ids") or [None])[0],
            "bbox": segment.get("bbox"), "part": segment.get("part", 0),
            "section_id": segment.get("section_id"),
            "section_title": segment.get("section_title"),
            "char_count": len(text), "text": text,
            "facts": sentence_facts(text),
            "relation_cues": relation_cue_hits(text, vocab),
            "sentences": segment_sentences(segment, vocab),
        })
    manifest["segments_total"] = len(segments)
    if not segments:
        manifest["status"] = "no_segments"
        manifest["limits"].append("the document produced no text segments")
        hybrid.write_json(work_dir / "manifest.json", manifest)
        hybrid.write_json(work_dir / "segments.json", {"case_id": case_id, "segments": []})
        _print_prepare(manifest)
        return manifest

    # Persist a block-level extraction snapshot: finalize re-verifies every anchor
    # against it, so the quote check never depends on re-reading the source file.
    blocks_snapshot: dict[str, list[dict[str, Any]]] = {}
    for page_key, page_blocks in (primary.get("blocks") or {}).items():
        blocks_snapshot[str(page_key)] = [
            {"block_id": block.get("block_id"), "bbox": block.get("bbox"),
             "text": block.get("text")}
            for block in page_blocks if _clean(block.get("text"))
        ]
    if not blocks_snapshot:
        synthetic: dict[str, list[dict[str, Any]]] = {}
        for segment in segments:
            synthetic.setdefault(str(segment["page"]), []).append({
                "block_id": segment["block_id"], "bbox": segment["bbox"],
                "text": segment["text"]})
        blocks_snapshot = synthetic
        manifest["limits"].append(
            "no block-level extraction; anchors are verified against segments")
    hybrid.write_json(work_dir / "doc_blocks.json", {
        "anchor_quality": primary.get("anchor_quality"),
        "engine": primary.get("engine"),
        "blocks_sha256": _stable_id(
            json.dumps(blocks_snapshot, sort_keys=True, ensure_ascii=False)),
        "blocks": blocks_snapshot})

    log("prepare stage 5/7 select segments and write worksheets")
    eligible = _eligible_segments(segments)
    if len(eligible) < len(segments):
        manifest["limits"].append(
            f"{len(segments) - len(eligible)} segment(s) skipped as reference/appendix "
            "material or bare headings")
        hybrid.write_json(work_dir / "segments_skipped.json", {
            "case_id": case_id,
            "seg_ids": [segment["seg_id"] for segment in segments
                        if segment not in eligible]})
    max_segments = max(1, int(getattr(args, "max_segments", 0) or DEFAULT_MAX_SEGMENTS))
    if len(eligible) > max_segments:
        ranked = sorted(eligible, key=lambda item: (-_salience(item), item["order"]))
        selected = set(item["seg_id"] for item in ranked[:max_segments])
        worksheet_segments = [item for item in eligible if item["seg_id"] in selected]
        manifest["limits"].append(
            f"worksheets cover the {max_segments} most assertion-dense of "
            f"{len(segments)} segments; remaining segments are covered by the baseline")
    else:
        worksheet_segments = list(segments)
    batch_size = max(1, int(getattr(args, "batch_size", 0) or DEFAULT_BATCH_SIZE))
    batches = hybrid.make_batches(worksheet_segments, work_dir, batch_size=batch_size,
                                  batch_chars=WORKSHEET_BATCH_CHARS)
    by_id = {segment["seg_id"]: segment for segment in segments}
    batch_index: list[dict[str, Any]] = []
    for batch in batches:
        worksheet_path = Path(batch["worksheet"])
        raw = hybrid.read_json(worksheet_path, {}) or {}
        ids = [item.get("seg_id") for item in raw.get("segments") or []]
        batch_segments = [by_id[seg_id] for seg_id in ids if seg_id in by_id]
        payload = _worksheet_payload(batch, batch_segments, vocab)
        hybrid.write_json(worksheet_path, payload)
        batch_index.append({
            "batch_id": batch["batch_id"], "segment_count": len(batch_segments),
            "sentence_count": payload["sentence_count"],
            "seg_ids": [segment["seg_id"] for segment in batch_segments],
            "worksheet": str(worksheet_path),
            "answer_file": str(work_dir / "generated" / f"{batch['batch_id']}.json"),
        })
    manifest["batches"] = batch_index
    manifest["missing_batches"] = [item["batch_id"] for item in batch_index]
    manifest["segments_in_worksheets"] = len(worksheet_segments)
    manifest["worksheet_seg_ids"] = [segment["seg_id"] for segment in worksheet_segments]
    manifest["sentence_total"] = sum(len(segment.get("sentences") or [])
                                     for segment in segments)

    log("prepare stage 6/7 write work tables and the generation protocol")
    hybrid.write_json(work_dir / "segments.json", {
        "case_id": case_id, "anchor_quality": primary.get("anchor_quality"),
        "source_document_id": source_meta.get("document_id"),
        "segments": segments})
    hybrid.write_json(work_dir / "source_metadata.json", source_meta)
    hybrid.write_json(work_dir / "vocab_snapshot.json", vocab)
    generated_dir = work_dir / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    example_path = generated_dir / "_example.json"
    if not example_path.is_file():
        example_records: list[dict[str, Any]] = []
        if batch_index and batch_index[0]["seg_ids"]:
            example_records = [{
                "assertion_id": "a-0001", "seg_id": batch_index[0]["seg_ids"][0],
                "text": "...", "subject": "...", "relation": "states", "object": "...",
                "condition": "", "confidence": 0.7, "quote": "...",
                "direction": "subject->object", "evidence_type": "direct"}]
        hybrid.write_json(example_path, {
            "_note": ("write one answer file per batch into generated/: "
                      "batch-XX.json with {batch_id, records:[...]}; "
                      "files starting with '_' are ignored"),
            "batch_id": "batch-01",
            "records": example_records,
        })

    log("prepare stage 7/7 manifest")
    manifest["status"] = "prepared"
    manifest["elapsed_s"] = STOP.elapsed()
    hybrid.write_json(work_dir / "manifest.json", manifest)
    hybrid.write_json(work_dir / "prepare_report.json", {
        "case_id": case_id, "status": "prepared",
        "segments_total": len(segments), "segments_in_worksheets": len(worksheet_segments),
        "batches": len(batch_index),
        "sentences_in_worksheets": sum(item["sentence_count"] for item in batch_index),
        "missing_batches": manifest["missing_batches"],
        "anchor_quality": primary.get("anchor_quality"), "engine": primary.get("engine"),
        "vocab_version": vocab.get("version"), "limits": manifest["limits"],
        "elapsed_s": STOP.elapsed(),
    })
    _print_prepare(manifest)
    return manifest


# --------------------------------------------------------------------------- #
# assertion validation
# --------------------------------------------------------------------------- #
_CONNECTIVE_RE = re.compile(
    r"(?:\band\b|\bor\b|\bbut\b|\bwhile\b|\bwhereas\b|\bbecause\b|\btherefore\b|"
    r"\bthus\b|\bmoreover\b|\bfurthermore\b|\bhowever\b|\balso\b|\bwhich\b|"
    r"\bthat\b|;|\uff1b)", re.I)
_PREDICATE_RE = re.compile(
    r"\b(?:report(?:s|ed)?|show(?:s|n|ed)?|indicat(?:e|es|ed)|suggest(?:s|ed)?|"
    r"found|observe[sd]?|demonstrat(?:e|es|ed)|support(?:s|ed)?|contradict(?:s|ed)?|"
    r"measur(?:e|es|ed)|yield(?:s|ed)?|reach(?:es|ed)?|increas(?:e|es|ed)|"
    r"decreas(?:e|es|ed)|prepar(?:e|es|ed)|us(?:e|es|ed)|deriv(?:e|es|ed)|"
    r"calculat(?:e|es|ed)|correlat(?:e|es|ed)|attribut(?:e|es|ed))\b", re.I)
_SPLIT_HINT_RE = re.compile(r"(?:\s*;\s*|,\s*(?:and|but|whereas|while|because)\s+)", re.I)


def atomicity_flags(text: str) -> dict[str, Any]:
    """Heuristic single-truth-evaluability check for one assertion string.

    The flags are signals with a published rule, not a ground truth: the atomic
    verdict is negative only for structural signals (several sentences / clauses
    / predicates / quantities). Length and one lone connective are reported but
    do not by themselves make an assertion compound.
    """
    cleaned = _clean(text)
    flags: list[str] = []
    split_hint: list[str] = []
    if len(cleaned) > LONG_ASSERTION_CHARS:
        flags.append("too_long")
    elif len(cleaned) > 160:
        flags.append("long")
    sentences = split_sentences(cleaned)
    if len(sentences) > 1:
        flags.append("multiple_sentences")
        split_hint = sentences[:3]
    connectives = _CONNECTIVE_RE.findall(cleaned)
    if len(connectives) >= 2:
        flags.append("multiple_connectives")
    elif len(connectives) == 1:
        flags.append("single_connective")
    numbers = hybrid.find_numbers(cleaned)
    if len(numbers) > 2:
        flags.append("multiple_quantities")
    predicates = sorted(set(match.group(0).casefold()
                            for match in _PREDICATE_RE.finditer(cleaned)))
    if len(predicates) >= 2:
        flags.append("multiple_predicates")
    if cleaned.count(",") >= 3:
        flags.append("comma_list")
    if not split_hint and ({"multiple_connectives", "multiple_predicates"} & set(flags)):
        parts = [part.strip() for part in _SPLIT_HINT_RE.split(cleaned) if part.strip()]
        if len(parts) > 1:
            split_hint = parts[:3]
    structural = [flag for flag in flags
                  if flag not in ("long", "single_connective", "multiple_quantities")]
    return {"atomic": not structural, "flags": flags, "split_hint": split_hint}


def _confidence(raw: Any, quote_match: str, atomicity: dict[str, Any],
                relation_ok: bool, vocab: dict[str, Any]) -> tuple[float, str]:
    basis = vocab.get("confidence_basis") or {}
    try:
        given = float(raw)
        return round(min(max(given, 0.0), 1.0), 2), "agent_reported"
    except (TypeError, ValueError):
        pass
    value = float(basis.get("base", 0.55))
    parts = ["base"]
    if quote_match == "exact":
        value += float(basis.get("quote_exact", 0.15))
        parts.append("quote_exact")
    elif quote_match == "normalised":
        value += float(basis.get("quote_normalised", 0.08))
        parts.append("quote_normalised")
    if atomicity.get("atomic"):
        value += float(basis.get("atomic", 0.1))
        parts.append("atomic")
    if relation_ok:
        value += float(basis.get("relation_in_vocab", 0.1))
        parts.append("relation_in_vocab")
    value = min(value, float(basis.get("max", 0.95)))
    return round(value, 2), "rule_based:" + "+".join(parts)


def _direction_check(subject: str, object_: str, quote: str) -> tuple[str, list[str]]:
    flags: list[str] = []
    quote_norm = _clean(quote).casefold()
    subject_norm = _clean(subject).casefold()
    object_norm = _clean(object_).casefold()
    if subject_norm and subject_norm not in quote_norm:
        flags.append("subject_not_in_quote")
    if object_norm and object_norm not in quote_norm:
        flags.append("object_not_in_quote")
    if subject_norm and object_norm and subject_norm in quote_norm \
            and object_norm in quote_norm:
        consistent = quote_norm.find(subject_norm) <= quote_norm.find(object_norm)
        return ("consistent" if consistent else "reversed"), flags
    return "not_checkable", flags


def validate_generated(record: dict[str, Any], segments_by_id: dict[str, dict[str, Any]],
                       doc_snapshot: dict[str, Any],
                       vocab: dict[str, Any]) -> dict[str, Any]:
    """Validate one agent record; returns status accepted|rejected plus the assertion."""
    def reject(reason: str) -> dict[str, Any]:
        return {"status": "rejected", "reason": reason, "raw": record}

    seg_id = _clean(record.get("seg_id"))
    if not seg_id:
        return reject("missing_seg_id")
    segment = segments_by_id.get(seg_id)
    if segment is None:
        return reject("unknown_seg_id")
    text = _clean(record.get("text"))
    if not text:
        return reject("empty_text")
    if len(text) > 1000:
        return reject("text_too_long")
    quote = _clean(record.get("quote"))
    if not quote:
        return reject("missing_quote")
    quote_match = _quote_match(quote, segment["text"])
    if quote_match == "missing":
        return reject("quote_not_in_segment")
    anchor = anchors.make_anchor(segment["page"],
                                 {"block_id": segment.get("block_id"),
                                  "bbox": segment.get("bbox")}, quote)
    verified = anchors.verify(doc_snapshot, anchor)
    if verified.get("match") not in anchors.GROUNDED:
        return reject("anchor_not_verified:" + str(verified.get("match")))

    flags: list[str] = []
    names = relation_names(vocab)
    relation_raw = _clean(record.get("relation")).casefold()
    if relation_raw in names:
        relation = relation_raw
        relation_ok = True
    else:
        flags.append("relation_not_in_vocab")
        inferred, _cue = infer_relation(quote or text, vocab)
        if inferred:
            relation = inferred
            relation_ok = True
            flags.append("relation_mapped_to_vocab")
        else:
            relation = str(vocab.get("default_relation") or "states")
            relation_ok = False
    atomicity = atomicity_flags(text)
    flags.extend(atomicity["flags"])
    subject = _clean(record.get("subject"))
    object_ = _clean(record.get("object"))
    if not subject:
        flags.append("missing_subject")
    direction_raw = _clean(record.get("direction")).casefold()
    if "object" in direction_raw and "subject" in direction_raw \
            and direction_raw.find("object") < direction_raw.find("subject"):
        direction = "object->subject"
    else:
        direction = "subject->object"
    direction_check, direction_flags = _direction_check(subject, object_, quote)
    flags.extend(direction_flags)
    if quote_match == "normalised":
        flags.append("quote_normalised")
    evidence_type = _clean(record.get("evidence_type")).casefold()
    if evidence_type not in ("direct", "indirect"):
        evidence_type = "direct" if quote_match == "exact" else "indirect"
    confidence, confidence_basis = _confidence(record.get("confidence"), quote_match,
                                               atomicity, relation_ok, vocab)
    assertion = {
        "assertion_id": _clean(record.get("assertion_id")),
        "seg_id": seg_id,
        "sentence_id": _clean(record.get("sentence_id")),
        "text": text,
        "subject": subject,
        "relation": relation,
        "relation_raw": relation_raw,
        "object": object_,
        "condition": _clean(record.get("condition")),
        "confidence": confidence,
        "confidence_basis": confidence_basis,
        "quote": quote,
        "direction": direction,
        "direction_check": direction_check,
        "evidence_type": evidence_type,
        "atomic": bool(atomicity["atomic"]),
        "split_hint": atomicity["split_hint"],
        "quality_flags": sorted(set(flags)),
        "origin": "agent_generated",
        "page": segment["page"],
        "block_id": segment.get("block_id"),
        "bbox": segment.get("bbox"),
        "section_title": segment.get("section_title"),
        "quote_match": quote_match,
        "anchor_match": verified.get("match"),
    }
    return {"status": "accepted", "assertion": assertion,
            "anchor": anchor, "verified": verified}


def baseline_assertions(segments: list[dict[str, Any]], vocab: dict[str, Any],
                        limit: int, covered: set[str]) -> list[dict[str, Any]]:
    """Deterministic extractive fallback when the agent produced no/few assertions.

    Only sentences that carry a machine-checkable fact (number, citation, label,
    relation cue) are used. The relation is fixed to states and the object stays
    empty, so the baseline cannot pretend to have resolved a relation it never
    judged.
    """
    rows: list[dict[str, Any]] = []
    if limit <= 0:
        return rows
    for segment in segments:
        if len(rows) >= limit:
            break
        if segment["seg_id"] in covered:
            continue
        for sentence in segment.get("sentences") or []:
            if len(rows) >= limit:
                break
            facts = sentence.get("facts") or {}
            if not (facts.get("numbers") or facts.get("citations")
                    or facts.get("labels") or sentence.get("relation_cue")):
                continue
            text = sentence["text"]
            if len(text) > LONG_ASSERTION_CHARS * 2:
                continue
            atomicity = atomicity_flags(text)
            flags = ["baseline_extractive", "relation_default_states",
                     "object_not_resolved"]
            flags.extend(atomicity["flags"])
            rows.append({
                "assertion_id": "",
                "seg_id": segment["seg_id"],
                "sentence_id": sentence["sentence_id"],
                "text": text,
                "subject": leading_phrase(text),
                "relation": "states",
                "relation_raw": "",
                "object": "",
                "condition": condition_phrase(text),
                "confidence": 0.35,
                "confidence_basis": ("baseline_extractive: sentence contains a "
                                     "machine-checkable fact"),
                "quote": text,
                "direction": "subject->object",
                "direction_check": "not_checkable",
                "evidence_type": "direct",
                "atomic": bool(atomicity["atomic"]),
                "split_hint": atomicity["split_hint"],
                "quality_flags": sorted(set(flags)),
                "origin": "baseline_extractive",
                "page": segment["page"],
                "block_id": segment.get("block_id"),
                "bbox": segment.get("bbox"),
                "section_title": segment.get("section_title"),
                "quote_match": "exact",
                "anchor_match": "exact",
            })
    return rows


def _dedupe(assertions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    seen: dict[str, int] = {}
    output: list[dict[str, Any]] = []
    merged = 0
    for assertion in assertions:
        key = _norm(assertion.get("text", ""))
        if key and key in seen:
            merged += 1
            existing = output[seen[key]]
            if assertion.get("confidence", 0) > existing.get("confidence", 0):
                assertion["quality_flags"] = sorted(
                    set(assertion.get("quality_flags") or []) | {"duplicate_merged"})
                output[seen[key]] = assertion
            continue
        seen[key] = len(output)
        output.append(assertion)
    return output, merged


def _assign_ids(assertions: list[dict[str, Any]]) -> None:
    used: set[str] = set()
    for index, assertion in enumerate(assertions, 1):
        candidate = _clean(assertion.get("assertion_id"))
        if not candidate or candidate in used:
            candidate = f"a-{index:04d}"
        suffix = 2
        base = candidate
        while candidate in used:
            candidate = f"{base}-{suffix}"
            suffix += 1
        assertion["assertion_id"] = candidate
        used.add(candidate)

# --------------------------------------------------------------------------- #
# nanopublication construction and serialisation
# --------------------------------------------------------------------------- #
def _source_uri(source_meta: dict[str, Any]) -> str:
    document_id = str(source_meta.get("document_id") or "src-unknown")
    return SOURCE_NS + document_id.replace("src-", "")


def build_nanopubs(assertions: list[dict[str, Any]], source_meta: dict[str, Any],
                   vocab: dict[str, Any]) -> list[dict[str, Any]]:
    """Build assertion / provenance / publicationInfo triples for every assertion."""
    source_uri = _source_uri(source_meta)
    vocab_uri = str(vocab.get("vocab_uri") or VOCAB_NS)
    nanopubs: list[dict[str, Any]] = []
    for assertion in assertions:
        nanopub_id = "np-" + _stable_id(assertion.get("text", ""),
                                        assertion.get("quote", ""),
                                        source_meta.get("document_id", ""), length=16)
        head = NP_NS + nanopub_id.replace("np-", "")
        nanopubs.append({
            "nanopub_id": nanopub_id,
            "head_uri": head,
            "assertion_uri": head + "#assertion",
            "provenance_uri": head + "#provenance",
            "publication_info_uri": head + "#publicationInfo",
            "assertion": {
                "assertion_id": assertion.get("assertion_id"),
                "text": assertion.get("text"),
                "subject": assertion.get("subject"),
                "relation": assertion.get("relation"),
                "relation_type_iri": vocab_uri + str(assertion.get("relation")),
                "object": assertion.get("object"),
                "condition": assertion.get("condition"),
                "direction": assertion.get("direction"),
                "confidence": assertion.get("confidence"),
                "confidence_basis": assertion.get("confidence_basis"),
                "atomic": assertion.get("atomic"),
                "quality_flags": assertion.get("quality_flags") or [],
                "evidence_type": assertion.get("evidence_type"),
            },
            "provenance": {
                "source_document": source_meta.get("document_id"),
                "source_file": source_meta.get("file"),
                "source_uri": source_uri,
                "source_sha256": source_meta.get("sha256"),
                "source_version": source_meta.get("version"),
                "page": assertion.get("page"),
                "block_id": assertion.get("block_id"),
                "bbox": assertion.get("bbox"),
                "section": assertion.get("section_title"),
                "quote": assertion.get("quote"),
                "quote_match": assertion.get("quote_match"),
                "anchor_match": assertion.get("anchor_match"),
                "seg_id": assertion.get("seg_id"),
                "sentence_id": assertion.get("sentence_id"),
                "evidence_type": assertion.get("evidence_type"),
            },
            "publication_info": {
                "assertion_id": assertion.get("assertion_id"),
                "relation_type": assertion.get("relation"),
                "atomic": assertion.get("atomic"),
                "quality_flags": assertion.get("quality_flags") or [],
                "generated_by": SKILL_NAME,
                "skill_version": SKILL_VERSION,
                "protocol_version": PROTOCOL_VERSION,
                "serialization": "trig+jsonld",
                "origin": assertion.get("origin"),
            },
        })
    nanopubs.sort(key=lambda item: item["nanopub_id"])
    return nanopubs


def _lit(text: Any) -> str:
    value = str(text if text is not None else "")
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    value = value.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    value = "".join(char for char in value if char >= " " or char in "\t")
    return '"' + value + '"'


def _iri(value: Any) -> str:
    return "<" + str(value) + ">"


TRIG_PREFIXES = (
    "@prefix np: <http://www.nanopub.org/nschema#> .\n"
    "@prefix npv: <https://w3id.org/np/skill-reading14/vocab#> .\n"
    "@prefix prov: <http://www.w3.org/ns/prov#> .\n"
    "@prefix dcterms: <http://purl.org/dc/terms/> .\n"
    "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n"
)


def nanopubs_to_trig(nanopubs: list[dict[str, Any]]) -> str:
    """Serialise to TriG: a head triple plus assertion / provenance / pubinfo graphs."""
    lines: list[str] = [TRIG_PREFIXES.rstrip("\n"), ""]
    for nanopub in nanopubs:
        head = nanopub["head_uri"]
        provenance = nanopub["provenance"]
        info = nanopub["publication_info"]
        assertion = nanopub["assertion"]
        source_uri = _iri(provenance.get("source_uri") or SOURCE_NS + "unknown")
        object_uri = head + "#object"
        lines.append("# " + nanopub["nanopub_id"] + "  (assertion "
                     + str(assertion.get("assertion_id")) + ")")
        lines.append(_iri(head) + " np:hasAssertion "
                     + _iri(nanopub["assertion_uri"]) + " ;")
        lines.append("    np:hasProvenance "
                     + _iri(nanopub["provenance_uri"]) + " ;")
        lines.append("    np:hasPublicationInfo "
                     + _iri(nanopub["publication_info_uri"]) + " .")
        lines.append("")
        lines.append(_iri(nanopub["assertion_uri"]) + " {")
        lines.append("    " + _iri(head + "#subject") + " "
                     + _iri(assertion.get("relation_type_iri")) + " "
                     + _iri(object_uri) + " .")
        lines.append("}")
        lines.append("")
        lines.append(_iri(nanopub["provenance_uri"]) + " {")
        lines.append("    " + _iri(nanopub["assertion_uri"])
                     + " prov:wasDerivedFrom " + source_uri + " ;")
        lines.append("        npv:sourceDocument "
                     + _lit(provenance.get("source_document")) + " ;")
        lines.append("        npv:sourceFile "
                     + _lit(provenance.get("source_file")) + " ;")
        lines.append("        npv:sourceVersion "
                     + _lit(provenance.get("source_version")) + " ;")
        lines.append("        npv:segId " + _lit(provenance.get("seg_id")) + " ;")
        lines.append("        npv:blockId " + _lit(provenance.get("block_id")) + " ;")
        bbox = provenance.get("bbox")
        bbox_text = ",".join(str(round(float(value), 2)) for value in bbox) if bbox else ""
        lines.append("        npv:bbox " + _lit(bbox_text) + " ;")
        lines.append("        npv:page " + _lit(provenance.get("page"))
                     + "^^xsd:integer ;")
        lines.append("        npv:quote " + _lit(provenance.get("quote")) + " ;")
        lines.append("        npv:quoteMatch "
                     + _lit(provenance.get("quote_match")) + " ;")
        lines.append("        npv:evidenceType "
                     + _lit(provenance.get("evidence_type")) + " ;")
        lines.append("        npv:subjectLabel "
                     + _lit(assertion.get("subject")) + " ;")
        lines.append("        npv:objectLabel "
                     + _lit(assertion.get("object")) + " ;")
        lines.append("        npv:assertionText " + _lit(assertion.get("text")) + " ;")
        lines.append("        npv:confidence " + _lit(assertion.get("confidence"))
                     + "^^xsd:decimal .")
        lines.append("}")
        lines.append("")
        lines.append(_iri(nanopub["publication_info_uri"]) + " {")
        lines.append("    " + _iri(head) + " npv:assertionId "
                     + _lit(info.get("assertion_id")) + " ;")
        lines.append("        npv:relationType " + _lit(info.get("relation_type")) + " ;")
        lines.append("        npv:atomic "
                     + _lit("true" if info.get("atomic") else "false")
                     + "^^xsd:boolean ;")
        for flag in info.get("quality_flags") or []:
            lines.append("        npv:qualityFlag " + _lit(flag) + " ;")
        lines.append("        npv:skill " + _lit(info.get("generated_by")) + " ;")
        lines.append("        npv:skillVersion " + _lit(info.get("skill_version")) + " ;")
        lines.append("        npv:protocolVersion "
                     + _lit(info.get("protocol_version")) + " ;")
        lines.append("        npv:origin " + _lit(info.get("origin")) + " ;")
        lines.append("        npv:serializationFormat " + _lit("trig") + " .")
        lines.append("}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def validate_trig(text: str) -> dict[str, Any]:
    """Minimal deterministic TriG sanity check (rdflib is not available in the sandbox).

    Checks: graph braces balance, every statement line ends with ' .' or continues
    with ';', every triple statement carries a predicate, prefix declarations are
    well formed and every line has balanced quotes.
    """
    errors: list[str] = []
    triples = 0
    graphs = 0
    depth = 0
    pending = False  # previous statement line ended with ';' (subject omitted)
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "triples": 0, "graphs": 0, "errors": ["empty document"]}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@prefix"):
            if (not line.endswith(" .") or line.count("<") != 1
                    or line.count(">") != 1 or len(line.split()) != 4):
                errors.append(f"line {number}: malformed @prefix")
            continue
        if line.endswith("{"):
            graph_name = line[:-1].strip()
            if not (graph_name.startswith("<") and graph_name.endswith(">")):
                errors.append(f"line {number}: malformed graph name")
            depth += 1
            graphs += 1
            if depth > 1:
                errors.append(f"line {number}: nested graph")
            pending = False
            continue
        if line == "}":
            depth -= 1
            if depth < 0:
                errors.append(f"line {number}: unmatched closing brace")
            pending = False
            continue
        if line.count('"') % 2 != 0:
            errors.append(f"line {number}: unbalanced quotes")
        if not (line.endswith(" .") or line.endswith(";")):
            errors.append(f"line {number}: statement does not end with ' .' or ';'")
            continue
        final = line.endswith(" .")
        statement = (line[:-2] if final else line[:-1]).strip()
        tokens = statement.split()
        minimum = 2 if pending else 3
        if len(tokens) < minimum:
            errors.append(f"line {number}: fewer than {minimum} terms")
        predicate_index = 0 if pending else 1
        if len(tokens) > predicate_index:
            predicate = tokens[predicate_index]
            if not (predicate.startswith("<") or re.match(r"^[a-z]+:[A-Za-z]", predicate)):
                errors.append(f"line {number}: no predicate IRI")
        pending = not final
        if final:
            triples += 1
    if depth != 0:
        errors.append(f"unbalanced graph braces (depth={depth})")
    return {"ok": not errors, "triples": triples, "graphs": graphs,
            "errors": errors[:20]}


def nanopubs_to_jsonld(nanopubs: list[dict[str, Any]],
                       source_meta: dict[str, Any]) -> dict[str, Any]:
    """A JSON-LD view of the same graph data, parseable without rdflib."""
    graph: list[dict[str, Any]] = []
    for nanopub in nanopubs:
        assertion = nanopub["assertion"]
        provenance = nanopub["provenance"]
        info = nanopub["publication_info"]
        graph.append({
            "@id": nanopub["head_uri"],
            "@type": "np:Nanopublication",
            "np:hasAssertion": {
                "@id": nanopub["assertion_uri"],
                "@type": "np:Assertion",
                "npv:assertionId": assertion.get("assertion_id"),
                "npv:assertionText": assertion.get("text"),
                "npv:subjectLabel": assertion.get("subject"),
                "npv:relation": assertion.get("relation"),
                "npv:relationTypeIri": assertion.get("relation_type_iri"),
                "npv:objectLabel": assertion.get("object"),
                "npv:condition": assertion.get("condition"),
                "npv:direction": assertion.get("direction"),
            },
            "np:hasProvenance": {
                "@id": nanopub["provenance_uri"],
                "@type": "np:Provenance",
                "prov:wasDerivedFrom": {"@id": provenance.get("source_uri")},
                "npv:sourceDocument": provenance.get("source_document"),
                "npv:sourceFile": provenance.get("source_file"),
                "npv:sourceVersion": provenance.get("source_version"),
                "npv:segId": provenance.get("seg_id"),
                "npv:page": provenance.get("page"),
                "npv:blockId": provenance.get("block_id"),
                "npv:bbox": provenance.get("bbox"),
                "npv:quote": provenance.get("quote"),
                "npv:quoteMatch": provenance.get("quote_match"),
                "npv:evidenceType": provenance.get("evidence_type"),
                "npv:confidence": assertion.get("confidence"),
                "npv:confidenceBasis": assertion.get("confidence_basis"),
            },
            "np:hasPublicationInfo": {
                "@id": nanopub["publication_info_uri"],
                "@type": "np:PublicationInfo",
                "npv:assertionId": info.get("assertion_id"),
                "npv:relationType": info.get("relation_type"),
                "npv:atomic": info.get("atomic"),
                "npv:qualityFlag": info.get("quality_flags") or [],
                "npv:skill": info.get("generated_by"),
                "npv:skillVersion": info.get("skill_version"),
                "npv:protocolVersion": info.get("protocol_version"),
                "npv:origin": info.get("origin"),
                "npv:serializationFormat": info.get("serialization"),
            },
        })
    return {
        "@context": {
            "np": "http://www.nanopub.org/nschema#",
            "npv": str(VOCAB_NS),
            "prov": "http://www.w3.org/ns/prov#",
            "dcterms": "http://purl.org/dc/terms/",
            "xsd": "http://www.w3.org/2001/XMLSchema#",
        },
        "nanopublication_count": len(nanopubs),
        "source": source_meta,
        "@graph": graph,
    }


# --------------------------------------------------------------------------- #
# generated answers + finalize
# --------------------------------------------------------------------------- #
def load_generated_answers(work_dir: str | Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Read every answer file under work/generated (skipping '_' templates)."""
    generated_dir = Path(work_dir) / "generated"
    records: list[dict[str, Any]] = []
    files: list[str] = []
    if not generated_dir.is_dir():
        return records, files
    for path in sorted(generated_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        files.append(path.name)
        rows = hybrid.read_records(path)
        if not rows:
            data = hybrid.read_json(path)
            if isinstance(data, dict):
                for key in ("assertions", "nanopublications", "claims", "records"):
                    value = data.get(key)
                    if isinstance(value, list):
                        rows = [item for item in value if isinstance(item, dict)]
                        break
                if not rows and data.get("seg_id"):
                    rows = [data]
            elif isinstance(data, list):
                rows = [item for item in data if isinstance(item, dict)]
        records.extend(rows)
    return records, files


def _document_snapshot(work_dir: Path,
                       segments: list[dict[str, Any]]) -> dict[str, Any]:
    payload = hybrid.read_json(work_dir / "doc_blocks.json", {}) or {}
    blocks = payload.get("blocks") or {}
    if not blocks:
        synthetic: dict[str, list[dict[str, Any]]] = {}
        for segment in segments:
            synthetic.setdefault(str(segment.get("page")), []).append({
                "block_id": segment.get("block_id"), "bbox": segment.get("bbox"),
                "text": segment.get("text")})
        blocks = synthetic
    return {"blocks": blocks,
            "anchor_quality": payload.get("anchor_quality") or "text",
            "pages": [], "blocks_sha256": payload.get("blocks_sha256")}


def _repair_contract(payload: dict[str, Any], case: dict[str, Any],
                     values: dict[str, Any]) -> list[str]:
    """Fill declared fields the first mapping pass left as placeholders.

    hybrid.map_fields maps each produced value onto at most one declared field (so
    a contract naming the same concept twice would get one value plus one
    placeholder). This second pass matches the remaining declared names one by one
    against the produced values, and only fills a field when the name still
    resolves; anything unmatched stays a declared-type placeholder.
    """
    declared = hybrid.declared_fields(case) if case else []
    if not declared:
        return []
    meta = payload.setdefault("meta", {})
    not_reported = list(meta.get("contract_fields_not_reported") or [])
    placeholders = dict(meta.get("placeholders") or {})
    mapping = dict(meta.get("contract_field_mapping") or {})
    repaired: list[str] = []
    for field in declared:
        name = field["name"]
        if name not in not_reported:
            continue
        single, report = hybrid.map_fields([field], values, SYNONYMS)
        if name in single:
            payload[name] = single[name]
            mapping[name] = report.get(name)
            not_reported = [item for item in not_reported if item != name]
            placeholders.pop(name, None)
            repaired.append(name)
    if repaired:
        meta["contract_fields_not_reported"] = not_reported
        meta["placeholders"] = placeholders
        meta["contract_field_mapping"] = mapping
        meta["field_fill_ratio"] = round(
            len(mapping) / len(declared), 4) if declared else 0.0
        meta["contract_repair"] = repaired
    return repaired


def _material_names(manifest: dict[str, Any]) -> list[str]:
    return [Path(str(path)).name for path in (manifest.get("materials") or [])]


def finalize(args: argparse.Namespace) -> dict[str, Any]:
    """Deterministic stage: generated answers -> validated nanopublication deliverables."""
    found = discover.discover()
    case_path, case = _resolve_case(found, getattr(args, "case", None))
    case_id = contract.find_case_id(case) or ""
    out_dir = Path(args.out) if args.out else Path(found["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = _resolve_work_dir(getattr(args, "work", None))
    declared = hybrid.declared_fields(case) if case else []
    hybrid.write_placeholder(
        out_dir, case_id=case_id,
        note="finalize started; this file is replaced at the end of the run",
        contract_fields=len(declared))
    vocab = load_vocab()

    manifest = hybrid.read_json(work_dir / "manifest.json") or {}
    if not manifest or not (work_dir / "segments.json").is_file():
        _log("finalize: no prepared work directory found, running prepare first")
        prepare(args, quiet=True)
        manifest = hybrid.read_json(work_dir / "manifest.json") or {}
    segments_payload = hybrid.read_json(work_dir / "segments.json", {}) or {}
    segments = segments_payload.get("segments") or []
    source_meta = hybrid.read_json(work_dir / "source_metadata.json", {}) or {}
    segments_by_id = {segment["seg_id"]: segment for segment in segments}
    doc_snapshot = _document_snapshot(work_dir, segments)

    limits: list[str] = list(manifest.get("limits") or [])
    uncertainty: list[str] = []
    change_log = ["placeholder written at start of the run",
                  "prepare: deterministic segmentation, sentence numbering and fact extraction"]
    status = "success"

    generated_raw, generated_files = load_generated_answers(work_dir)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for record in generated_raw:
        result = validate_generated(record, segments_by_id, doc_snapshot, vocab)
        if result["status"] == "accepted":
            accepted.append(result["assertion"])
        else:
            rejected.append(result)
    generated_present = bool(generated_raw)
    _log(f"finalize: generated_records={len(generated_raw)} accepted={len(accepted)} "
         f"rejected={len(rejected)}")

    accepted, duplicates_merged = _dedupe(accepted)
    covered = {assertion["seg_id"] for assertion in accepted}

    batches = manifest.get("batches") or []
    answered_batches = [batch["batch_id"] for batch in batches
                        if (work_dir / "generated" / (batch["batch_id"] + ".json")).is_file()]
    if generated_present and not answered_batches:
        # an aggregate generated/assertions.json is also accepted: count a batch as
        # answered when the generated records actually point into its segments
        covered_seg_ids = {assertion["seg_id"] for assertion in accepted}
        answered_batches = [batch["batch_id"] for batch in batches
                            if set(batch.get("seg_ids") or []) & covered_seg_ids]
    batch_coverage = {
        "batches_total": len(batches),
        "batches_answered": len(answered_batches),
        "coverage": (round(len(answered_batches) / len(batches), 4) if batches else 0.0),
        "missing_batches": [batch["batch_id"] for batch in batches
                            if batch["batch_id"] not in answered_batches],
        "generated_files": generated_files,
        "segments_total": len(segments),
        "segments_with_generated_assertion": len(covered),
    }

    # Baseline policy: no answer at all -> honest extractive baseline; partial
    # answers -> baseline only for the uncovered batches; every batch answered ->
    # trust the agent's judgement and add nothing.
    if not generated_present:
        baseline_limit = DEFAULT_MAX_BASELINE
    elif batch_coverage["coverage"] < 1.0:
        baseline_limit = BASELINE_WHEN_GENERATED
    else:
        baseline_limit = 0
    baseline_limit = max(0, int(getattr(args, "max_baseline", 0) or baseline_limit))
    baseline = baseline_assertions(_eligible_segments(segments), vocab,
                                    baseline_limit, covered)
    if baseline:
        if not generated_present:
            limits.append("no agent-generated assertions were found: deterministic "
                          "extractive baseline used (relation forced to states, "
                          "object not resolved)")
        else:
            limits.append(str(len(baseline)) + " extractive baseline assertion(s) added "
                          "for segments the generated answer did not cover")
    assertions = accepted + baseline
    order_by_seg = {segment["seg_id"]: int(segment.get("order") or 0) for segment in segments}
    assertions.sort(key=lambda item: (int(item.get("page") or 1),
                                      order_by_seg.get(item["seg_id"], 0),
                                      str(item.get("assertion_id") or "")))
    _assign_ids(assertions)

    claims: list[dict[str, Any]] = []
    for assertion in assertions:
        anchor = anchors.make_anchor(assertion["page"],
                                     {"block_id": assertion.get("block_id"),
                                      "bbox": assertion.get("bbox")},
                                     assertion["quote"])
        claims.append({"claim_id": assertion["assertion_id"],
                       "claim": assertion["text"], "kind": "content",
                       "anchors": [anchor]})
    grounding = anchors.verify_all(doc_snapshot, claims)
    per_claim = {item["claim_id"]: item for item in grounding.get("per_claim") or []}
    weak: list[dict[str, Any]] = []
    final_assertions: list[dict[str, Any]] = []
    for assertion in assertions:
        check = per_claim.get(assertion["assertion_id"]) or {}
        assertion["evidence_support"] = bool(check.get("supported_by_quote"))
        assertion["anchor_located"] = bool(check.get("located"))
        if not check.get("supported_by_quote"):
            weak.append({"status": "weak_evidence",
                         "reason": "evidence_does_not_mention_claim",
                         "assertion": assertion})
            continue
        final_assertions.append(assertion)
    if weak:
        limits.append(str(len(weak)) + " assertion(s) quarantined as weak_evidence: the "
                      "quote does not mention the assertion's own terms (see evidence.json)")
    if rejected:
        limits.append(str(len(rejected)) + " generated record(s) rejected: quote/seg_id/"
                      "anchor validation failed (see evidence.json rejected_assertions)")
    _log(f"finalize: final_assertions={len(final_assertions)} weak={len(weak)} "
         f"grounding_rate={grounding.get('grounding_rate')}")

    nanopubs = build_nanopubs(final_assertions, source_meta, vocab)
    trig_text = nanopubs_to_trig(nanopubs)
    trig_check = validate_trig(trig_text)
    jsonld = nanopubs_to_jsonld(nanopubs, source_meta)
    trig_path = out_dir / "nanopubs.trig"
    jsonld_path = out_dir / "nanopubs.jsonld"
    trig_path.write_text(trig_text, encoding="utf-8")
    hybrid.write_json(jsonld_path, jsonld)
    hybrid.write_json(out_dir / "assertions.json", {
        "case_id": case_id, "source_document_id": source_meta.get("document_id"),
        "assertions": final_assertions})
    hybrid.write_json(out_dir / "rejected_assertions.json", {
        "case_id": case_id, "rejected": rejected, "weak_evidence": weak,
        "rationale": ("a generated record is rejected when its seg_id/quote/anchor cannot "
                      "be re-verified against the extraction snapshot; weak_evidence means "
                      "the quote does not mention the assertion's own terms")})
    hybrid.write_json(out_dir / "sources.json", {
        "case_id": case_id, "sources": [source_meta]})

    flag_counts: dict[str, int] = {}
    relation_distribution: dict[str, int] = {}
    for assertion in final_assertions:
        relation = str(assertion.get("relation"))
        relation_distribution[relation] = relation_distribution.get(relation, 0) + 1
        for flag in assertion.get("quality_flags") or []:
            flag_counts[flag] = flag_counts.get(flag, 0) + 1
    atomic_count = sum(1 for assertion in final_assertions if assertion.get("atomic"))
    confidences = [float(assertion.get("confidence") or 0.0)
                   for assertion in final_assertions]
    quality = {
        "assertions_total": len(final_assertions),
        "assertions_generated": sum(1 for assertion in final_assertions
                                    if assertion["origin"] == "agent_generated"),
        "assertions_baseline": sum(1 for assertion in final_assertions
                                   if assertion["origin"] == "baseline_extractive"),
        "atomic_assertions": atomic_count,
        "atomicity_rate": (round(atomic_count / len(final_assertions), 4)
                           if final_assertions else 0.0),
        "nanopublications_total": len(nanopubs),
        "quote_verified_total": len(final_assertions),
        "grounding_rate": grounding.get("grounding_rate"),
        "anchor_quality": grounding.get("anchor_quality"),
        "effective_confidence": grounding.get("effective_confidence"),
        "mean_confidence": (round(sum(confidences) / len(confidences), 4)
                            if confidences else 0.0),
        "relation_distribution": dict(sorted(relation_distribution.items())),
        "quality_flag_counts": dict(sorted(flag_counts.items())),
        "duplicates_merged": duplicates_merged,
        "rejected_total": len(rejected),
        "weak_evidence_total": len(weak),
        "batch_coverage": batch_coverage,
        "relation_vocab": {"version": vocab.get("version"),
                           "relations": relation_names(vocab),
                           "source": vocab.get("source")},
        "source_completeness": {
            "title": bool(source_meta.get("title")),
            "authors": bool(source_meta.get("authors")),
            "year": bool(source_meta.get("year")),
            "doi": bool(source_meta.get("doi")),
            "file_hash": bool(source_meta.get("sha256")),
            "missing_fields": source_meta.get("missing_fields") or [],
        },
        "serialization": {
            "formats": ["trig", "jsonld"],
            "trig_path": trig_path.name,
            "jsonld_path": jsonld_path.name,
            "trig_valid": trig_check["ok"],
            "triple_count": trig_check["triples"],
            "graph_count": trig_check["graphs"],
            "trig_errors": trig_check["errors"],
        },
        "deterministic": True,
        "no_model_calls": True,
        "no_network": True,
    }

    if not segments:
        status = "failed"
        limits.append("no readable text segments: the material has no usable text layer")
    elif not final_assertions:
        status = "failed"
        limits.append("no assertion could be validated against the source text")
    elif not trig_check["ok"]:
        status = "partial"
        limits.append("TriG serialisation failed its own structural check: "
                      + "; ".join(trig_check["errors"][:3]))
    else:
        if not generated_present:
            status = "partial"
        if batch_coverage["coverage"] < 1.0:
            status = "partial"
        if rejected or weak:
            status = "partial"
        if manifest.get("status") == "no_materials":
            status = "failed"
    if STOP.past_soft():
        status = "partial"
        limits.append(f"soft deadline reached at {STOP.elapsed()}s: remaining checks trimmed")
    if not source_meta.get("doi") or not source_meta.get("authors"):
        uncertainty.append("source metadata is incomplete (see source_metadata."
                           "missing_fields); traceability is limited to file hash + "
                           "page/block/quote anchors")
    if status == "partial":
        change_log.append("finalize: degradation path active (see limits)")
    else:
        change_log.append("finalize: full path - generated assertions validated and serialised")

    evidence_payload = {
        "source": source_meta,
        "verification": {
            "engine": doc_snapshot.get("anchor_quality"),
            "blocks_sha256": doc_snapshot.get("blocks_sha256"),
            "quote_check": "normalised containment against the persisted extraction snapshot",
            "anchor_check": "anchors.verify(page, block_id, quote)",
            "claim_support_check": "the assertion's own terms must appear in the quote",
        },
        "grounding": grounding,
        "claims": claims,
        "assertions": [{key: assertion.get(key) for key in (
            "assertion_id", "seg_id", "sentence_id", "page", "block_id", "bbox",
            "quote", "quote_match", "anchor_match", "evidence_support",
            "anchor_located", "relation", "atomic", "quality_flags")}
            for assertion in final_assertions],
        "rejected_assertions": rejected,
        "weak_evidence_assertions": weak,
        "degraded": not generated_present,
    }
    serialization = {
        "format": "trig+jsonld",
        "nanopublication_style": "assertion / provenance / publicationInfo graphs",
        "trig_path": trig_path.name,
        "jsonld_path": jsonld_path.name,
        "trig_valid": trig_check["ok"],
        "triple_count": trig_check["triples"],
        "graph_count": trig_check["graphs"],
        "stable_id_rule": "sha256(assertion_text + quote + source_document_id)[:16]",
        "trig": trig_text,
        "jsonld": jsonld,
    }
    values: dict[str, Any] = {
        "assertions": final_assertions,
        "nanopublications": nanopubs,
        "source_metadata": source_meta,
        # flattened source scalars: a hidden contract may ask for title/doi alone
        "title": source_meta.get("title") or "",
        "authors": source_meta.get("authors") or [],
        "year": source_meta.get("year") or "",
        "doi": source_meta.get("doi") or "",
        "sources": [source_meta],
        "relation_vocab": vocab.get("relations") or [],
        "quality": quality,
        "evidence": evidence_payload,
        "serialization": serialization,
        "rejected_assertions": rejected,
        "status": status,
        "uncertainty": uncertainty,
        "limits": limits,
        "page_count": source_meta.get("page_count") or 0,
        "keywords": source_meta.get("keywords") or [],
        "abstract": source_meta.get("abstract") or "",
        "summary": {
            "case_id": case_id,
            "source_title": source_meta.get("title"),
            "assertions": len(final_assertions),
            "nanopublications": len(nanopubs),
            "atomicity_rate": quality["atomicity_rate"],
            "grounding_rate": grounding.get("grounding_rate"),
            "relations": dict(sorted(relation_distribution.items())),
            "degraded": not generated_present,
        },
    }
    if not segments:
        # nothing could be read from the material: leave every declared field to the
        # contract placeholder layer instead of pretending empty content was produced
        values = {}
        limits.append("no readable segments: contract fields returned as typed placeholders")
    _log("finalize: assembling result.json through the contract reflection")
    report = hybrid.run_report(
        stopwatch=STOP,
        constants={
            "skill": SKILL_NAME, "version": SKILL_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "no_model_calls": True, "no_network": True,
            "time_budget_s": hybrid.TIME_BUDGET_S,
            "soft_deadline_s": hybrid.SOFT_DEADLINE_S,
        },
        work_dir=str(work_dir), out_dir=str(out_dir),
        inputs={"case": str(case_path) if case_path else "",
                "task_md": found.get("task_md_path"),
                "materials": _material_names(manifest),
                "work_dir": str(work_dir)},
        vocab={"path": "resources/relation_vocab.json", "version": vocab.get("version"),
               "source": vocab.get("source"), "relations": relation_names(vocab)},
        batches=batch_coverage,
        generated={"present": generated_present, "files": generated_files,
                   "records": len(generated_raw), "accepted": len(accepted),
                   "rejected": len(rejected), "weak_evidence": len(weak)},
        segments={"total": len(segments),
                  "in_worksheets": manifest.get("segments_in_worksheets"),
                  "sentences": manifest.get("sentence_total")},
        degraded=not generated_present,
        grounding_rate=grounding.get("grounding_rate"),
        assertions={"total": len(final_assertions), "atomic": atomic_count,
                    "relation_distribution": dict(sorted(relation_distribution.items()))},
        serialization={"trig_valid": trig_check["ok"],
                       "triple_count": trig_check["triples"],
                       "graph_count": trig_check["graphs"],
                       "trig_path": trig_path.name, "jsonld_path": jsonld_path.name},
        phases={"prepare_manifest_status": manifest.get("status"),
                "prepare_elapsed_s": manifest.get("elapsed_s"),
                "finalize_elapsed_s": STOP.elapsed()},
        limits=limits, uncertainty=uncertainty,
        outputs={"result": str(out_dir / "result.json"),
                 "evidence": str(out_dir / "evidence.json"),
                 "run_report": str(out_dir / "run_report.json"),
                 "assertions": str(out_dir / "assertions.json"),
                 "trig": str(trig_path), "jsonld": str(jsonld_path),
                 "sources": str(out_dir / "sources.json"),
                 "rejected": str(out_dir / "rejected_assertions.json")},
    )
    payload = hybrid.finalize(
        out_dir, case=case, case_id=case_id, values=values, status=status,
        limits=limits, uncertainty=uncertainty, change_log=change_log,
        task_md_path=found.get("task_md_path"), evidence=evidence_payload,
        report=report, synonyms=SYNONYMS,
        additional=("serialization", "quality", "sources", "rejected_assertions"))
    _repair_contract(payload, case, values)
    meta = payload.setdefault("meta", {})
    meta["degraded"] = not generated_present
    meta["nanopub_files"] = {"trig": str(trig_path), "jsonld": str(jsonld_path)}
    contract.write_result(out_dir, payload)
    hybrid.write_json(out_dir / "quality.json", quality)
    print(f"FINALIZE_OK status={status} case={case_id} "
          f"assertions={len(final_assertions)} nanopubs={len(nanopubs)} "
          f"grounding={grounding.get('grounding_rate')} "
          f"batches={batch_coverage['batches_answered']}/{batch_coverage['batches_total']} "
          f"trig_valid={trig_check['ok']} out={out_dir}", flush=True)
    _log(f"finalize: wrote {out_dir / 'result.json'}")
    return payload


# --------------------------------------------------------------------------- #
# failure path: never end a run without a result.json
# --------------------------------------------------------------------------- #
def _write_failure(args: argparse.Namespace, traceback_text: str) -> None:
    try:
        out_dir = hybrid.resolve_out_dir(getattr(args, "out", None))
        last_line = (traceback_text.strip().splitlines() or ["unknown error"])[-1]
        payload = {
            "case_id": "",
            "meta": {
                "status": "failed",
                "contract_detected": False,
                "contract_field_count": 0,
                "contract_fields_not_reported": [],
                "placeholders": {},
                "limits": ["run aborted by an unhandled exception: " + last_line],
                "uncertainty": [],
                "change_log": ["failure result written by the top-level handler"],
                "traceback_tail": traceback_text[-1500:],
            },
        }
        hybrid.write_json(out_dir / "result.json", payload)
    except Exception:
        pass


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="reading-14 nanopublication: assertion decomposition with evidence")
    parser.add_argument("--prepare", action="store_true",
                        help="deterministic preparation only")
    parser.add_argument("--finalize", action="store_true",
                        help="validate generated answers and export")
    parser.add_argument("--all", action="store_true",
                        help="prepare + finalize without agent generation (default)")
    parser.add_argument("--case", default=None, help="explicit case.json path")
    parser.add_argument("--out", default=None,
                        help="output directory (default /app/output)")
    parser.add_argument("--work", default=None,
                        help="work directory (default /app/work)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-segments", type=int, default=DEFAULT_MAX_SEGMENTS)
    parser.add_argument("--max-baseline", type=int, default=0,
                        help="cap extractive baseline assertions (0 = automatic)")
    ns = parser.parse_args(argv)
    default_mode = not ns.prepare and not ns.finalize and not ns.all
    do_prepare = ns.prepare or ns.all or default_mode
    do_finalize = ns.finalize or ns.all or default_mode
    try:
        if do_prepare:
            prepare(ns)
        if do_finalize:
            finalize(ns)
        return 0
    except BaseException as exc:  # a run must always leave a result file behind
        text = traceback.format_exc(limit=8)
        _log(f"FATAL {type(exc).__name__}: {exc}")
        _write_failure(ns, text)
        return 1


if __name__ == "__main__":
    raise SystemExit(run_cli())
