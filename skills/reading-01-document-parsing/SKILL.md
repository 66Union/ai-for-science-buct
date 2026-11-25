---
name: reading-01-document-parsing
description: Parse a scientific PDF or text document into a page-anchored structured result — page blocks with bounding boxes, section outline, candidate tables and formulas, parsed references, and front matter (title/authors/abstract/DOI) — emitting a result.json that mirrors the case's declared output_contract. Use when a task asks to read, parse, or extract structure from research literature and requires every finding to point back to a page.
version: 1.1.0
author: 66Union
license: MIT
tags: [pdf-parsing, document-structure, evidence-anchors, output-contract, two-column-layout]
---

# 文献文档解析（Reading-01 Document Parsing）

## 一、技能定位

对应本题考察的四个问题：

| 考察问题 | 本技能的做法 | 可验证的证据 |
|---|---|---|
| **提得全** | 逐页提取正文、按钮式保留每个文本块的坐标；页眉页脚**不单独剔除**（见§6 限制） | `parsed_pages[].blocks[]` 每条带 `bbox` |
| **排得对** | 检测双栏版面（栏缝近似空白），按**列优先**输出：左栏读完再右栏；上标/下标按字号加权归并到基线行 | `reading_order.multi_column_suspected_pages`；`_column_split` 判定 |
| **拆得清** | 章节层级、表格候选、公式候选、参考文献、前置信息分别成结构，各自带 `confidence` 与 `why` | `section_outline` / `table_index` / `formula_index` / `reference_list` / `frontmatter` |
| **核得回** | 每个结构发现绑定锚点（页码 + 块 id + bbox + 引文），且**回源校验后才输出**；引文必须真的提及主张 | `meta.evidence.grounding.per_claim[].checks[]` |

**与常见做法的差异**：多数同类技能让模型自述"已核验"，本技能把校验写成**可执行断言**，并且区分两类主张：内容主张要求引文里出现主张的关键词（`supported_by_quote`），位置主张（如"第 2 页这块区域是表格候选"）只要求位置成立。任一不过即标 `evidence_does_not_mention_claim` / `ungrounded` 并计入 `meta.uncertainty`。

## 二、适用条件

| 项目 | 说明 |
|---|---|
| 输入格式 | PDF（含扫描件）、JATS XML、DOCX、TEX/BIB、Markdown、纯文本、CSV/TSV |
| 语言 | 不限（结构化解析不依赖语言理解） |
| 篇幅 | 单页 ~ 数百页；按页处理 |
| 依赖 | **仅 Python 标准库**；若存在 PyMuPDF / pdfplumber / `pdf-text-tool` / `pdftotext` 则自动改用（保真度更高） |
| 不适用 | 需要理解图表科学含义；无 OCR 工具时的纯扫描件；手写体；极低分辨率影像 |

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

`case.json.output_contract` 是唯一输出契约来源，**字段名由题目决定**，技能不预设。

### 输出

写入输出目录（默认 `/app/output`）：

| 文件 | 内容 |
|---|---|
| `result.json` | **契约字段 + case_id + meta**。声明字段按声明顺序输出；无法产出的按声明类型给占位（array→`[]`、object→`{}`、string→`""`），并列入 `meta.contract_fields_not_reported` 与 `meta.placeholders` |
| `evidence.json` | 每条结论的锚点、校验结果、材料清单、**每个引擎的尝试记录** |
| `run_report.json` | 状态、耗时、预算、限制、不确定性、实际读取的输入路径、`engines_probed` |

**启动即写占位 `result.json`**（`status: partial`），跑完覆盖。这样即使容器在 900 秒预算处被杀，产物仍在，而不是零字节。

## 四、核心工作流

### 阶段 1：输入发现
1. 依次探测 `/input`、`/app/input`、`/app/work`、`/app/environment`、`/app`，最后退到当前目录；**每个 root 都扫**。
2. 按特征识别 case（含 `case_id` / `output_contract` 的 JSON，容忍 BOM）与 `task.md`。
3. 材料 = pdf/xml/docx/tex/bib/md/txt/csv/tsv，排除 `case.json` 与 `task.md`。
4. 排序确定性：`paper.pdf` 优先 → `reference/` 下优先 → PDF 优先 → 体积降序 → 路径字典序。三次重复必须给出同一主文档。
5. 输出目录做**写探测**（写一个文件再删），只读目录会被跳过。

### 阶段 2：文本与坐标提取
按保真度降序尝试，命中即停，并记录 `attempts`（含 `not installed` 这类真实原因）：

1. PyMuPDF（span 级 bbox + 字号）
2. pdfplumber（word 级 bbox）
3. 外部工具 `pdf-text-tool` / `pdftotext -layout`（仅文本；其 JSON 会被归一化成页级对象，形状不认识就丢弃并记账）
4. 内置解析器（标准库）

**坐标系统一**：PyMuPDF/pdfplumber 报的是 y 向下，进入流水线前翻成 PDF 惯例的 y 向上，下游只面对一种坐标系。每个引擎调用前检查 deadline，超预算即跳过并记账。

内置解析器对以下情况会显式发 `warnings`（不静默）：`/Encrypt` 加密、非 FlateDecode 滤镜、无可用 ToUnicode 且含非 ASCII 字符。

### 阶段 3：版面切块
以「行距 > 字号 × 1.6」**或**「字号跳变 ≥ 1pt」切块。字号阈值必须是绝对值：12pt 标题压 10pt 正文只差 2pt，纯比例判据会漏。行聚类容差取**该行主导字号的 45%**，让上标（约 3pt 偏移、字号更小）回到基线行而不是自成一行。单行且显著大于页面中位字号者标 `likely_heading`。

### 阶段 4：结构识别
- **章节**：已知标题名 / 编号标题 / 全大写短行 / `likely_heading` 四条判据
- **表格**：用**块内 span 的 x 起点**算列间隙（`col_gaps`），要求「≥2 个列间隙且 ≥2 行」或「≥4 个数值且 ≥2 行」。仅版面推断，不验证单元格边界
- **公式**：运算符 + 数学符号 + 尾部编号；只做结构识别，不解读含义
- **参考文献**：定位 References 标题后，按 `[n]` / `n.` 边界切分（同一块内的多条也会拆开）
- **前置信息**：标题 = 首页最大字号的块；作者 = 标题后符合条件的块；摘要 = `Abstract` 标题之后到下一个标题之间；关键词与 DOI 用正则

### 阶段 5：证据绑定与校验
1. 为每条结构发现生成锚点（page / block_id / bbox / quote）。
2. `anchors.verify` 回源校验，结果分 `exact` / `normalised` / `block_only` / `page_only` / `missing`。
3. 内容主张额外要求引文含主张关键词；位置主张只要求位置成立。
4. 输出 `grounding_rate`，并按 `anchor_quality` 给置信上限（`span` 1.0 / `page` 0.7 / `text` 0.4），另给 `effective_confidence`。

### 阶段 6：契约组装与落盘
1. 读 `output_contract`，用**词干匹配**（去复数/集合后缀，精确 → 前缀 → 包含）把内部产物映射到声明字段。
2. 未命中的字段按声明类型给占位；未声明但已产出的内容放进 `meta.additional_outputs`。
3. 写 `result.json` / `evidence.json` / `run_report.json`；超软上限（720s）则降级 `partial` 并写明。

## 五、异常处理

| 异常情况 | 检测方式 | 处理策略 | 状态标记 |
|---|---|---|---|
| 找不到 case.json | 发现阶段为空 | 仍解析材料，`contract_detected=false`，产物全进 `additional_outputs` | partial |
| 无材料文档 | 材料列表为空 | 输出 failed 结果并说明 | failed |
| 扫描件无文字层 | 提取字符数 ≤ 20 | 不臆造文本，标注需要 OCR | partial |
| 加密 PDF | 检测到 `/Encrypt` | 记 `warnings`，能读到多少写多少 | partial |
| 非 FlateDecode 滤镜 | 检查 `/Filter` | 记 `warnings` 并说明该流未解码 | partial |
| 无 ToUnicode 的 CID 文字 | 非 ASCII 且无 CMap | 记 `warnings`，提示可能乱码 | partial |
| 多栏串栏风险 | 检测到栏缝 | 按列优先输出，并在 `reading_order` 说明 | success（带 uncertainty） |
| 引用子进程挂住 | deadline 检查 | 跳过该引擎并记账，继续降级 | partial |
| 任何未捕获异常 | 顶层 `BaseException` | 写 failed 结果并附 `traceback_tail` | failed |
| 超时（>720s 软上限） | 计时 | 先落盘已完成部分 | partial |

## 六、使用与方法说明

### 完整调用步骤

```bash
# 1. 环境自检（确认本机有哪些引擎）
python lib/extract.py --probe

# 2. 运行（在含 case.json 的目录，或让技能自动发现输入）
python scripts/run_reading.py --out /app/output

# 3. 自测（可选，验证解析与契约逻辑）
python tests/test_core.py
python tests/test_extraction_quality.py

# 4. 检查产物
cat /app/output/run_report.json      # 状态 / 限制 / 不确定性 / engines_probed
cat /app/output/evidence.json        # 每条主张的锚点与校验结果
```

### 关键方法

- **契约反射而非硬编码**：隐藏案例的字段名不可知，任何写死的 schema 都是错的。缺值按声明类型占位，既保持形状又不伪造内容。
- **证据可校验、且区分主张类型**：位置主张靠位置成立，内容主张靠引文含关键词。这样 `grounding_rate` 才有信息量。
- **坐标系归一**：不同引擎的 y 轴方向不同，不归一会让整页顺序倒置而结果形状依然合法。
- **确定性**：全程无模型调用、无网络；文件遍历排序、主文档排序固定，满足"3 案例 × 3 次重复"的可比要求。
- **先落盘再干活**：占位 `result.json` 让"被杀"不等于"零产出"。

### 限制与边界

1. **不做页眉页脚剔除**：页眉、页脚、页码会留在正文块里。若契约要求分离，这类字段会返回空并记入 `not_reported`。
2. 内置解析器只处理 `FlateDecode`；其他滤镜与加密文档会失败或部分失败（会如实报 `partial`/`failed`）。
3. 表格基于 span 几何的**候选**，不还原合并单元格与跨页续表。
4. 公式只做结构识别（编号、运算符密度），不解析数学语义。
5. 无 OCR 工具时不产生扫描件文本层。
6. `full_text` 是一次性拼接的字符串（便于契约字段直接消费），超长文档会有内存与耗时压力。
7. 前置信息（标题/作者）是版面启发式，多作者跨栏或特殊模板下可能漏人或并入单位。

### 失败处理与自查

| 现象 | 自查方法 | 处理 |
|---|---|---|
| `text_layer=false` | 看 `run_report.json` 的 `documents[].total_chars` 与 `attempts` | 确认是否扫描件；`attempts` 里能看到每个引擎失败的真实原因 |
| 章节数远少于预期 | 看 `parsed_pages[].blocks[].size` 与 `likely_heading` | 该文档字号层次不明显，需人工核对 |
| 双栏被串 | 看 `reading_order.multi_column_suspected_pages` | 若为空说明栏缝判定未触发，检查版面是否有跨栏元素 |
| `grounding_rate` 偏低 | 看 `meta.evidence.grounding.per_claim[]` 的 `located` 与 `supported_by_quote` | `located=false` 是提取丢内容；`supported_by_quote=false` 是引文没覆盖主张 |
| 契约字段为空 | 看 `meta.contract_detected` 与 `contract_field_mapping` | `false` 说明没找到 case.json，检查输入路径 |
| 表格候选偏多/偏少 | 看 `table_index[].confidence` 与 `why` | 按 `confidence ≥ 0.5` 过滤；`why` 里写明列间隙与数值个数 |

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

**示意输出**（`out/result.json` 摘要，**这是示例，不是正式案例结果**，数字取自实际运行）：

```json
{
  "case_id": "demo-case-001",
  "parsed_pages": [
    {"page": 1, "block_count": 4,
     "blocks": [{"block_id": "p1-b1", "bbox": [72.0, 747.2, 472.0, 764.0],
                 "text": "Structure and Reactivity of Supported Ni Catalysts"}]}
  ],
  "section_outline": [
    {"title": "Structure and Reactivity of Supported Ni Catalysts", "page": 1, "level": 2},
    {"title": "Abstract", "page": 1, "level": 1},
    {"title": "1 Introduction", "page": 1, "level": 2}
  ],
  "table_index": [{"table_id": "tbl-1-1", "page": 1, "confidence": 0.45,
                   "why": "0 column gaps, 3 numeric tokens, 2 lines"}],
  "formula_index": [{"formula_id": "eq-2-1", "page": 2, "numbered": true, "confidence": 0.65}],
  "reference_list": {"entry_count": 4, "entries": [{"ref_id": "1", "page": 3}]},
  "full_text": "...",
  "meta": {
    "status": "success",
    "contract_fields_not_reported": [],
    "placeholders": {},
    "evidence": {"grounding": {"claims_total": 9, "grounded": 9, "grounding_rate": 1.0,
                               "anchor_quality": "span", "effective_confidence": 1.0}}
  }
}
```

> 该样例的表格在合成时把一行写成单个字符串，所以 `col_gaps = 0`；真实 PDF 的单元格是独立文本段，列间隙会被正常检出。

## 八、架构参考与差异说明

文档组织方式参考了平台公开技能目录中的 `scientific-paper-translator`（章节编排、异常处理表、状态机、核验报告、限制与自查四段式）与 `bohr-cli`（"踩坑"章节）。此处只借鉴**结构组织**，未复制其内容或代码。

| 维度 | 范例做法 | 本技能做法 |
|---|---|---|
| 核验 | 模型自述 `pass/warning/fail` | 代码回源断言，且区分内容主张与位置主张 |
| 输出契约 | 固定 JSON schema | 从 `case.json` 反射；缺值按声明类型占位 |
| 工具依赖 | 声明依赖，缺失即失败 | 运行时探测 + 逐级降级，降级原因写入 `attempts`/`warnings` |
| 坐标系 | 未提及 | 引擎边界统一翻转为 y 向上 |
| 被杀风险 | 未提及 | 启动即写占位 `result.json` |

## 九、环境踩坑（`--help` 不会告诉你的）

1. **`%TEMP%` 可能不可写**：受限沙箱下 `tempfile.TemporaryDirectory()` 直接 `PermissionError`，中间产物一律写到工作目录内。
2. **不要用 `pip install` 补依赖**：受限环境装包会因元数据写入被拒而失败；本技能刻意只用标准库。
3. **`pdf-text-tool` 的参数未知**：只按 `{pdf}` 单参模板探测，失败立即降级；其 JSON 若形状不认识会被丢弃并记账，而不是让整次运行崩掉。
4. **PDF 内括号必须转义**：真实 PDF 把文本串里的 `(` `)` 写作 `\(` `\)`；未转义的非法 PDF 会导致截断，这是提取有损的常见来源。
5. **PDF 的 `stream` 关键字**：必须后跟换行才算流开始，否则字典里 `/Title (a data stream guide)` 这类值会让解析器把后面全部吞掉；优先用 `/Length` 定位结尾。
6. **字体是按资源名引用的**：内容流里 `Tf` 用的是 `/F1`，不是 `/BaseFont`；按 `/BaseFont` 建索引会让 ToUnicode 永远命中不了，CID 文字全部变成乱码。
7. **页序来自 `/Pages /Kids`**：按对象出现顺序取页，在增量更新的 PDF 上会给出错误页码与重复页。
