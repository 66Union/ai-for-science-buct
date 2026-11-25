# BUCT × Bohrium AI for Science — Agent Skills

Submission repository for the **Bohrium Campus Challenge · Beijing University of Chemical Technology station · Period 1**
(2026-11-23 → 2026-11-29). Ten challenges, one shared kernel, ten topic adapters.

> Every directory under `skills/` is an independently submittable Skill: platform
> loads it at `/skills/weekly-skill`, runs it against three hidden cases, and
> scores the produced `result.json`.

---

## What this repository is

The contest is not "write a paper with AI". It is "author a Skill that an Agent
can load and execute so it does a literature task measurably better than the bare
Agent". The scoring table is identical for all ten challenges:

| Dimension | Points | What actually earns it |
|---|---|---|
| 实际效果 Actual effect | 40 | correct, complete, **verifiable** structured output on 3 hidden cases |
| 使用增益 Improvement over baseline | 25 | measured against the same case run by the bare Agent |
| 创新增益 Novelty | 15 | capability the baseline provably lacks |
| 可靠体验 Reliability | 10 | easy to invoke, stable across repeats, honest failure handling |
| 生态贡献 Ecosystem | 10 | complete docs, reusable resources, shareable practice |
| GitHub link | +5 | a public link a reviewer can open while logged out |

So this repo optimises for three things a reviewer can check quickly:
**verifiability**, **honest degradation**, and **documentation completeness**.

## Layout

```
skills/
├── _core/                        shared kernel (not submitted on its own)
│   ├── lib/
│   │   ├── discover.py           input discovery — no hardcoded container paths
│   │   ├── pdftext.py            zero-dependency PDF text extraction (bbox, blocks)
│   │   ├── extract.py            engine probe chain + explicit degradation
│   │   ├── structure.py          sections / tables / formulas / references
│   │   ├── anchors.py            evidence anchors + quote re-verification
│   │   └── contract.py           result.json reflected from case.json contract
│   ├── scripts/run_reading.py    reading-chain orchestrator
│   └── tests/                    self-tests + synthetic fixture generator
└── reading-01-document-parsing/
    └── SKILL.md                  submission for 阅读01｜文献文档解析
```

## Design commitments

1. **Output shape is decided by the case, not by us.**
   `result.json` is generated from `case.json.output_contract` by stem-matching;
   fields we cannot produce are `null` and listed in
   `meta.contract_fields_not_reported`. A hardcoded schema would be wrong by
   construction on a hidden case.

2. **Evidence is computed, not asserted.**
   Every structural finding carries an anchor (page + block id + bbox + quote)
   which is **re-verified against the source text** before it is emitted.
   `verify` distinguishes `exact` / `normalised` / `block_only` / `page_only` /
   `missing`; anything that does not re-verify is flagged `ungrounded` and
   recorded under `meta.uncertainty` instead of being promoted to fact.

3. **Degrade explicitly, never silently.**
   The evaluation image's helper tools are not documented to us, so each engine
   is probed at runtime (PyMuPDF → pdfplumber → `pdf-text-tool`/`pdftotext` →
   builtin). The engine actually used and the resulting `anchor_quality`
   (`span` > `page` > `text`) are written into the result.

4. **Deterministic core.**
   Extraction and structuring make no model call and no network request, so three
   repetitions on one case produce the same output — which is what the
   "3 cases × 3 repeats" contract needs.

## Run it

```bash
# 0. optional: see which extraction engines this environment has
python skills/_core/lib/extract.py --probe

# 1. build the synthetic fixture (a 3-page paper, not a real publication)
python skills/_core/tests/make_fixture.py /tmp/fixture

# 2. run the reading chain inside that directory
cd /tmp/fixture && python <repo>/skills/_core/scripts/run_reading.py --out out

# 3. inspect
cat out/run_report.json   # status, limits, uncertainty, inputs actually read
cat out/result.json       # contract-shaped output
cat out/evidence.json     # per-claim anchor checks
```

## Self-tests

```bash
python skills/_core/tests/test_core.py
```

Covers text extraction with real coordinates, the extraction chain, anchor
verification (including the case where an **invented quote must fail** to
verify), and contract reflection with and without a declared contract.

## Notes on scope

- The synthetic fixture under `tests/` is explicitly a fixture, not a result.
  No official case output, score, or run log is reproduced here.
- Challenge statements belong to the organisers; this repository contains only
  our own code and documentation.
