---
name: reading-03-translation
description: Glossary-locked, paragraph-aligned scientific literature translation (EN->ZH by default). A deterministic prepare phase cuts the paper into page-anchored paragraphs, extracts machine-checkable facts (numbers, units, citations, formulas, hedging/negation/modals, candidate terms) and writes small per-batch worksheets; the Agent translates one small batch per model request against a locked terminology table; a deterministic finalize phase verifies coverage, terminology consistency and fact preservation, re-verifies every evidence anchor against the source document, and reflects case.json.output_contract into result.json. Use when a task asks to translate research literature, produce bilingual paragraph alignment, enforce terminology consistency, or prove that a translation kept numbers / citations / formulas / hedges intact.
version: 1.0.0
author: 66Union
license: MIT
tags: [translation, terminology-glossary, bilingual-alignment, evidence-anchors, output-contract, two-phase-pipeline]
---

# 文献翻译（Reading-03 Translation）

## 一、技能定位

本题考察四个问题，本技能的对应做法与**可执行证据**如下（每条都能在产物里复查，而不是自述）：

| 考察问题 | 本技能的做法 | 可验证的证据 |
|---|---|---|
| **术语稳** | 先由 Agent 基于候选术语表写一份**锁定术语表**（term → 固定译名，可选 keep_original），再把它按段注入每张批次工作表；finalize 逐段统计每个术语在「出现该术语的段」里是否都使用了锁定译名 | `term_loyalty_report.per_term[].rate / status`、`term_rate`、`usage_rate`、`violations` |
| **段落全** | 以**块 = 段落**切分（跨页、跨栏由共享解析器保证阅读顺序），每段有稳定 `seg_id`、页码、`block_id`、`bbox`；断点续跑时复用段清单（fingerprint 校验），不会错位 | `zh_en_paragraph_pairs[]`（src 逐字等于原文段）、`paragraph_completeness.coverage`、`batches_pending` |
| **关系准** | 每段译文做**集合式保真检查**：数字/单位、文献引用、公式 token、图表编号必须逐字保留；限定词（hedge）、否定、情态动词做**跨语言标记检查**（原文有 may/not/must，译文必须出现 可能/不/必须，或刻意保留英文 token） | 每对 pair 内嵌 `checks`（`numbers_missing` 等），汇总在 `grade_metrics.preservation` |
| **可核验** | 每段译文带锚点 `{page, block_id, bbox, quote}`，finalize 阶段**回源文档重新校验**，返回 `exact/normalised/block_only/page_only/missing`；无法翻译或译不准的段落进入 `omission_list` 与 `review_checklist.md`，不含混冒充已完成 | `evidence_anchors.grounding.grounding_rate`、`per_pair_anchors[].check` |

**与常见做法（含基础 Agent 直接翻译）的差异**：

1. **术语表不是提示词，是数据**：术语表由 Agent 写一次（`work/generated/_glossary.json`），随后被注入每张工作表；finalize 用代码逐段核对，出现第二种译名就记为 `inconsistent` 并给出违规段号。
2. **段落号与页码是一等公民**：`seg_id` 由源文档确定性生成，译文按 `seg_id` 回收；段落不会重复、不会被合并，未作答的段有独立状态而不是被静默跳过。
3. **翻译与核验分离**：翻译（判断）只花 Agent 的模型请求；切段、抽事实、统计、契约反射全部是**确定性 Python**，900 秒预算与「3 案例 × 3 次重复」的可比性不依赖运气。
4. **降级也产出**：没生成、生成残缺、术语表缺失、原文档读不到，都会产出**形状合法**的 result.json，并把缺口写进 `meta.limits` 与 `omission_list`；绝不因异常而不落盘。

## 二、适用条件

| 项目 | 说明 |
|---|---|
| 输入格式 | PDF（有文字层）、DOCX、TXT、Markdown、JATS XML（走共享提取链）；同目录若带术语表（文件名含 gloss/term/术语/词表，扩展名 json/md/txt/csv/tsv/yaml）会被识别为**题目自带术语表**并优先锁定 |
| 语言方向 | 默认 **英→中**；若检测到源文以中文为主，finalize 自动切到 **中→英** 口径（标记检查改用英文标记；仍以段为单位） |
| 篇幅 | 单段 ≤ 1200 字符（更长的块按句子边界拆段）；工作表默认 **每批 6 段 / ≤5200 字符**，适配 16384 tokens 的响应预算；长文可断点续跑 |
| 依赖 | **仅 Python 3.8+ 标准库**；无网络、无模型 API、无第三方包。PDF 提取优先用共享内核已有的 PyMuPDF/pdfplumber（若镜像提供），否则用内置解析器 |
| 模型请求 | 本技能脚本**自身 0 次模型请求**；Agent 侧协议是「1 次术语表 + 每批 1 次」，一个 18 段的样例 = 1 + 3 = 4 次 |
| 不适用 | 扫描件/纯图片 PDF（无 OCR 能力，会如实报 failed 并给原因）；需要重排表格结构、重绘公式的任务；需要评判译文文采/学术风格（脚本只给保真与一致性证据，文采由评审判断） |

## 三、调用方式

### 3.1 触发方式

当任务要求「把这篇文章翻译成中文」「给出段落级双语对照」「术语必须全篇统一」「核对译文有没有改数字/漏引用」时触发。技能名 `reading-03-translation`。

### 3.2 输入（运行时由评测环境给出，路径不硬编码）

```
/input/case.json        必选：含 case_id 与 output_contract（字段名由题目决定）
/input/task.md          必选：题目说明
environment/**          可选：随题材料（PDF 等）与题目自带术语表，位置不限，自动扫描
```

输入发现顺序：`/input` → `/app/input` → `/app/work` → `/app/environment` → `/app` → 当前目录；材料优先级：`paper.pdf` → `reference/` 下 → PDF → 体积降序 → 路径字典序（保证三次运行同一主文档）。

### 3.3 输出文件

最终产物写到输出目录（默认 `/app/output`，本地退化为 `<cwd>/output`）：

| 文件 | 内容 |
|---|---|
| `result.json` | **契约字段 + case_id + meta**。字段由 `case.json.output_contract` 反射生成：声明顺序输出，声明了但没产出的按声明类型占位（array→[]、object→{}、string→""）并记入 `meta.contract_fields_not_reported` |
| `evidence.json` | 每段的锚点与回源校验结果、段清单来源（prepare 清单/重算）、生成文件清单、术语表状态、材料与解析引擎记录 |
| `run_report.json` | 状态、耗时、覆盖率、术语一致率、缺失批次、限制与不确定性、`engines_probed`、输出文件清单 |
| `translation_bilingual.md` | **双语对照全文**：术语表 + 按页分组的逐段「原文 / 译文 / 机器校验 / 译法依据 / 证据锚点」 |
| `glossary.json` | 可复用的术语表产物（term、translation、keep_original、来源、逐术语一致率） |
| `review_checklist.md` | 待人工确认清单（未翻译、数字/引用/公式缺失、术语不一致、锚点未落地），按类型分组 |
| `work_report.json` | prepare 阶段摘要（段数、批次数、候选术语数、锁定术语数），便于断点续跑时确认状态 |

中间工作目录（默认 `/app/work`，本地为 `<out>/work`）：

```
work/
├── segments.json            段清单（seg_id/页码/block_id/bbox/文本/fingerprint），断点续跑的依据
├── batches/batch-01.json    每批工作表：待译段落 + 每段必须保留的事实 + 本批锁定的术语 + 相邻上下文
├── glossary_worksheet.json  术语候选表（出现次数、页码、例句、种子建议译名）+ 题目自带术语表
├── term_candidates.json     术语候选原始清单
├── provided_glossary.json   题目自带术语表解析结果（若有）
├── facts.json               每段机器可判事实（数字/引用/公式/限定词/否定/情态/图表编号/句数）
├── protocol.json            机器可读的下一步协议（文件的 JSON 形状 + 命令）
├── index.json / state.json  阶段状态与批次索引（state.json 记录断点续跑状态，不含时间戳）
└── generated/               Agent 写这里（本技能只读）
    ├── _glossary.json       锁定术语表
    └── batch-01.json        第一批译文
```

### 3.4 字段字典（内部值 → 契约字段）

内部产物名固定，契约字段名由题目决定；映射由 `lib/hybrid.map_fields()` 完成（精确 → 别名 → 词干包含，最长优先），映射结果写在 `result.json.meta.contract_field_mapping`。下表「典型声明字段名」只是常见写法，**隐藏案例可任意命名**。

| 内部值 | 类型 | 结构要点 | 典型声明字段名 |
|---|---|---|---|
| `bilingual` | array | 段落级对照。每项：`pair_id, seg_id, order, page, block_id, bbox, section, src, tgt, status, reason, anchor{page,block_id,bbox,quote}, anchor_check{grounded,located,match}, basis{terms[{term,translation,keep_original,used}],notes}, checks{...}` | `bilingual_pairs`、`zh_en_paragraph_pairs`、`paragraph_pairs`、`aligned_pairs`、`双语对照` |
| `glossary` | array | 锁定术语表。每项：`term, translation, keep_original, note, source`（`agent` / `case_provided` / `auto_seed`） | `term_glossary`、`terminology_bank`、`term_list`、`术语表` |
| `coverage` | object | `total_segments, received, coverage, translated, source_chinese, untranslated, missing_seg_ids, missing_count, duplicate_seg_ids, unknown_seg_ids, incomplete_seg_ids, batches_total, batches_answered, batches_pending, pages, source_chars, target_chars, length_ratio` | `translation_coverage`、`paragraph_completeness`、`completeness` |
| `untranslated` | array | 未译/降级段。每项：`seg_id, page, order, status, reason`（status ∈ `untranslated` / `copied_source`） | `untranslated_segments`、`omission_list`、`omissions` |
| `term_consistency` | object | `available, terms_checked, consistent_terms, inconsistent_terms, term_rate, instances_total, instances_used, usage_rate, per_term[], violations[], ambiguous_translations[], glossary_status` | `term_loyalty_report`、`terminology_consistency`、`term_consistency` |
| `evidence` | object | `grounding{claims_total,grounded,ungrounded,grounding_rate,anchor_quality,effective_confidence}, inventory_source, documents, pair_anchors_grounded` | `evidence_anchors`、`anchors`、`provenance` |
| `quality` | object | `engine, anchor_quality, page_count, segments_total, translated, untranslated, coverage, preservation{checked,clean,clean_rate,numbers_missing_total,...}, checks_failed, term_rate, term_usage_rate, glossary_entries, anchor_grounding_rate, detected_source_language, target_language, elapsed_s` | `grade_metrics`、`quality_metrics`、`validation` |
| `checks` | array | 逐段核验摘要：`seg_id, page, status, ok, issues[], numbers_missing[], citations_missing[], formulas_missing[]` | `translation_checks`、`verification`、`preservation_checks` |
| `review_checklist` | array | 人工确认项：`kind`（untranslated / preservation / anchor / term_consistency / ambiguous_translation）+ 定位与详情 | `needs_review`、`manual_review`、`review_list` |
| `translation_text` | string | 全文译文（未译段落写「（未生成译文：seg-XXXX，需人工确认）」） | `translated_body`、`full_translation`、`translated_text` |
| `source_text` | string | 原文全文（带 `[[page N]]` 标记） | `原文全文`、`original_text` |
| `segments` | array | 段清单（不含长文本）：`seg_id, order, page, block_id, bbox, section, char_count` | `source_passages`、`segment_index` |
| `limitations` / `uncertainty` / `status` / `language_pair` | array/array/string/object | 与 `meta.limits` / `meta.uncertainty` 同源；`language_pair` 含 `source, target, detected_source_language, resolved_target_language` | `limitations`、`uncertainty`、`status`、`language` |

**缺值语义**：契约声明了但没产出 → 按类型占位并列入 `meta.contract_fields_not_reported`；未声明但已产出的紧凑内容 → `meta.additional_outputs`（大数组仅在契约点名时进入契约字段，避免 result.json 无谓膨胀）；`case.json` 完全没有 output_contract 时，全部产物进入 `meta.additional_outputs`。

### 3.5 运行模式

```bash
python scripts/run_translate.py              # 默认 = --all：prepare 后立即 finalize（无 generated 时降级为 partial）
python scripts/run_translate.py --prepare    # 只做确定性准备，写 work/ 工作表与占位 result.json
python scripts/run_translate.py --finalize   # 只做核验与契约反射（可重复运行，覆盖旧 result.json）
python scripts/run_translate.py --check      # 只读检查已写出的 generated，输出 JSON 进度，不做提取
# 公共参数：--out <输出目录>  --work <工作目录>（默认依次探测 /app/work、<out>/work、<cwd>/work）
```

## 四、核心工作流

### 阶段 0：启动即落盘

无论哪个模式，进程启动后**先写占位 `result.json`**（`status: partial`，含 case_id 与契约字段数），再开始慢工作；顶层再包一层 `BaseException` 捕获，任何未预料的异常都会改写成 `status: failed` + `meta.traceback_tail`。容器在 900 秒被杀时，产物目录不会是零字节。

### 阶段 1（prepare）：确定性切段与抽事实

1. 发现输入 → 读 `case.json`（容忍 BOM）→ 解析 `output_contract` 与 `case_id`。
2. 解析题目自带术语表（`provided_glossary.json`）：json 数组/对象、csv/tsv/md 两列表格都能读；解析不了的记入 uncertainty，不猜。
3. 提取主文档（PDF/DOCX/TXT/MD…），跳过术语表类文件；多份材料时取第一份有文字层的。每步检查软截止（720 秒 - 90 秒余量）。
4. `hybrid.build_segments()` 切段：一个文本块 = 一段（锚点才能精确），超 1200 字符按句边界再拆；段上带 `order/page/block_ids/part/bbox/section_title/char_count/text`。
5. 段清单写 `work/segments.json`（含 `fingerprint`）。再次 prepare 时若 fingerprint 一致就**复用旧清单**，保证 `seg_id` 稳定（断点续跑不会错位）；不一致则重建并在 uncertainty 里说明。
6. 逐段抽机器可判事实（`facts.json` + 每张工作表内的 `must_keep`）。
7. 术语候选：`hybrid.term_candidates()`（缩写 + 高频 n-gram，top 80），逐条补出现次数、页码、例句、种子术语表建议译名；输出 `glossary_worksheet.json`。
8. 切批（每批 6 段 / ≤5200 字符）→ 用 `hybrid.make_batches()` 写 `work/batches/batch-XX.json`，再**就地增强**每张工作表：本批涉及的锁定术语、每段的 `must_keep`、上/下一批的边界上下文（跨页续译时携带）。
9. 若 Agent 已写 `generated/_glossary.json`（第二次 prepare），工作表会带上 `locked_terms`；已存在的批次答案标记 `already_answered: true`，**提示跳过，不重复消耗模型请求**。
10. 写 `protocol.json/index.json/state.json`，把占位 result.json 更新为「已准备、待 finalize」，stdout 打印下一步命令。

### 阶段 2（Agent 判断）：每批一次模型请求

**这是唯一的模型开销，严格按下面顺序执行。**

**第 1 步：写锁定术语表**（1 次请求）

- 读 `work/glossary_worksheet.json`：`candidates[]` 给出候选术语、出现次数、页码、例句、种子建议译名；`provided_terms[]` 是题目自带术语表（**必须沿用**，冲突时以题目为准）。
- 写 `work/generated/_glossary.json`，JSON 数组：

```json
[
  {"term": "supported nickel catalysts", "translation": "负载型镍催化剂", "keep_original": false, "note": "全文核心对象"},
  {"term": "XRD", "translation": "X射线衍射", "keep_original": true, "note": "缩写首次出现保留拉丁写法"}
]
```

- 规则：能全篇统一的术语才登记；`keep_original: true` 表示译文里必须保留原文写法（缩写、基因/蛋白名、仪器型号、单位）；不同源术语不能共用同一个译名（finalize 会把这种歧义记入 `ambiguous_translations`）。

**第 2 步：把锁定术语回灌到工作表**（0 次请求）

```bash
python scripts/run_translate.py --prepare
```

再跑一次 prepare 是**幂等**的：段清单复用、批次不重排，只是把 `locked_terms` 与本批每段的 `terms_in_segment` 写进工作表。这一步让「术语锁定」真正落到每批的输入里，而不是留在提示词里。

**第 3 步：逐批翻译**（每批 1 次请求，**这是「每批一次模型请求」的硬约定**）

- 对 `work/batches/` 下**尚未有对应答案**的每个 `batch-XX.json`：读该文件 → 把 `segments[]` 全部译成中文 → 写 `work/generated/batch-XX.json`（JSON 数组，一段一条）。
- `already_answered: true` 的批次**必须跳过**（保留已有答案），用来做「上下文/预算耗尽后断点续跑」。

```json
[
  {"seg_id": "seg-0001", "translation": "负载型镍催化剂上的段落对齐与术语研究", "terms_used": ["supported nickel catalysts"], "note": ""},
  {"seg_id": "seg-0002", "translation": "A. 作者，B. 作者——催化研究所", "terms_used": [], "note": ""}
]
```

翻译时必须遵守工作表里的 `instruction.rules`：

1. 只用 `locked_terms` / `terms_in_segment` 给出的译名，同一术语不得出现第二种译法；
2. 数字、单位、引用方括号、公式 token、图表/公式编号**逐字保留**在中文句子里（如 `92%`、`750 K`、`[3]`、`k = A exp(-Ea/RT)`、`图 2`）；
3. 限定词与否定要落地：may/might/suggest/probably → 可能/或许/提示/推测；not/cannot/never → 不/未能/不能；must/should → 必须/应当；
4. 不合并、不丢弃、不新增 `seg_id`；长段可以在句号处分句，但必须放在同一条记录的 `translation` 里；
5. 译不准或有歧义的地方写进 `note`，finalize 会把它当作「需人工确认」的线索（不会自动算作已完成质量）。

**第 4 步：进度自检**（0 次请求）

```bash
python scripts/run_translate.py --check
```

输出纯 JSON：`ready`、`batches_pending`、`coverage`、`problems`（未知/重复 seg_id、空译文、格式错误）、`next_actions`。`ready: true` 再进入 finalize；不 ready 就继续补批次（已答批次不会被重复要求）。

**第 5 步：finalize**（0 次请求）

```bash
python scripts/run_translate.py --finalize
```

### 阶段 3（finalize）：核验与契约反射

1. 读段清单（优先 prepare 的 `segments.json`，缺失则从文档重算并记 limit）；读 `generated/*.json` 与 `_glossary.json`。
2. `hybrid.merge_generated()` 校验覆盖率：`missing / duplicates / unknown_ids / incomplete` 全部进 `coverage`；有答案但 `translation` 为空的记录算 `incomplete`，等同未作答。
3. 逐段判定状态：`translated`（有译文且与原文不同）、`untranslated`（无记录/空译文/无中文字符）、`copied_source`（与原文完全相同）、`source_chinese`（源段本身是中文且目标为中→英时不需要翻译）。
4. 逐段保真检查 `translation_check()`：数字/单位、引用、公式 token、图表编号做集合比较；hedge/否定/情态做跨语言标记检查；汇总进 `grade_metrics.preservation`。
5. 术语一致性 `check_terms()`：对每个锁定术语，找出所有含该术语的源段，逐段检查译文是否含锁定译名（`keep_original` 时检查原文写法），产出 `per_term[].rate/status/offending_seg_ids` 与总 `term_rate`、`usage_rate`。
6. 锚点回源：为每对生成 `{page, block_id, bbox, quote}`，用 `anchors.verify_all()` 对**重新提取的文档**校验，得到 `exact/normalised/block_only/page_only/missing`；未落地即标 `grounded: false` 并写入 uncertainty。
7. 组装 values → `hybrid.finalize()` 反射契约写 `result.json`；另写 `evidence.json / run_report.json / translation_bilingual.md / glossary.json / review_checklist.md`。
8. 状态判定：全覆盖 = `success`；缺段/术语表缺失/文档不可回验 = `partial`（并把原因写进 `meta.limits`）；无段可译 = `failed`。

## 五、异常处理

| 异常情况 | 检测方式 | 处理策略 | 状态标记 |
|---|---|---|---|
| 找不到 case.json | 发现阶段 `case_path=null` | 照常解析材料与翻译，`meta.contract_detected=false`，全部产物进 `meta.additional_outputs` | partial |
| case.json 没有 output_contract | 契约解析为空 | 同上：不伪造字段，内容仍可从 additional_outputs 取 | partial |
| 没有材料文档 | `material_docs=[]` | 仍写 result.json（空数组 + 说明），不崩溃 | failed |
| 扫描件/无文字层 | 提取字符数 ≤20 或 `text_layer=false` | 不臆造文本，记 `attempts` 与 warnings（OCR 不在本技能能力内） | failed |
| 加密 PDF / 非 FlateDecode | 提取器 warnings | 能读多少写多少，锚点质量降级（`anchor_quality` 变 page/text） | partial |
| 首选 PDF 引擎缺失 | `engines_probed` / `attempts` | 自动降级到下一引擎（PyMuPDF→pdfplumber→外部工具→内置解析器） | success（带 uncertainty） |
| Agent 完全没写 generated | `records=[]` | 每段输出 `tgt=""` + `status="untranslated"`，写入 `omission_list` 与 limits | partial |
| 部分批次缺失/译文为空 | coverage.missing/incomplete 非空 | 缺失段降级为空译文并列出，其余段照常核验 | partial |
| generated JSON 损坏/形状不对 | 读取时按数组/`records`/JSONL 容错解析 | 坏记录跳过，`--check` 报 `problems`，finalize 覆盖率下降 | partial |
| seg_id 未知/重复 | merge 报告 `unknown_ids` / `duplicates` | 未知段忽略、重复段保留第一条；两者都写进 coverage 供复核 | partial（若有缺口） |
| 未写术语表 | `_glossary.json` 缺失 | 降级为「题目自带术语表 + 种子术语表」并标记未锁定，术语一致率仍会计算但来源标记 `source`；不改写译文 | partial |
| 题目自带术语表与 Agent 冲突 | merge 时逐条比对 | 以题目为准，记录 `glossary_status.conflicts`（不静默丢） | success（带 uncertainty） |
| 源段已是中文 | 段级判定 | 目标为中→英时按正常段处理；目标为 zh 时标 `source_chinese`、不要求译文 | success / partial |
| 源文档在 finalize 读不到 | `doc=None` | 锚点照发但标 `unavailable`，grounding 说明原因 | partial |
| 超过软截止（720 秒） | Stopwatch | 停止后续提取/阶段，先落盘已完成部分并写明 | partial |
| 任何未捕获异常 | 顶层 `BaseException` | 改写 result.json（failed + traceback_tail），保证有产物 | failed |
| 重复运行 | 每次覆盖写入 | 确定性输出（无时间戳；相同输入 → 相同 result 契约部分），只有 run_report 的 elapsed 会变 | success |

## 六、使用与方法说明

### 6.1 完整调用步骤（评测环境）

```bash
# 0) 环境自检（可选）：确认本机 PDF 引擎
python -c "import sys; sys.path.insert(0,'.'); from lib import extract; print(extract.probe())"

# 1) 确定性准备：切段 + 抽事实 + 写工作表 + 占位 result.json
python scripts/run_translate.py --prepare

# 2) Agent：读 work/glossary_worksheet.json → 写 work/generated/_glossary.json
# 3) Agent：再跑一次 prepare，把锁定术语注入每张工作表
python scripts/run_translate.py --prepare

# 4) Agent：逐批翻译（每批 1 次模型请求）→ 写 work/generated/batch-XX.json
# 5) 进度自检
python scripts/run_translate.py --check

# 6) 核验 + 契约反射
python scripts/run_translate.py --finalize

# 7) 检查产物
cat output/run_report.json             # 状态 / 覆盖 / 术语一致率 / 缺失批次 / 限制
cat output/glossary.json               # 术语表与逐术语一致率
less output/translation_bilingual.md   # 逐段双语对照与校验结论
```

一次性跑通（不含 Agent 判断）用 `--all`，此时 status 一定是 `partial`（没有译文），用来验证「零生成也产出合法契约」：

```bash
python scripts/run_translate.py --all
```

### 6.2 关键方法（为什么这样做）

- **两阶段混合架构**：判断（翻译）交给 Agent，其余全部确定性代码。模型只花在「译」上，重复运行、3×3 对照、900 秒预算都可控。
- **术语锁定写进数据流**：术语表 → 工作表 → 逐段 `terms_in_segment` → 逐段核对。基础 Agent 的「术语表」通常只是一段系统提示，换个段落就漂移；这里是输入字段 + 输出断言。
- **上下文携带**：每张工作表带上一批末段、下一批首段各 220 字符，跨页/跨栏段落不会因为切开而丢掉衔接。
- **断点续跑**：段清单 fingerprint 复用、`already_answered` 标记、`--check` 进度 JSON，让「预算耗尽的半成品」可以被下一次运行接着做完，而不是从头再花模型请求。
- **证据回源而非自证**：锚点 quote 从源段取，finalize 时对**重新提取的文档**校验（不是对同一份内存快照），match 结果如实分档；术语/数字核验是独立于锚点的第二路证据。
- **契约反射**：隐藏案例的字段名未知，写死 schema 必错；映射结果写进 `meta.contract_field_mapping` 与 `field_fill_ratio`，评审可核对每个声明字段由哪个内部值满足。

### 6.3 限制与边界（务必如实转述给用户）

1. **不做 OCR**：无文字层的扫描件会报 failed 并给出原因，不猜测内容。
2. **脚本不产生译文**：译文完全来自 Agent（评测的模型）。脚本负责「切得对、术语锁得住、译完能核验、契约填得满」。
3. **术语一致性是字符串包含判定**：能抓「同一术语两种译名」，不能判断同义改写是否等价；中文词形变化（如「催化剂的」vs「催化剂」）会命中，而语序重排可能误报，需看 `per_term[].offending_seg_ids` 人工确认。
4. **数字/引用/公式是集合比较**：允许位置变化，要求逐字保留（去空格后比较）；做了单位归一化后仍不相等即报缺。若译者刻意用中文数字（「三种」而不是「3 种」）会被标记，需在 `note` 中说明。
5. **表格不重建**：表头、表注、图注按普通段落处理；表格结构（行列）不在本技能范围。
6. **bbox 是块级**：一个段落块的包围盒，不是句子级坐标。
7. **不评判译文文采**：流畅度、学术语气、术语是否最优，不在脚本判定范围；脚本只给保真、一致性、覆盖、可核验四类证据。
8. **术语候选是启发式的**：缩写 + 高频短语（top 80），专有名词/低频术语可能漏；Agent 可自由补充（`--check` 会提示未知候选，但不会阻断）。
9. **模型请求计数以文件为准**：`run_report.model_requests_used` 记录脚本自身的 0 次；Agent 侧开销可由 `work/generated/*.json` 文件数推算（1 术语表 + 每批 1 次）。
10. **方向自动判断有边界**：源文是中英混排时按拉丁字母为主判定；若判定错误，脚本不会强制纠偏，只影响检查口径（仍会如实记录 `language_pair`）。

### 6.4 失败处理与自查

| 现象 | 自查方法 | 处理 |
|---|---|---|
| result.json 里某字段是空占位 | 看 `meta.contract_fields_not_reported` 与 `meta.contract_field_mapping` | 若在 not_reported：该内容确实没产出（看 limits 找原因）；若在 mapping：映射命中了别的值，检查 case.json 字段名是否与内部值同义 |
| 覆盖率 < 1 | 看 `paragraph_completeness.missing_seg_ids` 与 `batches_pending` | 补齐对应 `batch-XX.json` 后重跑 --finalize；已答批次不必重做 |
| 术语一致率低 | 看 `term_loyalty_report.per_term[].offending_seg_ids` | 找到违规段，把该段译名改成锁定译名，或修订术语表（二者取一，不能并存） |
| 数字缺失报警 | 看该 pair 的 `checks.numbers_missing` | 译文里补回被改写的数字/单位；若原文本身笔误，在 `note` 说明并保留原文数值 |
| 锚点 grounded=false | 看 `evidence.json.per_pair_anchors[].check.match` | `block_only/page_only`：提取引擎换了导致块边界变化；`missing`：该段来自错误的文档。最终结论以 evidence 为准，不要口头声明 |
| prepare 看起来「没反应」 | 看 `work_report.json` 与 `work/state.json` | 若 segments=0：材料无文字层（见 limits）；若 batch 已存在答案：`already_answered: true` 是正常跳过 |
| 重复运行结果不同 | 比对两次 `result.json` 的契约部分（忽略 `meta.output_files` 与 elapsed_s） | 契约内容应一致；不一致说明材料/引擎变化，看 run_report 的 `engines_probed` 与 uncertainty |
| 900 秒被中断 | 看 result.json 的 `meta.status` 与 `limits` | 占位/部分结果已落盘；重跑 --finalize 即可（无需重新翻译） |

## 七、最小调用示例（**示意，非正式案例结果**）

输入（由 `tests/test_translate.py` 生成的合成样例，3 页英文论文，18 段，6 条参考文献）：

```
fixture/
├── case.json      # case_id=tr-demo-003，output_contract 声明 11 个字段（字段名故意与内部名不同）
├── task.md
└── paper.pdf      # 合成论文：标题/摘要/方法/结果/讨论/结论/参考文献，含数字、引用、公式
```

调用：

```bash
cd fixture
python ../../skills/reading-03-translation/scripts/run_translate.py --prepare
# Agent 写生成物（见 §4 阶段 2）
python ../../skills/reading-03-translation/scripts/run_translate.py --finalize
```

prepare 的 stdout 末尾：

```
PREPARE OK  case=tr-demo-003  segments=18  batches=3  terms=6  locked=0  work=output\work
NEXT (agent):
  1. read output\work\glossary_worksheet.json -> write output\work\generated\_glossary.json
  2. (optional but recommended) re-run --prepare so worksheets carry locked_terms
  3. for each output\work\batches\batch-XX.json write output\work\generated\batch-XX.json (one model request per batch)
  4. python scripts/run_translate.py --finalize --work output\work
  5. python scripts/run_translate.py --check --work output\work   # resume-safe progress check
```

finalize 的 stdout 末尾：

```
FINALIZE DONE  case=tr-demo-003  status=success  segments=18  coverage=1.0  term_rate=1.0  checks_clean=18/18
  outputs: output\result.json, output\translation_bilingual.md, output\glossary.json, output\review_checklist.md
```

`result.json` 摘要（**示例，非正式结果**；字段顺序 = 声明顺序）：

```json
{
  "case_id": "tr-demo-003",
  "zh_en_paragraph_pairs": [
    {
      "pair_id": "pair-0004", "seg_id": "seg-0004", "order": 4, "page": 1,
      "block_id": "p1-b4", "bbox": [72.0, 660.0, 637.0, 684.5],
      "section": "Abstract",
      "src": "We report that supported nickel catalysts may reach 92% selectivity ...",
      "tgt": "我们报道负载型镍催化剂在 750 K、1 atm 下对甲烷重整的选择性可达 92% [1]……",
      "status": "translated",
      "anchor": {"page": 1, "block_id": "p1-b4", "bbox": [72.0, 660.0, 637.0, 684.5], "quote": "We report that supported nickel catalysts may reach 92% ..."},
      "anchor_check": {"grounded": true, "located": true, "match": "exact"},
      "basis": {"terms": [{"term": "supported nickel catalysts", "translation": "负载型镍催化剂", "keep_original": false, "used": true}], "notes": []},
      "checks": {"label": "seg-0004", "ok": true, "issues": [], "numbers_total": 5, "numbers_missing": [], "citations_total": 1, "citations_missing": [], "formulas_total": 0, "formulas_missing": [], "hedges_source": ["may"], "hedges_target_markers": ["可达"], "hedges_missing": [], "negations_source": ["not"], "negations_missing": [], "modals_source": [], "modals_missing": [], "length_source": 214, "length_target": 148}
    }
  ],
  "terminology_bank": [{"term": "conversion", "translation": "转化率", "keep_original": false, "note": "", "source": "agent"}],
  "translated_body": "……",
  "paragraph_completeness": {"total_segments": 18, "received": 18, "coverage": 1.0, "translated": 18, "untranslated": 0, "missing_seg_ids": [], "batches_total": 3, "batches_answered": 3, "batches_pending": [], "pages": {"1": 6, "2": 6, "3": 6}},
  "term_loyalty_report": {"available": true, "terms_checked": 7, "consistent_terms": 7, "inconsistent_terms": 0, "term_rate": 1.0, "instances_total": 14, "instances_used": 14, "usage_rate": 1.0, "per_term": [], "violations": []},
  "omission_list": [],
  "evidence_anchors": {"grounding": {"claims_total": 18, "grounded": 18, "ungrounded": 0, "grounding_rate": 1.0, "anchor_quality": "span", "effective_confidence": 1.0}, "inventory_source": "prepare_manifest", "pair_anchors_grounded": 18},
  "grade_metrics": {"checks_failed": 0, "term_rate": 1.0, "anchor_grounding_rate": 1.0, "preservation": {"checked": 18, "clean": 18, "clean_rate": 1.0, "numbers_missing_total": 0, "citations_missing_total": 0}},
  "source_passages": [{"seg_id": "seg-0001", "order": 1, "page": 1, "block_id": "p1-b1", "bbox": [72.0, 744.0, 637.0, 762.0], "section": null, "char_count": 88}],
  "原文全文": "[[page 1]] Paragraph Alignment and Terminology in the Translation of Supported Nickel Catalysts ...",
  "qqq_unmodelled_field": "",
  "meta": {
    "status": "success",
    "contract_detected": true,
    "contract_field_count": 11,
    "contract_fields_not_reported": ["qqq_unmodelled_field"],
    "field_fill_ratio": 0.9091,
    "contract_field_mapping": {"zh_en_paragraph_pairs": "bilingual", "terminology_bank": "glossary", "translated_body": "translation_text", "paragraph_completeness": "coverage", "term_loyalty_report": "term_consistency", "omission_list": "untranslated", "evidence_anchors": "evidence", "grade_metrics": "quality", "source_passages": "segments", "原文全文": "source_text"},
    "limits": [], "uncertainty": [], "change_log": ["finalize: deterministic coverage / preservation / term checks and contract reflection"]
  }
}
```

> 说明：以上数字取自 `tests/test_translate.py` 的实际运行（18 段、3 批、术语一致率 1.0、锚点 18/18 exact），但它是**合成 fixture 的示意输出**，不代表任何正式案例的结果。

## 八、架构参考与差异说明

文档结构参考了平台公开技能目录中的 `scientific-paper-translator`（技能定位→适用条件→调用方式→工作流→异常处理→使用说明→最小示例）与 `bohr-cli`（「踩坑」章节），代码复用本仓库 `skills/_core` 共享内核（`hybrid` / `contract` / `anchors` / `extract` / `structure` / `discover`）。只借鉴结构组织与共享能力，翻译侧的切段、术语锁定、跨语言校验、批次协议是本技能新增的。

| 维度 | 常见做法（含基础 Agent） | 本技能做法 |
|---|---|---|
| 术语 | 系统提示里写「术语统一」 | 术语表落成 JSON 数据 → 注入每批工作表 → 逐段代码核对，违规定位到 seg_id |
| 分段 | 按字符数硬切 | 块=段落切分，带页码/bbox；超长块只按句边界拆，锚点不丢 |
| 跨页/断点 | 从头重译 | 段清单 fingerprint 复用 + `already_answered` + `--check` 进度 JSON |
| 核验 | 模型自述「已核对」 | 数字/引用/公式集合比较 + 跨语言标记检查 + 锚点回源校验，全部落盘 |
| 输出契约 | 固定 schema | 从 `case.json` 反射；缺值按声明类型占位并记账 |
| 失败 | 抛错/无产物 | 启动即占位、顶层兜底、缺生成降级为 `untranslated` 并给原因 |
| 运行 | 依赖第三方包 | 仅标准库；提取引擎运行时探测并逐级降级 |

## 九、环境踩坑（`--help` 不会告诉你的）

1. **%TEMP% 可能不可写**：受限沙箱里 `tempfile.TemporaryDirectory()` 会 `PermissionError`。本技能所有中间产物都在 `<out>/work`（或 `/app/work`）内，测试也把临时目录建在包内。
2. **工作目录解析两阶段必须一致**：prepare 与 finalize 分开两次运行时，`/app/work` 不一定可写。解析顺序是 `--work` → `/app/work` → `<out>/work` → `<cwd>/work`，**优先复用已存在且含 batches/generated/segments.json 的目录**，两边不要传不同路径。不确定时显式传 `--work`。
3. **必须跑两次 prepare**：第一次生成候选术语表；Agent 写完 `_glossary.json` 后要再跑一次，工作表才会带 `locked_terms`。跳过这一步，术语只会被事后核对，不会被「预先锁定」。
4. **不要手改 work/batches/**：prepare 会用确定性的切分覆盖它们。要改译文请改 `work/generated/` 下的答案文件。
5. **generated 文件必须是 JSON 数组**（或 `{"records":[...]}` / JSONL）：`_glossary.json` 以 `_` 开头，所以不会被当成批次记录；批次答案文件名必须与工作表同名（`batch-01.json`）。
6. **seg_id 是稳定契约**：不要重编号、不要新增。改了段清单（换文档/换引擎导致 fingerprint 变化）会让旧答案对不上，finalize 会把它报告成 unknown/missing，而不是猜。
7. **BOM**：`case.json` / 用户提供的术语表可能带 BOM（Windows 工具写入），读取一律用 `utf-8-sig`；写出的 JSON 一律不带 BOM。
8. **控制台编码**：Windows PowerShell 里直接 `Out-File` 中文会乱码/写 BOM；本技能的产物由 Python 直接 `write_text(encoding="utf-8")`，用 `jq` / Python 查看即可，不要用 PS 的重定向。
9. **import 兼容两种布局**：开发布局是 `skills/<题目>/` + 同级 `skills/_core`；打包后是单目录（`lib/` `scripts/` `tests/` `resources/` 展平）。脚本用 `try: from lib import ...` / `except ImportError: 插入 ../_core` 的写法，自行扩展时保持这个模式。
10. **模型请求预算要按批核算**：1 次术语表 + 每批 1 次；18 段样例 = 4 次。不要为每段单独发请求（会把 64 次预算打光），也不要一次把所有段塞进一个请求（会超 16384 tokens 且术语漂移）。
11. **不要用 result.json 的体积判断质量**：`meta.additional_outputs` 只保留紧凑值，大数组只在契约点名时出现；判断质量看 `grade_metrics` / `term_loyalty_report` / `evidence_anchors`。
