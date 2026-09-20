//+------------------------------------------------------------------+
//|                                              ComplexEA.mq5       |
//| Compile stress test: CTrade, CSymbolInfo, indicator handles,      |
//| OnTradeTransaction, arrays, templates, string ops.                |
//+------------------------------------------------------------------+
#property copyright "powerx test"
#property version   "1.10"
#property strict

#include <Trade/Trade.mqh>
#include <Trade/SymbolInfo.mqh>
#include <Trade/PositionInfo.mqh>
#include <Trade/OrderInfo.mqh>
#include <Trade/DealInfo.mqh>

input double InpLots        = 0.10;
input int    InpFastMA      = 9;
input int    InpSlowMA      = 21;
input int    InpRSIPeriod   = 14;
input int    InpATRPeriod   = 14;
input double InpRiskPct     = 1.0;
input int    InpMagic       = 20240920;

CTrade         trade;
CSymbolInfo    sym;
CPositionInfo  pos;
COrderInfo     ord;
CDealInfo      deal;

int hFast = INVALID_HANDLE;
int hSlow = INVALID_HANDLE;
int hRSI  = INVALID_HANDLE;
int hATR  = INVALID_HANDLE;

struct Signal
  {
   int      dir;      // +1 buy, -1 sell, 0 none
   double   sl;
   double   tp;
   string   reason;
  };

//+------------------------------------------------------------------+
int OnInit()
  {
   if(!sym.Name(_Symbol))
      return(INIT_FAILED);
   sym.RefreshRates();

   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(20);
   trade.SetTypeFillingBySymbol(_Symbol);
   trade.SetMarginMode();
   trade.LogLevel(LOG_LEVEL_ERRORS);

   hFast = iMA(_Symbol, PERIOD_CURRENT, InpFastMA, 0, MODE_EMA, PRICE_CLOSE);
   hSlow = iMA(_Symbol, PERIOD_CURRENT, InpSlowMA, 0, MODE_EMA, PRICE_CLOSE);
   hRSI  = iRSI(_Symbol, PERIOD_CURRENT, InpRSIPeriod, PRICE_CLOSE);
   hATR  = iATR(_Symbol, PERIOD_CURRENT, InpATRPeriod);

   if(hFast == INVALID_HANDLE || hSlow == INVALID_HANDLE ||
      hRSI == INVALID_HANDLE || hATR == INVALID_HANDLE)
     {
      Print("indicator handle creation failed: ", GetLastError());
      return(INIT_FAILED);
     }
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   if(hFast != INVALID_HANDLE) IndicatorRelease(hFast);
   if(hSlow != INVALID_HANDLE) IndicatorRelease(hSlow);
   if(hRSI  != INVALID_HANDLE) IndicatorRelease(hRSI);
   if(hATR  != INVALID_HANDLE) IndicatorRelease(hATR);
   Print("deinit reason=", reason);
  }

//+------------------------------------------------------------------+
bool ReadBuffer(const int handle, const int shift, double &out)
  {
   double buf[];
   if(CopyBuffer(handle, 0, shift, 1, buf) != 1)
      return(false);
   out = buf[0];
   return(true);
  }

//+------------------------------------------------------------------+
Signal Evaluate()
  {
   Signal s;
   s.dir    = 0;
   s.sl     = 0.0;
   s.tp     = 0.0;
   s.reason = "";

   double fast1, fast2, slow1, slow2, rsi, atr;
   if(!ReadBuffer(hFast, 1, fast1) || !ReadBuffer(hFast, 2, fast2)) return(s);
   if(!ReadBuffer(hSlow, 1, slow1) || !ReadBuffer(hSlow, 2, slow2)) return(s);
   if(!ReadBuffer(hRSI,  1, rsi))                                return(s);
   if(!ReadBuffer(hATR,  1, atr))                                return(s);

   double point = sym.Point();
   double slDist = atr * 1.5;
   double tpDist = atr * 3.0;

   bool crossUp   = (fast2 <= slow2 && fast1 > slow1);
   bool crossDown = (fast2 >= slow2 && fast1 < slow1);

   if(crossUp && rsi < 70.0)
     {
      s.dir    = 1;
      s.sl     = sym.Ask() - slDist;
      s.tp     = sym.Ask() + tpDist;
      s.reason = StringFormat("cross-up rsi=%.2f atr=%.5f point=%.5f", rsi, atr, point);
     }
   else if(crossDown && rsi > 30.0)
     {
      s.dir    = -1;
      s.sl     = sym.Bid() + slDist;
      s.tp     = sym.Bid() - tpDist;
      s.reason = StringFormat("cross-down rsi=%.2f atr=%.5f", rsi, atr);
     }
   return(s);
  }

//+------------------------------------------------------------------+
double NormalizeLots(const double raw)
  {
   double minLot  = sym.LotsMin();
   double maxLot  = sym.LotsMax();
   double stepLot = sym.LotsStep();
   double lots    = MathFloor(raw / stepLot) * stepLot;
   lots = MathMax(minLot, MathMin(maxLot, lots));
   return(NormalizeDouble(lots, 2));
  }

//+------------------------------------------------------------------+
int CountPositions()
  {
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      if(!pos.SelectByIndex(i)) continue;
      if(pos.Symbol() == _Symbol && pos.Magic() == InpMagic) n++;
     }
   return(n);
  }

//+------------------------------------------------------------------+
void OnTick()
  {
   static datetime lastBar = 0;
   datetime barTime = (datetime)SeriesInfoInteger(_Symbol, PERIOD_CURRENT, SERIES_LASTBAR_DATE);
   if(barTime == lastBar) return;
   lastBar = barTime;

   sym.RefreshRates();
   if(!sym.Refresh()) return;

   if(CountPositions() > 0)
      return;

   Signal s = Evaluate();
   if(s.dir == 0) return;

   double equity   = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskCash = equity * InpRiskPct / 100.0;
   double slPoints = MathAbs(sym.Ask() - s.sl) / sym.Point();
   if(slPoints <= 0.0) return;

   double tickValue = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double rawLots   = 0.0;
   if(tickValue > 0.0)
      rawLots = riskCash / (slPoints * tickValue);
   double lots = NormalizeLots(rawLots);

   bool ok = false;
   if(s.dir > 0)
      ok = trade.Buy(lots, _Symbol, 0.0, s.sl, s.tp, s.reason);
   else
      ok = trade.Sell(lots, _Symbol, 0.0, s.sl, s.tp, s.reason);

   if(!ok)
      PrintFormat("order failed retcode=%u desc=%s", trade.ResultRetcode(), trade.ResultRetcodeDescription());
   else
      PrintFormat("order ok deal=%I64u lots=%.2f %s", trade.ResultDeal(), lots, s.reason);
  }

//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
  {
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD) return;
   if(!deal.SelectByIndex(0)) return;
   PrintFormat("deal added: ticket=%I64u type=%d volume=%.2f price=%.5f",
               deal.Ticket(), (int)deal.DealType(), deal.Volume(), deal.Price());
  }
//+------------------------------------------------------------------+