"""Indicators, the three strategies, and the backtester shared by live scanning and learning.

Every strategy can go long (+1) or short (-1). A signal comes with a strength
(how convincing it is, used to rank candidates across assets). Exits are shared:
stop-loss at sl_atr*ATR, take-profit at tp_atr*ATR, or a time limit.
"""
import itertools


# ---------------- indicators ----------------

def ema(v, n):
    out, k, prev = [], 2 / (n + 1), None
    for x in v:
        prev = x if prev is None else x * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(c, n=14):
    out = [50.0] * len(c)
    if len(c) <= n:
        return out
    g = sum(max(c[i] - c[i - 1], 0) for i in range(1, n + 1)) / n
    l = sum(max(c[i - 1] - c[i], 0) for i in range(1, n + 1)) / n
    for i in range(n, len(c)):
        if i > n:
            d = c[i] - c[i - 1]
            g = (g * (n - 1) + max(d, 0)) / n
            l = (l * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def atr(h, l, c, n=14):
    out, prev = [], None
    for i in range(len(c)):
        tr = h[i] - l[i] if i == 0 else max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        prev = tr if prev is None else (prev * (n - 1) + tr) / n
        out.append(prev)
    return out


class Series:
    """Candles plus lazily cached indicators."""

    def __init__(self, cs):
        self.cs = cs
        self.c = [x["close"] for x in cs]
        self.h = [x["high"] for x in cs]
        self.l = [x["low"] for x in cs]
        self._cache = {}

    def _get(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def ema(self, n):
        return self._get(("ema", n), lambda: ema(self.c, n))

    def rsi(self, n=14):
        return self._get(("rsi", n), lambda: rsi(self.c, n))

    def atr(self, n=14):
        return self._get(("atr", n), lambda: atr(self.h, self.l, self.c, n))

    def __len__(self):
        return len(self.cs)


# ---------------- strategies ----------------
# each returns (direction, strength) for the close of bar i

def trend_pullback(s, i, p):
    f, sl, r, a = s.ema(p["fast"]), s.ema(p["slow"]), s.rsi(), s.atr()
    lvl = p["rsi"]
    gap = abs(f[i] - sl[i]) / a[i] if a[i] else 0
    if s.c[i] > sl[i] and f[i] > sl[i] and r[i - 1] < lvl <= r[i]:
        return 1, min(gap, 5)
    if s.c[i] < sl[i] and f[i] < sl[i] and r[i - 1] > 100 - lvl >= r[i]:
        return -1, min(gap, 5)
    return 0, 0


def breakout(s, i, p):
    n, a = p["n"], s.atr()
    hi, lo = max(s.h[i - n:i]), min(s.l[i - n:i])
    trend = s.ema(100)
    if s.c[i] > hi and s.c[i - 1] <= hi and s.c[i] > trend[i]:
        return 1, min((s.c[i] - hi) / a[i] + 1, 5) if a[i] else 0
    if s.c[i] < lo and s.c[i - 1] >= lo and s.c[i] < trend[i]:
        return -1, min((lo - s.c[i]) / a[i] + 1, 5) if a[i] else 0
    return 0, 0


def mean_revert(s, i, p):
    r, trend = s.rsi(), s.ema(200)
    lo = p["lo"]
    if r[i - 1] < lo <= r[i] and s.c[i] > trend[i]:
        return 1, (lo - min(r[i - 3:i])) / 10 + 1
    if r[i - 1] > 100 - lo >= r[i] and s.c[i] < trend[i]:
        return -1, (max(r[i - 3:i]) - (100 - lo)) / 10 + 1
    return 0, 0


STRATEGIES = {"trend_pullback": trend_pullback, "breakout": breakout, "mean_revert": mean_revert}

GRIDS = {
    "trend_pullback": {"fast": [10, 20], "slow": [50, 100], "rsi": [40, 45],
                       "sl": [1.5, 2.0], "tp": [2.0, 3.0], "max_bars": [48]},
    "breakout": {"n": [20, 48], "sl": [1.5, 2.0], "tp": [2.0, 3.0, 4.0], "max_bars": [48]},
    "mean_revert": {"lo": [25, 30], "sl": [1.5, 2.0], "tp": [1.0, 1.5], "max_bars": [24]},
}
WARMUP = 210
MAX_STRENGTH = 3.0  # skip entries where price already ran too far (chasing)


def grid(name):
    g = GRIDS[name]
    keys = list(g)
    for vals in itertools.product(*(g[k] for k in keys)):
        yield dict(zip(keys, vals))


def signal(s, i, name, p):
    if i < WARMUP or i >= len(s):
        return 0, 0
    d, strength = STRATEGIES[name](s, i, p)
    if d and strength > MAX_STRENGTH:
        return 0, 0
    return d, strength


def exit_levels(s, i, side, p):
    a = s.atr()[i]
    px = s.c[i]
    return px - side * p["sl"] * a, px + side * p["tp"] * a


def check_exit(pos, bar):
    """Exit (price, reason) if a closed bar touched SL or TP. SL wins if both."""
    side = pos["side"]
    if (side == 1 and bar["low"] <= pos["sl"]) or (side == -1 and bar["high"] >= pos["sl"]):
        return pos["sl"], "stop"
    if (side == 1 and bar["high"] >= pos["tp"]) or (side == -1 and bar["low"] <= pos["tp"]):
        return pos["tp"], "target"
    return None, None


def backtest(s, name, p, fee, start=0, end=None):
    end = len(s) if end is None else end
    trades, pos = [], None
    for i in range(max(start, WARMUP), end):
        bar = s.cs[i]
        if pos:
            px, why = check_exit(pos, bar)
            if not px and i - pos["i"] >= p["max_bars"]:
                px, why = bar["close"], "time"
            if px:
                ret = pos["side"] * (px / pos["entry"] - 1) - 2 * fee
                trades.append({"ret": ret, "reason": why})
                pos = None
            continue
        d, _ = signal(s, i, name, p)
        if d:
            sl, tp = exit_levels(s, i, d, p)
            pos = {"side": d, "entry": bar["close"], "sl": sl, "tp": tp, "i": i}
    return stats(trades)


def stats(trades):
    n = len(trades)
    if not n:
        return {"trades": 0, "win_rate": 0, "ret": 0, "max_dd": 0, "pf": 0, "score": -1}
    w = [t["ret"] for t in trades if t["ret"] > 0]
    lo = [-t["ret"] for t in trades if t["ret"] <= 0]
    eq = peak = 1.0
    dd = 0.0
    for t in trades:
        eq *= 1 + t["ret"]
        peak = max(peak, eq)
        dd = max(dd, 1 - eq / peak)
    pf = sum(w) / sum(lo) if lo and sum(lo) > 0 else (9.99 if w else 0)
    return {"trades": n, "win_rate": round(len(w) / n, 3), "ret": round(eq - 1, 4),
            "max_dd": round(dd, 4), "pf": round(pf, 2), "score": round(eq - 1 - 0.5 * dd, 4)}
