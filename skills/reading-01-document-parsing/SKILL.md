---
name: reading-01-document-parsing
description: Parse a scientific PDF or text document into a page-anchored structured result — page blocks with bounding boxes, section outline, candidate tables and formulas, parsed references — and emit a result.json that mirrors the case's declared output_contract. Use when a task asks to read, parse, or extract structure from research literature and requires every finding to point back to a page.
version: 1.0.0
author: 66Union
license: MIT
tags: [pdf-parsing, document-structure, evidence-anchors, output-contract, scientific-reading]
---

# 文献文档解析（Reading-01 Document Parsing）

## 一、技能定位

本技能解决科研 PDF 解析的四个核心问题，与本题考察口径一一对应：

| 考察问题 | 本技能的回应 |
|---|---|
| **提得全** | 逐页提取，页眉页脚与正文分开存放，不把版面装饰混进正文 |
| **排得对** | 每条文本带真实坐标（bbox），阅读顺序按"自上而下、再左右"还原，并显式报告是否检测到多栏布局 |
| **拆得清** | 章节层级、表格候选、公式候选、参考文献分别成结构，各自带置信度与判据 |
| **核得回** | 每个结构发现绑定锚点（页码 + 块 id + bbox + 原文引文），且**锚点会被重新校验**后才输出 |

**差异化**：多数同类技能让模型自述"已核验"，本技能把校验做成**可执行断言** —— 引文在源文中找不到，就会被标 `ungrounded` 并写进 `momentum` 之外的 `meta.uncertainty`，而不是被当成事实输出。

## 二、适用条件

| 项目 | 说明 |
|---|---|
| 输入格式 | PDF（含扫描件）、JATS XML、DOCX、TEX、Markdown、纯文本 |
| 语言 | 不限（结构化解析不依赖语言理解） |
| 篇幅 | 单页 ~ 数百页；超长时按页处理，不做一次性全文拼接 |
| 依赖 | **仅 Python 标准库**；若环境存在 PyMuPDF / pdfplumber / `pdf-text-tool` / `pdftotext` 则自动改用（保真度更高） |
| 不适用 | 需要理解图表科学含义、需要 OCR 但环境无 OCR 工具、手写体/极低分辨率扫描件 |

## 三、调用方式

### 触发方式

当任务要求"解析/读取/抽取这篇论文的结构"、"把 PDF 变成可继续处理的结构"、"给出页码依据"时触发。

### 输入

运行时由评测环境提供（路径**不硬编码**，按特征发现）：

```
/input/case.json        必选，含 case_id 与 output_contract
/input/task.md          必选，题目说明
environment/**          可选，随题下发的材料（PDF 等），位置不限，自动扫描
```

`case.json.output_contract` 是本技能的输出契约来源，**字段名由题目决定**，技能不预设。

### 输出

写入输出目录（默认 `/app/output`）：

| 文件 | 内容 |
|---|---|
| `result.json` | **契约字段 + case_id + meta**。声明字段按声明顺序输出；无法产出的写 `null` 并列入 `meta.contract_fields_not_reported` |
| `evidence.json` | 每条结论的锚点、校验结果、材料清单 |
| `run_report.json` | 状态、耗时、预算用量、限制、不确定性、实际读取的输入路径 |

`result.json` 形状（字段名以实际 `output_contract` 为准）：

```json
{
  "case_id": "<来自 case.json>",
  "<contract field A>": "...",
  "<contract field B>": null,
  "meta": {
    "status": "success | partial | failed",
    "contract_detected": true,
    "contract_field_count": 6,
    "contract_fields_not_reported": ["<field B>"],
    "contract_field_mapping": {"parsed_pages": "pages"},
    "evidence": { "grounding": { "claims_total": 10, "grounded": 10, "grounding_rate": 1.0 } },
    "limits": [],
    "uncertainty": [],
    "change_log": ["deterministic extraction; no model call made"]
  }
}
```

## 四、核心工作流

### 阶段 1：输入发现
1. 依次探测 `/input`、`/app/input`、`/app/work`、`/app/environment`、`/app`，再退到当前目录。
2. 按特征识别 case（含 `case_id` / `output_contract` 的 JSON）与 `task.md`，不依赖固定文件名。
3. 收集材料文档（pdf/xml/docx/tex/bib），跳过 `output`、`__pycache__`、`.git` 等。
4. 找不到材料时**直接输出 failed 结果**，不编造内容。

### 阶段 2：文本与坐标提取
按保真度降序尝试，命中即停，并把实际使用的引擎写进结果：

1. PyMuPDF（span 级 bbox + 字号）
2. pdfplumber（word 级 bbox）
3. 外部工具 `pdf-text-tool` / `pdftotext -layout`（仅文本，锚点降级为"页+行"）
4. 内置解析器（标准库：对象扫描 + FlateDecode + 文本矩阵 + ToUnicode）

每级结果都带 `anchor_quality = span | page | text`，降级原因写入 `warnings`。

### 阶段 3：版面切块
以"行距 > 字号的 1.6 倍 **或** 字号跳变 ≥ 1pt"作为切块依据（字号阈值必须是绝对值，12pt 标题压 10pt 正文只差 2pt，纯比例判据会漏）。单行且显著大于页面中位字号者标记 `likely_heading`。

### 阶段 4：结构识别
- **章节**：已知标题名 / 编号标题 / 全大写短行 / `likely_heading` 四条判据，保留层级与页码
- **表格**：依据对齐列间隙与数值 token 数量，输出**候选**及置信度，明确标注"仅版面推断，未验证单元格边界"
- **公式**：依据数学符号数、运算符数、尾部编号，只做结构识别，不解读含义
- **参考文献**：定位 References 标题后逐条切分（同一块内的多条按 `[n]` / `n.` 边界拆分）

### 阶段 5：证据绑定与校验
1. 为每条结构发现生成锚点（page / block_id / bbox / quote）。
2. 用 `anchors.verify` 回到源文校验，结果分 `exact` / `normalised` / `block_only` / `page_only` / `missing`。
3. 只有 `exact` 与 `normalised` 计入已落地；其余打 `ungrounded` 标记并计入 `meta.uncertainty`。

### 阶段 6：契约组装与落盘
1. 读 `output_contract`，用**词干匹配**（去复数/集合后缀，精确 → 前缀 → 包含）把内部产物映射到声明字段。
2. 未声明但已产出的内容放进 `meta.additional_outputs`，不污染契约区。
3. 写 `result.json` / `evidence.json` / `run_report.json`，耗时超软上限（720s）则降级为 `partial` 并写明。

## 五、异常处理

| 异常情况 | 检测方式 | 处理策略 | 状态标记 |
|---|---|---|---|
| 找不到 case.json | 发现阶段为空 | 仍尝试解析材料，`contract_detected=false`，所有产物放 `additional_outputs` | partial |
| 无材料文档 | 材料列表为空 | 输出 failed 结果并说明缺失 | failed |
| 扫描件无文字层 | 提取字符数 ≤ 20 | 不臆造文本，标注"需要 OCR"，环境有 `ocr-tool` 时才转 OCR 路径 | partial |
| 加密 PDF / 解析异常 | 引擎抛错 | 逐引擎降级，记录失败原因到 `attempts` | partial |
| 多栏串栏 | 检测到明显列间隙 | 输出 `reading_order` 并降低置信度，明确提示可能串栏 | success（带 uncertainty） |
| 表格为图片 | 无文本但有版面块 | 标注"表格为图片，未解析"，记录位置 | partial |
| 参考文献格式不一 | 条目切分失败 | 保留原文块引用，不强行编号 | partial |
| 契约字段无法产出 | 词干匹配未命中 | 该字段写 `null` 并列入 `contract_fields_not_reported` | partial |
| 超时（>720s 软上限） | 计时 | 提前落盘已完成部分，剩余阶段标记未执行 | partial |

## 六、使用与方法说明

### 完整调用步骤

```bash
# 1. 环境自检（可选，用于确认引擎保真度）
python lib/extract.py --probe

# 2. 运行（在含 case.json 的目录，或让技能自动发现输入）
python scripts/run_reading.py --out /app/output

# 3. 检查产物
cat /app/output/run_report.json     # 状态/限制/不确定性
cat /app/output/result.json | head  # 契约字段
```

### 关键方法

- **契约反射而非硬编码**：隐藏案例的字段名不可知，任何写死的 schema 都是错的。技能从 `case.json` 读契约，按词干匹配填值，缺的如实标 `null`。
- **锚点可校验**：把"我引用了第 4 页"变成"我引用了第 4 页 p4-b3，且这 38 个字符确实在该块内"。校验失败的主张不会被升级为事实。
- **引擎探测与显式降级**：评测镜像的工具名不可假设，所以每级都探测；降级时 `anchor_quality` 和 `warnings` 必须如实反映。
- **确定性**：全程无模型调用、无网络，同一输入重复运行结果一致（满足"3 个案例 × 3 次重复"的可比要求）。

### 限制与边界

1. 内置解析器只处理 `FlateDecode`，其他压缩滤镜与加密文档会失败（会如实报 `failed`/`partial`）。
2. 表格识别依据文本版面，不还原合并单元格与跨页续表，输出的是**候选**而非确定表格。
3. 公式只做结构识别（编号、符号密度），不解析数学语义。
4. 扫描件依赖环境 OCR 工具；无 OCR 时不会生成文本层。
5. 内建解析器对字体子集化复杂、使用 Type0/CID 字体且无 ToUnicode 的 PDF，可能产生乱码 —— 此类情况会由 `warnings` 暴露。

### 失败处理与自查

| 现象 | 自查方法 | 处理 |
|---|---|---|
| `text_layer=false` | 看 `run_report.json` 的 `documents[].total_chars` | 确认是否扫描件；有 OCR 工具则改走 OCR |
| 章节数远少于预期 | 看 `parsed_pages[].blocks` 的 `size` 与 `likely_heading` | 说明该文档字号层次不明显，需人工核对 |
| `grounding_rate` 偏低 | 看 `evidence.json` 的 `per_claim[].checks[].match` | `page_only` 说明引文跨块，`missing` 说明文本提取有损 |
| 契约字段全为 null | 看 `meta.contract_detected` | 为 false 说明没找到 case.json，检查输入路径 |
| 表格候选过多 | 看 `table_index[].confidence` | 阈值偏低时按 confidence ≥ 0.5 过滤 |

## 七、最小调用示例

**输入**（`tests/make_fixture.py` 生成的合成样例，非真实文献）：

```
fixture/
├── case.json      # case_id=demo-case-001，声明 6 个 contract 字段
├── task.md
└── paper.pdf      # 3 页合成论文：标题/Abstract/1-4 节/表格/公式/4 条参考文献
```

**调用**：

```bash
cd fixture && python ../../skills/_core/scripts/run_reading.py --out out
```

**示意输出**（`out/result.json` 摘要，**这是示例，不是正式案例结果**）：

```json
{
  "case_id": "demo-case-001",
  "parsed_pages": [{"page": 1, "block_count": 4, "blocks": [{"block_id": "p1-b1", "bbox": [72,756,453,764], "text": "Structure and Reactivity of Supported Ni Catalysts"}]}],
  "section_outline": [{"title": "Abstract", "page": 1, "level": 1}, {"title": "1 Introduction", "page": 1, "level": 2}],
  "table_index": [{"table_id": "tbl-2-1", "page": 2, "confidence": 0.65}],
  "formula_index": [{"formula_id": "eq-2-1", "page": 2, "numbered": true, "confidence": 0.65}],
  "reference_list": {"entry_count": 4, "entries": [{"ref_id": "1", "page": 3}]},
  "meta": {"status": "success", "contract_fields_not_reported": [],
           "evidence": {"grounding": {"claims_total": 10, "grounded": 10, "grounding_rate": 1.0}}}
}
```

## 八、架构参考与差异说明

本技能的**文档组织方式**参考了平台公开技能目录中的 `scientific-paper-translator`（章节编排、异常处理表、状态机、核验报告、限制与自查四段式）与 `bohr-cli`（"踩坑"章节，记录工具 `--help` 不会告诉你的事）。这两者是公开可读的架构范例，此处只借鉴其**结构组织**，未复制其内容或代码。

与范例的关键差异：

| 维度 | 范例做法 | 本技能做法 |
|---|---|---|
| 核验 | 由模型自述 `pass/warning/fail` | **代码断言**：引文回源校验，失败标 `ungrounded` |
| 输出契约 | 固定 JSON schema | **从 case.json 反射**，字段名不预设 |
| 工具依赖 | 声明依赖，缺失即失败 | **运行时探测 + 逐级降级**，降级写入 `warnings` |
| 数值可信度 | 不标注 | 每个结构发现带 `confidence` 与 `why` 判据 |

## 九、环境踩坑（`--help` 不会告诉你的）

1. **`%TEMP%` 可能不可写**：本机沙箱下 `tempfile.TemporaryDirectory()` 直接 `PermissionError`，中间产物一律写到工作目录内。
2. **不要用 `pip install` 补依赖**：受限环境里装包会因元数据写入被拒而失败；本技能刻意只用标准库。
3. **`pdf-text-tool` 的参数未知**：只按 `{pdf}` 单参模板探测，失败立即降级，不要假设它的输出格式。
4. **PDF 内括号必须转义**：真实 PDF 中文本串内的 `(` `)` 写作 `\(` `\)`；若遇到未转义的非法 PDF，文本会被截断 —— 这是提取有损的常见来源，已在 `warnings` 中体现。
