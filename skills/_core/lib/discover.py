#!/usr/bin/env python3
"""Input discovery for the Playground Harbor skill runtime.

The evaluation container gives us ``/input/case.json`` and ``task.md``, plus
whatever the task package bundled under ``environment/``. We must NOT hardcode
those paths: the task package only promises that bundled resources resolve
somewhere beneath ``environment/``, and the per-topic material differs.

So every path is discovered, and whatever we find is recorded in the run report
so a reviewer can see exactly what the skill read.

Exit contract: ``discover()`` returns a plain dict. It never raises for a
missing optional input; it records ``missing`` instead, because the topic
requires material gaps to be reported honestly rather than papered over.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

# Directories that may hold the Agent-visible inputs, most specific first.
INPUT_ROOTS = ("/input", "/app/input", "/app/work", "/app/environment", "/app")
OUTPUT_ROOTS = ("/app/output", "/output", "output")

# A case file is any JSON that names the case or carries the output contract.
CASE_MARKERS = ("case_id", "output_contract", "caseId")

TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".tsv"}
DOC_SUFFIXES = {".pdf", ".xml", ".docx", ".pptx", ".tex", ".bib"}
MATERIAL_SUFFIXES = TEXT_SUFFIXES | DOC_SUFFIXES

# Never treat our own deliverables as input material.
EXCLUDE_DIR_NAMES = {"output", "outputs", "result", "results", "__pycache__", ".git", "node_modules"}


def _iter_roots(roots: Iterable[str]) -> list[Path]:
    seen: list[Path] = []
    for raw in roots:
        p = Path(raw)
        if p.is_dir() and p not in seen:
            seen.append(p)
    return seen


def _walk_files(root: Path, max_files: int = 2000) -> list[Path]:
    """Bounded walk, skipping output and VCS directories."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIR_NAMES]
        for name in filenames:
            out.append(Path(dirpath) / name)
            if len(out) >= max_files:
                return out
    return out


def _looks_like_case(path: Path) -> bool:
    if path.suffix.lower() != ".json":
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    return any(k in data for k in CASE_MARKERS)


def _first_existing(candidates: Iterable[Path]) -> Path | None:
    for c in candidates:
        if c.is_file():
            return c
    return None


def _locate_case(roots: list[Path], files: list[Path]) -> Path | None:
    # 1) the documented location
    for root in roots:
        direct = _first_existing([root / "case.json", root / "input" / "case.json"])
        if direct:
            return direct
    # 2) any case-shaped JSON near the top of an input root
    for root in roots:
        for candidate in sorted(root.glob("*.json")):
            if _looks_like_case(candidate):
                return candidate
    # 3) anywhere in the bounded walk (shallow first)
    for candidate in sorted(files, key=lambda p: len(p.parts)):
        if _looks_like_case(candidate):
            return candidate
    return None


def _locate_task_md(roots: list[Path], files: list[Path]) -> Path | None:
    for root in roots:
        hit = _first_existing([root / "task.md", root / "instruction.md", root / "README.md"])
        if hit:
            return hit
    for candidate in sorted(files, key=lambda p: len(p.parts)):
        if candidate.name.lower() in {"task.md", "instruction.md"}:
            return candidate
    return None


def discover() -> dict[str, Any]:
    """Find the case, the task brief, the material files, and an output directory."""
    roots = _iter_roots(INPUT_ROOTS)
    if not roots:
        roots = [Path.cwd()]

    files: list[Path] = []
    for root in roots[:3]:  # bound the scan; the first roots are the specific ones
        files.extend(_walk_files(root))
    # de-duplicate while keeping order
    seen: set[str] = set()
    uniq_files: list[Path] = []
    for f in files:
        key = str(f.resolve()) if f.exists() else str(f)
        if key not in seen:
            seen.add(key)
            uniq_files.append(f)

    case_path = _locate_case(roots, uniq_files)
    task_md = _locate_task_md(roots, uniq_files)

    material_docs = [
        f for f in uniq_files
        if f.suffix.lower() in DOC_SUFFIXES and f != case_path
        and not any(part in EXCLUDE_DIR_NAMES for part in f.parts)
    ]
    # Prefer the canonical paper location when present.
    canonical_paper = [f for f in material_docs if f.name == "paper.pdf" or "reference" in f.parts]
    ordered_docs = canonical_paper + [f for f in material_docs if f not in canonical_paper]

    out_dir = None
    for raw in OUTPUT_ROOTS:
        p = Path(raw)
        try:
            p.mkdir(parents=True, exist_ok=True)
            out_dir = p
            break
        except Exception:
            continue
    if out_dir is None:
        out_dir = Path.cwd() / "output"
        out_dir.mkdir(parents=True, exist_ok=True)

    missing = []
    if case_path is None:
        missing.append("case.json")
    if task_md is None:
        missing.append("task.md")
    if not ordered_docs:
        missing.append("material documents (pdf/xml/docx/tex)")

    return {
        "input_roots": [str(r) for r in roots],
        "case_path": str(case_path) if case_path else None,
        "task_md_path": str(task_md) if task_md else None,
        "material_docs": [str(p) for p in ordered_docs],
        "output_dir": str(out_dir),
        "files_scanned": len(uniq_files),
        "missing": missing,
    }


if __name__ == "__main__":
    print(json.dumps(discover(), ensure_ascii=False, indent=2))
