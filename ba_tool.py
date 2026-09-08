#!/usr/bin/env python3
"""Read-only Broken Arrow GameLogs monitor and report generator."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional
from analysis_engine import historical_performance

TIMESTAMP_RE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?)\]$")
LOG_RE = re.compile(r"\.(?:log|txt)$", re.I)
GAMELOG_RE = re.compile(r"^Gamelog__", re.I)

@dataclass
class Player:
    id: str
    name: str
    team: Optional[str]
    relationship: dict = field(default_factory=dict)
    party_signal: dict = field(default_factory=dict)

@dataclass
class Match:
    fid: Optional[str] = None
    map: str = ""
    scenario: str = ""
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    duration_sec: Optional[int] = None
    points: Optional[float] = None
    local_deck: str = ""
    players: list[Player] = field(default_factory=list)
    source_file: Optional[str] = None
    complete: bool = False
    player_stats: dict[str, dict] = field(default_factory=dict)

    def add_player(self, player: Player) -> bool:
        if any(p.id == player.id for p in self.players):
            return False
        self.players.append(player)
        return True

    def jsonable(self) -> dict:
        value = asdict(self)
        value["players"] = [asdict(p) for p in self.players]
        return value

class CaptchaRequired(RuntimeError):
    """Raised when the API answers with an HTML human-verification page instead of JSON."""

class PublicStatsClient:
    """Optional BATrace-compatible public API client; never sends log contents."""
    def __init__(self, base_url: str, timeout: float = 8.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> dict:
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(f"{self.base_url}{path}?{query}", headers={"User-Agent": "BrokenArrowLogTool/0.1"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = response.read()
            content_type = (response.headers.get("Content-Type") or "").lower()
        # A JSON endpoint answering HTML is the CDN's human-verification challenge; retrying cannot fix it.
        if "text/html" in content_type or b"EdgeOne" in body[:512]:
            raise CaptchaRequired("human verification page received / 收到人机验证页面")
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("API returned a non-object response")
        return data

    def player_report(self, player_id: str) -> dict:
        return self._get("/api/analysis/player", {"stbid": player_id})

    def match_report(self, match_id: str) -> dict:
        return self._get("/api/analysis/match", {"matchid": match_id})

    def maggot_index(self, player_id: str) -> Optional[float]:
        analysis = self.player_report(player_id)
        trend = analysis.get("trend") if isinstance(analysis.get("trend"), dict) else {}
        points = trend.get("points") if isinstance(trend.get("points"), list) else []
        candidates = [p for p in points if p.get("matchId") and abs((p.get("ratingAfter") or 0) - (p.get("ratingBefore") or 0)) >= 0.01][-30:][::-1]
        ranks = []
        for point in candidates:
            try:
                raw = self.match_report(str(point["matchId"]))
                rows = raw.get("mvpRanking")
                if not isinstance(rows, list) or len(rows) < 10:
                    continue
                me = next((p for p in rows if str(p.get("playerId")) == str(player_id)), None)
                if not me:
                    continue
                allies = [p for p in rows if p.get("teamId") == me.get("teamId")]
                allies.sort(key=lambda p: p.get("score") or 0, reverse=True)
                ranks.append(allies.index(me) + 1)
                if len(ranks) == 12:
                    break
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
        if len(ranks) < 12:
            return None
        average = sum(ranks) / len(ranks)
        normalized = (average - 1) / 4
        return round(1 + ((1 - math.cos(normalized * math.pi)) / 2) * 9, 1)

def compact_player_report(data: dict) -> dict:
    trend = data.get("trend") if isinstance(data.get("trend"), dict) else {}
    points = trend.get("points") if isinstance(trend.get("points"), list) else []
    latest = points[-1] if points else {}
    return {
        "elo": latest.get("ratingAfter", data.get("elo", data.get("rating"))),
        "kd": latest.get("kdRatio", data.get("kd")),
        "win_rate": data.get("winRate", data.get("win_rate")),
        "match_count": data.get("matchCount", len(points)),
        "maggot_index": None,
        "play_style": data.get("playStyle"),
        "error": None,
    }

def human_report(match: Match) -> str:
    lines = ["", "=" * 72, "BROKEN ARROW MATCH ANALYSIS / 断箭对局分析", "=" * 72]
    lines += [f"Map / 地图: {match.map or 'Unknown / 未知'}", f"FID: {match.fid or 'Unknown / 未知'}", f"Time / 时间: {match.start_time or '?'} -> {match.end_time or '?'}"]
    if match.duration_sec is not None:
        lines.append(f"Duration / 时长: {match.duration_sec // 60}m {match.duration_sec % 60}s")
    if match.points is not None:
        lines.append(f"Local points / 本机总分: {match.points:.2f}")
    lines += ["", "PRE-MATCH PERFORMANCE / 局前综合表现", "-" * 72]
    for team in ("Alpha", "Bravo", "Spectators", None):
        members = [p for p in match.players if p.team == team]
        if not members:
            continue
        lines.append(f"[{team or 'Unknown / 未知'}]")
        for player in members:
            stats = match.player_stats.get(player.id, {})
            if player.id.startswith("-"):
                value = "BOT"
            elif stats.get("performance_index") is not None:
                interval = stats.get("interval") or []
                ci = f" ({interval[0]:.1f}-{interval[1]:.1f})" if len(interval) == 2 else ""
                value = f"{float(stats['performance_index']):.2f}{ci}"
            else:
                value = stats.get("status", "pending")
            lines.append(f"  {player.name} [{player.id}]  index={value}  ELO={stats.get('elo', '?')}  sample={stats.get('sample', 0)}/12")
    real = [p for p in match.players if not p.id.startswith("-")]
    values = [float(match.player_stats[p.id]["performance_index"]) for p in real if match.player_stats.get(p.id, {}).get("performance_index") is not None]
    lines += ["", "SUMMARY / 总结"]
    lines.append(f"Known-index average / 已知指数平均值: {sum(values) / len(values):.2f}" if values else "No comprehensive scores were available. / 没有可用的综合表现评分。")
    lines.append(f"Roster: {len(real)} human, {len(match.players) - len(real)} bot(s) / 真人 {len(real)}，机器人 {len(match.players) - len(real)}")
    return "\n".join(lines)

class MatchAnalysis:
    def __init__(self, client: Optional[PublicStatsClient], on_update: Optional[Callable[[Match], None]] = None):
        self.client = client
        self.on_update = on_update or (lambda _match: None)
        self.timer: Optional[threading.Timer] = None
        self.lock = threading.Lock()
        self.stats_by_fid: dict[str, dict[str, dict]] = {}

    def on_roster(self, match: Match) -> None:
        if not self.client:
            return
        with self.lock:
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(2.0, self.query_match, args=(match,))
            self.timer.daemon = True
            self.timer.start()

    def query_match(self, match: Match) -> None:
        if not self.client:
            return
        for player in list(match.players):
            if player.id.startswith("-") or player.id in match.player_stats:
                continue
            match.player_stats[player.id] = {"status": "queued", "reason": None}
            self.on_update(match)
            try:
                profile = self.client.player_report(player.id)
                def progress(state: dict, pid=player.id) -> None:
                    match.player_stats[pid] = {**match.player_stats.get(pid, {}), **state}
                    self.on_update(match)
                stats = historical_performance(self.client, player.id, profile, progress)
                current = next((p for p in ((profile.get("trend") or {}).get("points") or []) if str(p.get("matchId")) == str(match.fid)), None)
                if current and current.get("ratingBefore") is not None and current.get("ratingAfter") is not None:
                    stats["elo_delta"] = round(float(current["ratingAfter"]) - float(current["ratingBefore"]), 2)
                match.player_stats[player.id] = stats
            except (OSError, ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
                match.player_stats[player.id] = {"status": "api_error", "reason": str(exc), "performance_index": None}
            self.on_update(match)
        if match.fid:
            self.stats_by_fid.setdefault(str(match.fid), {}).update(match.player_stats)

    def finish(self, match: Match) -> str:
        if self.timer:
            self.timer.cancel()
        if match.fid:
            match.player_stats.update(self.stats_by_fid.get(str(match.fid), {}))
        self.query_match(match)
        return human_report(match)

class LogParser:
    """Line-oriented state machine with explicit workflow health telemetry."""
    def __init__(self, on_event: Optional[Callable[[str, dict], None]] = None):
        self.on_event = on_event or (lambda _kind, _data: None)
        self.local_name: Optional[str] = None
        self.lobby_players: dict[str, str] = {}
        self.current_deck = ""
        self.current: Optional[Match] = None
        self.matches: list[Match] = []
        self.last_ended: Optional[Match] = None
        self._timestamp: Optional[str] = None
        self._in_player_list = False
        self.health = {"events": 0, "last_timestamp": None, "phase": "idle", "markers": {}}

    def reset(self, keep_local_name: bool = True) -> None:
        name = self.local_name if keep_local_name else None
        self.__init__(self.on_event)
        self.local_name = name

    def feed(self, lines: Iterable[str], source_file: Optional[str] = None) -> None:
        for line in lines:
            self.handle_line(line, source_file)

    def handle_line(self, raw: str, source_file: Optional[str] = None) -> None:
        line = raw.rstrip("\r\n")
        if not line:
            return
        timestamp = TIMESTAMP_RE.match(line)
        if timestamp:
            self._timestamp = timestamp.group(1)
            self.health["last_timestamp"] = self._timestamp
            return

        def emit(kind: str, data: dict) -> None:
            self.health["events"] += 1
            self.health["phase"] = kind
            self.health["markers"][kind] = self.health["markers"].get(kind, 0) + 1
            self.on_event(kind, data)
            self.on_event("parser_health", {"health": self.health_snapshot()})


        m = re.match(r"^Log: GetPersonaName\s+(.+)$", line)
        if m:
            self.local_name = m.group(1).strip()
            emit("local_name", {"name": self.local_name})
            return

        if re.match(r"^Log: (?:Enter to lobby \(id: \d+\)|Exit lobby)", line):
            self.lobby_players.clear()
            self.current = None
            self._in_player_list = False
            emit("lobby_reset", {})
            return

        m = re.match(r"^Log: Incoming client (.*?):(-?\d+) to lobby", line)
        if m:
            self.lobby_players[m.group(2)] = m.group(1).strip()
            emit("lobby", {"players": dict(self.lobby_players)})
            return

        m = re.match(r"^Log: Outgoing client (.*?):(-?\d+) exit from lobby", line)
        if m:
            self.lobby_players.pop(m.group(2), None)
            emit("lobby", {"players": dict(self.lobby_players)})
            return

        m = re.match(r"^Log: Start loading battle\.\.\. map: ([^,]+),\s*scenario:\s*(.*)$", line)
        if m:
            self._begin(m.group(1).strip(), m.group(2).strip(), source_file)
            return

        if re.match(r"^Log: \[NET_ROOM\] GameRoom entered", line):
            if self.current is None:
                self._begin("", "", source_file)
            return

        if re.match(r"^Log: Player list:\s*$", line):
            self._in_player_list = True
            return

        if self._in_player_list:
            m = re.match(r"^ID: (-?\d+), Name: (.*?), Team: (\w+)$", line)
            if m and self.current:
                player = Player(m.group(1), m.group(2).strip(), m.group(3))
                if self.current.add_player(player):
                    emit("roster", {"match": self.current.jsonable()})
                return

        m = re.match(r"^Log: Room: \(GameRoom\|\d+\), Client: \(([^|]+)\|(-?\d+)\)", line)
        if m and self.current:
            player = Player(m.group(2), m.group(1).strip(), None)
            if self.current.add_player(player):
                emit("roster", {"match": self.current.jsonable()})
            return

        m = re.match(r"^Log: FID:(\d+)", line)
        if m:
            if self.current:
                self.current.fid = m.group(1)
            emit("fid", {"fid": m.group(1)})
            return

        m = re.match(r"^Log: Deck set to: (.*)$", line)
        if m:
            value = m.group(1).strip()
            self.current_deck = "" if value.lower() == "null" else value
            if self.current:
                self.current.local_deck = self.current_deck
            return

        m = re.match(r"^Log: TOTAL POINTS: ([0-9.]+)", line)
        if m and self.current:
            self.current.points = float(m.group(1))
            return

        m = re.match(r"^Log: \[NET_ROOM\] Connection closed\..*LiveTime: (\d+) sec", line)
        if m:
            seconds = int(m.group(1))
            target = self.current or self.last_ended
            if target and target.duration_sec is None:
                target.duration_sec = seconds
                emit("match_meta", {"match": target.jsonable()})
            return

        if re.match(r"^Log: GameController dispose called", line):
            self._end()
            return

        if re.match(r"^Log: \[NET_ROOM\] GameRoom exited", line) and self.current and self.current.players:
            self._end()

    def _begin(self, map_name: str, scenario: str, source_file: Optional[str]) -> None:
        if self.current and self.current.players:
            self._end()
        self.current = Match(
            map=map_name,
            scenario=scenario,
            start_time=self._timestamp,
            local_deck=self.current_deck,
            source_file=source_file,
        )
        self._in_player_list = False
        self.health["phase"] = "match_start"
        self.health["markers"]["match_start"] = self.health["markers"].get("match_start", 0) + 1
        self.on_event("match_start", {"match": self.current.jsonable()})
        self.on_event("parser_health", {"health": self.health_snapshot()})

    def _end(self) -> None:
        if not self.current:
            return
        self.current.end_time = self._timestamp
        if self.current.duration_sec is None and self.current.start_time and self.current.end_time:
            try:
                start = datetime.fromisoformat(self.current.start_time)
                end = datetime.fromisoformat(self.current.end_time)
                self.current.duration_sec = max(0, round((end - start).total_seconds()))
            except ValueError:
                pass
        self.current.complete = True
        self.matches.append(self.current)
        self.last_ended = self.current
        self.health["phase"] = "match_end"
        self.health["markers"]["match_end"] = self.health["markers"].get("match_end", 0) + 1
        self.on_event("match_end", {"match": self.current.jsonable()})
        self.on_event("parser_health", {"health": self.health_snapshot()})
        self.current = None
        self._in_player_list = False
    def health_snapshot(self) -> dict:
        required = ("match_start", "fid", "roster", "match_end")
        markers = dict(self.health["markers"])
        missing = [name for name in required if not markers.get(name)]
        return {**self.health, "markers": markers, "missing": missing, "compatible": not missing if self.matches else None}


    def snapshot(self) -> dict:
        return {
            "local_name": self.local_name,
            "lobby_players": dict(self.lobby_players),
            "current_deck": self.current_deck,
            "current_match": self.current.jsonable() if self.current else None,
            "matches": len(self.matches),
        }

DEFAULT_GAMELOGS = Path(r"D:\SteamLibrary\steamapps\common\broken_arrow\GameLogs")

def find_gamelogs() -> Optional[Path]:
    """Discover Broken Arrow's GameLogs through Steam library folders; None when not found."""
    suffix = Path("steamapps") / "common" / "broken_arrow" / "GameLogs"
    steam = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Steam"
    roots = [steam, DEFAULT_GAMELOGS.parents[2]]
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    try:
        roots += [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="replace"))]
    except OSError:
        pass
    seen: set[str] = set()
    for root in roots:
        key = str(root).lower()
        if key in seen:
            continue
        seen.add(key)
        candidate = root / suffix
        if candidate.is_dir():
            return candidate
    return None

class LogWatcher:
    def __init__(self, directory: Path, parser: LogParser, poll_seconds: float = 1.5):
        self.directory = directory
        self.parser = parser
        self.poll_seconds = poll_seconds
        self.current_file: Optional[Path] = None
        self.offset = 0
        self.pending = b""

    def newest(self) -> Optional[Path]:
        try:
            candidates = [p for p in self.directory.iterdir() if p.is_file() and LOG_RE.search(p.name)]
        except OSError:
            return None
        candidates.sort(key=lambda p: (bool(GAMELOG_RE.match(p.name)), p.stat().st_mtime_ns), reverse=True)
        return candidates[0] if candidates else None

    def poll(self) -> bool:
        path = self.newest()
        if path is None:
            return False
        if path != self.current_file:
            self.current_file = path
            self.offset = 0
            self.pending = b""
            self.parser.reset(True)
        try:
            size = path.stat().st_size
            if size < self.offset:
                self.offset = 0
                self.pending = b""
            if size == self.offset:
                return True
            with path.open("rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read(size - self.offset)
            self.offset = size
        except OSError:
            return False
        data = self.pending + chunk
        lines = data.split(b"\n")
        self.pending = lines.pop() or b""
        self.parser.feed((line.decode("utf-8", errors="replace") for line in lines), path.name)
        return True

    def run(self) -> None:
        print(f"Watching {self.directory} (Ctrl+C to stop)")
        try:
            while True:
                self.poll()
                time.sleep(self.poll_seconds)
        except KeyboardInterrupt:
            print("Stopped.")

def files_in(directory: Path) -> list[Path]:
    paths = [p for p in directory.iterdir() if p.is_file() and LOG_RE.search(p.name)]
    return sorted(paths, key=lambda p: p.name.lower())

def scan(directory: Path) -> LogParser:
    aggregate = LogParser()
    for path in files_in(directory):
        parser = LogParser()
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            parser.feed(handle, path.name)
        aggregate.matches.extend(parser.matches)
        if parser.local_name:
            aggregate.local_name = parser.local_name
    return aggregate

def print_match(match: Match) -> None:
    fid = match.fid or "no FID"
    print(f"{match.start_time or '?'}  {fid}  {match.map or '?'}  {len(match.players)} players")
def match_from_dict(data: dict) -> Match:
    fields = ("fid", "map", "scenario", "start_time", "end_time", "duration_sec", "points", "local_deck", "source_file", "complete")
    match = Match(**{key: data.get(key) for key in fields})
    match.players = [Player(str(p.get("id")), p.get("name", ""), p.get("team")) for p in data.get("players", [])]
    match.player_stats = dict(data.get("player_stats", {}))
    return match


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only Broken Arrow GameLogs analyzer")
    ap.add_argument("--dir", type=Path, default=None, help="GameLogs directory (auto-detected via Steam when omitted)")
    sub = ap.add_subparsers(dest="command", required=True)
    scan_cmd = sub.add_parser("scan", help="scan historical logs")
    scan_cmd.add_argument("--json", dest="json_path", type=Path, help="write report JSON")
    watch_cmd = sub.add_parser("watch", help="monitor newest log")
    watch_cmd.add_argument("--interval", type=float, default=1.5)
    watch_cmd.add_argument("--stats-api", default="https://app.batrace.top", help="public stats API base URL")
    watch_cmd.add_argument("--no-stats", action="store_true", help="disable public player lookups")
    args = ap.parse_args(argv)
    if args.dir is None:
        args.dir = find_gamelogs() or DEFAULT_GAMELOGS
    if not args.dir.is_dir():
        print(f"Log directory not found: {args.dir} (auto-detect failed; pass --dir) / 未找到日志目录", file=sys.stderr)
        return 2
    if args.command == "scan":
        parser = scan(args.dir)
        report = {
            "directory": str(args.dir),
            "local_name": parser.local_name,
            "match_count": len(parser.matches),
            "matches": [m.jsonable() for m in parser.matches],
        }
        print(f"Scanned {len(files_in(args.dir))} log files; found {len(parser.matches)} completed matches.")
        for match in parser.matches:
            print_match(match)
        if args.json_path:
            args.json_path.parent.mkdir(parents=True, exist_ok=True)
            args.json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Report written to {args.json_path}")
        return 0
    analysis = MatchAnalysis(None if args.no_stats else PublicStatsClient(args.stats_api))
    def on_event(kind: str, data: dict) -> None:
        print(json.dumps({"event": kind, **data}, ensure_ascii=False))
        if kind == "roster" and data.get("match"):
            analysis.on_roster(match_from_dict(data["match"]))
        elif kind == "match_end" and data.get("match"):
            print(analysis.finish(match_from_dict(data["match"])), flush=True)
    parser = LogParser(on_event)
    LogWatcher(args.dir, parser, args.interval).run()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
