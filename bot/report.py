"""Daily report -> reports/latest.md, reports/<date>.md, data/summary.json."""
from collections import defaultdict
from datetime import datetime, timezone

import common as C
import trust as T


def main():
    cfg = C.load_json(C.CONFIG, None)
    st = C.load_json(C.STATE, {})
    models = C.load_json(C.MODELS, {"combos": [], "failed": []})
    trades = C.read_csv(C.TRADES)
    port = C.read_csv(C.PORTFOLIO)
    runs = C.read_csv(C.RUNLOG)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    pnl = lambda ts: round(sum(float(t["pnl_usdt"]) for t in ts), 2)
    wins = lambda ts: sum(float(t["pnl_usdt"]) > 0 for t in ts)
    t_today = [t for t in trades if t["close_time"].startswith(today)]
    eq_now = float(port[-1]["equity"]) if port else cfg["capital_usdt"]
    eq_start = next((float(p["equity"]) for p in port if p["time"].startswith(today)), eq_now)
    scans = sum(1 for r in runs if r["time"].startswith(today) and r["action"] == "SCAN")
    errors = [r for r in runs if r["time"].startswith(today) and r["action"] in ("ERROR", "DATA")]

    by_asset = defaultdict(list)
    for t in trades:
        by_asset[(t["asset"], t["strategy"])].append(t)

    summary = {
        "date": today, "equity": round(eq_now, 2), "capital": cfg["capital_usdt"],
        "day_change": round(eq_now - eq_start, 2), "total_pnl": round(eq_now - cfg["capital_usdt"], 2),
        "trades_today": len(t_today), "wins_today": wins(t_today), "pnl_today": pnl(t_today),
        "trades_total": len(trades), "win_rate": round(wins(trades) / len(trades), 3) if trades else None,
        "open_positions": st.get("positions", []), "halted": st.get("halted", False),
        "scans_today": scans, "data_errors_today": len(errors),
        "active": [f"{c['asset']}/{c['strategy']}" for c in models["combos"] if c["tier"] == "active"],
        "explore": [f"{c['asset']}/{c['strategy']}" for c in models["combos"] if c["tier"] == "explore"],
        "combos_tested": models.get("tested", 0),
    }
    C.save_json(C.ROOT / "data" / "summary.json", summary)

    s = summary
    rot = C.load_json(C.ROOT / "data" / "rotation_state.json", {})
    rtr = C.read_csv(C.ROOT / "data" / "rotation_trades.csv")
    req = C.read_csv(C.ROOT / "data" / "rotation_equity.csv")
    R = ["## Основна стратегія: моментум-ротація (з 17.10)", ""]
    if req:
        e = float(req[-1]["equity"])
        R += [f"**Баланс:** {e:.2f} USDT зі старту 1000 ({e / 10 - 100:+.2f}%)",
              f"**Зараз тримає:** {req[-1]['holdings'] or 'кеш'} · режим ринку: {'BTC вище SMA100, торгуємо' if rot.get('regime') else 'BTC нижче SMA100, у кеші'}",
              f"**Угод ротації:** {len(rtr)}", ""]
    else:
        R += ["Ще не запускалась.", ""]
    for prefix, title in (("multi", "Щоденна торгівля 4h (з фільтром денного тренду)"),
                          ("shadow", "Тіньовий режим 15m / 1h (тільки записує, гроші не чіпає)")):
        mt = C.read_csv(C.ROOT / "data" / f"{prefix}_trades.csv")
        me = C.read_csv(C.ROOT / "data" / f"{prefix}_equity.csv")
        R += [f"## {title}", ""]
        if not me:
            R += ["Ще без угод.", ""]
            continue
        rs = [float(t["r"] or 0) for t in mt]
        mt_today = [t for t in mt if t["close_time"].startswith(today)]
        R += [f"**Баланс:** {float(me[-1]['equity']):.2f} USDT зі старту 1000 · відкрито {me[-1]['open_positions']}",
              f"**Угод:** {len(mt)} (сьогодні {len(mt_today)}), виграшних {sum(r > 0 for r in rs)}, "
              f"середній R {sum(rs) / len(rs) if rs else 0:+.2f}", ""]
        by = defaultdict(list)
        for t in mt:
            by[t["strategy"]].append(float(t["r"] or 0))
        for k, v in sorted(by.items()):
            R.append(f"- {k}: {len(v)} угод, сума R {sum(v):+.2f}")
        R.append("")
    L = [f"# Звіт за {today}", ""] + R + ["## Стара внутрішньоденна стратегія (вимкнена для нових входів)", "",
         f"**Баланс:** {s['equity']} USDT (старт {s['capital']}) · за день {s['day_change']:+} · всього {s['total_pnl']:+}",
         f"**Угоди сьогодні:** {s['trades_today']} (виграшних {s['wins_today']}), P&L {s['pnl_today']:+} USDT",
         f"**Всього угод:** {s['trades_total']} · win rate {s['win_rate']}",
         f"**Сканувань за день:** {s['scans_today']} · помилок даних {s['data_errors_today']}"
         + (" · ⛔ ЗУПИНЕНО запобіжником" if s["halted"] else ""), "",
         f"## Що бот торгує (перевірено {s['combos_tested']} комбінацій)",
         f"- Активні (довів прибутковість на нових даних): {', '.join(s['active']) or 'жодної'}",
         f"- Пробні (мінімальний ризик): {', '.join(s['explore']) or 'жодної'}", ""]
    if s["open_positions"]:
        L.append("## Відкриті позиції")
        for p in s["open_positions"]:
            L.append(f"- {p['asset']} {'LONG' if p['side'] == 1 else 'SHORT'} ({p['strategy']}) "
                     f"вхід {p['entry']:.6g}, стоп {p['sl']:.6g}, ціль {p['tp']:.6g}")
        L.append("")
    if t_today:
        L.append("## Угоди за день")
        for t in t_today:
            L.append(f"- {t['asset']} {t['side']} {t['strategy']}: {t['entry']} → {t['exit']} "
                     f"= {float(t['pnl_usdt']):+.2f} USDT ({t['reason']})")
        L.append("")
    if by_asset:
        L += ["## Результат по активах (вся історія)", "| Актив | Стратегія | Угод | Win | P&L |", "|---|---|---|---|---|"]
        for (a, strat), ts in sorted(by_asset.items(), key=lambda x: -pnl(x[1])):
            L.append(f"| {a} | {strat} | {len(ts)} | {wins(ts)} | {pnl(ts):+} |")
    tbl = T.table()
    if tbl:
        L += ["", "## Рейтинг довіри (винагорода бота)",
              "Більше довіри → більший ризик на угоду (до x2). Мінус → менше грошей або лава запасних.", "",
              "| Актив/стратегія | Угод | Сума R | Просадка R | Довіра | Множник ризику |",
              "|---|---|---|---|---|---|"]
        for (a, strat), v in sorted(tbl.items(), key=lambda x: -x[1]["trust"]):
            m = "на лаві" if v["benched"] else f"x{v['mult']}"
            L.append(f"| {a}/{strat} | {v['trades']} | {v['total_r']:+} | {v['max_dd_r']} | {v['trust']:+} | {m} |")
    md = "\n".join(L) + "\n"
    (C.ROOT / "reports").mkdir(exist_ok=True)
    (C.ROOT / "reports" / "latest.md").write_text(md)
    (C.ROOT / "reports" / f"{today}.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
