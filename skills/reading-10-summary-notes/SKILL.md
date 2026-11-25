---
name: reading-10-summary-notes
description: Turn a real research paper into section-aligned structured notes — a uniform paper card (research question / data / methods / findings / conclusions / limitations), per-section notes in the paper's own order, key points, limitations and a retrieval index — where every key note carries page + block_id + bbox + verbatim quote that is re-verified against the source block, plus independent number/citation fidelity checks. Emits result.json by reflecting the case's declared output_contract. Use when a task asks to summarise, note, or structure research literature and requires traceable evidence instead of a compressed abstract.
version: 1.0.0
author: 66Union
license: MIT
tags: [paper-notes, section-alignment, paper-card, evidence-anchors, output-contract, two-phase-pipeline]
---

# 文献总结与结构化笔记（Reading-10 Summary Notes）

面向赛题「阅读10｜文献总结与结构化笔记」：把一篇真实论文变成**与论文自身章节对齐**、**字段齐备**、**每条关键笔记都能回到原文位置**的可复用笔记，而不是一段压缩后的通读摘要。

## 一、技能定位

本题考察四个问题，本技能的对应关系是「问题 → 做法 → 可验证的证据」：

| 考察问题 | 本技能的做法 | 可验证证据 |
|---|---|---|
| **对得上**（骨架与论文自身章节、论证顺序一致） | 章节来自论文自己的标题（已知标题名 / 编号标题 / 全大写短行 / 版面标题四条判据），段落在切分时**继承所属章节 id**；笔记按 section 分组并按原文顺序输出；纯标题节（如论文标题）标为 navigation，只作导航、不要求写笔记、也不计入覆盖率分母 | `section_notes[].section_id`、`section_notes[].notes[].section_id`；`coverage.notes_per_section`；`sections[].heading_anchor` |
| **留得住**（研究问题、数据、方法条件、主要发现、局限都在） | 六字段论文卡片（research_question / data / methods / findings / conclusions / limitations）；卡片候选句子由受控关键词 + 章节提示确定性排序后交给 Agent 判断；找不到的字段写 `not reported` 并单列 | `paper_card`、`coverage.card_fields_*`、`limitations[].scope=paper|reading_pipeline` |
| **找得回**（每条笔记指回章节、段落、页码、图表） | 每条笔记 = page + block_id + bbox + quote 四元组；quote 用共享内核 anchors 回源校验（exact / normalised / block_only / page_only / missing），未过即标 ungrounded；位置主张标 structural | `evidence.json` 的 `claims[].anchor/match/located/supported`、`quality.grounding`、`quality.evidence_density` |
| **用得上**（可检索、可比较、可继续加工） | 输出结构化 `key_points`、按节 `section_summaries`、术语表 `glossary`、检索索引 `index`（term / point_type / section → seg_id）、可直接使用的 Markdown 笔记 `notes_markdown` | `index.by_term / by_point_type / by_section`、`notes_markdown` |

**与常见做法的差异**：多数同类技能让模型自述「已核验」；本技能把核验写成**可执行断言**——引文必须真的出现在所引 block 中，笔记里的数字和引用必须真的出现在全文里，跨语言笔记会显式降级支持判定并写进 `support_check`。另一处差异是**契约反射**：隐藏案例的字段名不可知，任何写死的 schema 都是错的。

## 二、适用条件

| 项目 | 说明 |
|---|---|
| 输入格式 | PDF（有文字层最佳；内置解析器兜底）、JATS XML、DOCX、PPTX、TEX、Markdown、纯文本 |
| 语言 | 不限。论文为英文而笔记写成中文也可以（支持判定会标注 `cross_language_anchor_only`） |
| 篇幅 | 单页到数百页；段数超过 400 时截断并写进 limits，避免工作表撑爆单次响应 |
| 依赖 | **仅 Python 标准库**；若镜像里有 PyMuPDF / pdfplumber / pdftotext 等则自动升级保真度，没有也不失败 |
| 运行预算 | 脚本自身零模型调用、零联网；完整流程通常在 3 秒内，适合 900 秒 / 64 次请求的评测预算 |
| 不适用 | 需要理解图表科学含义、纯扫描件 OCR、手写体、无文本层的加密文档（会如实标 failed/partial，不臆造） |
| 输出契约 | 必须由 `case.json` 的 `output_contract` 驱动；技能不预设任何字段名 |

## 三、调用方式

### 触发方式

当任务要求「读这篇论文并做笔记 / 总结 / 结构化 / 卡片 / 章节摘要 / 知识点」，且要求「结论要能回到原文」时触发。

### 输入（路径不硬编码，按特征发现）

```
/input/case.json          必选，含 case_id 与 output_contract（字段名由题目决定）
/input/task.md            必选，题目说明
environment/**            随题下发的材料（paper.pdf 等），位置不限，自动扫描
```

发现顺序：探测 `/input`、`/app/input`、`/app/work`、`/app/environment`、`/app`，最后退到当前目录；每个根都扫。材料按「paper.pdf 优先 → reference/ 下优先 → PDF 优先 → 体积降序 → 路径字典序」排序，保证三次重复运行的主文档一致。

### 输出

写入输出目录（默认 `/app/output`，可用 `--out` 覆盖）：

| 文件 | 内容 |
|---|---|
| `result.json` | **契约字段 + case_id + meta**。声明字段按声明顺序输出；未产出按声明类型占位（array→`[]`、object→`{}`、string→`""`），并记入 `meta.contract_fields_not_reported` 与 `meta.placeholders`；映射关系在 `meta.contract_field_mapping` |
| `evidence.json` | 每条笔记/卡片字段的锚点、回源校验结果（match/located/supported/grounded）、数字与引用保真检查、生成批次统计、章节清单 |
| `run_report.json` | 状态、耗时、预算、限制、不确定性、输入发现结果、实际读过的文档与引擎、资源告警 |

工作目录（默认 `/app/work`，不可写则退到 `<out>/work`，可用 `--work` 覆盖）：

| 产物 | 内容 |
|---|---|
| `work/sections.json` | 章节清单：section_id、标题、层级、页码区间、seg_ids、标题锚点 |
| `work/facts.json` | 每段的机器可判事实：数字/单位、引用、限定词、图表标签、缩写；全篇术语候选 |
| `work/card_worksheet.json` | 六字段卡片工作表：每字段的问题、候选 seg 与候选句（含数字/引用） |
| `work/batches/batch-XX.json` | 分批笔记工作表：每段原文 + 事实 + point_type 建议 + 应写的答案文件路径 |
| `work/manifest.json` | 一次运行的总目录：数量、工作表路径、应写文件清单、Agent 四步协议、词表 |
| `work/generated/_README.json` | 生成文件的精确 schema 与规则（以 `_` 开头，不会被读入） |

### 三种模式

```bash
# 1) 准备（确定性）：发现输入 → 提文本 → 切章节 → 切段 → 抽事实 → 写工作表
python scripts/run_summary_notes.py --prepare --out /app/output --work /app/work

# 2) Agent 判断（模型写文件）：/app/work/generated/card.json + batch-XX.json

# 3) 收尾（确定性）：校验覆盖/守恒 → 回源校验锚点 → 反射契约 → 写三个结果文件
python scripts/run_summary_notes.py --finalize --out /app/output --work /app/work

# 兜底：没有生成内容时直接跑完整流程，会降级为确定性的抽取式基线并标 partial
python scripts/run_summary_notes.py --all --out /app/output
```

不带参数等价于 `--all`。任何模式下**启动即写占位 result.json**（status=partial），所以即使进程在 900 秒处被杀，产物也不是零字节；顶层再包一层 `BaseException` 兜底写 status=failed。退出码：0 表示已产出结果（含 partial/failed 的诚实结果），1 表示最后由异常兜底写入。

## 四、字段字典

### 4.1 论文卡片六字段（`card_fields`，见 resources/note_fields.json）

| key | 中文 | 取义 | 候选来源（工作表排序依据） |
|---|---|---|---|
| `research_question` | 研究问题 | 论文要回答的核心问题 | Abstract / Introduction，含 we investigate / aim / purpose / 本研究 等 |
| `data` | 数据与材料 | 数据、样本、语料、材料、规模、来源 | Abstract / Methods / Results，含 dataset / sample / we used / n = 与数字单位 |
| `methods` | 方法与条件 | 方法、模型、参数、实验条件、分析流程 | Methods / Experiment，含 method / prepared / measured / condition |
| `findings` | 主要发现 | 带数值或方向性的结果 | Results，含 show / increase / decrease / 表明 / 发现 与数字 |
| `conclusions` | 结论 | 作者解释、结论、意义（保留限定词） | Conclusion / Discussion，含 conclude / suggest / 结论 / 综上 |
| `limitations` | 局限与前提 | 局限、前提、未报告、未来工作 | Limitation / Discussion / Conclusion，含 limitation / however / cannot / 局限 |

值被材料否定时写哨兵 `not reported`（资源文件可改），该字段会进入 `coverage.card_fields_not_reported` 与 `limitations`（scope=reading_pipeline），**不补造**。

### 4.2 笔记类型受控词表（point_type）

`RQ` 研究问题、`DATA` 数据与材料、`METHOD` 方法与条件、`RESULT` 结果、`CONCLUSION` 结论、`LIMITATION` 局限与前提、`DEFINITION` 定义与术语、`RELATION` 章节承接、`GAP` 未报告/缺口。

`BASELINE` 是脚本兜底专用的保留类型：Agent 用了会被改写为 `NOTE` 并打 `reserved_point_type_used` 标记；词表外的类型会打 `unknown_point_type` 并归为 `NOTE`。

### 4.3 内部产物 key 与契约映射

内部值 key（优先级顺序即映射优先级）：`paper_card`、`section_notes`、`section_summaries`、`key_points`、`notes`、`limitations`、`coverage`、`evidence`、`quality`、`status`、`uncertainty`、`notes_markdown`、`research_question`、`data`、`methods`、`findings`、`conclusions`、`outline`、`sections`、`tables`、`formulas`、`references`、`frontmatter`、`glossary`、`index`。

映射由 `lib/hybrid.map_fields` 完成：精确名 100 分 → 同义词 80 分 → 前缀/包含 25–40 分，按分数、再按声明顺序稳定排序，分数低于 24 不采用。技能额外登记了一批常见同义字段名（随实现维护）：`structured_card→paper_card`、`chapter_notes→section_notes`、`knowledge_points→key_points`、`caveats→limitations`、`coverage_report→coverage`、`evidence_anchors→evidence`、`note_markdown→notes_markdown`、`terminology→glossary`、`search_index→index` 等。

**无法映射的声明字段一律按声明类型占位并列入 `meta.contract_fields_not_reported`**——这是「未报告要说出来」的实现，不是缺陷。未被声明但已产出的内容进 `meta.additional_outputs`（`sections`、`tables`、`glossary`、`index`、`notes`、`section_summaries`、`quality` 等），评审仍可看到。

### 4.4 锚点格式

| 字段 | 含义 |
|---|---|
| `page` | 1 起的页码，与提取引擎一致 |
| `block_id` | 段落块 id，形如 `p2-b3`（第 2 页第 3 块） |
| `bbox` | `[x0, y0, x1, y1]`，PDF 惯例（原点左下、y 向上）；非 PDF 输入为页/行级伪坐标，`anchor_quality` 会降级并写进 limits |
| `quote` | 从该块**逐字复制**的引文；不得改写、不得加省略号（否则校验降级） |
| `kind` | `content` 内容主张（引文须提到主张）/ `structural` 位置主张（只看位置是否成立） |

回源校验分级：`exact`（逐字命中）→ `normalised`（NFKC/空白归一后命中）→ `block_only`（块存在但引文不在块内）→ `page_only`（引文在本页其他块）→ `missing`。只有前两级算 located；`grounded = located 且支持判定通过且无编造数字/引用`。

### 4.5 质量与覆盖指标（`quality` / `coverage`）

- `grounding.grounding_rate`：笔记中 grounded 的比例；`located`、`supported` 分别统计；`anchor_quality` 与 `confidence_ceiling` 给出上限（span 1.0 / page 0.7 / text 0.4），`effective_confidence` 取两者较小值。
- `number_fidelity`：笔记里出现的数字必须出现在所属段（否则可以是同篇其他段），**否则记为 fabricated_numbers 并使该条 ungrounded**；引用 `[1]` 同理。
- `hedge_fidelity`：引文有限定词（may / suggest / 约 …）而笔记丢了，打 `hedge_dropped`（不致命，但计入 `uncertainty`）。
- `coverage`：章节覆盖、段覆盖、每节笔记数、卡片字段填充、已答/未答批次数、是否用了基线。覆盖率的分母是**内容节**（`content_sections_total`），纯标题节列在 `navigation_sections` 里不计分母，否则一套完整笔记也会显示不到 100%。
- `key_points`：正常来自 Agent 笔记；降级运行时抽取式基线条目也会进入该列表，并用 `source=deterministic_baseline` 标明来源，保证 partial 结果仍有可核验知识点。
- `status`：success 需同时满足——有生成笔记、卡片六字段全部由 Agent 生成、grounding ≥ 0.85、章节覆盖 ≥ 0.6、所有批次已答、无编造数字/引用；否则 partial；无可用文本段则 failed。

## 五、Agent 操作协议（精确到命令与 JSON）

### Step 0：环境自检

```bash
python --version                                  # 仅需标准库
python scripts/run_summary_notes.py --help        # 确认 --prepare / --finalize / --all 存在
```

### Step 1：准备

```bash
python scripts/run_summary_notes.py --prepare --out /app/output --work /app/work
# stdout 末尾会打印：PREPARE case=... sections=... segments=... batches=...
#                      worksheets: ...  NEXT 1 / NEXT 2 / NEXT 3
```

此时 `/app/output/result.json` 是 status=partial 的占位文件，**必须在 Step 5 覆盖它**。

### Step 2：读工作表（不要重新解析 PDF）

按需读：`work/card_worksheet.json`（六字段候选）、`work/sections.json`（章节清单）、`work/batches/batch-XX.json`（分批段）、`work/generated/_README.json`（schema）。每批 6 段左右，单批原文 ≤ 5000 字符，适合一次响应处理完。

### Step 3：写论文卡片 `work/generated/card.json`

```json
{
  "research_question": {"text": "Nickel loading controls CO2 conversion over silica.", "seg_id": "seg-0003", "quote": "We investigate the effect of nickel loading on CO2 conversion over silica."},
  "data": "10 mol% Ni on SiO2, measured at 523 K and 1 bar",
  "methods": {"text": "Incipient wetness impregnation, XRD and fixed-bed tests at 523 K.", "seg_id": "seg-0011", "quote": "Samples were prepared by incipient wetness impregnation at 300 C."},
  "findings": {"text": "Conversion rises from 42% at 5 mol% to 85% at 10 mol%.", "seg_id": "seg-0015", "quote": "Conversion increased from 42% at 5 mol% Ni to 85% at 10 mol% Ni."},
  "conclusions": {"text": "10 mol% Ni on SiO2 is the most active loading tested.", "seg_id": "seg-0022", "quote": "We conclude that 10 mol% Ni on SiO2 is the most active loading tested."},
  "limitations": {"text": "Only one support and one pressure were tested.", "seg_id": "seg-0020", "quote": "A limitation is that only one support and one pressure were tested."}
}
```

规则：六键齐全；值可以是字符串（锚点会弱一些）或 `{text, seg_id, quote}`；`quote` 必须从 `seg_id` 对应段的原文逐字复制；材料没写的字段写 `not reported`。

### Step 4：写分批笔记 `work/generated/batch-XX.json`

每批一个文件，文件名与工作表同名（`batch-01.json` …）。格式为数组（也接受 `{"records": [...]}`）：

```json
[
  {"seg_id": "seg-0015", "point_type": "RESULT",
   "note": "Conversion increased from 42% at 5 mol% Ni to 85% at 10 mol% Ni.",
   "quote": "Conversion increased from 42% at 5 mol% Ni to 85% at 10 mol% Ni."},
  {"seg_id": "seg-0020", "point_type": "LIMITATION",
   "note": "Only one support and one pressure were tested, so generality is untested.",
   "quote": "A limitation is that only one support and one pressure were tested."}
]
```

规则（finalize 会逐条检查）：

1. 每个 `seg_id` 最多一条；`seg_id` 必须来自工作表，不要自己发明。
2. `quote` **逐字复制**该段原文，不改写、不加省略号、不拼接其他段；没有引文也能通过（会标 `quote_not_supplied` 并按段首自动截取），但证据强度下降。
3. `point_type` 从 4.2 词表取；不确定就写 `RESULT`。
4. 笔记里的数字、单位、引用必须真的出现在论文里：编造会让该条 ungrounded、把 status 打成 partial，并写进 `limits`。
5. 笔记归属由 seg 自动继承的 section 决定，**不要跨节合并**；保持原文论证顺序。
6. 材料没给的内容写 `point_type=GAP`，不要用推测补齐。
7. 一次写一整批（一个文件），减少往返；能答的批次都要答，未答批次会进 `coverage.generated_batches_missing` 并阻止 success。
8. 笔记长度上限 320 字符（超出会截断并保留原分句），单条只表达一个判断。

### Step 5：收尾

```bash
python scripts/run_summary_notes.py --finalize --out /app/output --work /app/work
# stdout：FINALIZE case=... status=... contract_fields=... mapped=... not_reported=[...]
```

### Step 6：自查（必须做，写入报告时引用）

```bash
python - <<'PY'
import json
r = json.load(open('/app/output/result.json', encoding='utf-8'))
e = json.load(open('/app/output/evidence.json', encoding='utf-8'))
print('status', r['meta']['status'])
print('missing contract fields', r['meta']['contract_fields_not_reported'])
print('grounding', r['meta']['evidence']['grounding']['grounding_rate'])
print('ungrounded notes', [c['claim_id'] for c in e['claims'] if not c['grounded']][:10])
PY
```

若某批回答不了（例如超预算），**不要伪造**：直接跑 `--finalize`，脚本会用逐字抽取的基线补齐未覆盖章节并标 partial，同时在 `limits` 写清原因。

## 六、核心工作流（实现细节）

**阶段 1 输入发现**：多根探测 + 特征识别（含 `case_id`/`output_contract` 的 JSON，容忍 BOM）；输出目录做真实写探测；材料排序固定，保证重复运行可比。

**阶段 2 文本与坐标提取**：PyMuPDF → pdfplumber → 外部 `pdf-text-tool`/`pdftotext` → 内置解析器逐级降级，命中即停并记录每次尝试的真实原因；坐标在引擎边界统一为 y 向上；每个引擎调用前检查软截止（720 s）。

**阶段 3 章节切分**：共享内核 structure.outline 的四条判据（已知标题名、编号标题、全大写短行、版面大字号单行），并给出标题块 id、页码、层级。

**阶段 4 分段与事实抽取**：一段 = 一个段落块（超长块按句边界切开，不新增事实）；每段继承 section_id/section_title/page/bbox；同时抽取数字+单位、引用、限定词、负向词、图表标签、缩写，并统计全篇术语候选。

**阶段 5 工作表**：每 6 段或 5000 字符一批；每批注入该段事实与 point_type 建议；卡片工作表按「字段关键词命中数 + 所在章节匹配 + 是否有数字/引用」打分排序，取每字段前 6 个候选段，每段最多 3 句候选。

**阶段 6 Agent 判断**：写 `card.json` 与 `batch-XX.json`（见第五节）。

**阶段 7 校验与契约组装**：读生成内容 → 覆盖/重复/未完成校验（缺失、duplicates、unknown_ids、incomplete 全部记录）→ 逐条构造 anchors 锚点并回源校验 → 数字/引用保真与限定词丢失检查 → 未覆盖章节用确定性基线补齐（部分生成时只补完全没答的章节，绝不覆盖已写笔记）→ 组装 `paper_card`/`section_notes`/`key_points`/`limitations`/`coverage`/`evidence`/`quality`/`index`/`notes_markdown` → 反射契约写三个结果文件。

## 七、异常处理

| 异常情况 | 检测方式 | 处理策略 | 状态标记 |
|---|---|---|---|
| 找不到 case.json | 发现阶段为空 | 仍解析材料，契约字段无法反射；产物进 additional_outputs | partial（contract_detected=false） |
| 无材料文档 | 材料列表为空 | 写出诚实空壳，limits 说明「未找到材料」 | failed |
| 扫描件无文字层 | 提取字符数 ≤ 20 | 不臆造文本，说明需要 OCR | failed（无段） |
| 加密/权限受限 PDF | 提取 warnings 含 /Encrypt | 能读多少写多少，warnings 进 uncertainty | partial |
| 双栏串栏 | reading_order.multi_column_suspected_pages 非空 | 按列优先输出，并在 evidence 说明 | success（带 uncertainty） |
| 章节识别不到 | sections 只有 sec-000 | 整篇作为一节处理，基线仍产出笔记 | partial（章节覆盖低） |
| Agent 未写任何生成内容 | work/generated 为空 | 逐字抽取基线（每段首/末句），标 BASELINE | partial |
| 只写了部分批次 | merge_generated.missing 非空 | 已写笔记保留，未答章节用基线补齐 | partial |
| 引文与所引块不符 | anchors.verify ≠ exact/normalised | 标 ungrounded，附 elsewhere_page（若引文在别页） | partial |
| 引文找不到 | match=missing | 同上，不静默升级 | partial |
| 笔记编造数字/引用 | 数字不在全文/引用不在全文 | 该条 ungrounded，计入 fabricated_*，limits 说明 | partial |
| 笔记类型非法 | 不在 point_type 词表 | 归为 NOTE 并标 unknown_point_type | 不改变状态 |
| 卡片字段缺失 | 生成 card.json 少键 | 用基线句子补或写哨兵，记 card_fields_not_reported | partial |
| 卡片声称 not reported | 值等于哨兵/未报告 | 认可为「明确未报告」，计入 coverage，不算缺字段 | 不改变状态 |
| 生成 JSON 损坏 | read_records 解析失败 | 逐行 JSONL 兜底；仍失败则该文件被忽略并记账 | partial |
| 任何未捕获异常 | 顶层 BaseException | 写 status=failed 的 result.json（含 traceback_tail） | failed |
| 超时（>720 s 软截止） | Stopwatch | 跳过剩余材料/引擎并记账，先落盘已完成部分 | partial |

## 八、限制与边界

1. **不解析表格单元格**：只给表格候选区域（页码、bbox、置信度），表格里的数字可能缺席笔记；此限制写入 `limitations`（scope=reading_pipeline）。
2. **公式只做结构识别**（编号、运算符密度），不解析数学语义。
3. **扫描件无 OCR**：没有文字层时不会凭空产出笔记。
4. **章节切分是启发式**：字号层次不明显或标题不规范时会少切/多切，章节覆盖与 `sections` 需人工核对一次。
5. **基线是逐字抽取，不是改写**：基线笔记的 grounded 只能证明位置成立，不证明摘要质量；`coverage.baseline_used=true` 时引用结论要谨慎。
6. **跨语言笔记的支持判定较弱**：中文笔记配英文原文时降级为 `cross_language_anchor_only`（只保证锚点与数字），词面支持检查不适用。
7. **段数 > 400 会截断**（写进 limits），超长综述类论文后半部分可能没有笔记。
8. **主文档只取一篇**：根据排序选主文档（paper.pdf 优先），其他材料记录在 run_report 但不进入笔记。
9. **不判断科学对错**：只检查「笔记是否忠于原文」，不评价论文本身的方法优劣。

## 九、失败处理与自查

| 现象 | 自查方法 | 处理 |
|---|---|---|
| `meta.status=partial` 且 limits 有 no_generated_notes | 看 `work/generated/` 是否有文件；看 prepare 是否成功 | 补写批次后重跑 --finalize |
| 某条笔记 `grounded=false` | 看 evidence.json 中该 claim 的 `match`、`elsewhere_page`、`flags` | 引文不在所引块：按 flags 修正 quote（必须是原文逐字） |
| `fabricated_numbers` | 对比 `number_checks.numbers_in_note` 与原文 | 数字必须来自论文；不确定就删掉数字 |
| `contract_fields_not_reported` 非空 | 看 `meta.contract_field_mapping` 判断是否名称不匹配 | 若字段语义确实没产出，保留占位并在报告里说明；不要写假值 |
| `section_coverage_rate` 偏低 | 看 `coverage.sections_without_notes` 与 `sections.json` | 补该节批次的笔记，或确认章节切分是否错误 |
| `grounding_rate < 1` 但引文看起来没问题 | 看 `quality.number_fidelity` / `hedge_fidelity` | 通常是数字或限定词丢失，而非位置错误 |
| 卡片字段为 not reported | 看 `work/card_worksheet.json` 是否有候选句 | 候选为空说明原文确实没写，保持 not reported |
| 结果里缺某个契约字段 | 看 `meta.declared_fields` 与 `meta.placeholders` | 检查 case.json 的 output_contract 形状是否被支持 |

## 十、最小调用示例（**示意输出，非正式案例结果**）

**输入**（`tests/test_summary_notes.py` 生成的合成样例，非真实文献）：

```
fixture/
├── case.json       # case_id=demo-full，声明 8 个契约字段（故意与内部命名不同）
├── task.md
└── paper.pdf       # 2 页合成论文：标题/Abstract/1-4 节/数字/引用/局限句
```

**调用**：

```bash
cd fixture
python scripts/run_summary_notes.py --prepare --out output --work work
# ... Agent 写 work/generated/card.json 与 work/generated/batch-01.json
python scripts/run_summary_notes.py --finalize --out output --work work
```

**示意输出**（`output/result.json` 摘要，字段名来自该 fixture 的契约）：

```json
{
  "case_id": "demo-full",
  "structured_card": {"research_question": {"text": "We investigate the effect of nickel loading ...", "page": 1, "block_id": "p1-b3", "match": "exact", "source": "agent"}},
  "chapter_notes": [{"section_id": "sec-001", "title": "Abstract", "pages_label": "1",
                     "note_count": 3, "notes": [{"seg_id": "seg-0003", "page": 1,
                     "block_id": "p1-b3", "point_type": "RQ", "note": "...",
                     "quote": "...", "grounded": true}]}],
  "knowledge_points": [{"point_id": "kp-0001", "point_type": "RQ", "seg_id": "seg-0003",
                        "page": 1, "quote": "...", "grounded": true}],
  "caveats": [{"limitation_id": "lim-0001", "scope": "paper", "page": 2,
               "text": "Only one support and one pressure were tested.", "grounded": true}],
  "coverage_report": {"sections_total": 7, "sections_with_notes": 7, "section_coverage_rate": 1.0,
                      "baseline_used": false},
  "evidence_anchors": {"grounding": {"notes_total": 14, "grounding_rate": 1.0, "anchor_quality": "span"}},
  "note_markdown": "# Paper notes: ...",
  "referee_commentary": "",
  "meta": {"status": "success", "contract_field_mapping": {"structured_card": "paper_card"},
           "contract_fields_not_reported": ["referee_commentary"]}
}
```

> 该样例里 `referee_commentary` 故意是一个本技能不产出的字段，所以它是空串并出现在 `contract_fields_not_reported`——这正是「缺值按声明类型占位且明说」的行为。

## 十一、架构参考与差异说明

章节编排参考平台公开技能目录中的 `scientific-paper-translator`（技能定位/适用条件/调用方式/工作流/异常四列表/最小示例）与 `bohr-cli`（「踩坑」章节）；共享的发现、提取、锚点、契约反射代码来自本仓库 `skills/_core`（reading-01 已实测）。本技能新增的是**章节对齐的卡片与分节笔记**、**逐字回源 + 数字/引用保真 + 限定词丢失三类断言**、**未生成时的抽取式基线降级**。

| 维度 | 常见做法 | 本技能做法 |
|---|---|---|
| 笔记结构 | 通篇摘要模板 | 按论文自身章节分组，段落在切分时就继承 section_id |
| 核验 | 模型自述已核验 | 代码回源断言：quote 必须出现在所引 block；数字/引用必须出现在全文；限定词丢失另记 |
| 缺内容 | 让模型补全 | 写 not reported / GAP，并在 coverage、limitations、limits 三处明说 |
| 输出契约 | 固定 schema | 反射 case.json.output_contract，缺值按声明类型占位 |
| 未生成 | 直接失败 | 确定性抽取式基线 + status=partial，绝不抛错 |
| 预算 | 让模型反复读原文 | 确定性阶段做完全部机械劳动，模型只写判断，单批 ≤ 5000 字符 |
| 可分性 | 只有最终结果 | 工作表、生成文件、证据、报告全部留痕，可独立复核 |

## 十二、环境踩坑（`--help` 不会告诉你的）

1. **%TEMP% 可能不可写**：受限沙箱下 `tempfile` 直接 PermissionError；本技能的自测临时目录建在技能目录内并在 finally 清理。
2. **不要用 pip 补依赖**：受限环境装包会因元数据写入被拒而失败；本技能只用标准库。
3. **Windows 控制台编码**：progress 日志全部 ASCII，中文只写进 UTF-8 文件——中文 print 在 cp936 控制台会抛 UnicodeEncodeError 并杀死整次运行。
4. **JSON 读入要容忍 BOM**：共享内核用 `utf-8-sig` 读；否则 Windows 工具写出的带 BOM case.json 会被当成「没有契约」，字段全空。
5. **生成文件放 `generated/`，以 `_` 开头的会被忽略**：把 schema 说明写成 `_README.json`，避免它被当成一条笔记。
6. **quote 不要写省略号**：`exact/normalised` 才计 located，中间的省略号会让校验降级为 block_only 甚至 missing。
7. **同一 seg 写两条会被当重复丢弃**（记入 duplicates），不会合并。
8. **批次文件缺失不等于失败**：只要写了部分批次，finalize 会保留已写内容并补齐其余章节，状态为 partial 而非报错。
9. **先落盘再干活**：占位 result.json 让「被 900 秒杀掉」不等于「零产出」；这一条在真实评测里比任何优化都值钱。

---

### 维护信息

- 版本：1.0.0（schema `reading10/1.0`）
- 词表与阈值：`resources/note_fields.json`（缺失时使用脚本内置同构默认值，并在 limits 记一条告警）
- 自测：`python tests/test_summary_notes.py`（5 组端到端用例：工作表与占位、完整生成与契约反射、缺生成降级、坏引文/编造数字捕获、无材料 failed；含运行确定性断言）
- 依赖：Python ≥ 3.8 标准库；可选引擎 PyMuPDF / pdfplumber / pdftotext 自动探测
- 复现：脚本零模型调用、零联网，同一输入重复运行的 result.json 字节一致（自测中有断言）
