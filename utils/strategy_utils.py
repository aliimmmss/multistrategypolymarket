import math
from datetime import datetime, timezone

class Indicators:
    @staticmethod
    def sma(data, period):
        if len(data) < period: return None
        return sum(data[-period:]) / period

    @staticmethod
    def calculate_rsi(closes, period=14):
        if len(closes) < period + 1: return None
        
        gains = []
        losses = []
        
        # 1. Initial SMA RS
        for i in range(1, period + 1):
            change = closes[i] - closes[i-1]
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))
        
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        
        if avg_loss == 0: return 100
        
        # 2. Wilder's Smoothing
        current_avg_gain = avg_gain
        current_avg_loss = avg_loss
        
        for i in range(period + 1, len(closes)):
            change = closes[i] - closes[i-1]
            gain = change if change > 0 else 0
            loss = abs(change) if change < 0 else 0
            
            current_avg_gain = (current_avg_gain * (period - 1) + gain) / period
            current_avg_loss = (current_avg_loss * (period - 1) + loss) / period
        
        if current_avg_loss == 0: return 100
        rs = current_avg_gain / current_avg_loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def calculate_macd(closes, fast=12, slow=26, sign=9):
        if len(closes) < slow + sign: return None
        
        def get_ema_series(values, length):
            k = 2 / (length + 1)
            emas = []
            seed = sum(values[:length]) / length
            emas.append(seed)
            curr = seed
            for val in values[length:]:
                curr = (val * k) + (curr * (1 - k))
                emas.append(curr)
            return emas

        ema12 = get_ema_series(closes, fast)
        ema26 = get_ema_series(closes, slow)
        
        # Align series
        overlap_len = min(len(ema12), len(ema26))
        macd_line = []
        
        # Walk backwards to align
        for i in range(1, overlap_len + 1):
            val_slow = ema26[-i]
            val_fast = ema12[-i] 
            macd_line.insert(0, val_fast - val_slow)
            
        if len(macd_line) < sign: return None
        signal_line = get_ema_series(macd_line, sign)
        
        last_macd = macd_line[-1]
        last_signal = signal_line[-1]
        prev_macd = macd_line[-2]
        prev_signal = signal_line[-2]
        
        hist = last_macd - last_signal
        prev_hist = prev_macd - prev_signal
        
        return {
            "macd": last_macd, 
            "signal": last_signal, 
            "hist": hist, 
            "hist_delta": hist - prev_hist
        }

    @staticmethod
    def calculate_atr(candles, period=14):
        # candles: [ts, o, h, l, c, v]
        if len(candles) < period + 1: return None
        
        tr_list = []
        for i in range(1, len(candles)):
            curr = candles[i]
            prev = candles[i-1]
            
            h = float(curr[2])
            l = float(curr[3])
            cp = float(prev[4])
            
            tr1 = h - l
            tr2 = abs(h - cp)
            tr3 = abs(l - cp)
            tr = max(tr1, tr2, tr3)
            tr_list.append(tr)
            
        if len(tr_list) < period: return None
        
        current_atr = sum(tr_list[:period]) / period
        for i in range(period, len(tr_list)):
            tr = tr_list[i]
            current_atr = ((current_atr * (period - 1)) + tr) / period
            
        return current_atr

    @staticmethod
    def calculate_vwap_intraday(candles):
        # candles: [ts, o, h, l, c, v]
        if not candles: return []
        
        start_index = 0
        last_ts = float(candles[-1][0])
        # Handle ms timestamp
        try:
            last_dt = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc)
        except:
             # Fallback for seconds
             last_dt = datetime.fromtimestamp(last_ts, tz=timezone.utc)

        day_start_ts = last_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
        
        for i in range(len(candles)-1, -1, -1):
            if float(candles[i][0]) < day_start_ts:
                start_index = i + 1
                break
                
        pv_sum = 0
        v_sum = 0
        vwap_series = []
        for i in range(start_index, len(candles)):
            c = candles[i]
            close = float(c[4])
            vol = float(c[5])
            tp = (float(c[2]) + float(c[3]) + close) / 3
            pv_sum += tp * vol
            v_sum += vol
            vwap_series.append(pv_sum / v_sum if v_sum else None)
            
        return vwap_series

    @staticmethod
    def calculate_heiken_ashi(candles):
        ha_candles = []
        c0 = candles[0]
        o0, h0, l0, cl0 = float(c0[1]), float(c0[2]), float(c0[3]), float(c0[4])
        
        ha_open = (o0 + cl0) / 2
        ha_close = (o0 + h0 + l0 + cl0) / 4
        ha_candles.append({'open': ha_open, 'close': ha_close, 'is_green': ha_close >= ha_open})
        
        for i in range(1, len(candles)):
            c = candles[i]
            curr_o, curr_h, curr_l, curr_c = float(c[1]), float(c[2]), float(c[3]), float(c[4])
            prev_ha = ha_candles[-1]
            
            ha_close = (curr_o + curr_h + curr_l + curr_c) / 4
            ha_open = (prev_ha['open'] + prev_ha['close']) / 2 
            
            ha_candles.append({'open': ha_open, 'close': ha_close, 'is_green': ha_close >= ha_open})
            
        last = ha_candles[-1]
        target = last['is_green']
        count = 0
        for i in range(len(ha_candles)-1, -1, -1):
            if ha_candles[i]['is_green'] == target: count += 1
            else: break
        
        return {'color': 'green' if target else 'red', 'count': count}

    @staticmethod
    def calculate_realized_volatility(closes, window=20):
        if len(closes) < window + 1: return 0.5
        returns = []
        for i in range(len(closes) - window, len(closes)):
            if closes[i-1] > 0:
                returns.append(math.log(closes[i] / closes[i-1]))
        if not returns: return 0.5
        std = (sum((r - sum(returns)/len(returns))**2 for r in returns) / len(returns)) ** 0.5
        return std * math.sqrt(4 * 24 * 365) # Annualize

    @staticmethod
    def calculate_weighted_obi(ob, mid_price):
        if not ob.bids or not ob.asks: return 0, 0
        
        def weight(price):
            dist = abs(mid_price - float(price))
            return 1 / (1 + dist)

        weighted_bids = sum(float(b.size) * weight(b.price) for b in ob.bids[:5])
        weighted_asks = sum(float(a.size) * weight(a.price) for a in ob.asks[:5])
        return weighted_bids, weighted_asks


class BayesianPredictor:
    """
    Implements a recursive Bayesian update for market direction.
    Uses log-odds for numerical stability.
    """
    def __init__(self, prior_prob=0.5):
        self.prior_log_odds = math.log(prior_prob / (1 - prior_prob))
        self.evidence_log_odds = 0.0
        self.components = {}

    def reset(self):
        self.evidence_log_odds = 0.0
        self.components = {}

    def add_evidence(self, name, bayes_factor):
        """
        Update belief with new evidence.
        Bayes Factor > 1.0 supports Hypothesis (UP)
        Bayes Factor < 1.0 supports Null (DOWN)
        """
        if bayes_factor <= 0: return # Invalid
        
        lo = math.log(bayes_factor)
        self.evidence_log_odds += lo
        self.components[name] = bayes_factor

    def get_probability(self):
        total_log_odds = self.prior_log_odds + self.evidence_log_odds
        try:
            odds = math.exp(total_log_odds)
            prob = odds / (1 + odds)
            return prob
        except OverflowError:
            return 1.0 if total_log_odds > 0 else 0.0

    def get_components(self):
        return self.components

    # --- Configuration Constants ---
    # Bayes Factors (BF > 1.0 = Support UP, BF < 1.0 = Support DOWN)
    BF_TREND_STRONG = 2.0
    BF_TREND_WEAK = 0.5
    BF_SLOPE_UP = 1.5
    BF_SLOPE_DOWN = 0.66
    
    BF_MOMENTUM_STRONG = 2.0
    BF_MOMENTUM_WEAK = 0.5
    
    BF_MACD_EXP_UP = 2.0
    BF_MACD_EXP_DOWN = 0.5
    BF_MACD_TREND_UP = 1.2
    BF_MACD_TREND_DOWN = 0.83
    
    BF_HA_TREND_UP = 1.5
    BF_HA_TREND_DOWN = 0.66
    
    BF_MONEYNESS_ITM = 1.5
    BF_MONEYNESS_OTM = 0.66
    
    BF_CROWD_BULL = 2.0
    BF_CROWD_BEAR = 0.5
    
    BF_OBI_BULL = 1.5
    BF_OBI_BEAR = 0.66
    
    BF_SQUEEZE_LONG = 0.66
    BF_SQUEEZE_SHORT = 1.5
    
    BF_LATENCY_ARB_UP = 4.0
    BF_LATENCY_ARB_DOWN = 0.25
    
    BF_CANDLE_TREND_UP = 1.5
    BF_CANDLE_TREND_DOWN = 0.66

    # Thresholds
    TH_RSI_HIGH = 55
    TH_RSI_LOW = 45
    
    TH_POLY_SPREAD = 0.05
    TH_POLY_PRICE_HIGH = 0.60
    TH_POLY_PRICE_LOW = 0.40
    
    TH_OBI_RATIO = 0.3
    
    TH_FUNDING_HIGH = 0.01
    TH_FUNDING_LOW = -0.01
    
    TH_TREND_CHANGE = 0.005

    @staticmethod
    def calculate_bayes_score(
        price, vwap, vwap_slope, 
        rsi, rsi_slope, 
        macd, 
        ha_color, ha_count, 
        poly_price=None, poly_spread=None,
        vol_up=0, vol_down=0, 
        funding_rate=0.0, 
        latency_score=0, 
        moneyness=None, 
        time_decay=1.0, 
        fear_greed=50, 
        last_close=None
    ):
        """
        Wrapper to convert market data into specific Bayes Factors
        and return the final probability.
        """
        predictor = BayesianPredictor()
        
        # Access constants via class
        BP = BayesianPredictor

        # 1. Price vs VWAP (Trend)
        if price and vwap:
            if price > vwap: predictor.add_evidence("vwap_trend", BP.BF_TREND_STRONG)
            elif price < vwap: predictor.add_evidence("vwap_trend", BP.BF_TREND_WEAK)
        
        if vwap_slope > 0: predictor.add_evidence("vwap_slope", BP.BF_SLOPE_UP)
        elif vwap_slope < 0: predictor.add_evidence("vwap_slope", BP.BF_SLOPE_DOWN)

        # 2. RSI (Momentum)
        if rsi and rsi_slope:
            if rsi > BP.TH_RSI_HIGH and rsi_slope > 0: predictor.add_evidence("rsi_mom", BP.BF_MOMENTUM_STRONG)
            elif rsi < BP.TH_RSI_LOW and rsi_slope < 0: predictor.add_evidence("rsi_mom", BP.BF_MOMENTUM_WEAK)
        
        # 3. MACD (Momentum)
        if macd:
            if macd['hist'] > 0 and macd['hist_delta'] > 0: predictor.add_evidence("macd_exp", BP.BF_MACD_EXP_UP)
            elif macd['hist'] < 0 and macd['hist_delta'] < 0: predictor.add_evidence("macd_exp", BP.BF_MACD_EXP_DOWN)
            
            if macd['macd'] > 0: predictor.add_evidence("macd_trend", BP.BF_MACD_TREND_UP)
            elif macd['macd'] < 0: predictor.add_evidence("macd_trend", BP.BF_MACD_TREND_DOWN)

        # 4. Heiken Ashi (Trend Consistency)
        if ha_color == 'green' and ha_count >= 2: predictor.add_evidence("ha_trend", BP.BF_HA_TREND_UP)
        elif ha_color == 'red' and ha_count >= 2: predictor.add_evidence("ha_trend", BP.BF_HA_TREND_DOWN)

        # 5. Moneyness (Strike Bias)
        if moneyness is not None:
            # Time Decay amplification
            decay_mult = time_decay if time_decay >= 1.0 else 1.0
            
            if moneyness > 0: # ITM for UP
                # If ITM + Late expiry -> Strong confidence it stays ITM
                bf = BP.BF_MONEYNESS_ITM * decay_mult
                predictor.add_evidence("moneyness", bf)
            elif moneyness < 0: # OTM for UP (ITM for DOWN)
                bf = BP.BF_MONEYNESS_OTM / decay_mult
                predictor.add_evidence("moneyness", bf)

        # 6. Polymarket Wisdom (Crowd)
        if poly_price and poly_spread and poly_spread < BP.TH_POLY_SPREAD:
            if poly_price > BP.TH_POLY_PRICE_HIGH: predictor.add_evidence("poly_crowd", BP.BF_CROWD_BULL)
            elif poly_price < BP.TH_POLY_PRICE_LOW: predictor.add_evidence("poly_crowd", BP.BF_CROWD_BEAR)

        # 7. OBI (Order Flow)
        if vol_up > 0 and vol_down > 0:
            obi = (vol_up - vol_down) / (vol_up + vol_down)
            if obi > BP.TH_OBI_RATIO: predictor.add_evidence("obi", BP.BF_OBI_BULL)
            elif obi < -BP.TH_OBI_RATIO: predictor.add_evidence("obi", BP.BF_OBI_BEAR)

        # 8. Funding Rate (Squeeze)
        if funding_rate < BP.TH_FUNDING_LOW: predictor.add_evidence("funding_squeeze", BP.BF_SQUEEZE_SHORT) # Short Squeeze likely
        elif funding_rate > BP.TH_FUNDING_HIGH: predictor.add_evidence("funding_squeeze", BP.BF_SQUEEZE_LONG) # Long Squeeze likely

        # 9. Latency Arb (Oracle) - STRONGEST SIGNAL
        if latency_score > 0: predictor.add_evidence("latency_arb", BP.BF_LATENCY_ARB_UP) # Coinbase leading UP
        elif latency_score < 0: predictor.add_evidence("latency_arb", BP.BF_LATENCY_ARB_DOWN) # Coinbase leading DOWN
        
        # 10. Trend Continuation
        if last_close and price:
            change = (price - last_close) / last_close
            if change > BP.TH_TREND_CHANGE: predictor.add_evidence("candle_trend", BP.BF_CANDLE_TREND_UP)
            elif change < -BP.TH_TREND_CHANGE: predictor.add_evidence("candle_trend", BP.BF_CANDLE_TREND_DOWN)

        return predictor.get_probability(), predictor
