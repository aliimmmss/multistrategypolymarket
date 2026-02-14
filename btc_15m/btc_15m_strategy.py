import os
import time
import json
import math
import requests
import sys
import re
import json
import os
from datetime import datetime, timedelta, timezone
from strategy_utils import Indicators, BayesianPredictor

# State File
STATE_FILE = "bot_state.json"

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=4)
        
def update_position_state(token_id, entry_price, size, side="BUY"):
    state = load_state()
    if side == "BUY":
        # Overwrite or Average? For MVP, overwrite (assuming single active trade per 15m)
        state["current_position"] = {
            "token_id": token_id,
            "entry_price": float(entry_price),
            "size": float(size),
            "timestamp": datetime.now().isoformat()
        }
    else: # SELL (Clear)
        if "current_position" in state:
            del state["current_position"]
    save_state(state)

# --- CLOB Client Setup ---
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
except ImportError:
    print("❌ py-clob-client not installed.")
    sys.exit(1)

# --- Configuration ---
BINANCE_API_URL = "https://api.binance.com/api/v3/klines"
POLY_MARKET_URL = "https://clob.polymarket.com"
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
# Load keys from Env or file (similar to other scripts)
# Load keys from Env
PRIVATE_KEY = os.getenv("POLYGON_PRIVATE_KEY")
FUNDER_ADDRESS = os.getenv("PROXY_WALLET_ADDRESS") 

if not PRIVATE_KEY:
    print("❌ POLYGON_PRIVATE_KEY not found in .env")
    # Fallback removed for security
    pass

# Strategy Params
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
VWAP_RESET_HOUR = 0 # UTC
CANDLE_LIMIT = 200 # For accurate EMA/RSI

# Indicators moved to strategy_utils.py

# --- Data Fetching ---
def fetch_binance_candles(symbol="BTCUSDT", interval="15m", limit=200):
    try:
        url = f"{BINANCE_API_URL}?symbol={symbol}&interval={interval}&limit={limit}"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"⚠️ Binance Error: {e}")
        return []

# score_direction moved to strategy_utils.py

# --- Execution Logic ---
GAMMA_API_URL = "https://gamma-api.polymarket.com/events"

def get_btc_markets(limit=100):
    try:
        # Filter for markets expiring in the future (removes 2025 zombies)
        now_utc = datetime.now(timezone.utc)
        
        resp = requests.get(GAMMA_API_URL, params={
            "limit": 500, "active": "true", "archived": "false", "closed": "false",
            "order": "endDate", "ascending": "true", # Soonest Expiry
            "end_date_min": now_utc.isoformat()
        })
        resp.raise_for_status()
        events = resp.json()
    except Exception as e:
        print(f"Error fetching markets: {e}")
        return []

    targets = []
    
    for event in events:
        markets = event.get("markets", [])
        for market in markets:
            question = market.get("question", "")
            
            if "Bitcoin" not in question and "BTC" not in question: continue
            
            # Parse for "Up or Down" (Flash) ONLY
            # Format: "Bitcoin Up or Down - January 30, 3PM ET"
            if "Up or Down" in question:
                # Token IDs
                raw_tokens = market.get("clobTokenIds")
                if not raw_tokens: continue
                if isinstance(raw_tokens, str): tokens = json.loads(raw_tokens)
                else: tokens = raw_tokens
                if len(tokens) != 2: continue
                
                # STRICT FILTER: Must be "15m" type (Time Range "X-Y")
                # Ex: "10:00AM-10:15AM" or "6PM-6:15PM" or "6:45-7PM"
                # Excludes "6PM ET" (Hourly/Daily)
                # Regex: Digits(:Digits)? (AM/PM)? - Digits(:Digits)? (AM/PM)
                if not re.search(r'\d+(?::\d+)?(?:AM|PM)?\s*-\s*\d+(?::\d+)?(?:AM|PM)', question):
                    # print(f"      [Skip] Not a 15m Range: {question}")
                    continue
                
                # Check Expiry
                end_date_str = market.get("endDate") # ISO 8601
                if not end_date_str: continue
                
                # Simple ISO parse (Python 3.11+ handles Z, 3.10 might need replace)
                end_date_str = end_date_str.replace("Z", "+00:00")
                end_dt = datetime.fromisoformat(end_date_str)
                now_utc = datetime.now(timezone.utc)
                
                # 1. Check API Expiry
                if end_dt <= now_utc:
                    continue
                    
                # 2. STRICT TITLE CHECK (Source of Truth)
                # Parse: "January 30, ... X PM ET"
                try:
                    # Regex to find Date and END Time (After the dash)
                    # Matches: "January 30" ... "- 7PM ET" or "- 6:15PM ET"
                    match = re.search(r'([A-Z][a-z]+) (\d{1,2}).*?-\s*(\d{1,2}(?::\d{2})?)([AP]M) ET', question)
                    if match:
                        month_str, day_str, time_str, ampm_str = match.groups()
                        
                        # Parse Month
                        try:
                            month = datetime.strptime(month_str, "%B").month
                        except:
                            continue # Fail safe
                            
                        # Parse Time
                        # 6PM -> 6:00, 6:45PM -> 6:45
                        if ":" in time_str:
                            h, m = map(int, time_str.split(":"))
                        else:
                            h = int(time_str)
                            m = 0
                            
                        if ampm_str == "PM" and h != 12: h += 12
                        if ampm_str == "AM" and h == 12: h = 0
                        
                        # Construct Expiry DT (ET Timezone approx UTC-5)
                        # Note: We assume current year.
                        year = now_utc.year
                        # Handle Year Rollover (Dec market in Jan) - Unlikely for flash
                        
                        # Create naive then force UTC-5
                        # ET is UTC-5 (Standard) or UTC-4 (DST). Jan is Standard.
                        # We used fixed offset for simplicity or safe buffer.
                        # Better: Use UTC and adjust. 6PM ET = 23:00 UTC.
                        expiry_et = datetime(year, month, int(day_str), h, m)
                        expiry_utc = expiry_et + timedelta(hours=5) # Add 5h to ET to get UTC
                        expiry_utc = expiry_utc.replace(tzinfo=timezone.utc)
                        
                        # VALIDATION LOGIC
                        # 1. If Expiry < Now: OLD (Don't trade)
                        # 2. If Expiry > Now + 20min: FUTURE (Don't trade)
                        
                        # Debug Parse Result
                        print(f"      [Parsed] {question} -> {expiry_utc} (Now: {now_utc})")
                        
                        # Buffer: Allow 1 minute grace? No, strict.
                        if expiry_utc < now_utc:
                             print(f"      [Skip] Title Expired: {question}")
                             continue
                            
                        diff_minutes = (expiry_utc - now_utc).total_seconds() / 60
                        if diff_minutes < 2:
                             print(f"      [Skip] Expires Soon: {question} ({diff_minutes:.1f}m left)")
                             continue
                             
                        if diff_minutes > 25:
                             print(f"      [Skip] Too Future: {question} (+{diff_minutes:.0f} mins)")
                             continue
                            
                    else:
                        print(f"      [Skip] Regex Failed to Match: {question}")
                        continue # STRICT MODE: If we can't parse title, don't trust it.
                        
                except Exception as parse_e:
                    print(f"      [Error] Parse Exception: {parse_e} - SKIPPING")
                    continue # IMPORTANT: If parsing fails, DO NOT trade. Skip.
                    
                targets.append({
                    "question": question,
                    "strike": 0, 
                    "yes_id": tokens[0],
                    "no_id": tokens[1],
                    "type": "flash_up_down",
                    "end_dt": end_dt
                })

    # Sort targets by Expiry (Soonest First) to catch the current 15m window
    # instead of tomorrow's window.
    targets.sort(key=lambda x: x.get("end_dt", datetime.max.replace(tzinfo=timezone.utc)))
    
    return targets

def get_position_balance(clob_client, token_id):
    if not clob_client: return 0.0
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        resp = clob_client.get_balance_allowance(
            BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
                signature_type=2
            )
        )
        return float(resp.get('balance', 0)) / 1_000_000
    except:
        return 0.0

def sell_position(client, token_id, amount):
    try:
        # Check limit price (Sell into Bid)
        ob = client.get_order_book(token_id)
        if not ob.bids:
            print("   ⚠️ No Bids to sell into.")
            return
            
        best_bid = float(ob.bids[0].price)
        limit_price = max(best_bid - 0.02, 0.01) # Aggressive sell
        
        print(f"   🔻 SELLING {amount} Shares @ {limit_price}...")
        resp = client.create_and_post_order(
            OrderArgs(
                price=limit_price,
                size=amount,
                side=SELL,
                token_id=token_id
            )
        )
        print(f"   ✅ Sell Order Sent! ID: {resp.get('orderID')}")
        
        # Update State
        update_position_state(token_id, 0, 0, side="SELL")
        
    except Exception as e:
        print(f"   ❌ Sell Failed: {e}")

def check_take_profit(client):
    if not client: return
    
    state = load_state()
    pos = state.get("current_position")
    if not pos: return # No active tracking
    
    token_id = pos["token_id"]
    entry_price = pos["entry_price"]
    size = pos["size"]
    
    # 1. Check if we still hold it (in case manual sell or expire)
    real_bal = get_position_balance(client, token_id)
    if real_bal < 0.1:
        # We lost it? Clear state
        update_position_state(token_id, 0, 0, side="SELL")
        return

    # 2. Check Price
    try:
        ob = client.get_order_book(token_id)
        if not ob.bids: 
            print(f"   ⚠️ TP Check: No Liquidity (Bid 0.00). Asks: {[a.price for a in ob.asks[:3]]}")
            return
            
        # SORTING CRITICAL (Fix PnL Bug)
        sorted_bids = sorted(ob.bids, key=lambda x: float(x.price), reverse=True)
        best_bid = float(sorted_bids[0].price)
        
        # DEBUG: Print Book
        # print(f"      [Book] Bids: {[b.price for b in ob.bids[:3]]}")
        
        # Calc ROI
        if entry_price <= 0: return # div by zero
        roi = (best_bid - entry_price) / entry_price
        roi_pct = roi * 100
        
        # Periodic Status (Use simple counter or time check to avoid spam?)
        print(f"   💰 Holding: {size} @ {entry_price:.2f} | Bid: {best_bid:.2f} | PnL: {roi_pct:+.2f}%")
        
        # TARGET: 10% (User requested increase)
        # Logic: If Gain > 10% OR (Safety: Gain < -20%?) No, hold.
        if roi_pct >= 10.0:
            print(f"   🤑 TAKE PROFIT TRIGGERED! (+{roi_pct:.2f}%)")
            sell_position(client, token_id, real_bal)
            
    except Exception as e:
        print(f"   ⚠️ TP Check Error: {e}")

def execute_signal(direction, current_price, client):
    if not client: 
        print("   ⚠️ No Client (Dry Run Mode)")
        return
        
    print(f"   🔎 Searching for best market to trade {direction}...")
    markets = get_btc_markets(50)
    
    # Filter for ATM (At The Money)
    # We want closest strike to current price
    best_market = None
    min_diff = float('inf')
    
    for m in markets:
        # Only accepting Flash Markets
        if "Up or Down" in m['question']:
            # Pick the NEAREST expiry (first one because we filtered for future?)
            # We should sort the results?
            # Actually, let's track the nearest end_dt logic just to be safe
            if not best_market or m['end_dt'] < best_market['end_dt']:
                best_market = m
            
    if best_market:
        print(f"   ⚡ Found Target Market: {best_market['question']}")
            
    if not best_market:
        print("   ❌ No suitable market found.")
        return
        
    print(f"   🎯 Target: {best_market['question']} (Strike: {best_market['strike']})")
    
    print(f"   🎯 Target: {best_market['question']} (Strike: {best_market['strike']})")
    
    # IDs
    yes_id = best_market['yes_id']
    no_id = best_market['no_id']
    
    # Determine Correct Side based on Signal
    target_token_id = yes_id if direction == "UP" else no_id
    opposing_token_id = no_id if direction == "UP" else yes_id
    
    # 1. Check & Sell Opposing Position (Hedge/Flip)
    opposing_bal = get_position_balance(client, opposing_token_id)
    if opposing_bal > 0.1:
        print(f"   🔄 FLIP: Holding {opposing_bal} of WRONG side. Selling first...")
        sell_position(client, opposing_token_id, opposing_bal)
        time.sleep(2) # Wait for processing
        
    # 2. Check Target Position
    bal = get_position_balance(client, target_token_id)
    if bal >= 5.0:
        print(f"   🛡️ Safety: Already own {bal} shares of Correct Side. Holding.")
        return
        
    # 3. Execute Buy logic
    token_id = target_token_id
    # ... continue to buy ...
        
    # Execute
    try:
        # Get Price first? Or Market Buy?
        # Let's use Limit Buy at slightly higher than best ask (Pseudo-Market)
        # We need the OrderBook to know price.
        # Quick Hack using Tick Size to just place a safe limit or Market Order if allowed
        # But Client.create_market_order requires Level 1? We have Level 2.
        
        # Checking Orderbook for Price
        ob = client.get_order_book(token_id)
        if not ob.asks:
            print("   ⚠️ No Asks in Orderbook.")
            return
            
        best_ask = float(ob.asks[0].price)
        print(f"   💲 Best Ask: {best_ask}")
        
        if best_ask > 0.99:
            print("   ⚠️ Price too high (>0.99). Skipping.")
            return
            
        print(f"      [Debug] Raw Asks: {[a.price for a in ob.asks[:3]]}")
        
        # SORTING CRITICAL
        sorted_asks = sorted(ob.asks, key=lambda x: float(x.price))
        sorted_bids = sorted(ob.bids, key=lambda x: float(x.price), reverse=True)
        
        if not sorted_asks:
             print("   ⚠️ No Asks after sort.")
             return

        best_ask = float(sorted_asks[0].price)
        print(f"   💲 Best Ask (Real): {best_ask}")
        
        if best_ask > 0.99:
            print("   ⚠️ Price too high (>0.99). Skipping.")
            return

        limit_price = min(best_ask + 0.02, 0.99)
        
        print(f"   💸 Placing Buy for 6.0 Shares @ {limit_price} (est fill: {best_ask})...")
        resp = client.create_and_post_order(
            OrderArgs(
                price=limit_price,
                size=6.0,
                side=BUY,
                token_id=token_id
            )
        )
        print(f"   ✅ Order Sent! ID: {resp.get('orderID')}")
        
        # Save State for TP
        # CRITICAL: Use `best_ask` (Market Price) as entry, not `limit_price` (Cap)
        # This keeps PnL realistic.
        update_position_state(token_id, best_ask, 6.0, side="BUY")
        
    except Exception as e:
        print(f"   ❌ Execution Failed: {e}")
def main():
    print("--- BTC 15m Strategy Bot ---")
    
    # Init CLOB
    clob_client = None
    if PRIVATE_KEY:
        try:
            clob_client = ClobClient(
                HOST, 
                key=PRIVATE_KEY, 
                chain_id=CHAIN_ID,
                signature_type=2, # 2 = PolyGnosisSafe
                funder=FUNDER_ADDRESS # Proxy Wallet
            )
            creds = clob_client.create_or_derive_api_creds()
            clob_client.set_api_creds(creds)
            print(f"✅ Executing from: {clob_client.get_address()}")
        except Exception as e:
            print(f"❌ Execution Init Failed: {e}")
            return
    else:
        print("⚠️ No Private Key (Monitoring Only)")

    print("🚀 Starting High-Frequency Logic Loop...")
    
    last_strategy_check = 0
    
    while True:
        try:
            # --- 1. Fast Loop: Take Profit & Management (Every 10s) ---
            if clob_client:
                check_take_profit(clob_client)
            
            # --- 2. Slow Loop: Strategy & Entry (Every 60s) ---
            if time.time() - last_strategy_check > 60:
                last_strategy_check = time.time()
                
                # 1. Get Data
                candles = fetch_binance_candles("BTCUSDT", "15m", 250)
                if not candles:
                    time.sleep(10)
                    continue
                    
                # Parse Closes for Indicators
                closes = [float(c[4]) for c in candles]
                current_price = closes[-1]
                
                # 2. Calculate Indicators
                # RSI
                rsi = Indicators.calculate_rsi(closes)
                rsi_prev = Indicators.calculate_rsi(closes[:-1])
                rsi_slope = rsi - rsi_prev if (rsi and rsi_prev) else 0
                
                # MACD
                macd = Indicators.calculate_macd(closes)
                
                # VWAP
                vwap_series = Indicators.calculate_vwap_intraday(candles)
                vwap = vwap_series[-1] if vwap_series else None
                # Slope (last 3 candles)
                vwap_slope = 0
                if vwap_series and len(vwap_series) > 3:
                    vwap_slope = vwap_series[-1] - vwap_series[-4]
                
                # Heiken Ashi
                ha = Indicators.calculate_heiken_ashi(candles)
                
                # 3. Score
                # Updated to use BayesianPredictor (Legacy score_direction removed)
                score, predictor = BayesianPredictor.calculate_bayes_score(
                    current_price, vwap, vwap_slope, 
                    rsi, rsi_slope, 
                    macd, 
                    ha['color'], ha['count'],
                    poly_price=None, # Not fetching poly data in sync loop for now
                    last_close=closes[-2] if len(closes) > 1 else None
                )
                
                # 4. Display Status in One Line
                tstamp = datetime.now().strftime("%H:%M:%S")
                
                # Resolution Logic: Price(End) >= Price(Start)
                candle_open = float(candles[-1][1])
                gap = current_price - candle_open
                market_state = "WINNING" if gap >= 0 else "LOSING"
                
                print(f"[{tstamp}] BTC: ${current_price:,.0f} | Gap: {gap:+.2f} ({market_state}) | Score: {score:.2f} | RSI: {rsi:.1f}")
                if vwap:
                     print(f"            VWAP: ${vwap:,.0f} | MACD Hist: {macd['hist']:.2f} | HA: {ha['color']} x{ha['count']}")

                if score >= 0.7:
                     print("   ✅ SIGNAL: STRONG BUY (YES) - Trend Up")
                     execute_signal("UP", current_price, clob_client)
                elif score <= 0.3:
                     print("   🔻 SIGNAL: STRONG SHORT (NO/Buy NO) - Trend Down")
                     execute_signal("DOWN", current_price, clob_client)
                     
            # Fast Loop (1s for Real-Time PnL)
            time.sleep(1)
            
        except Exception as e:
            print(f"Error in loop: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(10)

if __name__ == "__main__":
    main()
