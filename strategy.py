# -*- coding: utf-8 -*-
# strategy.py — TP + SL(-2%) بعد الشراء، مع طباعة سبب Bitvavo عند الفشل ومحاولات fallback ذكية.

import time, json, threading

# —— إعدادات قابلة للتعديل ——
HEADROOM_EUR = 0.30
SL_FIXED_PCT = -2.0  # SL ثابت دائماً
DEBUG_BV = True      # لو True يطبع ردّ Bitvavo الخام عند الأخطاء

# ===== أدوات مساعدة سريعة =====
def _fetch_candles(core, market: str, interval="1m", limit=240):
    data = core.bv_request("GET", f"/{market}/candles?interval={interval}&limit={limit}")
    if not isinstance(data, list): return [], [], []
    highs = [float(r[2]) for r in data]
    lows  = [float(r[3]) for r in data]
    closes= [float(r[4]) for r in data]
    return highs, lows, closes

def _series_ema(values, period):
    if len(values) < period: return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def _rsi(closes, period=14):
    if len(closes) < period+1: return None
    gains = losses = 0.0
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

def _market_regime(core, market: str):
    highs, lows, closes = _fetch_candles(core, market, "1m", 240)
    if len(closes) < 210:
        return {"ok": False, "tp_pct": 0.6, "trend_up": False, "adx": 0.0, "rsi": 50.0}
    ema50  = _series_ema(closes, 50)[-1]
    ema200 = _series_ema(closes, 200)[-1]
    trend_up = ema50 > ema200
    rsi = _rsi(closes, 14) or 50.0
    atr = _atr(highs, lows, closes, 14) or 0.0
    last_close = closes[-1]
    atr_pct = (atr / last_close) if (atr and last_close>0) else 0.0
    adx_val = _adx(highs, lows, closes, 14) or 0.0

    # اختيار TP% بسيط حسب القوة
    if adx_val < 18: tp_pct = 0.40
    elif adx_val < 22: tp_pct = 0.55 if trend_up else 0.45
    elif adx_val < 28: tp_pct = 0.80 if trend_up else 0.60
    else: tp_pct = 1.00 if trend_up else 0.70
    if rsi >= 75: tp_pct = min(tp_pct, 0.70)
    if rsi <= 40: tp_pct = max(tp_pct, 0.50 if trend_up else 0.40)
    tp_pct += min(0.30, (atr_pct * 1000) * 0.06)  # تعزيز بسيط مع ATR

    return {"ok": True, "tp_pct": tp_pct, "trend_up": trend_up, "adx": adx_val, "rsi": rsi}

def choose_tp_price(core, market: str, avg_price: float):
    reg = _market_regime(core, market)
    tp_pct = reg.get("tp_pct", 0.6)
    return avg_price * (1.0 + tp_pct/100.0), reg

# ===== مطاردة شراء (نستخدم الموجودة في core.place_limit_postonly) =====
def chase_buy(core, market:str, spend_eur:float)->dict:
    """مطاردة Maker سريعة؛ تُعيد أيضا last_oid لنستعمله كـ operatorId للـ SL."""
    last_oid=None; last_price=None
    min_tick = float(1.0 / (10 ** core.price_decimals(market)))
    while True:
        bid, _ = core.get_best_bid_ask(market)
        if bid <= 0:
            time.sleep(0.25); continue
        price = bid
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
            st = core.order_status(market, last_oid)
            s  = (st or {}).get("status","").lower()
            if s in ("filled","partiallyfilled"):
                fb = float((st or {}).get("filledAmount",0) or 0)
                fq = float((st or {}).get("filledAmountQuote",0) or 0)
                avg = (fq/fb) if (fb>0 and fq>0) else last_price
                return {"ok": True, "status": s, "avg_price": avg, "filled_base": fb, "spent_eur": fq, "last_oid": last_oid}
            bid2, _ = core.get_best_bid_ask(market)
            reprice_due = (bid2 > 0 and abs(bid2 - last_price) >= min_tick) or (time.time()-t0 >= 2.0)
            if reprice_due: break
            time.sleep(0.18 if (time.time()-t0) < 4 else 0.35)

# ===== لا نحرك SL (ثابت -2%) — واجهة مطلوبة من الكور =====
def maybe_move_sl(core, market:str, avg:float, base:float, current_bid:float, current_sl_price:float):
    return current_sl_price

# ===== أدوات طباعة أخطاء Bitvavo =====
def _explain_bv_error(resp, where:str, body_preview:dict, core):
    """
    يطبع ردّ Bitvavo كما هو + تفسير مختصر.
    body_preview: جزء آمن من البودي (بدون أسرار).
    """
    if not DEBUG_BV: return
    try:
        msg = json.dumps(resp, ensure_ascii=False)
    except Exception:
        msg = str(resp)
    hint = ""
    try:
        code = int(resp.get("errorCode"))
        err  = (resp.get("error") or "").lower()
        if code == 205 and "operatorid" in err:
            hint = "💡 بعض الأسواق لا تقبل operatorId مع أوامر SL؛ سنجرب بدون operatorId."
        elif code == 205 and "triggertype" in err:
            hint = "💡 استخدمنا triggerType=price + triggerReference (lastTrade/bestBid/bestAsk)."
    except Exception:
        pass
    core.tg_send(f"🩻 BV ERROR @ {where}\n{msg}\n— payload: {json.dumps(body_preview, ensure_ascii=False)}\n{hint}")

# ===== مراقبة بيع يدوي لإرسال Ready =====
def _watch_manual_sell(core, market: str, order_id: str, amt_hint: float|None):
    base_guess = float(amt_hint or 0.0)
    deadline = time.time() + 180.0
    last_status = ""
    while time.time() < deadline:
        st = core.order_status(market, order_id) or {}
        s = (st.get("status") or "").lower()
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
                    f"💰 بيع يدوي — {market}\n"
                    f"AvgIn {avg_in:.8f} → AvgOut {sell_avg:.8f} | Base {sold_b}\n"
                    f"PnL €{pnl_eur:.2f}"
                )
                core.notify_ready(market, reason="manual_sell_filled", pnl_eur=round(pnl_eur,4))
            else:
                core.tg_send(f"💬 بيع يدوي مملوء — {market} (لا أقدر أحسب PnL بدقة).")
                core.notify_ready(market, reason="manual_sell_filled", pnl_eur=None)
            return
        time.sleep(0.7)
    core.tg_send(f"ℹ️ مراقبة البيع اليدوي انتهت — {market} (status={last_status}).")

# ===== تنفيذ الشراء عند إشارة أبو صياح =====
def on_hook_buy(core, coin:str):
    market = core.coin_to_market(coin)
    if not market:
        core.tg_send(f"⛔ سوق غير مدعوم — {coin}"); return

    eur_avail = core.balance("EUR")
    spend = max(0.0, eur_avail - HEADROOM_EUR)
    if spend <= 0:
        core.tg_send(f"⛔ لا يوجد EUR كافٍ (avail={eur_avail:.2f})")
        core.notify_ready(market,"buy_failed"); return

    # مطاردة حتى الامتلاء
    core.open_set(market, {"side":"buy", "abort": False})
    res = chase_buy(core, market, spend)
    if not res.get("ok"):
        core.tg_send(f"⚠️ فشل الشراء — {market}\n{json.dumps(res,ensure_ascii=False)}")
        core.notify_ready(market,"buy_failed"); return

    avg = float(res.get("avg_price") or 0.0)
    base_bought = float(res.get("filled_base") or 0.0)
    buy_oid = (res.get("last_oid") or "")  # سنحاول ربط SL به؛ ولو رفضت المنصة نزيله.
    base_sym = market.split("-")[0]

    # تسوية الرصيد
    bal = core.balance(base_sym)
    if bal > base_bought:
        base_bought = core.round_amount_down(market, bal)

    # اختر TP + سبب مختصر
    tp_price, reg = choose_tp_price(core, market, avg)
    tp_pct = reg.get("tp_pct", 0.7)
    reasons = []
    if reg:
        if reg.get("trend_up"): reasons.append("EMA50>EMA200")
        reasons.append(f"ADX={reg.get('adx',0):.1f}")
        reasons.append(f"RSI={reg.get('rsi',0):.0f}")
    reason_txt = ", ".join(reasons)

    # ضع TP Maker على كامل المتاح (مع محاولات)
    minb = core.min_base(market)
    sell_amt = core.round_amount_down(market, max(core.balance(base_sym), base_bought))
    tp_oid = None; tp_resp=None
    if sell_amt >= minb:
        for _ in range(5):
            _, tp_resp = core.place_limit_postonly(market, "sell", tp_price, sell_amt)
            if isinstance(tp_resp, dict) and not tp_resp.get("error"):
                tp_oid = tp_resp.get("orderId"); break
            time.sleep(0.45)

    # —— تأكيد الامتلاء قبل أي SL (تحقق مزدوج) ——
    st_buy = core.order_status(market, buy_oid) or {}
    if (st_buy.get("status","").lower() not in ("filled","partiallyfilled")):
        time.sleep(0.4)
        st_buy = core.order_status(market, buy_oid) or {}

    # —— SL رسمي ثابت -2% ——
    sl_price      = avg * (1.0 + SL_FIXED_PCT/100.0)       # 98%
    trigger_price = avg * (1.0 + (SL_FIXED_PCT-0.1)/100.0) # 97.9%

    def _send_sl(payload, tag):
        resp = core.bv_request("POST", "/order", body=payload)
        ok = isinstance(resp, dict) and not resp.get("error")
        if not ok:
            # اطبع الرد الخام + جزء من البودي لتشخيص المشكلة
            preview = {
                "market": payload.get("market"),
                "orderType": payload.get("orderType"),
                "amount": payload.get("amount"),
                "price": payload.get("price"),
                "triggerType": payload.get("triggerType"),
                "triggerReference": payload.get("triggerReference", None),
                "triggerPrice": payload.get("triggerPrice"),
                "operator": payload.get("operator", None),
                "hasOperatorId": "operatorId" in payload,
                "hasClientOrderId": "clientOrderId" in payload
            }
            _explain_bv_error(resp, f"SL:{tag}", preview, core)
        return ok, resp

    # الشكل الأساسي: triggerType="price" + triggerReference + operator=lte (لأننا SL بيع)
    base_sl_body = {
        "market": market,
        "side": "sell",
        "orderType": "stopLossLimit",
        "amount": core.fmt_amount(market, sell_amt),
        "price": core.fmt_price(market, sl_price),              # سعر تنفيذ الأمر (Limit)
        "triggerType": "price",
        "triggerReference": "lastTrade",
        "triggerPrice": core.fmt_price(market, trigger_price),  # سعر التفعيل
        "timeInForce": "GTC",
        "responseRequired": True,
        "operator": "lte"  # اتجاه التريغر للـ SL بيع
    }

    sl_oid = None
    last_resp = None

    # A) مع operatorId مربوط بأمر الشراء
    bodyA = dict(base_sl_body); bodyA["operatorId"] = buy_oid
    okA, respA = _send_sl(bodyA, "A/operatorId")
    if okA:
        sl_oid = (respA or {}).get("orderId"); last_resp = respA
    else:
        last_resp = respA
        # إذا المنصة لا تقبل 'operator' كحقل، احذفه للمحاولات التالية
        try:
            if "operator" in bodyA and "operator" in str(respA.get("error","")).lower():
                base_sl_body.pop("operator", None)
        except Exception:
            pass

    # B) جرّب clientOrderId بدل operatorId
    if not sl_oid:
        bodyB = dict(base_sl_body); bodyB["clientOrderId"] = buy_oid
        okB, respB = _send_sl(bodyB, "B/clientOrderId")
        if okB:
            sl_oid = (respB or {}).get("orderId"); last_resp = respB
        else:
            last_resp = respB

    # C) بدون أي ربط
    if not sl_oid:
        bodyC = dict(base_sl_body)
        okC, respC = _send_sl(bodyC, "C/no-link")
        if okC:
            sl_oid = (respC or {}).get("orderId"); last_resp = respC
        else:
            last_resp = respC

    # خزّن الحالة للـ watchdog
    core.pos_set(market, {
        "avg": avg, "base": base_bought,
        "tp_oid": tp_oid, "tp_target": tp_price,
        "sl_oid": sl_oid, "sl_price": sl_price,
        "buy_oid": buy_oid
    })
    core.open_clear(market)

    # رسائل واضحة
    core.tg_send(
        "✅ BUY {m}\n"
        "Avg {a:.8f} | Base {b}\n"
        "TP {tp:.8f} (+{pct:.2f}%) — {why}\n"
        "SL {sl:.8f} (−2% ثابت)".format(
            m=market, a=avg, b=base_bought, tp=tp_price, pct=tp_pct, why=reason_txt, sl=sl_price
        )
    )
    if tp_oid:
        core.tg_send(f"🏷️ TP OID: {tp_oid}")
    else:
        core.tg_send(f"⚠️ فشل وضع TP — {json.dumps(tp_resp, ensure_ascii=False)[:300]}")
    if sl_oid:
        core.tg_send(f"🛡️ SL OID: {sl_oid}" + (" (linked to buy)" if buy_oid else ""))
    else:
        core.tg_send(f"⚠️ فشل وضع SL — {json.dumps(last_resp, ensure_ascii=False)[:300]}")
# ===== أوامر تيليغرام =====
def on_tg_command(core, text):
    t = (text or "").strip().lower()

    if t in ("restart", "reset", "ريستارت", "ريست", "اعادة", "إعادة"):
        try:
            res = core.reset_state()
            if res.get("ok"):
                core.tg_send("✅ تم مسح الحالة بالكامل. جاهز.")
                try: core.notify_ready("ALL-EUR", reason="emergency_reset", pnl_eur=None)
                except: pass
            else:
                core.tg_send(f"⚠️ فشل المسح: {res}")
        except Exception as e:
            core.tg_send(f"🐞 reset err: {type(e).__name__}: {e}")
        return

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
        core.tg_send(("✅ أمر بيع أُرسل" if ok else "⚠️ فشل البيع") + f" — {market}")
        if ok and oid:
            threading.Thread(target=_watch_manual_sell, args=(core, market, oid, amt), daemon=True).start()
        return

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
        core.tg_send(("✅ تم الإلغاء" if ok else "ℹ️ أوقفت المطاردة") + f" — status={final}")
        return

    core.tg_send("الأوامر: «بيع COIN [AMOUNT]» ، «الغ COIN» — الشراء عبر /hook")