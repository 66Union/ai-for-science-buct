---
name: writing-04-reference-management
description: Reference-list and in-citation management for a manuscript plus a bibliography - renumber in-text markers in first-appearance order (or convert numeric <-> author-year), rewrite the reference list in GB/T 7714-2015 or APA 7th, merge duplicates with explainable criteria, check text/list consistency and per-sentence claim support, then emit a contract-shaped result.json with editable manuscript and bibliography artifacts. Use when a task asks to fix citations, renumber references, dedupe a library, convert a citation style, or verify that each cited claim is actually supported. Runs offline with the standard library: prepare (deterministic) -> agent judgement file -> finalize (deterministic).
version: 1.0.0
author: 66Union
license: MIT
tags: [reference-management, citation-renumbering, bibliography-dedup, gbt7714, apa7, claim-support, evidence-anchors, output-contract]
---

# 参考文献与引用管理（Writing-04 Reference & Citation Management）

## 一、技能定位

本技能处理"一份可编辑稿件 + 一份文献库"的引用整理任务，交付**引用标记已同步的稿件**与**同一编号体系的文献表**，并把每一步判断变成可独立复核的证据。四个考察问题与本技能的对应关系：

| 考察问题 | 本技能的做法 | 可验证的证据 |
|---|---|---|
| **挂得对**（引用是否插到需要来源的句子） | 句子级引用位置索引：每条正文标记落到 句 id + 页 + block + bbox + 引文；无引用但含事实性论断的句子进入"待插引"清单并给出候选文献 | citation_map[].page/block_id；insertion_suggestions[]；evidence.json.grounding.per_claim[] |
| **编得顺**（编号与文末列表同步） | 按**正文首次出现顺序**重排编号，重写正文标记并重建文献表；支持 顺序编码制 <-> 著者-出版年 双向转换、同年 a/b 消歧、连续区间合并（[1-3]） | citation_map[] 的 before/after；changes[] 的 citation_marker 记录；consistency 10 项检查 |
| **收得净**（重复文献归并） | 自写四种导出格式解析器（BibTeX/RIS/EndNote/编号纯文本），按 DOI、规范化题名、预印本-正式版、近同题+同作者 等**显式判据**合并；证据不足一律进"待人工确认"而**不自动合并** | duplicates.groups[].criteria/confidence/rationale/field_fills；duplicates.needs_review[]；duplicates.conflicts[] |
| **对得上**（论断与文献是否匹配） | 逐句做确定性词面/作者匹配基线，并**校验 Agent 的逐句判断**（未知 ref_key、未知 relation 直接拒绝）；引用条目与正文句都回源校验锚点 | claim_support[].matched_refs[].baseline_relation；evidence.json.judgement.invalid[]；entry_grounding |

**与"让模型说自己核验过了"的区别**：本技能把核验写成**可执行断言**——引文必须能在抽取出的源文本块里被找到（exact/normalised/block_only/page_only/missing），未落地的主张会被打上 ungrounded 而不是悄悄升级成已核验。result.json 由 case.json.output_contract **反射**生成，字段名不写死。

**两阶段混合架构**（关键设计，直接决定 900 秒预算内能否产出）：

1. **prepare（确定性，零模型请求）**：发现输入 → 抽取正文/文献库 → 解析引用与条目 → 检测重复 → 建句子级引用索引 → 写工作表与 work 快照 → **立刻写一份完整的基线 result.json**。
2. **Agent 判断（少量模型请求）**：只读少量工作表，写一个小 JSON 判断文件（默认 work/generated/claim_support.json）。
3. **finalize（确定性，零模型请求）**：合并判断、重排编号、按目标规范格式化、回源校验锚点、反射契约、写 result/evidence/run_report 与可编辑产物。

即使 Agent 完全不写判断文件，--prepare 已经产出契约形状完整的 status=partial 结果；--finalize 会降级为确定性基线并把缺口写进 meta.limits，**任何情况下都不会因为"缺生成内容"而崩掉或产出零字节**。

## 二、适用条件

| 项目 | 说明 |
|---|---|
| 输入格式（正文） | .md / .markdown / .txt / .tex / .latex / .docx / .pdf / .pptx；.docx/.pptx 用标准库 OOXML 兜底读取 |
| 输入格式（文献库） | .bib / .bibtex / .ris / .nbib / .enw / .endnote / 含条目数组的 .json / 编号纯文本参考文献表；**稿内**"参考文献"章节也会被解析 |
| 引用标记 | LaTeX cite 族（含 upcite、supercite 等）、数字方括号（[1]、[1,3]、[1-3]、全角括号）、上标数字（Unicode 上标与 ^[n]）、著者-出版年（(Smith, 2020)、(Smith et al., 2019)、Smith (2020)、中文（张三，2020）） |
| 目标规范 | **GB/T 7714-2015（顺序编码制）** 与 **APA 第 7 版（著者-出版年）**；由 task.md/case.json 中的明确指令决定，否则按语种与现有标记风格推断，也可用 --style 强制 |
| 语言 | 中英文（可混排）；作者姓名不做拼音互转，中英文题名不机械合并 |
| 篇幅 | 正文数万字符、文献条目数百条规模；处理时间为秒级 |
| 依赖 | **仅 Python 标准库**，无网络、无第三方包、不调用模型 API；输出确定性（重复运行一致） |
| 不适用 | 需要联网核实 DOI/真实性的场景；无文字层的扫描 PDF（无 OCR）；图表公式交叉引用（"图 1/表 2/式 (3)"）的重新编号；纯题录管理系统的增删改；需要语义级"结论是否被文献支持"的判定（本技能只给词面/作者基线与 Agent 判断框架，不假装理解因果） |

## 三、调用方式

### 触发方式

当任务要求：整理参考文献、重排引用编号、正文与文献表同步、合并重复文献、BibTeX/RIS/EndNote 与 GB/T 或 APA 互转、检查"正文论断-所引文献"是否对应、给出可继续编辑的稿件与文献表时触发。典型指令包括"把引用改成 GB/T 7714""按 APA 第 7 版整理参考文献""合并重复文献""正文编号和文献表对不上""逐句核对引用是否支撑论断"。

### 运行时输入

评测环境提供（路径**不硬编码**，按特征发现）：

- case.json：含 case_id 与 output_contract（唯一输出契约来源）；
- task.md：任务说明与目标规范线索；
- 材料文件：正文、文献库、补充说明，位置不限（/input、/app/input、/app/work、/app/environment、/app 都会扫描；也可用 --input 指定目录）。

### 输出（默认写 /app/output，可 --out 覆盖）

| 文件 | 内容 |
|---|---|
| result.json | **契约字段 + case_id + meta**；声明字段按声明顺序输出，缺值按声明类型占位并记入 meta.contract_fields_not_reported；meta 记录 status/limits/uncertainty/change_log/contract_field_mapping/additional_outputs/output_files |
| evidence.json | 每句主张、每条引用标记、每个文献条目的锚点与回源校验结果；重复组判据；生成判断的接收/拒绝记录 |
| run_report.json | 阶段、耗时、预算（模型请求=0）、文献库来源、各类计数、一致性检查、渲染探测、限制与不确定性 |
| revised_manuscript.md/.docx/.html | **可继续编辑的稿件**（引用标记已重写、文献表已替换）；HTML 为无需渲染器的预览 |
| references.md/.bib/.ris | 同步的文献表三种形态（人读版 / BibTeX 导出 / RIS 导出） |
| change_log.md | 逐条改动记录（引用标记、合并、格式转换、文献表重建） |
| work/ | 中间产物：citation_index.json、bib_entries.json、style.json、batches/batch-XX.json、generated/（Agent 写入）、citation_state.json |

### 三个运行模式

@```bash
python scripts/run_reference_management.py --prepare    # 确定性阶段 + 基线 result.json
python scripts/run_reference_management.py --finalize   # 合并 Agent 判断 + 最终产物
python scripts/run_reference_management.py --all        # 不读判断文件，prepare+finalize 一次跑完（默认）
@```

| 参数 | 默认 | 说明 |
|---|---|---|
| --input DIR | 运行时发现 | 输入目录（相对路径按当前目录解析）；缺省扫描评测输入根 |
| --work DIR | /app/work（不可写则 work） | 工作表与状态目录；Agent 的判断文件写在这里 |
| --out DIR | /app/output | 结果目录（有写探测，只读目录会被跳过） |
| --style gbt7714-2015 或 apa7 | 自动推断 | 强制目标规范 |
| --no-baseline | 关 | prepare 只写工作表与占位 result.json，不写基线完整结果 |

退出码：0 = 正常产出（状态见 result.json.meta.status）；1 = 顶层异常，但仍会写 status=failed 的 result.json。**不要手动编辑 result.json**——它是反射产物，重跑即可覆盖。

## 四、字段字典与产物格式

### 4.1 契约反射规则（字段名由 case.json 决定，禁止写死）

finalize 用**同义词族**把内部产物映射到 @output_contract 声明字段：先精确名匹配（100 分），再同义词族匹配（80 分），再前缀/包含匹配（40/25 分），按分数从高到低分配，每个内部值只被认领一次，映射结果写入 @result.json.meta.contract_field_mapping。声明字段没有对应值时，按声明类型占位（array→@[]、object→@{}、string→@""，无类型→@null），并列入 @meta.contract_fields_not_reported；未声明但已产出的内容放进 @meta.additional_outputs。

内部键与常见声明名的对应（节选，实际同义词表见脚本 @SYNONYMS）：

| 内部键 | 常见声明字段名（同义词族） | 值形态 |
|---|---|---|
| revised_text / renumbered_text / manuscript | manuscript_final、revised_manuscript、updated_text、renumbered_manuscript、final_text | string：全文（正文引用标记已重写 + 文献表已替换） |
| references / reference_list / bibliography | works_cited、reference_entries、formatted_references、bibliography、reference_list | array：每条 {number, ref_key, aliases, style, formatted, text, entry_type, year, title, doi, merge_group, merged_from, field_sources} |
| citation_map / intext_citations | intext_marker_trace、citation_locations、citation_index、citation_occurrences | array：每条 {cite_id, marker, marker_style, new_marker, ref_keys, resolved_via, resolution, page, block_id, sentence_id, section} |
| duplicates | duplicate_report、dedup_report、merge_report | object：{groups, needs_review, conflicts, merges, removed_keys, merge_count} |
| duplicate_groups | merged_bibliography_groups、merged_groups、duplicate_groups | array：每组 {group_id, members, criteria, confidence, rationale, merged_into, merged_keys, field_fills} |
| merges | merge_records、merges | array：每次合并的判据与字段补齐来源 |
| metadata_issues | field_problems、metadata_problems、metadata_errors、quality_issues | array：{issue_id, ref_key, field, severity(error/warning/info), message, value} |
| claim_support | sentence_level_support、claim_matches、claim_evidence、assertion_support | array：见 5.3 的合并后记录（含 baseline_relation、source=agent/baseline、insertion） |
| insertion_suggestions | citation_insertions、insertion_points、suggested_citations | array：无引用但需来源的句子 + 候选文献 |
| consistency | consistency_report、consistency_check、validation_report | object：{checks[10], passed, warnings, failures, ok} |
| resolution | resolution_report、citation_resolution | object：标记总数/解析数/未解析项/解析率 |
| quality | quality_metrics、metrics、statistics、counts | object：见 6.9 计数 |
| evidence | evidence_anchors、anchors、provenance_map、source_map | array：每条主张的锚点与校验结果（完整块在 evidence.json） |
| changes | change_records、change_log、modifications、diff | array：{change_id, type(citation_marker/duplicate_merge/format_conversion/reference_list_rebuild), before, after, reason, ref_key(s), page, block_id} |
| uncertainty / limits | uncertainties、open_questions、limitations、gaps | array：字符串 |
| style | target_style、format、citation_style、formatting_standard | object：{style, label, basis, hint_evidence, marker_counts, manuscript_language} |
| summary | overview、brief、summary_statistics | object：关键计数与状态 |

字段名映射不保证命中隐藏案例的每一个怪字段（如 @unmodelled_field_xyz）：此时按类型占位并列入 not_reported，**既保持形状又不伪造内容**。

### 4.2 result.json 结构

@```json
{
  "case_id": "case-xxx",
  "<契约字段1>": "...",            // 按 case.json 声明顺序
  "<契约字段2>": [...],
  "meta": {
    "status": "success|partial|failed",
    "contract_detected": true,
    "contract_field_count": 9,
    "contract_fields_not_reported": ["unmodelled_field_xyz"],
    "placeholders": {"unmodelled_field_xyz": ""},
    "limits": ["..."],
    "uncertainty": ["..."],
    "change_log": ["..."],
    "contract_field_mapping": {"works_cited": "references"},
    "field_fill_ratio": 0.8889,
    "additional_outputs": {"quality": {...}, "changes": [...]},
    "evidence": {...},              // 与 evidence.json 同源的精简块
    "output_files": {"result": "...", "evidence": "...", "run_report": "..."}
  }
}
@```

### 4.3 work 目录快照（确定性、可审计）

- @work/citation_index.json：每条正文标记的 {cite_id, style, raw, page, block_id, sentence_id, resolved_refs, via, narrative} + 未解析项 + 按风格计数；
- @work/bib_entries.json：{style, sources, entry_count, number_map[{old_number, ref_key}], entries[合并后条目全量], duplicate_report}；条目包含 authors/title/year/journal/booktitle/publisher/address/volume/issue/pages/doi/url/type/language/missing_fields/completeness/source(raw 截断)/merged_from/field_sources；
- @work/style.json：目标规范判定与依据（指令证据、标记风格计数、语种）；
- @work/batches/batch-XX.json：Agent 工作表（见 5.2）；
- @work/citation_state.json：finalize 所需完整状态（正文原文/块、句子、标记、条目、锚点、基线逐句记录）。**finalize 只读这个状态 + generated/，不重新抽取**。

## 五、Agent 操作协议（精确命令与 JSON 格式）

目标：**最少模型请求**完成"引用整理 + 逐句论断核对"。prepare 不发模型请求；Agent 只需读少量工作表、写 1 个判断文件；finalize 不发模型请求。推荐 ≤5 条命令、≤3 次读文件、1 次写文件。

### 5.1 四步流程

@```bash
# 第 1 步：确定性阶段（会写 work/ 快照 + 基线 result.json）
python scripts/run_reference_management.py --prepare

# 第 2 步：读清单（人读也方便，含批次列表、条目数、风格依据）
#   work/claim_worksheets.json
#   work/batches/batch-01.json, batch-02.json, ...

# 第 3 步：把判断写入（可以只写你判断过的段，未写段自动降级为确定性基线）
#   work/generated/claim_support.json

# 第 4 步：合并 + 格式化 + 回源校验 + 反射契约 + 写最终产物
python scripts/run_reference_management.py --finalize
@```

准备/收尾命令的位置：技能安装在 @/skills/weekly-skill，建议在其目录下执行；@--input/--work/--out 可用绝对路径显式指定（例如 @--input /input）。若不确定技能目录，用 @python -c "import pathlib;print(pathlib.Path.cwd())" 先确认。

### 5.2 工作表格式（work/batches/batch-XX.json，只读）

@```json
{
  "batch_id": "batch-01",
  "instructions": ["Fill work/generated/claim_support.json ...", "..."],
  "style": "gbt7714-2015",
  "segments": [
    {
      "seg_id": "s0004",
      "page": 1,
      "section": "## 摘要",
      "sentence": "文献报道CO2转化率可达85%[2]，甲烷选择性约为92%[5]。",
      "has_citation": true,
      "cited_refs": ["张三2021", "chen2019a"],
      "baseline_claim_terms": ["co2", "文献报道", "转化率可达", "催化", "选择性"],
      "candidate_refs": [
        {"ref_key": "张三2021", "role": "cited", "title": "镍基催化剂研究",
         "authors": ["张三", "李四"], "year": "2021", "venue": "催化学报",
         "doi": "", "type": "journal", "terms": ["镍基", "催化", "研究"]},
        {"ref_key": "chen2019a", "role": "cited", "title": "Carbon deposition on Ni-based catalysts",
         "authors": ["Chen", "Zhao"], "year": "2019", "venue": "Catalysis Today",
         "doi": "10.1016/j.cattod.2018.11.032", "type": "journal", "terms": ["carbon", "catalyst"]}
      ]
    }
  ]
}
@```

字段说明：@seg_id 是唯一键（句 id）；@has_citation=false 表示该句是"待插引"候选；@cited_refs 是该句已引条目的 ref_key；@candidate_refs 中 role=cited 是句内已引文献，role=lexical_alternative 是词面更接近但未被引用的条目（用于发现"引错文献"）；@candidate_refs[].ref_key 是**唯一允许**出现在判断文件里的键。

### 5.3 判断文件格式（work/generated/claim_support.json，唯一必写文件）

顶层是数组；允许字段如下，**未知字段会被忽略，未知 seg_id/ref_key/relation 会被拒绝并记录**：

@```json
[
  {
    "seg_id": "s0004",
    "claim_terms": ["co2", "转化率", "选择性"],
    "matched_refs": [
      {"ref_key": "张三2021", "relation": "supported", "confidence": 0.8,
       "reason": "文献题目与研究体系一致，覆盖转化率论断"},
      {"ref_key": "chen2019a", "relation": "partial", "confidence": 0.55,
       "reason": "只支持积碳/稳定性部分，不直接支持选择性数值"}
    ],
    "insertion": {"needed": false, "suggested_refs": [], "note": ""},
    "confidence": 0.75,
    "note": "四句合一的摘要段，按分号拆分逐条比对"
  },
  {
    "seg_id": "s0013",
    "claim_terms": ["比表面积", "活性"],
    "matched_refs": [],
    "insertion": {"needed": true, "suggested_refs": ["王五2018"],
                  "note": "正文陈述比表面积与活性关系但无引用，建议在句末补引"},
    "confidence": 0.6,
    "note": "无引用的事实性论断"
  }
]
@```

硬性约束（违反会被拒收，拒收记录写入 evidence.json 的 judgement.invalid[]）：

| 字段 | 约束 |
|---|---|
| seg_id | 必须是工作表里的 seg_id，不能自造 |
| matched_refs[].ref_key | 必须出现在该段 candidate_refs 或 work/bib_entries.json 的 entries 中；自造键被拒 |
| matched_refs[].relation | 只接受 supported / partial / unrelated / contradicts / unverified |
| matched_refs[].confidence | 0~1 的数，越界会被夹到边界 |
| insertion.suggested_refs | 必须是已知 ref_key；未知项直接丢弃 |
| claim_terms / note / reason | 字符串或字符串数组，超长会截断（note/reason ≤ 300 字符） |

容错：可以只写部分段（未写段回退确定性基线，并在 limits 说明）；同一 seg_id 重复出现只取第一条并记 duplicate_segments；文件不存在/为空/全是非法记录时，finalize 仍产出完整结果，status=partial，limits 里写明"agent judgement was missing or empty"。

### 5.4 可选：覆盖目标规范（work/generated/style.json）

@```json
{"style": "apa7"}
@```

可用值：@gbt7714-2015（=gbt7714/numeric）、@apa7（=apa/author-year/author_year）。task.md/case.json 已明确规范时不要写此文件；只有任务说明模糊、而稿件明显需要另一种规范时才覆盖。覆盖会写入 limits 与 style.basis。

### 5.5 收尾自查（30 秒内可完成）

1. 读 @result.json：@meta.status（success/partial）、@meta.limits、@meta.contract_fields_not_reported；确认 @case_id 正确。
2. 读 @evidence.json：@grounding.grounding_rate（论断锚点）、@marker_grounding.grounding_rate（引用标记）、@entry_grounding.grounding_rate（文献条目）、@judgement.invalid（被拒判断）。
3. 读 @run_report.json：@counts 里的 markers_resolved / duplicates_merged / metadata_errors / consistency_failures。
4. 若 status=partial 且原因可补救（例如漏写判断文件、seg 覆盖不全），补写文件后**只重跑 --finalize**，不要重跑全流程；最多重试一次，避免超出 900 秒预算。
5. **禁止**手工编辑 result.json / evidence.json；它们由脚本生成，改完会被下一次运行覆盖且破坏可复核性。

## 六、核心工作流（确定性实现细节）

### 6.1 输入发现与材料分类（prepare）

扫描 @/input、@/app/input、@/app/work、@/app/environment、@/app（或 @--input 指定目录），按特征识别 @case.json（含 case_id / output_contract / caseId 的 JSON，容忍 BOM）与 @task.md；材料按后缀与内容嗅探分类为 **正文 / 文献库 / 支撑文本**：@.bib/.bibtex/.ris/.nbib/.enw/.endnote 直接归为文献库；@.json 若含条目数组（有 title/author/doi 的 dict 列表）也归为文献库；其他文本先嗅探格式（@sniff_format 识别 bibtex/ris/endnote），命令式内容才是文献库。正文按"文件名提示（manuscript/paper/draft/正文/论文…）→ 后缀优先级（docx/pdf/tex/md）→ 体积 → 路径"确定性排序，最多读 3 份候选。

### 6.2 抽取与证据锚点

正文抽取走共享抽取链（PyMuPDF → pdfplumber → 外部工具 → 标准库），结果带 @engine 与 @anchor_quality（span>page>text），降级写入 warnings。**文本类材料（md/txt/tex/docx/pptx/bib/ris/json）会从原文重新切块**：空白行分段、超长段按行切到 ≤1500 字符，块 id 形如 @raw-b001。原因见"踩坑"：通用纯文本抽取会用合成坐标把两行合并且**不补空格**，锚点校验会拿被污染的文本自证。PDF 保留引擎给出的 block/bbox（y 轴已统一为 PDF 惯例 y 向上）。

### 6.3 文献库解析

- **BibTeX**：手写花括号/引号平衡扫描，支持 @article/@inproceedings/@book/@incollection/@phdthesis/@techreport/@misc/@unpublished/@online 等；@string/@comment/@preamble 跳过；字段别名（journaltitle→journal、number→issue、school/institution→school 等）；LaTeX 清理（@\&、@\%、重音、@\emph{} 解包、@-- → en dash、@$x$ 去符号）。
- **RIS**：TY/AU/TI/JO/PY/VL/IS/SP/EP/DO/UR/PB/CY/KW/AB/ER，多记录。
- **EndNote tagged**：%0/%A/%T/%J/%D/%V/%N/%P/%R/%U/%I/%C/%K/%X，多记录。
- **编号纯文本**：@[1] … / @1. … 切条；GB/T 式按 @[J]/[M]/[C]/[D]/[R]/[S]/[P]/[EB/OL] 类型标识拆"作者 / 题名 / 出处"，并从出处里切出 年、卷(期)、页、出版地: 出版者；APA 式按 @(2020). 拆作者、题名、出处，并把出处尾部（@, 12(3), 45-67）剪掉只留刊名；不能拆的条目原样透传并标 @parse_status=raw_only。
- **JSON**：条目数组或 {references/entries/items/...} 包裹。

### 6.4 引用标记定位

四类正则 + 优先级消歧（LaTeX > 数字 > 上标 > 著者-出版年 > 叙述式），重叠候选只保留优先级高者；数字标记展开区间（@1,3-5 → 1,3,4,5）；一句话多引、多句一引都会各自成为独立 @cite_id。**先切出稿内参考文献区**（末尾 35% 之后、标题独立成行、其后有编号条目或条目样文本），正文标记只在正文区扫描，避免把文献表的 @[1] 当成正文引用。未解析标记（编号越界、cite key 不存在、作者-年对不上）不猜测，列入 unresolved 并在 result 的 metadata_issues 里报 error。

### 6.5 编号映射（老编号 → 条目）

稿内有编号参考文献表时，用解析出的 @old_number 建映射（最可靠）；只有外部文献库时，**假定文献库条目顺序等于原编号顺序**（顺序编码制的常见约定），并在 uncertainty 里注明；若检测到文献库按首作者字母序排列，会额外警告该假设的风险（重排后所有标记仍可通过 citation_map 追溯）。重排规则：**正文首次出现顺序** 1..N，只保留被引用条目；未被引用的条目在 @quality.uncited_entries 计数与 @metadata_issues 里单独列出，不进入正式文献表（APA 同理）。

### 6.6 重复检测（可解释、保守）

| 判据 | 置信度 | 处理 |
|---|---|---|
| DOI 规范化后完全相同 | 1.00 | 合并，canonical 取信息更全/已出版版本 |
| 规范化题名（去 LaTeX/标点/大小写）完全相同 | 0.95 | 合并；双方 DOI 都非空且不同 → 转 conflicts，不合并 |
| 题名相似 ≥0.85 且一方是预印本（arXiv/bioRxiv/medRxiv/SSRN/preprint…） | 0.92 | 合并，canonical 优先正式版 |
| 题名相似 ≥0.96 且首作者相同 | 0.90 | 合并 |
| 题名相似 ≥0.90 且首作者相同且年份差 ≤1 | 0.90 | 合并 |
| 中英文题名对（一方 CJK）且共享 ≥2 个书目信号（同年/同页/同卷/同刊名 token） | 0.5~0.6 | **只进 needs_review**，不自动合并 |
| 题名相似 ≥0.86 且年份差 ≤1（作者不同） | — | 进 needs_review |
| 题名相似 ≥0.85 但双方 DOI 冲突 | — | 进 conflicts（元数据错配，人工修） |

合并只做"字段补齐"（空字段从其它成员补，@field_sources 记录来源），规范输出以 canonical 为准，**所有成员 key 作为 aliases 保留**，正文引用因此可继续解析。合并依据、置信度、补齐字段全部写进 duplicate_report，可逐条复核。

### 6.7 格式化（GB/T 7714-2015 与 APA 7th）

只用解析出的字段拼装，**缺字段就跳过该段，绝不补造**。GB/T：@作者. 题名[J]. 刊名, 年, 卷(期): 页. DOI: …，英文作者"姓全大写 + 名首字母"（≤3 人全列，>3 人前 3 + ", et al."），中文 >3 人用 ", 等"；图书 @[M]、会议 @[C]//论文集、学位 @[D]、报告 @[R]、预印本/网页 @[EB/OL]；中文出版地: 出版者。APA 7：@作者 (年). 题名. 刊名, 卷(期), 页. https://doi.org/…；≤20 作者全列（最后一人前加 ", & "），>20 列前 19 + ", ... " + 末位；图书/章节/会议/学位/预印本/网页分别套用对应模板；年缺失写 @(n.d.)；页码 en dash；中文作者不反转、不用 &。两套格式都输出**纯文本**（斜体等版式在 docx/md 中用文本表达，属于已知限制）。

### 6.8 正文重写

只替换引用标记本身，**其余字符逐字保留**（基于字符偏移做切片拼接）；数字模式合并连续编号（≥3 连号写 @1-3，两连号写 @1,2）；著者-出版年模式按文献表排序生成 @(Smith & Jones, 2020) / @(Smith et al., 2019) / 中文 @（张三等, 2020），同作者同年加 a/b 消歧；叙述式标记（@Smith et al. (2020)）在数字模式下保留作者名并追加编号（@Smith et al. [3]），在著者-年模式下重写为规范叙述式。未解析标记原样保留并报 error，**不猜**。LaTeX 稿件（.tex）默认保留 @\cite 原样（编号由 tex 文献表体现），重排顺序仍写入 citation_map。

### 6.9 一致性与质量检查（10 项）

in_text_resolution（标记解析率）、numbering_continuous（1..N 连续）、in_text_numbers_in_range（编号不越界）、no_uncited_entries（未被引条目）、marker_style_uniform（标记风格统一）、dedupe_complete（无残留重复）、field_completeness（按文献类型必需字段）、doi_format（10.xxxx/yyy）、no_fabricated_fields（格式化文本里的数字必须来自条目字段）、reference_section_sync（文献表条数=被引条数）。共 10 项检查，任一 fail 会把 status 降为 partial 并写进 limits；warn 不影响 status 但计入 quality。质量计数包含：条目数/被引数/未引数、标记总数与解析率、重复组合并数与待确认数、元数据 error/warning 数、逐句 grounding 率、条目 grounding 率、标记 grounding 率、插入建议数、Agent 判断覆盖率、耗时。

### 6.10 逐句论断匹配与"插引"建议

每个"有引用"或"含事实性论断但无引用"的句子都会得到一条记录：@claim_terms（确定性内容词：英文实词 + 中文 2 字 bigram）、@matched_refs（句中已引条目，relation 由词面覆盖/作者提及决定：supported/partial/unverified）、@candidate_alternatives（未被引但词面更接近的条目——用于发现"引错文献"）、@possible_misattribution（候选替代项显著优于已引项时置 true 并报 warning）、@insertion（无引用句的插引建议与候选 ref_key）。**unverified 不是 unrelated**：跨语言/改写导致词面为零时不能宣称"不相关"，代码只给"未验证"，最终判断交给 Agent 写 judgement（并被校验）。每条记录都带 page/block_id/bbox/quote 锚点并回源校验。

## 七、异常处理

| 异常情况 | 检测方式 | 处理策略 | 状态标记 |
|---|---|---|---|
| 找不到 case.json | 输入发现为空 | 仍然处理材料；@contract_detected=false，全部产物放 @meta.additional_outputs，不臆造契约字段 | partial |
| output_contract 为空/形状怪异 | 契约规范化后字段数=0 | 同上；若为 list/dict/嵌套/meta 内嵌都能识别 | partial |
| 没有任何材料文件 | materials=0 | 输出契约形状空值（string→""、array→[]）并写明"no manuscript/bibliography" | partial |
| 正文没有文本层（扫描件） | 抽取 total_chars≈0 | 不编正文；产出空值 + 提示需要 OCR；文献库仍尽力解析 | partial |
| 没有文献库 | 条目数=0 | 文献表为空、标记全部 unresolved、metadata_issues 报 error；正文照常输出（标记原样保留） | partial |
| 文献库格式损坏 | 解析条目数=0 或字段残缺 | 换格式嗅探重试；单条拆不出字段则原样透传并标 raw_only，不丢条目 | partial（带 warnings） |
| 条目字段缺失（年/卷/页/DOI） | 按文献类型必需字段检查 | 格式化时跳过该段，不补造；写入 metadata_issues（error/warning 分级） | success/partial（视严重度） |
| 正文引用指向不存在的条目 | 编号越界 / cite key 不存在 / 作者-年对不上 | 该标记保持原样、列入 unresolved、metadata_issues 报 error；其它标记照常重排 | partial |
| 数字编号越界或重复 | 与文献表条数/编号表比对 | 越界标 warn/error；重复编号报错并保留原标记 | partial |
| 疑似重复但 DOI 冲突 | 题名相同/相近且双方 DOI 不同 | **不合并**，进 conflicts 并说明；文献表保留两条 | partial（不确定） |
| 中英文题名疑似同一文献 | 一方 CJK、共享 ≥2 书目信号 | **不合并**，进 needs_review | partial |
| Agent 判断文件缺失/为空 | @work/generated 无文件或 0 条 | 全部逐句结论降级为确定性词面基线，limits 写明缺口 | partial |
| Agent 判断只覆盖部分段 | 记录数 < 工作表段数 | 覆盖段用 Agent 结论、其余用基线；limits 写覆盖率 | partial |
| 判断里有未知 seg_id / ref_key / relation | 白名单校验 | 单条拒收、写 @evidence.json.judgement.invalid[]，不污染结果 | 同上（不影响 success 判定逻辑，但会在 uncertainty 说明） |
| 同一段重复写判断 | seg_id 去重 | 取第一条，重复项记 duplicate_segments | partial（uncertainty 说明） |
| 工作目录只读 | 写探测失败 | 依次尝试 @/app/work、@work、@cwd/work；输出目录同理（@/app/output、@output、@output） | partial（limits 说明） |
| 无 office/TeX 渲染器 | @render.probe 全空 | 不伪造 PDF/PNG；保留 @.docx/@.md/@.html 可编辑产物并写 limits | success（带 limits） |
| 顶层任何异常 | try/except BaseException | 写 @status=failed 的 result.json + evidence.json + run_report.json（含 traceback_tail），退出码 1 | failed |
| 超过软截止（720s） | Stopwatch | 停止继续抽取/解析，先落盘已完成部分 | partial |

所有异常路径的共同底线：**result.json 永远存在且带 case_id/meta**；"缺什么"必须写在 meta.limits / meta.uncertainty，不允许静默。

## 八、限制与边界

1. **不联网**：DOI 只做格式（10.xxxx/yyy）与库内一致性检查，**不声称已联网核实真实性**；编造条目无法被本技能验真（只能靠题面材料比对与人工复核）。
2. **不做图表公式交叉引用重编号**："图 1 / 表 2 / 式 (3)" 只作为正文标签被识别，不进入文献编号体系；若题面要求一并重排，需要人工确认。
3. **不做语义级支持性判定**：词面/作者匹配只是可复核基线；"文献结论是否真的支持这句论断"由 Agent 判断文件表达，本技能负责**校验格式与可追溯性**，不能替代领域专家。
4. **不做中英文题名互译**：中英文对照的同一文献只能进 needs_review（可解释、待人工确认），不会自动合并。
5. **作者姓名不转拼音**：中文作者与英文拼写（张三 vs Zhang San）无法机械对齐，相关的重复检测只依赖书目信号。
6. **PDF 锚点是 block 级**：内置解析器/引擎给出的 block 与 bbox 用于定位，未做 span 级视觉复核；文本类材料用原文段落块。
7. **无 OCR**：扫描件不产生文本层时不编文本。
8. **不区分直接引用/间接引用/转引**：没有语言模型参与时无法可靠判定，只按"句内是否有标记"处理。
9. **上标数字必须带上下文**：单位指数（m²、s⁻¹、10²）会被显式排除，不与引用混淆；但正文里孤立的上标数字仍可能被误判为引用（此时 citation_map 会给出 before/after 供核对）。
10. **LaTeX 稿件默认保留 \cite**：不改写 tex 标记（否则会破坏编译/文献域），重排结果体现在文献表顺序与 citation_map；如果题面要"正文也变成数字"，用 @--style 与文本类稿件，或人工确认。
11. **一次性处理**：不做增量编辑（不读旧 result.json 做 diff），每次运行从输入重建，保证 3 次重复运行结果一致。
12. **文献表只列被引条目**：未被引用的条目不会进入正式文献表（GB/T 与 APA 惯例），但会在 quality/uncited_entries 中列出。

## 九、自查方法

| 现象 | 自查方法 | 处理 |
|---|---|---|
| result.json 没有契约字段 | 看 @meta.contract_detected 与 @meta.contract_field_mapping | false 说明没找到 case.json/契约为空；检查 @--input 是否指到含 case.json 的目录 |
| 字段是 null/空串 | 看 @meta.contract_fields_not_reported 与 @meta.placeholders | 该字段无法从材料产出；不要伪造，改材料或接受占位 |
| status=partial | 先读 @meta.limits（每条都写明原因），再看 run_report.judgement.coverage | 漏写 judgement → 补写后只重跑 --finalize；材料缺失 → 如实保留 |
| 编号没变化 | 看 @work/citation_index.json 的 markers 与 resolved_refs | 原编号本来就从 1 开始按顺序时不会变化；换顺序的稿子才看得到 before/after |
| 正文标记没换 | 看 @work/bib_entries.json 的 number_map 是否为空 | 空=没有编号表/文献库且无法假设顺序；检查文献库是否按字母排序（见 6.5） |
| 有未解析引用 | 看 citation_index.unresolved 与 metadata_issues | 编号越界/key 不存在/作者-年对不上；补文献库或人工确认 |
| 重复文献没合并 | 看 duplicate_report.needs_review / conflicts | 依据不足或 DOI 冲突，人工确认；不要手改结果 |
| 逐句 grounding 率低 | 看 evidence.json.grounding.per_claim[].checks[].match | missing=抽取丢文本（扫描件/编码问题）；page_only=块定位失败 |
| 条目 grounding 率低 | 看 evidence.json.entry_checks | 文献库 raw 与原文差异过大；检查编码或导出格式 |
| 组件测试 | @python tests/test_reference_management.py（标准库全绿）、@python tests/test_hybrid.py、@python tests/test_core.py | 见第十节示例 |

## 十、最小调用示例（示例，非正式案例结果）

**输入**（@tests/make_fixture.py 生成的合成样例，全部内容为虚构，非真实文献）：

@```
fixture/gbt/
├── case.json        # case_id=fixture-citation-gbt-001，9 个契约字段（故意与内部命名不同）
├── task.md          # 要求 GB/T 7714-2015 顺序编码制
├── manuscript.md    # 中文稿件：13 处引用（含 [2]、[5]、[2,6]、[1,3]、Wang等（2021）等）
└── references.bib   # 7 条 BibTeX + 稿内 6 条编号文献表（含同 DOI 重复、arXiv 预印本、中英文对）
@```

**调用**：

@```bash
python scripts/run_reference_management.py --prepare  --input fixture/gbt --work work --out out
# （Agent 读 work/claim_worksheets.json 与 work/batches/*.json，写 work/generated/claim_support.json）
python scripts/run_reference_management.py --finalize --work work --out out
@```

**示意输出**（result.json 摘要，**这是示例，不是正式案例结果**；数字来自本地合成 fixture 的实际运行）：

@```json
{
  "case_id": "fixture-citation-gbt-001",
  "manuscript_final": "# 镍基催化剂在CO2加氢中的研究进展 ... 转化率可达85%[1] ... 积碳问题仍然存在[5]",
  "works_cited": [
    {"number": 1, "ref_key": "张三2021", "formatted": "[1] 张三, 李四. 镍基催化剂研究[J]. 催化学报, 2021, 41(3): 100-110."},
    {"number": 2, "ref_key": "chen2019a", "formatted": "[2] CHEN H, ZHAO Y. Carbon deposition on Ni-based catalysts[J]. Catalysis Today, 2019, 330: 88-95. DOI: 10.1016/j.cattod.2018.11.032."}
  ],
  "intext_marker_trace": [
    {"marker": "[2]", "new_marker": "[1]", "ref_keys": ["张三2021"], "resolution": "resolved"},
    {"marker": "[1]", "new_marker": "[5]", "ref_keys": ["smith2020a"], "resolution": "resolved"}
  ],
  "merged_bibliography_groups": [
    {"group_id": "dup-001", "members": ["smith2020", "smith2020a", "smith2020dup", "smith2020pre"],
     "criteria": ["same_doi", "same_normalized_title"], "confidence": 1.0,
     "merged_into": "smith2020a", "field_fills": {"eprint": "smith2020pre"}}
  ],
  "field_problems": [{"severity": "info", "field": "citation", "message": "bibliography entry is never cited in the manuscript"}],
  "sentence_level_support": [
    {"seg_id": "s0004", "sentence": "文献报道CO2转化率可达85%[2]，甲烷选择性约为92%[5]。",
     "source": "agent", "matched_refs": [{"ref_key": "张三2021", "relation": "supported", "confidence": 0.8}]}
  ],
  "provenance_map": [{"claim_id": "claim-s0004", "grounded": true, "checks": [{"match": "exact"}]}],
  "target_style": {"style": "gbt7714-2015", "label": "GB/T 7714-2015 (numeric)", "basis": "explicit instruction in the task material"},
  "unmodelled_field_xyz": "",
  "meta": {"status": "success", "contract_detected": true, "contract_field_count": 9,
           "contract_fields_not_reported": ["unmodelled_field_xyz"],
           "contract_field_mapping": {"works_cited": "references", "manuscript_final": "revised_text"},
           "field_fill_ratio": 0.8889}
}
@```

该次运行的 evidence.json 摘要：@claim grounding 12/12（1.0）、@marker grounding 13/13（1.0）、@entry grounding 8/8（1.0）、@judgement.coverage 1.0；run_report.counts：引用标记 13、解析 13、合并重复 3 组、待确认 1 组、未被引条目 2。旧编号→新编号：@[2]→[1]、[5]→[2]、[6]→[3]、[4]→[4]、[1]→[5]、[3]→[6]（按正文首次出现顺序重排）。

## 十一、架构参考与差异说明

目录结构（提交包会由 @tools/package_skill.py 把共享内核展平进来）：

@```
writing-04-reference-management/
├── SKILL.md                     # 本文档
├── GITHUB_URL.txt
├── scripts/
│   ├── run_reference_management.py   # 两阶段主流程（--prepare/--finalize/--all）
│   ├── refparse.py                   # 解析：BibTeX/RIS/EndNote/纯文本/引用标记/句子切分
│   └── refmanage.py                  # 去重/格式化/重排/一致性/逐句基线/导出
├── tests/
│   ├── test_reference_management.py  # 本技能自测（130 项断言）
│   ├── make_fixture.py               # 合成 fixture 生成器（虚构材料）
│   ├── test_hybrid.py / test_core.py # 共享内核自测（随包携带）
├── resources/
│   └── reference_style_notes.md      # GB/T 与 APA 字段规则、去重判据备忘
└── lib/                              # 共享内核（contract/discover/extract/anchors/hybrid/...）
@```

| 维度 | 常见同类做法 | 本技能做法 |
|---|---|---|
| 核验 | 模型自述"已核验" | 代码回源断言，区分 exact/normalised/block_only/missing，失败标 ungrounded |
| 输出契约 | 固定 JSON schema | 从 case.json 反射；缺值按类型占位并记录 |
| 依赖 | 依赖 bibtexparser/python-docx 等 | 仅标准库，格式解析与 OOXML 全部自己实现 |
| 被杀风险 | 无 | 启动即写占位 result.json；prepare 即写基线完整结果 |
| Agent 生成内容 | 直接采信 | 白名单校验（seg_id/ref_key/relation/置信度），拒收项留痕 |
| 编号重排 | 只改文末列表 | 正文标记 + 列表 + 交叉映射三处同步，逐条 before/after 可查 |

## 十二、踩坑（--help 不会告诉你的）

1. **上标 m² 不是引用**：把 Unicode 上标数字一律当引用会把"m²/g"解析成 @[2]。已加上下文守卫（前一个字符是 ASCII 字母/数字，或后跟 @/、@^、@-、@+ 时跳过）；不传单位的上标引用仍能识别。
2. **纯文本块会吞空格**：共享抽取链对纯文本用合成坐标聚类行，相邻两行可能被合并且**不补空格**（@with + @activity → @withactivity），拿它自校验会得到"看起来通过其实错位"的锚点。本技能对文本类材料改用原文段落切块（@raw-bNNN）。
3. **文献表里的 @[1] 不是正文引用**：不先切出参考文献区，会把列表序号当正文标记并使编号全错。
4. **文献库顺序 ≠ 原编号**：只有稿内编号表能给可靠 old_number；外部 BibTeX 若按字母序导出，"假定顺序=编号"会错。脚本会检测字母序并写警告；重排后仍可用 citation_map 追溯每个标记。
5. **中文叙述式 @张三等（2021）**：正则要同时接受"英文姓 + 等 + 全角括号"与"中文姓名 + 全角括号"，否则整句匹配不到（已覆盖）。
6. **全角标点**：中文稿件里 @，、；、（） 与半角混用；作者/页码/区间正则都同时接受两种写法。
7. **BOM**：Windows 工具写的 case.json 常带 UTF-8 BOM；所有 JSON 读取用 @utf-8-sig，否则会误判"没有契约"。
8. **占位先行**：容器到 900 秒直接杀进程；先写占位/基线 result.json，比"跑完再写"安全一个数量级。
9. **生成内容被拒不会静默**：未知 ref_key 等拒收记录在 evidence.json.judgement.invalid[]，不要以为是脚本 bug。
10. **不要手改 result.json**：它是 finalize 的反射产物；重跑即可。文档中所有"示例输出"均为示意，正式结果以一次真实运行为准。
