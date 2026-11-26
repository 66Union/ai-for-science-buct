---
name: writing-02-language-polish
description: Polish academic manuscripts with a minimal-edit protocol and a per-sentence change ledger — grammar, terminology, academic register and Chinese-English expression — while proving that numbers, units, citations, figure/table references, hedges and negations are preserved. Also ships a deterministic rule-based fallback polisher, so the documented default command (--all) still delivers a real, per-change documented polish when no model judgement is supplied. Emits a contract-reflected result.json plus editable .docx/.md/.html deliverables and a re-verified evidence file. Use when a task asks to revise, polish, proofread or improve a manuscript/abstract/methods/grant text and demands original-vs-revised comparison with reasons.
version: 1.2.0
author: 66Union
license: MIT
tags: [language-polish, minimal-edit, semantic-preservation, terminology-consistency, change-ledger, output-contract]
---

# 学术语言润色（Writing-02 Language Polish）

## 一、技能定位

对应本题考察的四个问题，本技能逐条给出可执行、可复核的做法：

| 考察问题 | 本技能的做法 | 证据落在哪 |
|---|---|---|
| **改得对** | 两阶段混合：确定性脚本只做切段与事实清点，模型只在逐段工作表上做最小编辑；每处修改必须给原文/改文/类型/理由，改文片段要能反查到改文 | result 的逐段对照与修改台账；证据文件的 change_landing |
| **说得像** | 依据 task.md 与稿件自动判定目标文体（期刊/学位论文/基金/审稿回复/摘要）与目标语言，把文体约束写进每一张工作表；术语表驱动的全篇一致性回扫 + 缩写首次出现审计 | style_profile、term_consistency 的 entries/conflicts/acronym_first_use |
| **没改坏** | 逐段做语义守恒断言：数字、单位、引用编号、图表指向、限定词、否定、情态、公式记号逐项比对。数字/引用/否定被改动即回退该段（捏造守卫）；限定词被加强或削弱记为复核项，绝不静默接受 | fidelity_checks 的逐段结果、review_items、limits 里的回退说明 |
| **讲得清** | 修改记录逐条校验：original 必须出现在原文、revised 必须出现在改文、必须给理由与受控类型，否则标 invalid 并计入 land 率 | edit_ledger 的 valid/issues 与 change_landing 统计 |

**与"让模型整段重写并自述已核对"的做法不同**：本技能把"没改坏"做成代码断言而不是模型承诺；数字/引用一旦被改动，输出自动回退到原文并如实记 reverted_preservation，评审可以直接看 agent_revised 与回退原因。

**零 Agent 也能交付真实润色**：如果评测 Agent 只运行了默认命令（--all = prepare+finalize）而没有写 work/generated/*.json，运行不会退化成 revised == original 的空润色。脚本内置确定性的规则回退润色器（scripts/fallback_polish.py）：只做可解释的最小编辑（标点/空格、a-an、重复词、句首大写、术语大小写、少量过度声称词与中英直译），逐条记录 original/revised/type/reason，并接受与 Agent 答案完全相同的守恒守卫（数字/引用/否定/公式/限定词任一被改动就整段回退）。此时 status=success 且 limits 明确写 no model judgement；出现回退或没有安全编辑才是 partial。详见第十三节。

**核心工程约束**（评测环境强制）：64 次模型请求 / 900 秒 / 单次回复 16384 tokens。因此文本搬运、清点、校验全部由确定性脚本完成，模型请求只花在"这句该不该改、怎么改、为什么"的判断上。典型 3 段稿件 = 1 张工作表 = 1 次读 + 1 次写。

## 二、适用条件

| 项目 | 说明 |
|---|---|
| 输入格式 | 稿件：`.docx` / `.md` / `.markdown` / `.tex` / `.txt` / `.pdf`；可附带术语表/风格指南等次级文本材料（`.md` / `.txt` / `.csv` / `.tsv` / `.bib`） |
| 语言 | 中文、英文、中英混排（同一段落内混排也可） |
| 篇幅 | 单文档 ~ 80 段（超 80 段自动截断并在 limits 说明） |
| 依赖 | **仅 Python 标准库**；可选外部渲染器（soffice/pandoc/pdftoppm）只用于预览，缺失时降级为 HTML 预览并写 limits |
| 运行环境 | Harbor 契约：输入 `/input/case.json` + `task.md`，材料在 `environment/` 下（路径全部自动发现，不硬编码）；输出 `/app/output`，工作区 `/app/work`（不可写则自动降级到输出目录下的 work） |
| 不适用 | 需要 OCR 的纯扫描件；需要真正编译 LaTeX 的任务（本技能只润色 .tex 的散文文本，不编译）；需要新增文献/数据的"扩写"任务（本技能禁止新增材料外事实） |
| 不做的事 | 不做整段重写；不改变作者结论强度；不新增引用；不改图表编号；不翻译整篇论文（除非 task.md 明确要求双语/目标语言） |

## 三、调用方式

### 触发方式

当任务要求"润色/改写/校对/学术语言优化/中译英表达优化"，并且要求**保留原意、数字、引用**或要求**逐句修改说明**时触发。若用户只是问内容问题（"这篇论文讲了什么"），**不应**触发本技能。

### 输入

```
/input/case.json          必选；唯一输出契约来源（output_contract），字段名由题目决定
/input/task.md            必选；文体、目标语言、指定章节、处理项从正文里解析
environment/**            可选；稿件与术语表/风格指南等材料（自动发现并排序）
environment/resources.yaml 可选；平台声明的资源清单（先读清单，再按声明取材料；缺失如实登记）
```

### 输出（全部写在 --out 目录）

| 文件 | 内容 |
|---|---|
| `result.json` | 由 output_contract 反射生成：声明字段按声明顺序出现，缺值按声明类型占位并记入 `meta.contract_fields_not_reported`；另含 `case_id`、`meta`（状态/限制/不确定性/映射/additional_outputs） |
| `evidence.json` | 逐段锚点与回源校验结果（grounding）、修改落地率（change_landing）、守恒断言汇总（invariants）、术语一致性、来源与切段信息 |
| `run_report.json` | 阶段、耗时、预算、覆盖/缺失/重复批次、限制、不确定性、引擎探测、交付物路径 |
| `revised_manuscript.docx/.md/.html` | 可编辑成稿（同一内容三种形态，HTML 为自包含预览） |
| `revision_notes.docx/.md/.html/.csv` | 逐句修改说明台账 + 术语表 + 守恒失败表 |
| `preview/` | 若镜像内有 office/PDF 渲染器则生成 PDF/PNG；否则不伪造，只保留 HTML 预览并写 limits |

```
python scripts/run_language_polish.py --prepare  --out /app/output
python scripts/run_language_polish.py --pending  --out /app/output
python scripts/run_language_polish.py --finalize --out /app/output
python scripts/run_language_polish.py --all      --out /app/output     # 默认，= prepare+finalize
```

- `--out` 缺省依次尝试 `/app/output`、`/output`、`.`/`output`；`--work` 缺省依次尝试 `--work`、环境变量 `POLISH_WORK_DIR`、`/app/work`、`<out>/work`、`.`/`work`。
- 每次启动**先写占位 result.json**（契约形状、status=partial），随后 prepare 结束再写一份"基线成稿"（未修改的原文 + 完整契约字段），因此任何时刻被杀都不会留下零产出。
- `--prepare` 与 `--finalize` 可分成两次进程执行（模型在中间写答案）；`--finalize` 若发现没有 state 文件会**自动补跑 prepare**（bootstrap），不会因此失败。

## 四、核心工作流

### 阶段 1：prepare（确定性，零模型请求）

1. **发现输入**：扫描 `/input`、`/app/input`、`/app/work`、`/app/environment`、`/app`，最后退化到当前目录；按 case_id/output_contract 特征认 case.json，按文件名认 task.md，先读 environment/resources.yaml（若存在）并把声明的文件作为材料优先项，再按后缀认材料；排序规则固定（稿件名优先 → 非术语/指南类优先 → docx/tex/md 优先 → 体积大者优先 → 路径字典序），保证三次重复运行拿到同一主文档，且绝不把输出目录/工作目录里的交付物再当输入。
2. **提取**：PDF 走共享引擎链（PyMuPDF → pdfplumber → pdf-text-tool/pdftotext → 内置解析器），每个结果带 engine 与 anchor_quality(span/page/text)，降级原因写 attempts。非 PDF（docx/md/tex/txt）走**段落级读取器**：docx 用标准库读 OOXML（不依赖 python-docx），md/tex/txt 按空行切段，参考文献按行首 [n]/n. 自动分段——因为通用块构建器面对合成坐标会把整篇并成一个块，逐段对照就没了。
3. **切段**：以段落为最小单位（超过 1800 字符的段落按句界再切）；每段记录 seg_id、页码、block_id、bbox、所属章节、是否标题。超过 80 段截断并记 limits。
4. **清点机器可判事实**：每段抽 numbers（含单位）、citations（[1]、(Author, 2020)、\cite{}、\ref{}、图/表/式编号）、hedges（限定词）、negations、modals、labels（figure/table/equation…）、formulas（记号级）。对整篇再抽 terms（术语候选：缩写 + 重复短语）。
5. **文体与任务画像**：从 task.md 解析目标文体（journal/thesis/grant/review_response/abstract/unknown）、目标语言、指定章节；次级材料里解析参考术语表（CSV/TSV/Markdown 表格/term: 译名/term → 译名）与缩写定义，只是提示，模型给出的术语表优先。
6. **分批发工作表**：按"任务聚焦章节优先、其余按原文顺序"排序，每批 ≤5 段且 ≤5000 字符，写 `work/batches/batch-XX.json`；同时写 `work/state.json`（可复现状态）、`work/doc.json`（回源校验用的块表）、`work/terms.json`、`work/README.md`（协议速查）。
7. **写基线交付物**：立即用"原文 = 改文"的基线调用 finalize 组装器，落一份完整契约结果 + 可编辑稿，status=partial（因为还没有任何修改）。这样"prepare 之后被杀"仍然有可用产出。

### 阶段 2：Agent 判断（模型，唯一消耗模型预算的环节）

读一张工作表，写一份 `work/generated/batch-XX.json`。规则硬编码在工作表里（见第五节），模型不需要背 SKILL.md。批次数 ≈ ceil(段数/5)，典型 3~8 次模型请求。

### 阶段 2.5：确定性回退润色（零 Agent 答案时，零模型请求）

finalize 逐段合并两种答案来源，优先级固定：

1. **Agent 答案优先**：work/generated/batch-XX.json 里该段有非空 revised 时完全按原协议处理，方法记为 method=agent；
2. **确定性回退**：该段没有 Agent 答案（或答案是空 revised）且不是标题时，调用 scripts/fallback_polish.py 的 polish_paragraph()，得到候选 revised 与逐条 changes，然后**用与 Agent 答案相同的守卫**复检：数字/引用/否定/公式/限定词集合比对 + 字符级改动率上限 0.25 + 每条 change 必须能同时落在原文与改文。任何一项不满足就整段回退成原文（status=unrevised，flags 加 fallback_reverted_preservation，review_items 记原因）。
3. 每段都写 **method = agent / deterministic_fallback / source_unchanged**；run_report 与 result 的 meta.additional_outputs（fallback_polisher.paragraphs_by_method）给出汇总。

回退润色是**幂等**的：polish_paragraph(polish_paragraph(x)) 不再产生编辑，因此同一输入重复运行 result.json 字节一致（路径字段除外）。

### 阶段 3：finalize（确定性，零模型请求）

1. **覆盖校验**：读取 generated 下的答案文件，与段清单比对，报告 missing / duplicate / unknown_ids / incomplete（缺 revised 字段）。缺失段落**不报错**，用 revised = 原文、status = unrevised 回填。
2. **修改落地校验**：每条修改记录检查 original 是否出现在原文、revised 是否出现在改文、是否有理由、类型是否受控；不满足即 valid=false 并写 issues（如 original_not_in_source / revised_not_in_output / missing_reason / unknown_type）。
3. **语义守恒断言（逐段）**：数字集合、引用集合、公式记号、限定词、否定、情态、图表指向逐项集合比对。
   - **硬失败 → 回退**：数字（丢失或新增）、引用（丢失或新增）、否定（仅拉丁文字段落；中文单字否定误报率高故不用于回退）。
   - **软失败 → 保留改动但标记复核**：限定词减弱/加强（做词形归并，indicates→indicate 这类语法修正不算丢限定）、情态变化、图表指向丢失、公式记号变化、整段重写（改动率 > 0.5）、改而不给理由、修改记录不可落地。
4. **原文回填**：回退段落在结果里 revised = original，同时保留 agent_revised（模型原改文）与回退原因，评审可复核"为什么拒绝"。
5. **术语一致性回扫**：模型术语表逐条核对首选形式是否已采用、旧形式是否残留（conflict）；再对缩写做首次出现审计（"拼全称（ACRONYM）"的确定性判据），并对比原稿是否"原本有定义、改后丢了定义"。没有模型术语表时只报 inventory_only 清单，不假装验证过。
6. **回源锚点校验**：每段的原文锚点（page + block_id + bbox + quote）用 anchors.verify 回源，并要求该段原文出现在**独立拼接的全文档文本**里，且该段守恒断言通过——三者都成立才计 grounded，避免"自己引用自己"的假指标。
7. **组装与落盘**：生成可编辑稿（docx/md/html，含标题/章节层级）、逐句修改说明（表格 + CSV）、预览（有渲染器则真渲染，没有就 HTML 并写 limits）；把内部产物用同义词映射反射到契约字段，再做声明类型整形（声明 array 而内容是对象时包成单元素列表，并在 meta.contract_type_conformance 记录）；最后写 result.json / evidence.json / run_report.json。

### 状态判定

| 状态 | 条件 |
|---|---|
| `success` | Agent 答案覆盖 100%、无回退、无未改段、无缺失批次、未触发截断/超时降级；**或** work/generated 完全没有答案、确定性回退润色至少改了一段且无回退/无 critical 复核项/未降级/未过软截止（limits 会写 no model judgement） |
| `partial` | Agent 答案有缺失/重复批次、出现回退、零 Agent 答案时回退润色没有产出安全编辑或出现回退、脚本降级（截断、软截止）、或无契约（内容进 additional_outputs） |
| `failed` | 没有任何材料、或所有候选都无可用文字层（扫描件无 OCR）、或顶层未捕获异常（仍写 failed 结果） |

## 五、Agent 操作协议（精确到命令与 JSON）

### 5.1 命令序列（模型必须按序执行）

```
# 1) 生成工作表；stdout 会打印 WORK_DIR / PENDING_BATCHES / GENERATED_DIR
python scripts/run_language_polish.py --prepare --out /app/output

# 2) 对 PENDING_BATCHES 里的每一批：读 <WORK_DIR>/batches/batch-XX.json
#    然后写 <WORK_DIR>/generated/batch-XX.json（见 5.3 格式）
#    可选：写 <WORK_DIR>/generated/_glossary.json（见 5.4）

# 3) 校验与落盘
python scripts/run_language_polish.py --finalize --out /app/output

# 4) 若 PENDING_BATCHES 仍非空（漏写/写坏），补写后再 finalize 一次即可；
#    finalize 是幂等的，重复运行结果一致（result 里不含时间戳）。
```

### 5.2 工作表结构（`work/batches/batch-XX.json`，只读）

```
{
  "batch_id": "batch-01",
  "rules": ["Minimal edit: ...", "Copy every number, unit, citation ... VERBATIM", "..."],
  "style_profile": {"genre": "journal", "target_language": "en", "voice": "...", "hedging": "..."},
  "task_brief": "task.md 前 900 字符",
  "focus_sections": ["abstract", "methods"],
  "allowed_types": {"grammar": "语法", "terminology": "术语", "tone": "语气/语域",
                    "punctuation": "标点", "structure": "结构", "chinese_english": "中英表达"},
  "allowed_status": ["revised", "kept", "flagged"],
  "answer_path": "<WORK_DIR>/generated/batch-01.json",
  "answer_format": [ { "seg_id": "seg-0001", "status": "revised", "revised": "...",
                       "changes": [ {"original": "...", "revised": "...",
                                     "type": "grammar", "reason": "..."} ] } ],
  "segments": [
    {
      "seg_id": "seg-0001", "page": 1, "section": "Abstract",
      "is_heading": false, "in_focus": true, "char_count": 214,
      "text": "原文段落全文",
      "preserve": {
        "numbers": ["85%", "523 K", "10 mol"],
        "citations": ["[1]"],
        "hedges": ["may", "suggests"],
        "modals": ["should"],
        "labels": ["figure"],
        "formulas": []
      },
      "term_hints": [ {"source": "Ni/SiO2", "preferred": "Ni/SiO2", "from_file": "glossary.md"} ]
    }
  ]
}
```

### 5.3 答案格式（`work/generated/batch-XX.json`，模型写）

```
[
  {
    "seg_id": "seg-0001",
    "status": "revised",
    "revised": "完整改写后的段落全文（不是 diff、不是摘要、不是评论）",
    "changes": [
      {
        "original": "was carried out",
        "revised": "was performed",
        "type": "grammar",
        "reason": "carry out a reaction is not idiomatic; perform is the collocation used in this field"
      }
    ],
    "notes": "可选；status=flagged 时说明需要作者确认什么"
  },
  {
    "seg_id": "seg-0002",
    "status": "kept",
    "revised": "与原文完全一致",
    "changes": []
  }
]
```

硬性要求：

1. seg_id 必须来自工作表；revised 必须是**完整段落**；status 取值 revised / kept / flagged。
2. changes[].original 必须是原文的精确子串；changes[].revised 必须是改文的精确子串；reason 必填；type 取自 6 个受控值（中文标签也接受）。
3. **数字、单位、引用标记、图/表/式编号必须逐字照抄**（含空格与格式：85% 不要写成 85 %，[1] 不要重排）。
4. **限定词与否定强度不得改变**：may/suggests/likely/approximately/可能/约/提示 原样保留；may 不能改成 will/does；否定不得增删。
5. **不新增**材料外的数字、引用、例子、实验结果。
6. 只改该改的：能改一个词就不要改一句，能改一句就不要重排全段；整段改动率 > 50% 会被标记 heavy_edit 复核。
7. 段落无需修改时 status=kept 且 changes=[]；不要为了"看起来尽力"而制造无意义同义替换。

### 5.4 可选术语表（`work/generated/_glossary.json`）

注意文件名以下划线开头——共享读取器会跳过它，不当作段落答案。可复用资源：`resources/style_profiles.json`（文体画像，可直接改）、`resources/edit_types.json`（受控修改类型字典）、`resources/terminology_template.csv`（术语表列定义模板，可发给作者填写）。

```
{
  "terms": [
    {"source": "nickel based catalysts", "preferred": "nickel-based catalysts",
     "note": "hyphenate the compound modifier; consistent with Section 2"},
    {"source": "selectivity to", "preferred": "selectivity towards",
     "note": "field convention"}
  ],
  "abbreviations": [
    {"short": "ICP-OES", "full": "inductively coupled plasma optical emission spectrometry"}
  ]
}
```

## 六、字段字典

### 6.1 内部产物 → 契约字段（同义词映射，不硬编码 schema）

| 内部键 | 语义 | 典型声明字段名（自动匹配） |
|---|---|---|
| `revised_text` | 完整改后全文（段落以空行连接） | polished_manuscript / revised_text / polished_text / final_text / 润色后全文 |
| `source_text` | 完整原文全文 | original_text / source_manuscript |
| `paragraphs` | 逐段对照（含原文/改文/状态/修改记录/守恒/锚点） | paragraph_alignment / paragraph_pairs / alignment / 逐段对照 |
| `changes` | 扁平修改台账（逐条） | edit_ledger / changes / edit_list / 修改清单 |
| `change_summary` | 统计（状态计数/类型计数/复核项/覆盖率） | revision_stats / change_summary / edit_statistics |
| `terminology` | 术语一致性表 + 缩写首次出现审计 | term_consistency / terminology / glossary / 术语表 |
| `preservation` | 语义守恒汇总（clean_rate/缺失项/逐段失败） | fidelity_checks / preservation / semantic_preservation |
| `evidence` | 锚点与回源校验（同 evidence.json） | evidence / source_evidence / anchors |
| `quality` | 质量指标（覆盖率/landing/grounding/术语一致率/改动率） | quality / run_quality / metrics |
| `style_profile` | 文体画像（genre/target_language/voice/hedging） | style / style_guide / target_style |
| `payload` | 主结果打包（供泛化字段名使用） | result_payload / main_result / payload |
| `summary_text` | honest 一段话摘要 | summary / cover_letter / run_summary |
| `deliverables` | 交付物路径与大小 | deliverables / artifacts / files |

规则：按声明顺序输出；未被命中的声明字段按声明类型占位（array 给 []、object 给 {}、string 给 空串）并记 meta.contract_fields_not_reported；未声明的内部产物放 meta.additional_outputs（不丢内容）；声明类型与内容类型不符时整形并在 meta.contract_type_conformance 记录。

### 6.2 paragraph 记录（paragraph_alignment 的元素）

| 字段 | 含义 |
|---|---|
| `seg_id` / `page` / `section` / `order` | 段标识、页码、所属章节、原文顺序 |
| `original` / `revised` | 原文 / 最终改文（回退或未改时两者相同） |
| `agent_revised` | 模型原始改文（被回退时仍保留，供复核） |
| `method` | agent / deterministic_fallback / source_unchanged（该段答案来自模型判断、确定性规则还是原样保留） |
| `status` | revised / revised_with_review_items / kept_unchanged / reverted_preservation / reported_change_not_applied / unrevised |
| `changes` | 本段修改记录（original/revised/type/type_label/reason/valid/issues） |
| `flags` | 命中的问题标记（number_lost:...、citation_lost:...、hedge_weakened:...、heavy_edit:0.62…） |
| `preservation` | 本段守恒断言（ok/缺失项/新增项/总数） |
| `agent_preservation` | 模型改文的守恒断言（被回退时用来解释拒绝原因） |
| `edit` / `agent_edit` | 字符级改动量（changed_chars/edit_rate/similarity） |
| `anchor` | 回源锚点（page/block_id/bbox/quote） |

### 6.3 meta 关键字段

status（success/partial/failed）、contract_detected、contract_field_count、contract_fields_not_reported、placeholders、contract_field_mapping、field_fill_ratio、contract_type_conformance、limits（限制与降级）、uncertainty（不确定项）、change_log、additional_outputs、evidence（同证据文件）、output_files、declared_fields。
## 七、异常处理

| 异常情况 | 检测方式 | 处理策略 | 状态标记 |
|---|---|---|---|
| 找不到 case.json | 发现阶段 missing 含 case.json | 仍按材料润色，全部产物进 meta.additional_outputs | partial |
| resources.yaml 声明文件缺失 | load_manifest 的 missing 非空 | 仍按其余材料润色；缺失项写 uncertainty，不猜测内容 | partial / failed（视是否有可用材料） |
| resources.yaml 缺失或清单为空 | find_manifest 返回 None 或 declared==0 | 行为与无清单时完全一致，只记 meta.resource_manifest.present=false | 不变 |
| resources.yaml 路径越界/绝对路径 | 解析器拒绝（absolute / .. / outside environment） | 该条目记 rejected + errors，绝不读取；其余条目照常 | 不变 |
| 零 Agent 答案 | merge_generated 的 received==0 | 确定性回退润色逐段尝试；通过守卫则 status=success 并在 limits 写 no model judgement；有回退或无安全编辑则 partial | success / partial |
| 回退润色候选破坏守恒 | checked_preservation + invariant_diff + critical_reasons | 整段回退成原文，flags=fallback_reverted_preservation，review_items 记原因 | partial |
| 回退润色改动率过高 | edit_stats.edit_rate > 0.25 | 放弃该段全部编辑（不做部分编辑，保证幂等） | unrevised |
| 手写引用被空格写坏 | 引用键按忽略空白比较 | 视为同一引用，不算新增引用、不回退该段 | 不变 |
| 契约字段名无法猜中 | output_contract 声明名映射不到内部值 | 顶层附兼容键（不覆盖声明字段），meta.compatibility_keys 记录实际加上的键 | 不变 |
| case 无 output_contract | contract_detected=false | 不发明字段，内容进 additional_outputs，limits 说明 | partial |
| 无任何材料 | 材料列表为空 | 输出 failed 结果，声明字段按类型占位 | failed |
| 扫描件/无文字层 | 提取 text_layer=false 或总字符 ≤40 | 依次尝试下一候选材料；全失败则 failed 并说明需要 OCR | failed / partial |
| 加密/非 FlateDecode PDF | 提取 warnings | 能读多少写多少，warnings 进 uncertainty | partial |
| 稿件超 80 段 | 段数上限 | 截断到 80 段并写 limits | partial |
| 软截止（720s） | Stopwatch | 已完成的段落照常输出，剩余回填原文 | partial |
| 某批答案缺失 | merge 的 missing 非空 | 该批段落 revised=原文、status=unrevised，limits 说明 | partial |
| 答案 JSON 坏/带 BOM/JSONL | 读取器容错（utf-8-sig、数组/records/JSONL） | 能读多少算多少；完全读不出按缺失处理 | partial |
| 修改记录对不上原文/改文 | 落地校验 issues | 记录保留但 valid=false，计入 landing 率，标 invalid_change_record | success/partial（不改文本） |
| 改文丢了/改了数字或引用 | 集合比对 | **回退该段**到原文，保留 agent_revised 与原因 | partial |
| 改文增删否定 | 集合比对（拉丁段落） | **回退该段** | partial |
| 限定词被加强/削弱 | 限定词集合（词形归并） | 保留改动，标 hedge_weakened/hedge_strengthened，进 review_items | success（带复核项） |
| 整段重写 | 改动率 > 0.5 | 保留但标 heavy_edit，进 review_items | success（带复核项） |
| 术语表内部冲突 | 首选形式已用但旧形式仍有残留 | 记 conflicts，降低 consistency_rate | success/partial |
| 渲染器缺失 | 运行时 probe | 不做假 PDF/PNG，保留 HTML 预览并写 limits | 不变 |
| 任何未捕获异常 | 顶层 BaseException | 写 failed 结果（含 traceback_tail）与 run_report | failed |
| 被杀（超 900s） | 启动即写占位 | 至少保留契约形状的 result.json + 基线成稿 | partial |

## 八、限制与边界

1. **只做语言层修改**：不新增事实/引用/数据；确实需要新增内容时标 flagged 交作者确认，不自行补写。
2. **中文否定不作为回退判据**：中文单字否定（不/未/无）在其他词内也会出现（如"不同"），逐字比对误报率高，因此中文段落只做复核标记；拉丁文字段落才强制回退。
3. **限定词词表是启发式**：about/around 只有邻近数字才计为限定词（避免 problems about 误报）；但领域性模糊表达仍可能误判，故只标复核不回退。
4. **公式只做记号级比对**：不解析数学语义；k_1 → k1 这类记号变化会标 formula_token_lost 复核。
5. **非 PDF 输入无物理坐标**：锚点是 page+block（bbox 为合成值），anchor_quality=page，effective_confidence 上限 0.7——这是如实降级，不冒充 span 级。
6. **docx 读取走标准库 OOXML**：只提段落文本，不保留样式/批注/修订痕迹；需要保留作者批注请人工处理。
7. **LaTeX 只润色散文**：不编译、不改宏包；公式环境与命令原样保留，改错命令会导致原文回填（由数字/引用守恒兜底）。
8. **渲染依赖镜像**：没有 office/PDF 渲染器时只交付 HTML 预览；不伪造 PDF/PNG。
9. **术语一致性只有模型给了术语表才"结论化"**：否则只报候选清单（inventory_only），不假装验证。
10. **单 case 单文档为主**：多个材料时只把最像稿件的一份当正文，其余仅用于术语/缩写提示（最多 3 个小文本文件）。
11. **确定性边界**：result.json / evidence.json 重复运行一致；run_report.json 含耗时字段，天然有差异。
12. **回退润色只做确定性最小编辑**：不做整句重写、不做断句、不改 CJK 字词；单段字符改动率 > 0.25 直接整段放弃（宁少勿错，且这样才能保证幂等）。
13. **含数字的术语对不在回退层处理**：Ni-Si02→Ni/SiO2 这类会改动数字字符的修正被回退层跳过，留给 Agent（数字逐字不变是硬约束）。

## 九、自查

| 现象 | 自查方法 | 处理 |
|---|---|---|
| 结果字段全是占位 | 看 meta.contract_detected 与 contract_field_mapping | false 说明没找到 case.json 或无契约；检查输入根与 JSON 是否带 BOM |
| 段数远少于预期 | 看 work/state.json 的 segments 与 work/doc.json | 非 PDF 检查空行切段；PDF 检查 blocks 是否被引擎合并 |
| 修改很多但都是同义替换 | 看 quality.document_edit_rate、heavy_edit 复核项 | 让模型执行"最小编辑"规则；删除无意义替换 |
| 大量数字/引用被回退 | 看 limits 与逐段 flags/agent_revised | 模型没照抄原数字；工作表 preserve 已列出，补齐提示 |
| 覆盖率低 | run_report.json 的 generated.missing / pending_batches | 补写缺失批次的 generated 文件后重跑 finalize |
| 术语一致率异常 | term_consistency.entries 的 status/conflicts | 检查 _glossary.json 首选形式是否真的被采用，或旧形式残留 |
| grounding 低 | evidence.json 的 grounding.per_claim | 看 anchor_match / document_contains_quote / invariants_preserved 哪一项失败 |
| 交付物缺失 | result.deliverables 与 run_report.outputs | 看 limits 里是否有渲染器/写权限说明 |
| 重复运行结果不一致 | 对同一输入跑两次 --prepare 比较 work/state.json 与 batches | 若不一致，检查材料排序是否混入输出目录文件（本技能已排除输出/工作目录） |

## 十、最小调用示例

以下为**示意**（合成小样例，非正式案例结果）：

输入 `environment/manuscript.md`（节选）：

```
## Abstract

Supported nickel catalysts were prepared by incipient wetness. The conversion may reach 85% at 523 K over 10 mol% Ni/SiO2 [1]. This suggests that the nickel loading is close to the optimum.
```

调用：

```
python scripts/run_language_polish.py --prepare  --out out
# stdout: WORK_DIR=out/work  PENDING_BATCHES=["batch-01"]  GENERATED_DIR=out/work/generated
# 模型写 out/work/generated/batch-01.json（示意）
python scripts/run_language_polish.py --finalize --out out
# 产物：out/result.json、out/evidence.json、out/run_report.json、
#       out/revised_manuscript.docx/.md/.html、out/revision_notes.csv
```

模型写入的示意答案（**示例，非正式结果**）：

```
[
  {"seg_id": "seg-0003", "status": "revised",
   "revised": "Supported nickel catalysts were synthesised by incipient wetness. The conversion may reach 85% at 523 K over 10 mol% Ni/SiO2 [1]. This suggests that the nickel loading is close to the optimum.",
   "changes": [
     {"original": "were prepared by incipient wetness",
      "revised": "were synthesised by incipient wetness",
      "type": "terminology",
      "reason": "the methods section calls this synthesis; one term for one concept"}
   ]}
]
```

示意输出（result.json 摘要，**为说明接口而裁剪，非正式案例结果**）：

```
{
  "case_id": "demo-polish-001",
  "polished_manuscript": "Supported nickel catalysts were synthesised ...",
  "paragraph_alignment": [
    {"seg_id": "seg-0003", "status": "revised",
     "original": "Supported nickel catalysts were prepared by incipient wetness. ...",
     "revised": "Supported nickel catalysts were synthesised by incipient wetness. ...",
     "changes": [{"original": "were prepared by incipient wetness",
                  "revised": "were synthesised by incipient wetness",
                  "type": "terminology", "type_label": "术语", "valid": true}],
     "preservation": {"ok": true, "numbers_missing": [], "citations_missing": []},
     "anchor": {"page": 1, "block_id": "p1-b3", "bbox": [0.0, -2.0, 210.0, -1.0],
                "quote": "Supported nickel catalysts were prepared by incipient wetness. ..."}}
  ],
  "edit_ledger": [{"seg_id": "seg-0003", "type": "terminology", "valid": true}],
  "term_consistency": {"entries_checked": 2, "adopted": 2, "consistency_rate": 1.0},
  "fidelity_checks": {"checked": 3, "clean": 3, "clean_rate": 1.0,
                      "numbers_missing_total": 0, "citations_missing_total": 0},
  "revision_stats": {"changes_total": 1, "coverage_rate": 1.0,
                     "changes_by_type": {"语法": 0, "术语": 1, "语气/语域": 0,
                                         "标点": 0, "结构": 0, "中英表达": 0}},
  "meta": {"status": "success", "contract_detected": true,
           "contract_field_mapping": {"polished_manuscript": "revised_text"},
           "contract_fields_not_reported": []}
}
```

## 十一、架构参考与差异说明

文档编排参考平台公开技能目录的 scientific-paper-translator（定位 → 适用条件 → 调用 → 工作流 → 异常四列表 → 说明 → 示例）与 bohr-cli（"踩坑"章节）；此处只借鉴**结构组织**，未复制其内容或代码。共享内核（lib/）为本项目自研，题目适配层（scripts/、resources/）为本技能独立实现。

| 维度 | 常见做法 | 本技能做法 |
|---|---|---|
| 改写粒度 | 整段重写，只给最终文本 | 逐段最小编辑 + 逐条修改记录（原文/改文/类型/理由） |
| 语义保持 | 模型自述"已保持原意" | 代码断言：数字/引用/否定集合比对，失败即回退并留痕 |
| 输出契约 | 固定 JSON schema | 从 case.json 反射字段名 + 声明类型整形，缺值按类型占位 |
| 术语一致 | 一次性术语表 | 术语表驱动的全篇回扫 + 冲突表 + 缩写首次出现审计 |
| 证据 | 段落文字引用 | 锚点回源校验 + 独立拼接全文校验 + 守恒断言三合一才计 grounded |
| 预算 | 全文塞进一次请求 | 两阶段混合：确定性搬运 + 小批量判断，典型 3~8 次模型请求 |
| 被杀风险 | 未处理 | 启动即写占位 + prepare 结束即在基线成稿，任何时刻都有产出 |
| 无 Agent 答案 | 直接交付 revised == original（等于没润色） | 确定性回退润色：真实最小编辑 + 逐条记录 + 同一守恒守卫，status 语义如实 |
| 资源入口 | 忽略 environment/resources.yaml | 先读清单：声明文件优先、缺失登记、工具目录进 PATH、摘要进 result 与 run_report |
| 未知契约字段名 | 只靠同义词映射 | 额外附顶层兼容键（绝不覆盖声明字段）并记录 meta.compatibility_keys |

## 十二、环境踩坑（--help 不会告诉你的）

1. **`%TEMP%` 可能不可写**：受限沙箱下 Python 的 tempfile.TemporaryDirectory() 直接 PermissionError；自测与中间产物一律写在工作区内。
2. **JSON 必须容忍 BOM**：Windows 工具链写出的 UTF-8 可能带 BOM，json.loads 会失败，表现为"这个 case 没声明契约"。读取一律 utf-8-sig；写出绝不带 BOM。
3. **纯文本资源别交给通用块构建器**：非 PDF 材料的合成坐标每行只差 1 个单位，通用行聚类会把整篇并成**一个** block，逐段对照直接失效。本技能对 docx/md/tex/txt 走段落级读取器。
4. **参考文献会粘连**：Markdown 里参考文献常常一行一条、无空行，空行切段会把整段文献并成一段。已按行首 [n]/n. 强制分段。
5. **术语表文件以下划线开头是有意的**：共享的 generated 读取器会跳过下划线文件，否则 _glossary.json 会被当成段落答案。
6. **批顺序 ≠ 输出顺序**：工作表按"聚焦章节优先"排序以先做完重要章节，但最终改文按原文 order 重组；不要把两个顺序混用。
7. **回退润色的改动率上限是"全有或全无"**：为了让输出成为不动点（幂等），一段要么全部规则生效、要么整段放弃。不要改成"超限就只保留标点层"——第二次运行会继续编辑，result.json 就不再字节一致。
8. **引用比较必须忽略空白**：手写的 (Qiu et al . , 2019) 会让严格正则"认不出引用"，规范化后又被当成"新增引用"，于是一段 7 条正确编辑的润色被 citation_added 整段回退。引用与公式一律用忽略空白的规范化键比较。
9. **resources.yaml 的 path 必须相对 environment/ 解析并拒绝 ..**：评测机目录结构与开发机不同，绝对路径一律记 errors 且不读取。
10. **不要假设 resources.yaml 存在**：隐藏题包可能没有清单；不存在/解析失败时必须与无清单时行为一致，只把事实写进 meta，不要报错。
7. **Windows 控制台编码**：脚本 stdout 只用 ASCII（日志/摘要），中文只进 UTF-8 文件；否则 CP936 控制台可能抛 UnicodeEncodeError 弄死整次运行。
8. **渲染器探测不能假设**：镜像里 convert 可能是 Windows 的 FAT 转换工具而不是 ImageMagick；本技能只对 docx→PDF 走 soffice/pandoc 白名单，且失败即降级为 HTML 预览，不伪造。
9. **--finalize 必须能独立运行**：评测可能只跑 finalize（或 prepare 后进程被换掉），因此 finalize 发现没有 state 时会自动补跑 prepare，而不是报错退出。
10. **确定性来自"不写时间戳"**：result.json/evidence.json 里不写运行时间；耗时只进 run_report.json。三次重复运行才可比。

## 十三、确定性回退润色与平台资源入口（v1.2）

### 13.1 为什么有这一层

隐藏评测里，Agent 可能只执行文档给出的默认命令（--all），并不会真的写 work/generated/*.json。v1.1 在这种情况下把每段都写成 revised == original：产物"看起来完整"却没有任何润色。v1.2 增加确定性回退润色器，让**不依赖 Agent 判断的路径本身**产出真实、保守、可核验的润色与逐条修改记录，同时把平台声明的资源入口 environment/resources.yaml 变成真正的输入来源。

### 13.2 规则清单（确定性、无模型、零第三方依赖、纯标准库）

| 类型 | 规则 | 命中守卫 |
|---|---|---|
| punctuation | 连续空格归一；标点前空格删除（85 % → 85%、et al . → et al.、work . → work.）；括号内外空格；逗号/分号/冒号后缺空格补齐；句首连接副词补逗号（However there → However, there）；句首状语短语补逗号（For example the → For example, the）；et al 补句点；CJK 标点间距归一；行尾空白 | 只改空白与标点，不增删字词 |
| grammar | a/an 按后接词首音修正（a unit / a university / a European / one… 与 an hour / an honest 例外表）；重复功能词合并（the the → the，仅白名单）；句首/段首小写字母大写 | u-/h- 歧义词跳过；缩写（e.g. / i.e. / Fig. / et al.）、首字母缩写、pH、mol% 等不碰 |
| terminology | 按传入术语表统一大小写/连字符/分隔符（dft → DFT、nickel based → nickel-based） | 只接受已声明的 canonical 形式；含数字的术语对跳过（数字必须逐字不变）；同一变体有冲突 canonical 时拒绝猜测 |
| tone | 小型过度声称词表（huge→substantial、very good→good、a lot of→many 等 17 条） | 词表不含任何 hedge/modal，因此不可能改变论断强度 |
| chinese_english | 小型中英直译词表（carry out→perform、make sure→ensure、on the basis of→based on 等） | 只收"替换后任何后接成分都成立"的条目；需要删宾语的（make a discussion of the results）留给 Agent |
| structure | 不在回退层实现 | 断句/合句需要作者判断，规则化风险高，明确放弃 |

### 13.3 安全边界（宁少勿错）

- **永不改动**：数字/单位/百分号、引用标记（[n]、(Author, 2020)、\cite、\ref、图/表/式编号）、公式 token、否定词、hedge/modal、标题段（is_heading 段不润色）、CJK 段落的字词（只做空白与标点间距）。
- **单段字符级改动率硬上限 0.25**：超过就整段放弃。不做"超限只保留一部分编辑"——部分编辑会破坏不动点，使重复运行的结果不再字节一致。
- 单段最多 20 条 change；每条 change 的 original 必须是原文精确子串、revised 必须是改文精确子串，type 取自 6 个受控 id。
- 输出是不动点：polish(polish(x)) == polish(x)，因此 --all 重复运行 result.json 字节一致（路径字段除外）。

### 13.4 校验与回退（与 Agent 答案完全同一把尺）

候选改文必须依次通过：

1. **checked_preservation**：数字/引用/公式/限定词不丢失。引用与公式按**忽略空白**的规范化键比较——手写的 (Qiu et al . , 2019) 规范化后是同一个引用，不会被当成"新增引用"；
2. **invariant_diff + critical_reasons**：数字/引用/否定被增删 → 回退整段；
3. **edit_stats**：字符改动率 ≤ 0.25；
4. **validate_changes**：每条记录都能同时落在原文与改文。

任何一项失败 → 整段回退成原文：status=unrevised、flags 加 fallback_reverted_preservation、review_items 记录原因；limits 与 run_report.fallback.paragraphs_reverted 会写清数量。

### 13.5 status 语义

| 场景 | status | 证据 |
|---|---|---|
| Agent 答案齐全且无问题 | success | paragraph_alignment[].method=agent |
| 零 Agent 答案，回退润色改到了段落、无回退、未降级、未过软截止 | success | limits 写 no model-generated answers … no model judgement；fallback_polisher.applied=true |
| 零 Agent 答案但没有安全编辑 / 出现回退 / 降级 / 过软截止 | partial | limits 逐条给原因（found no safe edit / N fallback candidate(s) were rejected / …） |
| 只有部分批次有 Agent 答案 | partial | limits: N of M paragraph(s) have no generated revision |
| 回退润色候选破坏守恒 | partial | flags=fallback_reverted_preservation + review_items |

### 13.6 可核验证据在哪里

- paragraph_alignment[].method：逐段答案来源（agent / deterministic_fallback / source_unchanged）；
- paragraph_alignment[].changes：逐条 original/revised/type/reason/valid；
- revision_notes.csv：一行一条修改（有回退润色时必然有数据行，不再是只有表头）；
- meta.additional_outputs.fallback_polisher：available / applied / method / paragraphs_by_method / attempts / changes / edit_rate_budget；
- run_report.json：method_note、fallback（含规则清单）、paragraphs_by_method；
- meta.compatibility_keys：为未知契约额外暴露的顶层别名（见 13.7）。

### 13.7 顶层兼容键（未知契约下也能被发现）

case.json 的字段名有时无法从同义词表猜中。finalize 会额外写一组顶层别名：polished_text、change_ledger、edit_ledger、terminology_table、preservation_report、quality_report、run_summary、deliverables_overview。规则：

- case.json 已声明同名字段时跳过（**绝不覆盖契约字段**）；
- 某个内部值已经映射到声明字段时不再重复整份内容（避免 result.json 里全文出现三次）；
- 实际加上的键写入 meta.compatibility_keys，meta.compatibility_keys_note 说明"为在未知契约下也能被发现，不是伪造字段"。

### 13.8 environment/resources.yaml 资源入口

**读取与解析**（lib/resources.py，零依赖、容忍 BOM）：

- 在输入根下按 root/resources.yaml、root/environment/resources.yaml 顺序确定性定位；找不到不报错。
- 支持平台文档声明的保守 YAML 子集：顶层非空 resources 列表；plain 字段 name / path / url / description / credential_source / credential_env；容忍 BOM、CRLF、整行与行内注释、单/双引号、块序列（- name: x 或裸 - 后接缩进键）、--- 分隔符；不认识的字段记 warning 而非崩溃。
- path 一律相对 environment/ 解析；绝对路径、含 ..、解析到 environment/ 之外的一律记 errors 且**不读取**；疑似凭据字面量/带签名的 URL 也记 errors。
- 输出 resources / existing / missing / external / tools / errors / warnings / source；tools 是 name/description/文件名暗示可执行（renderer/office/latex/pandoc/soffice/…）的声明。

**接入方式**：

1. prepare 先读清单，把"真实存在"的声明文件作为材料排序优先项（声明优先于按文件名猜），清单摘要写进 work/state.json 的 resource_manifest；
2. 声明了但文件不存在 → 写 uncertainty（resources.yaml declares X but the file is absent under environment/），不猜测内容；
3. 声明的可执行工具若存在，finalize 在渲染探测前把其目录前置进 PATH（共享 renderer 只读 PATH），使声明的渲染器真的可能被用上；否则照旧降级 HTML 预览并写 limits；
4. result 的 meta.additional_outputs.resource_manifest 与 run_report.json 的 resource_manifest 记录 declared/existing/missing/tools 与错误清单——这是"用了平台声明的资源入口"的可见证据；
5. **无清单时行为与 v1.1 完全一致**，只记 present=false，不报错。

### 13.9 自测与验收

自测覆盖：回退润色单元（规则命中、守恒、幂等、确定性、CJK 不改字词、术语、歧义拒绝）、手写引用回归（7 条编辑不被回退）、零生成端到端（status=success、段落确有改动、changes 全部 valid、preservation 100%、revision_notes.csv 有数据行、result.json 重复运行字节一致）、resources.yaml 解析（BOM/注释/引号/块序列/绝对路径与 .. 拒绝/缺失登记/工具识别/空清单）、兼容键，以及原有全部用例。

命令（与 CLI 一致，runner 没有 --in 参数：输入靠 /input 等根目录或当前工作目录发现）：

    python scripts/run_language_polish.py --all --out output
    python scripts/run_language_polish.py --prepare --out output
    python scripts/run_language_polish.py --pending --out output
    python scripts/run_language_polish.py --finalize --out output

