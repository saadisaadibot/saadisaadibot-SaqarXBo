# -*- coding: utf-8 -*-
"""
Saqer X — Maker-Only Executor (Bitvavo/EUR)
- شراء/بيع Maker (postOnly) بالدقة الصحيحة + تجميع partial fills.
- Backoff لخطأ 216، ومنع السبام بإيقاع محاولات حقيقي.
- يلغي كل الأوامر المعلّقة تلقائياً عند الفشل/الإغلاق.
- “الصبر” ديناميكي لكل ماركت.
- يشغّل الشراء فقط من بوت الإشارة عبر /hook.
- باقي الأوامر عبر تيليجرام: /summary /enable /disable /close
"""

import os, re, time, json, math, traceback
import requests, websocket, redis
from uuid import uuid4
from threading import Thread, Lock
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ================== Boot / ENV ==================
load_dotenv()
app = Flask(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID     = os.getenv("CHAT_ID", "").strip()

API_KEY     = os.getenv("BITVAVO_API_KEY", "").strip()
API_SECRET  = os.getenv("BITVAVO_API_SECRET", "").strip()

REDIS_URL   = os.getenv("REDIS_URL", "").strip()
r           = redis.from_url(REDIS_URL) if REDIS_URL else redis.Redis()

PORT        = int(os.getenv("PORT", "8080"))
RUN_LOCAL   = os.getenv("RUN_LOCAL", "0") == "1"

BASE_URL    = "https://api.bitvavo.com/v2"
WS_URL      = "wss://ws.bitvavo.com/v2/"

# ======= ضبط حماية الرصيد والسرعات =======
BUY_MIN_EUR           = 5.0
EST_FEE_RATE          = float(os.getenv("FEE_RATE_EST", "0.0025"))     # ≈0.25%
HEADROOM_EUR_MIN      = float(os.getenv("HEADROOM_EUR_MIN", "0.50"))   # ≥0.50€
MAX_SPEND_FRACTION    = float(os.getenv("MAX_SPEND_FRACTION", "0.90"))
FIXED_EUR_PER_TRADE   = float(os.getenv("FIXED_EUR", "0"))             # 0=تعطيل

MAKER_REPRICE_EVERY   = float(os.getenv("MAKER_REPRICE_EVERY", "2.0"))
MAKER_REPRICE_THRESH  = float(os.getenv("MAKER_REPRICE_THRESH", "0.0005")) # 0.05%

MAKER_WAIT_BASE_SEC   = int(os.getenv("MAKER_WAIT_BASE_SEC", "45"))
MAKER_WAIT_MAX_SEC    = int(os.getenv("MAKER_WAIT_MAX_SEC" , "300"))
MAKER_WAIT_STEP_UP    = int(os.getenv("MAKER_WAIT_STEP_UP" , "15"))
MAKER_WAIT_STEP_DOWN  = int(os.getenv("MAKER_WAIT_STEP_DOWN", "10"))

# محاولات واقعية
PLACE_THROTTLE_SEC    = float(os.getenv("PLACE_THROTTLE_SEC", "1.0"))  # فراغ زمني بين محاولات وضع الأوامر
CHECK_LOOP_SLEEP      = float(os.getenv("CHECK_LOOP_SLEEP", "0.35"))   # زمن بين فحوصات حالة الأمر

# Backoff لخطأ الرصيد 216
IB_BACKOFF_FACTOR     = float(os.getenv("IB_BACKOFF_FACTOR", "0.96"))  # كل فشل: -4%
IB_BACKOFF_TRIES      = int(os.getenv("IB_BACKOFF_TRIES", "5"))        # عدد المحاولات

# وقف متدرج حسب القمة
STOP_LADDER = [
    (0.0,  -2.0),
    (1.0,  -1.0),
    (2.0,   0.0),
    (3.0,  +1.0),
    (4.0,  +2.0),
    (5.0,  +3.0),
]

# ================== حالة التشغيل ==================
enabled         = True
active_trade    = None
executed_trades = []
lk              = Lock()

MARKET_MAP  = {}   # "ADA" -> "ADA-EUR"
MARKET_META = {}   # "ADA-EUR" -> {"minQuote","minBase","tick","step"}

_ws_prices = {}
_ws_lock   = Lock()

# ================== أدوات عامة ==================
def tg(text: str):
    try:
        if BOT_TOKEN and CHAT_ID:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": text}, timeout=10
            )
        else:
            print("TG>", text)
    except Exception as e:
        print("TG err:", e)

def _sig(ts, method, path, body=""):
    import hmac, hashlib
    return hmac.new(API_SECRET.encode(), f"{ts}{method}{path}{body}".encode(), hashlib.sha256).hexdigest()

def _req(method, path, body=None, timeout=12):
    url = f"{BASE_URL}{path}"
    ts  = str(int(time.time()*1000))
    bstr= "" if method=="GET" else json.dumps(body or {}, separators=(',',':'))
    headers = {
        "Bitvavo-Access-Key": API_KEY,
        "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Signature": _sig(ts, method, f"/v2{path}", bstr),
        "Bitvavo-Access-Window": "10000"
    }
    try:
        resp = requests.request(method, url, headers=headers,
                                json=(body if method!="GET" else None),
                                timeout=timeout)
        j = resp.json()
        if isinstance(j, dict) and j.get("error"):
            print("Bitvavo error:", j)
        return j
    except Exception as e:
        print("HTTP err:", e)
        return {"error":"request_failed"}

def get_eur_available():
    try:
        rows = _req("GET", "/balance")
        for x in rows or []:
            if x.get("symbol") == "EUR":
                return max(0.0, float(x.get("available", 0) or 0))
    except Exception:
        pass
    return 0.0

# ================== ماركت/دقة ==================
def load_markets():
    global MARKET_MAP, MARKET_META
    try:
        rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
        mm, meta = {}, {}
        for r0 in rows or []:
            if r0.get("quote") != "EUR": continue
            base  = r0.get("base"); market = r0.get("market")
            if not base or not market: continue
            mm[base.upper()] = market
            meta[market] = {
                "minQuote": float(r0.get("minOrderInQuoteAsset", BUY_MIN_EUR) or BUY_MIN_EUR),
                "minBase":  float(r0.get("minOrderInBaseAsset", 0) or 0.0),
                "tick":     float(r0.get("pricePrecision", 1e-6) or 1e-6),
                "step":     float(r0.get("amountPrecision", 1e-8) or 1e-8),
            }
        if mm:   MARKET_MAP  = mm
        if meta: MARKET_META = meta
    except Exception as e:
        print("load_markets err:", e)

def coin_to_market(coin: str):
    if not MARKET_MAP: load_markets()
    return MARKET_MAP.get(coin.upper())

def _decimals(step: float) -> int:
    try:
        if step >= 1: return 0
        return max(0, int(round(-math.log10(step))))
    except Exception:
        return 8

def _round_price(mkt, p):
    tick = (MARKET_META.get(mkt, {}) or {}).get("tick", 1e-6)
    decs = _decimals(tick)
    p2 = round(float(p), decs)
    return max(tick, p2)

def _round_amount(mkt, a):
    step = (MARKET_META.get(mkt, {}) or {}).get("step", 1e-8)
    a = math.floor(float(a)/step)*step
    decs = _decimals(step)
    return round(max(step, a), decs)

def _fmt_price(mkt, p):   return f"{_round_price(mkt, p):.{_decimals((MARKET_META.get(mkt, {}) or {}).get('tick',1e-6))}f}"
def _fmt_amount(mkt, a):  return f"{_round_amount(mkt, a):.{_decimals((MARKET_META.get(mkt, {}) or {}).get('step',1e-8))}f}"
def _min_quote(mkt):      return (MARKET_META.get(mkt, {}) or {}).get("minQuote", BUY_MIN_EUR)
def _min_base(mkt):       return (MARKET_META.get(mkt, {}) or {}).get("minBase", 0.0)

# ================== الأسعار ==================
def _ws_on_message(ws, msg):
    try:
        d = json.loads(msg)
        if d.get("event") == "ticker":
            m = d.get("market")
            p = d.get("price") or d.get("lastPrice") or d.get("open")
            p = float(p or 0)
            if p > 0:
                with _ws_lock: _ws_prices[m] = {"price": p, "ts": time.time()}
    except Exception:
        pass

def _ws_runner():
    while True:
        try:
            ws = websocket.WebSocketApp(WS_URL, on_message=_ws_on_message)
            ws.run_forever(ping_interval=25, ping_timeout=10)
        except Exception as e:
            print("WS err:", e)
        time.sleep(2)

Thread(target=_ws_runner, daemon=True).start()

def ws_sub(markets):
    try:
        ws = websocket.create_connection(WS_URL, timeout=5)
        ws.send(json.dumps({"action":"subscribe","channels":[{"name":"ticker","markets":markets}]}))
        ws.close()
    except Exception:
        pass

def fetch_price_ws_or_http(market: str, staleness=2.0):
    now = time.time()
    with _ws_lock:
        rec = _ws_prices.get(market)
    if rec and (now - rec["ts"]) <= staleness:
        return rec["price"]
    ws_sub([market])
    try:
        j = requests.get(f"{BASE_URL}/ticker/price?market={market}", timeout=6).json()
        p = float(j.get("price", 0) or 0)
        if p>0:
            with _ws_lock: _ws_prices[market] = {"price": p, "ts": now}
            return p
    except Exception:
        pass
    return None

def fetch_orderbook(market):
    try:
        j = requests.get(f"{BASE_URL}/{market}/book", timeout=6).json()
        if j and j.get("bids") and j.get("asks"):
            return j
    except Exception:
        pass
    return None

# ================== أوامر Maker ==================
def _place_limit_postonly(market, side, price, amount):
    body = {
        "market": market,
        "side": side,
        "orderType": "limit",
        "postOnly": True,
        "clientOrderId": str(uuid4()),
        "price": _fmt_price(market, price),
        "amount": _fmt_amount(market, amount),
        "operatorId": ""  # ✅ ضروري
    }
    return _req("POST", "/order", body)

def _fetch_order(orderId, market=None):
    if orderId: return _req("GET", f"/order?orderId={orderId}")
    return {"error":"no_order"}

def _cancel_order(orderId):
    if not orderId: return {"error":"no_order"}
    return _req("DELETE", f"/order?orderId={orderId}")

def cancel_all_open_orders(market: str):
    """يلغي كل أوامر هذا الماركت (احتياط عند الفشل/الإغلاق)."""
    try:
        _req("DELETE", f"/orders?market={market}")
    except Exception as e:
        print("cancel_all err:", e)

def totals_from_fills(fills):
    tb=tq=fee=0.0
    for f in (fills or []):
        amt=float(f["amount"]); pr=float(f["price"]); fe=float(f.get("fee",0) or 0)
        tb+=amt; tq+=amt*pr; fee+=fe
    return tb,tq,fee

# ================== “الصبر” ديناميكي ==================
def pat_key(m): return f"maker:patience:{m}"
def get_patience(m):
    try:
        v = r.get(pat_key(m))
        if v: return min(MAKER_WAIT_MAX_SEC, max(MAKER_WAIT_BASE_SEC, int(v)))
    except: pass
    return MAKER_WAIT_BASE_SEC
def bump_patience(m):
    try: r.set(pat_key(m), min(MAKER_WAIT_MAX_SEC, get_patience(m)+MAKER_WAIT_STEP_UP))
    except: pass
def relax_patience(m):
    try: r.set(pat_key(m), max(MAKER_WAIT_BASE_SEC, get_patience(m)-MAKER_WAIT_STEP_DOWN))
    except: pass

# ================== فتح شراء Maker ==================
def _calc_base_from_eur(market, eur, price):
    price = max(1e-12, float(price))
    base  = float(eur) / price
    base  = max(base, _min_base(market))
    return _round_amount(market, base)

def open_maker_buy(market: str, eur_amount: float):
    eur_avail = get_eur_available()

    # اختيار المبلغ
    if FIXED_EUR_PER_TRADE > 0:
        target = FIXED_EUR_PER_TRADE
    elif not eur_amount or eur_amount <= 0:
        target = eur_avail
    else:
        target = float(eur_amount)

    target = min(target, eur_avail * MAX_SPEND_FRACTION)
    minq   = _min_quote(market)
    buffer = max(HEADROOM_EUR_MIN, target * EST_FEE_RATE * 2.0, 0.05)
    spend  = min(target, max(0.0, eur_avail - buffer))

    if spend < max(minq, BUY_MIN_EUR):
        need = max(minq, BUY_MIN_EUR)
        tg(f"⛔ الرصيد غير كافٍ: متاح €{eur_avail:.2f} | بعد الهامش €{spend:.2f} "
           f"| هامش €{buffer:.2f} | المطلوب ≥ €{need:.2f}.")
        return None

    tg(f"💰 EUR متاح: €{eur_avail:.2f} | سننفق: €{spend:.2f} (هامش €{buffer:.2f})")

    patience = get_patience(market)
    started  = time.time()

    placed_orders = 0
    inner_attempts= 0
    last_order    = None
    last_bid      = None
    all_fills     = []
    remaining_eur = float(spend)
    last_place_ts = 0.0

    try:
        while (time.time()-started) < patience and remaining_eur >= (minq*0.999):
            ob = fetch_orderbook(market)
            if not ob or not ob.get("bids") or not ob.get("asks"):
                time.sleep(0.25); continue

            best_bid = float(ob["bids"][0][0])
            best_ask = float(ob["asks"][0][0])
            price    = _round_price(market, min(best_bid, best_ask*(1.0-1e-6)))

            # متابعة أمر قائم
            if last_order:
                st = _fetch_order(last_order); status = st.get("status")
                if status in ("filled","partiallyFilled"):
                    fills = st.get("fills", []) or []
                    if fills:
                        b,q,fee = totals_from_fills(fills)
                        all_fills += fills
                        remaining_eur = max(0.0, remaining_eur - (q + fee))
                if status == "filled" or remaining_eur < (minq*0.999):
                    try: _cancel_order(last_order)
                    except: pass
                    last_order=None
                    break

                # هل نحتاج إعادة تسعير؟
                if (last_bid is None) or (abs(best_bid/last_bid - 1.0) >= MAKER_REPRICE_THRESH):
                    try: _cancel_order(last_order)
                    except: pass
                    last_order=None
                else:
                    # فترة انتظار قصيرة قبل الفحص التالي
                    t0=time.time()
                    while time.time()-t0 < MAKER_REPRICE_EVERY:
                        st=_fetch_order(last_order); status=st.get("status")
                        if status in ("filled","partiallyFilled"):
                            fills=st.get("fills",[]) or []
                            if fills:
                                b,q,fee = totals_from_fills(fills)
                                all_fills += fills
                                remaining_eur = max(0.0, remaining_eur - (q+fee))
                            if status=="filled" or remaining_eur < (minq*0.999):
                                try:_cancel_order(last_order)
                                except: pass
                                last_order=None
                                break
                        time.sleep(CHECK_LOOP_SLEEP)
                    if last_order:
                        continue

            # وضع أمر جديد (Throttle + Backoff 216)
            if not last_order and remaining_eur >= (minq*0.999):
                # throttle
                wait_left = max(0.0, PLACE_THROTTLE_SEC - (time.time()-last_place_ts))
                if wait_left > 0: time.sleep(wait_left)

                attempt=0; placed=False; cur_price=price
                while attempt < IB_BACKOFF_TRIES and remaining_eur >= (minq*0.999):
                    inner_attempts += 1
                    amt_base = _calc_base_from_eur(market, remaining_eur, cur_price)
                    if amt_base <= 0: break

                    exp_eur = amt_base * cur_price
                    tg(f"🧪 محاولة شراء #{attempt+1}: amount={_fmt_amount(market, amt_base)} | "
                       f"سعر≈{_fmt_price(market, cur_price)} | EUR≈{exp_eur:.2f}")

                    res     = _place_limit_postonly(market, "buy", cur_price, amt_base)
                    orderId = (res or {}).get("orderId")
                    err_txt = str((res or {}).get("error","")).lower()

                    if orderId:
                        last_order = orderId
                        last_bid   = best_bid
                        placed     = True
                        placed_orders += 1
                        last_place_ts = time.time()
                        break

                    # Backoff لخطأ الرصيد
                    if "insufficient balance" in err_txt or "not have sufficient balance" in err_txt or "code': 216" in err_txt:
                        remaining_eur = remaining_eur * IB_BACKOFF_FACTOR
                        attempt += 1
                        time.sleep(0.25)
                        continue

                    # خطأ آخر → اخرج من الحلقة الداخلية
                    attempt = IB_BACKOFF_TRIES
                    break

                if not placed:
                    # لم نضع أمر فعلياً، أعطِ فرصة صغيرة قبل الدورة التالية
                    time.sleep(0.35)
                    continue

                # متابعة قصيرة بعد الوضع
                t0=time.time()
                while time.time()-t0 < MAKER_REPRICE_EVERY:
                    st=_fetch_order(last_order); status=st.get("status")
                    if status in ("filled","partiallyFilled"):
                        fills=st.get("fills",[]) or []
                        if fills:
                            b,q,fee = totals_from_fills(fills)
                            all_fills += fills
                            remaining_eur = max(0.0, remaining_eur - (q+fee))
                        if status=="filled" or remaining_eur < (minq*0.999):
                            try:_cancel_order(last_order)
                            except: pass
                            last_order=None
                            break
                    time.sleep(CHECK_LOOP_SLEEP)

        # تنظيف
        if last_order:
            try:_cancel_order(last_order)
            except: pass

    except Exception as e:
        print("open_maker_buy err:", e)

    # تقييم النتيجة
    if not all_fills:
        bump_patience(market)
        cancel_all_open_orders(market)
        elapsed = time.time()-started
        tg("⚠️ لم يكتمل شراء Maker.\n"
           f"• أوامر موضوعة: {placed_orders}\n"
           f"• محاولات داخلية: {inner_attempts}\n"
           f"• زمن: {elapsed:.1f}s\n"
           "سنرفع الصبر تلقائيًا وسنحاول لاحقًا.")
        return None

    base_amt, quote_eur, fee_eur = totals_from_fills(all_fills)
    if base_amt <= 0:
        bump_patience(market)
        cancel_all_open_orders(market)
        return None

    relax_patience(market)
    avg = (quote_eur + fee_eur) / base_amt
    return {"amount": base_amt, "avg": avg, "cost_eur": quote_eur + fee_eur, "fee_eur": fee_eur}

# ================== بيع Maker ==================
def close_maker_sell(market: str, amount: float):
    patience = get_patience(market)
    started  = time.time()
    remaining= float(amount)
    all_fills=[]; last_order=None; last_ask=None

    try:
        while (time.time()-started) < patience and remaining > 0:
            ob = fetch_orderbook(market)
            if not ob or not ob.get("bids") or not ob.get("asks"):
                time.sleep(0.25); continue

            best_bid=float(ob["bids"][0][0])
            best_ask=float(ob["asks"][0][0])
            price   = _round_price(market, max(best_ask, best_bid*(1.0+1e-6)))

            if last_order:
                st=_fetch_order(last_order); stt=st.get("status")
                if stt in ("filled","partiallyFilled"):
                    fills=st.get("fills",[]) or []
                    if fills:
                        b,_,_ = totals_from_fills(fills)
                        remaining=max(0.0, remaining-b)
                        all_fills += fills
                if stt=="filled" or remaining<=0:
                    try:_cancel_order(last_order)
                    except: pass
                    last_order=None
                    break

                if (last_ask is None) or (abs(best_ask/last_ask - 1.0) >= MAKER_REPRICE_THRESH):
                    try:_cancel_order(last_order)
                    except: pass
                    last_order=None
                else:
                    t0=time.time()
                    while time.time()-t0 < MAKER_REPRICE_EVERY:
                        st=_fetch_order(last_order); stt=st.get("status")
                        if stt in ("filled","partiallyFilled"):
                            fills=st.get("fills",[]) or []
                            if fills:
                                b,_,_ = totals_from_fills(fills)
                                remaining=max(0.0, remaining-b)
                                all_fills += fills
                            if stt=="filled" or remaining<=0:
                                try:_cancel_order(last_order)
                                except: pass
                                last_order=None
                                break
                        time.sleep(CHECK_LOOP_SLEEP)
                    if last_order: continue

            if remaining>0:
                amt=_round_amount(market, remaining)
                res=_place_limit_postonly(market, "sell", price, amt)
                oid=res.get("orderId")
                if not oid:
                    price2=_round_price(market, best_ask)
                    res=_place_limit_postonly(market, "sell", price2, amt)
                    oid=res.get("orderId")
                if not oid:
                    time.sleep(0.35); continue

                last_order=oid; last_ask=best_ask

                t0=time.time()
                while time.time()-t0 < MAKER_REPRICE_EVERY:
                    st=_fetch_order(last_order); stt=st.get("status")
                    if stt in ("filled","partiallyFilled"):
                        fills=st.get("fills",[]) or []
                        if fills:
                            b,_,_ = totals_from_fills(fills)
                            remaining=max(0.0, remaining-b)
                            all_fills += fills
                        if stt=="filled" or remaining<=0:
                            try:_cancel_order(last_order)
                            except: pass
                            last_order=None
                            break
                    time.sleep(CHECK_LOOP_SLEEP)

        if last_order:
            try:_cancel_order(last_order)
            except: pass

    except Exception as e:
        print("close_maker_sell err:", e)

    proceeds=fee=base=0.0
    for f in all_fills:
        a=float(f["amount"]); p=float(f["price"]); fe=float(f.get("fee",0) or 0)
        proceeds += a*p; fee += fe; base += a
    proceeds -= fee
    return base, proceeds, fee

# ================== مراقبة الوقف ==================
def dyn_stop_from_peak(peak):
    s=-999.0
    for th,val in STOP_LADDER:
        if peak>=th: s=val
    return s

def monitor_loop():
    global active_trade
    while True:
        try:
            with lk:
                at = active_trade.copy() if active_trade else None
            if not at:
                time.sleep(0.25); continue

            m=at["symbol"]; ent=at["entry"]
            cur = fetch_price_ws_or_http(m) or ent
            pnl = ((cur/ent)-1.0)*100.0

            new_peak = max(at["peak_pct"], pnl)
            new_sl   = dyn_stop_from_peak(new_peak)

            changed=False
            with lk:
                if active_trade:
                    if new_peak > active_trade["peak_pct"]+1e-9:
                        active_trade["peak_pct"]=new_peak; changed=True
                    if abs(new_sl - active_trade.get("dyn_stop_pct",-999.0))>1e-9:
                        active_trade["dyn_stop_pct"]=new_sl; changed=True
            if changed:
                tg(f"📈 تحديث: Peak={new_peak:.2f}% → SL {new_sl:+.2f}%")

            with lk:
                at2 = active_trade.copy() if active_trade else None
            if at2 and pnl <= at2.get("dyn_stop_pct",-2.0):
                do_close_maker("Dynamic stop"); time.sleep(0.5); continue

            time.sleep(0.12)
        except Exception as e:
            print("monitor err:", e)
            time.sleep(0.5)

Thread(target=monitor_loop, daemon=True).start()

# ================== تدفّق الصفقة ==================
def do_open_maker(market: str, eur: float):
    def run():
        global active_trade
        try:
            with lk:
                if active_trade:
                    tg("⛔ توجد صفقة نشطة. أغلقها أولاً."); return
            res = open_maker_buy(market, eur)
            if not res:
                tg("⏳ لم يكتمل شراء Maker. سنحاول لاحقًا (الصبر يتكيف تلقائياً).")
                return
            with lk:
                active_trade = {
                    "symbol": market,
                    "entry":  float(res["avg"]),
                    "amount": float(res["amount"]),
                    "cost_eur": float(res["cost_eur"]),
                    "buy_fee_eur": float(res["fee_eur"]),
                    "opened_at": time.time(),
                    "peak_pct": 0.0,
                    "dyn_stop_pct": -2.0
                }
                executed_trades.append(active_trade.copy())
            tg(f"✅ شراء {market.replace('-EUR','')} (Maker) @ €{active_trade['entry']:.8f} | "
               f"كمية {active_trade['amount']:.8f}")
        except Exception as e:
            traceback.print_exc()
            tg(f"🐞 خطأ أثناء الفتح: {e}")
    Thread(target=run, daemon=True).start()

def do_close_maker(reason=""):
    global active_trade
    try:
        with lk:
            if not active_trade: return
            m   = active_trade["symbol"]
            amt = float(active_trade["amount"])
            cost= float(active_trade["cost_eur"])
        # إلغاء وقائي لأي أوامر
        cancel_all_open_orders(m)
        sold, proceeds, fee = close_maker_sell(m, amt)
        with lk:
            pnl_eur = proceeds - cost
            pnl_pct = (proceeds/cost - 1.0)*100.0 if cost>0 else 0.0
            for t in reversed(executed_trades):
                if t["symbol"]==m and "exit_eur" not in t:
                    t.update({"exit_eur": proceeds, "sell_fee_eur": fee,
                              "pnl_eur": pnl_eur, "pnl_pct": pnl_pct,
                              "exit_time": time.time()})
                    break
            active_trade=None
        tg(f"💰 بيع {m.replace('-EUR','')} (Maker) | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%) {('— '+reason) if reason else ''}")
    except Exception as e:
        traceback.print_exc()
        tg(f"🐞 خطأ أثناء الإغلاق: {e}")

def summary_text():
    lines=[]
    with lk:
        at = active_trade
        closed=[x for x in executed_trades if "exit_eur" in x]
    if at:
        cur = fetch_price_ws_or_http(at["symbol"]) or at["entry"]
        pnl = ((cur/at["entry"])-1.0)*100.0
        lines.append("📌 صفقة نشطة:")
        lines.append(f"• {at['symbol'].replace('-EUR','')} @ €{at['entry']:.8f} | "
                     f"PnL {pnl:+.2f}% | Peak {at['peak_pct']:.2f}% | SL {at.get('dyn_stop_pct',-2.0):+.2f}%")
    else:
        lines.append("📌 لا صفقات نشطة.")
    pnl_eur=sum(float(x["pnl_eur"]) for x in closed)
    wins=sum(1 for x in closed if float(x.get("pnl_eur",0))>=0)
    lines.append(f"\n📊 صفقات مكتملة: {len(closed)} | محققة: {pnl_eur:+.2f}€ | ف/خ: {wins}/{len(closed)-wins}")
    lines.append("\n⚙️ buy=Maker | sell=Maker | وقف متدرّج: -2%→-1%→0%→+1%…")
    return "\n".join(lines)

# ================== Webhooks ==================
@app.route("/", methods=["GET"])
def health(): return "Saqer X — OK", 200

# — (1) إشارة الشراء فقط من بوت الإشارة
@app.route("/hook", methods=["POST"])
def hook_buy():
    """
    مثال JSON من بوت الإشارة:
    { "cmd":"buy", "coin":"ADA", "eur": 15.0 }   # eur اختياري
    """
    try:
        data = request.get_json(silent=True) or {}
        if (data.get("cmd") or "").lower() != "buy":
            return jsonify({"ok":False,"err":"only_buy_supported"}), 400
        coin = (data.get("coin") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{2,15}", coin or ""):
            return jsonify({"ok":False,"err":"bad_coin"}), 400
        mkt = coin_to_market(coin)
        if not mkt:
            tg(f"⛔ {coin}-EUR غير متاح.")
            return jsonify({"ok":False,"err":"market_unavailable"}), 404
        if not enabled:
            return jsonify({"ok":False,"err":"bot_disabled"}), 403
        eur = float(data.get("eur")) if data.get("eur") is not None else None
        do_open_maker(mkt, eur)
        return jsonify({"ok":True,"market":mkt})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok":False,"err":str(e)}), 500

# — (2) تيليجرام: أوامر إدارية فقط
@app.route("/tg", methods=["POST"])
def telegram_webhook():
    try:
        upd = request.get_json(silent=True) or {}
        msg = upd.get("message") or upd.get("edited_message") or {}
        text = (msg.get("text") or "").strip()
        cmd  = text.split()[0].lower()

        global enabled
        if cmd in ("/enable","enable"):
            enabled=True; tg("✅ تم التفعيل."); return jsonify(ok=True)
        if cmd in ("/disable","disable"):
            enabled=False; tg("🛑 تم الإيقاف."); return jsonify(ok=True)
        if cmd in ("/summary","summary"):
            tg(summary_text()); return jsonify(ok=True)
        if cmd in ("/close","close","/sell","sell","/exit","exit"):
            do_close_maker("Manual"); return jsonify(ok=True)

        tg("أوامر: /summary /close /enable /disable")
        return jsonify(ok=True)
    except Exception as e:
        traceback.print_exc()
        return jsonify(ok=False, err=str(e)), 200

# ================== Main ==================
if __name__ == "__main__" or RUN_LOCAL:
    load_markets()
    app.run(host="0.0.0.0", port=PORT)