"""Research lab v2: simple, well-documented strategies with FIXED parameters on daily data.

Lab v1 showed that tuning many parameters per coin overfits (good on history, bad on new data).
Here nothing is tuned per coin: every rule is tested with the same parameters on all coins,
across ~5 years, and reported per half so we can see whether an edge is stable.

Strategies (daily bars, long or cash, fees 0.1% per side):
  trend_N    : hold a coin while yesterday's close > SMA(N)
  momo_L     : every 7 days hold the 3 coins with best L-day return, only while BTC > SMA(100)
  hold       : buy and hold every coin (benchmark)
"""
import math
import sys

import requests

import common as C

COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "LTC", "DOT", "TRX"]
FEE = 0.001
DAYS = 1900


def daily(sym):
    out, end = [], None
    while len(out) < DAYS:
        params = {"symbol": sym + "USDT", "interval": "1d", "limit": 1000}
        if end:
            params["endTime"] = end
        r = requests.get(C.BINANCE_URLS[0] + "/api/v3/klines", params=params, timeout=20)
        r.raise_for_status()
        b = r.json()
        if not b:
            break
        out = b + out
        end = b[0][0] - 1
        if len(b) < 1000:
            break
    return {k[0] // 86_400_000: float(k[4]) for k in out[-DAYS:]}


def sma(xs, n, i):
    return sum(xs[i - n + 1:i + 1]) / n if i >= n - 1 else None


def stats(rets):
    if not rets:
        return {"cagr": 0, "max_dd": 0, "sharpe": 0}
    eq = peak = 1.0
    dd = 0.0
    for r in rets:
        eq *= 1 + r
        peak = max(peak, eq)
        dd = max(dd, 1 - eq / peak)
    yrs = len(rets) / 365
    mu = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / len(rets)) or 1e-9
    return {"cagr": round(eq ** (1 / yrs) - 1, 3) if eq > 0 else -1, "max_dd": round(dd, 3),
            "sharpe": round(mu / sd * math.sqrt(365), 2)}


def main():
    px = {}
    for c in COINS:
        try:
            px[c] = daily(c)
        except Exception as e:
            print("skip", c, e)
    days = sorted(set.intersection(*[set(v) for k, v in px.items() if k == "BTC"]))
    first = days[0]
    coins = [c for c in px if min(px[c]) <= first + 400]  # need ~4+ years of history
    days = [d for d in days if all(d in px[c] for c in coins)]
    closes = {c: [px[c][d] for d in days] for c in coins}
    n = len(days)
    print("coins", coins, "days", n)

    def portfolio(weights_fn, start=250):
        rets, prev_w = [], {}
        for i in range(start, n):
            w = weights_fn(i - 1)  # decide on yesterday's close, earn today's return
            turnover = sum(abs(w.get(c, 0) - prev_w.get(c, 0)) for c in coins)
            r = sum(w.get(c, 0) * (closes[c][i] / closes[c][i - 1] - 1) for c in coins) - turnover * FEE
            rets.append(r)
            prev_w = w
        return rets

    tests = {"hold": lambda i: {c: 1 / len(coins) for c in coins}}
    for N in (20, 50, 100, 200):
        tests[f"trend_{N}"] = (lambda N: lambda i: {c: 1 / len(coins) for c in coins
                                                     if closes[c][i] > (sma(closes[c], N, i) or 1e18)})(N)
    for L in (14, 30, 60):
        def momo(i, L=L):
            if closes["BTC"][i] <= (sma(closes["BTC"], 100, i) or 1e18):
                return {}
            day = i - (i % 7)  # rebalance weekly
            ranked = sorted(coins, key=lambda c: closes[c][day] / closes[c][day - L], reverse=True)
            return {c: 1 / 3 for c in ranked[:3]}
        tests[f"momo_{L}"] = momo

    res = {}
    for name, fn in tests.items():
        rets = portfolio(fn)
        h = len(rets) // 2
        res[name] = {"all": stats(rets), "first_half": stats(rets[:h]), "second_half": stats(rets[h:])}
        print(name, res[name], flush=True)
    C.save_json(C.ROOT / "data" / "research2.json", {"time": C.now_iso(), "coins": coins, "days": n, "results": res})
    L = [f"# Лабораторія v2 {C.now_iso()}", "", f"Денні свічки, {len(coins)} монет, ~{(n - 250) / 365:.1f} років. "
         "Параметри однакові для всіх монет, нічого не підбиралось.", "",
         "| Стратегія | Річна дох. | Макс. просадка | Sharpe | 1-ша половина | 2-га половина |", "|---|---|---|---|---|---|"]
    for name, r in res.items():
        a, f, s = r["all"], r["first_half"], r["second_half"]
        L.append(f"| {name} | {a['cagr']:+.0%} | {a['max_dd']:.0%} | {a['sharpe']} | "
                 f"{f['cagr']:+.0%} / dd {f['max_dd']:.0%} | {s['cagr']:+.0%} / dd {s['max_dd']:.0%} |")
    (C.ROOT / "reports").mkdir(exist_ok=True)
    (C.ROOT / "reports" / "research2.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("research2 failed:", e)
        sys.exit(1)
