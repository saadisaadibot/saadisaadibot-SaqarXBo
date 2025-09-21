# -*- coding: utf-8 -*-
# strategy.py — Zero-Latency Entry/Exit + Mirror-Exit (for Saqer core-1.3)

import os, time, json, threading, requests

# ===== إعدادات عامة =====
HEADROOM_EUR           = 0.30
DEBUG_BV               = False

# ===== نافذة دخول سريعة =====
ENTRY_MAX_WINDOW_SEC   = 8.0
ENTRY_REPRICE_MIN_TICK = 1
ENTRY_REPRICE_MAX_WAIT = 1.8
ENTRY_FAIL_COOLDOWN    = 0.35

# Flash mode
FLASH_SCORE_MIN        = 0.78
FLASH_ENTRY_WINDOW_SEC = 5.0
FLASH_REPRICE_MAX_WAIT = 1.2

# ===== TP ديناميكي =====
TP_MIN_PCT             = 0.25
TP_INIT_PCT_DEFAULT    = 0.70
TP_RATCHET_MIN         = 4
TP_DECAY_START_MIN     = 6
TP_DECAY_WINDOW_MIN    = 8
REPRICE_SEC            = 1.0
MIN_TICK_REPRICE       = 1
EDGE_TICKS_ABOVE_ASK   = 1

# ===== خروج طوارئ =====
FORCE_TAKER_AFTER_MIN  = 12
EMERGENCY_ONLY_PROFIT  = 0

# ===== Express Hint Sources =====
EXPRESS_URL = os.getenv("EXPRESS_URL","")  # مثال: http://express:8081
try:
    import redis
    R = redis.Redis.from_url(os.getenv("REDIS_URL",""), decode_responses=True, socket_timeout=2, socket_connect_timeout=2) if os.getenv("REDIS_URL") else None
except Exception:
    R = None

def read_hint(market: str) -> dict:
    # 1) Redis
    if R:
        try:
            s = R.get(f"express:signal:{market}")
            if s: return json.loads(s)
        except Exception:
            pass
    # 2) HTTP
    if EXPRESS_URL:
        try:
            r = requests.get(f"{EXPRESS_URL.rstrip('/')}/hint", params={"market": market}, timeout=2)
            j = r.json()
            if j.get("ok") and j.get("hint"): return j["hint"]
        except Exception:
            pass
    return {}

# ===== أدوات =====
_NOTIFY_MUTE = {}
def tg_once(core, key: str, text: str, window_sec: float = 20.0):
    now = time.time(); last = _NOTIFY_MUTE.get(key, 0)
    if now - last >= window_sec:
        _NOTIFY_MUTE[key] = now; core.tg_send(text)

def _tick(core, market: str) -> float:
    return core.price_tick(market) or (1.0 / (10 ** core.price_decimals(market)))

def _clip_sell_maker(core, market: str, target: float, bid: float, ask: float) -> float:
    if ask <= 0 or bid <= 0: return target
    return max(target, ask + EDGE_TICKS_ABOVE_ASK * _tick(core, market))

def _remaining_from_status(st: dict, fallback_amt: float) -> float:
    try:
        fa = float(st.get("filledAmount", 0) or 0.0)
        return max(0.0, fallback_amt - fa)
    except Exception:
        return fallback_amt

# ===== مؤشرات خفيفة لاختيار TP مبدئي =====
def _fetch_candles(core, market: str, interval="1m", limit=240):
    data = core.bv_request("GET", f"/{market}/candles?interval={interval}&limit={limit}")
    if not isinstance(data, list): return [], [], []
    highs = [float(r[2]) for r in data]
    lows  = [float(r[3]) for r in data]
    closes= [float(r[4]) for r in data]
    return highs, lows, closes

def _ema(vals, n):
    if len(vals) < n: return []
    k = 2.0 / (n + 1.0)
    out = [vals[0]]
    for v in vals[1:]: out.append(v*k + out[-1]*(1-k))
    return out

def _rsi(cl, n=14):
    if len(cl) < n+1: return 50.0
    gains=loss=0.0
    for i in range(-n,0):
        d = cl[i]-cl[i-1]
        gains += d if d>=0 else 0.0
        loss  += -d if d<0 else 0.0
    loss = loss if loss>0 else 1e-9
    rs = gains/loss
    return 100.0-(100.0/(1.0+rs))

def _choose_tp_pct(core, market: str, fallback_pct=TP_INIT_PCT_DEFAULT):
    _,_,closes = _fetch_candles(core, market, "1m", 240)
    if len(closes) < 210: return fallback_pct
    ema50  = _ema(closes, 50)[-1]
    ema200 = _ema(closes, 200)[-1]
    trend_up = ema50 > ema200
    rsi = _rsi(closes, 14)
    if rsi >= 75: base = 0.55
    elif rsi <= 40: base = 0.40 if not trend_up else 0.55
    else: base = 0.60 if trend_up else 0.50
    return max(TP_MIN_PCT, base)

# ===== مطاردة شراء سريعة =====
def chase_buy(core, market:str, spend_eur:float, entry_hint:float|None=None,
              max_window_sec:float=ENTRY_MAX_WINDOW_SEC, reprice_max_wait:float=ENTRY_REPRICE_MAX_WAIT) -> dict:
    last_oid=None; last_price=None
    start=time.time(); min_tick=_tick(core, market)
    while True:
        if time.time()-start >= max_window_sec:
            if last_oid:
                try: core.cancel_order_blocking(market, last_oid, wait_sec=2.0)
                except: pass
            return {"ok": False, "ctx":"entry_timeout"}

        bid, ask = core.get_best_bid_ask(market)
        px = max(bid, entry_hint or 0.0) if entry_hint else bid
        if px <= 0: time.sleep(0.15); continue

        amount = core.round_amount_down(market, spend_eur / max(px,1e-12))
        if amount < core.min_base(market):
            return {"ok": False, "ctx":"amount_too_small"}

        if last_oid:
            try: core.cancel_order_blocking(market, last_oid, wait_sec=2.0)
            except: pass

        _, resp = core.place_limit_postonly(market, "buy", px, amount)
        if isinstance(resp, dict) and resp.get("error"):
            time.sleep(ENTRY_FAIL_COOLDOWN); continue

        last_oid = resp.get("orderId"); last_price = px
        core.open_set(market, {"orderId": last_oid, "side":"buy", "amount_init": amount})

        t0=time.time()
        while True:
            st = core.order_status(market, last_oid)
            s  = (st or {}).get("status","").lower()
            if s in ("filled","partiallyfilled"):
                fb = float((st or {}).get("filledAmount",0) or 0)
                fq = float((st or {}).get("filledAmountQuote",0) or 0)
                avg = (fq/fb) if (fb>0 and fq>0) else last_price
                return {"ok": True, "status": s, "avg_price": avg, "filled_base": fb, "spent_eur": fq, "last_oid": last_oid}

            bid2, _ = core.get_best_bid_ask(market)
            if (bid2 > 0 and abs(bid2 - last_price) >= min_tick*ENTRY_REPRICE_MIN_TICK) or (time.time()-t0 >= reprice_max_wait):
                break
            time.sleep(0.12 if (time.time()-t0) < 3 else 0.25)

# ===== قفل ربح اختياري للكور =====
def maybe_move_sl(core, market:str, avg:float, base:float, current_bid:float, current_sl_price:float):
    try:
        if current_bid <= 0 or avg <= 0: return current_sl_price
        gain_pct = (current_bid/avg - 1.0)*100.0
        if gain_pct >= 0.8:  return max(current_sl_price or 0.0, avg*1.0025)
        if gain_pct >= 0.5:  return max(current_sl_price or 0.0, avg*1.0005)
        return current_sl_price
    except: return current_sl_price

# ===== TP Loop مع Mirror-Exit =====
def _tp_loop(core, market: str, entry: float, base_size: float,
             tp_init_price: float, init_oid: str|None, init_price: float|None):

    try:
        minb = core.min_base(market)
        amt  = core.round_amount_down(market, float(base_size))
        if amt < minb:
            core.tg_send(f"⛔ كمية غير كافية للبيع — {market}"); core.notify_ready(market, "no_amount", None); return

        last_oid   = init_oid or None
        last_price = float(init_price or 0.0)
        last_place_ts = time.time() if init_oid else 0.0
        start=time.time()

        tp_floor = entry * (1.0 + TP_MIN_PCT/100.0)
        tp_top   = max(tp_init_price, tp_floor)
        target   = max(tp_init_price, tp_floor)

        core.tg_send(f"🎯 TP — {market} | Entry {entry:.8f} | Init {tp_init_price:.8f} → Min {tp_floor:.8f}")

        phaseB=False
        while True:
            # Mirror exit: Express قال exit_now=1؟
            try:
                hint = read_hint(market)
                if hint and int(hint.get("exit_now",0)) == 1:
                    if last_oid:
                        try: core.cancel_order_blocking(market, last_oid, wait_sec=2.5)
                        except: pass
                    core.tg_send(f"🤖 Mirror Exit — {market}")
                    res = core.emergency_taker_sell(market, amt)
                    if res.get("ok"):
                        st2=res.get("response") or {}
                        try:
                            fq=float(st2.get("filledAmountQuote",0) or 0)
                            fa=float(st2.get("filledAmount",0) or 0)
                            avg_out=(fq/fa) if (fa>0 and fq>0) else (core.get_best_bid_ask(market)[0] or entry)
                        except:
                            avg_out=core.get_best_bid_ask(market)[0] or entry; fa=amt
                        pnl=(avg_out-entry)*fa
                        core.pos_clear(market); core.notify_ready(market,"mirror_exit", round(pnl,4))
                    else:
                        core.notify_ready(market,"mirror_exit_failed", None)
                    return
            except Exception:
                pass

            pos=core.pos_get(market) or {}
            if float(pos.get("base", amt) or amt) <= 0.0:
                core.notify_ready(market,"closed_external", None); return

            bid, ask = core.get_best_bid_ask(market)
            elapsed_min=int((time.time()-start)/60)

            if elapsed_min < max(1, TP_RATCHET_MIN):
                if ask>0:
                    cand=_clip_sell_maker(core, market, ask, bid, ask)
                    if cand>target: target=cand
                    if cand>tp_top: tp_top=cand
            else:
                if not phaseB:
                    core.tg_send(f"⤵️ Decay — {market}")
                    phaseB=True
                span=max(1, TP_DECAY_WINDOW_MIN)
                prog=min(1.0, (elapsed_min - TP_DECAY_START_MIN)/span)
                top_pct=(tp_top/entry) - 1.0
                floor_pct=(TP_MIN_PCT/100.0)
                t_pct=(1.0-prog)*top_pct + prog*floor_pct
                target=entry*(1.0+max(t_pct, floor_pct))
                if ask>0: target=_clip_sell_maker(core, market, target, bid, ask)

            tick=_tick(core, market)
            need_reprice=(abs(target-last_price) >= MIN_TICK_REPRICE*tick) or ((time.time()-last_place_ts) >= max(2.0, REPRICE_SEC*2))
            if need_reprice:
                if last_oid:
                    st=core.order_status(market, last_oid) or {}
                    if (st.get("status","").lower())=="filled":
                        try:
                            fq=float(st.get("filledAmountQuote",0) or 0); fa=float(st.get("filledAmount",0) or 0)
                            avg_out=(fq/fa) if (fa>0 and fq>0) else last_price
                        except:
                            avg_out=last_price; fa=amt
                        pnl=(avg_out-entry)*fa
                        core.pos_clear(market); core.notify_ready(market,"tp_filled", round(pnl,4)); return
                    amt = core.round_amount_down(market, _remaining_from_status(st, amt))
                    try: core.cancel_order_blocking(market, last_oid, wait_sec=3.5)
                    except: pass
                    last_oid=None; time.sleep(0.20)
                if amt < minb: core.notify_ready(market,"dust_leftover", None); return
                p_to_place=target if ask<=0 else _clip_sell_maker(core, market, target, bid, ask)
                _, resp = core.place_limit_postonly(market, "sell", p_to_place, amt)
                if isinstance(resp, dict) and not resp.get("error"):
                    last_oid=resp.get("orderId"); last_price=p_to_place; last_place_ts=time.time()
                else:
                    code=0
                    try: code=int(resp.get("errorCode",0))
                    except: pass
                    if code==216:
                        tg_once(core, f"216:{market}", f"🧯 BV216 — تعديل كمية")
                        amt = core.round_amount_down(market, amt*0.9995)
                    else:
                        tg_once(core, f"ERR:{market}:{code}", f"🩻 ERR sell {market}: {json.dumps(resp, ensure_ascii=False)[:200]}")
                    time.sleep(0.5)

            if FORCE_TAKER_AFTER_MIN and elapsed_min >= FORCE_TAKER_AFTER_MIN:
                if EMERGENCY_ONLY_PROFIT and (bid <= entry):
                    pass
                else:
                    if last_oid:
                        try: core.cancel_order_blocking(market, last_oid, wait_sec=2.5)
                        except: pass
                    core.tg_send(f"⚡ Taker Exit — {market}")
                    res=core.emergency_taker_sell(market, amt)
                    if res.get("ok"):
                        st2=res.get("response") or {}
                        try:
                            fq=float(st2.get("filledAmountQuote",0) or 0); fa=float(st2.get("filledAmount",0) or 0)
                            avg_out=(fq/fa) if (fa>0 and fq>0) else (core.get_best_bid_ask(market)[0] or entry)
                        except:
                            avg_out=core.get_best_bid_ask(market)[0] or entry; fa=amt
                        pnl=(avg_out-entry)*fa
                        core.pos_clear(market); core.notify_ready(market,"taker_emergency", round(pnl,4))
                    else:
                        core.notify_ready(market,"taker_failed", None)
                    return

            time.sleep(REPRICE_SEC)

    except Exception as e:
        core.tg_send(f"⛔ خطأ TP — {market}\n{type(e).__name__}: {e}")
        core.notify_ready(market, "tp_loop_error", None)

# ===== تنفيذ الشراء (/hook) =====
def on_hook_buy(core, coin:str):
    market = core.coin_to_market(coin)
    if not market:
        core.tg_send(f"⛔ سوق غير مدعوم — {coin}"); return

    eur_avail = core.balance("EUR")
    spend = max(0.0, eur_avail - HEADROOM_EUR)
    if spend <= 0:
        core.tg_send(f"⛔ EUR غير كافٍ (avail={eur_avail:.2f})")
        core.notify_ready(market,"buy_failed"); return

    # اقرأ Hint من Express
    hint = read_hint(market)
    entry_hint = float(hint.get("entry_hint") or 0.0) or None
    signal_score = float(hint.get("score") or 0.0) if hint else None
    fast = bool(int(hint.get("flash",0))) if hint else False

    max_window = FLASH_ENTRY_WINDOW_SEC if (fast or (signal_score and signal_score>=FLASH_SCORE_MIN)) else ENTRY_MAX_WINDOW_SEC
    reprice_wait = FLASH_REPRICE_MAX_WAIT if (fast or (signal_score and signal_score>=FLASH_SCORE_MIN)) else ENTRY_REPRICE_MAX_WAIT

    core.open_set(market, {"side":"buy", "abort": False})
    res = chase_buy(core, market, spend, entry_hint=entry_hint,
                    max_window_sec=max_window, reprice_max_wait=reprice_wait)
    if not res.get("ok"):
        core.tg_send(f"⚠️ فشل الدخول — {market}\n{json.dumps(res,ensure_ascii=False)}")
        core.notify_ready(market,"buy_failed"); return

    avg = float(res.get("avg_price") or 0.0)
    base_bought = float(res.get("filled_base") or 0.0)
    base_sym = market.split("-")[0]

    time.sleep(0.6)
    bal = core.balance(base_sym)
    if bal > base_bought:
        base_bought = core.round_amount_down(market, bal)

    minb = core.min_base(market)
    core.tg_send(f"🟢 BUY — {market}\nAvg={avg:.8f} | Base={base_bought} | minBase={minb}")

    if base_bought < minb:
        core.notify_ready(market,"buy_below_min")
        core.pos_set(market, {"avg": avg, "base": base_bought})
        core.open_clear(market); return

    # TP مبدئي
    tp_pct = _choose_tp_pct(core, market, fallback_pct=TP_INIT_PCT_DEFAULT)
    tp_init = avg * (1.0 + tp_pct/100.0)

    bid, ask = core.get_best_bid_ask(market)
    p0 = tp_init if ask<=0 else _clip_sell_maker(core, market, tp_init, bid, ask)

    _, tp_resp = core.place_limit_postonly(market, "sell", p0, base_bought)
    tp_oid = None
    if isinstance(tp_resp, dict) and not tp_resp.get("error"):
        tp_oid = tp_resp.get("orderId")
        core.tg_send(f"🏷️ TP وُضع — OID={tp_oid} @ {p0:.8f} (+{tp_pct:.2f}%)")
    else:
        core.tg_send(f"⚠️ فشل وضع TP — {json.dumps(tp_resp, ensure_ascii=False)[:240]}")

    core.pos_set(market, {"avg": avg, "base": base_bought, "tp_oid": tp_oid, "tp_target": p0, "sl_oid": None, "sl_price": 0.0})
    core.open_clear(market)

    threading.Thread(target=_tp_loop, args=(core, market, avg, base_bought, tp_init, tp_oid, p0), daemon=True).start()

# ===== أوامر تيليغرام =====
def on_tg_command(core, text):
    t=(text or "").strip().lower()

    if t in ("restart","reset","ريستارت","ريست","اعادة","إعادة"):
        try:
            res = core.reset_state()
            if res.get("ok"):
                core.tg_send("✅ تم مسح الحالة. جاهز.")
                try: core.notify_ready("ALL-EUR", reason="emergency_reset", pnl_eur=None)
                except: pass
            else:
                core.tg_send(f"⚠️ فشل المسح: {res}")
        except Exception as e:
            core.tg_send(f"🐞 reset err: {type(e).__name__}: {e}")
        return

    if t.startswith("بيع"):
        parts = text.split()
        if len(parts)<2:
            core.tg_send("صيغة: بيع COIN [AMOUNT]"); return
        coin=parts[1].upper().strip()
        market = core.coin_to_market(coin)
        if not market: core.tg_send("⛔ عملة غير صالحة."); return
        amt=None
        if len(parts)>=3:
            try: amt=float(parts[2])
            except: amt=None
        if amt is None:
            base=market.split("-")[0]; bal=core.balance(base)
            amt=core.round_amount_down(market, bal)
        ask=core.get_best_bid_ask(market)[1]
        _, resp = core.place_limit_postonly(market, "sell", ask, amt)
        ok = isinstance(resp, dict) and not resp.get("error")
        core.tg_send(("✅ بيع أُرسل" if ok else "⚠️ فشل البيع")+f" — {market}")
        return

    if t.startswith("الغ"):
        parts=text.split()
        if len(parts)<2:
            core.tg_send("صيغة: الغ COIN"); return
        coin=parts[1].upper().strip()
        market=core.coin_to_market(coin)
        if not market: core.tg_send("⛔ عملة غير صالحة."); return
        info=core.open_get(market) or {}
        info["abort"]=True; core.open_set(market, info)
        if info.get("orderId"):
            ok, final, last = core.cancel_order_blocking(market, info["orderId"], wait_sec=12.0)
            if ok: core.open_clear(market)
            core.tg_send(("✅ تم الإلغاء" if ok else "ℹ️ أوقفت المطاردة")+f" — status={final}")
        else:
            core.tg_send("ℹ️ أوقفت المطاردة (لا يوجد OID)")
        return

    core.tg_send("الأوامر: «بيع COIN [AMOUNT]» ، «الغ COIN» — الشراء عبر /hook")