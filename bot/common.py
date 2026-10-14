"""Shared helpers: files, logs and market data (Binance for crypto, Yahoo for the rest)."""
import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "bot" / "config.json"
MODELS = ROOT / "data" / "models.json"
STATE = ROOT / "data" / "state_v2.json"
TRADES = ROOT / "data" / "trades_v2.csv"
PORTFOLIO = ROOT / "data" / "portfolio.csv"
RUNLOG = ROOT / "data" / "runs_v2.csv"
LEARNLOG = ROOT / "data" / "learning_v2.csv"

BINANCE_URLS = ["https://data-api.binance.vision", "https://api.binance.com"]
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"
UA = {"User-Agent": "Mozilla/5.0 (redline-bot research)"}
BAR_MS = {"15m": 15 * 60_000, "1h": 3_600_000}

TRADE_FIELDS = ["open_time", "close_time", "asset", "strategy", "tier", "side", "entry", "exit",
                "qty", "pnl_usdt", "pnl_pct", "reason", "bars"]
PORTFOLIO_FIELDS = ["time", "equity", "realized", "unrealized", "open_positions", "assets_ok"]
RUN_FIELDS = ["time", "action", "detail"]
LEARN_FIELDS = ["time", "asset", "strategy", "params", "tier", "train_trades", "train_ret",
                "valid_trades", "valid_ret", "valid_pf", "note"]


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, indent=2) + "\n")


def append_csv(path, fields, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})


def read_csv(path):
    try:
        with Path(path).open() as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


# ---------------- market data ----------------

def _binance(sym, interval, limit, end_time=None):
    params = {"symbol": sym, "interval": interval, "limit": limit}
    if end_time:
        params["endTime"] = end_time
    err = None
    for base in BINANCE_URLS:
        try:
            r = requests.get(f"{base}/api/v3/klines", params=params, timeout=20)
            r.raise_for_status()
            return [{"open_time": k[0], "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                     "close": float(k[4]), "close_time": k[6]} for k in r.json()]
        except Exception as e:
            err = e
    raise RuntimeError(f"binance {sym}: {err}")


def _yahoo(sym, interval, range_):
    r = requests.get(YAHOO_URL + sym, params={"interval": interval, "range": range_},
                     headers=UA, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    step = BAR_MS[interval]
    out = []
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        out.append({"open_time": ts * 1000, "open": o, "high": h, "low": l, "close": c,
                    "close_time": ts * 1000 + step - 1})
    return out


def candles(asset, interval, n):
    """Most recent `n` candles (the last one may still be forming)."""
    if asset["src"] == "binance":
        out, end = [], None
        while len(out) < n:
            batch = _binance(asset["sym"], interval, min(1000, n - len(out)), end)
            if not batch:
                break
            out = batch + out
            end = batch[0]["open_time"] - 1
            if len(batch) < 1000:
                break
        return out[-n:]
    days = max(2, min(59, n * BAR_MS[interval] // 86_400_000 + 2))
    return _yahoo(asset["sym"], interval, f"{days}d")[-n:]


def split_closed(cs):
    """Return (closed candles, forming candle or None)."""
    now_ms = int(time.time() * 1000)
    closed = [c for c in cs if c["close_time"] < now_ms]
    forming = cs[-1] if cs and cs[-1]["close_time"] >= now_ms else None
    return closed, forming
