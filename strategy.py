# -*- coding: utf-8 -*-
# strategy.py — الجزء القابل للتعديل
# يضع TP + SL(-2%) فور الشراء برسائل واضحة، ويراقب البيع اليدوي لإرسال Ready.

import time, json, threading
from strategy_base import (
    ADX_LEN, RSI_LEN, EMA_FAST, EMA_SLOW, ATR_LEN,
    TP_MIN_PCT, TP_MID_PCT, TP_MAX_PCT,
    chase_buy, choose_tp_price, infer_recent_avg_buy
)

# إعدادات سريعة (تُعدّل هنا فقط)
HEADROOM_EUR = 0.30
SL_FIXED_PCT = -2.0  # ثابت دائمًا

# ---------- SL trailing (مُعطَّل لأن SL رسمي ثابت) ----------
def maybe_move_sl(core, market:str, avg:float, base:float, current_bid:float, current_sl_price:float):
    return current_sl_price

# ---------- مراقبة بيع يدوي لإرسال Ready ----------
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

# ---------- on_hook_buy ----------
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
    base_sym = market.split("-")[0]

    # تسوية الرصيد (لو الامتلاء على دفعات) + تحسين المتوسط من آخر دقيقة
    bal = core.balance(base_sym)
    if bal > base_bought:
        base_bought = core.round_amount_down(market, bal)
    avg2 = infer_recent_avg_buy(core, market)
    if avg2 > 0: avg = avg2

    # اختر TP + علّل السبب بإيجاز
    tp_price, reg = choose_tp_price(core, market, avg)
    tp_pct = ((tp_price / avg) - 1.0) * 100.0 if avg > 0 else 0.0
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

    # ضع SL رسمي ثابت -2% (StopLossLimit) — limit تحت الـstop بشوي
    sl_stop  = avg * (1.0 + SL_FIXED_PCT/100.0)      # 98%
    sl_limit = sl_stop * 0.999
    sl_oid = None; sl_resp=None
    if sell_amt >= minb:
        _, sl_resp = core.place_stoploss_limit(market, sell_amt, sl_stop, sl_limit)
        if isinstance(sl_resp, dict) and not sl_resp.get("error"):
            sl_oid = sl_resp.get("orderId")

    # خزّن المركز للـwatchdog
    core.pos_set(market, {
        "avg": avg, "base": base_bought,
        "tp_oid": tp_oid, "tp_target": tp_price,
        "sl_oid": sl_oid, "sl_price": sl_stop
    })
    core.open_clear(market)

    # --- رسائل قصيرة مفهومة ---
    core.tg_send(
        f"✅ BUY {market}\n"
        f"Avg {avg:.8f} | Base {base_bought}\n"
        f"TP {tp_price:.8f} ({tp_pct:+.2f}%) — {reason_txt}\n"
        f"SL {sl_stop:.8f} (−2% ثابت)"
    )
    if tp_oid:
        core.tg_send(f"🏷️ TP OID: {tp_oid}")
    else:
        core.tg_send(f"⚠️ فشل وضع TP — {json.dumps(tp_resp, ensure_ascii=False)[:300]}")
    if sl_oid:
        core.tg_send(f"🛡️ SL OID: {sl_oid}")
    else:
        core.tg_send(f"⚠️ فشل وضع SL — {json.dumps(sl_resp, ensure_ascii=False)[:300]}")

# ---------- أوامر تيليغرام ----------
def on_tg_command(core, text):
    t = (text or "").strip().lower()

    # طوارئ
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

    # بيع يدوي (مع مراقبة جاهز)
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

    # إلغاء المطاردة الحالية
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