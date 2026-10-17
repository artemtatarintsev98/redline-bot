"""Daily active trading on several timeframes (paper, its own 1000 USDT).

Every scan it looks at 15m / 1h / 4h / 1d candles of the crypto coins and opens trades
from FIXED-parameter strategies, but only in the direction of the daily trend:
  long  only when the coin's daily close > SMA50 and BTC's > SMA100
  short only when both are below
Which timeframe+strategy pairs are switched on is decided by the research lab (ENABLED).
Risk: 0.5% of the sleeve per trade (scaled by trust), max 4 open, max 2 per direction.
"""
import time

import common as C
import strategy as S
import trust as T

COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "LTC", "DOT", "TRX"]
FIXED = {
    "trend_pullback": {"fast": 20, "slow": 50, "rsi": 40, "sl": 2.0, "tp": 3.0, "max_bars": 48},
    "breakout": {"n": 20, "sl": 2.0, "tp": 3.0, "max_bars": 48},
}
CAPITAL, RISK, MAX_RISK, FEE = 1000.0, 0.005, 0.01, 0.001
MAX_POS, MAX_SAME_SIDE, COOLDOWN_MIN = 4, 2, 120
FETCH = {"15m": 400, "1h": 400, "4h": 400, "1d": 300}
C.BAR_MS.setdefault("1d", 86_400_000)
STATE = C.ROOT / "data" / "multi_state.json"
TRADES = C.ROOT / "data" / "multi_trades.csv"
EQUITY = C.ROOT / "data" / "multi_equity.csv"
TRADE_FIELDS = C.TRADE_FIELDS + ["interval"]
EQUITY_FIELDS = ["time", "equity", "realized", "unrealized", "open_positions"]


def log(action, detail):
    tag = "SHADOW" if "shadow" in str(TRADES) else "MULTI"
    print(f"[{tag} {action}] {detail}")
    C.append_csv(C.RUNLOG, C.RUN_FIELDS, {"time": C.now_iso(), "action": f"{tag}-{action}", "detail": detail})


_CACHE = {}


def get(sym, iv, n):
    if (sym, iv) not in _CACHE:
        _CACHE[(sym, iv)] = C.split_closed(C.candles({"src": "binance", "sym": sym + "USDT"}, iv, n))
    return _CACHE[(sym, iv)]


def daily_bias(daily, btc):
    """+1 / -1 / 0 from the last closed daily candles."""
    c = [x["close"] for x in daily]
    b = [x["close"] for x in btc]
    if len(c) < 50 or len(b) < 100:
        return 0
    up = c[-1] > sum(c[-50:]) / 50 and b[-1] > sum(b[-100:]) / 100
    dn = c[-1] < sum(c[-50:]) / 50 and b[-1] < sum(b[-100:]) / 100
    return 1 if up else -1 if dn else 0


def close(st, pos, px, reason):
    pnl = pos["side"] * (px - pos["entry"]) * pos["qty"] - FEE * (px + pos["entry"]) * pos["qty"]
    st["realized"] += pnl
    if reason == "stop":
        st["cooldown"][pos["asset"]] = time.time() + COOLDOWN_MIN * 60
    C.append_csv(TRADES, TRADE_FIELDS, {
        "open_time": pos["time"], "close_time": C.now_iso(), "asset": pos["asset"], "strategy": pos["strategy"],
        "tier": "multi", "side": "long" if pos["side"] == 1 else "short", "entry": round(pos["entry"], 6),
        "exit": round(px, 6), "qty": round(pos["qty"], 8), "pnl_usdt": round(pnl, 2),
        "pnl_pct": round(pos["side"] * (px / pos["entry"] - 1) * 100, 2), "reason": reason,
        "bars": pos.get("bars", 0), "risk_usdt": round(pos["risk_usdt"], 2),
        "r": round(pnl / pos["risk_usdt"], 2), "interval": pos["interval"]})
    log("EXIT", f"{pos['asset']} {pos['interval']} {pos['strategy']} {reason} @ {px:.6g} pnl {pnl:+.2f}")


def main():
    """Run the live sleeve and the shadow sleeve (records what it would do, no real effect)."""
    global STATE, TRADES, EQUITY
    cfg = C.load_json(C.CONFIG, {})
    _CACHE.clear()
    base = C.ROOT / "data"
    for key, prefix in (("multi_enabled", "multi"), ("multi_shadow", "shadow")):
        enabled = [tuple(x) for x in cfg.get(key, [])]
        if not enabled:
            continue
        STATE, TRADES, EQUITY = (base / f"{prefix}_state.json", base / f"{prefix}_trades.csv",
                                 base / f"{prefix}_equity.csv")
        run(enabled, prefix)


def run(ENABLED, prefix):
    st = C.load_json(STATE, {"realized": 0.0, "positions": [], "last_bar": {}, "cooldown": {}})
    ivs = sorted({iv for iv, _ in ENABLED})
    data, live = {}, {}
    dailies = {}
    for c in COINS:
        try:
            dailies[c], _ = get(c, "1d", FETCH["1d"])
            for iv in ivs:
                closed, forming = get(c, iv, FETCH[iv])
                data[(c, iv)] = S.Series(closed)
                live[c] = forming["close"] if forming else closed[-1]["close"]
        except Exception as e:
            log("DATA", f"{c}: {str(e)[:100]}")

    # manage open positions
    keep = []
    for pos in st["positions"]:
        s = data.get((pos["asset"], pos["interval"]))
        if not s:
            keep.append(pos)
            continue
        px = why = None
        for bar in s.cs:
            if bar["open_time"] <= pos["last_checked"]:
                continue
            pos["last_checked"] = bar["open_time"]
            pos["bars"] = pos.get("bars", 0) + 1
            px, why = S.check_exit(pos, bar)
            if px:
                break
            if pos["bars"] >= pos["max_bars"]:
                px, why = bar["close"], "time"
                break
        if not px:
            lp, sd = live[pos["asset"]], pos["side"]
            if (sd == 1 and lp <= pos["sl"]) or (sd == -1 and lp >= pos["sl"]):
                px, why = lp, "stop"
            elif (sd == 1 and lp >= pos["tp"]) or (sd == -1 and lp <= pos["tp"]):
                px, why = lp, "target"
        if px:
            close(st, pos, px, why)
        else:
            keep.append(pos)
    st["positions"] = keep

    unreal = sum(p["side"] * (live.get(p["asset"], p["entry"]) - p["entry"]) * p["qty"] for p in st["positions"])
    equity = CAPITAL + st["realized"] + unreal
    st["cooldown"] = {a: t for a, t in st["cooldown"].items() if t > time.time()}
    trust = T.table(TRADES)

    # new entries, one decision per freshly closed bar
    for c in COINS:
        if "BTC" not in dailies or c not in dailies:
            continue
        bias = daily_bias(dailies[c], dailies["BTC"])
        for iv, name in ENABLED:
            s = data.get((c, iv))
            key = f"{c}|{iv}|{name}"
            if not s or s.cs[-1]["open_time"] <= st["last_bar"].get(key, 0):
                continue
            st["last_bar"][key] = s.cs[-1]["open_time"]
            if not bias or len(st["positions"]) >= MAX_POS or c in st["cooldown"]:
                continue
            if any(p["asset"] == c for p in st["positions"]):
                continue
            d, _ = S.signal(s, len(s) - 1, name, FIXED[name])
            if d != bias:
                continue
            if sum(p["side"] == d for p in st["positions"]) >= MAX_SAME_SIDE:
                continue
            tr = T.get(trust, c, f"{iv}:{name}")
            if tr["benched"]:
                continue
            i = len(s) - 1
            sl, tp = S.exit_levels(s, i, d, FIXED[name])
            entry = live[c]
            shift = entry - s.c[i]
            sl, tp = sl + shift, tp + shift
            dist = abs(entry - sl)
            if dist <= 0:
                continue
            risk_usdt = equity * min(RISK * tr["mult"], MAX_RISK)
            qty = min(risk_usdt / dist, equity * 0.5 / entry)
            st["positions"].append({"asset": c, "strategy": f"{iv}:{name}", "interval": iv, "side": d,
                                    "entry": entry, "qty": qty, "sl": sl, "tp": tp, "time": C.now_iso(),
                                    "last_checked": s.cs[i]["open_time"], "max_bars": FIXED[name]["max_bars"],
                                    "bars": 0, "risk_usdt": qty * dist})
            log("ENTRY", f"{c} {'LONG' if d == 1 else 'SHORT'} {iv} {name} @ {entry:.6g} "
                         f"SL {sl:.6g} TP {tp:.6g} risk {qty * dist:.2f}")

    unreal = sum(p["side"] * (live.get(p["asset"], p["entry"]) - p["entry"]) * p["qty"] for p in st["positions"])
    C.append_csv(EQUITY, EQUITY_FIELDS, {"time": C.now_iso(), "equity": round(CAPITAL + st["realized"] + unreal, 2),
                                         "realized": round(st["realized"], 2), "unrealized": round(unreal, 2),
                                         "open_positions": len(st["positions"])})
    C.save_json(STATE, st)


if __name__ == "__main__":
    main()
