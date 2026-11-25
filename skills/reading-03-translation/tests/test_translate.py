#!/usr/bin/env python3
"""Self-test for the reading-03 translation adapter.

Builds a synthetic EN paper (PDF written with the standard library only), runs the
real CLI through prepare -> agent answers -> check -> finalize, and asserts the
things the topic is scored on:

  * paragraph-level alignment is exact (src identical, page/block_id/bbox carried);
  * a glossary term rendered differently in one paragraph is caught;
  * a changed number is caught;
  * missing generated batches degrade to status="partial" with tgt="" and
    status="untranslated" instead of crashing;
  * the case's own output_contract field names are reflected (they are
    deliberately different from the internal value names);
  * two prepare runs are byte-identical (deterministic).

Standard library only. Temporary files live inside the repository because a
restricted sandbox cannot write to %TEMP%.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
try:  # packaged layout: <skill root>/lib exists
    sys.path.insert(0, str(ROOT))
    from lib import hybrid  # noqa: E402
except ImportError:  # development layout: skills/_core sits next to the challenge dir
    sys.path.insert(0, str(ROOT.parent / "_core"))
    from lib import hybrid  # noqa: E402

TMP = ROOT / "_selftest_tmp"
SCRIPT = ROOT / "scripts" / "run_translate.py"
FAILS: list[str] = []

CASE = {
    "case_id": "tr-demo-003",
    "target_language": "zh",
    "task": "Translate the supplied paper into Chinese with a locked glossary and paragraph-level alignment.",
    "output_contract": {
        "zh_en_paragraph_pairs": {"description": "paragraph-level bilingual pairs", "type": "array"},
        "terminology_bank": {"description": "locked glossary with translations", "type": "array"},
        "translated_body": {"description": "full Chinese translation", "type": "string"},
        "paragraph_completeness": {"description": "coverage of source paragraphs", "type": "object"},
        "term_loyalty_report": {"description": "terminology consistency report", "type": "object"},
        "omission_list": {"description": "paragraphs that were not translated", "type": "array"},
        "evidence_anchors": {"description": "page/block anchors for every pair", "type": "object"},
        "grade_metrics": {"description": "preservation and alignment metrics", "type": "object"},
        "source_passages": {"description": "segment inventory of the source", "type": "array"},
        "原文全文": {"description": "the full source text", "type": "string"},
        "qqq_unmodelled_field": {"description": "intentionally undeclared artifact", "type": "string"},
    },
    "material": {"file": "paper.pdf", "note": "synthetic fixture, not a real publication"},
}

TASK_MD = """# Task

Translate paper.pdf (English) into Chinese.

Required: paragraph-level bilingual alignment, a locked terminology table applied in
every paragraph, a machine-checked preservation report (numbers, units, citations,
formulas, hedges, negations), and explicit markings for anything that could not be
translated. Evidence must point back to page and block.
"""

# Synthetic paper: (x, y, font size, text). Gaps > 1.6 x size start a new block,
# so each paragraph below becomes exactly one addressable segment.
PAGES = [
    [
        (72, 760, 16, "Paragraph Alignment and Terminology in the Translation of Supported Nickel Catalysts"),
        (72, 736, 10, "A. Author, B. Author - Institute of Catalysis"),
        (72, 700, 12, "Abstract"),
        (72, 682, 10, "We report that supported nickel catalysts may reach 92% selectivity for methane reforming at 750 K and 1 atm [1]."),
        (72, 668, 10, "The conversion was not stable above 800 K, although the selectivity remained high."),
        (72, 620, 12, "1 Introduction"),
        (72, 602, 10, "Supported nickel catalysts are widely used in methane reforming reactions [2]."),
        (72, 588, 10, "Earlier work suggested that the selectivity decreases after 24 h on stream, and the XRD pattern shows a mean particle size of 8.5 nm [3]."),
    ],
    [
        (72, 760, 12, "2 Methods"),
        (72, 742, 10, "The catalyst precursor was dissolved in ethanol and dried at 373 K for 12 h."),
        (72, 728, 10, "Nickel loading was 5 wt% as determined by ICP-OES, and the rate constant follows k = A exp(-Ea/RT) (eq. 1)."),
        (72, 700, 12, "3 Results"),
        (72, 682, 10, "The conversion increased from 71% to 95% between 700 K and 800 K, whereas the selectivity decreased from 84% to 79% [4]."),
        (72, 668, 10, "Figure 2 summarises these trends for the supported nickel catalysts."),
        (72, 640, 12, "Table 1"),
        (72, 622, 10, "Table 1. Conversion and selectivity of supported nickel catalysts at three temperatures."),
    ],
    [
        (72, 760, 12, "4 Discussion"),
        (72, 742, 10, "The decrease in selectivity is probably caused by sintering of the supported nickel catalysts, consistent with earlier reports [5]."),
        (72, 728, 10, "We cannot exclude carbon deposition under these conditions, although the conversion remained stable."),
        (72, 700, 12, "5 Conclusion"),
        (72, 682, 10, "Supported nickel catalysts remain promising for methane reforming, but long-term stability must be verified beyond 100 h."),
        (72, 654, 12, "References"),
        (72, 636, 10, "[1] Smith J. et al. J. Catal. 2019, 371, 112-120."),
        (72, 622, 10, "[2] Chen L. et al. ACS Catal. 2020, 10, 4455-4463."),
        (72, 608, 10, "[3] Wang H. et al. Catal. Today 2018, 316, 88-95."),
        (72, 594, 10, "[4] Garcia M. et al. Appl. Catal. A 2021, 610, 117940."),
    ],
]

FONT_RESOURCES = {10: "F1", 12: "F2", 16: "F3"}

# Mock agent side: a glossary with one deliberate inconsistency and one number edit.
GLOSSARY = [
    {"term": "supported nickel catalysts", "translation": "负载型镍催化剂", "keep_original": False, "note": "core subject"},
    {"term": "selectivity", "translation": "选择性", "keep_original": False},
    {"term": "conversion", "translation": "转化率", "keep_original": False},
    {"term": "methane reforming", "translation": "甲烷重整", "keep_original": False},
    {"term": "XRD", "translation": "X射线衍射", "keep_original": True, "note": "acronym kept"},
    {"term": "ICP-OES", "translation": "电感耦合等离子体发射光谱", "keep_original": True},
    {"term": "sintering", "translation": "烧结", "keep_original": False},
    {"term": "carbon deposition", "translation": "积碳", "keep_original": False},
]
TERMS = [(entry["term"], entry["translation"]) for entry in GLOSSARY]


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        FAILS.append(f"{name}: {detail}")
        print(f"  FAIL {name} {detail}")


def _esc(text: str) -> str:
    backslash = chr(92)
    return text.replace(backslash, backslash * 2).replace("(", backslash + "(").replace(")", backslash + ")")


def make_pdf(path: Path) -> None:
    objects: list[bytes] = []
    page_start = 3
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{page_start + 2 * i} 0 R" for i in range(len(PAGES)))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(PAGES)} >>".encode())
    font_base = page_start + 2 * len(PAGES)
    for index, lines in enumerate(PAGES):
        ops = ["BT"]
        for x, y, size, text in lines:
            ops.append(f"/{FONT_RESOURCES[size]} {size} Tf 1 0 0 1 {x} {y} Tm ({_esc(text)}) Tj")
        ops.append("ET")
        content = "\n".join(ops).encode("latin-1")
        page_num = page_start + 2 * index
        content_num = page_num + 1
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Contents {content_num} 0 R "
            f"/Resources << /Font << /F1 {font_base} 0 R /F2 {font_base + 1} 0 R "
            f"/F3 {font_base + 2} 0 R >> >> >>".encode())
        objects.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
                       + content + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n").encode()
    path.write_bytes(bytes(out))


def build_fixture(root: Path) -> Path:
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    make_pdf(root / "paper.pdf")
    (root / "case.json").write_text(json.dumps(CASE, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "task.md").write_text(TASK_MD, encoding="utf-8")
    return root


def run_cli(cwd: Path, *args: str, name: str = "") -> subprocess.CompletedProcess:
    proc = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=str(cwd),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    label = name or ("run " + " ".join(args))
    check(f"{label} exit={proc.returncode}", proc.returncode == 0,
          (proc.stdout[-600:] + "\n" + proc.stderr[-1200:]))
    return proc


def mock_translate(text: str, tamper: bool = False) -> str:
    out = text
    for source, target in TERMS:
        if source.isupper():
            replacement = target + "（" + source + "）"
        else:
            replacement = target
        out = re.sub(re.escape(source), replacement, out, flags=re.I)
    if tamper:
        out = out.replace("转化率", "转换率").replace("79%", "78%")
    return "【译】" + out


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def stage_prepare(fixture: Path) -> dict:
    print("prepare")
    work = fixture / "output" / "work"
    proc = run_cli(fixture, "--prepare", name="prepare")
    check("prepare banner", "PREPARE OK" in proc.stdout, proc.stdout[-400:])
    check("work dir", work.is_dir(), str(work))
    inventory = read_json(work / "segments.json")
    segments = inventory["segments"]
    check("segments >= 15", len(segments) >= 15, str(len(segments)))
    check("segment ids ordered", [s["seg_id"] for s in segments]
          == [f"seg-{i:04d}" for i in range(1, len(segments) + 1)])
    check("segments carry page+bbox", all(s.get("page") and s.get("bbox") for s in segments))
    check("pages 1..3", sorted({s["page"] for s in segments}) == [1, 2, 3], str(sorted({s["page"] for s in segments})))
    batches = sorted((work / "batches").glob("batch-*.json"))
    check("at least 3 batches", len(batches) >= 3, str([p.name for p in batches]))
    worksheet = read_json(batches[0])
    row = worksheet["segments"][0]
    check("worksheet has must_keep", "must_keep" in row and "text" in row, str(list(row.keys())))
    check("worksheet output file", worksheet["output_file"] == "generated/" + batches[0].name)
    check("worksheet instructions", bool(worksheet["instruction"]["rules"]))
    glossary_ws = read_json(work / "glossary_worksheet.json")
    terms = [item["term"].casefold() for item in glossary_ws["candidates"]]
    check("candidate term found", any("supported nickel catalysts" in t for t in terms), str(terms[:12]))
    check("candidate has pages", any(item["pages"] for item in glossary_ws["candidates"]
                                     if "supported nickel catalysts" in item["term"].casefold()))
    facts = read_json(work / "facts.json")
    check("facts written", len(facts["segments"]) == len(segments))
    return {"work": work, "segments": segments, "batches": batches}


def stage_glossary_and_answer(fixture: Path, context: dict) -> dict:
    print("agent answers")
    work = context["work"]
    generated = work / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    (generated / "_glossary.json").write_text(json.dumps(GLOSSARY, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
    proc = run_cli(fixture, "--prepare", name="prepare#2")
    check("second prepare banner", "PREPARE OK" in proc.stdout, proc.stdout[-300:])
    worksheets = [read_json(p) for p in sorted((work / "batches").glob("batch-*.json"))]
    locked = [term for worksheet in worksheets for term in worksheet["locked_terms"]]
    check("locked terms embedded in worksheets",
          any(term["term"] == "conversion" for term in locked), str(locked[:6]))
    check("no batch marked answered yet",
          all(not worksheet["already_answered"] for worksheet in worksheets))

    segments_by_id = {segment["seg_id"]: segment for segment in context["segments"]}
    tampered: str | None = None
    for worksheet in worksheets:
        records = []
        for row in worksheet["segments"]:
            source = row["text"]
            is_tampered = ("79%" in source) and ("conversion" in source.casefold())
            if is_tampered:
                tampered = row["seg_id"]
            records.append({"seg_id": row["seg_id"],
                            "translation": mock_translate(source, tamper=is_tampered),
                            "terms_used": [term["term"] for term in row["terms_in_segment"]],
                            "note": ""})
        (generated / (worksheet["batch_id"] + ".json")).write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    check("tampered segment identified", tampered is not None)
    context["tampered"] = tampered
    context["segments_by_id"] = segments_by_id
    context["generated_files"] = sorted(p.name for p in generated.glob("*.json"))
    return context


def stage_check(fixture: Path, context: dict) -> None:
    print("check mode")
    proc = run_cli(fixture, "--check", name="check")
    payload = json.loads(proc.stdout)
    check("check ready", payload.get("ready") is True, json.dumps(payload.get("problems"), ensure_ascii=False))
    check("check coverage", payload.get("coverage") == 1.0, str(payload.get("coverage")))
    check("check records", payload.get("translated") == len(context["segments"]))


def stage_finalize(fixture: Path, context: dict) -> dict:
    print("finalize")
    out = fixture / "output"
    proc = run_cli(fixture, "--finalize", name="finalize")
    check("finalize banner", "FINALIZE DONE" in proc.stdout, proc.stdout[-400:])
    result = read_json(out / "result.json")
    declared = list(CASE["output_contract"].keys())
    check("case_id", result.get("case_id") == "tr-demo-003", str(result.get("case_id")))
    check("contract order", list(result.keys())[:len(declared) + 1] == ["case_id"] + declared,
          str(list(result.keys())[:6]))
    check("only unknown field reported missing",
          result["meta"]["contract_fields_not_reported"] == ["qqq_unmodelled_field"],
          str(result["meta"]["contract_fields_not_reported"]))
    check("unknown field shaped by type", result.get("qqq_unmodelled_field") == "")
    check("status success", result["meta"]["status"] == "success", str(result["meta"].get("limits")))

    pairs = result["zh_en_paragraph_pairs"]
    segments_by_id = context["segments_by_id"]
    check("pair count", len(pairs) == len(context["segments"]), str(len(pairs)))
    check("pair src identical", all(pair["src"] == segments_by_id[pair["seg_id"]]["text"] for pair in pairs))
    check("pair page/block carried",
          all(pair["page"] == segments_by_id[pair["seg_id"]]["page"]
              and pair["block_id"] == segments_by_id[pair["seg_id"]]["block_ids"][0]
              and pair["bbox"] == segments_by_id[pair["seg_id"]]["bbox"] for pair in pairs))
    check("pair order ascending", [pair["order"] for pair in pairs] == sorted(pair["order"] for pair in pairs))
    check("pair anchors present", all(pair["anchor"]["page"] == pair["page"]
                                      and pair["anchor"]["block_id"] == pair["block_id"]
                                      and pair["anchor"]["quote"] for pair in pairs))
    check("every pair translated", all(pair["status"] == "translated" for pair in pairs),
          str([pair["seg_id"] for pair in pairs if pair["status"] != "translated"]))
    check("anchor checks grounded", all((pair["anchor_check"] or {}).get("grounded") for pair in pairs),
          str([(pair["seg_id"], pair["anchor_check"]) for pair in pairs
               if not (pair["anchor_check"] or {}).get("grounded")][:5]))
    check("grounding rate 1.0", result["evidence_anchors"]["grounding"]["grounding_rate"] == 1.0,
          str(result["evidence_anchors"]["grounding"]))

    tampered_pair = next(pair for pair in pairs if pair["seg_id"] == context["tampered"])
    check("number edit caught", any("79" in number for number in tampered_pair["checks"]["numbers_missing"]),
          str(tampered_pair["checks"]["numbers_missing"]))
    check("tampered pair verdict false", tampered_pair["checks"]["ok"] is False,
          str(tampered_pair["checks"]["issues"]))
    others = [pair for pair in pairs if pair["seg_id"] != context["tampered"]]
    check("all other pairs clean", all(pair["checks"]["ok"] for pair in others),
          str([(p["seg_id"], p["checks"]["issues"]) for p in others if not p["checks"]["ok"]][:5]))

    loyalty = result["term_loyalty_report"]
    conversion = next(row for row in loyalty["per_term"] if row["term"].casefold() == "conversion")
    check("term inconsistency caught", conversion["status"] == "inconsistent", json.dumps(conversion, ensure_ascii=False))
    check("term offending seg listed", context["tampered"] in conversion["offending_seg_ids"])
    check("term rate below one", loyalty["term_rate"] < 1.0, str(loyalty["term_rate"]))
    check("usage rate below one", loyalty["usage_rate"] < 1.0, str(loyalty["usage_rate"]))
    check("glossary locked", bool(result["terminology_bank"]) and
          any(entry["term"] == "conversion" and entry["translation"] == "转化率"
              for entry in result["terminology_bank"]))
    completeness = result["paragraph_completeness"]
    check("coverage complete", completeness["coverage"] == 1.0, str(completeness))
    check("no omissions", result["omission_list"] == [], str(result["omission_list"])[:200])
    check("translated body carries chinese", "选择性" in result["translated_body"])
    check("source passages echoed", len(result["source_passages"]) == len(context["segments"]))
    check("source full text field", "supported nickel catalysts" in result["原文全文"].casefold())
    check("metrics report failures", result["grade_metrics"]["checks_failed"] >= 1,
          json.dumps(result["grade_metrics"], ensure_ascii=False)[:400])

    check("evidence.json written", (out / "evidence.json").is_file())
    check("run_report.json written", (out / "run_report.json").is_file())
    check("bilingual markdown written", (out / "translation_bilingual.md").is_file())
    check("glossary deliverable written", (out / "glossary.json").is_file())
    check("review checklist written", (out / "review_checklist.md").is_file())
    report = read_json(out / "run_report.json")
    check("report coverage", report["coverage"]["coverage"] == 1.0, str(report.get("coverage")))
    check("report generated files", len(report.get("generated_files") or []) == len(context["generated_files"]))
    markdown = (out / "translation_bilingual.md").read_text(encoding="utf-8")
    check("bilingual markdown has translation", "选择性" in markdown)
    checklist = (out / "review_checklist.md").read_text(encoding="utf-8")
    check("checklist names the tampered paragraph", context["tampered"] in checklist, checklist[:300])
    context["result"] = result
    return result


def stage_degrade(fixture: Path, context: dict) -> None:
    print("degrade (missing generated batch)")
    work = context["work"]
    out = fixture / "output"
    answer_files = sorted((work / "generated").glob("batch-*.json"))
    removed = read_json(answer_files[-1])
    removed_ids = [record["seg_id"] for record in removed]
    answer_files[-1].unlink()
    run_cli(fixture, "--finalize", name="finalize#degrade")
    result = read_json(out / "result.json")
    check("degraded status partial", result["meta"]["status"] == "partial",
          str(result["meta"]["status"]))
    pairs = result["zh_en_paragraph_pairs"]
    check("pair count unchanged", len(pairs) == len(context["segments"]))
    degraded = [pair for pair in pairs if pair["seg_id"] in removed_ids]
    check("degraded pairs have empty tgt",
          all(pair["tgt"] == "" and pair["status"] == "untranslated" for pair in degraded),
          str([(pair["seg_id"], pair["status"], pair["tgt"][:10]) for pair in degraded]))
    omissions = {item["seg_id"] for item in result["omission_list"]}
    check("omissions listed", set(removed_ids) <= omissions, str(sorted(omissions))[:200])
    check("coverage below one", result["paragraph_completeness"]["coverage"] < 1.0,
          str(result["paragraph_completeness"]["coverage"]))
    check("degraded contract still complete",
          all(field in result for field in CASE["output_contract"]),
          str([field for field in CASE["output_contract"] if field not in result]))
    check("degraded limits explain", any("no usable" in str(limit) for limit in result["meta"]["limits"]),
          str(result["meta"]["limits"])[:300])
    markdown = (out / "translation_bilingual.md").read_text(encoding="utf-8")
    check("markdown marks untranslated", "未生成译文" in markdown)


def stage_all_mode() -> None:
    print("all mode (no generated content)")
    fixture = build_fixture(TMP / "fixture_all")
    proc = run_cli(fixture, "--all", name="all")
    check("all banner", "PREPARE OK" in proc.stdout and "FINALIZE DONE" in proc.stdout, proc.stdout[-400:])
    result = read_json(fixture / "output" / "result.json")
    check("all mode contract complete",
          all(field in result for field in CASE["output_contract"]))
    check("all mode status partial", result["meta"]["status"] == "partial", str(result["meta"]["status"]))
    pairs = result["zh_en_paragraph_pairs"]
    check("all mode pairs present", bool(pairs))
    check("all mode pairs untranslated", all(pair["tgt"] == "" and pair["status"] == "untranslated"
                                             for pair in pairs))
    check("all mode evidence + report", (fixture / "output" / "evidence.json").is_file()
          and (fixture / "output" / "run_report.json").is_file())


def stage_deterministic() -> None:
    print("determinism")
    fixture = build_fixture(TMP / "fixture_det")
    run_cli(fixture, "--prepare", name="det prepare#1")
    work = fixture / "output" / "work"
    snapshot_one = {p.relative_to(work).as_posix(): p.read_bytes()
                    for p in sorted(work.rglob("*.json"))
                    if p.name != "state.json"}
    run_cli(fixture, "--prepare", name="det prepare#2")
    snapshot_two = {p.relative_to(work).as_posix(): p.read_bytes()
                    for p in sorted(work.rglob("*.json"))
                    if p.name != "state.json"}
    differing = [name for name in snapshot_one if snapshot_one.get(name) != snapshot_two.get(name)]
    check("prepare deterministic", snapshot_one == snapshot_two, str(differing))


def main() -> int:
    try:
        shutil.rmtree(TMP, ignore_errors=True)
        fixture = build_fixture(TMP / "fixture")
        context = stage_prepare(fixture)
        context = stage_glossary_and_answer(fixture, context)
        stage_check(fixture, context)
        stage_finalize(fixture, context)
        stage_degrade(fixture, context)
        stage_all_mode()
        stage_deterministic()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    if FAILS:
        print(f"\nFAILED {len(FAILS)} check(s):")
        for failure in FAILS:
            print(" -", failure)
        return 1
    print("\nall translation checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
