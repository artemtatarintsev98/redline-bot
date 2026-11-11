"""TraderPro — trades like a disciplined swing/day trader, not a bag-holder.

Rules
- Only long, only with the trend: 1d close > EMA50, 4h EMA50 > EMA200 and price above EMA50,
  BTC on 4h above its EMA50 (no buying alts while BTC is dumping).
- Entry on a pullback: 1h price dipped to its EMA21 zone / RSI cooled down, then on 15m
  momentum turns back up (MACD histogram flips up or RSI crosses 40) with volume.
- Hard stop at entry: below the recent 15m swing low or 1.5 x ATR, whichever is lower
  (clipped to 0.8%..6%). Never averaging down.
- Risk per trade = 1% of the account: position size = 1% / stop distance.
- Management: at +1R stop moves to breakeven, at +2R stop locks +1R, target +3R.
  Exit early if the 4h trend breaks.
- Protections: cooldown after a trade, pause after a series of stops, pause on drawdown.
"""
from datetime import datetime

import talib.abstract as ta
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative, stoploss_from_absolute, timeframe_to_prev_date


class TraderPro(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "15m"
    can_short = False
    process_only_new_candles = True
    startup_candle_count = 250

    minimal_roi = {"0": 100}  # exits are managed by stops / targets, not ROI table
    stoploss = -0.08  # hard fallback, the real stop is custom_stoploss
    use_custom_stoploss = True
    use_exit_signal = True
    exit_profit_only = False
    position_adjustment_enable = False

    risk_per_trade = 0.01
    target_r = 3.0          # None = no fixed target
    breakeven_at = 1.0      # None = never move to breakeven
    lock_at = 2.0           # at +lock_at R lock +1R
    trail_atr = None        # trail stop at current - N*ATR once in profit by 1R
    min_stop, max_stop = 0.008, 0.06

    @property
    def protections(self):
        return [
            {"method": "CooldownPeriod", "stop_duration_candles": 4},
            {"method": "StoplossGuard", "lookback_period_candles": 96, "trade_limit": 3,
             "stop_duration_candles": 48, "only_per_pair": False},
            {"method": "StoplossGuard", "lookback_period_candles": 192, "trade_limit": 2,
             "stop_duration_candles": 96, "only_per_pair": True},
            {"method": "MaxDrawdown", "lookback_period_candles": 288, "trade_limit": 4,
             "stop_duration_candles": 96, "max_allowed_drawdown": 0.06},
        ]

    # ---------- higher timeframes ----------
    @informative("1d")
    def populate_indicators_1d(self, df: DataFrame, metadata: dict) -> DataFrame:
        df["ema50"] = ta.EMA(df, timeperiod=50)
        return df

    @informative("4h")
    def populate_indicators_4h(self, df: DataFrame, metadata: dict) -> DataFrame:
        df["ema50"] = ta.EMA(df, timeperiod=50)
        df["ema200"] = ta.EMA(df, timeperiod=200)
        df["adx"] = ta.ADX(df)
        return df

    @informative("4h", "BTC/{stake}")
    def populate_indicators_btc_4h(self, df: DataFrame, metadata: dict) -> DataFrame:
        df["ema50"] = ta.EMA(df, timeperiod=50)
        return df

    @informative("1h")
    def populate_indicators_1h(self, df: DataFrame, metadata: dict) -> DataFrame:
        df["ema21"] = ta.EMA(df, timeperiod=21)
        df["ema50"] = ta.EMA(df, timeperiod=50)
        df["rsi"] = ta.RSI(df, timeperiod=14)
        return df

    # ---------- base timeframe ----------
    def populate_indicators(self, df: DataFrame, metadata: dict) -> DataFrame:
        df["ema9"] = ta.EMA(df, timeperiod=9)
        df["rsi"] = ta.RSI(df, timeperiod=14)
        macd = ta.MACD(df)
        df["macdhist"] = macd["macdhist"]
        df["atr"] = ta.ATR(df, timeperiod=14)
        df["vol_mean"] = df["volume"].rolling(20).mean()
        swing_low = df["low"].rolling(12).min()
        raw_stop = swing_low.where(swing_low < df["close"] - 1.5 * df["atr"], df["close"] - 1.5 * df["atr"])
        dist = ((df["close"] - raw_stop) / df["close"]).clip(self.min_stop, self.max_stop)
        df["stop_dist"] = dist
        df["stop_price"] = df["close"] * (1 - dist)
        return df

    def populate_entry_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        trend = (
            (df["close_1d"] > df["ema50_1d"])
            & (df["ema50_4h"] > df["ema200_4h"])
            & (df["close_4h"] > df["ema50_4h"])
            & (df["btc_usdt_close_4h"] > df["btc_usdt_ema50_4h"])
        )
        pullback = (
            (df["low"].rolling(8).min() <= df["ema21_1h"] * 1.005)
            | (df["rsi_1h"].between(35, 55))
        ) & (df["close"] > df["ema50_1h"])
        trigger = (
            ((df["macdhist"] > 0) & (df["macdhist"].shift(1) <= 0))
            | ((df["rsi"] > 40) & (df["rsi"].shift(1) <= 40))
        ) & (df["close"] > df["ema9"]) & (df["volume"] > 0.8 * df["vol_mean"])
        df.loc[trend & pullback & trigger, ["enter_long", "enter_tag"]] = (1, "trend_pullback")
        return df

    def populate_exit_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        df.loc[(df["close_4h"] < df["ema50_4h"]) & (df["close"] < df["ema50_1h"]), ["exit_long", "exit_tag"]] = (1, "trend_break")
        return df

    # ---------- risk & trade management ----------
    def _entry_candle(self, pair: str, when: datetime):
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if df is None or df.empty:
            return None
        t = timeframe_to_prev_date(self.timeframe, when)
        c = df.loc[df["date"] == t]
        return None if c.empty else c.iloc[-1]

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float, proposed_stake: float,
                            min_stake: float | None, max_stake: float, leverage: float, entry_tag: str | None,
                            side: str, **kwargs) -> float:
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if df is None or df.empty:
            return proposed_stake
        dist = float(df.iloc[-1]["stop_dist"])
        equity = self.wallets.get_total_stake_amount()
        stake = equity * self.risk_per_trade / max(dist, self.min_stop)
        return max(min(stake, max_stake, equity * 0.30), min_stake or 0)

    def _risk_pct(self, trade: Trade) -> float:
        c = self._entry_candle(trade.pair, trade.open_date_utc)
        return float(c["stop_dist"]) if c is not None else 0.03

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                        current_profit: float, after_fill: bool, **kwargs) -> float | None:
        r = self._risk_pct(trade)
        stop = trade.open_rate * (1 - r)
        if self.lock_at and current_profit >= self.lock_at * r:
            stop = max(stop, trade.open_rate * (1 + r))      # lock +1R
        elif self.breakeven_at and current_profit >= self.breakeven_at * r:
            stop = max(stop, trade.open_rate * 1.002)        # breakeven (covers fees)
        if self.trail_atr and current_profit >= r:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if df is not None and not df.empty:
                stop = max(stop, current_rate - self.trail_atr * float(df.iloc[-1]["atr"]))
        return stoploss_from_absolute(stop, current_rate, is_short=trade.is_short, leverage=trade.leverage)

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs):
        if self.target_r and current_profit >= self.target_r * self._risk_pct(trade):
            return "take_profit_3R"
        return None


class TraderProB(TraderPro):
    """Wider stops (fees eat less of R), no early breakeven, target 2.5R."""
    min_stop, max_stop = 0.015, 0.08
    breakeven_at = None
    lock_at = 2.0
    target_r = 2.5


class TraderProC(TraderPro):
    """Wider stops, no fixed target: let winners run with a 2.5 x ATR trailing stop after +1R."""
    min_stop, max_stop = 0.015, 0.08
    breakeven_at = None
    lock_at = None
    target_r = None
    trail_atr = 2.5
