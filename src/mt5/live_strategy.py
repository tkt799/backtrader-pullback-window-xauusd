"""
Sunrise Ogle Live Strategy — 移植自 Backtrader 的实盘版本
================================================================
完全复刻 src/strategy/sunrise_ogle_xauusd.py 的核心逻辑，但脱离 Backtrader，
使用 pandas + 纯 Python 实现，可直接对接 MT5 实盘或进行离线验证。

核心逻辑：4阶段波动率扩张通道状态机
  SCANNING → ARMED_LONG/SHORT → WINDOW_OPEN → BREAKOUT → 下单

与回测版本的参数保持 1:1 一致，修改请同步两份文件顶部的 CONFIG 区域。

用法（示例）：
    from src.mt5.live_strategy import LiveSunriseConfig, LiveSunriseStrategy
    import pandas as pd

    df = pd.read_csv("data/XAUUSD_5m_5Yea.csv")  # 或从 MT5 拉取
    config = LiveSunriseConfig()
    strat = LiveSunriseStrategy(config)
    for _, bar in df.iterrows():
        signal = strat.on_bar(bar)  # 返回 "BUY"/"SELL"/None
        if signal:
            print(signal, strat.last_signal_info)

离线验证：
    python -m src.mt5.live_strategy --csv data/XAUUSD_5m_5Yea.csv --plot

"""

from __future__ import annotations
import math
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple
from pathlib import Path

try:
    import pandas as pd
    import numpy as np
except ImportError:
    pd = None  # type: ignore
    np = None  # type: ignore

logger = logging.getLogger("LiveStrategy")

# =============================================================
# 与 sunrise_ogle_xauusd.py 顶部 CONFIG 保持一致
# 修改此处即可同步调整回测与实盘
# =============================================================
@dataclass
class LiveSunriseConfig:
    # --- EMA ---
    ema_fast_length: int = 14
    ema_medium_length: int = 14
    ema_slow_length: int = 24
    ema_confirm_length: int = 1
    ema_filter_price_length: int = 100
    ema_exit_length: int = 25

    # --- ATR ---
    atr_length: int = 10

    # --- 交易方向 ---
    enable_long_trades: bool = True
    enable_short_trades: bool = False  # 回测默认 LONG ONLY

    # --- LONG ATR 过滤 ---
    long_use_atr_filter: bool = True
    long_atr_min_threshold: float = 0.0
    long_atr_max_threshold: float = 2.00
    long_use_atr_increment_filter: bool = True
    long_atr_increment_min_threshold: float = 0.2
    long_atr_increment_max_threshold: float = 1.6
    long_use_atr_decrement_filter: bool = False
    long_atr_decrement_min_threshold: float = -0.00002
    long_atr_decrement_max_threshold: float = -0.00001

    # --- SHORT ATR 过滤 ---
    short_use_atr_filter: bool = True
    short_atr_min_threshold: float = 0.000400
    short_atr_max_threshold: float = 0.000750
    short_use_atr_increment_filter: bool = True
    short_atr_increment_min_threshold: float = 0.000001
    short_atr_increment_max_threshold: float = 0.001000
    short_use_atr_decrement_filter: bool = True
    short_atr_decrement_min_threshold: float = -0.000080
    short_atr_decrement_max_threshold: float = -0.000020

    # --- LONG 过滤 ---
    long_use_ema_order_condition: bool = False
    long_use_price_filter_ema: bool = True
    long_use_candle_direction_filter: bool = False
    long_use_angle_filter: bool = False
    long_min_angle: float = 35.0
    long_max_angle: float = 95.0
    long_angle_scale_factor: float = 10.0
    long_use_ema_below_price_filter: bool = False
    long_atr_sl_multiplier: float = 4.5
    long_atr_tp_multiplier: float = 6.5

    # --- SHORT 过滤 ---
    short_use_ema_order_condition: bool = True
    short_use_price_filter_ema: bool = True
    short_use_candle_direction_filter: bool = True
    short_use_angle_filter: bool = True
    short_min_angle: float = -90.0
    short_max_angle: float = -20.0
    short_angle_scale_factor: float = 10.0
    short_use_ema_above_price_filter: bool = False
    short_atr_sl_multiplier: float = 2.5
    short_atr_tp_multiplier: float = 6.5

    # --- Pullback 窗口 ---
    long_use_pullback_entry: bool = True
    long_pullback_max_candles: int = 3
    long_entry_window_periods: int = 1
    short_use_pullback_entry: bool = True
    short_pullback_max_candles: int = 2
    short_entry_window_periods: int = 7
    use_window_time_offset: bool = False
    window_offset_multiplier: float = 1.0
    window_price_offset_multiplier: float = 0.001

    # --- 时间过滤 ---
    use_time_range_filter: bool = False
    entry_start_hour: int = 0
    entry_start_minute: int = 0
    entry_end_hour: int = 8
    entry_end_minute: int = 0

    # --- 风控 ---
    risk_percent: float = 0.01
    contract_size: float = 100.0  # XAUUSD 1手=100盎司（MT5侧会被 symbol_info 覆盖）
    enable_risk_sizing: bool = True

    # --- 其他 ---
    print_signals: bool = True
    verbose_debug: bool = False


# =============================================================
# 指标计算 (纯 pandas，不依赖 backtrader / TA-Lib)
# =============================================================
def ema(series: "pd.Series", period: int) -> "pd.Series":
    """EMA 与 backtrader 一致：span=period, adjust=False"""
    return series.ewm(span=period, adjust=False, min_periods=period).mean()

def atr(df: "pd.DataFrame", period: int = 10) -> "pd.Series":
    """
    ATR (Wilder's smoothing) 与 backtrader.bt.ind.ATR 一致
    TR = max(H-L, |H-C_prev|, |L-C_prev|)
    ATR = Wilder's RMA of TR (alpha=1/period)
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    # Wilder's smoothing = ewm(alpha=1/period, adjust=False)
    atr_series = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return atr_series

def cross_above(a: float, a_prev: float, b: float, b_prev: float) -> bool:
    """Pine ta.crossover 等价：当前 a>b 且 前一根 a<=b"""
    return (a > b) and (a_prev <= b_prev)

def cross_below(a: float, a_prev: float, b: float, b_prev: float) -> bool:
    return (a < b) and (a_prev >= b_prev)

def ema_angle(current_ema: float, prev_ema: float, scale: float) -> float:
    """与 _angle() 一致：atan((ema - ema_prev)*scale) -> degrees"""
    rise = (current_ema - prev_ema) * scale
    return math.degrees(math.atan(rise))


# =============================================================
# 实时策略类
# =============================================================
class LiveSunriseStrategy:
    """
    无状态回测 / 有状态实盘 统一接口。
    内部维护与 Backtrader 版本完全一致的状态机变量：
      entry_state, armed_direction, pullback_candle_count, window_*, signal_trigger_candle 等

    核心方法：
      on_bar(bar_dict)  -> 逐根K线驱动，返回 "BUY"/"SELL"/None
      prepare_history(df) -> 批量预热指标（实盘启动时调用）
    """

    def __init__(self, config: Optional[LiveSunriseConfig] = None):
        self.config = config or LiveSunriseConfig()

        # ----- 指标历史（用于增量计算）-----
        self.history: List[Dict[str, Any]] = []  # 每根bar的 open/high/low/close/time
        self.max_history: int = 500  # 保留足够长度以计算 EMA100

        # 计算好的指标（与 history 一一对应，最后一个是当前bar）
        self.ema_fast: List[float] = []
        self.ema_medium: List[float] = []
        self.ema_slow: List[float] = []
        self.ema_confirm: List[float] = []
        self.ema_filter_price: List[float] = []
        self.atr_series: List[float] = []

        # ----- 状态机（与 Backtrader 版本 1:1）-----
        self.entry_state: str = "SCANNING"  # SCANNING, ARMED_LONG, ARMED_SHORT, WINDOW_OPEN
        self.armed_direction: Optional[str] = None
        self.pullback_candle_count: int = 0
        self.last_pullback_candle_high: Optional[float] = None
        self.last_pullback_candle_low: Optional[float] = None
        self.window_top_limit: Optional[float] = None
        self.window_bottom_limit: Optional[float] = None
        self.window_expiry_bar: Optional[int] = None
        self.window_bar_start: Optional[int] = None
        self.signal_trigger_candle: Optional[Dict[str, Any]] = None
        self.signal_detection_atr: Optional[float] = None
        self.signal_detection_bar: Optional[int] = None

        # 统计
        self.bar_index: int = 0  # 全局bar计数，对应 Backtrader 的 len(self)
        self.trades: int = 0
        self.wins: int = 0
        self.losses: int = 0
        self.last_signal_info: Optional[Dict[str, Any]] = None

        # 缓存最新指标值
        self._last_indicators: Dict[str, float] = {}

    # ---------- 工具 ----------
    def _is_in_trading_time_range(self, dt: datetime) -> bool:
        if not self.config.use_time_range_filter:
            return True
        # 统一按 UTC 判断
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        current_minutes = dt.hour * 60 + dt.minute
        start = self.config.entry_start_hour * 60 + self.config.entry_start_minute
        end = self.config.entry_end_hour * 60 + self.config.entry_end_minute
        if start <= end:
            return start <= current_minutes <= end
        else:  # 跨午夜
            return current_minutes >= start or current_minutes <= end

    def _log(self, msg: str):
        if self.config.print_signals:
            logger.info(msg)
            print(msg)

    def _debug(self, msg: str):
        if self.config.verbose_debug:
            logger.debug(msg)
            print(msg)

    # ---------- 指标预热 ----------
    def prepare_history(self, df: "pd.DataFrame"):
        """
        实盘启动时：用历史K线预热所有指标，避免冷启动误触发。
        df 需包含列: time, open, high, low, close (time 为 datetime)
        """
        if pd is None:
            raise RuntimeError("需要 pandas")

        if len(df) < max(self.config.ema_filter_price_length, self.config.atr_length) + 10:
            logger.warning(f"历史数据不足 {len(df)} 根，建议至少 {self.config.ema_filter_price_length + 10} 根以预热 EMA100")

        # 批量计算指标
        close = df["close"]
        df["_ema_fast"] = ema(close, self.config.ema_fast_length)
        df["_ema_medium"] = ema(close, self.config.ema_medium_length)
        df["_ema_slow"] = ema(close, self.config.ema_slow_length)
        df["_ema_confirm"] = ema(close, self.config.ema_confirm_length)
        df["_ema_filter"] = ema(close, self.config.ema_filter_price_length)
        df["_atr"] = atr(df, self.config.atr_length)

        # 填充到内部状态
        self.history = []
        self.ema_fast = []
        self.ema_medium = []
        self.ema_slow = []
        self.ema_confirm = []
        self.ema_filter_price = []
        self.atr_series = []

        for _, row in df.iterrows():
            self.history.append({
                "time": row["time"] if "time" in row else row.name,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            })
            self.ema_fast.append(float(row["_ema_fast"]) if not pd.isna(row["_ema_fast"]) else float("nan"))
            self.ema_medium.append(float(row["_ema_medium"]) if not pd.isna(row["_ema_medium"]) else float("nan"))
            self.ema_slow.append(float(row["_ema_slow"]) if not pd.isna(row["_ema_slow"]) else float("nan"))
            self.ema_confirm.append(float(row["_ema_confirm"]) if not pd.isna(row["_ema_confirm"]) else float("nan"))
            self.ema_filter_price.append(float(row["_ema_filter"]) if not pd.isna(row["_ema_filter"]) else float("nan"))
            self.atr_series.append(float(row["_atr"]) if not pd.isna(row["_atr"]) else float("nan"))

        self.bar_index = len(self.history)
        # 限制内存
        if len(self.history) > self.max_history:
            excess = len(self.history) - self.max_history
            self.history = self.history[excess:]
            self.ema_fast = self.ema_fast[excess:]
            self.ema_medium = self.ema_medium[excess:]
            self.ema_slow = self.ema_slow[excess:]
            self.ema_confirm = self.ema_confirm[excess:]
            self.ema_filter_price = self.ema_filter_price[excess:]
            self.atr_series = self.atr_series[excess:]
            self.bar_index = len(self.history)

        logger.info(f"✅ 预热完成 | 历史 {len(self.history)} 根 | 最新 close={self.history[-1]['close']:.2f} ATR={self.atr_series[-1]:.4f} EMA_confirm={self.ema_confirm[-1]:.2f}")

    def _append_bar_and_update_indicators(self, bar: Dict[str, Any]):
        """
        增量更新：追加一根新bar并更新所有指标（用于实盘逐根驱动）。
        为保证与批量计算一致，这里采用与批量相同的 ewm 递推公式。
        简化实现：每次重新用 pandas 计算最近 N 根（N=200），开销可接受（5秒一次）。
        """
        self.history.append(bar)
        self.bar_index += 1

        # 保持窗口大小
        if len(self.history) > self.max_history:
            self.history.pop(0)
            # 指标也同步裁剪，稍后重算会补齐
            if self.ema_fast:
                self.ema_fast.pop(0)
                self.ema_medium.pop(0)
                self.ema_slow.pop(0)
                self.ema_confirm.pop(0)
                self.ema_filter_price.pop(0)
                self.atr_series.pop(0)

        # 用 pandas 重算最近窗口（保证精度与回测一致）
        df = pd.DataFrame(self.history)
        close = df["close"]
        # 只重算最后1根的值，但为准确性重算全量再取值
        ema_f = ema(close, self.config.ema_fast_length).iloc[-1]
        ema_m = ema(close, self.config.ema_medium_length).iloc[-1]
        ema_s = ema(close, self.config.ema_slow_length).iloc[-1]
        ema_c = ema(close, self.config.ema_confirm_length).iloc[-1]
        ema_flt = ema(close, self.config.ema_filter_price_length).iloc[-1]
        atr_v = atr(df, self.config.atr_length).iloc[-1]

        self.ema_fast.append(float(ema_f) if not pd.isna(ema_f) else float("nan"))
        self.ema_medium.append(float(ema_m) if not pd.isna(ema_m) else float("nan"))
        self.ema_slow.append(float(ema_s) if not pd.isna(ema_s) else float("nan"))
        self.ema_confirm.append(float(ema_c) if not pd.isna(ema_c) else float("nan"))
        self.ema_filter_price.append(float(ema_flt) if not pd.isna(ema_flt) else float("nan"))
        self.atr_series.append(float(atr_v) if not pd.isna(atr_v) else float("nan"))

        # 同步裁剪到 history 长度
        # (上面已处理)

    # ---------- 状态机辅助 ----------
    def _reset_entry_state(self):
        self.entry_state = "SCANNING"
        self.armed_direction = None
        self.pullback_candle_count = 0
        self.last_pullback_candle_high = None
        self.last_pullback_candle_low = None
        self.window_top_limit = None
        self.window_bottom_limit = None
        self.window_expiry_bar = None
        self.window_bar_start = None
        self.signal_trigger_candle = None
        # 注意：signal_detection_atr / signal_detection_bar 保留到下单后才清，与原策略一致

    def _reset_signal_tracking(self):
        self.signal_detection_atr = None
        self.signal_detection_bar = None

    def _has_enough_data(self) -> bool:
        # 需要至少 max(EMA100, ATR) 根且非 NaN
        if len(self.history) < 2:
            return False
        if math.isnan(self.ema_filter_price[-1]) or math.isnan(self.atr_series[-1]):
            return False
        if math.isnan(self.ema_confirm[-1]) or math.isnan(self.ema_confirm[-2]):
            return False
        return True

    # ---------- 4阶段逻辑 ----------
    def _phase1_scan_for_signal(self) -> Optional[str]:
        """与 Backtrader _phase1_scan_for_signal 1:1 复刻"""
        if len(self.history) < 2:
            return None

        curr_close = self.history[-1]["close"]
        curr_open = self.history[-1]["open"]
        prev_close = self.history[-2]["close"]
        prev_open = self.history[-2]["open"]

        ema_c = self.ema_confirm[-1]
        ema_c_prev = self.ema_confirm[-2]
        ema_f = self.ema_fast[-1]
        ema_f_prev = self.ema_fast[-2]
        ema_m = self.ema_medium[-1]
        ema_m_prev = self.ema_medium[-2]
        ema_s = self.ema_slow[-1]
        ema_s_prev = self.ema_slow[-2]
        ema_flt = self.ema_filter_price[-1]
        atr_now = self.atr_series[-1]

        # LONG
        if self.config.enable_long_trades:
            prev_bull = prev_close > prev_open
            cross_fast = cross_above(ema_c, ema_c_prev, ema_f, ema_f_prev)
            cross_medium = cross_above(ema_c, ema_c_prev, ema_m, ema_m_prev)
            cross_slow = cross_above(ema_c, ema_c_prev, ema_s, ema_s_prev)
            cross_any = cross_fast or cross_medium or cross_slow

            candle_ok = True
            if self.config.long_use_candle_direction_filter:
                candle_ok = prev_bull

            if candle_ok and cross_any:
                valid = True
                if self.config.long_use_ema_order_condition:
                    if not (ema_c > ema_f and ema_c > ema_m and ema_c > ema_s):
                        valid = False
                if valid and self.config.long_use_price_filter_ema:
                    if not (curr_close > ema_flt):
                        valid = False
                if valid and self.config.long_use_ema_below_price_filter:
                    if not (ema_f < curr_close and ema_m < curr_close and ema_s < curr_close):
                        valid = False
                if valid and self.config.long_use_angle_filter:
                    # LONG 角度：需计算
                    if len(self.ema_confirm) >= 2 and not math.isnan(ema_c_prev):
                        angle = ema_angle(ema_c, ema_c_prev, self.config.long_angle_scale_factor)
                        if not (self.config.long_min_angle <= angle <= self.config.long_max_angle):
                            valid = False
                    else:
                        valid = False
                if valid and self.config.long_use_atr_filter:
                    if not (self.config.long_atr_min_threshold <= atr_now <= self.config.long_atr_max_threshold):
                        valid = False

                if valid:
                    self.signal_detection_atr = atr_now
                    self.signal_detection_bar = self.bar_index
                    return "LONG"

        # SHORT
        if self.config.enable_short_trades:
            prev_bear = prev_close < prev_open
            cross_fast = cross_below(ema_c, ema_c_prev, ema_f, ema_f_prev)
            cross_medium = cross_below(ema_c, ema_c_prev, ema_m, ema_m_prev)
            cross_slow = cross_below(ema_c, ema_c_prev, ema_s, ema_s_prev)
            cross_any = cross_fast or cross_medium or cross_slow

            candle_ok = True
            if self.config.short_use_candle_direction_filter:
                candle_ok = prev_bear

            if candle_ok and cross_any:
                valid = True
                if self.config.short_use_ema_order_condition:
                    if not (ema_c < ema_f and ema_c < ema_m and ema_c < ema_s):
                        valid = False
                if valid and self.config.short_use_price_filter_ema:
                    if not (curr_close < ema_flt):
                        valid = False
                if valid and self.config.short_use_ema_above_price_filter:
                    if not (ema_f > curr_close and ema_m > curr_close and ema_s > curr_close):
                        valid = False
                if valid and self.config.short_use_angle_filter:
                    if len(self.ema_confirm) >= 2 and not math.isnan(ema_c_prev):
                        # SHORT 用 short 的 scale
                        angle = ema_angle(ema_c, ema_c_prev, self.config.short_angle_scale_factor)
                        if not (self.config.short_min_angle <= angle <= self.config.short_max_angle):
                            valid = False
                    else:
                        valid = False
                if valid and self.config.short_use_atr_filter:
                    if not (self.config.short_atr_min_threshold <= atr_now <= self.config.short_atr_max_threshold):
                        valid = False

                if valid:
                    self.signal_detection_atr = atr_now
                    self.signal_detection_bar = self.bar_index
                    if self.config.print_signals:
                        self._log(f"🔍 SHORT 信号捕获 | Prev熊={prev_bear} CrossFast={cross_fast} CrossMed={cross_medium} CrossSlow={cross_slow}")
                    return "SHORT"

        return None

    def _phase2_confirm_pullback(self, armed_direction: str) -> bool:
        curr_close = self.history[-1]["close"]
        curr_open = self.history[-1]["open"]
        curr_high = self.history[-1]["high"]
        curr_low = self.history[-1]["low"]

        if armed_direction == "LONG":
            is_pullback = curr_close < curr_open  # 红
        else:
            is_pullback = curr_close > curr_open  # 绿

        if is_pullback:
            self.pullback_candle_count += 1
            max_candles = self.config.long_pullback_max_candles if armed_direction == "LONG" else self.config.short_pullback_max_candles
            if self.pullback_candle_count >= max_candles:
                self.last_pullback_candle_high = curr_high
                self.last_pullback_candle_low = curr_low
                self._log(f"✅ Pullback 确认完成 | {armed_direction} {self.pullback_candle_count} 根")
                return True
        else:
            self._log(f"⚠️ Pullback 被破坏 | {armed_direction} 非回撤K线，重置为 SCANNING")
            self._reset_entry_state()
        return False

    def _phase3_open_breakout_window(self, armed_direction: str):
        current_bar = self.bar_index
        window_start = current_bar
        if self.config.use_window_time_offset:
            offset = int(self.pullback_candle_count * self.config.window_offset_multiplier)
            window_start = current_bar + offset
        self.window_bar_start = window_start

        window_periods = self.config.long_entry_window_periods if armed_direction == "LONG" else self.config.short_entry_window_periods
        self.window_expiry_bar = window_start + window_periods

        last_high = self.last_pullback_candle_high
        last_low = self.last_pullback_candle_low
        if last_high is None or last_low is None:
            logger.error("缺少回撤K线高低点，无法开窗")
            return
        candle_range = last_high - last_low
        price_offset = candle_range * self.config.window_price_offset_multiplier

        self.window_top_limit = last_high + price_offset
        self.window_bottom_limit = last_low - price_offset
        self.entry_state = "WINDOW_OPEN"

        if armed_direction == "LONG":
            success_level = self.window_top_limit
            failure_level = self.window_bottom_limit
        else:
            success_level = self.window_bottom_limit
            failure_level = self.window_top_limit
        self._log(f"🚪 窗口已开 | {armed_direction} | 区间 {self.window_bar_start}~{self.window_expiry_bar} | "
                  f"成功位={success_level:.2f} 失败位={failure_level:.2f}")

    def _phase4_monitor_window(self, armed_direction: str) -> Optional[str]:
        current_bar = self.bar_index
        if self.window_bar_start is None or self.window_expiry_bar is None:
            return None
        if current_bar < self.window_bar_start:
            return None  # 尚未到窗口开启时间（有 time offset 时）

        if current_bar > self.window_expiry_bar:
            self._log(f"⏰ 窗口超时 | {armed_direction} 未突破，回到 ARMED")
            # 回到 ARMED，重置计数但保留 armed_direction
            self.entry_state = f"ARMED_{armed_direction}"
            self.pullback_candle_count = 0
            self.window_top_limit = None
            self.window_bottom_limit = None
            self.window_expiry_bar = None
            self.window_bar_start = None
            return None

        curr_high = self.history[-1]["high"]
        curr_low = self.history[-1]["low"]

        if armed_direction == "LONG":
            if curr_high >= self.window_top_limit:  # type: ignore
                self._log(f"🚀 LONG 成功突破 | High {curr_high:.2f} >= {self.window_top_limit:.2f}")  # type: ignore
                return "SUCCESS"
            elif curr_low <= self.window_bottom_limit:  # type: ignore
                self._log(f"💥 LONG 失败边界被破 | Low {curr_low:.2f} <= {self.window_bottom_limit:.2f} → 回到 ARMED")  # type: ignore
                self.entry_state = "ARMED_LONG"
                self.pullback_candle_count = 0
                self.window_top_limit = None
                self.window_bottom_limit = None
                self.window_expiry_bar = None
                self.window_bar_start = None
                return None
        else:  # SHORT
            if curr_low <= self.window_bottom_limit:  # type: ignore
                self._log(f"🚀 SHORT 成功突破 | Low {curr_low:.2f} <= {self.window_bottom_limit:.2f}")  # type: ignore
                return "SUCCESS"
            elif curr_high >= self.window_top_limit:  # type: ignore
                self._log(f"💥 SHORT 失败边界被破 | High {curr_high:.2f} >= {self.window_top_limit:.2f} → 回到 ARMED")  # type: ignore
                self.entry_state = "ARMED_SHORT"
                self.pullback_candle_count = 0
                self.window_top_limit = None
                self.window_bottom_limit = None
                self.window_expiry_bar = None
                self.window_bar_start = None
                return None
        return None

    def _validate_entry_filters(self, direction: str) -> bool:
        """突破后最终校验（角度、ATR 等），与 Backtrader 的 _validate_all_* 一致"""
        if len(self.history) < 2:
            return False
        curr_close = self.history[-1]["close"]
        ema_c = self.ema_confirm[-1]
        ema_c_prev = self.ema_confirm[-2]
        ema_f = self.ema_fast[-1]
        ema_m = self.ema_medium[-1]
        ema_s = self.ema_slow[-1]
        ema_flt = self.ema_filter_price[-1]

        if direction == "LONG":
            if self.config.long_use_ema_order_condition:
                if not (ema_c > ema_f and ema_c > ema_m and ema_c > ema_s):
                    self._debug("LONG EMA排序过滤未通过")
                    return False
            if self.config.long_use_price_filter_ema:
                if not (curr_close > ema_flt):
                    self._debug("LONG 价格过滤未通过")
                    return False
            if self.config.long_use_ema_below_price_filter:
                if not (ema_f < curr_close and ema_m < curr_close and ema_s < curr_close):
                    self._debug("LONG EMA位置过滤未通过")
                    return False
            if self.config.long_use_angle_filter:
                angle = ema_angle(ema_c, ema_c_prev, self.config.long_angle_scale_factor)
                if not (self.config.long_min_angle <= angle <= self.config.long_max_angle):
                    self._debug(f"LONG 角度过滤未通过: {angle:.1f}°")
                    return False
        else:  # SHORT
            if self.config.short_use_ema_order_condition:
                if not (ema_c < ema_f and ema_c < ema_m and ema_c < ema_s):
                    self._debug("SHORT EMA排序过滤未通过")
                    return False
            if self.config.short_use_price_filter_ema:
                if not (curr_close < ema_flt):
                    self._debug("SHORT 价格过滤未通过")
                    return False
            if self.config.short_use_ema_above_price_filter:
                if not (ema_f > curr_close and ema_m > curr_close and ema_s > curr_close):
                    self._debug("SHORT EMA位置过滤未通过")
                    return False
            if self.config.short_use_angle_filter:
                angle = ema_angle(ema_c, ema_c_prev, self.config.short_angle_scale_factor)
                if not (self.config.short_min_angle <= angle <= self.config.short_max_angle):
                    self._debug(f"SHORT 角度过滤未通过: {angle:.1f}° not in [{self.config.short_min_angle},{self.config.short_max_angle}]")
                    return False

        # ATR 增量过滤（突破时刻校验）
        atr_now = self.atr_series[-1]
        if math.isnan(atr_now):
            return False

        if direction == "LONG" and self.config.long_use_atr_filter and self.signal_detection_atr is not None:
            atr_change = atr_now - self.signal_detection_atr
            if atr_change > 0:
                if self.config.long_use_atr_increment_filter:
                    if not (self.config.long_atr_increment_min_threshold <= atr_change <= self.config.long_atr_increment_max_threshold):
                        self._debug(f"LONG ATR增量过滤未通过: {atr_change:+.6f}")
                        return False
                else:
                    self._debug(f"LONG ATR增量被禁用，拒绝所有增量: {atr_change:+.6f}")
                    return False
            elif atr_change < 0:
                if self.config.long_use_atr_decrement_filter:
                    if not (self.config.long_atr_decrement_min_threshold <= atr_change <= self.config.long_atr_decrement_max_threshold):
                        self._debug(f"LONG ATR减量过滤未通过: {atr_change:+.6f}")
                        return False
        elif direction == "SHORT" and self.config.short_use_atr_filter and self.signal_detection_atr is not None:
            atr_change = atr_now - self.signal_detection_atr
            if atr_change > 0:
                if self.config.short_use_atr_increment_filter:
                    if not (self.config.short_atr_increment_min_threshold <= atr_change <= self.config.short_atr_increment_max_threshold):
                        self._debug(f"SHORT ATR增量过滤未通过: {atr_change:+.6f}")
                        return False
            elif atr_change < 0:
                if self.config.short_use_atr_decrement_filter:
                    if not (self.config.short_atr_decrement_min_threshold <= atr_change <= self.config.short_atr_decrement_max_threshold):
                        self._debug(f"SHORT ATR减量过滤未通过: {atr_change:+.6f}")
                        return False
        return True

    # ---------- 主入口 ----------
    def on_bar(self, bar: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        逐根K线驱动。
        bar 需包含: time(datetime), open, high, low, close, volume(可选)
        返回:
          None  → 无信号
          {"action": "BUY"/"SELL", "entry": float, "sl": float, "tp": float, "atr": float, "info": str}
        """
        # 标准化 bar
        if "time" not in bar:
            bar["time"] = datetime.now(timezone.utc)
        # 确保 time 是 datetime
        if isinstance(bar["time"], str):
            bar["time"] = pd.to_datetime(bar["time"], utc=True).to_pydatetime()  # type: ignore

        # 追加并更新指标
        # 如果是批量预热后的第一根实盘bar，history 已有，这里直接追加
        # 为避免重复追加（prepare_history 已包含所有历史），调用方应只传新bar
        self._append_bar_and_update_indicators({
            "time": bar["time"],
            "open": float(bar["open"]),
            "high": float(bar["high"]),
            "low": float(bar["low"]),
            "close": float(bar["close"]),
        })

        if not self._has_enough_data():
            self._debug(f"数据预热中... bar_index={self.bar_index}")
            return None

        dt: datetime = bar["time"]  # type: ignore
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        curr_close = float(bar["close"])
        curr_high = float(bar["high"])
        curr_low = float(bar["low"])
        atr_now = float(self.atr_series[-1])

        # 全局失效检查（与 Backtrader next() 顶部一致）
        if self.entry_state in ["ARMED_LONG", "ARMED_SHORT"]:
            opposing = None
            if self.entry_state == "ARMED_LONG":
                # 检查是否有 SHORT 信号出现 → 失效
                # 简化：用当前bar的交叉判断
                ema_c = self.ema_confirm[-1]
                ema_c_prev = self.ema_confirm[-2]
                ema_f = self.ema_fast[-1]
                ema_f_prev = self.ema_fast[-2]
                ema_m = self.ema_medium[-1]
                ema_m_prev = self.ema_medium[-2]
                ema_s = self.ema_slow[-1]
                ema_s_prev = self.ema_slow[-2]
                prev_bear = self.history[-2]["close"] < self.history[-2]["open"]
                if prev_bear and (cross_below(ema_c, ema_c_prev, ema_f, ema_f_prev) or
                                  cross_below(ema_c, ema_c_prev, ema_m, ema_m_prev) or
                                  cross_below(ema_c, ema_c_prev, ema_s, ema_s_prev)):
                    opposing = "SHORT"
            elif self.entry_state == "ARMED_SHORT":
                ema_c = self.ema_confirm[-1]
                ema_c_prev = self.ema_confirm[-2]
                ema_f = self.ema_fast[-1]
                ema_f_prev = self.ema_fast[-2]
                ema_m = self.ema_medium[-1]
                ema_m_prev = self.ema_medium[-2]
                ema_s = self.ema_slow[-1]
                ema_s_prev = self.ema_slow[-2]
                prev_bull = self.history[-2]["close"] > self.history[-2]["open"]
                if prev_bull and (cross_above(ema_c, ema_c_prev, ema_f, ema_f_prev) or
                                  cross_above(ema_c, ema_c_prev, ema_m, ema_m_prev) or
                                  cross_above(ema_c, ema_c_prev, ema_s, ema_s_prev)):
                    opposing = "LONG"
            if opposing:
                self._log(f"🔄 全局失效 | {opposing} 反向信号出现，重置 {self.entry_state}")
                self._reset_entry_state()

        # 状态机路由
        if self.entry_state == "SCANNING":
            signal = self._phase1_scan_for_signal()
            if signal:
                self.entry_state = f"ARMED_{signal}"
                self.armed_direction = signal
                self.pullback_candle_count = 0
                # 记录触发K线（前一根）
                prev = self.history[-2]
                self.signal_trigger_candle = {
                    "open": prev["open"], "close": prev["close"],
                    "high": prev["high"], "low": prev["low"],
                    "time": prev["time"],
                    "is_bullish": prev["close"] > prev["open"],
                    "is_bearish": prev["close"] < prev["open"],
                }
                self._log(f"📡 SCANNING → ARMED_{signal} | 触发K线 C={prev['close']:.2f} O={prev['open']:.2f} ATR={atr_now:.4f}")
            return None

        elif self.entry_state in ["ARMED_LONG", "ARMED_SHORT"]:
            if self._phase2_confirm_pullback(self.armed_direction):  # type: ignore
                self.entry_state = "WINDOW_OPEN"
                self._phase3_open_breakout_window(self.armed_direction)  # type: ignore
            return None

        elif self.entry_state == "WINDOW_OPEN":
            breakout = self._phase4_monitor_window(self.armed_direction)  # type: ignore
            if breakout == "SUCCESS":
                direction = self.armed_direction
                # 时间过滤最终校验
                if not self._is_in_trading_time_range(dt):
                    self._log(f"❌ 时间过滤拦截 | {direction} 突破发生在 {dt.strftime('%H:%M')} UTC，禁止入场")
                    self._reset_entry_state()
                    self._reset_signal_tracking()
                    return None

                # 蜡烛方向最终校验（用最初触发K线）
                if self.signal_trigger_candle:
                    trig = self.signal_trigger_candle
                    body = abs(trig["close"] - trig["open"])
                    min_body = 0.00001
                    trig_bull = trig["is_bullish"] and body >= min_body
                    trig_bear = trig["is_bearish"] and body >= min_body
                    if direction == "LONG" and self.config.long_use_candle_direction_filter and not trig_bull:
                        self._log("❌ LONG 被拦截：触发K线非阳线")
                        self._reset_entry_state()
                        self._reset_signal_tracking()
                        return None
                    if direction == "SHORT" and self.config.short_use_candle_direction_filter and not trig_bear:
                        self._log("❌ SHORT 被拦截：触发K线非阴线")
                        self._reset_entry_state()
                        self._reset_signal_tracking()
                        return None

                # 全部过滤器最终校验
                if not self._validate_entry_filters(direction):  # type: ignore
                    self._log(f"❌ {direction} 过滤器校验失败，放弃入场")
                    self._reset_entry_state()
                    self._reset_signal_tracking()
                    return None

                if atr_now <= 0 or math.isnan(atr_now):
                    self._log("❌ ATR 无效，放弃入场")
                    self._reset_entry_state()
                    self._reset_signal_tracking()
                    return None

                # 计算 SL/TP（与 Backtrader 一致：用当前bar的 high/low）
                entry_price = curr_close
                if direction == "LONG":
                    sl = curr_low - atr_now * self.config.long_atr_sl_multiplier
                    tp = curr_high + atr_now * self.config.long_atr_tp_multiplier
                    action = "BUY"
                else:  # SHORT
                    sl = curr_high + atr_now * self.config.short_atr_sl_multiplier
                    tp = curr_low - atr_now * self.config.short_atr_tp_multiplier
                    action = "SELL"

                if (direction == "LONG" and (entry_price - sl) <= 0) or (direction == "SHORT" and (sl - entry_price) <= 0):
                    self._log("❌ SL 距离异常，放弃入场")
                    self._reset_entry_state()
                    self._reset_signal_tracking()
                    return None

                rr = (tp - entry_price) / (entry_price - sl) if direction == "LONG" else (entry_price - tp) / (sl - entry_price)

                info = {
                    "action": action,
                    "direction": direction,
                    "entry": entry_price,
                    "sl": sl,
                    "tp": tp,
                    "atr": atr_now,
                    "rr": rr,
                    "time": dt,
                    "bar_index": self.bar_index,
                    "window_top": self.window_top_limit,
                    "window_bottom": self.window_bottom_limit,
                    "trigger_candle": self.signal_trigger_candle,
                    "atr_at_signal": self.signal_detection_atr,
                }
                self.last_signal_info = info
                self._log(f"🎯 波动率扩张入场信号 | {action} {direction} | Entry={entry_price:.2f} SL={sl:.2f} TP={tp:.2f} RR=1:{rr:.2f} ATR={atr_now:.4f}")

                # 重置状态机
                self._reset_entry_state()
                self._reset_signal_tracking()
                return info
            return None

        return None

    # ---------- 离线批量回测辅助 ----------
    def backtest_dataframe(self, df: "pd.DataFrame", initial_cash: float = 100000.0, limit: Optional[int] = None) -> Dict[str, Any]:
        """
        用 DataFrame 离线验证逻辑是否与 Backtrader 回测一致。
        不做真实资金曲线，仅统计信号数。

        优化版：一次性向量化计算所有指标，避免逐根 pandas 重算，350k 根可在数秒内完成。
        limit: 仅测试前 N 根（用于快速验证），None=全部
        """
        if pd is None:
            raise RuntimeError("需要 pandas")
        if limit is not None:
            df = df.iloc[:limit].copy()

        warmup = max(self.config.ema_filter_price_length, self.config.atr_length) + 5
        if len(df) <= warmup:
            raise ValueError(f"数据长度 {len(df)} 不足以预热，需要 >{warmup}")

        # 向量化一次性计算所有指标
        close = df["close"]
        df["_ema_fast"] = ema(close, self.config.ema_fast_length)
        df["_ema_medium"] = ema(close, self.config.ema_medium_length)
        df["_ema_slow"] = ema(close, self.config.ema_slow_length)
        df["_ema_confirm"] = ema(close, self.config.ema_confirm_length)
        df["_ema_filter"] = ema(close, self.config.ema_filter_price_length)
        df["_atr"] = atr(df, self.config.atr_length)

        # 提取为 numpy 加速循环
        times = df["time"].values if "time" in df.columns else df.index.values
        opens = df["open"].values.astype(float)
        highs = df["high"].values.astype(float)
        lows = df["low"].values.astype(float)
        closes = df["close"].values.astype(float)
        ema_f = df["_ema_fast"].values.astype(float)
        ema_m = df["_ema_medium"].values.astype(float)
        ema_s = df["_ema_slow"].values.astype(float)
        ema_c = df["_ema_confirm"].values.astype(float)
        ema_flt = df["_ema_filter"].values.astype(float)
        atr_arr = df["_atr"].values.astype(float)

        # 状态机变量（与 on_bar 完全一致，但基于数组索引）
        entry_state = "SCANNING"
        armed_direction: Optional[str] = None
        pullback_count = 0
        last_high: Optional[float] = None
        last_low: Optional[float] = None
        window_top: Optional[float] = None
        window_bottom: Optional[float] = None
        window_bar_start: Optional[int] = None
        window_expiry: Optional[int] = None
        signal_detection_atr: Optional[float] = None
        signal_detection_bar: Optional[int] = None
        signal_trigger_candle: Optional[Dict[str, Any]] = None

        signals: List[Dict[str, Any]] = []

        # 辅助：时间过滤
        def is_in_time_range(idx: int) -> bool:
            if not self.config.use_time_range_filter:
                return True
            t = times[idx]
            # 转 datetime
            if isinstance(t, (int, float)):
                dt = pd.to_datetime(t, utc=True).to_pydatetime()
            else:
                dt = pd.to_datetime(t, utc=True).to_pydatetime() if not isinstance(t, datetime) else t
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            cur = dt.hour * 60 + dt.minute
            start = self.config.entry_start_hour * 60 + self.config.entry_start_minute
            end = self.config.entry_end_hour * 60 + self.config.entry_end_minute
            if start <= end:
                return start <= cur <= end
            else:
                return cur >= start or cur <= end

        # 主循环：从 warmup 开始模拟逐根
        for i in range(warmup, len(df)):
            # 数据有效性
            if np.isnan(ema_flt[i]) or np.isnan(atr_arr[i]) or np.isnan(ema_c[i]) or np.isnan(ema_c[i-1]):
                continue

            curr_close = closes[i]
            curr_open = opens[i]
            curr_high = highs[i]
            curr_low = lows[i]
            prev_close = closes[i-1]
            prev_open = opens[i-1]
            ema_c0 = ema_c[i]; ema_c1 = ema_c[i-1]
            ema_f0 = ema_f[i]; ema_f1 = ema_f[i-1]
            ema_m0 = ema_m[i]; ema_m1 = ema_m[i-1]
            ema_s0 = ema_s[i]; ema_s1 = ema_s[i-1]
            ema_flt0 = ema_flt[i]
            atr0 = atr_arr[i]

            # 全局失效
            if entry_state in ["ARMED_LONG", "ARMED_SHORT"]:
                opp = None
                if entry_state == "ARMED_LONG":
                    if (prev_close < prev_open) and (cross_below(ema_c0, ema_c1, ema_f0, ema_f1) or cross_below(ema_c0, ema_c1, ema_m0, ema_m1) or cross_below(ema_c0, ema_c1, ema_s0, ema_s1)):
                        opp = "SHORT"
                elif entry_state == "ARMED_SHORT":
                    if (prev_close > prev_open) and (cross_above(ema_c0, ema_c1, ema_f0, ema_f1) or cross_above(ema_c0, ema_c1, ema_m0, ema_m1) or cross_above(ema_c0, ema_c1, ema_s0, ema_s1)):
                        opp = "LONG"
                if opp:
                    entry_state = "SCANNING"
                    armed_direction = None
                    pullback_count = 0
                    last_high = None; last_low = None
                    window_top = None; window_bottom = None
                    window_bar_start = None; window_expiry = None
                    signal_trigger_candle = None
                    continue

            if entry_state == "SCANNING":
                sig = None
                # LONG
                if self.config.enable_long_trades:
                    prev_bull = prev_close > prev_open
                    cf = cross_above(ema_c0, ema_c1, ema_f0, ema_f1)
                    cm = cross_above(ema_c0, ema_c1, ema_m0, ema_m1)
                    cs = cross_above(ema_c0, ema_c1, ema_s0, ema_s1)
                    candle_ok = prev_bull if self.config.long_use_candle_direction_filter else True
                    if candle_ok and (cf or cm or cs):
                        valid = True
                        if self.config.long_use_ema_order_condition and not (ema_c0 > ema_f0 and ema_c0 > ema_m0 and ema_c0 > ema_s0):
                            valid = False
                        if valid and self.config.long_use_price_filter_ema and not (curr_close > ema_flt0):
                            valid = False
                        if valid and self.config.long_use_ema_below_price_filter and not (ema_f0 < curr_close and ema_m0 < curr_close and ema_s0 < curr_close):
                            valid = False
                        if valid and self.config.long_use_angle_filter:
                            ang = ema_angle(ema_c0, ema_c1, self.config.long_angle_scale_factor)
                            if not (self.config.long_min_angle <= ang <= self.config.long_max_angle):
                                valid = False
                        if valid and self.config.long_use_atr_filter and not (self.config.long_atr_min_threshold <= atr0 <= self.config.long_atr_max_threshold):
                            valid = False
                        if valid:
                            sig = "LONG"
                            signal_detection_atr = atr0
                            signal_detection_bar = i
                # SHORT
                if sig is None and self.config.enable_short_trades:
                    prev_bear = prev_close < prev_open
                    cf = cross_below(ema_c0, ema_c1, ema_f0, ema_f1)
                    cm = cross_below(ema_c0, ema_c1, ema_m0, ema_m1)
                    cs = cross_below(ema_c0, ema_c1, ema_s0, ema_s1)
                    candle_ok = prev_bear if self.config.short_use_candle_direction_filter else True
                    if candle_ok and (cf or cm or cs):
                        valid = True
                        if self.config.short_use_ema_order_condition and not (ema_c0 < ema_f0 and ema_c0 < ema_m0 and ema_c0 < ema_s0):
                            valid = False
                        if valid and self.config.short_use_price_filter_ema and not (curr_close < ema_flt0):
                            valid = False
                        if valid and self.config.short_use_ema_above_price_filter and not (ema_f0 > curr_close and ema_m0 > curr_close and ema_s0 > curr_close):
                            valid = False
                        if valid and self.config.short_use_angle_filter:
                            ang = ema_angle(ema_c0, ema_c1, self.config.short_angle_scale_factor)
                            if not (self.config.short_min_angle <= ang <= self.config.short_max_angle):
                                valid = False
                        if valid and self.config.short_use_atr_filter and not (self.config.short_atr_min_threshold <= atr0 <= self.config.short_atr_max_threshold):
                            valid = False
                        if valid:
                            sig = "SHORT"
                            signal_detection_atr = atr0
                            signal_detection_bar = i
                if sig:
                    entry_state = f"ARMED_{sig}"
                    armed_direction = sig
                    pullback_count = 0
                    signal_trigger_candle = {
                        "open": prev_open, "close": prev_close, "high": highs[i-1], "low": lows[i-1],
                        "time": times[i-1], "is_bullish": prev_close > prev_open, "is_bearish": prev_close < prev_open
                    }

            elif entry_state in ["ARMED_LONG", "ARMED_SHORT"]:
                is_pull = (curr_close < curr_open) if armed_direction == "LONG" else (curr_close > curr_open)
                if is_pull:
                    pullback_count += 1
                    need = self.config.long_pullback_max_candles if armed_direction == "LONG" else self.config.short_pullback_max_candles
                    if pullback_count >= need:
                        last_high = curr_high
                        last_low = curr_low
                        # 开窗
                        cur_bar = i
                        win_start = cur_bar
                        if self.config.use_window_time_offset:
                            win_start = cur_bar + int(pullback_count * self.config.window_offset_multiplier)
                        window_bar_start = win_start
                        win_periods = self.config.long_entry_window_periods if armed_direction == "LONG" else self.config.short_entry_window_periods
                        window_expiry = win_start + win_periods
                        rng = last_high - last_low
                        off = rng * self.config.window_price_offset_multiplier
                        window_top = last_high + off
                        window_bottom = last_low - off
                        entry_state = "WINDOW_OPEN"
                else:
                    # 回撤破坏
                    entry_state = "SCANNING"
                    armed_direction = None
                    pullback_count = 0
                    last_high = None; last_low = None
                    window_top = None; window_bottom = None
                    window_bar_start = None; window_expiry = None
                    signal_trigger_candle = None

            elif entry_state == "WINDOW_OPEN":
                if window_bar_start is None or window_expiry is None:
                    continue
                if i < window_bar_start:
                    continue
                if i > window_expiry:
                    entry_state = f"ARMED_{armed_direction}"
                    pullback_count = 0
                    window_top = None; window_bottom = None
                    window_bar_start = None; window_expiry = None
                    continue

                succ = False; fail = False
                if armed_direction == "LONG":
                    if curr_high >= window_top:  # type: ignore
                        succ = True
                    elif curr_low <= window_bottom:  # type: ignore
                        fail = True
                else:  # SHORT
                    if curr_low <= window_bottom:  # type: ignore
                        succ = True
                    elif curr_high >= window_top:  # type: ignore
                        fail = True

                if fail:
                    entry_state = f"ARMED_{armed_direction}"
                    pullback_count = 0
                    window_top = None; window_bottom = None
                    window_bar_start = None; window_expiry = None
                    continue

                if succ:
                    # 时间过滤
                    if not is_in_time_range(i):
                        entry_state = "SCANNING"
                        armed_direction = None
                        pullback_count = 0
                        window_top = None; window_bottom = None
                        window_bar_start = None; window_expiry = None
                        signal_detection_atr = None; signal_detection_bar = None
                        signal_trigger_candle = None
                        continue
                    # 蜡烛最终校验
                    if signal_trigger_candle:
                        body = abs(signal_trigger_candle["close"] - signal_trigger_candle["open"])
                        min_body = 0.00001
                        trig_bull = signal_trigger_candle["is_bullish"] and body >= min_body
                        trig_bear = signal_trigger_candle["is_bearish"] and body >= min_body
                        if armed_direction == "LONG" and self.config.long_use_candle_direction_filter and not trig_bull:
                            entry_state = "SCANNING"; armed_direction=None; pullback_count=0; window_top=None; window_bottom=None; window_bar_start=None; window_expiry=None; signal_detection_atr=None; signal_detection_bar=None; signal_trigger_candle=None
                            continue
                        if armed_direction == "SHORT" and self.config.short_use_candle_direction_filter and not trig_bear:
                            entry_state = "SCANNING"; armed_direction=None; pullback_count=0; window_top=None; window_bottom=None; window_bar_start=None; window_expiry=None; signal_detection_atr=None; signal_detection_bar=None; signal_trigger_candle=None
                            continue
                    # 过滤器校验
                    valid = True
                    ema_c0 = ema_c[i]; ema_c1 = ema_c[i-1]
                    ema_f0 = ema_f[i]; ema_m0 = ema_m[i]; ema_s0 = ema_s[i]
                    ema_flt0 = ema_flt[i]
                    atr0 = atr_arr[i]
                    if armed_direction == "LONG":
                        if self.config.long_use_ema_order_condition and not (ema_c0 > ema_f0 and ema_c0 > ema_m0 and ema_c0 > ema_s0):
                            valid=False
                        if valid and self.config.long_use_price_filter_ema and not (curr_close > ema_flt0):
                            valid=False
                        if valid and self.config.long_use_ema_below_price_filter and not (ema_f0 < curr_close and ema_m0 < curr_close and ema_s0 < curr_close):
                            valid=False
                        if valid and self.config.long_use_angle_filter:
                            ang = ema_angle(ema_c0, ema_c1, self.config.long_angle_scale_factor)
                            if not (self.config.long_min_angle <= ang <= self.config.long_max_angle):
                                valid=False
                        if valid and self.config.long_use_atr_filter and signal_detection_atr is not None:
                            chg = atr0 - signal_detection_atr
                            if chg > 0:
                                if self.config.long_use_atr_increment_filter:
                                    if not (self.config.long_atr_increment_min_threshold <= chg <= self.config.long_atr_increment_max_threshold):
                                        valid=False
                                else:
                                    valid=False
                            elif chg < 0 and self.config.long_use_atr_decrement_filter:
                                if not (self.config.long_atr_decrement_min_threshold <= chg <= self.config.long_atr_decrement_max_threshold):
                                    valid=False
                    else:  # SHORT
                        if self.config.short_use_ema_order_condition and not (ema_c0 < ema_f0 and ema_c0 < ema_m0 and ema_c0 < ema_s0):
                            valid=False
                        if valid and self.config.short_use_price_filter_ema and not (curr_close < ema_flt0):
                            valid=False
                        if valid and self.config.short_use_ema_above_price_filter and not (ema_f0 > curr_close and ema_m0 > curr_close and ema_s0 > curr_close):
                            valid=False
                        if valid and self.config.short_use_angle_filter:
                            ang = ema_angle(ema_c0, ema_c1, self.config.short_angle_scale_factor)
                            if not (self.config.short_min_angle <= ang <= self.config.short_max_angle):
                                valid=False
                        if valid and self.config.short_use_atr_filter and signal_detection_atr is not None:
                            chg = atr0 - signal_detection_atr
                            if chg > 0 and self.config.short_use_atr_increment_filter:
                                if not (self.config.short_atr_increment_min_threshold <= chg <= self.config.short_atr_increment_max_threshold):
                                    valid=False
                            elif chg < 0 and self.config.short_use_atr_decrement_filter:
                                if not (self.config.short_atr_decrement_min_threshold <= chg <= self.config.short_atr_decrement_max_threshold):
                                    valid=False
                    if not valid:
                        entry_state = "SCANNING"
                        armed_direction = None
                        pullback_count = 0
                        window_top = None; window_bottom = None
                        window_bar_start = None; window_expiry = None
                        signal_detection_atr = None; signal_detection_bar = None
                        signal_trigger_candle = None
                        continue
                    if atr0 <= 0 or np.isnan(atr0):
                        entry_state = "SCANNING"
                        armed_direction = None
                        pullback_count = 0
                        window_top = None; window_bottom = None
                        window_bar_start = None; window_expiry = None
                        signal_detection_atr = None; signal_detection_bar = None
                        signal_trigger_candle = None
                        continue

                    # 计算 SL/TP
                    entry_price = curr_close
                    if armed_direction == "LONG":
                        sl = curr_low - atr0 * self.config.long_atr_sl_multiplier
                        tp = curr_high + atr0 * self.config.long_atr_tp_multiplier
                        action = "BUY"
                    else:
                        sl = curr_high + atr0 * self.config.short_atr_sl_multiplier
                        tp = curr_low - atr0 * self.config.short_atr_tp_multiplier
                        action = "SELL"

                    if (armed_direction == "LONG" and (entry_price - sl) <= 0) or (armed_direction == "SHORT" and (sl - entry_price) <= 0):
                        entry_state = "SCANNING"
                        armed_direction = None
                        pullback_count = 0
                        window_top = None; window_bottom = None
                        window_bar_start = None; window_expiry = None
                        signal_detection_atr = None; signal_detection_bar = None
                        signal_trigger_candle = None
                        continue

                    rr = (tp - entry_price) / (entry_price - sl) if armed_direction == "LONG" else (entry_price - tp) / (sl - entry_price)
                    t_val = times[i]
                    dt = pd.to_datetime(t_val, utc=True).to_pydatetime() if not isinstance(t_val, datetime) else t_val
                    info = {
                        "action": action,
                        "direction": armed_direction,
                        "entry": entry_price,
                        "sl": sl,
                        "tp": tp,
                        "atr": atr0,
                        "rr": rr,
                        "time": dt,
                        "bar_index": i,
                        "window_top": window_top,
                        "window_bottom": window_bottom,
                        "trigger_candle": signal_trigger_candle,
                        "atr_at_signal": signal_detection_atr,
                    }
                    signals.append(info)
                    # 重置
                    entry_state = "SCANNING"
                    armed_direction = None
                    pullback_count = 0
                    last_high = None; last_low = None
                    window_top = None; window_bottom = None
                    window_bar_start = None; window_expiry = None
                    signal_detection_atr = None; signal_detection_bar = None
                    signal_trigger_candle = None

        return {
            "total_bars": len(df),
            "warmup": warmup,
            "signals": signals,
            "signal_count": len(signals),
            "long_signals": len([s for s in signals if s["direction"] == "LONG"]),
            "short_signals": len([s for s in signals if s["direction"] == "SHORT"]),
        }


# =============================================================
# 命令行入口：离线验证
# =============================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sunrise Ogle 实盘逻辑离线验证")
    parser.add_argument("--csv", type=str, default="data/XAUUSD_5m_5Yea.csv", help="CSV 路径")
    parser.add_argument("--plot", action="store_true", help="是否绘图")
    args = parser.parse_args()

    if pd is None:
        raise SystemExit("请先安装 pandas: pip install pandas matplotlib")

    csv_path = Path(args.csv)
    if not csv_path.is_absolute():
        # 相对于项目根目录
        csv_path = Path(__file__).resolve().parent.parent.parent / csv_path

    if not csv_path.exists():
        raise SystemExit(f"CSV 不存在: {csv_path}")

    print(f"读取 {csv_path} ...")
    df_raw = pd.read_csv(csv_path)
    if "Date" in df_raw.columns:
        df_raw["time"] = pd.to_datetime(df_raw["Date"].astype(str) + " " + df_raw["Time"].astype(str), utc=True)
        df_raw = df_raw.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "tick_volume"})
        df_raw["spread"] = 30
        df_raw["real_volume"] = df_raw["tick_volume"]
        df = df_raw[["time", "open", "high", "low", "close", "tick_volume"]].copy()
    else:
        df = df_raw.copy()
        if "time" not in df.columns and "datetime" in df.columns:
            df["time"] = pd.to_datetime(df["datetime"], utc=True)

    config = LiveSunriseConfig(print_signals=False, verbose_debug=False)
    strat = LiveSunriseStrategy(config)
    result = strat.backtest_dataframe(df)

    print("\n" + "="*70)
    print("离线验证结果（LiveStrategy  vs  Backtrader）")
    print("="*70)
    print(f"总K线: {result['total_bars']} | 预热: {result['warmup']} | 信号数: {result['signal_count']}")
    print(f"LONG: {result['long_signals']} | SHORT: {result['short_signals']}")
    if result["signals"]:
        print("\n前5个信号:")
        for s in result["signals"][:5]:
            print(f"  {s['time']} {s['action']} Entry={s['entry']:.2f} SL={s['sl']:.2f} TP={s['tp']:.2f} RR=1:{s['rr']:.2f}")

    if args.plot and result["signals"]:
        import matplotlib.pyplot as plt
        closes = df["close"].values
        times = range(len(closes))
        plt.figure(figsize=(14, 6))
        plt.plot(times, closes, label="XAUUSD Close", alpha=0.7, linewidth=0.8)
        for s in result["signals"]:
            idx = df.index[df["time"] == s["time"]].tolist()
            if idx:
                i = idx[0]
                color = "green" if s["direction"] == "LONG" else "red"
                marker = "^" if s["direction"] == "LONG" else "v"
                plt.scatter(i, s["entry"], color=color, marker=marker, s=80, zorder=5, label=s["direction"] if s == result["signals"][0] else "")
        plt.title(f"LiveStrategy 离线信号图 | 信号数: {result['signal_count']}")
        plt.xlabel("Bar Index (M5)")
        plt.ylabel("Price")
        plt.legend()
        plt.grid(alpha=0.3)
        plt.tight_layout()
        plt.show()
