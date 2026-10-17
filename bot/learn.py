"""Daily learning: decide which asset+strategy combos the scanner may trade.

For every asset and every strategy:
  1. tune parameters on the older 70% of recent history (train)
  2. check the winner on the newest 30% it never saw (validation)
Tiers:
  active  - profitable on validation (PF >= 1.1, >= 3 trades): full risk
  explore - best train results that did not prove themselves yet: tiny risk,
            so the bot keeps trying new markets and collects live evidence
Live feedback: a combo that lost money over its last 5+ live trades is benched.
"""
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import common as C
import strategy as S

TRAIN_MIN, VALID_MIN, PF_MIN = 6, 3, 1.1
BARS = {"binance": 3000, "yahoo": 4000}


def live_bench():
    """Combos whose recent live trades are net negative."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")
    by = defaultdict(list)
    for t in C.read_csv(C.TRADES):
        if t["close_time"][:10] >= cutoff:
            by[(t["asset"], t["strategy"])].append(float(t["pnl_usdt"]))
    return {k for k, v in by.items() if len(v) >= 5 and sum(v) < 0}


def main():
    cfg = C.load_json(C.CONFIG, None)
    benched = live_bench()
    results, failed = [], []
    for a in cfg["assets"]:
        if a["src"] == "yahoo" and cfg["interval"] not in C.YAHOO_INTERVALS:
            continue
        try:
            closed, _ = C.split_closed(C.candles(a, cfg["interval"], BARS[a["src"]]))
        except Exception as e:
            failed.append(f"{a['id']}: {str(e)[:80]}")
            continue
        if len(closed) < S.WARMUP + 300:
            failed.append(f"{a['id']}: only {len(closed)} bars")
            continue
        s = S.Series(closed)
        split = int(len(s) * 0.7)
        fee = cfg["fees"][a["cls"]]
        for name in S.STRATEGIES:
            best, bt = None, None
            for p in S.grid(name):
                t = S.backtest(s, name, p, fee, end=split)
                if t["trades"] >= TRAIN_MIN and (bt is None or t["score"] > bt["score"]):
                    best, bt = p, t
            if not best:
                continue
            v = S.backtest(s, name, best, fee, start=split)
            results.append({"asset": a["id"], "strategy": name, "params": best, "train": bt, "valid": v,
                            "benched": (a["id"], name) in benched})

    for r in results:
        v = r["valid"]
        r["tier"] = ("benched" if r["benched"] else
                     "active" if v["trades"] >= VALID_MIN and v["pf"] >= PF_MIN and v["ret"] > 0 else None)
    pool = sorted((r for r in results if r["tier"] is None and r["train"]["ret"] > 0),
                  key=lambda r: r["train"]["score"], reverse=True)
    for r in pool[: cfg["explore_slots"]]:
        r["tier"] = "explore"

    combos = [r for r in results if r["tier"] in ("active", "explore")]
    combos.sort(key=lambda r: (r["tier"] != "active", -r["valid"]["score"]))
    C.save_json(C.MODELS, {"updated": C.now_iso(), "interval": cfg["interval"], "combos": combos, "failed": failed,
                           "tested": len(results),
                           "all": [{k: r[k] for k in ("asset", "strategy", "tier", "valid")} for r in results]})
    now = C.now_iso()
    for r in results:
        if r["tier"]:
            C.append_csv(C.LEARNLOG, C.LEARN_FIELDS, {
                "time": now, "asset": r["asset"], "strategy": r["strategy"], "params": json.dumps(r["params"]),
                "tier": r["tier"], "train_trades": r["train"]["trades"], "train_ret": r["train"]["ret"],
                "valid_trades": r["valid"]["trades"], "valid_ret": r["valid"]["ret"],
                "valid_pf": r["valid"]["pf"], "note": "benched: lost money live" if r["tier"] == "benched" else ""})
    act = sum(r["tier"] == "active" for r in combos)
    print(f"tested {len(results)} combos, active {act}, explore {len(combos) - act}, failed {failed}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("learn failed:", e)
        sys.exit(1)
