import os
import re
import json
import math
import time
import asyncio
import logging
import traceback
from datetime import datetime, timezone, timedelta, date

import aiohttp

from btc_15m.config import (
    POLY_MARKET_URL, GAMMA_API_URL, POLYGON_CHAIN_ID,
    USDC_DECIMALS, BTC_15M_SERIES_ID,
    MIN_TRADE_USD, MIN_BET_FALLBACK_USD, MAX_SHARES_CAP,
    MAX_BUY_PRICE, MAX_BUY_PRICE_AGGRESSIVE,
    LIMIT_PRICE_OFFSET, SELL_PRICE_DUMP_OFFSET,
    MIN_SELL_BALANCE, DUST_FILTER,
    POSITION_GRACE_PERIOD, COOLDOWN_SECONDS,
    HARD_STOP_LOSS_PCT, TRAIL_ACTIVATION_PCT,
    TRAIL_MIN_DIST_PCT, TRAIL_MAX_DIST_PCT, ATR_TRAIL_MULTIPLIER,
    MIN_TIME_TO_EXPIRY_MINS, MAX_MARKET_DURATION_MINS,
    LOW_LIQUIDITY_PRICE_FLOOR, LOW_LIQUIDITY_AGE_SECONDS,
    SAFE_MIDPOINT_FALLBACK,
    TP_PRICE_HIGH, TP_PRICE_LOW, TP_ENTRY_THRESHOLD,
    is_paper_trading, is_aggressive_mode, get_paper_balance,
)
from btc_15m.db import db, StateManager
from btc_15m.risk import KellyEngine

logger = logging.getLogger("bot")

try:
    from mev_handler import FastLaneClient
    from web3 import Web3
    MEV_AVAILABLE = True
except ImportError:
    MEV_AVAILABLE = False

try:
    from py_clob_client.exceptions import PolyApiException
except ImportError:
    PolyApiException = Exception


class PolymarketManager:
    def __init__(self, key_path, risk_manager=None):
        self.client = None
        self.private_key = None
        self.key_path = key_path
        self.risk = risk_manager
        self.last_recovery_time = 0
        self.current_atr = 0.05
        self.last_exit_time = 0
        self.current_market = None
        self.latched_strike = None

        self.is_paper = is_paper_trading()
        self.virtual_balance = float(db.get_setting("paper_balance", str(get_paper_balance())))

        self._init_client()
        self._init_mev()

    def _init_client(self):
        try:
            from py_clob_client.client import ClobClient

            key = os.getenv("POLYGON_PRIVATE_KEY")
            if not key:
                logger.error("❌ POLYGON_PRIVATE_KEY not found in .env")
                return
            self.private_key = key.strip()

            funder = os.getenv("PROXY_WALLET_ADDRESS")
            sig_type = int(os.getenv("CLOB_SIGNATURE_TYPE", "2"))

            creds = self._load_api_credentials()

            self.client = ClobClient(
                POLY_MARKET_URL,
                key=self.private_key,
                chain_id=POLYGON_CHAIN_ID,
                signature_type=sig_type,
                funder=funder,
                creds=creds
            )

            if not creds:
                logger.info("📡 Deriving L2 API Credentials via L1/Private Key...")
                derived_creds = self.client.create_or_derive_api_creds()
                self.client.set_api_creds(derived_creds)

            logger.info(f"✅ Polymarket Client Ready: {self.client.get_address()} (SigType: {sig_type})")

        except Exception as e:
            logger.error(f"❌ Client Init Failed: {e}")

    def _load_api_credentials(self):
        api_key = os.getenv("CLOB_API_KEY")
        api_secret = os.getenv("CLOB_API_SECRET")
        api_pass = os.getenv("CLOB_API_PASSPHRASE")

        if not (api_key and api_secret and api_pass):
            return None

        logger.info("🔑 Using pre-defined L2 API Credentials from .env")
        return {"apiKey": api_key, "secret": api_secret, "passphrase": api_pass}

    def _init_mev(self):
        self.mev = None
        if MEV_AVAILABLE and self.private_key:
            try:
                self.mev = FastLaneClient(self.private_key)
                logger.info("MEV/FastLane Integration Active.")
            except Exception as e:
                logger.warning(f"⚠️ MEV Init Failed: {e}")

    # --- Market Discovery ---

    async def get_btc_markets(self):
        try:
            events = await self._fetch_gamma_events()
            if not events:
                return []
            return self._parse_market_events(events)
        except Exception as e:
            logger.error(f"Market Fetch Error: {e}")
            return []

    async def _fetch_gamma_events(self):
        params = {
            "series_id": BTC_15M_SERIES_ID,
            "limit": "50",
            "active": "true",
            "archived": "false",
            "closed": "false",
            "order": "endDate",
            "ascending": "true",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(GAMMA_API_URL, params=params) as resp:
                if resp.status != 200:
                    return []
                return await resp.json()

    def _parse_market_events(self, events):
        now_utc = datetime.now(timezone.utc)
        targets = []

        for event in events:
            if not self._is_event_started(event, now_utc):
                continue

            for market in event.get("markets", []):
                parsed = self._parse_single_market(market, now_utc)
                if parsed:
                    targets.append(parsed)

        targets.sort(key=lambda x: x['end_dt'])
        return targets

    def _is_event_started(self, event, now_utc):
        event_start_str = event.get("eventStartTime")
        if not event_start_str:
            return True
        event_start_str = event_start_str.replace("Z", "+00:00")
        try:
            event_start_dt = datetime.fromisoformat(event_start_str)
            if event_start_dt.tzinfo is None:
                event_start_dt = event_start_dt.replace(tzinfo=timezone.utc)
            return event_start_dt <= now_utc
        except Exception:
            return True

    def _parse_single_market(self, market, now_utc):
        question = market.get("question", "")

        if not self._is_btc_price_market(question):
            return None

        tokens = self._extract_tokens(market)
        if not tokens:
            return None

        start_dt, end_dt = self._parse_time_window(market, question, now_utc)
        if not end_dt:
            return None

        if not self._is_within_trading_window(start_dt, end_dt, now_utc):
            return None

        strike_price = self._extract_strike_price(market, question)
        best_bid = market.get("bestBid")
        best_ask = market.get("bestAsk")
        last_trade = market.get("lastTradePrice")
        market_id = market.get("id") or market.get("condition_id") or ""
        minutes_to_expiry = (end_dt - now_utc).total_seconds() / 60

        return {
            "id": market_id,
            "question": question,
            "yes_id": tokens[0],
            "no_id": tokens[1],
            "end_dt": end_dt,
            "start_dt": start_dt,
            "strike_price": strike_price,
            "best_bid": float(best_bid) if best_bid else None,
            "best_ask": float(best_ask) if best_ask else None,
            "last_trade": float(last_trade) if last_trade else None,
            "is_up_down": self._is_up_down_market(question),
            "minutes_to_expiry": round(minutes_to_expiry, 1),
        }

    def _is_btc_price_market(self, question):
        if "Bitcoin" not in question and "BTC" not in question:
            return False
        keywords = [">", "Above", "Below", "Price", "High", "Low", "Up", "Down", "Settle"]
        return any(kw in question for kw in keywords)

    def _extract_tokens(self, market):
        raw_tokens = market.get("clobTokenIds")
        if not raw_tokens:
            return None
        tokens = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
        return tokens if len(tokens) == 2 else None

    def _parse_time_window(self, market, question, now_utc):
        start_dt, end_dt = self._parse_time_from_title(question, now_utc)
        if end_dt:
            return start_dt, end_dt
        return self._parse_time_from_api(market)

    def _parse_time_from_title(self, question, now_utc):
        pattern = r"([A-Za-z]+) (\d{1,2}), (\d{1,2}:\d{2})\s*([AP]M)\s*[-\u2013\u2014]\s*(\d{1,2}:\d{2})\s*([AP]M)\s*ET"
        match = re.search(pattern, question)
        if not match:
            return None, None

        try:
            month_str, day_str, start_t, start_ap, end_t, end_ap = match.groups()

            def to_time(t_str, ampm):
                return datetime.strptime(f"{t_str} {ampm}", "%I:%M %p").time()

            month_num = datetime.strptime(month_str, "%B").month
            dt_date = date(now_utc.year, month_num, int(day_str))

            start_dt = datetime.combine(dt_date, to_time(start_t, start_ap))
            end_dt = datetime.combine(dt_date, to_time(end_t, end_ap))
            if end_dt < start_dt:
                end_dt += timedelta(days=1)

            # ET = UTC-5 (EST)
            start_dt = start_dt.replace(tzinfo=timezone.utc) + timedelta(hours=5)
            end_dt = end_dt.replace(tzinfo=timezone.utc) + timedelta(hours=5)
            return start_dt, end_dt
        except Exception:
            return None, None

    def _parse_time_from_api(self, market):
        end_str = market.get("endDateIso") or market.get("endDate")
        if not end_str:
            return None, None

        end_str = end_str.replace("Z", "+00:00")
        try:
            end_dt = datetime.fromisoformat(end_str)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None, None

        start_str = market.get("startDateIso") or market.get("startDate")
        if start_str:
            start_str = start_str.replace("Z", "+00:00")
            try:
                start_dt = datetime.fromisoformat(start_str)
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
            except Exception:
                start_dt = end_dt - timedelta(minutes=15)
        else:
            start_dt = end_dt - timedelta(minutes=15)

        return start_dt, end_dt

    def _is_within_trading_window(self, start_dt, end_dt, now_utc):
        duration_minutes = (end_dt - start_dt).total_seconds() / 60
        if not (start_dt <= now_utc < end_dt):
            return False
        if duration_minutes > MAX_MARKET_DURATION_MINS:
            return False
        return True

    def _is_up_down_market(self, question):
        """Detect if this is a binary 'Up or Down' market (no fixed dollar strike)."""
        q_lower = question.lower()
        return ("up or down" in q_lower or "up/down" in q_lower
                or ("up" in q_lower and "down" in q_lower and "$" not in question))

    def _extract_strike_price(self, market, question):
        # Tier 1: groupItemThreshold
        threshold = market.get("groupItemThreshold")
        if threshold:
            try:
                val = float(threshold)
                if val > 0:
                    return val
            except Exception:
                pass

        # Tier 2: Structured fields
        for field in ["strikePrice", "targetPrice", "line"]:
            v = market.get(field)
            if v:
                try:
                    val = float(v)
                    if val > 0:
                        return val
                except Exception:
                    pass

        # Tier 3: Regex fallback — dollar amount in question
        match = re.search(r"\$([\d,]+(\.\d+)?)", question)
        if match:
            try:
                val = float(match.group(1).replace(",", ""))
                if val > 0:
                    return val
            except Exception:
                pass

        # Tier 4: For "Up or Down" markets, try to extract reference price
        # from the market description (e.g. "Resolves based on BTC price at $97,500")
        desc = market.get("description", "")
        if desc:
            desc_match = re.search(r"\$([\d,]+(?:\.\d+)?)", desc)
            if desc_match:
                try:
                    val = float(desc_match.group(1).replace(",", ""))
                    if val > 1000:  # Sanity check: BTC prices are > $1000
                        return val
                except Exception:
                    pass

        return None

    # --- Balance & Valuation ---

    async def get_usdc_balance(self):
        if self.is_paper:
            return self.virtual_balance

        if not self.client:
            return 0.0

        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            sig_type = int(os.getenv("CLOB_SIGNATURE_TYPE", "2"))
            resp = await asyncio.to_thread(
                self.client.get_balance_allowance,
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig_type)
            )
            return float(resp.get('balance', 0)) / USDC_DECIMALS
        except Exception as e:
            logger.warning(f"Balance Fetch Error: {e}")
            return 0.0

    async def get_total_account_value(self):
        usdc = await self.get_usdc_balance()
        state = StateManager.load()
        pos = state.get("current_position")
        if not pos:
            return usdc

        token_id = pos["token_id"]
        entry_price = float(pos["entry_price"])
        size = float(pos["size"])

        try:
            ob = await asyncio.to_thread(self.client.get_order_book, token_id)
        except Exception:
            return usdc + size * entry_price

        if not ob.bids:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(pos["timestamp"])).total_seconds()
            if age < LOW_LIQUIDITY_AGE_SECONDS:
                return usdc + size * max(entry_price, LOW_LIQUIDITY_PRICE_FLOOR)
            return usdc + size * SAFE_MIDPOINT_FALLBACK

        best_bid = float(sorted(ob.bids, key=lambda x: float(x.price), reverse=True)[0].price)
        return usdc + size * best_bid

    # --- Ghost Position Recovery ---

    async def recover_ghost_positions(self):
        if not self.client:
            return
        if time.time() - self.last_recovery_time < 300:
            return
        self.last_recovery_time = time.time()

        state = StateManager.load()
        if state.get("current_position"):
            return

        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            sig_type = int(os.getenv("CLOB_SIGNATURE_TYPE", "2"))
            markets = await self.get_btc_markets()
            if not markets:
                return

            for m in markets:
                for token_id in [m["yes_id"], m["no_id"]]:
                    resp = await asyncio.to_thread(
                        self.client.get_balance_allowance,
                        BalanceAllowanceParams(
                            asset_type=AssetType.CONDITIONAL,
                            token_id=token_id,
                            signature_type=sig_type
                        )
                    )
                    bal = float(resp.get('balance', 0)) / USDC_DECIMALS
                    if bal >= DUST_FILTER:
                        pred = "UP" if token_id == m["yes_id"] else "DOWN"
                        logger.warning(f"🔁 Ghost Position Found: {bal} shares on {m['question']} ({pred})")
                        StateManager.update_position(
                            token_id, SAFE_MIDPOINT_FALLBACK, bal,
                            side="BUY", prediction=pred,
                            market_id=m.get("id", "")
                        )
                        return
        except Exception as e:
            logger.error(f"Ghost Recovery Error: {e}")

    # --- Trade Execution ---

    async def execute_trade(self, direction, token_id, score=0.5, prediction="UP", use_flashbots=False):
        if not self.client:
            return

        if self.is_paper:
            return await self._execute_paper_trade(direction, token_id, score, prediction)

        signed = await self._create_signed_orders(direction, token_id, score, prediction)
        if not signed:
            return

        if signed.get("action") == "CLEAR_STATE":
            StateManager.update_position(token_id, 0, 0, side="SELL")
            return

        primary_order = signed["primary_order"]
        tp_order = signed.get("tp_order")
        meta = signed["meta"]

        # Submit order
        if use_flashbots and self.mev and direction == "BUY":
            resp = await self._submit_via_mev(primary_order, meta)
        else:
            resp = await self._async_post_signed_order(primary_order)

        if not resp:
            return

        logger.info(f"✅ Order Submitted: {resp}")
        market_id = self.current_market.get("id", "") if self.current_market else ""
        StateManager.update_position(
            token_id, meta["price"], meta["size"],
            side="BUY" if direction == "BUY" else "SELL",
            prediction=prediction,
            market_id=market_id
        )

        if self.risk and direction == "SELL":
            pnl = StateManager.update_position(token_id, meta["price"], meta["size"], side="SELL")
            if pnl:
                self.risk.update_pnl(pnl)

        # Submit TP order if exists
        if tp_order:
            try:
                tp_resp = await self._async_post_signed_order(tp_order)
                if tp_resp:
                    tp_id = tp_resp.get("orderID") or tp_resp.get("id")
                    if tp_id:
                        state = StateManager.load()
                        pos = state.get("current_position")
                        if pos:
                            pos["tp_order_id"] = tp_id
                            state["current_position"] = pos
                            StateManager.save(state)
                    logger.info(f"✅ TP Limit Order Placed: {tp_id}")
            except Exception as e:
                logger.warning(f"⚠️ TP Post Failed: {e}")

    async def _execute_paper_trade(self, direction, token_id, score, prediction):
        if not self.client:
            return

        try:
            ob = await asyncio.to_thread(self.client.get_order_book, token_id)
        except Exception as e:
            logger.error(f"🧪 [PAPER] OB Fetch Error: {e}")
            return

        if direction == "BUY":
            if not ob.asks:
                return
            best_ask = float(sorted(ob.asks, key=lambda x: float(x.price))[0].price)
            usd_size = KellyEngine.calculate_size(self.virtual_balance, score, best_ask)
            if usd_size < MIN_TRADE_USD:
                return
            size = round(usd_size / best_ask, 2)
            self.virtual_balance -= usd_size
            db.save_setting("paper_balance", self.virtual_balance)
            market_id = self.current_market.get("id", "") if self.current_market else ""
            StateManager.update_position(token_id, best_ask, size, side="BUY", prediction=prediction,
                                         market_id=market_id)
            logger.info(f"🧪 [PAPER] BUY {size} shares @ ${best_ask:.2f}")
        else:
            if not ob.bids:
                return
            best_bid = float(sorted(ob.bids, key=lambda x: float(x.price), reverse=True)[0].price)
            state = StateManager.load()
            pos = state.get("current_position")
            if not pos:
                return
            sell_price = max(best_bid - 0.01, 0.01)
            proceeds = sell_price * float(pos.get("size", 0))
            self.virtual_balance += proceeds
            db.save_setting("paper_balance", self.virtual_balance)
            pnl = StateManager.update_position(token_id, sell_price, pos["size"], side="SELL")
            if self.risk and pnl:
                self.risk.update_pnl(pnl)
            logger.info(f"🧪 [PAPER] SELL @ ${sell_price:.2f} | Proceeds: ${proceeds:.2f}")

    async def _submit_via_mev(self, primary_order, meta):
        try:
            tx = self._prepare_onchain_fill_tx(primary_order, meta['size'])
            if tx:
                bundle = self.mev.create_bundle(tx)
                result = await asyncio.to_thread(self.mev.send_bundle, bundle)
                logger.info(f"⚡ FastLane Bundle: {result}")
                return result
        except Exception as e:
            logger.warning(f"⚠️ MEV Bundle Failed: {e}")
        return await self._async_post_signed_order(primary_order)

    async def _async_post_signed_order(self, signed_order):
        try:
            return await asyncio.to_thread(self.client.post_order, signed_order)
        except Exception as e:
            logger.error(f"❌ Async Post Failed: {e}")
            raise

    async def _create_signed_orders(self, direction, token_id, score, prediction):
        try:
            from py_clob_client.order_builder.constants import BUY, SELL
            side = BUY if direction == "BUY" else SELL

            try:
                ob = await asyncio.to_thread(self.client.get_order_book, token_id)
            except Exception as e:
                logger.error(f"Failed to fetch OB: {e}")
                return None

            if side == BUY:
                return await self._prepare_buy_order(ob, token_id, score)
            return await self._prepare_sell_order(ob, token_id)
        except Exception as e:
            logger.error(f"Order Prep Error: {e}")
            return None

    async def _prepare_buy_order(self, ob, token_id, score):
        try:
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.order_builder.constants import BUY, SELL

            if not ob.asks:
                return None

            sorted_asks = sorted(ob.asks, key=lambda x: float(x.price))
            best_ask = float(sorted_asks[0].price)

            max_price = MAX_BUY_PRICE_AGGRESSIVE if is_aggressive_mode() else MAX_BUY_PRICE
            if best_ask > max_price:
                logger.warning(f"Price too high ({best_ask}). Skipping.")
                return None
            if best_ask > MAX_BUY_PRICE_AGGRESSIVE:
                return None

            limit_price = min(best_ask + LIMIT_PRICE_OFFSET, MAX_BUY_PRICE_AGGRESSIVE)

            try:
                usdc_bal = await self.get_usdc_balance()
            except Exception:
                usdc_bal = 0.0

            if usdc_bal < MIN_TRADE_USD + 1:
                logger.error(f"❌ Low Balance: ${usdc_bal:.2f}. Cannot trade.")
                return None

            usd_size = KellyEngine.calculate_size(usdc_bal, score, limit_price)
            if usd_size > 0 and usd_size < MIN_BET_FALLBACK_USD and usdc_bal > MIN_BET_FALLBACK_USD:
                usd_size = MIN_BET_FALLBACK_USD
            if usd_size > usdc_bal:
                usd_size = usdc_bal * 0.99

            size = round(usd_size / limit_price, 2)
            if size > MAX_SHARES_CAP:
                logger.warning(f"⚠️ Size Capped at {MAX_SHARES_CAP} Shares (Calculated: {size})")
                size = MAX_SHARES_CAP

            implied_risk = usd_size / usdc_bal if usdc_bal > 0 else 0
            logger.info(f"💣 CONFIDENCE {score:.2f} | Bal: ${usdc_bal:.0f} | Risking {implied_risk:.1%} (${usd_size:.1f}) -> {size} Shares")

            args = OrderArgs(price=limit_price, size=size, side=BUY, token_id=token_id)
            signed_order = self.client.create_order(args)

            tp_signed = self._sign_tp_order(limit_price, size, token_id)

            return {
                "primary_order": signed_order,
                "tp_order": tp_signed,
                "meta": {"price": limit_price, "size": size}
            }
        except Exception as e:
            logger.error(f"Buy Prep Error: {e}")
            return None

    def _sign_tp_order(self, entry_price, size, token_id):
        try:
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.order_builder.constants import SELL

            tp_price = TP_PRICE_HIGH if entry_price >= TP_ENTRY_THRESHOLD else TP_PRICE_LOW
            tp_args = OrderArgs(price=tp_price, size=size, side=SELL, token_id=token_id)
            signed = self.client.create_order(tp_args)
            logger.info(f"🔫 Pre-Signed TP Order @ {tp_price}")
            return signed
        except Exception as e:
            logger.warning(f"⚠️ Failed to sign TP Order: {e}")
            return None

    async def _prepare_sell_order(self, ob, token_id):
        try:
            from py_clob_client.clob_types import OrderArgs, BalanceAllowanceParams, AssetType
            from py_clob_client.order_builder.constants import SELL

            if not ob.bids:
                return None

            real_bal = await self._get_token_balance(token_id)
            if real_bal < MIN_SELL_BALANCE:
                return {"action": "CLEAR_STATE"}

            self._cancel_pending_tp(token_id)

            size = math.floor(real_bal * 100) / 100
            if size <= 0:
                size = real_bal

            sorted_bids = sorted(ob.bids, key=lambda x: float(x.price), reverse=True)
            best_bid = float(sorted_bids[0].price)
            limit_price = max(best_bid - SELL_PRICE_DUMP_OFFSET, 0.01)

            logger.warning(f"📉 DUMPING {size} Shares (RealBal: {real_bal}) @ {limit_price}")

            args = OrderArgs(price=limit_price, size=size, side=SELL, token_id=token_id)
            signed_order = self.client.create_order(args)

            return {
                "primary_order": signed_order,
                "tp_order": None,
                "meta": {"price": limit_price, "size": size}
            }
        except Exception as e:
            logger.error(f"Sell Prep Error: {e}")
            return None

    async def _get_token_balance(self, token_id):
        if self.is_paper:
            state = StateManager.load()
            pos = state.get("current_position")
            return float(pos.get("size", 0)) if pos and pos.get("token_id") == token_id else 0.0

        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            sig_type = int(os.getenv("CLOB_SIGNATURE_TYPE", "2"))
            resp = await asyncio.to_thread(
                self.client.get_balance_allowance,
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id, signature_type=sig_type)
            )
            return float(resp.get('balance', 0)) / USDC_DECIMALS
        except Exception:
            return 0.0

    def _cancel_pending_tp(self, token_id):
        state = StateManager.load()
        pos = state.get("current_position")
        if not pos or not pos.get("tp_order_id"):
            return
        try:
            logger.info(f"🚫 Cancelling TP Limit Order {pos['tp_order_id']}...")
            self.client.cancel_order(pos["tp_order_id"])
        except Exception as ce:
            logger.warning(f"⚠️ TP Cancel failed: {ce}")

    # --- Trailing Stop Loss ---

    async def check_trailing_stop(self, current_price=0.0, current_atr=0.0):
        if not self.client:
            return

    def _sync_check_tsl(self, current_price, current_atr):
        try:
            state = StateManager.load()
            pos = state.get("current_position")
            if not pos:
                return

            token_id = pos["token_id"]
            entry = pos["entry_price"]
            size = pos["size"]
            entry_ts = datetime.fromisoformat(pos["timestamp"])
            highest_roi = pos.get("highest_roi", 0.0)

            bal = self._check_position_balance(token_id, entry_ts, size)
            if bal is None:
                StateManager.update_position(token_id, 0, 0, side="SELL")
                return
            if bal == 0:
                return  # Skip check this cycle

            best_bid = self._get_best_bid(token_id)
            if best_bid is None:
                return

            roi_pct = ((best_bid - entry) / entry) * 100
            highest_roi = self._update_high_water_mark(pos, state, roi_pct, highest_roi)

            if roi_pct <= HARD_STOP_LOSS_PCT:
                logger.info(f"🛑 HARD STOP FLOOR HIT! ROI: {roi_pct:.1f}%")
                self._sell_all(token_id, bal)
                return

            if highest_roi >= TRAIL_ACTIVATION_PCT:
                trail_dist = self._calculate_trail_distance(current_atr, current_price)
                logger.info(f"🛡️ ATR Trail: BTC ATR=${current_atr:.0f} -> Dist: {trail_dist:.2f}% (ROI: {roi_pct:.1f}%)")
                if roi_pct <= (highest_roi - trail_dist):
                    logger.info(f"🛑 TRAILING STOP HIT! Peak: {highest_roi:.1f}% | Drop: {trail_dist:.1f}%")
                    self._sell_all(token_id, bal)
        except Exception as e:
            logger.error(f"TSL Error: {e}")

    def _check_position_balance(self, token_id, entry_ts, size):
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        try:
            resp = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id, signature_type=2)
            )
            bal = float(resp.get('balance', 0)) / USDC_DECIMALS
        except Exception:
            bal = 0.0

        time_since_entry = (datetime.now(timezone.utc) - entry_ts).total_seconds()
        if bal < MIN_SELL_BALANCE:
            if time_since_entry < POSITION_GRACE_PERIOD:
                return size  # Use expected size during grace
            return None  # Signal to clear
        return bal

    def _get_best_bid(self, token_id):
        try:
            ob = self.client.get_order_book(token_id)
        except Exception as e:
            logger.warning(f"⚠️ TSL Orderbook Fetch Failed: {e}. Skipping check.")
            return None
        if not ob.bids:
            return None
        return float(sorted(ob.bids, key=lambda x: float(x.price), reverse=True)[0].price)

    def _update_high_water_mark(self, pos, state, roi_pct, highest_roi):
        if roi_pct > highest_roi:
            pos["highest_roi"] = roi_pct
            state["current_position"] = pos
            StateManager.save(state)
            return roi_pct
        return highest_roi

    def _calculate_trail_distance(self, current_atr, current_price):
        trail_dist = TRAIL_MIN_DIST_PCT
        if current_atr > 0 and current_price > 0:
            atr_pct = (current_atr / current_price) * 100
            trail_dist = max(ATR_TRAIL_MULTIPLIER * atr_pct, TRAIL_MIN_DIST_PCT)
        return min(max(trail_dist, TRAIL_MIN_DIST_PCT), TRAIL_MAX_DIST_PCT)

    def _sell_all(self, token_id, amount):
        if self.is_paper:
            self._paper_sell_all(token_id, amount)
            return

        try:
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.order_builder.constants import SELL

            self._cancel_pending_tp(token_id)

            try:
                ob = self.client.get_order_book(token_id)
            except PolyApiException:
                logger.warning("Token Expired during sell. Clearing state only.")
                pnl = StateManager.update_position(token_id, 0.0, 0, side="SELL")
                if self.risk and pnl:
                    self.risk.update_pnl(pnl)
                return

            if not ob.bids:
                return

            best_bid = float(sorted(ob.bids, key=lambda x: float(x.price), reverse=True)[0].price)
            limit = max(best_bid - SELL_PRICE_DUMP_OFFSET, 0.01)
            self.client.create_and_post_order(
                OrderArgs(price=limit, size=amount, side=SELL, token_id=token_id)
            )
            pnl = StateManager.update_position(token_id, limit, amount, side="SELL")
            if self.risk and pnl:
                self.risk.update_pnl(pnl)
            logger.info("✅ Position Closed.")
            self.last_exit_time = time.time()
        except Exception as e:
            logger.error(f"❌ Sell Failed: {e}")

    def _paper_sell_all(self, token_id, amount):
        try:
            ob = self.client.get_order_book(token_id)
            if ob.bids:
                best_bid = float(sorted(ob.bids, key=lambda x: float(x.price), reverse=True)[0].price)
                sell_price = max(best_bid - 0.01, 0.01)
                proceeds = sell_price * amount
                self.virtual_balance += proceeds
                db.save_setting("paper_balance", self.virtual_balance)
                logger.info(f"🧪 [PAPER] TSL Sell: {amount} shares @ ${sell_price:.2f}")
                pnl = StateManager.update_position(token_id, sell_price, amount, side="SELL")
                if self.risk and pnl:
                    self.risk.update_pnl(pnl)
            else:
                StateManager.update_position(token_id, 0.0, 0, side="SELL")
        except Exception as e:
            logger.error(f"🧪 [PAPER] TSL Fail: {e}")

    # --- MEV On-Chain Fill ---

    def _prepare_onchain_fill_tx(self, clob_order, fill_amount):
        if not self.mev:
            return None
        try:
            EXCHANGE_ADDR = Web3.to_checksum_address("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")

            def to_6d(val):
                return int(float(val) * 10**6) if isinstance(val, str) else int(val * 10**6)

            order_struct = {
                "salt": int(clob_order.get('salt', 0)),
                "maker": Web3.to_checksum_address(clob_order.get('maker', clob_order.get('maker_address'))),
                "signer": Web3.to_checksum_address(clob_order.get('signer', clob_order.get('signer_address', clob_order.get('maker_address')))),
                "taker": Web3.to_checksum_address(clob_order.get('taker', "0x0000000000000000000000000000000000000000")),
                "tokenId": int(clob_order.get('token_id', clob_order.get('asset_id'))),
                "makerAmount": to_6d(clob_order['maker_amount']),
                "takerAmount": to_6d(clob_order['taker_amount']),
                "expiration": int(clob_order['expiration']),
                "nonce": int(clob_order.get('nonce', 0)),
                "feeRateBps": int(clob_order.get('fee_rate_bps', 0)),
                "side": 0 if clob_order['side'].lower() == 'buy' else 1,
                "signatureType": int(clob_order.get('signature_type', 0)),
                "signature": Web3.to_bytes(hexstr=clob_order['signature'])
            }

            FILL_ORDER_ABI = [{
                "name": "fillOrder", "type": "function", "stateMutability": "nonpayable",
                "inputs": [
                    {"name": "order", "type": "tuple", "components": [
                        {"name": "salt", "type": "uint256"}, {"name": "maker", "type": "address"},
                        {"name": "signer", "type": "address"}, {"name": "taker", "type": "address"},
                        {"name": "tokenId", "type": "uint256"}, {"name": "makerAmount", "type": "uint256"},
                        {"name": "takerAmount", "type": "uint256"}, {"name": "expiration", "type": "uint256"},
                        {"name": "nonce", "type": "uint256"}, {"name": "feeRateBps", "type": "uint256"},
                        {"name": "side", "type": "uint8"}, {"name": "signatureType", "type": "uint8"},
                        {"name": "signature", "type": "bytes"}
                    ]},
                    {"name": "fillAmount", "type": "uint256"}
                ]
            }]

            w3 = self.mev.w3
            contract = w3.eth.contract(address=EXCHANGE_ADDR, abi=FILL_ORDER_ABI)
            data = contract.encode_abi("fillOrder", [order_struct, to_6d(fill_amount)])

            return {
                "from": self.mev.address, "to": EXCHANGE_ADDR, "data": data,
                "gas": 400000, "gasPrice": w3.eth.gas_price, "chainId": POLYGON_CHAIN_ID
            }
        except Exception as e:
            logger.error(f"❌ TX Encoding Failed: {e}")
            logger.debug(traceback.format_exc())
            return None

    # --- Market Resolution ---

    async def check_market_resolution(self, market_id):
        """Check if a market has resolved via the Gamma API.
        Returns: 'Yes', 'No', or None (still open)."""
        if not market_id:
            return None
        try:
            url = f"https://gamma-api.polymarket.com/markets/{market_id}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()

                    if not data.get("closed"):
                        return None

                    # Check outcomePrices for definitive resolution
                    outcome_prices = data.get("outcomePrices")
                    if outcome_prices:
                        try:
                            prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                            if len(prices) >= 2:
                                if float(prices[0]) >= 0.99:
                                    return "Yes"
                                elif float(prices[1]) >= 0.99:
                                    return "No"
                        except Exception:
                            pass

                    # Fallback: check resolution field
                    resolution = data.get("resolution")
                    if resolution:
                        return resolution  # 'Yes' or 'No'

                    return None
        except Exception as e:
            logger.debug(f"Resolution check failed for {market_id}: {e}")
            return None

    async def check_active_paper_resolutions(self):
        """Monitor active paper position for resolution and close it automatically."""
        if not self.is_paper:
            return

        state = StateManager.load()
        pos = state.get("current_position")
        if not pos or not pos.get("market_id") or not pos.get("token_id"):
            return

        market_id = pos["market_id"]
        token_id = pos["token_id"]
        size = float(pos.get("size", 0))

        # Check for resolution
        resolution = await self.check_market_resolution(market_id)
        if resolution is None:
            return

        try:
            # For BTC 15m markets, we always buy the YES token for the intended direction
            # Resolution 'Yes' = WIN, 'No' = LOSS
            won = (resolution == "Yes")
            exit_price = 1.0 if won else 0.0
            proceeds = exit_price * size
            
            logger.info(f"✨ [PAPER] Market Resolved: {resolution} | Trade {'WON' if won else 'LOST'}")
            
            # Update Balance
            self.virtual_balance += proceeds
            db.save_setting("paper_balance", self.virtual_balance)
            
            # Clear position and log trade
            pnl = StateManager.update_position(token_id, exit_price, size, side="SELL")
            if self.risk and pnl:
                self.risk.update_pnl(pnl)
                
            logger.info(f"✅ Paper Position Resolved & Closed. New Balance: ${self.virtual_balance:.2f}")

        except Exception as e:
            logger.error(f"❌ Paper Resolution Error: {e}")

    async def check_pending_resolutions(self):
        """Check all pending trade outcomes and update them if markets have resolved."""
        pending = db.get_pending_outcomes()
        if not pending:
            return

        for trade in pending[:5]:  # Limit to 5 per cycle
            market_id = trade.get("market_id", "")
            token_id = trade.get("token_id", "")
            if not market_id:
                continue

            resolution = await self.check_market_resolution(market_id)
            if resolution is None:
                continue  # Market still open

            # Determine if this trade was a market-level win
            # We need to know whether the user held YES or NO
            # For BTC 15m markets, BUY trades are always on YES token
            # Resolution 'Yes' = YES wins, 'No' = NO wins
            outcome = "WIN" if resolution == "Yes" else "LOSS"
            db.update_trade_outcome(token_id, outcome)
            logger.info(f"📊 Market Resolved: {resolution} → Trade {outcome}")
