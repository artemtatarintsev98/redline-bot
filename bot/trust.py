"""Trust score: the bot's reward system.

Every asset+strategy combo earns or loses "trust" from its own live trades.
Trust decides how much capital the combo is allowed to risk next time.

Rewards are measured in R (profit divided by the money that was risked), so a
combo is rewarded for being right, not for betting big. Drawdowns are punished,
and a combo with little history is pulled toward neutral until it proves itself.
"""
from collections import defaultdict

import common as C

PRIOR_TRADES = 10       # new combos count as 10 neutral trades: trust must be earned
DD_PENALTY = 0.5        # each R of drawdown costs half an R of trust
MIN_MULT, MAX_MULT = 0.25, 2.0
BENCH_AFTER, BENCH_BELOW = 8, -0.15
TIER_RISK_GUESS = {"active": 10.0, "explore": 2.5}  # for older trades without risk_usdt


def trade_r(t):
    risk = float(t.get("risk_usdt") or 0) or TIER_RISK_GUESS.get(t.get("tier"), 10.0)
    return float(t["pnl_usdt"]) / risk if risk else 0.0


def table():
    """{(asset, strategy): {...}} built from all closed trades."""
    by = defaultdict(list)
    for t in C.read_csv(C.TRADES):
        by[(t["asset"], t["strategy"])].append(trade_r(t))
    out = {}
    for key, rs in by.items():
        eq = peak = dd = 0.0
        streak = worst = 0
        for r in rs:
            eq += r
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
            streak = streak + 1 if r < 0 else 0
            worst = max(worst, streak)
        n = len(rs)
        trust = (sum(rs) - DD_PENALTY * dd) / (n + PRIOR_TRADES)
        cap_up = 1 + (MAX_MULT - 1) * min(n, 20) / 20  # bigger risk only after ~20 proven trades
        mult = max(MIN_MULT, min(cap_up, 1 + 3 * trust))
        benched = n >= BENCH_AFTER and trust < BENCH_BELOW
        out[key] = {"trades": n, "total_r": round(sum(rs), 2), "max_dd_r": round(dd, 2),
                    "loss_streak": worst, "wins": sum(r > 0 for r in rs),
                    "trust": round(trust, 3), "mult": 0.0 if benched else round(mult, 2),
                    "benched": benched}
    return out


def get(tbl, asset, strategy):
    return tbl.get((asset, strategy), {"trades": 0, "trust": 0.0, "mult": 1.0, "benched": False,
                                       "total_r": 0, "max_dd_r": 0, "loss_streak": 0, "wins": 0})
