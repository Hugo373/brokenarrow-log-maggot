"""Historical and post-match comprehensive analysis over BATrace-compatible data."""
from __future__ import annotations
from typing import Callable, Optional
from scoring import aggregate, score_match


def _number(value):
    try: return float(value) if value is not None else None
    except (TypeError, ValueError): return None


def match_rows(raw: dict) -> list[dict]:
    rows=[]
    economy={str(p.get("playerId")):p for p in (raw.get("economy") or {}).get("players",[]) if p.get("playerId") is not None}
    damage={str(p.get("playerId")):p for side in ("teamA","teamB") for p in (raw.get("damageContribution") or {}).get(side,[]) if p.get("playerId") is not None}
    for item in raw.get("mvpRanking") or []:
        pid=str(item.get("playerId")); econ=economy.get(pid,{}); dmg=damage.get(pid,{}); breakdown=item.get("breakdown") or {}
        rows.append({
            "id":pid,"name":item.get("playerName") or pid,"team_id":item.get("teamId"),
            "mvp_score":_number(item.get("score")),"kill_value":_number(econ.get("returnValue")),
            "loss_value":max(0.0,(_number(econ.get("investment")) or 0)-(_number(econ.get("refunded")) or 0)) if econ else None,
            "damage_dealt":_number(dmg.get("damageDealt")),"damage_taken":None,
            "objective_value":_number(breakdown.get("objectives")),"supply_value":_number(breakdown.get("supply")),
            "breakdown":breakdown,"roi":_number(econ.get("roi")),
        })
    return rows


def analyze_single_match(raw: dict, results: Optional[dict[str,bool]]=None) -> dict:
    rows=match_rows(raw); results=results or {}; scored=[]
    for player in rows:
        team=[p for p in rows if p["team_id"]==player["team_id"]]
        enemy=[p for p in rows if p["team_id"]!=player["team_id"]]
        score=score_match(player,team,enemy,results.get(player["id"]),0.5 if player["id"] in results else None)
        scored.append({**player,**score})
    teams={}
    for team_id in sorted({p["team_id"] for p in scored},key=lambda x:str(x)):
        members=[p for p in scored if p["team_id"]==team_id]
        values=[p["score"] for p in members if p["score"] is not None]
        teams[str(team_id)]={"players":members,"average_score":round(sum(values)/len(values)*100,1) if values else None,"coverage":round(sum(p["coverage"] for p in members)/len(members),2) if members else 0}
    comparison=raw.get("teamComparison") or {}
    return {"match_id":str(raw.get("matchId") or ""),"teams":teams,"team_totals":{"0":comparison.get("teamATotals"),"1":comparison.get("teamBTotals")},"balance":raw.get("balance")}


def historical_performance(client, player_id: str, profile: Optional[dict]=None, progress: Optional[Callable[[dict],None]]=None) -> dict:
    progress=progress or (lambda _state:None); profile=profile or client.player_report(player_id)
    trend=(profile.get("trend") or {}).get("points") or []
    candidates=[p for p in trend if p.get("matchId") and abs((_number(p.get("ratingAfter")) or 0)-(_number(p.get("ratingBefore")) or 0))>=.01][-30:][::-1]
    progress({"status":"fetching_matches","candidate_count":len(candidates),"valid":0,"required":12})
    matches=[]; skipped={"missing_player":0,"incomplete_match":0,"request_failed":0}
    for point in candidates:
        if len(matches)>=12: break
        try: raw=client.match_report(str(point["matchId"]))
        except Exception:
            skipped["request_failed"]+=1; continue
        rows=match_rows(raw)
        if len(rows)<10:
            skipped["incomplete_match"]+=1; continue
        me=next((p for p in rows if p["id"]==str(player_id)),None)
        if not me:
            skipped["missing_player"]+=1; continue
        team=[p for p in rows if p["team_id"]==me["team_id"]]; enemy=[p for p in rows if p["team_id"]!=me["team_id"]]
        scored=score_match(me,team,enemy,bool(point.get("won")),0.5)
        scored.update({"match_id":str(point["matchId"]),"won":bool(point.get("won")),"elo_delta":round((_number(point.get("ratingAfter")) or 0)-(_number(point.get("ratingBefore")) or 0),2),"_teammates":[str(p["id"]) for p in team if str(p["id"])!=str(player_id)]})
        matches.append(scored); progress({"status":"fetching_matches","candidate_count":len(candidates),"valid":len(matches),"required":12})
    # A teammate appearing in 3+ sampled matches signals a regular party; those games say less about the individual.
    teammate_counts: dict[str, int] = {}
    for match in matches:
        for mate in match.get("_teammates", []):
            teammate_counts[mate] = teammate_counts.get(mate, 0) + 1
    for match in matches:
        mates = match.pop("_teammates", [])
        if any(teammate_counts.get(mate, 0) >= 3 for mate in mates):
            match["weight"] = 0.6
    total=aggregate(matches)
    components={}
    for key in ("mvp","combat","efficiency","teamwork","context_result"):
        vals=[m["components"].get(key) for m in matches if m["components"].get(key) is not None]
        components[key]=round(sum(vals)/len(vals)*100,1) if vals else None
    points=(profile.get("trend") or {}).get("points") or []; latest=points[-1] if points else {}
    wins=sum(bool(p.get("won")) for p in points[-12:]); sample=min(12,len(points))
    status="ready" if total["sample"]>=12 else ("provisional" if total["sample"]>=4 else "insufficient_data")
    return {"status":status,"performance_index":total["index"],"interval":total["interval"],"confidence":total["confidence"],"sample":total["sample"],"n_eff":total.get("n_eff"),"stacked":total.get("stacked"),"required":12,"components":components,"matches":matches,"skipped":skipped,"elo":_number(latest.get("ratingAfter")),"kd":_number(latest.get("kdRatio")),"win_rate":round(wins/sample*100) if sample else None,"match_count":profile.get("matchCount",len(points)),"play_style":profile.get("playStyle")}
