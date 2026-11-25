#!/usr/bin/env python3
"""Deterministic knowledge-graph core for the reading-15 skill.

This module is the part of the pipeline that must NOT consume model requests:
finding the material files, turning them into addressable paragraph/segment units,
extracting machine-checkable candidate entities and relation triggers, building a
draft graph, and verifying every claim back against the source.

Design rules (from the contest contract):
* locators are what the package really has.  For the JSON/JATS material pack the
  locator is the publisher paragraph id, NOT a PDF page, so no PDF page number is
  invented.  For a real PDF the page number is real and is reported as such.
* no network, no third party package, deterministic ordering everywhere.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
try:  # packaged layout: <skill-root>/lib
    sys.path.insert(0, str(ROOT))
    from lib import anchors, extract, hybrid, structure
except ImportError:  # dev layout: skills/<topic>/scripts + skills/_core/lib
    sys.path.insert(0, str(ROOT.parent / "_core"))
    from lib import anchors, extract, hybrid, structure

RESOURCES = ROOT / "resources"

# --------------------------------------------------------------------------- #
# controlled vocabulary (mirrors resources/kg_schema.json)
# --------------------------------------------------------------------------- #
DEFAULT_SCHEMA: dict[str, Any] = {
    "version": "1.0.0",
    "node_types": {
        "researcher": "a person or author named in the material",
        "material": "a physical material, sample, compound or reagent",
        "method": "a method, technique, instrument or procedure",
        "task": "a research task, benchmark, dataset or problem setting",
        "metric": "a measured quantity, metric or performance indicator",
        "conclusion": "a finding/claim stated by the paper",
        "dataset": "a named dataset or corpus",
        "concept": "a concept that does not fit the other types",
        "other": "a real entity whose type is not in the schema",
    },
    "relation_types": {
        "uses": {"direction": "directed", "definition": "source uses/applies target"},
        "applies_to": {"direction": "directed", "definition": "source applies to target"},
        "evaluates_on": {"direction": "directed", "definition": "source is evaluated on target"},
        "outperforms": {"direction": "directed", "definition": "source performs better than target"},
        "improves": {"direction": "directed", "definition": "source improves/raises/reduces target"},
        "characterizes": {"direction": "directed", "definition": "source method characterizes target"},
        "supports": {"direction": "directed", "definition": "evidence supports a claim"},
        "contradicts": {"direction": "directed", "definition": "source contradicts target"},
        "causes": {"direction": "directed", "definition": "source causes/leads to target"},
        "proposes": {"direction": "directed", "definition": "source proposes/presents target"},
        "is_a": {"direction": "directed", "definition": "source is a kind of target"},
        "compares_with": {"direction": "undirected", "definition": "source and target are compared"},
        "part_of": {"direction": "directed", "definition": "source is part of target"},
        "co_occurs": {"direction": "undirected",
                      "definition": "deterministic fallback: same paragraph, relation NOT asserted"},
        "other": {"direction": "unspecified", "definition": "real relation whose type is not in the schema"},
    },
    "relation_aliases": {
        "use": "uses", "used": "uses", "using": "uses", "employs": "uses",
        "applies": "uses", "based_on": "uses", "utilizes": "uses",
        "apply": "applies_to", "applied_to": "applies_to",
        "evaluated_on": "evaluates_on", "tested_on": "evaluates_on",
        "benchmarked_on": "evaluates_on", "trained_on": "evaluates_on",
        "better_than": "outperforms", "superior_to": "outperforms",
        "improved_over": "outperforms", "exceeds": "outperforms",
        "compare": "compares_with", "compared_with": "compares_with",
        "compared_to": "compares_with", "vs": "compares_with",
        "characterizes": "characterizes", "characterises": "characterizes",
        "measured_by": "characterizes", "analyzed_by": "characterizes",
        "support": "supports", "supported_by": "supports", "confirms": "supports",
        "contradict": "contradicts", "conflicts_with": "contradicts",
        "cause": "causes", "leads_to": "causes", "results_in": "causes",
        "propose": "proposes", "introduces": "proposes", "presents": "proposes",
        "improve": "improves", "enhances": "improves", "increases": "improves",
        "reduces": "improves", "is_a": "is_a", "is_a_kind_of": "is_a",
        "part_of": "part_of", "co_occurrence": "co_occurs", "cooccurs": "co_occurs",
    },
    "merge_rules": {
        "same_surface": "identical normalised surface (case/hyphen/space insensitive)",
        "acronym_definition": "acronym and full form defined in the material itself",
        "agent_alias": "alias declared by the agent in nodes.json",
        "never_merge": "different declared types are a homonym: kept separate, logged",
    },
    "strength_levels": {"strong": 0.9, "moderate": 0.6, "weak": 0.35},
}

TYPE_ORDER = ("researcher", "material", "method", "task", "dataset", "metric",
              "conclusion", "concept", "other")
ANCHOR_QUALITY_CEILING = {"span": 1.0, "paragraph": 0.95, "page": 0.7,
                          "block": 0.7, "text": 0.4}

METHOD_LEX = {
    "xrd", "xps", "ftir", "nmr", "tem", "sem", "afm", "dft", "md", "pcr",
    "hplc", "gc", "ms", "icp", "uv-vis", "raman", "bet", "tga", "dsc", "eels",
    "stem", "hrtem", "edx", "eds", "xrf", "stm", "cd", "ellipsometry",
    "spectroscopy", "diffraction", "microscopy", "chromatography",
    "electrochemistry", "calorimetry", "titration", "thermogravimetry",
    "simulation", "modelling", "modeling", "machine learning", "deep learning",
    "regression", "classification", "clustering", "fine-tuning", "pretraining",
    "impregnation", "calcination", "reduction", "oxidation", "precipitation",
    "synthesis", "polymerization", "annealing", "sintering", "etching",
    "deposition", "electrospinning", "lyophilization", "centrifugation",
    "assay", "elisa", "western blot", "flow cytometry", "sequencing",
    "ablation", "benchmarking", "cross-validation", "random forest", "xgboost",
    "transformer", "attention", "diffusion model", "gnn", "bert", "llm",
}
METRIC_LEX = {
    "conversion", "selectivity", "yield", "turnover frequency", "tof",
    "activity", "stability", "efficiency", "overpotential", "current density",
    "capacity", "conductivity", "band gap", "surface area", "particle size",
    "loading", "accuracy", "precision", "recall", "f1", "f1-score", "auc",
    "rmse", "mae", "mse", "r2", "r-squared", "correlation", "p-value",
    "sensitivity", "specificity", "latency", "throughput", "cost", "bleu",
    "rouge", "perplexity", "success rate", "error rate", "detection rate",
    "adsorption capacity", "onset potential", "half-wave potential",
    "exchange current density", "mass activity", "specific activity",
}
MATERIAL_LEX = {
    "catalyst", "oxide", "zeolite", "polymer", "alloy", "graphene",
    "nanoparticle", "nanoparticles", "nanotube", "nanotubes", "membrane",
    "electrode", "crystal", "sample", "support", "solvent", "ligand",
    "protein", "enzyme", "antibody", "cell", "cells", "tissue", "perovskite",
    "mof", "cof", "composite", "hydrogel", "aerogel", "solution", "reagent",
    "substrate", "adsorbent", "photocatalyst", "electrocatalyst", "biomass",
    "lignin", "cellulose", "chitosan", "collagen", "dna", "rna", "lipid",
    "serum", "plasma", "buffer", "medium", "catalyst support", "metal",
}
TASK_LEX = {
    "task", "benchmark", "dataset", "classification", "regression",
    "segmentation", "detection", "prediction", "generation", "translation",
    "question answering", "retrieval", "reasoning", "summarization",
    "recognition", "diagnosis", "screening", "optimization", "control",
}
CLAIM_MARKERS = (
    "we find", "we found", "we show", "we showed", "we report", "we propose",
    "our results", "results show", "results suggest", "results indicate",
    "this suggests", "these results", "demonstrate", "demonstrates", "reveal",
    "reveals", "conclude", "concluded", "found that", "shown that", "suggest",
    "suggests", "indicate", "indicates", "confirm", "confirms", "imply",
    "implies", "highlight", "highlights", "provide evidence",
)
CONDITION_KEYS = ("at", "under", "in the presence of", "with", "when", "after",
                  "before", "for", "within", "over", "using")

ELEMENT_SYMBOLS = {
    "H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne", "Na", "Mg", "Al",
    "Si", "P", "S", "Cl", "Ar", "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn", "Fe",
    "Co", "Ni", "Cu", "Zn", "Ga", "Ge", "As", "Se", "Br", "Kr", "Rb", "Sr",
    "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd", "In", "Sn",
    "Sb", "Te", "I", "Xe", "Cs", "Ba", "La", "Ce", "Pr", "Nd", "Pm", "Sm",
    "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm", "Yb", "Lu", "Hf", "Ta", "W",
    "Re", "Os", "Ir", "Pt", "Au", "Hg", "Tl", "Pb", "Bi", "Th", "U",
}
CHEM_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Z][a-z]?\d{0,3}){2,}(?:[-/](?:[A-Z][a-z]?\d{0,3})+)?(?![a-z])")
FORMULA_TAIL_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Z][a-z]?\d{0,2}[-/])+[A-Z][a-z]?\d{0,3}(?![a-z])")
CAP_PHRASE_RE = re.compile(r"[A-Z][A-Za-z0-9\-]*(?:\s+[A-Z][A-Za-z0-9\-]*){1,3}")
ET_AL_RE = re.compile(r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\s+et\s+al\.")
ACRONYM_DEF_RES = (
    re.compile(r"([A-Z][A-Za-z0-9 /\-]{3,60}?)\s*\(([A-Z][A-Za-z0-9\-]{1,10})\)"),
    re.compile(r"\b([A-Z][A-Za-z0-9\-]{1,10})\s*\(([A-Za-z][A-Za-z0-9 /\-]{3,60})\)"),
)
CONDITION_VALUE_RE = re.compile(
    r"(?:-?\d+(?:\.\d+)?\s*(?:%|°C|K|h|min|s|mL|mg|g|mol|bar|MPa|V|mA|"
    r"mg/mL|mol/L|M|nm|eV|Hz|rpm))", re.I)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def norm_label(text: str) -> str:
    """Case/whitespace/punctuation-insensitive key used for label comparison."""
    t = unicodedata.normalize("NFKC", text or "").casefold()
    t = t.replace("-", " ").replace("_", " ").replace("/", " ")
    return re.sub(r"\s+", " ", t).strip()


def norm_key(text: str) -> str:
    """Aggressive key for entity-alignment decisions (all separators removed)."""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", norm_label(text))


def clip(text: str, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    pos = max(cut.rfind(". "), cut.rfind("; "), cut.rfind(" "))
    if pos > limit * 0.5:
        return cut[:pos + 1].strip()
    return cut.rstrip() + "..."


def load_schema() -> dict[str, Any]:
    """Load resources/kg_schema.json, falling back to the embedded copy."""
    path = RESOURCES / "kg_schema.json"
    data = hybrid.read_json(path)
    if isinstance(data, dict) and data.get("relation_types"):
        schema = dict(DEFAULT_SCHEMA)
        schema.update(data)
        return schema
    return DEFAULT_SCHEMA


def relation_spec(schema: dict[str, Any], relation: str) -> tuple[str, str, bool]:
    """(canonical_relation, direction, known) for an agent-declared relation."""
    raw = norm_label(relation).replace(" ", "_")
    types = schema.get("relation_types") or DEFAULT_SCHEMA["relation_types"]
    aliases = schema.get("relation_aliases") or DEFAULT_SCHEMA["relation_aliases"]
    candidate = aliases.get(raw, raw)
    if candidate in types:
        spec = types[candidate]
        direction = spec.get("direction") if isinstance(spec, dict) else "directed"
        return candidate, (direction or "directed"), True
    return (raw or "other"), "unspecified", False


def anchor_ceiling(quality: str) -> float:
    return ANCHOR_QUALITY_CEILING.get(str(quality or ""), 0.4)


def _author_list(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        parts = re.split(r"[;]|,\s*(?:and\s+)?", value)
        out = [p.strip() for p in parts if p.strip()]
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                for key in ("name", "full_name", "fullName", "given_name", "surname"):
                    if isinstance(item.get(key), str) and item[key].strip():
                        out.append(item[key].strip())
                        break
                else:
                    given = item.get("given") or item.get("first")
                    family = item.get("family") or item.get("last")
                    if isinstance(given, str) or isinstance(family, str):
                        out.append(" ".join(x for x in (given, family) if isinstance(x, str)))
    seen: set[str] = set()
    uniq: list[str] = []
    for name in out:
        key = norm_label(name)
        if key and key not in seen and len(name) <= 80:
            seen.add(key)
            uniq.append(name)
    return uniq[:12]


def _first_str(node: dict[str, Any], keys: Iterable[str], max_len: int = 300) -> str | None:
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip() and len(value.strip()) <= max_len:
            return value.strip()
        if isinstance(value, (int, float)) and key in {"year", "publication_year"}:
            return str(value)
    return None


# --------------------------------------------------------------------------- #
# paragraph-level material parsing (JSON / JATS-style packs)
# --------------------------------------------------------------------------- #
PARA_LIST_KEYS = ("paragraphs", "paras", "paragraph_list", "body_paragraphs")
SECTION_LIST_KEYS = ("sections", "chapters", "subsections", "section_list")
TEXT_KEYS = ("text", "content", "body", "paragraph", "para", "sentence", "value")
ID_KEYS = ("paragraph_id", "para_id", "paragraphId", "pid", "p_id", "id",
           "@id", "xml:id", "jid", "paragraph-id")
DOC_MARKER_KEYS = ("doc_id", "document_id", "paper_id", "source_id", "docId",
                   "paperId", "source")
TITLE_KEYS = ("title", "paper_title", "name", "heading")
CONTAINER_DOC_KEYS = ("papers", "documents", "docs", "material", "materials",
                      "items", "sources", "corpus", "corpora", "records")


def _para_text(node: dict[str, Any]) -> str:
    for key in TEXT_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return re.sub(r"\s+", " ", value).strip()
    return ""


def _para_id(node: dict[str, Any]) -> str | None:
    for key in ID_KEYS:
        value = node.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return None


def _para_section(node: dict[str, Any]) -> str | None:
    for key in ("section", "section_title", "sectionTitle", "chapter", "heading"):
        value = node.get(key)
        if isinstance(value, str) and value.strip() and len(value.strip()) <= 200:
            return value.strip()
    return None


def _para_doc_marker(node: dict[str, Any]) -> str | None:
    for key in DOC_MARKER_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _is_paragraph(node: Any) -> bool:
    return isinstance(node, dict) and bool(_para_text(node))


def _has_text_structure(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    for key in PARA_LIST_KEYS + SECTION_LIST_KEYS + ("text", "content", "full_text", "body", "abstract"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, list) and any(
                (_is_paragraph(item) or (isinstance(item, str) and len(item.strip()) > 20))
                for item in value):
            return True
    return False


def _iter_paragraphs(node: Any, section: str | None = None,
                     depth: int = 0, out: list[tuple[str | None, dict[str, Any]]] | None = None
                     ) -> list[tuple[str | None, dict[str, Any]]]:
    """Collect paragraph dicts in document order, carrying the section heading."""
    if out is None:
        out = []
    if depth > 7 or len(out) > 1200:
        return out
    if isinstance(node, list):
        for item in node:
            if _is_paragraph(item):
                out.append((_para_section(item) or section, item))
            elif isinstance(item, str) and len(item.strip()) > 20:
                out.append((section, {"text": item}))
            elif isinstance(item, (dict, list)):
                _iter_paragraphs(item, section, depth + 1, out)
        return out
    if not isinstance(node, dict):
        return out
    local_section = section
    heading = _first_str(node, ("title", "heading", "section_title", "name"), 200)
    if heading:
        local_section = heading
    consumed = False
    for key in PARA_LIST_KEYS:
        value = node.get(key)
        if isinstance(value, list):
            for item in value:
                if _is_paragraph(item):
                    out.append((_para_section(item) or local_section, item))
                elif isinstance(item, str) and len(item.strip()) > 20:
                    out.append((local_section, {"text": item}))
                elif isinstance(item, (dict, list)):
                    _iter_paragraphs(item, local_section, depth + 1, out)
            consumed = True
    for key in SECTION_LIST_KEYS:
        value = node.get(key)
        if isinstance(value, (list, dict)):
            _iter_paragraphs(value, local_section, depth + 1, out)
            consumed = True
    if not consumed and _is_paragraph(node) and not any(
            isinstance(node.get(k), (list, dict)) for k in PARA_LIST_KEYS + SECTION_LIST_KEYS):
        out.append((_para_section(node) or local_section, node))
        return out
    if not consumed:
        for key, value in node.items():
            if key in ("authors", "author", "author_list", "references", "bibliography",
                       "metadata", "meta", "paragraphs", "sections"):
                continue
            if isinstance(value, (dict, list)):
                _iter_paragraphs(value, local_section, depth + 1, out)
    return out


def _split_paper_docs(data: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Find the paper-level dicts inside one JSON material file."""
    if depth > 4:
        return []
    if isinstance(data, list):
        papers = [item for item in data if isinstance(item, dict) and _has_text_structure(item)]
        if papers and len(papers) == len([i for i in data if isinstance(i, dict)]):
            return papers
        return []
    if not isinstance(data, dict):
        return []
    for key in CONTAINER_DOC_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            papers = [item for item in value if isinstance(item, dict) and _has_text_structure(item)]
            if papers:
                return papers
    if _has_text_structure(data):
        return [data]
    for key, value in data.items():
        if isinstance(value, (dict, list)) and key not in (
                "metadata", "meta", "authors", "author", "references", "bibliography"):
            found = _split_paper_docs(value, depth + 1)
            if found:
                return found
    return []


def _own_metadata(node: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "title": _first_str(node, TITLE_KEYS),
        "doi": _first_str(node, ("doi", "DOI", "doi_link", "DOI_link"), 120),
        "authors": _author_list(node.get("authors") or node.get("author")
                                or node.get("author_list")),
        "year": _first_str(node, ("year", "publication_year", "pub_year"), 8),
        "license": _first_str(node, ("license", "license_spdx", "rights"), 120),
        "metadata_source": "own_file",
    }
    return {k: v for k, v in meta.items() if v not in (None, "", [])} | {
        "metadata_source": "own_file"}


def _assemble_json_doc(raw: dict[str, Any], source_file: str,
                       paragraphs: list[tuple[str | None, dict[str, Any]]],
                       doc_marker: str | None = None) -> dict[str, Any]:
    blocks: dict[str, list[dict[str, Any]]] = {}
    used_ids: set[str] = set()
    source_ids = 0
    for index, (section, para) in enumerate(paragraphs, start=1):
        text = _para_text(para)
        if not text:
            continue
        pid = _para_id(para)
        if pid:
            source_ids += 1
        else:
            pid = f"p{index:04d}"
        block_id = pid
        suffix = 2
        while block_id in used_ids:
            block_id = f"{pid}-{suffix}"
            suffix += 1
        used_ids.add(block_id)
        blocks[str(len(blocks) + 1)] = [{
            "block_id": block_id,
            "paragraph_id": pid,
            "section": section,
            "bbox": None,
            "text": text,
            "locator_source": "source" if _para_id(para) else "generated",
        }]
    meta = _own_metadata(raw)
    locator_kind = ("jats_paragraph_id" if source_ids and source_ids == len(blocks)
                    else "generated_paragraph_id")
    return {
        "source_file": source_file,
        "format": "json",
        "doc_marker": doc_marker,
        "blocks": blocks,
        "pages": [],
        "page_count": 0,
        "paragraph_count": len(blocks),
        "char_count": sum(len(b[0]["text"]) for b in blocks.values()),
        "anchor_quality": "paragraph",
        "locator_kind": locator_kind,
        "engine": "json-material",
        "warnings": [],
        **meta,
    }




# --------------------------------------------------------------------------- #
# material discovery -> document list
# --------------------------------------------------------------------------- #
def parse_json_material(path: str | Path) -> list[dict[str, Any]]:
    """Turn one JSON/JATS pack file into 0..n paper documents.

    Handles three shapes seen in such packs:
    * one file per paper (primary-material.json);
    * one aggregate file with a papers/documents list;
    * one aggregate file whose paragraphs carry a per-paper marker.
    """
    source_file = Path(path).name
    data = hybrid.read_json(path)
    if data is None:
        return []
    raw_docs = _split_paper_docs(data)
    if not raw_docs:
        return []
    docs: list[dict[str, Any]] = []
    for raw in raw_docs:
        paragraphs = _iter_paragraphs(raw)
        if not paragraphs:
            continue
        markers = {_para_doc_marker(para) for _section, para in paragraphs}
        markers.discard(None)
        if len(markers) > 1:
            groups: dict[str | None, list[tuple[str | None, dict[str, Any]]]] = {}
            for section, para in paragraphs:
                groups.setdefault(_para_doc_marker(para), []).append((section, para))
            for marker in sorted(groups, key=lambda m: (str(m), len(groups.get(m) or []))):
                doc = _assemble_json_doc(raw, source_file, groups[marker], marker)
                doc["doc_marker"] = marker
                docs.append(doc)
        else:
            doc = _assemble_json_doc(raw, source_file, paragraphs)
            doc["doc_marker"] = next(iter(markers), None)
            docs.append(doc)
    return docs


def _walk_metadata_records(node: Any, depth: int = 0,
                           out: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Find paper metadata records (doi/title/authors) anywhere in a manifest."""
    if out is None:
        out = []
    if depth > 5 or len(out) > 100:
        return out
    if isinstance(node, list):
        for item in node:
            _walk_metadata_records(item, depth + 1, out)
        return out
    if not isinstance(node, dict):
        return out
    has_id = any(isinstance(node.get(k), str) and node.get(k, "").strip()
                 for k in ("doi", "DOI"))
    has_title = isinstance(node.get("title"), str) and node.get("title", "").strip()
    if has_id or (has_title and (node.get("authors") or node.get("author"))):
        out.append(node)
    for key, value in node.items():
        if isinstance(value, (dict, list)) and key not in ("authors", "author", "author_list"):
            _walk_metadata_records(value, depth + 1, out)
    return out


def load_manifest_records(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in paths:
        path = Path(raw)
        data = hybrid.read_json(path)
        if data is None:
            continue
        for node in _walk_metadata_records(data):
            meta = _own_metadata(node)
            source_file = _first_str(node, ("source_file", "file", "path"), 200)
            if source_file:
                meta["source_file"] = Path(source_file).name
            if meta.get("doi") or meta.get("title"):
                records.append(meta)
    return records


def _match_manifest(doc: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any] | None:
    doc_doi = norm_label(doc.get("doi") or "").replace(" ", "")
    doc_title = norm_label(doc.get("title") or "")
    source = norm_label(doc.get("source_file") or "")
    for record in records:
        record_doi = norm_label(record.get("doi") or "").replace(" ", "")
        if doc_doi and record_doi and doc_doi == record_doi:
            return record
    for record in records:
        record_title = norm_label(record.get("title") or "")
        if doc_title and record_title and (doc_title == record_title
                                           or record_title in doc_title
                                           or doc_title in record_title):
            return record
    for record in records:
        record_source = norm_label(record.get("source_file") or "")
        if source and record_source and (source == record_source
                                         or source in record_source):
            return record
    return None


def _text_doc_from_extraction(raw_doc: dict[str, Any], source_file: str) -> dict[str, Any]:
    suffix = Path(source_file).suffix.lower()
    locator_kind = "pdf_page" if suffix == ".pdf" else "block"
    blocks: dict[str, list[dict[str, Any]]] = {}
    for page_key in sorted((raw_doc.get("blocks") or {}), key=lambda k: int(k)):
        page_blocks = []
        for block in raw_doc["blocks"][page_key]:
            text = re.sub(r"\s+", " ", block.get("text") or "").strip()
            if not text:
                continue
            page_blocks.append({
                "block_id": block.get("block_id"),
                "paragraph_id": block.get("block_id"),
                "section": None,
                "bbox": block.get("bbox"),
                "text": text,
                "locator_source": "extracted",
            })
        if page_blocks:
            blocks[str(page_key)] = page_blocks
    return {
        "source_file": source_file,
        "format": "pdf" if suffix == ".pdf" else "text",
        "blocks": blocks,
        "pages": raw_doc.get("pages") or [],
        "page_count": len(raw_doc.get("pages") or []),
        "paragraph_count": sum(len(v) for v in blocks.values()),
        "char_count": raw_doc.get("total_chars") or 0,
        "anchor_quality": raw_doc.get("anchor_quality") or "text",
        "locator_kind": locator_kind,
        "engine": raw_doc.get("engine"),
        "warnings": list(raw_doc.get("warnings") or []),
        "attempts": raw_doc.get("attempts"),
        "metadata_source": "not_reported",
    }


def build_documents(entries: list[dict[str, Any]],
                    manifest_paths: Iterable[str | Path] = (),
                    *, deadline: float | None = None, max_docs: int = 6,
                    limits: list[str] | None = None,
                    uncertainty: list[str] | None = None) -> list[dict[str, Any]]:
    """entries: ordered [{"path": str, "role": "primary"|"secondary"|"generic"}].

    Deterministic: the caller has already sorted the entries.  A document never
    inherits title/authors from the case root - the package's root DOI belongs to
    the first paper only, so borrowing it for the second paper would be a lie.
    """
    limits = limits if limits is not None else []
    uncertainty = uncertainty if uncertainty is not None else []
    records = load_manifest_records(manifest_paths)
    docs: list[dict[str, Any]] = []
    for entry in entries[:max_docs]:
        path = Path(entry["path"])
        role = entry.get("role") or "generic"
        parsed: list[dict[str, Any]] = []
        if path.suffix.lower() == ".json":
            parsed = parse_json_material(path)
            if not parsed and not entry.get("allow_raw_json"):
                uncertainty.append(f"{path.name}: JSON file has no paragraph layer; skipped "
                                   f"(a JSON index/manifest is not paper text)")
                continue
            if not parsed:
                uncertainty.append(f"{path.name}: JSON material had no paragraph layer; "
                                   f"fell back to raw-text extraction")
        if not parsed:
            import time as _time
            if deadline is not None and _time.time() > deadline - 5:
                limits.append(f"extraction deadline reached before {path.name}")
                continue
            raw_doc = extract.extract_document(path, deadline=deadline)
            parsed = [_text_doc_from_extraction(raw_doc, path.name)]
            if not (raw_doc.get("text_layer") or raw_doc.get("total_chars")):
                uncertainty.append(f"{path.name}: no text layer (scan?) - nothing usable extracted")
        for doc in parsed:
            doc["role"] = role
            doc["source_file"] = doc.get("source_file") or path.name
            if not doc.get("blocks"):
                continue
            match = _match_manifest(doc, records)
            if match:
                for key in ("title", "doi", "authors", "year", "license"):
                    if not doc.get(key) and match.get(key):
                        doc[key] = match[key]
                doc["metadata_source"] = "corpus_manifest"
            if doc.get("metadata_source") == "nonempty":
                doc["metadata_source"] = "own_file"
            docs.append(doc)
    for index, doc in enumerate(docs, start=1):
        doc["doc_id"] = f"doc-{index:02d}"
        doc["order"] = index
    if len(entries) > max_docs:
        limits.append(f"only the first {max_docs} material files were processed")
    return docs


def summarize_documents(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for doc in docs:
        out.append({
            "doc_id": doc.get("doc_id"),
            "role": doc.get("role"),
            "source_file": doc.get("source_file"),
            "title": doc.get("title"),
            "doi": doc.get("doi"),
            "authors": doc.get("authors"),
            "year": doc.get("year"),
            "license": doc.get("license"),
            "metadata_source": doc.get("metadata_source"),
            "format": doc.get("format"),
            "locator_kind": doc.get("locator_kind"),
            "anchor_quality": doc.get("anchor_quality"),
            "paragraph_count": doc.get("paragraph_count"),
            "char_count": doc.get("char_count"),
            "engine": doc.get("engine"),
            "warnings": doc.get("warnings") or [],
        })
    return out


# --------------------------------------------------------------------------- #
# segments: the addressable units the agent and the verifier both use
# --------------------------------------------------------------------------- #
def segments_for_docs(docs: list[dict[str, Any]], *, max_segments_per_doc: int = 140,
                      max_total: int = 420) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for doc in docs:
        if len(segments) >= max_total:
            break
        if doc.get("format") == "json":
            raw_segments = hybrid.build_segments(doc, None)
            for seg in raw_segments:
                page_blocks = doc["blocks"].get(str(seg["page"])) or []
                block = page_blocks[0] if page_blocks else {}
                seg["paragraph_id"] = block.get("paragraph_id")
                seg["section_title"] = block.get("section")
        else:
            try:
                struct = structure.outline(doc)
            except Exception:
                struct = {"sections": []}
            raw_segments = hybrid.build_segments(doc, struct)
            for seg in raw_segments:
                seg["paragraph_id"] = (seg.get("block_ids") or [None])[0]
        for index, seg in enumerate(raw_segments[:max_segments_per_doc], start=1):
            seg["seg_id"] = f"{doc['doc_id']}-s{index:04d}"
            seg["doc_id"] = doc["doc_id"]
            seg["order"] = len(segments) + 1
            seg["locator_kind"] = doc.get("locator_kind")
            segments.append(seg)
    return segments


def documents_by_id(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {doc["doc_id"]: doc for doc in docs}


# --------------------------------------------------------------------------- #
# entity candidates (deterministic inventory, the agent makes the judgement)
# --------------------------------------------------------------------------- #
TERM_STOP = set(hybrid.TERM_STOP) | {
    "figure", "fig", "table", "tab", "eq", "equation", "section", "abstract",
    "introduction", "conclusion", "results", "discussion", "methods", "acknowledgements",
    "references", "supplementary", "author", "authors", "corresponding", "email",
    "journal", "volume", "issue", "pages", "doi", "copyright", "license", "received",
    "accepted", "published", "keywords", "highlights", "graphical",
    "for", "from", "was", "were", "are", "is", "by", "of", "to", "in", "on", "at",
    "as", "its", "it", "we", "our", "their", "has", "have", "had", "be", "been",
    "not", "no", "can", "may", "will", "which", "than", "then", "these", "those",
    "also", "such", "more", "most", "less", "very", "between", "within", "during",
    "after", "before", "under", "over", "about", "into", "via", "respectively",
    "shows", "show", "shown", "found", "observe", "observed", "reported", "report",
    "studied", "study", "using", "used", "based", "however", "therefore", "thus",
}
# Bare generic words are real English words but not useful graph nodes: a node
# called "catalyst" does not identify a research object.
GENERIC_BLOCK = {
    "catalyst", "catalysts", "sample", "samples", "support", "metal", "cell",
    "cells", "tissue", "solution", "reagent", "substrate", "medium", "buffer",
    "serum", "plasma", "method", "methods", "task", "dataset", "benchmark",
    "model", "models", "data", "analysis", "study", "paper", "result", "results",
    "material", "materials", "product", "products", "reaction", "reactions",
    "condition", "conditions", "temperature", "pressure", "time", "rate",
}
PROCESS_LEX = {
    "methanation", "hydrogenation", "dehydrogenation", "oxidation", "reduction",
    "carbonation", "nitration", "esterification", "hydrolysis", "polymerization",
    "combustion", "reforming", "electrolysis", "photocatalysis", "electrocatalysis",
    "adsorption", "desorption", "precipitation", "co-precipitation",
}
RELATION_TRIGGERS: list[tuple[str, re.Pattern[str]]] = [
    ("outperforms", re.compile(r"\b(?:outperform\w*|better than|higher than|greater than|"
                               r"superior to|improved over|exceed\w*|surpass\w*)\b", re.I)),
    ("contradicts", re.compile(r"\b(?:contradict\w*|inconsistent with|disagree\w*|"
                               r"conflict\w* with|unlike|contrary to)\b", re.I)),
    ("supports", re.compile(r"\b(?:support\w*|confirm\w*|consistent with|in line with|"
                            r"corroborat\w*|agree\w* with)\b", re.I)),
    ("compares_with", re.compile(r"\b(?:compared (?:with|to)|comparison (?:with|to)|"
                                 r"versus|vs\.?)\b", re.I)),
    ("evaluates_on", re.compile(r"\b(?:evaluat\w* (?:on|with|using)|tested on|benchmark\w* on|"
                                r"measured on|trained on|applied to)\b", re.I)),
    ("characterizes", re.compile(r"\b(?:characteri[sz]\w*|analy[sz]\w* (?:by|with)|"
                                 r"measured by|determined by|obtained by|detected by|"
                                 r"monitored by|identified by)\b", re.I)),
    ("improves", re.compile(r"\b(?:improv\w*|enhanc\w*|increas\w*|reduc\w*|promot\w*|boost\w*)\b", re.I)),
    ("causes", re.compile(r"\b(?:leads? to|caus\w*|results? in|resulted in|due to|"
                          r"attribut\w* to)\b", re.I)),
    ("proposes", re.compile(r"\b(?:propos\w*|introduc\w*|develop\w*|present\w*|report\w*)\b", re.I)),
    ("uses", re.compile(r"\b(?:us(?:e|es|ed|ing)|employ\w*|appl(?:y|ies|ied|ying)|"
                        r"utiliz\w*|rel(?:y|ies|ied) on|via|based on|by means of)\b", re.I)),
    ("is_a", re.compile(r"\b(?:is an?|are an?|refers? to|defined as|known as|serves? as)\b", re.I)),
]
METHOD_SUFFIX_RE = re.compile(r"(?:tion|sion|sis|ing|graphy|scopy|lysis|metry|ization|ation)$", re.I)
CHEM_FULL_RE = re.compile(r"(?:[A-Z][a-z]?\d{0,3})(?:[-/](?:[A-Z][a-z]?\d{0,3})+)+")


def _doc_text(doc: dict[str, Any]) -> str:
    return "\n".join(block["text"] for page in doc.get("blocks", {}).values()
                     for block in page if block.get("text"))


def _contains_label(text: str, label: str) -> bool:
    if not label or not text:
        return False
    if re.search(r"[\u4e00-\u9fff]", label):
        return label.casefold() in text.casefold()
    pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(label) + r"(?![A-Za-z0-9])", re.I)
    if pattern.search(text):
        return True
    key = norm_key(label)
    return bool(key) and key in norm_key(text)


def _best_quote(text: str, labels: Iterable[str] = (), limit: int = 240) -> str:
    """Shortest readable window that covers as many of the labels as possible."""
    text = re.sub(r"\s+", " ", text or "").strip()
    labels = [label for label in labels if label]
    if len(text) <= limit:
        return text
    lowered = text.casefold()
    spans: list[tuple[int, int]] = []
    for label in labels:
        pos = lowered.find(label.casefold())
        if pos < 0:
            key = norm_key(label)
            if key:
                pos = norm_key(text).find(key)
        if pos >= 0:
            spans.append((pos, min(len(text), pos + len(label))))
    if len(spans) >= 2:
        start = max(0, min(s for s, _e in spans) - 60)
        end = min(len(text), max(e for _s, e in spans) + 60)
        if end - start > limit:
            start = max(0, max(e for _s, e in spans) - limit)
            end = min(len(text), start + limit)
        return text[start:end].strip()
    return anchors.pick_quote(text, labels, width=limit)


def guess_type(label: str, contexts: Iterable[str] = ()) -> str:
    low = norm_label(label)
    if not low:
        return "concept"
    if re.search(r"\bet al\b", low) or re.search(r"\bet al\.", label):
        return "researcher"
    tokens = low.split()
    if low in METRIC_LEX or any(token in METRIC_LEX for token in tokens):
        return "metric"
    if low in METHOD_LEX or any(token in METHOD_LEX for token in tokens):
        return "method"
    if low in MATERIAL_LEX or any(token in MATERIAL_LEX for token in tokens):
        return "material"
    if low in TASK_LEX or any(token in TASK_LEX for token in tokens):
        return "task"
    if any(token in PROCESS_LEX for token in tokens):
        return "task"
    if CHEM_FULL_RE.fullmatch(label.strip()) or CHEM_RE.fullmatch(label.strip()):
        return "material"
    if METHOD_SUFFIX_RE.search(low) and len(low) > 6:
        return "method"
    joined = " ".join(contexts)
    for match in re.finditer(re.escape(label), joined, flags=re.I):
        window = joined[match.end():match.end() + 80]
        if CONDITION_VALUE_RE.match(window.lstrip()) or re.match(
                r"\s*(?:of|was|were|reached|up to|=|:|at)\s*-?\d", window, flags=re.I):
            return "metric"
    return "concept"


def _count_occurrences(text: str, label: str) -> int:
    if re.search(r"[\u4e00-\u9fff]", label):
        return text.casefold().count(label.casefold())
    pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(label) + r"(?![A-Za-z0-9])", re.I)
    return len(pattern.findall(text))


def seg_anchor(doc: dict[str, Any], seg: dict[str, Any], quote: str,
               note: str = "") -> dict[str, Any]:
    """Internal anchor: real page for PDFs, paragraph ordinal kept for verification."""
    block = {"block_id": (seg.get("block_ids") or [None])[0], "bbox": seg.get("bbox")}
    anchor = anchors.make_anchor(int(seg.get("page") or 1), block, quote, note)
    anchor["doc_id"] = doc["doc_id"]
    anchor["paragraph_id"] = seg.get("paragraph_id")
    anchor["locator_kind"] = doc.get("locator_kind") or "block"
    anchor["section"] = seg.get("section_title")
    anchor["seg_id"] = seg.get("seg_id")
    return anchor


def public_anchor(anchor: dict[str, Any]) -> dict[str, Any]:
    """Emitted anchor. A paragraph/block locator never masquerades as a PDF page."""
    kind = anchor.get("locator_kind") or "block"
    page = int(anchor["page"]) if kind == "pdf_page" and anchor.get("page") is not None else None
    return {
        "doc_id": anchor.get("doc_id"),
        "paragraph_id": anchor.get("paragraph_id"),
        "page": page,
        "block_id": anchor.get("block_id"),
        "bbox": anchor.get("bbox"),
        "quote": anchor.get("quote"),
        "locator_kind": kind,
        "section": anchor.get("section"),
    }


def _candidate_occurrences(label: str, docs: list[dict[str, Any]],
                           segments: list[dict[str, Any]], max_n: int = 3) -> list[dict[str, Any]]:
    docs_by = documents_by_id(docs)
    found: list[dict[str, Any]] = []
    for seg in segments:
        doc = docs_by.get(seg.get("doc_id") or "")
        if not doc:
            continue
        text = seg.get("text") or ""
        if not _contains_label(text, label):
            continue
        quote = _best_quote(text, [label], limit=200)
        found.append(seg_anchor(doc, seg, quote))
        if len(found) >= max_n:
            break
    return found


def collect_candidates(docs: list[dict[str, Any]], segments: list[dict[str, Any]],
                       *, top_terms: int = 60, limit: int = 120) -> dict[str, Any]:
    """Inventory of candidate entities + acronym definitions + conclusions."""
    doc_texts = {doc["doc_id"]: _doc_text(doc) for doc in docs}
    global_text = "\n".join(doc_texts[doc["doc_id"]] for doc in docs)
    raw: dict[str, dict[str, Any]] = {}

    def add(label: str, signal: str, type_hint: str | None = None,
            count: int | None = None) -> None:
        label = re.sub(r"\s+", " ", (label or "")).strip(" .,;:()[]-\u2013")
        if len(label) < 2 or len(label) > 90:
            return
        key = norm_key(label)
        if not key or len(key) < 2 or key.isdigit():
            return
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9\-]*|[\u4e00-\u9fff]+", label)
        if not tokens:
            return
        first, last = tokens[0].casefold(), tokens[-1].casefold()
        if signal != "lexicon" and (first in TERM_STOP or last in TERM_STOP):
            return
        if len(tokens) == 1 and first in GENERIC_BLOCK and signal != "chemical_formula":
            return
        entry = raw.setdefault(key, {"label": label, "signals": [], "type_hint": None,
                                     "count": 0})
        if signal not in entry["signals"]:
            entry["signals"].append(signal)
        if type_hint and not entry["type_hint"]:
            entry["type_hint"] = type_hint
        if count:
            entry["count"] = max(entry["count"], int(count))

    for term in hybrid.term_candidates(global_text, top=top_terms, min_count=2):
        add(term["term"], "repeated_phrase", None, term["count"])
    for doc in docs:
        text = doc_texts[doc["doc_id"]]
        for term in hybrid.term_candidates(text, top=20, min_count=2):
            add(term["term"], "repeated_phrase_in_doc", None, term["count"])
        for acronym in hybrid.find_acronyms(text):
            if acronym.casefold() in {"fig", "table", "eq", "section", "tab"}:
                continue
            add(acronym, "acronym", guess_type(acronym), _count_occurrences(text, acronym))
        for match in list(CHEM_RE.finditer(text))[:40]:
            add(match.group(0), "chemical_formula", "material", 1)
        for match in list(FORMULA_TAIL_RE.finditer(text))[:40]:
            add(match.group(0), "chemical_formula", "material", 1)
        for match in list(CAP_PHRASE_RE.finditer(text))[:60]:
            phrase = match.group(0)
            if len(phrase) > 6 and not phrase.isupper():
                add(phrase, "capitalized_phrase", None, _count_occurrences(text, phrase))
    for lexicon, type_hint in ((METHOD_LEX, "method"), (METRIC_LEX, "metric"),
                               (MATERIAL_LEX, "material"), (TASK_LEX, "task")):
        for term in sorted(lexicon):
            count = _count_occurrences(global_text, term)
            if count:
                add(term, "lexicon", type_hint, count)
    for doc in docs:
        for author in doc.get("authors") or []:
            add(author, "author_metadata", "researcher", 1)
        for match in ET_AL_RE.finditer(doc_texts[doc["doc_id"]]):
            add(match.group(1) + " et al.", "author_mention", "researcher", 1)

    candidates: list[dict[str, Any]] = []
    for entry in raw.values():
        occurrences = _candidate_occurrences(entry["label"], docs, segments)
        if not occurrences:
            continue
        contexts = [anchor["quote"] for anchor in occurrences]
        type_guess = entry["type_hint"] or guess_type(entry["label"], contexts)
        candidates.append({
            "label": entry["label"],
            "type": type_guess,
            "type_source": "lexicon" if entry["type_hint"] else "heuristic",
            "count": entry["count"] or len(occurrences),
            "signals": entry["signals"],
            "doc_ids": sorted({a.get("doc_id") for a in occurrences if a.get("doc_id")}),
            "occurrences": occurrences,
        })

    conclusions = _conclusion_candidates(docs, segments)
    candidates.extend(conclusions)
    candidates.sort(key=lambda c: (TYPE_ORDER.index(c["type"]) if c["type"] in TYPE_ORDER else 9,
                                   -c["count"], norm_key(c["label"])))
    # entities first, conclusions last: a reviewer wants the graph readable
    candidates = [c for c in candidates if c["type"] != "conclusion"][:limit] + \
                 [c for c in candidates if c["type"] == "conclusion"][:16]
    for index, candidate in enumerate(candidates, start=1):
        candidate["candidate_id"] = f"c-{index:04d}"
    acronym_defs = collect_acronym_definitions(docs, segments)
    return {"candidates": candidates, "acronym_definitions": acronym_defs}


def _conclusion_candidates(docs: list[dict[str, Any]],
                           segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    docs_by = documents_by_id(docs)
    out: list[dict[str, Any]] = []
    for seg in segments:
        doc = docs_by.get(seg.get("doc_id") or "")
        if not doc:
            continue
        for sentence in hybrid.sentences(seg.get("text") or ""):
            low = sentence.casefold()
            if not any(marker in low for marker in CLAIM_MARKERS):
                continue
            if len(sentence) < 40:
                continue
            quote = clip(sentence, 240)
            out.append({
                "label": clip(sentence, 90),
                "type": "conclusion",
                "type_source": "claim_marker",
                "count": 1,
                "signals": ["claim_sentence"],
                "doc_ids": [doc["doc_id"]],
                "occurrences": [seg_anchor(doc, seg, quote)],
                "is_conclusion": True,
            })
            if len(out) >= 16:
                return out
    return out


def collect_acronym_definitions(docs: list[dict[str, Any]],
                                segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Acronym <-> full form definitions that the material states itself."""
    docs_by = documents_by_id(docs)
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for seg in segments:
        doc = docs_by.get(seg.get("doc_id") or "")
        if not doc:
            continue
        text = seg.get("text") or ""
        for pattern in ACRONYM_DEF_RES:
            for match in pattern.finditer(text):
                first, second = match.group(1).strip(), match.group(2).strip()
                for acronym, expansion in ((second, first), (first, second)):
                    if not (2 <= len(acronym) <= 10 and acronym.isupper()):
                        continue
                    words = re.findall(r"[A-Za-z][A-Za-z0-9\-]+", expansion)
                    if len(words) < 2:
                        continue
                    initials = "".join(word[0] for word in words).upper()
                    if not initials.startswith(acronym):
                        continue
                    key = (norm_key(acronym), norm_key(expansion))
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({
                        "acronym": acronym,
                        "expansion": expansion,
                        "doc_id": doc["doc_id"],
                        "quote": clip(match.group(0), 200),
                        "anchor": public_anchor(seg_anchor(doc, seg, clip(text, 200))),
                    })
    return out


def collect_relation_evidence(segments: list[dict[str, Any]],
                              candidates: list[dict[str, Any]],
                              *, max_items: int = 70) -> list[dict[str, Any]]:
    """Sentences that mention >=2 candidates, with the trigger verb found."""
    labels = [(c["label"], c["type"]) for c in candidates
              if not c.get("is_conclusion")]
    out: list[dict[str, Any]] = []
    for seg in segments:
        text = seg.get("text") or ""
        for sentence in hybrid.sentences(text):
            if len(sentence) < 25:
                continue
            lowered = sentence.casefold()
            mentions: list[tuple[int, str]] = []
            for label, _type in labels:
                pos = lowered.find(label.casefold())
                if pos < 0 and re.search(r"[\u4e00-\u9fff]", label):
                    pos = sentence.find(label)
                if pos >= 0:
                    mentions.append((pos, label))
            if len(mentions) < 2:
                continue
            mentions.sort()
            relations: list[dict[str, Any]] = []
            for index in range(len(mentions) - 1):
                (start_a, label_a), (start_b, label_b) = mentions[index], mentions[index + 1]
                middle = sentence[start_a + len(label_a):start_b]
                trigger = _match_trigger(middle) or _match_trigger(sentence)
                relations.append({
                    "source_label": label_a,
                    "target_label": label_b,
                    "relation": trigger[0] if trigger else None,
                    "trigger": trigger[1] if trigger else None,
                })
            out.append({
                "seg_id": seg.get("seg_id"),
                "doc_id": seg.get("doc_id"),
                "paragraph_id": seg.get("paragraph_id"),
                "sentence": clip(sentence, 240),
                "mentions": [label for _pos, label in mentions][:6],
                "relations": relations,
                "conditions": _conditions(sentence),
            })
            if len(out) >= max_items:
                return out
    return out


def _match_trigger(text: str) -> tuple[str, str] | None:
    for relation, pattern in RELATION_TRIGGERS:
        match = pattern.search(text or "")
        if match:
            return relation, match.group(0)
    return None


def _conditions(sentence: str) -> list[str]:
    out: list[str] = []
    for match in re.finditer(r"\b(?:at|under|in|with|when|after|before|for|within|over)\s+"
                             r"([^,.;]{2,70})", sentence or "", flags=re.I):
        snippet = clip(match.group(0), 90)
        if re.search(r"\d|presence|condition", snippet, flags=re.I):
            out.append(snippet)
    for match in CONDITION_VALUE_RE.finditer(sentence or ""):
        snippet = match.group(0)
        if snippet not in out:
            out.append(snippet)
    unique: list[str] = []
    for item in out:
        if item not in unique:
            unique.append(item)
    return unique[:3]


def build_baseline_graph(docs: list[dict[str, Any]], segments: list[dict[str, Any]],
                         candidates: list[dict[str, Any]], schema: dict[str, Any],
                         *, max_nodes: int = 90, max_edges: int = 200
                         ) -> dict[str, Any]:
    """Evidence-first draft graph assembled without any model judgement."""
    segments_by = {seg["seg_id"]: seg for seg in segments}
    docs_by = documents_by_id(docs)
    nodes: list[dict[str, Any]] = []
    label_to_node: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        if not candidate.get("occurrences"):
            continue
        nodes.append({
            "label": candidate["label"],
            "type": candidate["type"],
            "type_source": candidate.get("type_source") or "heuristic",
            "aliases": [],
            "doc_ids": candidate.get("doc_ids") or [],
            "occurrences": candidate["occurrences"][:3],
            "origin": "baseline",
            "candidate_id": candidate.get("candidate_id"),
            "is_conclusion": bool(candidate.get("is_conclusion")),
        })
        label_to_node[norm_key(candidate["label"])] = nodes[-1]
        if len(nodes) >= max_nodes:
            break
    edges: list[dict[str, Any]] = []
    co_occurrence: dict[tuple[str, str], dict[str, Any]] = {}
    seen_edges: set[tuple[str, str, str, str]] = set()
    for item in collect_relation_evidence(segments, candidates):
        seg = segments_by.get(item.get("seg_id") or "")
        doc = docs_by.get(item.get("doc_id") or "")
        if not seg or not doc:
            continue
        for relation_item in item.get("relations") or []:
            source = label_to_node.get(norm_key(relation_item["source_label"]))
            target = label_to_node.get(norm_key(relation_item["target_label"]))
            if not source or not target or source is target:
                continue
            pair = (norm_key(relation_item["source_label"]), norm_key(relation_item["target_label"]))
            if pair not in co_occurrence:
                co_occurrence[pair] = {
                    "source_label": relation_item["source_label"],
                    "target_label": relation_item["target_label"],
                    "seg_ids": [seg["seg_id"]],
                    "count": 1,
                    "quote": item["sentence"],
                    "doc_ids": [doc["doc_id"]],
                }
            else:
                record = co_occurrence[pair]
                record["count"] += 1
                if seg["seg_id"] not in record["seg_ids"]:
                    record["seg_ids"].append(seg["seg_id"])
                if doc["doc_id"] not in record["doc_ids"]:
                    record["doc_ids"].append(doc["doc_id"])
            relation = relation_item.get("relation")
            if not relation:
                continue
            canonical, direction, _known = relation_spec(schema, relation)
            key = (doc["doc_id"], norm_key(source["label"]), canonical, norm_key(target["label"]))
            if key in seen_edges:
                continue
            seen_edges.add(key)
            quote = item["sentence"]
            edges.append({
                "source_label": source["label"],
                "target_label": target["label"],
                "relation": canonical,
                "direction": direction,
                "condition": "; ".join(item.get("conditions") or [])[:200],
                "strength": "moderate",
                "strength_score": 0.6,
                "doc_id": doc["doc_id"],
                "paragraph_id": seg.get("paragraph_id"),
                "seg_id": seg["seg_id"],
                "quote": quote,
                "anchors": [seg_anchor(doc, seg, quote)],
                "origin": "baseline",
                "trigger": relation_item.get("trigger"),
            })
            if len(edges) >= max_edges:
                break
        if len(edges) >= max_edges:
            break
    return {
        "nodes": nodes,
        "edges": edges,
        "co_occurrence_candidates": [
            co_occurrence[key] for key in sorted(co_occurrence, key=lambda k: (-co_occurrence[k]["count"], k))
        ][:80],
        "notes": [
            "baseline nodes were typed by a deterministic heuristic (lexicon + suffix + formula)",
            "baseline edges require a trigger verb in the same sentence and both endpoints in the quote",
            "co_occurrence_candidates are NOT asserted relations; they are hints for the agent",
        ],
    }


# --------------------------------------------------------------------------- #
# evidence verification: every node and edge goes back to the source
# --------------------------------------------------------------------------- #
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]{2,}|[\u4e00-\u9fff]{2,}")
STOPWORDS = {"the", "and", "with", "that", "this", "these", "those", "from",
             "into", "were", "have", "been", "which", "their", "there", "however",
             "using", "used", "based", "also", "such", "more", "than", "are",
             "was", "for", "its", "appears", "material", "source", "text"}


def _keywords(text: str, limit: int = 8) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for word in WORD_RE.findall(text or ""):
        low = word.casefold()
        if low in STOPWORDS or low in seen:
            continue
        seen.add(low)
        out.append(word)
        if len(out) >= limit:
            break
    return out


def _quote_supports(quote: str, label_groups: Iterable[Iterable[str]],
                    claim_text: str = "") -> bool:
    """label_groups: AND across groups, OR inside one group (label or an alias)."""
    groups = [list(group) for group in label_groups if any(group)]
    if groups:
        return all(any(_contains_label(quote, label) for label in group) for group in groups)
    terms = _keywords(claim_text)
    if not terms:
        return bool(quote)
    hits = sum(1 for term in terms if term.casefold() in (quote or "").casefold())
    return hits >= max(1, min(2, len(terms) // 3))


def _verify_item(docs_by: dict[str, dict[str, Any]], anchors_list: list[dict[str, Any]],
                 label_groups: Iterable[Iterable[str]], claim_text: str) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    located = False
    supported = False
    for anchor in anchors_list:
        doc = docs_by.get(anchor.get("doc_id") or "")
        if not doc:
            checks.append({"match": "missing", "reason": "unknown doc_id"})
            continue
        result = anchors.verify(doc, anchor)
        checks.append(result)
        if result.get("match") in anchors.GROUNDED:
            located = True
            if _quote_supports(anchor.get("quote") or "", label_groups, claim_text):
                supported = True
    return {
        "grounded": bool(located and supported),
        "located": located,
        "supported_by_quote": supported,
        "checks": checks,
    }


def verify_evidence(docs: list[dict[str, Any]], nodes: list[dict[str, Any]],
                    edges: list[dict[str, Any]]) -> dict[str, Any]:
    """Re-read the source and assert the quotes really are there.

    An item that cannot be located is dropped from the graph by the caller and
    listed as unverified; that is deliberately stricter than letting the model
    self-report "verified".
    """
    docs_by = documents_by_id(docs)
    qualities = {str(doc.get("anchor_quality") or "text") for doc in docs}
    quality = "text"
    for candidate in ("span", "paragraph", "page", "block", "text"):
        if candidate in qualities:
            quality = candidate
            break
    total = grounded = 0
    variants: dict[str, list[str]] = {}
    for node in nodes:
        variants[node["node_id"]] = [node.get("label") or ""] + list(node.get("aliases") or [])
    node_verdicts: dict[str, dict[str, Any]] = {}
    for node in nodes:
        claim = f"{node.get('label')} ({node.get('type')}) appears in the material"
        groups = [] if node.get("is_conclusion") else [variants[node["node_id"]]]
        verdict = _verify_item(docs_by, node.get("occurrences") or [], groups, claim)
        node_verdicts[node.get("node_id") or node.get("label")] = verdict
        node["evidence_status"] = "grounded" if verdict["grounded"] else "unverified"
        total += 1
        grounded += 1 if verdict["grounded"] else 0
    edge_verdicts: dict[str, dict[str, Any]] = {}
    for edge in edges:
        claim = f"{edge.get('source_label')} {edge.get('relation')} {edge.get('target_label')}"
        groups = []
        for node_id in (edge.get("source_id"), edge.get("target_id")):
            group = variants.get(node_id or "")
            if group:
                groups.append(group)
        if not groups:
            groups = [[label] for label in (edge.get("source_label"),
                                            edge.get("target_label")) if label]
        verdict = _verify_item(docs_by, edge.get("anchors") or [], groups, claim)
        edge_verdicts[edge.get("edge_id") or f"{edge.get('source_label')}-{edge.get('target_label')}"] = verdict
        edge["evidence_status"] = "grounded" if verdict["grounded"] else "unverified"
        total += 1
        grounded += 1 if verdict["grounded"] else 0
    rate = round(grounded / total, 4) if total else 0.0
    ceiling = anchor_ceiling(quality)
    return {
        "grounding": {
            "claims_total": total,
            "grounded": grounded,
            "ungrounded": total - grounded,
            "grounding_rate": rate,
            "anchor_quality": quality,
            "confidence_ceiling": ceiling,
            "effective_confidence": round(min(rate, ceiling), 4),
            "method": "anchors.verify per doc + endpoint/claim-term check inside the quote",
        },
        "node_verdicts": node_verdicts,
        "edge_verdicts": edge_verdicts,
    }


# === END PART 3 ===
