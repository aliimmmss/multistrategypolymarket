import asyncio
import aiohttp
import json
import time
import os
import sys
from datetime import datetime
from collections import deque
import logging

# Configure Logging
logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger("Assistant")

# Helper for Colors
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    RESET = '\033[0m'
    BOLD = '\033[1m'

# Constants
BINANCE_WSS = "wss://stream.binance.com:9443/ws/btcusdt@kline_1m"
BINANCE_REST = "https://api.binance.com/api/v3/klines"
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com/events"
POLY_WS_URL = "wss://ws-live-data.polymarket.com"

# --- TA Calculations ---
def calculate_rsi(prices, period=14):
    if len(prices) < period + 1: return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_macd(prices, fast=12, slow=26, signal=9):
    if len(prices) < slow + signal: return 0, 0, 0
    # Simple EMA for lightweight approx
    def ema(data, period):
        k = 2 / (period + 1)
        res = [data[0]]
        for i in range(1, len(data)):
            res.append(data[i] * k + res[i-1] * (1-k))
        return res
    
    emas_fast = ema(prices, fast)
    emas_slow = ema(prices, slow)
    macd_line = [f - s for f, s in zip(emas_fast, emas_slow)]
    signal_line = ema(macd_line, signal)
    
    return macd_line[-1], signal_line[-1], macd_line[-1] - signal_line[-1]

# --- Main Assistant Class ---
class PolymarketAssistant:
    def __init__(self):
        self.closes = []
        self.current_price = 0.0
        self.poly_price = None  # Clob price
        self.ref_price = None   # Chainlink/Ref price
        self.market = None
        
        self.rsi = 50.0
        self.macd_hist = 0.0
        self.ha_color = "NEUTRAL"
        
        self.prediction = "WAIT"
        self.predict_confidence = 0.0

        # Output Buffer
        self.last_print = 0

    async def find_latest_market(self):
        print(f"{Colors.YELLOW}🔍 Scanning for latest 15m BTC Market...{Colors.RESET}")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(POLYMARKET_GAMMA, params={
                    "limit": 50, "active": "true", "archived": "false", 
                    "closed": "false", "order": "startDate", "ascending": "false"
                }) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for event in data:
                            markets = event.get("markets", [])
                            for m in markets:
                                q = m.get("question", "")
                                if "Bitcoin" in q and "Up or Down" in q:
                                    # Found it
                                    self.market = m
                                    self.market['slug'] = event.get('slug')
                                    print(f"{Colors.GREEN}✅ Found: {q} (End: {m.get('endDate')}){Colors.RESET}")
                                    return
        except Exception as e:
            print(f"{Colors.RED}scan error: {e}{Colors.RESET}")

    async def fetch_history(self):
        print(f"{Colors.YELLOW}⏳ Fetching History for TA...{Colors.RESET}")
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{BINANCE_REST}?symbol=BTCUSDT&interval=1m&limit=100"
                async with session.get(url) as resp:
                    data = await resp.json()
                    self.closes = [float(k[4]) for k in data]
                    print(f"{Colors.GREEN}✅ History Loaded ({len(self.closes)} candles){Colors.RESET}")
        except:
            pass

    def run_analysis(self):
        # Update Indicators
        if not self.closes: return
        
        self.rsi = calculate_rsi(self.closes)
        _, _, self.macd_hist = calculate_macd(self.closes)
        
        # Simple Prediction Logic
        score = 0
        if self.rsi > 60: score += 1
        if self.rsi < 40: score -= 1
        if self.macd_hist > 0: score += 1
        if self.macd_hist < 0: score -= 1
        
        if self.current_price > self.closes[-2]: score += 1 # Trend
        
        if score >= 2: 
            self.prediction = f"{Colors.GREEN}BULLISH 🚀{Colors.RESET}"
            self.predict_confidence = 70 + (score * 5)
        elif score <= -2:
            self.prediction = f"{Colors.RED}BEARISH 🩸{Colors.RESET}"
            self.predict_confidence = 70 + (abs(score) * 5)
        else:
            self.prediction = f"{Colors.YELLOW}NEUTRAL ⚖️{Colors.RESET}"
            self.predict_confidence = 50.0

    async def stream_binance(self):
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(BINANCE_WSS) as ws:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        k = data['k']
                        self.current_price = float(k['c'])
                        if k['x']: # Candle closed
                            self.closes.append(self.current_price)
                            if len(self.closes) > 100: self.closes.pop(0)
                        
                        self.run_analysis()
                        self.print_dashboard()

    def print_dashboard(self):
        if time.time() - self.last_print < 1.0: return
        self.last_print = time.time()
        
        # Clear Screen (ANSI)
        print("\033[H\033[J", end="")
        
        print(f"{Colors.CYAN}{Colors.BOLD}╔════════════════════════════════════════════╗{Colors.RESET}")
        print(f"{Colors.CYAN}║      POLYMARKET BTC 15m ASSISTANT      ║{Colors.RESET}")
        print(f"{Colors.CYAN}╚════════════════════════════════════════════╝{Colors.RESET}\n")
        
        if self.market:
            print(f"🎯 Market: {self.market.get('question')}")
        
        print(f"\n💰 {Colors.BOLD}BTC Price:{Colors.RESET} ${self.current_price:,.2f}")
        
        rsi_color = Colors.GREEN if self.rsi > 55 else (Colors.RED if self.rsi < 45 else Colors.YELLOW)
        print(f"📊 {Colors.BOLD}RSI (1m):{Colors.RESET}  {rsi_color}{self.rsi:.1f}{Colors.RESET}")
        
        macd_color = Colors.GREEN if self.macd_hist > 0 else Colors.RED
        print(f"🌊 {Colors.BOLD}MACD:{Colors.RESET}      {macd_color}{self.macd_hist:.2f}{Colors.RESET}")
        
        print(f"\n🔮 {Colors.BOLD}PREDICTION:{Colors.RESET}  {self.prediction}")
        print(f"🛡️ {Colors.BOLD}Confidence:{Colors.RESET} {self.predict_confidence:.1f}%")
        
        print(f"\n{Colors.BLUE}Press Ctrl+C to stop{Colors.RESET}")

    async def run(self):
        await self.find_latest_market()
        await self.fetch_history()
        await self.stream_binance()

if __name__ == "__main__":
    try:
        if sys.platform == 'win32':
             asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(PolymarketAssistant().run())
    except KeyboardInterrupt:
        print("\n👋 Exiting...")
