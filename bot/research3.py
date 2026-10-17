"""Research lab v3: can the bot trade every day without bleeding?

A) Momentum rotation with faster rebalancing (every 1 / 3 / 7 days).
B) Intraday entries on 15m / 1h / 4h with FIXED parameters (no per-coin tuning),
   with and without a daily trend filter:
     long only when the coin's daily close > SMA50 and BTC's > SMA100,
     short only when both are below.
Reports trades per day, win rate, average R (profit in units of risk) and both halves.
"""
import sys

import common as C
import research2 as R2
import strategy as S

COINS = R2.COINS
DAY = 86_400_000
FIXED = {
    "trend_pullback": {"fast": 20, "slow": 50, "rsi": 40, "sl": 2.0, "tp": 3.0, "max_bars": 48},
    "breakout": {"n": 20, "sl": 2.0, "tp": 3.0, "max_bars": 48},
}
INTRADAY_BARS = {"15m": 17000, "1h": 8000, "4h": 3000}
FEE = 0.001


def bias_map(daily_coin, daily_btc):
    """day -> +1 / -1 / 0 using closes up to the previous day only."""
    out = {}
    dc, db = sorted(daily_coin), sorted(daily_btc)
    cc = [daily_coin[d] for d in dc]
    bc = {d: daily_btc[d] for d in db}
    bdays = [d for d in db]
    bclose = [bc[d] for d in bdays]
    bidx = {d: i for i, d in enumerate(bdays)}
    for i in range(100, len(dc)):
        d_prev = dc[i - 1]
        sma50 = sum(cc[i - 50:i]) / 50
        j = bidx.get(d_prev)
        if j is None or j < 100:
            continue
        bsma = sum(bclose[j - 99:j + 1]) / 100
        up = cc[i - 1] > sma50 and bclose[j] > bsma
        dn = cc[i - 1] < sma50 and bclose[j] < bsma
        out[dc[i]] = 1 if up else -1 if dn else 0
    return out


def run(s, name, p, bias=None):
    trades, pos = [], None
    for i in range(S.WARMUP, len(s)):
        bar = s.cs[i]
        if pos:
            px, why = S.check_exit(pos, bar)
            if not px and i - pos["i"] >= p["max_bars"]:
                px, why = bar["close"], "time"
            if px:
                ret = pos["side"] * (px / pos["entry"] - 1) - 2 * FEE
                trades.append({"r": ret / pos["risk"], "ret": ret, "t": bar["open_time"]})
                pos = None
            continue
        d, _ = S.signal(s, i, name, p)
        if not d:
            continue
        if bias is not None and bias.get(bar["open_time"] // DAY, 0) != d:
            continue
        sl, tp = S.exit_levels(s, i, d, p)
        risk = abs(bar["close"] - sl) / bar["close"]
        if risk <= 0:
            continue
        pos = {"side": d, "entry": bar["close"], "sl": sl, "tp": tp, "i": i, "risk": risk}
    return trades


def summarize(trades, span_days):
    if not trades:
        return {"trades": 0, "per_day": 0, "win": 0, "avg_r": 0, "sum_r": 0}
    trades.sort(key=lambda t: t["t"])
    r = [t["r"] for t in trades]
    return {"trades": len(r), "per_day": round(len(r) / max(span_days, 1), 2),
            "win": round(sum(x > 0 for x in r) / len(r), 3), "avg_r": round(sum(r) / len(r), 3),
            "sum_r": round(sum(r), 1)}


def halves(trades, span_days):
    trades.sort(key=lambda t: t["t"])
    h = len(trades) // 2
    return summarize(trades[:h], span_days / 2)["avg_r"], summarize(trades[h:], span_days / 2)["avg_r"]


def main():
    out = {"time": C.now_iso(), "rotation": {}, "intraday": {}}
    daily = {c: R2.daily(c) for c in COINS}

    # ---- A) rotation rebalance speed ----
    days = sorted(daily["BTC"])
    coins = [c for c in COINS if min(daily[c]) <= days[0] + 400]
    days = [d for d in days if all(d in daily[c] for c in coins)]
    closes = {c: [daily[c][d] for d in days] for c in coins}
    n = len(days)
    for reb in (1, 3, 7):
        rets, prev = [], {}
        for i in range(250, n):
            j = i - 1
            if closes["BTC"][j] <= sum(closes["BTC"][j - 99:j + 1]) / 100:
                w = {}
            else:
                k = j - (j % reb)
                score = {c: 0 for c in coins}
                for L in (14, 30, 60):
                    for rk, c in enumerate(sorted(coins, key=lambda c: closes[c][k] / closes[c][k - L], reverse=True)):
                        score[c] += rk
                w = {c: 1 / 3 for c in sorted(score, key=score.get)[:3]}
            turn = sum(abs(w.get(c, 0) - prev.get(c, 0)) for c in coins)
            rets.append(sum(w.get(c, 0) * (closes[c][i] / closes[c][i - 1] - 1) for c in coins) - turn * FEE)
            prev = w
        h = len(rets) // 2
        out["rotation"][f"rebalance_{reb}d"] = {"all": R2.stats(rets), "first": R2.stats(rets[:h]),
                                               "second": R2.stats(rets[h:])}
        print("rotation", reb, out["rotation"][f"rebalance_{reb}d"], flush=True)

    # ---- B) intraday with / without daily filter ----
    for iv, nbars in INTRADAY_BARS.items():
        res = {f"{nm}_{f}": [] for nm in FIXED for f in ("raw", "filtered")}
        span = 0
        for c in COINS:
            try:
                closed, _ = C.split_closed(C.candles({"src": "binance", "sym": c + "USDT"}, iv, nbars))
            except Exception as e:
                print("skip", c, iv, e)
                continue
            s = S.Series(closed)
            span = max(span, (closed[-1]["open_time"] - closed[S.WARMUP]["open_time"]) / DAY)
            bias = bias_map(daily[c], daily["BTC"])
            for nm, p in FIXED.items():
                res[f"{nm}_raw"] += run(s, nm, p)
                res[f"{nm}_filtered"] += run(s, nm, p, bias)
        out["intraday"][iv] = {}
        for k, tr in res.items():
            sm = summarize(tr, span)
            sm["first_half_avg_r"], sm["second_half_avg_r"] = halves(tr, span)
            out["intraday"][iv][k] = sm
            print(iv, k, sm, flush=True)

    C.save_json(C.ROOT / "data" / "research3.json", out)
    L = [f"# Лабораторія v3 {out['time']}", "", "## A. Ротація: як часто перебалансовувати", "",
         "| Варіант | Річна дох. | Просадка | Sharpe | 1-ша пол. | 2-га пол. |", "|---|---|---|---|---|---|"]
    for k, v in out["rotation"].items():
        L.append(f"| {k} | {v['all']['cagr']:+.0%} | {v['all']['max_dd']:.0%} | {v['all']['sharpe']} | "
                 f"{v['first']['cagr']:+.0%} | {v['second']['cagr']:+.0%} |")
    L += ["", "## B. Щоденна торгівля: без фільтра vs з денним фільтром тренду",
          "Параметри фіксовані для всіх монет. R = прибуток у одиницях ризику. Плюс > 0 — перевага є.", "",
          "| Таймфрейм | Стратегія | Угод/день | Win | Сер. R | 1-ша пол. R | 2-га пол. R |", "|---|---|---|---|---|---|---|"]
    for iv, d in out["intraday"].items():
        for k, v in d.items():
            L.append(f"| {iv} | {k} | {v['per_day']} | {v['win']:.0%} | {v['avg_r']:+.3f} | "
                     f"{v['first_half_avg_r']:+.3f} | {v['second_half_avg_r']:+.3f} |")
    (C.ROOT / "reports" / "research3.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
