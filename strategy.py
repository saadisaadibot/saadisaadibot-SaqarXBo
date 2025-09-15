# -*- coding: utf-8 -*-
# strategy.py — EDIT HERE
# مطاردة شراء سريعة + TP ذكي (RSI+ADX+ATR+EMA) + SL ATR trailing
# بلا تبريد نهائيًا — إلغاء مطاردة عبر abort فقط
# يتأكد من الرصيد بعد الشراء + يراقب البيع اليدوي + يرسل إشعارات وReady عند البيع

import time, json, math, threading

# ===== إعدادات سريعة =====
SL_PCT        = -1.0      # SL ابتدائي -1% (يتفعّل من الكور)
TP_MIN_PCT    = 0.30
TP_MID_PCT    = 0.70
TP_MAX_PCT    = 1.20
HEADROOM_EUR  = 0.30

# مؤشرات
ADX_LEN       = 14
RSI_LEN       = 14
EMA_FAST      = 50
EMA_SLOW      = 200
ATR_LEN       = 14

# ============ مؤشرات ============
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
        d = closes[i] - closes[i-1]
        if d >= 0: gains += d
        else: losses += -d
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

# ============ سلوك السوق + اختيار TP ============
def _fetch_candles(core, market: str, interval="1m", limit=240):
    data = core.bv_request("GET", f"/{market}/candles?interval={interval}&limit={limit}")
    if not isinstance(data, list): return [], [], []
    highs = [float(r[2]) for r in data]
    lows  = [float(r[3]) for r in data]
    closes= [float(r[4]) for r in data]
    return highs, lows, closes

def _market_regime(core, market: str):
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

def _choose_tp_price_percent(core, market: str, avg_price: float) -> float:
    reg = _market_regime(core, market)
    if not reg.get("ok"): return avg_price * (1.0 + TP_MID_PCT/100.0)
    adx, rsi, atr_pct, trend_up = reg["adx"], reg["rsi"], reg["atr_pct"], reg["trend_up"]
    if adx < 18: base_pct = 0.40
    elif adx < 22: base_pct = 0.55 if trend_up else 0.45
    elif adx < 28: base_pct = 0.85 if trend_up else 0.60
    else: base_pct = 1.05 if trend_up else 0.70
    if rsi >= 75: base_pct = min(base_pct, 0.70)
    elif rsi <= 40: base_pct = max(base_pct, 0.60 if trend_up else 0.40)
    tp_pct = base_pct + min(0.30, (atr_pct * 1000) * 0.06)
    tp_pct = max(TP_MIN_PCT, min(tp_pct, TP_MAX_PCT))
    return avg_price * (1.0 + tp_pct/100.0)

# ============ مطاردة شراء سريعة + إلغاء عبر abort ============
def _abort_requested(core, market: str) -> bool:
    info = core.open_get(market) or {}
    return bool(info.get("abort"))

def chase_buy(core, market:str, spend_eur:float) -> dict:
    last_oid=None; last_price=None
    min_tick = float(1.0 / (10 ** core.price_decimals(market)))
    while True:
        if _abort_requested(core, market):
            return {"ok": False, "ctx": "aborted"}
        bid, _ = core.get_best_bid_ask(market)
        if bid <= 0:
            time.sleep(0.25);  # أسرع شوي
            continue
        price  = bid
        amount = core.round_amount_down(market, spend_eur / max(price,1e-12))
        if amount < core.min_base(market):
            return {"ok": False, "ctx":"amount_too_small"}
        if last_oid:
            try: core.cancel_order_blocking(market, last_oid, wait_sec=2.5)
            except: pass
        _, resp = core.place_limit_postonly(market, "buy", price, amount)
        if isinstance(resp, dict) and resp.get("error"):
            time.sleep(0.35); continue  # تقليل التأخير
        last_oid = resp.get("orderId"); last_price = price
        core.open_set(market, {"orderId": last_oid, "side":"buy", "amount_init": amount})
        t0 = time.time()
        while True:
            if _abort_requested(core, market):
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
            # Reprice أسرع: لو تحرك ≥ tick أو مرّ ≥ 2 ثواني
            reprice_due = (bid2 > 0 and abs(bid2 - last_price) >= min_tick) or (time.time()-t0 >= 2.0)
            if reprice_due:
                break
            time.sleep(0.18 if (time.time()-t0) < 4 else 0.35)

# ============ ATR للـSL ============
def _atr_price(core, market: str):
    highs, lows, closes = _fetch_candles(core, market, "1m", 120)
    if len(closes) < ATR_LEN+5: return None
    return _atr(highs, lows, closes, ATR_LEN)

def maybe_move_sl(core, market:str, avg:float, base:float, current_bid:float, current_sl_price:float):
    if base<=0 or current_bid<=0 or avg<=0: return current_sl_price
    gain_pct = (current_bid - avg) / avg * 100.0
    atr = _atr_price(core, market)
    if gain_pct <= 0.3: return current_sl_price
    if atr and atr > 0:
        be = avg
        target = be + (0.50 if gain_pct > 0.7 else 0.25) * atr
        new_sl = max(current_sl_price or 0.0, target)
        return min(new_sl, current_bid)
    return max(current_sl_price or 0.0, avg)

# ============ أدوات مساعدة ============
def _infer_recent_avg_buy(core, market: str, within_ms=60_000):
    """متوسط آخر صفقات شراء خلال دقيقة لتصحيح avg عند fills غريبة."""
    trades = core.bv_request("GET", f"/trades?market={market}&limit=50")
    if not isinstance(trades, list): return 0.0
    now = int(time.time()*1000)
    q=b=0.0
    for t in trades:
        if (t.get("side","").lower() != "buy"): continue
        ts = int(t.get("timestamp",0) or 0)
        if now - ts > within_ms: continue
        a = float(t.get("amount",0) or 0); p = float(t.get("price",0) or 0)
        q += a; b += a*p
    return (b/q) if (q>0 and b>0) else 0.0

def _watch_manual_sell(core, market: str, order_id: str, amt_hint: float|None):
    """يراقب بيع يدوي حتى الامتلاء ثم يرسل Ready + تلغرام."""
    base_guess = float(amt_hint or 0.0)
    deadline = time.time() + 120.0
    last_status = ""
    while time.time() < deadline:
        st = core.order_status(market, order_id) or {}
        s  = (st.get("status") or "").lower()
        last_status = s or last_status
        if s == "filled":
            try:
                fa = float(st.get("filledAmount",0) or 0)
                fq = float(st.get("filledAmountQuote",0) or 0)
                sell_avg = (fq/fa) if (fa>0 and fq>0) else 0.0
                sold_b   = fa if fa>0 else base_guess
            except: sell_avg, sold_b = 0.0, base_guess
            pos = core.pos_get(market) or {}
            avg_in = float(pos.get("avg") or 0.0)
            if avg_in>0 and sold_b>0 and sell_avg>0:
                pnl_eur = (sell_avg-avg_in)*sold_b
                core.tg_send(
                    f"💰 تم البيع — {market} (manual)\n"
                    f"🧾 Avg In: {avg_in:.8f}\n"
                    f"🏷️ Avg Out: {sell_avg:.8f}\n"
                    f"📦 Base: {sold_b}\n"
                    f"📊 PnL: €{pnl_eur:.2f}"
                )
                core.notify_ready(market, reason="manual_sell_filled", pnl_eur=round(pnl_eur,4))
            else:
                core.tg_send(f"💬 عملية بيع يدوية مملوءة — {market} (لا أستطيع حساب PnL بدقة).")
                core.notify_ready(market, reason="manual_sell_filled", pnl_eur=None)
            return
        time.sleep(0.7)
    core.tg_send(f"ℹ️ مراقبة البيع اليدوي انتهت دون امتلاء — {market} (status={last_status}).")

# ============ تنفيذ شراء عند إشارة أبو صياح ============
def on_hook_buy(core, coin:str):
    market = core.coin_to_market(coin)
    if not market:
        core.tg_send(f"⛔ سوق غير مدعوم — {coin}"); return

    eur_avail = core.balance("EUR")
    spend = max(0.0, eur_avail - HEADROOM_EUR)
    if spend <= 0:
        core.tg_send(f"⛔ لا يوجد EUR كافٍ (avail={eur_avail:.2f})")
        core.notify_ready(market,"buy_failed"); return

    # مطاردة حتى الامتلاء (بدون تبريد)
    core.open_set(market, {"side":"buy", "abort": False})
    res = chase_buy(core, market, spend)
    if not res.get("ok"):
        core.tg_send(f"⚠️ فشل الشراء — {market}\n{json.dumps(res,ensure_ascii=False)}")
        info = res.get("last_oid")
        if info:
            try: core.cancel_order_blocking(market, info, wait_sec=3.0)
            except: pass
        core.notify_ready(market,"buy_failed"); return

    avg = float(res.get("avg_price") or 0)
    base_bought = float(res.get("filled_base") or 0)
    base_sym = market.split("-")[0]

    # تسوية الرصيد (لو الامتلاء على دفعات) + محاولة تحسين متوسط الدخول من آخر دقيقة
    bal = core.balance(base_sym)
    if bal > base_bought:
        base_bought = core.round_amount_down(market, bal)
    avg2 = _infer_recent_avg_buy(core, market)
    if avg2 > 0: avg = avg2

    core.tg_send(f"✅ اشترى — {market}\nAvg={avg:.8f}, Base={base_bought}")

    # اختيار هدف TP
    tp_price = _choose_tp_price_percent(core, market, avg)

    # انتظر تحديث الرصيد بسرعة ثم ضع TP (PostOnly) مع إعادة محاولات
    minb = core.min_base(market)
    sell_amt = 0.0; t0 = time.time()
    while time.time()-t0 < 6.0:
        avail = core.balance(base_sym)
        sell_amt = core.round_amount_down(market, max(0.0, avail))
        if sell_amt >= minb: break
        time.sleep(0.35)

    tp_oid = None; last_err = None
    if sell_amt >= minb:
        for _ in range(5):
            _, resp = core.place_limit_postonly(market, "sell", tp_price, sell_amt)
            if isinstance(resp, dict) and not resp.get("error"):
                tp_oid = resp.get("orderId"); break
            last_err = resp
            err = (resp or {}).get("error","").lower()
            if "insufficient" in err:
                step_amt = core.step(market) or 0.0
                sell_amt = core.round_amount_down(market, max(0.0, sell_amt - step_amt))
                if sell_amt < minb: break
            time.sleep(0.45)
    else:
        core.tg_send(f"ℹ️ لم أضع TP لأن الكمية المتاحة < minBase ({minb}).")

    if not tp_oid and last_err:
        core.tg_send(f"⚠️ فشل وضع TP — {market}\n{json.dumps(last_err, ensure_ascii=False)}")

    # ثبّت SL وخزّن الحالة ليكمل الـwatchdog القفل والبيع
    sl_price = avg * (1.0 + (SL_PCT/100.0))
    core.pos_set(market, {"avg": avg, "base": base_bought, "tp_oid": tp_oid,
                          "sl_price": sl_price, "tp_target": tp_price})
    core.open_clear(market)
    core.tg_send(f"📈 TP={tp_price:.8f} | SL={sl_price:.8f}")

# ============ أوامر تيليغرام ============
def on_tg_command(core, text):
    t = (text or "").strip().lower()

    # —— Emergency reset
    if t in ("restart", "reset", "ريستارت", "ريست", "اعادة", "إعادة"):
        try:
            res = core.reset_state()
            if res.get("ok"):
                core.tg_send("✅ تم مسح الحالة بالكامل (Redis). جاهز لأوامر جديدة.")
                try: core.notify_ready("ALL-EUR", reason="emergency_reset", pnl_eur=None)
                except: pass
            else:
                core.tg_send(f"⚠️ فشل المسح: {res}")
        except Exception as e:
            core.tg_send(f"🐞 خطأ أثناء reset: {type(e).__name__}: {e}")
        return

    # بيع يدوي (ويراقب الامتلاء لإرسال Ready)
    if t.startswith("بيع"):
        parts = text.split()
        if len(parts) < 2:
            core.tg_send("صيغة: بيع COIN [AMOUNT]"); return
        coin = parts[1].upper().strip()
        market = core.coin_to_market(coin)
        if not market: core.tg_send("⛔ عملة غير صالحة."); return
        amt = None
        if len(parts) >= 3:
            try: amt = float(parts[2])
            except: amt = None
        if amt is None:
            base = market.split("-")[0]; bal = core.balance(base)
            amt = core.round_amount_down(market, bal)
        ask = core.get_best_bid_ask(market)[1]
        _, resp = core.place_limit_postonly(market, "sell", ask, amt)
        ok = isinstance(resp, dict) and not resp.get("error")
        oid = (resp or {}).get("orderId")
        core.tg_send(("✅ أُرسل أمر بيع" if ok else "⚠️ فشل البيع") + f" — {market}\n{json.dumps(resp,ensure_ascii=False)}")
        if ok and oid:
            threading.Thread(target=_watch_manual_sell, args=(core, market, oid, amt), daemon=True).start()
        return

    # إلغاء مطاردة شراء حالية — بلا تبريد، abort فقط
    if t.startswith("الغ"):
        parts = text.split()
        if len(parts) < 2:
            core.tg_send("صيغة: الغ COIN"); return
        coin = parts[1].upper().strip()
        market = core.coin_to_market(coin)
        if not market: core.tg_send("⛔ عملة غير صالحة."); return
        info = core.open_get(market) or {}
        info["abort"] = True
        core.open_set(market, info)
        ok=False; final="unknown"; last={}
        if info.get("orderId"):
            ok, final, last = core.cancel_order_blocking(market, info["orderId"], wait_sec=12.0)
            if ok: core.open_clear(market)
        core.tg_send(("✅ تم الإلغاء" if ok else "⚠️ فشل الإلغاء") + f" — status={final}\n{json.dumps(last,ensure_ascii=False)}")
        return

    core.tg_send("الأوامر: «بيع COIN [AMOUNT]» ، «الغ COIN» — الشراء عبر أبو صياح /hook")