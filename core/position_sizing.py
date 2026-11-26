from typing import Dict, Optional


DEFAULT_SIZING_CFG = {
    "score_min_factor": 0.2,
    "score_max_factor": 1.0,
    "expectancy_min_factor": 0.5,
    "expectancy_max_factor": 1.5,
    "kelly_limit": 0.25,
    "kelly_min_factor": 0.5,
    "kelly_max_factor": 1.5,
}


def _clamp(min_v: float, max_v: float, val: float) -> float:
    return max(min_v, min(max_v, val))


def compute_size_factor(
    score: float,
    cfg: Optional[Dict] = None,
    expectancy_r: Optional[float] = None,
    win_rate: Optional[float] = None,
    payoff_ratio: Optional[float] = None,
) -> Dict[str, float]:
    """
    Compute dynamic position size factor based on:
    - score (0..100)
    - expectancy (R) if available, otherwise Kelly from win_rate + RR
    """
    sizing_cfg = dict(DEFAULT_SIZING_CFG)
    if cfg:
        sizing_cfg.update(cfg)

    base = score / 100.0 if score is not None else 0.0
    base = _clamp(sizing_cfg["score_min_factor"], sizing_cfg["score_max_factor"], base)

    used_expectancy = None
    used_win_rate = None
    used_payoff = None
    kelly_fraction = 0.0

    if expectancy_r is not None:
        used_expectancy = float(expectancy_r)
        exp_mult = _clamp(
            sizing_cfg["expectancy_min_factor"],
            sizing_cfg["expectancy_max_factor"],
            used_expectancy,
        )
        size_factor = base * exp_mult
        return {
            "size_factor": size_factor,
            "base_factor": base,
            "expectancy_factor": exp_mult,
            "kelly_fraction": kelly_fraction,
            "expectancy_r": used_expectancy,
            "win_rate": used_win_rate,
            "payoff_ratio": used_payoff,
        }

    if win_rate is not None and payoff_ratio is not None:
        used_win_rate = float(win_rate)
        used_payoff = float(payoff_ratio)
        if used_payoff > 0:
            p = used_win_rate / 100.0
            q = 1.0 - p
            kelly_fraction = max(0.0, (used_payoff * p - q) / used_payoff)
        kelly_limit = max(0.0, float(sizing_cfg.get("kelly_limit", 0.25)))
        if kelly_limit > 0:
            kelly_fraction = min(kelly_fraction, kelly_limit)
            scale = kelly_fraction / kelly_limit
        else:
            scale = 0.0

        kelly_mult = sizing_cfg["kelly_min_factor"] + scale * (
            sizing_cfg["kelly_max_factor"] - sizing_cfg["kelly_min_factor"]
        )
        kelly_mult = _clamp(
            sizing_cfg["kelly_min_factor"],
            sizing_cfg["kelly_max_factor"],
            kelly_mult,
        )
        size_factor = base * kelly_mult
        return {
            "size_factor": size_factor,
            "base_factor": base,
            "kelly_multiplier": kelly_mult,
            "kelly_fraction": kelly_fraction,
            "expectancy_r": used_expectancy,
            "win_rate": used_win_rate,
            "payoff_ratio": used_payoff,
        }

    return {
        "size_factor": base,
        "base_factor": base,
        "kelly_fraction": kelly_fraction,
        "expectancy_r": used_expectancy,
        "win_rate": used_win_rate,
        "payoff_ratio": used_payoff,
    }
