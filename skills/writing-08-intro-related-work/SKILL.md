---
name: writing-08-intro-related-work
description: Draft or rewrite an academic Introduction and Related Work section with citation-to-claim binding — build a page-anchored brief from the manuscript draft and the reference library, let the agent write background/problem/gap/contributions and a grouped comparison, then deterministically resolve every citation against the library, re-verify every claim against the manuscript, audit citation coverage and emit an editable manuscript whose result.json mirrors the case's declared output_contract. Use when a task asks to write, revise or check research background, related work, research gap, contribution statements or citation coverage.
version: 1.0.0
author: 66Union
license: MIT
tags: [academic-writing, introduction, related-work, citation-coverage, claim-evidence, research-gap, output-contract, two-phase]
---

# 引言与相关工作写作（Writing-08 Introduction & Related Work）

## 一、技能定位

本技能针对赛题「写作08｜引言与相关工作写作」的四个考察问题，每一条都落到**可执行、可复核**的动作上，而不是写作建议清单。

| 考察问题 | 本技能的做法 | 可验证的证据 |
|---|---|---|
| **立得住**（背景指向真实脉络与具体问题） | prepare 先把稿件切成带页码的段（seg），再抽出摘要、章节边界、贡献句候选、图表编号；Agent 写的每条 background 必须挂库内真实 ref_key，并指向一个 seg | `background[].citations_resolved` + `background[].evidence.anchor`（page + block_id + bbox + quote） |
| **分得清**（相关工作按维度分组比较） | related work 强制结构为 `groups[].dimension` + `works[].claim/limitation/compared_on`；同一维度下逐篇给"结论 + 局限 + 比较轴"，而不是逐篇摘要 | `related_work.groups`；每篇 work 的 `ref_check` + `flags` |
| **说得实**（空白由已引文献推出、断言有证据） | gap 必须写 `derived_from_refs`（必须真实存在于文献库）+ `manifestation`（稿件里能落地的位置）；贡献必须指向 seg / 逐字引文 / 图表标签 | `gap_analysis[].derived_from_refs`、`gap_analysis[].manifestation.ok`、`contributions[].manuscript_evidence.anchor` |
| **对得上**（贡献与图表实验一一对应、引用元数据逐条可核） | contributions 的每条证据回源校验；citation_coverage 逐条列出"库里有/稿件引了/新文本引了"，元数据缺年缺作者逐条记账 | `citation_coverage.per_key`、`claim_support_map.grounding`、`citation_coverage.format_consistency` |

**与"直接让模型写一段引言"的差别**：模型只负责**判断**（写哪几句、怎么分组、空白怎么表述），**事实搬运与核验全部由代码完成**。代码做四件事：① 把稿件切成带页码和 block_id 的可寻址单元；② 把文献库解析成带 ref_key 的清单并把 `[3]` / `(Smith, 2019)` / `\cite{smith2019}` 归一化成 key；③ 逐条回源校验（引文是否真在稿件的这个段里、ref 是否真在库里）；④ 按 `case.json.output_contract` 反射生成 result.json。任何一条核验失败都会被打上标记并计入 `limits/uncertainty`，不会被静默升级。

## 二、适用条件

| 项目 | 说明 |
|---|---|
| 输入 | `case.json`（含 `case_id` + `output_contract`，**必需**）、`task.md`、随题材料（稿件草稿 + 参考文献库，可选结果说明/相关文献 PDF） |
| 稿件格式 | `.md` / `.txt` / `.tex` / `.docx` / `.pdf`（扫描件除外）/ `.pptx`；多份材料时按"文件名含 draft/manuscript/paper/main > 后缀 > 体积"确定性排序取主稿 |
| 文献库格式 | BibTeX（`.bib/.bibtex/.nbib`）、RIS（`.ris/.enw`）、CSL-JSON（`.json` 数组或 `{items:[...]}`）、纯文本编号列表（`[1] ...` / `1. ...` 行）；可多文件合并 |
| 引用风格 | 数字（`[1,3-5]`）、作者-年（`(Smith et al., 2019)`）、LaTeX（`\cite{key}`）、直接写 bib key；混排会被检出并记录（不自动改写） |
| 语言 | 英文 / 中文稿件均可（章节标题词表含中英双语）；**不做翻译**，新写文本的语言由 Agent 决定 |
| 依赖 | **仅 Python 标准库**；PDF 提取在运行时探测 PyMuPDF/pdfplumber/pdftotext 并逐级降级到内置解析器；渲染预览探测 soffice/pandoc/pdftoppm 等，缺失时给 HTML 预览并如实记限制 |
| 篇幅 | 稿件 1 页 ~ 数百页；段长默认 1200 字符自动切段；单次运行通常 < 3 秒（无网络、无模型调用） |
| 不适用 | 需要真实联网检索新文献；需要 OCR 的纯扫描 PDF；需要期刊模板排版/LaTeX 编译；需要改写方法/实验章节；需要判断图表里的科学含义 |

## 三、调用方式

### 触发方式

当任务里出现"写引言 / 写相关工作 / 整理 related work / 补引用 / 核对贡献是否与结果对得上 / 这项工作与已有工作的区别是什么 / 研究空白"等指令，且提供了稿件与文献库时触发。技能本身不假设任何案例内容，全部从输入文件读取。

### 命令行（三个模式，默认 --all）

~~~bash
# 1) 确定性准备：发现输入 -> 解析稿件与文献库 -> 切段/抽事实 -> 写工作表
python scripts/run_intro.py --prepare  --out /app/output --work /app/work

# 2) Agent 判断：写 work/generated/intro.json 与 related_work.json（见第五节协议）

# 3) 确定性收尾：校验 -> 回源核验 -> 反射契约 -> 写 result.json / evidence.json / run_report.json
python scripts/run_intro.py --finalize --out /app/output --work /app/work

# 冒烟/兜底：一次跑完两段（没有 generated 内容时全部走确定性基线）
python scripts/run_intro.py --all      --out /app/output --work /app/work
~~~

| 参数 | 默认 | 说明 |
|---|---|---|
| `--prepare` / `--finalize` / `--all` | `--all` | 互斥三选一；不传等价于 `--all` |
| `--out` | `/app/output` → `/output` → `./output`（取第一个可写） | 产物目录；每个候选都做真实写探测，只读目录会被跳过 |
| `--work` | `/app/work` → `<out>/../work` → `./work` | 工作表与 `generated/` 所在目录 |
| `--input` | 自动发现 `/input`、`/app/input`、`/app/work`、`/app/environment`、`/app`，最后退到当前目录 | 指定材料目录（内部 chdir 后再发现，避免硬编码路径） |
| `--quiet` | 关 | 只打日志，不打印 JSON 摘要 |

退出码：`0` = 成功/部分成功（result.json 必然存在且含全部契约字段）；`1` = 顶层异常（仍会写出 `status=failed` 的 result.json，绝不零产出）。

### 产物

| 文件 | 内容 |
|---|---|
| `result.json` | `case_id` + 契约字段（按 `output_contract` 声明顺序）+ `meta`。声明了但产不出的字段按声明类型占位（array→`[]`、object→`{}`、string→`""`）并列入 `meta.contract_fields_not_reported`；未声明的产物放 `meta.additional_outputs` |
| `evidence.json` | 每条主张的锚点与校验结果、文献库清单（含被引/未被引/查不到）、ref 逐条核对、citation_coverage、生成文件覆盖情况、渲染探测结果 |
| `run_report.json` | status、耗时、limits/uncertainty/change_log、degraded、输入发现结果、generated 覆盖、产物路径 |
| `revised_manuscript.docx` / `.md` / `.html` / `revised_manuscript_full.md` | 可直接编辑的成稿：新写的 1 Introduction + 2 Related Work 已替换/插入原稿，附带引用覆盖附录与证据登记附录 |
| `preview/`（可选） | 有 soffice/pandoc 时渲染出的 PDF + 首页 PNG；没有渲染器时以 HTML 预览代替并记入 limits |

**启动即写占位 `result.json`**（`status=partial`），跑完覆盖；顶层再包一层 `BaseException` → 写 `status=failed`。这样即使 900 秒预算处被杀，产物仍在且带 case_id。

## 四、字段字典（内部值键 -> 契约字段）

result.json 的字段名由 `case.json.output_contract` 决定，**不允许硬编码**。技能内部先产出下面这组"值键"，再由 `hybrid.map_fields` 按"精确 -> 别名 -> 词干前缀/包含"打分映射到声明字段；映射结果写进 `meta.contract_field_mapping`，未命中的声明字段进 `meta.contract_fields_not_reported`。

| 内部值键 | 内容 | 常见声明字段名（别名，片段） |
|---|---|---|
| `introduction` | 四段合一的完整引言对象：`background/problem_statement/gap_analysis/contributions/text/baseline` | introduction, intro_text, introduction_section, section_one … |
| `background` | 背景条目数组，每条带 `citations_resolved/citations_unresolved/evidence` | opening_background, research_background, context, motivation, background_and_motivation … |
| `problem_statement` | 具体未解决问题（对象，含引文与证据） | problem, research_problem, problem_formulation, open_problem … |
| `gap_analysis` | 研究空白数组：`gap/derived_from_refs/refs_unresolved/manifestation/flags` | research_gap, gaps, gap_statement, knowledge_gap, empty_space … |
| `contributions` | 贡献数组：`contribution/manuscript_evidence/flags` | contribution, contribution_statements, novelty, novelty_statements, key_contributions … |
| `related_work` | 分组比较对象：`source/groups/dimensions/text/baseline`；每组 `dimension/why_this_dimension/works[]` | related_works, related_works_comparison, prior_art_survey, literature_review, state_of_the_art … |
| `citation_coverage` | 逐条覆盖表：`library_size/cited_by_manuscript/manuscript_coverage_rate/cited_in_generated/generated_coverage_rate/uncited_keys/generated_unused_keys/missing_tokens/per_key[]/format_consistency` | citation_audit, bibliography_audit, citation_report, reference_coverage, citation_check … |
| `citation_inventory` | 文献库清单（keys、numeric_index、来源文件、被引/未被引/查不到） | citations, bibliography, reference_list, references, references_library … |
| `claim_evidence` | 主张-证据表：`claims[]/grounding/ref_checks/ref_check_failures/register/counts` | claim_support_map, claim_support, evidence_map, evidence_register, grounding … |
| `revised_manuscript` | 成稿对象：`title/markdown/files{docx,markdown,html,full_markdown}/bytes/preview/removed_existing_sections/inserted_before_order/sections_inserted/word_count` | revised_draft, revised_introduction, revised_paper, updated_paper, manuscript_with_intro … |
| `quality` | 自检对象：`status_inputs/citation/claim_support/related_work/degraded/contract_fields_declared` | self_check, quality_report, run_quality, metrics, statistics |
| `evidence` | 证据摘要（生成覆盖、degraded、grounding、citation_coverage_rate） | evidence_anchors, anchors, provenance, sources |
| `figure_table_map` | 稿件中的图/表标签清单（label/kind/seg_id/page/context） | figure_table_inventory, figures_and_tables, labels |
| `literature` | 文献库条目（ref_key/title/authors/year/venue/doi）与来源 | library, ref_entries, parsed_references, bib_entries |
| `manuscript_sections` | 检测到的章节（canonical/name/seg_id/order/page） | sections, outline, section_structure |

写作相关的字段名均可被上面任一别名命中的情况**允许一个值喂多个声明字段**：映射按声明顺序进行，同分时先声明的先取（确定性），竞争失败的值会退回 `meta.additional_outputs` 而不是丢弃。

## 五、Agent 操作协议（精确到命令与 JSON）

预算提醒：整条流水线（prepare + finalize）只用 **0 次模型请求**；模型请求只花在写两个 JSON 文件上。建议节奏：读 brief（1 次）→ 写 intro.json（1-3 次）→ 写 related_work.json（1-3 次）→ finalize（1 次）→ 看 status 决定是否补写（不超过 2 轮）。

### 步骤 1：确定性准备

~~~bash
python scripts/run_intro.py --prepare --out /app/output --work /app/work
~~~

stdout 会打印 `segments / sections / library_size / batches / expected_generated` 以及下一步命令。然后按顺序读：

1. `work/brief.json` —— 唯一权威摘要：任务原文摘录、稿件标题/摘要/章节/已有引言与相关工作位置、`citation_inventory`（被引/未被引/查不到的 token）、`contribution_candidates`（带 seg_id 的贡献句候选）、`figure_tables`、文献库 keys、batch 工作表清单、以及**你要写的两个文件的 schema（schemas.intro / schemas.related_work）**。
2. `work/manuscript.json` —— `segments[]`：每段 `seg_id / page / section_title / text`。**所有证据必须用这里的 seg_id**。
3. `work/literature.json` —— `entries[]`（ref_key/title/authors/year/venue/doi/abstract）与 `numeric_index`（`数字编号 -> ref_key`）。
4. `work/batches/batch-XX.json`（可选）—— 每份含不超过 6 篇文献的元数据与摘要片段。

### 步骤 2：写 `work/generated/intro.json`

~~~json
{
  "background": [
    {"text": "一段背景（稿件语言），指向具体问题而不是泛泛重要性",
     "citations": ["[1]", "[2]", "smith2019"],
     "evidence_seg": "seg-0003"}
  ],
  "problem_statement": {
    "text": "具体未解决问题（一句话到一段）",
    "citations": ["[1]"],
    "evidence_seg": "seg-0007"
  },
  "gap_analysis": [
    {"gap": "由下列已引工作的共同限制推出的空白",
     "derived_from_refs": ["smith2019", "wang2018"],
     "manifestation": {"seg_id": "seg-0009", "quote": "稿件里逐字的句子"}}
  ],
  "contributions": [
    {"contribution": "可核对、不夸大范围的贡献条目",
     "manuscript_evidence": {"seg_id": "seg-0013", "quote": "稿件里逐字的句子"}},
    {"contribution": "依赖图/表的贡献",
     "manuscript_evidence": {"figure_or_table": "Figure 2"}}
  ]
}
~~~

字段规则：

- `background[].text`：必填；每条**至少一个能解析的引用**（bib key、数字编号、作者-年、LaTeX cite 都行），否则该条被标 `unsupported_citation` 并让 `status=partial`。
- `evidence_seg`：可选但计分；写 `segments[]` 里的 seg_id。也可写 `{"seg_id":..., "quote":...}` 对象。
- `gap_analysis[].derived_from_refs`：必填且必须真实存在于文献库（这是"空白由已引文献推出"的机器可判证据）。
- `gap_analysis[].manifestation`：空白在稿件中的落点（seg_id / 逐字 quote / block_id / 图表标签）。缺失只标 `gap_manifestation_missing`，不伪造。
- `contributions[].manuscript_evidence`：必填。三种形态任选：`seg_id 加 quote`、只有逐字 quote（会被定位到所在段）、`figure_or_table` 标签（走 structural 锚点）。解析不到即 `evidence_missing`。
- 不确定的内容宁可不写：空字段会被基线补位，但**编造**会让 ref/锚点核验直接失败并在 evidence.json 留证。

写完立刻自检（可选，成本很低）：

~~~bash
python -c "import json;d=json.load(open('work/generated/intro.json'));print({k:(len(v) if isinstance(v,list) else bool(v)) for k,v in d.items()})"
~~~

### 步骤 3：写 `work/generated/related_work.json`

~~~json
{
  "groups": [
    {"dimension": "比较维度（问题设定 / 方法路线 / 数据与评价方式）",
     "why_this_dimension": "为什么这些工作属于同一维度、为什么值得放在一起比",
     "works": [
       {"ref_key": "smith2019",
        "claim": "该工作真正确立了什么（转述，不改写事实）",
        "limitation": "它没有覆盖的条件 / 适用边界",
        "compared_on": "在这一维度上用来比较的量或设定"}
     ]}
  ]
}
~~~

- `dimension` 建议 2-4 组，每组至少 2 篇，避免"逐篇摘要拼接"。
- `ref_key` 必须在 `work/literature.json` 的 keys 里，否则该条被标 `ref_not_in_library` 并计入 `related_work_unknown_refs`。
- `limitation` 缺失不会让运行失败，但会记入 `uncertainty`（分组比较不完整）。
- 若文献库带 abstract，finalize 会算 `claim_overlap`（claim 内容词在 abstract 中的命中率）并在低于 0.2 时标 `claim_not_supported_by_abstract`，这是"转述是否与原文一致"最便宜的机器信号。

可选：把每份 `work/batches/batch-XX.json` 答成 `work/generated/batch-XX.json`（形如 `{"records":[{"ref_key":..., "dimension":..., "claim":..., "limitation":...}]}`）。没有 related_work.json 时，finalize 会用这些记录按 dimension 组装分组；两者都有时以 related_work.json 为准。

### 步骤 4：确定性收尾

~~~bash
python scripts/run_intro.py --finalize --out /app/output --work /app/work
~~~

stdout 打印 `status / degraded / contract_field_mapping / contract_fields_not_reported / field_fill_ratio / outputs`。然后看 `result.json` 的 `meta.limits` 与 `meta.uncertainty`：

- `status=success`：没有降级项，可以直接交付。
- `status=partial`：按 `meta.degraded` 里的标记定位问题（`intro_partial:gap_analysis` 表示 gap 走了基线、`unsupported_citation` 表示有条背景没有库内引用、`contribution_evidence_missing` 表示贡献证据落不了地）。**只补写被标的那一部分**，再跑一次 `--finalize`（幂等，可重复执行）。
- `status=failed`：没有可读稿件（或输入目录为空）。result.json 仍含全部契约字段占位与失败原因。

## 六、核心工作流（两个确定性阶段 + 中间一次判断）

### 阶段 1 prepare（确定性）

1. **发现输入**：扫 `/input`、`/app/input`、`/app/work`、`/app/environment`、`/app`（每个 root 都扫，最后退当前目录）；按特征识别 case（含 `case_id` 或 `output_contract` 的 JSON，容忍 BOM）与 task.md。
2. **材料分类**：文献库后缀（`.bib/.bibtex/.nbib/.ris/.enw`，其余文本按纯文本编号列表解析）、稿件后缀（`.md/.txt/.tex/.docx/.pdf/.pptx`），其余文本材料作为"结果说明"附在 brief 里（每条取前 500 字符）。
3. **稿件抽取**：PDF 走共享引擎链（PyMuPDF → pdfplumber → 外部 pdftotext → 内置解析器，逐级降级并记录 attempts）；`.docx` 用标准库 OOXML 逐段读取；纯文本按空行/标题行/表格行切块，每块给合成行范围 bbox 与 `anchor_quality=page`。
4. **结构抽取**：章节识别（英中标题词表 + 编号标题 + Markdown/LaTeX 标题），摘要定位，标题猜测，贡献句候选（we propose / we show / 我们提出 等线索词），图/表标签清单（Figure/Fig./Table/Scheme/Algorithm/图/表 加编号）。
5. **引用清点**：对每段抽 citation token（数字区间、作者-年、LaTeX cite），解析成 ref_key，得到"被引/未被引/查不到"三张清单；引用风格（numeric / author-year / latex / 混排 / none）写进 brief。
6. **相关文献 PDF**（若随题下发）：最多 3 篇，抽出首页文本与摘要片段，按标题词重叠度与文献库条目配对（重叠度至少 0.5 才写入该条 abstract，来源记 `abstract_source`）。
7. **写工作表**：`brief.json`（含两个生成文件的 schema）、`literature.json`、`manuscript.json`（segments 全文）、`batches/batch-XX.json`（每份不超过 6 篇）、`manifest.json`（期望的 generated 文件清单）。

### 阶段 2 judgement（Agent）

按第五节写 `generated/intro.json` 与 `generated/related_work.json`（可选 batch 记录）。平台预算 64 次模型请求 / 900 秒 / 单响应 16384 tokens；本阶段建议不超过 8 次请求。

### 阶段 3 finalize（确定性）

1. **重新发现与抽取**（不依赖 prepare 的产物：即使 brief 被删也能跑）。
2. **读 generated**：逐文件读 JSON（容忍 BOM / JSONL / records 包装）；坏 JSON 记 `broken_generated:文件名` 并降级。
3. **归一化引文**：ref_key、数字编号、编号区间（区间限幅 20）、作者-年、LaTeX cite → 库内 key；解析不到写 `citations_unresolved`。
4. **证据回源**：为每条主张生成 `anchors.make_anchor(page, block, quote)`（quote 取自该段或 Agent 给的逐字引文），再 `anchors.verify_all` 回源校验，区分 `exact / normalised / block_only / page_only / missing`；位置型证据（图表标签）用 `kind=structural`，只要求位置成立。
5. **硬判定**：`located`（引文是否真在稿件的这个段里）驱动 status；`supported_by_quote`（引文是否提到主张的关键词）单独统计为严格 `grounding_rate`——新写的句子通常无法被旧句子逐词覆盖，所以它只作质量信号，不冒充失败。
6. **引用覆盖审计**：逐 entry 标 `cited_by_manuscript / manuscript_count / cited_in_generated`，另给 `uncited_keys`、`generated_unused_keys`、`missing_tokens`（稿件引了库里没有的编号）与格式一致性检查（无年/无作者/混排风格）。
7. **成稿装配**：若原稿已有 Introduction / Related Work，按其段集合**整段替换**（不重复插入）；否则插入到第一个正文节（method/experiments/results 等）之前，其次是摘要之后。原稿其余章节逐段保留（标题加 `##` 层级）。
8. **可编辑产物**：docx + md + html + full.md（写 docx 后固定 zip 时间戳，保证重复运行字节一致）＋渲染预览（有渲染器给 PDF/PNG，否则 HTML 预览加 limits）。
9. **契约反射**：`hybrid.finalize` → `contract.build_result` 按声明顺序输出，缺值占位并记账，写 `result.json / evidence.json / run_report.json`。
10. **状态判定**：`degraded` 为空才是 `success`；任何降级或硬检查失败 → `partial`（并在 limits 第 1 条说明）；无可读稿件 → `failed`（仍写全部契约字段占位）。

## 七、异常处理

| 异常情况 | 检测方式 | 处理策略 | 状态标记 |
|---|---|---|---|
| 找不到 case.json | 发现阶段为空 | 仍完整运行；`contract_detected=false`，全部内容进 `meta.additional_outputs` | partial |
| 输入目录里没有稿件 | 材料分类后 manuscript 为空 | 不写正文，仍反射契约（全字段占位）并说明原因 | **failed** |
| 扫描件无文字层 | `text_layer=false` | 不臆造文本，按"无稿件"处理并提示需要 OCR | failed |
| 没有文献库 | `library_size=0` | 记录无库；background 无可解析引用即标 `unsupported_citation`；related work 只能给标题基线 | partial |
| 单个文献库文件解析失败 | 每条来源的 `warning` | 跳过该文件继续用其余来源，来源清单里保留失败原因 | 视剩余库而定 |
| generated 文件缺失 | 扫描 `work/generated/` | 对应部分用确定性基线（摘要抽取式背景 / 标题关键词共现分组），degraded 记录 | partial |
| generated JSON 损坏 | 读入返回空 | 记 `broken_generated:文件名`，该部分走基线，不抛错 | partial |
| 背景/问题陈述的引用查不到 | `citations_unresolved` 非空且无 key 命中 | 条目 flag `unsupported_citation` / `citation_not_in_library` | partial |
| 稿件自身引了库里没有的编号 | `missing_tokens` | 写入 `citation_coverage` 与 uncertainty（输入侧缺陷，只报告不修改） | 状态不变 |
| gap 没有 derived_from_refs | 字段为空/全查不到 | flag `gap_without_reference` | partial |
| gap 的落点找不到 | `manifestation` 解析失败 | flag `gap_manifestation_missing`，不伪造位置 | partial |
| 贡献证据落不了地 | `manuscript_evidence` 解析失败 | flag `evidence_missing` | partial |
| 相关工作的 ref_key 不在库 | `ref_check.exists=false` | flag `ref_not_in_library`，计入 `related_work_unknown_refs` | partial |
| 相关工作缺 limitation | 字段为空 | 只记 uncertainty（分组比较不完整），不伪造局限 | 状态不变 |
| 引文被改写到无法逐词命中 | `supported_by_quote=false` | 记录严格 `grounding_rate` 与"转述可疑"提示；只要引文仍能在段内定位就不算失败 | 状态不变 |
| 图/表标签在稿件里不存在 | 与标签清单比对失败 | 视为 `evidence_missing` | partial |
| 找不到 office/TeX 渲染器 | `render.probe` 为空 | 写 HTML 预览并在 limits 说明无 PDF/PNG | 状态不变 |
| 可编辑产物写入失败 | 捕获异常 | 记 `deliverable_writer_failed`，markdown 正文仍在结果对象里 | partial |
| 超时/容器被杀 | 900 秒预算 | 启动即写占位 result.json；顶层 BaseException 写 failed | partial / failed |
| 重复运行 | 无随机数、无时间戳、docx 固定 zip 时间 | result.json / evidence.json / docx 字节一致（已自测） | — |

## 八、限制与边界

1. **不做 OCR**：纯扫描 PDF 没有文字层，本技能明确返回 failed 而不是编造内容。
2. **不联网检索**：只用随题下发的文献库；"近期进展是否漏引"只能基于给定库判断。
3. **数字引用按库内顺序解析**：`[1]` 指文献库第 1 条（IEEE 假设）。若原作者编号与库顺序不一致，会解析成错误条目——`numeric_index` 与 `citation_inventory` 都在 evidence.json 里，可人工复核；混排风格只报告不自动改写。
4. **转述忠实度只能弱核验**：有 abstract 时算 `claim_overlap`（内容词命中率），没有 abstract 时只能复述标题（`abstract_source=none`）。真正逐字比对需要原文全文，这一点如实写进 evidence。
5. **纯文本/Word 的 bbox 是合成值**：一行=1 单位高度，`anchor_quality=page`；PDF 才有真实坐标（`anchor_quality=span`）。校验用段文本包含关系，不用像素距离。
6. **相关工作分组质量取决于 Agent**：缺 generated 时的基线只按标题关键词共现分组，claim 只能是标题、limitation 为空（会被标出来）。
7. **不做期刊模板/格式排版**：`target_journal_style` 这类字段给类型占位并列入 not_reported。
8. **docx 写入器只支持小词汇**（title/heading/para/list/table/page_break），不是通用 Word 库；复杂版式（分栏、脚注、公式）不会被保留。
9. **只替换/插入引言与相关工作两节**，原稿其他章节逐段照抄（标题统一加 `##` 层级）。
10. **不翻译**：新写文本的语言由 Agent 依稿件语言决定；技能只做结构与核验。
11. **不判断科学正确性**：只判断"引文是否存在、是否落在该段、贡献是否指向稿件某处"，不判断论断是否科学成立。

## 九、失败处理与自查

| 现象 | 自查方法 | 处理 |
|---|---|---|
| `status=partial` 但看不出原因 | 读 `meta.degraded` 与 `meta.limits[0]` | degraded 每个标记对应一个可补写的文件或字段 |
| 有背景条目被标 `unsupported_citation` | 看该条的 `citations_requested` 与 `citations_unresolved` | 换成 `work/literature.json` 里真实存在的 key 或 `[n]` |
| `citation_coverage.missing_tokens` 非空 | 这是**输入侧**问题：稿件引的编号不在库里 | 补全文献库（不是改结果）；技能只如实报告 |
| `citation_coverage.uncited_keys` 很长 | 库里有工作完全没被新文本用到 | 视需要补写 background/related work，或接受（并非所有库条目都必须引用） |
| `claim_support_map.grounding.location_rate` 小于 1 | 看 `per_claim` 里 `located=false` 的 claim | 该证据的 quote 不在稿件里——改成稿件里的逐字句子，或改用 seg_id |
| 严格 `grounding_rate` 低于 `location_rate` | 说明新写的句子与所引句子用词差异大 | 正常现象，只在需要更强证据时换更贴切的 quote |
| `gap_statement` 里出现 "Gap analysis was not produced" | 说明没写 gap，走了基线 | 补写 `gap_analysis` 后重跑 `--finalize` |
| `related_works_comparison.baseline=true` | 说明没写 related work 或格式不可用 | 按第五节 schema 补写 `generated/related_work.json` |
| 成稿里出现两段 Introduction | 理论上不会（按已有章节整段替换），若出现说明章节没被识别 | 在 brief 里看 `manuscript.sections`；可手动补 `## Introduction` 标题让识别成立 |
| `revised_introduction.files.docx` 为 null | 看 `meta.limits` 里的 writer 失败原因 | markdown 正文仍可用；也可只用 `.md/.html` |
| 预览没有 PNG | 看 `evidence.render_probe` | 环境无渲染器（正常）；HTML 预览可看 |
| 结果字段名对不上题目要求 | 看 `meta.contract_field_mapping` 与 `contract_fields_not_reported` | 若题目把字段名取得很偏，可在 `SYNONYMS` 里加别名（不要改内部值键） |

自测命令（三份，全部零依赖、全部在工作区内建临时目录并自动清理）：

~~~bash
python tests/test_intro.py     # 本技能自测：库解析/引用解析/端到端完整/降级/失败路径
python tests/test_hybrid.py    # 共享两阶段框架自测
python tests/test_core.py      # 共享内核自测（契约反射、锚点校验等）
~~~

## 十、最小调用示例（示意，非正式案例结果）

**输入**（`tests/make_intro_fixture.py` 生成的合成样例，不是真实文献）：

~~~
fixture/
├── case.json          # case_id=writing08-demo-002，10 个契约字段（字段名故意与内部命名不同）
├── task.md            # 写作要求：背景/相关工作分组/空白推导/贡献可核对/证据可追溯
├── draft.md           # 合成稿件：Abstract + Introduction + Related Work + Methods + Results + Conclusion + References
├── refs.bib           # 6 条 BibTeX（其中 li2022 未被引用；稿件里还有一条 [9] 不在库中）
└── notes_results.md   # 结果说明（表格/图编号）
~~~

**调用**：

~~~bash
python scripts/run_intro.py --prepare  --out out --work work --input fixture
# -> 写 work/brief.json / literature.json / manuscript.json / batches/batch-01.json
# -> 并把模拟的 generated/intro.json + related_work.json 放进 work/generated/
python scripts/run_intro.py --finalize --out out --work work --input fixture
~~~

**示意输出**（真实运行结果的裁剪，**这是示例，不是正式案例结果**）：

~~~json
{
  "case_id": "writing08-demo-002",
  "opening_background": [
    {"id": "bg-1",
     "text": "Supported nickel catalysts are cheap and active, but their practical use is limited by sintering of the nickel particles above 973 K.",
     "citations_requested": ["[1]", "[2]"],
     "citations_resolved": ["smith2019", "chen2020"],
     "citations_unresolved": [],
     "evidence": {"ok": true, "how": "seg_id"},
     "flags": []}
  ],
  "related_works_comparison": {
    "source": "generated/related_work.json",
    "groups": [
      {"dimension": "particle sintering and deactivation under reforming conditions",
       "works": [{"ref_key": "smith2019", "claim": "particle growth above 973 K halves the active surface", "limitation": "does not separate particle size from support effects", "compared_on": "operating temperature", "flags": []},
                 {"ref_key": "garcia2021", "claim": "operando scattering resolves particle growth", "limitation": "only the first hours and one catalyst", "compared_on": "time on stream", "flags": []}]},
      {"dimension": "coke formation and kinetic description",
       "works": [{"ref_key": "wang2018", "limitation": "dry reforming only"}, {"ref_key": "kumar2017", "limitation": "assumes a constant active area"}]}
    ],
    "dimensions": ["particle sintering and deactivation under reforming conditions", "coke formation and kinetic description"],
    "baseline": false
  },
  "gap_statement": [{"id": "gap-1", "gap": "none of the cited works reports a size-selectivity relation at 750 K", "derived_from_refs": ["smith2019", "wang2018", "garcia2021"], "refs_unresolved": [], "flags": []}],
  "citation_audit": {"library_size": 6, "cited_by_manuscript": 5, "manuscript_coverage_rate": 0.8333,
                     "cited_in_generated": 5, "generated_coverage_rate": 0.8333,
                     "uncited_keys": ["li2022"], "missing_tokens": ["[9]"], "citation_style": "author-year+numeric"},
  "claim_support_map": {"grounding": {"claims_total": 7, "located": 7, "location_rate": 1.0,
                                      "strict_grounding_rate": 0.7143, "anchor_quality": "page"}},
  "revised_introduction": {"title": "A Low-Temperature Nickel Catalyst for Selective Methane Reforming",
                           "word_count": 647, "sections_inserted": ["1 Introduction", "2 Related Work"],
                           "removed_existing_sections": ["Introduction", "Related Work"],
                           "files": {"docx": "out/revised_manuscript.docx", "markdown": "out/revised_manuscript.md", "html": "out/revised_manuscript.html", "full_markdown": "out/revised_manuscript_full.md"}},
  "self_check": {"degraded": [], "citation": {"missing_tokens": 1},
                 "claim_support": {"claims": 7, "location_rate": 1.0, "strict_grounding_rate": 0.7143}},
  "evidence_anchors": {"degraded": [], "grounding": {"claims_total": 7, "located": 7}},
  "target_journal_style": {},
  "meta": {"status": "success", "contract_fields_not_reported": ["target_journal_style"],
           "field_fill_ratio": 0.9,
           "contract_field_mapping": {"opening_background": "background",
                                      "related_works_comparison": "related_work",
                                      "contribution_statements": "contributions",
                                      "gap_statement": "gap_analysis",
                                      "citation_audit": "citation_coverage",
                                      "claim_support_map": "claim_evidence",
                                      "revised_introduction": "revised_manuscript",
                                      "self_check": "quality",
                                      "evidence_anchors": "evidence"}}
}
~~~

产物文件（真实运行，示例）：`result.json` 约 78 KB、`evidence.json` 约 22 KB、`run_report.json` 约 3 KB、`revised_manuscript.docx` 3.4 KB / `.md` 3.3 KB / `.html` 4.5 KB / `revised_manuscript_full.md` 4.3 KB。整条链路耗时 < 1 秒、模型请求 0 次（只有写 generated 的两个文件消耗模型请求）。

## 十一、架构参考与差异说明

**两阶段混合架构**（prepare 确定性 / judgement 由 Agent / finalize 确定性）来自本项目的共享内核 `skills/_core/lib/hybrid.py`：把"搬运与核验"留给代码，把"判断与措辞"留给模型，从而把 64 次模型请求预算几乎全部花在写作上。文档组织参考了平台公开技能 `scientific-paper-translator`（章节编排、异常处理表、核验报告、限制与自查）与 `bohr-cli`（"踩坑"章节），只借鉴结构，未复制内容或代码。

| 维度 | 通用对话模型 / 文献管理工具草稿 | 本技能 |
|---|---|---|
| 事实来源 | 靠模型记忆与上下文，容易被"发明引用"污染 | 引用必须解析到库内 key，解析不到直接标 `unsupported_citation` |
| 论断与证据 | 模型自述"已核对" | 代码回源断言：quote 必须能在该 page/block 里定位，失败标 `ungrounded` 并留证 |
| 相关工作组织 | 逐篇摘要/时间线罗列 | 强制 `dimension + claim/limitation/compared_on`，缺 limitation 会记 uncertainty |
| 研究空白 | 常写"近年来受到广泛关注"式空转，或凭空断言空白 | 空白必须列出 `derived_from_refs`（库内真实存在）并与稿件落点对齐 |
| 贡献表述 | 自由发挥，容易夸大 | 每条贡献必须指向 seg / 逐字引文 / 图表标签，核验不过即 partial |
| 输出契约 | 固定 JSON schema | 从 `case.json.output_contract` 反射；缺值按声明类型占位并记账 |
| 重复运行可比 | 温度采样导致不同输出 | 全流程确定性（无随机、无时间戳、docx 固定 zip 时间），result/evidence/docx 字节一致 |
| 被杀风险 | 无处理，可能零产出 | 启动即写占位 result.json + 顶层 BaseException 兜底 |

可复用资源：`scripts/run_intro.py`（适配层，含 BibTeX/RIS/CSL-JSON 解析器、引用解析器、证据回源、成稿装配）、`tests/make_intro_fixture.py`（合成案例生成器，也可当作 Benchmark 模板）、`tests/test_intro.py`（覆盖完整/降级/失败三条路径的自测）。新的写作题可以直接复用 prepare 的"发现+切段+抽事实"与 finalize 的"契约反射+锚点核验"骨架。

## 十二、踩坑（`--help` 不会告诉你的）

1. **`%TEMP%` 可能不可写**：受限沙箱下 `tempfile.TemporaryDirectory()` 直接 PermissionError。自测的临时目录一律建在包内 `_selftest_tmp`，`finally` 里删干净。
2. **不要 `pip install` 补依赖**：受限环境装包会因元数据写入被拒而失败，本技能刻意只用标准库。
3. **Windows 的 JSON 可能带 BOM**：PowerShell `Out-File -Encoding utf8` 写出 UTF-8 BOM，`json.loads` 会直接失败（表现为"这个 case 没声明契约"、字段全 null）。所有读 JSON 的位置都走 `utf-8-sig`。
4. **docx 默认不确定**：`zipfile.writestr` 用当前时间做 zip 时间戳，内容相同字节不同。写完 docx 后用固定 `date_time` 重写一遍，三份重复运行才可比。
5. **纯文本材料只会给出一个巨大 block**：共享提取器把整篇文本塞进一个块，锚点粒度变成"整篇"；所以纯文本/Word 路径在本适配层里自己按空行、标题行、表格行切块，才能给出段落级锚点。
6. **BibTeX 不能用"按逗号切分"解析**：`title = {Nickel, coke and steam}` 里的逗号会把字段切碎。必须做括号深度感知的切分与配对花括号定位。
7. **数字引用区间要限幅**：`[3-5]` 展开成 3,4,5 是对的，但 `[1-99]` 会把整库当引用；超过 20 个的区间按字面处理并留待人工确认。
8. **作者-年可能有歧义**：同一作者同年多篇时取 key 字典序最小者，并把 `ambiguous` 记录写进解析详情（可复核，不静默选一个）。
9. **章节识别前先清洗标题**：`## 1 Introduction` 直接匹配"introduction"会失败；要先去 Markdown/LaTeX 标记与编号，再匹配中英词表。
10. **成稿替换要幂等**：先把原稿已有 Introduction/Related Work 覆盖的段集合整体移除，再在最早被移除段的顺序位插入新内容；否则重复运行会堆出两段引言。
11. **只靠内部命名映射会漏字段**：`map_fields` 的精确匹配要求内部键名与声明名相同，所以额外维护 `SYNONYMS` 别名表，并用"字段名故意与内部命名不同"的合成 case 证明反射真的生效（自测里逐字段断言 mapping）。
12. **图表标签证据没有文本锚点**：标签本身不在正文句子里，所以要把清单里该标签所在段的 context 当 quote、用 `kind=structural` 只校验位置；否则这类贡献会被误判为 `evidence_missing`。
13. **不要用"文本里是否有连续空格"判断表格**：上游已 `\s+` 归一化，这个判据永远不会命中（死代码）。表格/图表证据统一走编号正则 + 标签清单。
