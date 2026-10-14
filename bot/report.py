"""Daily report: writes data/summary.json and reports/latest.md (+ a dated copy)."""
from datetime import datetime, timezone

import common as C


def main():
    cfg = C.load_json(C.CONFIG, None)
    st = C.load_json(C.STATE, {})
    trades = C.read_csv(C.TRADES)
    equity = C.read_csv(C.EQUITY)
    runs = C.read_csv(C.RUNLOG)
    learn = C.read_csv(C.LEARNLOG)
    bt = C.load_json(C.ROOT / "data" / "backtest_latest.json", {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    t_today = [t for t in trades if t["close_time"].startswith(today)]
    pnl = lambda ts: round(sum(float(t["pnl_usdt"]) for t in ts), 2)
    wins = lambda ts: [t for t in ts if float(t["pnl_usdt"]) > 0]
    eq_now = float(equity[-1]["equity"]) if equity else cfg["capital_usdt"]
    eq_day_start = next((float(e["equity"]) for e in equity if e["time"].startswith(today)), eq_now)
    errors = [r for r in runs if r["time"].startswith(today) and r["action"] == "ERROR"]
    learn_today = [l for l in learn if l["time"].startswith(today)]

    summary = {
        "date": today,
        "mode": st.get("mode", "?"),
        "capital_start": cfg["capital_usdt"],
        "equity": round(eq_now, 2),
        "day_change_usdt": round(eq_now - eq_day_start, 2),
        "total_pnl_usdt": round(eq_now - cfg["capital_usdt"], 2),
        "trades_today": len(t_today),
        "wins_today": len(wins(t_today)),
        "losses_today": len(t_today) - len(wins(t_today)),
        "pnl_today_usdt": pnl(t_today),
        "trades_total": len(trades),
        "win_rate_total": round(len(wins(trades)) / len(trades), 3) if trades else None,
        "open_position": st.get("position"),
        "halted": st.get("halted", False),
        "params_version": cfg["params_version"],
        "params": cfg["params"],
        "learning_today": learn_today[-1] if learn_today else None,
        "backtest": bt,
        "errors_today": len(errors),
        "runs_today": sum(1 for r in runs if r["time"].startswith(today)),
        "today_trades": t_today,
    }
    C.save_json(C.ROOT / "data" / "summary.json", summary)

    s = summary
    lines = [f"# Звіт за {today}", "",
             f"- Режим: **{s['mode']}** (віртуальні гроші)",
             f"- Баланс бота: **{s['equity']} USDT** (старт {s['capital_start']}), за день {s['day_change_usdt']:+} USDT",
             f"- Угод сьогодні: {s['trades_today']} (+{s['wins_today']} / -{s['losses_today']}), P&L {s['pnl_today_usdt']:+} USDT",
             f"- Всього угод: {s['trades_total']}, win rate: {s['win_rate_total']}",
             f"- Відкрита позиція: {'так' if s['open_position'] else 'ні'}",
             f"- Зупинено запобіжником: {'ТАК' if s['halted'] else 'ні'}",
             f"- Версія параметрів: v{s['params_version']} {s['params']}",
             f"- Помилок сьогодні: {s['errors_today']}"]
    if s["learning_today"]:
        l = s["learning_today"]
        lines += ["", f"**Навчання:** {l['decision']} — {l['note']}"]
    for t in t_today:
        lines.append(f"- {t['reason']}: {t['entry']} → {t['exit']}, {t['pnl_usdt']} USDT ({t['pnl_pct']}%)")
    md = "\n".join(lines) + "\n"
    (C.ROOT / "reports").mkdir(exist_ok=True)
    (C.ROOT / "reports" / "latest.md").write_text(md)
    (C.ROOT / "reports" / f"{today}.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
