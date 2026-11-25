#!/usr/bin/env python3
"""Self-test for lib/hybrid.py - the shared two-phase helpers.

Runs with the standard library only and writes its temporary files inside the
repository (tempfile under a restricted sandbox cannot write to %TEMP%).
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import hybrid  # noqa: E402

TMP = ROOT / "_selftest_hybrid"
FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        FAILS.append(f"{name}: {detail}")
        print(f"  FAIL {name} {detail}")


def fake_doc() -> dict:
    blocks = {
        "1": [
            {"block_id": "p1-b1", "bbox": [72, 700, 480, 730], "size": 18,
             "text": "A Study of Supported Nickel Catalysts"},
            {"block_id": "p1-b2", "bbox": [72, 640, 480, 690], "size": 10,
             "text": "Abstract We report 10 mol% Ni on SiO2 at 523 K. "
                     "The conversion may reach 85% [1]."},
        ],
        "2": [
            {"block_id": "p2-b1", "bbox": [72, 720, 480, 740], "size": 12,
             "text": "2 Methods"},
            {"block_id": "p2-b2", "bbox": [72, 600, 480, 700], "size": 10,
             "text": "Samples were prepared by incipient wetness at 300 C. "
                     "The rate constant k_1 was 3.2 s-1 (eq. 2)."},
        ],
    }
    return {
        "path": "paper.pdf", "engine": "builtin", "anchor_quality": "span",
        "text_layer": True, "total_chars": 300, "pages": [], "blocks": blocks,
    }


def test_segments() -> None:
    print("segments")
    doc = fake_doc()
    sections = [
        {"id": "sec-1", "title": "A Study of Supported Nickel Catalysts",
         "page": 1, "block_id": "p1-b1", "level": 2},
        {"id": "sec-2", "title": "2 Methods", "page": 2, "block_id": "p2-b1", "level": 1},
    ]
    segments = hybrid.build_segments(doc, {"sections": sections})
    check("segment count", len(segments) == 4, str(len(segments)))
    check("ordered ids", [s["seg_id"] for s in segments]
          == ["seg-0001", "seg-0002", "seg-0003", "seg-0004"])
    check("page carried", segments[2]["page"] == 2)
    check("section carried", segments[2]["section_title"] == "2 Methods")
    check("bbox kept", segments[0]["bbox"] == [72, 700, 480, 730])


def test_batches() -> None:
    print("batches")
    shutil.rmtree(TMP, ignore_errors=True)
    segments = hybrid.build_segments(fake_doc())
    work = TMP / "work"
    batches = hybrid.make_batches(segments, work, batch_size=2)
    check("batch count", len(batches) == 2, str(batches))
    check("worksheet written", (work / "batches" / "batch-01.json").is_file())
    check("pending initially", hybrid.missing_batches(work) == ["batch-01", "batch-02"])

    hybrid.write_json(work / "generated" / "batch-01.json", [
        {"seg_id": "seg-0001", "text": "A Study of Supported Nickel Catalysts"},
        {"seg_id": "seg-0002", "text": "Abstract translation"},
    ])
    records = hybrid.load_generated(work)
    merged = hybrid.merge_generated(segments, records, required=("text",))
    check("coverage", merged["coverage"] == 0.5, str(merged["coverage"]))
    check("missing listed", merged["missing"] == ["seg-0003", "seg-0004"], str(merged["missing"]))
    check("pending after one", hybrid.missing_batches(work) == ["batch-02"])

    hybrid.write_json(work / "generated" / "batch-02.json",
                      {"records": [{"seg_id": "seg-0003", "text": "x"},
                                   {"seg_id": "seg-0004", "text": "y"}]})
    merged = hybrid.merge_generated(hybrid.build_segments(fake_doc()), hybrid.load_generated(work))
    check("full coverage", merged["coverage"] == 1.0, str(merged["coverage"]))

    bad = hybrid.merge_generated(segments, [
        {"seg_id": "seg-0001", "text": "a"}, {"seg_id": "seg-0001", "text": "b"},
        {"seg_id": "seg-9999", "text": "c"}, {"seg_id": "seg-0002", "text": ""},
    ], required=("text",))
    check("duplicates detected", bad["duplicates"] == ["seg-0001"], str(bad["duplicates"]))
    check("unknown detected", bad["unknown_ids"] == ["seg-9999"], str(bad["unknown_ids"]))
    check("incomplete detected", bad["incomplete"] == ["seg-0002"], str(bad["incomplete"]))


def test_text_facts() -> None:
    print("text facts")
    text = "The conversion may reach 85% at 523 K and 10 mol% Ni [1,2]. Fig. 3 shows it."
    numbers = hybrid.find_numbers(text)
    for expected in ("85%", "523 K", "10", "3"):
        check(f"number {expected}", any(expected.split()[0] in n for n in numbers), str(numbers))
    check("citation [1,2]", "[1,2]" in hybrid.find_citations(text), str(hybrid.find_citations(text)))
    check("hedge may", "may" in hybrid.find_hedges(text), str(hybrid.find_hedges(text)))
    check("labels", "figure" in hybrid.find_labels(text) or "fig." in hybrid.find_labels(text))

    report = hybrid.preservation_check(
        "The conversion may reach 85% at 523 K [1].",
        "Conversion can reach 85% at 523 K [1].")
    check("preservation keeps numbers", report["numbers_missing"] == [], str(report))
    check("preservation flags hedge loss", "may" in report["hedges_missing"], str(report))
    report2 = hybrid.preservation_check(
        "The conversion may reach 85% at 523 K [1].",
        "The conversion reached 91% at 543 K.")
    check("preservation catches edits", not report2["ok"], str(report2))
    check("preservation counts missing citations",
          report2["citations_missing"] == ["[1]"], str(report2))


def test_terms() -> None:
    print("terms")
    text = ("Nickel catalysts were used. Nickel catalysts improved conversion. "
            "The XRD pattern of nickel catalysts was measured. XRD again.")
    terms = [t["term"] for t in hybrid.term_candidates(text, top=30)]
    joined = " | ".join(terms).casefold()
    check("phrase candidate", "nickel catalysts" in joined, joined)
    check("acronym candidate", "xrd" in joined, joined)


def test_mapping() -> None:
    print("contract mapping")
    declared = [
        {"name": "case_id", "spec": None},
        {"name": "parsed_pages", "spec": {"type": "array"}},
        {"name": "full_text", "spec": {"type": "string"}},
        {"name": "section_outline", "spec": {"type": "array"}},
        {"name": "term_glossary", "spec": {"type": "array"}},
        {"name": "bilingual_paragraphs", "spec": {"type": "array"}},
        {"name": "evidence_anchors", "spec": {"type": "array"}},
        {"name": "qqq_unmodelled_field", "spec": {"type": "string"}},
    ]
    values = {
        "pages": [{"page": 1}],
        "text": "hello",
        "sections": [{"title": "x"}],
        "glossary": [{"term": "a"}],
        "bilingual": [{"src": "a", "tgt": "b"}],
        "evidence": [{"page": 1}],
    }
    mapped, report = hybrid.map_fields(declared, values)
    check("exact pages", mapped.get("parsed_pages") == values["pages"], str(report))
    check("alias full_text", mapped.get("full_text") == "hello", str(report))
    check("alias outline", mapped.get("section_outline") == values["sections"], str(report))
    check("alias glossary", mapped.get("term_glossary") == values["glossary"], str(report))
    check("alias bilingual", mapped.get("bilingual_paragraphs") == values["bilingual"], str(report))
    check("alias evidence", mapped.get("evidence_anchors") == values["evidence"], str(report))
    check("unknown left out", "qqq_unmodelled_field" not in mapped, str(report))


def test_finalize() -> None:
    print("finalize")
    case = {"case_id": "demo-1", "output_contract": {
        "parsed_pages": {"type": "array"},
        "full_text": {"type": "string"},
        "translation_glossary": {"type": "array"},
        "qqq_unmodelled_field": {"type": "string"},
    }}
    out = TMP / "out"
    shutil.rmtree(out, ignore_errors=True)
    payload = hybrid.finalize(
        out, case=case, case_id="demo-1",
        values={"pages": [{"page": 1}], "text": "abc", "glossary": [{"term": "a"}]},
        status="success", limits=[], uncertainty=[],
        change_log=["test"], evidence={"grounding": {"grounding_rate": 1.0}},
        report=hybrid.run_report(stopwatch=hybrid.Stopwatch(), docs=1))
    check("result writer", (out / "result.json").is_file())
    check("evidence writer", (out / "evidence.json").is_file())
    check("report writer", (out / "run_report.json").is_file())
    on_disk = json.loads((out / "result.json").read_text(encoding="utf-8"))
    check("case_id kept", on_disk.get("case_id") == "demo-1")
    check("contract field filled", on_disk.get("full_text") == "abc", str(on_disk.keys()))
    check("mapping reported", payload["meta"]["contract_field_mapping"]["full_text"] == "text")
    check("fill ratio", payload["meta"]["field_fill_ratio"] == 0.75,
          str(payload["meta"]["field_fill_ratio"]))
    check("only unknown field reported missing",
          on_disk["meta"]["contract_fields_not_reported"] == ["qqq_unmodelled_field"],
          str(on_disk["meta"]["contract_fields_not_reported"]))
    # missing field keeps the declared shape and is listed
    check("missing field shape", on_disk.get("qqq_unmodelled_field") == "",
          str(on_disk.get("qqq_unmodelled_field")))


def test_placeholder() -> None:
    print("placeholder")
    out = TMP / "out2"
    shutil.rmtree(out, ignore_errors=True)
    path = hybrid.write_placeholder(out, case_id="c-9", contract_fields=3)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    check("placeholder file", data["case_id"] == "c-9")
    check("placeholder status", data["meta"]["status"] == "partial")
    check("placeholder note", bool(data["meta"]["limits"]))


def main() -> int:
    try:
        test_segments()
        test_batches()
        test_text_facts()
        test_terms()
        test_mapping()
        test_finalize()
        test_placeholder()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    if FAILS:
        print(f"\nFAILED {len(FAILS)} check(s):")
        for failure in FAILS:
            print(" -", failure)
        return 1
    print("\nall hybrid checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
