import os
import csv
import json
import sqlite3
import logging
from datetime import datetime, timezone
from filelock import FileLock

from btc_15m.config import (
    DB_FILE, STATE_FILE, DATA_DIR,
    TRADES_CSV, SIGNALS_CSV, POSITIONS_JSON, SETTINGS_JSON,
    is_paper_trading,
)

logger = logging.getLogger("bot")

# Ensure data directory exists
os.makedirs(DATA_DIR, exist_ok=True)

# --- CSV Field Definitions ---
TRADES_FIELDS = ["timestamp", "token_id", "side", "price", "size", "roi", "is_paper", "outcome", "market_id"]
SIGNALS_FIELDS = ["timestamp", "price", "rsi", "atr", "score", "up", "down", "result"]


def _ensure_csv(filepath, fields):
    """Create CSV with header if it doesn't exist or has no header."""
    if not os.path.exists(filepath):
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
        return

    # Check if existing file has a header
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
        if first_line and first_line != ",".join(fields):
            # Header mismatch — might be old schema. Migrate columns.
            _migrate_csv_columns(filepath, fields)
    except Exception:
        pass


def _migrate_csv_columns(filepath, new_fields):
    """Add missing columns to an existing CSV file."""
    try:
        with open(filepath, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            existing_fields = reader.fieldnames or []
            rows = list(reader)

        # If existing fields are a subset, just add new columns
        missing = [f for f in new_fields if f not in existing_fields]
        if not missing:
            return

        logger.info(f"📦 Migrating CSV {os.path.basename(filepath)}: adding columns {missing}")
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=new_fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                # Fill missing columns with empty string
                for m in missing:
                    row.setdefault(m, "")
                writer.writerow(row)
    except Exception as e:
        logger.error(f"CSV migration failed for {filepath}: {e}")


def _append_csv(filepath, fields, row_dict):
    """Append a single row to a CSV file (thread-safe via file lock)."""
    _ensure_csv(filepath, fields)
    lock = FileLock(filepath + ".lock", timeout=5)
    with lock:
        with open(filepath, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writerow(row_dict)


def _read_csv(filepath, fields, limit=None):
    """Read all rows from CSV, newest first. Returns list of dicts."""
    _ensure_csv(filepath, fields)
    rows = []
    try:
        with open(filepath, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
    except Exception:
        return []
    rows.reverse()  # newest first
    if limit:
        rows = rows[:limit]
    return rows


def _read_json(filepath, default=None):
    """Read JSON file, return default if missing or corrupt."""
    if default is None:
        default = {}
    try:
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError):
        pass
    return default


def _write_json(filepath, data):
    """Atomically write JSON file."""
    lock = FileLock(filepath + ".lock", timeout=5)
    with lock:
        tmp = filepath + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, filepath)


class DatabaseManager:
    """Flat-file storage: CSV for trades/signals, JSON for positions/settings."""

    def __init__(self):
        _ensure_csv(TRADES_CSV, TRADES_FIELDS)
        _ensure_csv(SIGNALS_CSV, SIGNALS_FIELDS)

    # --- Positions (JSON) ---

    def _load_positions(self):
        return _read_json(POSITIONS_JSON, default=[])

    def _save_positions(self, positions):
        _write_json(POSITIONS_JSON, positions)

    def get_position(self, is_paper=False):
        paper_val = 1 if is_paper else 0
        for p in self._load_positions():
            if int(p.get("is_paper", 0)) == paper_val:
                return p
        return None

    def get_all_positions(self):
        return self._load_positions()

    def save_position(self, pos_data, is_paper=False):
        paper_val = 1 if is_paper else 0
        positions = self._load_positions()
        # Upsert: replace existing position with same paper mode
        positions = [p for p in positions if int(p.get("is_paper", 0)) != paper_val]
        pos_data["is_paper"] = paper_val
        positions.append(pos_data)
        self._save_positions(positions)

    def clear_position(self, is_paper=False):
        paper_val = 1 if is_paper else 0
        positions = self._load_positions()
        positions = [p for p in positions if int(p.get("is_paper", 0)) != paper_val]
        self._save_positions(positions)

    # --- Trades (CSV) ---

    def log_trade(self, token_id, side, price, size, roi=0.0, is_paper=False,
                  outcome="", market_id=""):
        paper_val = 1 if is_paper else 0
        _append_csv(TRADES_CSV, TRADES_FIELDS, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "token_id": token_id,
            "side": side,
            "price": price,
            "size": size,
            "roi": roi,
            "is_paper": paper_val,
            "outcome": outcome,
            "market_id": market_id,
        })

    def get_trades(self, limit=50):
        return _read_csv(TRADES_CSV, TRADES_FIELDS, limit=limit)

    def update_trade_outcome(self, token_id, outcome, side_filter="SELL"):
        """Retroactively set outcome (WIN/LOSS) on the most recent matching SELL trade."""
        try:
            lock = FileLock(TRADES_CSV + ".lock", timeout=5)
            with lock:
                with open(TRADES_CSV, "r", newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)

                updated = False
                for row in reversed(rows):
                    if (row.get("token_id") == token_id and
                            row.get("side", "").upper() == side_filter and
                            not row.get("outcome")):
                        row["outcome"] = outcome
                        updated = True
                        break

                if updated:
                    with open(TRADES_CSV, "w", newline="", encoding="utf-8") as f:
                        writer = csv.DictWriter(f, fieldnames=TRADES_FIELDS)
                        writer.writeheader()
                        writer.writerows(rows)
                    logger.info(f"✅ Trade outcome updated: {token_id[:20]}... → {outcome}")
        except Exception as e:
            logger.error(f"Failed to update trade outcome: {e}")

    def get_pending_outcomes(self):
        """Get SELL trades that don't have an outcome yet."""
        trades = _read_csv(TRADES_CSV, TRADES_FIELDS)
        pending = []
        for t in trades:
            if t.get("side", "").upper() == "SELL" and not t.get("outcome"):
                market_id = t.get("market_id", "")
                if market_id:
                    pending.append(t)
        return pending

    # --- Settings (JSON) ---

    def get_setting(self, key, default=None):
        settings = _read_json(SETTINGS_JSON, default={})
        return settings.get(key, default)

    def save_setting(self, key, value):
        settings = _read_json(SETTINGS_JSON, default={})
        settings[key] = str(value)
        _write_json(SETTINGS_JSON, settings)

    # --- Signals (CSV) ---

    def log_signal(self, price, rsi, atr, score, up, down, result):
        _append_csv(SIGNALS_CSV, SIGNALS_FIELDS, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "price": price,
            "rsi": rsi,
            "atr": atr,
            "score": score,
            "up": up,
            "down": down,
            "result": result,
        })

    def get_signals(self, limit=100):
        return _read_csv(SIGNALS_CSV, SIGNALS_FIELDS, limit=limit)


# Global singleton
db = DatabaseManager()


class StateManager:
    @staticmethod
    def load():
        pos = db.get_position(is_paper=is_paper_trading())
        if pos:
            return {"current_position": pos}
        return {}

    @staticmethod
    def save(state):
        is_paper = is_paper_trading()
        pos = state.get("current_position")
        if pos:
            db.save_position(pos, is_paper=is_paper)
        else:
            db.clear_position(is_paper=is_paper)

    @staticmethod
    def update_position(token_id, price, size, side="BUY", prediction="UP",
                        tp_order_id=None, market_id=""):
        is_paper = is_paper_trading()
        if side == "BUY":
            pos_data = {
                "token_id": token_id,
                "entry_price": float(price),
                "size": float(size),
                "prediction": prediction,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "highest_roi": 0.0,
                "tp_order_id": tp_order_id,
                "market_id": market_id,
            }
            db.save_position(pos_data, is_paper=is_paper)
            db.log_trade(token_id, "BUY", price, size, is_paper=is_paper,
                         market_id=market_id)
        else:
            pos = db.get_position(is_paper=is_paper)
            realized_pnl = 0.0
            roi = 0.0
            stored_market_id = ""
            if pos:
                entry = float(pos.get("entry_price", 0))
                sell_price = float(price)
                stored_market_id = pos.get("market_id", market_id)
                if entry > 0:
                    roi = (sell_price - entry) / entry * 100
                    realized_pnl = (sell_price - entry) * float(pos.get("size", 0))

            # Determine outcome from PnL
            outcome = "WIN" if realized_pnl > 0 else ("LOSS" if realized_pnl < 0 else "FLAT")

            db.clear_position(is_paper=is_paper)
            db.log_trade(token_id, "SELL", price, size, roi=realized_pnl, is_paper=is_paper,
                         outcome=outcome, market_id=stored_market_id)
            return realized_pnl

    @staticmethod
    def migrate_from_sqlite():
        """Migrate data from legacy SQLite DB to flat files."""
        if not os.path.exists(DB_FILE):
            return

        logger.info("📦 Migrating from SQLite to CSV/JSON...")
        try:
            conn = sqlite3.connect(DB_FILE)
            conn.row_factory = sqlite3.Row

            # Migrate positions
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM positions")
            positions = [dict(row) for row in cursor.fetchall()]
            if positions:
                _write_json(POSITIONS_JSON, positions)
                logger.info(f"  ✅ Migrated {len(positions)} positions")

            # Migrate trades
            cursor.execute("SELECT * FROM trades ORDER BY id ASC")
            trades = [dict(row) for row in cursor.fetchall()]
            if trades:
                _ensure_csv(TRADES_CSV, TRADES_FIELDS)
                with open(TRADES_CSV, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=TRADES_FIELDS, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(trades)
                logger.info(f"  ✅ Migrated {len(trades)} trades")

            # Migrate signals
            cursor.execute("SELECT * FROM signals ORDER BY id ASC")
            signals = [dict(row) for row in cursor.fetchall()]
            if signals:
                _ensure_csv(SIGNALS_CSV, SIGNALS_FIELDS)
                with open(SIGNALS_CSV, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=SIGNALS_FIELDS, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(signals)
                logger.info(f"  ✅ Migrated {len(signals)} signals")

            # Migrate settings
            cursor.execute("SELECT * FROM settings")
            settings = {row["key"]: row["value"] for row in cursor.fetchall()}
            if settings:
                _write_json(SETTINGS_JSON, settings)
                logger.info(f"  ✅ Migrated {len(settings)} settings")

            conn.close()
            os.rename(DB_FILE, DB_FILE + ".migrated")
            logger.info("✅ SQLite migration complete. Old DB renamed to .migrated")
        except Exception as e:
            logger.error(f"Migration failed: {e}")

    @staticmethod
    def migrate_from_json():
        """Migrate from legacy bot_state_async.json."""
        if not os.path.exists(STATE_FILE):
            return
        logger.info("📦 Detecting legacy JSON state. Migrating...")
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
            pos = data.get("current_position")
            if pos:
                db.save_position(pos)
            os.rename(STATE_FILE, STATE_FILE + ".bak")
            logger.info("✅ JSON state migration successful.")
        except Exception as e:
            logger.error(f"Migration failed: {e}")


# Run migrations on import
StateManager.migrate_from_sqlite()
StateManager.migrate_from_json()
