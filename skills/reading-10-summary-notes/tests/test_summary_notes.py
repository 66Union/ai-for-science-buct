#!/usr/bin/env python3
"""End-to-end self-test for the reading-10 adapter (section notes + paper card).

Everything runs on a synthetic, self-generated PDF fixture: the test never needs a
real paper, a network or a third-party package, so it behaves the same on a reviewer
machine, in the packaged submission directory and inside the evaluation image.

Temporary files are written inside this repository (a restricted sandbox cannot write
to %TEMP%) and removed in a finally block.

Run:
    python tests/test_summary_notes.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
try:  # packaged layout: <skill root>/lib
    sys.path.insert(0, str(ROOT))
    from lib import hybrid  # noqa: E402,F401
except ImportError:  # development layout: skills/_core/lib
    sys.path.insert(0, str(ROOT.parent / "_core"))
    from lib import hybrid  # noqa: E402,F401

SCRIPT = ROOT / "scripts" / "run_summary_notes.py"
TMP = ROOT / "_selftest_tmp"
FAILS: list[str] = []
SENTINEL = "not reported"

# Field names deliberately differ from the adapter's internal keys: the whole point of
# the reflection layer is that a hidden case decides the schema.
CONTRACT = {
    "structured_card": {"type": "object"},
    "chapter_notes": {"type": "array"},
    "knowledge_points": {"type": "array"},
    "caveats": {"type": "array"},
    "coverage_report": {"type": "object"},
    "evidence_anchors": {"type": "array"},
    "note_markdown": {"type": "string"},
    "referee_commentary": {"type": "string"},
}
EXPECTED_MAPPING = {
    "structured_card": "paper_card",
    "chapter_notes": "section_notes",
    "knowledge_points": "key_points",
    "caveats": "limitations",
    "coverage_report": "coverage",
    "evidence_anchors": "evidence",
    "note_markdown": "notes_markdown",
}
CARD_KEYS = ["research_question", "data", "methods", "findings", "conclusions", "limitations"]

PAGES = [
    [(72, 800, 16, "Structured Notes on Supported Nickel Catalysts"),
     (72, 762, 12, "Abstract"),
     (72, 744, 10, "We investigate the effect of nickel loading on CO2 conversion over silica."),
     (72, 730, 10, "We used 10 mol% Ni on SiO2 and measured conversion at 523 K and 1 bar."),
     (72, 716, 10, "The conversion may reach 85% under these conditions [1]."),
     (72, 692, 12, "1 Introduction"),
     (72, 674, 10, "Supported nickel catalysts are widely used for CO2 hydrogenation."),
     (72, 660, 10, "However the active site structure remains debated in the literature."),
     (72, 646, 10, "This study aims to link nickel loading to measured conversion."),
     (72, 622, 12, "2 Methods"),
     (72, 604, 10, "Samples were prepared by incipient wetness impregnation at 300 C."),
     (72, 590, 10, "We performed X-ray diffraction (XRD) and N2 physisorption."),
     (72, 576, 10, "Reaction tests ran in a fixed bed reactor at 523 K for 12 h."),
     (72, 562, 10, "Conversion was analysed by gas chromatography with a thermal detector.")],
    [(72, 800, 12, "3 Results"),
     (72, 782, 10, "Conversion increased from 42% at 5 mol% Ni to 85% at 10 mol% Ni."),
     (72, 768, 10, "The 15 mol% sample showed lower conversion of 61% due to particle growth."),
     (72, 754, 10, "XRD patterns confirmed metallic nickel domains after reduction at 773 K."),
     (72, 740, 10, "The surface area decreased from 210 m2/g to 168 m2/g with higher loading."),
     (72, 716, 12, "4 Discussion"),
     (72, 698, 10, "The results suggest that 10 mol% Ni gives the best dispersion."),
     (72, 684, 10, "However the study cannot exclude sintering during the 12 h test."),
     (72, 670, 10, "A limitation is that only one support and one pressure were tested."),
     (72, 646, 12, "5 Conclusion"),
     (72, 628, 10, "We conclude that 10 mol% Ni on SiO2 is the most active loading tested."),
     (72, 614, 10, "Further study of other supports is needed before generalisation.")],
]


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print("  ok   %s" % name)
    else:
        FAILS.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))


def escape_pdf(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(path: Path, pages: list) -> None:
    """Minimal multi-page PDF with per-line font sizes (Helvetica, no compression)."""
    contents = []
    for lines in pages:
        ops = ["BT"]
        for x, y, size, text in lines:
            ops.append("/F1 %d Tf 1 0 0 1 %d %d Tm (%s) Tj" % (size, x, y, escape_pdf(text)))
        ops.append("ET")
        contents.append("\n".join(ops).encode("latin-1"))
    count = len(pages)
    page_numbers = [3 + i for i in range(count)]
    content_numbers = [3 + count + i for i in range(count)]
    font_number = 3 + 2 * count
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % n for n in page_numbers)
           + b"] /Count %d >>" % count,
        font_number: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for index in range(count):
        objects[page_numbers[index]] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents %d 0 R "
            b"/Resources << /Font << /F1 %d 0 R >> >> >>"
            % (content_numbers[index], font_number))
        objects[content_numbers[index]] = (
            b"<< /Length " + str(len(contents[index])).encode() + b" >>\nstream\n"
            + contents[index] + b"\nendstream")
    raw = bytearray(b"%PDF-1.4\n")
    offsets = [0] * (font_number + 1)
    for number in range(1, font_number + 1):
        offsets[number] = len(raw)
        raw += b"%d 0 obj\n" % number + objects[number] + b"\nendobj\n"
    xref = len(raw)
    raw += b"xref\n0 %d\n" % (font_number + 1) + b"0000000000 65535 f \n"
    for number in range(1, font_number + 1):
        raw += b"%010d 00000 n \n" % offsets[number]
    raw += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (font_number + 1, xref))
    path.write_bytes(bytes(raw))


def new_case(name: str, *, material: bool = True) -> Path:
    base = TMP / name
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)
    (base / "case.json").write_text(json.dumps(
        {"case_id": "demo-%s" % name, "output_contract": CONTRACT},
        ensure_ascii=False, indent=2), encoding="utf-8")
    (base / "task.md").write_text(
        "# demo task\nRead the paper and produce section-aligned structured notes.\n",
        encoding="utf-8")
    if material:
        make_pdf(base / "paper.pdf", PAGES)
    return base


def run_skill(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(SCRIPT)] + list(args)
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)
    if proc.returncode != 0:
        print("    command failed:", " ".join(args), "rc=", proc.returncode)
        print("    stderr:", (proc.stderr or "")[-2500:])
        print("    stdout:", (proc.stdout or "")[-1500:])
    return proc


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def produced(result: dict, key: str):
    """A value maps to a declared field when the case declares it, else it is kept
    under meta.additional_outputs. Read whichever applies."""
    if key in result:
        return result[key]
    return (result.get("meta") or {}).get("additional_outputs", {}).get(key)


def write_generated_notes(base: Path, only_batches: int | None = None) -> int:
    """Answer the worksheets the way the SKILL.md protocol asks an agent to."""
    work = base / "work"
    answers = 0
    for worksheet in sorted((work / "batches").glob("batch-*.json")):
        if only_batches is not None and answers >= only_batches:
            break
        data = load(worksheet)
        records = []
        for seg in data["segments"]:
            text = seg["text"]
            records.append({
                "seg_id": seg["seg_id"],
                "point_type": seg.get("point_type_hint") or "RESULT",
                "note": "Note on %s: %s" % (seg.get("section") or "section", text[:110]),
                "quote": text[:110],
            })
        (work / "generated").mkdir(parents=True, exist_ok=True)
        (work / "generated" / worksheet.name).write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        answers += 1
    return answers


def write_generated_card(base: Path) -> None:
    work = base / "work"
    worksheet = load(work / "card_worksheet.json")
    card = {}
    for field in worksheet["fields"]:
        if field["candidates"]:
            candidate = field["candidates"][0]
            text = candidate["sentences"][0]
            card[field["key"]] = {"text": text, "seg_id": candidate["seg_id"], "quote": text}
        else:
            card[field["key"]] = SENTINEL
    (work / "generated").mkdir(parents=True, exist_ok=True)
    (work / "generated" / "card.json").write_text(
        json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1. prepare: worksheets, placeholder, determinism
# --------------------------------------------------------------------------- #
def test_prepare() -> None:
    print("prepare + placeholder")
    base = new_case("prepare")
    out = base / "output"
    proc = run_skill(base, "--prepare", "--out", str(out), "--work", str(base / "work"))
    check("prepare exits 0", proc.returncode == 0)
    check("prepare prints next steps", "NEXT 1" in proc.stdout, proc.stdout[-200:])
    check("placeholder result.json written at start", (out / "result.json").is_file())
    placeholder = load(out / "result.json")
    check("placeholder status partial", placeholder["meta"]["status"] == "partial",
          str(placeholder["meta"].get("status")))
    check("placeholder names the case", placeholder["case_id"] == "demo-prepare")

    sections = load(base / "work" / "sections.json")
    titles = [s["title"].casefold() for s in sections["sections"]]
    check("sections detected", sections["section_count"] >= 5, str(sections["section_count"]))
    for wanted in ("abstract", "introduction", "methods", "results", "conclusion"):
        check("section %s detected" % wanted, any(wanted in title for title in titles), str(titles))
    check("each section carries segments",
          all(s["seg_count"] >= 1 for s in sections["sections"]))
    check("each section has a page label",
          all(s["pages_label"] for s in sections["sections"]))
    check("heading anchors kept",
          any(s["heading_block_id"] for s in sections["sections"]))

    facts = load(base / "work" / "facts.json")
    check("machine facts extracted", facts["document"]["numbers_total"] >= 5,
          str(facts["document"]))
    check("facts per segment", len(facts["segments"]) >= 10, str(len(facts["segments"])))

    worksheet = load(base / "work" / "card_worksheet.json")
    check("six card fields offered", len(worksheet["fields"]) == 6,
          str([f["key"] for f in worksheet["fields"]]))
    check("card fields have candidate evidence",
          sum(1 for f in worksheet["fields"] if f["candidates"]) >= 5,
          str([(f["key"], f["candidate_total"]) for f in worksheet["fields"]]))

    manifest = load(base / "work" / "manifest.json")
    batches = manifest["worksheets"]["batches"]
    check("batches written", len(batches) >= 2, str(len(batches)))
    check("batch worksheets exist",
          all((base / "work" / "batches" / (b["batch_id"] + ".json")).is_file() for b in batches))
    first_batch = load(base / "work" / "batches" / (batches[0]["batch_id"] + ".json"))
    check("batch carries facts", all("facts" in seg or "point_type_hint" in seg
                                     for seg in first_batch["segments"]))
    check("batch names its answer file",
          all("answer_file" in batch for batch in batches))
    check("generated readme schema",
          (base / "work" / "generated" / "_README.json").is_file())
    check("manifest agent protocol", "step_4" in manifest["agent_protocol"])

    before = (base / "work" / "sections.json").read_bytes()
    run_skill(base, "--prepare", "--out", str(out), "--work", str(base / "work"))
    check("prepare is deterministic",
          before == (base / "work" / "sections.json").read_bytes())


# --------------------------------------------------------------------------- #
# 2. full generation: contract reflection + anchors + status success
# --------------------------------------------------------------------------- #
def test_full_generation() -> None:
    print("full generation")
    base = new_case("full")
    out = base / "output"
    work = base / "work"
    run_skill(base, "--prepare", "--out", str(out), "--work", str(work))
    answered = write_generated_notes(base)
    check("answered every batch worksheet", answered >= 2, str(answered))
    write_generated_card(base)
    proc = run_skill(base, "--finalize", "--out", str(out), "--work", str(work))
    check("finalize exits 0", proc.returncode == 0)

    result = load(out / "result.json")
    check("case_id echoed", result.get("case_id") == "demo-full", str(result.get("case_id")))
    check("all contract fields present",
          all(key in result for key in CONTRACT), str([k for k in CONTRACT if k not in result]))
    check("reflection mapping exact",
          result["meta"]["contract_field_mapping"] == EXPECTED_MAPPING,
          str(result["meta"]["contract_field_mapping"]))
    check("unsupported field declared missing",
          result["meta"]["contract_fields_not_reported"] == ["referee_commentary"],
          str(result["meta"]["contract_fields_not_reported"]))
    check("unsupported field keeps declared shape", result.get("referee_commentary") == "")

    card = result["structured_card"]
    check("paper card has six fields", all(key in card for key in CARD_KEYS), str(list(card)))
    check("card fields come from the agent",
          all(card[key].get("source") == "agent" for key in CARD_KEYS),
          str({key: card[key].get("source") for key in CARD_KEYS}))
    check("card fields anchored",
          all(card[key].get("page") and card[key].get("block_id") for key in CARD_KEYS),
          str({key: (card[key].get("page"), card[key].get("block_id")) for key in CARD_KEYS}))
    check("card quotes verified",
          all(card[key].get("match") in ("exact", "normalised") for key in CARD_KEYS),
          str({key: card[key].get("match") for key in CARD_KEYS}))

    groups = result["chapter_notes"]
    check("notes grouped by section", len(groups) == len(load(work / "sections.json")["sections"]),
          "%d groups" % len(groups))
    check("every section has notes", all(group["note_count"] >= 1 for group in groups),
          str([(g["section_id"], g["note_count"]) for g in groups if not g["note_count"]]))
    check("groups keep paper order",
          [g["section_id"] for g in groups] == sorted(g["section_id"] for g in groups))
    all_notes = [note for group in groups for note in group["notes"]]
    check("anchors carry page + block + bbox + quote",
          all(note["page"] and note["block_id"] and len(note["bbox"]) == 4 and note["quote"]
              for note in all_notes))
    check("anchor block ids have the source shape",
          all(str(note["block_id"]).startswith("p") and "-b" in str(note["block_id"])
              for note in all_notes))
    check("notes carry section ids", all(note["section_id"] for note in all_notes))
    check("grounding is complete",
          result["evidence_anchors"]["grounding"]["grounding_rate"] == 1.0,
          str(result["evidence_anchors"]["grounding"]))

    points = result["knowledge_points"]
    check("key points extracted", len(points) >= 10, str(len(points)))
    check("key points are anchored",
          all(point["seg_id"] and point["page"] and point["quote"] for point in points))
    check("point types from the vocabulary",
          all(point["point_type"] in ("RQ", "DATA", "METHOD", "RESULT", "CONCLUSION",
                                      "LIMITATION", "DEFINITION", "RELATION", "GAP")
              for point in points), str({p["point_type"] for p in points}))

    caveats = result["caveats"]
    check("limitations reported", len(caveats) >= 1, str(len(caveats)))
    check("paper limitation anchored",
          any(item.get("scope") == "paper" and item.get("page") for item in caveats),
          str([(c.get("scope"), c.get("page")) for c in caveats]))

    coverage = result["coverage_report"]
    check("no uncovered sections", coverage["sections_without_notes"] == [],
          str(coverage["sections_without_notes"]))
    check("card completeness is one", coverage["card_fields_generated"] == 6,
          str(coverage["card_fields_generated"]))
    check("baseline not used", coverage["baseline_used"] is False)

    markdown = result["note_markdown"]
    check("markdown has card and section parts",
          "## Paper card" in markdown and "## Section notes" in markdown)
    check("markdown names the title", "Nickel" in markdown, markdown[:120])

    check("status success", result["meta"]["status"] == "success",
          str(result["meta"]["status"]) + " " + str(result["meta"]["limits"]))
    evidence = load(out / "evidence.json")
    check("evidence has per-claim checks", evidence["claims_total"] >= len(all_notes),
          str(evidence.get("claims_total")))
    check("evidence documents the anchor format", "anchor_format" in evidence)
    check("evidence lists generated batches", evidence["generated"]["batches_missing"] == [])
    report = load(out / "run_report.json")
    check("run report counts generated notes",
          report["notes_source"]["generated"] == len(all_notes), str(report.get("notes_source")))

    before = (out / "result.json").read_bytes()
    run_skill(base, "--finalize", "--out", str(out), "--work", str(work))
    check("finalize is deterministic", before == (out / "result.json").read_bytes())


# --------------------------------------------------------------------------- #
# 3. missing generated content: honest degradation
# --------------------------------------------------------------------------- #
def test_degradation() -> None:
    print("degradation without generated notes")
    base = new_case("degrade")
    out = base / "output"
    work = base / "work"
    # the contract path: prepare in one process, write nothing, finalize in another
    proc = run_skill(base, "--prepare", "--out", str(out), "--work", str(work))
    check("prepare exits 0 before the missing-generation case", proc.returncode == 0)
    proc = run_skill(base, "--finalize", "--out", str(out), "--work", str(work))
    check("finalize exits 0 without generated content", proc.returncode == 0)
    result = load(out / "result.json")
    check("status partial", result["meta"]["status"] == "partial",
          str(result["meta"]["status"]))
    limits = " ".join(result["meta"]["limits"]).casefold()
    check("limits admit the baseline", "baseline" in limits, limits[:400])
    check("all contract fields still present",
          all(key in result for key in CONTRACT))
    groups = result["chapter_notes"]
    check("baseline still produces section notes", len(groups) >= 5, str(len(groups)))
    baseline_notes = [note for group in groups for note in group["notes"]]
    check("baseline notes are labelled",
          baseline_notes and all(note["source"] == "deterministic_baseline"
                                 for note in baseline_notes))
    check("baseline notes still anchored",
          all(note["page"] and note["block_id"] and note["quote"] for note in baseline_notes))
    card = result["structured_card"]
    check("card filled from baseline",
          sum(1 for key in CARD_KEYS if card.get(key, {}).get("source") == "deterministic_baseline") >= 3,
          str({key: card.get(key, {}).get("source") for key in CARD_KEYS}))
    check("baseline is not presented as agent work",
          result["coverage_report"]["baseline_used"] is True)
    check("quality reports baseline notes",
          produced(result, "quality")["grounding"]["baseline_notes"] > 0,
          str(produced(result, "quality")["grounding"]))
    check("evidence admits the baseline", load(out / "evidence.json")["baseline_used"] is True)
    check("knowledge points survive the baseline", len(result["knowledge_points"]) >= 5,
          str(len(result["knowledge_points"])))
    coverage = result["coverage_report"]
    check("navigation sections do not lower coverage",
          coverage["content_sections_total"] <= coverage["sections_total"]
          and coverage["section_coverage_rate"] >= 0.6,
          str(coverage)[:300])
    check("pages of navigation sections reported",
          isinstance(coverage.get("navigation_sections"), list))
    # the one-shot path must behave the same as prepare+finalize
    base2 = new_case("degrade_all")
    proc = run_skill(base2, "--all", "--out", str(base2 / "output"), "--work", str(base2 / "work"))
    check("--all exits 0", proc.returncode == 0)
    check("--all degrades the same way",
          load(base2 / "output" / "result.json")["meta"]["status"] == "partial")


# --------------------------------------------------------------------------- #
# 4. bad quotes / fabricated numbers: verification must catch them
# --------------------------------------------------------------------------- #
def test_verification_catches_bad_notes() -> None:
    print("verification of bad notes")
    base = new_case("bad")
    out = base / "output"
    work = base / "work"
    run_skill(base, "--prepare", "--out", str(out), "--work", str(work))
    batch = load(work / "batches" / sorted(p.name for p in (work / "batches").glob("batch-*.json"))[0])
    first, second = batch["segments"][0], batch["segments"][1]
    records = [
        {"seg_id": first["seg_id"], "point_type": "RESULT",
         "note": "The conversion reached 12345.678 kWh at 999 K.",
         "quote": "this sentence does not exist anywhere in the paper"},
        {"seg_id": second["seg_id"], "point_type": "NONSENSE",
         "note": "Supported nickel catalysts are widely used for CO2 hydrogenation.",
         "quote": second["text"][:80]},
    ]
    (work / "generated").mkdir(parents=True, exist_ok=True)
    (work / "generated" / "batch-01.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    run_skill(base, "--finalize", "--out", str(out), "--work", str(work))
    result = load(out / "result.json")
    check("status partial for unverified notes", result["meta"]["status"] == "partial",
          str(result["meta"]["status"]))
    evidence = load(out / "evidence.json")
    claims = {claim["claim_id"]: claim for claim in evidence["claims"]}
    bad = [claim for claim in evidence["claims"] if claim.get("source") == "agent"]
    check("both generated notes present", len(bad) == 2, str(len(bad)))
    fabricated = [claim for claim in bad if "fabricated_numbers" in (claim.get("flags") or [])]
    check("fabricated numbers detected", len(fabricated) == 1,
          str([claim.get("flags") for claim in bad]))
    ungrounded = [claim for claim in bad if not claim.get("grounded")]
    check("ungrounded note flagged", len(ungrounded) >= 1,
          str([(claim.get("match"), claim.get("grounded")) for claim in bad]))
    unknown = [claim for claim in bad if "unknown_point_type" in (claim.get("flags") or [])]
    check("unknown point type flagged", len(unknown) == 1,
          str([claim.get("flags") for claim in bad]))
    check("number fidelity reported",
          produced(result, "quality")["number_fidelity"]["fabricated_numbers"] >= 1,
          str(produced(result, "quality")["number_fidelity"]))
    check("limits explain the partial status",
          "fabricated_numbers" in " ".join(result["meta"]["limits"]).casefold(),
          " ".join(result["meta"]["limits"])[:400])
    check("claims index matches", len(claims) >= 2)


# --------------------------------------------------------------------------- #
# 5. no material at all: failed status, honest shell
# --------------------------------------------------------------------------- #
def test_no_material() -> None:
    print("no material")
    base = new_case("nomaterial", material=False)
    out = base / "output"
    proc = run_skill(base, "--all", "--out", str(out), "--work", str(base / "work"))
    check("exits 0 with no material", proc.returncode == 0)
    check("result.json exists", (out / "result.json").is_file())
    result = load(out / "result.json")
    check("status failed", result["meta"]["status"] == "failed",
          str(result["meta"]["status"]))
    check("contract fields keep their shape",
          all(key in result for key in CONTRACT),
          str([key for key in CONTRACT if key not in result]))
    check("array fields are empty arrays", result["chapter_notes"] == [])
    check("string fields are empty strings", result["referee_commentary"] == "")
    check("limits state the missing material",
          "material" in " ".join(result["meta"]["limits"]).casefold(),
          " ".join(result["meta"]["limits"])[:300])
    check("run report exists", (out / "run_report.json").is_file())
    check("evidence exists", (out / "evidence.json").is_file())


def main() -> int:
    if not SCRIPT.is_file():
        print("adapter script not found:", SCRIPT)
        return 1
    TMP.mkdir(parents=True, exist_ok=True)
    try:
        test_prepare()
        test_full_generation()
        test_degradation()
        test_verification_catches_bad_notes()
        test_no_material()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    if FAILS:
        print("\nFAILED %d check(s):" % len(FAILS))
        for failure in FAILS:
            print(" -", failure)
        return 1
    print("\nall reading-10 checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
