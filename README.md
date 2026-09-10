# Broken Arrow Log Analyzer / 断箭日志分析工具

[English](README.en.md)

本地工具，只读《Broken Arrow》的 GameLogs：实时识别当前对局与名单，查询公开统计并计算玩家表现分（仪表盘 0~100，越高越强）；同时用本地数据库追踪你遇到过的玩家，包括相遇战绩、改名历史、封禁提醒。一切呈现在浏览器仪表盘：局前速览、局后复盘与诊断。

## 快速上手

1. 打开 [Releases](../../releases)，下载最新的 `BrokenArrowLogTool-*.zip`
2. 解压到任意目录
3. 双击 `start.bat` 浏览器自动打开仪表盘

无需安装 Python 或任何依赖，包内自带运行时。首次启动自动从 Steam 库定位日志目录。进入对局、名单出现后等几秒，每位玩家的指数自动填入「局前速览」。

## 功能概览

- **实时日志监控**：字节偏移增量读取最新 Gamelog；大厅/对局/名单/FID 状态机与解析遥测
- **Web 仪表盘**：单页布局，吸顶状态栏（阶段/配额/熔断/连接/明暗主题）、本局（局前速览与局后复盘同容器翻动）、历史、诊断（折叠分组）；双击启动、端口自动回退、重复启动复用实例、失败弹窗
- **玩家指数**：队内连续相对评分，含时间衰减、组排降权、贝叶斯收缩与 90% 置信区间；数据不足时显示 N/A 而非猜测
- **关系标注**：本地 SQLite 记录同局历史；点玩家名展开调查（相遇次数、同队/敌对战绩、曾用名、最近同局）；一键标记开黑好友；固定队提示；遇到过的玩家被封禁时横幅提醒
- **对局历史**：「历史」区块列出本地记录的全部对局（时间、地图、FID、胜负、人数、名单），点击行展开玩家明细
- **API 治理**：分接口缓存 TTL、fresh/stale 区分、分级熔断、限流退避、24h 滚动配额、人机验证识别、过期缓存回退、失败后按熔断窗口自动重试；系统代理失效时自动降级为直连重试，频繁切换代理/加速器/TUN 无需重启，工具自动换通道；界面会显示当前通道。
- **工程**：零三方依赖（仅 Python 3 标准库）；端到端冒烟测试覆盖 HTTP 面/名单/日志管线/配额/关系库；GitHub Actions 门禁与一键发版

## 命令行（源码运行）

需要 Python 3.12+：

```text
python web_ui.py                     # Web 仪表盘（默认端口 8765）
python ba_tool.py scan               # 历史扫描全部日志
python ba_tool.py scan --json r.json # 导出 JSON 报告
python ba_tool.py watch              # 实时监控最新日志（JSON 事件）
```

| 参数 | 适用 | 说明 |
|---|---|---|
| `--dir <目录>` | 全部 | GameLogs 目录（省略时自动探测 Steam 库） |
| `--port N` | web_ui.py | 首选端口；占用时自动递增，最多 20 个（默认 8765） |
| `--no-stats` | web_ui.py / watch | 离线模式，零网络请求 |
| `--no-browser` | web_ui.py | 不自动打开浏览器 |
| `--daily-limit N` | web_ui.py / watch | 24 小时滚动 API 配额（默认 500）；命中缓存不计费 |
| `--rel-db <文件>` | web_ui.py | 关系数据库路径（默认程序目录下 `ba-relationships.sqlite`） |
| `--cache <文件>` | web_ui.py / watch | API 缓存路径（默认 `ba-api-cache.json`） |
| `--reset-quota` | web_ui.py / watch | 清零本地 24h 配额计数（你的本地文件，随意重置） |
| `--json <文件>` | scan | 导出 JSON 报告（scan 无网络请求） |
| `--interval N` | watch | 日志轮询间隔秒数（默认 1.5） |
| `--stats-api <URL>` | watch | 公开统计接口地址（默认官方地址） |

> 注意：同一时间只运行一个实例 —— watch 与网页版共享同一个缓存/配额文件，并行运行会让两边内存中的额度互相覆盖（合计可能超限 2 倍）；如需并行，用不同的 `--cache` 路径分开记账。

## 玩家指数

**数据源**：只请求公开统计接口，只携带数字玩家 ID 与对局 ID——

```text
GET /api/analysis/player?stbid=<玩家ID>   # 近期趋势与 ELO 变化
GET /api/analysis/match?matchid=<对局ID>  # 单局 mvpRanking/economy/damage 明细
```

**取样**：最近 30 场候选中筛出 12 场有效对局（有 matchId、ELO 实际变化、完整名单、本人在列）。

**单场**（`scoring.score_match`）：在己方队内按中位数/IQR 稳健化 + logistic 压缩计算连续相对分，合成五个成分——综合 MVP 0.35 / 战斗 0.25 / 效率 0.15 / 团队 0.15 / 赛果 0.10；缺失维度只缩小分母，并输出 coverage 完整度。

**聚合**（`scoring.aggregate`）：

```text
权重     = 0.92^场次序号 × 组排因子（与固定队友共现 ≥3 场 → ×0.6）
有效样本 = (Σw)² / Σw²                        # Kish effective sample size
收缩     = (n_eff × 均值 + 4 × 0.5) / (n_eff + 4)   # 向中性先验做贝叶斯收缩
内部指数 = 10 − 9 × 收缩值（1~10，与上游参考公式同域），附 1.645 × SE × 9 的 90% 置信区间
展示     = 100 × (10 − 指数) / 9    # 仪表盘统一为 0~100，越高越强
```

参考实现 `ba_tool.maggot_index` 保留了上游的队内名次余弦公式，以供对照。

## 架构

```text
GameLogs ──→ LogWatcher（增量读/半行缓冲）──→ LogParser（状态机+遥测）
        ──→ Match 模型 ──→ CLI / State（聚合、关系标注、封禁巡检）
ResilientClient ──→ 公开 API（缓存 / 熔断 / 配额 / 退避）
State ──→ ThreadingHTTPServer 127.0.0.1 ──→ web_ui.html（轮询渲染）
```

`ba_tool.py` 领域模型+解析器+监控器+CLI，含 web_ui.py 与 watch 共用的弹性客户端（缓存/熔断/配额）；`analysis_engine.py` 数据归一化与组排降权；`scoring.py` 传输无关评分引擎；`relationships.py` SQLite 关系库；`web_ui.py` HTTP 服务；`web_ui.html` 仪表盘。

## 隐私与安全

只读 GameLogs；不读取/修改进程内存、不注入进程、没有修改游戏文件。API 查询只携带数字 ID 与对局 ID。服务仅绑定 127.0.0.1。

## 游戏更新兼容性

解析器对未识别日志行静默忽略、对非法字节替换容错，热更新一般无需改动。官方更新后建议核对：`Version:` 字符串、`Player list:` 行格式、`FID:<数字>`、`GameRoom entered/exited`、`GameController dispose`、登录失败行前缀、本地化编码标记不变就无需调整。
## 开发

```text
python smoke_test.py    # 端到端冒烟：HTTP 面 / 名单增删 / 日志管线 / 配额 / 关系库
```

分支：仅 `main` 一条线；CI 对每个 push/PR 跑冒烟；推送 `v*` 标签触发自助发版（出便携 zip 并建 Release）。

发版：`python smoke_test.py && git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z`
