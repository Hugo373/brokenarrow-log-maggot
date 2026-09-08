# Broken Arrow Log Analyzer / 断箭日志分析工具

本地只读的《Broken Arrow》GameLogs 分析器：实时识别当前对局与玩家名单，结合公开统计接口计算玩家综合表现指数（1~10，越低越强），并在浏览器仪表盘中展示局前速览、局后复盘与诊断信息。

Read-only local analyzer for Broken Arrow GameLogs: detects the live match roster, queries public statistics for per-player performance indices, and presents pre-match / post-match dashboards in the browser. It never touches game memory, injects code, installs input hooks, or modifies game files.

## 功能概览 / Features

- **实时日志监控**：字节偏移增量读取最新 Gamelog，大厅/对局/名单/FID 事件状态机，解析遥测
- **CLI**：`scan` 历史扫描（可导出 JSON）、`watch` 实时监控（NDJSON 事件流）
- **Web 仪表盘**：工作情况 / 局前速览 / 局后复盘 / 诊断四页签；熟人黑名单与固定队提示
- **玩家指数**：队内连续相对评分 + 时间衰减 + 组排降权 + 贝叶斯收缩 + 90% 置信区间；数据不足绝不编造，显示 N/A
- **API 治理**：分接口缓存 TTL、fresh/stale 区分、分级熔断、限流退避、24h 滚动配额持久化、人机验证页识别、过期缓存回退
- **工程**：零三方依赖（仅 Python 3 标准库）、端到端冒烟测试、GitHub Actions 门禁

## 快速开始 / Quick Start

双击 `启动BA工具.bat` —— 隐藏控制台启动 Web 界面并自动打开浏览器；端口被占用时自动尝试后续端口（8765–8784）。

```text
python web_ui.py                     # Web 仪表盘（默认端口 8765）
python ba_tool.py scan               # 历史扫描全部日志
python ba_tool.py scan --json r.json # 导出 JSON 报告
python ba_tool.py watch              # 实时监控最新日志（JSON 事件）
```

| 参数 | 说明 |
|---|---|
| `--dir <目录>` | GameLogs 目录（默认开发用 Steam 路径） |
| `--port N` | 首选端口；占用时自动递增，最多尝试 20 个 |
| `--no-stats` | 离线模式，不发起任何 API 请求 |
| `--no-browser` | 不自动打开浏览器 |
| `--daily-limit N` | 24 小时滚动 API 配额（默认 300）；命中缓存不计费 |

默认读取 `D:\SteamLibrary\steamapps\common\broken_arrow\GameLogs`，可用 `--dir` 覆盖。

## 玩家指数 / Player Index

### 数据源

只请求公开统计接口，且只发送数字玩家 ID 或对局 ID，原始日志永不上传：

```text
GET /api/analysis/player?stbid=<玩家ID>   # 近期趋势与 ELO 变化
GET /api/analysis/match?matchid=<对局ID>  # 单局 mvpRanking/economy/damage 明细
```

### 有效对局筛选

一场对局进入计算必须满足：存在 matchId；ELO 有 ≥0.01 的实际变化；对局分析可返回完整 `mvpRanking`；排名表至少 10 人；目标玩家本人在表中。最多扫描最近 30 场候选，凑满 12 场有效对局；不足 12 场时按 `provisional`/`insufficient_data` 标注，绝不用缺失数据凑数。

### 单场评分（scoring.score_match）

不打离散名次，而在己方队内计算连续相对分：每个维度经 中位数/IQR 稳健化 + logistic 压缩到 0~1，加权合成五个成分——

| 成分 | 权重 | 覆盖维度 |
|---|---:|---|
| 综合 MVP | 0.35 | MVP 分数 |
| 战斗 | 0.25 | 击杀 / 伤害 / 交换比 / 生存 |
| 效率 | 0.15 | 交换比 / 损失控制 |
| 团队 | 0.15 | 占点 / 补给 |
| 赛果 | 0.10 | 胜负相对预期 |

缺失维度只缩小分母、不计为差表现，并输出 `coverage` 完整度。

### 聚合（scoring.aggregate）

```text
权重      = 0.92^场次序号 × 组排因子（队友共现 ≥3 场 → ×0.6）
有效样本  = (Σw)² / Σw²            # Kish effective sample size
收缩      = (n_eff × 均值 + 4 × 0.5) / (n_eff + 4)   # 贝叶斯收缩到中性先验
指数      = 10 − 9 × 收缩值
区间      = 指数 ± 1.645 × SE × 9  # 90% 置信区间
```

### 指数档位

| 指数 | 标签 | 含义 |
|---:|---|---|
| ≤ 2.0 | 👑 神 | 近期经常位于队伍前列 |
| 2.0–4.0 | 🦁 团队支柱 | 稳定贡献 |
| 4.0–6.0 | 😐 平平淡淡 | 居中 |
| 6.0–8.0 | 🐛 有点蛆 | 偏后 |
| > 8.0 | 💩 蛆！ | 经常垫底 |

标签为娱乐性描述，非官方评价，更不是反作弊结论。

### 参考实现

`ba_tool.maggot_index` 保留上游参考算法供对照：12 场队内 MVP 名次的平均经余弦曲线映射（平均名次 1/2/3/4/5 ↔ 指数约 1.0/2.3/5.5/8.7/10.0）。线上仪表盘已改用上述评分引擎。

## 架构 / Architecture

```text
GameLogs 目录
    ↓ LogWatcher（最新文件选择 + 字节偏移增量读 + 半行缓冲）
LogParser（逐行状态机 + 健康遥测）
    ↓ 结构化事件
Match 模型 ──→ CLI JSON / 报告
    ↓ roster 防抖 2 秒
MatchAnalysis ──→ ResilientClient ──→ 公开统计 API
                     ├─ Cache（分接口 TTL 持久化）
                     ├─ Quota（24h 滚动配额持久化）
                     └─ 熔断 / 退避 / 验证页识别 / stale 回退
    ↓
State ──→ ThreadingHTTPServer 127.0.0.1 ──→ web_ui.html（1.5s 轮询）
```

- 解析器事件：`GetPersonaName`、大厅进出、Incoming/Outgoing client、`Start loading battle`、`FID`、`Player list` 玩家行、`Room:` 兜底名单、`Deck set to`、`TOTAL POINTS`、连接时长、`GameController dispose`（对局结束）
- 机器人保持负数 ID，永不被静默丢弃，也不发起 API 请求
- 关系库 `relationships.py`：SQLite 记录对局参与者，支持熟人黑名单与同队共现（固定队）启发式

## 隐私与安全 / Privacy

- 只读 GameLogs；不访问进程内存、不注入、不装输入钩子、不改游戏文件
- API 查询只携带数字 ID 与对局 ID；本地缓存/关系库不回写游戏目录
- 服务仅绑定 `127.0.0.1`；离线模式 `--no-stats` 下零网络请求

## 游戏更新兼容性核查 / Update Compatibility Checklist

解析器对未识别日志行采取静默忽略策略，对非法字节做替换容错，因此游戏热更新通常无需修改工具。每次官方更新后建议核对：

1. 新日志 `Version:` 字符串是否变化
2. `Player list:` 后是否仍紧跟标准玩家行
3. FID 是否仍为 `FID:<数字>` 格式
4. `[NET_ROOM] GameRoom entered/exited` 是否仍出现
5. 局后是否仍有 `GameController dispose called`
6. 登录失败详情是否引入多行堆栈或新前缀
7. 新本地化文本是否含特殊编码或换行

以上核心标记不变则工作流无需调整。1.2.0.2 热补丁已按此清单实机核查通过（FID 8647791，地图 Ruda，10 人，正常起止事件完整）。

## 开发与测试 / Development

```text
python smoke_test.py    # 端到端冒烟：HTTP 面 + 黑名单 + 日志管线 + 配额，共 13 项断言
```

- CI：`.github/workflows/smoke.yml`，push/PR 自动执行冒烟测试，失败即拦截
- 分支：`main` 为稳定线，`dev` 为开发线
- 目录结构：

```text
ba_tool.py          领域模型 + 解析器 + 监控器 + API 客户端 + CLI
analysis_engine.py  API 载荷归一化 + 历史表现聚合（组排降权）
scoring.py          传输无关评分引擎
relationships.py    SQLite 关系库（黑名单/共现）
web_ui.py           弹性客户端 + 配额 + 状态聚合 + HTTP 服务
web_ui.html         仪表盘单页
smoke_test.py       端到端冒烟测试
```

运行时产物（`ba-api-cache.json`、`ba-api-quota.json`、`ba-relationships.sqlite`、`scan-report.json`）已被 `.gitignore` 排除。

## English Notes

- Live mode listens to the newest GameLog file with byte-offset incremental reads; roster events trigger a 2-second debounce before querying public per-player statistics.
- Indices are computed from 12 valid ELO-changing matches out of up to 30 recent candidates: continuous team-relative components (median/IQR + logistic), 0.92^i recency decay, ×0.6 down-weighting for games shared with a regular party mate, Bayesian shrinkage over the Kish effective sample size, and a 90% confidence interval. Missing data never fabricates an index.
- The API layer features tiered circuit breakers, per-endpoint cache TTLs, a persisted rolling 24h quota, human-verification page detection, and stale-cache fallback.
- Zero third-party dependencies; `python smoke_test.py` runs a 13-assertion end-to-end smoke test; GitHub Actions enforces it on every push and PR.
