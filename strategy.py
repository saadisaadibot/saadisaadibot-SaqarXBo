# -*- coding: utf-8 -*-
# strategy.py — TP ديناميكي + فوز فاست-فيل (بيع ماركت/تايكر عندما نفقد الأمل)

import time, json, threading

# ===== إعدادات عامة =====
HEADROOM_EUR = 0.30
DEBUG_BV     = True

# ===== TP ديناميكي =====
TP_INIT_PCT            = 0.70
TP_DECAY_START_MIN     = 6       # كان 15
TP_DECAY_WINDOW_MIN    = 6       # كان 10
TP_MIN_PCT             = 0.25
REPRICE_SEC            = 1.0
MIN_TICK_REPRICE       = 1
EDGE_TICKS_ABOVE_ASK   = 0       # صف على الـAsk (مش فوقه)
CLIP_TO_BOOK           = 1

# ===== فقدان الأمل / فيوزات بيع تايكر =====
FORCE_TAKER_AFTER_MIN  = 10      # قطع نهائي بعد هالمدة (0 لتعطيل)
ORDER_STALE_SEC        = 25      # إذا الأمر عمره >= 25s
BID_GAP_TICKS          = 2       # والفارق ≤ 2 ticks ⇒ Taker سريع
MAX_HOPE_MIN           = 18      # قطع إضافي لو تعطّلت شروط ثانية

ERROR_BACKOFF_SEC      = 0.8

# ===== مؤشرات مساعدة خفيفة لاختيار TP =====
def _fetch_candles(core, market: str, interval="1m", limit=240):
    data = core.bv_request("GET", f"/{market}/candles?interval={interval}&limit={limit}")
    if not isinstance(data, list): return [], [], []
    highs=[float(r[2]) for r in data]; lows=[float(r[3]) for r in data]; closes=[float(r[4]) for r in data]
    return highs, lows, closes

def _series_ema(values, period):
    if len(values) < period: return []
    k = 2.0 / (period + 1.0); out=[values[0]]
    for v in values[1:]: out.append(v*k + out[-1]*(1-k))
    return out

def _rsi(closes, period=14):
    if len(closes) < period+1: return None
    g=l=0.0
    for i in range(-period,0):
        d=closes[i]-closes[i-1]; g += d if d>=0 else 0.0; l += -d if d<0 else 0.0
    avg_gain=g/period; avg_loss=(l/period) if l>0 else 1e-9; rs=avg_gain/avg_loss
    return 100.0 - (100.0/(1.0+rs))

def _atr(highs,lows,closes,period=14):
    n=len(closes); 
    if n < period+1: return None
    trs=[]; 
    for i in range(1,n): trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    atr=sum(trs[:period])/period
    for tr in trs[period:]: atr=(atr*(period-1)+tr)/period
    return atr

def _adx(highs,lows,closes,period=14):
    n=len(closes); 
    if n < period+1: return None
    plus_dm=[]; minus_dm=[]; tr_list=[]
    for i in range(1,n):
        up=highs[i]-highs[i-1]; dn=lows[i-1]-lows[i]
        plus_dm.append(up if (up>dn and up>0) else 0.0)
        minus_dm.append(dn if (dn>up and dn>0) else 0.0)
        tr_list.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    tr14=sum(tr_list[:period]); pDM14=sum(plus_dm[:period]); mDM14=sum(minus_dm[:period])
    for i in range(period,len(tr_list)):
        tr14 = tr14 - (tr14/period) + tr_list[i]
        pDM14= pDM14- (pDM14/period)+ plus_dm[i]
        mDM14= mDM14- (mDM14/period)+ minus_dm[i]
    if tr14 <= 0: return 0.0
    pDI=(pDM14/tr14)*100.0; mDI=(mDM14/tr14)*100.0
    dx=(abs(pDI-mDI)/max(pDI+mDI,1e-9))*100.0
    return dx

def _market_regime(core, market: str):
    highs,lows,closes=_fetch_candles(core, market, "1m", 240)
    if len(closes)<210:
        return {"ok": False, "tp_pct": TP_INIT_PCT, "trend_up": False, "adx": 0.0, "rsi": 50.0}
    ema50=_series_ema(closes,50)[-1]; ema200=_series_ema(closes,200)[-1]
    trend_up = ema50>ema200; rsi=_rsi(closes,14) or 50.0
    atr=_atr(highs,lows,closes,14) or 0.0; last=closes[-1]
    atr_pct=(atr/last) if (atr and last>0) else 0.0
    adx_val=_adx(highs,lows,closes,14) or 0.0
    if adx_val < 18: tp_pct = 0.40
    elif adx_val < 22: tp_pct = 0.55 if trend_up else 0.45
    elif adx_val < 28: tp_pct = 0.80 if trend_up else 0.60
    else: tp_pct = 1.00 if trend_up else 0.70
    if rsi >= 75: tp_pct = min(tp_pct, 0.70)
    if rsi <= 40: tp_pct = max(tp_pct, 0.50 if trend_up else 0.40)
    tp_pct += min(0.30, (atr_pct*1000)*0.06)
    tp_pct = max(TP_MIN_PCT, tp_pct)
    return {"ok": True, "tp_pct": tp_pct, "trend_up": trend_up, "adx": adx_val, "rsi": rsi}

def choose_tp_price(core, market: str, avg_price: float):
    reg=_market_regime(core, market); tp_pct=reg.get("tp_pct", TP_INIT_PCT)
    return avg_price*(1.0+tp_pct/100.0), reg

# ===== Anti-spam =====
_NOTIFY_MUTE={}
def tg_once(core, key, text, window_sec=20.0):
    now=time.time(); last=_NOTIFY_MUTE.get(key,0)
    if now-last >= window_sec:
        _NOTIFY_MUTE[key]=now; core.tg_send(text)

# ===== SL (غير مستخدم) =====
def maybe_move_sl(core, market, avg, base, current_bid, current_sl_price): return current_sl_price

# ===== أدوات =====
def _clip_to_orderbook_sell(core, market, target_price, bid, ask):
    if ask<=0 or bid<=0: return target_price
    tick = core.price_tick(market) or (1.0 / (10 ** core.price_decimals(market)))
    return max(target_price, ask + EDGE_TICKS_ABOVE_ASK * tick)

def _remaining_from_status(st, fallback_amt):
    try:
        fa=float(st.get("filledAmount",0) or 0.0); return max(0.0, fallback_amt - fa)
    except Exception: return fallback_amt

# ===== TP Loop مع فيوزات Taker =====
def _dynamic_tp_loop(core, market, entry, base_size, tp_init_price, init_oid, init_price):
    try:
        minb=core.min_base(market)
        amt = core.round_amount_down(market, float(base_size))
        if amt < minb:
            core.tg_send(f"⛔ لا توجد كمية كافية للبيع — {market} (amt={amt}, min={minb})")
            core.notify_ready(market, reason="no_amount", pnl_eur=None); return

        start=time.time()
        last_oid   = init_oid or None
        last_price = float(init_price or 0.0)
        last_place_ts = time.time() if init_oid else 0.0

        tp_floor_price = entry*(1.0 + TP_MIN_PCT/100.0)
        tp_top_price   = max(tp_init_price, tp_floor_price)
        current_target = max(tp_init_price, tp_floor_price)

        core.tg_send(
            f"🎯 بدء TP ديناميكي — {market}\n"
            f"Entry {entry:.8f} | Amt {amt}\n"
            f"InitTP {tp_init_price:.8f} → MinTP {tp_floor_price:.8f}"
            + (f"\n📌 OID: {last_oid}" if last_oid else "")
        )
        phaseB_announced=False

        while True:
            pos = core.pos_get(market) or {}
            if float(pos.get("base", amt) or amt) <= 0.0:
                core.notify_ready(market, reason="closed_external", pnl_eur=None); return

            bid, ask = core.get_best_bid_ask(market)
            now=time.time(); elapsed_min=int((now-start)/60)
            tick = core.price_tick(market) or (1.0 / (10 ** core.price_decimals(market)))

            # Phase A: ratchet-up
            if elapsed_min < TP_DECAY_START_MIN:
                if ask>0:
                    candidate = ask + EDGE_TICKS_ABOVE_ASK * tick
                    if candidate > current_target: current_target = candidate
                    if candidate > tp_top_price:   tp_top_price   = candidate
            # Phase B: decay
            else:
                if not phaseB_announced:
                    core.tg_send(f"⤵️ بدء الانكماش — {market} (من {tp_top_price:.8f} نحو ≥{tp_floor_price:.8f})")
                    phaseB_announced=True
                span=max(1, TP_DECAY_WINDOW_MIN)
                prog=min(1.0, (elapsed_min-TP_DECAY_START_MIN)/span)
                top_pct=(tp_top_price/entry)-1.0; floor_pct=(TP_MIN_PCT/100.0)
                target_pct=(1.0-prog)*top_pct + prog*floor_pct
                current_target = entry*(1.0 + max(target_pct, floor_pct))
                if CLIP_TO_BOOK and ask>0:
                    current_target = _clip_to_orderbook_sell(core, market, current_target, bid, ask)

            # إعادة تسعير
            need_reprice = (abs(current_target - last_price) >= (MIN_TICK_REPRICE*tick)) or \
                           ((time.time() - last_place_ts) >= max(3.0, REPRICE_SEC*2))

            if need_reprice:
                # تحقق من الامتلاء
                if last_oid:
                    st = core.order_status(market, last_oid) or {}
                    s  = (st.get("status") or "").lower()
                    if s == "filled":
                        try:
                            fq=float(st.get("filledAmountQuote",0) or 0)
                            fa=float(st.get("filledAmount",0) or 0)
                            avg_out=(fq/fa) if (fa>0 and fq>0) else last_price
                        except:
                            avg_out=last_price; fa=amt
                        pnl_eur=(avg_out-entry)*fa
                        core.tg_send(f"🏁 TP مكتمل — {market} @ {avg_out:.8f} | PnL≈€{pnl_eur:.2f}")
                        core.pos_clear(market)
                        core.notify_ready(market, reason="tp_filled", pnl_eur=round(pnl_eur,4)); return

                    # حدّث المتبقي وألغ القديم
                    amt = core.round_amount_down(market, _remaining_from_status(st, amt))
                    try: core.cancel_order_blocking(market, last_oid, wait_sec=4.0)
                    except: pass
                    last_oid=None; time.sleep(0.25)

                # ضع أمر جديد
                p_to_place=current_target
                if CLIP_TO_BOOK and ask>0:
                    p_to_place = _clip_to_orderbook_sell(core, market, current_target, bid, ask)

                if amt < minb:
                    core.tg_send(f"ℹ️ الكمية المتبقية تحت الحد الأدنى — {market} (amt={amt}, min={minb})")
                    core.notify_ready(market, reason="dust_leftover", pnl_eur=None); return

                _, resp = core.place_limit_postonly(market, "sell", p_to_place, amt)
                if isinstance(resp, dict) and not resp.get("error"):
                    last_oid=resp.get("orderId"); last_price=p_to_place; last_place_ts=time.time()
                else:
                    try: code=int(resp.get("errorCode",0))
                    except Exception: code=0
                    if code==216:
                        tg_once(core, f"216:{market}", f"🧯 BV 216 — {market}: insufficient balance (تحرير رزرف)")
                        # نزّل الكمية شعرة
                        dec = amt * 0.0005
                        amt = core.round_amount_down(market, max(minb, amt - dec))
                    else:
                        tg_once(core, f"ERR:{market}:{code}",
                                f"🩻 BV ERR place sell @{market}: {json.dumps(resp, ensure_ascii=False)[:220]}")
                    time.sleep(ERROR_BACKOFF_SEC)

            # ======= فيوزات فقدان الأمل (تايكر) =======
            # 1) قريب من البيد وعمر الأمر طويل
            if last_oid and bid>0 and tick>0:
                age = time.time() - last_place_ts
                gap_ticks = max(0, int((current_target - bid)/tick))
                if age >= ORDER_STALE_SEC and gap_ticks <= BID_GAP_TICKS:
                    try: core.cancel_order_blocking(market, last_oid, wait_sec=2.0)
                    except: pass
                    core.tg_send(f"⚡ تحويل إلى Taker (قريب من الـBid) — {market}")
                    res = core.emergency_taker_sell(market, amt)
                    if res.get("ok"):
                        st2=res.get("response") or {}
                        try:
                            fq=float(st2.get("filledAmountQuote",0) or 0)
                            fa=float(st2.get("filledAmount",0) or 0)
                            avg_out=(fq/fa) if (fa>0 and fq>0) else (bid or entry)
                        except:
                            avg_out=bid or entry; fa=amt
                        pnl_eur=(avg_out-entry)*fa
                        core.pos_clear(market)
                        core.notify_ready(market, reason="taker_fastgap", pnl_eur=round(pnl_eur,4))
                    else:
                        core.notify_ready(market, reason="taker_failed", pnl_eur=None)
                    return

            # 2) مهلة قصوى
            if (FORCE_TAKER_AFTER_MIN and elapsed_min >= FORCE_TAKER_AFTER_MIN) or (elapsed_min >= MAX_HOPE_MIN):
                if last_oid:
                    try: core.cancel_order_blocking(market, last_oid, wait_sec=3.0)
                    except: pass
                core.tg_send(f"⚡ خروج طوارئ Taker (مهلة) — {market}")
                res = core.emergency_taker_sell(market, amt)
                if res.get("ok"):
                    st2=res.get("response") or {}
                    try:
                        fq=float(st2.get("filledAmountQuote",0) or 0)
                        fa=float(st2.get("filledAmount",0) or 0)
                        avg_out=(fq/fa) if (fa>0 and fq>0) else (bid or entry)
                    except:
                        avg_out=bid or entry; fa=amt
                    pnl_eur=(avg_out-entry)*fa
                    core.pos_clear(market)
                    core.notify_ready(market, reason="taker_emergency", pnl_eur=round(pnl_eur,4))
                else:
                    core.notify_ready(market, reason="taker_failed", pnl_eur=None)
                return

            time.sleep(REPRICE_SEC)

    except Exception as e:
        core.tg_send(f"⛔ خطأ في TP loop — {market}\n{type(e).__name__}: {e}")
        core.notify_ready(market, reason="tp_loop_error", pnl_eur=None)

# ===== تنفيذ الشراء =====
def on_hook_buy(core, coin:str):
    market = core.coin_to_market(coin)
    if not market:
        core.tg_send(f"⛔ سوق غير مدعوم — {coin}"); return

    eur_avail = core.balance("EUR")
    spend = max(0.0, eur_avail - HEADROOM_EUR)
    if spend <= 0:
        core.tg_send(f"⛔ لا يوجد EUR كافٍ (avail={eur_avail:.2f})")
        core.notify_ready(market,"buy_failed"); return

    core.open_set(market, {"side":"buy", "abort": False})
    res = chase_buy(core, market, spend)
    if not res.get("ok"):
        core.tg_send(f"⚠️ فشل الشراء — {market}\n{json.dumps(res,ensure_ascii=False)}")
        core.notify_ready(market,"buy_failed"); return

    avg=float(res.get("avg_price") or 0.0)
    base_bought=float(res.get("filled_base") or 0.0)
    base_sym=market.split("-")[0]

    time.sleep(1.0); bal=core.balance(base_sym)
    if bal > base_bought: base_bought = core.round_amount_down(market, bal)

    minb = core.min_base(market)
    core.tg_send(f"🟢 BUY filled — {market}\nAvg={avg:.8f} | Base={base_bought} | minBase={minb}")
    if base_bought < minb:
        core.tg_send(f"⚠️ كمية أقل من الحد الأدنى — لا أستطيع وضع TP. (base={base_bought}, min={minb})")
        core.notify_ready(market,"buy_below_min")
        core.pos_set(market, {"avg": avg, "base": base_bought})
        core.open_clear(market); return

    # TP مبدئي ذكي
    tp_price_init, reg = choose_tp_price(core, market, avg)
    tp_pct = round((tp_price_init/avg - 1.0)*100.0, 2)
    reasons=[]
    if reg:
        if reg.get("trend_up"): reasons.append("EMA50>EMA200")
        reasons.append(f"ADX={reg.get('adx',0):.1f}")
        reasons.append(f"RSI={reg.get('rsi',0):.0f}")

    bid, ask = core.get_best_bid_ask(market)
    tick = core.price_tick(market) or (1.0 / (10 ** core.price_decimals(market)))
    p0 = tp_price_init
    if CLIP_TO_BOOK and ask>0:
        p0 = max(tp_price_init, ask + EDGE_TICKS_ABOVE_ASK*tick)

    _, tp_resp = core.place_limit_postonly(market, "sell", p0, base_bought)
    tp_oid=None
    if isinstance(tp_resp, dict) and not tp_resp.get("error"):
        tp_oid = tp_resp.get("orderId")
        core.tg_send(f"🏷️ TP مبدئي وُضع — OID={tp_oid} @ {p0:.8f}")
    else:
        core.tg_send(f"⚠️ فشل وضع TP المبدئي — {json.dumps(tp_resp, ensure_ascii=False)[:250]}")

    core.pos_set(market, {"avg": avg, "base": base_bought,
                          "tp_oid": tp_oid, "tp_target": p0,
                          "sl_oid": None, "sl_price": 0.0})
    core.open_clear(market)

    core.tg_send(
        "✅ BUY {m}\nAvg {a:.8f} | Base {b}\nInit TP {tp:.8f} (+{pct:.2f}%) — {why}\nSL: ❌ (غير مفعّل)".format(
            m=market, a=avg, b=base_bought, tp=p0, pct=tp_pct, why=(", ".join(reasons) or "-")
        )
    )

    threading.Thread(target=_dynamic_tp_loop,
                     args=(core, market, avg, base_bought, tp_price_init, tp_oid, p0),
                     daemon=True).start()

# ===== مطاردة شراء Maker (كما هي) =====
def chase_buy(core, market:str, spend_eur:float)->dict:
    last_oid=None; last_price=None
    min_tick = float(1.0 / (10 ** core.price_decimals(market)))
    while True:
        bid, _ = core.get_best_bid_ask(market)
        if bid <= 0: time.sleep(0.25); continue
        price = bid
        amount = core.round_amount_down(market, spend_eur / max(price,1e-12))
        if amount < core.min_base(market): return {"ok": False, "ctx":"amount_too_small"}
        if last_oid:
            try: core.cancel_order_blocking(market, last_oid, wait_sec=2.5)
            except: pass
        _, resp = core.place_limit_postonly(market, "buy", price, amount)
        if isinstance(resp, dict) and resp.get("error"): time.sleep(0.35); continue
        last_oid = resp.get("orderId"); last_price = price
        core.open_set(market, {"orderId": last_oid, "side":"buy", "amount_init": amount})
        t0 = time.time()
        while True:
            st = core.order_status(market, last_oid)
            s  = (st or {}).get("status","").lower()
            if s in ("filled","partiallyfilled"):
                fb=float((st or {}).get("filledAmount",0) or 0); fq=float((st or {}).get("filledAmountQuote",0) or 0)
                avg=(fq/fb) if (fb>0 and fq>0) else last_price
                return {"ok": True, "status": s, "avg_price": avg, "filled_base": fb, "spent_eur": fq, "last_oid": last_oid}
            bid2, _ = core.get_best_bid_ask(market)
            reprice_due = (bid2 > 0 and abs(bid2 - last_price) >= min_tick) or (time.time()-t0 >= 2.0)
            if reprice_due: break
            time.sleep(0.18 if (time.time()-t0) < 4 else 0.35)

# ===== أوامر تيليغرام =====
def on_tg_command(core, text):
    t=(text or "").strip().lower()

    if t in ("restart", "reset", "ريستارت", "ريست", "اعادة", "إعادة"):
        try:
            res=core.reset_state()
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
        parts=text.split()
        if len(parts)<2:
            core.tg_send("صيغة: بيع COIN [AMOUNT]"); return
        coin=parts[1].upper().strip(); market=core.coin_to_market(coin)
        if not market: core.tg_send("⛔ عملة غير صالحة."); return
        if len(parts) >= 3:
            try: amt=float(parts[2])
            except: amt=None
        else:
            amt=None
        if amt is None:
            base = market.split("-")[0]; bal = core.balance(base)
            amt = core.round_amount_down(market, bal)
        # خروج Taker فوري
        res=core.emergency_taker_sell(market, amt)
        ok=bool(res.get("ok"))
        core.tg_send(("✅ أمر بيع Taker" if ok else "⚠️ فشل البيع") + f" — {market}")
        return

    if t.startswith("الغ"):
        parts=text.split()
        if len(parts)<2:
            core.tg_send("صيغة: الغ COIN"); return
        coin=parts[1].upper().strip(); market=core.coin_to_market(coin)
        if not market: core.tg_send("⛔ عملة غير صالحة."); return
        info=core.open_get(market) or {}; info["abort"]=True; core.open_set(market, info)
        if info.get("orderId"):
            ok, final, last = core.cancel_order_blocking(market, info["orderId"], wait_sec=12.0)
            if ok: core.open_clear(market)
            core.tg_send(("✅ تم الإلغاء" if ok else "ℹ️ أوقفت المطاردة") + f" — status={final}")
        else:
            core.tg_send("ℹ️ أوقفت المطاردة (لا يوجد OID)")
        return

    core.tg_send("الأوامر: «بيع COIN [AMOUNT]» ، «الغ COIN» — الشراء عبر /hook")