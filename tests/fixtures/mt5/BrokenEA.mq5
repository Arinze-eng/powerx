//+------------------------------------------------------------------+
//| BrokenEA.mq5 — deliberately broken, verifies error reporting      |
//+------------------------------------------------------------------+
#property strict
#include <Trade/Trade.mqh>

CTrade trade;

void OnTick()
  {
   // error: undeclared identifier
   double x = undefinedVariable + 1;

   // error: missing semicolon
   int y = 5

   // error: wrong argument count
   trade.Buy();

   Print(x, y);
  }