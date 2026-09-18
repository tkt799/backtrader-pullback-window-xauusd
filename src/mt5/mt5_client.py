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
import os
import glob
import platform
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

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

# ============ 自动识别常量 ============
# 常见黄金品种命名（不同经纪商差异很大，自动探测时依次尝试）
GOLD_SYMBOL_CANDIDATES = [
    "XAUUSD", "GOLD", "XAUUSD.a", "XAUUSDc", "XAUUSD.", "GOLD#",
    "XAUUSD.r", "XAUUSDm", "XAUUSDpro", "XAU/USD", "XAUUSD-ECN",
    "XAUUSD.s", "GOLD.m", "GOLDpro", "XAUEUR", "XAUUSD_",
    "GOLD-ECN", "XAUUSDecn", "Gold", "XAUUSD_ecn",
]

# 常见 MT5 终端路径（Windows）
COMMON_MT5_PATHS = [
    r"C:\Program Files\MetaTrader 5\terminal64.exe",
    r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe",
    r"C:\Program Files\MetaTrader 5 Terminal\terminal64.exe",
    r"C:\MT5\terminal64.exe",
    r"C:\Tickmill MT5\terminal64.exe",
    r"C:\ICMarkets MT5\terminal64.exe",
    r"C:\Exness MT5\terminal64.exe",
    r"C:\XM MT5\terminal64.exe",
    r"C:\Darwinex MT5\terminal64.exe",
]


def find_mt5_terminals() -> List[Path]:
    """
    自动扫描本机 MT5 终端路径，返回所有找到的 terminal64.exe
    策略：
    1. 环境变量 MT5_PATH / MT5_TERMINAL_PATH
    2. Windows 注册表 (MetaQuotes)
    3. 常见安装路径 + 通配扫描
    4. 去重并按修改时间排序（最新的在前）
    适合 --auto 模式，无需用户手动指定 --path
    """
    found: List[Path] = []

    # 1. 环境变量
    for env_key in ("MT5_PATH", "MT5_TERMINAL_PATH", "MT5_TERMINAL"):
        env_val = os.getenv(env_key)
        if env_val:
            p = Path(env_val)
            if p.is_file() and p.name.lower() == "terminal64.exe":
                if p not in found:
                    found.append(p)
                    logger.debug(f"通过环境变量 {env_key} 找到: {p}")
            elif p.is_dir():
                cand = p / "terminal64.exe"
                if cand.is_file() and cand not in found:
                    found.append(cand)

    # 2. Windows 注册表（仅 Windows）
    if platform.system() == "Windows":
        try:
            import winreg  # type: ignore
            reg_paths = [
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\MetaQuotes\MetaTrader 5"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\MetaQuotes\MetaTrader 5"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\MetaQuotes\MetaTrader 5"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\MetaQuotes\MetaTrader"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\MetaQuotes\MetaTrader"),
            ]
            for hive, key_path in reg_paths:
                try:
                    with winreg.OpenKey(hive, key_path) as key:
                        # 遍历所有子键或值
                        try:
                            # 尝试读取 Path / InstallPath 等值
                            for val_name in ("Path", "InstallPath", "Directory", ""):
                                try:
                                    val, _ = winreg.QueryValueEx(key, val_name)
                                    if val:
                                        cand = Path(str(val)) / "terminal64.exe"
                                        if cand.is_file() and cand not in found:
                                            found.append(cand)
                                            logger.debug(f"通过注册表 {key_path} \\ {val_name} 找到: {cand}")
                                except FileNotFoundError:
                                    continue
                        except OSError:
                            pass
                        # 遍历子键
                        try:
                            i = 0
                            while True:
                                sub = winreg.EnumKey(key, i)
                                try:
                                    with winreg.OpenKey(key, sub) as subkey:
                                        try:
                                            val, _ = winreg.QueryValueEx(subkey, "Path")
                                            cand = Path(str(val)) / "terminal64.exe"
                                            if cand.is_file() and cand not in found:
                                                found.append(cand)
                                        except FileNotFoundError:
                                            pass
                                except OSError:
                                    pass
                                i += 1
                        except OSError:
                            pass
                except FileNotFoundError:
                    continue
        except ImportError:
            logger.debug("winreg 不可用，跳过注册表扫描")
        except Exception as e:
            logger.debug(f"注册表扫描异常: {e}")

    # 3. 常见路径直接检查
    for p_str in COMMON_MT5_PATHS:
        p = Path(p_str)
        if p.is_file() and p not in found:
            found.append(p)
            logger.debug(f"通过常见路径找到: {p}")

    # 4. 通配扫描 Program Files 下的所有 terminal64.exe（限 Windows，避免过慢）
    if platform.system() == "Windows":
        for base in [r"C:\Program Files", r"C:\Program Files (x86)", r"C:\MT5", r"C:\Trading"]:
            if os.path.isdir(base):
                try:
                    # 限制扫描深度 2 层，避免全盘扫描
                    pattern = os.path.join(base, "*", "terminal64.exe")
                    for match in glob.glob(pattern):
                        p = Path(match)
                        if p.is_file() and p not in found:
                            found.append(p)
                            logger.debug(f"通过通配扫描找到: {p}")
                    # 2层深度
                    pattern2 = os.path.join(base, "*", "*", "terminal64.exe")
                    for match in glob.glob(pattern2):
                        p = Path(match)
                        if p.is_file() and p not in found and len(found) < 10:  # 限制数量
                            found.append(p)
                except Exception:
                    continue

    # 去重 + 按修改时间排序（新安装的在前）
    unique: List[Path] = []
    seen = set()
    for p in found:
        rp = str(p.resolve()).lower()
        if rp not in seen:
            seen.add(rp)
            unique.append(p)

    # 按 mtime 排序
    try:
        unique.sort(key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)
    except Exception:
        pass

    if unique:
        logger.info(f"🔍 自动发现 {len(unique)} 个 MT5 终端:")
        for idx, p in enumerate(unique, 1):
            logger.info(f"  [{idx}] {p}")
    else:
        logger.debug("未自动发现任何 MT5 终端")

    return unique


def detect_gold_symbol_fallback() -> List[str]:
    """
    返回按优先级排序的黄金品种候选（用于离线 Dry-Run 时的提示）
    """
    return GOLD_SYMBOL_CANDIDATES.copy()

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

    # ---------- 自动识别 ----------
    def auto_detect_symbol(self, preferred: Optional[str] = None) -> Optional[str]:
        """
        自动识别黄金品种：遍历候选列表 + 调用 MT5 的 symbols_get 模糊搜索
        优先使用 preferred，其次按 GOLD_SYMBOL_CANDIDATES 顺序
        成功则更新 self.symbol 并返回品种名，失败返回 None
        """
        if self.dry_run:
            # Dry-Run 下不需要真实探测，直接沿用
            return self.symbol

        if not MT5_AVAILABLE or not self.connected:
            logger.debug("未连接 MT5，无法自动识别品种")
            return None

        # 候选队列：用户指定优先，其次内置候选
        candidates: List[str] = []
        if preferred and preferred not in candidates:
            candidates.append(preferred)
        if self.symbol and self.symbol not in candidates:
            candidates.append(self.symbol)
        for cand in GOLD_SYMBOL_CANDIDATES:
            if cand not in candidates:
                candidates.append(cand)

        # 1. 直接尝试候选
        for cand in candidates:
            info = mt5.symbol_info(cand)  # type: ignore
            if info is not None:
                # 有些品种存在但被禁用交易，需检查 trade_mode
                # 优先选可见或可交易的
                logger.info(f"🔍 品种探测命中: {cand} (可见={info.visible} 点差={info.point} 合约={info.contract_size})")
                # 自动订阅
                if not info.visible:
                    try:
                        mt5.symbol_select(cand, True)  # type: ignore
                    except Exception:
                        pass
                self.symbol = cand
                return cand

        # 2. 模糊搜索：遍历所有含 GOLD/XAU 的品种
        try:
            all_symbols = mt5.symbols_get()  # type: ignore
            if all_symbols:
                gold_like = []
                for s in all_symbols:
                    name = s.name.upper()
                    if "GOLD" in name or "XAU" in name:
                        gold_like.append(s.name)
                if gold_like:
                    logger.info(f"🔍 模糊搜索发现 {len(gold_like)} 个黄金相关品种: {gold_like[:10]}")
                    for cand in gold_like:
                        # 优先 XAUUSD / GOLD 精确匹配
                        if cand.upper() in [c.upper() for c in GOLD_SYMBOL_CANDIDATES]:
                            info = mt5.symbol_info(cand)  # type: ignore
                            if info is not None:
                                if not info.visible:
                                    mt5.symbol_select(cand, True)  # type: ignore
                                self.symbol = cand
                                logger.info(f"✅ 通过模糊搜索自动识别品种: {cand}")
                                return cand
                    # 否则返回第一个可见的
                    for cand in gold_like:
                        info = mt5.symbol_info(cand)  # type: ignore
                        if info and info.visible:
                            self.symbol = cand
                            logger.info(f"✅ 自动选用第一个可见黄金品种: {cand}")
                            return cand
                    # 兜底：第一个
                    if gold_like:
                        cand = gold_like[0]
                        mt5.symbol_select(cand, True)  # type: ignore
                        self.symbol = cand
                        return cand
        except Exception as e:
            logger.debug(f"模糊搜索异常: {e}")

        logger.warning(f"❌ 未能自动识别任何黄金品种，候选已全部尝试: {candidates[:8]}...")
        return None

    def auto_detect_mt5_path(self) -> Optional[str]:
        """
        自动探测 MT5 安装路径，返回首选的 terminal64.exe
        """
        terminals = find_mt5_terminals()
        if not terminals:
            logger.warning("未自动发现 MT5 终端，请手动指定 --path")
            return None
        best = str(terminals[0])
        logger.info(f"✅ 自动选用 MT5 路径: {best} (共发现 {len(terminals)} 个)")
        if len(terminals) > 1:
            logger.info(f"   提示：如需指定其他终端，请使用 --path \"{terminals[1]}\"")
        return best

    # ---------- 连接 ----------
    def connect(
        self,
        login: Optional[int] = None,
        password: Optional[str] = None,
        server: Optional[str] = None,
        path: Optional[str] = None,
        timeout: int = 60000,
        auto_symbol: bool = True,
        auto_path: bool = True,
    ) -> bool:
        """
        连接到 MT5 终端。
        参数都可在 MT5 终端 -> 工具 -> 选项 -> 服务器 中找到。
        path: terminal64.exe 完整路径，Windows 上建议显式传入，避免多终端混淆
        auto_symbol: True 时若指定品种不存在，自动尝试黄金候选列表
        auto_path: True 时若 path 未指定，自动扫描本机 MT5 安装

        新增自动识别能力：
        - login/password/server 均不传时，自动复用终端已登录的账号（适合 --auto）
        - path 为空时自动扫描注册表和常见路径
        - 品种不存在时自动遍历 XAUUSD/GOLD 等候选
        返回 True 表示连接成功
        """
        if self.dry_run:
            logger.info("[DRY_RUN] 模拟连接成功 (无需真实MT5终端)")
            # Dry-Run 下也演示品种探测逻辑
            if auto_symbol and self.symbol.upper() not in [c.upper() for c in GOLD_SYMBOL_CANDIDATES]:
                logger.info(f"[DRY_RUN] 品种 {self.symbol} 不在常规列表，仍演示自动候选: {GOLD_SYMBOL_CANDIDATES[:4]}...")
            self.connected = True
            return True

        if not MT5_AVAILABLE:
            logger.error("MetaTrader5 包未安装，请在 Windows 上执行: pip install MetaTrader5")
            return False

        # 自动路径探测
        original_path = path
        if auto_path and not path:
            detected = self.auto_detect_mt5_path()
            if detected:
                path = detected
                logger.info(f"🔍 自动识别 MT5 路径: {path}")
            else:
                logger.info("未指定 --path 且未自动发现，将尝试默认初始化（依赖 MT5 已运行）")

        # 初始化终端
        init_kwargs = {}
        if path:
            init_kwargs["path"] = path
        # 只有当用户显式提供时才传入登录参数；否则让 MT5 复用已登录会话
        # 这样 --auto 模式下无需账号密码也能连接演示账户
        if login is not None:
            init_kwargs["login"] = login
        if password is not None:
            init_kwargs["password"] = password
        if server is not None:
            init_kwargs["server"] = server
        init_kwargs["timeout"] = timeout
        init_kwargs["portable"] = False

        # 日志：区分自动 vs 手动
        if login is None and password is None and server is None:
            logger.info(f"正在自动连接 MT5 终端（复用已登录会话）... path={path or '默认'}")
        else:
            logger.info(f"正在连接 MT5 终端... login={login} server={server} path={path or '默认'}")

        # 若指定路径失败，尝试回退到无 path 的默认初始化（常见于便携版）
        if not mt5.initialize(**init_kwargs):  # type: ignore
            err = mt5.last_error()  # type: ignore
            logger.warning(f"MT5 initialize 失败: {err} | 参数={ {k: ('***' if k=='password' else v) for k,v in init_kwargs.items()} }")
            # 自动回退：若最初带 path 失败，尝试不带 path 重试
            if path and original_path is None:
                # 说明是自动探测的路径失败，回退
                logger.info("尝试回退：不指定 path 重新 initialize（使用已运行终端）...")
                retry_kwargs = {k: v for k, v in init_kwargs.items() if k != "path"}
                if not mt5.initialize(**retry_kwargs):  # type: ignore
                    err2 = mt5.last_error()  # type: ignore
                    logger.error(f"回退后仍失败: {err2}")
                    # 若是多终端，尝试其他路径
                    if auto_path:
                        alts = find_mt5_terminals()
                        for alt in alts[1:3]:  # 最多再试2个
                            logger.info(f"尝试备用路径: {alt}")
                            retry_kwargs["path"] = str(alt)
                            if mt5.initialize(**retry_kwargs):  # type: ignore
                                logger.info(f"✅ 备用路径连接成功: {alt}")
                                path = str(alt)
                                break
                        else:
                            return False
                    else:
                        return False
                else:
                    logger.info("✅ 回退后连接成功（复用已运行终端）")
            else:
                # 非自动路径，直接失败
                # 如果是自动探测但用户显式指定 path 为空，仍有机会尝试其他候选
                if auto_path and not original_path:
                    alts = find_mt5_terminals()
                    if len(alts) > 1:
                        for alt in alts:
                            if str(alt) == path:
                                continue
                            logger.info(f"尝试备用路径: {alt}")
                            init_kwargs["path"] = str(alt)
                            if mt5.initialize(**init_kwargs):  # type: ignore
                                logger.info(f"✅ 备用路径连接成功: {alt}")
                                path = str(alt)
                                break
                        else:
                            logger.error("所有自动发现路径均连接失败")
                            return False
                    else:
                        return False
                else:
                    return False

        # 登录校验（自动模式下可能已登录，无需额外校验）
        account = mt5.account_info()  # type: ignore
        if account is None:
            logger.error(f"无法获取账户信息: {mt5.last_error()}")  # type: ignore
            # 尝试给出更友好的提示
            terminal = mt5.terminal_info()  # type: ignore
            if terminal is None or not terminal.connected:
                logger.error("终端未连接经纪商服务器，请检查网络或在 MT5 中手动登录")
            return False

        # 自动识别提示：若用户未提供登录信息，说明是复用会话
        if login is None:
            logger.info(f"✅ 已自动识别并复用终端已登录账户 | 账户: {account.login} | 服务器: {account.server} | 余额: {account.balance:.2f} {account.currency} | 杠杆: 1:{account.leverage}")
        else:
            logger.info(f"✅ 已连接 MT5 | 账户: {account.login} | 服务器: {account.server} | 余额: {account.balance:.2f} {account.currency} | 杠杆: 1:{account.leverage}")

        # 检查交易权限
        terminal = mt5.terminal_info()  # type: ignore
        if terminal is not None:
            logger.info(f"终端状态: 已连接={terminal.connected} | 交易允许={terminal.trade_allowed} | Algo交易={terminal.trade_allowed}")
            if not terminal.trade_allowed:
                logger.warning("⚠️  终端未允许自动交易！请在 MT5 中点击 'Algo Trading' 按钮使其变绿，并在 工具->选项->EA交易 中勾选 '允许自动交易'")

        # 检查品种可用性（带自动回退）
        original_symbol = self.symbol
        symbol_info = mt5.symbol_info(self.symbol)  # type: ignore
        if symbol_info is None:
            logger.warning(f"品种 {self.symbol} 不存在，尝试自动识别黄金品种...")
            if auto_symbol:
                detected = self.auto_detect_symbol(preferred=original_symbol)
                if detected:
                    symbol_info = mt5.symbol_info(detected)  # type: ignore
                    logger.info(f"✅ 自动识别品种成功: {original_symbol} → {detected}")
                else:
                    logger.error(f"品种 {original_symbol} 不存在，且自动识别失败。请检查是否拼写为 XAUUSD / GOLD 等 (不同经纪商命名不同)，或用 --symbol 指定")
                    # 打印可用黄金品种供用户参考
                    try:
                        all_syms = mt5.symbols_get()  # type: ignore
                        if all_syms:
                            golds = [s.name for s in all_syms if "GOLD" in s.name.upper() or "XAU" in s.name.upper()][:10]
                            if golds:
                                logger.info(f"终端中可用的黄金相关品种: {golds}")
                    except Exception:
                        pass
                    return False
            else:
                logger.error(f"品种 {self.symbol} 不存在，请检查是否拼写为 XAUUSD / GOLD 等 (不同经纪商命名不同)")
                return False
        else:
            # 即使品种存在，也尝试确认是否可见；若不可见，标记为需订阅
            logger.debug(f"品种 {self.symbol} 存在: 可见={symbol_info.visible}")

        # 选中品种到市场报价
        if not symbol_info.visible:
            logger.info(f"正在订阅品种 {self.symbol}...")
            if not mt5.symbol_select(self.symbol, True):  # type: ignore
                logger.error(f"订阅 {self.symbol} 失败")
                # 尝试自动识别备用品种
                if auto_symbol and self.symbol == original_symbol:
                    logger.info("订阅失败，尝试自动切换到其他黄金品种...")
                    alt = self.auto_detect_symbol()
                    if alt and alt != original_symbol:
                        symbol_info = mt5.symbol_info(alt)  # type: ignore
                        if symbol_info:
                            logger.info(f"✅ 已切换到备用品种: {alt}")
                        else:
                            return False
                    else:
                        return False
                else:
                    return False

        self.connected = True
        # 记录最终使用的路径和品种，供外部查询
        self._connected_path = path
        self._connected_symbol = self.symbol
        logger.info(f"✅ 品种 {self.symbol} 已就绪 | 点差: {symbol_info.point} | 合约大小: {symbol_info.contract_size}")

        # 打印关键配置
        logger.info(f"时间周期: M{self.timeframe_minutes} | Magic: {self.magic} | 允许偏差: {self.deviation} points")
        if auto_symbol and original_symbol != self.symbol:
            logger.info(f"🔍 品种自动识别生效: 输入 {original_symbol} → 实际使用 {self.symbol}")
        if auto_path and original_path != path:
            logger.info(f"🔍 路径自动识别生效: {path}")

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
