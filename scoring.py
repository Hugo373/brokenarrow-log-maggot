"""Comprehensive, non-entertainment Broken Arrow performance scoring.

The module is deliberately independent of transport/UI. It consumes normalized
per-match records and never treats missing data as poor performance.
"""
from __future__ import annotations
import math
from statistics import median
from typing import Iterable, Optional


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def percentile(value: Optional[float], values: Iterable[float], reverse: bool = False) -> Optional[float]:
    if value is None:
        return None
    seq = sorted(float(v) for v in values if v is not None and math.isfinite(float(v)))
    if not seq:
        return None
    if len(seq) == 1:
        result = 0.5
    else:
        below = sum(v < float(value) for v in seq)
        equal = sum(v == float(value) for v in seq)
        result = (below + max(equal - 1, 0) / 2) / (len(seq) - 1)
    return clamp(1.0 - result if reverse else result)


def robust_score(value: Optional[float], team_values: Iterable[float], reverse: bool = False) -> Optional[float]:
    """Continuous team-relative score using median/IQR and logistic compression."""
    if value is None:
        return None
    seq = sorted(float(v) for v in team_values if v is not None and math.isfinite(float(v)))
    if not seq:
        return None
    med = median(seq)
    q1, q3 = seq[max(0, (len(seq) - 1) // 4)], seq[min(len(seq) - 1, 3 * (len(seq) - 1) // 4)]
    scale = max(q3 - q1, abs(med) * 0.05, 1.0)
    z = (float(value) - med) / scale
    if reverse:
        z = -z
    try:
        return 1.0 / (1.0 + math.exp(-z / 1.2))
    except OverflowError:
        return 0.0  # z deeply negative: exact logistic limit; exp underflows harmlessly on the other side


def weighted_available(parts: list[tuple[Optional[float], float]]) -> Optional[float]:
    available = [(float(v), float(w)) for v, w in parts if v is not None and math.isfinite(float(v)) and w > 0]
    if not available:
        return None
    return sum(v * w for v, w in available) / sum(w for _, w in available)


def score_match(player: dict, team: list[dict], enemy: list[dict], result: Optional[bool] = None, expected_win: Optional[float] = None) -> dict:
    """Return one match's continuous 0..1 score and transparent components.

    Accepted fields are mvp_score, kill_value, loss_value, damage_dealt,
    damage_taken, objective_value, supply_value, elo, and team_elo.
    Missing fields reduce coverage and are excluded from the denominator.
    """
    def field(name: str) -> Optional[float]:
        value = player.get(name)
        try: return None if value is None else float(value)
        except (TypeError, ValueError): return None
    def team_field(name: str) -> list[float]:
        out=[]
        for row in team:
            try:
                if row.get(name) is not None: out.append(float(row[name]))
            except (TypeError, ValueError): pass
        return out
    mvp = robust_score(field("mvp_score"), team_field("mvp_score"))
    kill = robust_score(field("kill_value"), team_field("kill_value"))
    loss = robust_score(field("loss_value"), team_field("loss_value"), reverse=True)
    damage = robust_score(field("damage_dealt"), team_field("damage_dealt"))
    taken = robust_score(field("damage_taken"), team_field("damage_taken"), reverse=True)
    objective = robust_score(field("objective_value"), team_field("objective_value"))
    supply = robust_score(field("supply_value"), team_field("supply_value"))
    loss_raw, kill_raw = field("loss_value"), field("kill_value")
    exchange = None if kill_raw is None or loss_raw is None else math.log1p(max(kill_raw, 0) / max(loss_raw, 1.0))
    exchange_score = robust_score(exchange, [math.log1p(max(float(r.get("kill_value", 0)), 0) / max(float(r.get("loss_value", 0)), 1.0)) for r in team if r.get("kill_value") is not None and r.get("loss_value") is not None])
    activity = weighted_available([(kill, .6), (damage, .4)])
    survival = weighted_available([(loss, .6), (activity, .4)])
    if result is None or expected_win is None:
        outcome = None
    else:
        outcome = clamp((1.0 if result else 0.0) - clamp(expected_win) + 0.5)
    components = {
        "mvp": mvp, "combat": weighted_available([(kill,.35),(damage,.25),(exchange_score,.20),(survival,.10),(mvp,.10)]),
        "efficiency": weighted_available([(exchange_score,.60),(loss,.25),(survival,.15)]),
        "teamwork": weighted_available([(objective,.40),(supply,.25),(mvp,.15),(damage,.10),(survival,.10)]),
        "context_result": outcome,
    }
    total = weighted_available([(components["mvp"],.35),(components["combat"],.25),(components["efficiency"],.15),(components["teamwork"],.15),(components["context_result"],.10)])
    fields_present = sum(v is not None for v in (mvp, kill, loss, damage, objective, supply))
    return {"score": total, "index": None if total is None else round(10.0 - 9.0 * total, 2), "components": components, "coverage": round(fields_present / 6, 2)}


def aggregate(matches: list[dict], prior: float = 0.5, prior_count: float = 4.0) -> dict:
    valid = [m for m in matches if m.get("score") is not None]
    if not valid:
        return {"index": None, "sample": 0, "n_eff": 0.0, "stacked": 0, "confidence": 0.0, "interval": None}
    values = [clamp(m["score"]) for m in valid]
    # Per-match extra weight (e.g. party down-weighting) multiplies recency decay.
    weights = [(0.92 ** i) * float(m.get("weight", 1.0)) for i, m in enumerate(valid)]
    total_weight = sum(weights)
    mean = sum(v*w for v,w in zip(values,weights)) / total_weight
    n = len(values)
    # Kish effective sample size: unequal weights count less than n raw matches.
    square_sum = sum(w*w for w in weights)
    n_eff = total_weight * total_weight / square_sum if square_sum > 0 else 0.0
    adjusted = (n_eff * mean + prior_count * prior) / (n_eff + prior_count)
    variance = sum((v - mean) ** 2 for v in values) / max(n - 1, 1)
    se = math.sqrt(variance / max(n_eff, 1))
    margin = 1.645 * se * 9.0
    index = 10.0 - adjusted * 9.0
    stacked = sum(1 for m in valid if float(m.get("weight", 1.0)) < 0.999)
    return {"index": round(index, 2), "sample": n, "n_eff": round(n_eff, 2), "stacked": stacked, "confidence": round(min(1.0, n_eff / 12.0), 2), "interval": [round(max(1.0, index - margin), 2), round(min(10.0, index + margin), 2)], "mean_score": round(adjusted, 4)}
