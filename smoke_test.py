#!/usr/bin/env python3
"""End-to-end smoke test: boots web_ui.py against a synthetic GameLogs directory.

Zero network, zero game installation required:
- HTTP surface: '/', '/api/state', 404, POST '/api/blacklist' (valid + malformed).
- Log pipeline: appended lines must reach the parser (local_name marker within budget).
- Quota gate: limit respected, persistence file written.
"""
from __future__ import annotations
import json
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
            handle.write("[2099-01-01 00:00:01]\nLog: GetPersonaName SmokeTester\nLog: Enter to lobby (id: 7)\n")
        check("log pipeline reaches parser", wait_for(
            lambda: "local_name" in (json.loads(http(port, "/api/state")[1]).get("parser_health", {}).get("markers") or {}), 10))
    finally:
        proc.kill()
        proc.wait(timeout=10)

    # Quota is in-process logic; assert it directly rather than burning real requests.
    from web_ui import Quota
    quota = Quota(workdir / "quota.json", limit=2)
    check("quota allows up to limit", quota.try_consume() and quota.try_consume())
    check("quota blocks past limit", not quota.try_consume())
    summary = quota.summary()
    check("quota summary", summary["used"] == 2 and summary["remaining"] == 0, json.dumps(summary))
    check("quota persists across reload", Quota(workdir / "quota.json", limit=2).summary()["used"] == 2)

    failed = [name for name, ok, _ in CHECKS if not ok]
    for name, ok, detail in CHECKS:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
