#!/usr/bin/env python3
"""End-to-end smoke test: boots web_ui.py against a synthetic GameLogs directory.

Zero network, zero game installation required:
- HTTP surface: '/', '/api/state', 404, POST '/api/blacklist' (valid + malformed).
- Log pipeline: appended lines must reach the parser (local_name marker within budget).
- Quota gate: limit respected, persistence file written.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))


def http(port: int, path: str, payload: dict | None = None, raw_body: bytes | None = None):
    url = f"http://127.0.0.1:{port}{path}"
    data = raw_body if raw_body is not None else (json.dumps(payload).encode() if payload is not None else None)
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def wait_for(predicate, timeout: float, interval: float = 0.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="ba-smoke-"))
    logs = workdir / "GameLogs"
    logs.mkdir()
    (logs / "Gamelog__2099_01_01__00_00.log").write_text("", encoding="utf-8")

    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "web_ui.py"), "--dir", str(logs), "--port", str(port),
         "--no-stats", "--no-browser", "--daily-limit", "10",
         "--rel-db", str(workdir / "rel.sqlite"),
         "--cache", str(workdir / "cache.json")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=ROOT)
    try:
        check("server starts and serves UI", wait_for(lambda: http(port, "/")[0] == 200, 20))
        status, body = http(port, "/")
        check("'/' returns dashboard", status == 200 and "工作情况" in body.decode("utf-8"))

        status, body = http(port, "/api/state")
        state = json.loads(body) if status == 200 else {}
        check("/api/state shape", status == 200 and all(k in state for k in ("connected", "phase", "cache", "blacklist")), str(status))
        check("stats disabled offline", state.get("stats_enabled") is False)

        check("unknown path is 404", http(port, "/nope")[0] == 404)

        status, body = http(port, "/api/blacklist", {"id": "999001", "note": "smoke"})
        check("blacklist add", status == 200 and json.loads(body).get("ok") is True, str(status))
        _, body = http(port, "/api/state")
        check("blacklist persists in state", any(x.get("id") == "999001" for x in json.loads(body).get("blacklist", [])))

        check("malformed POST is 400", http(port, "/api/blacklist", raw_body=b"{broken")[0] == 400)

        status, _ = http(port, "/api/blacklist", {"id": "999001", "op": "remove"})
        check("blacklist remove", status == 200, str(status))
        _, body = http(port, "/api/state")
        check("blacklist gone after remove", not any(x.get("id") == "999001" for x in json.loads(body).get("blacklist", [])))

        with (logs / "Gamelog__2099_01_01__00_00.log").open("a", encoding="utf-8") as handle:
            handle.write(
                "[2099-01-01 00:00:01]\nLog: GetPersonaName SmokeTester\nLog: Enter to lobby (id: 7)\n"
                "[2099-01-01 00:05:01]\nLog: Start loading battle... map: SmokeMap, scenario: - Smoke_network\n"
                "[2099-01-01 00:05:11]\nLog: FID:990001\nLog: Player list:\n"
                "ID: 12345, Name: SmokeTester, Team: Alpha\nID: 67890, Name: FriendDude, Team: Alpha\nID: 54321, Name: EnemyDude, Team: Bravo\n"
                "[2099-01-01 00:40:01]\nLog: GameController dispose called\n")
        check("log pipeline reaches parser", wait_for(
            lambda: "local_name" in (json.loads(http(port, "/api/state")[1]).get("parser_health", {}).get("markers") or {}), 10))
        check("match end recorded", wait_for(
            lambda: "match_end" in (json.loads(http(port, "/api/state")[1]).get("parser_health", {}).get("markers") or {}), 10))

        status, body = http(port, "/api/investigate?id=67890")
        detail = json.loads(body) if status == 200 else {}
        check("investigate teammate", status == 200 and (detail.get("teammate") or {}).get("matches") == 1 and detail.get("names"), str(status))
        status, body = http(port, "/api/investigate?id=54321")
        detail = json.loads(body) if status == 200 else {}
        check("investigate opponent side", status == 200 and (detail.get("opponent") or {}).get("matches") == 1, str(status))
        check("investigate requires id", http(port, "/api/investigate")[0] == 400)
    finally:
        proc.kill()
        proc.wait(timeout=10)

    # 接管语义：不同构建关停旧实例并替换；同构建直接复用退出
    sock2 = socket.socket(); sock2.bind(("127.0.0.1", 0)); port2 = sock2.getsockname()[1]; sock2.close()
    def boot(build):
        env = dict(os.environ, BA_BUILD_ID=build, BA_URL_FILE=str(workdir / "takeover.url"))
        return subprocess.Popen(
            [sys.executable, str(ROOT / "web_ui.py"), "--dir", str(logs), "--port", str(port2),
             "--no-stats", "--no-browser", "--rel-db", str(workdir / "r2.sqlite"),
             "--cache", str(workdir / "c2.json")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=ROOT, env=env)
    pa = boot("build-a"); pb = None
    try:
        check("takeover baseline up", wait_for(lambda: http(port2, "/api/state")[0] == 200, 20))
        check("build fingerprint exposed", json.loads(http(port2, "/api/state")[1]).get("build") == "build-a")
        pb = boot("build-b")
        check("takeover replaces different build", wait_for(
            lambda: pa.poll() is not None and json.loads(http(port2, "/api/state")[1]).get("build") == "build-b", 25))
        pc = boot("build-b")
        try:
            out = pc.communicate(timeout=15)[0]
        except subprocess.TimeoutExpired:
            pc.kill(); out = pc.communicate()[0]
            print("!! same-build instance hung; incumbent said:\n" + (pb and pb.stdout and pb.stdout.read() or "[no output]")[:600])
        check("same build exits quietly", pc.returncode == 0 and "already running" in out, out[:80])
        try:
            check("incumbent still serving", json.loads(http(port2, "/api/state")[1]).get("build") == "build-b")
        except (OSError, ValueError, RuntimeError) as exc:
            check("incumbent still serving", False, type(exc).__name__)
    finally:
        for p in (pa, pb):
            if p and p.poll() is None:
                p.kill(); p.wait(timeout=10)

    # Quota is in-process logic; assert it directly rather than burning real requests.
    from web_ui import Quota
    quota = Quota(workdir / "quota.json", limit=2)
    check("quota allows up to limit", quota.try_consume() and quota.try_consume())
    check("quota blocks past limit", not quota.try_consume())
    summary = quota.summary()
    check("quota summary", summary["used"] == 2 and summary["remaining"] == 0, json.dumps(summary))
    check("quota persists across reload", Quota(workdir / "quota.json", limit=2).summary()["used"] == 2)

    from relationships import RelationshipDB
    rel = RelationshipDB(workdir / "rel-unit.sqlite")
    rel.add_match({"fid": "f1", "start_time": "2099-01-01 10:00:00", "map": "A", "players": [
        {"id": "1", "name": "me", "team": "Alpha"}, {"id": "2", "name": "pal", "team": "Alpha"}, {"id": "3", "name": "foe", "team": "Bravo"}]})
    rel.add_match({"fid": "f2", "start_time": "2099-01-02 10:00:00", "map": "B", "players": [
        {"id": "1", "name": "me", "team": "Alpha"}, {"id": "2", "name": "buddy", "team": "Alpha"}, {"id": "4", "name": "foe2", "team": "Bravo"}]})
    rel.record_result("f1", True); rel.record_result("f2", False)
    ann = rel.annotate_against_local([{"id": "2", "team": "Alpha"}], "1")
    check("teammate co-play stats", ann["2"]["prior_teammate_matches"] == 2 and ann["2"]["teammate_wins"] == 1 and ann["2"]["teammate_losses"] == 1, str(ann.get("2")))
    check("name history on rename", [n["name"] for n in rel.name_history("2")] == ["buddy", "pal"])
    inv = rel.investigate("2", "1")
    check("investigate aggregates", inv["teammate"]["matches"] == 2 and len(inv["recent"]) == 2 and inv["recent"][0]["won"] is False)
    check("ban baseline silent", rel.apply_ban_snapshot([{"id": 99, "name": "x"}]) == [])
    check("ban alerts only met newcomers", rel.apply_ban_snapshot([{"id": 99, "name": "x"}, {"id": "3", "name": "foe"}, {"id": "5", "name": "never-met"}]) == [{"id": "3", "name": "foe"}], "foe3")

    # 玩家查询失败后应被自动重试恢复（模拟先全网失败、随后恢复）
    import ba_tool
    from ba_tool import Match, Player, MatchAnalysis
    old_delays = ba_tool.MATCH_RETRY_DELAYS
    ba_tool.MATCH_RETRY_DELAYS = (0.2, 0.4, 0.8)
    class FlakyClient:
        def __init__(self):
            self.calls = 0
        def player_report(self, _pid):
            self.calls += 1
            if self.calls <= 3:
                raise RuntimeError("simulated outage")
            return {"trend": {"points": []}, "matchCount": 0}
    mt = Match(fid="t-retry")
    mt.players = [Player("11", "A", "Alpha"), Player("22", "B", "Bravo")]
    ma = MatchAnalysis(FlakyClient())
    try:
        ma.query_match(mt)
        first = [mt.player_stats[p.id]["status"] for p in mt.players]
        check("failed queries marked api_error", all(s == "api_error" for s in first), str(first))
        deadline = time.time() + 8
        while time.time() < deadline and any((mt.player_stats.get(p.id) or {}).get("status") == "api_error" for p in mt.players):
            time.sleep(0.2)
        second = [mt.player_stats[p.id]["status"] for p in mt.players]
        check("auto retry recovers players", all(s != "api_error" for s in second), str(second))
    finally:
        ba_tool.MATCH_RETRY_DELAYS = old_delays

    # 配额耗尽时：有过期缓存则继续服务，无缓存给可读错误而不是无意义重试
    from web_ui import ResilientClient, Cache
    qcache = Cache(workdir / "qc-cache.json")
    qkey = "player:" + json.dumps({"stbid": "42"}, sort_keys=True)
    qcache.put(qkey, {"elo": 1})
    qcache.d["entries"][qkey]["time"] = 0  # 强制变为 stale
    rquota = Quota(workdir / "qc-quota.json", limit=0)
    rc = ResilientClient("https://127.0.0.1:9", qcache, rquota)
    check("quota exhausted serves stale cache", rc.player_report("42") == {"elo": 1})
    try:
        rc.player_report("43")
        check("quota exhausted raises clear error", False, "no error raised")
    except RuntimeError as exc:
        check("quota exhausted raises clear error", "配额" in str(exc))

    failed = [name for name, ok, _ in CHECKS if not ok]
    for name, ok, detail in CHECKS:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
