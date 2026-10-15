"""Scanner run (every ~5 minutes on GitHub Actions). Paper trading on real prices.

1. Pull fresh 15m candles for every asset the learner approved (+ any open position).
2. Manage open positions: stop / target / time limit, also against the live price.
3. Score new signals across all assets, rank them, open the best ones within risk limits.
"""
import sys
import time
from datetime import datetime, timezone

import common as C
import strategy as S
import trust as T

FETCH = {"binance": 400, "yahoo": 1200}
FRESH_MS = 60 * 60_000  # ignore markets whose last closed bar is older than 1h (closed market)
MAX_SAME_SIDE = {"crypto": 2}  # crypto coins move together: cap positions per direction
COOLDOWN_MIN = 120  # after a stop, leave that asset alone for 2 hours
MAX_RISK = 0.02  # hard cap per trade, even for the most trusted combo


def log(action, detail=""):
    print(f"[{action}] {detail}")
    C.append_csv(C.RUNLOG, C.RUN_FIELDS, {"time": C.now_iso(), "action": action, "detail": detail})


def close_pos(cfg, st, pos, px, reason, fees):
    fee = fees[pos["asset"]]
    pnl = pos["side"] * (px - pos["entry"]) * pos["qty"] - fee * (px + pos["entry"]) * pos["qty"]
    st["realized"] += pnl
    if reason == "stop":
        st.setdefault("cooldown", {})[pos["asset"]] = int(time.time()) + COOLDOWN_MIN * 60
    C.append_csv(C.TRADES, C.TRADE_FIELDS, {
        "open_time": pos["time"], "close_time": C.now_iso(), "asset": pos["asset"],
        "strategy": pos["strategy"], "tier": pos["tier"], "side": "long" if pos["side"] == 1 else "short",
        "entry": round(pos["entry"], 6), "exit": round(px, 6), "qty": round(pos["qty"], 8),
        "pnl_usdt": round(pnl, 2), "pnl_pct": round(pos["side"] * (px / pos["entry"] - 1) * 100, 2),
        "reason": reason, "bars": pos.get("bars", 0), "risk_usdt": round(pos.get("risk_usdt", 0), 2),
        "r": round(pnl / pos["risk_usdt"], 2) if pos.get("risk_usdt") else ""})
    log("EXIT", f"{pos['asset']} {pos['strategy']} {reason} @ {px:.6g} pnl {pnl:+.2f}")


def main():
    cfg = C.load_json(C.CONFIG, None)
    models = C.load_json(C.MODELS, {"combos": []})
    st = C.load_json(C.STATE, {})
    for k, v in {"realized": 0.0, "positions": [], "peak_equity": cfg["capital_usdt"],
                 "halted": False, "last_bar": {}}.items():
        st.setdefault(k, v)
    assets = {a["id"]: a for a in cfg["assets"]}
    fees = {k: cfg["fees"][a["cls"]] for k, a in assets.items()}
    combos = [c for c in models["combos"] if c["asset"] in assets]
    need = {c["asset"] for c in combos} | {p["asset"] for p in st["positions"]}

    series, live_px, ok = {}, {}, []
    now_ms = int(time.time() * 1000)
    for aid in sorted(need):
        try:
            closed, forming = C.split_closed(C.candles(assets[aid], cfg["interval"], FETCH[assets[aid]["src"]]))
        except Exception as e:
            log("DATA", f"{aid} failed: {str(e)[:120]}")
            continue
        if not closed:
            continue
        series[aid] = S.Series(closed)
        live_px[aid] = forming["close"] if forming else closed[-1]["close"]
        ok.append(aid)

    # ---- manage open positions ----
    keep = []
    for pos in st["positions"]:
        s = series.get(pos["asset"])
        if not s:
            keep.append(pos)
            continue
        px = reason = None
        for bar in s.cs:
            if bar["open_time"] <= pos["last_checked"]:
                continue
            pos["bars"] = pos.get("bars", 0) + 1
            pos["last_checked"] = bar["open_time"]
            px, reason = S.check_exit(pos, bar)
            if px:
                break
            if pos["bars"] >= pos["max_bars"]:
                px, reason = bar["close"], "time"
                break
        if not px:  # real-time check against the live price
            lp, side = live_px[pos["asset"]], pos["side"]
            if (side == 1 and lp <= pos["sl"]) or (side == -1 and lp >= pos["sl"]):
                px, reason = lp, "stop"
            elif (side == 1 and lp >= pos["tp"]) or (side == -1 and lp <= pos["tp"]):
                px, reason = lp, "target"
        if px:
            close_pos(cfg, st, pos, px, reason, fees)
        else:
            keep.append(pos)
    st["positions"] = keep

    # ---- equity and safety limits ----
    def unreal():
        return sum(p["side"] * (live_px.get(p["asset"], p["entry"]) - p["entry"]) * p["qty"]
                   for p in st["positions"])

    equity = cfg["capital_usdt"] + st["realized"] + unreal()
    st["peak_equity"] = max(st["peak_equity"], equity)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if st.get("day") != today:
        st["day"], st["day_start_equity"] = today, equity
    if 1 - equity / st["peak_equity"] >= cfg["kill_switch_drawdown_pct"] and not st["halted"]:
        st["halted"] = True
        log("HALT", f"drawdown limit hit, equity {equity:.2f}")
    day_blocked = 1 - equity / st["day_start_equity"] >= cfg["daily_loss_limit_pct"]

    # ---- find and rank new signals (trust = reward from past live trades) ----
    trust = T.table()
    cands = []
    for c in combos:
        s = series.get(c["asset"])
        if not s:
            continue
        i = len(s) - 1
        bar = s.cs[i]
        if bar["open_time"] <= st["last_bar"].get(c["asset"], 0) or now_ms - bar["close_time"] > FRESH_MS:
            continue
        d, strength = S.signal(s, i, c["strategy"], c["params"])
        tr = T.get(trust, c["asset"], c["strategy"])
        if d and tr["benched"]:
            log("SKIP", f"{c['asset']}/{c['strategy']} benched: trust {tr['trust']} after {tr['trades']} trades")
            continue
        if d:
            rank = (tr["mult"], 1 if c["tier"] == "active" else 0,
                    max(c["valid"]["score"], 0) * strength + strength / 100)
            cands.append((rank, c, d, strength))
    for aid, s in series.items():
        st["last_bar"][aid] = s.cs[-1]["open_time"]

    cands.sort(key=lambda x: x[0], reverse=True)
    opened = []
    held = {p["asset"] for p in st["positions"]}
    cool = {a: t for a, t in st.get("cooldown", {}).items() if t > time.time()}
    st["cooldown"] = cool
    for _, c, d, strength in cands:
        if st["halted"] or day_blocked or len(st["positions"]) >= cfg["max_positions"]:
            break
        if c["asset"] in held:
            continue
        if c["asset"] in cool:
            log("SKIP", f"{c['asset']} cooling down after a stop")
            continue
        cls = assets[c["asset"]]["cls"]
        same = sum(1 for p in st["positions"] if p["side"] == d and assets[p["asset"]]["cls"] == cls)
        if cls in MAX_SAME_SIDE and same >= MAX_SAME_SIDE[cls]:
            log("SKIP", f"{c['asset']} {'LONG' if d == 1 else 'SHORT'}: already {same} {cls} positions this way")
            continue
        s = series[c["asset"]]
        i = len(s) - 1
        entry = live_px[c["asset"]]
        sl, tp = S.exit_levels(s, i, d, c["params"])
        shift = entry - s.c[i]  # re-anchor levels on the live price
        sl, tp = sl + shift, tp + shift
        tr = T.get(trust, c["asset"], c["strategy"])
        base = cfg["risk_active"] if c["tier"] == "active" else cfg["risk_explore"]
        risk = min(base * tr["mult"], MAX_RISK)
        dist = abs(entry - sl)
        if dist <= 0:
            continue
        qty = min(equity * risk / dist, equity * cfg["max_notional_pct"] / entry)
        risk_usdt = qty * dist
        pos = {"asset": c["asset"], "strategy": c["strategy"], "tier": c["tier"], "side": d,
               "risk_usdt": risk_usdt, "trust_mult": tr["mult"],
               "entry": entry, "qty": qty, "sl": sl, "tp": tp, "time": C.now_iso(),
               "last_checked": s.cs[i]["open_time"], "max_bars": c["params"]["max_bars"], "bars": 0}
        st["positions"].append(pos)
        held.add(c["asset"])
        opened.append(c["asset"])
        log("ENTRY", f"{c['asset']} {'LONG' if d == 1 else 'SHORT'} {c['strategy']} [{c['tier']}] "
                     f"@ {entry:.6g} SL {sl:.6g} TP {tp:.6g} strength {strength:.2f} "
                     f"risk {risk_usdt:.2f} USDT (trust x{tr['mult']})")

    equity = cfg["capital_usdt"] + st["realized"] + unreal()
    log("SCAN", f"{len(ok)}/{len(need)} assets, {len(cands)} signals, opened {len(opened)}, "
                f"open {len(st['positions'])}, equity {equity:.2f}"
                + (" HALTED" if st["halted"] else "") + (" DAY-LIMIT" if day_blocked else ""))
    C.append_csv(C.PORTFOLIO, C.PORTFOLIO_FIELDS, {
        "time": C.now_iso(), "equity": round(equity, 2), "realized": round(st["realized"], 2),
        "unrealized": round(unreal(), 2), "open_positions": len(st["positions"]), "assets_ok": len(ok)})
    C.save_json(C.STATE, st)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("ERROR", str(e)[:300])
        sys.exit(1)
