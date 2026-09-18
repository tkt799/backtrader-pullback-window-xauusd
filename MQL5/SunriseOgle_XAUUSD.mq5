//+------------------------------------------------------------------+
//|                                                SunriseOgle_XAUUSD.mq5 |
//|  Sunrise Ogle XAUUSD — MT5 原生 EA（与 Python/Backtrader 逻辑 1:1） |
//|  推荐：追求最低延迟 / 长期挂机 / VPS 部署 时使用此 EA                 |
//|  Python 桥接版见 src/mt5/run_live.py，二选一即可                     |
//+------------------------------------------------------------------+
//| 使用方法：
//|  1. 将此文件复制到 MT5 数据目录/MQL5/Experts/ 下
//|  2. 在 MT5 中按 F4 打开 MetaEditor，编译（Compile）
//|  3. 重启 MT5，在导航 -> EA交易 中找到 SunriseOgle_XAUUSD，拖到 XAUUSD M5 图表
//|  4. 勾选“允许自动交易”，配置参数后点击确定
//|  5. 图表右上角笑脸表示运行正常
//+------------------------------------------------------------------+
#property copyright "Sunrise Ogle XAUUSD — Ported from Backtrader"
#property link      "https://github.com/tkt799/backtrader-pullback-window-xauusd"
#property version   "1.00"
#property strict

#include <Trade/Trade.mqh>

//--- 输入参数（与 Backtrader 顶部 CONFIG 一一对应） ---
input string InpInfo1 = "===== EMA 设置 ====="; // 分组标题
input int    InpEMA_Fast       = 14;     // 快线周期
input int    InpEMA_Medium     = 14;     // 中线周期
input int    InpEMA_Slow       = 24;     // 慢线周期
input int    InpEMA_Confirm    = 1;      // 确认线周期（通常1）
input int    InpEMA_Filter     = 100;    // 过滤线周期
input int    InpATR_Period     = 10;     // ATR 周期

input string InpInfo2 = "===== 交易方向 ====="; // 分组标题
input bool   InpEnableLong     = true;   // 允许做多
input bool   InpEnableShort    = false;  // 允许做空（默认关闭，与回测一致）

input string InpInfo3 = "===== LONG 过滤 ====="; // 分组标题
input bool   InpLong_UseATRFilter = true;
input double InpLong_ATR_Min   = 0.0;
input double InpLong_ATR_Max   = 2.00;
input bool   InpLong_UseCandleFilter = false;
input bool   InpLong_UseEMAOrder = false;
input bool   InpLong_UsePriceFilter = true;
input bool   InpLong_UseAngleFilter = false;
input double InpLong_MinAngle  = 35.0;
input double InpLong_MaxAngle  = 95.0;
input double InpLong_AngleScale = 10.0;
input double InpLong_SL_ATR    = 4.5;    // 止损 ATR 倍数
input double InpLong_TP_ATR    = 6.5;    // 止盈 ATR 倍数

input string InpInfo4 = "===== SHORT 过滤 ====="; // 分组标题
input bool   InpShort_UseATRFilter = true;
input double InpShort_ATR_Min  = 0.0004;
input double InpShort_ATR_Max  = 0.00075;
input bool   InpShort_UseCandleFilter = true;
input bool   InpShort_UseEMAOrder = true;
input bool   InpShort_UsePriceFilter = true;
input bool   InpShort_UseAngleFilter = true;
input double InpShort_MinAngle = -90.0;
input double InpShort_MaxAngle = -20.0;
input double InpShort_AngleScale = 10.0;
input double InpShort_SL_ATR   = 2.5;
input double InpShort_TP_ATR   = 6.5;

input string InpInfo5 = "===== 回撤窗口系统 ====="; // 分组标题
input int    InpLong_PullbackMax = 3;    // LONG回撤K线数
input int    InpLong_WindowPeriods = 1;  // LONG窗口期
input int    InpShort_PullbackMax = 2;   // SHORT回撤K线数
input int    InpShort_WindowPeriods = 7; // SHORT窗口期
input bool   InpUseWindowTimeOffset = false;
input double InpWindowOffsetMultiplier = 1.0;
input double InpWindowPriceOffsetMultiplier = 0.001; // 通道扩张

input string InpInfo6 = "===== 风控 ====="; // 分组标题
input double InpRiskPercent   = 0.01;    // 每笔风险 1%
input double InpFixedLots     = 0.0;     // 固定手数（0=按风险自动计算）
input int    InpMagicNumber   = 20250918;
input int    InpDeviation     = 20;      // 滑点容忍 points

input string InpInfo7 = "===== 时间过滤 ====="; // 分组标题
input bool   InpUseTimeFilter = false;
input int    InpStartHour     = 0;
input int    InpStartMinute   = 0;
input int    InpEndHour       = 8;
input int    InpEndMinute     = 0;

input string InpInfo8 = "===== 调试 ====="; // 分组标题
input bool   InpPrintSignals  = true;    // 打印信号

//--- 全局变量 ---
CTrade   trade;
int      handleEMA_Fast, handleEMA_Medium, handleEMA_Slow, handleEMA_Confirm, handleEMA_Filter, handleATR;
double   bufEMA_Fast[], bufEMA_Medium[], bufEMA_Slow[], bufEMA_Confirm[], bufEMA_Filter[], bufATR[];
// 状态机
enum ENTRY_STATE { SCANNING, ARMED_LONG, ARMED_SHORT, WINDOW_OPEN };
ENTRY_STATE g_state = SCANNING;
string   g_armedDir = "";
int      g_pullbackCount = 0;
double   g_lastPullbackHigh = 0, g_lastPullbackLow = 0;
double   g_windowTop = 0, g_windowBottom = 0;
int      g_windowBarStart = 0, g_windowExpiryBar = 0;
double   g_signalDetectionATR = 0;
int      g_signalDetectionBar = 0;
datetime g_lastBarTime = 0;
int      g_barIndex = 0;

//+------------------------------------------------------------------+
//| 初始化                                                           |
//+------------------------------------------------------------------+
int OnInit()
  {
   trade.SetExpertMagicNumber(InpMagicNumber);
   trade.SetDeviationInPoints(InpDeviation);
   trade.SetTypeFilling(ORDER_FILLING_IOC);

   // 创建指标句柄
   handleEMA_Fast    = iMA(_Symbol, PERIOD_M5, InpEMA_Fast,    0, MODE_EMA, PRICE_CLOSE);
   handleEMA_Medium  = iMA(_Symbol, PERIOD_M5, InpEMA_Medium,  0, MODE_EMA, PRICE_CLOSE);
   handleEMA_Slow    = iMA(_Symbol, PERIOD_M5, InpEMA_Slow,    0, MODE_EMA, PRICE_CLOSE);
   handleEMA_Confirm = iMA(_Symbol, PERIOD_M5, InpEMA_Confirm, 0, MODE_EMA, PRICE_CLOSE);
   handleEMA_Filter  = iMA(_Symbol, PERIOD_M5, InpEMA_Filter,  0, MODE_EMA, PRICE_CLOSE);
   handleATR         = iATR(_Symbol, PERIOD_M5, InpATR_Period);

   if(handleEMA_Fast==INVALID_HANDLE || handleATR==INVALID_HANDLE)
     {
      Print("❌ 指标创建失败");
      return INIT_FAILED;
     }

   ArraySetAsSeries(bufEMA_Fast, true);
   ArraySetAsSeries(bufEMA_Medium, true);
   ArraySetAsSeries(bufEMA_Slow, true);
   ArraySetAsSeries(bufEMA_Confirm, true);
   ArraySetAsSeries(bufEMA_Filter, true);
   ArraySetAsSeries(bufATR, true);

   Print("✅ SunriseOgle EA 初始化完成 | ", _Symbol, " M5 | Magic=", InpMagicNumber,
         " LONG=", InpEnableLong, " SHORT=", InpEnableShort);

   // 预热：等待足够历史
   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
//| 释放                                                             |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   IndicatorRelease(handleEMA_Fast);
   IndicatorRelease(handleEMA_Medium);
   IndicatorRelease(handleEMA_Slow);
   IndicatorRelease(handleEMA_Confirm);
   IndicatorRelease(handleEMA_Filter);
   IndicatorRelease(handleATR);
   Print("EA 已卸载, 原因=", reason);
  }

//+------------------------------------------------------------------+
//| 工具函数                                                         |
//+------------------------------------------------------------------+
bool CopyEnough(int handle, double &buf[], int need=5)
  {
   if(CopyBuffer(handle, 0, 0, need, buf) < need) return false;
   return true;
  }

double EMAAngle(double cur, double prev, double scale)
  {
   double rise = (cur - prev) * scale;
   return MathArctan(rise) * 180.0 / M_PI;
  }

bool CrossAbove(double a0, double a1, double b0, double b1) { return (a0 > b0 && a1 <= b1); }
bool CrossBelow(double a0, double a1, double b0, double b1) { return (a0 < b0 && a1 >= b1); }

bool IsInTimeRange(datetime t)
  {
   if(!InpUseTimeFilter) return true;
   MqlDateTime dt; TimeToStruct(t, dt);
   int cur = dt.hour * 60 + dt.min;
   int start = InpStartHour * 60 + InpStartMinute;
   int end_ = InpEndHour * 60 + InpEndMinute;
   if(start <= end_) return (cur >= start && cur <= end_);
   return (cur >= start || cur <= end_);
  }

double CalcLots(double entry, double sl)
  {
   if(InpFixedLots > 0) return InpFixedLots;
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double riskAmt = balance * InpRiskPercent;
   double dist = MathAbs(entry - sl);
   if(dist <= 0) return SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double contract = SymbolInfoDouble(_Symbol, SYMBOL_CONTRACT_SIZE);
   if(contract <= 0) contract = 100;
   double lots = riskAmt / (dist * contract);
   double volMin = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double volMax = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double volStep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   lots = MathMax(volMin, MathMin(volMax, lots));
   lots = MathRound(lots / volStep) * volStep;
   lots = NormalizeDouble(lots, 2);
   return lots;
  }

void ResetState()
  {
   g_state = SCANNING;
   g_armedDir = "";
   g_pullbackCount = 0;
   g_lastPullbackHigh = 0; g_lastPullbackLow = 0;
   g_windowTop = 0; g_windowBottom = 0;
   g_windowBarStart = 0; g_windowExpiryBar = 0;
  }

//+------------------------------------------------------------------+
//| 主逻辑：每 tick 调用，但仅在新 Bar 收盘时判断                     |
//+------------------------------------------------------------------+
void OnTick()
  {
   // 仅在新 Bar 时执行
   datetime curBarTime = iTime(_Symbol, PERIOD_M5, 0);
   if(curBarTime == g_lastBarTime) return;
   // 等待 Bar 收盘：当前 Bar 是 0（未收盘），我们用 1（已收盘）的数据做判断
   // 为避免过早，这里要求 0 号 Bar 已经过至少 10 秒
   // 更严格：等待下一根 Bar 出现才用上一根收盘价判断（已满足）

   // 确保有足够数据
   if(!CopyEnough(handleEMA_Fast, bufEMA_Fast, 3)) return;
   if(!CopyEnough(handleEMA_Medium, bufEMA_Medium, 3)) return;
   if(!CopyEnough(handleEMA_Slow, bufEMA_Slow, 3)) return;
   if(!CopyEnough(handleEMA_Confirm, bufEMA_Confirm, 3)) return;
   if(!CopyEnough(handleEMA_Filter, bufEMA_Filter, 3)) return;
   if(!CopyEnough(handleATR, bufATR, 3)) return;

   // 检查指标有效性
   if(bufEMA_Filter[1]==EMPTY_VALUE || bufATR[1]==EMPTY_VALUE) return;

   g_barIndex++;
   g_lastBarTime = curBarTime;

   // 有持仓则跳过开仓逻辑（单持仓限制）
   if(PositionSelect(_Symbol))
     {
      // 可选：仅在持仓时更新图表注释
      return;
     }

   // 取收盘 Bar（1号）数据
   double close1 = iClose(_Symbol, PERIOD_M5, 1);
   double open1  = iOpen(_Symbol, PERIOD_M5, 1);
   double high1  = iHigh(_Symbol, PERIOD_M5, 1);
   double low1   = iLow(_Symbol, PERIOD_M5, 1);
   double close2 = iClose(_Symbol, PERIOD_M5, 2);
   double open2  = iOpen(_Symbol, PERIOD_M5, 2);

   double emaC0 = bufEMA_Confirm[1], emaC1 = bufEMA_Confirm[2];
   double emaF0 = bufEMA_Fast[1],    emaF1 = bufEMA_Fast[2];
   double emaM0 = bufEMA_Medium[1],  emaM1 = bufEMA_Medium[2];
   double emaS0 = bufEMA_Slow[1],    emaS1 = bufEMA_Slow[2];
   double emaFlt0 = bufEMA_Filter[1];
   double atr0 = bufATR[1];

   bool prevBull = close2 > open2;
   bool prevBear = close2 < open2;

   // --- 全局失效：ARMED 状态下反向信号出现则重置 ---
   if(g_state==ARMED_LONG || g_state==ARMED_SHORT)
     {
      bool oppSignal=false;
      if(g_state==ARMED_LONG)
        {
         bool caF = CrossBelow(emaC0, emaC1, emaF0, emaF1);
         bool caM = CrossBelow(emaC0, emaC1, emaM0, emaM1);
         bool caS = CrossBelow(emaC0, emaC1, emaS0, emaS1);
         if(prevBear && (caF||caM||caS)) oppSignal=true;
        }
      else if(g_state==ARMED_SHORT)
        {
         bool caF = CrossAbove(emaC0, emaC1, emaF0, emaF1);
         bool caM = CrossAbove(emaC0, emaC1, emaM0, emaM1);
         bool caS = CrossAbove(emaC0, emaC1, emaS0, emaS1);
         if(prevBull && (caF||caM||caS)) oppSignal=true;
        }
      if(oppSignal)
        {
         if(InpPrintSignals) Print("🔄 全局失效，反向信号重置 ", g_state);
         ResetState();
        }
     }

   // --- 状态机 ---
   if(g_state==SCANNING)
     {
      string sig = "";
      // LONG 扫描
      if(InpEnableLong)
        {
         bool crossF = CrossAbove(emaC0, emaC1, emaF0, emaF1);
         bool crossM = CrossAbove(emaC0, emaC1, emaM0, emaM1);
         bool crossS = CrossAbove(emaC0, emaC1, emaS0, emaS1);
         bool candleOk = InpLong_UseCandleFilter ? prevBull : true;
         if(candleOk && (crossF||crossM||crossS))
           {
            bool valid=true;
            if(InpLong_UseEMAOrder && !(emaC0 > emaF0 && emaC0 > emaM0 && emaC0 > emaS0)) valid=false;
            if(valid && InpLong_UsePriceFilter && !(close1 > emaFlt0)) valid=false;
            if(valid && InpLong_UseAngleFilter)
              {
               double ang = EMAAngle(emaC0, emaC1, InpLong_AngleScale);
               if(!(ang >= InpLong_MinAngle && ang <= InpLong_MaxAngle)) valid=false;
              }
            if(valid && InpLong_UseATRFilter && !(atr0 >= InpLong_ATR_Min && atr0 <= InpLong_ATR_Max)) valid=false;
            if(valid) sig="LONG";
           }
        }
      // SHORT 扫描（仅当 LONG 未命中时）
      if(sig=="" && InpEnableShort)
        {
         bool crossF = CrossBelow(emaC0, emaC1, emaF0, emaF1);
         bool crossM = CrossBelow(emaC0, emaC1, emaM0, emaM1);
         bool crossS = CrossBelow(emaC0, emaC1, emaS0, emaS1);
         bool candleOk = InpShort_UseCandleFilter ? prevBear : true;
         if(candleOk && (crossF||crossM||crossS))
           {
            bool valid=true;
            if(InpShort_UseEMAOrder && !(emaC0 < emaF0 && emaC0 < emaM0 && emaC0 < emaS0)) valid=false;
            if(valid && InpShort_UsePriceFilter && !(close1 < emaFlt0)) valid=false;
            if(valid && InpShort_UseAngleFilter)
              {
               double ang = EMAAngle(emaC0, emaC1, InpShort_AngleScale);
               if(!(ang >= InpShort_MinAngle && ang <= InpShort_MaxAngle)) valid=false;
              }
            if(valid && InpShort_UseATRFilter && !(atr0 >= InpShort_ATR_Min && atr0 <= InpShort_ATR_Max)) valid=false;
            if(valid) sig="SHORT";
           }
        }

      if(sig!="")
        {
         g_state = (sig=="LONG" ? ARMED_LONG : ARMED_SHORT);
         g_armedDir = sig;
         g_pullbackCount=0;
         g_signalDetectionATR = atr0;
         g_signalDetectionBar = g_barIndex;
         if(InpPrintSignals) Print("📡 SCANNING -> ARMED_", sig, " | close2=", close2, " open2=", open2, " ATR=", atr0);
        }
     }
   else if(g_state==ARMED_LONG || g_state==ARMED_SHORT)
     {
      // 回撤确认
      bool isPullback = (g_armedDir=="LONG") ? (close1 < open1) : (close1 > open1);
      if(isPullback)
        {
         g_pullbackCount++;
         int need = (g_armedDir=="LONG" ? InpLong_PullbackMax : InpShort_PullbackMax);
         if(g_pullbackCount >= need)
           {
            g_lastPullbackHigh = high1;
            g_lastPullbackLow  = low1;
            // 开窗
            int curBar = g_barIndex;
            int winStart = curBar;
            if(InpUseWindowTimeOffset)
               winStart = curBar + (int)(g_pullbackCount * InpWindowOffsetMultiplier);
            g_windowBarStart = winStart;
            int winPeriods = (g_armedDir=="LONG" ? InpLong_WindowPeriods : InpShort_WindowPeriods);
            g_windowExpiryBar = winStart + winPeriods;
            double range = g_lastPullbackHigh - g_lastPullbackLow;
            double offset = range * InpWindowPriceOffsetMultiplier;
            g_windowTop = g_lastPullbackHigh + offset;
            g_windowBottom = g_lastPullbackLow - offset;
            g_state = WINDOW_OPEN;
            if(InpPrintSignals) Print("🚪 窗口已开 ", g_armedDir, " ", g_windowBarStart, "~", g_windowExpiryBar,
                                      " Top=", g_windowTop, " Bottom=", g_windowBottom);
           }
        }
      else
        {
         if(InpPrintSignals) Print("⚠️ 回撤被破坏，重置");
         ResetState();
        }
     }
   else if(g_state==WINDOW_OPEN)
     {
      if(g_barIndex < g_windowBarStart) return; // 尚未到开窗时间
      if(g_barIndex > g_windowExpiryBar)
        {
         if(InpPrintSignals) Print("⏰ 窗口超时，回到 ARMED_", g_armedDir);
         g_state = (g_armedDir=="LONG" ? ARMED_LONG : ARMED_SHORT);
         g_pullbackCount=0;
         g_windowTop=0; g_windowBottom=0; g_windowExpiryBar=0; g_windowBarStart=0;
         return;
        }

      bool success=false, failure=false;
      if(g_armedDir=="LONG")
        {
         if(high1 >= g_windowTop) success=true;
         else if(low1 <= g_windowBottom) failure=true;
        }
      else // SHORT
        {
         if(low1 <= g_windowBottom) success=true;
         else if(high1 >= g_windowTop) failure=true;
        }

      if(failure)
        {
         if(InpPrintSignals) Print("💥 失败边界被破，回到 ARMED_", g_armedDir);
         g_state = (g_armedDir=="LONG" ? ARMED_LONG : ARMED_SHORT);
         g_pullbackCount=0;
         g_windowTop=0; g_windowBottom=0; g_windowExpiryBar=0; g_windowBarStart=0;
         return;
        }

      if(success)
        {
         // 时间过滤最终校验
         datetime barTime = iTime(_Symbol, PERIOD_M5, 1);
         if(!IsInTimeRange(barTime))
           {
            if(InpPrintSignals) Print("❌ 时间过滤拦截");
            ResetState();
            g_signalDetectionATR=0; g_signalDetectionBar=0;
            return;
           }

         double entry = close1;
         double atr = atr0;
         if(atr<=0) { ResetState(); return; }

         double sl, tp;
         string action;
         if(g_armedDir=="LONG")
           {
            sl = low1 - atr * InpLong_SL_ATR;
            tp = high1 + atr * InpLong_TP_ATR;
            action="BUY";
           }
         else
           {
            sl = high1 + atr * InpShort_SL_ATR;
            tp = low1 - atr * InpShort_TP_ATR;
            action="SELL";
           }

         double lots = CalcLots(entry, sl);
         double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
         double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
         double price = (action=="BUY" ? ask : bid);

         if(InpPrintSignals) Print("🎯 突破成功 ", action, " ", g_armedDir, " Entry=", entry, " SL=", sl, " TP=", tp, " Lots=", lots);

         bool ok=false;
         if(action=="BUY")
            ok = trade.Buy(lots, _Symbol, price, sl, tp, "Sunrise LONG");
         else
            ok = trade.Sell(lots, _Symbol, price, sl, tp, "Sunrise SHORT");

         if(ok) Print("✅ 下单成功 ", action, " ", lots, " lots");
         else   Print("❌ 下单失败 ", GetLastError(), " ", ErrorDescription(GetLastError()));

         ResetState();
         g_signalDetectionATR=0; g_signalDetectionBar=0;
        }
     }

   // 图表注释
   string stateStr = (g_state==SCANNING ? "SCANNING" : g_state==ARMED_LONG ? "ARMED_LONG" : g_state==ARMED_SHORT ? "ARMED_SHORT" : "WINDOW_OPEN");
   string comment = StringFormat("SunriseOgle XAUUSD M5\n状态: %s | 方向: %s\nBar: %d | 窗口: %.2f / %.2f\nATR: %.4f",
                                 stateStr, g_armedDir, g_barIndex, g_windowTop, g_windowBottom, bufATR[1]);
   Comment(comment);
  }
//+------------------------------------------------------------------+
