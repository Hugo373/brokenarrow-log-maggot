# Broken Arrow Log Tool — Architecture / 断箭日志工具——架构

## Scope / 范围

**English:** The tool is a local, read-only GameLogs analyzer. It does not use process memory, DLL injection, input hooks, screen capture, or game-file writes.

**中文：** 本工具是本地只读的 GameLogs 分析器。它不访问进程内存、不使用 DLL 注入、不安装输入钩子、不截取屏幕，也不写入游戏文件。

## Components / 组件

`ba_tool.py` contains four layers:

`ba_tool.py` 包含四个层次：

1. `Player` and `Match` dataclasses: stable JSON-friendly domain model.
   `Player` 和 `Match` 数据类：稳定、适合 JSON 的领域模型。
2. `LogParser`: line-oriented state machine for lobby and match markers.
   `LogParser`：根据大厅和对局标记运行的逐行状态机。
3. `LogWatcher`: newest-log selection plus byte-offset incremental reads and partial-line buffering.
   `LogWatcher`：选择最新日志，通过字节偏移增量读取，并缓存不完整的行。
4. CLI: historical scan and live JSON-event monitor.
   CLI：历史扫描和实时 JSON 事件监控。

## Event model / 事件模型

The parser recognizes the following events.

解析器识别以下事件：

- `GetPersonaName` — local player name / 本机玩家名
- `Enter to lobby` / `Exit lobby` — lobby lifecycle / 大厅生命周期
- `Incoming client` / `Outgoing client` — lobby participants / 大厅参与者
- `Start loading battle` — map and scenario / 地图和场景
- `[NET_ROOM] GameRoom entered` / `exited` — network room lifecycle / 网络房间生命周期
- `FID` — match identifier / 对局标识符
- `Player list` and player rows — final roster / 最终玩家列表
- `Room: (GameRoom..., Client...)` — fallback roster rows / 备用玩家列表
- `Deck set to` — active deck / 当前卡组
- `TOTAL POINTS` — local total score / 本机总分
- `[NET_ROOM] Connection closed ... LiveTime` — connection duration / 连接时长
- `GameController dispose called` — match termination / 对局结束

## Data flow / 数据流

```text
GameLogs / 游戏日志
    ↓
LogWatcher / 日志监控器
    ↓ appended lines / 追加行
LogParser / 日志解析器
    ↓ structured events / 结构化事件
Match model / 对局模型
    ↓
CLI JSON or report / CLI JSON 或报告
```

## Output / 输出

**English:** `scan --json` produces a report containing the source directory, detected local name, completed-match count, and structured match records. `watch` emits newline-delimited JSON events suitable for a future UI.

**中文：** `scan --json` 会生成包含源目录、本机玩家名、已完成对局数量和结构化对局记录的报告。`watch` 输出以换行分隔的 JSON 事件，可供未来界面使用。

## Read-only guarantees / 只读保证

**English:** Game files and source logs are opened for reading only. The optional JSON report is written to the selected output path and is separate from the game installation.

**中文：** 游戏文件和源日志只以读取方式打开。可选的 JSON 报告会写入用户指定的输出路径，并与游戏安装目录分离。

## Verified local data / 已验证本地数据

The implementation was exercised against the installed GameLogs directory.

本实现已针对本机安装的 GameLogs 目录执行验证：

- 462 log files scanned / 扫描 462 个日志文件。
- 262 completed match records detected / 检测到 262 条已完成对局记录。
- Recent FID `8642058` detected on map `Daugavpils` with 10 players.
  检测到最近的 FID `8642058`，地图为 `Daugavpils`，包含 10 名玩家。
- Negative bot IDs are preserved / 保留机器人的负数 ID。
- The incremental watcher selected `Gamelog__2026_09_07__17_10.log` and parsed its completed match.
  增量监控器选择了该日志，并解析出其中已完成的对局。

## Player index pipeline / 玩家指数流程

**English:** When roster lines arrive in live mode, a two-second debounce lets the final roster settle. Human players are then queried through the public profile endpoint. For each player, the engine selects up to 30 recent matches with a real ELO change and fetches match analysis until 12 valid matches are available. Each match is scored continuously inside the player's own team (`scoring.score_match`: median/IQR-robust, logistic-compressed components for MVP, combat, efficiency, teamwork and result context; missing data shrinks the denominator instead of counting as poor play). Aggregation (`scoring.aggregate`) applies 0.92^i recency decay, a ×0.6 down-weight for games played with a regular party mate (co-occurrence ≥ 3 inside the sample), Bayesian shrinkage toward 0.5 over the Kish effective sample size (`n_eff = (Σw)² / Σw²`), and reports a 90% confidence interval. The legacy cosine ranking formula survives only as the reference implementation `ba_tool.maggot_index`.

**中文：** 实时模式收到玩家列表后，会延迟两秒等待最终名单稳定，然后通过公开档案接口查询真人玩家。每名玩家最多选择最近 30 场且 ELO 确实变化的对局，继续获取对局分析，直到凑满 12 场有效对局。单场按玩家在己方队内的连续相对表现计分（`scoring.score_match`：基于中位数/IQR 稳健化与 logistic 压缩，覆盖综合/战斗/效率/团队/赛果五个成分；缺失数据只缩小分母，绝不当作差表现）。聚合（`scoring.aggregate`）按 0.92^i 时间衰减加权，对与固定队友共现 ≥3 场的组排局降权 ×0.6，再围绕 Kish 有效样本量（`n_eff = (Σw)² / Σw²`）向 0.5 做贝叶斯收缩，并输出 90% 置信区间。旧版队内名次余弦公式仅保留为参考实现 `ba_tool.maggot_index`。

The tool never substitutes a guessed index when public data is missing. It reports `N/A` or `API unavailable` instead.

如果公开数据缺失，工具不会猜测指数，而是显示 `N/A` 或 `API unavailable / API不可用`。
