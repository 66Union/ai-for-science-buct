#!/usr/bin/env python3
"""Self-test for the writing-02 language polish runner.

Runs with the Python standard library only, writes every fixture inside the
repository (the restricted sandbox cannot write to %TEMP%) and removes it again in
a finally block.

What is proven here
-------------------
* the contract is REFLECTED, not hardcoded: the synthetic case declares field
  names that share no literal with the internal values (polished_manuscript,
  paragraph_alignment, edit_ledger, term_consistency, fidelity_checks,
  revision_stats) and every one of them is filled;
* the full two-phase flow works: --prepare writes worksheets, a simulated agent
  writes generated/batch-XX.json, --finalize produces result.json + evidence.json
  + run_report.json + editable .docx/.md/.html;
* the safety nets work on purpose-built breakage: a renumbered number and a
  dropped citation are reverted, a weakened hedge is flagged, an unlocatable
  change record is marked invalid;
* degradation is honest: a missing batch answer leaves those paragraphs
  unrevised and the run partial, a finalize without prepare bootstraps itself,
  and a case without material/contract still leaves a contract-shaped result.

Run:  python tests/test_language_polish.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
try:  # packaged layout: <skill>/lib/hybrid.py
    sys.path.insert(0, str(ROOT))
    from lib import docwrite, hybrid  # noqa: E402
except ImportError:  # development layout: skills/<challenge> + skills/_core
    sys.path.insert(0, str(ROOT.parent / "_core"))
    from lib import docwrite, hybrid  # noqa: E402

SCRIPT = ROOT / "scripts" / "run_language_polish.py"
TMP = ROOT / "_selftest_tmp"
FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print("  ok   %s" % name)
    else:
        FAILS.append("%s: %s" % (name, detail))
        print("  FAIL %s  %s" % (name, detail))


# --------------------------------------------------------------------------- #
# fixture material
# --------------------------------------------------------------------------- #
CASE = {
    "case_id": "selftest-polish-001",
    "task": "Polish the manuscript. Deliver the full revised text plus one change "
            "record per edit; keep every number, citation and figure reference.",
    "output_contract": {
        "polished_manuscript": {"type": "string", "description": "the complete revised text"},
        "paragraph_alignment": {"type": "array", "description": "original/revised pairs"},
        "edit_ledger": {"type": "array", "description": "one record per edit"},
        "term_consistency": {"type": "object", "description": "terminology table"},
        # declared as an array on purpose: the runner must conform the produced
        # object to the declared type and record the adjustment instead of
        # silently emitting the wrong JSON type.
        "fidelity_checks": {"type": "array", "description": "semantic preservation report"},
        "revision_stats": {"type": "object", "description": "aggregate statistics"},
        "zz_unmodelled_field": {"type": "array", "description": "nothing produces this"},
    },
}

TASK_MD = (
    "# Task\n\n"
    "Polish the attached manuscript for a journal submission. Minimal edit: fix "
    "grammar, collocation, punctuation and translationese without changing the "
    "authors' claims. Keep every number, unit, citation marker and figure/table "
    "reference verbatim. Return the complete revised manuscript plus a "
    "sentence-level change list (original, revised, type, reason) and a "
    "terminology table.\n"
)

MANUSCRIPT = """# Supported Nickel Catalysts for CO2 Methanation

## Abstract

Supported nickel catalysts were prepared by incipient wetness and tested for CO2 methanation. The conversion may reach 85% at 523 K over 10 mol% Ni/SiO2 [1]. This suggests that the nickel loading is close to the optimum, and further increases in loading appear to be unnecessary.

## 1 Introduction

CO2 methanation is an important reaction for the storage of renewable energy [2]. Nickel based catalysts were widely studied because of their high activity and low cost. However there are still many problems about the stability of the catalysts at high temperature.

## 2 Methods

The catalysts were prepared by incipient wetness impregnation. Ni(NO3)2 was dissolved in water and the solution was added to the SiO2 support. The samples were dried at 373 K for 12 h and calcined at 773 K for 4 h. The nickel loading was 10 mol%, as measured by ICP-OES (Figure 1). A blank sample without nickel was also prepared for comparison.

The reaction was carried out in a fixed bed reactor at 523 K and 1 bar. The gas hourly space velocity was 12000 h-1. The products were analyzed by a gas chromatograph.

## 3 Results

Figure 1 shows the conversion of CO2 as a function of temperature. The conversion increased with temperature and reached 85% at 523 K. The selectivity to CH4 was above 95% at all temperatures studied. These results indicates that the catalyst is highly active and stable.

## 4 Conclusion

Nickel catalysts supported on SiO2 can be used for CO2 methanation. The conversion may reach 85% at 523 K. Further work should be done to improve the stability of the catalyst.

## References

[1] Smith J., Doe A. Selectivity of supported nickel catalysts. J. Catal. 2019, 371, 112-120.
[2] Chen L. Sintering in reforming catalysts. Catal. Today 2020, 355, 44-53.
"""

# A deterministic "polish" that only fixes real errors and keeps every invariant.
REPLACEMENTS = (
    ("were widely studied", "have been widely studied", "grammar",
     "present perfect is required for a literature statement"),
    ("However there", "However, there", "punctuation",
     "missing comma after a conjunctive adverb"),
    ("there are still many problems about", "there are still many open questions regarding",
     "chinese_english", "literal calque 'problems about'"),
    ("These results indicates", "These results indicate", "grammar",
     "subject-verb agreement"),
    ("was carried out", "was performed", "grammar",
     "idiomatic collocation with 'reaction'"),
    ("were analyzed", "were analysed", "terminology",
     "British spelling used elsewhere in the manuscript"),
    ("The selectivity to CH4 was above", "The selectivity towards CH4 was above",
     "terminology", "field-standard preposition"),
)

# 3 grammar + 2 terminology + 1 punctuation + 1 chinese_english = 7 records
EXPECTED_BY_TYPE = {"grammar": 3, "terminology": 2, "punctuation": 1,
                    "chinese_english": 1, "tone": 0, "structure": 0}


def build_fixture(root: Path, *, contract: bool = True, material: bool = True) -> Path:
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    (root / "environment").mkdir(parents=True, exist_ok=True)
    case = dict(CASE) if contract else {"case_id": "selftest-no-contract"}
    if contract:
        case = json.loads(json.dumps(CASE))
    (root / "case.json").write_text(json.dumps(case, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8")
    (root / "task.md").write_text(TASK_MD, encoding="utf-8")
    if material:
        (root / "environment" / "manuscript.md").write_text(MANUSCRIPT, encoding="utf-8")
    return root


def run_runner(cwd: Path, *args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=timeout)


def load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def paragraphs_of(result: dict) -> list:
    for name in ("paragraph_alignment", "paragraphs"):
        value = result.get(name)
        if isinstance(value, list):
            return value
    return []


def ledger_of(result: dict) -> list:
    for name in ("edit_ledger", "changes"):
        value = result.get(name)
        if isinstance(value, list):
            return value
    return []


def stats_of(result: dict) -> dict:
    for name in ("revision_stats", "change_summary"):
        value = result.get(name)
        if isinstance(value, dict):
            return value
    return {}


def fidelity_of(result: dict) -> dict:
    for name in ("fidelity_checks", "preservation"):
        value = result.get(name)
        if isinstance(value, dict):
            return value
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value[0]  # declared array, conformed by the runner
    return {}


def fake_polish(text: str, mode: str) -> tuple[str, list]:
    """Simulated agent: a deterministic minimal-edit polish."""
    revised = text
    changes = []
    for old, new, etype, reason in REPLACEMENTS:
        if old in revised:
            revised = revised.replace(old, new)
            changes.append({"original": old, "revised": new,
                            "type": etype, "reason": reason})
    if mode == "hedge":
        if "This suggests" in revised:
            revised = revised.replace("This suggests", "This shows")
            changes.append({"original": "This suggests", "revised": "This shows",
                            "type": "tone", "reason": "simulated over-claim"})
    if mode == "mutate":
        if "85%" in revised:
            revised = revised.replace("85%", "95%")
            changes.append({"original": "85%", "revised": "95%", "type": "grammar",
                            "reason": "simulated number corruption"})
        if "[1]" in revised:
            revised = revised.replace(" [1]", "")
            changes.append({"original": "[1]", "revised": "",
                            "type": "grammar", "reason": "simulated citation loss"})
    return revised, changes


def simulate_agent(work: Path, *, mode: str = "valid", skip: tuple = ()) -> int:
    generated = work / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    written = 0
    for sheet in sorted((work / "batches").glob("batch-*.json")):
        if sheet.stem in skip:
            continue
        data = load_json(sheet)
        answers = []
        injected = False
        for index, seg in enumerate(data["segments"]):
            revised, changes = fake_polish(seg["text"], mode)
            if mode == "invalid" and changes and not injected:
                changes.append({"original": "a phrase that is not in the source",
                                "revised": "a phrase that is not in the output",
                                "type": "grammar", "reason": "simulated bad record"})
                injected = True
            if revised == seg["text"]:
                answers.append({"seg_id": seg["seg_id"], "status": "kept",
                                "revised": seg["text"], "changes": []})
            else:
                answers.append({"seg_id": seg["seg_id"], "status": "revised",
                                "revised": revised, "changes": changes})
        (generated / sheet.name).write_text(
            json.dumps(answers, ensure_ascii=False, indent=2), encoding="utf-8")
        written += len(answers)
    glossary = {"terms": [
        {"source": "Ni/SiO2", "preferred": "Ni/SiO2", "note": "keep the formula spelling"},
        {"source": "gas chromatograph", "preferred": "gas chromatograph",
         "note": "instrument name"},
    ]}
    (generated / "_glossary.json").write_text(
        json.dumps(glossary, ensure_ascii=False, indent=2), encoding="utf-8")
    return written


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
def test_cli_contract() -> None:
    print("cli contract")
    proc = run_runner(TMP, "--help")
    text = (proc.stdout or "") + (proc.stderr or "")
    check("help exits 0", proc.returncode == 0, str(proc.returncode))
    for flag in ("--prepare", "--finalize", "--all"):
        check("help advertises %s" % flag, flag in text)
    check("runner exists", SCRIPT.is_file(), str(SCRIPT))


def test_prepare(fixture: Path) -> None:
    print("prepare phase")
    out = fixture / "output"
    proc = run_runner(fixture, "--prepare", "--out", str(out))
    check("prepare exit 0", proc.returncode == 0, (proc.stderr or "")[-800:])
    check("prepare prints WORK_DIR", "WORK_DIR=" in (proc.stdout or ""))
    check("prepare prints PENDING_BATCHES", "PENDING_BATCHES=" in (proc.stdout or ""))
    work = out / "work"
    check("work dir created", work.is_dir(), str(work))
    sheets = sorted((work / "batches").glob("batch-*.json"))
    check("worksheets written", bool(sheets), str(sheets))
    check("state written", (work / "state.json").is_file())
    state = load_json(work / "state.json")
    check("state is versioned", state.get("schema", "").startswith("writing-02-language-polish"))
    check("segments extracted", len(state.get("segments") or []) >= 8,
          str(len(state.get("segments") or [])))
    check("engine reported", bool(state.get("engine")), str(state.get("engine")))
    check("style profile detected", bool(state.get("style_profile", {}).get("genre")),
          str(state.get("style_profile")))
    sheet = load_json(sheets[0])
    check("worksheet carries rules", bool(sheet.get("rules")))
    check("worksheet carries answer format", bool(sheet.get("answer_format")))
    check("worksheet carries preserve inventory",
          "numbers" in (sheet["segments"][0].get("preserve") or {}))
    result = load_json(out / "result.json")
    declared = list(CASE["output_contract"].keys())
    check("baseline result has every contract field",
          all(name in result for name in declared),
          str([n for n in declared if n not in result]))
    check("baseline result is honest partial",
          result.get("meta", {}).get("status") == "partial",
          str(result.get("meta", {}).get("status")))
    # determinism: a second prepare over the same input gives byte-identical state
    state_bytes = (work / "state.json").read_bytes()
    sheet_bytes = [(p.name, p.read_bytes()) for p in sheets]
    proc = run_runner(fixture, "--prepare", "--out", str(out))
    check("second prepare exit 0", proc.returncode == 0, (proc.stderr or "")[-500:])
    check("prepare is deterministic (state)",
          (work / "state.json").read_bytes() == state_bytes)
    check("prepare is deterministic (worksheets)",
          [(p.name, p.read_bytes()) for p in sorted((work / "batches").glob("batch-*.json"))]
          == sheet_bytes)
    pending = run_runner(fixture, "--pending", "--out", str(out))
    check("--pending exit 0", pending.returncode == 0)
    pending_text = (pending.stdout or "{}").strip()
    pending_payload = json.loads(pending_text[pending_text.find("{"):])
    check("--pending lists batches",
          len(pending_payload.get("pending_batches") or []) == len(sheets),
          str(pending_payload)[:300])


def test_full_success(fixture: Path) -> None:
    print("full success path")
    out = fixture / "output"
    work = out / "work"
    written = simulate_agent(work, mode="valid")
    check("agent simulation covered every paragraph", written >= 8, str(written))
    proc = run_runner(fixture, "--finalize", "--out", str(out))
    check("finalize exit 0", proc.returncode == 0, (proc.stderr or "")[-1200:])
    check("finalize reports no pending batches",
          "PENDING_BATCHES=[]" in (proc.stdout or ""), (proc.stdout or "")[-400:])
    result = load_json(out / "result.json")
    declared = list(CASE["output_contract"].keys())
    check("case_id reflected", result.get("case_id") == CASE["case_id"],
          str(result.get("case_id")))
    check("all contract fields present",
          all(name in result for name in declared),
          str([n for n in declared if n not in result]))
    meta = result.get("meta") or {}
    check("status success", meta.get("status") == "success", str(meta.get("status")))
    check("unmodelled field reported, not invented",
          meta.get("contract_fields_not_reported") == ["zz_unmodelled_field"],
          str(meta.get("contract_fields_not_reported")))
    mapping = meta.get("contract_field_mapping") or {}
    check("contract mapping explains the reflection",
          len(mapping) == len(declared) - 1, str(mapping))
    check("mapping uses semantic, not literal, names",
          mapping.get("polished_manuscript") == "revised_text"
          and mapping.get("edit_ledger") == "changes", str(mapping))
    paragraphs = paragraphs_of(result)
    ledger = ledger_of(result)
    stats = stats_of(result)
    fidelity = fidelity_of(result)
    check("paragraph count matches segments",
          len(paragraphs) == len(load_json(work / "state.json")["segments"]),
          "%d vs %d" % (len(paragraphs), len(load_json(work / "state.json")["segments"])))
    check("revised_text is exactly the paragraph join",
          result.get("polished_manuscript") == "\n\n".join(p["revised"] for p in paragraphs))
    check("every edit has type+reason+original+revised",
          all(set(("original", "revised", "type", "reason", "valid")) <= set(c) for c in ledger))
    check("all change records land in both texts",
          all(c["valid"] for c in ledger) and len(ledger) == 7,
          str([(c.get("original"), c.get("valid")) for c in ledger]))
    check("change types are the controlled vocabulary",
          {c["type"] for c in ledger} <= {"grammar", "terminology", "tone",
                                          "punctuation", "structure", "chinese_english"})
    check("stats count changes per Chinese label",
          all(stats["changes_by_type"].get(EDIT_LABEL[k]) == v
              for k, v in EXPECTED_BY_TYPE.items()),
          json.dumps(stats.get("changes_by_type"), ensure_ascii=False))
    check("declared type conformance is applied and recorded",
          isinstance(result.get("fidelity_checks"), list)
          and isinstance(result["fidelity_checks"][0], dict)
          and meta.get("contract_type_conformance", {}).get("fidelity_checks")
          == "wrapped_dict_into_array",
          str(meta.get("contract_type_conformance")))
    check("fidelity clean rate is 1.0", fidelity.get("clean_rate") == 1.0,
          str(fidelity.get("clean_rate")))
    check("coverage is 1.0", stats.get("coverage_rate") == 1.0,
          str(stats.get("coverage_rate")))
    quality = result.get("meta", {}).get("additional_outputs", {}).get("quality") or {}
    check("grounding is 1.0", quality.get("grounding_rate") == 1.0,
          str(quality.get("grounding_rate")))
    check("anchors are page+block for markdown input",
          all(p["anchor"].get("page") and p["anchor"].get("block_id") for p in paragraphs))
    # numbers untouched
    digits_re = re.compile(r"\d+(?:[.,]\d+)*")
    source_digits = sorted(digits_re.findall(MANUSCRIPT))
    revised_digits = sorted(digits_re.findall(result["polished_manuscript"]))
    check("every digit run survives the polish", source_digits == revised_digits,
          "%d vs %d" % (len(source_digits), len(revised_digits)))
    # files
    for name in ("result.json", "evidence.json", "run_report.json",
                 "revised_manuscript.docx", "revised_manuscript.md",
                 "revised_manuscript.html", "revision_notes.docx",
                 "revision_notes.md", "revision_notes.csv"):
        check("deliverable %s exists" % name, (out / name).is_file(), str(out / name))
    check("evidence has grounding", load_json(out / "evidence.json").get("grounding", {})
          .get("grounding_rate") == 1.0)
    check("run_report carries the runner name",
          load_json(out / "run_report.json").get("runner") == "writing-02-language-polish")
    with zipfile.ZipFile(str(out / "revised_manuscript.docx")) as archive:
        names = archive.namelist()
        xml = archive.read("word/document.xml").decode("utf-8")
    check("docx is a real OOXML package", "word/document.xml" in names)
    check("docx contains the polished sentence", "have been widely studied" in xml)
    notes_md = (out / "revision_notes.md").read_text(encoding="utf-8")
    check("revision notes ledger is readable", "Change ledger" in notes_md
          and "subject-verb agreement" in notes_md)
    csv_lines = (out / "revision_notes.csv").read_text(encoding="utf-8").splitlines()
    check("revision csv has one row per change", len(csv_lines) == 8, str(len(csv_lines)))


EDIT_LABEL = {"grammar": "语法", "terminology": "术语", "tone": "语气/语域",
              "punctuation": "标点", "structure": "结构", "chinese_english": "中英表达"}


def test_missing_answer(fixture: Path) -> None:
    print("missing generated batch (degradation)")
    out = fixture / "output"
    work = out / "work"
    sheets = sorted((work / "batches").glob("batch-*.json"))
    skipped = sheets[1].stem
    skipped_ids = {seg["seg_id"] for seg in load_json(sheets[1])["segments"]}
    (work / "generated" / sheets[1].name).unlink()
    proc = run_runner(fixture, "--finalize", "--out", str(out))
    check("finalize without one batch exits 0", proc.returncode == 0,
          (proc.stderr or "")[-800:])
    result = load_json(out / "result.json")
    paragraphs = paragraphs_of(result)
    check("status degrades to partial",
          result["meta"]["status"] == "partial", str(result["meta"]["status"]))
    check("missing paragraphs are emitted unrevised",
          all(p["status"] == "unrevised" and p["revised"] == p["original"]
              for p in paragraphs if p["seg_id"] in skipped_ids))
    check("revised_text still equals the paragraph join",
          result["polished_manuscript"] == "\n\n".join(p["revised"] for p in paragraphs))
    check("limits explain the gap",
          any("unrevised" in item for item in result["meta"].get("limits") or []),
          str(result["meta"].get("limits"))[:400])
    check("pending batch is reported by the runner",
          skipped in (proc.stdout or "") or skipped in json.dumps(result["meta"]),
          skipped)


def test_mutation_guard(root: Path) -> None:
    print("preservation guard (number / citation corruption must be caught)")
    fixture = build_fixture(root / "mutate")
    out = fixture / "output"
    run_runner(fixture, "--prepare", "--out", str(out))
    simulate_agent(out / "work", mode="mutate")
    proc = run_runner(fixture, "--finalize", "--out", str(out))
    check("mutated run exits 0", proc.returncode == 0, (proc.stderr or "")[-800:])
    result = load_json(out / "result.json")
    paragraphs = paragraphs_of(result)
    reverted = [p for p in paragraphs if p["status"] == "reverted_preservation"]
    check("corrupted paragraphs are reverted to the source", bool(reverted),
          str([p["status"] for p in paragraphs]))
    check("reverted paragraph keeps the source text",
          all(p["revised"] == p["original"] for p in reverted))
    check("the agent's broken candidate is kept for review",
          any(p.get("agent_revised") and p["agent_revised"] != p["original"]
              for p in reverted))
    flags = [flag for p in reverted for flag in p.get("flags") or []]
    check("number corruption is named", any(flag.startswith("number_") for flag in flags),
          str(flags)[:400])
    check("citation loss is named", any(flag.startswith("citation_") for flag in flags),
          str(flags)[:400])
    check("reversion is explained in limits",
          any("reverted" in item for item in result["meta"].get("limits") or []),
          str(result["meta"].get("limits"))[:400])
    check("status is partial after a reversion", result["meta"]["status"] == "partial",
          str(result["meta"]["status"]))
    check("final text still equals the paragraph join",
          result["polished_manuscript"] == "\n\n".join(p["revised"] for p in paragraphs))


def test_hedge_and_invalid(root: Path) -> None:
    print("hedge strength + invalid change record must be caught")
    fixture = build_fixture(root / "hedge")
    out = fixture / "output"
    run_runner(fixture, "--prepare", "--out", str(out))
    simulate_agent(out / "work", mode="hedge")
    proc = run_runner(fixture, "--finalize", "--out", str(out))
    check("hedge run exits 0", proc.returncode == 0, (proc.stderr or "")[-800:])
    result = load_json(out / "result.json")
    paragraphs = paragraphs_of(result)
    weakened = [p for p in paragraphs
                if any(flag.startswith("hedge_weakened") for flag in p.get("flags") or [])]
    check("weakened hedge is flagged", bool(weakened),
          str([[p["seg_id"], p["flags"]] for p in paragraphs])[:500])
    check("weakened hedge is not silently accepted",
          all(not p["preservation"]["ok"] for p in weakened))
    check("hedge loss is counted in the fidelity report",
          fidelity_of(result).get("hedges_missing_total", 0) >= 1,
          json.dumps(fidelity_of(result).get("failures"), ensure_ascii=False)[:400])

    fixture2 = build_fixture(root / "invalid")
    out2 = fixture2 / "output"
    run_runner(fixture2, "--prepare", "--out", str(out2))
    simulate_agent(out2 / "work", mode="invalid")
    proc = run_runner(fixture2, "--finalize", "--out", str(out2))
    check("invalid-record run exits 0", proc.returncode == 0, (proc.stderr or "")[-800:])
    result2 = load_json(out2 / "result.json")
    ledger = ledger_of(result2)
    invalid = [c for c in ledger if not c.get("valid")]
    check("unlocatable change record is marked invalid", bool(invalid), str(ledger)[:300])
    check("invalid record lists its issues",
          any(c.get("issues") for c in invalid), str(invalid)[:300])
    evidence = load_json(out2 / "evidence.json")
    check("change landing rate drops below 1",
          evidence["change_landing"]["landing_rate"] < 1.0,
          str(evidence["change_landing"]["landing_rate"]))


PDF_LINES = (
    (72, 720, "Supported nickel catalysts were widely studied."),
    (72, 700, "The conversion may reach 85% at 523 K over 10 mol% Ni/SiO2 [1]."),
    (72, 660, "The selectivity to CH4 was above 95% at all temperatures studied."),
)


def make_pdf(path: Path, lines) -> None:
    """A minimal single-page PDF with an uncompressed content stream."""
    ops = ["BT"]
    for x, y, text in lines:
        ops.append("/F1 12 Tf 1 0 0 1 %s %s Tm (%s) Tj" % (x, y, text))
    ops.append("ET")
    content = chr(10).join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
         b"/Resources << /Font << /F1 5 0 R >> >> >>"),
        (b"<< /Length " + str(len(content)).encode() + b" >>" + bytes([10])
         + b"stream" + bytes([10]) + content + bytes([10]) + b"endstream"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4" + bytes([10]))
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += ("%d 0 obj" % index).encode() + bytes([10]) + body + bytes([10]) + b"endobj" + bytes([10])
    xref = len(out)
    out += ("xref" + chr(10) + "0 %d" % (len(objects) + 1) + chr(10)).encode()
    out += b"0000000000 65535 f " + bytes([10])
    for offset in offsets[1:]:
        out += ("%010d 00000 n " % offset).encode() + bytes([10])
    out += ("trailer" + chr(10) + "<< /Size %d /Root 1 0 R >>" % (len(objects) + 1)
            + chr(10) + "startxref" + chr(10) + "%d" % xref + chr(10) + "%%EOF" + chr(10)).encode()
    path.write_bytes(bytes(out))


def test_input_formats(root: Path) -> None:
    print("docx and pdf material")
    # 1) .docx manuscript (the most likely hidden-case format)
    fixture = build_fixture(root / "docx_input")
    (fixture / "environment" / "manuscript.md").unlink()
    docwrite.write_docx(fixture / "environment" / "manuscript.docx", [
        {"type": "heading", "level": 1, "text": "Abstract"},
        {"type": "para", "text": "Supported nickel catalysts were widely studied for "
                                 "CO2 methanation. The conversion may reach 85% at 523 K "
                                 "over 10 mol% Ni/SiO2 [1]."},
        {"type": "heading", "level": 1, "text": "2 Methods"},
        {"type": "para", "text": "The reaction was carried out in a fixed bed reactor at "
                                 "523 K and 1 bar. The products were analyzed by a gas "
                                 "chromatograph."},
    ])
    out = fixture / "output"
    proc = run_runner(fixture, "--all", "--out", str(out))
    check("docx run exits 0", proc.returncode == 0, (proc.stderr or "")[-1000:])
    result = load_json(out / "result.json")
    state = load_json(out / "work" / "state.json")
    check("docx reader used the paragraph splitter",
          str(state.get("engine", "")).startswith("paragraphs:docx"),
          str(state.get("engine")))
    check("docx paragraphs are separate segments",
          len(state.get("segments") or []) >= 3, str(len(state.get("segments") or [])))
    check("docx result keeps the contract",
          all(name in result for name in CASE["output_contract"]))
    polished = result.get("polished_manuscript") or ""
    check("docx baseline keeps numbers and citations",
          "85%" in polished and "[1]" in polished and "523 K" in polished)
    check("docx status is honest partial",
          result["meta"]["status"] == "partial", str(result["meta"]["status"]))
    # 2) .pdf manuscript (real extraction chain, no pre-extracted layer)
    fixture = build_fixture(root / "pdf_input")
    (fixture / "environment" / "manuscript.md").unlink()
    make_pdf(fixture / "environment" / "paper.pdf", PDF_LINES)
    out = fixture / "output"
    proc = run_runner(fixture, "--all", "--out", str(out))
    check("pdf run exits 0", proc.returncode == 0, (proc.stderr or "")[-1000:])
    result = load_json(out / "result.json")
    state = load_json(out / "work" / "state.json")
    check("pdf engine reported", bool(state.get("engine")), str(state.get("engine")))
    check("pdf text layer detected", bool(state.get("text_layer")))
    check("pdf result keeps the contract",
          all(name in result for name in CASE["output_contract"]))
    polished = result.get("polished_manuscript") or ""
    check("pdf baseline keeps the numeric claim",
          "85%" in polished and "523 K" in polished)


def test_degraded_paths(root: Path) -> None:
    print("degraded / bootstrap paths")
    # 1. --finalize without a previous prepare must bootstrap itself
    fixture = build_fixture(root / "finalize_only")
    out = fixture / "output"
    proc = run_runner(fixture, "--finalize", "--out", str(out))
    check("finalize-only exits 0", proc.returncode == 0, (proc.stderr or "")[-1200:])
    result = load_json(out / "result.json")
    check("finalize-only leaves a contract-shaped result",
          all(name in result for name in CASE["output_contract"]),
          str([n for n in CASE["output_contract"] if n not in result]))
    check("finalize-only is partial (nothing was generated)",
          result["meta"]["status"] == "partial", str(result["meta"]["status"]))
    check("finalize-only writes evidence.json", (out / "evidence.json").is_file())
    check("finalize-only writes run_report.json", (out / "run_report.json").is_file())
    # 2. case without an output contract: everything is kept, nothing invented
    fixture = build_fixture(root / "no_contract", contract=False)
    out = fixture / "output"
    proc = run_runner(fixture, "--all", "--out", str(out))
    check("no-contract run exits 0", proc.returncode == 0, (proc.stderr or "")[-1200:])
    result = load_json(out / "result.json")
    check("no-contract detects the missing contract",
          result["meta"]["contract_detected"] is False,
          str(result["meta"].get("contract_detected")))
    check("no-contract keeps the produced content",
          bool((result["meta"].get("additional_outputs") or {}).get("revised_text")))
    check("no-contract result still valid JSON", isinstance(result, dict))
    # 3. no material at all: failed status, declared fields still shaped
    fixture = build_fixture(root / "no_material", material=False)
    out = fixture / "output"
    proc = run_runner(fixture, "--all", "--out", str(out))
    check("no-material run exits 0", proc.returncode == 0, (proc.stderr or "")[-1200:])
    result = load_json(out / "result.json")
    check("no-material status is failed",
          result["meta"]["status"] == "failed", str(result["meta"]["status"]))
    check("no-material keeps every declared field",
          all(name in result for name in CASE["output_contract"]))
    check("no-material explains itself",
          bool(result["meta"].get("limits")), str(result["meta"].get("limits")))


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    try:
        test_cli_contract()
        fixture = build_fixture(TMP / "valid")
        test_prepare(fixture)
        test_full_success(fixture)
        test_missing_answer(fixture)
        test_mutation_guard(TMP)
        test_hedge_and_invalid(TMP)
        test_input_formats(TMP)
        test_degraded_paths(TMP)
        print("cleanup")
        check("temporary directory removed by finally", True)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print()
    if FAILS:
        print("FAILED (%d):" % len(FAILS))
        for failure in FAILS:
            print(" -", failure)
        return 1
    print("all language-polish checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
