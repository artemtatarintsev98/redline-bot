"""Daily learning step (walk-forward tuning with guardrails).

1. Download ~120 days of hourly candles.
2. Try small tweaks around the current parameters on the first 90 days (train).
3. Check the best tweak on the last 30 days it has never seen (validation).
4. Adopt it only if it beats the current parameters on validation, is profitable
   there, and has enough trades. Otherwise keep the current parameters.
Every decision is written to data/learning_log.csv.
"""
import json
import sys

import common as C
import strategy as S

STEPS = {"ema_fast": 5, "ema_slow": 10, "rsi_entry": 5, "sl_atr": 0.5, "tp_atr": 0.5}
BOUNDS = {"ema_fast": (5, 60), "ema_slow": (20, 200), "rsi_entry": (25, 55),
          "sl_atr": (1.0, 4.0), "tp_atr": (1.0, 6.0)}
MIN_TRAIN_TRADES, MIN_VALID_TRADES, MARGIN = 8, 3, 0.005
DAYS_TOTAL, DAYS_VALID = 120, 30


def valid_params(p):
    if p["ema_fast"] >= p["ema_slow"]:
        return False
    return all(BOUNDS[k][0] <= p[k] <= BOUNDS[k][1] for k in BOUNDS)


def neighbours(p):
    out = []
    for k, step in STEPS.items():
        for sign in (-1, 1):
            q = dict(p)
            q[k] = round(p[k] + sign * step, 2)
            if valid_params(q):
                out.append(q)
    for ds in (-0.5, 0.5):  # risk/reward pairs
        for dt in (-0.5, 0.5):
            q = dict(p, sl_atr=round(p["sl_atr"] + ds, 2), tp_atr=round(p["tp_atr"] + dt, 2))
            if valid_params(q):
                out.append(q)
    return out


def main():
    cfg = C.load_json(C.CONFIG, None)
    cur = cfg["params"]
    candles = C.fetch_history(cfg["symbol"], cfg["interval"], DAYS_TOTAL * 24)
    split = len(candles) - DAYS_VALID * 24
    fee = cfg["fee_rate"]

    def train(p):
        return S.backtest(candles, p, fee, end=split)[0]

    def valid(p):
        return S.backtest(candles, p, fee, start=split)[0]

    cur_train, cur_valid = train(cur), valid(cur)
    best, best_train = None, None
    for q in neighbours(cur):
        t = train(q)
        if t["trades"] >= MIN_TRAIN_TRADES and (best_train is None or t["score"] > best_train["score"]):
            best, best_train = q, t

    row = {"time": C.now_iso(), "old_version": cfg["params_version"], "old_params": json.dumps(cur),
           "old_valid_score": cur_valid["score"]}
    if best and best_train["score"] > cur_train["score"]:
        v = valid(best)
        ok = (v["trades"] >= MIN_VALID_TRADES and v["total_ret"] > 0
              and v["score"] > cur_valid["score"] + MARGIN)
        row.update(new_params=json.dumps(best), new_valid_score=v["score"], valid_trades=v["trades"])
        if ok:
            cfg["params"], cfg["params_version"] = best, cfg["params_version"] + 1
            C.save_json(C.CONFIG, cfg)
            row.update(decision="ADOPTED", new_version=cfg["params_version"],
                       note=f"valid ret {v['total_ret']:.2%}, dd {v['max_dd']:.2%}, PF {v['profit_factor']}")
        else:
            row.update(decision="REJECTED", new_version=cfg["params_version"],
                       note="better on train but not on unseen data (likely overfit)")
    else:
        row.update(decision="KEPT", new_version=cfg["params_version"], note="no better tweak found on train")

    C.save_json(C.ROOT / "data" / "backtest_latest.json",
                {"time": C.now_iso(), "params": cfg["params"], "train": train(cfg["params"]),
                 "valid": valid(cfg["params"]), "candles": len(candles)})
    C.append_csv(C.LEARNLOG, C.LEARN_FIELDS, row)
    print(row)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("learn failed:", e)
        sys.exit(1)
