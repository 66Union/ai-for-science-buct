#!/usr/bin/env python3
"""Independent end-to-end check for a packaged Skill submission.

This is the Lead's acceptance gate, deliberately independent of the tests each
topic ships: it builds a throw-away input sandbox that mimics the Harbor runtime
(/input-shaped case.json + task.md + materials), runs the packaged runner there and
checks the one contract the platform enforces:

    /app/output/result.json exists, is valid JSON, carries case_id and every field
    declared by case.json.output_contract, and reports an honest status.

It also exercises the two failure paths the scoring container will hit on a bad day:
a finalize without any agent-generated content (the degradation path) and an input
directory with no material at all. A run that crashes instead of leaving a result
file fails the check.

Usage:
    python tools/e2e_check.py --package out/submit-reading-03-translation
    python tools/e2e_check.py --package out/submit-x --script scripts/run_x.py --keep
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORK = REPO / ".tmp" / "e2e"
CORE_TESTS = REPO / "skills" / "_core" / "tests"

CONTRACT_A = {
    "case_id": "e2e-case-alpha",
    "task": "end-to-end acceptance fixture",
    "output_contract": {
        "result_payload": {"type": "object", "description": "the main structured result"},
        "source_evidence": {"type": "array", "description": "anchors back into the source"},
        "run_quality": {"type": "object", "description": "quality and coverage metrics"},
        "cover_letter": {"type": "string", "description": "one-paragraph honest summary"},
        "zz_unmodelled_field": {"type": "array", "description": "nothing produces this"},
    },
}

DOCX_XML = (
    "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
    "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
    "<w:body>"
    "<w:p><w:r><w:t>Polished Abstract</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>Supported nickel catalysts reach 92% selectivity at 750 K [1].</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>We may conclude that sintering limits stability above 800 K [2].</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>2 Methods</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>Samples were dried at 373 K for 12 h; nickel loading was 5 wt%.</w:t></w:r></w:p>"
    "</w:body></w:document>"
)

BIBTEX = """@article{smith2019,
  author = {Smith, J. and Doe, A.},
  title = {Selectivity of supported nickel catalysts},
  journal = {Journal of Catalysis},
  year = {2019}, volume = {371}, pages = {112--120}, doi = {10.1016/j.jcat.2019.01.001}
}
@inproceedings{chen2020,
  author = {Chen, L.},
  title = {Sintering in reforming catalysts},
  booktitle = {Proceedings of the Catalysis Conference},
  year = {2020}, pages = {4455--4463}, doi = {10.1021/acscatal.0b00123}
}
"""

TEX = r"""\documentclass{article}
\usepackage{amsmath,booktabs}
\title{Supported Nickel Catalysts}
\author{A. Author}
\begin{document}
\maketitle
\begin{abstract}
We report 92\% selectivity at 750 K \cite{smith2019}.
\end{abstract}
\section{Introduction}
Supported catalysts are widely used \cite{chen2020}. See Figure~\ref{fig:one} and Eq.~\eqref{eq:rate}.
\section{Methods}
The rate constant follows
\begin{equation}
k = A \exp(-E_a/RT) \label{eq:rate}
\end{equation}
\begin{table}[h]\centering
\begin{tabular}{lll}\toprule T (K) & Conversion & Selectivity \\ \midrule 750 & 88 & 92 \\ \bottomrule\end{tabular}
\caption{Measured performance.}\label{tab:one}
\end{table}
\begin{figure}[h]\centering\fbox{plot}\caption{Conversion vs temperature.}\label{fig:one}\end{figure}
\section{Results}
Selectivity peaks at 750 K. This may indicate a kinetic limit \cite{smith2019}.
\bibliographystyle{plain}
\bibliography{refs}
\end{document}
"""

CONFIG_JSON = json.dumps({"optimizer": {"lr": 0.001, "weight_decay": 0.0001, "epochs": 300},
                          "data": {"dataset": "catalyst-bench-v2", "split": {"train": 0.8, "val": 0.1, "test": 0.1}},
                          "seed": 42, "batch_size": 32}, indent=2)

LOG = """2026-01-01 09:00 start training seed=42
2026-01-01 09:00 dataset=catalyst-bench-v2 train=8000 val=1000 test=1000
2026-01-01 09:01 model=gnn-v3 lr=0.001 weight_decay=0.0001 epochs=300 batch_size=32
2026-01-01 12:00 epoch=300 val_loss=0.041 test_accuracy=0.913
2026-01-01 12:01 gpu=NVIDIA A100 driver=535.54 cuda=12.2
"""


def make_pdf(path: Path) -> None:
    """Reuse the shared fixture generator so the e2e paper stays realistic."""
    sys.path.insert(0, str(CORE_TESTS))
    import importlib

    make_fixture = importlib.import_module("make_fixture")
    make_fixture.make_pdf(path)


def make_docx(path: Path) -> None:
    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml",
                         "<?xml version='1.0'?><Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'>"
                         "<Default Extension='rels' ContentType='application/vnd.openxmlformats-package.relationships+xml'/>"
                         "<Default Extension='xml' ContentType='application/xml'/>"
                         "<Override PartName='/word/document.xml' ContentType='application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'/></Types>")
        archive.writestr("_rels/.rels",
                         "<?xml version='1.0'?><Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
                         "<Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument' Target='word/document.xml'/></Relationships>")
        archive.writestr("word/document.xml", DOCX_XML)


CONTRACT_LIST = ["parsed_pages", "full_text", "section_outline"]
CONTRACT_NESTED = {"task": {"case_id": "e2e-case-gamma",
                            "output_contract": {
                                "document_structure": {"type": "object"},
                                "evidence_bundle": {"type": "array"},
                                "run_status": {"type": "string"},
                            }}}
CONTRACT_CORRUPT = "{ this is not valid json, a real case.json never is, but robustness matters"


def contract_payload(shape: str | None) -> str:
    """Serialise the requested case.json shape."""
    if shape == "dict":
        return json.dumps(CONTRACT_A, ensure_ascii=False, indent=2)
    if shape == "list":
        return json.dumps({"case_id": "e2e-case-alpha", "output_contract": CONTRACT_LIST},
                          ensure_ascii=False, indent=2)
    if shape == "nested":
        return json.dumps(CONTRACT_NESTED, ensure_ascii=False, indent=2)
    if shape == "corrupt":
        return CONTRACT_CORRUPT
    return json.dumps({"case_id": "e2e-case-beta", "task": "no contract declared"}, indent=2)


def expected_fields(shape: str | None) -> list[str]:
    if shape == "dict":
        return list(CONTRACT_A["output_contract"])
    if shape == "list":
        return list(CONTRACT_LIST)
    if shape == "nested":
        return list(CONTRACT_NESTED["task"]["output_contract"])
    return []


def build_input(root: Path, variant: str, *, contract: str | None = "dict",
                material: str | None = "flat") -> Path:
    """Create an /input-like sandbox for one variant.

    contract: "dict" | "list" | "nested" | "corrupt" | None
    material: "flat" (environment/*) | "canonical" (environment/reference/paper.pdf +
              instruction.md) | None
    """
    inbox = root / variant
    shutil.rmtree(inbox, ignore_errors=True)
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "environment").mkdir(exist_ok=True)
    (inbox / "case.json").write_text(contract_payload(contract), encoding="utf-8")

    if material == "canonical":
        reference = inbox / "environment" / "reference"
        reference.mkdir(exist_ok=True)
        try:
            make_pdf(reference / "paper.pdf")
        except Exception as exc:
            print(f"  warn: could not build the synthetic PDF: {exc}")
        (inbox / "environment" / "resources.yaml").write_text(
            "resources:\n  - name: paper\n    path: reference/paper.pdf\n"
            "    description: the paper to read\n", encoding="utf-8")
        (inbox / "instruction.md").write_text(
            "# Task\n\nRead environment/reference/paper.pdf and produce the structured "
            "result required by case.json.output_contract.\n", encoding="utf-8")
        return inbox

    if material == "flat":
        try:
            make_pdf(inbox / "environment" / "paper.pdf")
        except Exception as exc:  # pragma: no cover - fixture import must work
            print(f"  warn: could not build the synthetic PDF: {exc}")
        make_docx(inbox / "environment" / "manuscript.docx")
        (inbox / "environment" / "refs.bib").write_text(BIBTEX, encoding="utf-8")
        (inbox / "environment" / "main.tex").write_text(TEX, encoding="utf-8")
        (inbox / "environment" / "config.json").write_text(CONFIG_JSON, encoding="utf-8")
        (inbox / "environment" / "train.log").write_text(LOG, encoding="utf-8")

    (inbox / "task.md").write_text(
        "# Task\n\nRead the material and produce the structured result required by "
        "case.json.output_contract. Every finding must point back to its source "
        "location. Report honestly what could not be determined.\n", encoding="utf-8")
    return inbox


def find_script(package: Path, explicit: str | None) -> Path | None:
    if explicit:
        candidate = package / explicit
        return candidate if candidate.is_file() else None
    scripts = sorted(p for p in (package / "scripts").glob("run_*.py")
                     if p.name not in {"run_reading.py"})
    return scripts[0] if scripts else None


def supported_modes(package: Path, script: Path) -> set[str]:
    """Which phase flags does this runner accept? (not every skill needs two phases)"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(package)
    try:
        proc = subprocess.run([sys.executable, str(script), "--help"], env=env,
                              capture_output=True, text=True, timeout=60)
        text = (proc.stdout or "") + (proc.stderr or "")
    except Exception:
        return set()
    return {flag for flag in ("--prepare", "--finalize", "--all") if flag in text}


def run_script(package: Path, script: Path, inbox: Path, mode: str, timeout: int) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(package)
    env["PYTHONIOENCODING"] = "utf-8"
    # importing lib/* from the package must not leave __pycache__ inside the bundle
    # we are about to upload
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    argv = [sys.executable, str(script)]
    if mode != "default":
        argv.append(f"--{mode}")
    argv.extend(["--out", str(inbox / "output")])
    try:
        proc = subprocess.run(argv, cwd=str(inbox), env=env, capture_output=True,
                              text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
        return {"argv": argv, "returncode": proc.returncode,
                "stdout": (proc.stdout or "")[-2500:], "stderr": (proc.stderr or "")[-2500:]}
    except subprocess.TimeoutExpired:
        return {"argv": argv, "returncode": None, "error": f"timeout {timeout}s"}


def check_result(inbox: Path, expected: list[str], case_id: str | None,
                 *, require_evidence: bool) -> list[str]:
    problems: list[str] = []
    result_path = inbox / "output" / "result.json"
    if not result_path.is_file():
        return [f"missing result.json under {inbox / 'output'}"]
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"result.json is not valid JSON: {exc}"]
    if not isinstance(data, dict):
        return ["result.json is not a JSON object"]

    if case_id and data.get("case_id") != case_id:
        problems.append(f"case_id mismatch: {data.get('case_id')!r} != {case_id!r}")
    for field in expected:
        if field not in data:
            problems.append(f"declared contract field missing from result.json: {field}")
    meta = data.get("meta")
    if not isinstance(meta, dict):
        problems.append("meta object missing")
    else:
        if meta.get("status") not in {"success", "partial", "failed"}:
            problems.append(f"unexpected meta.status: {meta.get('status')!r}")
        if "contract_detected" not in meta:
            problems.append("meta.contract_detected missing")
    if require_evidence and not (inbox / "output" / "evidence.json").is_file():
        problems.append("evidence.json missing")
    if require_evidence and not (inbox / "output" / "run_report.json").is_file():
        problems.append("run_report.json missing")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True)
    parser.add_argument("--script", default=None)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--keep", action="store_true")
    ns = parser.parse_args()

    package = Path(ns.package).resolve()
    if not package.is_dir():
        print(f"FAIL package directory not found: {package}")
        return 1
    script = find_script(package, ns.script)
    if not script:
        print(f"FAIL no runner script found under {package / 'scripts'}")
        return 1
    print(f"package : {package}")
    print(f"script  : {script.relative_to(package)}")

    modes = supported_modes(package, script)
    print(f"modes   : {sorted(modes) or ['default (single-phase runner)']}")
    primary = "all" if "--all" in modes else "default"
    # variant -> (mode, contract shape, material shape, expected contract fields,
    #             expected case_id, require evidence/run-report)
    variants = [
        ("full", primary, "dict", "flat",
         expected_fields("dict"), CONTRACT_A["case_id"], True),
        # a contract written as a list of names, and one nested under task.* - both
        # are shapes the reflection layer must tolerate on a hidden case
        ("contract_list", primary, "list", "flat",
         expected_fields("list"), "e2e-case-alpha", False),
        # the documented canonical layout: environment/reference/paper.pdf + instruction.md
        ("canonical_layout", primary, "dict", "canonical",
         expected_fields("dict"), CONTRACT_A["case_id"], True),
        ("no_contract", primary, None, "flat", [], "e2e-case-beta", False),
        # a malformed case.json must not take the run down: a result file is still
        # required, it just cannot promise contract fields
        ("corrupt_case", primary, "corrupt", "flat", [], None, False),
        ("no_material", primary, "dict", None,
         expected_fields("dict"), CONTRACT_A["case_id"], False),
    ]
    if "--finalize" in modes:
        # the degradation path: finalize with no agent-generated batches at all
        variants.insert(1, ("full_finalize_only", "finalize", "dict", "flat",
                            expected_fields("dict"), CONTRACT_A["case_id"], True))

    root = WORK / package.name
    failures: list[str] = []
    for variant, mode, contract_shape, material_shape, expected, case_id, require_evidence in variants:
        inbox = build_input(root, variant, contract=contract_shape, material=material_shape)
        print(f"\n[{variant}] mode=--{mode} cwd={inbox}")
        outcome = run_script(package, script, inbox, mode, ns.timeout)
        rc = outcome.get("returncode")
        print(f"  exit={rc}")
        if rc != 0:
            failures.append(f"{variant}: runner exited {rc}")
            tail = (outcome.get("stderr") or outcome.get("stdout") or "")[-800:]
            print("  tail:", tail.replace("\n", "\n  "))
        problems = check_result(inbox, expected, case_id, require_evidence=require_evidence)
        for problem in problems:
            print("  PROBLEM:", problem)
            failures.append(f"{variant}: {problem}")
        if not problems:
            data = json.loads((inbox / "output" / "result.json").read_text(encoding="utf-8"))
            print(f"  status={data.get('meta', {}).get('status')} "
                  f"fields={len(data)} bytes={(inbox / 'output' / 'result.json').stat().st_size}")

    if not ns.keep:
        shutil.rmtree(root, ignore_errors=True)
    print("\n" + ("PASS: " + package.name if not failures else
                  f"FAIL({len(failures)}): " + package.name))
    for failure in failures:
        print(" -", failure)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
