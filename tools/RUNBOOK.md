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

