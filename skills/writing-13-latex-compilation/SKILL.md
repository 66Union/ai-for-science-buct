---
name: writing-13-latex-compilation
description: Audit, repair and actually compile a LaTeX manuscript project - discover the master .tex, resolve \input/\include recursively, statically detect undefined refs, missing bib entries, duplicate labels, unclosed environments, package conflicts, encoding and template problems; let the agent write minimal literal fixes; apply them strictly (unmatched edits are rejected), re-audit, compile with the TeX engine that exists in the image (latexmk -> xelatex -> pdflatex -> lualatex -> tectonic, multi-round with bibtex/biber), parse the .log into file:line:root-cause records, and emit an editable source tree, PDF/PNG preview when possible, evidence.json, run_report.json and a result.json that mirrors the case's declared output_contract. Use when a task asks to fix LaTeX build errors, check or repair citations/cross-references, adapt a manuscript to a template, or deliver compilable .tex sources.
version: 1.0.0
author: 66Union
license: MIT
tags: [latex, compilation, build-reproducibility, static-analysis, error-localization, bibliography, cross-reference, output-contract, two-phase]
---

# LaTeX 写作与编译（Writing-13 LaTeX Compilation）

## 一、技能定位

本技能针对赛题「写作13｜LaTeX 写作与编译」的四个考察问题，每一条都落到**可执行、可复核**的动作上，而不是给 Agent 一份"改 LaTeX 的注意事项"。

| 考察问题 | 本技能的做法 | 可验证的证据 |
|---|---|---|
| **编译得通**（指定模板与发行版下能按预期轮次通过，报错定位到文件:行号:根因） | prepare 先静态体检并探测真实存在的 TeX 引擎；finalize 用非交互模式**真实编译**（latexmk 优先，否则按 xelatex/pdflatex/lualatex 多轮 + bibtex/biber），把 .log 解析成 errors[].location = 文件:行号 与 root_cause，而不是停在第一条 ! 上 | compile_report.errors[]（file/line/location/error_type/message/root_cause）、compile_report.rerun_needed、compile_report.rounds、compile_report.pdf |
| **改得对**（公式/表格/交叉引用/文献/版式逐项对齐模板与原文意图） | 静态审计 14 类检查：花括号/环境栈/文档结构/子文件解析/label 唯一/ref 有定义/cite 与 .bib 对账/宏包冲突与加载顺序/模板必填项/表格列结构/占位标记/数学定界符/编码与 BOM/引擎提示；每条缺陷带 root_cause 与 typical_fix，可修复项直接给出**逐字替换建议**（含近似 label 的 difflib 匹配） | static_checks.issues_before[]、static_checks.issues_after[]、static_report.md、fixes_applied.md |
| **交得可用**（交付可继续编辑维护的源码，而不是 PDF/截图/片段） | finalize 把修复后的**完整工程树**写到 out/source/（主文件 + 全部 include 子文件 + .bib + .cls/.sty + 图片，统一去 BOM、LF 换行），并交付 build_notes.md 与 reproducibility.json；源码不是"补丁片段" | fixed_sources[]（path/bytes/sha256/changed）、source_dir、out/source/** |
| **复现得了**（模板版本、宏包、字体、编译命令与轮次可复现） | 记录实际执行的完整命令行、引擎与版本、轮次、bibtex 轮次、文档类、宏包清单、模板文件、源码 sha256、环境（OS/Python/引擎探测结果）；无引擎时明确写 compile_unavailable 而不是假装成功 | reproducibility（tex_engine/build_command/rounds/packages/source_checksums/environment）、build_notes.md、latex_build.log |

**与"让模型直接改 LaTeX"的差别**：模型只负责**判断**（哪一处该改、改成什么、为什么），**结构解析、定位、替换、复检、编译、日志归因、契约反射全部由代码完成**。代码做五件事：① 把工程解析成"主文件 + 递归 include 图 + 宏包/标签/引用/文献清单"；② 静态审计并给每条缺陷一个稳定签名；③ 严格字面替换（原文找不到就拒绝，不硬改）；④ 修复后复检 + 真实编译 + 日志解析；⑤ 按 case.json.output_contract 反射生成 result.json。任何一条核验失败都会打标记并计入 limits/degraded，不会静默升级成"已完成"。

## 二、适用条件

| 项目 | 说明 |
|---|---|
| 输入 | case.json（含 case_id + output_contract，**必需**）、task.md、随题材料（TeX 工程：.tex/.ltx、.bib、.cls/.sty/.bst、图片；可含 README/说明 md） |
| 工程形态 | 单文件、主文件 + include 子文件、多级目录、带模板类；主文件按"含 documentclass > 文件名 main/paper/thesis/manuscript > 体积 > 路径浅"确定性选择 |
| 文献格式 | BibTeX（.bib）或 thebibliography + bibitem；biblatex（addbibresource，有 biber 时调用） |
| 语言 | 英文 / 中文 / 混排均可；CJK 文本会触发 xelatex 引擎建议并检查 ctex/xeCJK |
| 依赖 | **仅 Python 标准库**；TeX 引擎运行时探测（latexmk / xelatex / pdflatex / lualatex / tectonic / latex-compiler），PDF 转 PNG 复用共享内核的渲染链，缺失时如实降级 |
| 篇幅 | 工程 ≤ 400 个文件（超限截断并记 limits）；单个文本 ≤ 4 MB；编译每轮 ≤ 150 s，总预算受 900 s 约束 |
| 不适用 | 需要 shell-escape 的稿子（出于安全不开启）；需要联网下载宏包；纯扫描件 PDF 的 OCR；需要解读图表科学含义；把 PDF/截图当作交付物 |

## 三、调用方式

### 触发方式

当任务出现"修 LaTeX 编译错误 / 让这个工程编译通过 / 检查交叉引用与参考文献 / 迁入模板 / 交付可编译 .tex / 复现构建"等指令，且输入里有 TeX 工程时触发。技能不假设案例内容，主文件、模板、引擎全部从实际文件与运行环境探测。

### 命令行（三种模式，默认 --all）

```bash
# 1) 确定性准备：发现工程 -> 递归解析 -> 静态体检 -> 探测引擎 -> 写工作表
python scripts/run_latex.py --prepare  --out /app/output --work /app/work

# 2) Agent 判断：写 work/generated/fixes.json（可选 notes.json）

# 3) 确定性收尾：严格应用修复 -> 复检 -> 真实编译 -> 交付源码/报告 -> 反射契约
python scripts/run_latex.py --finalize --out /app/output --work /app/work

# 一次跑完两段（没有 generated 内容时降级为静态路径，不崩）
python scripts/run_latex.py --all      --out /app/output --work /app/work
```

| 参数 | 默认 | 说明 |
|---|---|---|
| --prepare / --finalize / --all | --all | 互斥三选一；不传等价于 --all |
| --out | /app/output → /output → ./output（取第一个**真实写探测**通过者） | 产物目录 |
| --work | /app/work → out/work → ./work（或环境变量 LATEX_WORK_DIR） | 工作表、fixed/ 编辑副本、generated/ 所在目录 |
| --input | 自动发现 /input、/app/input、/app/work、/app/environment、/app，退到当前目录 | 指定材料目录（内部 chdir 后再发现，避免硬编码路径） |
| --quiet | 关 | 只打日志，不打印 JSON 摘要 |

退出码：0 = 成功/部分成功（result.json 必然存在且含全部契约字段）；1 = 顶层异常（仍会写出 status=failed 的 result.json，绝不零产出）。

### prepare 产物（Agent 的输入）

| 文件 | 内容 |
|---|---|
| work/fixes_worksheet.json | **主工作表**：main_file、files[]、static_summary、issues[]（最多 40 条，按 error/warning/info 排序，每条含 issue_id/file/line/error_type/severity/message/root_cause/typical_fix/evidence/fixable/suggested_fix）、answer_path、rules、answer_format、notes_format |
| work/tex_index.json | 工程结构：main_file、files[]、include_edges[]、document_class、packages[]（含选项与行号）、labels[]、references[]、citations[]、bib_entries[]、outline[]、engine_guess |
| work/static_issues.json | 完整审计（不限 40 条）：counts、by_type、checks[]（14 类检查的 pass/warning/fail）、issues[] |
| work/fixed/ | 工程的可编辑副本：Agent 也可直接改这里（finalize 以此为基线） |
| work/state.json | 清单：ok、main_file、file_count、files[]（sha256）、staticmethod、declared_fields |
| work/generated/ | Agent 写答案的目录（默认为空） |
| work/README_AGENT.md | 同一份协议的速查版 |

### finalize 产物（--out 目录）

| 文件 | 内容 |
|---|---|
| result.json | case_id + 契约字段（按 output_contract 声明顺序）+ meta。声明了但产不出的字段按声明类型占位（array→[]、object→{}、string→""）并列入 meta.contract_fields_not_reported；未声明的产物放 meta.additional_outputs |
| evidence.json | 输入发现、工程结构、修复前后审计全文、每条 fix 的 applied/rejected、编译尝试与日志摘要、交付清单、degraded 列表 |
| run_report.json | status、耗时、limits/uncertainty/change_log、generated 覆盖、修复统计、编译统计、产物路径 |
| source/ | **修复后的完整可编辑工程树**（去 BOM、LF、确定顺序） |
| static_report.md | 修复前后错误计数、14 类检查表、逐条缺陷与逐条修复、剩余缺陷 |
| fixes_applied.md | 修改记录表（applied + rejected，含 original/fixed/reason/resolved） |
| build_notes.md | 重建步骤：main 文件、引擎猜测与实际使用、轮次、交付文件、宏包清单、本次限制、Agent notes |
| reproducibility.json | 机器可读的复现记录（命令、轮次、宏包、模板文件、源码 sha256、环境） |
| latex_build.log / build_log_excerpt.txt | 有引擎时的完整编译日志与尾部摘要 |
| main.pdf / preview/page-1.png | 编译成功时的 PDF 与首页 PNG（渲染器可用时） |

**启动即写占位 result.json**（status=partial），跑完覆盖；顶层再包一层 BaseException → 写 status=failed。这样即使 900 秒预算处被杀，产物仍在且带 case_id。

## 四、字段字典（内部值键 → 契约字段）

result.json 的字段名由 case.json.output_contract 决定，**不允许硬编码**。技能内部先产出下表的"值键"，再由共享内核 hybrid.map_fields 按"精确 → 别名 → 词干前缀/包含"打分映射到声明字段（本技能另带一份 SYNONYMS 近义词表，见 scripts/run_latex.py 顶部）。映射结果写进 meta.contract_field_mapping，未命中的声明字段进 meta.contract_fields_not_reported。

| 内部值键 | 内容 | 可命中的常见声明字段（片段） |
|---|---|---|
| fixed_sources | 交付源码清单：path/delivered/role/bytes/sha256/sha256_original/changed | corrected_tex_sources、fixed_sources、source_files、tex_sources、corrected_sources、editable_sources |
| source_dir | out/source 路径 | source_directory、source_root |
| compile_report | 编译报告：engine_available/engine_used/engine_version/probe/plan/command/commands/rounds/bibtex_rounds/success/pdf/png/page_count/errors/warnings/undefined_references/undefined_citations/missing_packages/overfull_hboxes/rerun_needed/attempts/bibtex_runs/log_file/log_tail/reason/duration_s | latex_compile_report、compilation_report、compile_result、build_report、compilation |
| pdf_path | 编译成功时的 PDF 路径，否则 null | compiled_pdf_path、pdf、output_pdf、compiled_pdf |
| png_preview | 首页 PNG 路径，否则 null | first_page_png、preview_png、png、screenshot |
| build_log_excerpt | 日志尾部（无引擎时给出静态审计摘要与原因） | latex_log_excerpt、build_log、log_excerpt、log_tail |
| reproducibility | 复现记录：引擎/版本/命令/轮次/宏包/模板文件/源码 sha256/环境 | build_reproducibility、reproducibility_record、build_environment、toolchain |
| unresolved_warnings | 修复后仍存在的 warning/info 与编译 warning 字符串列表 | remaining_latex_warnings、warnings、remaining_warnings |
| static_checks | before/after 计数与 by_type、checks[]、issues_before[]、issues_after[]、resolved_issue_count | source_static_checks、static_analysis、source_checks、static_report |
| fixes_applied | 逐条应用记录：fix_id/file/line/error_type/original/fixed/reason/verified/resolved/resolution_note | applied_fix_records、applied_fixes、fixes、fix_records、changes |
| fixes_rejected | 逐条拒绝记录：fix_id/file/error_type/reason | rejected_fixes、failed_fixes、apply_failures |
| error_locations | 统一诊断（静态 + 编译），每条 source/file/line/location/error_type/severity/message/root_cause/resolved | located_errors、errors、error_report、diagnostics |
| tex_index | 工程结构（同 work/tex_index.json 的修复后视图） | project_structure、project_index、tex_inventory、document_structure |
| template_adaptation | 模板探测：document_class/class_files/style_files/packages/engine + 模板检查 + requirements_missing + agent_notes | template_adaptation_notes、template_report、template_compliance |
| quality | 自检指标：修复前后错误/警告数、fixes requested/applied/rejected/resolved/unresolved、compile_success/pdf_produced/page_count、checks_passed、degraded | run_quality、self_check、quality_report、metrics |
| evidence | 证据摘要（引擎猜测、编译成功、pdf/png、修复统计、复现摘要、交付路径） | evidence_anchors、anchors、provenance、traceability |
| build_command | 实际执行的命令字符串（无引擎时为说明文本） | compile_command、build_cmd、command |
| engine | 实际使用的引擎（或源码建议引擎） | tex_engine、engine_used、compiler、latex_engine |
| page_count | PDF 页数 | pages、num_pages、page_total |
| main_file | 主 .tex 相对路径 | main_tex、main_tex_file、root_file、master_file |
| notes | Agent 写的 notes.json（模板适配说明/未决问题/决策） | agent_notes、adaptation_notes、decisions |

契约声明里若出现 case_id，一律填真实 case_id，不会当占位。**允许一个值喂多个声明字段**，竞争失败的值退回 meta.additional_outputs 而不是丢弃。

## 五、Agent 操作协议（精确到命令与 JSON）

预算提醒：prepare + finalize 全程只用 **0 次模型请求**；模型请求只花在"读工作表 + 写 fixes.json + 必要时写 notes.json"上。建议节奏：prepare（1 次命令）→ 读 fixes_worksheet.json（1 次）→ 读被点名的源码片段 → 写 fixes.json（1-3 次）→ finalize（1 次）→ 读 finalize 摘要决定是否补一轮（≤2 轮）。

### 步骤 1：确定性准备

```bash
python scripts/run_latex.py --prepare --out /app/output --work /app/work
```

stdout 打印 ok / main_file / project_files / engine_probe / engine_guess / static_counts / worksheet_issues / expected_generated。随后按顺序读：

1. work/fixes_worksheet.json —— 唯一权威清单：每条缺陷的 error_type（决定修法）、root_cause（决定为什么）、evidence（要替换的原文）、suggested_fix（可直接采用的逐字替换建议，可能为 null）。
2. work/tex_index.json —— 主文件在哪、include 图、所有 label、所有 cite、.bib 条目、宏包加载顺序、engine_guess。
3. work/static_issues.json —— 若工作表只收了前 40 条，完整清单在这里。
4. 用 read 工具查看被点名的 work/fixed/文件 行附近内容——**不要凭记忆猜原文**，original 必须逐字匹配。

### 步骤 2：写 work/generated/fixes.json

顶层是一个 JSON 数组，每条记录：

```json
[
  {"file": "main.tex", "line": 21, "error_type": "undefined_reference",
   "original": "\\ref{sec:intr}", "fixed": "\\ref{sec:intro}",
   "reason": "工程里只有 sec:intro；原写法会让 PDF 印出 ??"}
]
```

| 键 | 必填 | 说明 |
|---|---|---|
| file | 是 | 工程相对路径（如 main.tex、sections/methods.tex）；也接受绝对路径或唯一文件名 |
| original | 是 | **要替换的原文**，必须是当前文件内容的逐字子串（或整行逐字相等）；多行用 \n |
| fixed | 是 | 替换文本；**空字符串表示删除该行** |
| line | 否 | 1-based 提示；当 original 在文件里出现多次时**必须给**，用于选定所在行 |
| error_type | 推荐 | 取工作表的 error_type，用于"这条修复是否真的解决了该缺陷"的复检归因 |
| reason | 推荐 | 一句话说明为什么这样改——会原样进入 fixes_applied.md 供评审复核 |

别名兼容：path/source_file → file，find → original，replace → fixed，type → error_type，why → reason。

**替换规则（代码强制，违反会被拒绝并记账）**：① original 在文件里必须唯一；出现多次且没有 line → ambiguous 拒绝；② 找不到 → not_found 拒绝；③ 空白差异但整行等价 → 允许按整行替换（保留缩进）；④ 替换后与原文相同（no-op）→ 拒绝；⑤ 文件不在工程内 → 拒绝；⑥ 最多应用 120 条（超出截断并记 limits）。

### 步骤 3（可选）：写 work/generated/notes.json

```json
{
  "template_adaptation": "沿用 article 类，subfig 是模板要求的子图宏包，未引入新宏包",
  "unresolved": [
    {"file": "main.tex", "line": 20,
     "issue": "cite missing2020 在 .bib 里没有条目，材料也未给出该文献元数据，需作者补录"}
  ],
  "decisions": [{"issue_id": "tex-0006", "decision": "保留 jones2021 未引用，等待作者确认"}]
}
```

unresolved 里的内容会进 meta.uncertainty，是"材料不足/需人工确认"的正式出口——**不要用编造的 bib 条目或数值把它填平**。

### 步骤 4：确定性收尾

```bash
python scripts/run_latex.py --finalize --out /app/output --work /app/work
```

stdout 打印 status / degraded / compile{engine_available,engine_used,success,pdf,png,page_count,errors} / contract_field_mapping / contract_fields_not_reported / field_fill_ratio / outputs。

### 步骤 5：按 status 决定是否补一轮

| 观察到的现象 | 含义 | 行动 |
|---|---|---|
| status=success | 无降级：修复被接受且编译成功（或有引擎时 PDF 产出） | 收尾 |
| degraded 含 compile_unavailable | 镜像没有 TeX 引擎：静态路径 + 源码已交付，这是**默认降级路径** | 检查 static_checks.issues_after 是否还有 error；不要再改 fixes 也编译不了 |
| degraded 含 fix_rejected / static_errors_remaining | 有修复没匹配上或仍有 error | 读 fixes_rejected[].reason 与 static_checks.issues_after[]，按提示重写 fixes.json 后重新 finalize |
| degraded 含 compile_failed | 引擎存在但没产出 PDF | 读 compile_report.errors[]，按 error_type 修源码（可能要先补宏包/字体，见第六节策略表） |
| contract_fields_not_reported 含 compiled_pdf_path | 没编译出 PDF（通常因无引擎或编译失败），如实留空 | 不要伪造路径 |

## 六、核心工作流（两个确定性阶段 + 中间一次判断）

### 阶段 1 prepare（确定性）

1. **输入发现**：扫描 /input、/app/input、/app/work、/app/environment、/app（最后退当前目录），找出 case.json、task.md 与全部 TeX 工程文件；跳过 output/work/generated/__pycache__ 等目录，避免把自己的产物当输入。
2. **主文件选择**：对每个 .tex 打分（含 documentclass +100、含 begin{document} +20、文件名命中 main/paper/thesis 等 +30、路径更浅、体积更大者优先），取最高分为编译入口；候选清单保留在 work/state.json。
3. **递归解析**：从主文件 BFS 展开 input/include/subfile，解析 bibliography/addbibresource/includegraphics；缺失目标记为 missing_input_file 或 missing_file。工程根取全部相关文件的公共祖先目录，保证上级相对路径（如 ../sections/x）在交付树里仍然成立。
4. **静态审计 14 类检查**（work/static_issues.json 的 checks[] 给出每类 pass/warning/fail）：
   - 主文件识别、编码与 BOM（UTF-8 可解码、无 BOM、无 NUL）
   - 花括号平衡、环境栈闭合（verbatim 类环境内不计数，避免误报）
   - 文档结构（documentclass、begin{document}、end{document}）
   - 子文件解析（include/bibliography/图片目标存在）
   - 交叉引用（label 唯一、ref/eqref/cref 有定义；近似 label 用 difflib 给出建议）
   - 参考文献（cite 键集合 vs .bib + bibitem；缺条目/未引用/重复 key/字段不全/缺文献命令）
   - 宏包卫生（重复加载、已知冲突对、过时宏包、cleveref 与 hyperref 顺序）
   - 引擎提示（CJK/fontspec/luatexja → xelatex/lualatex；无中文支持 → cjk_without_package）
   - 模板必填项（title/author/abstract/maketitle、模板类是否被使用/是否存在）
   - 表格列结构（tabular 列格式解析，含 p{3cm} / *{2}{c}；行 & 数不符）
   - 占位标记（TODO/FIXME/待补/??）、数学定界符（$ 配对）
5. **引擎探测**：调用共享内核 render.probe()，记录 tex 实际存在的二进制；按源码给出 engine_guess（CJK→xelatex，fontspec→xelatex，luatexja→lualatex，否则 pdflatex）。
6. **写工作表 + 编辑副本**：fixes_worksheet.json（前 40 条）、tex_index.json、static_issues.json、state.json、README_AGENT.md，并把工程复制到 work/fixed/ 供 Agent 直接编辑。

### 阶段 2 judgement（Agent，见第五节）

按 error_type 选策略。下表是**推荐修法**（不是唯一解，但违反"不臆造事实"的修法会被文档与评审否定）：

| error_type | 典型修法 | 禁止 |
|---|---|---|
| undefined_reference | 改用真实 label（有 suggested_fix 时优先采用），或在正确位置补 label | 把 ref 改成硬编码编号 |
| missing_bib_entry | 材料里有该文献元数据 → 补 .bib 条目；没有 → 写进 notes.unresolved | 编造作者/年份/期刊；把引用删掉当"修好" |
| duplicate_label | 重命名后出现的 label 并同步其 ref | 只删 ref 保留重复 label |
| missing_end_environment / missing_end_document | 在正确位置补 end 环境 / end{document} | 删掉 begin 来"配平" |
| unbalanced_brace / unbalanced_math_dollar | 定位分组/公式起点补配对符号 | 全局替换花括号 |
| package_conflict | 按模板要求保留一个（如 subfigure 与 subfig 只留其一） | 两个都留、或引入镜像里没有的新宏包 |
| deprecated_package | 换成现代等价宏包（epsfig→graphicx、times→newtx 系列） | 保留过时宏包又叠加新宏包 |
| duplicate_package | 删除后一次加载（用 line 提示区分两次出现） | 删除第一次加载而留下带不同选项的第二次 |
| cjk_without_package | 加载 ctex（工作表建议插在 documentclass 后）并改用 xelatex | 用拼音/英文替换中文正文 |
| utf8_bom / encoding_not_utf8 | 转存无 BOM 的 UTF-8，内容逐字不变 | 借转码之机改正文 |
| table_column_mismatch | 对齐该行 & 数量或调整列格式 | 缩小字号/整页缩放掩盖溢出 |
| template_required_field | 按模板要求补标题/作者/摘要，内容取自材料 | 编造作者单位或基金号 |
| 工作表给出 suggested_fix 的可修复项 | 可直接采用（original/fixed/reason 三件套） | 忽略建议自己另写一版 |
| compile 侧 missing_package / font_not_found | 换用镜像自带宏包/字体，或在模板允许范围内去掉该设置；记入 notes | 声明"已安装"（本环境不能联网安装） |

### 阶段 3 finalize（确定性）

1. **加载 Agent 输出**：读 work/generated/fixes.json（数组 / {fixes:[...]} / JSONL 都认）与 notes.json；坏 JSON 降级而不是崩。
2. **严格应用修复**：在 work/fixed/ 上按第五节规则逐条字面替换，记录 applied/rejected/files_changed（含前后 sha256）；work/fixed 缺失时先从原始输入重建。
3. **复检**：对修复后的树重跑完整静态审计，按稳定签名比较修复前后，给每条 fix 打 resolved: true/false/null（null = 该缺陷类型没有可归因的静态发现），并把剩余缺陷写进 static_checks.issues_after。
4. **交付源码**：把修复后的整棵工程树写到 out/source/（去 BOM、LF、保留目录结构、保留图片），记录每个文件的 bytes/sha256/是否改动。
5. **真实编译**（有引擎时）：编译计划 latexmk（带 -pdf/-xelatex/-lualatex）→ 源码建议引擎 → 其余引擎 → tectonic → latex-compiler；统一 -interaction=nonstopmode -file-line-error、最多 3 轮；第一轮后若 .aux 含 bibdata/citation 则跑 bibtex（有 .bcf 且有 biber 则跑 biber），再补轮直到日志不再 Rerun；找到 PDF 后复制到 out/，再从日志取页数，并尝试 PDF 转 PNG 生成首页预览。
6. **日志归因**：把 .log 解析成 errors[]（file、line、location、error_type、message、root_cause、typical_fix、context）、warnings[]、undefined_references[]、undefined_citations[]、overfull_hboxes、rerun_needed；一条日志里有多条 ! 时全部收集（不因第一条错误提前停止）。
7. **契约反射与落盘**：汇总 values → hybrid.finalize → 写 result.json（声明顺序、缺值按类型占位并记账）、evidence.json、run_report.json、static_report.md、fixes_applied.md、build_notes.md、reproducibility.json。

状态判定：**success** 仅当"无降级"（有引擎时编译成功且日志无 error；无引擎时不可能 success）；**partial** 当 compile_unavailable、compile_failed、fix_rejected、static_errors_remaining、fix_not_effective、缺少 generated 且仍有缺陷等；**failed** 仅在找不到任何 LaTeX 工程（或顶层异常）时出现——此时仍按契约写占位结果。

## 七、异常处理

| 异常情况 | 检测方式 | 处理策略 | 状态标记 |
|---|---|---|---|
| 找不到 case.json | 发现阶段无 case | 仍解析工程与材料，contract_detected=false，产物进 meta.additional_outputs | partial |
| 找不到任何 .tex | 主文件选择为空 | 写 status=failed + 声明字段占位 + limits 说明 | failed |
| 主文件判断错（多个候选） | 候选表 score 接近 | 全部候选写入 state.json/project.candidates，供人工复核；按分数确定性取第一 | success（带 uncertainty） |
| input 目标缺失 | 递归解析时解析失败 | 记 missing_input_file（error）+ 交付树保留缺失说明，不伪造文件 | partial |
| 模板类缺失 | documentclass 非标准类且工程内无 .cls | 记 missing_class_file；不自动替换类 | partial |
| 无 TeX 引擎 | render.probe()["tex"] 为空 | 跳过编译，compile_unavailable 写进 limits/uncertainty；静态报告 + out/source 照常交付 | partial |
| 编译不产 PDF | 运行后找不到 main.pdf | 解析全部错误、尝试下一个引擎（≤3 个）；compile_failed 记账 | partial |
| 缺宏包/缺字体 | 日志 File ... not found / Font ... not loadable | 归因到具体宏包/字体名，写 missing_packages，提示换等价宏包或去掉字体设置 | partial |
| 报错级联（几十条 !） | 日志解析上限 40 条 + 去重 | 全部收集并保留上下文，先修最早一条；不假装"只有一条错误" | partial |
| Agent 修复原文找不到 | 字面替换 0 次命中 | 拒绝该条（not_found），不强行改；其余修复照常应用 | partial（fix_rejected） |
| Agent 修复命中多处 | 出现次数 >1 且无/错 line | 拒绝（ambiguous），提示补 line | partial（fix_rejected） |
| 修复是 no-op / 目标文件不在工程 | 替换前后相同 / 文件名不匹配 | 拒绝并记账，不静默丢弃 | partial（fix_rejected） |
| generated JSON 损坏 | 解析失败 | 视为无 generated，静态路径继续，broken_generated:* 记账 | partial |
| 修复后仍有 error | 复检计数 | 全部列进 static_checks.issues_after 与 static_report.md，不掩盖 | partial |
| 编码非 UTF-8 / 带 BOM | 字节解码与首三字节检查 | 记录 encoding_not_utf8 / utf8_bom；交付树统一去 BOM，不改内容 | partial |
| 工程过大/文件过多 | 文件数与体积上限 | 超出部分跳过并写 limits，不静默截断 | partial |
| 编译超时 | 每轮 subprocess.run(timeout) + 剩余预算检查 | 终止该轮、记 timeout、尝试下一引擎；软截止前停止 | partial |
| 顶层任何未捕获异常 | except BaseException | 写 status=failed 的 result.json（含 traceback 尾部），绝不零字节 | failed |

## 八、限制与边界

1. **不开启 shell-escape**：需要外部程序/harmful 宏包的稿子本技能不跑；这是安全约束，会在 limits 说明。
2. **无引擎时不产出 PDF/PNG**：compiled_pdf_path/first_page_png 按声明类型占位并列入 contract_fields_not_reported；这是**默认降级路径**，静态审计与修复后源码仍然完整交付。
3. **不联网、不安装宏包/字体**：缺宏包只能换等价宏包或记入 notes；不伪造"已安装"。
4. **不编造事实**：bib 条目、数值、作者、定理陈述一律不得新增；材料不足写 notes.unresolved。
5. **日志的文件归因是启发式**：TeX 的括号栈不可靠，本技能用"错误行向前 40 行内的最近文件标记 + l.N 行号"推断，file 可能为空；location 仍给出行号与上下文，便于人工复核。
6. **静态审计是语法/结构级**：不判断公式数学正确性、不验证表格数值语义、不解析宏包内部行为；table_column_mismatch 只看 & 数量。
7. **环境差异**：不同 TeX 发行版可能给出不同页数/断行；复现记录保存命令与版本，但不保证字节级 PDF 相同。
8. **上限**：工作表 40 条、应用 120 条修复、工程 400 文件、单文本 4 MB、交付源码 24 MB、编译每轮 150 s；超限均记 limits。
9. **不处理二进制/加密工程**：.tex 无法按 UTF-8 解码时只记缺陷不强行改。
10. **不替代人工判断**：模板必填项、引用是否恰当、术语与版式取舍仍需作者/评审确认（会出现在 meta.uncertainty）。

## 九、失败处理与自查

| 现象 | 自查方法 | 处理 |
|---|---|---|
| status=failed 且 limits 含 no LaTeX project | 看 run_report.json.inputs.material_docs 与 case.json 是否在扫描目录 | 确认案例确实给了 .tex；否则检查 --input 是否指错 |
| 主文件选错 | 看 work/state.json 的 files、tex_index.json 的 main_file、state.project.candidates | 把正确主文件的 documentclass/文件名补齐，或直接编辑 work/fixed 后 finalize |
| 所有修复都被拒绝 | 看 fixes_rejected[].reason：not_found = original 不对；ambiguous = 缺 line；not part of the delivered project = file 名不对 | 用 read 工具重取原文；多行原文别手打 |
| static_errors_after 仍高 | 看 static_checks.issues_after[] 的 error_type 与 root_cause | 按第六节策略表补 fixes；缺材料项写 notes.unresolved |
| compile failed 但错误看似奇怪 | 看 compile_report.log_tail 与 errors[].context | TeX 常在文件末尾才报"分组未闭合"，真实位置在上文；先修最早一条 |
| 日志里全是 Undefined control sequence | 看 compile_report.errors[] 里是否同源于缺宏包 | 先解决 missing_package，其余多为级联 |
| page_count=null | 看 compile_report.pdf 是否为 null | 无 PDF 时页数无从计算；不要填假值 |
| field_fill_ratio 偏低 | 看 meta.contract_field_mapping 与 contract_fields_not_reported | 低通常是"确实产不出"（无引擎）或字段名确实无法映射；确认后如实保留 |
| 重复运行结果不一致 | 看两次 work/static_issues.json（本技能自测覆盖该点） | 若不一致，检查是否有文件在两次间被改动（例如 Agent 直接改了 work/fixed） |

## 十、最小调用示例（**示意输出，非正式案例结果**）

**输入**（自测里用同一结构生成的合成工程，非真实论文）：

```
fixture/
  case.json      case_id=latex-selftest-001，声明 16 个契约字段（字段名与内部命名不同）
  task.md
  main.tex       article 类；含 input{sections/results}、bibliography{refs}、若干缺陷
  sections/results.tex
  refs.bib       smith2019（被引）、jones2021（未引）
  figures/plot.png
```

**调用**：

```bash
cd fixture
python ../../skills/writing-13-latex-compilation/scripts/run_latex.py --prepare  --out out --work work
# Agent 写 work/generated/fixes.json
python ../../skills/writing-13-latex-compilation/scripts/run_latex.py --finalize --out out --work work
```

**示意输出**（result.json 摘要，**这是示例，不是正式案例结果**；数字取自本地实际运行，本机无 TeX 引擎）：

```json
{
  "case_id": "latex-selftest-001",
  "corrected_tex_sources": [
    {"path": "main.tex", "bytes": 912, "sha256": "...", "changed": true,
     "role": "main", "delivered": "out/source/main.tex"},
    {"path": "sections/results.tex", "changed": false, "role": "source"},
    {"path": "refs.bib", "changed": false, "role": "bibliography"},
    {"path": "figures/plot.png", "changed": false, "role": "figure"}
  ],
  "source_static_checks": {
    "before": {"counts": {"error": 6, "warning": 2, "info": 1, "total": 9}},
    "after":  {"counts": {"error": 2, "warning": 2, "info": 1, "total": 5}},
    "checks": [{"check_id": "label_reference", "status": "pass", "findings": 0}]
  },
  "latex_compile_report": {
    "engine_available": false, "engine_used": null, "success": false,
    "probe": {"tex": [], "capabilities": {"tex_to_pdf": false}},
    "reason": "no TeX engine found on PATH (probed: none); compilation was not attempted"
  },
  "compiled_pdf_path": "",
  "first_page_png": "",
  "latex_log_excerpt": "No TeX engine was available in this image ...",
  "build_reproducibility": {"tex_engine": null, "build_command": null,
                             "engine_guessed": "pdflatex", "rounds": 0},
  "remaining_latex_warnings": ["missing_bib_entry: 引用的 key 'missing2020' 不在 .bib ..."],
  "located_errors": [
    {"source": "static", "file": "main.tex", "line": 21,
     "location": "main.tex:21", "error_type": "undefined_reference",
     "root_cause": "引用了不存在的 label，PDF 会印出 ?? ...", "resolved": true},
    {"source": "static", "file": "main.tex", "line": 20,
     "error_type": "missing_bib_entry", "resolved": false}
  ],
  "applied_fix_records": [
    {"fix_id": "fix-001", "file": "main.tex", "line": 4,
     "error_type": "package_conflict", "resolved": true}
  ],
  "project_structure": {"main_file": "main.tex", "file_count": 5,
                         "include_edges": [{"parent": "main.tex", "child": "results.tex"}],
                         "engine_guess": {"engine": "pdflatex"}},
  "template_adaptation_notes": {"detected_template": {"document_class": {"name": "article"}}},
  "run_quality": {"static_errors_before": 6, "static_errors_after": 2,
                  "fixes_applied": 4, "fixes_rejected": 2,
                  "compile_attempted": false, "compile_success": false},
  "evidence_anchors": {"compile_success": false, "static_errors_after": 2},
  "zzz_unmodelled_field": "",
  "meta": {
    "status": "partial",
    "contract_detected": true,
    "contract_field_count": 16,
    "contract_fields_not_reported": ["compiled_pdf_path", "first_page_png",
                                     "zzz_unmodelled_field"],
    "field_fill_ratio": 0.8125,
    "limits": ["status=partial: ...", "compile_unavailable: no TeX engine in this image; ..."],
    "uncertainty": ["agent notes record 1 unresolved item(s); they need an author decision"],
    "contract_field_mapping": {"corrected_tex_sources": "fixed_sources", "...": "..."},
    "output_files": {"result": "out/result.json", "evidence": "out/evidence.json",
                     "run_report": "out/run_report.json"}
  }
}
```

> 该示例里 compiled_pdf_path 为空、状态 partial，是本机没有 TeX 引擎时的**诚实降级**：静态审计与修复后源码已交付，换到有 TeX 的镜像执行同一条命令即可得到 PDF 与 success。

## 十一、架构参考与差异说明

文档组织参考了平台公开技能目录的写法（frontmatter → 技能定位 → 适用条件 → 调用方式 → 字段字典 → Agent 协议 → 工作流 → 异常处理 → 限制 → 自查 → 最小示例 → 踩坑），并复用本项目共享内核 skills/_core（hybrid 两阶段框架、contract 契约反射、discover 输入发现、render 渲染探测）。此处只借鉴**结构组织与已验证的公共件**，LaTeX 适配层与静态/编译逻辑为本技能自写。

| 维度 | 常见做法 | 本技能做法 |
|---|---|---|
| 修复应用 | 让模型重写整段源码，或"尽力而为"地替换 | 严格字面替换：找不到/多处/无变化一律拒绝并记账（fixes_rejected） |
| 编译验证 | 只看有没有报错，或停在第一条错误 | 非交互多轮编译 + .log 全量归因（file:line:root_cause）+ 复检前后对比 |
| 无 TeX 环境 | 直接失败或声称成功 | 降级 partial：静态审计 + 源码交付 + compile_unavailable 如实记录 |
| 输出契约 | 固定 JSON schema | 从 case.json.output_contract 反射；缺值按声明类型占位并记账 |
| 证据 | 模型自述"已修复" | 每条修复带前后 sha256、复检 resolved、编译命令与日志摘要 |
| 可复现 | 写"需要安装 TeX" | 记录实际引擎/版本/命令/轮次/宏包/模板文件/源码校验和 |
| 被杀风险 | 最后才写结果 | 启动即写占位 result.json，顶层异常再兜底写 failed 结果 |

## 十二、环境踩坑（--help 不会告诉你的）

1. **%TEMP% 可能不可写**：受限沙箱下 tempfile.TemporaryDirectory() 直接 PermissionError，本技能所有中间产物都写在 --work 目录内；自测也只在包内 _selftest_tmp 落盘。
2. **不要用 pip install 补依赖**：受限环境装包会因元数据写入被拒而失败；本技能刻意只用标准库。
3. **TeX 日志的文件归属不可靠**：日志里的左右括号是文件栈标记，跨行、嵌套、宏包日志都会打乱它；本技能只做"向前找最近文件标记 + l.N"的启发式并在文档里写明，location 为空的错误仍有 context 与 message 可定位。
4. **halt-on-error 会掩盖后续错误**：本技能刻意**不用**它，配合 nonstopmode 收集全部 ! 错误；代价是可能拿到一堆级联错误，所以日志解析做了去重与分类。
5. **latexmk 的引擎开关是 -pdf/-xelatex/-lualatex**：不是 -pdflatex；传错会退回默认 pdflatex，中文稿会在第一步就失败。
6. **多轮编译是硬需求**：交叉引用、目录、ref 稳定至少要 2 轮，带 bibtex 时正确顺序是 latex → bibtex → latex → latex（latexmk 会自动做，单引擎模式由本技能补跑）。
7. **BOM 与 CRLF**：Windows 工具链常写带 BOM 的 UTF-8；本技能内部统一 LF、交付树统一去 BOM，否则 documentclass 前多出的字节会在部分引擎上直接报错/产生空白页。
8. **中文需要 xelatex + ctex/xeCJK**：pdflatex 对 CJK 不会"报错"，只会印出缺字方框——所以静态检查里 CJK 无宏包是 error 而不是 warning。
9. **输入目录里会有我们自己的产物**：out/、work/、generated/ 都被列入扫描排除名单；否则第二次运行会把上次的 source/ 当材料，主文件选择会漂移。
10. **技能目录本身不能被当材料**：开发期在仓库根运行 --all 时，skills/ 下的测试工程可能被误扫；技能只在**扫描根不在技能目录内**时排除技能目录（自身测试 fixture 在技能目录内时必须可扫描，否则自测全灭）。
11. **工作区路径可能带空格/中文**：命令行参数统一用 subprocess.run(list)，不做 shell 拼接，避免路径转义问题。
12. **契约字段名不可猜**：不要按"通常叫 fixed_sources"写死；先看 case.json.output_contract，映射结果以 meta.contract_field_mapping 为准。

## 维护信息

- 版本：1.0.0；作者：66Union；许可证：MIT。
- 运行依赖：Python >= 3.9（标准库）；可选 TeX 发行版（latexmk/xelatex/pdflatex/lualatex/tectonic）与 PDF 转 PNG 工具。
- 文件结构：scripts/run_latex.py（唯一入口，含 prepare/finalize/CLI）、resources/latex_error_taxonomy.json（46 类缺陷的错误分类表：中文标签/根因/严重度/可修复性/典型修法；运行时与内置兜底表合并）、tests/test_latex.py（合成工程端到端自测）、lib/（共享内核）。
- 扩展方式：给 resources/latex_error_taxonomy.json 增加 error_types.<new_type> 即可新增分类（无需改代码）；新增静态检查需在 run_latex.py 的 CHECK_DEFS 与对应 check_ 函数中同时登记，保证 checks[] 与文档一致。
- 维护注意：改动 check_ 逻辑或错误签名格式后，必须同步跑 tests/test_latex.py（其中"确定性审计"与"修复 resolved 归因"两条会直接暴露签名漂移）。
