import json
import logging

from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.align import Align

from btc_15m.config import is_paper_trading, DASHBOARD_STATE_FILE


class DashboardLogHandler(logging.Handler):
    def __init__(self, buffer_size=10):
        super().__init__()
        self.buffer = []
        self.max_len = buffer_size

    def emit(self, record):
        try:
            msg = self.format(record)
            Dashboard.logs.append(msg)
            if len(Dashboard.logs) > self.max_len:
                Dashboard.logs.pop(0)
        except Exception:
            self.handleError(record)


class Dashboard:
    console = Console()
    layout = Layout()
    logs = []

    market_question = "-"
    time_left = "-"

    predict_label = "NEUTRAL"
    predict_conf = 0.0

    ha_label = "-"
    rsi_val = 0.0
    rsi_arrow = "-"
    macd_label = "-"

    delta_1m = 0.0
    delta_3m = 0.0

    vwap_val = 0.0
    vwap_dist = 0.0
    vwap_slope = "-"

    poly_up = 0.0
    poly_down = 0.0
    liquidity = 0
    price_to_beat = 0.0

    btc_price = 0.0
    binance_price = 0.0

    impulse = 0.0
    entry_blocked = False
    minutes_to_expiry = 0.0

    @staticmethod
    def render(layout=None):
        if layout is None:
            layout = Dashboard.layout

        if not layout.children:
            layout.update(Panel(Align.center("Initialize..."), title="Status"))

        mode_text = "[bold yellow]PAPER[/]" if is_paper_trading() else "[bold green]LIVE[/]"
        btc_text = f"BTC: [bold white]${Dashboard.btc_price:,.0f}[/]"

        market_text = f"Market: [cyan]{Dashboard.market_question[:30]}...[/]"
        strike_text = f"Strike: [bold white]${Dashboard.price_to_beat:,.0f}[/]"
        time_text = f"Exp: [bold cyan]{Dashboard.time_left}[/]"

        sig_color = "green" if "BULL" in Dashboard.predict_label else ("red" if "BEAR" in Dashboard.predict_label else "white")
        signal_text = f"Signal: [{sig_color}]{Dashboard.predict_label}[/] ({Dashboard.predict_conf:.0f}%)"

        status_content = Table.grid(padding=(0, 2))
        status_content.add_column(justify="center")

        row1 = f"{mode_text} | {btc_text} | {market_text}"
        row2 = f"{strike_text} | {time_text} | {signal_text}"

        status_content.add_row(Align.center(row1))
        status_content.add_row(Align.center(row2))

        layout.update(Panel(status_content, title="Polymarket BTC 15m (Minimal)", border_style="blue"))

        return layout

    @staticmethod
    def export_state():
        """Write dashboard state to JSON for the web dashboard bridge."""
        state = {
            "btc_price": Dashboard.btc_price,
            "binance_price": Dashboard.binance_price,
            "market_question": Dashboard.market_question,
            "time_left": Dashboard.time_left,
            "price_to_beat": Dashboard.price_to_beat,
            "predict_label": Dashboard.predict_label,
            "predict_conf": Dashboard.predict_conf,
            "rsi_val": Dashboard.rsi_val,
            "rsi_arrow": Dashboard.rsi_arrow,
            "macd_label": Dashboard.macd_label,
            "ha_label": Dashboard.ha_label,
            "vwap_val": Dashboard.vwap_val,
            "delta_1m": Dashboard.delta_1m,
            "delta_3m": Dashboard.delta_3m,
            "poly_up": Dashboard.poly_up,
            "poly_down": Dashboard.poly_down,
            "impulse": Dashboard.impulse,
            "entry_blocked": Dashboard.entry_blocked,
            "minutes_to_expiry": Dashboard.minutes_to_expiry,
        }
        try:
            with open(DASHBOARD_STATE_FILE, "w") as f:
                json.dump(state, f)
        except Exception:
            pass
