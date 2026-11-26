# RUNBOOK — 玻尔 Playground 北化站参赛流水线

> 本文件是本机唯一可信的命令来源。所有路径已适配 Windows + DSH 沙箱。
> **绝不要把 `PLAYGROUND_TOKEN` 的值打印到终端或写进任何提交目录。**

---

## 0. 本机已就绪状态

| 组件 | 位置 / 状态 |
|---|---|
| Playground CLI 0.1.58 | `D:\AI-for-science\.tools\playground.cmd` |
| Bohrium CLI (`bohr`) | `D:\AI-for-science\.tools\bohr.cmd` |
| npm cache（沙箱必需） | `D:\AI-for-science\.npm-cache` |
| API 令牌 | `D:\AI-for-science\.secrets\playground_token.txt`（52 字符） |
| trace 转换器 | `D:\AI-for-science\tools\dsh2trace.mjs` |
| 赛题包（08 题） | `D:\AI-for-science\task-08\`（challenge.json / task.md / config.json） |
| 账号 | 三花猫联盟 · id 3944299 · 所属机构 北京化工大学 · 邀请码 h7ds2uyx 已绑定（不可改） |

---

## 1. 每个新 shell 的第一件事：注入令牌

```powershell
$env:PLAYGROUND_TOKEN = (Get-Content "D:\AI-for-science\.secrets\playground_token.txt" -Raw).Trim()
```

不要 `Write-Output $env:PLAYGROUND_TOKEN`。要检查是否加载成功，只看长度：

```powershell
"token length = $($env:PLAYGROUND_TOKEN.Length)"
```

## 2. 认证检查

```powershell
& "D:\AI-for-science\.tools\playground.cmd" auth status
```

期望：`"authenticated": true`，`user.id = 3944299`。

## 3. 下载 / 更新赛题

```powershell
& "D:\AI-for-science\.tools\playground.cmd" task download `
  --challenge-id product-arena-08-3027d350 `
  --out "D:\AI-for-science\task-08"
```

10 题 ID 见 `00-赛前分析-北化分站赛第一期.md` §2。

## 4. 生成可用 trace（**每次提交前必做**）

DSH 原生日志是 zstd 压缩的，**不能直接当 --trace**。用转换器：

```powershell
node "D:\AI-for-science\tools\dsh2trace.mjs" `
  --session <你的会话ID> `
  --out "D:\AI-for-science\out\trace.jsonl"
```

不指定 `--session` 时用 `--latest`（取最新会话）。转换器会打印 step 统计与成本合计。

## 5. 校验 trace

```powershell
& "D:\AI-for-science\.tools\playground.cmd" trace validate --trace "D:\AI-for-science\out\trace.jsonl"
```

期望：`"valid": true`，`usable_events` 与 `jsonl_events` 同值。

**本地硬门槛**（不过就重做，不要改数字去凑）：

| 门槛 | 要求 |
|---|---|
| `tool_call_pairing` | 每个 tool_call 有同 id 的 tool_result |
| `cost_floor` | 全轨迹 cost 合计 ≥ 0.01 USD |
| `thought_chain_thin` | ≥3 条 thought 且 body ≥80 字符 |
| `zero_resource_signals` | 至少一步有 cost 或 tokens |
| `timestamp_monotonic` | 时间戳非递减 |
| `step_id_unique` | step_id 唯一 |

## 6. 预检（不发网络请求）

```powershell
& "D:\AI-for-science\.tools\playground.cmd" submit `
  --challenge-id product-arena-08-3027d350 `
  --outputs "D:\AI-for-science\out\<题目目录>" `
  --trace "D:\AI-for-science\out\trace.jsonl" --dry-run *> "D:\AI-for-science\out\dryrun.log"
```

⚠️ `--dry-run` 会把全部 trace 步骤打印出来（大几百行）。**必须重定向到文件**，只看摘要：

```powershell
Select-String -Path "D:\AI-for-science\out\dryrun.log" -Pattern '"status"|"bundle_bytes"|"violations"' | Select-Object -First 10
```

## 7. 正式提交

```powershell
& "D:\AI-for-science\.tools\playground.cmd" submit `
  --challenge-id product-arena-08-3027d350 `
  --outputs "D:\AI-for-science\out\<题目目录>" `
  --trace "D:\AI-for-science\out\trace.jsonl"
```

记下返回的 **Attempt ID**。每题最多 10 次（被评分才计数）。

## 8. 查状态

```powershell
& "D:\AI-for-science\.tools\playground.cmd" status --attempt-id <ATTEMPT_ID>
```

---

## 9. 打包纪律（会被 CLI 直接拒绝的几件事）

1. **凭据扫描**：`scanForSecrets` 会扫 `--outputs` 目录，命中即拒绝打包。`.secrets/`、任何 `.env`、任何含 token 的文件**绝不能出现在 outputs 目录里**。
2. **体积**：Skill 资源 ≤50 个 UTF-8 文本文件、单文件 ≤500 KB、总量 ≤2 MB、无符号链接。
3. **不要手造 ARM 文件**：`arm_manifest.json` / `characterization.json` / `execution/run.log` 由 CLI 在打包时自动合成，不要自己写。题面 `expected_artifacts` 里那串是给评审看的清单，不是打包硬要求（`--dry-run` 已实测通过）。
4. **每题一个 GitHub 链接**：提交目录根放 `GITHUB_URL.txt`，第一行链接，第二行（可选）本次提交目录内的相对路径。

## 10. 目录约定

```
D:\AI-for-science\
├─ 00-赛前分析-北化分站赛第一期.md
├─ tools\dsh2trace.mjs          # 会话日志 → ARM trace
├─ tools\RUNBOOK.md             # 本文件
├─ skills\<challenge-slug>\     # 各题 Skill 交付目录（含 SKILL.md + 资源 + GITHUB_URL.txt）
├─ out\trace.jsonl              # 生成的 trace
├─ out\<题目目录>\              # 待提交目录
├─ task-08\ ...                 # 平台下载的赛题包
├─ .secrets\                    # 令牌（永不入库、永不上传）
├─ .tools\  .npm-cache\         # 本机安装物
└─ tasks\                       # 分析期抓取的原始证据
```

---

## 11. 实战踩到的坑（实测记录）

### 11.1 PowerShell `*>` 写的是 UTF-16，Node 读不了

`cmd *> file.log` 在 PS 5.1 下产出 UTF-16LE，Node 的 `readFileSync(...,'utf8')` 会拿到 NUL 插空的乱码并抛 `SyntaxError`。

- 要再用 Node 解析 → 用 `2>&1 | Out-File -FilePath X -Encoding utf8`
- 只用 PowerShell 自己读 → `*>` 也行（PS 会自动识别），`Select-String` 正常

### 11.2 正式提交必须显式带 `--harness`

不带时 CLI 会猜，实测被猜成 `opencode`（我们是 DSH）。正式命令：

```powershell
& "D:\AI-for-science\.tools\playground.cmd" submit `
  --challenge-id <ID> `
  --outputs "D:\AI-for-science\out\<题目目录>" `
  --trace "D:\AI-for-science\out\trace.jsonl" `
  --harness "deepseek-harness" --model "deepseek-flash"
```

### 11.3 评分队列很长，别靠反复提交试错

实测 08 题一次提交后 `queuePosition: 5074`、`stage: queued`。自动评分要排很久，所以**不能把提交当作快速反馈手段**，必须先把 Skill 做对再交。

### 11.4 上传配额窗口

实测返回：`limit: 10, used: 1, policy: contest-rounds-v4-20261121-p4, rule_id: bohr-product-p4`，
`window_start: 2026-11-18T13:00:00Z` → `window_end: 2026-11-25T13:00:00Z`（= 北京时间 11-25 21:00）。

即**配额按周重置**，不是整个赛期一共 10 次。北京 21:00 后换新窗口。

### 11.5 git 推 GitHub 必须换 TLS 后端

沙箱里 `git` 默认走 schannel，报 `SEC_E_NO_CREDENTIALS` 无法访问 github.com：

```powershell
git -c http.sslBackend=openssl <cmd>
# 或在仓库内固化：
git -C <repo> config http.sslBackend openssl
```

实测 `git -c http.sslBackend=openssl ls-remote https://github.com/git/git.git HEAD` 返回正常。

### 11.6 GitHub 网页注册在本机自动化浏览器里不可行

`https://github.com/signup` 被 DataDome 硬拦（"访问暂时受限"，页面明示 IP 与网络机器人特征相同），
**不是可解的人机验证**，`ego_captcha`/`ego_wait_for_human` 都识别不到。
但同一台机器用 Node `fetch` 访问 `https://github.com/` 返回 200 —— 即 IP 没被封，挡的是浏览器指纹。
结论：**账号必须在用户自己的常规浏览器里注册**；建好账号后本机可用 git(openssl)+PAT 推送。


---

## 12. 共享内核 v2（2026-11-25 晚新增，10 题共用）

为“写作/翻译类”题目补了两阶段混合框架与产物工具，全部零第三方依赖：

| 模块 | 作用 |
|---|---|
| `lib/hybrid.py` | 两阶段框架：`write_placeholder`（启动即落占位 result.json）、`build_segments`（段落级切分，带 page/block/bbox/section）、`make_batches`/`missing_batches`/`load_generated`/`merge_generated`（Agent 分批生成与覆盖率校验）、`term_candidates`/`find_numbers`/`find_citations`/`find_hedges`（机器可判事实）、`preservation_check`（改写守恒断言）、`finalize`（契约反射 + result/evidence/run_report 落盘）、`map_fields`（声明字段 ↔ 内部产物的别名匹配） |
| `lib/docwrite.py` | 纯标准库写 .docx（OOXML zip）/ .md / .html；`write_editable()` 一次写三份，供审阅与预览 |
| `lib/render.py` | 渲染探测：soffice/libreoffice/office-renderer → PDF；pdftoppm/pdftocairo/mutool/magick/gs → PNG；latexmk/xelatex/pdflatex/tectonic/latex-compiler → PDF。**找不到就如实记录，不伪造** |
| `lib/extract.py`（改） | docx/pptx 增加纯标准库 OOXML 兜底（python-docx 缺失时仍能读） |

配套工具：

| 工具 | 作用 |
|---|---|
| `tools/e2e_check.py` | **独立验收门**：造 /input 形状沙盒（case.json+task.md+environment/ 材料），跑打包后的 runner，校验 result.json 存在/合法/含 case_id 与全部契约字段/status 诚实；另测无契约、无材料两条失败路径（不崩、有产出） |
| `tools/package_skill.py`（改） | 除展平 `_core/{lib,scripts,tests}` 外，还会把题目目录下的 `scripts/`、`tests/`、`resources/` 覆盖进包；继续强制 50 文件 / 500KB / 2MB / 无符号链接 |
| `tools/submit_plan.json` | 9 题提交顺序与 challenge-id 清单 |
| `tools/submit_batch.py` | 批量 dry-run / 提交驱动；逐题记录 attempt_id、bundle 哈希、配额快照到 `out/submit-log.jsonl`；平台拒绝即停 |

### 12.1 打包 + 独立验收（每题）

```powershell
$py = "C:\Users\86188\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
& $py "D:\AI-for-science\tools\package_skill.py" "<challenge-dir>" "D:\AI-for-science\out\submit-<challenge-dir>"
$env:PYTHONPATH = "D:\AI-for-science\out\submit-<challenge-dir>"
& $py "D:\AI-for-science\tools\e2e_check.py" --package "D:\AI-for-science\out\submit-<challenge-dir>"
```

`e2e_check.py` 的 PASS 是提交前的硬门槛：它验的是平台唯一强制的东西（result.json 契约形状），而不是我们自己的测试。

### 12.2 批量提交

```powershell
# 1) 先用当前会话日志生成 trace 并校验
node "D:\AI-for-science\tools\dsh2trace.mjs" --session session-0945b7b3-f4d2-48d1-babb-b0b1e7dbf6e9 --out "D:\AI-for-science\.tmp\trace-final.jsonl"
& "D:\AI-for-science\.tools\playground.cmd" trace validate --trace "D:\AI-for-science\.tmp\trace-final.jsonl"

# 2) 全部 dry-run（不消耗配额、不发提交）
& $py "D:\AI-for-science\tools\submit_batch.py" --trace .tmp/trace-final.jsonl --dry-run

# 3) 正式提交（自动带 --harness deepseek-harness --model deepseek-flash）
& $py "D:\AI-for-science\tools\submit_batch.py" --trace .tmp/trace-final.jsonl --submit
```

### 12.3 Harbor 任务包的公开事实（来自 `playground task guide`）

- 题目包 = `instruction.md` + `task.toml` + `environment/**`（Agent 可见）+ `tests/**`（**隐藏**，`tests/test.sh` 必须写 `/logs/verifier/reward.json`）+ 可选 `solution/`。
- 论文类输入的标准路径是 **`environment/reference/paper.pdf`**，资源清单在 **`environment/resources.yaml`**。
- 有隐藏 verifier 且会产出 reward → 排行榜分数 = reward×100 的推断成立；人工评审（9 题）另行打分。
- 下载接口只能拿 `challenge.json`/`config.json`/`task.md`，**拿不到 `environment/` 与隐藏测试**；`interface-example` 资源同理不可下载。契约字段名只能靠反射，不能提前知道 —— 这再次确认“禁止硬编码 schema”是唯一正确解。

---

## 13. 评分巡检与自动迭代（2026-11-26 18:35 重建）

> 当前任务：`schedule-c7369578-7c3a-4f12-9d8e-306934f31ea9`「评分巡检与出分迭代（18:35 重建）」。
> 周期：每 30 分钟，首次 2026-11-26 19:00（北京），直到 11-29 24:00（rrule `DTSTART;TZID=Asia/Shanghai:20261126T190000` + `FREQ=MINUTELY;INTERVAL=30;UNTIL=20261129T160000Z`）。
> 绑定会话 `session-45b12627-7562-4836-a146-2b7c5ba95d0f`（本会话）。
> 历史：16:10 曾把 schedule-e4ffd09a 改绑到本会话；该任务 18:35 被用户在会话外删除（可能误删），已按用户要求用 schedule_create 重建（新 id）。
> Prompt 已更新为当前状态：20 条在队（16:00 批 + 18:10 批）、2 题已出分、108295 被平台重新排队观察、queuePosition 不可用于估出分时间。

巡检只做一件事：`python tools/check_scores.py` —— 对比 `out/score-snapshot.json`，输出
`changed` / `new_scores` / `evaluation_errors` / `stage_changes` / `queue_min`。
无变化时只回一行，不烧 token；一旦出分，按任务提示词进入「诊断 → 一个具体改进 → 三道门 → 提交」循环。

`out/attempts.json`：**39 个 attempt** 与题目/目录的对应表（巡检和迭代都靠它；18:10 批 113093/113096/113098/113100/113101/113104/113105/113107/113109/113110 已补入）。
`out/iteration-log.md`：出分后的「分数 → 诊断 → 改动 → 新 attempt」记录。
`out/ORGANIZER-REPORT.md`：给主办方的 re-score 请求（16:20 已按最新证据重写：20 次评测 2 成功 18 失败）。

手动巡检（不用等定时任务）：

```powershell
$py = "C:\Users\86188\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
& $py "D:\AI-for-science\tools\check_scores.py" --raw
```

### 13.1 当前状态与出分模型（2026-11-26 16:10 快照）

- 已评测 20 条：**2 成功 / 18 evaluation_error**（108865 阅读15=60、112113 写作02=65.6667）。
- 在队 9 条：112207 写作09、112248 写作04、112313 写作13、112779 阅读15、112927 阅读01、112928 阅读03、112929 阅读10、112930 阅读14、112931 写作08。
- **重要修正**：`scoringQueue.queuePosition`（当前 3950–4300）**不能用来估出分时间**。实测 112113 提交后约 7.3 h 出分、112069 约 3.5 h、112070 约 4.5 h，而队列位一直显示 4000+。实际领取评测的时间与队列位无稳定关系，只能按「提交后 4–10 小时内可能被领取」的经验值观察，且约 10% 才能评测成功。
- 含义：**同一题在赛程内仍可能有 2–3 轮「提交 → 出分」机会**（不是 1 轮），所以重交节奏按 4–10 h 规划，不要按队列位算 40 h。

### 13.2 重建/改绑自动巡检

- 当前任务：`schedule-c7369578-7c3a-4f12-9d8e-306934f31ea9`（"评分巡检与出分迭代（18:35 重建）"，本会话）。
- 重建方式：schedule_create，target=**this_session**，rrule = `DTSTART;TZID=Asia/Shanghai:<下一个时段>` + `RRULE:FREQ=MINUTELY;INTERVAL=<自适应分钟数>;UNTIL=20261129T160000Z`。
- 改绑/改期：schedule_list 取本任务最新 updatedAt → schedule_edit(id, action="update", rrule=…, expectedUpdatedAt=…)。注意**每次投递都会刷新 updatedAt**，所以必须每轮现取。
- 新会话的 trace 用 `node tools/dsh2trace.mjs --session session-45b12627-7562-4836-a146-2b7c5ba95d0f --out .tmp/trace-final.jsonl`。

### 13.3 自适应巡检间隔（2026-11-26 20:12 起，用户已确认的原则）

> **原则：巡检间隔不固定，按队列位数（最前位 queue_min）与历史推进速率动态调整；越靠近队首越密，越远越疏。**

- `tools/check_scores.py` 每轮把一条记录追加到 `out/queue-history.json`（checked_at / queue_min / queue_max / still_queued / scored / changed / 本轮新分数与失败 id）。
- `tools/patrol_interval.py` 读该历史，输出 JSON：`queue_min / rate_per_hour / eta_hours / recommended_interval_min / next_dtstart_local / rrule / reason`。
  分级规则（**2026-11-26 22:31 细化：已知推进速率时以 ETA 档为主，位置档只在队首附近收紧**）：
  - 位置档（速率未知时的兜底）：queue_min ≤300 → 10；≤800 → 20；≤1500 → 30；≤2500 → 45；≤3500 → 60；>3500 → 90 分钟。
  - ETA 档（有速率时主导）：queue_min / 观测推进速率（positions/hour）：≤0.5h → 10；≤1h → 20；≤2h → 30；≤4h → 60；≤8h → 90；>8h → 120 分钟。
  - 收紧：有 ETA 时，queue_min ≤300 再把间隔压到 ≤20 分钟、≤800 压到 ≤45 分钟。
  - 紧急：只要有 attempt 的 stage 不是 queued（已被 worker 领取/评测中），直接 10 分钟。
  - 出现 new_scores / evaluation_errors 的下一轮，覆盖为 15–20 分钟（尽快看后续结果）。
  - 实测例子：22:31 queue_min=2255、推进 26.3 位/小时（ETA≈86h）→ 120 分钟；早先 20:11（速率未知，位置 2316）→ 45 分钟。
- 巡检任务每轮按上述脚本输出的 rrule 调用 schedule_edit 自我重排下一次；`schedule_conflict` 就重新 schedule_list 再试一次。
- 手动查询建议：
  ```powershell
  & $py tools/check_scores.py; & $py tools/patrol_interval.py
  ```
- 例：2026-11-26 20:11 时 queue_min=2316、35 条在队 → 位置档 45 分钟 → 下次 21:00（21:05 起若 ETA 可算会再自动调整）。