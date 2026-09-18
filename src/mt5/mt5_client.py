"""
MT5 Client Wrapper — 封装所有与 MetaTrader 5 终端的交互
========================================================
功能：
- 终端连接 / 断线重连
- 实时行情获取（M5 XAUUSD）
- 指标计算所需历史K线拉取
- 下单 / 设置 SL/TP / 平仓
- 账户信息 / 持仓查询
- Dry-Run 模拟模式（无真实MT5环境时也能测试逻辑）

支持两种运行模式：
1. LIVE  : Windows 上已安装 MT5 终端 + 已安装 MetaTrader5 pip 包 → 真实下单
2. DRY_RUN / MOCK : Linux / Mac / CI 环境 → 打印日志，不发真实订单，用于验证状态机逻辑

作者：基于 src/strategy/sunrise_ogle_xauusd.py 移植
"""

from __future__ import annotations
import time
import math
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List

try:
    import MetaTrader5 as mt5  # type: ignore
    MT5_AVAILABLE = True
except ImportError:
    mt5 = None  # type: ignore
    MT5_AVAILABLE = False

try:
    import pandas as pd
    import numpy as np
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False

logger = logging.getLogger("MT5Client")

# ============ 数据结构 ============
@dataclass
class SymbolInfo:
    symbol: str
    point: float          # 最小变动价位
    tick_value: float     # 每 tick 价值
    tick_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    contract_size: float  # 1手对应盎司数，XAUUSD通常100
    spread: float
    margin_initial: float

@dataclass
class PositionInfo:
    ticket: int
    symbol: str
    type: int  # 0=BUY, 1=SELL
    volume: float
    price_open: float
    price_current: float
    sl: float
    tp: float
    profit: float
    magic: int

@dataclass
class OrderResult:
    success: bool
    retcode: int
    message: str
    ticket: Optional[int] = None
    volume: Optional[float] = None
    price: Optional[float] = None


# ============ MT5 客户端 ============
class MT5Client:
    """
    统一的 MT5 操作入口。
    用法：
        client = MT5Client(symbol="XAUUSD", magic=20250918, dry_run=False)
        client.connect(login=..., password=..., server=..., path=r"C:\\Program Files\\MetaTrader 5\\terminal64.exe")
        df = client.get_rates(count=500)  # 获取最近500根M5
        client.buy(volume=0.1, sl=..., tp=...)
    """

    def __init__(
        self,
        symbol: str = "XAUUSD",
        timeframe_minutes: int = 5,
        magic: int = 20250918,
        deviation: int = 20,
        dry_run: bool = False,
        log_level: int = logging.INFO,
    ):
        self.symbol = symbol
        self.timeframe_minutes = timeframe_minutes
        self.magic = magic
        self.deviation = deviation
        self.dry_run = dry_run
        self.connected = False

        # 如果 MetaTrader5 包未安装，强制 dry_run
        if not MT5_AVAILABLE and not dry_run:
            logger.warning("MetaTrader5 包未安装，自动切换到 DRY_RUN 模拟模式")
            self.dry_run = True

        logging.basicConfig(
            level=log_level,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # Dry-run 时的模拟状态
        self._mock_positions: List[PositionInfo] = []
        self._mock_balance: float = 100000.0
        self._mock_ticket_counter: int = 100000

    # ---------- 连接 ----------
    def connect(
        self,
        login: Optional[int] = None,
        password: Optional[str] = None,
        server: Optional[str] = None,
        path: Optional[str] = None,
        timeout: int = 60000,
    ) -> bool:
        """
        连接到 MT5 终端。
        参数都可在 MT5 终端 -> 工具 -> 选项 -> 服务器 中找到。
        path: terminal64.exe 完整路径，Windows 上建议显式传入，避免多终端混淆

        返回 True 表示连接成功
        """
        if self.dry_run:
            logger.info("[DRY_RUN] 模拟连接成功 (无需真实MT5终端)")
            self.connected = True
            return True

        if not MT5_AVAILABLE:
            logger.error("MetaTrader5 包未安装，请在 Windows 上执行: pip install MetaTrader5")
            return False

        # 初始化终端
        init_kwargs = {}
        if path:
            init_kwargs["path"] = path
        if login is not None:
            init_kwargs["login"] = login
        if password is not None:
            init_kwargs["password"] = password
        if server is not None:
            init_kwargs["server"] = server
        init_kwargs["timeout"] = timeout
        init_kwargs["portable"] = False

        logger.info(f"正在连接 MT5 终端... login={login} server={server} path={path or '默认'}")
        if not mt5.initialize(**init_kwargs):  # type: ignore
            err = mt5.last_error()  # type: ignore
            logger.error(f"MT5 initialize 失败: {err}")
            return False

        # 登录校验
        account = mt5.account_info()  # type: ignore
        if account is None:
            logger.error(f"无法获取账户信息: {mt5.last_error()}")  # type: ignore
            return False

        logger.info(f"✅ 已连接 MT5 | 账户: {account.login} | 服务器: {account.server} | 余额: {account.balance:.2f} {account.currency} | 杠杆: 1:{account.leverage}")

        # 检查交易权限
        terminal = mt5.terminal_info()  # type: ignore
        if terminal is not None:
            logger.info(f"终端状态: 已连接={terminal.connected} | 交易允许={terminal.trade_allowed} | Algo交易={terminal.trade_allowed}")
            if not terminal.trade_allowed:
                logger.warning("⚠️  终端未允许自动交易！请在 MT5 中点击 'Algo Trading' 按钮使其变绿，并在 工具->选项->EA交易 中勾选 '允许自动交易'")

        # 检查品种可用性
        symbol_info = mt5.symbol_info(self.symbol)  # type: ignore
        if symbol_info is None:
            logger.error(f"品种 {self.symbol} 不存在，请检查是否拼写为 XAUUSD / GOLD 等 (不同经纪商命名不同)")
            return False

        # 选中品种到市场报价
        if not symbol_info.visible:
            logger.info(f"正在订阅品种 {self.symbol}...")
            if not mt5.symbol_select(self.symbol, True):  # type: ignore
                logger.error(f"订阅 {self.symbol} 失败")
                return False

        self.connected = True
        logger.info(f"✅ 品种 {self.symbol} 已就绪 | 点差: {symbol_info.point} | 合约大小: {symbol_info.contract_size}")

        # 打印关键配置
        logger.info(f"时间周期: M{self.timeframe_minutes} | Magic: {self.magic} | 允许偏差: {self.deviation} points")

        return True

    def disconnect(self):
        if not self.dry_run and MT5_AVAILABLE:
            mt5.shutdown()  # type: ignore
            logger.info("MT5 连接已断开")
        self.connected = False

    # ---------- 行情 ----------
    def _mt5_timeframe(self):
        if self.dry_run or not MT5_AVAILABLE:
            return None
        mapping = {
            1: mt5.TIMEFRAME_M1,  # type: ignore
            5: mt5.TIMEFRAME_M5,  # type: ignore
            15: mt5.TIMEFRAME_M15,  # type: ignore
            30: mt5.TIMEFRAME_M30,  # type: ignore
            60: mt5.TIMEFRAME_H1,  # type: ignore
            240: mt5.TIMEFRAME_H4,  # type: ignore
            1440: mt5.TIMEFRAME_D1,  # type: ignore
        }
        return mapping.get(self.timeframe_minutes, mt5.TIMEFRAME_M5)  # type: ignore

    def get_rates(self, count: int = 500, start_pos: int = 0) -> Optional["pd.DataFrame"]:
        """
        拉取最近 count 根 K线，返回 pandas DataFrame
        列: time, open, high, low, close, tick_volume, spread, real_volume
        时间为 UTC
        """
        if self.dry_run:
            # 尝试从本地 CSV 读取模拟数据
            return self._mock_get_rates(count)

        if not self.connected:
            logger.error("未连接 MT5，无法获取行情")
            return None

        tf = self._mt5_timeframe()
        rates = mt5.copy_rates_from_pos(self.symbol, tf, start_pos, count)  # type: ignore
        if rates is None or len(rates) == 0:
            logger.error(f"获取 {self.symbol} 行情失败: {mt5.last_error()}")  # type: ignore
            return None

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        # 重命名以匹配项目 CSV 格式
        # MT5 返回 open/high/low/close 已是正确价格
        logger.debug(f"获取 {len(df)} 根 {self.symbol} M{self.timeframe_minutes} K线 | 最新: {df.iloc[-1]['time']} O:{df.iloc[-1]['open']:.2f} C:{df.iloc[-1]['close']:.2f}")
        return df

    def _mock_get_rates(self, count: int = 500) -> Optional["pd.DataFrame"]:
        """Dry-run 模式：从本地 CSV 读取"""
        if not PANDAS_AVAILABLE:
            logger.error("需要 pandas 来读取 mock 数据")
            return None
        base = Path(__file__).resolve().parent.parent.parent
        candidates = [
            base / "data" / "XAUUSD_5m_5Yea.csv",
            base / "data" / "XAUUSD_5m_5Yea.csv",
        ]
        csv_path = None
        for p in candidates:
            if p.exists():
                csv_path = p
                break
        if csv_path is None:
            logger.warning("[DRY_RUN] 未找到本地 CSV，生成随机模拟数据")
            # 生成随机数据用于测试
            now = datetime.now(timezone.utc)
            times = [now - timedelta(minutes=5 * i) for i in range(count)][::-1]
            np.random.seed(42)
            base_price = 2000.0
            closes = base_price + np.cumsum(np.random.randn(count) * 2)
            df = pd.DataFrame({
                "time": times,
                "open": closes + np.random.randn(count) * 0.5,
                "high": closes + np.abs(np.random.randn(count)) * 1.5,
                "low": closes - np.abs(np.random.randn(count)) * 1.5,
                "close": closes,
                "tick_volume": np.random.randint(100000, 500000, count),
                "spread": np.random.randint(20, 40, count),
                "real_volume": np.random.randint(100000, 500000, count),
            })
            # 修正 high/low
            df["high"] = df[["open", "close", "high"]].max(axis=1)
            df["low"] = df[["open", "close", "low"]].min(axis=1)
            return df

        # 尝试解析项目中的 CSV 格式：Date,Time,Open,High,Low,Close,Volume
        try:
            df_raw = pd.read_csv(csv_path, nrows=count * 2)  # 多读一些
            # 兼容两种格式
            if "Date" in df_raw.columns and "Time" in df_raw.columns:
                df_raw["time"] = pd.to_datetime(df_raw["Date"].astype(str) + " " + df_raw["Time"].astype(str), utc=True)
                df_raw = df_raw.rename(columns={
                    "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "tick_volume"
                })
                df_raw["spread"] = 30
                df_raw["real_volume"] = df_raw["tick_volume"]
                df = df_raw[["time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]].tail(count).reset_index(drop=True)
            else:
                df = df_raw.tail(count).reset_index(drop=True)
            logger.info(f"[DRY_RUN] 从 {csv_path.name} 读取 {len(df)} 根K线作为模拟数据")
            return df
        except Exception as e:
            logger.warning(f"[DRY_RUN] 读取 CSV 失败: {e}，生成随机数据")
            return self._mock_get_rates.__wrapped__ if hasattr(self._mock_get_rates, "__wrapped__") else None

    def get_symbol_info(self) -> Optional[SymbolInfo]:
        if self.dry_run:
            return SymbolInfo(
                symbol=self.symbol,
                point=0.01,
                tick_value=1.0,
                tick_size=0.01,
                volume_min=0.01,
                volume_max=100.0,
                volume_step=0.01,
                contract_size=100.0,
                spread=30,
                margin_initial=0.0,
            )
        if not self.connected:
            return None
        info = mt5.symbol_info(self.symbol)  # type: ignore
        if info is None:
            return None
        return SymbolInfo(
            symbol=info.symbol,
            point=info.point,
            tick_value=info.trade_tick_value,
            tick_size=info.trade_tick_size,
            volume_min=info.volume_min,
            volume_max=info.volume_max,
            volume_step=info.volume_step,
            contract_size=info.contract_size,
            spread=info.spread,
            margin_initial=info.margin_initial,
        )

    def get_account_info(self) -> Optional[Dict[str, Any]]:
        if self.dry_run:
            return {"balance": self._mock_balance, "equity": self._mock_balance, "currency": "USD", "leverage": 30}
        if not self.connected:
            return None
        acc = mt5.account_info()  # type: ignore
        if acc is None:
            return None
        return {"balance": acc.balance, "equity": acc.equity, "currency": acc.currency, "leverage": acc.leverage, "profit": acc.profit}

    def get_positions(self) -> List[PositionInfo]:
        if self.dry_run:
            return [p for p in self._mock_positions if p.symbol == self.symbol]

        if not self.connected:
            return []
        positions = mt5.positions_get(symbol=self.symbol)  # type: ignore
        if positions is None:
            return []
        result = []
        for p in positions:
            result.append(PositionInfo(
                ticket=p.ticket, symbol=p.symbol, type=p.type, volume=p.volume,
                price_open=p.price_open, price_current=p.price_current,
                sl=p.sl, tp=p.tp, profit=p.profit, magic=p.magic
            ))
        return result

    def has_open_position(self) -> bool:
        return len(self.get_positions()) > 0

    # ---------- 下单 ----------
    def buy(self, volume: float, sl: Optional[float] = None, tp: Optional[float] = None, comment: str = "Sunrise LONG") -> OrderResult:
        return self._send_order("BUY", volume, sl, tp, comment)

    def sell(self, volume: float, sl: Optional[float] = None, tp: Optional[float] = None, comment: str = "Sunrise SHORT") -> OrderResult:
        return self._send_order("SELL", volume, sl, tp, comment)

    def close_all_positions(self) -> bool:
        positions = self.get_positions()
        if not positions:
            logger.info("无持仓需要平仓")
            return True

        success = True
        for pos in positions:
            result = self.close_position(pos)
            if not result.success:
                success = False
        return success

    def close_position(self, position: PositionInfo) -> OrderResult:
        if self.dry_run:
            logger.info(f"[DRY_RUN] 模拟平仓 | Ticket={position.ticket} {position.symbol} {position.type} Vol={position.volume} | 当前价≈{position.price_current:.2f}")
            self._mock_positions = [p for p in self._mock_positions if p.ticket != position.ticket]
            # 简单结算：假设价格回到开仓价附近随机
            import random
            pnl = random.uniform(-500, 800)
            self._mock_balance += pnl
            logger.info(f"[DRY_RUN] 平仓完成 PnL≈{pnl:.2f} | 新余额: {self._mock_balance:.2f}")
            return OrderResult(True, 10009, "Done (mock)", ticket=position.ticket, volume=position.volume, price=position.price_current)

        if not self.connected:
            return OrderResult(False, -1, "Not connected")

        # MT5 平仓 = 反向单
        order_type = mt5.ORDER_TYPE_SELL if position.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY  # type: ignore
        price = mt5.symbol_info_tick(self.symbol).bid if position.type == mt5.POSITION_TYPE_BUY else mt5.symbol_info_tick(self.symbol).ask  # type: ignore
        if price is None:
            return OrderResult(False, -1, "No tick")

        request = {
            "action": mt5.TRADE_ACTION_DEAL,  # type: ignore
            "symbol": self.symbol,
            "volume": position.volume,
            "type": order_type,
            "position": position.ticket,
            "price": price,
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": "Sunrise Close",
            "type_time": mt5.ORDER_TIME_GTC,  # type: ignore
            "type_filling": mt5.ORDER_FILLING_IOC,  # type: ignore
        }

        result = mt5.order_send(request)  # type: ignore
        if result is None:
            return OrderResult(False, -1, f"order_send returned None: {mt5.last_error()}")  # type: ignore
        success = result.retcode == mt5.TRADE_RETCODE_DONE  # type: ignore
        logger.info(f"平仓 {'成功' if success else '失败'} | {result.comment} | retcode={result.retcode} | deal={result.deal}")
        return OrderResult(success, result.retcode, result.comment, ticket=result.deal, volume=position.volume, price=price)

    def _send_order(self, direction: str, volume: float, sl: Optional[float], tp: Optional[float], comment: str) -> OrderResult:
        """
        发送市价单。volume 单位是 MT5 手数 (XAUUSD: 0.01起步，1手=100盎司)
        """
        # 规范 volume 到合法步进
        info = self.get_symbol_info()
        if info:
            volume = max(info.volume_min, min(info.volume_max, volume))
            # 按步进取整
            steps = round(volume / info.volume_step)
            volume = round(steps * info.volume_step, 2)
            # MT5 要求 2 位小数
            volume = round(volume, 2)

        if self.dry_run:
            self._mock_ticket_counter += 1
            ticket = self._mock_ticket_counter
            side = "BUY" if direction == "BUY" else "SELL"
            logger.info(f"[DRY_RUN] 模拟下单 | {side} {self.symbol} Vol={volume} SL={sl:.2f if sl else None} TP={tp:.2f if tp else None} | Magic={self.magic} | Comment={comment} | Ticket={ticket}")
            # 获取模拟价格
            df = self._mock_get_rates(1)
            price = float(df.iloc[-1]["close"]) if df is not None and len(df) > 0 else 2000.0
            pos_type = 0 if direction == "BUY" else 1
            mock_pos = PositionInfo(
                ticket=ticket, symbol=self.symbol, type=pos_type, volume=volume,
                price_open=price, price_current=price,
                sl=sl or 0.0, tp=tp or 0.0, profit=0.0, magic=self.magic
            )
            self._mock_positions.append(mock_pos)
            return OrderResult(True, 10009, "Done (mock)", ticket=ticket, volume=volume, price=price)

        if not self.connected:
            return OrderResult(False, -1, "Not connected")

        # 检查
        if self.has_open_position():
            logger.warning(f"已有 {self.symbol} 持仓，MT5 默认净额/对冲模式下可能拒绝新单或增加仓位。请确认账户模式。")

        tick = mt5.symbol_info_tick(self.symbol)  # type: ignore
        if tick is None:
            return OrderResult(False, -1, "No tick data")

        order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL  # type: ignore
        price = tick.ask if direction == "BUY" else tick.bid

        request = {
            "action": mt5.TRADE_ACTION_DEAL,  # type: ignore
            "symbol": self.symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": sl if sl else 0.0,
            "tp": tp if tp else 0.0,
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": comment[:31],  # MT5 限制 31 字符
            "type_time": mt5.ORDER_TIME_GTC,  # type: ignore
            "type_filling": mt5.ORDER_FILLING_IOC,  # type: ignore
        }

        logger.info(f"发送订单 | {direction} {self.symbol} Vol={volume} Price={price:.2f} SL={sl:.2f if sl else 0} TP={tp:.2f if tp else 0} Dev={self.deviation}")
        result = mt5.order_send(request)  # type: ignore
        if result is None:
            err = mt5.last_error()  # type: ignore
            logger.error(f"order_send 返回 None | last_error={err}")
            return OrderResult(False, -1, f"None: {err}")

        success = result.retcode == mt5.TRADE_RETCODE_DONE  # type: ignore
        if success:
            logger.info(f"✅ 订单成功 | Deal={result.deal} | Order={result.order} | Vol={result.volume} | Price={result.price:.2f} | Comment={result.comment}")
        else:
            logger.error(f"❌ 订单失败 | retcode={result.retcode} ({result.comment}) | request={request}")
            # 常见错误提示
            if result.retcode == 10027:  # TRADE_RETCODE_CLIENT_DISABLES_AT
                logger.error("  → 原因：EA自动交易被禁用。请在 MT5 终端点击 'Algo Trading' 使其变绿")
            elif result.retcode == 10030:
                logger.error("  → 原因：品种交易被禁用或处于只平仓模式")
            elif result.retcode == 10014:
                logger.error("  → 原因：手数无效，检查 volume_min/step")
            elif result.retcode == 10015:
                logger.error("  → 原因：价格无效，可能是点差过大或价格过期，重试即可")
            elif result.retcode == 10016:
                logger.error("  → 原因：止损/止盈距离太近，不满足经纪商最小止损距离")

        return OrderResult(success, result.retcode, result.comment, ticket=result.order, volume=result.volume, price=result.price)

    # ---------- 工具 ----------
    def calculate_lot_size(self, entry_price: float, stop_loss: float, risk_percent: float = 0.01, balance: Optional[float] = None) -> float:
        """
        风险百分比手数计算，逻辑与 Backtrader 版本一致：
        risk_amount = balance * risk_percent
        risk_per_oz = |entry - sl|
        lots = risk_amount / (risk_per_oz * contract_size)

        XAUUSD: contract_size=100 (1手=100盎司)，每盎司1美元波动
        """
        info = self.get_symbol_info()
        contract_size = info.contract_size if info else 100.0

        if balance is None:
            acc = self.get_account_info()
            balance = acc["balance"] if acc else 10000.0

        risk_distance = abs(entry_price - stop_loss)
        if risk_distance <= 0:
            logger.warning("SL 距离为0，无法计算手数，返回最小手数")
            return info.volume_min if info else 0.01

        risk_amount = balance * risk_percent
        # 对于 XAUUSD：每盎司价格波动1美元，1手100盎司，所以 risk_per_lot = risk_distance * 100
        risk_per_lot = risk_distance * contract_size
        lots = risk_amount / risk_per_lot

        # 按品种限制取整
        if info:
            lots = max(info.volume_min, min(info.volume_max, lots))
            steps = round(lots / info.volume_step)
            lots = round(steps * info.volume_step, 2)

        lots = max(0.01, round(lots, 2))
        logger.info(f"手数计算 | 余额=${balance:.2f} 风险={risk_percent*100:.1f}% SL距离={risk_distance:.2f} | 风险金额=${risk_amount:.2f} | 计算手数={lots} lots")
        return lots

    def is_trading_allowed(self) -> bool:
        """检查当前是否允许交易（连接 + 品种可交易 + Algo允许）"""
        if self.dry_run:
            return True
        if not self.connected:
            return False
        # 终端级
        terminal = mt5.terminal_info()  # type: ignore
        if terminal and not terminal.trade_allowed:
            return False
        # 品种级
        info = mt5.symbol_info(self.symbol)  # type: ignore
        if info and info.trade_mode == mt5.SYMBOL_TRADE_MODE_DISABLED:  # type: ignore
            return False
        return True
