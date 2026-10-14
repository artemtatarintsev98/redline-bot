"""Strategy logic shared by the live bot and the learning/backtest step.

Long-only trend-pullback strategy:
  - trend filter: close > EMA(slow) and EMA(fast) > EMA(slow)
  - entry: RSI crosses back up through `rsi_entry` (a pullback inside an uptrend ends)
  - exit: stop-loss at entry - sl_atr*ATR, take-profit at entry + tp_atr*ATR,
          or trend break (close < EMA(slow))
"""


def ema(values, length):
    out, k, prev = [], 2 / (length + 1), None
    for v in values:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(closes, length):
    out = [50.0] * len(closes)
    if len(closes) <= length:
        return out
    gains = losses = 0.0
    for i in range(1, length + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / length, losses / length
    out[length] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(length + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (length - 1) + max(d, 0)) / length
        al = (al * (length - 1) + max(-d, 0)) / length
        out[i] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(highs, lows, closes, length):
    trs = []
    for i in range(len(closes)):
        if i == 0:
            trs.append(highs[i] - lows[i])
        else:
            trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    out, prev = [], None
    for i, tr in enumerate(trs):
        prev = tr if prev is None else (prev * (length - 1) + tr) / length
        out.append(prev)
    return out


def indicators(candles, p):
    c = [x["close"] for x in candles]
    h = [x["high"] for x in candles]
    l = [x["low"] for x in candles]
    return {
        "ema_fast": ema(c, p["ema_fast"]),
        "ema_slow": ema(c, p["ema_slow"]),
        "rsi": rsi(c, p["rsi_len"]),
        "atr": atr(h, l, c, p["atr_len"]),
    }


def warmup(p):
    return max(p["ema_slow"], p["rsi_len"], p["atr_len"]) * 3


def entry_signal(candles, ind, i, p):
    """True if a long entry should open on the close of candle i."""
    if i < 1:
        return False
    close = candles[i]["close"]
    uptrend = close > ind["ema_slow"][i] and ind["ema_fast"][i] > ind["ema_slow"][i]
    rsi_cross = ind["rsi"][i - 1] < p["rsi_entry"] <= ind["rsi"][i]
    return uptrend and rsi_cross


def trend_broken(candles, ind, i):
    return candles[i]["close"] < ind["ema_slow"][i]


def backtest(candles, p, fee_rate=0.001, start=None, end=None):
    """Simulate the strategy. Returns stats dict and list of trades.

    Each trade risks a fixed fraction of 1.0 notional; returns are summed as %.
    """
    ind = indicators(candles, p)
    start = max(start or 0, warmup(p))
    end = len(candles) if end is None else end
    trades, pos = [], None
    for i in range(start, end):
        cdl = candles[i]
        if pos:
            exit_price, reason = None, None
            if cdl["low"] <= pos["sl"]:
                exit_price, reason = pos["sl"], "stop"
            elif cdl["high"] >= pos["tp"]:
                exit_price, reason = pos["tp"], "target"
            elif trend_broken(candles, ind, i):
                exit_price, reason = cdl["close"], "trend"
            if exit_price:
                ret = (exit_price / pos["entry"] - 1) - 2 * fee_rate
                trades.append({"ret": ret, "reason": reason, "bars": i - pos["i"]})
                pos = None
            continue
        if entry_signal(candles, ind, i, p):
            a = ind["atr"][i]
            pos = {"entry": cdl["close"], "sl": cdl["close"] - p["sl_atr"] * a,
                   "tp": cdl["close"] + p["tp_atr"] * a, "i": i}
    return stats(trades), trades


def stats(trades):
    n = len(trades)
    if n == 0:
        return {"trades": 0, "win_rate": 0, "total_ret": 0, "max_dd": 0, "profit_factor": 0, "score": -1}
    wins = [t["ret"] for t in trades if t["ret"] > 0]
    losses = [-t["ret"] for t in trades if t["ret"] <= 0]
    eq, peak, dd = 1.0, 1.0, 0.0
    for t in trades:
        eq *= 1 + t["ret"]
        peak = max(peak, eq)
        dd = max(dd, 1 - eq / peak)
    pf = (sum(wins) / sum(losses)) if losses and sum(losses) > 0 else (9.99 if wins else 0)
    total = eq - 1
    return {
        "trades": n,
        "win_rate": round(len(wins) / n, 3),
        "total_ret": round(total, 4),
        "max_dd": round(dd, 4),
        "profit_factor": round(pf, 2),
        "score": round(total - 0.5 * dd, 4),
    }
