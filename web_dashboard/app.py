import csv
import os
import json
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="Polymarket Bot Dashboard")

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_DIR = os.path.dirname(BASE_DIR)
DATA_DIR = os.path.join(BOT_DIR, "data")
TRADES_CSV = os.path.join(DATA_DIR, "trades.csv")
SIGNALS_CSV = os.path.join(DATA_DIR, "signals.csv")
POSITIONS_JSON = os.path.join(DATA_DIR, "positions.json")
STATE_FILE = os.path.join(BOT_DIR, "dashboard_state.json")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

templates = Jinja2Templates(directory=TEMPLATES_DIR)


# --- File Helpers ---

def read_csv(filepath, limit=None):
    """Read CSV file, return list of dicts (newest first)."""
    try:
        if not os.path.exists(filepath):
            return []
        with open(filepath, "r", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        rows.reverse()
        if limit:
            rows = rows[:limit]
        return rows
    except Exception:
        return []


def read_json(filepath, default=None):
    """Read JSON file, return default if missing/corrupt."""
    if default is None:
        default = {}
    try:
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError):
        pass
    return default


def read_live_state():
    return read_json(STATE_FILE)


# --- Pages ---

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# --- API Endpoints ---

@app.get("/api/stats")
async def get_stats():
    trades = read_csv(TRADES_CSV)
    total_pnl = sum(float(t.get("roi", 0) or 0) for t in trades)
    trade_count = len(trades)

    # Count wins from outcome field (preferred) or ROI fallback
    wins = 0
    losses = 0
    for t in trades:
        outcome = t.get("outcome", "").upper()
        if outcome == "WIN":
            wins += 1
        elif outcome == "LOSS":
            losses += 1
        elif outcome == "" and t.get("side", "").upper() == "SELL":
            # Fallback: use ROI if no explicit outcome
            if float(t.get("roi", 0) or 0) > 0:
                wins += 1
            elif float(t.get("roi", 0) or 0) < 0:
                losses += 1

    decided = wins + losses
    win_rate = (wins / decided * 100) if decided > 0 else 0.0

    positions = read_json(POSITIONS_JSON, default=[])
    if isinstance(positions, dict):
        positions = [positions] if positions else []
    active_positions = len(positions)
    total_value = sum(
        float(p.get("size", 0) or 0) * float(p.get("entry_price", 0) or 0)
        for p in positions
    )

    return {
        "total_pnl": round(total_pnl, 2),
        "trade_count": trade_count,
        "win_rate": round(win_rate, 1),
        "wins": wins,
        "losses": losses,
        "active_positions": active_positions,
        "total_position_value": round(total_value, 2),
    }


@app.get("/api/live_state")
async def get_live_state():
    state = read_live_state()
    if not state:
        return {
            "btc_price": 0, "market_question": "-", "time_left": "-",
            "price_to_beat": 0, "predict_label": "NEUTRAL", "predict_conf": 0,
            "rsi_val": 0, "macd_label": "-", "ha_label": "-", "vwap_val": 0,
            "poly_up": 0, "poly_down": 0, "delta_1m": 0, "delta_3m": 0,
            "impulse": 0, "rsi_arrow": "-", "binance_price": 0,
        }
    return state


@app.get("/api/trades")
async def get_trades(limit: int = 50):
    return read_csv(TRADES_CSV, limit=limit)


@app.get("/api/positions")
async def get_positions():
    positions = read_json(POSITIONS_JSON, default=[])
    if isinstance(positions, dict):
        positions = [positions] if positions else []
    return positions


@app.get("/api/signals")
async def get_signals(limit: int = 100):
    rows = read_csv(SIGNALS_CSV, limit=limit)
    return rows[::-1]  # chronological order for charts


@app.get("/api/activity")
async def get_activity(limit: int = 30):
    entries = []

    for r in read_csv(SIGNALS_CSV, limit=limit):
        label = r.get("result", "NEUTRAL")
        score = float(r.get("score", 0) or 0)
        price = float(r.get("price", 0) or 0)
        entries.append({
            "time": r.get("timestamp", ""),
            "message": f"Signal: {label} (score {score:.2f}) @ ${price:,.0f}",
            "type": "signal",
        })

    for r in read_csv(TRADES_CSV, limit=limit):
        roi = float(r.get("roi", 0) or 0)
        price = float(r.get("price", 0) or 0)
        size = float(r.get("size", 0) or 0)
        outcome = r.get("outcome", "")
        roi_str = f" → PnL: ${roi:.2f}" if roi else ""
        outcome_badge = f" [{outcome}]" if outcome else ""
        entries.append({
            "time": r.get("timestamp", ""),
            "message": f"Trade: {r.get('side', '?')} {size:.1f} shares @ ${price:.2f}{roi_str}{outcome_badge}",
            "type": "trade",
            "outcome": outcome,
        })

    entries.sort(key=lambda x: x["time"] or "", reverse=True)
    return entries[:limit]


if __name__ == "__main__":
    import uvicorn
    print(f"Data Dir: {DATA_DIR}")
    print(f"Live State: {STATE_FILE}")
    uvicorn.run(app, host="127.0.0.1", port=8000)
