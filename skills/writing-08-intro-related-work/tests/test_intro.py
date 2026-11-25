#!/usr/bin/env python3
"""Self-test for the writing-08 introduction / related-work skill.

Standard library only. Everything is written under <package>/_selftest_tmp and
deleted in a finally block: tempfile under the restricted sandbox cannot write to
%TEMP%, and the packaged skill must be verifiable without any dev-layout files.

Covered:
  1. BibTeX / RIS / plain reference parsing and the citation resolver
     (numeric, range, LaTeX cite, author-year, missing entry).
  2. Manuscript analysis: section detection, citation inventory, figure/table
     labels, contribution cues.
  3. End-to-end complete run: --prepare writes worksheets, simulated agent content
     is written, --finalize reflects the (deliberately differently named) case
     contract, writes result.json/evidence.json/run_report.json and the editable
     deliverables, and reports status=success.
  4. End-to-end degraded run: no generated content -> deterministic baselines,
     status=partial, every contract field still present.
  5. Failure paths: no manuscript -> status=failed; broken generated JSON -> the
     run degrades instead of crashing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
try:  # packaged layout: <skill>/lib sits next to <skill>/tests
    sys.path.insert(0, str(ROOT))
    from lib import hybrid  # noqa: E402
except ImportError:  # development layout: <repo>/skills/_core/lib
    sys.path.insert(0, str(ROOT.parent / "_core"))
    from lib import hybrid  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import make_intro_fixture  # noqa: E402
import run_intro  # noqa: E402

TMP = ROOT / "_selftest_tmp"
SCRIPT = ROOT / "scripts" / "run_intro.py"
FAILS: list[str] = []
CONTRACT_FIELDS = list(make_intro_fixture.CASE["output_contract"].keys())


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        FAILS.append(f"{name}: {detail}")
        print(f"  FAIL {name} {detail}")


def run_script(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          cwd=str(ROOT), capture_output=True, text=True, timeout=300)


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 1. library parsing and citation resolution
# --------------------------------------------------------------------------- #
def test_library() -> None:
    print("library parsing and citation resolution")
    entries = run_intro.parse_bibtex(make_intro_fixture.BIB, "refs.bib")
    check("bib entry count", len(entries) == 6, str(len(entries)))
    check("bib first key", entries[0]["ref_key"] == "smith2019", str(entries[0]["ref_key"]))
    check("bib year", entries[0]["year"] == "2019", str(entries[0]["year"]))
    check("bib authors", entries[0]["authors"][:2] == ["Smith", "Lee"],
          str(entries[0]["authors"]))
    check("bib abstract", "sinter" in entries[0]["abstract"].casefold(),
          entries[0]["abstract"][:60])

    # One format per directory: a numeric index depends on library order, so mixing
    # formats is tested separately from citation resolution.
    lib_dir = TMP / "lib_bib"
    lib_dir.mkdir(parents=True, exist_ok=True)
    (lib_dir / "refs.bib").write_text(make_intro_fixture.BIB, encoding="utf-8")
    ris_dir = TMP / "lib_ris"
    ris_dir.mkdir(parents=True, exist_ok=True)
    ris = "TY  - JOUR\nTI  - A RIS test entry\nAU  - Doe, Jane\nPY  - 2021\nER  - \n"
    (ris_dir / "extra.ris").write_text(ris, encoding="utf-8")
    txt_dir = TMP / "lib_txt"
    txt_dir.mkdir(parents=True, exist_ok=True)
    plain = "[1] Smith J. A plain reference line. J. Tests 2019.\n"
    (txt_dir / "refs.txt").write_text(plain, encoding="utf-8")
    merged = run_intro.load_library(sorted(lib_dir.glob("*")) + sorted(ris_dir.glob("*"))
                                    + sorted(txt_dir.glob("*")))
    check("library merges three formats", merged["library_size"] == 8,
          str(merged["library_size"]))
    check("library records its sources", [source["format"] for source in merged["sources"]]
          == ["bib", "ris", "txt"], str(merged["sources"]))
    ris_library = run_intro.load_library(sorted(ris_dir.glob("*")))
    check("ris title parsed", ris_library["entries"][0]["title"] == "A RIS test entry",
          str(ris_library["entries"][0]))
    plain_library = run_intro.load_library(sorted(txt_dir.glob("*")))
    check("plain reference parsed", plain_library["entries"][0]["year"] == "2019",
          str(plain_library["entries"][0]))
    library = run_intro.load_library(sorted(lib_dir.glob("*")))
    check("numeric index maps order", library["numeric_index"]["1"] == "smith2019",
          str(library["numeric_index"].get("1")))

    check("numeric token", run_intro.resolve_token("[4]", library)["keys"] == ["kumar2017"],
          str(run_intro.resolve_token("[4]", library)))
    check("numeric range",
          run_intro.resolve_token("[3-5]", library)["keys"]
          == ["wang2018", "kumar2017", "garcia2021"],
          str(run_intro.resolve_token("[3-5]", library)))
    check("latex token",
          run_intro.resolve_token("\\cite{garcia2021}", library)["keys"] == ["garcia2021"],
          str(run_intro.resolve_token("\\cite{garcia2021}", library)))
    check("author-year token",
          run_intro.resolve_token("(Kumar, 2017)", library)["keys"] == ["kumar2017"],
          str(run_intro.resolve_token("(Kumar, 2017)", library)))
    check("et al author-year token",
          run_intro.resolve_token("(Smith et al., 2019)", library)["keys"] == ["smith2019"],
          str(run_intro.resolve_token("(Smith et al., 2019)", library)))
    missing = run_intro.resolve_token("[9]", library)
    check("missing token unresolved", missing["keys"] == [] and missing["unresolved"] == ["[9]"],
          str(missing))
    check("figure label is not a citation",
          run_intro.citation_tokens("Figure 2 and Table 1 and [1]") == ["[1]"],
          str(run_intro.citation_tokens("Figure 2 and Table 1 and [1]")))


# --------------------------------------------------------------------------- #
# 2. manuscript analysis
# --------------------------------------------------------------------------- #
def test_analysis() -> None:
    print("manuscript analysis")
    lib_dir = TMP / "lib_bib"
    library = run_intro.load_library(sorted(lib_dir.glob("*")))
    doc, warnings = run_intro.load_manuscript(TMP / "fixture" / "draft.md", None)
    check("manuscript text layer", bool(doc["text_layer"]), str(warnings))
    check("manuscript is text engine", str(doc["engine"]).startswith("text:"),
          str(doc["engine"]))
    analysis = run_intro.analyse_manuscript(doc, library)
    canonicals = [section["canonical"] for section in analysis["sections"]]
    for wanted in ("abstract", "introduction", "related_work", "method", "results",
                   "conclusion", "references"):
        check(f"section detected: {wanted}", wanted in canonicals, str(canonicals))
    citation = analysis["citation"]
    check("cited keys", citation["cited_keys"]
          == ["smith2019", "chen2020", "wang2018", "kumar2017", "garcia2021"],
          str(citation["cited_keys"]))
    check("uncited key", citation["uncited_keys"] == ["li2022"],
          str(citation["uncited_keys"]))
    check("missing citation token", citation["missing_tokens"] == ["[9]"],
          str(citation["missing_tokens"]))
    labels = [item["label"] for item in
              analysis["figure_tables"]["figures"] + analysis["figure_tables"]["tables"]]
    check("figure label found", "Figure 2" in labels, str(labels))
    check("table label found", "Table 1" in labels, str(labels))
    cues = [item["cue"] for item in analysis["contributions_candidates"]]
    check("contribution cue found", bool(cues), str(cues))
    check("style is mixed numeric+author-year",
          analysis["citation"]["style"] == "author-year+numeric",
          analysis["citation"]["style"])


# --------------------------------------------------------------------------- #
# 3. end-to-end: complete generated content
# --------------------------------------------------------------------------- #
def seg_with(segments: list[dict], needle: str) -> dict:
    for segment in segments:
        if needle in segment["text"]:
            return segment
    raise AssertionError(f"no segment contains {needle!r}")


def write_complete_generated(work: Path) -> None:
    manuscript = load_json(work / "manuscript.json")
    segments = manuscript["segments"]
    bg0 = seg_with(segments, "Supported nickel catalysts are widely used in methane reforming")
    bg1 = seg_with(segments, "Kinetic models that describe the rate over nickel")
    problem = seg_with(segments, "However, the sensitivity of selectivity")
    result = seg_with(segments, "Selectivity peaks at 750 K")
    conclusion = seg_with(segments, "Dispersing nickel on ceria-doped silica")
    generated = work / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    intro = {
        "background": [
            {"text": "Supported nickel catalysts are cheap and active, but their practical use "
                     "is limited by sintering of the nickel particles above 973 K.",
             "citations": ["[1]", "[2]"],
             "evidence_seg": bg0["seg_id"]},
            {"text": "Kinetic models of reforming over nickel assume a fixed active surface "
                     "area, so they cannot describe a selectivity that depends on particle size.",
             "citations": ["wang2018", "[4]"],
             "evidence_seg": bg1["seg_id"]},
        ],
        "problem_statement": {
            "text": "It remains unclear how selectivity depends on the nickel particle size at "
                    "low temperature, and a route that keeps selectivity at 750 K has not been "
                    "demonstrated.",
            "citations": ["[1]", "[5]"],
            "evidence_seg": problem["seg_id"]},
        "gap_analysis": [{
            "gap": "The cited works measure sintering above 973 K, map coke pathways, or assume "
                   "a constant active area; none reports a size-selectivity relation at 750 K.",
            "derived_from_refs": ["smith2019", "wang2018", "garcia2021"],
            "manifestation": {"seg_id": problem["seg_id"],
                              "quote": problem["text"][:70]}}],
        "contributions": [
            {"contribution": result["text"],
             "manuscript_evidence": {"seg_id": result["seg_id"],
                                     "quote": result["text"][:60],
                                     "figure_or_table": "Figure 2"}},
            {"contribution": conclusion["text"],
             "manuscript_evidence": {"seg_id": conclusion["seg_id"],
                                     "quote": conclusion["text"][:60]}},
            # positional claim: evidence is a figure/table label, not a quotation
            {"contribution": "Table 1 reports the conversion and selectivity versus "
                             "temperature.",
             "manuscript_evidence": {"figure_or_table": "Table 1"}},
        ],
    }
    related = {"groups": [
        {"dimension": "particle sintering and deactivation under reforming conditions",
         "why_this_dimension": "both works compare how the active nickel surface is lost",
         "works": [
             {"ref_key": "smith2019",
              "claim": "particle growth above 973 K halves the active surface within hours "
                       "on stream",
              "limitation": "it does not separate particle size from support effects",
              "compared_on": "operating temperature"},
             {"ref_key": "garcia2021",
              "claim": "operando scattering resolves particle growth during the first hours "
                       "on stream",
              "limitation": "it covers only the first hours and one catalyst",
              "compared_on": "time on stream"},
         ]},
        {"dimension": "coke formation and kinetic description",
         "why_this_dimension": "both works describe competing surface processes a kinetic "
                               "model must include",
         "works": [
             {"ref_key": "wang2018",
              "claim": "coke forms through methane decomposition and covers the active sites",
              "limitation": "dry reforming only, with no steam co-feed",
              "compared_on": "feed composition"},
             {"ref_key": "kumar2017",
              "claim": "a kinetic model reproduces the reforming rate over nickel",
              "limitation": "it assumes a constant active area",
              "compared_on": "model assumption"},
         ]},
    ]}
    hybrid.write_json(generated / "intro.json", intro)
    hybrid.write_json(generated / "related_work.json", related)
    hybrid.write_json(generated / "batch-01.json", {"records": [
        {"ref_key": key, "dimension": "particle sintering", "claim": "title-level claim",
         "limitation": "title only"} for key in
        ["smith2019", "chen2020", "wang2018", "kumar2017", "garcia2021", "li2022"]]})


def test_complete_run() -> None:
    print("end-to-end: complete generated content")
    fixture = TMP / "fixture"
    out = TMP / "out"
    work = TMP / "work"
    prepared = run_script("--prepare", "--input", str(fixture), "--out", str(out),
                          "--work", str(work))
    check("prepare exit 0", prepared.returncode == 0,
          (prepared.stderr or prepared.stdout)[-600:])
    for name in ("brief.json", "literature.json", "manuscript.json", "manifest.json",
                 "batches/batch-01.json"):
        check(f"prepare wrote {name}", (work / name).is_file(), str(work))
    brief = load_json(work / "brief.json")
    check("brief carries citation inventory",
          brief["manuscript"]["citation_inventory"]["cited_keys"][0] == "smith2019",
          str(brief["manuscript"]["citation_inventory"]["cited_keys"]))
    check("brief carries schemas", "intro" in brief["schemas"], str(brief["schemas"].keys()))
    placeholder = load_json(out / "result.json")
    check("placeholder result exists", placeholder["case_id"] == "writing08-demo-002",
          str(placeholder.get("case_id")))
    check("placeholder status partial", placeholder["meta"]["status"] == "partial",
          str(placeholder["meta"]["status"]))

    write_complete_generated(work)
    finalized = run_script("--finalize", "--input", str(fixture), "--out", str(out),
                           "--work", str(work))
    check("finalize exit 0", finalized.returncode == 0,
          (finalized.stderr or finalized.stdout)[-800:])
    result = load_json(out / "result.json")
    print("    status:", result["meta"]["status"],
          "| mapping:", result["meta"].get("contract_field_mapping"))
    check("case_id preserved", result["case_id"] == "writing08-demo-002",
          str(result.get("case_id")))
    missing_fields = [field for field in CONTRACT_FIELDS if field not in result]
    check("all contract fields present", missing_fields == [], str(missing_fields))
    keys = [key for key in result if key != "meta"]
    check("declared order preserved", keys == ["case_id"] + CONTRACT_FIELDS, str(keys))
    check("status success", result["meta"]["status"] == "success",
          str(result["meta"]["status"]) + " limits=" + str(result["meta"]["limits"])[:600])
    check("only unproduced field is the placeholder",
          result["meta"]["contract_fields_not_reported"] == ["target_journal_style"],
          str(result["meta"]["contract_fields_not_reported"]))
    check("placeholder keeps declared shape", result["target_journal_style"] == {},
          str(result["target_journal_style"]))

    mapping = result["meta"]["contract_field_mapping"]
    expected_mapping = {
        "opening_background": "background",
        "related_works_comparison": "related_work",
        "contribution_statements": "contributions",
        "gap_statement": "gap_analysis",
        "citation_audit": "citation_coverage",
        "claim_support_map": "claim_evidence",
        "revised_introduction": "revised_manuscript",
        "self_check": "quality",
        "evidence_anchors": "evidence",
    }
    for field, internal in expected_mapping.items():
        check(f"reflected {field} <- {internal}", mapping.get(field) == internal,
              f"{mapping.get(field)}")

    background = result["opening_background"]
    check("background items", len(background) == 2, str(len(background)))
    check("background carries resolved citations",
          background[0]["citations_resolved"] == ["smith2019", "chen2020"],
          str(background[0]["citations_resolved"]))
    check("background evidence located", background[0]["evidence"]["ok"] is True,
          str(background[0]["evidence"]))
    gaps = result["gap_statement"]
    check("gap derived from refs", gaps[0]["derived_from_refs"]
          == ["smith2019", "wang2018", "garcia2021"], str(gaps[0]["derived_from_refs"]))
    contributions = result["contribution_statements"]
    check("contributions have evidence", all(item["manuscript_evidence"]["ok"]
                                             for item in contributions),
          str([item["manuscript_evidence"] for item in contributions]))
    check("three contributions", len(contributions) == 3, str(len(contributions)))
    check("figure/table contribution keeps a structural anchor",
          contributions[2]["manuscript_evidence"]["how"] == "figure_label"
          and contributions[2]["manuscript_evidence"]["anchor"] is not None,
          str(contributions[2]["manuscript_evidence"]))
    related = result["related_works_comparison"]
    check("two related-work groups", len(related["groups"]) == 2,
          str(len(related["groups"])))
    check("related work not a baseline", related["baseline"] is False,
          str(related["baseline"]))
    check("citation audit library size", result["citation_audit"]["library_size"] == 6,
          str(result["citation_audit"]["library_size"]))
    check("citation audit missing token", result["citation_audit"]["missing_tokens"] == ["[9]"],
          str(result["citation_audit"]["missing_tokens"]))
    grounding = result["claim_support_map"]["grounding"]
    check("claims all located", grounding["location_rate"] == 1.0, str(grounding))
    check("strict grounding reported", grounding["strict_grounding_rate"] is not None,
          str(grounding["strict_grounding_rate"]))
    revised = result["revised_introduction"]
    check("revised markdown has both sections",
          "1 Introduction" in revised["markdown"] and "2 Related Work" in revised["markdown"],
          revised["markdown"][:200])
    check("old introduction replaced once",
          revised["markdown"].count("## 1 Introduction") == 1,
          str(revised["markdown"].count("## 1 Introduction")))
    for key in ("docx", "markdown", "html", "full_markdown"):
        path = revised["files"].get(key)
        check(f"deliverable {key} on disk", bool(path) and Path(path).is_file(), str(path))
    check("deliverable bytes", all(value > 500 for value in revised["bytes"].values()),
          str(revised["bytes"]))
    self_check = result["self_check"]
    check("self check degraded empty", self_check["degraded"] == [],
          str(self_check["degraded"]))
    check("self check citation coverage", self_check["citation"]["manuscript_coverage_rate"]
          == round(5 / 6, 4), str(self_check["citation"]["manuscript_coverage_rate"]))
    check("evidence.json written", (out / "evidence.json").is_file())
    check("run_report.json written", (out / "run_report.json").is_file())
    evidence = load_json(out / "evidence.json")
    check("evidence carries claim anchors", bool(evidence["claims"]), str(evidence.keys())[:200])
    check("evidence carries ref checks", len(evidence["ref_checks"]) >= 4,
          str(len(evidence["ref_checks"])))
    report = load_json(out / "run_report.json")
    check("report status matches", report["status"] == "success", str(report.get("status")))
    check("report records zero model requests", report["model_requests_used"] == 0,
          str(report["model_requests_used"]))


# --------------------------------------------------------------------------- #
# 4. end-to-end: degraded (no generated content)
# --------------------------------------------------------------------------- #
def test_degraded_run() -> None:
    print("end-to-end: degraded (no generated content)")
    fixture = TMP / "fixture"
    out = TMP / "out_degraded"
    work = TMP / "work_degraded"
    prepared = run_script("--prepare", "--input", str(fixture), "--out", str(out),
                          "--work", str(work))
    check("degraded prepare exit 0", prepared.returncode == 0,
          (prepared.stderr or prepared.stdout)[-600:])
    finalized = run_script("--finalize", "--input", str(fixture), "--out", str(out),
                           "--work", str(work))
    check("degraded finalize exit 0", finalized.returncode == 0,
          (finalized.stderr or finalized.stdout)[-800:])
    result = load_json(out / "result.json")
    check("degraded status partial", result["meta"]["status"] == "partial",
          str(result["meta"]["status"]))
    missing_fields = [field for field in CONTRACT_FIELDS if field not in result]
    check("degraded keeps every contract field", missing_fields == [], str(missing_fields))
    degraded = result["self_check"]["degraded"]
    check("degraded names the missing intro", "intro_missing" in degraded, str(degraded))
    check("degraded names the missing related work", "related_work_missing" in degraded,
          str(degraded))
    revised = result["revised_introduction"]
    check("degraded still ships a manuscript", len(revised["markdown"]) > 1000,
          str(len(revised["markdown"])))
    check("degraded manuscript keeps the new headings",
          "1 Introduction" in revised["markdown"], revised["markdown"][:120])
    check("degraded gap is explicitly a placeholder",
          any("Gap analysis was not produced" in item["gap"]
              for item in result["gap_statement"]),
          str(result["gap_statement"])[:400])
    check("degraded related work is flagged as baseline",
          result["related_works_comparison"]["baseline"] is True,
          str(result["related_works_comparison"]["source"]))
    check("degraded limits explain the baseline",
          any("baseline" in limit for limit in result["meta"]["limits"]),
          str(result["meta"]["limits"])[:400])
    check("degraded claims report location rate", "location_rate" in
          result["claim_support_map"]["grounding"],
          str(result["claim_support_map"]["grounding"].keys()))


# --------------------------------------------------------------------------- #
# 5. failure paths
# --------------------------------------------------------------------------- #
def test_failure_paths() -> None:
    print("failure paths")
    empty = TMP / "fixture_empty"
    empty.mkdir(parents=True, exist_ok=True)
    (empty / "case.json").write_text(json.dumps({
        "case_id": "writing08-empty",
        "output_contract": make_intro_fixture.CASE["output_contract"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    out = TMP / "out_empty"
    work = TMP / "work_empty"
    run = run_script("--all", "--input", str(empty), "--out", str(out), "--work", str(work))
    result_path = out / "result.json"
    check("failure run still writes result.json", result_path.is_file(), str(result_path))
    result = load_json(result_path)
    check("failure status", result["meta"]["status"] == "failed",
          str(result["meta"]["status"]))
    check("failure keeps contract shape",
          all(field in result for field in CONTRACT_FIELDS),
          str([field for field in CONTRACT_FIELDS if field not in result]))
    check("failure explains itself",
          any("manuscript" in limit for limit in result["meta"]["limits"]),
          str(result["meta"]["limits"])[:300])

    # broken generated JSON must degrade, not crash
    fixture = TMP / "fixture"
    out2 = TMP / "out_broken"
    work2 = TMP / "work_broken"
    run_script("--prepare", "--input", str(fixture), "--out", str(out2), "--work", str(work2))
    generated = work2 / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    (generated / "intro.json").write_text("{ this is not json", encoding="utf-8")
    broken = run_script("--finalize", "--input", str(fixture), "--out", str(out2),
                        "--work", str(work2))
    check("broken generated finalize exit 0", broken.returncode == 0,
          (broken.stderr or broken.stdout)[-600:])
    broken_result = load_json(out2 / "result.json")
    check("broken generated degrades", broken_result["meta"]["status"] == "partial",
          str(broken_result["meta"]["status"]))
    check("broken generated is named",
          any("intro.json" in limit for limit in broken_result["meta"]["limits"]),
          str(broken_result["meta"]["limits"])[:400])


def main() -> int:
    shutil.rmtree(TMP, ignore_errors=True)
    try:
        make_intro_fixture.build(TMP / "fixture")
        test_library()
        test_analysis()
        test_complete_run()
        test_degraded_run()
        test_failure_paths()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    if FAILS:
        print(f"\nFAILED {len(FAILS)} check(s):")
        for failure in FAILS:
            print(" -", failure)
        return 1
    print("\nall writing-08 intro/related-work checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
