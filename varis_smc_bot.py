import time
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Dict, Optional

# ==========================================
# 1. MT5 CONNECTOR CLASS WITH RISK MANAGEMENT
# ==========================================
class MT5Connector:
    def __init__(self, symbol: str = "EURAUD", risk_percent: float = 1.0):
        self.symbol = symbol
        self.risk_percent = risk_percent

    def initialize(self) -> bool:
        if not mt5.initialize():
            print(f"[{datetime.now()}] MT5 Initialization failed. Error: {mt5.last_error()}")
            return False
        
        if not mt5.symbol_select(self.symbol, True):
            print(f"[{datetime.now()}] Failed to select {self.symbol} in Market Watch.")
            return False
            
        print(f"[{datetime.now()}] Connected to MT5 Terminal for {self.symbol} | Risk: {self.risk_percent}% per trade")
        return True

    def get_rates(self, timeframe, count: int = 100) -> pd.DataFrame:
        rates = mt5.copy_rates_from_pos(self.symbol, timeframe, 0, count)
        if rates is None or len(rates) == 0:
            raise ValueError(f"Failed to fetch rates for timeframe {timeframe}")
        
        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        return df[['time', 'open', 'high', 'low', 'close', 'tick_volume']]

    def calculate_dynamic_lot_size(self, entry_price: float, sl_price: float) -> float:
        """
        Calculates exact lot size corresponding to 1% account risk based on SL distance.
        Normalized to broker's volume step, min volume, and max volume limits.
        """
        account_info = mt5.account_info()
        symbol_info = mt5.symbol_info(self.symbol)
        
        if account_info is None or symbol_info is None:
            print(f"[{datetime.now()}] Error fetching account/symbol info for lot calculation.")
            return 0.01

        # Account balance & monetary risk target
        balance = account_info.balance
        risk_amount = balance * (self.risk_percent / 100.0)

        # Calculate SL distance in price terms
        sl_distance = abs(entry_price - sl_price)
        if sl_distance == 0:
            return symbol_info.volume_min

        # Symbol specifications
        tick_size = symbol_info.trade_tick_size
        tick_value = symbol_info.trade_tick_value
        volume_step = symbol_info.volume_step
        min_vol = symbol_info.volume_min
        max_vol = symbol_info.volume_max

        if tick_size == 0 or tick_value == 0:
            return min_vol

        # Risk formula: Risk = Lots * (SL Distance / Tick Size) * Tick Value
        loss_per_lot = (sl_distance / tick_size) * tick_value
        if loss_per_lot == 0:
            return min_vol

        raw_lots = risk_amount / loss_per_lot

        # Round down to nearest volume step (e.g. 0.01)
        step_precision = len(str(volume_step).split('.')[1]) if '.' in str(volume_step) else 0
        calculated_lots = math.floor(raw_lots / volume_step) * volume_step
        calculated_lots = round(calculated_lots, step_precision)

        # Clamp between broker min and max
        final_lots = max(min_vol, min(max_vol, calculated_lots))
        
        print(f"[{datetime.now()}] Account Balance: ${balance:.2f} | Risk Target (1%): ${risk_amount:.2f} | Calculated Lots: {final_lots}")
        return final_lots

    def has_active_position_or_order(self, magic_number: int = 202609) -> bool:
        """
        Checks if there are open positions or active pending limit orders 
        belonging to this bot's magic number for the current symbol.
        """
        # 1. Check active pending orders
        orders = mt5.orders_get(symbol=self.symbol)
        if orders is not None:
            for order in orders:
                if order.magic == magic_number:
                    return True

        # 2. Check open positions
        positions = mt5.positions_get(symbol=self.symbol)
        if positions is not None:
            for position in positions:
                if position.magic == magic_number:
                    return True

        return False

    def cancel_order(self, ticket: int) -> bool:
        """Cancels a pending order by ticket ID."""
        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": ticket,
        }
        result = mt5.order_send(request)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"[{datetime.now()}] CANCELED PENDING ORDER #{ticket} due to structural invalidation.")
            return True
        else:
            print(f"[{datetime.now()}] Failed to cancel order #{ticket}. Retcode: {result.retcode}")
            return False

    def manage_pending_order_invalidation(self, current_price: float, magic_number: int = 202609):
        """
        Scans open pending orders. If market price breaches the Stop Loss / Protected Level 
        BEFORE hitting the Limit Order entry price, cancel the order immediately.
        """
        orders = mt5.orders_get(symbol=self.symbol)
        if orders is None or len(orders) == 0:
            return

        for order in orders:
            if order.magic == magic_number:
                # BUY LIMIT INVALIDATION: Current price drops BELOW Stop Loss (Protected Low)
                if order.type == mt5.ORDER_TYPE_BUY_LIMIT and current_price <= order.sl:
                    print(f"[{datetime.now()}] Invalidation triggered! Price ({current_price}) dropped below Protected Low ({order.sl}).")
                    self.cancel_order(order.ticket)

                # SELL LIMIT INVALIDATION: Current price rises ABOVE Stop Loss (Protected High)
                elif order.type == mt5.ORDER_TYPE_SELL_LIMIT and current_price >= order.sl:
                    print(f"[{datetime.now()}] Invalidation triggered! Price ({current_price}) rose above Protected High ({order.sl}).")
                    self.cancel_order(order.ticket)

    def place_limit_order(self, order_type: str, price: float, sl: float, tp: float):
        # Calculate dynamic volume before executing
        volume = self.calculate_dynamic_lot_size(entry_price=price, sl_price=sl)
        
        type_flag = mt5.ORDER_TYPE_BUY_LIMIT if order_type == "BUY_LIMIT" else mt5.ORDER_TYPE_SELL_LIMIT
        
        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": self.symbol,
            "volume": volume,
            "type": type_flag,
            "price": round(price, 5),
            "sl": round(sl, 5),
            "tp": round(tp, 5),
            "deviation": 10,
            "magic": 202609,
            "comment": f"Varis SMC Bot ({self.risk_percent}% Risk)",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            print(f"[{datetime.now()}] Order Placement Failed! Code: {result.retcode}, Comment: {result.comment}")
        else:
            print(f"[{datetime.now()}] PENDING ORDER PLACED! Ticket: {result.order} | Type: {order_type} | Volume: {volume} Lots | Entry: {price} | SL: {sl} | TP: {tp}")

    def shutdown(self):
        mt5.shutdown()

# Import math module required for floor calculation
import math

# ==========================================
# 2. VARIS SMC ENGINE CLASS
# ==========================================
class VarisSMCEngine:
    def __init__(self, atr_period: int = 14, displacement_mult: float = 1.5):
        self.atr_period = atr_period
        self.displacement_mult = displacement_mult

    def _calc_atr(self, df: pd.DataFrame) -> pd.Series:
        high_low = df['high'] - df['low']
        high_close = np.abs(df['high'] - df['close'].shift())
        low_close = np.abs(df['low'] - df['close'].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        return np.max(ranges, axis=1).rolling(self.atr_period).mean()

    def check_macro_context(self, df_daily: pd.DataFrame) -> Dict[str, bool]:
        """Layer 1: Daily timeframe / 60-90 day window filter."""
        df_90d = df_daily.tail(90).copy()
        df_90d['atr'] = self._calc_atr(df_90d)
        df_90d['body'] = np.abs(df_90d['close'] - df_90d['open'])
        df_90d['is_displacement'] = df_90d['body'] > (df_90d['atr'] * self.displacement_mult)

        latest_close = df_90d['close'].iloc[-1]
        demand_zones = df_90d[df_90d['is_displacement'] & (df_90d['close'] > df_90d['open'])]
        supply_zones = df_90d[df_90d['is_displacement'] & (df_90d['close'] < df_90d['open'])]

        at_demand = False
        at_supply = False

        if not demand_zones.empty:
            last_demand_low = demand_zones['low'].iloc[-1]
            last_demand_high = demand_zones['high'].iloc[-1]
            if last_demand_low <= latest_close <= (last_demand_high * 1.002):
                at_demand = True

        if not supply_zones.empty:
            last_supply_low = supply_zones['low'].iloc[-1]
            last_supply_high = supply_zones['high'].iloc[-1]
            if (last_supply_low * 0.998) <= latest_close <= last_supply_high:
                at_supply = True

        return {"at_demand": at_demand, "at_supply": at_supply}

    def check_reversal_csd(self, df_4h: pd.DataFrame, bias: str) -> Optional[Dict]:
        """Layer 2: 4H Liquidity Sweep & CSD confirmation."""
        df_4h = df_4h.copy()
        swing_lo = df_4h['low'].rolling(10).min().shift(1)
        swing_hi = df_4h['high'].rolling(10).max().shift(1)

        for i in range(len(df_4h) - 8, len(df_4h) - 1):
            row = df_4h.iloc[i]
            
            if bias == "BULLISH":
                if (row['low'] < swing_lo.iloc[i]) and (row['close'] > swing_lo.iloc[i]):
                    csd_target = row['high']
                    for j in range(i + 1, len(df_4h)):
                        if df_4h.iloc[j]['close'] > csd_target:
                            return {"status": "CONFIRMED", "protected_level": row['low'], "direction": "BUY"}

            elif bias == "BEARISH":
                if (row['high'] > swing_hi.iloc[i]) and (row['close'] < swing_hi.iloc[i]):
                    csd_target = row['low']
                    for j in range(i + 1, len(df_4h)):
                        if df_4h.iloc[j]['close'] < csd_target:
                            return {"status": "CONFIRMED", "protected_level": row['high'], "direction": "SELL"}

        return None

    def evaluate_execution(self, df_15m: pd.DataFrame, csd_info: Dict) -> Optional[Dict]:
        """Layer 3: 15m Inducement & Internal Range Liquidity targeting."""
        recent = df_15m.tail(15)
        direction = csd_info['direction']
        
        if direction == "BUY":
            entry = recent['low'].min()
            sl = csd_info['protected_level']
            tp = recent['high'].rolling(5).max().iloc[-1] # Internal Range Liquidity
            
            if entry > sl and tp > entry:
                return {"type": "BUY_LIMIT", "entry": entry, "sl": sl, "tp": tp}

        elif direction == "SELL":
            entry = recent['high'].max()
            sl = csd_info['protected_level']
            tp = recent['low'].rolling(5).min().iloc[-1] # Internal Range Liquidity
            
            if entry < sl and tp < entry:
                return {"type": "SELL_LIMIT", "entry": entry, "sl": sl, "tp": tp}

        return None

# ==========================================
# 3. MAIN RUNNABLE EXECUTION LOOP
# ==========================================
def main():
    SYMBOL = "EURAUD"
    RISK_PERCENT = 1.0  # 1% account risk per trade
    POLL_INTERVAL_SECONDS = 60
    MAGIC_NUMBER = 202609

    connector = MT5Connector(symbol=SYMBOL, risk_percent=RISK_PERCENT)
    engine = VarisSMCEngine()

    if not connector.initialize():
        return

    print(f"[{datetime.now()}] Starting Varis SMC Automated Live Scanner...")

    try:
        while True:
            # Fetch Multi-Timeframe Data
            df_daily = connector.get_rates(mt5.TIMEFRAME_D1, count=100)
            df_4h    = connector.get_rates(mt5.TIMEFRAME_H4, count=100)
            df_15m   = connector.get_rates(mt5.TIMEFRAME_M15, count=100)

            current_close = df_15m['close'].iloc[-1]

            # 1. First, check and manage existing pending orders for invalidation
            connector.manage_pending_order_invalidation(current_price=current_close, magic_number=MAGIC_NUMBER)

            # 2. Check Layer 1: Macro Context
            context = engine.check_macro_context(df_daily)
            
            bias = None
            if context['at_demand']:
                bias = "BULLISH"
            elif context['at_supply']:
                bias = "BEARISH"

            if bias:
                # Check Layer 2: Reversal & CSD
                csd_result = engine.check_reversal_csd(df_4h, bias)
                if csd_result:
                    
                    # Check Layer 3: Execution Setup
                    setup = engine.evaluate_execution(df_15m, csd_result)
                    if setup:
                        # DEDUPLICATION GUARD: Only place if no active position or pending order exists
                        if not connector.has_active_position_or_order(magic_number=MAGIC_NUMBER):
                            connector.place_limit_order(
                                order_type=setup['type'],
                                price=setup['entry'],
                                sl=setup['sl'],
                                tp=setup['tp']
                            )
                        else:
                            print(f"[{datetime.now()}] Setup active, but an order/position is already open. Skipping duplicate.")
            
            time.sleep(POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print(f"\n[{datetime.now()}] Bot stopped manually.")
    finally:
        connector.shutdown()

if __name__ == "__main__":
    main()
