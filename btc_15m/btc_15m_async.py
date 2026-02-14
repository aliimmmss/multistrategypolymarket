"""
BTC 15-Minute Prediction Market Bot — Entry Point

Connects to Binance for real-time BTC price data and trades
15-minute Bitcoin prediction markets on Polymarket.
"""

import sys
import os
import asyncio
import time
import logging

# Ensure parent is on path for utils imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rich.live import Live

from btc_15m.config import is_paper_trading
from btc_15m.db import db, StateManager
from btc_15m.dashboard import Dashboard, DashboardLogHandler
from btc_15m.risk import RiskManager
from btc_15m.polymarket_client import PolymarketManager
from btc_15m.market_data import MarketData

# --- Logging Setup ---
logger = logging.getLogger("bot")
logger.setLevel(logging.INFO)

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(console_handler)

dash_handler = DashboardLogHandler(buffer_size=15)
dash_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(dash_handler)


async def main():
    mode = "PAPER" if is_paper_trading() else "LIVE"
    logger.info(f"🚀 Starting BTC 15m Bot ({mode} Mode)")

    risk = RiskManager()
    poly = PolymarketManager(key_path=None, risk_manager=risk)
    market_data = MarketData(poly_client=poly)

    await market_data.fetch_initial_history()
    await poly.recover_ghost_positions()

    ws_task = asyncio.create_task(market_data.stream_prices())
    logger.info("Bot Started. Waiting for candles...")

    try:
        Dashboard.render(Dashboard.layout)
        with Live(Dashboard.layout, refresh_per_second=2, screen=True) as live:
            while True:
                Dashboard.render(Dashboard.layout)
                Dashboard.export_state()
                await asyncio.sleep(0.5)

                now = time.time()
                if poly.client and (now % 10 < 1):
                    await poly.check_trailing_stop(
                        current_price=market_data.current_price,
                        current_atr=market_data.current_atr
                    )

                if poly.client and (now % 30 < 1):
                    val = await poly.get_total_account_value()
                    if val:
                        market_data.risk.check_circuit_breaker(val)
                    
                    # Also check for paper resolutions if applicable
                    if is_paper_trading():
                        await poly.check_active_paper_resolutions()

    except KeyboardInterrupt:
        logger.info("Bot Stopped.")
        ws_task.cancel()
    except Exception:
        logger.exception("Crash:")


if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
