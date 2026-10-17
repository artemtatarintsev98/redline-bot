"""Research lab: which timeframe / stop width actually works on real data?

For every setup it runs the whole bot pipeline honestly on three slices of history:
  train 50%  -> pick best params per asset+strategy
  select 25% -> keep only combos that are profitable there (what learn.py does)
  test 25%   -> never seen before; this is the number that matters
Writes data/research.json and reports/research.md.
"""
import json
import sys
import time

import common as C
import strategy as S

SETUPS = {
    "15m_base": ("15m", None),
    "1h_base": ("1h", None),
    "1h_wide": ("1h", {"sl": [2.0, 3.0], "tp": [3.0, 4.5]}),
    "4h_base": ("4h", None),
    "4h_wide": ("4h", {"sl": [2.0, 3.0], "tp": [3.0, 4.5]}),
}
BARS = {"15m": 3000, "1h": 3000, "4h": 2000}
C.BAR_MS.setdefault("4h", 4 * 3_600_000)


def run_setup(cfg, interval, override):
    saved = {k: dict(v) for k, v in S.GRIDS.items()}
    if override:
        for g in S.GRIDS.values():
            g.update({k: v for k, v in override.items() if k in g})
    test_trades, test_ret, picked, rows = 0, 0.0, 0, []
    try:
        for a in cfg["assets"]:
            if a["src"] != "binance" and interval == "4h":
                continue  # yahoo has no 4h bars
            try:
                closed, _ = C.split_closed(C.candles(a, interval, BARS[interval]))
            except Exception as e:
                rows.append({"asset": a["id"], "error": str(e)[:80]})
                continue
            if len(closed) < S.WARMUP + 400:
                continue
            s = S.Series(closed)
            n = len(s)
            t1, t2 = int(n * 0.5), int(n * 0.75)
            fee = cfg["fees"][a["cls"]]
            for name in S.STRATEGIES:
                best, bs = None, None
                for p in S.grid(name):
                    r = S.backtest(s, name, p, fee, end=t1)
                    if r["trades"] >= 6 and (bs is None or r["score"] > bs["score"]):
                        best, bs = p, r
                if not best or bs["ret"] <= 0:
                    continue
                sel = S.backtest(s, name, best, fee, start=t1, end=t2)
                if sel["trades"] < 3 or sel["pf"] < 1.1 or sel["ret"] <= 0:
                    continue
                tst = S.backtest(s, name, best, fee, start=t2)
                picked += 1
                test_trades += tst["trades"]
                test_ret += tst["ret"]
                rows.append({"asset": a["id"], "strategy": name, "params": best,
                             "select": sel, "test": tst})
    finally:
        S.GRIDS.clear()
        S.GRIDS.update(saved)
    wins = [r for r in rows if "test" in r and r["test"]["ret"] > 0]
    return {"interval": interval, "combos_picked": picked, "test_trades": test_trades,
            "test_ret_sum": round(test_ret, 4),
            "test_ret_avg": round(test_ret / picked, 4) if picked else 0,
            "combos_profitable_on_test": len(wins), "rows": rows}


def main():
    cfg = C.load_json(C.CONFIG, None)
    out = {"time": C.now_iso(), "setups": {}}
    for name, (interval, override) in SETUPS.items():
        t0 = time.time()
        out["setups"][name] = run_setup(cfg, interval, override)
        print(name, {k: v for k, v in out["setups"][name].items() if k != "rows"},
              f"{time.time() - t0:.0f}s", flush=True)
    C.save_json(C.ROOT / "data" / "research.json", out)
    L = [f"# Лабораторія {out['time']}", "", "Тест на останніх 25% історії, яких бот не бачив.", "",
         "| Варіант | Комбінацій | Угод на тесті | Сума дох. | Сер. дох. | Плюсових |", "|---|---|---|---|---|---|"]
    for name, r in out["setups"].items():
        L.append(f"| {name} | {r['combos_picked']} | {r['test_trades']} | {r['test_ret_sum']:+.2%} | "
                 f"{r['test_ret_avg']:+.2%} | {r['combos_profitable_on_test']}/{r['combos_picked']} |")
    (C.ROOT / "reports").mkdir(exist_ok=True)
    (C.ROOT / "reports" / "research.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("research failed:", e)
        sys.exit(1)
