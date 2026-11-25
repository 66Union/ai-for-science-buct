#!/usr/bin/env python3
"""End-to-end self-test for writing-09-methods-experiments.

The test builds two synthetic fixtures (a conflicting one and a clean one), runs the
real CLI as a subprocess in both phases and asserts the contract reflection, the
traceability checks, the deterministic degradation path and reproducibility.

Run (development layout):   python skills/writing-09-methods-experiments/tests/test_methods.py
Run (packaged layout):      python tests/test_methods.py     (PYTHONPATH=<package root>)

Temporary files are created inside the skill directory because %TEMP% is not
guaranteed writable in the evaluation sandbox.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
try:  # packaged layout
    sys.path.insert(0, str(ROOT))
    from lib import hybrid  # noqa: E402
except ImportError:  # development layout
    sys.path.insert(0, str(ROOT.parent / "_core"))
    from lib import hybrid  # noqa: E402

TMP = ROOT / "_selftest_tmp"
SCRIPT = ROOT / "scripts" / "run_methods.py"
FAILS: list[str] = []

CONTRACT = {
    "case_id": {"type": "string", "description": "stable input identifier"},
    "chapter_body": {"type": "string", "description": "final methods & experiments chapter"},
    "hyperparameter_table": {"type": "array", "description": "name/value/unit/target/source"},
    "data_card": {"type": "array", "description": "dataset name/version/size/split"},
    "run_environment": {"type": "object", "description": "hardware/software/seeds/repeats"},
    "comparison_baselines": {"type": "array", "description": "baseline, version, budget, metric"},
    "reproduction_manifest": {"type": "array", "description": "code version/env/commands/seeds"},
    "number_provenance": {"type": "array", "description": "where each stated number comes from"},
    "scope_caveats": {"type": "array", "description": "limitations with failure scenarios"},
    "self_audit": {"type": "object", "description": "machine-checked metrics"},
    "author_followups": {"type": "array", "description": "questions for the author"},
    "process_steps": {"type": "array", "description": "ordered method steps"},
    "zzz_unmodelled_quota": {"type": "string", "description": "a field this skill never produces"},
}

DRAFT_TEMPLATE = """# 3 Methods and Experiments

## 3.1 Data

We use the CIFAR-100 dataset (v1.0) with 50000 training images and 10000 test images.
The official split is 45000/5000/10000 for train/validation/test.

## 3.2 Training setup

We train ResNet-18 for 120 epochs with the AdamW optimizer. The learning rate is {lr} and the batch size is {batch}.
The weight decay is set to 0.05. The random seed is 42. Training uses a single RTX 3090 GPU and takes about 6.2 hours.

## 3.3 Evaluation

We report top-1 accuracy on the test split and compare with the ResNet-50 baseline under the same budget.
All experiments are repeated 3 times.
"""

CONFIG_YAML = """seed: 42
epochs: 120
batch_size: 64
learning_rate: 0.001
weight_decay: 0.05
optimizer: adamw
dataset: cifar100
data_root: ./data
num_workers: 8
repeats: 3
"""

LOG_TEXT = """2026-01-01 10:00:00 start training
seed=42 device=cuda:0
gpu: NVIDIA RTX 3090
epoch=120 val_acc=0.812 test_acc=0.803
total_time=6.2h
WARNING: validation accuracy dropped after epoch 100
"""

CODE_TEXT = """import argparse
import torch

NUM_EPOCHS = 120
DEFAULT_LR = 0.001
DEFAULT_BATCH = 64


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--seed", type=int, default=42)
    torch.manual_seed(42)


if __name__ == "__main__":
    main()
"""

README_TEXT = """# Reproduction

Install the dependencies:

pip install torch==2.1.0 torchvision==0.16.0

Run the training with:

python train.py --config configs/train.yaml --seed 42
"""

DATA_TEXT = """CIFAR-100 v1.0 contains 50000 training images and 10000 test images.
The official split is 45000/5000/10000 (train/val/test).
Model: ResNet-18 backbone; baseline: ResNet-50.
"""


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print("  ok   " + name)
    else:
        FAILS.append(name + ": " + detail)
        print("  FAIL " + name + " " + detail)


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_json(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig", errors="replace"))


def make_fixture(base: Path, *, clean: bool = False) -> Path:
    """Build a fixture. clean=True removes the draft/material conflicts."""
    fixture = base / "fixture"
    write_json(fixture / "case.json", {
        "case_id": "fixture-methods-01",
        "task": "Write the Methods and Experiments chapter from the supplied materials.",
        "output_contract": CONTRACT,
    })
    write_text(fixture / "task.md",
               "# Task\n\nWrite the Methods and Experiments chapter.\n"
               "Every number must be traceable to the config, log, code or data note.\n")
    write_text(fixture / "environment" / "draft" / "methods_draft.md",
               DRAFT_TEMPLATE.format(lr="0.001" if clean else "0.01",
                                     batch="64" if clean else "96"))
    write_text(fixture / "environment" / "configs" / "train.yaml", CONFIG_YAML)
    write_text(fixture / "environment" / "logs" / "run.log", LOG_TEXT)
    write_text(fixture / "environment" / "code" / "train.py", CODE_TEXT)
    write_text(fixture / "environment" / "README.md", README_TEXT)
    write_text(fixture / "environment" / "data" / "README_data.txt", DATA_TEXT)
    return fixture


def make_draft_only(base: Path) -> Path:
    """A case that ships only the chapter draft: nothing independent to check against."""
    fixture = base / "fixture"
    write_json(fixture / "case.json", {
        "case_id": "fixture-draft-only",
        "output_contract": CONTRACT,
    })
    write_text(fixture / "task.md", "# Task\n\nPolish the supplied methods draft.\n")
    write_text(fixture / "environment" / "draft" / "methods_draft.md",
               DRAFT_TEMPLATE.format(lr="0.01", batch="96"))
    return fixture


def run(args: list[str], cwd: Path, expect: int = 0) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.run([sys.executable, str(SCRIPT)] + args, cwd=str(cwd),
                          capture_output=True, encoding="utf-8", errors="replace",
                          env=env, timeout=600)
    if expect is not None:
        check("exit code " + " ".join(args[:1]) + " == " + str(expect),
              proc.returncode == expect,
              "rc=" + str(proc.returncode) + " stderr=" + (proc.stderr or "")[-800:])
    return proc


def read_segments(work: Path) -> list[dict]:
    data = read_json(work / "draft_segments.json")
    return data.get("segments") or []


def role_for(text: str) -> list[str]:
    low = (text or "").casefold()
    if "dataset" in low or "split" in low or "images" in low:
        return ["dataset"]
    if any(token in low for token in ("epoch", "learning rate", "batch size",
                                      "weight decay", "seed", "gpu", "optimizer")):
        return ["parameters", "settings"]
    if "accuracy" in low or "baseline" in low or "repeated" in low:
        return ["baseline", "repro"]
    if "method" in low or "experiment" in low:
        return ["step"]
    return ["none"]


def write_generated(work: Path, *, fabricated: bool) -> None:
    """Simulate what the agent writes after reading SKILL.md."""
    segments = read_segments(work)
    batch_one = [{"seg_id": item["seg_id"], "roles": role_for(item.get("text")),
                  "note": "method material"} for item in segments[:6]]
    batch_two = [{"seg_id": item["seg_id"], "roles": role_for(item.get("text"))}
                 for item in segments[6:]]
    write_json(work / "generated" / "batch-01.json", batch_one)
    write_json(work / "generated" / "batch-02.json", batch_two)
    section = "\n".join([
        "## 3 Methods and Experiments",
        "",
        "### 3.1 Data",
        "We use the CIFAR-100 dataset (v1.0) with 50000 training images and 10000 test images. "
        "The official split is 45000/5000/10000 for train/validation/test.",
        "",
        "### 3.2 Training setup",
        "We train ResNet-18 for " + ("120 epochs" if not fabricated else "130 epochs") +
        " with the AdamW optimizer. The learning rate is 0.001, the batch size is " +
        ("64" if not fabricated else "128") + " and the weight decay is 0.05. The random seed is 42.",
        "",
        "### 3.3 Evaluation",
        "We report top-1 accuracy on the test split and compare with the ResNet-50 baseline "
        "under the same budget. All experiments are repeated 3 times.",
    ] + (["", "For robustness we additionally used 12000 unlabeled images."] if fabricated else []))
    payload = {
        "agent": "selftest",
        "steps": [
            {"step_id": "S1", "input": "CIFAR-100 images",
             "operation": "load the official train/validation/test split",
             "output": "data loaders", "condition": "official split files present",
             "source": "environment/data/README_data.txt:2"},
            {"step_id": "S2", "input": "ResNet-18 backbone",
             "operation": "train for 120 epochs with AdamW (learning rate 0.001, batch size 64)",
             "output": "trained checkpoint", "condition": "validation accuracy stops improving",
             "source": "environment/configs/train.yaml:4"},
            {"step_id": "S3", "input": "trained checkpoint",
             "operation": "evaluate top-1 accuracy on the test split",
             "output": "test accuracy", "condition": "same budget as the baseline",
             "source": "environment/logs/run.log:4"},
        ],
        "parameter_table": [
            {"name": "learning_rate", "value": "0.001", "unit": "-",
             "target": "AdamW optimizer", "source": "environment/configs/train.yaml:4"},
            {"name": "batch_size", "value": "64", "unit": "-",
             "target": "training loader", "source": "environment/configs/train.yaml:3"},
            {"name": "epochs", "value": "120", "unit": "epochs",
             "target": "training loop", "source": "environment/configs/train.yaml:2"},
            {"name": "seed", "value": "42", "unit": "-", "target": "all random sources",
             "source": "environment/configs/train.yaml:1"},
        ] + ([{"name": "warmup_steps", "value": "500", "unit": "steps",
               "target": "scheduler", "source": "environment/configs/train.yaml:12"}]
             if fabricated else []),
        "datasets": [{"name": "CIFAR-100", "version": "v1.0",
                      "size": "50000 train / 10000 test",
                      "preprocessing": "official images",
                      "split": "45000/5000/10000", "split_method": "official protocol",
                      "subsets": ["train", "validation", "test"],
                      "source": "environment/data/README_data.txt:1"}],
        "settings": {"hardware": "1 x RTX 3090", "software": "torch 2.1.0",
                     "seeds": "42", "repeats": "3", "metrics": "top-1 accuracy"},
        "baselines": [{"name": "ResNet-50", "version": "torchvision 0.16.0",
                       "budget": "same 120 epochs", "metric": "top-1 accuracy",
                       "result": "paper table", "source": "environment/data/README_data.txt:3"}],
        "reproducibility": {"code_version": "not recorded in the materials",
                            "environment": "torch 2.1.0, torchvision 0.16.0",
                            "commands": ["python train.py --config configs/train.yaml --seed 42"],
                            "seeds": [42]},
        "limitations": [
            {"limitation": "Only a single GPU was used",
             "failure_scenario": "when the batch size grows beyond 64 on one GPU the "
                                 "effective schedule no longer matches the reported setting",
             "scope": "single-GPU training", "needs_author_confirmation": True},
            {"limitation": "Validation accuracy dropped after epoch 100",
             "failure_scenario": "beyond epoch 100 validation accuracy degrades, so longer "
                                 "schedules are outside the validated range",
             "scope": "runs longer than 120 epochs", "needs_author_confirmation": True},
        ],
        "methods_section": section,
        "questions": ([{"question": "Warmup steps are not in the config; confirm the value.",
                        "why": "no source", "severity": "high"}] if fabricated else []),
    }
    write_json(work / "generated" / "methods.json", payload)


# --------------------------------------------------------------------------- #
# assertions
# --------------------------------------------------------------------------- #
DECLARED = list(CONTRACT.keys())


def assert_contract(result: dict, label: str) -> None:
    missing = [name for name in DECLARED if name not in result]
    check(label + ": all contract fields present", not missing, str(missing))
    check(label + ": case_id echoed", result.get("case_id") == "fixture-methods-01",
          str(result.get("case_id")))
    check(label + ": unmodelled field is a typed placeholder",
          result.get("zzz_unmodelled_quota") == "",
          str(result.get("zzz_unmodelled_quota")))
    check(label + ": unmodelled field reported",
          "zzz_unmodelled_quota" in result["meta"]["contract_fields_not_reported"],
          str(result["meta"]["contract_fields_not_reported"]))
    mapping = result["meta"]["contract_field_mapping"]
    check(label + ": mapping proves reflection",
          mapping.get("chapter_body") == "methods_section"
          and mapping.get("hyperparameter_table") == "parameter_table"
          and mapping.get("number_provenance") == "traceability"
          and mapping.get("self_audit") == "quality"
          and mapping.get("author_followups") == "questions",
          str(mapping))


def assert_deliverables(out: Path, label: str) -> None:
    for name in ("result.json", "evidence.json", "run_report.json",
                 "methods_section.docx", "methods_section.md", "methods_section.html",
                 "parameter_table.csv", "author_questions.md"):
        check(label + ": " + name + " exists", (out / name).is_file())
    report = read_json(out / "run_report.json")
    check(label + ": report has budget fields",
          "elapsed_s" in report and report.get("model_requests_used") == 0,
          str(list(report.keys())[:12]))
    evidence = read_json(out / "evidence.json")
    check(label + ": evidence has grounding", "grounding" in evidence, str(evidence.keys()))


def test_prepare(work: Path) -> None:
    print("prepare")
    worksheet_path = work / "params_candidates.json"
    check("worksheet written", worksheet_path.is_file())
    worksheet = read_json(worksheet_path)
    params = worksheet.get("params") or []
    check("parameters extracted", len(params) >= 10, str(len(params)))
    check("every parameter carries file+line",
          all(item.get("source_path") and item.get("line", 0) > 0 for item in params),
          str([item for item in params if not item.get("line")][:2]))
    canon = {item["canonical"] for item in params}
    check("canonical names merged", {"learning_rate", "batch_size", "epochs", "seed"} <= canon,
          str(sorted(canon)))
    check("draft identified", str(worksheet["draft"]["path"]).endswith("methods_draft.md"),
          str(worksheet["draft"]["path"]))
    check("no false parameter conflicts", worksheet.get("param_conflicts") == [],
          str(worksheet.get("param_conflicts")))
    mentions = worksheet.get("draft_mentions") or []
    verdicts = {item["verdict"] for item in mentions}
    check("mismatch detected in draft", "mismatch" in verdicts, str(sorted(verdicts)))
    mismatched = [item for item in mentions if item["verdict"] == "mismatch"]
    check("mismatch bound to the right parameter",
          {item.get("bound_param") for item in mismatched} == {"learning_rate", "batch_size"},
          str([(item.get("bound_param"), item.get("value_raw")) for item in mismatched]))
    batches = sorted((work / "batches").glob("batch-*.json"))
    check("batch worksheets written", len(batches) >= 1, str(len(batches)))
    check("template worksheet written",
          (work / "generated" / "_template_methods.json").is_file())


def test_degraded(out: Path) -> None:
    print("degraded finalize (no generated content)")
    result = read_json(out / "result.json")
    check("status partial", result["meta"]["status"] == "partial", result["meta"]["status"])
    assert_contract(result, "degraded")
    limits = " ".join(result["meta"]["limits"])
    check("baseline degradation is declared", "deterministic baseline" in limits, limits[:400])
    check("chapter is the baseline", "Deterministic baseline" in result["chapter_body"],
          result["chapter_body"][:120])
    params = result["hyperparameter_table"]
    check("baseline parameter table is filled", len(params) >= 8, str(len(params)))
    check("baseline rows are traced",
          all(row.get("trace_status") == "traced" for row in params))
    check("limitations are present", len(result["scope_caveats"]) >= 1)
    check("questions are present", len(result["author_followups"]) >= 1)
    check("traceability records carry anchors",
          any(item.get("anchored") for item in result["number_provenance"]),
          str(len(result["number_provenance"])))
    check("structural claims are marked",
          any(item.get("kind") == "structural" for item in result["number_provenance"]))
    assert_deliverables(out, "degraded")


def test_generated(out: Path, work: Path) -> None:
    print("finalize with agent-generated content")
    result = read_json(out / "result.json")
    check("status partial because the draft conflicts", result["meta"]["status"] == "partial",
          result["meta"]["status"])
    assert_contract(result, "generated")
    check("chapter adopted from generated content",
          "We train ResNet-18" in result["chapter_body"], result["chapter_body"][:120])
    steps = result["process_steps"]
    check("three ordered steps", len(steps) == 3, str(len(steps)))
    check("steps keep all four fields",
          all(step.get("fields_complete") for step in steps), str(steps[:1]))
    check("step sources resolved",
          all(step.get("trace_status") == "source_located" for step in steps),
          str([step.get("trace_status") for step in steps]))
    params = {row["name"]: row for row in result["hyperparameter_table"]}
    check("traced parameter row", params.get("learning_rate", {}).get("trace_status") == "traced",
          str(params.get("learning_rate")))
    check("unsourced parameter row kept but flagged",
          params.get("warmup_steps", {}).get("trace_status") == "untraceable",
          str(params.get("warmup_steps")))
    check("question raised for the unsourced parameter",
          any("warmup_steps" in item["question"] for item in result["author_followups"]),
          str([item["question"][:40] for item in result["author_followups"]]))
    verdicts = [item["verdict"] for item in result["number_provenance"]]
    check("draft mismatches stay visible", verdicts.count("mismatch") >= 2, str(verdicts[:8]))
    check("untraceable numbers stay visible", "untraceable" in verdicts, str(sorted(set(verdicts))))
    audit = result["self_audit"]
    check("batch coverage complete", audit["batch_coverage"] == 1.0, str(audit["batch_coverage"]))
    check("delivered coverage complete", audit["delivered_coverage_ratio"] == 1.0,
          str(audit["delivered_six_element_coverage"]))
    check("fabricated section number detected",
          audit.get("generated_section_untraceable", 0) >= 1,
          str(audit.get("generated_section_untraceable")))
    check("section/material conflicts detected",
          audit.get("generated_section_mismatched", 0) >= 1,
          str(audit.get("generated_section_mismatched")))
    check("grounding re-verified in finalize", audit["grounding_rate"] >= 0.9,
          str(audit["grounding_rate"]))
    check("values are checked against their quotes",
          all(item.get("value_in_quote") is not False
              for item in result["number_provenance"] if item.get("value")),
          "value_in_quote False found")
    limits = " ".join(result["meta"]["limits"])
    check("generated conflicts reported in limits", "conflict with the materials" in limits,
          limits[:400])
    assert_deliverables(out, "generated")


def test_success(out: Path) -> None:
    print("clean case reaches success")
    result = read_json(out / "result.json")
    check("status success", result["meta"]["status"] == "success", result["meta"]["status"])
    assert_contract(result, "success")
    audit = result["self_audit"]
    check("no fabricated numbers in the chapter", audit["generated_section_untraceable"] == 0,
          str(audit["generated_section_untraceable"]))
    check("no chapter/material conflicts", audit.get("generated_section_mismatched", 0) == 0,
          str(audit.get("generated_section_mismatched")))
    check("no high-severity untraceable draft numbers",
          audit["draft_number_untraceable_high"] == 0, str(audit["draft_number_untraceable_high"]))
    counts = audit["parameter_trace_counts"]
    check("all parameters traced", counts.get("traced", 0) >= 4 and
          counts.get("untraceable", 0) == 0, str(counts))
    check("reproducibility checklist closed", audit["repro_missing"] == 0,
          str(audit["repro_missing"]))
    check("grounding rate is full", audit["grounding_rate"] == 1.0, str(audit["grounding_rate"]))


def test_missing_case(out: Path) -> None:
    print("missing case.json")
    result = read_json(out / "result.json")
    check("result still written", result.get("meta") is not None)
    check("contract not detected", result["meta"]["contract_detected"] is False,
          str(result["meta"].get("contract_detected")))
    limits = " ".join(result["meta"].get("limits") or [])
    check("missing contract is explained", "declared no output_contract" in limits, limits[:300])
    check("content preserved in additional outputs",
          bool(result["meta"].get("additional_outputs")), str(list(result["meta"].keys())))


def test_no_materials(out: Path) -> None:
    print("no materials")
    result = read_json(out / "result.json")
    check("status failed", result["meta"]["status"] == "failed", result["meta"]["status"])
    check("all contract fields still present",
          all(name in result for name in DECLARED),
          str([name for name in DECLARED if name not in result]))
    limits = " ".join(result["meta"]["limits"])
    check("missing materials explained", "no material files" in limits, limits[:300])
    check("no fabricated content", result["chapter_body"] == "" or
          "未能找到" in result["chapter_body"], result["chapter_body"][:120])
    check("evidence still written", (out / "evidence.json").is_file())


def test_draft_only(out: Path) -> None:
    print("draft-only case (no independent material)")
    result = read_json(out / "result.json")
    audit = result["self_audit"]
    check("draft numbers marked unverifiable, not invented",
          audit.get("draft_number_unverifiable", 0) >= 5,
          str(audit.get("draft_number_unverifiable")))
    check("no high-severity untraceable accusation",
          audit["draft_number_untraceable_high"] == 0,
          str(audit["draft_number_untraceable_high"]))
    limits = " ".join(result["meta"]["limits"])
    check("cross-check gap explained honestly", "unverifiable" in limits, limits[:400])
    check("chapter still produced", bool(result["chapter_body"].strip()))


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #
def main() -> int:
    if not SCRIPT.is_file():
        print("script not found: " + str(SCRIPT))
        return 1
    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    try:
        fixture = make_fixture(TMP / "a")
        out = fixture / "out"
        work = fixture / "work"
        run(["--prepare", "--out", str(out), "--work", str(work)], cwd=fixture)
        test_prepare(work)
        run(["--finalize", "--out", str(out), "--work", str(work)], cwd=fixture)
        test_degraded(out)
        write_generated(work, fabricated=True)
        run(["--finalize", "--out", str(out), "--work", str(work)], cwd=fixture)
        test_generated(out, work)

        print("determinism")
        before = (out / "result.json").read_bytes()
        run(["--finalize", "--out", str(out), "--work", str(work)], cwd=fixture)
        after = (out / "result.json").read_bytes()
        check("result.json is byte-identical across runs", before == after,
              "sizes " + str(len(before)) + " vs " + str(len(after)))

        clean_fixture = make_fixture(TMP / "b", clean=True)
        clean_out = clean_fixture / "out"
        clean_work = clean_fixture / "work"
        run(["--all", "--out", str(clean_out), "--work", str(clean_work)], cwd=clean_fixture)
        write_generated(clean_work, fabricated=False)
        run(["--finalize", "--out", str(clean_out), "--work", str(clean_work)],
            cwd=clean_fixture)
        test_success(clean_out)

        no_case = make_fixture(TMP / "c")
        (no_case / "case.json").unlink()
        run(["--all", "--out", str(no_case / "out"), "--work", str(no_case / "work")],
            cwd=no_case)
        test_missing_case(no_case / "out")

        empty = TMP / "d"
        write_json(empty / "case.json", {"case_id": "empty-case", "output_contract": CONTRACT})
        write_text(empty / "task.md", "# Task\n\nNothing to work with.\n")
        run(["--all", "--out", str(empty / "out"), "--work", str(empty / "work")], cwd=empty)
        test_no_materials(empty / "out")

        draft_only = make_draft_only(TMP / "e")
        run(["--all", "--out", str(draft_only / "out"), "--work", str(draft_only / "work")],
            cwd=draft_only)
        test_draft_only(draft_only / "out")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    if FAILS:
        print("")
        print("FAILED " + str(len(FAILS)) + " check(s):")
        for failure in FAILS:
            print(" - " + failure)
        return 1
    print("")
    print("all methods-experiments checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
