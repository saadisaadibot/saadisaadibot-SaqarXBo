# -*- coding: utf-8 -*-
# strategy_base.py — ثابت: مؤشرات + نظام اختيار TP + مطاردة شراء + مساعدات

import time, statistics as st

# إعدادات مؤشرات وحدود TP الافتراضية (قيم عملية وثابتة غالباً)
ADX_LEN = 14; RSI_LEN = 14; EMA_FAST = 50; EMA_SLOW = 200; ATR_LEN = 14
TP_MIN_PCT = 0.30; TP_MID_PCT = 0.70; TP_MAX_PCT = 1.20

# ===== مؤشرات =====
def _series_ema(values, period):
    if len(values) < period: return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def _rsi(closes, period=14):
    if len(closes) < period+1: return None
    gains = 0.0; losses = 0.0
    for i in range(-period, 0):
        d = closes[i] - closes[i-1]
        if d >= 0: gains += d
        else: losses += -d
    avg_gain = gains / period
    avg_loss = (losses / period) if losses > 0 else 1e-9
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def _atr(highs, lows, closes, period=14):
    n = len(closes)
    if n < period+1: return None
    trs = []
    for i in range(1, n):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr*(period-1) + tr) / period
    return atr

def _adx(highs, lows, closes, period=14):
    n = len(closes)
    if n < period+1: return None
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, n):
        up = highs[i] - highs[i-1]
        dn = lows[i-1] - lows[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        tr_list.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    tr14 = sum(tr_list[:period]); pDM14 = sum(plus_dm[:period]); mDM14 = sum(minus_dm[:period])
    for i in range(period, len(tr_list)):
        tr14  = tr14  - (tr14 / period)  + tr_list[i]
        pDM14 = pDM14 - (pDM14 / period) + plus_dm[i]
        mDM14 = mDM14 - (mDM14 / period) + minus_dm[i]
    if tr14 <= 0: return 0.0
    pDI = (pDM14 / tr14) * 100.0
    mDI = (mDM14 / tr14) * 100.0
    dx = (abs(pDI - mDI) / max(pDI + mDI, 1e-9)) * 100.0
    return dx

# ===== سلوك السوق + اختيار TP =====
def _fetch_candles(core, market: str, interval="1m", limit=240):
    data = core.bv_request("GET", f"/{market}/candles?interval={interval}&limit={limit}")
    if not isinstance(data, list): return [], [], []
    highs = [float(r[2]) for r in data]
    lows  = [float(r[3]) for r in data]
    closes= [float(r[4]) for r in data]
    return highs, lows, closes

def market_regime(core, market: str):
    highs, lows, closes = _fetch_candles(core, market, "1m", 240)
    if len(closes) < max(EMA_SLOW+5, ATR_LEN+5):
        return {"ok": False}
    ema_fast = _series_ema(closes, EMA_FAST)[-1]
    ema_slow = _series_ema(closes, EMA_SLOW)[-1]
    trend_up = ema_fast > ema_slow
    rsi = _rsi(closes, RSI_LEN) or 50.0
    atr = _atr(highs, lows, closes, ATR_LEN) or 0.0
    last_close = closes[-1]
    atr_pct = (atr / last_close) if (atr and last_close>0) else 0.0
    adx_val = _adx(highs, lows, closes, ADX_LEN) or 0.0
    return {"ok": True, "trend_up": trend_up, "rsi": rsi, "atr_pct": atr_pct, "adx": adx_val}

def choose_tp_price(core, market: str, avg_price: float) -> tuple[float, dict]:
    reg = market_regime(core, market)
    if not reg.get("ok"):
        pct = TP_MID_PCT
    else:
        adx, rsi, atr_pct, trend_up = reg["adx"], reg["rsi"], reg["atr_pct"], reg["trend_up"]
        if adx < 18: base_pct = 0.40
        elif adx < 22: base_pct = 0.55 if trend_up else 0.45
        elif adx < 28: base_pct = 0.85 if trend_up else 0.60
        else: base_pct = 1.05 if trend_up else 0.70
        if rsi >= 75: base_pct = min(base_pct, 0.70)
        elif rsi <= 40: base_pct = max(base_pct, 0.60 if trend_up else 0.40)
        pct = max(TP_MIN_PCT, min(base_pct + min(0.30, (atr_pct*1000)*0.06), TP_MAX_PCT))
    return avg_price * (1.0 + pct/100.0), {"tp_pct": pct, **(reg if reg.get("ok") else {})}

# ===== مطاردة شراء سريعة =====
def abort_requested(core, market: str) -> bool:
    info = core.open_get(market) or {}
    return bool(info.get("abort"))

def chase_buy(core, market:str, spend_eur:float) -> dict:
    last_oid=None; last_price=None
    min_tick = float(1.0 / (10 ** core.price_decimals(market)))
    while True:
        if abort_requested(core, market):
            return {"ok": False, "ctx": "aborted"}
        bid, _ = core.get_best_bid_ask(market)
        if bid <= 0:
            time.sleep(0.25); continue
        price  = bid
        amount = core.round_amount_down(market, spend_eur / max(price,1e-12))
        if amount < core.min_base(market):
            return {"ok": False, "ctx":"amount_too_small"}
        if last_oid:
            try: core.cancel_order_blocking(market, last_oid, wait_sec=2.5)
            except: pass
        _, resp = core.place_limit_postonly(market, "buy", price, amount)
        if isinstance(resp, dict) and resp.get("error"):
            time.sleep(0.35); continue
        last_oid = resp.get("orderId"); last_price = price
        core.open_set(market, {"orderId": last_oid, "side":"buy", "amount_init": amount})
        t0 = time.time()
        while True:
            if abort_requested(core, market):
                try: core.cancel_order_blocking(market, last_oid, wait_sec=2.0)
                except: pass
                return {"ok": False, "ctx": "aborted"}
            st = core.order_status(market, last_oid)
            s  = (st or {}).get("status","").lower()
            if s in ("filled","partiallyfilled"):
                fb = float((st or {}).get("filledAmount",0) or 0)
                fq = float((st or {}).get("filledAmountQuote",0) or 0)
                avg = (fq/fb) if (fb>0 and fq>0) else last_price
                return {"ok": True, "status": s, "avg_price": avg, "filled_base": fb, "spent_eur": fq, "last_oid": last_oid}
            bid2, _ = core.get_best_bid_ask(market)
            if bid2 > 0 and (last_price is None or abs(bid2 - last_price) >= min_tick) or (time.time()-t0 >= 2.0):
                break
            time.sleep(0.18 if (time.time()-t0) < 4 else 0.35)

# ===== مساعدات =====
def infer_recent_avg_buy(core, market: str, within_ms=60_000):
    trades = core.bv_request("GET", f"/trades?market={market}&limit=50")
    if not isinstance(trades, list): return 0.0
    now = int(time.time()*1000); q=b=0.0
    for t in trades:
        if (t.get("side","").lower() != "buy"): continue
        ts = int(t.get("timestamp",0) or 0)
        if now - ts > within_ms: continue
        a = float(t.get("amount",0) or 0); p = float(t.get("price",0) or 0)
        q += a; b += a*p
    return (b/q) if (q>0 and b>0) else 0.0
