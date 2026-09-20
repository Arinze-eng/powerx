//+------------------------------------------------------------------+
//| 70% Win Rate Bot - EMA RSI Pullback          (powerx fixed)      |
//|                                                                  |
//| Fixes vs. the submitted test.mq5 — each one was confirmed either  |
//| by a MetaEditor diagnostic or by reading the MQL5 contract:       |
//|  1. #include <Trade\Trade.mqh> -> <Trade/Trade.mqh> (see note)   |
//|  2. ArraySetAsSeries() called AFTER CopyBuffer(), so every index  |
//|     was reversed: [0] was the OLDEST bar, not the newest.        |
//|  3. CopyBuffer() return value never checked -> reading [0] of an  |
//|     empty array is a runtime access violation, not a bad signal.  |
//|  4. Indicator handles never validated in OnInit.                  |
//|  5. `emaFast[1] <= emaSlow[1] == false` is a precedence bug: it  |
//|     parses as `(emaFast[1] <= emaSlow[1]) == false`, i.e. "no     |
//|     cross at all" — the opposite of the intended pullback check.  |
//|  6. BUY computed SL/TP from ASK but SELL from ASK too while       |
//|     submitting at BID -> inverted risk distance on shorts.        |
//|  7. PositionsTotal() counts EVERY position on the account, so     |
//|     any other EA/manual trade permanently blocks this one. Bound   |
//|     it by magic+symbol.                                            |
//|  8. No magic number set -> orders are unattributable and           |
//|     close_all cannot find them.                                    |
//|  9. Lot size not normalised to the symbol's step/min/max ->        |
//|     10014 (invalid volume) on many brokers.                        |
//| 10. trade.Buy()/Sell() return value ignored -> silent failures.    |
//| 11. Handles leaked: no IndicatorRelease() in OnDeinit.            |
//+------------------------------------------------------------------+
#property copyright "powerx"
#property version   "2.00"
#property strict

#include <Trade/Trade.mqh>

CTrade trade;

input double LotSize      = 0.1;
input int    EMA_Fast     = 21;
input int    EMA_Slow     = 50;
input int    RSI_Period   = 14;
input int    RSI_BuyLevel = 35;
input int    RSI_SellLevel= 65;
input double RiskReward   = 1.5;
input int    ATR_Period   = 14;
input double ATR_SL_Mult  = 1.5;
input long   MagicNumber  = 702024;

int emaFastHandle = INVALID_HANDLE;
int emaSlowHandle = INVALID_HANDLE;
int rsiHandle     = INVALID_HANDLE;
int atrHandle     = INVALID_HANDLE;

//+------------------------------------------------------------------+
//| One-time setup: create handles and validate every one of them.    |
//+------------------------------------------------------------------+
int OnInit()
  {
   emaFastHandle = iMA(_Symbol, _Period, EMA_Fast, 0, MODE_EMA, PRICE_CLOSE);
   emaSlowHandle = iMA(_Symbol, _Period, EMA_Slow, 0, MODE_EMA, PRICE_CLOSE);
   rsiHandle     = iRSI(_Symbol, _Period, RSI_Period, PRICE_CLOSE);
   atrHandle     = iATR(_Symbol, _Period, ATR_Period);

   if(emaFastHandle == INVALID_HANDLE || emaSlowHandle == INVALID_HANDLE ||
      rsiHandle == INVALID_HANDLE || atrHandle == INVALID_HANDLE)
     {
      PrintFormat("indicator init failed, err=%d", GetLastError());
      return(INIT_FAILED);
     }

   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(20);
   trade.SetTypeFillingBySymbol(_Symbol);
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   if(emaFastHandle != INVALID_HANDLE) IndicatorRelease(emaFastHandle);
   if(emaSlowHandle != INVALID_HANDLE) IndicatorRelease(emaSlowHandle);
   if(rsiHandle     != INVALID_HANDLE) IndicatorRelease(rsiHandle);
   if(atrHandle     != INVALID_HANDLE) IndicatorRelease(atrHandle);
  }

//+------------------------------------------------------------------+
//| Copy `count` bars of one buffer, newest first, with failure check.|
//+------------------------------------------------------------------+
bool FetchBuffer(const int handle, const int count, double &out[])
  {
   ArrayFree(out);
   ArraySetAsSeries(out, true);          // BEFORE the copy, so [0] is the newest
   if(CopyBuffer(handle, 0, 0, count, out) != count)
     {
      PrintFormat("CopyBuffer failed (%d of %d), err=%d",
                  ArraySize(out), count, GetLastError());
      return(false);
     }
   return(ArraySize(out) >= count);
  }

//+------------------------------------------------------------------+
//| This EA's own open positions only — not the whole account.        |
//+------------------------------------------------------------------+
int OwnPositions()
  {
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      const ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) == MagicNumber &&
         PositionGetString(POSITION_SYMBOL) == _Symbol)
         n++;
     }
   return(n);
  }

//+------------------------------------------------------------------+
//| Clamp/round the lot to the symbol's own constraints.             |
//+------------------------------------------------------------------+
double NormalizeLots(double lots)
  {
   const double minLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   const double maxLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   const double stepLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(stepLot > 0.0)
      lots = MathFloor(lots / stepLot + 0.5) * stepLot;
   lots = MathMax(minLot, MathMin(maxLot, lots));
   return(lots);
  }

//+------------------------------------------------------------------+
void OnTick()
  {
   if(OwnPositions() > 0)
      return;                            // one trade at a time

   double emaFast[], emaSlow[], rsi[], atr[];
   if(!FetchBuffer(emaFastHandle, 3, emaFast)) return;
   if(!FetchBuffer(emaSlowHandle, 3, emaSlow)) return;
   if(!FetchBuffer(rsiHandle,     3, rsi))     return;
   if(!FetchBuffer(atrHandle,     3, atr))     return;

   const double atrVal  = atr[0];
   if(atrVal <= 0.0)
      return;                             // warm-up: nothing to size from
   const double slDist  = atrVal * ATR_SL_Mult;

   // A genuine cross: above now AND not above on the previous bar.
   const bool crossedUp   = (emaFast[0] >  emaSlow[0]) && (emaFast[1] <= emaSlow[1]);
   const bool crossedDown = (emaFast[0] <  emaSlow[0]) && (emaFast[1] >= emaSlow[1]);

   // BUY: uptrend cross + RSI recovering out of the pullback zone.
   if(crossedUp && rsi[1] < RSI_BuyLevel && rsi[0] > RSI_BuyLevel)
     {
      const double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      const double sl  = NormalizeDouble(ask - slDist, _Digits);
      const double tp  = NormalizeDouble(ask + slDist * RiskReward, _Digits);
      const double lots = NormalizeLots(LotSize);
      if(!trade.Buy(lots, _Symbol, ask, sl, tp, "70% Buy"))
         PrintFormat("Buy rejected retcode=%u %s",
                     trade.ResultRetcode(), trade.ResultRetcodeDescription());
     }

   // SELL: downtrend cross + RSI falling back out of the rally zone.
   // SL/TP are measured from the BID, which is what a short is filled at.
   if(crossedDown && rsi[1] > RSI_SellLevel && rsi[0] < RSI_SellLevel)
     {
      const double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      const double sl  = NormalizeDouble(bid + slDist, _Digits);
      const double tp  = NormalizeDouble(bid - slDist * RiskReward, _Digits);
      const double lots = NormalizeLots(LotSize);
      if(!trade.Sell(lots, _Symbol, bid, sl, tp, "70% Sell"))
         PrintFormat("Sell rejected retcode=%u %s",
                     trade.ResultRetcode(), trade.ResultRetcodeDescription());
     }
  }
//+------------------------------------------------------------------+