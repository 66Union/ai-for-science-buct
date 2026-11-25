---
name: reading-14-nanopublication
description: 把科研论文的结论拆成可独立判真假的原子断言，为每条断言绑定逐字证据片段与页码/块坐标，再导出 assertion–provenance–publicationInfo 三层结构的 Nanopublication（TriG + JSON-LD）并反射 case.json 的 output_contract。Use when a task asks to turn paper conclusions into atomic, evidence-bound, machine-readable assertions or nanopublications.
version: 1.0.0
author: 66Union
license: MIT
tags: [nanopublication, assertion-decomposition, evidence-binding, provenance, trig, jsonld, output-contract]
---

# 科研断言级 Nanopublication 构建（Reading-14 Nanopublication）

## 一、技能定位

对应本题四个考察问题，本技能给出的是**可执行、可复核**的机制，不是自述：

| 考察问题 | 本技能的做法 | 可验证的证据 |
|---|---|---|
| **拆得细** | 先把文档切成页锚定的 segment，再把每个 segment 切成**编号句子**（`seg-0003-s02`），把数字/引文/模糊限定/否定/情态/图表引用/关系线索等机器可判事实逐句抽出；再要求 Agent 一条断言只表达一个主张，并用**原子性判据**（多句/多连接词/多谓词/逗号列举）打分与给拆分建议 | `work/segments.json` 的 `sentences[].facts`；`assertions[].atomic` 与 `quality_flags`；`audit_metrics.atomicity_rate` |
| **对得上** | 每条断言必须提供 `seg_id` + `quote`；finalize 用**归一化包含校验**（NFKC + 空白折叠 + 大小写不敏感）确认 quote 真的是该 segment 原文的连续子串，再回源到持久化的块快照做锚点复核（page/block_id/bbox）；任一步不过即**拒绝**，不进结果 | `evidence.json` 的 `assertion_checks`；`rejected_assertions.json` 的 `reason`；`meta.status=partial` + limits |
| **追得回** | 每条断言携带 source 文档指纹（sha256）、作者/年份/DOI/版本、页码、块 id、bbox、原句；Nanopublication 的 provenance 图把断言指回 source URI，pubinfo 图记录断言 id、关系、置信度、质量标记、技能版本 | `nanopubs.trig` 的 `prov:wasDerivedFrom` 与 `npv:quote`；`sources.json`；`assertions[].provenance` |
| **读得懂** | 契约反射生成 `result.json`（字段名由隐藏 case 决定，绝不硬编码）；同时输出标准形态的 TriG（head 三元组 + 3 个命名图）与 JSON-LD 视图；稳定 ID 由 sha256 前 16 位决定，重复运行一致 | `result.json.meta.contract_field_mapping`；`nanopubs.trig` / `nanopubs.jsonld`；`audit_metrics.serialization` |

**与"让模型写一段 nanopublication 文本"的差别**：本技能把"拆—绑—核—序列化"拆成确定性代码与 Agent 判断两段。Agent 只做判断（拆句、定关系、给置信度），所有校验、编号、契约映射、序列化都由代码完成并且**失败会记账**（拒绝原因、weak_evidence、limits、status）。

## 二、适用条件

| 项目 | 说明 |
|---|---|
| 输入格式 | 论文 PDF（有文本层）、Markdown、TXT、JATS XML、DOCX、PPTX、TEX；隐藏案例的主材料自动发现，最多提取 3 份 |
| 语言 | 英文、中文论文均可（关系线索词表以英文为主，中文句切分与事实抽取可用） |
| 篇幅 | 单篇；工作簿默认覆盖 salience 最高的 96 个 segment（`--max-segments` 可调），其余由确定性基线兜底 |
| 依赖 | **仅 Python 标准库**；无网络、无模型 API、无需 rdflib |
| 不适用 | 扫描件无文本层（无 OCR）；需要图表数值还原；需要跨论文实体消歧/知识融合；需要 DOI 联网核验 |

## 三、调用方式

### 触发方式

任务要求"把论文结论拆成断言/构建 nanopublication/为结论绑定证据片段/导出可机器读取的断言结构/标注页码来源"时触发。

### 输入（运行时由评测环境提供）

```text
/input/case.json        必选，含 case_id 与 output_contract（唯一契约来源）
/input/task.md          必选，题目说明
environment/ 或材料目录   可选，随题下发的论文材料（位置自动发现）
```

### 输出（默认 /app/output）

| 文件 | 内容 |
|---|---|
| `result.json` | 契约字段 + case_id + meta（status / limits / uncertainty / contract_field_mapping / additional_outputs / evidence / degraded）。声明字段按声明顺序输出，缺值按声明类型占位并列入 `contract_fields_not_reported` |
| `nanopubs.trig` | TriG 序列化：每条断言一个 head 三元组 + assertion / provenance / publicationInfo 三个命名图 |
| `nanopubs.jsonld` | 同一图数据的 JSON-LD 视图（含 @context 与 @graph） |
| `assertions.json` | 最终断言表（= 契约里映射到断言字段的同一数组） |
| `rejected_assertions.json` | 被拒绝记录与 weak_evidence 记录 + 判定理由 |
| `sources.json` | 论文来源元数据（文件、sha256、版本、作者、年份、DOI、缺失字段） |
| `quality.json` | 质量指标（原子率、grounding、关系分布、flag 计数、批覆盖、序列化校验） |
| `evidence.json` | 每条断言的锚点、回源校验结果、块快照指纹、拒绝/弱证据清单 |
| `run_report.json` | 状态、耗时、预算、输入、批次进度、降级标记 |

### 三种运行模式

```bash
# 1) 只做确定性准备：写工作表，供 Agent 生成断言
python scripts/run_nanopublication.py --prepare --out /app/output --work /app/work

# 2) Agent 生成之后：校验、序列化、契约反射
python scripts/run_nanopublication.py --finalize --out /app/output --work /app/work

# 3) 没有判断输入时的确定性兜底（prepare + finalize，走抽取式基线）
python scripts/run_nanopublication.py --all --out /app/output --work /app/work
```

常用参数：`--case <case.json>`（显式指定契约文件）、`--out`、`--work`、`--batch-size`（默认 8）、`--max-segments`（默认 96）、`--max-baseline`（默认自动）。默认模式等价于 `--all`。

**脚本路径**：平台把本技能装到 `/skills/weekly-skill`（扁平布局：`lib/`、`scripts/`、`tests/`、`resources/`、`SKILL.md`），此时脚本是 `/skills/weekly-skill/scripts/run_nanopublication.py`；开发仓库布局下是 `skills/reading-14-nanopublication/scripts/run_nanopublication.py`。脚本自己解析 `lib/` 的两种位置，无需设置 `PYTHONPATH`。

**启动即写占位**：任何模式都在最慢的工作开始前先写一份 `status=partial` 的 `result.json`；顶层再包一层 `except BaseException`，异常时写 `status=failed` 的结果。容器在 900 秒预算处被 kill 时不会是零产出。

## 四、核心工作流

### 阶段 1：prepare（确定性，约 0.1–3 秒）

1. **发现输入**：按 `/input`、`/app/input`、`/app/work`、`/app/environment`、`/app`、当前目录的顺序找 case.json、task.md 与材料；找不到契约时仍继续，产物全进 `additional_outputs`。
2. **提取文本+坐标**：引擎链 PyMuPDF → pdfplumber → 外部工具 → 内置解析器，记录尝试原因；坐标统一为 y 向上。
3. **纯文本重建段落块**：当材料是 .txt/.md（引擎 `plain:*`）时，共享提取器会把整篇合并成单一 block（行距聚类容差覆盖了裸换行），因此本技能按**空行段落**重建块，并把 Abstract/Introduction/Results 等标题单独成块，否则会只有 1 个 segment、无法分批。
4. **切段 + 切句**：`hybrid.build_segments` 按页锚定切段（长块按句边界再切，不新增事实）；每个 segment 再切成编号句子（`seg-0001-s01`），逐句抽 numbers / citations / hedges / negations / modals / labels / formula_tokens / acronyms，并标注 `suggested_relation` 与命中的关系线索词。
5. **选段分批**：先剔除参考文献/致谢/附录等非断言来源段与纯标题短段（记入 `work/segments_skipped.json` 与 limits），再按 salience（数字×2 + 引文×2 + 关系线索×3 + 模糊限定 + 长度）挑出最多 `--max-segments` 段，按文档顺序写 `work/batches/batch-XX.json`；每批含 instructions、allowed_relations、answer_schema、example_record、segment 原文与编号句子。
6. **落表**：`work/segments.json`（全量段+句+事实）、`work/doc_blocks.json`（块级提取快照，finalize 回源校验用）、`work/source_metadata.json`、`work/vocab_snapshot.json`、`work/manifest.json`、`work/prepare_report.json`、`work/generated/_example.json`。
7. 结束打印 `PREPARE_OK case=... segments=... worksheets=... batches=... work=...`。

### 阶段 2：Agent 生成（判断，唯一需要模型的部分）

逐批执行：

```bash
# 1. 看有哪些工作表
ls /app/work/batches/          # batch-01.json, batch-02.json, ...

# 2. 读一批（含原文、编号句子、事实、允许的关系、答案 schema、字段示例）
#    work/batches/batch-01.json

# 3. 把本批断言写回同名文件（注意目录是 generated/，文件名与工作表一致）
#    work/generated/batch-01.json
```

答案文件格式（每个批次一个文件；也接受聚合文件 `work/generated/assertions.json` 里的 `assertions` 数组，但按批计覆盖时以 batch-XX.json 为准）：

```json
{
  "batch_id": "batch-01",
  "records": [
    {
      "assertion_id": "a-0001",
      "seg_id": "seg-0009",
      "sentence_id": "seg-0009-s02",
      "text": "Conversion increased from 42% to 85% as temperature rose from 473 K to 523 K.",
      "subject": "conversion",
      "relation": "measures",
      "object": "85% at 523 K",
      "condition": "473 K to 523 K",
      "confidence": 0.82,
      "quote": "Conversion increased from 42% to 85% as temperature rose from 473 K to 523 K.",
      "direction": "subject->object",
      "evidence_type": "direct"
    }
  ]
}
```

字段规则（finalize 会逐条校验，违反即拒绝）：

| 字段 | 必填 | 校验与规则 |
|---|---|---|
| `seg_id` | 是 | 必须是本工作表出现过的 seg_id，否则 `unknown_seg_id` 拒绝 |
| `text` | 是 | 一条断言只表达一个主张，≤240 字符最佳；多句/多连接词会被标 non-atomic，但只有严重超长（>1000 字符）才拒绝 |
| `quote` | 是 | 必须逐字复制自该 segment 原文的连续片段；先 exact、再归一化包含校验，都不过则 `quote_not_in_segment` 拒绝；随后还要通过块锚点复核，否则 `anchor_not_verified:*` 拒绝 |
| `subject` / `object` | 建议 | 尽量出现在 quote 里；不在会标 `subject_not_in_quote` / `object_not_in_quote`（不拒绝，但影响可复核性） |
| `relation` | 是 | 只能取 `allowed_relations` 中的 7 个受控关系；不在词表的会先按线索词映射，映射后打 `relation_not_in_vocab` + `relation_mapped_to_vocab`，仍失败则落 `states` |
| `confidence` | 否 | 0–1；给了就采用并标 `agent_reported`，没给按规则算（见 §六） |
| `direction` | 否 | `subject->object` 或 `object->subject`；两者都在 quote 中时会比对出现顺序并给 `consistent` / `reversed` |
| `evidence_type` | 否 | `direct`（quote 直接陈述）或 `indirect`（由 quote 推出）；未给时按 quote 匹配等级推断 |
| `condition` | 否 | 温度、剂量、人群、时间等限定范围 |

**判断纪律**：一段结论有并列/因果就拆成多条；不要把整段原文塞进一个 `text`；不要编造原文没有的数字；不确定的断言把 `confidence` 降低而不是删掉证据。

### 阶段 3：finalize（确定性，约 0.2–2 秒）

1. **加载**：读 work 目录（缺 manifest 时自动补跑 prepare 并写 limit 说明）、segments、块快照、来源元数据、词表。
2. **校验生成记录**：seg_id → text → quote 归一化校验 → 锚点回源（page/block_id/bbox）→ 关系词表映射 → 原子性判定 → 方向检查 → 置信度；失败进 rejected。
3. **去重**：按归一化后的 text 去重，保留置信度更高者，合并数记入 `duplicates_merged`。
4. **降级基线**（仅当生成内容缺失/不全）：见 §七。
5. **证据复核**：对全部断言调用 `anchors.verify_all`，要求 quote 位于并且断言自身关键词出现在 quote 中；不满足者隔离为 `weak_evidence`，不进入 nanopublication。
6. **构建 Nanopublication**：每条断言生成 head URI + 三个图；稳定 ID = sha256(text + quote + source_document_id) 前 16 位，前缀 `np-`。
7. **序列化写盘**：`nanopubs.trig`（自校验结构）、`nanopubs.jsonld`、`assertions.json`、`rejected_assertions.json`、`sources.json`、`quality.json`。
8. **契约反射**：`hybrid.finalize` 用别名/词干映射把内部产物映射到 case.json 声明的字段；再做一次**二次补齐**（同一概念被契约要求两次时，第二次仍能拿到值，记录在 `meta.contract_repair`）；仍未命中的按声明类型占位并列入 `contract_fields_not_reported`；写 `result.json`、`evidence.json`、`run_report.json`；打印 `FINALIZE_OK status=... assertions=... nanopubs=... grounding=... batches=... trig_valid=...`。

## 五、字段字典

### 5.1 断言对象（`assertions[]`，映射进契约的断言字段）

| 字段 | 类型 | 含义 |
|---|---|---|
| `assertion_id` | string | 唯一断言 id（Agent 给或按序生成 `a-0001`） |
| `seg_id` | string | 来源 segment，可回查到页/块/原文 |
| `sentence_id` | string | 来源句子编号（如 `seg-0009-s02`），可选 |
| `text` | string | 断言正文（单一可判真假的主张） |
| `subject` / `relation` / `object` | string | 主—关系—宾；`relation` 必在受控词表内 |
| `relation_raw` | string | Agent 原始关系名（若被映射，可追溯） |
| `condition` | string | 限定条件（温度、剂量、范围等），可空 |
| `confidence` / `confidence_basis` | number / string | 置信度与依据（`agent_reported` 或 `rule_based:base+quote_exact+atomic+relation_in_vocab`） |
| `quote` | string | 支持该断言的逐字证据片段 |
| `quote_match` | string | `exact` / `normalised`（校验等级） |
| `anchor_match` | string | 块锚点复核结果（`exact` / `normalised`） |
| `page` / `block_id` / `bbox` | int / string / array | 原文位置 |
| `section_title` | string | 所在章节（能识别时） |
| `direction` / `direction_check` | string | 关系方向与顺序检查（`consistent` / `reversed` / `not_checkable`） |
| `evidence_type` | string | `direct`（直接陈述）/ `indirect`（推论） |
| `atomic` / `split_hint` / `quality_flags` | bool / array / array | 原子性判定、拆分建议、质量标记 |
| `evidence_support` / `anchor_located` | bool | 关键词是否落在 quote / 锚点是否定位成功 |
| `origin` | string | `agent_generated` / `baseline_extractive` |

### 5.2 Nanopublication（`nanopublications[]`）

每条形如：

```json
{
  "nanopub_id": "np-07351aa587c230cd",
  "head_uri": "https://w3id.org/np/RA07351aa587c230cd",
  "assertion_uri": "https://w3id.org/np/RA07351aa587c230cd#assertion",
  "provenance_uri": "https://w3id.org/np/RA07351aa587c230cd#provenance",
  "publication_info_uri": "https://w3id.org/np/RA07351aa587c230cd#publicationInfo",
  "assertion": {"text": "...", "subject": "...", "relation": "measures",
                "relation_type_iri": "https://w3id.org/np/skill-reading14/vocab#measures",
                "object": "...", "condition": "...", "atomic": true,
                "quality_flags": [], "confidence": 0.82, "evidence_type": "direct"},
  "provenance": {"source_document": "src-...", "source_file": "paper.pdf",
                 "source_sha256": "...", "source_version": "v1+sha256:...",
                 "page": 3, "block_id": "p3-b2", "bbox": [72.0, 600.0, 480.0, 700.0],
                 "quote": "...", "quote_match": "exact", "seg_id": "seg-0009",
                 "sentence_id": "seg-0009-s02", "evidence_type": "direct"},
  "publication_info": {"assertion_id": "a-0001", "relation_type": "measures",
                       "atomic": true, "quality_flags": [],
                       "generated_by": "reading-14-nanopublication",
                       "skill_version": "1.0.0", "protocol_version": "1.0",
                       "serialization": "trig+jsonld", "origin": "agent_generated"}
}
```

### 5.3 来源元数据（`source_metadata`）

`document_id`、`file`、`path`、`sha256`、`bytes`、`hash_scope`、`version`（`v1+sha256:前12位`）、`retrieved`、`title`(+`title_source`)、`authors`(+`authors_source`)、`year`(+`year_source`)、`doi`(+`doi_source`)、`keywords`、`abstract`、`venue`、`engine`、`anchor_quality`、`page_count`、`total_chars`、`citation`（一行可读引用）、`provenance_chain`（case→file→sha256→page/block/quote）、`missing_fields`（诚实列出没识别到的字段）。若契约把 `title`/`authors`/`year`/`doi` 声明为独立字段，同时提供对应的扁平标量值（`paper_title` 等别名也能命中）。

### 5.4 质量指标（`quality`）

`assertions_total`、`assertions_generated`、`assertions_baseline`、`atomic_assertions`、`atomicity_rate`、`nanopublications_total`、`quote_verified_total`、`grounding_rate`、`anchor_quality`、`effective_confidence`、`mean_confidence`、`relation_distribution`、`quality_flag_counts`、`duplicates_merged`、`rejected_total`、`weak_evidence_total`、`batch_coverage`、`relation_vocab`、`source_completeness`、`serialization`（含 `trig_valid`/`triple_count`/`graph_count`）、`deterministic`、`no_model_calls`、`no_network`。

### 5.5 序列化（`serialization`）

`format`（`trig+jsonld`）、`trig_path`/`jsonld_path`（文件名，不放绝对路径以保证重复运行一致）、`trig_valid`、`triple_count`、`graph_count`、`stable_id_rule`、`trig`（完整 TriG 文本，便于下游直接解析）、`jsonld`（完整 JSON-LD 对象）。

## 六、受控关系词表（`resources/relation_vocab.json`，v1.0.0）

| relation | 语义 | 线索词（节选） |
|---|---|---|
| `supports` | 证据/结果支持某主张或假说 | support, consistent with, confirm, in line with, corroborat |
| `contradicts` | 与某主张/既往结论冲突 | contradict, inconsistent with, conflict with, unlike, challenges |
| `uses` | 研究使用某方法/材料/数据 | use, used, using, employ, based on, relies on |
| `measures` | 测量/量化并给出取值 | measure, measured, yield, reached, was found to be |
| `derived_from` | 由某数据/模型/前体推导或计算得到 | derived from, computed from, calculated from, obtained from |
| `part_of` | 组成部分/从属关系 | part of, component of, belongs to, subunit, member of |
| `states` | 源文直接陈述的事实（默认关系） | report, show, find, observe, demonstrate, indicate, suggest |

方向统一为 `subject->object`（subject 是断言谈论的实体）。Agent 给出的关系不在词表时：先用 quote 命中的线索词映射；映射成功打 `relation_not_in_vocab`+`relation_mapped_to_vocab`；失败则回落到 `states`。词表没有命中任何线索时，基线一律使用 `states`。

**置信度规则**（Agent 未给 `confidence` 时）：`0.55 基础 + 0.15 quote 精确匹配（或 +0.08 归一化匹配）+ 0.10 原子 + 0.10 关系在词表内`，上限 0.95。所有参数写在词表文件的 `confidence_basis` 段，可改可复用。

## 七、断言原子性判据（代码化，可复现）

| flag | 判据 | 是否影响 atomic |
|---|---|---|
| `multiple_sentences` | 句子切分后 >1 句 | 是 |
| `multiple_connectives` | 命中 ≥2 个连接词（and/or/but/while/whereas/because/therefore/thus/moreover/furthermore/however/also/which/that/;） | 是 |
| `multiple_predicates` | ≥2 个不同断言动词（report/show/indicate/suggest/support/contradict/measure/yield/reach/increase/decrease/…） | 是 |
| `comma_list` | ≥3 个逗号 | 是 |
| `too_long` | >240 字符 | 是（太长通常塞了多件事） |
| `long` | >160 字符 | 否（仅提示） |
| `single_connective` | 恰好 1 个连接词 | 否（仅提示） |
| `multiple_quantities` | >2 个数值 | 否（趋势句常有多个数） |

`atomic = 不存在任何"是"类 flag`。存在 `multiple_sentences`/`multiple_connectives`/`multiple_predicates` 时，还会给 `split_hint`（按句号和 ; / , and 等切开的前 1–3 段），Agent 可据此拆分为多条断言。该判据是**启发式**，用于提示与统计，不替代人工判断。

## 八、证据绑定与校验规则

1. **必须逐字**：`quote` 先做 exact 子串检查；失败再做归一化（NFKC + 空白折叠）大小写不敏感包含检查，得 `normalised`；都失败 = `quote_not_in_segment`，拒绝。改写一个标点都可能从 `exact` 退到 `normalised`。
2. **必须回源定位**：以 prepare 持久化的块快照 `work/doc_blocks.json`（含 `blocks_sha256`）为对照，用 `anchors.verify(page, block_id, quote)` 复核；结果不是 `exact`/`normalised` 即 `anchor_not_verified:*` 拒绝。
3. **必须真的提到断言内容**：`anchors.verify_all` 要求断言自身关键词出现在 quote 里（中文关键词走数字/英文词元）；不满足 = `weak_evidence` 隔离，不进 nanopublication，`meta.status` 降为 `partial`。
4. **隔离而非静默丢弃**：拒绝原因写 `rejected_assertions.json`（含原始记录），并在 `meta.limits` 与 `quality.rejected_total` 体现；弱证据另列 `weak_evidence`。
5. **grounding 指标**：`grounding_rate = 通过 / 全部`，另给 `anchor_quality` 上限（`span`=1.0 / `page`=0.7 / `text`=0.4）与 `effective_confidence`。基线（抽取式、无判断）一律 0.35 置信度，避免虚高。

**降级基线**（生成内容缺失时）：按文档顺序取含数字/引文/图表引用/关系线索的句子，最多 24 条（`--max-baseline` 可调），`relation="states"`、`object=""`、置信度 0.35，并打 `baseline_extractive` / `relation_default_states` / `object_not_resolved`。若只有部分批次有答案：仅为未覆盖批次补最多 8 条基线；若所有批次都有答案：不补基线（尊重 Agent 判断）。

## 九、序列化规范

- **稳定 ID**：`nanopub_id = "np-" + sha256(text + quote + source_document_id)[:16]`，head URI = `https://w3id.org/np/RA{16hex}`。同一输入重复运行 ID 不变，便于去重与引用。
- **TriG 结构**（每个 nanopub 一段）：

```turtle
@prefix np: <http://www.nanopub.org/nschema#> .
@prefix npv: <https://w3id.org/np/skill-reading14/vocab#> .
@prefix prov: <http://www.w3.org/ns/prov#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

<https://w3id.org/np/RA07351aa587c230cd> np:hasAssertion <...#assertion> ;
    np:hasProvenance <...#provenance> ;
    np:hasPublicationInfo <...#publicationInfo> .

<...#assertion> {
    <...#subject> <https://w3id.org/np/skill-reading14/vocab#measures> <...#object> .
}

<...#provenance> {
    <...#assertion> prov:wasDerivedFrom <https://w3id.org/np/source/15e8a0ea5e4b29ee> ;
        npv:sourceDocument "src-..." ; npv:sourceVersion "v1+sha256:..." ;
        npv:segId "seg-0009" ; npv:blockId "p3-b2" ; npv:bbox "72.0,600.0,480.0,700.0" ;
        npv:page "3"^^xsd:integer ; npv:quote "..." ; npv:quoteMatch "exact" ;
        npv:subjectLabel "conversion" ; npv:objectLabel "85% at 523 K" ;
        npv:assertionText "..." ; npv:confidence "0.82"^^xsd:decimal .
}

<...#publicationInfo> {
    <https://w3id.org/np/RA07351aa587c230cd> npv:assertionId "a-0001" ;
        npv:relationType "measures" ; npv:atomic "true"^^xsd:boolean ;
        npv:qualityFlag "..." ; npv:skill "reading-14-nanopublication" ;
        npv:skillVersion "1.0.0" ; npv:serializationFormat "trig" .
}
```

- **不带时间戳**：为保证"重复运行结果一致"，TriG/JSON-LD 不写创建时间；时间信息在 `run_report.elapsed_s`（允许每次不同）。
- **自校验**：沙箱没有 rdflib，`validate_trig` 自写检查（前缀声明、图花括号配平、每条语句以 ` .` 或 `;` 结尾、续行省略主语、引号配平、谓词形式），结果写 `quality.serialization.trig_valid`；不通过会把 status 降为 partial 并写 limits。
- **JSON-LD**：`@context` 映射 np/npv/prov/dcterms/xsd，`@graph` 中每个节点含 `np:hasAssertion/prov:wasDerivedFrom/PublicationInfo` 嵌套节点，可用任意 JSON 解析器直接读，也可被 JSON-LD 处理器展开。

## 十、异常处理

| 异常情况 | 检测方式 | 处理策略 | 状态标记 |
|---|---|---|---|
| 找不到 case.json | 发现阶段为空 | 继续产出，`contract_detected=false`，全部内容进 `meta.additional_outputs` | partial |
| 没有材料文档 | 材料列表为空 | 写 failed 结果，声明字段按类型占位 | failed |
| 扫描件无文本层 | `text_layer=false` | 不做 OCR，不臆造文本，limits 说明 | partial |
| 文档无可用 segment | segments 为空 | 契约字段全部占位，写失败原因 | failed |
| 加密/损坏 PDF | 提取器 attempts/warnings | 记录引擎尝试链，能读多少读多少 | partial |
| 生成文件缺失 | `generated/` 为空 | 抽取式基线；limits 写明降级 | partial |
| 生成文件缺部分批次 | 批覆盖 < 1.0 | 未覆盖批次补最多 8 条基线 | partial |
| 断言 quote 非原文 | 归一化包含校验失败 | 拒绝该条，记 `quote_not_in_segment` | partial（其余照常） |
| 断言 seg_id 不存在 | 段清单查找失败 | 拒绝，记 `unknown_seg_id` | partial |
| 关系不在词表 | 词表查找失败 | 先线索词映射，失败落 `states` 并打 flag | success（带 flag） |
| 断言与 quote 无关 | 关键词校验失败 | 隔离为 weak_evidence，不进 nanopub | partial |
| 重复断言 | 归一化 text 去重 | 保留高置信度，记 `duplicates_merged` | success |
| TriG 自检失败 | `validate_trig` | 仍写出文件，status 降级并写明 | partial |
| 超软截止（720s） | 计时器 | 先落盘已完成部分，后续步骤裁剪 | partial |
| 任何未捕获异常 | 顶层 `BaseException` | 写 failed 结果 + `traceback_tail` | failed |

## 十一、限制

1. 只对**主材料**做断言分割；最多提取 3 份材料，第 2/3 份仅记录在 manifest/sources。
2. 无 OCR：扫描件、图片型 PDF 无法产出文本层。
3. 段落/标题重建对纯文本材料是启发式（空行 + 标题正则）；排版怪异的文本可能切错段。
4. 关系判定是"受控词表 + 线索词 + Agent 判断"，不做深层语义推理；方向检查只对"主宾都出现在 quote 中"的断言有效。
5. 原子性判据是启发式统计指标，不等于人工标注的真值。
6. 不做实体消歧/共指消解（"the catalyst" 不会自动链接到 "Ni/SiO2"）。
7. 不做跨论文去重与知识图谱合并；本技能只解决单篇论文的断言级 nanopublication。
8. DOI/作者/年份来自本地正则识别，不联网核验；识别不到会列入 `source_metadata.missing_fields`。
9. TriG 是自写序列化器，语法自检只覆盖结构合法，不做 RDF 语义推理；如需严格校验请在允许第三方库的环境用 rdflib 复验。
10. 表格与公式作为普通文本处理，不还原单元格、不做数学语义解析。
11. 工作簿默认只覆盖 salience 最高的 96 段（大论文会截断，limits 说明）；可通过 `--max-segments` 调整，但会增加 Agent 请求数（评测预算 64 次模型请求 / 900 秒）。

## 十二、失败处理与自查

| 现象 | 自查方法 | 处理 |
|---|---|---|
| `status=partial` 且 `assertions_baseline>0` | 看 `quality.batch_coverage` 与 `meta.limits` | 说明 Agent 没写够批次；补齐 `work/generated/batch-XX.json` 后重跑 finalize |
| 断言数量为 0 | 看 `meta.status` 与 `rejected_assertions.json` | 若 segments 为空则是材料不可读；若全被拒绝看 reason |
| `grounding_rate < 1` | 看 `evidence.json` 的 `weak_evidence_assertions` | 断言 text 与 quote 用词差太远，改成贴近原文或补关键词 |
| 大量 `quote_not_in_segment` | 对比 quote 与 worksheet 原文 | 必须逐字复制；去掉省略号、改写、跨句拼接 |
| `trig_valid=false` | 看 `quality.serialization.trig_errors` | 自检报错会指出行号；通常是文本里未转义引号（本技能已自动转义） |
| 契约字段为 null/`[]` | 看 `meta.contract_field_mapping` 与 `contract_fields_not_reported` | 说明内部产物没匹配到声明字段；检查是否漏产出该族内容。若契约把同一概念声明两次，二次补齐应已填上（见 `meta.contract_repair`） |
| 重复运行结果不一致 | 对比两次 `nanopubs.trig` 与契约字段 | `run_report.elapsed_s` 每次不同属正常；断言/TriG 应完全一致（不含时间戳） |
| 找不到生成的答案 | `ls work/generated/` | 文件名必须是 `batch-XX.json`（与 worksheet 同名），且不能放在别的目录 |

## 十三、最小调用示例（示例，非正式案例结果）

```bash
# 目录准备：case.json + task.md + materials/paper.txt（合成示例，非真实文献）
python scripts/run_nanopublication.py --prepare \
    --out ./out --work ./work --batch-size 4
# 把模拟断言写入 work/generated/batch-01.json（格式见 §四阶段 2）
python scripts/run_nanopublication.py --finalize --out ./out --work ./work
```

**示意输出**（取自合成样例的实际运行，仅演示接口形态）：

```text
PREPARE_OK case=np-demo-001 segments=11 worksheets=11 batches=3 work=./work
FINALIZE_OK status=success case=np-demo-001 assertions=6 nanopubs=6 grounding=1.0
            batches=3/3 trig_valid=True out=./out
```

```json
{"case_id": "np-demo-001",
 "claim_table": [{"assertion_id": "a-0001", "seg_id": "seg-0009",
                  "text": "Conversion increased from 42% to 85% as temperature rose from 473 K to 523 K.",
                  "subject": "conversion", "relation": "measures", "object": "85% at 523 K",
                  "condition": "473 K to 523 K", "confidence": 0.82,
                  "quote": "Conversion increased from 42% to 85% as temperature rose from 473 K to 523 K.",
                  "page": 1, "block_id": "p1-b9", "quote_match": "exact",
                  "atomic": true, "quality_flags": [], "origin": "agent_generated"}],
 "nanopub_records": [{"nanopub_id": "np-...", "assertion": {"relation": "measures"},
                      "provenance": {"page": 1, "quote": "..."},
                      "publication_info": {"assertion_id": "a-0001"}}],
 "paper_provenance": {"title": "Nanopublication Demo: Supported Nickel Catalysts",
                      "authors": ["A. One", "B. Two"], "year": "2024",
                      "doi": "10.1234/demo.2024.001", "version": "v1+sha256:..."},
 "controlled_vocabulary": [{"relation": "supports", "definition": "..."}],
 "audit_metrics": {"assertions_total": 6, "grounding_rate": 1.0, "atomicity_rate": 1.0,
                   "serialization": {"trig_valid": true}},
 "trig_export": {"format": "trig+jsonld", "trig": "<TriG text>", "jsonld": {...}},
 "evidence_bundle": {"grounding": {"claims_total": 6, "grounding_rate": 1.0}},
 "unused_field_x": [],
 "meta": {"status": "success", "contract_fields_not_reported": ["unused_field_x"],
          "contract_field_mapping": {"claim_table": "assertions"}}}
```

> 以上为**示例输出**（合成样例、示意结构），不是任何正式案例的结果。契约字段名来自示例 case.json；正式案例字段名以隐藏 case 的 output_contract 为准。

## 十四、架构参考与差异说明

文档结构参考平台公开技能目录中的 `scientific-paper-translator`（章节编排、异常处理表、状态机、核验报告、限制与自查）与 `bohr-cli`（"踩坑"章节），实现复用本仓库共享内核 `lib/hybrid.py`（两阶段框架、契约反射、证据锚点、确定性工具）。只借鉴结构组织，未复制其内容。

| 维度 | 常见做法 | 本技能做法 |
|---|---|---|
| 核验 | 模型自述"已核对" | 代码断言：quote 归一化包含 + 块锚点回源 + 关键词支持，失败隔离并记账 |
| 输出契约 | 固定 JSON schema | 从 `case.json.output_contract` 反射，缺值按声明类型占位并记入 `contract_fields_not_reported` |
| 生成粒度 | 一段一个断言字段 | 段落→segment→编号句子→原子断言，四条可机读判据给拆分建议 |
| 来源可追溯 | 只写文件名 | 文件 sha256 + 版本串 + 页/块/bbox + 原句 + `prov:wasDerivedFrom` + 一行可读 citation |
| 序列化 | 自由 JSON | 稳定 ID + TriG（assertion/provenance/publicationInfo 三图）+ JSON-LD + 自校验 |
| 依赖 | 声明依赖、缺失即失败 | 运行时探测 + 逐级降级，降级原因写入 limits/attempts |
| 被杀风险 | 未处理 | 启动即写占位 result.json；顶层异常写 failed |

## 十五、环境踩坑（`--help` 不会告诉你的）

1. **纯文本会被合并成一个 block**：共享提取器对 .txt/.md 的行距容差覆盖裸换行，导致整篇一个 block、只有一个 segment。本技能在 `engine=plain:*` 时按空行重建段落块、标题单独成块，否则工作簿无法分批。
2. **`%TEMP%` 可能不可写**：受限沙箱下 `tempfile.TemporaryDirectory()` 直接 PermissionError。自测与中间产物一律落在工作区内的 `_selftest_tmp`。
3. **不要用 `pip install` 补依赖**：受限环境装包会因元数据写入被拒失败；本技能刻意只用标准库，TriG 校验器自写。
4. **finalize 依赖 prepare 产物**：work 目录被清空时 finalize 会自动补跑 prepare（会多花提取时间），正确做法是显式先 `--prepare`。
5. **生成文件路径/名必须对**：`work/generated/batch-01.json`（与 worksheet 同名，列表类字段用 `records`）。文件名不对 finalize 会认为"没生成"，直接走基线并降级。
6. **quote 逐字**：改时态、去冠词、加省略号都会退化成 `normalised` 或 `missing`；跨句拼接一定失败。
7. **Windows + PowerShell 管道**：本技能输出是 UTF-8；用 `Out-File -Encoding utf8` 会加 BOM，读取端必须 `utf-8-sig`（内核已这样读 case.json）。
8. **不要往 result.json 写绝对路径**：重复运行一致性要求同一输入同一输出；路径只放 `meta.output_files`，序列化字段用文件名。
9. **大论文的取舍**：默认工作簿 96 段；如果隐藏案例要求覆盖全文，调大 `--max-segments` 但要注意 64 次模型请求预算（每批 1 次请求）。
