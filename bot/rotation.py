"""Momentum rotation (paper). The strategy that survived Lab v2.

Once per day after the daily close:
  - regime: BTC close > SMA(100). If not -> everything to cash.
  - otherwise every 7 days hold the 3 coins with the strongest momentum
    (average rank of 14-, 30- and 60-day returns), equal weight.
Marks the portfolio to the live price every ~15 minutes.
"""
import time

import common as C

COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "LTC", "DOT", "TRX"]
LOOKBACKS = (14, 30, 60)
TOP_N, REBALANCE_DAYS, REGIME_SMA = 3, 7, 100
FEE, CAPITAL = 0.001, 1000.0
STATE = C.ROOT / "data" / "rotation_state.json"
TRADES = C.ROOT / "data" / "rotation_trades.csv"
EQUITY = C.ROOT / "data" / "rotation_equity.csv"
TRADE_FIELDS = ["time", "coin", "side", "qty", "price", "value_usdt", "fee_usdt", "reason"]
EQUITY_FIELDS = ["time", "equity", "cash", "holdings", "regime"]
RUN_EVERY = 900


def log(action, detail):
    print(f"[ROT {action}] {detail}")
    C.append_csv(C.RUNLOG, C.RUN_FIELDS, {"time": C.now_iso(), "action": f"ROT-{action}", "detail": detail})


def daily_closes():
    out, live = {}, {}
    for c in COINS:
        cs = C._binance(c + "USDT", "1d", 120)
        closed, forming = C.split_closed(cs)
        out[c] = closed
        live[c] = forming["close"] if forming else closed[-1]["close"]
    return out, live


def pick(closes):
    """Top coins by average momentum rank, or [] when the BTC regime is off."""
    btc = [x["close"] for x in closes["BTC"]]
    if len(btc) < REGIME_SMA or btc[-1] <= sum(btc[-REGIME_SMA:]) / REGIME_SMA:
        return [], False
    ranks = {c: 0.0 for c in closes if len(closes[c]) > max(LOOKBACKS)}
    for L in LOOKBACKS:
        order = sorted(ranks, key=lambda c: closes[c][-1]["close"] / closes[c][-1 - L]["close"], reverse=True)
        for r, c in enumerate(order):
            ranks[c] += r
    return sorted(ranks, key=ranks.get)[:TOP_N], True


def main(force=False):
    st = C.load_json(STATE, {"cash": CAPITAL, "holdings": {}, "last_day": 0, "last_rebalance": 0, "last_run": 0})
    if not force and time.time() - st["last_run"] < RUN_EVERY:
        return
    st["last_run"] = time.time()
    closes, live = daily_closes()
    day = closes["BTC"][-1]["open_time"] // 86_400_000
    value = lambda: st["cash"] + sum(q * live[c] for c, q in st["holdings"].items())

    regime = None
    if day > st["last_day"]:  # a new daily candle closed: decide
        st["last_day"] = day
        targets, regime = pick(closes)
        held = sorted(st["holdings"])
        due = day - st["last_rebalance"] >= REBALANCE_DAYS or not held
        if not regime:
            targets = []
            reason = "regime off (BTC below SMA100) -> cash"
        elif due:
            reason = f"weekly rebalance -> {', '.join(targets)}"
        else:
            targets = held
            reason = None
        if reason and sorted(targets) != held:
            eq = value()
            for c in list(st["holdings"]):  # sell what is no longer wanted
                if c not in targets:
                    q = st["holdings"].pop(c)
                    v = q * live[c]
                    st["cash"] += v * (1 - FEE)
                    C.append_csv(TRADES, TRADE_FIELDS, {"time": C.now_iso(), "coin": c, "side": "sell", "qty": round(q, 8),
                                                        "price": live[c], "value_usdt": round(v, 2),
                                                        "fee_usdt": round(v * FEE, 2), "reason": reason})
            for c in targets:  # buy the new ones with equal slices
                if c in st["holdings"]:
                    continue
                v = min(eq / len(targets), st["cash"])
                if v < 1:
                    continue
                st["cash"] -= v
                st["holdings"][c] = v * (1 - FEE) / live[c]
                C.append_csv(TRADES, TRADE_FIELDS, {"time": C.now_iso(), "coin": c, "side": "buy",
                                                    "qty": round(st["holdings"][c], 8), "price": live[c],
                                                    "value_usdt": round(v, 2), "fee_usdt": round(v * FEE, 2),
                                                    "reason": reason})
            st["last_rebalance"] = day
            log("REBALANCE", f"{reason}; equity {value():.2f}")
        elif reason and not regime:
            st["last_rebalance"] = day
    st["regime"] = st.get("regime") if regime is None else regime
    C.append_csv(EQUITY, EQUITY_FIELDS, {"time": C.now_iso(), "equity": round(value(), 2),
                                         "cash": round(st["cash"], 2), "holdings": "+".join(sorted(st["holdings"])),
                                         "regime": st.get("regime")})
    C.save_json(STATE, st)


if __name__ == "__main__":
    main(force=True)
