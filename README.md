# sample-kiro-credit-audit

[English](#english) | [中文](#中文)

---

## 中文

一个只读、只用标准库的 Python 示例脚本，用来查看本机 Kiro CLI 消耗了多少额度（credits），以及花在了哪些模型、会话、项目和工具上。

> [!IMPORTANT]
> 这是一个用于演示本地用量分析方法的示例，不是 Kiro 的官方功能、计费系统或支持工具。它按现状提供，不属于 AWS Support 的支持范围；输出应以 Kiro 官方 `/usage` 和公开文档为准。在生产工作流中使用前，请自行完成审查和测试。

> 脚本只读取本机 Kiro CLI 的会话记录目录（默认 `~/.kiro/sessions/cli/`）。它不联网、不写入任何文件，也不打开 `data.sqlite3` 或任何凭据文件。当前只覆盖 Kiro CLI，不覆盖 Kiro IDE。

### 使用边界

- **安全**：脚本只读取本地 `.json`/`.jsonl` 会话记录，不打开 `data.sqlite3` 或任何凭据文件，也不会上传任何数据。输出可能包含会话 ID 和本地项目目录名，公开分享前请检查并脱敏。请勿在 GitHub issue 中提交真实会话文件、提示词、客户数据或凭据；潜在安全问题请通过 [AWS 漏洞报告渠道](https://aws.amazon.com/security/vulnerability-reporting/) 提交。
- **兼容性**：脚本依赖 Kiro CLI 当前写入的本地记录格式；该格式并非稳定公共 API，未来版本可能变化。
- **成本**：脚本离线运行，不调用 AWS 服务，运行本身不会产生 AWS 费用。
- **清理**：无需清理；脚本不会创建文件或云端资源。
- **支持**：仅提供社区 best-effort 维护；功能问题可通过本仓库的 GitHub Issues 报告。

### 快速开始

需要 Python 3.10 或更高版本，不需要安装第三方包。

推荐先下载脚本、检查内容，再运行：

```bash
# Linux / macOS / WSL
curl -fsSLO https://raw.githubusercontent.com/aws-samples/sample-kiro-credit-audit/main/kiro_credit_audit.py
# 检查 kiro_credit_audit.py 后再执行
KIRO_SESSIONS_DIR=~/.kiro/sessions/cli python3 kiro_credit_audit.py --since 2026-09-01T00:00:00Z --tz-offset 8 --tools
```

```powershell
# Windows PowerShell
irm https://raw.githubusercontent.com/aws-samples/sample-kiro-credit-audit/main/kiro_credit_audit.py -OutFile kiro_credit_audit.py
# 检查 kiro_credit_audit.py 后再执行；PowerShell 5.1 请先设置 UTF-8 输出
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:KIRO_SESSIONS_DIR = "$env:USERPROFILE\.kiro\sessions\cli"; python kiro_credit_audit.py --since 2026-09-01T00:00:00Z --tz-offset 8 --tools
```

命令里的每一项都可以按需修改或删掉：

- `KIRO_SESSIONS_DIR=…`：会话目录。示例给的就是默认位置，用默认目录时可以整段删掉；要放在 `python3` 前面，不能放在 `curl` 前面。
- `--since 2026-09-01T00:00:00Z`：只统计该时间点之后的轮次（ISO 8601，不带时区按 UTC）。改成本计费月 1 日即可和 `/usage` 对账；删掉则统计磁盘上的全部记录。
- `--tz-offset 8`：本地时区相对 UTC 的小时数，可为小数。默认就是 8，UTC+8 地区可删掉。
- `--tools`：额外解析 `.jsonl` 做工具调用归因和图片统计，会慢一些；不需要时删掉。
- 把参数全部换成 `-h` 可查看内置帮助。

脚本只有一个文件，运行前可以先打开链接看一遍源码。

或者克隆后运行：

```bash
git clone https://github.com/aws-samples/sample-kiro-credit-audit.git
cd sample-kiro-credit-audit
python3 kiro_credit_audit.py            # Linux / macOS / WSL
python  kiro_credit_audit.py            # Windows
```

常用参数：

```bash
python3 kiro_credit_audit.py --since 2026-09-01T00:00:00Z   # 只看某个时间点之后
python3 kiro_credit_audit.py --tz-offset 8                   # 本地时区，默认 UTC+8，可为小数
python3 kiro_credit_audit.py --tools                         # 额外做工具调用归因（解析 .jsonl，慢一些）
KIRO_SESSIONS_DIR=/path/to/sessions/cli python3 kiro_credit_audit.py   # 指定会话目录
```

### 输出里优先看什么

1. 「汇总」段首行的 `credits=`：本机记录中的总额度。
2. 「当前计费周期」段：本计费月（UTC）的额度，用于和 Kiro CLI 里的 `/usage` 对账。
3. 「按模型」「按会话」「按项目目录」三张表：额度主要花在哪里。
4. 「额度最高的 10 轮」：哪些任务值得进一步检查。

其余表格（按 UTC 日期、按本地日期、上下文水位、循环深度、轮次耗时、请求额度分布）用于深入分析；工具归因需加 `--tools`。

### 输出字段解读

脚本按下面的顺序打印各段。表格里的 `cr` 都是 credits 的缩写。

**数据目录**：实际扫描的会话目录列表。WSL 下可能列出多个（WSL 家目录和 `/mnt/c/Users/<用户>`），用 `KIRO_SESSIONS_DIR` 指定时只有一个。

**汇总**

| 字段 | 含义 |
|---|---|
| `credits=` | 本机所有轮次的额度总和，直接来自会话记录的 `metering_usage`，不是估算。 |
| `turns=` | 统计到的轮数。一轮 = 用户发一条消息到模型回复结束。 |
| `sessions=35/938` | 有轮次记录的会话数 / 会话文件总数。子代理会话通常没有轮次，所以后者远大于前者是正常的；这个数字不受 `--since` 影响。 |
| `deduped=` | 同一个会话文件在多个目录重复出现（例如 WSL 和 Windows 两侧各一份）时被排除的轮次数。大于 0 时说明脚本只算了一次。 |
| `tz=` | 本地时区标签，来自 `--tz-offset`。 |
| `! skipped=` / `! denied=` / `! bad_turns=` / `! no_ts=` | 只在有异常时出现：无法解析为 JSON 对象的文件数 / 无读取权限的文件数 / 结构异常被跳过的轮次数 / 没有时间戳的轮次数。前三种会让总额偏低；`no_ts` 只在带 `--since` 时被跳过，否则计入总额但归入按日期表的 `?` 行。 |
| 统计范围 | 最早到最晚的轮次结束时间（UTC），跨度天数，以及其中有用量的天数。 |
| 活跃日均 | 有用量的日期的平均额度，不含没有时间戳的轮次。 |

**当前计费周期（用于和 /usage 对账）**

| 字段 | 含义 |
|---|---|
| 当前计费周期 | 从本月 1 日 00:00 UTC 起的额度，可直接与 Kiro CLI 里 `/usage` 对照。若 `--since` 晚于月初，这一行会改成「自 --since … 起」并提示不能对账。 |
| 按当前速度估算整月 | 本月已过天数线性外推到整月，样本不足 5 天时脚本会提示不要当预测。 |
| 计费边界 | 每月 1 日 00:00 UTC 换算成本地时间是几点。 |
| 对账提示 | 本机结果与 `/usage` 不一致时的常见原因。 |

**按 UTC 日期 / 按本地日期 / 按模型 / 按会话 / 按项目目录**：五张表结构相同。

| 列 | 含义 |
|---|---|
| `key` | 分组键：日期、模型名（后缀 `x2.2` 是内置倍率表的静态参考值，`x?` 表示未知）、会话 ID 前 8 位、或项目目录名（取 `cwd` 的最后一级）。 |
| `turns` | 该组轮数。 |
| `credits` | 该组额度合计。 |
| `cr/turn` | 每轮平均额度。 |
| `cr/cycle` | 有工具循环的轮次的每循环平均额度。带 `*` 表示该组有循环数据的轮次不足一半，样本不够，不要横向比较；`n/a` 表示没有任何工具循环。 |
| `cyc/turn` | 有工具循环的轮次的每轮平均循环数。 |
| `cyc cov` | 有工具循环的轮次数 / 该组轮数。 |

按 UTC 日期表用于和 `/usage` 对账；按本地日期表按 `--tz-offset` 换算，方便看工作日。没有时间戳的轮次会归入 `?` 行。

**上下文水位与每循环额度**：把有工具循环的轮次按轮开始时的上下文占用分桶。

| 列 | 含义 |
|---|---|
| `ctx` | 上下文占用区间，`90-100%` 为最后一桶。 |
| `turns` | 该区间轮数。 |
| `med cr/cyc` | 该区间每循环额度的中位数，优先看这一列。 |
| `pooled` | 该区间总额度 / 总循环数，会受循环深度差异影响。 |
| `cyc/turn` | 该区间每轮平均循环数。 |

这张表只反映相关性，不能据此下因果结论。

**循环深度与额度占比**：有工具循环的轮次按循环数分为 1、2-5、6-10、11-20、21+ 五档，给出每档轮数和额度及其占比。表前的「口径说明」交代有多少轮没有工具循环（cycle=0），它们计入总额和 `cr/turn`，不计入 `cr/cycle`。

**轮次耗时与额度**：按每轮耗时分为 <1min、1-5min、5-15min、>15min 四档。耗时只是任务复杂度的代理指标，Kiro 不按时间计费。

**模型请求额度分布**：每条 `metering_usage` 对应一次已计费的模型请求。`p50`/`p90`/`p99`/`max`/`mean` 是单次请求额度的分位数，最后一行是额度最高的约 1% 请求占总额的比例。

**工具调用归因（仅 `--tools`）**

| 字段 | 含义 |
|---|---|
| 注意：另有 N 个会话… | 转写里有模型回复、但 `.json` 缺失或没有轮次元数据的会话数。它们自身的轮次额度脚本看不到，是本机低于 `/usage` 的一个可能原因。 |
| 关联覆盖 | 关联到工具调用的轮数 / 总轮数，及对应额度占比；`toolUse a/b` 是归到具体轮次的工具调用次数 / 全部转写中的工具调用次数。 |
| `tool` / `calls` / `credits` / `cr/call` / `share` | 工具名 / 调用次数 / 归因额度 / 每次调用平均归因额度 / 占已覆盖额度比例。归因方式是把一轮的额度平均分给该轮所有工具调用，只用于识别高频、高额度工具，不代表工具有独立定价。 |
| [图片] | 转写中 image 项的出现次数、去重后的张数、原始字节的中位和最大值。含工具返回的图片，不含上下文压缩快照里的历史副本。 |

**额度最高的 10 轮**：每行依次是本地时间（月-日 时）、会话 ID 前 8 位、模型、该轮额度 `cr`、循环数 `cyc`、请求数 `req`、轮开始时的上下文占用 `ctx`。带 `(model inferred)` 表示该轮没有记录模型，用的是会话级的模型选择。

### 和 `/usage` 对账

计费周期从每月 1 日 00:00 UTC 开始，脚本会按 `--tz-offset` 换算成本地时间打印「计费边界」。若 `--since` 晚于本月 1 日 00:00 UTC，「当前计费周期」段只统计 `--since` 之后的部分并会明确标注，此时不要拿它和 `/usage` 对账。本机结果与 `/usage` 不一致时，先检查：

- 是否在其它电脑或 CI 中使用过 Kiro CLI。Windows 与 WSL 双环境请在 WSL 内运行，脚本会同时读取两侧。
- 是否有本地会话记录被删除。
- 是否有会话的轮次元数据没有落盘。这类会话的 `.jsonl` 里有模型回复，但对应 `.json` 缺失或没有轮次元数据，脚本看不到它们自身的轮次额度；`--tools` 会报告这类会话的数量。
- `/usage` 是否有更新延迟。
- 「汇总」行里的 `deduped` 是否大于 0（同一会话文件在多个目录重复出现时，脚本只算一次）。

### 已知限制

- `--tools` 的归因额度是估算：一轮的额度平均分给该轮所有工具调用，不代表工具有独立定价。
- 「按模型」表里的倍率来自脚本内置的静态表，只用于标注，不参与计算。模型不在表中时，若该轮模型与会话记录中的当前模型一致则借用会话记录自带的倍率，否则显示 `x?`。倍率调整后需要手动更新 `MULT`。
- 耗时、上下文水位、循环深度只用于定位复杂任务，脚本不据此计算额度。

### 开发

```bash
python3 -W error -m py_compile kiro_credit_audit.py
python3 -m unittest discover -s tests -v
```

测试全部使用临时目录里的合成数据，不会读取 `~/.kiro`。

---

## English

A read-only, standard-library-only Python sample that shows how many credits your local Kiro CLI has consumed, broken down by model, session, project directory and tool.

> [!IMPORTANT]
> This sample demonstrates a method for analyzing local usage records. It is not an official Kiro feature, billing system, or support tool. It is provided as-is and is not covered by AWS Support; treat Kiro's official `/usage` output and public documentation as authoritative. Review and test it before using it in production workflows.

> It only reads the local Kiro CLI session directory (default `~/.kiro/sessions/cli/`). No network access, no writes, and it never opens `data.sqlite3` or credential files. Kiro CLI only; the Kiro IDE is not covered.

### Usage boundaries

- **Security:** The script reads only local `.json`/`.jsonl` session records, never opens `data.sqlite3` or credential files, and uploads no data. Output may contain session IDs and local project directory names; review and redact it before sharing publicly. Do not attach real session files, prompts, customer data, or credentials to GitHub issues. Report potential security vulnerabilities through [AWS Vulnerability Reporting](https://aws.amazon.com/security/vulnerability-reporting/).
- **Compatibility:** The script depends on the local record format currently written by Kiro CLI. That format is not a stable public API and may change in future releases.
- **Cost:** The script runs offline and invokes no AWS services, so running it does not incur AWS charges.
- **Cleanup:** No cleanup is required; the script creates no files or cloud resources.
- **Support:** This sample is maintained on a community, best-effort basis. Use this repository's GitHub Issues for functional problems.

### Quick start

Python 3.10+ and no third-party packages.

Download the script, inspect it, and then run it:

```bash
# Linux / macOS / WSL
curl -fsSLO https://raw.githubusercontent.com/aws-samples/sample-kiro-credit-audit/main/kiro_credit_audit.py
# Inspect kiro_credit_audit.py before running it
KIRO_SESSIONS_DIR=~/.kiro/sessions/cli python3 kiro_credit_audit.py --since 2026-09-01T00:00:00Z --tz-offset 8 --tools
```

```powershell
# Windows PowerShell
irm https://raw.githubusercontent.com/aws-samples/sample-kiro-credit-audit/main/kiro_credit_audit.py -OutFile kiro_credit_audit.py
# Inspect kiro_credit_audit.py before running it; PowerShell 5.1 also needs UTF-8 output
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:KIRO_SESSIONS_DIR = "$env:USERPROFILE\.kiro\sessions\cli"; python kiro_credit_audit.py --since 2026-09-01T00:00:00Z --tz-offset 8 --tools
```

Adjust or drop any part of it:

- `KIRO_SESSIONS_DIR=…`: session directory. The example is the default location, so drop it unless you need another directory; it must precede `python3`, not `curl`.
- `--since 2026-09-01T00:00:00Z`: only count turns at or after this time (ISO 8601, UTC if no offset). Set it to the 1st of the current month to reconcile with `/usage`; drop it to count everything on disk.
- `--tz-offset 8`: local UTC offset in hours, fractional allowed. 8 is the default.
- `--tools`: also parse the `.jsonl` transcripts for tool-call attribution and image stats (slower); drop it if not needed.
- Replace all options with `-h` for the built-in help.

It is a single file, so feel free to open the URL and read it first.

Or clone and run:

```bash
git clone https://github.com/aws-samples/sample-kiro-credit-audit.git
cd sample-kiro-credit-audit
python3 kiro_credit_audit.py            # Linux / macOS / WSL
python  kiro_credit_audit.py            # Windows
```

Options:

| Option | Meaning |
|---|---|
| `--since TIMESTAMP` | Only count turns that ended at or after this time (ISO 8601; UTC if no offset). If it is later than the 1st of the current month 00:00 UTC, the "current billing period" section covers only the part after `--since` and says so; do not reconcile that number with `/usage`. |
| `--tz-offset HOURS` | Local UTC offset in hours for local-time output (by-local-date table, billing boundary, top-10 timestamps); may be fractional. Default `8`. |
| `--tools` | Also attribute credits to tool calls by parsing the `.jsonl` transcripts (slower). |
| `KIRO_SESSIONS_DIR` | Environment variable: use this session directory only. |

### What to look at first

1. `credits=` in the summary line: total credits found on this machine.
2. The "current billing period" section: this UTC month, for reconciling with `/usage` inside Kiro CLI.
3. The by-model / by-session / by-project tables: where the credits went.
4. The top-10 most expensive turns.

### Understanding the output

Sections appear in the order below. `cr` is short for credits everywhere.

**Data directories**: the session directories actually scanned. Under WSL there may be several (WSL home plus `/mnt/c/Users/<user>`); with `KIRO_SESSIONS_DIR` there is exactly one.

**Summary**

| Field | Meaning |
|---|---|
| `credits=` | Sum of all turns on this machine, read straight from `metering_usage`; not an estimate. |
| `turns=` | Number of turns counted. A turn is one user message through the end of the model's reply. |
| `sessions=35/938` | Sessions with turn metadata / total session files. Sub-agent sessions usually have no turns, so the second number is normally much larger; unaffected by `--since`. |
| `deduped=` | Turns dropped because the same session file appeared under several roots (e.g. WSL and Windows). |
| `tz=` | Local time-zone label from `--tz-offset`. |
| `! skipped=` / `! denied=` / `! bad_turns=` / `! no_ts=` | Only shown when non-zero: files that are not valid JSON objects / unreadable files / malformed turn records / turns without a timestamp. The first three lower the total; `no_ts` turns are skipped only with `--since`, otherwise they count toward the total and show as a `?` row in the per-date tables. |
| Range | Earliest to latest turn end time (UTC), span in days, and how many of those days had usage. |
| Per active day | Average credits over days with usage; timestamp-less turns excluded. |

**Current billing period (for `/usage`)**

| Field | Meaning |
|---|---|
| Current billing period | Credits since the 1st of this month 00:00 UTC, directly comparable with `/usage` in Kiro CLI. If `--since` is later than the month start the line changes to "since --since …" and warns that it cannot be reconciled. |
| Full-month estimate | Linear extrapolation from days elapsed; flagged as unreliable when fewer than 5 days have passed. |
| Billing boundary | The 1st 00:00 UTC expressed in your local time. |
| Reconciliation hints | Common reasons the local total differs from `/usage`. |

**By UTC date / by local date / by model / by session / by project**: five tables with the same columns.

| Column | Meaning |
|---|---|
| `key` | Group key: date, model (the `x2.2` suffix is the static multiplier from the built-in table, `x?` = unknown), first 8 chars of the session id, or project directory (last path component of `cwd`). |
| `turns` | Turns in the group. |
| `credits` | Credits in the group. |
| `cr/turn` | Average credits per turn. |
| `cr/cycle` | Average credits per cycle over turns that had tool cycles. A trailing `*` means fewer than half the turns had cycle data, so do not compare across rows; `n/a` means no tool cycles at all. |
| `cyc/turn` | Average cycles per turn over turns that had tool cycles. |
| `cyc cov` | Turns with tool cycles / turns in the group. |

Use the UTC table to reconcile with `/usage`; the local-date table applies `--tz-offset` for a working-day view. Turns without a timestamp land in a `?` row.

**Context usage vs credits per cycle**: turns with tool cycles, bucketed by context usage at the start of the turn.

| Column | Meaning |
|---|---|
| `ctx` | Context-usage bucket; `90-100%` is the last one. |
| `turns` | Turns in the bucket. |
| `med cr/cyc` | Median credits per cycle in the bucket; read this column first. |
| `pooled` | Bucket credits / bucket cycles; sensitive to differences in cycle depth. |
| `cyc/turn` | Average cycles per turn in the bucket. |

This table shows correlation only; do not draw causal conclusions from it.

**Cycle depth vs credit share**: turns with tool cycles split into 1, 2-5, 6-10, 11-20 and 21+ cycles, with turn and credit counts and shares. The note above the table says how many turns had no tool cycles (cycle=0); those count toward the total and `cr/turn` but not `cr/cycle`.

**Turn duration vs credits**: <1min, 1-5min, 5-15min, >15min. Duration is only a proxy for task complexity; Kiro does not bill by time.

**Per-request credit distribution**: one `metering_usage` entry per billed model request. `p50`/`p90`/`p99`/`max`/`mean` describe single-request credits; the last line is the share of the most expensive ~1% of requests.

**Tool-call attribution (`--tools` only)**

| Field | Meaning |
|---|---|
| Note: N more sessions… | Sessions whose transcript has model replies but whose `.json` is missing or has no turn metadata. Their own turn credits are invisible to the script, one possible reason for a local total below `/usage`. |
| Coverage | Turns matched to tool calls / all turns, with the credit share; `toolUse a/b` is tool calls attributed to a turn / all tool calls in the transcripts. |
| `tool` / `calls` / `credits` / `cr/call` / `share` | Tool name / number of calls / attributed credits / average per call / share of covered credits. A turn's credits are split evenly across its tool calls, so this identifies frequent and expensive tools but is not a per-tool price. |
| [images] | Image items in transcripts, distinct images after hashing, median and max raw size. Includes images returned by tools; excludes copies inside context-compaction snapshots. |

**Top 10 most expensive turns**: local time (MM-DD HHh), first 8 chars of the session id, model, credits `cr`, cycles `cyc`, requests `req`, context usage at turn start `ctx`. `(model inferred)` means the turn did not record a model and the session-level choice was used.

### Reconciling with `/usage`

The billing period starts on the 1st of each month at 00:00 UTC. If the local total differs from `/usage`, check whether Kiro CLI was used on other machines or in CI, whether local session files were deleted, whether some sessions have a transcript but a `.json` that is missing or has no turn metadata (`--tools` reports how many), whether `/usage` is lagging, and whether `deduped` in the summary line is non-zero (the same session file seen under several roots is counted once).

### Known limitations

- Tool attribution is an estimate: a turn's credits are split evenly across its tool calls.
- Model multipliers come from a static table inside the script and are display-only. For models missing from the table, the session's own multiplier is used when the turn's model matches the session's current model; otherwise `x?` is shown.
- Duration, context usage and cycle depth help locate expensive tasks; credits are never computed from them.

### Development

```bash
python3 -W error -m py_compile kiro_credit_audit.py
python3 -m unittest discover -s tests -v
```

Tests use synthetic data in a temp directory and never touch `~/.kiro`.

## Security

This sample reads local Kiro CLI records and may print session IDs and local project directory names. Review and redact output before sharing it publicly. Do not submit real session files, prompts, customer data, or credentials in GitHub issues. Report potential security vulnerabilities through [AWS Vulnerability Reporting](https://aws.amazon.com/security/vulnerability-reporting/).

## License

This project is licensed under the [MIT-0 License](LICENSE).
