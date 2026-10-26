"""Summarize the Freqtrade dry-run database into ft/report.md and ft/trades.csv."""
import csv, sqlite3, sys, datetime as dt
db, name, out = sys.argv[1], sys.argv[2], sys.argv[3]
start = float(sys.argv[4]) if len(sys.argv) > 4 else 1000.0
con = sqlite3.connect(db)
rows = con.execute("select pair, is_open, open_date, close_date, open_rate, close_rate, stake_amount, "
                   "close_profit_abs, close_profit, exit_reason, is_short from trades order by id").fetchall()
with open(f"{out}_trades.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["pair", "open", "open_date", "close_date", "open_rate", "close_rate", "stake", "profit_usdt",
                "profit_pct", "exit_reason", "short"])
    w.writerows(rows)
closed = [r for r in rows if not r[1]]
pnl = sum(r[7] or 0 for r in closed)
wins = sum((r[7] or 0) > 0 for r in closed)
L = [f"# {name} (demo) — {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC", "",
     f"**Balance:** {start + pnl:.2f} USDT (start {start:.0f}, {pnl:+.2f}, {pnl / start:+.2%})",
     f"**Closed trades:** {len(closed)}, wins {wins}" + (f" ({wins / len(closed):.0%})" if closed else ""),
     f"**Open now:** {sum(1 for r in rows if r[1])}", "", "| Pair | Opened | Closed | Profit | Reason |", "|---|---|---|---|---|"]
for r in rows[-15:]:
    L.append(f"| {r[0]} | {str(r[2])[:16]} | {str(r[3] or 'open')[:16]} | {(r[7] or 0):+.2f} | {r[9] or ''} |")
open(f"{out}_report.md", "w").write("\n".join(L) + "\n")
print("\n".join(L[:5]))
