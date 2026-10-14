import csv
import hashlib
import hmac
import json
import os
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "bot" / "config.json"
STATE = ROOT / "data" / "state.json"
TRADES = ROOT / "data" / "trades.csv"
EQUITY = ROOT / "data" / "equity.csv"
RUNLOG = ROOT / "data" / "runs.csv"
LEARNLOG = ROOT / "data" / "learning_log.csv"

# Public market data (real prices). Orders go to the testnet (fake money).
DATA_URLS = ["https://data-api.binance.vision", "https://api.binance.com"]
TESTNET_URL = "https://testnet.binance.vision"

TRADE_FIELDS = ["open_time", "close_time", "symbol", "entry", "exit", "qty", "pnl_usdt",
                "pnl_pct", "reason", "params_version", "mode"]
EQUITY_FIELDS = ["time", "price", "equity", "realized", "unrealized", "in_position"]
RUN_FIELDS = ["time", "action", "detail"]
LEARN_FIELDS = ["time", "decision", "old_version", "new_version", "old_params", "new_params",
                "old_valid_score", "new_valid_score", "valid_trades", "note"]


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


def fetch_klines(symbol, interval, limit=1000, end_time=None):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if end_time:
        params["endTime"] = end_time
    last_err = None
    for base in DATA_URLS:
        try:
            r = requests.get(f"{base}/api/v3/klines", params=params, timeout=20)
            r.raise_for_status()
            return [{"open_time": k[0], "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                     "close": float(k[4]), "close_time": k[6]} for k in r.json()]
        except Exception as e:  # try the next mirror
            last_err = e
    raise RuntimeError(f"Could not fetch market data: {last_err}")


def fetch_history(symbol, interval, total):
    """Fetch `total` most recent candles (paginated, 1000 per request)."""
    out, end = [], None
    while len(out) < total:
        batch = fetch_klines(symbol, interval, min(1000, total - len(out)), end)
        if not batch:
            break
        out = batch + out
        end = batch[0]["open_time"] - 1
        if len(batch) < 1000:
            break
    return out[-total:]


def closed_candles(candles):
    now_ms = int(time.time() * 1000)
    return [c for c in candles if c["close_time"] < now_ms]


class Testnet:
    """Minimal signed client for the Binance Spot Testnet (virtual funds only)."""

    def __init__(self, key, secret):
        self.key, self.secret = key, secret.encode()

    def _signed(self, method, path, params):
        params = dict(params, timestamp=int(time.time() * 1000), recvWindow=10000)
        q = urllib.parse.urlencode(params)
        sig = hmac.new(self.secret, q.encode(), hashlib.sha256).hexdigest()
        r = requests.request(method, f"{TESTNET_URL}{path}?{q}&signature={sig}",
                             headers={"X-MBX-APIKEY": self.key}, timeout=20)
        if r.status_code >= 400:
            raise RuntimeError(f"Testnet {path} {r.status_code}: {r.text[:300]}")
        return r.json()

    def lot_step(self, symbol):
        r = requests.get(f"{TESTNET_URL}/api/v3/exchangeInfo", params={"symbol": symbol}, timeout=20)
        r.raise_for_status()
        for f in r.json()["symbols"][0]["filters"]:
            if f["filterType"] == "LOT_SIZE":
                return float(f["stepSize"])
        return 0.00001

    def market_buy_quote(self, symbol, quote_usdt):
        return self._signed("POST", "/api/v3/order", {
            "symbol": symbol, "side": "BUY", "type": "MARKET", "quoteOrderQty": f"{quote_usdt:.2f}"})

    def market_sell(self, symbol, qty):
        return self._signed("POST", "/api/v3/order", {
            "symbol": symbol, "side": "SELL", "type": "MARKET", "quantity": f"{qty:.8f}".rstrip("0").rstrip(".")})

    def account(self):
        return self._signed("GET", "/api/v3/account", {})


def fill_price(order):
    qty = float(order.get("executedQty", 0) or 0)
    quote = float(order.get("cummulativeQuoteQty", 0) or 0)
    return (quote / qty if qty else 0.0), qty
