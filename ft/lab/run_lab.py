"""Backtest every candidate strategy on the same coins/period and rank them.
Runs inside the freqtrade docker image. Usage: python run_lab.py TIMERANGE STRATDIR OUTDIR"""
import json, subprocess, sys, time, ast
from pathlib import Path
from freqtrade.data.btanalysis import get_latest_backtest_filename, load_backtest_stats

timerange, stratdir, outdir = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
only = set(sys.argv[4].split(",")) if len(sys.argv) > 4 and sys.argv[4] else None
outdir.mkdir(parents=True, exist_ok=True)
names = []
for f in sorted(stratdir.glob("*.py")):
    try:
        tree = ast.parse(f.read_text())
    except Exception:
        continue
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and any(getattr(b, "id", getattr(b, "attr", "")) == "IStrategy" for b in node.bases):
            names.append(node.name)
if only:
    names = sorted(only)
print("candidates:", len(names), names, flush=True)
results = []
for name in names:
    bt = Path("/tmp/bt") / timerange / name
    bt.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    cmd = ["freqtrade", "backtesting", "-c", "/lab/config.json", "-s", name, "--strategy-path", str(stratdir),
           "--timerange", timerange, "--export", "trades", "--backtest-directory", str(bt), "--cache", "none", "--enable-protections"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=1500)
        st = load_backtest_stats(bt / get_latest_backtest_filename(bt))["strategy"][name]
        r = {"strategy": name, "timeframe": st.get("timeframe"), "trades": st["total_trades"],
             "per_day": st["trades_per_day"], "profit_pct": round(st["profit_total"] * 100, 2),
             "winrate": round(st.get("winrate", 0) * 100, 1), "max_dd_pct": round(st["max_drawdown_account"] * 100, 2),
             "profit_factor": round(st.get("profit_factor") or 0, 2),
             "market_pct": round(st.get("market_change", 0) * 100, 2), "secs": int(time.time() - t0)}
    except Exception as e:
        tail = (p.stderr[-400:] if "p" in dir() and p else "") if not isinstance(e, subprocess.TimeoutExpired) else "timeout"
        r = {"strategy": name, "error": f"{type(e).__name__}: {str(e)[:120]} | {tail[-300:]}"}
    print(r, flush=True)
    results.append(r)
    json.dump(results, open(outdir / f"lab_results_{timerange}.json", "w"), indent=1)

ok = [r for r in results if "error" not in r]
ok.sort(key=lambda r: (r["profit_pct"] > 0, r["profit_pct"] / (r["max_dd_pct"] + 1)), reverse=True)
L = [f"# Strategy lab — {timerange}", "", "Same 30 coins, 1000 USDT, max 5 trades, Binance spot fees.", "",
     "| # | Strategy | TF | Trades | Per day | Profit % | Max DD % | Win % | PF | Market % |", "|---|---|---|---|---|---|---|---|---|---|"]
for i, r in enumerate(ok, 1):
    L.append(f"| {i} | {r['strategy']} | {r['timeframe']} | {r['trades']} | {r['per_day']} | {r['profit_pct']:+} | "
             f"{r['max_dd_pct']} | {r['winrate']} | {r['profit_factor']} | {r['market_pct']:+} |")
bad = [r for r in results if "error" in r]
if bad:
    L += ["", "Failed: " + ", ".join(r["strategy"] for r in bad)]
(outdir / f"lab_report_{timerange}.md").write_text("\n".join(L) + "\n")
print("\n".join(L))
