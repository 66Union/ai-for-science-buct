#!/usr/bin/env python3
"""Self-test for the reference & citation management skill.

Standard library only, no network, no model call. Temporary files are written
inside the repository (tempfile under the sandbox cannot write to %TEMP%) and
removed in a finally block.

Run:  python tests/test_reference_management.py
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:  # packaged layout: <skill>/lib
    from lib import hybrid  # noqa: F401
except ImportError:  # dev layout: skills/<challenge> + skills/_core
    sys.path.insert(0, str(ROOT.parent / "_core"))
    from lib import hybrid  # noqa: F401

sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import make_fixture  # noqa: E402
import refmanage as M  # noqa: E402
import refparse as R  # noqa: E402
import run_reference_management as runner  # noqa: E402

TMP = ROOT / "_selftest_tmp"
FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print("  ok   " + name)
    else:
        FAILS.append(name + ": " + str(detail))
        print("  FAIL " + name + " " + str(detail))


def load(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def quiet(fn, argv) -> int:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = fn(argv)
    return code, buffer.getvalue()


def test_parsers() -> None:
    print("parsers")
    bib = R.parse_bibtex("""
@article{key1, author = {Kaufman, Brett and Lee, Chris},
  title = {The {\em Effect} of CO$_2$ on {Ni} Catalysts},
  journal = {J. Catal.}, year = {2020}, volume = {12}, number = {3},
  pages = {45--67}, doi = {https://doi.org/10.1016/J.X.2020.1}}
@string{ignored = "x"}
""")
    check("bibtex entry count", len(bib) == 1, str(len(bib)))
    check("bibtex type", bib[0]["type"] == "journal", str(bib[0].get("type")))
    check("bibtex latex cleaned", "Effect" in bib[0]["title"] and "{" not in bib[0]["title"],
          bib[0]["title"])
    check("bibtex doi normalised", R.norm_doi(bib[0]["doi"]) == "10.1016/j.x.2020.1",
          bib[0].get("doi"))
    ris = R.parse_ris("TY  - JOUR\nAU  - Smith, John\nAU  - Jones, Mary\nTI  - Title\n"
                      "JO  - Journal\nPY  - 2020\nVL  - 1\nIS  - 2\nSP  - 3\nEP  - 4\n"
                      "DO  - 10.1/x\nER  -\n")
    check("ris entry count", len(ris) == 1, str(len(ris)))
    check("ris authors joined", ris[0]["authors_raw"] == "Smith, John; Jones, Mary",
          ris[0].get("authors_raw"))
    check("ris pages", ris[0]["pages"] == "3-4", ris[0].get("pages"))
    plain = R.parse_plain_reference_list(
        "[1] 张三, 李四. 镍基催化剂研究[J]. 催化学报, 2020, 41(3): 45-67.\n"
        "[2] SMITH J, JONES M. Nickel catalysts[J]. Journal of Catalysis, 2019, 12(3): 45-67.\n"
        "[3] 王五. 催化剂表征[M]. 北京: 科学出版社, 2018.\n")
    check("plain entry count", len(plain) == 3, str(len(plain)))
    check("plain zh journal trimmed", plain[0]["journal"] == "催化学报", plain[0].get("journal"))
    check("plain zh volume/issue/pages",
          (plain[0]["volume"], plain[0]["issue"], plain[0]["pages"]) == ("41", "3", "45-67"),
          str(plain[0]))
    check("plain book publisher", plain[2]["publisher"] == "科学出版社", plain[2].get("publisher"))
    apa = R.parse_plain_reference_list(
        "Smith, J., & Jones, M. (2020). A title. Journal of Catalysis, 12(3), 45-67.\n"
        "Lee, C., & Wang, L. (2019). Another title. In Proc. Conf. (pp. 1-10).\n")
    check("apa plain count", len(apa) == 2, str(len(apa)))
    check("apa journal trimmed", apa[0]["journal"] == "Journal of Catalysis", apa[0].get("journal"))
    check("sniff bibtex", R.sniff_format("@article{k, title={x}}") == "bibtex")
    check("sniff ris", R.sniff_format("TY  - JOUR\nTI  - x\nER  -\n") == "ris")

    text = (r"Results were reported \cite{smith2020}. The value reached 85% [1,3]. "
            r"Smith et al. (2020) agreed. 张三（2021）也报道了. The area was 12 m²/g.")
    markers = R.find_citation_markers(text)
    styles = [marker["style"] for marker in markers]
    check("latex marker found", "latex" in styles, str(styles))
    check("numeric marker found", "numeric" in styles, str(styles))
    check("author-year narrative found", "author_year_narrative" in styles, str(styles))
    check("unit exponent is not a citation", len(markers) == 4, str([m["raw"] for m in markers]))
    numeric = [marker for marker in markers if marker["style"] == "numeric"][0]
    check("numeric range expanded", numeric["numbers"] == [1, 3], str(numeric.get("numbers")))
    check("author parsing GB/T", [(a["family"], a["given"])
                                  for a in R.parse_authors("SMITH J, LI Y")]
          == [("Smith", "J"), ("Li", "Y")], str(R.parse_authors("SMITH J, LI Y")))
    check("author parsing APA", [(a["family"], a["given"])
                                 for a in R.parse_authors("Smith, J., & Jones, M.")]
          == [("Smith", "J."), ("Jones", "M.")], str(R.parse_authors("Smith, J., & Jones, M.")))
    check("sentence paragraph split",
          [s["text"][:12] for s in R.split_sentences("# A\n\n## B\n\nSome text.")][:3]
          == ["# A", "## B", "Some text."],
          str([s["text"] for s in R.split_sentences("# A\n\n## B\n\nSome text.")]))


def test_dedupe_and_formatters() -> None:
    print("dedupe and formatters")
    raw = R.parse_bibtex("""
@article{a1, author={Smith, John}, title={Nickel catalysts for CO2 hydrogenation},
  journal={J. Catal.}, year={2020}, volume={12}, number={3}, pages={45--67},
  doi={10.1016/j.jcat.2020.01.001}}
@article{a2, author={Smith, John}, title={Nickel catalysts for CO2 hydrogenation},
  journal={J. Catal.}, year={2020}, volume={12}, number={3}, pages={45--67},
  doi={10.1016/J.JCAT.2020.01.001}}
@misc{a3, author={Smith, John}, title={Nickel catalysts for CO2 hydrogenation},
  year={2019}, journal={arXiv preprint arXiv:2001.00001}, eprint={2001.00001}}
@article{zh1, author={张三}, title={镍基催化剂研究}, journal={催化学报}, year={2020},
  volume={41}, number={3}, pages={45--67}}
@article{zh2, author={Zhang, San}, title={Nickel-based catalyst research},
  journal={Chinese Journal of Catalysis}, year={2020}, volume={41}, number={3},
  pages={45--67}}
""")
    used: set = set()
    entries = [R.normalize_entry(item, {"file": "x.bib", "format": "bibtex"}, index, used)
               for index, item in enumerate(raw)]
    report = M.find_duplicates(entries)
    check("doi duplicate group", any("same_doi" in group["criteria"] for group in report["groups"]),
          str(report["groups"]))
    check("preprint merged with published",
          any("a3" in group["members"] for group in report["groups"]), str(report["groups"]))
    check("bilingual pair needs review",
          any(item["criterion"] == "possible_bilingual_duplicate"
              for item in report["needs_review"]), str(report["needs_review"]))
    applied = M.apply_duplicates([dict(entry) for entry in entries], report)
    # three canonical records: the merged English article + two Chinese records that
    # are only *suspected* duplicates (needs_review must not auto-merge them)
    check("merged entry count", len(applied["entries"]) == 3, str(len(applied["entries"])))
    canonical = applied["entries"][0]
    check("merge keeps published record", "preprint" not in (canonical.get("journal") or "").casefold(),
          canonical.get("journal"))

    gbt = M.format_entry_gbt(canonical)
    check("gbt has type mark [J]", "[J]" in gbt, gbt)
    check("gbt has doi", "DOI: 10.1016/j.jcat.2020.01.001" in gbt, gbt)
    apa = M.format_entry_apa(canonical)
    check("apa has year parentheses", "(2020)." in apa, apa)
    check("apa has doi link", "https://doi.org/10.1016/j.jcat.2020.01.001" in apa, apa)
    check("gbt conference mark", "[C]//" in M.format_entry_gbt(
        {"type": "conference", "title": "Title", "authors": [{"family": "Lee", "given": "C"}],
         "booktitle": "Proc. Conf.", "year": "2019", "pages": "1-10", "language": "en"}),
        M.format_entry_gbt({"type": "conference", "title": "Title",
                            "authors": [{"family": "Lee", "given": "C"}],
                            "booktitle": "Proc. Conf.", "year": "2019", "pages": "1-10",
                            "language": "en"}))
    zh_book = {"type": "book", "title": "镍基催化剂表征",
               "authors": [{"family": "张三", "given": ""}], "year": "2018",
               "publisher": "科学出版社", "address": "北京", "language": "zh"}
    check("gbt chinese book list", "北京: 科学出版社" in M.format_entry_gbt(zh_book),
          M.format_entry_gbt(zh_book))
    check("apa chinese book period", "张三. (2018)." in M.format_entry_apa(zh_book),
          M.format_entry_apa(zh_book))


def test_renumbering() -> None:
    print("renumbering and marker rewriting")
    entries = [
        {"ref_key": "a", "aliases": ["a"], "type": "journal", "title": "Alpha",
         "authors": [{"family": "A", "given": "A"}], "year": "2020", "language": "en"},
        {"ref_key": "b", "aliases": ["b"], "type": "journal", "title": "Beta",
         "authors": [{"family": "B", "given": "B"}], "year": "2021", "language": "en"},
        {"ref_key": "c", "aliases": ["c"], "type": "journal", "title": "Gamma",
         "authors": [{"family": "C", "given": "C"}], "year": "2022", "language": "en"},
    ]
    text = "First claim [2]. Second claim [1,3]. Third claim \\cite{b}."
    markers = R.find_citation_markers(text)
    number_map = {1: entries[0], 2: entries[1], 3: entries[2]}
    R.resolve_markers(markers, entries, number_map)
    plan = M.plan_renumbering(markers, entries, M.STYLE_GBT)
    check("numeric first appearance order", plan["entry_order"] == ["b", "a", "c"],
          str(plan["entry_order"]))
    rewritten = M.rewrite_citations(text, markers, plan)
    check("numeric renumbering", rewritten["text"] == "First claim [1]. Second claim [2,3]. Third claim [1].",
          rewritten["text"])
    check("numeric change records", len(rewritten["changes"]) == 3, str(len(rewritten["changes"])))
    plan_apa = M.plan_renumbering(markers, entries, M.STYLE_APA)
    check("apa alphabetical order", plan_apa["entry_order"] == ["a", "b", "c"],
          str(plan_apa["entry_order"]))
    rewritten_apa = M.rewrite_citations(text, markers, plan_apa)
    check("author-year conversion",
          rewritten_apa["text"] == "First claim (B, 2021). Second claim (A, 2020; C, 2022). "
                                   "Third claim (B, 2021).",
          rewritten_apa["text"])
    check("collapse numbers", M.collapse_numbers([1, 2, 3, 5]) == "1-3,5",
          M.collapse_numbers([1, 2, 3, 5]))
    check("collapse pair stays comma", M.collapse_numbers([1, 2]) == "1,2",
          M.collapse_numbers([1, 2]))


def test_inline_reference_list() -> None:
    print("inline numbered reference list (PDF text layer flattening)")
    section = ("[1] Smith J. et al. J. Catal. 2019, 371, 112-120. "
               "[2] Chen L. et al. ACS Catal. 2020, 10, 4455-4463. "
               "[3] Wang H. et al. Catal. Today 2018, 316, 88-95. "
               "[4] Garcia M. et al. Appl. Catal. A 2021, 610, 117940.")
    entries = R.parse_plain_reference_list(section)
    check("inline list yields four entries", len(entries) == 4, str(len(entries)))
    check("inline list keeps the old numbers",
          [entry.get("old_number") for entry in entries] == [1, 2, 3, 4],
          str([entry.get("old_number") for entry in entries]))
    check("inline list parses years",
          [str(entry.get("year")) for entry in entries] == ["2019", "2020", "2018", "2021"],
          str([entry.get("year") for entry in entries]))
    normalized = [R.normalize_entry(entry, {"file": "paper.pdf", "format": "plain"}, index, set())
                  for index, entry in enumerate(entries)]
    body = "Earlier work reported lower selectivity [3]. Sintering explains it [4]."
    markers = R.find_citation_markers(body)
    number_map = {int(entry["old_number"]): entry for entry in normalized
                  if entry.get("old_number")}
    R.resolve_markers(markers, normalized, number_map)
    check("all inline markers resolve",
          all(marker.get("targets") for marker in markers),
          str([marker.get("raw") for marker in markers if not marker.get("targets")]))
    plan = M.plan_renumbering(markers, normalized, M.STYLE_GBT)
    check("inline list produces a non-empty cited order", len(plan["entry_order"]) == 2,
          str(plan["entry_order"]))


def _prepare(fixtures: dict, name: str, tmp: Path, extra=None):
    work = tmp / (name + "-work")
    out = tmp / (name + "-out")
    argv = ["--prepare", "--input", str(fixtures[name]), "--work", str(work), "--out", str(out)]
    if extra:
        argv.extend(extra)
    code, log = quiet(runner.main, argv)
    return code, log, work, out


def test_gbt_end_to_end() -> None:
    print("end-to-end: GB/T numeric manuscript")
    tmp = TMP / "e2e_gbt"
    fixtures = make_fixture.build_fixtures(tmp / "fixtures")
    code, log, work, out = _prepare(fixtures, "gbt", tmp)
    check("prepare exit code", code == 0, log[-800:])
    for name in ("citation_index.json", "bib_entries.json", "style.json", "citation_state.json"):
        check("prepare wrote " + name, (work / name).is_file())
    index = load(work / "citation_index.json")
    check("citation markers indexed", index["marker_count"] == 13, str(index["marker_count"]))
    bibliography = load(work / "bib_entries.json")
    check("entries deduplicated", bibliography["entry_count"] == 8, str(bibliography["entry_count"]))
    check("duplicate groups detected", len(bibliography["duplicate_report"]["groups"]) == 3,
          str(len(bibliography["duplicate_report"]["groups"])))
    check("number map recovered from the numbered list",
          [item["old_number"] for item in bibliography["number_map"]] == [1, 2, 3, 4, 5, 6],
          str(bibliography["number_map"]))
    batches = sorted((work / "batches").glob("batch-*.json"))
    check("worksheets written", len(batches) >= 1, str(len(batches)))

    baseline = load(out / "result.json")
    check("baseline case_id", baseline.get("case_id") == "fixture-citation-gbt-001",
          str(baseline.get("case_id")))
    check("baseline contract field filled", isinstance(baseline.get("manuscript_final"), str)
          and len(baseline["manuscript_final"]) > 100, str(type(baseline.get("manuscript_final"))))
    check("baseline status partial", baseline["meta"]["status"] == "partial",
          baseline["meta"]["status"])
    check("baseline placeholders recorded",
          "unmodelled_field_xyz" in baseline["meta"]["contract_fields_not_reported"],
          str(baseline["meta"].get("contract_fields_not_reported")))
    check("baseline mapping reported",
          baseline["meta"]["contract_field_mapping"].get("manuscript_final") == "revised_text",
          str(baseline["meta"].get("contract_field_mapping")))
    check("no BOM in result.json",
          (out / "result.json").read_text(encoding="utf-8")[0] == "{", "BOM found")

    # the agent answers every worksheet segment
    state = load(work / "citation_state.json")
    judgement = []
    for record in state["claim_records"]:
        matched = [{"ref_key": item["ref_key"],
                    "relation": "supported" if item["relation"] in {"supported", "partial"} else "unverified",
                    "confidence": 0.8,
                    "reason": "self-test agent judgement"}
                   for item in record["matched_refs"]]
        item = {"seg_id": record["seg_id"], "claim_terms": record["claim_terms"],
                "matched_refs": matched, "confidence": 0.8, "note": "self-test"}
        if record["insertion"]["needed"]:
            item["insertion"] = {"needed": True,
                                 "suggested_refs": record["insertion"]["suggested_refs"][:2],
                                 "note": "uncited claim"}
        judgement.append(item)
    (work / "generated").mkdir(parents=True, exist_ok=True)
    (work / "generated" / "claim_support.json").write_text(
        json.dumps(judgement, ensure_ascii=False, indent=2), encoding="utf-8")
    code, log = quiet(runner.main, ["--finalize", "--work", str(work), "--out", str(out)])
    check("finalize exit code", code == 0, log[-800:])
    result = load(out / "result.json")
    check("final status success", result["meta"]["status"] == "success", result["meta"]["status"])
    check("judgement coverage full",
          load(out / "evidence.json")["judgement"]["coverage"] == 1.0,
          str(load(out / "evidence.json")["judgement"]["coverage"]))
    reference_list = result.get("works_cited") or []
    check("reference list length", len(reference_list) == 6, str(len(reference_list)))
    check("reference list renumbered from first appearance",
          reference_list[0]["ref_key"] == "张三2021" and reference_list[1]["ref_key"] == "chen2019a",
          str([item["ref_key"] for item in reference_list]))
    check("gbt formatted entry", "[J]" in reference_list[0]["formatted"],
          reference_list[0]["formatted"])
    manuscript = result.get("manuscript_final") or ""
    check("body renumbered (old [2] -> new [1])", "转化率可达85%[1]" in manuscript, manuscript[:300])
    check("body renumbered (old [1] -> new [5])", "积碳问题仍然存在[5]" in manuscript, manuscript[:600])
    trace = result.get("intext_marker_trace") or []
    check("citation trace length", len(trace) == 13, str(len(trace)))
    check("trace keeps the old marker", any(item["marker"] == "[2]" and item["new_marker"] == "[1]"
                                            for item in trace), str(trace[:2]))
    duplicates = result.get("merged_bibliography_groups") or []
    check("duplicate groups reported as a list", isinstance(duplicates, list) and len(duplicates) == 3,
          str(duplicates)[:300])
    check("merged group carries its decision",
          bool(duplicates) and duplicates[0].get("merged_into") and duplicates[0].get("criteria"),
          str(duplicates[0] if duplicates else {}))
    duplicate_report = (result["meta"].get("additional_outputs") or {}).get("duplicates") or {}
    check("review items reported", len(duplicate_report.get("needs_review") or []) == 1,
          str(duplicate_report.get("needs_review")))
    check("claim support reported", len(result.get("sentence_level_support") or []) >= 10,
          str(len(result.get("sentence_level_support") or [])))
    check("agent judgement marked",
          any(item.get("source") == "agent" for item in result["sentence_level_support"]
              if isinstance(item, dict)), "no agent source")
    insertion = [item for item in result["sentence_level_support"]
                 if isinstance(item, dict) and item.get("insertion", {}).get("needed")]
    check("insertion suggestion for an uncited claim", len(insertion) >= 1, str(len(insertion)))
    evidence = load(out / "evidence.json")
    check("claim grounding full", evidence["grounding"]["grounding_rate"] == 1.0,
          str(evidence["grounding"]["grounding_rate"]))
    check("marker grounding full", evidence["marker_grounding"]["grounding_rate"] == 1.0,
          str(evidence["marker_grounding"]))
    check("entry grounding full", evidence["entry_grounding"]["grounding_rate"] == 1.0,
          str(evidence["entry_grounding"]))
    for name in ("evidence.json", "run_report.json", "revised_manuscript.md",
                 "revised_manuscript.docx", "revised_manuscript.html", "references.md",
                 "references.bib", "references.ris", "change_log.md"):
        check("artifact " + name, (out / name).is_file())
    check("docx is a real zip", (out / "revised_manuscript.docx").read_bytes()[:2] == b"PK",
          str((out / "revised_manuscript.docx").read_bytes()[:4]))
    report = load(out / "run_report.json")
    check("run report counts", report["counts"]["duplicates_merged"] == 3,
          str(report["counts"].get("duplicates_merged")))
    check("run report marks no model call", report["model_requests_used"] == 0,
          str(report.get("model_requests_used")))

    # regression: remove the judgement file -> honest partial fallback
    shutil.rmtree(work / "generated")
    code, log = quiet(runner.main, ["--finalize", "--work", str(work), "--out", str(out)])
    fallback = load(out / "result.json")
    check("fallback exit code", code == 0, log[-500:])
    check("fallback status partial", fallback["meta"]["status"] == "partial",
          fallback["meta"]["status"])
    check("fallback explains the missing judgement",
          any("claim_support" in limit or "baseline" in limit
              for limit in fallback["meta"]["limits"]), str(fallback["meta"]["limits"]))
    check("fallback keeps contract shape",
          isinstance(fallback.get("manuscript_final"), str) and isinstance(
              fallback.get("works_cited"), list), str(list(fallback.keys())))
    check("fallback claim records still present",
          len(fallback.get("sentence_level_support") or []) >= 10,
          str(len(fallback.get("sentence_level_support") or [])))
    check("fallback marked as baseline",
          all(item.get("source") == "baseline" for item in fallback["sentence_level_support"]
              if isinstance(item, dict)), "non-baseline source present")

    # determinism: a second prepare over the same material must agree
    work2 = tmp / "gbt-work-2"
    out2 = tmp / "gbt-out-2"
    quiet(runner.main, ["--prepare", "--input", str(fixtures["gbt"]), "--work", str(work2),
                        "--out", str(out2)])
    check("deterministic citation index",
          load(work2 / "citation_index.json") == load(work / "citation_index.json"))
    check("deterministic bibliography snapshot",
          load(work2 / "bib_entries.json") == load(work / "bib_entries.json"))

    # style override file written by an agent
    (work / "generated").mkdir(parents=True, exist_ok=True)
    (work / "generated" / "style.json").write_text(json.dumps({"style": "apa7"}), encoding="utf-8")
    code, log = quiet(runner.main, ["--finalize", "--work", str(work), "--out", str(out)])
    overridden = load(out / "result.json")
    style_value = overridden.get("target_style")
    check("style override applied",
          isinstance(style_value, dict) and style_value.get("style") == "apa7", str(style_value))
    check("override mention in limits",
          any("style override" in str(limit) or "style" in str(limit)
              for limit in overridden["meta"]["limits"]), str(overridden["meta"]["limits"]))


def test_apa_end_to_end() -> None:
    print("end-to-end: APA author-year manuscript")
    tmp = TMP / "e2e_apa"
    fixtures = make_fixture.build_fixtures(tmp / "fixtures")
    work = tmp / "apa-work"
    out = tmp / "apa-out"
    code, log = quiet(runner.main, ["--all", "--input", str(fixtures["apa"]),
                                    "--work", str(work), "--out", str(out)])
    check("all-mode exit code", code == 0, log[-800:])
    result = load(out / "result.json")
    check("apa case_id", result.get("case_id") == "fixture-citation-apa-002",
          str(result.get("case_id")))
    check("apa contract array field is a list", isinstance(result.get("duplicate_groups"), list),
          str(type(result.get("duplicate_groups"))))
    check("apa contract fields all reported", result["meta"]["contract_fields_not_reported"] == [],
          str(result["meta"].get("contract_fields_not_reported")))
    references = result.get("reference_entries") or []
    check("apa references reported", len(references) == 4, str(len(references)))
    surnames = [item["formatted"].split(",")[0] for item in references]
    check("apa reference list sorted alphabetically", surnames == sorted(surnames), str(surnames))
    check("apa formatted with year", all("(20" in item["formatted"] for item in references),
          str([item["formatted"][:40] for item in references]))
    manuscript = result.get("revised_manuscript") or ""
    check("apa in-text marker converted", "(Smith & Jones, 2020)" in manuscript, manuscript[:400])
    check("apa en dash page range", "45–67" in manuscript or "45-67" in manuscript,
          manuscript[:200])
    check("apa status partial without judgement", result["meta"]["status"] == "partial",
          result["meta"]["status"])
    check("apa style evidence",
          "APA" in str(result.get("limitations")) + str(result["meta"].get("additional_outputs")),
          "no style clue")
    evidence = load(out / "evidence.json")
    check("apa entry grounding full", evidence["entry_grounding"]["grounding_rate"] == 1.0,
          str(evidence["entry_grounding"]))
    check("apa placeholder shape for unused field",
          result.get("limitations") is not None, "limitations missing")


def test_edge_cases() -> None:
    print("edge cases")
    tmp = TMP / "edge"
    tmp.mkdir(parents=True, exist_ok=True)

    # 1) case.json but no material files at all
    empty = tmp / "empty"
    empty.mkdir(parents=True, exist_ok=True)
    (empty / "case.json").write_text(json.dumps({
        "case_id": "edge-empty-1",
        "output_contract": {"final_text": {"type": "string"},
                            "reference_list": {"type": "array"}},
    }, ensure_ascii=False), encoding="utf-8")
    work = tmp / "empty-work"
    out = tmp / "empty-out"
    code, log = quiet(runner.main, ["--prepare", "--input", str(empty),
                                    "--work", str(work), "--out", str(out)])
    result = load(out / "result.json")
    check("empty-input exit code", code == 0, log[-500:])
    check("empty-input contract reflected", result["meta"]["contract_field_count"] == 2,
          str(result["meta"].get("contract_field_count")))
    check("empty-input declares partial", result["meta"]["status"] == "partial",
          result["meta"]["status"])
    check("empty-input placeholder shapes",
          result.get("final_text") == "" and result.get("reference_list") == [],
          str((result.get("final_text"), result.get("reference_list"))))
    check("empty-input limits explain", any("manuscript" in str(limit)
                                            for limit in result["meta"]["limits"]),
          str(result["meta"]["limits"]))

    # 2) case.json without any output_contract
    no_contract = tmp / "nocontract"
    no_contract.mkdir(parents=True, exist_ok=True)
    (no_contract / "case.json").write_text(json.dumps({"case_id": "edge-nocontract-2"}),
                                           encoding="utf-8")
    (no_contract / "manuscript.md").write_text(
        "A short note with one claim [1].\n\n## References\n\n[1] A. B. Title[J]. J, 2020, 1(2): 3-4.\n",
        encoding="utf-8")
    work2 = tmp / "nocontract-work"
    out2 = tmp / "nocontract-out"
    code, log = quiet(runner.main, ["--prepare", "--input", str(no_contract),
                                    "--work", str(work2), "--out", str(out2)])
    result2 = load(out2 / "result.json")
    check("no-contract exit code", code == 0, log[-500:])
    check("no-contract detected false", result2["meta"]["contract_detected"] is False,
          str(result2["meta"].get("contract_detected")))
    check("no-contract keeps produced content",
          "additional_outputs" in result2["meta"]
          and len(result2["meta"]["additional_outputs"]) > 0,
          str(list((result2["meta"].get("additional_outputs") or {}).keys())[:6]))

    # 3) BOM tolerance on case.json (Windows shells write UTF-8 with BOM)
    bom = tmp / "bom"
    bom.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"case_id": "edge-bom-3",
                          "output_contract": {"text": {"type": "string"}}}, ensure_ascii=False)
    (bom / "case.json").write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))
    work3 = tmp / "bom-work"
    out3 = tmp / "bom-out"
    code, log = quiet(runner.main, ["--prepare", "--input", str(bom),
                                    "--work", str(work3), "--out", str(out3)])
    result3 = load(out3 / "result.json")
    check("bom exit code", code == 0, log[-500:])
    check("bom case_id recovered", result3.get("case_id") == "edge-bom-3",
          str(result3.get("case_id")))
    check("bom contract detected", result3["meta"]["contract_detected"] is True,
          str(result3["meta"].get("contract_detected")))

    # 4) --no-baseline writes the early placeholder but no full baseline
    code, log = quiet(runner.main, ["--prepare", "--input", str(fixtures_gbt()),
                                    "--work", str(tmp / "nb-work"), "--out", str(tmp / "nb-out"),
                                    "--no-baseline"])
    placeholder = load(tmp / "nb-out" / "result.json")
    check("no-baseline exit code", code == 0, log[-400:])
    check("no-baseline placeholder written", placeholder["meta"]["status"] == "partial",
          placeholder["meta"]["status"])
    check("no-baseline states the run did not finish",
          any("placeholder" in str(limit) or "started" in str(limit)
              for limit in placeholder["meta"]["limits"]),
          str(placeholder["meta"]["limits"]))

    # 5) failure writer still produces a machine-readable failed result
    fail_out = tmp / "fail-out"
    runner.write_failure(fail_out, types.SimpleNamespace(input=str(empty)),
                         RuntimeError("synthetic failure"))
    failed = load(fail_out / "result.json")
    check("failure status", failed["meta"]["status"] == "failed", failed["meta"]["status"])
    check("failure keeps contract keys",
          "final_text" in failed and "reference_list" in failed, str(list(failed.keys())))
    check("failure records traceback", "traceback_tail" in failed["meta"],
          str(list(failed["meta"].keys())))
    check("failure writes evidence/report",
          (fail_out / "evidence.json").is_file() and (fail_out / "run_report.json").is_file())


_FIXTURE_CACHE: dict = {}


def fixtures_gbt() -> Path:
    if "gbt" not in _FIXTURE_CACHE:
        _FIXTURE_CACHE.update(make_fixture.build_fixtures(TMP / "shared_fixtures"))
    return _FIXTURE_CACHE["gbt"]


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    try:
        test_parsers()
        test_dedupe_and_formatters()
        test_renumbering()
        test_inline_reference_list()
        test_gbt_end_to_end()
        test_apa_end_to_end()
        test_edge_cases()
    except BaseException:
        traceback.print_exc()
        FAILS.append("unhandled exception")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print()
    if FAILS:
        print("FAILED (%d):" % len(FAILS))
        for failure in FAILS:
            print(" -", failure)
        return 1
    print("all reference-management checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
