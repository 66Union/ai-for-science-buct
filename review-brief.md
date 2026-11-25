# 代码审查任务书（给 Kimi）

你是一个**敌对的代码审查者**。目标是找出这个参赛项目在真实评测环境里会失败或失分的地方，而不是夸奖它。

## 背景（评测方怎么跑我们的代码）

我们在参加玻尔 Playground 的 AI for Science 赛事（北化站第一期）。提交物是一个 **Agent Skill**，由平台在 Harbor 容器里执行：

```
评测流程：
1. 把我们的 Skill 目录安装到 /skills/weekly-skill
2. 用官方 DeepSeek Harness 加载它，对 3 个隐藏案例各跑 3 次
3. 运行时输入: /input/case.json （含 case_id 与 output_contract）+ task.md
4. 我们的脚本必须产出 /app/output/result.json，且必须包含 output_contract 的**全部字段**与 case_id
5. 预算：64 次模型请求 / 900 秒 / 单响应 16384 tokens
6. 资源限制：≤50 个 UTF-8 文本文件，单文件 ≤500KB，总量 ≤2MB，禁止符号链接
```

评分维度：实际效果 40（正确性/完整性/可核验性）、使用增益 25、创新增益 15、可靠体验 10（其中**异常处理 3 分、运行稳定性 4 分**）、生态贡献 10（文档完整性 3、资源可复用性 4）。

**隐藏案例的 shape 我们不知道。** 所以"假设输入长什么样"是最危险的地方。

## 需要你审查的文件

全部在 `D:\AI-for-science\`：

```
skills/_core/lib/discover.py      输入发现
skills/_core/lib/pdftext.py       零依赖 PDF 文本提取（对象扫描/Flate/文本矩阵/ToUnicode）
skills/_core/lib/extract.py       引擎探测链 + 降级
skills/_core/lib/structure.py     章节/表格/公式/参考文献识别
skills/_core/lib/anchors.py       证据锚点与引文校验
skills/_core/lib/contract.py      output_contract 反射生成 result.json
skills/_core/scripts/run_reading.py  主流程
skills/_core/tests/test_core.py   自测
skills/reading-01-document-parsing/SKILL.md  对外文档（承诺了什么）
tools/dsh2trace.mjs               DSH 会话日志 → ARM trace 转换器
```

## 请重点找这几类问题

### A. 边界条件（按严重度排）
1. `case.json` 不存在 / 是空对象 / `output_contract` 为空 / 为 list / 为嵌套 / 字段名含非 ASCII 或空格时，`result.json` 还能不能生成且形状正确？
2. 材料文档：0 个 / 1 个 / 5 个 / 非 PDF（docx/xml/tex/md）/ 加密 PDF / 纯图片扫描件 / 超大（几百页）——分别会怎样？
3. PDF 内部：无 xref（损坏）、增量更新、对象流（ObjStm）、非 Flate 滤镜、CID 字体无 ToUnicode、注释里含 `stream` 字样、文本串里嵌套未转义括号——`pdftext.py` 哪些会崩、哪些会**静默产出错误文本**（后者更糟）？
4. 输出目录只读 / 不存在 / 已有同名 `result.json` 时行为？
5. `--out` 参数缺失、传相对路径、传不存在目录时？

### B. 静默错误（最致命：不报错但结果是错的）
- `pdftext.py` 里对象扫描用"下一个 obj 之前"作为当前对象结尾，遇到 `stream` 内含 `obj` 字样会怎样？
- 文本矩阵只处理了 `Tf/Td/TD/Tm/T*/Tj/TJ/'/"`，缺 `TL` 之外还有哪些常见操作符会改变位置却没处理（如 `cm`、`q/Q`、`Ts`、`Tz`）？会不会导致列顺序错乱却仍标 `anchor_quality=span`？
- `_cluster_lines` 用固定容差 2.0 聚行，遇到斜体/上标（y 偏移 > 2）会不会把同一行拆成两行、进而让 anchor 校验"通过"但引文错位？
- `anchors.verify` 的 `block_only` 分支：块存在但引文不在其中，是否可能被上游误当作已落地？
- `structure.detect_tables` 用空格数量当列数——单栏正文里的长空格缩进会不会被误判成表格并推高置信度？

### C. 契约与分数的实际风险
- `run_reading.py` 里 `contract.build_result` 被调用了两次（一次原值一次映射后）。请检查：第二次是否会丢掉第一次写入的 `meta` 内容？`additional_outputs` 是否会与 `meta.evidence` 重复导致 `result.json` 体积翻倍（2MB 资源限制不适用 result.json，但上传 bundle 有 512MiB 上限、评分器可能读不完）？
- 契约字段映射失败时结果是 `null`。评委看到 `null` 与"字段缺失"哪个更扣分？有没有更好的处理（例如放入 `meta.contract_fields_not_reported` 同时给一个结构化占位）？
- 900 秒预算：`run_reading.py` 的 `SOFT_DEADLINE_S=720` 只在阶段之间检查一次。若单个 PDF 提取卡住（几十秒级）是否仍会超时？有没有真正的硬超时机制？

### D. 文档与实现是否一致（会被评委抓）
逐条核对 `SKILL.md` 的承诺与代码实际行为，特别是我可能"写了但没实现"的地方：
- "页眉页脚与正文分开存放" —— 代码里真的区分了吗？
- "阅读顺序按自上而下再左右还原" —— `_cluster_lines` 之后再 `blocks()`，顺序真的对吗？
- "逐引擎降级，记录失败原因到 `attempts`" —— 所有分支都记了吗？
- 示例输出里的数字（如 `bbox: [72,756,453,764]`、`claims_total: 10`）与我实际跑出来的是否一致？

### E. 安全与合规
- 密钥会不会被写进产物（`scanForSecrets` 会拒绝打包）？
- `dsh2trace.mjs` 的脱敏：`.secrets` 里若有一个长度 ≥6 的普通词（比如误存），会不会把 trace 里大量正常文本打成 `<SECRET_REDACTED>` 导致 trace 语义毁掉？

## 输出格式要求

按 **P0（会导致 0 分 / 评测失败）→ P1（明显丢分）→ P2（可改进）** 分级，每条包含：

```
[级别] 文件:行号 — 一句话问题
触发条件：什么样的输入/环境会命中
后果：具体会怎样（崩？静默错？丢几分？）
最小修复：具体改法
```

**不要给出泛泛建议**（如"建议加更多测试"）。要么指出具体的代码路径，要么跳过。如果你认为某处其实没问题，也请明确说"已检查、无问题"，这样我知道覆盖面。

不要修改任何文件，只做阅读和报告。
