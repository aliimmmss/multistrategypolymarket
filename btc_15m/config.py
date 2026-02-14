import os
import sys
from dotenv import load_dotenv

# Add parent directory to sys.path for absolute imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load environment variables from root
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

# --- API Endpoints ---
BINANCE_WSS = "wss://stream.binance.com:9443/ws/btcusdt@kline_15m"
BINANCE_WSS_1M = "wss://stream.binance.com:9443/ws/btcusdt@kline_1m"
BINANCE_REST = "https://api.binance.com/api/v3/klines"
BINANCE_FUTURES_REST = "https://fapi.binance.com/fapi/v1/premiumIndex"
COINBASE_WSS = "wss://ws-feed.exchange.coinbase.com"
POLY_MARKET_URL = "https://clob.polymarket.com"
GAMMA_API_URL = "https://gamma-api.polymarket.com/events"
CHAINLINK_BTC_USD = "https://data.chain.link/streams/btc-usd"
FEAR_GREED_API = "https://api.alternative.me/fng/"

# --- Series / Market IDs ---
BTC_15M_SERIES_ID = "10192"

# --- File Paths ---
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(ROOT_DIR, "bot_state_async.json")
DB_FILE = os.path.join(ROOT_DIR, "bot_state.db")  # legacy, kept for migration
DASHBOARD_STATE_FILE = os.path.join(ROOT_DIR, "dashboard_state.json")

# --- New flat-file storage ---
DATA_DIR = os.path.join(ROOT_DIR, "data")
TRADES_CSV = os.path.join(DATA_DIR, "trades.csv")
SIGNALS_CSV = os.path.join(DATA_DIR, "signals.csv")
POSITIONS_JSON = os.path.join(DATA_DIR, "positions.json")
SETTINGS_JSON = os.path.join(DATA_DIR, "settings.json")

# --- Trading Constants ---
POLYGON_CHAIN_ID = 137
USDC_DECIMALS = 1_000_000
MIN_TRADE_USD = 1.0
MIN_BET_FALLBACK_USD = 5.0
MAX_SHARES_CAP = 100.0
MAX_BUY_PRICE = 0.98
MAX_BUY_PRICE_AGGRESSIVE = 0.99
LIMIT_PRICE_OFFSET = 0.02
SELL_PRICE_DUMP_OFFSET = 0.05
MIN_SELL_BALANCE = 0.1
DUST_FILTER = 0.1
COOLDOWN_SECONDS = 60
POSITION_GRACE_PERIOD = 180  # 3 mins

# --- Kelly Criterion Defaults ---
KELLY_MULTIPLIER = 0.15
KELLY_MULTIPLIER_AGGRESSIVE = 0.4
MAX_RISK_CAP = 0.25
MAX_RISK_CAP_AGGRESSIVE = 0.60

# --- Risk Management ---
MAX_DAILY_LOSS_PCT = 0.10
CIRCUIT_BREAKER_HALT_HOURS = 6
HARD_STOP_LOSS_PCT = -15.0
TRAIL_ACTIVATION_PCT = 5.0
TRAIL_MIN_DIST_PCT = 10.0
TRAIL_MAX_DIST_PCT = 20.0
ATR_TRAIL_MULTIPLIER = 3.0

# --- Market Discovery ---
MIN_TIME_TO_EXPIRY_MINS = 2
MAX_MARKET_DURATION_MINS = 60
POLY_PRICE_FLOOR = 0.10
POLY_PRICE_CEILING = 0.90
MAX_SPREAD = 0.05

# --- Scoring Thresholds ---
BUY_SCORE_THRESHOLD = 0.78
SHORT_SCORE_THRESHOLD = 0.30
HEDGE_DUMP_LONG_THRESHOLD = 0.2
HEDGE_DUMP_SHORT_THRESHOLD = 0.8

# --- Strategy Intervals ---
STRATEGY_INTERVAL_SECONDS = 30
TSL_INTERVAL_ACTIVE = 1      # seconds, when holding position
TSL_INTERVAL_IDLE = 10       # seconds, when flat
HISTORY_CANDLES = 200
WARMUP_CANDLES = 50

# --- Valuation ---
LOW_LIQUIDITY_PRICE_FLOOR = 0.05
LOW_LIQUIDITY_AGE_SECONDS = 300
SAFE_MIDPOINT_FALLBACK = 0.50

# --- TP Order Defaults ---
TP_PRICE_HIGH = 0.95  # TP when entry >= 0.88
TP_PRICE_LOW = 0.90   # TP when entry < 0.88
TP_ENTRY_THRESHOLD = 0.88

# --- Latency Arb ---
LATENCY_DIVERGENCE_PCT = 0.05

# --- Environment Helpers ---
def is_paper_trading():
    return os.getenv("PAPER_TRADING", "false").lower() == "true"

def is_aggressive_mode():
    return os.getenv("AGGRESSIVE_MODE", "false").lower() == "true"

def get_paper_balance():
    return float(os.getenv("PAPER_BALANCE", "1000.0"))

def use_flashbots():
    return os.getenv("USE_FLASHBOTS", "false").lower() == "true"
