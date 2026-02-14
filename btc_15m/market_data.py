import os
import json
import time
import asyncio
import logging
from datetime import datetime, timezone, timedelta

import aiohttp
import websockets

from utils.strategy_utils import Indicators, BayesianPredictor
from btc_15m.config import (
    BINANCE_WSS, BINANCE_WSS_1M, BINANCE_REST, BINANCE_FUTURES_REST,
    COINBASE_WSS, FEAR_GREED_API,
    HISTORY_CANDLES, WARMUP_CANDLES,
    POLY_PRICE_FLOOR, POLY_PRICE_CEILING, MAX_SPREAD,
    BUY_SCORE_THRESHOLD, SHORT_SCORE_THRESHOLD,
    HEDGE_DUMP_LONG_THRESHOLD, HEDGE_DUMP_SHORT_THRESHOLD,
    STRATEGY_INTERVAL_SECONDS,
    TSL_INTERVAL_ACTIVE, TSL_INTERVAL_IDLE, COOLDOWN_SECONDS,
    LATENCY_DIVERGENCE_PCT,
    MIN_TIME_TO_EXPIRY_MINS,
    use_flashbots,
)
from btc_15m.db import db, StateManager
from btc_15m.dashboard import Dashboard
from btc_15m.risk import RiskManager

logger = logging.getLogger("bot")

try:
    from mev_handler import FastLaneClient
    MEV_AVAILABLE = True
except ImportError:
    MEV_AVAILABLE = False


class MarketData:
    def __init__(self, poly_client):
        self.poly = poly_client
        self.risk = poly_client.risk if poly_client.risk else RiskManager()
        self.closes = []
        self.closes_1m = []
        self.candles = []
        self.current_price = 0.0
        self.last_tsl_check = datetime.now(timezone.utc) - timedelta(days=1)
        self.last_strategy_run = datetime.now(timezone.utc) - timedelta(days=1)
        self.coinbase_price = None
        self.chainlink_price = None
        self.fear_greed_index = 50
        self.current_atr = 0.0

        self.bayesian = BayesianPredictor()
        self.current_market_id = None
        self.latched_strike = None

        asyncio.create_task(self._coinbase_listener())
        asyncio.create_task(self._chainlink_poller())
        asyncio.create_task(self._fear_greed_poller())

        self.mev = None
        if MEV_AVAILABLE and hasattr(self.poly, 'private_key') and self.poly.private_key:
            try:
                self.mev = FastLaneClient(private_key=self.poly.private_key)
                logger.info("Flashbots (FastLane) Ready.")
            except Exception as e:
                logger.warning(f"⚠️ FastLane Init Failed: {e}")

    # --- External Data Streams ---

    async def _coinbase_listener(self):
        while True:
            try:
                async with websockets.connect(COINBASE_WSS) as ws:
                    await ws.send(json.dumps({
                        "type": "subscribe",
                        "product_ids": ["BTC-USD"],
                        "channels": ["ticker"]
                    }))
                    logger.info("📡 Connected to Coinbase Pro (Latency Oracle)")
                    async for msg in ws:
                        data = json.loads(msg)
                        if data.get("type") == "ticker" and "price" in data:
                            self.coinbase_price = float(data["price"])
                            Dashboard.binance_price = self.coinbase_price
            except Exception as e:
                logger.debug(f"⚠️ Coinbase WS Error: {e}")
                await asyncio.sleep(5)

    async def _chainlink_poller(self):
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            self.chainlink_price = data.get("bitcoin", {}).get("usd")
            except Exception as e:
                logger.debug(f"⚠️ Chainlink Poller Error: {e}")
            await asyncio.sleep(10)

    async def _fear_greed_poller(self):
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(FEAR_GREED_API) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            val = data.get("data", [{}])[0].get("value", "50")
                            self.fear_greed_index = int(val)
            except Exception as e:
                logger.debug(f"⚠️ Fear & Greed Error: {e}")
            await asyncio.sleep(300)

    @staticmethod
    def calculate_time_decay_factor(expiry_dt):
        remaining_sec = (expiry_dt - datetime.now(timezone.utc)).total_seconds()
        if remaining_sec < 180:
            return 2.0
        if remaining_sec < 600:
            return 1.5
        return 1.0

    # --- Binance Streams ---

    async def fetch_initial_history(self):
        try:
            logger.info("⏳ Fetching 48h History from Binance...")
            async with aiohttp.ClientSession() as session:
                url = f"{BINANCE_REST}?symbol=BTCUSDT&interval=15m&limit={HISTORY_CANDLES}"
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.error("Failed to fetch history.")
                        return
                    data = await resp.json()
                    for k in data:
                        self.closes.append(float(k[4]))
                        self.candles.append([k[0], k[1], k[2], k[3], k[4], k[5]])

            logger.info(f"✅ Loaded {len(self.closes)} Historical Candles!")
            if self.closes:
                self.current_price = self.closes[-1]
            await self.run_strategy()
        except Exception as e:
            logger.error(f"History Fetch Error: {e}")

    async def stream_prices(self):
        await asyncio.gather(self.stream_15m_candles(), self.stream_1m_candles())

    async def stream_15m_candles(self):
        retry_delay = 1
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(BINANCE_WSS, heartbeat=30.0) as ws:
                        logger.info("🔌 Connected to Binance Websocket (15m)")
                        retry_delay = 1
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self.process_candle(json.loads(msg.data))
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                break
            except Exception as e:
                logger.error(f"WS (15m) Error: {e}. Retrying in {retry_delay}s...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30)

    async def stream_1m_candles(self):
        retry_delay = 1
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(BINANCE_WSS_1M, heartbeat=30.0) as ws:
                        logger.info("🔌 Connected to Binance Websocket (1m)")
                        retry_delay = 1
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                k = data['k']
                                if k['x']:
                                    self.closes_1m.append(float(k['c']))
                                    if len(self.closes_1m) > 100:
                                        self.closes_1m.pop(0)
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                break
            except Exception as e:
                logger.error(f"WS (1m) Error: {e}. Retrying in {retry_delay}s...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30)

    async def get_btc_funding(self):
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{BINANCE_FUTURES_REST}?symbol=BTCUSDT"
                async with session.get(url, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return float(data.get('lastFundingRate', 0))
            return 0.0
        except Exception:
            return 0.0

    # --- Candle Processing ---

    async def process_candle(self, data):
        k = data['k']
        is_closed = k['x']
        close_price = float(k['c'])
        self.current_price = close_price
        Dashboard.btc_price = self.current_price

        now_ts = datetime.now(timezone.utc)
        state = StateManager.load()
        has_position = state.get("current_position") is not None
        tsl_interval = TSL_INTERVAL_ACTIVE if has_position else TSL_INTERVAL_IDLE

        if (now_ts - self.last_tsl_check).total_seconds() > tsl_interval:
            self.last_tsl_check = now_ts
            asyncio.create_task(self.poly.check_trailing_stop(
                current_price=self.current_price, current_atr=self.current_atr
            ))

        if (datetime.now(timezone.utc) - self.last_strategy_run).total_seconds() > STRATEGY_INTERVAL_SECONDS:
            self.last_strategy_run = datetime.now(timezone.utc)
            await self.run_strategy()

        if is_closed:
            self.closes.append(close_price)
            self.candles.append([k['t'], k['o'], k['h'], k['l'], k['c'], k['v']])
            if len(self.closes) > HISTORY_CANDLES:
                self.closes.pop(0)
                self.candles.pop(0)
            logger.info(f"🕯️ Candle Closed: ${close_price:,.0f} | Running Strategy...")
            self.last_strategy_run = datetime.now(timezone.utc)
            await self.run_strategy()

    # --- Strategy ---

    async def run_strategy(self):
        if len(self.closes) < WARMUP_CANDLES:
            logger.info("⚠️ Warming up... need more data.")
            return

        indicators = self._calculate_indicators()
        context = await self._get_market_context()
        score = self._calculate_score(indicators, context) if context else 0.5
        self._update_dashboard(score, indicators, context)

        if not context:
            return
        await self._handle_risk(score, context)
        await self._execute_entry(score, context)

    def _calculate_indicators(self):
        rsi = Indicators.calculate_rsi(self.closes)
        atr = Indicators.calculate_atr(self.candles)
        self.current_atr = atr if atr else 0.05

        rsi_prev = Indicators.calculate_rsi(self.closes[:-1])
        rsi_slope = rsi - rsi_prev if (rsi and rsi_prev) else 0

        macd = Indicators.calculate_macd(self.closes)
        vwap_series = Indicators.calculate_vwap_intraday(self.candles)
        vwap = vwap_series[-1] if vwap_series else None
        vwap_slope = (vwap_series[-1] - vwap_series[-4]) if vwap_series and len(vwap_series) > 3 else 0

        ha = Indicators.calculate_heiken_ashi(self.candles)

        return {
            "rsi": rsi, "rsi_slope": rsi_slope, "atr": atr,
            "macd": macd, "vwap": vwap, "vwap_slope": vwap_slope, "ha": ha
        }

    async def _get_market_context(self):
        context = {
            "market": None, "poly_price": None, "poly_spread": None,
            "vol_up": 0.0, "vol_down": 0.0, "moneyness": None, "time_decay": 1.0,
            "funding_rate": 0.0, "latency_score": 0, "reference_price": self.closes[-1]
        }

        active_markets = await self.poly.get_btc_markets()
        if not active_markets:
            return None

        for target_m in active_markets:
            try:
                poly_ctx = await self._evaluate_market(target_m, context)
                if poly_ctx:
                    return poly_ctx
            except Exception as e:
                logger.error(f"Error processing market {target_m.get('question')}: {e}")

        logger.warning("No eligible BTC markets found in this cycle.")
        return None

    async def _evaluate_market(self, target_m, context):
        bid, ask, ob = await self._get_market_prices(target_m)
        if not bid or not ask:
            return None

        poly_price = (bid + ask) / 2
        poly_spread = ask - bid

        if poly_price > POLY_PRICE_CEILING or poly_price < POLY_PRICE_FLOOR:
            return None
        if poly_spread > MAX_SPREAD:
            return None

        self._latch_market(target_m)

        context["market"] = target_m
        context["poly_price"] = poly_price
        context["poly_spread"] = poly_spread
        self.poly.current_market = target_m

        strike = target_m.get("strike_price")
        if strike:
            context["moneyness"] = context["reference_price"] - strike

        # OBI
        try:
            if not ob:
                ob = await asyncio.to_thread(self.poly.client.get_order_book, target_m['yes_id'])
            vol_up, vol_down = Indicators.calculate_weighted_obi(ob, poly_price)
            context["vol_up"] = vol_up
            context["vol_down"] = vol_down
        except Exception:
            pass

        context["funding_rate"] = await self.get_btc_funding()
        context["latency_score"] = self._calculate_latency_score()

        return context

    async def _get_market_prices(self, target_m):
        bid = target_m.get("best_bid")
        ask = target_m.get("best_ask")
        ob = None

        if bid is None or ask is None:
            ob = await asyncio.to_thread(self.poly.client.get_order_book, target_m['yes_id'])
            if ob.bids and ob.asks:
                bid = float(ob.bids[0].price)
                ask = float(ob.asks[0].price)

        return bid, ask, ob

    def _latch_market(self, target_m):
        if target_m.get("id") != self.current_market_id:
            self.current_market_id = target_m.get("id")
            self.latched_strike = target_m.get("strike_price")

        if not self.latched_strike and target_m.get("strike_price"):
            self.latched_strike = target_m.get("strike_price")

        # For "Up or Down" markets with no explicit strike,
        # use the current BTC price at market open as the reference "strike"
        if not self.latched_strike and target_m.get("is_up_down"):
            self.latched_strike = self.current_price

        if self.latched_strike:
            target_m["strike_price"] = self.latched_strike

    def _calculate_latency_score(self):
        if not self.coinbase_price:
            return 0
        delta_pct = ((self.coinbase_price - self.current_price) / self.current_price) * 100
        if delta_pct > LATENCY_DIVERGENCE_PCT:
            return 1
        if delta_pct < -LATENCY_DIVERGENCE_PCT:
            return -1
        return 0

    def _calculate_score(self, ind, ctx):
        score, _ = BayesianPredictor.calculate_bayes_score(
            self.current_price, ind["vwap"], ind["vwap_slope"],
            ind["rsi"], ind["rsi_slope"], ind["macd"],
            ind["ha"]['color'], ind["ha"]['count'],
            ctx["poly_price"], ctx["poly_spread"],
            ctx["vol_up"], ctx["vol_down"],
            ctx["funding_rate"], ctx["latency_score"],
            moneyness=ctx.get("moneyness"),
            time_decay=1.0,
            fear_greed=self.fear_greed_index,
            last_close=self.closes[-1]
        )
        return score

    def _update_dashboard(self, score, ind, ctx):
        Dashboard.rsi_val = ind["rsi"] if ind["rsi"] else 0
        Dashboard.rsi_arrow = "↑" if ind["rsi_slope"] > 0 else "↓"
        Dashboard.macd_label = "bullish" if ind["macd"] and ind["macd"].get("hist", 0) > 0 else "bearish"
        Dashboard.ha_label = f"{ind['ha']['color']} x{ind['ha']['count']}"
        Dashboard.vwap_val = ind["vwap"] if ind["vwap"] else 0

        delta_1m = self.closes_1m[-1] - self.closes_1m[-2] if len(self.closes_1m) >= 2 else 0.0
        delta_3m = self.closes_1m[-1] - self.closes_1m[-4] if len(self.closes_1m) >= 4 else 0.0

        Dashboard.delta_1m = delta_1m
        Dashboard.delta_3m = delta_3m
        Dashboard.impulse = delta_1m
        Dashboard.predict_conf = max(score, 1 - score) * 100
        Dashboard.predict_label = "BULLISH" if score > 0.6 else ("BEARISH" if score < 0.4 else "NEUTRAL")

        Dashboard.poly_up = ctx["poly_price"] * 100 if ctx and ctx.get("poly_price") else 0
        Dashboard.poly_down = (1 - ctx["poly_price"]) * 100 if ctx and ctx.get("poly_price") else 0

        if ctx and ctx.get("market"):
            minutes_left = ctx["market"].get("minutes_to_expiry", 999)
            Dashboard.minutes_to_expiry = round(minutes_left, 1)
            Dashboard.entry_blocked = minutes_left < MIN_TIME_TO_EXPIRY_MINS
            strike_val = ctx["market"].get('strike_price')
            if strike_val:
                Dashboard.price_to_beat = float(strike_val)
            elif ctx["market"].get("is_up_down"):
                # For Up/Down markets, show BTC reference price
                Dashboard.price_to_beat = self.current_price
            else:
                Dashboard.price_to_beat = 0.0
            Dashboard.market_question = ctx["market"].get("question", "-")

            end_dt = ctx["market"].get("end_dt")
            if end_dt:
                diff = end_dt - datetime.now(timezone.utc)
                if diff.total_seconds() > 0:
                    mins, secs = divmod(int(diff.total_seconds()), 60)
                    Dashboard.time_left = f"{mins}m {secs}s"
                else:
                    Dashboard.time_left = "Expired"
            else:
                Dashboard.time_left = "-"
        else:
            Dashboard.market_question = "-"
            Dashboard.time_left = "-"

        Dashboard.btc_price = self.current_price
        Dashboard.export_state()

        db.log_signal(
            self.current_price, ind["rsi"] if ind["rsi"] else 0, self.current_atr,
            score, 0, 0,
            "LONG" if score >= 0.7 else "SHORT" if score <= 0.3 else "NEUTRAL"
        )
        logger.info(f"Final Score: {score:.2f} ({Dashboard.predict_label})")

    async def _handle_risk(self, score, ctx):
        await self.poly.check_trailing_stop(
            current_price=self.current_price, current_atr=self.current_atr
        )

        # Check pending trade resolutions periodically
        await self.poly.check_pending_resolutions()

        state = StateManager.load()
        pos = state.get("current_position")
        if not pos:
            return

        pred = pos.get("prediction", "UP")
        should_dump = (pred == "UP" and score <= HEDGE_DUMP_LONG_THRESHOLD) or \
                      (pred == "DOWN" and score >= HEDGE_DUMP_SHORT_THRESHOLD)

        if should_dump:
            logger.warning("HEDGE SIGNAL: Reversing Position!")
            if pos.get("tp_order_id"):
                try:
                    await asyncio.to_thread(self.poly.client.cancel_order, pos["tp_order_id"])
                except Exception:
                    pass
            await self.poly.execute_trade("SELL", pos["token_id"])

    async def _execute_entry(self, score, ctx):
        state = StateManager.load()
        if state.get("current_position"):
            return

        if time.time() - self.poly.last_exit_time < COOLDOWN_SECONDS:
            return

        target = ctx["market"]
        if not target:
            return

        # Block new entries within MIN_TIME_TO_EXPIRY_MINS of resolution
        if target.get("minutes_to_expiry", 999) < MIN_TIME_TO_EXPIRY_MINS:
            logger.info(f"⏸ Entry blocked: {target.get('minutes_to_expiry', 0):.1f} min to resolution")
            return

        fb = use_flashbots()

        if ctx["latency_score"] == -1 and score >= 0.65:
            logger.warning("VETO: Coinbase Bearish Divergence.")
            return

        if score >= BUY_SCORE_THRESHOLD:
            logger.info(f"BUY SIGNAL (Score {score:.2f}) on {target['question']}")
            await self.poly.execute_trade("BUY", target["yes_id"], score=score, prediction="UP", use_flashbots=fb)
        elif score <= SHORT_SCORE_THRESHOLD:
            logger.info(f"SHORT SIGNAL (Score {score:.2f}) on {target['question']}")
            await self.poly.execute_trade("BUY", target["no_id"], score=(1 - score), prediction="DOWN", use_flashbots=fb)
