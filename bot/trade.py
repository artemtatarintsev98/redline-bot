"""Hourly run: read the market, manage the open position, maybe open a new one.

Runs on GitHub Actions. Orders go to the Binance Spot Testnet (virtual money).
If no API keys are set, it runs in 'paper' mode and simulates fills at market price.
"""
import math
import os
import sys
from datetime import datetime, timezone

import common as C
import strategy as S


def log(action, detail=""):
    print(f"[{action}] {detail}")
    C.append_csv(C.RUNLOG, C.RUN_FIELDS, {"time": C.now_iso(), "action": action, "detail": detail})


def main():
    cfg = C.load_json(C.CONFIG, None)
    p, sym = cfg["params"], cfg["symbol"]
    st = C.load_json(C.STATE, {})
    st.setdefault("realized", 0.0)
    st.setdefault("position", None)
    st.setdefault("peak_equity", cfg["capital_usdt"])
    st.setdefault("halted", False)
    st.setdefault("last_candle", 0)

    key, secret = os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET")
    mode = "testnet" if (cfg["mode"] == "testnet" and key and secret) else "paper"
    client = C.Testnet(key, secret) if mode == "testnet" else None
    if client:
        acct = client.account()  # fails loudly if keys or access are wrong
        usdt = next((b["free"] for b in acct.get("balances", []) if b["asset"] == "USDT"), "?")
        print(f"[TESTNET] connected, USDT balance {usdt}")

    candles = C.closed_candles(C.fetch_klines(sym, cfg["interval"], 500))
    ind = S.indicators(candles, p)
    i = len(candles) - 1
    last = candles[i]
    price = last["close"]
    new_candles = [c for c in candles if c["open_time"] > st["last_candle"]]

    # ---- manage the open position ----
    pos = st["position"]
    if pos:
        reason = None
        for c in new_candles:
            if c["low"] <= pos["sl"]:
                reason = "stop"
                break
            if c["high"] >= pos["tp"]:
                reason = "target"
                break
        if not reason and S.trend_broken(candles, ind, i):
            reason = "trend"
        if reason:
            if client:
                exit_px, _ = C.fill_price(client.market_sell(sym, pos["qty"]))
            else:
                exit_px = price
            gross = (exit_px - pos["entry"]) * pos["qty"]
            fees = (exit_px + pos["entry"]) * pos["qty"] * cfg["fee_rate"]
            pnl = gross - fees
            st["realized"] += pnl
            C.append_csv(C.TRADES, C.TRADE_FIELDS, {
                "open_time": pos["time"], "close_time": C.now_iso(), "symbol": sym,
                "entry": round(pos["entry"], 2), "exit": round(exit_px, 2), "qty": pos["qty"],
                "pnl_usdt": round(pnl, 2), "pnl_pct": round(pnl / (pos["entry"] * pos["qty"]) * 100, 2),
                "reason": reason, "params_version": pos["params_version"], "mode": mode})
            log("EXIT", f"{reason} @ {exit_px:.2f} pnl {pnl:+.2f} USDT")
            st["position"] = pos = None

    # ---- equity, daily loss limit, kill switch ----
    unreal = (price - pos["entry"]) * pos["qty"] if pos else 0.0
    equity = cfg["capital_usdt"] + st["realized"] + unreal
    st["peak_equity"] = max(st["peak_equity"], equity)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if st.get("day") != today:
        st["day"], st["day_start_equity"] = today, equity
    day_loss = 1 - equity / st["day_start_equity"]
    if 1 - equity / st["peak_equity"] >= cfg["kill_switch_drawdown_pct"] and not st["halted"]:
        st["halted"] = True
        log("HALT", f"drawdown limit hit, equity {equity:.2f}. Trading stopped until reviewed.")

    # ---- maybe open a new position (only once per new candle) ----
    if not pos and new_candles and not st["halted"]:
        if day_loss >= cfg["daily_loss_limit_pct"]:
            log("SKIP", "daily loss limit reached")
        elif S.entry_signal(candles, ind, i, p):
            sl_dist = p["sl_atr"] * ind["atr"][i]
            risk_usdt = equity * cfg["risk_per_trade"]
            quote = min(risk_usdt / sl_dist * price, equity * cfg["max_position_pct"])
            if quote >= 10:  # exchange minimum notional
                if client:
                    entry_px, qty = C.fill_price(client.market_buy_quote(sym, quote))
                    step = client.lot_step(sym)
                    qty = math.floor(qty / step) * step
                else:
                    entry_px, qty = price, round(quote / price, 6)
                st["position"] = pos = {
                    "time": C.now_iso(), "entry": entry_px, "qty": qty,
                    "sl": entry_px - sl_dist, "tp": entry_px + p["tp_atr"] * ind["atr"][i],
                    "params_version": cfg["params_version"]}
                log("ENTRY", f"buy {qty} @ {entry_px:.2f}, SL {pos['sl']:.2f}, TP {pos['tp']:.2f}")
        else:
            log("WAIT", f"[{mode}] no signal, price {price:.2f}, RSI {ind['rsi'][i]:.1f}")

    st["last_candle"] = last["open_time"]
    C.append_csv(C.EQUITY, C.EQUITY_FIELDS, {
        "time": C.now_iso(), "price": round(price, 2), "equity": round(equity, 2),
        "realized": round(st["realized"], 2), "unrealized": round(unreal, 2), "in_position": int(bool(pos))})
    st["mode"] = mode
    C.save_json(C.STATE, st)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("ERROR", str(e)[:300])
        sys.exit(1)
