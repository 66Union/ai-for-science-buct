---
name: reading-15-knowledge-graph
description: Build an evidence-bound literature knowledge graph from one or more papers - extract entities (researcher/material/method/task/metric/conclusion), align aliases and acronyms across papers without merging homonyms, keep only relations the text actually states (with direction, condition and a verbatim quote), bind every node and edge to a paragraph locator, execute at least three real queries, and export graph.json / nodes.csv / edges.csv / graph.graphml plus a result.json that mirrors the case output_contract. Use when a task asks to construct, align, query or audit a citation/evidence-grounded knowledge graph from research literature.
version: 1.0.0
author: 66Union
license: MIT
tags: [knowledge-graph, entity-alignment, relation-extraction, evidence-grounding, output-contract, two-phase-hybrid, graphml]
---

# 文献知识图谱构建（Reading-15 Knowledge Graph）

## 一、技能定位

对应本题考察的四个问题：

| 考察问题 | 本技能的做法 | 可验证的证据 |
|---|---|---|
| **抽得准** | 候选实体由确定性抽取器给出（重复术语、缩写、化学式、大写短语、领域词表、作者行、含主张标记的结论句），Agent 只做"是不是论文里真实出现的科研对象"的判断；实体必须能在材料段落中定位到逐字文本 | graph_nodes[].evidence[] 的 paragraph_id + quote；丢掉无法定位的实体并记入 evidence.json.unverified |
| **连得对** | 只保留有触发动词支撑的边；每条边带 relation / direction / condition / strength；边的 quote 必须同时包含两端实体的写法，否则代码断言失败、该边被丢弃 | relationship_edges[].quote；graph_audit_metrics.grounding |
| **对得齐** | 三类合并依据（同形归一 / 材料内缩写定义 / Agent 显式别名），每次合并记录 kept_label、merged_labels、basis、confidence、evidence；类型不同的同名对象**永不合并**，进 conflict_register | entity_alias_map[].basis；conflict_register[].kind=homonym_type_conflict |
| **查得到、回得去** | 至少 3 个真实执行的查询（邻居 / 关系过滤 / 类型清单 / 证据定位 / 跨篇共享 / 跨篇一致性 / 路径 / 未决问题），每个结果带结果子图与支撑证据；无证据时明确返回 no_result 或 undetermined，不编造 | query_outcome_bundle[].status/answer/evidence；source_evidence_anchors[] |

**与常见做法的差异**：
1. **两阶段混合架构**：切段、找候选、抽机器可判事实、回源校验全部不消耗模型请求（Harbor 预算 64 请求 / 900 秒），模型只做判断层；同一输入重复运行结果逐字节一致。
2. **证据是代码断言，不是模型自述**：每条节点/边的 quote 由 anchors.verify 回源定位，并额外要求节点 quote 含该实体写法、边 quote 同时含两端写法；失败即丢弃并记账。
3. **契约反射**：result.json 由 case.json 的 output_contract 反射生成，隐藏案例的字段名不可知，本技能不写死 schema；缺值按声明类型占位并列入 meta.contract_fields_not_reported。
4. **定位不造假**：材料包给的是 JATS/发布方段落 ID 时，anchor.page 一律为 null（locator_kind=jats_paragraph_id 或 generated_paragraph_id），绝不把自建序号叫 PDF 页码；只有材料真的是 PDF 时才报告真实页码。
5. **同名冲突不强行合并**、**跨篇同一关系但条件不同不合并成一条结论**、**生成内容缺失时降级为确定性基线图**而不是空结果。
6. **增量保号**：同一输出目录重跑时，命中既有图节点（同类型 + 同归一化 surface）的节点沿用原 node_id，便于"新论文加入时保留既有编号"。

## 二、适用条件

| 项目 | 说明 |
|---|---|
| 输入格式 | JSON/JATS 式材料包（primary-material.json、secondary-material.json、material.json、corpus-manifest.json 等）、PDF、Markdown、TXT、DOCX、TEX、BIB；材料文件按内容特征发现，不依赖固定文件名 |
| 语言 | 不限；实体/关系触发词表以英文为主，中文材料回退到共现与引文证据 |
| 篇数 | 1~6 篇（--max-docs 可调）；两篇及以上才有跨篇对齐与跨篇查询价值 |
| 定位方式 | 材料自带段落 ID 时用段落 ID；PDF 用真实页码 + 块 ID；纯文本用块 ID（page 为 null） |
| 依赖 | **仅 Python 标准库**（json / re / csv / xml / pathlib / argparse）；不联网、不调用模型 API、不装第三方包 |
| 不适用 | 需要 OCR 的扫描件；需要读取图像/补充附件里数值的题目；需要链接到外部权威实体库（Wikidata 等）的实体链接；要求实时增量的在线图谱数据库 |

## 三、调用方式

### 触发方式

当任务要求"从一批论文构建知识图谱 / 抽取实体与关系 / 做跨篇别名对齐 / 为图谱提供可执行查询 / 为每条边给出原文依据 / 导出可读图谱文件"时触发。纯单篇摘要、翻译、润色任务不应触发本技能。

### 命令（三种模式，默认 --all）

```bash
# 0) 进入技能根目录（平台安装位置）
cd /skills/weekly-skill

# 1) 默认：prepare + finalize 一次跑完，并在没有 Agent 生成文件时交付确定性基线图
python scripts/run_kg.py --all --out /app/output

# 2) 两阶段：先准备批工作表与草稿图（同时先落一份基线 result.json）
python scripts/run_kg.py --prepare --out /app/output --work /app/output/work

# 3) Agent 写完 work/generated/*.json 后升级结果
python scripts/run_kg.py --finalize --out /app/output --work /app/output/work
```

| 参数 | 默认 | 说明 |
|---|---|---|
| --all | 默认模式 | prepare + finalize；无 Agent 生成内容时自动降级基线图 |
| --prepare | 关 | 只做确定性准备：材料发现、切段、候选实体、关系证据、批工作表、草稿图；结束时先落一份 status=partial 的基线 result.json |
| --finalize | 关 | 读取 work/generated/*.json，合并、回源校验、执行查询、反射契约、导出图文件 |
| --out | 自动探测 /app/output、/output、output | 输出目录 |
| --work | <out>/work | 工作表与 Agent 生成文件目录 |
| --case | 自动发现 | 显式指定 case.json |
| --max-docs | 6 | 最多读取的材料文件数 |

### 输入发现顺序

1. 依次扫描 /input、/app/input、/app/work、/app/environment、/app 与当前目录（discover 不硬编码路径）。
2. 按扩展名收集材料；**跳过** case.json、task.md、上次运行产物（result.json / evidence.json / run_report.json / graph.json / manifest.json）与输出目录子树。
3. 材料角色分类：文件名含 primary → 原论文；含 secondary → 新增论文；含 manifest/corpus/license → 元数据清单（只用于补 DOI/作者/许可，不当作论文正文）；material.json 类 → 汇总索引（有 primary/secondary 时**不重复抽取**）；其余 JSON → 尝试按段落解析，解析不出段落层就跳过（不把 JSON 索引当正文）；PDF/MD/TXT/DOCX 走 extract 引擎链。
4. 每篇论文的 title/DOI/authors 只来自它自己的文件或 corpus-manifest 里与该 DOI/标题/文件名匹配的记录；**绝不把 case.json 根部的 title/DOI/authors 套到第二篇论文**，缺失就留 null 并说明。

### 输出文件

| 文件 | 内容 |
|---|---|
| result.json | case_id + **契约字段**（按 case.json 声明顺序）+ meta（status、映射表、占位字段、limits、uncertainty、change_log、evidence 摘要、output_files） |
| evidence.json | grounding 汇总、逐节点/逐边证据与校验结果、别名合并记录、冲突、被丢弃的不可验证断言、材料清单、查询执行记录 |
| run_report.json | 状态、耗时、预算、材料与引擎、生成文件清单、生成的节点/边使用量、增量信息、limits / uncertainty |
| graph.json | 可读图谱（nodes / edges / merge_log / conflicts / documents / stats） |
| nodes.csv / edges.csv | 表格版节点与边（含 quote 列），可直接进 Excel/pandas |
| graph.graphml | 手写 XML（标准 graphml 命名空间），可导入 Gephi / Cytoscape / NetworkX |

### 运行状态

| status | 判定 |
|---|---|
| success | 有材料，且 Agent 生成的节点与边至少各有一条通过校验，无不可验证断言被丢弃，≥3 个查询可执行，未触发软截止 |
| partial | 有材料但发生降级：无 work/generated/*.json、生成的节点或边全部被丢弃、有断言因证据不可定位被丢弃、可执行查询不足 3 个、超过 720 秒软截止 |
| failed | 没有任何可用材料（仍写出契约形状与失败原因，绝不零字节） |

## 四、核心工作流

### 阶段 0：启动即占位

进程启动后立刻用 hybrid.write_placeholder 写 result.json（status=partial），再开始任何慢操作。容器在 900 秒预算处被直接杀掉时，这份占位文件保证"被杀 ≠ 零产出"。顶层 try/except BaseException 再兜底写 status=failed 的 result.json（含 traceback_tail）。

### 阶段 1：--prepare（确定性，不消耗模型请求）

1. **材料发现与分类**（见 §3 输入发现顺序），写 work/doc_index.json：每篇论文的 doc_id、角色、来源文件、title/DOI/authors/license、元数据来源（own_file / corpus_manifest / not_reported）、定位方式、段落数。
2. **段落解析**：对 JSON 材料识别三种形态（一文件一篇 / 一个 papers 列表 / 段落自带 doc_id 的汇总文件）；保留材料自带的 paragraph_id（locator_kind=jats_paragraph_id），缺失时生成 <doc>-p0001（generated_paragraph_id）。非 JSON 材料走 lib/extract 引擎链（PyMuPDF → pdfplumber → 外部工具 → 内置解析器），真实 PDF 报告真实页码。
3. **切段**：调用 hybrid.build_segments，一段一块（块长超限时按句边界切分），生成全局唯一 seg_id（doc-01-s0001 形式）；每段携带 doc_id、paragraph_id、section、bbox、文本。
4. **候选实体**（只是清单，不是决定）：重复术语（2/3-gram + 缩写词）、大写短语、化学式（TiO2 / Ni/SiO2）、领域词表（方法/指标/材料/任务）、作者行与 "X et al."、含主张标记的结论句；逐个候选回扫段落生成最多 3 个证据锚点，启发式给类型，写 work/entity_candidates.json。
5. **关系证据**：逐句找同时提到 ≥2 个候选的句子，检测触发动词（outperform / compared with / supports / contradicts / evaluated on / characterized by / uses / improves / leads to / proposes / is a），抽取条件片段（温度、压力、浓度、数据集），写 work/relation_evidence.json。
6. **草稿图**：把候选实体与触发词边组装成 work/baseline_graph.json（含 co_occurrence_candidates 共现候选，并明确标注"共现不是关系"）。
7. **批工作表**：按每批 ≤6 段、≤4800 字符切批，写 work/batches/batch-XX.json（含 documents、segments、candidate_entities、relation_hints、expected_shape、write_to）。
8. **协议与清单**：写 work/agent_protocol.md（Agent 生成协议全文）、work/query_seeds.json（可执行查询种子 + 可复用查询模板）、work/manifest.json（统计与下一步命令）。
9. **先落基线结果**：prepare 末尾立即跑一次 finalize 逻辑但忽略未生成的 Agent 内容，写出一份完整的确定性基线 result.json（status=partial）。这样即使 Agent 不写任何生成文件，也会得到一份合法图谱；后续 --finalize 只是覆盖升级。

### 阶段 2：Agent 判断层

按 §五 的协议读取工作表，写 work/generated/nodes.json、edges.json、queries.json（或按批写 batch-XX.json）。这一步消耗模型请求，应该只做判断：选真实实体、补别名、确认关系类型与方向、抄原文条件、写自然语言查询。

### 阶段 3：--finalize（确定性）

1. **读取生成内容**：整图模式读三个文件；分批模式读 batch-XX.json（支持 nodes/edges 键、records 数组、单条记录）；按字段而不是文件名判断记录属于节点还是边。
2. **证据定位与回源校验**：Agent 给的 seg_id 必须在段落清单里；quote 必须是段落文本的子串（大小写可不同）；节点要求 quote 含 label 或其别名；边要求 quote 同时含两端写法（label 或别名）。每类记录实际调用 anchors.verify 回源，失败即丢弃并写入 evidence.json.unverified。
3. **别名合并与同名冲突**（见 §六 合并规则）。
4. **跨篇条件冲突**：同一对节点、同一关系在多篇论文中条件不同 → 保留 doc-scoped 边，不合成一条结论，写入 conflict_register（kind=cross_doc_condition_conflict）。
5. **查询执行**：先执行 Agent 声明的查询，不足 3 个可执行查询时用确定性模板补齐；结果带结果子图与证据。
6. **导出与契约反射**：导出 graph.json / nodes.csv / edges.csv / graph.graphml，再调用 hybrid.finalize 反射 output_contract 写 result.json / evidence.json / run_report.json。
7. **增量保号**：若输出目录已有 graph.json，按（类型 + 归一化标签/别名）匹配既有节点并沿用 node_id，run_report 记录 preserved/new。

### 阶段 4：状态与降级

按 §三 的状态判定计算 success/partial/failed；降级原因逐条写入 result.meta.limits 并同步到 run_report。

## 五、Agent 操作协议（精确命令与 JSON 格式）

### 步骤 1：准备

```bash
cd /skills/weekly-skill
python scripts/run_kg.py --prepare --out /app/output --work /app/output/work
```

stdout 最后一行是 JSON 摘要（stage=prepare，含 documents / segments / candidates / baseline_nodes / baseline_edges / batches / work_dir）。随后读取：

- /app/output/work/doc_index.json —— 两篇论文的 doc_id、标题、DOI、定位方式（paragraph_id 是发布方段落 ID，page 为 null 表示材料包没有 PDF 页码）。
- /app/output/work/segments.json —— 全部段落（seg_id / doc_id / paragraph_id / section / text）。
- /app/output/work/entity_candidates.json —— 候选实体 + 出现位置；只是候选，可增可删。
- /app/output/work/relation_evidence.json —— 同时提到多个候选的句子、触发动词、条件片段。
- /app/output/work/batches/batch-01.json … —— 分批工作表，字段见下。
- /app/output/work/baseline_graph.json —— 确定性草稿图（可直接复制为 generated 再修正，节省时间）。
- /app/output/work/agent_protocol.md —— 本协议的工作目录版。

### 步骤 2：写生成文件（整图模式，推荐）

**nodes.json**：JSON 数组，每个元素字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| label | 是 | 实体在原文里的写法；必须能在 seg_id 段落的 text 中逐字找到（大小写可不同） |
| type | 建议 | researcher / material / method / task / metric / conclusion / dataset / concept / other；缺省按 other 处理 |
| node_id | 否 | 自定 id，供 edges.source/target 引用；不写则用 label 引用 |
| aliases | 否 | 同一对象的其他写法（缩写、全称、大小写变体）；用于跨篇对齐与边端点匹配 |
| doc_id | 建议 | 首次出现所在论文；缺省时用 seg_id 推导 |
| seg_id | 建议 | 证据段落；缺失时脚本按 label 在全部段落里回找 |
| quote | 建议 | 该段落文本的逐字子串；缺失时脚本用段落原文挑窗口 |

```json
[
  {"node_id": "n1", "label": "Ni/SiO2 catalyst", "type": "material",
   "aliases": ["Ni/SiO2"], "doc_id": "doc-01", "seg_id": "doc-01-s0001",
   "quote": "We report a Ni/SiO2 catalyst for CO2 methanation"},
  {"node_id": "n2", "label": "CO2 methanation", "type": "task",
   "aliases": ["CO2 hydrogenation"], "doc_id": "doc-01",
   "seg_id": "doc-01-s0002", "quote": "CO2 methanation is a key reaction"}
]
```

**edges.json**：JSON 数组，每个元素字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| source / target | 是 | 节点 node_id 或节点 label（大小写不敏感）；解析不到即拒绝并记账 |
| relation | 是 | uses / applies_to / evaluates_on / outperforms / improves / characterizes / supports / contradicts / causes / proposes / is_a / compares_with / part_of / other |
| direction | 否 | directed / undirected；缺省按关系类型表 |
| condition | 否 | 原文的适用条件（温度、压力、浓度、数据集、样本），没有则空串 |
| doc_id / seg_id | 建议 | 证据所在论文与段落 |
| quote | 是（否则丢弃） | 同时包含两端实体写法的逐字原句 |
| strength | 否 | strong / moderate / weak，或 0~1 数值 |
| edge_id | 否 | 自定 id |

```json
[
  {"edge_id": "e1", "source": "n1", "target": "n2", "relation": "applies_to",
   "direction": "directed", "condition": "at 350 C and 1 bar",
   "doc_id": "doc-01", "seg_id": "doc-01-s0001",
   "quote": "We report a Ni/SiO2 catalyst for CO2 methanation that reaches 85% CO2 conversion at 350 C and 1 bar.",
   "strength": "strong"}
]
```

**queries.json**：至少 3 个真实查询，每个元素：query_id、natural_language、op、params。

| op | params | 返回 |
|---|---|---|
| neighbors | entity（label 或 node_id） | 与该实体直接相连的节点 + 关联边 + 证据；无关系时 no_result |
| relation | relation（关系类型） | 该类型的全部边及证据；没有该关系时明确回答"没有证据支持" |
| type_list | node_type | 指定类型的节点清单 |
| evidence_lookup | ref_id / entity / edge_id | 节点或边在原文的段落 + 原句 + 校验结果 |
| cross_doc_shared | node_type（可选） | 出现在 ≥2 篇论文的概念节点（跨篇对齐结果） |
| cross_doc_consistency | 无 | 两篇论文是否存在同一物理样品/数据集/条件：未报告时返回 undetermined 并说明 |
| path | source、target、max_hops（默认 3） | 两节点最短证据路径；不通时 no_result |
| unresolved | 无 | 合并冲突与证据不足的未决项 |

```json
[
  {"query_id": "q1", "op": "neighbors", "params": {"entity": "Ni/SiO2 catalyst"},
   "natural_language": "与 Ni/SiO2 catalyst 直接相关的实体有哪些？"},
  {"query_id": "q2", "op": "cross_doc_shared", "params": {"node_type": "method"},
   "natural_language": "两篇论文共同使用了哪些方法？"},
  {"query_id": "q3", "op": "cross_doc_consistency", "params": {},
   "natural_language": "两篇论文是否使用了同一物理样品或实验条件？"}
]
```

### 步骤 3：分批模式（可选）

对每个 work/batches/batch-XX.json 写 work/generated/batch-XX.json：{"batch_id": "batch-01", "nodes": [...], "edges": [...]}。整图模式与分批模式可以混用，finalize 会合并去重。

### 步骤 4：收尾

```bash
python scripts/run_kg.py --finalize --out /app/output --work /app/output/work
```

stdout 最后一行 JSON 给出 status / nodes / edges / queries_executed / grounding_rate / fallbacks。随后自查：

```bash
# 契约字段是否齐、哪些字段没产出
python -c "import json;d=json.load(open('/app/output/result.json',encoding='utf-8'));print(d['meta']['status']);print(d['meta']['contract_fields_not_reported']);print(len(d.get('graph_nodes') or []),len(d.get('relationship_edges') or []))"
# 证据与丢弃项
python -c "import json;d=json.load(open('/app/output/evidence.json',encoding='utf-8'));print(d['grounding']['grounding_rate']);print(len(d.get('unverified') or []))"
```

### 步骤 5：硬性规则

1. label 必须能在对应段落中逐字找到；写不出 quote 的实体不要写（会被丢弃并计 partial）。
2. 边的 quote 必须同时含两端写法；不要把"同一段里一起出现"当关系。
3. 关系方向：directed 表示 source 作用于 target；对称比较用 compares_with（undirected）。
4. condition 写原文条件，不要跨论文合并数值或样本。
5. 两篇论文的同一术语只在概念层合并；相同通用术语不等于相同物理样品/数据集。
6. 不要用 case.json 根部的 title/DOI/authors 给第二篇论文赋值；用 work/doc_index.json 里各自的元数据。
7. 每篇论文的实体尽量都有 doc_id/seg_id；跨篇概念请显式写 aliases 说明合并依据。

## 六、字段字典

### 节点类型（9）

| type | 定义 |
|---|---|
| researcher | 材料中出现的作者/研究者 |
| material | 物理材料、样品、化合物、催化剂、试剂 |
| method | 方法、技术、仪器、流程 |
| task | 研究任务、反应、基准、数据集、问题设置 |
| metric | 测量量、性能指标（conversion / selectivity / accuracy 等） |
| conclusion | 论文陈述的发现/主张（一句一条） |
| dataset | 具名数据集/语料 |
| concept | 不属于上述类型的科研概念 |
| other | 真实实体但类型不在词表内 |

### 关系类型（15）

| relation | 方向 | 定义 |
|---|---|---|
| uses | directed | source 使用/应用 target |
| applies_to | directed | source 适用于 target |
| evaluates_on | directed | source 在 target 上被评估 |
| outperforms | directed | source 优于 target |
| improves | directed | source 提升/降低 target |
| characterizes | directed | source 方法表征 target |
| supports | directed | source 证据支持 target 主张 |
| contradicts | directed | source 与 target 矛盾 |
| causes | directed | source 导致 target |
| proposes | directed | source 提出/给出 target |
| is_a | directed | source 是 target 的一种 |
| compares_with | undirected | 二者被比较 |
| part_of | directed | source 是 target 的一部分 |
| co_occurs | undirected | 确定性降级：同段共现，**明确不断言关系** |
| other | unspecified | 真实但不在词表的关系（关系名保留在 relation_raw，relation_known=false） |

别名规范：use/used/employ 等映射到 uses；better_than/superior_to 映射到 outperforms；compared_with/vs 映射到 compares_with；同义表见 resources/kg_schema.json 的 relation_aliases。

### 强度

strong（同句显式动词）=0.9 / moderate（跨小句）=0.6 / weak=0.35 / co_occurrence（仅同段）=0.2。

### 节点字段（result.graph_nodes[] / graph.json.nodes[]）

node_id、label、type、aliases、doc_ids、paragraph_ids、evidence（锚点数组，≤3）、evidence_count、evidence_status（grounded/unverified）、origin（agent/baseline）、type_source、is_conclusion、cross_doc_merged、scope（concept）。

### 边字段（result.relationship_edges[] / graph.json.edges[]）

edge_id、source、target、source_label、target_label、relation、relation_raw、relation_known、direction、condition、strength、strength_score、doc_id、paragraph_id、seg_id、quote、evidence（锚点数组，≤2）、evidence_status、origin。

### 查询结果字段（result.query_outcome_bundle[]）

query_id、natural_language、op、params、status（answered / no_result / undetermined / unsupported_op）、answer、result_nodes[]（node_id/label/type/doc_ids）、result_edges[]（边字段子集）、evidence[]、limitations[]、auto_generated（模板补足时为 true）。

### 证据锚点字段（result.source_evidence_anchors[] / evidence.json）

evidence_id、kind（node/edge）、ref_id、label、doc_id、paragraph_id、page（非 PDF 材料恒为 null）、block_id、bbox（JSON 段落为 null）、quote、locator_kind（jats_paragraph_id / generated_paragraph_id / pdf_page / block）、section、match（exact/normalised/block_only/page_only/missing）、supported_by_quote、grounded。

### 合并与冲突字段

- entity_alias_map[]：merge_id、node_id、kept_label、merged_labels[]、basis[]（same_surface / acronym_definition / agent_alias / label_variant）、rule、confidence（同形 0.95，其余 0.9）、doc_ids、evidence[]。
- conflict_register[]：conflict_id、kind（homonym_type_conflict / cross_doc_condition_conflict）、labels[]、types[] 或 relation、doc_ids、conditions[]、detail、resolution（kept_separate / kept_doc_scoped）。

### 质量字段（result.graph_audit_metrics）

documents、paragraphs、segments、nodes、edges、node_type_counts、relation_type_counts、entity_type_coverage{required,present,ratio}、relation_type_coverage{schema_types,present,ratio,off_schema_relations}、cross_doc{shared_nodes,shared_node_types}、grounding{claims_total,grounded,ungrounded,grounding_rate,anchor_quality,confidence_ceiling,effective_confidence,graph_grounding_rate,method}、alias_merge{merge_groups,merged_labels,conflicts}、coverage{node_evidence_ratio,edge_evidence_ratio}、generated{nodes_declared,nodes_used,edges_declared,edges_used,baseline_edges_added,co_occurrence_edges_added,source}、queries{declared_queries,executed_queries,answered,no_result,undetermined,unsupported,minimum_three_queries_satisfied}、incremental{preserved_node_ids,new_node_ids,previous_graph_found}、fallbacks[]、deterministic、schema_version。

### 置信上限

按材料定位方式给证据置信上限：span 1.0 / paragraph 0.95 / page 0.7 / block 0.7 / text 0.4；effective_confidence = min(grounding_rate, ceiling)。JSON 段落材料最高就是 paragraph 级（无 bbox），不冒充 span。
## 七、异常处理

| 异常情况 | 检测方式 | 处理策略 | 状态标记 |
|---|---|---|---|
| 找不到 case.json | discover 返回 case_path=null | 照常建图，产物放 meta.additional_outputs，contract_detected=false | partial |
| 无任何材料 | 材料清单为空 | 写 failed 结果与原因，不编造图 | failed |
| JSON 材料没有段落层 | parse_json_material 返回空 | 跳过该文件并记 uncertainty（不把 JSON 索引当正文）；若是索引角色则不重复抽取 | partial（有其它材料时） |
| 材料包里 primary/secondary 之外的 material.json | 文件名/角色分类 | 当作汇总索引，只在没有 primary/secondary 时作为正文来源 | success |
| PDF 无文本层/扫描件 | total_chars≤20 | 不臆造文本，记 uncertainty，其它材料继续 | partial |
| 只有一篇材料 | docs=1 | 跨篇查询返回 no_result/undetermined 并说明，不伪造第二篇 | success/partial |
| 生成的实体无法定位 | label 在该论文段落中找不到 | 丢弃该节点，写 evidence.json.unverified | partial |
| 边的 quote 不同时含两端 | 端点包含检查失败 | 丢弃该边，写 unverified 并计入 fallbacks | partial |
| 边的 source/target 解析不到 | 节点索引未命中 | 拒绝记录，run_report.rejected_records 记账 | partial |
| 同名不同对象 | 归一化 surface 相同但类型冲突 | 保留两个节点 + conflict_register（homonym_type_conflict），不强行合并 | success（冲突是正常输出） |
| 同关系跨篇条件不同 | 同 endpoints+relation 的多篇边条件不一致 | 各边保持 doc-scoped，记 cross_doc_condition_conflict | success |
| 查询 op 不支持 | op 不在 8 种内 | 结果 status=unsupported_op，补足自动查询到 ≥3 | partial 仅当不足 3 个可执行 |
| 查询无结果 | 图内无匹配 | status=no_result 并说明"材料不支持"，不编答案 | success（诚实空结果） |
| 生成内容完全缺失 | work/generated 为空 | 降级确定性基线图（触发词边 + 共现候选），limits 写明 | partial |
| 超过 720 秒软截止 | Stopwatch.past_soft | 在阶段边界裁剪剩余工作并写 partial | partial |
| 输出目录只读 | mkdir/写探测失败 | 回退到当前目录下的 output/ | 视材料而定 |
| 任何未捕获异常 | 顶层 BaseException | 写 status=failed 的契约形状 result.json + traceback_tail | failed |
| 900 秒被杀 | —— | 启动占位文件保证有产物；prepare 结束即落基线结果 | partial |

## 八、限制与边界

1. **不做真实指代消解与外部实体链接**：只在材料内部做别名/缩写/同形对齐，不查询 Wikidata、不推断作者身份、不跨库消歧。
2. **类型是启发式**：确定性层的类型来自词表与后缀规则，可能把反应名判成 method、把指标判成 concept；Agent 层应修正，未修正时 type_source=heuristic 可被审计。
3. **跨篇只在概念层合并**：同一术语在多篇论文中合并为概念节点，但每篇论文的样品、条件、数值保留在各自的 doc-scoped 边里；不推断"相同物理样品"，相关查询返回 undetermined。
4. **JSON 材料没有 bbox**：anchor.bbox=null 是如实结果，不代表抽取失败；只有 PDF 材料才有真实坐标。
5. **page 只在真 PDF 时出现**：材料包给的是发布方段落 ID，本技能不把它换算成自建页码，也不把自建序号叫 PDF 页。
6. **规模上限**：最多 6 份材料、单篇 140 段、总计 420 段；候选实体上限 120（结论句另计 16）；基线节点上限 90、基线边上限 200（共现边另计 60）；Agent 生成的记录不做硬截断，但应与材料规模匹配，否则 result.json 会明显变大。
7. **关系只建立文本明确陈述的边**：共现仅作为降级模式的 co_occurs 边，且 condition 明写 "relation NOT asserted"。
8. **不做图像/补充材料理解**：不读取图注像素、不解析补充附件里的数值。
9. **确定性边界**：result.json / evidence.json / graph.json / csv / graphml / 查询结果逐字节确定；run_report.json 的 elapsed_s 是唯一随机器变化的字段。
10. **增量只在同一输出目录**：换 out 目录会重新编号；同 out 重跑才会保留既有 node_id。

## 九、自查与验收清单

| 检查点 | 命令/位置 | 通过标准 |
|---|---|---|
| 契约字段齐全 | result.json 顶层键 vs case.json.output_contract | 无缺字段；meta.contract_fields_not_reported 只列确实产不出的字段 |
| 契约映射可审计 | meta.contract_field_mapping | 每个字段都能看到内部来源（如 graph_nodes←nodes） |
| 证据落地率 | graph_audit_metrics.grounding.grounding_rate | 1.0 为满分；<1.0 看 evidence.json.unverified 找原因 |
| 定位语义 | source_evidence_anchors[].locator_kind / page | JSON 材料 page 必须为 null；PDF 材料 page 为真实页码 |
| 别名合并 | entity_alias_map[] | 每次合并都有 basis 与 evidence；XRD/全称类合并不产生重复节点 |
| 同名冲突 | conflict_register[] | 类型不同的同名对象各自成节点且被记录 |
| 查询 | query_outcome_bundle[] | ≥3 个可执行查询；无证据时 status=no_result/undetermined |
| 图文件 | graph.json / nodes.csv / edges.csv / graph.graphml | 行数与节点/边数一致；graphml 可被 XML 解析器解析 |
| 降级说明 | meta.limits、run_report.limits | 无 Agent 生成时必须出现 "no work/generated" 字样 |
| 重复运行一致 | 两次 --all 到不同目录 | graph.json / nodes.csv / edges.csv 逐字节相同 |

## 十、最小调用示例（示例，非正式案例结果）

**输入**（合成双论文材料包，仅用于演示接口）：

```text
fixture/
├── case.json                 # case_id=kg-synthetic-001，10 个契约字段（故意与内部命名不同）
├── corpus-manifest.json      # 两篇论文 DOI/作者/许可/来源文件
├── primary-material.json     # 论文 A：5 段，段落 ID A-p1..A-p5（Ni/SiO2 / CO2 methanation）
├── secondary-material.json   # 论文 B：5 段，段落 ID B-p1..B-p5（Co/TiO2 / CO2 hydrogenation）
├── material.json             # 汇总索引（不重复抽取）
└── task.md
```

**调用**：

```bash
cd /skills/weekly-skill
python scripts/run_kg.py --prepare --out fixture/out --work fixture/out/work
# （Agent 读取 work/ 后写入 work/generated/nodes.json、edges.json、queries.json）
python scripts/run_kg.py --finalize --out fixture/out --work fixture/out/work
```

**示意输出**（摘自实际运行，**示例，不是正式案例结果**）：

```json
{
  "case_id": "kg-synthetic-001",
  "graph_nodes": [
    {"node_id": "n-0017", "label": "X-ray diffraction", "type": "method",
     "aliases": ["XRD"], "doc_ids": ["doc-01", "doc-02"],
     "paragraph_ids": ["A-p3", "A-p4", "B-p3"],
     "evidence": [{"doc_id": "doc-01", "paragraph_id": "A-p3", "page": null,
                   "block_id": "A-p3", "bbox": null,
                   "quote": "Samples were characterized by X-ray diffraction (XRD)",
                   "locator_kind": "jats_paragraph_id", "section": "Methods"}]}
  ],
  "relationship_edges": [
    {"edge_id": "e-0022", "source": "n-0033", "target": "n-0034",
     "source_label": "Co/TiO2 catalyst", "target_label": "Ni/SiO2 catalyst",
     "relation": "outperforms", "direction": "directed", "condition": "under 10 bar",
     "strength": "moderate", "doc_id": "doc-02", "paragraph_id": "B-p4",
     "quote": "The Co/TiO2 catalyst shows higher CH4 selectivity than the Ni/SiO2 catalyst under 10 bar"}
  ],
  "entity_alias_map": [{"merge_id": "m-0002", "node_id": "n-0017",
                        "kept_label": "X-ray diffraction",
                        "merged_labels": ["XRD"], "basis": ["agent_alias", "same_surface"],
                        "confidence": 0.95, "doc_ids": ["doc-01", "doc-02"]}],
  "query_outcome_bundle": [{"query_id": "q2", "op": "cross_doc_shared", "status": "answered",
                            "answer": "1 concept(s) appear in both papers at concept level; per-paper samples, conditions and numbers stay doc-scoped"}],
  "graph_audit_metrics": {"nodes": 34, "edges": 23,
                          "grounding": {"grounding_rate": 1.0, "anchor_quality": "paragraph"}},
  "source_evidence_anchors": [{"evidence_id": "ev-0001", "kind": "node", "ref_id": "n-0017",
                               "doc_id": "doc-01", "paragraph_id": "A-p3", "page": null,
                               "quote": "Samples were characterized by X-ray diffraction (XRD)",
                               "locator_kind": "jats_paragraph_id", "grounded": true}],
  "meta": {"status": "success", "contract_field_mapping": {"graph_nodes": "nodes"},
           "contract_fields_not_reported": ["not_in_material_field"]}
}
```

> 上例数字来自本技能自测 fixture（2 篇合成论文、10 段落）；隐藏案例的论文数、段落数与字段名以实际 case.json 为准。本示例只说明接口与输出形状，不是任何正式案例的答案。

## 十一、架构参考与差异化

文档组织参考平台公开技能目录的 scientific-paper-translator（章节编排、异常处理表、状态机、核验报告、限制与自查）与 bohr-cli 的"踩坑"章节；本技能复用赛事共享内核 skills/_core 的 lib（contract / discover / extract / anchors / structure / hybrid），只借鉴结构组织，不复制他题内容。

| 维度 | 常见做法 | 本技能做法 |
|---|---|---|
| 实体与关系 | 让模型直接输出 JSON，无回源校验 | 确定性候选 + Agent 判断 + 代码断言回源；不可验证即丢弃 |
| 别名合并 | 字符串相似即合并 | 三类可解释依据 + 类型冲突永不合并 + 每次合并带证据 |
| 跨篇 | 把两篇内容合成一个结论 | 概念层合并、边与条件 doc-scoped、跨篇一致性查询返回 undetermined |
| 定位 | 自建页码冒充 PDF 页 | 发布方段落 ID 优先；page 仅在真 PDF 时出现 |
| 输出契约 | 固定 schema | 从 case.json 反射；缺值按声明类型占位 |
| 失败处理 | 依赖缺失就失败 | 运行时探测 + 逐级降级 + 基线图兜底 + 启动占位 |
| 重复运行 | 结果漂移 | 全流程确定性（run_report 除外） |

**文件清单**：SKILL.md、scripts/run_kg.py（CLI 与两阶段编排）、scripts/kg_core.py（材料解析/候选/证据校验）、scripts/kg_graph.py（合并/查询/导出）、resources/kg_schema.json（实体与关系受控词表 v1.0.0）、resources/query_templates.json（可复用查询模板 v1.0.0）、tests/test_kg.py（合成 fixture 端到端自测）。运行时另依赖随包展平的 lib/ 与 scripts/ 共享内核；仅标准库，无版本锁定问题。

## 十二、踩坑（--help 不会告诉你的）

1. **material.json 不是第三篇论文**：材料包里它常是段落汇总索引，若与 primary/secondary 一起抽取会把同样内容算两遍，导致重复实体和假跨篇证据。本技能按文件名与角色分类，索引只在没有 primary/secondary 时作为正文来源。
2. **根 DOI/title/authors 只属于第一篇**：把 case.json 根部的元数据套到第二篇是常见错误。本技能每篇论文只用自己的文件或 corpus-manifest 里与该 DOI/标题/文件名匹配的记录，缺失就留 null 并说明 metadata_source=not_reported。
3. **位置是发布方段落 ID，不是 PDF 页码**：材料包没有 PDF 页，anchor.page 必须为 null；把自建序号当"第 1 页"会在人工复核时暴露为编造定位。
4. **JSON 段落没有 bbox**：bbox=null 是如实结果；伪造 [0,0,0,0] 会让"可核验性"变成负分。
5. **共现不是关系**：同一段里出现两个实体不代表它们有语义关系。降级模式的 co_occurs 边必须写明 relation NOT asserted，且 strength 最低。
6. **同名不同对象不要合并**：类型冲突（同一写法既是方法又是数据集）必须保留两个节点并在 conflict_register 说明；强行合并会同时造成"错误合并"和"信息丢失"两项扣分。
7. **跨篇同关系条件不同不要合并**：两篇论文的"X 优于 Y"可能是在完全不同温度/压力/数据集下得到，条件不同的边保持 doc-scoped。
8. **quote 要逐字**：校验用 NFKC + 空白归一化，大小写可不同，但词序和拼写必须与段落一致；跨句引用请用段落原文连续片段。
9. **文件名不可信**：generated 目录里判断记录类型看字段（source/target/relation → 边；label/node_id/type → 节点），不要只看文件名。
10. **Windows 写 JSON 可能带 BOM**：读取一律 encoding=utf-8-sig（hybrid.read_json 已处理），否则会表现为"这个案例没声明契约"、字段全空。
11. **%TEMP% 可能不可写**：受限沙箱下 tempfile 会 PermissionError；本技能与自测都把中间文件写在输出目录/技能目录内。
12. **批工作表要小**：模型单响应 16384 token，批太大（>5000 字符）会导致一次答不完；本技能默认每批 6 段 / 4800 字符。
13. **重复运行不要把自己的产物当材料**：result.json / graph.json 等会被材料扫描器跳过（文件名黑名单 + 输出目录子树排除），否则第二遍会把上次图谱读成"论文"。
14. **软截止比硬预算先到**：720 秒时主动降级 partial 并落盘，比等到 900 秒被杀（占位文件）好；prepare 结束即写基线结果就是为此。

