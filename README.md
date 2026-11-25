# BUCT × Bohrium AI for Science — Agent Skills

Submission repository for the **Bohrium Campus Challenge · Beijing University of Chemical Technology station · Period 1**
(2026-11-23 → 2026-11-29). Ten challenges, one shared kernel, ten topic adapters.

> Every directory under `skills/` (except `_core`) is an independently submittable Skill:
> the platform installs it at `/skills/weekly-skill`, runs it against three hidden cases,
> and scores the produced `result.json`.

---

## What this repository is

The contest is not "write a paper with AI". It is "author a Skill that an Agent can load
and execute so it does a literature task measurably better than the bare Agent". The
scoring table is identical for all ten challenges:

| Dimension | Points | What actually earns it |
|---|---|---|
| 实际效果 Actual effect | 40 | correct, complete, **verifiable** structured output on 3 hidden cases |
| 使用增益 Improvement over baseline | 25 | measured against the same case run by the bare Agent |
| 创新增益 Novelty | 15 | a capability the baseline provably lacks |
| 可靠体验 Reliability | 10 | easy to invoke, stable across repeats, honest failure handling |
| 生态贡献 Ecosystem | 10 | complete docs, reusable resources, shareable practice |
| GitHub link | +5 | a public link a reviewer can open while logged out |

So the repository optimises for three things a reviewer can check quickly:
**verifiability**, **honest degradation**, and **documentation completeness**.

## Ten challenges, ten Skills

| # | Challenge | Skill directory | Core mechanism |
|---|---|---|---|
| 1 | 阅读01 文献文档解析 | [reading-01-document-parsing](skills/reading-01-document-parsing) | engine-probed PDF extraction → structure detection → anchor re-verification → contract reflection; fully deterministic |
| 2 | 阅读03 文献翻译 | [reading-03-translation](skills/reading-03-translation) | locked terminology + paragraph-level bilingual alignment; per-segment conservation checks (numbers, citations, formulas, hedges) and per-term consistency rate |
| 3 | 阅读10 文献总结与结构化笔记 | [reading-10-summary-notes](skills/reading-10-summary-notes) | section-aligned paper card + typed field notes, every note quote-grounded, extracted-fact inventory instead of a free-form summary |
| 4 | 阅读14 Nanopublication | [reading-14-nanopublication](skills/reading-14-nanopublication) | atomic assertion splitting, minimal evidence binding, controlled relation vocabulary, weak-evidence rejection, TriG + JSON-LD export |
| 5 | 阅读15 文献知识图谱构建 | [reading-15-knowledge-graph](skills/reading-15-knowledge-graph) | multi-paper entity/relation extraction with alias merging and a conflict registry, evidence-bound nodes and edges, real queries, GraphML/CSV export |
| 6 | 写作02 学术语言润色 | [writing-02-language-polish](skills/writing-02-language-polish) | minimal-edit polish with per-sentence change records; a conservation guard rolls back any edit that drops a number, citation, or negation; terminology/abbreviation audit |
| 7 | 写作04 参考文献与引用管理 | [writing-04-reference-management](skills/writing-04-reference-management) | BibTeX/RIS/EndNote/numbered parsing, explainable de-duplication, GB/T 7714-2015 and APA-7 formatting, renumbering + in-text marker rewrite, 10 consistency checks, sentence↔reference matching |
| 8 | 写作08 引言与相关工作写作 | [writing-08-intro-related-work](skills/writing-08-intro-related-work) | background / gap / contribution grounded in a citation inventory; grouped related work; claim→evidence binding; manuscript splice with docx/md/html output |
| 9 | 写作09 方法与实验章节写作 | [writing-09-methods-experiments](skills/writing-09-methods-experiments) | parameter extraction from config/log/code with file:line provenance, draft-number cross-check against materials (untraceable values are never invented), reproducibility checklist |
| 10 | 写作13 LaTeX 写作与编译 | [writing-13-latex-compilation](skills/writing-13-latex-compilation) | 14 classes of static checks, strict literal fix application (ambiguous matches rejected), engine probing + multi-round compile with BibTeX, log parsing down to file:line:root cause |

## Shared architecture

```
skills/
├── _core/                          shared kernel — not submitted on its own
│   ├── lib/
│   │   ├── discover.py             input discovery, no hardcoded container paths
│   │   ├── pdftext.py              zero-dependency PDF text extraction (bbox, blocks)
│   │   ├── extract.py              engine probe chain + explicit degradation + OOXML fallback
│   │   ├── structure.py            sections / tables / formulas / references / front matter
│   │   ├── anchors.py              evidence anchors + quote re-verification
│   │   ├── contract.py             result.json reflected from case.json's output_contract
│   │   ├── hybrid.py               two-phase framework: segments, batches, coverage, conservation, finalize
│   │   ├── docwrite.py             stdlib-only .docx / .md / .html writer
│   │   └── render.py               PDF/PNG preview rendering, probed and honestly degraded
│   ├── scripts/run_reading.py      deterministic reading chain
│   └── tests/                      self-tests + synthetic fixture generator
└── <ten topic directories>/        SKILL.md + scripts/ + tests/ + resources/
```

Nine of the ten Skills use the same **two-phase hybrid** contract:

1. **prepare** — deterministic. Discover inputs, extract text and coordinates, cut the
   document into addressable segments, extract the machine-checkable facts (numbers,
   units, citations, formulas, hedges, negations, candidate terms), and write small
   batch worksheets for the Agent. A placeholder `result.json` is written **before**
   any slow work, so an interrupted run still leaves a file.
2. **judgement** — the Agent writes one small JSON file per batch (translations, notes,
   assertions, entities, revisions, fixes...). This is the only part that needs a model.
3. **finalize** — deterministic again. Validate coverage and conservation, re-verify
   evidence anchors against the source, reflect `case.json.output_contract`, and write
   `result.json` / `evidence.json` / `run_report.json`. Missing judgements degrade to an
   honest deterministic baseline with `status: partial` instead of failing.

The reading-01 Skill is the fully deterministic variant of the same pipeline.

## Design commitments

1. **Output shape is decided by the case, not by us.**
   `result.json` is generated from `case.json.output_contract`; fields we cannot produce
   keep their declared type as a placeholder and are listed in
   `meta.contract_fields_not_reported`. A hardcoded schema would be wrong by construction
   on a hidden case.

2. **Evidence is computed, not asserted.**
   Every structural finding carries an anchor (page + block id + bbox + quote) that is
   **re-verified against the source text** before it is emitted: `exact` / `normalised` /
   `block_only` / `page_only` / `missing`. Anything that does not re-verify is flagged
   `ungrounded` and recorded under `meta.uncertainty` instead of being promoted to fact.
   The same idea is reused as a conservation guard in the writing skills: a rewrite that
   drops a number, citation, or negation is rolled back.

3. **Degrade explicitly, never silently.**
   The evaluation image's helper tools are not documented to us, so each engine is probed
   at runtime and the winner plus its `anchor_quality` (`span` > `page` > `text`) is written
   into the result. A missing renderer or compiler is reported as a limitation, never faked.

4. **Deterministic core.**
   Extraction, structuring, validation and assembly make no model call and no network
   request, so repeated runs on one case produce byte-identical output — which is what the
   "3 cases × 3 repeats" contract needs.

## Run a Skill locally

```bash
# 0. optional: see which extraction engines this environment has
python skills/_core/lib/extract.py --probe

# 1. build the synthetic fixture (a 3-page paper, not a real publication)
python skills/_core/tests/make_fixture.py /tmp/fixture

# 2. run a deterministic reading chain
cd /tmp/fixture && python <repo>/skills/_core/scripts/run_reading.py --out out

# 3. run a two-phase Skill: prepare -> (Agent writes work/generated/*.json) -> finalize
python <repo>/skills/reading-03-translation/scripts/run_translate.py --prepare --out out --work out/work
python <repo>/skills/reading-03-translation/scripts/run_translate.py --finalize --out out --work out/work

# 4. inspect
cat out/result.json        # contract-shaped output
cat out/evidence.json      # per-claim anchor checks
cat out/run_report.json    # status, limits, uncertainty, engines probed
```

## Acceptance gates (how each Skill was verified before submission)

| Tool | Question it answers |
|---|---|
| `skills/_core/tests/*.py` | do extraction, anchors, contract reflection and the hybrid helpers still hold? |
| `<skill>/tests/test_*.py` | does this topic's pipeline behave (coverage, conservation, degradation, determinism)? |
| `tools/package_skill.py` | does the flattened bundle respect the platform limits (≤50 UTF-8 files, ≤500 KB each, ≤2 MB total, no symlinks)? |
| `tools/audit_package.py` | is the bundle acceptable: frontmatter, docs depth, no developer paths, no credentials, no compiled bytecode? |
| `tools/e2e_check.py` | does the packaged runner, in an `/input`-shaped sandbox, always leave a valid `result.json` carrying every declared contract field — including the no-contract and no-material paths? |

`e2e_check` is the gate that matters most: it checks the one thing the platform enforces,
not our own tests.

## Notes on scope

- The synthetic fixtures under `tests/` are explicitly fixtures, not results. No official
  case output, score, or run log is reproduced here.
- Challenge statements belong to the organisers; this repository contains only our own
  code and documentation.
- The submission bundles are generated from these directories by `tools/package_skill.py`
  (the shared kernel is flattened into each bundle because the platform installs one
  directory); no bundle is stored in this repository.
