#!/usr/bin/env python3
"""Self-test for the writing-13 LaTeX authoring/compilation skill.

Standard library only. Every temporary file is written under <package>/_selftest_tmp
and deleted in a finally block: tempfile under the restricted sandbox cannot write
to %TEMP%, and the packaged skill must be verifiable without any dev-layout files.

Covered:
  1. Log parsing: several independent diagnostics, file:line:root-cause records,
     undefined references/citations, missing package/font, rerun detection.
  2. Engine planning and source hints (CJK/fontspec -> xelatex; latexmk preferred).
  3. Static audit: undefined ref, missing bib entry, duplicate label, missing
     end itemize, missing end document, package conflict, BOM.
  4. Fix application: strict literal replacement, ambiguous/no-op/unmatched
     rejection, and a line hint for disambiguation.
  5. End-to-end complete run: prepare -> simulated agent fixes.json -> finalize
     reflects a deliberately differently-named output_contract, writes result.json /
     evidence.json / run_report.json / out/source and reports partial with
     compile_unavailable when the image has no TeX engine.
  6. End-to-end zero-generation run: --all with no generated content degrades to
     status=partial without crashing and still carries every contract field.
  7. Failure path: no LaTeX project -> status=failed with the declared shape.
  8. Determinism: two consecutive prepare runs produce byte-identical audits.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
try:  # packaged layout: <skill>/lib sits next to <skill>/scripts and <skill>/tests
    sys.path.insert(0, str(ROOT))
    from lib import hybrid  # noqa: E402
except ImportError:  # development layout: <repo>/skills/_core/lib
    sys.path.insert(0, str(ROOT.parent / "_core"))
    from lib import hybrid  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))

import run_latex  # noqa: E402

TMP = ROOT / "_selftest_tmp"
SCRIPT = ROOT / "scripts" / "run_latex.py"
FAILS = []

CONTRACT = {
    "case_id": {"type": "string"},
    "corrected_tex_sources": {"type": "array"},
    "source_static_checks": {"type": "object"},
    "latex_compile_report": {"type": "object"},
    "compiled_pdf_path": {"type": "string"},
    "first_page_png": {"type": "string"},
    "latex_log_excerpt": {"type": "string"},
    "build_reproducibility": {"type": "object"},
    "remaining_latex_warnings": {"type": "array"},
    "located_errors": {"type": "array"},
    "applied_fix_records": {"type": "array"},
    "project_structure": {"type": "object"},
    "template_adaptation_notes": {"type": "object"},
    "run_quality": {"type": "object"},
    "evidence_anchors": {"type": "array"},
    "zzz_unmodelled_field": {"type": "string"},
}

MAIN_TEX = r"""\documentclass[11pt]{article}
\usepackage{amsmath}
\usepackage{graphicx}
\usepackage{subfigure}
\usepackage{subfig}
\usepackage{hyperref}
\usepackage{cleveref}
\usepackage{amsmath}
\title{A Reproducible Study of Nickel Catalysts}
\author{A. Author}
\begin{document}
\maketitle
\begin{abstract}
We report 10 mol% Ni on SiO2 with a measured rate of 3.2 s-1.
\end{abstract}

\section{Introduction}
\label{sec:intro}
Catalysis is important \cite{smith2019} and \cite{missing2020}.
See Section~\ref{sec:intr} for details.
\label{sec:dup}
\label{sec:dup}

\begin{figure}
\centering
\includegraphics[width=0.5\textwidth]{figures/plot.png}
\caption{A plot of conversion against temperature.}
\end{figure}

\begin{itemize}
\item first observation
\item second observation

\input{sections/results}

\bibliographystyle{plain}
\bibliography{refs}
"""

RESULTS_TEX = r"""\section{Results}
\label{sec:results}
The conversion was 85\% at 523 K and stayed stable for 10 h.
\begin{equation}
k = 3.2 \, \mathrm{s}^{-1}
\end{equation}
"""

REFS_BIB = r"""@article{smith2019,
  author = {Smith, John and Lee, Ann},
  title = {Nickel catalysis for methane conversion},
  journal = {Journal of Catalysis},
  year = {2019},
  volume = {12},
  pages = {100--110}
}

@article{jones2021,
  author = {Jones, Mary},
  title = {Support effects in nickel catalysts},
  journal = {Catalysis Today},
  year = {2021}
}
"""

PNG_BYTES = bytes([137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82,
                   0, 0, 0, 1, 0, 0, 0, 1, 8, 6, 0, 0, 0, 31, 21, 196, 137,
                   0, 0, 0, 10, 73, 68, 65, 84, 120, 156, 99, 0, 1, 0, 0, 5, 0, 1,
                   13, 10, 45, 180, 0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130])


def check(name, condition, detail=""):
    if condition:
        print("  ok   %s" % name)
    else:
        FAILS.append("%s: %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def run_script(cwd: Path, *args: str):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=str(cwd),
                          capture_output=True, text=True, timeout=300)


def make_fixture(name: str) -> Path:
    fixture = TMP / name
    if fixture.exists():
        shutil.rmtree(fixture)
    (fixture / "sections").mkdir(parents=True)
    (fixture / "figures").mkdir(parents=True)
    (fixture / "case.json").write_text(json.dumps({
        "case_id": "latex-selftest-001",
        "output_contract": CONTRACT,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (fixture / "task.md").write_text(
        "# Task\nFix the LaTeX build errors, keep the facts unchanged and deliver "
        "compilable sources.\n", encoding="utf-8")
    (fixture / "main.tex").write_text(MAIN_TEX, encoding="utf-8")
    (fixture / "sections" / "results.tex").write_text(RESULTS_TEX, encoding="utf-8")
    (fixture / "refs.bib").write_text(REFS_BIB, encoding="utf-8")
    (fixture / "figures" / "plot.png").write_bytes(PNG_BYTES)
    return fixture


def agent_fixes():
    return [
        {"file": "main.tex", "line": 4, "error_type": "package_conflict",
         "original": "\\usepackage{subfigure}\n", "fixed": "",
         "reason": "subfigure conflicts with subfig; the project keeps subfig"},
        {"file": "main.tex", "error_type": "duplicate_package",
         "original": "\\usepackage{amsmath}", "fixed": "\\usepackage{amsmath}",
         "reason": "ambiguous/no-op probe: must be rejected"},
        {"file": "main.tex", "error_type": "undefined_reference",
         "original": "\\ref{sec:intr}", "fixed": "\\ref{sec:intro}",
         "reason": "only sec:intro exists; the PDF would print ??"},
        {"file": "main.tex", "error_type": "missing_end_environment",
         "original": "\\item second observation",
         "fixed": "\\item second observation\n\\end{itemize}",
         "reason": "the itemize environment is never closed"},
        {"file": "main.tex", "error_type": "missing_end_document",
         "original": "\\bibliography{refs}",
         "fixed": "\\bibliography{refs}\n\\end{document}",
         "reason": "main file ends without \\end{document}"},
        {"file": "not-a-file.tex", "error_type": "other", "original": "x", "fixed": "y",
         "reason": "unknown file probe: must be rejected"},
    ]


# --------------------------------------------------------------------------- #
# 1. compile-log parsing
# --------------------------------------------------------------------------- #
def test_log_parsing():
    print("log parsing")
    log = "\n".join([
        "This is XeTeX, Version 3.141592653-2.6-0.999995 (TeX Live 2023)",
        "(./main.tex",
        "! LaTeX Error: File \x60missingpkg.sty' not found.",
        "",
        "Type X to quit or <RETURN> to proceed,",
        "l.12 \\usepackage{missingpkg}",
        "! Undefined control sequence.",
        "l.20 \\badmacro",
        "! Package foo Error: bar clash.",
        "l.33 \\foo",
        "LaTeX Warning: Reference \x60sec:result' on page 1 undefined on input line 21.",
        "LaTeX Warning: Citation \x60missing2020' on page 1 undefined on input line 22.",
        "LaTeX Warning: Label(s) may have changed. Rerun to get cross-references right.",
        "Overfull \\hbox (12.5pt too wide) in paragraph at lines 40--42",
        ")",
    ])
    parsed = run_latex.parse_tex_log(log)
    types = [error["error_type"] for error in parsed["errors"]]
    check("multiple errors collected", len(parsed["errors"]) >= 3, str(types))
    check("missing package classified", "missing_package" in types, str(types))
    check("missing package name captured",
          any(error.get("missing") == "missingpkg.sty" for error in parsed["errors"]),
          str(parsed["errors"]))
    check("undefined control sequence classified",
          "undefined_control_sequence" in types, str(types))
    check("package error classified", "package_error" in types, str(types))
    check("line number extracted",
          any(error.get("line") == 12 for error in parsed["errors"]),
          str([error.get("line") for error in parsed["errors"]]))
    check("file inferred from log context",
          any((error.get("file") or "").endswith("main.tex") for error in parsed["errors"]),
          str([error.get("file") for error in parsed["errors"]]))
    check("root cause is more than the message",
          all(len(error.get("root_cause") or "") > 10 for error in parsed["errors"]))
    check("undefined reference parsed",
          parsed["undefined_references"][0]["key"] == "sec:result",
          str(parsed["undefined_references"]))
    check("undefined citation parsed",
          parsed["undefined_citations"][0]["key"] == "missing2020",
          str(parsed["undefined_citations"]))
    check("rerun detected", parsed["rerun_needed"] is True)
    check("overfull counted", parsed["overfull_hboxes"] == 1)

    # file:line:error output of -file-line-error must also be understood
    file_line_log = "./main.tex:17: Undefined control sequence.\nl.17 \\oops\n"
    parsed_two = run_latex.parse_tex_log(file_line_log)
    check("file-line-error parsed", bool(parsed_two["errors"]), str(parsed_two["errors"]))
    check("file-line-error location",
          (parsed_two["errors"][0].get("line") == 17
           or str(parsed_two["errors"][0].get("location", "")).endswith(":17")),
          str(parsed_two["errors"][0]))
    bib_parsed = run_latex.parse_bibtex_log(
        "Warning--I didn't find a database entry for x\n"
        "I couldn't open database file refs.bib\n")
    check("bibtex log parsing", bool(bib_parsed["errors"]), str(bib_parsed))


# --------------------------------------------------------------------------- #
# 2. engine plan and source hints
# --------------------------------------------------------------------------- #
def test_engine_plan():
    print("engine plan")
    plan = run_latex.engine_plan({"tex": ["latexmk", "pdflatex", "xelatex"]}, "xelatex")
    check("latexmk first", plan[0]["binary"] == "latexmk", str(plan))
    check("latexmk uses the xelatex flag", plan[0]["flag"] == "-xelatex", str(plan[0]))
    check("pdflatex fallback present",
          any(item["binary"] == "pdflatex" for item in plan), str(plan))
    check("no engine means empty plan",
          run_latex.engine_plan({"tex": []}, "pdflatex") == [])
    check("tabular columns", run_latex.tabular_columns("lcr") == 3)
    check("tabular starred columns",
          run_latex.tabular_columns("*{2}{c}|p{3cm}") == 3)
    inventory = {"packages": [{"name": "ctex"}], "document_class": {"name": "article"}}
    guess = run_latex.guess_engine(inventory, {"main.tex": "中文测试"},
                                   {"main": "main.tex"})
    check("cjk source means xelatex", guess["engine"] == "xelatex", str(guess))
    inventory_two = {"packages": [{"name": "amsmath"}],
                     "document_class": {"name": "article"}}
    guess_two = run_latex.guess_engine(inventory_two, {"main.tex": "plain ascii"},
                                       {"main": "main.tex"})
    check("ascii source means pdflatex", guess_two["engine"] == "pdflatex",
          str(guess_two))


# --------------------------------------------------------------------------- #
# 3. static audit on a real project tree
# --------------------------------------------------------------------------- #
def test_static_audit():
    print("static audit")
    fixture = make_fixture("static")
    try:
        found = {
            "input_roots": [str(fixture)],
            "case_path": str(fixture / "case.json"),
            "task_md_path": str(fixture / "task.md"),
            "material_docs": [str(fixture / "main.tex"), str(fixture / "refs.bib")],
            "output_dir": str(fixture / "out"),
        }
        project = run_latex.load_tex_project(found, fixture / "out", fixture / "work")
        check("project discovered", project["ok"] is True, str(project.get("reason")))
        check("main file", project["main"] == "main.tex", project["main"])
        check("include resolved",
              any(edge.get("child") == "results.tex"
                  for edge in project["include_edges"]),
              str(project["include_edges"]))
        check("figure discovered",
              "figures/plot.png" in [entry["path"] for entry in project["files"]],
              str([entry["path"] for entry in project["files"]]))
        uncertainty = []
        scan = run_latex.scan_project(project, hybrid.Stopwatch(), uncertainty)
        types = {issue["error_type"] for issue in scan["issues"]}
        for expected in ("undefined_reference", "missing_bib_entry", "duplicate_label",
                         "missing_end_environment", "missing_end_document",
                         "package_conflict"):
            check("detects %s" % expected, expected in types, str(sorted(types)))
        check("undefined ref gets a similar-label suggestion",
              any((issue.get("suggested_fix") or {}).get("fixed") == "\\ref{sec:intro}"
                  for issue in scan["issues"]),
              str([issue.get("suggested_fix") for issue in scan["issues"]
                   if issue.get("error_type") == "undefined_reference"]))
        check("duplicate label identified by name",
              any(issue["signature"] == "duplicate_label|sec:dup"
                  for issue in scan["issues"]))
        check("labels and references inventoried",
              len(scan["inventory"]["labels"]) >= 3
              and len(scan["inventory"]["references"]) >= 1,
              str(scan["inventory"]["labels"]))
        check("bib entries parsed", len(scan["inventory"]["bib_entries"]) == 2,
              str(scan["inventory"]["bib_entries"]))
        check("uncited bib entry reported",
              any(issue["error_type"] == "uncited_bib_entry"
                  for issue in scan["issues"]))

        bom_fixture = fixture / "bom.tex"
        bom_fixture.write_bytes(b"\xef\xbb\xbf\\section{BOM}\n")
        issues = []
        run_latex.scan_encoding("bom.tex", bom_fixture, issues, [])
        check("BOM detected",
              any(issue["error_type"] == "utf8_bom" for issue in issues), str(issues))

        scan_two = run_latex.scan_project(project, hybrid.Stopwatch(), [])
        check("deterministic audit",
              [issue["issue_id"] for issue in scan["issues"]]
              == [issue["issue_id"] for issue in scan_two["issues"]])
    finally:
        shutil.rmtree(fixture, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 4. strict fix application
# --------------------------------------------------------------------------- #
def test_apply_fixes():
    print("apply fixes")
    fixture = make_fixture("apply")
    try:
        found = {
            "input_roots": [str(fixture)],
            "case_path": str(fixture / "case.json"),
            "task_md_path": str(fixture / "task.md"),
            "material_docs": [],
            "output_dir": str(fixture / "out"),
        }
        project = run_latex.load_tex_project(found, fixture / "out", fixture / "work")
        base = fixture / "fixed"
        run_latex.reset_tree(base)
        run_latex.copy_project_tree(project, base)
        fix_list = [
            {"file": "main.tex", "error_type": "x", "original": "\\ref{sec:intr}",
             "fixed": "\\ref{sec:intro}", "reason": "typo"},
            {"file": "main.tex", "error_type": "x",
             "original": "TEXT-THAT-IS-NOT-THERE", "fixed": "y",
             "reason": "unmatched"},
            {"file": "main.tex", "error_type": "x",
             "original": "\\usepackage{amsmath}", "fixed": "\\usepackage{amsmath}",
             "reason": "ambiguous and a no-op"},
            {"file": "nope.tex", "error_type": "x", "original": "a", "fixed": "b",
             "reason": "unknown file"},
        ]
        report = run_latex.apply_fixes(base, project, fix_list, hybrid.Stopwatch())
        check("one fix applied", len(report["applied"]) == 1, str(report["applied"]))
        check("three fixes rejected", len(report["rejected"]) == 3,
              str([item["reason"] for item in report["rejected"]]))
        reasons = " | ".join(item["reason"] for item in report["rejected"])
        check("unmatched reported", "not found" in reasons, reasons)
        check("ambiguous reported",
              "ambiguous" in reasons or "occurs 2 times" in reasons, reasons)
        check("unknown file reported",
              "not part of the delivered project" in reasons, reasons)
        text = (base / "main.tex").read_text(encoding="utf-8")
        check("replacement written",
              "\\ref{sec:intro}" in text and "\\ref{sec:intr}" not in text)
        check("file change recorded",
              report["files_changed"][0]["path"] == "main.tex",
              str(report["files_changed"]))
        check("before and after hashes differ",
              report["files_changed"][0]["before_sha256"]
              != report["files_changed"][0]["after_sha256"])

        run_latex.reset_tree(base)
        run_latex.copy_project_tree(project, base)
        hinted = [{"file": "main.tex", "line": 8, "error_type": "duplicate_package",
                   "original": "\\usepackage{amsmath}", "fixed": "",
                   "reason": "the second load is redundant"}]
        hinted_report = run_latex.apply_fixes(base, project, hinted,
                                              hybrid.Stopwatch())
        check("line hint applies an ambiguous fix",
              len(hinted_report["applied"]) == 1,
              str(hinted_report["rejected"]))
        after = (base / "main.tex").read_text(encoding="utf-8")
        check("line hint removed the second load",
              after.count("\\usepackage{amsmath}") == 1,
              str(after.count("\\usepackage{amsmath}")))
    finally:
        shutil.rmtree(fixture, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 5. end-to-end: prepare -> agent fixes -> finalize
# --------------------------------------------------------------------------- #
def test_end_to_end():
    print("end-to-end complete run")
    fixture = make_fixture("e2e")
    try:
        prepared = run_script(fixture, "--prepare", "--out", "out", "--work", "work")
        check("prepare exit", prepared.returncode == 0, prepared.stderr[-800:])
        work = fixture / "work"
        out = fixture / "out"
        check("state written", (work / "state.json").is_file())
        check("index written", (work / "tex_index.json").is_file())
        check("worksheet written", (work / "fixes_worksheet.json").is_file())
        check("audit written", (work / "static_issues.json").is_file())
        check("editable tree prepared", (work / "fixed" / "main.tex").is_file())
        worksheet = load_json(work / "fixes_worksheet.json")
        check("worksheet has issues", len(worksheet["issues"]) > 0,
              str(worksheet.get("issue_count")))
        check("worksheet documents the answer format",
              bool(worksheet.get("answer_format"))
              and bool(worksheet["answer_format"][0]["error_type"]))

        generated = work / "generated"
        generated.mkdir(parents=True, exist_ok=True)
        (generated / "fixes.json").write_text(
            json.dumps(agent_fixes(), ensure_ascii=False, indent=2), encoding="utf-8")
        (generated / "notes.json").write_text(json.dumps({
            "template_adaptation": "kept the article class, subfig is the preferred package",
            "unresolved": [{"file": "main.tex",
                            "issue": "missing2020 has no metadata in the materials"}],
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        finished = run_script(fixture, "--finalize", "--out", "out", "--work", "work")
        check("finalize exit", finished.returncode == 0, finished.stderr[-800:])
        result = load_json(out / "result.json")
        meta = result["meta"]
        check("case_id carried", result.get("case_id") == "latex-selftest-001",
              str(result.get("case_id")))
        missing = [name for name in CONTRACT if name not in result]
        check("all contract fields present", not missing, str(missing))
        expect_unreported = {"compiled_pdf_path", "first_page_png",
                             "zzz_unmodelled_field"}
        unreported = set(meta["contract_fields_not_reported"])
        check("real values mapped (only unproducible or unmodelled missing)",
              unreported <= expect_unreported, str(sorted(unreported)))
        engine_available = run_latex.render.probe().get("tex")
        if not engine_available:
            check("compile unavailable recorded",
                  "compiled_pdf_path" in unreported
                  and any("compile_unavailable" in str(limit)
                          for limit in meta.get("limits") or []),
                  str(meta.get("limits")))
            check("no-TeX run is partial", meta["status"] == "partial", meta["status"])
        else:
            check("compile attempted when an engine exists",
                  "compiled_pdf_path" not in unreported, str(sorted(unreported)))
        check("evidence.json written", (out / "evidence.json").is_file())
        check("run_report.json written", (out / "run_report.json").is_file())
        check("editable source delivered", (out / "source" / "main.tex").is_file())
        check("bibliography delivered", (out / "source" / "refs.bib").is_file())
        check("figure delivered", (out / "source" / "figures" / "plot.png").is_file())
        check("static report written", (out / "static_report.md").is_file())
        check("change log written", (out / "fixes_applied.md").is_file())
        check("build notes written", (out / "build_notes.md").is_file())
        check("reproducibility written", (out / "reproducibility.json").is_file())

        fixed_main = (out / "source" / "main.tex").read_text(encoding="utf-8")
        check("undefined ref repaired", "\\ref{sec:intro}" in fixed_main)
        check("conflicting package removed",
              "\\usepackage{subfigure}" not in fixed_main)
        check("missing environment closed", "\\end{itemize}" in fixed_main)
        check("missing document end closed", "\\end{document}" in fixed_main)
        check("unhandled defect keeps an honest record",
              any(issue.get("error_type") == "missing_bib_entry"
                  for issue in result["source_static_checks"]["issues_after"]),
              str([issue.get("error_type")
                   for issue in result["source_static_checks"]["issues_after"]]))
        check("applied fixes are recorded",
              len(result["applied_fix_records"]) >= 3,
              str(len(result["applied_fix_records"])))
        check("rejected fixes are recorded",
              any("not part of the delivered project" in str(item.get("reason"))
                  for item in meta["additional_outputs"]["fixes_rejected"]),
              str(meta["additional_outputs"].get("fixes_rejected")))
        check("quality metrics computed",
              result["run_quality"]["static_errors_after"]
              < result["run_quality"]["static_errors_before"],
              str(result["run_quality"]))
        check("build log excerpt present", bool(result["latex_log_excerpt"]))
        check("error locations carry file:line",
              all(":" in str(item.get("location"))
                  for item in result["located_errors"]))
        check("result.json parses from disk",
              isinstance(load_json(out / "result.json"), dict))
        check("no BOM in the delivered source",
              not (out / "source" / "main.tex").read_bytes().startswith(
                  b"\xef\xbb\xbf"))
        check("compile report records the probe",
              isinstance(result["latex_compile_report"].get("probe"), dict))
        check("project index lists the include graph",
              len(result["project_structure"]["include_edges"]) >= 1,
              str(result["project_structure"].get("include_edges")))
    finally:
        shutil.rmtree(fixture, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 6. zero-generation degradation and 8. determinism
# --------------------------------------------------------------------------- #
def test_zero_generation():
    print("zero-generation degraded run")
    fixture = make_fixture("zero")
    try:
        prepared = run_script(fixture, "--prepare", "--out", "out", "--work", "work")
        check("first prepare exit", prepared.returncode == 0, prepared.stderr[-500:])
        audit_one = (fixture / "work" / "static_issues.json").read_bytes()
        prepared_two = run_script(fixture, "--prepare", "--out", "out", "--work", "work")
        check("second prepare exit", prepared_two.returncode == 0,
              prepared_two.stderr[-500:])
        audit_two = (fixture / "work" / "static_issues.json").read_bytes()
        check("audit is deterministic across runs", audit_one == audit_two)

        all_run = run_script(fixture, "--all", "--out", "out", "--work", "work")
        check("all exit", all_run.returncode == 0, all_run.stderr[-800:])
        out = fixture / "out"
        result = load_json(out / "result.json")
        check("zero-generation result written",
              result.get("case_id") == "latex-selftest-001")
        check("zero-generation status is partial",
              result["meta"]["status"] == "partial", result["meta"]["status"])
        missing = [name for name in CONTRACT if name not in result]
        check("zero-generation keeps the declared shape", not missing, str(missing))
        source = out / "source" / "main.tex"
        check("source delivered even without fixes", source.is_file())
        check("untouched source equals the input",
              source.read_text(encoding="utf-8") == MAIN_TEX,
              "source differs from the input after a zero-generation run")
        report = load_json(out / "run_report.json")
        check("run report records the gap",
              any("fixes.json is missing" in str(limit)
                  for limit in report.get("limits") or []),
              str(report.get("limits"))[:400])
        check("result.json is not an empty placeholder",
              (out / "result.json").stat().st_size > 200)
    finally:
        shutil.rmtree(fixture, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 7. failure path: no LaTeX project at all
# --------------------------------------------------------------------------- #
def test_no_project():
    print("no-project failure path")
    fixture = TMP / "empty"
    if fixture.exists():
        shutil.rmtree(fixture)
    fixture.mkdir(parents=True)
    try:
        (fixture / "case.json").write_text(json.dumps({
            "case_id": "latex-empty-002", "output_contract": CONTRACT,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        (fixture / "task.md").write_text("# empty case\n", encoding="utf-8")
        (fixture / "notes.md").write_text("no tex here\n", encoding="utf-8")
        proc = run_script(fixture, "--all", "--out", "out", "--work", "work")
        check("no-project exit is 0 (a result is still written)",
              proc.returncode == 0, proc.stderr[-800:])
        result = load_json(fixture / "out" / "result.json")
        check("no-project status failed", result["meta"]["status"] == "failed",
              result["meta"]["status"])
        missing = [name for name in CONTRACT if name not in result]
        check("no-project keeps the declared shape", not missing, str(missing))
        check("no-project explains itself", bool(result["meta"].get("limits")),
              str(result["meta"].get("limits")))
    finally:
        shutil.rmtree(fixture, ignore_errors=True)


def main():
    TMP.mkdir(parents=True, exist_ok=True)
    try:
        test_log_parsing()
        test_engine_plan()
        test_static_audit()
        test_apply_fixes()
        test_end_to_end()
        test_zero_generation()
        test_no_project()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    if FAILS:
        print("\nFAILED %d check(s):" % len(FAILS))
        for failure in FAILS:
            print(" - %s" % failure)
        return 1
    print("\nall LaTeX checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
