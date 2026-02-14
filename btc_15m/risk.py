import logging
from datetime import datetime, timezone, timedelta

from btc_15m.config import (
    MAX_DAILY_LOSS_PCT,
    CIRCUIT_BREAKER_HALT_HOURS,
    KELLY_MULTIPLIER,
    KELLY_MULTIPLIER_AGGRESSIVE,
    MAX_RISK_CAP,
    MAX_RISK_CAP_AGGRESSIVE,
    MIN_TRADE_USD,
    is_aggressive_mode,
)

logger = logging.getLogger("bot")


class KellyEngine:
    @staticmethod
    def calculate_size(balance, score, price):
        prob = max(score, 1 - score)
        b = (1 / price) - 1 if price < 1 else 0.01
        q = 1 - prob
        if b <= 0:
            return 0

        kelly_f = (prob * b - q) / b
        if kelly_f <= 0:
            return 0

        multiplier = KELLY_MULTIPLIER_AGGRESSIVE if is_aggressive_mode() else KELLY_MULTIPLIER
        risk_cap = MAX_RISK_CAP_AGGRESSIVE if is_aggressive_mode() else MAX_RISK_CAP
        raw_size = balance * kelly_f * multiplier
        max_size = balance * risk_cap
        return min(raw_size, max_size, balance * 0.95)


class RiskManager:
    def __init__(self):
        self.daily_start_balance = None
        self.last_reset_date = None
        self.realized_pnl_daily = 0.0
        self.max_daily_loss_pct = MAX_DAILY_LOSS_PCT
        self.is_halted = False
        self.halt_until = None

    def check_circuit_breaker(self, current_balance):
        now = datetime.now(timezone.utc)
        today = now.date()

        if self.last_reset_date != today:
            logger.info(f"New Trading Day: Setting reference balance to ${current_balance:.2f}")
            self.daily_start_balance = current_balance
            self.last_reset_date = today
            self.is_halted = False
            return False

        if self.is_halted:
            if now > self.halt_until:
                logger.info("🟢 Circuit Breaker Reset period over. Resuming...")
                self.is_halted = False
                self.daily_start_balance = current_balance
                return False
            return True

        if not self.daily_start_balance or self.daily_start_balance <= 0:
            return False

        drawdown = (self.daily_start_balance - current_balance) / self.daily_start_balance
        if drawdown >= self.max_daily_loss_pct:
            logger.error(f"🚨 CIRCUIT BREAKER TRIPPED! Drawdown: {drawdown:.1%}. Halting for {CIRCUIT_BREAKER_HALT_HOURS} hours.")
            self.is_halted = True
            self.halt_until = now + timedelta(hours=CIRCUIT_BREAKER_HALT_HOURS)
            return True

        return False

    def update_pnl(self, pnl_dollars):
        self.realized_pnl_daily += pnl_dollars
        logger.info(f"📈 PnL Sync: {pnl_dollars:+.2f}$ | Daily Total: {self.realized_pnl_daily:+.2f}$")
