#!/usr/bin/env python3
"""Self-test for the reading-14 nanopublication skill.

Runs the real CLI end to end on a synthetic fixture (no network, no model):

  1. unit checks for sentence splitting, atomicity flags, relation inference,
     vocabulary loading and the TriG validator;
  2. prepare -> inspect worksheets / manifest / source metadata;
  3. simulated agent answers -> finalize -> contract reflection, nanopublications,
     TriG + JSON-LD, evidence/report files, determinism;
  4. rejected records (bad quote / unknown segment / invalid relation);
  5. degraded paths: no generated answers (--all) and no material document.

Temporary files stay inside the repository (the sandbox cannot write %TEMP%).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
try:  # packaged layout: <skill-root>/lib
    sys.path.insert(0, str(ROOT))
    from lib import hybrid  # noqa: F401,E402
except ImportError:  # development layout: skills/_core/lib
    sys.path.insert(0, str(ROOT.parent / "_core"))
    from lib import hybrid  # noqa: F401,E402

sys.path.insert(0, str(ROOT / "scripts"))
import run_nanopublication as runner  # noqa: E402

TMP = ROOT / "_selftest_tmp"
SCRIPT = ROOT / "scripts" / "run_nanopublication.py"
FAILS: list[str] = []

PAPER = """Nanopublication Demo: Supported Nickel Catalysts
Authors: A. One, B. Two
doi: 10.1234/demo.2024.001

Abstract
We report 10 mol% Ni on SiO2 at 523 K. The conversion may reach 85% [1]. The catalyst uses incipient wetness preparation and yields 92% selectivity under 1 bar.

1 Introduction
Supported metal catalysts are widely used in hydrogenation reactions [1,2]. Previous work suggests that particle size controls activity; however the mechanism remains debated. Our study builds on the model of Smith et al. (2019).

2 Methods
Samples were prepared by incipient wetness at 300 C. The nickel loading was measured to be 9.8 wt%. Each sample was reduced in H2 at 723 K for 2 h.

3 Results
The rate constant k_1 was 3.2 s-1 (eq. 2). Conversion increased from 42% to 85% as temperature rose from 473 K to 523 K. Selectivity did not decrease with time on stream. These results support the hypothesis that Ni loading determines activity, and they contradict the earlier claim of particle-size control.

4 Conclusion
Our findings demonstrate that 10 mol% Ni on SiO2 reaches 85% conversion at 523 K. Further work should test lower loadings.
"""

CONTRACT = [
    {"name": "nanopub_records", "type": "array"},
    {"name": "claim_table", "type": "array"},
    {"name": "paper_provenance", "type": "object"},
    {"name": "controlled_vocabulary", "type": "array"},
    {"name": "audit_metrics", "type": "object"},
    {"name": "trig_export", "type": "object"},
    {"name": "evidence_bundle", "type": "object"},
    {"name": "unused_field_x", "type": "array"},
]

TASK_MD = """# Task
Read the material and build nanopublications: split the conclusions into atomic
assertions, bind each assertion to a verbatim quote with page/block location, and
export machine-readable structures (JSON assertion table + TriG/JSON-LD).
"""


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print("  ok   " + name)
    else:
        FAILS.append(name + ": " + detail)
        print("  FAIL " + name + " " + detail)


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def run_cli(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=str(cwd),
                          capture_output=True, text=True, timeout=300)


def write_fixture(base: Path, with_material: bool = True) -> None:
    shutil.rmtree(base, ignore_errors=True)
    (base / "materials").mkdir(parents=True, exist_ok=True)
    (base / "case.json").write_text(
        json.dumps({"case_id": "np-demo-001", "output_contract": CONTRACT}, indent=2),
        encoding="utf-8")
    (base / "task.md").write_text(TASK_MD, encoding="utf-8")
    if with_material:
        (base / "materials" / "paper.txt").write_text(PAPER, encoding="utf-8")


def batch_files(work: Path) -> list[Path]:
    return sorted((work / "batches").glob("batch-*.json"))


def collect_segments(work: Path) -> list[dict]:
    data = load_json(work / "segments.json", {}) or {}
    return data.get("segments") or []


def find_sentence(segments: list[dict], needle: str) -> tuple[dict, dict]:
    for segment in segments:
        for sentence in segment.get("sentences") or []:
            if needle in sentence["text"]:
                return segment, sentence
    raise AssertionError("sentence not found: " + needle)


def answer_all_batches(work: Path, segments: list[dict], good: bool = True) -> int:
    """Write one valid record for every batch so coverage is 1.0."""
    written = 0
    for worksheet_path in batch_files(work):
        worksheet = load_json(worksheet_path, {}) or {}
        records = []
        for segment in worksheet.get("segments") or []:
            for sentence in segment.get("sentences") or []:
                text = sentence["text"]
                if len(text) < 20 or len(text) > 240:
                    continue
                if not (sentence.get("facts") or {}).get("numbers") \
                        and not sentence.get("relation_cue"):
                    continue
                records.append({
                    "assertion_id": "a-" + sentence["sentence_id"],
                    "seg_id": segment["seg_id"],
                    "sentence_id": sentence["sentence_id"],
                    "text": text,
                    "subject": runner.leading_phrase(text),
                    "relation": sentence["suggested_relation"],
                    "object": "",
                    "condition": "",
                    "confidence": 0.7,
                    "quote": text,
                    "direction": "subject->object",
                    "evidence_type": "direct",
                })
                if len(records) >= 2:
                    break
            if len(records) >= 2:
                break
        if not records:
            for segment in worksheet.get("segments") or []:
                for sentence in segment.get("sentences") or []:
                    if 20 <= len(sentence["text"]) <= 240:
                        records.append({
                            "assertion_id": "a-" + sentence["sentence_id"],
                            "seg_id": segment["seg_id"],
                            "text": sentence["text"],
                            "subject": runner.leading_phrase(sentence["text"]),
                            "relation": "states", "object": "", "condition": "",
                            "confidence": 0.6, "quote": sentence["text"],
                            "direction": "subject->object", "evidence_type": "direct",
                        })
                        break
                if records:
                    break
        generated = work / "generated" / worksheet_path.name
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_text(json.dumps(
            {"batch_id": worksheet.get("batch_id"), "records": records}, indent=2),
            encoding="utf-8")
        written += 1
    return written


# --------------------------------------------------------------------------- #
# 1. unit checks
# --------------------------------------------------------------------------- #
def test_units() -> None:
    print("units")
    sentences = runner.split_sentences(
        "We report 10 mol% Ni. E.g. this is fine. However, Fig. 3 shows it.")
    check("sentence split keeps abbreviations", len(sentences) == 3, str(sentences))
    check("abbreviation not split", "E.g." in sentences[1], str(sentences))
    simple = runner.atomicity_flags("The nickel loading was measured to be 9.8 wt%.")
    check("single clause is atomic", simple["atomic"], json.dumps(simple))
    compound = runner.atomicity_flags(
        "These results support the hypothesis that Ni loading determines activity, "
        "and they contradict the earlier claim of particle-size control.")
    check("compound flagged", not compound["atomic"], json.dumps(compound))
    check("compound has split hint", bool(compound["split_hint"]), json.dumps(compound))
    check("compound flags include connectives",
          "multiple_connectives" in compound["flags"], json.dumps(compound))
    long_text = "This claim " + "and also " * 40 + "ends here."
    check("long assertion flagged", "too_long" in runner.atomicity_flags(long_text)["flags"])

    vocab = runner.load_vocab()
    names = runner.relation_names(vocab)
    check("vocab has 7 controlled relations", len(names) == 7, str(names))
    for relation in ("supports", "contradicts", "uses", "measures",
                     "derived_from", "part_of", "states"):
        check("vocab contains " + relation, relation in names, str(names))
    check("vocab loaded from resources",
          vocab.get("source") == "resources/relation_vocab.json", str(vocab.get("source")))
    check("relation cue inference",
          runner.infer_relation("These results support the hypothesis.", vocab)[0]
          == "supports")
    check("relation cue contradictions",
          runner.infer_relation("This contradicts the earlier claim.", vocab)[0]
          == "contradicts")

    valid_trig = """@prefix np: <http://www.nanopub.org/nschema#> .
@prefix npv: <https://w3id.org/np/skill-reading14/vocab#> .

<https://w3id.org/np/RAabc> {
    <https://w3id.org/np/RAabc#s> <https://w3id.org/np/skill-reading14/vocab#states> <https://w3id.org/np/RAabc#o> .
    <https://w3id.org/np/RAabc#s> <https://w3id.org/np/skill-reading14/vocab#states> <https://w3id.org/np/RAabc#o2> .
}
"""
    verdict = runner.validate_trig(valid_trig)
    check("valid trig accepted", verdict["ok"], json.dumps(verdict))
    check("trig triple count", verdict["triples"] == 2, json.dumps(verdict))
    broken = "<https://w3id.org/np/RAabc> { this is not trig\n"
    check("broken trig rejected", not runner.validate_trig(broken)["ok"])


# --------------------------------------------------------------------------- #
# 2. prepare
# --------------------------------------------------------------------------- #
def test_prepare(fixture: Path, run_dir: Path) -> None:
    print("prepare")
    out_dir = run_dir / "out"
    work = run_dir / "work"
    shutil.rmtree(run_dir, ignore_errors=True)
    result = run_cli("--prepare", "--out", str(out_dir), "--work", str(work),
                     "--batch-size", "3", cwd=fixture)
    check("prepare exit 0", result.returncode == 0, result.stderr[-800:])
    check("prepare prints PREPARE_OK", "PREPARE_OK" in result.stdout, result.stdout[-400:])

    manifest = load_json(work / "manifest.json", {}) or {}
    check("manifest written", bool(manifest), "missing work/manifest.json")
    check("manifest status prepared", manifest.get("status") == "prepared",
          str(manifest.get("status")))
    check("case id propagated", manifest.get("case_id") == "np-demo-001",
          str(manifest.get("case_id")))
    check("segments found", int(manifest.get("segments_total") or 0) >= 6,
          str(manifest.get("segments_total")))
    check("batches written", len(batch_files(work)) >= 2, str(len(batch_files(work))))

    worksheets = [load_json(path, {}) for path in batch_files(work)]
    first = worksheets[0]
    check("worksheet has answer schema", bool(first.get("answer_schema")), str(first.keys()))
    check("worksheet has allowed relations",
          first.get("allowed_relations") == runner.relation_names(runner.load_vocab()),
          str(first.get("allowed_relations")))
    check("worksheet has instructions", bool(first.get("instructions")))
    check("worksheet has example record", bool(first.get("example_record")))
    segments = first.get("segments") or []
    check("worksheet segments have anchors",
          all(item.get("seg_id") and item.get("page") and item.get("block_id")
              for item in segments))
    check("worksheet sentences numbered",
          all(sentence.get("sentence_id") for item in segments
              for sentence in item.get("sentences") or []))
    check("worksheet sentences carry facts",
          all(isinstance(sentence.get("facts"), dict) for item in segments
              for sentence in item.get("sentences") or []))

    inventory = collect_segments(work)
    check("segments inventory matches manifest",
          len(inventory) == int(manifest.get("segments_total") or -1),
          str(len(inventory)))
    check("segment ids unique", len({item["seg_id"] for item in inventory}) == len(inventory))
    check("segment ids ordered",
          [item["seg_id"] for item in inventory]
          == [f"seg-{index:04d}" for index in range(1, len(inventory) + 1)],
          str([item["seg_id"] for item in inventory[:3]]))

    source = load_json(work / "source_metadata.json", {}) or {}
    check("source title detected",
          "Supported Nickel Catalysts" in str(source.get("title")),
          str(source.get("title")))
    check("source authors detected", bool(source.get("authors")),
          str(source.get("authors")))
    check("source doi detected", str(source.get("doi")).startswith("10.1234/"),
          str(source.get("doi")))
    check("source year detected", source.get("year") == "2024", str(source.get("year")))
    check("source file hashed", len(str(source.get("sha256"))) == 64,
          str(source.get("sha256"))[:12])

    placeholder = load_json(out_dir / "result.json", {}) or {}
    check("placeholder result written before slow work",
          bool(placeholder), "no out/result.json after --prepare")
    check("placeholder status partial",
          (placeholder.get("meta") or {}).get("status") == "partial",
          str(placeholder.get("meta", {}).get("status")))
    check("example answer template written",
          (work / "generated" / "_example.json").is_file())


# --------------------------------------------------------------------------- #
# 3. finalize with complete answers
# --------------------------------------------------------------------------- #
def _contract_values(payload: dict) -> dict:
    return {field["name"]: payload.get(field["name"]) for field in CONTRACT}


def test_finalize_complete(fixture: Path, run_dir: Path) -> None:
    print("finalize complete")
    work = run_dir / "work"
    out_dir = run_dir / "out"
    segments = collect_segments(work)
    batches = answer_all_batches(work, segments)
    check("answers written for every batch", batches >= 2, str(batches))
    result = run_cli("--finalize", "--out", str(out_dir), "--work", str(work),
                     cwd=fixture)
    check("finalize exit 0", result.returncode == 0, result.stderr[-1200:])
    check("finalize prints FINALIZE_OK", "FINALIZE_OK" in result.stdout,
          result.stdout[-400:])

    payload = load_json(out_dir / "result.json", {}) or {}
    check("result has case_id", payload.get("case_id") == "np-demo-001",
          str(payload.get("case_id")))
    for field in CONTRACT:
        check("contract field present: " + field["name"], field["name"] in payload)
    check("contract order preserved",
          list(payload.keys())[1:len(CONTRACT) + 1]
          == [field["name"] for field in CONTRACT],
          str(list(payload.keys())))
    check("unused field is a typed placeholder", payload.get("unused_field_x") == [],
          str(payload.get("unused_field_x")))
    meta = payload.get("meta") or {}
    check("unused field recorded as not reported",
          "unused_field_x" in (meta.get("contract_fields_not_reported") or []),
          str(meta.get("contract_fields_not_reported")))
    mapping = meta.get("contract_field_mapping") or {}
    check("mapping reflects different contract names",
          mapping.get("claim_table") == "assertions"
          and mapping.get("paper_provenance") == "source_metadata",
          json.dumps(mapping))
    # one declared field (unused_field_x) is intentionally unmapped, hence 7/8
    check("fill ratio reflects mapped fields",
          meta.get("field_fill_ratio") == 0.875, str(meta.get("field_fill_ratio")))
    check("run status success", meta.get("status") == "success", str(meta.get("status")))
    check("not degraded", meta.get("degraded") is False, str(meta.get("degraded")))

    assertions = payload.get("claim_table") or []
    nanopubs = payload.get("nanopub_records") or []
    check("assertions produced", len(assertions) >= 4, str(len(assertions)))
    check("nanopublication per assertion", len(nanopubs) == len(assertions),
          str((len(nanopubs), len(assertions))))
    check("all assertions have quotes",
          all(item.get("quote") for item in assertions))
    check("all assertions verified",
          all(item.get("quote_match") in ("exact", "normalised") for item in assertions))
    check("all assertions have page and block",
          all(item.get("page") and item.get("block_id") for item in assertions))
    check("assertion ids unique",
          len({item["assertion_id"] for item in assertions}) == len(assertions))
    first_np = nanopubs[0]
    check("nanopub has three graphs",
          all(key in first_np for key in ("assertion", "provenance", "publication_info")),
          str(list(first_np.keys())))
    check("nanopub id is stable sha prefix",
          str(first_np.get("nanopub_id", "")).startswith("np-")
          and len(str(first_np.get("nanopub_id"))) == 19,
          str(first_np.get("nanopub_id")))
    check("provenance carries quote and location",
          bool(first_np["provenance"].get("quote"))
          and first_np["provenance"].get("page") is not None
          and bool(first_np["provenance"].get("block_id")))
    check("provenance links to source document",
          bool(first_np["provenance"].get("source_document")),
          str(first_np["provenance"].get("source_document")))

    quality = payload.get("audit_metrics") or {}
    check("grounding rate 1.0", quality.get("grounding_rate") == 1.0,
          str(quality.get("grounding_rate")))
    check("no baseline in complete run", quality.get("assertions_baseline") == 0,
          str(quality.get("assertions_baseline")))
    check("batch coverage complete",
          (quality.get("batch_coverage") or {}).get("coverage") == 1.0,
          json.dumps(quality.get("batch_coverage")))
    check("rejected none", quality.get("rejected_total") == 0,
          str(quality.get("rejected_total")))
    check("trig serialisation valid",
          (quality.get("serialization") or {}).get("trig_valid") is True,
          json.dumps(quality.get("serialization")))

    trig = out_dir / "nanopubs.trig"
    jsonld_path = out_dir / "nanopubs.jsonld"
    check("trig file written", trig.is_file() and trig.stat().st_size > 200)
    trig_text = trig.read_text(encoding="utf-8")
    check("trig has prefix declarations", "@prefix np:" in trig_text)
    check("trig has assertion graph blocks",
          trig_text.count("np:hasAssertion") == len(nanopubs), str(trig_text.count("np:hasAssertion")))
    check("trig has provenance graph", "prov:wasDerivedFrom" in trig_text)
    check("trig has publication info graph", "npv:assertionId" in trig_text)
    check("trig lines are triples",
          all(line.strip().endswith((" .", ";", "{", "}")) or not line.strip()
              or line.strip().startswith(("#", "@prefix"))
              for line in trig_text.splitlines()))
    check("trig revalidates", runner.validate_trig(trig_text)["ok"])
    jsonld = load_json(jsonld_path, {}) or {}
    check("jsonld written and parseable", bool(jsonld), "nanopubs.jsonld missing")
    check("jsonld has @graph", len(jsonld.get("@graph") or []) == len(nanopubs))
    check("jsonld carries npv context",
          "npv" in (jsonld.get("@context") or {}), json.dumps(jsonld.get("@context")))
    check("result trig export matches file",
          (payload.get("trig_export") or {}).get("trig") == trig_text)

    for name in ("evidence.json", "run_report.json", "assertions.json",
                 "sources.json", "quality.json", "rejected_assertions.json"):
        check("deliverable written: " + name, (out_dir / name).is_file())
    evidence = load_json(out_dir / "evidence.json", {}) or {}
    check("evidence carries grounding", bool(evidence.get("grounding")),
          str(list(evidence.keys())))
    check("evidence carries claims", len(evidence.get("claims") or []) == len(assertions))
    check("evidence links blocks snapshot",
          bool((evidence.get("verification") or {}).get("blocks_sha256")))
    report = load_json(out_dir / "run_report.json", {}) or {}
    check("run report no model calls", report.get("no_model_calls") is True)
    check("run report lists inputs", bool(report.get("inputs")))
    check("run report lists progress", bool(report.get("generated")))

    # determinism: same work dir, different out dir -> identical contract payload
    out_b = run_dir / "out_b"
    result_b = run_cli("--finalize", "--out", str(out_b), "--work", str(work),
                       cwd=fixture)
    check("repeat finalize exit 0", result_b.returncode == 0, result_b.stderr[-600:])
    payload_b = load_json(out_b / "result.json", {}) or {}
    check("repeat run contract identical",
          _contract_values(payload) == _contract_values(payload_b))
    check("repeat run trig identical",
          (out_dir / "nanopubs.trig").read_text(encoding="utf-8")
          == (out_b / "nanopubs.trig").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 4. reject path + relation mapping
# --------------------------------------------------------------------------- #
def test_reject_and_relation_map(fixture: Path, run_dir: Path) -> None:
    print("reject and relation mapping")
    work = run_dir / "work_bad"
    out_dir = run_dir / "out_bad"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(run_dir / "work", work)
    segments = collect_segments(work)
    conv_seg, conv_sentence = find_sentence(segments, "Conversion increased from 42%")
    compound_seg, compound_sentence = find_sentence(
        segments, "contradict the earlier claim")
    records = [
        {"assertion_id": "a-bad-1", "seg_id": "seg-9999", "text": "Unknown segment.",
         "subject": "x", "relation": "states", "object": "", "confidence": 0.5,
         "quote": "Unknown segment.", "direction": "subject->object",
         "evidence_type": "direct"},
        {"assertion_id": "a-bad-2", "seg_id": conv_seg["seg_id"],
         "text": "The catalyst reaches 85% conversion.", "subject": "catalyst",
         "relation": "measures", "object": "85%", "confidence": 0.6,
         "quote": "Not the real sentence at all.", "direction": "subject->object",
         "evidence_type": "direct"},
        {"assertion_id": "a-map-1", "seg_id": conv_seg["seg_id"],
         "sentence_id": conv_sentence["sentence_id"],
         "text": conv_sentence["text"], "subject": "conversion",
         "relation": "causes", "object": "temperature", "confidence": 0.8,
         "quote": conv_sentence["text"], "direction": "subject->object",
         "evidence_type": "direct"},
        {"assertion_id": "a-compound", "seg_id": compound_seg["seg_id"],
         "sentence_id": compound_sentence["sentence_id"],
         "text": compound_sentence["text"], "subject": "results",
         "relation": "supports", "object": "hypothesis", "confidence": 0.9,
         "quote": compound_sentence["text"], "direction": "subject->object",
         "evidence_type": "direct"},
    ]
    for worksheet_path in batch_files(work):
        (work / "generated" / worksheet_path.name).write_text(json.dumps(
            {"batch_id": worksheet_path.stem, "records": records}, indent=2),
            encoding="utf-8")
        break
    result = run_cli("--finalize", "--out", str(out_dir), "--work", str(work),
                     cwd=fixture)
    check("reject-path finalize exit 0", result.returncode == 0, result.stderr[-1000:])
    payload = load_json(out_dir / "result.json", {}) or {}
    meta = payload.get("meta") or {}
    quality = payload.get("audit_metrics") or {}
    check("reject path is partial", meta.get("status") == "partial",
          str(meta.get("status")))
    check("rejected records counted", int(quality.get("rejected_total") or 0) >= 2,
          str(quality.get("rejected_total")))
    rejected = load_json(out_dir / "rejected_assertions.json", {}) or {}
    reasons = [item.get("reason") for item in rejected.get("rejected") or []]
    check("unknown segment rejected", "unknown_seg_id" in reasons, str(reasons))
    check("bad quote rejected", "quote_not_in_segment" in reasons, str(reasons))
    limits = " ".join(meta.get("limits") or [])
    check("limits explain rejection", "rejected" in limits, limits[-300:])

    assertions = payload.get("claim_table") or []
    mapped = [item for item in assertions if item.get("assertion_id") == "a-map-1"]
    check("invalid relation still accepted", bool(mapped), str(len(assertions)))
    if mapped:
        item = mapped[0]
        check("invalid relation mapped to vocabulary",
              item.get("relation") in runner.relation_names(runner.load_vocab()),
              str(item.get("relation")))
        check("relation mapping flagged",
              "relation_not_in_vocab" in (item.get("quality_flags") or []),
              str(item.get("quality_flags")))
    compound = [item for item in assertions if item.get("assertion_id") == "a-compound"]
    check("compound assertion flagged as non-atomic",
          bool(compound) and compound[0].get("atomic") is False,
          json.dumps(compound[0].get("quality_flags") if compound else []))


# --------------------------------------------------------------------------- #
# 5. degraded paths
# --------------------------------------------------------------------------- #
def test_degraded_all(fixture: Path, run_dir: Path) -> None:
    print("degraded --all (no generated answers)")
    out_dir = run_dir / "out_degraded"
    work = run_dir / "work_degraded"
    shutil.rmtree(run_dir / "out_degraded", ignore_errors=True)
    shutil.rmtree(work, ignore_errors=True)
    result = run_cli("--all", "--out", str(out_dir), "--work", str(work),
                     cwd=fixture)
    check("all-mode exit 0", result.returncode == 0, result.stderr[-1000:])
    check("all-mode prints both stages",
          "PREPARE_OK" in result.stdout and "FINALIZE_OK" in result.stdout,
          result.stdout[-400:])
    payload = load_json(out_dir / "result.json", {}) or {}
    meta = payload.get("meta") or {}
    quality = payload.get("audit_metrics") or {}
    check("degraded status partial", meta.get("status") == "partial",
          str(meta.get("status")))
    check("degraded flag", meta.get("degraded") is True, str(meta.get("degraded")))
    assertions = payload.get("claim_table") or []
    check("baseline still produces assertions", len(assertions) > 0,
          str(len(assertions)))
    check("baseline relations are states only",
          all(item.get("relation") == "states" for item in assertions),
          str([item.get("relation") for item in assertions]))
    check("baseline origin recorded",
          all(item.get("origin") == "baseline_extractive" for item in assertions))
    check("baseline limits reported",
          "baseline" in " ".join(meta.get("limits") or []).casefold(),
          str(meta.get("limits")))
    check("degraded quality flag",
          int(quality.get("assertions_baseline") or 0) == len(assertions),
          str(quality.get("assertions_baseline")))
    for field in CONTRACT:
        check("degraded contract field: " + field["name"], field["name"] in payload)
    check("degraded trig still valid",
          (out_dir / "nanopubs.trig").is_file()
          and runner.validate_trig((out_dir / "nanopubs.trig").read_text(
              encoding="utf-8"))["ok"])
    evidence = load_json(out_dir / "evidence.json", {}) or {}
    check("degraded evidence flag", evidence.get("degraded") is True,
          str(evidence.get("degraded")))
    check("degraded placeholder survived", (out_dir / "result.json").stat().st_size > 100)


def test_no_material(empty_case: Path, run_dir: Path) -> None:
    print("no material document")
    out_dir = run_dir / "out_nomaterial"
    work = run_dir / "work_nomaterial"
    shutil.rmtree(out_dir, ignore_errors=True)
    shutil.rmtree(work, ignore_errors=True)
    result = run_cli("--all", "--out", str(out_dir), "--work", str(work),
                     cwd=empty_case)
    check("no-material exit 0", result.returncode == 0, result.stderr[-1000:])
    payload = load_json(out_dir / "result.json", {}) or {}
    meta = payload.get("meta") or {}
    check("no-material status failed", meta.get("status") == "failed",
          str(meta.get("status")))
    check("no-material case id present", payload.get("case_id") == "np-demo-001",
          str(payload.get("case_id")))
    check("no-material contract still reflected", meta.get("contract_detected") is True)
    placeholders = meta.get("contract_fields_not_reported") or []
    check("no-material all fields placeholders",
          sorted(placeholders) == sorted(field["name"] for field in CONTRACT),
          str(placeholders))
    check("no-material placeholder types kept",
          payload.get("nanopub_records") == [] and payload.get("trig_export") == {},
          str((payload.get("nanopub_records"), payload.get("trig_export"))))
    check("array placeholder is []", payload.get("claim_table") == [],
          str(payload.get("claim_table")))
    check("object placeholder is {}", payload.get("paper_provenance") == {},
          str(payload.get("paper_provenance")))
    check("failure reason recorded", bool(meta.get("limits")), str(meta.get("limits")))
    check("no zero-byte result", (out_dir / "result.json").stat().st_size > 100)


def test_finalize_auto_prepare(fixture: Path, run_dir: Path) -> None:
    print("finalize auto-prepares a missing work dir")
    out_dir = run_dir / "out_auto"
    work = run_dir / "work_auto"
    shutil.rmtree(out_dir, ignore_errors=True)
    shutil.rmtree(work, ignore_errors=True)
    result = run_cli("--finalize", "--out", str(out_dir), "--work", str(work),
                     cwd=fixture)
    check("auto-prepare finalize exit 0", result.returncode == 0, result.stderr[-1000:])
    check("auto-prepare created manifest", (work / "manifest.json").is_file())
    payload = load_json(out_dir / "result.json", {}) or {}
    check("auto-prepare result partial",
          (payload.get("meta") or {}).get("status") == "partial",
          str((payload.get("meta") or {}).get("status")))


# --------------------------------------------------------------------------- #
# 6. documentation / resource consistency
# --------------------------------------------------------------------------- #
def test_docs() -> None:
    print("documentation")
    skill_md = ROOT / "SKILL.md"
    check("SKILL.md exists", skill_md.is_file(), str(skill_md))
    if not skill_md.is_file():
        return
    text = skill_md.read_text(encoding="utf-8")
    check("SKILL.md long enough", len(text) >= 4500, str(len(text)))
    for needle in ("--prepare", "--finalize", "--all", "work/generated",
                   "assertion_id", "seg_id", "quote", "TriG", "JSON-LD",
                   "relation_vocab.json", "异常处理", "限制", "踩坑"):
        check("SKILL.md mentions " + needle, needle in text)
    check("GITHUB_URL.txt exists", (ROOT / "GITHUB_URL.txt").is_file())
    check("vocabulary resource exists",
          (ROOT / "resources" / "relation_vocab.json").is_file())
    check("vocabulary is valid json",
          isinstance(load_json(ROOT / "resources" / "relation_vocab.json"), dict))


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    print("reading-14 nanopublication self-test")
    print("root =", ROOT)
    fixture = TMP / "fixture"
    empty_case = TMP / "empty_case"
    run_prepare = TMP / "run_prepare"
    try:
        shutil.rmtree(TMP, ignore_errors=True)
        TMP.mkdir(parents=True, exist_ok=True)
        write_fixture(fixture, with_material=True)
        write_fixture(empty_case, with_material=False)
        test_units()
        test_prepare(fixture, run_prepare)
        test_finalize_complete(fixture, run_prepare)
        test_reject_and_relation_map(fixture, run_prepare)
        test_degraded_all(fixture, TMP / "run_degraded")
        test_no_material(empty_case, TMP / "run_nomaterial")
        test_finalize_auto_prepare(fixture, TMP / "run_auto")
        test_docs()
    except Exception as exc:  # noqa: BLE001 - a crashed check must fail loudly
        import traceback
        traceback.print_exc()
        FAILS.append("unexpected exception: " + type(exc).__name__ + ": " + str(exc))
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("")
    if FAILS:
        print("FAILED " + str(len(FAILS)) + " check(s):")
        for failure in FAILS:
            print("  - " + failure)
        return 1
    print("all nanopublication checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
