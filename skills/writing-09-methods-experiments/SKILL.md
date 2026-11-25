---
name: writing-09-methods-experiments
description: Turn scattered experiment material — methods drafts, config files, logs, code constants, dataset notes — into a traceable Methods & Experiments chapter plus a parameter table, dataset card, baseline table, reproducibility checklist, limitation list and an explicit author-question list. Every stated number is cross-checked against the supplied files; values that cannot be traced are flagged instead of being invented. Use when a task asks to write, complete, audit or align a methods/experiments section against configuration and logs.
version: 1.0.0
author: 66Union
license: MIT
tags: [methods-section, experiment-setup, parameter-table, config-alignment, reproducibility, traceability, two-phase, output-contract]
---

# 方法与实验章节写作（Writing-09 Methods & Experiments）

## 一、技能定位

对应本题考察的四个问题：

| 考察问题 | 本技能的做法 | 可验证的证据 |
|---|---|---|
| **写得全** | 把材料切成可寻址段落，逐段要求 Agent 标注它供给哪个要素（步骤/设置/参数/数据集/对照/复现与限制），覆盖不足即降级并记录 | `work/batches/batch-XX.json` 段清单 + `self_audit.batch_coverage` |
| **写得准** | 从 config/log/code/README 抽参数（每条带**文件+行号+原文引文**），把草稿里每个数值绑定到最近参数名逐值比对；无来源标 `untraceable`、冲突标 `mismatch` | `number_provenance[]`、`author_followups[]`、`self_audit.draft_number_verdicts` |
| **做得着** | 复现清单固定七项（代码版本/运行环境/运行命令/随机种子/数据获取/硬件环境/重复与统计），逐项给 `verified/partial/missing` 与来源行 | `reproduction_manifest[]`、`self_audit.repro_verified/repro_missing` |
| **说得住** | 草稿与材料分歧、配置冲突、日志异常、缺失要素逐条转成**作者确认问题**（带严重度与证据行）；限制按“是否指向具体失效场景”打分，套话判 `generic` | `author_followups[]`、`scope_caveats[].quality`、`self_audit.limitations_specific` |

**与常见做法（填空式章节模板）的差异**：模板只能保证“有段落”，保证不了“数值有来源”。本技能把“正文数值 ↔ 材料”的对齐写成**可执行的确定性断言**：

1. 参数名归一到同一主键（`learning rate` / `lr` / `DEFAULT_LR` → `learning_rate`），取值按数值比较（`0.001` 与 `1e-3` 视为相等）；
2. 绑定只在**同一句**内做（避免把 “learning rate is 0.01 and the batch size is 96” 里的 0.01 绑到 batch_size）；
3. 草稿数值只与**非草稿材料**比对（用自己的行验证自己等于没验证）；
4. Agent 生成章节里的数值与**全部材料**比对，并**独立复核**“引文里是否真的出现该数值”（`value_in_quote`）；
5. 结论先回源校验再输出：`lib/anchors.verify_all` 逐条重读源文件，只承认 `exact/normalised` 命中；位置类结论（某段来自某行、某步引用某文件）标 `kind="structural"`。

---

## 二、适用条件

| 项目 | 说明 |
|---|---|
| 输入格式 | 方法草稿 `.md .txt .tex .rst .docx .pdf`；配置 `.yaml .yml .json .ini .toml .cfg .conf .env`；日志 `.log .out .err`；代码 `.py .sh .m .R .jl .c .cpp .h .java .js .ts .go`；说明 `README*/requirements.txt/environment.yml`；数据说明 `.csv/.tsv` |
| 语言 | 中英文材料均可（抽取与校验不依赖语言理解） |
| 篇幅 | 单文件 ≤400 KB（超出截断并记入 `warnings`），材料数 ≤80 |
| 依赖 | **仅 Python 标准库**；不联网、不调模型 API；`lib/` 由平台随 Skill 一起安装 |
| 运行环境 | 评测容器 `/input` + `/app/environment` + `/app/output`；本地可用 `--input/--out/--work` 重定向 |
| 不适用 | 扫描件（无文字层、无 OCR）；图片/图表中的数值；真正重写实验或生成新数据；纯排版（LaTeX 编译）任务 |

---

## 三、调用方式

### 触发条件

用户说“补写/整理/核对方法与实验章节”“把配置和日志整理成参数表”“检查正文参数与配置是否一致”“出复现清单/限制条件/待确认问题”时触发。

### 输入（路径不硬编码，运行时发现）

```
/input/case.json         必选：case_id + output_contract（输出契约唯一来源）
/input/task.md           必选：题目说明
/app/environment/**      材料（配置/日志/代码/草稿/数据说明），位置不限，自动扫描
```

`case.json.output_contract` 是唯一输出契约来源，字段名由案例决定，技能**不预设任何字段名**。

### 输出（默认写到 `/app/output`）

| 文件 | 内容 |
|---|---|
| `result.json` | 契约字段（按 `output_contract` 声明顺序）+ `case_id` + `meta`。未产出字段按声明类型占位（array→`[]`、object→`{}`、string→`""`）并列入 `meta.contract_fields_not_reported` |
| `evidence.json` | 每条结论的锚点与回源校验、材料清单、参数冲突、非法数值、六要素覆盖、批次覆盖 |
| `run_report.json` | 状态、耗时、预算、限制、不确定项、实际读取的输入路径、渲染探测结果 |
| `methods_section.docx/.md/.html` | 可编辑成稿（同一内容三种格式；html 即预览） |
| `parameter_table.csv` | 参数表（可直接粘贴进补充材料或表格工具） |
| `author_questions.md` | 待作者确认清单（按严重度排序，附证据行） |
| `methods_section.pdf/.png` | 仅当镜像内存在 office/pdf 渲染器时产出（探测失败如实写进 `run_report.artifacts.rendering`） |

**启动即写占位 `result.json`（status=partial）**，解析出契约后立刻再写一份“契约形状骨架”（所有声明字段都在、值为声明类型占位），然后才开始慢工作。容器在 900 秒被杀时产物仍在，不会出现零字节目录。

### 三种运行模式（默认 `--all`）

```bash
# 0) 保底：一次跑完 prepare + finalize（没有生成内容也一定产出成稿与 result.json）
python scripts/run_methods.py --all

# 1) 阶段一：确定性抽取，写工作表（不写最终 result.json 内容）
python scripts/run_methods.py --prepare --out /app/output --work /app/work/writing-09-methods-experiments

# 2) （Agent 写 work/generated/methods.json 与 work/generated/batch-XX.json）

# 3) 阶段二：校验 + 反射契约 + 出成稿
python scripts/run_methods.py --finalize --out /app/output --work /app/work/writing-09-methods-experiments
```

可选参数：`--out DIR` 输出目录；`--work DIR` 工作目录（默认 `/app/work/writing-09-methods-experiments`，不可写时回退到 `<out>/work`）；`--input DIR` 追加一个优先扫描的输入根。

---

## 四、核心工作流

### 阶段 0：输入发现与占位
1. 依次探测 `/input`、`/app/input`、`/app/work`、`/app/environment`、`/app`，最后退到当前目录（共享内核 `discover`）。
2. 按特征识别 `case.json`（含 `case_id`/`output_contract` 的 JSON，容忍 BOM）与 `task.md`。
3. 材料扫描用**本适配层自己的后缀表**（共享 `discover` 不认 `.json/.log/.ini/.toml/.py`，而它们正是本题主要证据源）；跳过 `output/work/batches/generated` 等目录与已解析出的 `--out/--work` 路径，避免把自己的产物当材料读回去（自证循环）。
4. 立即写占位 `result.json`；解析出契约后再写契约形状骨架。

### 阶段 1：prepare（确定性，无模型调用）
1. **分类材料**：config / log / code / readme / data / draft_candidate / document。
2. **选草稿**：文件名线索（methods/draft/section/chapter/manuscript/稿/正文）> 正文线索 > `environment/` 根加分 > 输出目录减分 > 体量/正文比例；选择结果写进 run_report。
3. **抽事实**（全部带 `source_path` + `line` + `quote`）：配置行级解析（YAML/INI/TOML/.env）、JSON 递归扁平化后按 key 定位行号；日志键值+指标+版本 pin+硬件+CLI+异常；代码常量/argparse 默认值/manual_seed；README 安装与运行命令。
4. **参数归一与冲突**：`lr/learning-rate/DEFAULT_LR` → `learning_rate`（支持 `num_`/`max_` 前缀与后缀别名）；同名不同值 → `param_conflicts`。
5. **草稿数值对齐**：每个数值判上下文（`value/label/model_suffix/small_integer`）→ 同一句内绑定参数名 → 与材料比对，得到 `match / mismatch / unit_mismatch / traced_number / untraceable`。
6. **候选识别**：数据集/划分/基线行（带行号与引文）；六要素覆盖评估；**生成缺口与追问清单**。
7. **写工作表**：`params_candidates.json`、`draft_segments.json`、`gaps.json`、`batches/batch-XX.json`（每批 ≤6 段）、`generated/_template_methods.json`（填空模板）。

### 阶段 2：Agent 生成（judgement）
按 §六 协议写 `work/generated/methods.json` 与每份 batch 答案。**不写脚本逻辑**，只做判断：把段落组织成步骤、把参数归类、把限制写成具体失效场景。

### 阶段 3：finalize（确定性）
1. 读生成内容；**独立重跑 analyze()**（不信任 prepare 的 worksheet，防止过期/被改写的中间产物把无来源数值洗成有来源）。
2. 校验覆盖：段落级覆盖率、要素级齐备度；缺失即降级为确定性基线并记 `limits`。
3. 校验参数：Agent 声明的 `source` 必须真能解析且**该行真的含该值**（`source_verified`）；解析失败回退到材料重定位（`note` 说明）；都失败 → `untraceable`，保留行但打标。
4. 校验生成章节：数值与全部材料比对；`mismatch`（与配置冲突）与 `untraceable`（无来源）分别计数并入追问清单。
5. 回源校验锚点：每条可定位结论生成 `page + block_id + bbox + quote` 锚点，按文件分组调用 `anchors.verify_all`，输出 `grounding_rate` 与 `effective_confidence`（text 材料置信上限 0.7，PDF span 级 1.0）；**无来源结论不进分母**，作为 gap 单独统计，避免稀释指标。
6. 反射契约：`hybrid.map_fields` 用别名/词干把内部产物映射到声明字段；未命中按声明类型占位（见 §五）。
7. 落盘：`result.json`、`evidence.json`、`run_report.json` 与可编辑成稿（docx/md/html/csv/questions.md）；有渲染器时再出 PDF/PNG 预览。

---

## 五、字段字典

### 5.1 `result.json` 的契约反射

内部产物（固定 12 个键）→ 声明字段，按“精确 → 别名 → 词干/包含”匹配；同一内部值只喂一个声明字段，未声明的产物放进 `meta.additional_outputs`。

| 内部键 | 含义（内容形状） | 常见声明字段名（同义词表内置） |
|---|---|---|
| `methods_section` | 章节正文（Markdown 字符串） | `chapter_body / chapter_text / methods / methods_text / section` |
| `methods_steps` | `[{step_id,order,input,operation,output,condition,source,trace_status,fields_complete}]` | `process_steps / steps / procedure / workflow` |
| `parameter_table` | `[{name,value,unit,target,source_path,line,quote,trace_status,agent_source,source_verified,note}]` | `hyperparameter_table / parameters / params / param_table` |
| `datasets` | `[{name,version,size,preprocessing,split,split_method,subsets,source_path,line,trace_status}]` | `data_card / datasets / dataset_table` |
| `experimental_settings` | `{hardware[],software[],seeds[],repeats[],metrics[],commands[],agent_reported?,trace_status}` | `run_environment / settings / environment / experiment_setup` |
| `baselines` | `[{name,version,budget,metric,result,source_path,line,trace_status}]` | `comparison_baselines / baselines / controls` |
| `reproducibility_checklist` | 七项 `[{item,status(verified/partial/missing),detail,sources[]}]` | `reproduction_manifest / reproducibility / repro_manifest` |
| `traceability` | `[{claim_id,kind(content/structural),claim,verdict,severity,source_path,page,block_id,line,quote,value,anchored,anchor_verified,value_in_quote,declared_values[]}]` | `number_provenance / source_trace / fact_source_map / provenance` |
| `limitations` | `[{limitation,failure_scenario,scope,needs_author_confirmation,quality(specific/vague/generic),trace_status}]` | `scope_caveats / limitations / caveats` |
| `quality` | 见 5.3 自检指标 | `self_audit / quality_report / metrics` |
| `questions` | `[{question_id,question,why,severity(blocking/high/medium/advisory),evidence[],origin}]` | `author_followups / author_questions / open_questions` |
| `change_log` | 本轮做了什么（字符串数组） | `changes / revision_log` |

> 隐藏案例的字段名不可知，所以**没有任何硬编码 schema**；自测里故意用 `chapter_body / hyperparameter_table / data_card / number_provenance / self_audit` 这类与内部命名不同的字段，验证反射确实生效。

### 5.2 `work/generated/methods.json`（Agent 唯一主产物）

```json
{
  "steps": [{"step_id":"S1","input":"","operation":"","output":"","condition":"","source":"environment/configs/train.yaml:12"}],
  "parameter_table": [{"name":"learning_rate","value":"0.001","unit":"-","target":"AdamW optimizer","source":"environment/configs/train.yaml:4"}],
  "datasets": [{"name":"","version":"","size":"","preprocessing":"","split":"","split_method":"","subsets":[],"source":"environment/data/README_data.txt:1"}],
  "settings": {"hardware":"","software":"","seeds":"","repeats":"","metrics":""},
  "baselines": [{"name":"","version":"","budget":"","metric":"","result":"","source":""}],
  "reproducibility": {"code_version":"","environment":"","commands":[],"seeds":[]},
  "limitations": [{"limitation":"","failure_scenario":"","scope":"","needs_author_confirmation":true}],
  "methods_section": "## Methods\n...",
  "questions": [{"question":"","why":"","severity":"advisory"}]
}
```

字段缺失不会报错：缺 `parameter_table` 就用材料事实表、缺 `methods_section` 就用确定性基线正文，并在 `limits` 写明降级原因。`source` 一律写 `"相对路径:行号"`（如 `environment/configs/train.yaml:4`）——写成自然语言会被判 `source_verified=false`。

### 5.3 `quality`（`self_audit`）自检指标

`status_generated_content`、`delivered_six_element_coverage`、`delivered_coverage_ratio`、`material_element_coverage`、`material_coverage_ratio`、`step_count`、`steps_with_source`、`steps_with_complete_fields`、`parameter_count`、`parameter_trace_counts`、`draft_number_mentions`、`draft_number_verdicts`、`draft_number_untraceable_high`、`generated_section_numbers`、`generated_section_untraceable`、`generated_section_mismatched`、`dataset_count`、`baseline_count`、`repro_items`、`repro_verified`、`repro_missing`、`limitation_count`、`limitations_specific`、`limitations_generic`、`batch_coverage`、`grounding_rate`、`effective_confidence`、`anchor_quality`、`materials_scanned`。

### 5.4 判词表（verdict / trace_status）

| 判词 | 含义 | 处理 |
|---|---|---|
| `match` / `traced` | 绑定到参数且材料同值 / 行级来源可定位 | 写入正文与参数表，锚点回源校验 |
| `mismatch` | 草稿或生成章节的值与材料取值冲突 | 保留但高亮 + 高严重度追问，**状态降为 partial** |
| `unit_mismatch` | 数值相同、单位不同 | 追问确认单位 |
| `traced_number` | 数值在材料中出现但未绑定到参数名 | 可写入，标 `traced_number` |
| `untraceable` | 材料中找不到该数值/该参数无来源 | **绝不补造**；列入 `author_followups`；`severity=high` 时降级 partial |
| `unverifiable` | 材料中只有草稿本身，没有独立材料可交叉核对 | **不当作造假**；记入 `draft_number_unverifiable`，并在 `limits` 说明“无法核对” |
| `source_located` / `source_missing` | 步骤/数据集/基线声明的来源可/不可解析 | 缺失进限制与追问 |
| `candidate` / `partial` | 只有候选行/信息不全（无版本、无预算等） | 写进表并注明缺什么 |

---

## 六、Agent 操作协议（精确到命令与 JSON）

### 6.0 必须按顺序做（否则拿不到成稿分）

1. `python scripts/run_methods.py --all`（**先拿到保底的 result.json + 成稿**，即使后面超预算也已有产出）；
2. 读工作表：`<work>/params_candidates.json`、`<work>/draft_segments.json`、`<work>/gaps.json`、`<work>/batches/*.json`、`<work>/generated/_template_methods.json`；
3. 写 `<work>/generated/methods.json`（§5.2）与每份 `<work>/generated/batch-XX.json`（§6.2）；
4. `python scripts/run_methods.py --finalize`（**必须再跑一次**，否则 result.json 里仍是确定性基线）；
5. 检查 `run_report.json` 的 `status/limits`、`author_questions.md`，把不能核实的项在回复里讲清楚，**不要自行编数值**。

### 6.1 参数表怎么填
- 一行一个参数：`name` 用材料里的原名或主键名均可（`lr`→`learning_rate` 会被自动归一）；
- `value` 必须是材料里出现过的值；`unit` 材料没写就留空，**不要猜**；
- `source` 写 `"environment/configs/train.yaml:4"` 这种可解析定位；写 `"配置文件"` 之类会被判 `source_verified=false`；
- 材料里没有的参数（例如 `warmup_steps`）**不要为了凑表补**：要么不写，要么写进 `questions` 让作者确认。

### 6.2 batch 答案格式（段落覆盖，用于“写得全”）
对 `<work>/batches/batch-XX.json` 里的每个 `seg_id` 回一条：

```json
[
  {"seg_id": "seg-0003", "roles": ["dataset"], "note": "官方划分说明"},
  {"seg_id": "seg-0006", "roles": ["parameters", "settings"], "note": "训练超参"}
]
```

`roles` 取值：`step / setting / parameters / dataset / baseline / repro / limitation / none`。全部段落都要覆盖；遗漏会让 `batch_coverage` 下降并触发 `partial`。

### 6.3 成稿正文怎么写
- 用 Markdown：`## 3 Methods and Experiments`、`### 3.1 Data` …，会被转成 docx 标题/段落/表格；
- **每个数值都必须能在材料里找到**（校验只比对数值，不比对措辞）；
- 材料没有的数字一律不写；确实需要作者补的，写进 `questions`；
- 限制条件必须写 `failure_scenario`（什么条件下失效），否则 `quality` 判 `vague/generic`。

---

## 七、异常处理（四列表）

| 异常情况 | 检测方式 | 处理策略 | 状态标记 |
|---|---|---|---|
| 找不到 `case.json` | 输入发现为空 | 仍完整跑流程；产物全进 `meta.additional_outputs`，`contract_detected=false` | partial + limit |
| 找不到任何材料 | 材料列表为空 | 写 `status=failed` 的完整结果（全字段按声明类型占位）并说明原因 | failed |
| 没有方法草稿 | 草稿选择为空 | 仅从配置/日志/代码生成参数表与复现清单；限制逐条要求作者补草稿 | partial + 高严重度追问 |
| Agent 未写生成内容 | `work/generated` 为空 | 用确定性基线（原文回填式正文 + 材料事实参数表 + 日志异常限制）并写明降级 | partial |
| 生成内容缺字段 | 归一化后为空 | 逐字段降级（缺参数表→材料事实表；缺正文→基线正文），不抛错 | partial |
| 参数在材料间冲突 | 同主键不同值 | 保留全部取值与来源；正文标冲突；生成高严重度追问 | partial |
| 草稿数值无来源 | 数值不在非草稿材料中 | 标 `untraceable`（高严重度计入 partial），转成追问 | partial |
| 只有草稿、无独立核对材料 | 无 config/log/code/README | 数值标 `unverifiable`（不判造假），在 limits 写明“无法核对” | success/partial（带 limit） |
| 生成章节数值无来源 | 数值不在任何材料中 | 标 `untraceable`，写入 `generated_section_untraceable` 与追问 | partial |
| 生成章节数值与配置冲突 | 绑定参数且不同值 | 标 `mismatch`，写入 `generated_section_mismatched` 与追问 | partial |
| Agent 声明的 `source` 解析不到 | 路径/行号不匹配 | `source_verified=false`，回退到材料重定位；仍失败则 `untraceable` | partial |
| 段落未全覆盖 | `merge_generated` 缺失清单 | 记录 `coverage`/`missing`，`batch_coverage<1` 写进 limits | partial |
| 无渲染器（office/pdf） | `lib/render.probe()` 为空 | 保留 docx/md/html + 写 `rendering.reason`，不伪造 PDF/PNG | success（带 limit） |
| PDF/扫描件无文字层 | `text_layer=false` | 记 `warnings`，不臆造文本；该材料不参与数值比对 | partial |
| 文件不可读/非 UTF-8 | 读取异常 | 记 `warnings` 并跳过该文件，其余照常 | partial |
| 任何未捕获异常 | 顶层 `BaseException` | 写 `status=failed` 的 result.json + `traceback_tail`，绝不零字节退出 | failed |
| 超时（>720 s 软上限） | `Stopwatch.past_soft()` | 停止继续扫描材料，用已完成内容出结果 | partial |

---

## 八、使用与方法说明

### 8.1 完整调用步骤

```bash
# 1) 环境自检（不需要第三方包）
python -c "import sys, json; print(sys.version)"

# 2) 保底全流程（一定产出 result.json 与成稿）
python scripts/run_methods.py --all

# 3) （Agent 生成阶段：读工作表，写 work/generated/methods.json + batch-XX.json）

# 4) 校验 + 出成稿（必须再跑一次）
python scripts/run_methods.py --finalize

# 5) 查看产物
cat /app/output/run_report.json      # status / limits / 材料清单 / 渲染探测
cat /app/output/author_questions.md  # 待作者确认清单
python tests/test_methods.py         # 自测（合成 fixture，端到端，约 30 秒）
```

### 8.2 关键方法
- **两阶段分离**：抽取与校验都是确定性代码，模型只做“判断”这一步（64 次模型请求/900 秒预算内可完成，且重复三次结果一致）。
- **独立复核而非自证**：finalize 重新跑一遍抽取；草稿数值只用非草稿材料验证；引文必须真的含该数值（`value_in_quote`）。
- **契约反射**：字段名由 `case.json` 决定，未命中的字段按声明类型占位并记录；自测用“故意不同的字段名”证明生效。
- **降级优先于崩溃**：任何生成内容缺失都退化成确定性基线并写 `limits`；“产出但不完美”远好于“报错无产物”。
- **可复现性**：无网络、无随机、文件遍历与排序固定；同一输入重复运行，`result.json` **字节一致**（耗时只写进 `run_report.json`）。

### 8.3 限制与边界
1. **不做语义级改写**：机器校验的是“来源是否成立、取值是否一致、要素是否齐全”，不判断行文是否优美。
2. **图表里的数值读不到**：只处理文本材料；图中数字需要作者补录。
3. **配置解析是行级启发式**：不支持 YAML 锚点、多行字符串、JSON 注释；嵌套同名键按完整路径分别记录。
4. **日志抽取依赖键值形态**（`key=value` / `key: value`）：自由文本日志只能抓到少量事实。
5. **草稿选择是启发式**：材料里同时有多份“方法”文档时按名称/根目录/体量排序；选错时脚本把选择结果写进 run_report，人工指认后重跑。
6. **PDF 走共享内核提取链**：无 PyMuPDF/pdfplumber 时用内置解析器，坐标可能退化为页级（`anchor_quality=page`，置信上限 0.7）。
7. **docx 是最小 OOXML**（标题/段落/列表/表格），不含样式模板、批注、修订；正式排版请用 md 源文件。
8. **渲染预览依赖镜像工具**：没有 soffice/pdftoppm 时只产出 html 预览，并如实记录原因，不伪造 PDF/PNG。
9. **不校验外部事实**：材料之外的真实世界信息不在能力范围；技能只保证“不新增材料外事实”。
10. **同名参数跨材料冲突不会自动裁决**：技能保留全部取值并要求作者确认，这是有意设计（自动裁决才是不可复现的根源）。

### 8.4 失败处理与自查

| 现象 | 自查方法 | 处理 |
|---|---|---|
| `status=partial` 且 limits 提到 baseline | 看 `self_audit.status_generated_content` | 多半是 Agent 没写生成内容；补写后重跑 `--finalize` |
| 参数表大量 `untraceable` | 看 `parameter_table[].agent_source` 与 `source_verified` | 多为 `source` 写成自然语言；改成 `path:line` 重跑 |
| `mismatch` 很多 | 看 `number_provenance[]` 的 `declared_values` | 按 `author_questions.md` 逐条确认，**不要直接改数** |
| `grounding_rate` 偏低 | 看 `evidence.json` 的 `grounding.per_claim[].located/supported_by_quote` | `located=false`=锚点位置失效；`supported_by_quote=false`=引文没覆盖结论 |
| 草稿选错 | 看 `evidence.json` 的 `draft.path` 与 `run_report.inputs` | 材料里有多份方法文档时，人工指认后重跑 |
| 六要素缺项 | 看 `self_audit.delivered_six_element_coverage` | 按缺失项补材料或补生成内容 |
| 没有 PDF/PNG 预览 | 看 `run_report.artifacts.rendering.reason` | 镜像无渲染器；用 docx/html 预览即可 |
| 契约字段为空 | 看 `meta.contract_detected` / `meta.contract_field_mapping` | `false` 说明没找到 case.json；检查输入路径 |

### 8.5 结果如何被复核（给评审或作者的三步核验）

1. **先看状态与限制**：`run_report.json` 的 `status`（success / partial / failed）与 `limits` 直接说明本次运行是“完整产出”还是“降级产出”，以及降级在哪一环（生成内容缺失、批次未覆盖、数值无来源、材料不可读）。`status=partial` 不等于失败，它表示“已有可用成稿，但有需要作者确认的项”。
2. **再核对每个数值的来源**：打开 `evidence.json` 的 `traceability` 数组，逐条看 `verdict`、`source_path`、`line`、`quote` 与 `anchor_verified`。任何 `mismatch` 或 `untraceable` 都同时出现在 `author_questions.md` 里，评审可以直接判定“技能没有替作者补数”。
3. **最后看六要素与复现清单**：`self_audit.delivered_six_element_coverage` 给出步骤/设置/参数/数据集/对照/复现与限制是否齐备；`reproduction_manifest` 逐项给 `verified/partial/missing`。缺项既可能是材料缺，也可能是生成内容没写，二者在 `limits` 里会分开说明。

三步都不需要重新跑模型：所有结论都是脚本写入的确定性断言，作者可以只改材料或只改 `work/generated/methods.json`，然后重跑 `--finalize` 得到新结果，便于反复迭代而不引入随机波动。

---

---

## 九、最小调用示例（示意，非正式案例结果）

**输入**（`tests/test_methods.py` 生成的合成 fixture）：

```
fixture/
├── case.json                      # case_id=fixture-methods-01，13 个契约字段（含 1 个技能永不产出的字段）
├── task.md
└── environment/
    ├── draft/methods_draft.md     # 草稿：lr 写 0.01、batch 写 96（与材料冲突）
    ├── configs/train.yaml         # lr 0.001、batch 64、epochs 120、seed 42、repeats 3
    ├── logs/run.log               # epoch=120 val_acc=0.812、RTX 3090、WARNING 行
    ├── code/train.py              # NUM_EPOCHS=120、DEFAULT_LR=0.001、manual_seed(42)
    ├── README.md                  # pip install torch==2.1.0、python train.py …
    └── data/README_data.txt       # CIFAR-100 v1.0、45000/5000/10000
```

**调用**：

```bash
cd fixture
python ../../scripts/run_methods.py --all
# （Agent 写 work/generated/methods.json + batch-XX.json）
python ../../scripts/run_methods.py --finalize
```

**示意输出**（`out/result.json` 摘要，**示例**，数字取自实际运行）：

```json
{
  "case_id": "fixture-methods-01",
  "chapter_body": "## 3 Methods and Experiments\n\n### 3.2 Training setup\n我们训练 ResNet-18 120 epochs …",
  "hyperparameter_table": [
    {"name": "learning_rate", "value": "0.001", "unit": "-", "target": "AdamW optimizer",
     "source_path": "environment/configs/train.yaml", "line": 4, "trace_status": "traced"},
    {"name": "warmup_steps", "value": "500", "unit": "steps", "trace_status": "untraceable"}
  ],
  "number_provenance": [
    {"claim_id": "trace-num-0003", "kind": "content", "verdict": "mismatch",
     "claim": "draft states 0.01 in: The learning rate is 0.01 and the batch size is 96.",
     "source_path": "environment/draft/methods_draft.md", "line": 10,
     "anchored": true, "anchor_verified": true, "value_in_quote": true}
  ],
  "self_audit": {"delivered_coverage_ratio": 1.0, "batch_coverage": 1.0,
                 "draft_number_untraceable_high": 0, "generated_section_untraceable": 0,
                 "parameter_trace_counts": {"traced": 5, "untraceable": 1},
                 "grounding_rate": 0.9688},
  "meta": {"status": "partial",
           "contract_field_mapping": {"chapter_body": "methods_section",
                                      "number_provenance": "traceability"},
           "contract_fields_not_reported": ["zzz_unmodelled_quota"],
           "limits": ["the draft disagrees with the materials on at least one value; …"]}
}
```

> 上述 `partial` 是正确的：草稿写 `learning rate 0.01` 而配置是 `0.001`，技能不会替作者抹平这个分歧，而是把它转成高严重度追问。

---

## 十、架构参考与差异说明

本技能复用团队共享内核 `skills/_core/lib`（`discover/contract/anchors/extract/docwrite/render/hybrid`），组织方式参考平台公开技能 `scientific-paper-translator` 的章节编排（技能定位/适用条件/调用方式/工作流/异常表/自查/最小示例）与 `bohr-cli` 的“踩坑”章节；未复制其内容或代码。

| 维度 | 常见做法（含公开范例） | 本技能做法 |
|---|---|---|
| 章节生成 | 模板填空，数值由模型自由写 | 先抽材料事实，再让 Agent 只做归类与行文；数值逐一回源 |
| 校验 | 模型自述 pass/warning/fail | 代码断言：回源锚点 + 数值集合比对 + 名称-取值绑定 |
| 输出契约 | 固定 JSON schema | 从 `case.json.output_contract` 反射；缺值按声明类型占位 |
| 缺失内容 | 报错或静默跳过 | 确定性基线降级 + `limits` 如实记录 |
| 参数冲突 | 取“最合理”的一个 | 全部保留、标记 `mismatch`、生成作者追问 |
| 可复现性 | 一句“见仓库” | 七项清单逐项 `verified/partial/missing` + 来源行 |
| 重复运行 | 结果随模型波动 | 抽取/校验全确定性，`result.json` 字节一致 |

---

## 十一、踩坑（`--help` 不会告诉你的）

1. **不要把自己的产物当材料**：`/app/output`、`<work>/batches`、`<work>/generated` 必须在扫描时排除，否则第二次运行会把上一轮的 `methods_section.md` 当成草稿——而且它看起来“更完整”，会赢过真正的草稿（本技能用路径排除 + 名称排除 + `environment/` 加分三重防护）。
2. **数值绑定要在句内做**：一行里常有多个参数（“learning rate is 0.01 and the batch size is 96”），整行取最近会绑错；本技能先切句，再按**区间距离**（而非起始点距离）绑定。
3. **`0.01` 与 `0.001` 必须用数值比较**：字符串比较会把 `1e-3` 与 `0.001` 判成不同；本技能统一 `float` + 相对容差，且先整体识别科学计数法（否则 `5e-4` 会被切成 `5` 与 `-4` 两个数）。
4. **“小整数/标签数字”不是参数值**：`top-1`、`ResNet-18`、`CIFAR-100` 里的数字若按普通数值处理，会制造大量假的 `untraceable`；本技能按上下文分级（`label/model_suffix/small_integer/value`），只有 `high` 级别才阻断 `success`。
5. **日志里的 `epoch=120 val_acc=0.812`**：正则若允许“数字+空白+字母”，会把 `120 val` 当成带单位的值，进而制造假的参数冲突；单位必须紧邻数字。
6. **同名参数跨来源要按“主键+数值”去重**：配置 `epochs: 120`、日志 `epoch=120`、代码 `NUM_EPOCHS = 120` 是同一个事实，不是三个冲突。
7. **无来源的结论不要计入 grounding 分母**：否则 `grounding_rate` 会把“已如实标记的缺口”当成“锚点失效”，指标反而失去信息量。
8. **Windows 下用 `encoding="utf-8-sig"` 读 JSON**：PowerShell 写出的 UTF-8 带 BOM，`json.loads` 直接失败，表现成“这个案例没声明契约”。
9. **`%TEMP%` 在沙箱里可能不可写**：中间产物一律写在工作目录内；本技能所有测试临时目录都建在技能目录下并在 `finally` 清理。
10. **不要 `pip install` 任何东西**：评测镜像只承诺通用科学栈，本技能刻意只用标准库；渲染与 PDF 解析都是“探测→能就用→不能就降级并记账”。
