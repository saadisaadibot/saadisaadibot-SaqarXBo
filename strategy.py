# -*- coding: utf-8 -*-
# strategy.py — EDIT HERE
# مطاردة طويلة النفس + اختيار TP ذكي لعام 2025 (RSI+ADX+ATR+اتجاه EMA) + SL trailing بالـATR

import time, json, math

# إعدادات سريعة التعديل
SL_PCT        = -1.0       # SL ابتدائي -1% (يتفعّل من الكور)
TP_MIN_PCT    = 0.30       # أدنى هدف ربح % في ظروف هادئة
TP_MID_PCT    = 0.70       # هدف متوسط
TP_MAX_PCT    = 1.20       # أقصى هدف في زخم قوي
HEADROOM_EUR  = 0.30       # احتياط EUR

ADX_LEN       = 14
RSI_LEN       = 14
EMA_FAST      = 50
EMA_SLOW      = 200
ATR_LEN       = 14

# ============ مؤشرات (بدون مكتبات خارجية) ============
def _ema(values, period):
    if len(values) < period: return None
    k = 2.0 / (period + 1.0)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return ema

def _series_ema(values, period):
    if len(values) < period: return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def _rsi(closes, period=14):
    if len(closes) < period+1: return None
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i-1]
        if diff >= 0: gains += diff
        else: losses += -diff
    avg_gain = gains / period
    avg_loss = losses / period if losses > 0 else 1e-9
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def _atr(highs, lows, closes, period=14):
    n = len(closes)
    if n < period+1: return None
    trs = []
    for i in range(1, n):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    # Wilder's smoothing
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

    # Wilder smoothing for TR, +DM, -DM
    tr14 = sum(tr_list[:period]); pDM14 = sum(plus_dm[:period]); mDM14 = sum(minus_dm[:period])
    for i in range(period, len(tr_list)):
        tr14  = tr14  - (tr14 / period)  + tr_list[i]
        pDM14 = pDM14 - (pDM14 / period) + plus_dm[i]
        mDM14 = mDM14 - (mDM14 / period) + minus_dm[i]
    if tr14 <= 0: return 0.0
    pDI = (pDM14 / tr14) * 100.0
    mDI = (mDM14 / tr14) * 100.0
    dx = (abs(pDI - mDI) / max(pDI + mDI, 1e-9)) * 100.0
    # ADX تقريبي (بدون سلسلة كاملة)
    return dx

# ============ سلوك السوق + اختيار TP ============
def _fetch_candles(core, market: str, interval="1m", limit=120):
    data = core.bv_request("GET", f"/{market}/candles?interval={interval}&limit={limit}")
    if not isinstance(data, list): return [], [], []
    # كل سطر: [time, open, high, low, close, volume]
    highs = [float(r[2]) for r in data]
    lows  = [float(r[3]) for r in data]
    closes= [float(r[4]) for r in data]
    return highs, lows, closes

def _market_regime(core, market: str):
    highs, lows, closes = _fetch_candles(core, market, "1m", 240)  # ~4 ساعات
    if len(closes) < max(EMA_SLOW+5, ATR_LEN+5):
        return {"ok": False}

    ema_fast_series = _series_ema(closes, EMA_FAST)
    ema_slow_series = _series_ema(closes, EMA_SLOW)
    ema_fast = ema_fast_series[-1]
    ema_slow = ema_slow_series[-1]
    trend_up = ema_fast > ema_slow

    rsi = _rsi(closes, RSI_LEN)
    atr = _atr(highs, lows, closes, ATR_LEN)
    last_close = closes[-1]
    atr_pct = (atr / last_close) if (atr and last_close>0) else 0.0

    adx_val = _adx(highs, lows, closes, ADX_LEN) or 0.0

    return {
        "ok": True,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "trend_up": trend_up,
        "rsi": rsi or 50.0,
        "adx": adx_val,
        "atr": atr or 0.0,
        "atr_pct": atr_pct
    }

def _choose_tp_price_percent(core, market: str, avg_price: float) -> float:
    """
    خوارزمية 2025: اندماج RSI+ADX+ATR+اتجاه EMA لتحديد TP%
    - سوق رينج (ADX<18): 0.30–0.60%
    - ترند صاعد (ADX≥22 و EMA50>EMA200 و RSI 50–75): 0.70–1.20%
    - ترند قوي جدًا (ADX≥28): قرب 1.20% (مع سقف)
    - حالة تشبّع (RSI>75): خفّض الهدف (0.40–0.70%) لتفادي ارتداد
    - ضبط أدق حسب ATR%
    """
    regime = _market_regime(core, market)
    if not regime.get("ok"):
        # fallback آمن
        return avg_price * (1.0 + TP_MID_PCT/100.0)

    adx = regime["adx"]; rsi = regime["rsi"]; atr_pct = regime["atr_pct"]; trend_up = regime["trend_up"]

    # قاعدة
    if adx < 18:
        base_pct = 0.40
    elif adx < 22:
        base_pct = 0.55 if trend_up else 0.45
    elif adx < 28:
        base_pct = 0.85 if trend_up else 0.60
    else:
        base_pct = 1.05 if trend_up else 0.70

    # تعديل RSI
    if rsi >= 75:
        base_pct = min(base_pct, 0.70)  # قرب ذروة — هدف أهدأ
    elif rsi <= 40:
        # ممكن ارتداد — لا نفرط، لكن إن كان ترند صاعد نخليها متوسطة
        base_pct = max(base_pct, 0.60 if trend_up else 0.40)

    # تعديل ATR% (0.2% = 0.002)
    # كل 0.1% ATR نضيف ~0.06% للهدف، بس بحد أقصى 0.3%
    adj_from_atr = min(0.30, (atr_pct * 1000) * 0.06)  # atr_pct*100 = % ، *10 -> لكل 0.1%
    tp_pct = base_pct + adj_from_atr

    # حدود نهائية
    tp_pct = max(TP_MIN_PCT, min(tp_pct, TP_MAX_PCT))
    return avg_price * (1.0 + tp_pct/100.0)

# ============ مطاردة شراء طويلة النفس (لا تفشل) ============
def chase_buy(core, market:str, spend_eur:float) -> dict:
    """
    ترجع: {ok, status, avg_price, filled_base, spent_eur, last_oid?, ctx}
    - تعيد التسعير بهدوء عندما يتحرك الـBid
    - تلغي الطلب السابق قبل وضع الجديد
    - تستمر حتى الامتلاء (أو تعيد ctx لو مشكلة حجم)
    """
    last_oid=None; last_price=None
    min_tick = float(1.0 / (10 ** core.price_decimals(market)))
    while True:
        bid, _ = core.get_best_bid_ask(market)
        if bid <= 0:
            time.sleep(0.5); continue
        price  = bid
        amount = core.round_amount_down(market, spend_eur / max(price,1e-12))
        if amount < core.min_base(market):
            return {"ok": False, "ctx":"amount_too_small"}
        if last_oid:
            try: core.cancel_order_blocking(market, last_oid, wait_sec=3.0)
            except: pass
        _, resp = core.place_limit_postonly(market, "buy", price, amount)
        if isinstance(resp, dict) and resp.get("error"):
            time.sleep(0.6); continue
        last_oid = resp.get("orderId"); last_price = price
        core.open_set(market, {"orderId": last_oid, "side":"buy", "amount_init": amount})
        # متابعة حتى الامتلاء — مع Reprice عند تحرك الـBid
        t0 = time.time()
        while True:
            st = core.order_status(market, last_oid)
            s  = (st or {}).get("status","").lower()
            if s in ("filled","partiallyfilled"):
                fb = float((st or {}).get("filledAmount",0) or 0)
                fq = float((st or {}).get("filledAmountQuote",0) or 0)
                avg = (fq/fb) if (fb>0 and fq>0) else last_price
                return {"ok": True, "status": s, "avg_price": avg, "filled_base": fb, "spent_eur": fq}
            bid2, _ = core.get_best_bid_ask(market)
            if bid2 > 0 and (last_price is None or abs(bid2 - last_price) >= min_tick):
                break
            time.sleep(0.25 if (time.time()-t0) < 5 else 0.5)
        # يعيد الحلقة لوضع سعر جديد

# ============ تحريك SL (Trailing باستخدام ATR) ============
def _atr_price(core, market: str):
    highs, lows, closes = _fetch_candles(core, market, "1m", 120)
    if len(closes) < ATR_LEN+5: return None
    return _atr(highs, lows, closes, ATR_LEN)

def maybe_move_sl(core, market:str, avg:float, base:float, current_bid:float, current_sl_price:float):
    """
    قواعد:
    - إذا الربح غير المحقق > 0.3% → انقل SL إلى BE + 0.25*ATR (إن توفر ATR)
    - إذا الربح > 0.7% → ارفع SL إلى BE + 0.50*ATR
    - لا تُنزل SL أبدًا (فقط ارفع)
    """
    if base<=0 or current_bid<=0 or avg<=0: return current_sl_price
    gain_pct = (current_bid - avg) / avg * 100.0
    atr = _atr_price(core, market)
    if gain_pct <= 0.3:
        return current_sl_price
    if atr and atr > 0:
        be = avg
        if gain_pct > 0.7:
            target = be + 0.50 * atr
        else:
            target = be + 0.25 * atr
        new_sl = max(current_sl_price or 0.0, target)
        # لا ترفع SL فوق السعر الحالي (تحاشي ضرب فوري)
        return min(new_sl, current_bid)
    else:
        # بدون ATR: انقل إلى BE فقط
        return max(current_sl_price or 0.0, avg)

# ============ تنفيذ شراء عند إشارة أبو صياح ============
def on_hook_buy(core, coin:str):
    market = core.coin_to_market(coin)
    if not market:
        core.tg_send(f"⛔ سوق غير مدعوم — {coin}"); return
    eur_avail = core.balance("EUR")
    spend = max(0.0, eur_avail - HEADROOM_EUR)
    if spend <= 0:
        core.tg_send(f"⛔ لا يوجد EUR كافٍ (avail={eur_avail:.2f})")
        core.notify_ready(market,"buy_failed")
        return

    # مطاردة حتى الامتلاء
    res = chase_buy(core, market, spend)
    if not res.get("ok"):
        core.tg_send(f"⚠️ فشل الشراء — {market}\n{json.dumps(res,ensure_ascii=False)}")
        info = res.get("last_oid")
        if info:
            try: core.cancel_order_blocking(market, info, wait_sec=3.0)
            except: pass
        core.notify_ready(market,"buy_failed")
        return

    avg=float(res.get("avg_price") or 0); base=float(res.get("filled_base") or 0)
    core.tg_send(f"✅ اشترى — {market}\nAvg={avg:.8f}, Base={base}")

    # اختيار هدف TP ديناميكي
    tp_price = _choose_tp_price_percent(core, market, avg)

    # طلب TP Maker
    _, resp = core.place_limit_postonly(market, "sell", tp_price, core.round_amount_down(market, base))
    if isinstance(resp, dict) and resp.get("error"):
        core.tg_send(f"⚠️ فشل وضع TP — {market}\n{json.dumps(resp,ensure_ascii=False)}")

    tp_oid = (resp or {}).get("orderId")
    sl_price = avg * (1.0 + (SL_PCT/100.0))  # SL ابتدائي -1%
    core.pos_set(market, {"avg":avg,"base":base,"tp_oid":tp_oid,"sl_price":sl_price,"tp_target":tp_price})
    core.open_clear(market)
    core.tg_send(f"📈 TP={tp_price:.8f} ، SL={sl_price:.8f} (RSI+ADX+ATR)")

# ============ أوامر تيليغرام ============
def on_tg_command(core, text:str):
    low = text.lower().strip()

    if low.startswith("بيع"):
        parts=text.split()
        if len(parts)<2: core.tg_send("صيغة: بيع COIN [AMOUNT]"); return
        coin = parts[1].upper().strip()
        market = core.coin_to_market(coin)
        if not market: core.tg_send("⛔ عملة غير صالحة."); return
        amt=None
        if len(parts)>=3:
            try: amt=float(parts[2])
            except: amt=None
        if amt is None:
            base=market.split("-")[0]; bal=core.balance(base)
            amt=core.round_amount_down(market, bal)
        ask = core.get_best_bid_ask(market)[1]
        _, resp = core.place_limit_postonly(market, "sell", ask, amt)
        ok = not bool((resp or {}).get("error"))
        core.tg_send(("✅ أُرسل أمر بيع" if ok else "⚠️ فشل البيع") + f" — {market}\n{json.dumps(resp,ensure_ascii=False)}")
        return

    if low.startswith("الغ"):
        parts=text.split()
        if len(parts)<2: core.tg_send("صيغة: الغ COIN"); return
        coin = parts[1].upper().strip()
        market = core.coin_to_market(coin)
        if not market: core.tg_send("⛔ عملة غير صالحة."); return
        info=core.open_get(market); ok=False; final="unknown"; last={}
        if info and info.get("orderId"):
            ok, final, last = core.cancel_order_blocking(market, info["orderId"], wait_sec=12.0)
            if ok: core.open_clear(market)
        core.tg_send(("✅ تم الإلغاء" if ok else "⚠️ فشل الإلغاء") + f" — status={final}\n{json.dumps(last,ensure_ascii=False)}")
        return

    core.tg_send("الأوامر: «بيع COIN [AMOUNT]» ، «الغ COIN» — الشراء عبر أبو صياح /hook")
