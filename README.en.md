# Broken Arrow Log Analyzer

[中文](README.md)

A read-only local tool for Broken Arrow GameLogs: it detects the live match roster, queries public statistics to compute a per-player performance score (dashboard 0–100, higher is stronger), and keeps a local database of the players you meet, including encounter records, name history, ban alerts. Everything shows up in a browser dashboard: pre-match board, post-match review, diagnostics.

## Quick Start

1. Open [Releases](../../releases) and download the latest `BrokenArrowLogTool-*.zip`
2. Extract it anywhere
3. Double-click `start.bat` and the dashboard opens in your browser automatically

No Python or other dependencies to install; a runtime is bundled. On first launch the GameLogs folder is located automatically via your Steam libraries. Enter a match, wait a few seconds after the roster appears, and every player's index shows up in the pre-match tab.

## Features

- **Live log monitoring**: byte-offset incremental reads of the newest Gamelog; lobby/match/roster/FID state machine with parser telemetry
- **Web dashboard**: single-page layout, sticky status bar (stage/quota/breaker/connection/light-dark theme), match pane (pre-match board and post-match review flip in the same container), history, diagnostics (collapsible groups); double-click launch, automatic port fallback, relaunch reuses the running instance, visible error dialog on failure
- **Player index**: continuous team-relative scoring with recency decay, party down-weighting, Bayesian shrinkage and a 90% confidence interval; shows N/A instead of guessing when data is insufficient
- **Relationship notes**: local SQLite history of everyone you met; click a name to investigate (encounters, W/L as ally and enemy, former names, recent games); one-click friend marks; party detection; banner alerts when someone you met gets banned
- **Match history**: the "history" tab lists every locally recorded game (time, map, FID, W/L, player count, roster); click a row to expand player details
- **API governance**: per-endpoint cache TTLs, fresh/stale distinction, tiered circuit breaker, throttled retries, persisted rolling 24h quota, human-verification page detection, stale-cache fallback, automatic retry aligned to breaker windows
- **Engineering**: zero third-party dependencies (Python 3 stdlib only); end-to-end smoke test covering the HTTP surface, list edits, log pipeline, quota and the relationship store; GitHub Actions gate plus tag-driven releases

## Command line (from source)

Requires Python 3.12+:

```text
python web_ui.py                     # web dashboard (default port 8765)
python ba_tool.py scan               # scan all historical logs
python ba_tool.py scan --json r.json # export a JSON report
python ba_tool.py watch              # live-monitor the newest log (JSON events)
```

| Flag | Meaning |
|---|---|
| `--dir <path>` | GameLogs directory (auto-detected via Steam libraries when omitted) |
| `--port N` | preferred port; the next 19 ports are tried when taken |
| `--no-stats` | offline mode, zero network requests |
| `--no-browser` | do not auto-open the dashboard |
| `--daily-limit N` | rolling 24h API budget (default 500); cache hits are free |
| `--rel-db <file>` | relationship database path (default `ba-relationships.sqlite` next to the program) |
| `--cache <file>` | API cache path (default `ba-api-cache.json`) |

## Player index

**Data source**: only public statistics endpoints are called, carrying only a numeric player or match ID—

```text
GET /api/analysis/player?stbid=<playerID>   # recent trend & ELO changes
GET /api/analysis/match?matchid=<matchID>   # per-match mvpRanking/economy/damage
```

**Sampling**: up to 30 recent candidates, of which 12 valid matches are required (has matchId, real ELO change, complete mvpRanking, player present).

**Per match** (`scoring.score_match`): continuous team-relative components via median/IQR robust scaling and logistic compression — MVP 0.35 / combat 0.25 / efficiency 0.15 / teamwork 0.15 / result 0.10. Missing fields shrink the denominator instead of counting as bad play, with a coverage value reported.

**Aggregation** (`scoring.aggregate`):

```text
weight      = 0.92^age × party factor (≥3 games with the same teammate → ×0.6)
effective n = (Σw)² / Σw²                          # Kish effective sample size
shrinkage   = (n_eff × mean + 4 × 0.5) / (n_eff + 4)  # Bayesian pull toward 0.5
index       = 10 − 9 × shrunk mean (1–10 domain, upstream-compatible), 90% CI = 1.645 × SE × 9
display     = 100 × (10 − index) / 9   # dashboard shows 0–100, higher is stronger
```

`ba_tool.maggot_index` keeps the upstream cosine rank formula as a reference implementation for comparison.

## Architecture

```text
GameLogs ──→ LogWatcher (incremental reads / partial-line buffer) ──→ LogParser (state machine + telemetry)
        ──→ Match model ──→ CLI / State (aggregation, relationship tags, ban watch)
ResilientClient ──→ public API (cache / breaker / quota / backoff)
State ──→ ThreadingHTTPServer 127.0.0.1 ──→ web_ui.html (polling renderer)
```

`ba_tool.py` domain model, parser, watcher and CLI · `analysis_engine.py` payload normalization and party down-weighting · `scoring.py` transport-independent scoring engine · `relationships.py` SQLite relationship store · `web_ui.py` resilient client and HTTP service · `web_ui.html` dashboard.

## Privacy & safety

Reads GameLogs only; no process-memory reads or writes, no injection, no game-file writes. API requests carry only numeric player/match IDs. The server binds 127.0.0.1 only.

## Game-update compatibility

The parser silently ignores unrecognized log lines and replaces malformed bytes, so hot patches rarely require changes. After an official update, check: the `Version:` string, `Player list:` row format, `FID:<digits>`, `GameRoom entered/exited`, `GameController dispose`, login-failure line prefixes; no adjustment needed as long as the localization-encoding markers stay unchanged.
## Development

```text
python smoke_test.py    # end-to-end smoke: HTTP surface / list edits / log pipeline / quota / relationship store
```

Branches: `main` is stable, `dev` for development. CI runs the smoke test on every push/PR; pushing a `v*` tag builds the portable zip and cuts a GitHub Release.

Release: `python smoke_test.py && git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z`
