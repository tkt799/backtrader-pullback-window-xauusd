"""
Run Live — MT5 实盘 / 模拟 运行入口
=====================================
这是把 Backtrader 策略接到 MT5 的最终执行脚本。

三种运行模式：
  1. python src/mt5/run_live.py --dry-run
     → 无需MT5终端，使用本地CSV模拟行情，验证状态机逻辑是否正常（推荐先跑这个）

  2. python src/mt5/run_live.py --live --login 123456 --password "xxx" --server "MetaQuotes-Demo"
     → Windows 已安装MT5终端，连接真实/模拟账户实盘运行

  3. python src/mt5/run_live.py --live --login ... --path "C:\\Program Files\\MetaTrader 5\\terminal64.exe"
     → 指定终端路径（多终端用户必备）

核心循环：
  每 5 分钟（或每 10 秒轮询）拉取最新K线 → LiveSunriseStrategy.on_bar() → 如有信号 → MT5Client.buy/sell()

安全特性：
  - 单品种单持仓限制（默认禁止加仓）
  - 每日最大亏损熔断
  - SL/TP 必带
  - 断线自动重连
  - Ctrl+C 优雅退出

作者：移植自 Backtrader，与回测参数 1:1 对齐
"""

from __future__ import annotations
import time
import argparse
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 兼容直接运行与模块运行
try:
    from src.mt5.mt5_client import MT5Client, find_mt5_terminals
    from src.mt5.live_strategy import LiveSunriseConfig, LiveSunriseStrategy
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from src.mt5.mt5_client import MT5Client, find_mt5_terminals
    from src.mt5.live_strategy import LiveSunriseConfig, LiveSunriseStrategy

logger = logging.getLogger("RunLive")

# =============================================================
# 可编辑配置区（也可通过命令行覆盖）
# =============================================================
DEFAULT_SYMBOL = "XAUUSD"
DEFAULT_TIMEFRAME = 5  # M5
DEFAULT_MAGIC = 20250918
DEFAULT_RISK_PERCENT = 0.01  # 每笔1%
DEFAULT_POLL_SECONDS = 10    # 轮询间隔；M5 策略每10秒检查一次即可，整5分钟必检
DEFAULT_MAX_DAILY_LOSS_PERCENT = 0.05  # 日内最大亏损5%则停机

# 与回测一致：默认 LONG ONLY
DEFAULT_ENABLE_LONG = True
DEFAULT_ENABLE_SHORT = False

# 时间过滤（与回测一致，默认关闭）
DEFAULT_USE_TIME_FILTER = False


def parse_args():
    p = argparse.ArgumentParser(description="Sunrise Ogle MT5 实盘运行器（支持 --auto 全自动识别）")
    p.add_argument("--dry-run", action="store_true", help="模拟模式（无需MT5终端，使用本地CSV）")
    p.add_argument("--live", action="store_true", help="实盘模式（需Windows MT5终端）")
    p.add_argument("--auto", action="store_true", help="全自动模式：自动识别 MT5 路径/黄金品种/已登录账号（无需 --login/--path/--symbol）")
    p.add_argument("--symbol", type=str, default=DEFAULT_SYMBOL, help="品种名，如 XAUUSD / GOLD（--auto 时自动探测，失败则回退到此值）")
    p.add_argument("--timeframe", type=int, default=DEFAULT_TIMEFRAME, help="周期分钟数，默认5")
    p.add_argument("--magic", type=int, default=DEFAULT_MAGIC, help="Magic Number，用于标识本策略订单")
    p.add_argument("--risk", type=float, default=DEFAULT_RISK_PERCENT, help="每笔风险百分比，默认0.01")
    p.add_argument("--poll", type=int, default=DEFAULT_POLL_SECONDS, help="轮询间隔秒数，默认10")
    p.add_argument("--login", type=int, default=None, help="MT5 账号（--auto 时可省略，自动复用终端已登录账号）")
    p.add_argument("--password", type=str, default=None, help="MT5 密码")
    p.add_argument("--server", type=str, default=None, help="MT5 服务器，如 MetaQuotes-Demo")
    p.add_argument("--path", type=str, default=None, help="MT5 terminal64.exe 完整路径（--auto 时自动扫描）")
    p.add_argument("--enable-short", action="store_true", help="启用 SHORT（默认仅LONG）")
    p.add_argument("--enable-long", action="store_true", help="显式启用 LONG（默认已启用）")
    p.add_argument("--disable-long", action="store_true", help="禁用 LONG，仅做SHORT")
    p.add_argument("--max-daily-loss", type=float, default=DEFAULT_MAX_DAILY_LOSS_PERCENT, help="日内最大亏损百分比，默认0.05")
    p.add_argument("--once", action="store_true", help="仅执行一次判断后退出（用于测试）")
    p.add_argument("--no-auto-symbol", action="store_true", help="禁用自动品种识别（强制使用 --symbol）")
    p.add_argument("--no-auto-path", action="store_true", help="禁用自动路径识别（强制使用 --path）")
    p.add_argument("--list-terminals", action="store_true", help="列出本机所有 MT5 终端路径后退出（调试用）")
    p.add_argument("--list-symbols", action="store_true", help="连接后列出所有黄金相关品种后退出（调试用）")
    return p.parse_args()


def run():
    args = parse_args()

    # 日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 特殊调试指令：列出终端路径（无需连接）
    if args.list_terminals:
        terminals = find_mt5_terminals()
        if not terminals:
            print("未发现任何 MT5 终端。请检查是否已安装 MT5，或用 --path 手动指定")
            print("提示：可设置环境变量 MT5_PATH 指向 terminal64.exe")
        else:
            print(f"发现 {len(terminals)} 个 MT5 终端：")
            for idx, t in enumerate(terminals, 1):
                print(f"  [{idx}] {t}")
            print("\n可直接运行： python src/mt5/run_live.py --auto  （自动选用第1个）")
            print(f"或指定： python src/mt5/run_live.py --live --path \"{terminals[0]}\"")
        return 0

    # 决定运行模式
    if args.live and args.dry_run:
        raise SystemExit("不能同时指定 --live 和 --dry-run")

    if not args.live and not args.dry_run:
        # 默认 dry-run，方便新手一键验证
        # 若用户指定了 --auto 但未指定 --live，也视为请求 live 自动模式
        if args.auto:
            args.live = True
            logger.info("--auto 已启用，自动进入 LIVE 模式（尝试复用已登录终端）")
        else:
            logger.warning("未指定 --live 或 --dry-run，默认进入 --dry-run 模拟模式（安全）")
            args.dry_run = True

    # --auto 隐含 --live
    if args.auto and args.dry_run:
        logger.warning("--auto 与 --dry-run 同时指定，--auto 在 dry-run 下仅演示自动识别逻辑，不会真实连接")

    dry_run = args.dry_run

    # 配置策略参数
    config = LiveSunriseConfig()
    config.risk_percent = args.risk
    config.use_time_range_filter = DEFAULT_USE_TIME_FILTER

    # 交易方向
    if args.disable_long:
        config.enable_long_trades = False
    elif args.enable_long:
        config.enable_long_trades = True
    else:
        config.enable_long_trades = DEFAULT_ENABLE_LONG

    if args.enable_short:
        config.enable_short_trades = True
    else:
        config.enable_short_trades = DEFAULT_ENABLE_SHORT

    # 如果用户显式 --enable-short 且未禁用 long，则为双向
    if args.enable_short and not args.disable_long:
        config.enable_long_trades = True

    config.print_signals = True
    config.verbose_debug = False

    logger.info("="*70)
    logger.info(f"Sunrise Ogle MT5 {'模拟' if dry_run else '实盘'} 启动{' [AUTO]' if args.auto else ''}")
    logger.info(f"品种: {args.symbol} {'(自动探测)' if args.auto and not args.no_auto_symbol else ''} | 周期: M{args.timeframe} | Magic: {args.magic} | 风险: {config.risk_percent*100:.1f}%")
    logger.info(f"方向: LONG={config.enable_long_trades} SHORT={config.enable_short_trades}")
    logger.info(f"模式: {'DRY_RUN (CSV模拟)' if dry_run else 'LIVE (连接MT5终端)'}")
    if args.auto:
        logger.info(f"自动识别: 路径={'开启' if not args.no_auto_path else '关闭'} | 品种={'开启' if not args.no_auto_symbol else '关闭'} | 账号={'复用已登录' if not args.login else '指定账号'}")
    if not dry_run:
        if args.auto and not args.login:
            logger.info(f"账户: 自动复用终端已登录账号 | 终端路径: {args.path or '自动扫描'}")
        else:
            logger.info(f"账户: {args.login} @ {args.server} | 终端路径: {args.path or ('自动扫描' if not args.no_auto_path else '默认')}")
    logger.info("="*70)

    # 初始化 MT5 客户端
    client = MT5Client(
        symbol=args.symbol,
        timeframe_minutes=args.timeframe,
        magic=args.magic,
        dry_run=dry_run,
    )

    # 连接（自动识别参数）
    auto_symbol = not args.no_auto_symbol  # 默认开启，除非显式禁用
    auto_path = not args.no_auto_path
    # --auto 强制开启
    if args.auto:
        auto_symbol = True
        auto_path = True
        logger.info("🤖 --auto 模式：启用全自动识别（路径/品种/账号）")

    if not client.connect(login=args.login, password=args.password, server=args.server, path=args.path, auto_symbol=auto_symbol, auto_path=auto_path):
        logger.error("MT5 连接失败，退出")
        if args.auto:
            logger.info("💡 --auto 失败排查：")
            logger.info("  1) 确认 MT5 终端已安装并登录（查看终端左上角账号）")
            logger.info("  2) 运行 python src/mt5/run_live.py --list-terminals 查看是否能发现终端")
            logger.info("  3) 尝试手动指定： python src/mt5/run_live.py --live --path \"C:\\Program Files\\MetaTrader 5\\terminal64.exe\"")
        return 1

    # --list-symbols 调试：列出黄金品种后退出
    if args.list_symbols:
        try:
            import MetaTrader5 as mt5_dbg  # type: ignore
            all_syms = mt5_dbg.symbols_get()  # type: ignore
            if all_syms:
                golds = [s.name for s in all_syms if "GOLD" in s.name.upper() or "XAU" in s.name.upper()]
                print(f"终端中黄金相关品种 ({len(golds)} 个)： {golds[:20]}")
                print(f"当前选用: {client.symbol}")
                # 显示详细信息
                info = mt5_dbg.symbol_info(client.symbol)  # type: ignore
                if info:
                    print(f"详情: {client.symbol} 点值={info.point} 合约={info.contract_size} 可见={info.visible}")
            else:
                print("symbols_get 返回空，请确认终端已连接")
        except Exception as e:
            print(f"列出品种失败: {e}")
        client.disconnect()
        return 0

    # 获取账户与品种信息
    acc = client.get_account_info()
    sym = client.get_symbol_info()
    if acc:
        logger.info(f"账户余额: ${acc['balance']:.2f} 净值: ${acc['equity']:.2f} 杠杆: 1:{acc['leverage']}")
    if sym:
        logger.info(f"品种信息: {sym.symbol} 点值={sym.point} 合约={sym.contract_size} 最小手数={sym.volume_min} 步进={sym.volume_step} 止损距离限制需查看经纪商规范")

    # 预热策略：拉取历史K线
    logger.info("正在预热指标（拉取历史K线）...")
    df = client.get_rates(count=500)
    if df is None or len(df) < 120:
        logger.error(f"历史数据不足 ({len(df) if df is not None else 0} 根)，无法预热，请检查品种名或等待市场开盘")
        client.disconnect()
        return 1

    # 转换 DataFrame 列名以兼容 live_strategy
    # MT5 返回的 df 已有 time/open/high/low/close
    try:
        import pandas as pd
    except ImportError:
        logger.error("需要 pandas，请先 pip install pandas")
        return 1

    # 确保列名小写
    df.columns = [c.lower() if isinstance(c, str) else c for c in df.columns]

    # 如果 dry_run 时从 CSV 读取，需确保列名
    if "tick_volume" not in df.columns and "volume" in df.columns:
        df["tick_volume"] = df["volume"]

    strategy = LiveSunriseStrategy(config)
    strategy.prepare_history(df)

    logger.info(f"预热完成，当前状态: {strategy.entry_state} | 最新价: {df.iloc[-1]['close']:.2f}")
    logger.info(f"最新指标: EMA_fast={strategy.ema_fast[-1]:.2f} EMA_confirm={strategy.ema_confirm[-1]:.2f} ATR={strategy.atr_series[-1]:.4f}")

    # 记录启动时的余额用于日内风控
    start_balance = acc["balance"] if acc else 100000.0
    start_equity = acc["equity"] if acc else 100000.0
    daily_loss_limit = start_balance * args.max_daily_loss

    logger.info(f"日内熔断: 最大亏损 ${daily_loss_limit:.2f} ({args.max_daily_loss*100:.1f}%)")
    if not dry_run:
        logger.info("⚠️  即将进入实盘循环，按 Ctrl+C 停止。建议先在模拟账户(demo)测试！")
        # 二次确认
        try:
            input("按回车继续，或 Ctrl+C 取消... ")
        except KeyboardInterrupt:
            logger.info("用户取消")
            client.disconnect()
            return 0
    else:
        logger.info("🧪 DRY_RUN 模式：不会发送真实订单，仅打印信号")

    # 主循环
    last_bar_time = df.iloc[-1]["time"] if "time" in df.columns else None
    iteration = 0

    try:
        while True:
            iteration += 1
            time.sleep(args.poll)

            # ----- 风控：检查日内亏损 -----
            acc_now = client.get_account_info()
            if acc_now and not dry_run:
                daily_pnl = acc_now["equity"] - start_equity
                if daily_pnl < -daily_loss_limit:
                    logger.error(f"🛑 日内亏损已达 ${-daily_pnl:.2f} 超过熔断线 ${daily_loss_limit:.2f}，自动停机并平仓")
                    client.close_all_positions()
                    break

            # ----- 持仓检查：如已有持仓，跳过新信号（单持仓模式）-----
            if client.has_open_position():
                positions = client.get_positions()
                # 可选：打印持仓状态
                if iteration % 30 == 0:  # 每 5分钟打印一次
                    for pos in positions:
                        logger.info(f"持仓中 | Ticket={pos.ticket} {'BUY' if pos.type==0 else 'SELL'} Vol={pos.volume} 开仓={pos.price_open:.2f} 现价={pos.price_current:.2f} SL={pos.sl:.2f} TP={pos.tp:.2f} 浮盈={pos.profit:.2f}")
                # 策略本身也会在持仓时不产生新信号，但我们提前跳过网络请求可省资源
                # 注意：仍需驱动策略状态机以保持同步，所以不完全跳过
                pass

            # ----- 获取最新K线 -----
            # 为减少请求，仅在整5分钟或dry_run演示时每次都拉
            df_new = client.get_rates(count=500)
            if df_new is None or len(df_new) == 0:
                logger.warning("获取行情失败，稍后重试")
                # 重连尝试
                if not dry_run:
                    logger.info("尝试重连 MT5...")
                    client.connect(login=args.login, password=args.password, server=args.server, path=args.path)
                continue

            df_new.columns = [c.lower() if isinstance(c, str) else c for c in df_new.columns]
            latest_time = df_new.iloc[-1]["time"]
            latest_bar = df_new.iloc[-1]

            # 判断是否有新Bar（M5 收盘）
            is_new_bar = False
            if last_bar_time is None or latest_time != last_bar_time:
                is_new_bar = True
                last_bar_time = latest_time
                logger.info(f"📊 新Bar | {latest_time} O:{latest_bar['open']:.2f} H:{latest_bar['high']:.2f} L:{latest_bar['low']:.2f} C:{latest_bar['close']:.2f} Vol:{latest_bar.get('tick_volume', 0)}")

            # 仅在新Bar时驱动策略（避免一根Bar内重复触发）
            # 但为调试，dry_run 可每轮都驱动
            should_drive = is_new_bar or dry_run
            if not should_drive:
                continue

            # 构造 bar 字典传入策略
            bar_dict = {
                "time": latest_time.to_pydatetime() if hasattr(latest_time, "to_pydatetime") else latest_time,
                "open": float(latest_bar["open"]),
                "high": float(latest_bar["high"]),
                "low": float(latest_bar["low"]),
                "close": float(latest_bar["close"]),
                "volume": float(latest_bar.get("tick_volume", latest_bar.get("volume", 0))),
            }

            signal = strategy.on_bar(bar_dict)

            if signal:
                logger.info("="*70)
                logger.info(f"🎯 收到入场信号 | {signal['action']} {signal['direction']} | Entry={signal['entry']:.2f} SL={signal['sl']:.2f} TP={signal['tp']:.2f} RR=1:{signal['rr']:.2f}")
                logger.info(f"   触发时间: {signal['time']} | 窗口: {signal['window_top']:.2f} / {signal['window_bottom']:.2f}")
                logger.info("="*70)

                # 二次风控：如已有持仓，拒绝新信号
                if client.has_open_position() and not dry_run:
                    logger.warning("已有持仓，跳过新信号（单持仓风控）")
                    continue

                # 计算手数
                acc_for_lot = client.get_account_info()
                balance = acc_for_lot["balance"] if acc_for_lot else start_balance
                lots = client.calculate_lot_size(
                    entry_price=signal["entry"],
                    stop_loss=signal["sl"],
                    risk_percent=config.risk_percent,
                    balance=balance,
                )

                # 下单
                if signal["action"] == "BUY":
                    result = client.buy(volume=lots, sl=signal["sl"], tp=signal["tp"], comment=f"Sunrise LONG {signal['rr']:.1f}R")
                else:
                    result = client.sell(volume=lots, sl=signal["sl"], tp=signal["tp"], comment=f"Sunrise SHORT {signal['rr']:.1f}R")

                if result.success:
                    logger.info(f"✅ 下单成功 | Ticket={result.ticket} {signal['action']} {lots} lots @ {result.price:.2f} SL={signal['sl']:.2f} TP={signal['tp']:.2f}")
                else:
                    logger.error(f"❌ 下单失败 | {result.message} retcode={result.retcode}")
                    if not dry_run and result.retcode in [10027, 10030]:
                        logger.error("请检查：1) MT5 Algo Trading 是否开启 2) 品种是否允许交易 3) 账户是否为只读")
            else:
                # 无信号时，定期打印状态
                if iteration % 36 == 0:  # 约每6分钟
                    logger.info(f"状态: {strategy.entry_state} | 等待信号... 最新价 {bar_dict['close']:.2f} ATR {strategy.atr_series[-1]:.4f}")

            if args.once:
                logger.info("--once 模式，退出")
                break

            # dry_run 演示：跑完本地数据后退出
            if dry_run and is_new_bar:
                # 如果是 dry_run 且已遍历完所有本地 CSV 数据，可停止
                # 这里简单判断：如果最新K线时间超过昨天，则认为已到实时
                # 否则继续（本地CSV最后时间是2020-08等，会一直是“新Bar”，需限制）
                if iteration > 300:  # 防止无限循环
                    logger.info("DRY_RUN 演示结束（已处理300根模拟Bar）")
                    break

    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，正在优雅退出...")

    finally:
        logger.info("="*70)
        logger.info("策略停止，汇总：")
        positions = client.get_positions()
        if positions:
            logger.info(f"仍有 {len(positions)} 个持仓未平：")
            for p in positions:
                logger.info(f"  Ticket={p.ticket} {'BUY' if p.type==0 else 'SELL'} {p.volume} lots PnL={p.profit:.2f}")
            if not dry_run:
                try:
                    ans = input("是否全部平仓？(y/N): ").strip().lower()
                    if ans in ["y", "yes"]:
                        client.close_all_positions()
                        logger.info("已全部平仓")
                except:
                    pass
        else:
            logger.info("无持仓")

        acc_final = client.get_account_info()
        if acc_final:
            logger.info(f"最终余额: ${acc_final['balance']:.2f} 净值: ${acc_final['equity']:.2f}")

        client.disconnect()
        logger.info("已断开 MT5，退出")

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
