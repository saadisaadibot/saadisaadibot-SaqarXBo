# -*- coding: utf-8 -*-
"""
Saqer — Maker-Only (Bitvavo/EUR) + Telegram Webhook
- شراء/بيع Maker (postOnly) فقط مع تجميع partial fills
- وقف ديناميكي متدرّج
- أمر واحد نشط
- إصلاحات دقة السعر/الكمية + operatorId + عدم استخدام amountQuote للّيمِت
- Backoff ذكي لـ 216 + تنظيف أوامر معلّقة + تبطيء محاولات
- Webhook تلغرام: /buy COIN [eur], /close, /summary, /enable, /disable
"""

import os, re, time, json, math, traceback, hmac, hashlib
import requests, redis, websocket
from threading import Thread, Lock
from uuid import uuid4
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ========= Boot / ENV =========
load_dotenv()
app = Flask(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID     = os.getenv("CHAT_ID", "").strip()
API_KEY     = os.getenv("BITVAVO_API_KEY", "").strip()
API_SECRET  = os.getenv("BITVAVO_API_SECRET", "").strip()
REDIS_URL   = os.getenv("REDIS_URL")
PORT        = int(os.getenv("PORT", "5000"))

# أمان الرصيد
EST_FEE_RATE        = float(os.getenv("FEE_RATE_EST", "0.0025"))   # ≈0.25%
HEADROOM_EUR_MIN    = float(os.getenv("HEADROOM_EUR_MIN", "0.50")) # ≥ 0.50€
MAX_SPEND_FRACTION  = float(os.getenv("MAX_SPEND_FRACTION", "0.90"))
FIXED_EUR_PER_TRADE = float(os.getenv("FIXED_EUR", "0"))           # 0=off

# Backoff لـ 216
IB_BACKOFF_FACTOR   = float(os.getenv("IB_BACKOFF_FACTOR", "0.96"))
IB_BACKOFF_TRIES    = int(os.getenv("IB_BACKOFF_TRIES", "6"))

# Endpoints
BASE_URL = "https://api.bitvavo.com/v2"
WS_URL   = "wss://ws.bitvavo.com/v2/"

# ========= Settings =========
MAX_TRADES            = 1
MAKER_REPRICE_EVERY   = 2.0           # متابعة نفس الأمر قبل إعادة التسعير
MAKER_REPRICE_THRESH  = 0.0005        # 0.05%
MAKER_WAIT_BASE_SEC   = 45
MAKER_WAIT_MAX_SEC    = 300
MAKER_WAIT_STEP_UP    = 15
MAKER_WAIT_STEP_DOWN  = 10
BUY_MIN_EUR           = 5.0
WS_STALENESS_SEC      = 2.0
POLL_INTERVAL         = 0.35
SLEEP_BETWEEN_ATTEMPTS= 1.0           # تبطيء محاولات وضع الأمر
TELE_ALLOWED_USERS    = set(x.strip() for x in os.getenv("TELE_USER_WHITELIST","").split(",") if x.strip())

STOP_LADDER = [(0.0,-2.0),(1.0,-1.0),(2.0,0.0),(3.0,1.0),(4.0,2.0),(5.0,3.0)]

# ========= Runtime =========
r  = redis.from_url(REDIS_URL) if REDIS_URL else redis.Redis()
lk = Lock()
enabled        = True
active_trade   = None
executed_trades= []
MARKET_MAP     = {}   # "ADA" -> "ADA-EUR"
MARKET_META    = {}   # "ADA-EUR" -> {"minQuote","minBase","tick","step"}
_ws_prices     = {}
_ws_lock       = Lock()

# ========= Utils =========
def send_message(text: str):
    if not text: return
    try:
        if BOT_TOKEN and CHAT_ID:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": text}, timeout=8)
        else:
            print("TG:", text)
    except Exception as e:
        print("TG err:", e)

def _sign(ts, method, path, body_str=""):
    msg = f"{ts}{method}{path}{body_str}"
    return hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def _request(method, path, body=None, timeout=15, retry_invalid_sig=True):
    """
    Bitvavo requires signing over '/v2' + path.
    Enlarged window to reduce 209 issues.
    """
    url = f"{BASE_URL}{path}"
    body = body or {}
    body_str = "" if method=="GET" else json.dumps(body, separators=(',',':'))
    ts  = str(int(time.time()*1000))
    headers = {
        "Bitvavo-Access-Key": API_KEY,
        "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Signature": _sign(ts, method, f"/v2{path}", body_str),
        "Bitvavo-Access-Window": "60000"
    }
    try:
        resp = requests.request(method, url, headers=headers,
                                json=(None if method=="GET" else body),
                                timeout=timeout)
        j = resp.json()
        if isinstance(j, dict) and j.get("error"):
            # إعادة محاولة واحدة عند invalid signature
            if retry_invalid_sig and "invalid" in str(j["error"]).lower():
                time.sleep(0.3)
                return _request(method, path, body, timeout, retry_invalid_sig=False)
            print("Bitvavo error:", j)
        return j
    except Exception as e:
        print("request err:", e)
        return {"error":"request_failed"}

def get_eur_available() -> float:
    try:
        bals = _request("GET", "/balance")
        if isinstance(bals, list):
            for b in bals:
                if b.get("symbol") == "EUR":
                    return max(0.0, float(b.get("available",0) or 0))
    except Exception: pass
    return 0.0

# ========= Markets / Meta =========
def load_markets():
    global MARKET_MAP, MARKET_META
    try:
        rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
        m, meta = {}, {}
        for r0 in rows:
            base = r0.get("base"); quote= r0.get("quote"); market = r0.get("market")
            if base and quote == "EUR" and market:
                price_prec = float(r0.get("pricePrecision", 1e-6) or 1e-6)
                amt_prec   = float(r0.get("amountPrecision", 1e-8) or 1e-8)
                m[base.upper()] = market
                meta[market] = {
                    "minQuote": float(r0.get("minOrderInQuoteAsset", 5) or 5.0),
                    "minBase":  float(r0.get("minOrderInBaseAsset",  0) or 0.0),
                    "tick":     price_prec,
                    "step":     amt_prec,
                }
        if m: MARKET_MAP = m
        if meta: MARKET_META = meta
    except Exception as e:
        print("load_markets err:", e)

def coin_to_market(coin: str):
    if not MARKET_MAP:
        load_markets()
    return MARKET_MAP.get((coin or "").upper())

def _decs(step: float) -> int:
    try:
        if step >= 1: return 0
        return max(0, int(round(-math.log10(step))))
    except Exception:
        return 8

def _round_price(market, price):
    tick = (MARKET_META.get(market, {}) or {}).get("tick", 1e-6)
    decs = _decs(tick)
    p = round(float(price), decs)
    return max(tick, p)

def _round_amount(market, amount):
    step = (MARKET_META.get(market, {}) or {}).get("step", 1e-8)
    amt = math.floor(float(amount) / step) * step
    decs = _decs(step)
    return round(max(step, amt), decs)

def _format_price(market, price) -> str:
    tick = (MARKET_META.get(market, {}) or {}).get("tick", 1e-6)
    decs = _decs(tick)
    return f"{_round_price(market, price):.{decs}f}"

def _format_amount(market, amount) -> str:
    step = (MARKET_META.get(market, {}) or {}).get("step", 1e-8)
    decs = _decs(step)
    return f"{_round_amount(market, amount):.{decs}f}"

def _min_quote(market): return (MARKET_META.get(market, {}) or {}).get("minQuote", BUY_MIN_EUR)
def _min_base(market):  return (MARKET_META.get(market, {}) or {}).get("minBase", 0.0)

# ========= WS Prices =========
def _ws_on_message(ws, msg):
    try:
        data = json.loads(msg)
        if isinstance(data, dict) and data.get("event") == "ticker":
            m = data.get("market"); price = data.get("price") or data.get("lastPrice")
            p = float(price or 0)
            if p>0:
                with _ws_lock: _ws_prices[m] = {"price": p, "ts": time.time()}
    except Exception: pass

def _ws_thread():
    while True:
        try:
            ws = websocket.WebSocketApp(WS_URL, on_message=_ws_on_message)
            ws.run_forever(ping_interval=25, ping_timeout=10)
        except Exception as e:
            print("WS loop ex:", e)
        time.sleep(2)
Thread(target=_ws_thread, daemon=True).start()

def fetch_orderbook(market):
    if not market: return None
    try:
        j = requests.get(f"{BASE_URL}/{market}/book", timeout=6).json()
        if j and j.get("bids") and j.get("asks"): return j
    except Exception: pass
    return None

# ========= Bitvavo orders =========
def _place_limit_postonly(market, side, price, amount):
    if not market: return {"error":"market_missing"}
    body={"market":market,"side":side,"orderType":"limit","postOnly":True,
          "clientOrderId":str(uuid4()),
          "price": _format_price(market, price),
          "amount": _format_amount(market, float(amount)),
          "operatorId": ""}   # إلزامي
    return _request("POST","/order",body)

def _fetch_order(orderId):   return _request("GET",    f"/order?orderId={orderId}")
def _cancel_order(orderId):  return _request("DELETE", f"/order?orderId={orderId}")

def totals_from_fills(fills):
    tb=tq=fee=0.0
    for f in (fills or []):
        amt=float(f["amount"]); price=float(f["price"]); fe=float(f.get("fee",0) or 0)
        tb+=amt; tq+=amt*price; fee+=fe
    return tb, tq, fee

# ========= Patience (Redis) =========
def _pat_key(market): return f"maker:patience:{market}"
def get_patience_sec(market):
    try:
        v = r.get(_pat_key(market))
        if v is not None:
            return min(MAKER_WAIT_MAX_SEC, max(MAKER_WAIT_BASE_SEC, int(v)))
    except Exception: pass
    return MAKER_WAIT_BASE_SEC
def bump_patience_on_fail(market):
    try:
        r.set(_pat_key(market), min(MAKER_WAIT_MAX_SEC, get_patience_sec(market)+MAKER_WAIT_STEP_UP))
    except Exception: pass
def relax_patience_on_success(market):
    try:
        r.set(_pat_key(market), max(MAKER_WAIT_BASE_SEC, get_patience_sec(market)-MAKER_WAIT_STEP_DOWN))
    except Exception: pass

# ========= Maker Buy =========
def _calc_buy_amount_base(market: str, target_eur: float, price: float) -> float:
    base_amt = float(target_eur) / max(1e-12, float(price))
    base_amt = max(base_amt, _min_base(market))
    return _round_amount(market, base_amt)

def open_maker_buy(market: str, eur_amount: float):
    """محاولة شراء Maker مع تبطيء + backoff + تنظيف."""
    if not market or market not in MARKET_META:
        send_message("⛔ الماركت غير معروف. حدّث/أعد التحميل."); return None

    eur_avail = get_eur_available()

    # اختيار المبلغ
    if FIXED_EUR_PER_TRADE>0: target_eur = FIXED_EUR_PER_TRADE
    elif not eur_amount or eur_amount<=0: target_eur = eur_avail
    else: target_eur = float(eur_amount)

    target_eur = min(target_eur, eur_avail*MAX_SPEND_FRACTION)
    minq = _min_quote(market)

    buf = max(HEADROOM_EUR_MIN, target_eur*EST_FEE_RATE*2.0, 0.05)
    spendable = min(target_eur, max(0.0, eur_avail - buf))

    if spendable < max(minq, BUY_MIN_EUR):
        need = max(minq, BUY_MIN_EUR)
        send_message(f"🚫 الرصيد غير كافٍ: متاح €{eur_avail:.2f} | بعد الهامش €{spendable:.2f} | الهامش €{buf:.2f} | المطلوب ≥ €{need:.2f}.")
        return None

    send_message(f"💰 متاح: €{eur_avail:.2f} | سننفق: €{spendable:.2f} (هامش €{buf:.2f})")

    patience = get_patience_sec(market)
    started  = time.time()
    last_order=None; last_bid=None
    all_fills=[]; remaining_eur=float(spendable)

    placed_count=0
    internal_attempts=0

    try:
        while (time.time()-started)<patience and remaining_eur >= (minq*0.999):
            ob = fetch_orderbook(market)
            if not ob: time.sleep(0.25); continue

            best_bid = float(ob["bids"][0][0])
            best_ask = float(ob["asks"][0][0])
            price    = _round_price(market, min(best_bid, best_ask*(1.0-1e-6)))

            # متابعة أمر قائم
            if last_order:
                st = _fetch_order(last_order); st_status = st.get("status")
                if st_status in ("filled","partiallyFilled"):
                    fills = st.get("fills", []) or []
                    if fills:
                        all_fills += fills
                        base, q_eur, fee = totals_from_fills(fills)
                        remaining_eur = max(0.0, remaining_eur - (q_eur + fee))
                if st_status=="filled" or remaining_eur < (minq*0.999):
                    try: _cancel_order(last_order)
                    except: pass
                    last_order=None
                    break

                # إعادة تسعير إذا تحرك bid
                if (last_bid is None) or (abs(best_bid/last_bid - 1.0) >= MAKER_REPRICE_THRESH):
                    try: _cancel_order(last_order)
                    except: pass
                    last_order=None
                else:
                    t0=time.time()
                    while time.time()-t0 < MAKER_REPRICE_EVERY:
                        st = _fetch_order(last_order); st_status = st.get("status")
                        if st_status in ("filled","partiallyFilled"):
                            fills = st.get("fills", []) or []
                            if fills:
                                all_fills += fills
                                base, q_eur, fee = totals_from_fills(fills)
                                remaining_eur = max(0.0, remaining_eur - (q_eur + fee))
                            if st_status=="filled" or remaining_eur < (minq*0.999):
                                try: _cancel_order(last_order)
                                except: pass
                                last_order=None
                                break
                        time.sleep(POLL_INTERVAL)
                    if last_order: 
                        continue  # ما زلنا ننتظر

            # وضع أمر جديد (محاولة حقيقية فقط)
            if not last_order and remaining_eur >= (minq*0.999):
                attempt = 0
                while attempt < IB_BACKOFF_TRIES and remaining_eur >= (minq*0.999):
                    amt = _calc_buy_amount_base(market, remaining_eur, price)
                    if amt <= 0: break

                    exp_eur = amt*price
                    send_message(f"🧪 محاولة شراء #{placed_count+1}: amount={_format_amount(market, amt)} | سعر≈{_format_price(market, price)} | EUR≈{exp_eur:.2f}")
                    internal_attempts += 1

                    res = _place_limit_postonly(market, "buy", price, amt)
                    err = str((res or {}).get("error","")).lower()
                    oid = (res or {}).get("orderId")

                    if oid:
                        placed_count += 1
                        last_order = oid
                        last_bid   = best_bid
                        break

                    # 216: نقص رصيد -> قلّل المبلغ وحاول بعد هدنة قصيرة
                    if "insufficient balance" in err or "not have sufficient balance" in err:
                        remaining_eur *= IB_BACKOFF_FACTOR
                        attempt += 1
                        time.sleep(SLEEP_BETWEEN_ATTEMPTS)
                        continue

                    # 203: market param — حماية
                    if "market parameter is required" in err or "market" in err and "required" in err:
                        send_message("⛔ فشل: الماركت مفقود/غير صالح. أعد الأمر مثل: /buy ADA 8.5")
                        return None

                    # أخطاء أخرى: جرّب إعادة التسعير لمرة واحدة
                    attempt = IB_BACKOFF_TRIES
                    break

                if not last_order:
                    time.sleep(SLEEP_BETWEEN_ATTEMPTS)
                    continue

                # متابعة قصيرة للأمر الجديد
                t0=time.time()
                while time.time()-t0 < MAKER_REPRICE_EVERY:
                    st = _fetch_order(last_order); st_status = st.get("status")
                    if st_status in ("filled","partiallyFilled"):
                        fills = st.get("fills", []) or []
                        if fills:
                            all_fills += fills
                            base, q_eur, fee = totals_from_fills(fills)
                            remaining_eur = max(0.0, remaining_eur - (q_eur + fee))
                        if st_status=="filled" or remaining_eur < (minq*0.999):
                            try: _cancel_order(last_order)
                            except: pass
                            last_order=None
                            break
                    time.sleep(POLL_INTERVAL)

        # تنظيف
        if last_order:
            try: _cancel_order(last_order)
            except: pass

    except Exception as e:
        traceback.print_exc()
        send_message(f"🐞 خطأ أثناء الشراء: {e}")

    if not all_fills:
        bump_patience_on_fail(market)
        elapsed = time.time()-started
        send_message(f"⚠️ لم يكتمل شراء Maker.\n• أوامر موضوعة: {placed_count}\n• محاولات داخلية: {internal_attempts}\n• زمن: {elapsed:.1f}s\nسنُعزّز الصبر تلقائيًا وسنحاول لاحقًا.")
        return None

    base_amt, quote_eur, fee_eur = totals_from_fills(all_fills)
    if base_amt <= 0:
        bump_patience_on_fail(market); return None

    relax_patience_on_success(market)
    avg = (quote_eur + fee_eur) / base_amt
    return {"amount": base_amt, "avg": avg, "cost_eur": quote_eur + fee_eur, "fee_eur": fee_eur}

# ========= Maker Sell =========
def close_maker_sell(market: str, amount: float):
    patience = get_patience_sec(market)
    started  = time.time()
    remaining = float(amount)
    all_fills = []
    last_order=None; last_ask=None

    try:
        while (time.time()-started)<patience and remaining>0:
            ob = fetch_orderbook(market)
            if not ob: time.sleep(0.25); continue

            best_bid = float(ob["bids"][0][0])
            best_ask = float(ob["asks"][0][0])
            price    = _round_price(market, max(best_ask, best_bid*(1.0+1e-6)))

            # متابعة
            if last_order:
                st=_fetch_order(last_order); st_status=st.get("status")
                if st_status in ("filled","partiallyFilled"):
                    fills = st.get("fills", []) or []
                    if fills:
                        sold, _, _ = totals_from_fills(fills)
                        remaining = max(0.0, remaining - sold)
                        all_fills += fills
                if st_status=="filled" or remaining<=0:
                    try: _cancel_order(last_order)
                    except: pass
                    last_order=None
                    break

                if (last_ask is None) or (abs(best_ask/last_ask - 1.0) >= MAKER_REPRICE_THRESH):
                    try: _cancel_order(last_order)
                    except: pass
                    last_order=None
                else:
                    t0=time.time()
                    while time.time()-t0 < MAKER_REPRICE_EVERY:
                        st=_fetch_order(last_order); st_status=st.get("status")
                        if st_status in ("filled","partiallyFilled"):
                            fills = st.get("fills", []) or []
                            if fills:
                                sold, _, _ = totals_from_fills(fills)
                                remaining = max(0.0, remaining - sold)
                                all_fills += fills
                            if st_status=="filled" or remaining<=0:
                                try: _cancel_order(last_order)
                                except: pass
                                last_order=None
                                break
                        time.sleep(POLL_INTERVAL)
                    if last_order: continue

            if remaining>0:
                amt = _round_amount(market, remaining)
                res = _place_limit_postonly(market,"sell",price,amt)
                oid = (res or {}).get("orderId")
                if not oid:
                    price2=_round_price(market,best_ask)
                    res = _place_limit_postonly(market,"sell",price2,amt)
                    oid = (res or {}).get("orderId")
                if not oid:
                    time.sleep(SLEEP_BETWEEN_ATTEMPTS); continue
                last_order=oid; last_ask=best_ask

                t0=time.time()
                while time.time()-t0 < MAKER_REPRICE_EVERY:
                    st=_fetch_order(last_order); st_status=st.get("status")
                    if st_status in ("filled","partiallyFilled"):
                        fills = st.get("fills", []) or []
                        if fills:
                            sold, _, _ = totals_from_fills(fills)
                            remaining = max(0.0, remaining - sold)
                            all_fills += fills
                        if st_status=="filled" or remaining<=0:
                            try: _cancel_order(last_order)
                            except: pass
                            last_order=None
                            break
                    time.sleep(POLL_INTERVAL)

        if last_order:
            try: _cancel_order(last_order)
            except: pass

    except Exception as e:
        traceback.print_exc()
        send_message(f"🐞 خطأ أثناء البيع: {e}")

    proceeds=0.0; fee=0.0; sold=0.0
    for f in all_fills:
        amt=float(f["amount"]); price=float(f["price"]); fe=float(f.get("fee",0) or 0)
        sold += amt; proceeds += amt*price; fee += fe
    proceeds -= fee
    return sold, proceeds, fee

# ========= Monitor =========
def current_stop_from_peak(peak_pct: float) -> float:
    stop=-999.0
    for th,v in STOP_LADDER:
        if peak_pct>=th: stop=v
    return stop

def monitor_loop():
    global active_trade
    while True:
        try:
            with lk: at = active_trade.copy() if active_trade else None
            if not at: time.sleep(0.25); continue
            m=at["symbol"]; ent=at["entry"]

            ob = fetch_orderbook(m)
            price = float(ob["bids"][0][0]) if ob else ent
            pnl = ((price/ent)-1.0)*100.0
            updated_peak = max(at["peak_pct"], pnl)
            new_stop = current_stop_from_peak(updated_peak)

            changed=False
            with lk:
                if active_trade:
                    if updated_peak>active_trade["peak_pct"]+1e-9:
                        active_trade["peak_pct"]=updated_peak; changed=True
                    if abs(new_stop-active_trade.get("dyn_stop_pct",-999.0))>1e-9:
                        active_trade["dyn_stop_pct"]=new_stop; changed=True
            if changed:
                send_message(f"📈 Peak={updated_peak:.2f}% → SL {new_stop:+.2f}%")

            with lk: at2 = active_trade.copy() if active_trade else None
            if at2 and pnl <= at2.get("dyn_stop_pct",-2.0):
                do_close_maker("Dynamic stop")
                time.sleep(0.5); continue
            time.sleep(0.15)
        except Exception as e:
            print("monitor err:", e); time.sleep(0.5)

Thread(target=monitor_loop, daemon=True).start()

# ========= Trade Flow =========
def do_open_maker(market: str, eur: float):
    def _runner():
        global active_trade
        try:
            with lk:
                if active_trade: 
                    send_message("⛔ توجد صفقة نشطة."); return
            res = open_maker_buy(market, eur)
            if not res:
                send_message("⏳ لم يكتمل شراء Maker. سنحاول لاحقًا (الصبر يتكيف تلقائياً).")
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
            send_message(f"✅ شراء {market.replace('-EUR','')} (Maker) @ €{active_trade['entry']:.8f} | كمية {active_trade['amount']:.8f}")
        except Exception as e:
            traceback.print_exc()
            send_message(f"🐞 خطأ أثناء الفتح: {e}")
    Thread(target=_runner, daemon=True).start()

def do_close_maker(reason=""):
    global active_trade
    try:
        with lk:
            if not active_trade: return
            m=active_trade["symbol"]; amt=float(active_trade["amount"]); cost=float(active_trade["cost_eur"])
        sold, proceeds, sell_fee = close_maker_sell(m, amt)
        with lk:
            pnl_eur = proceeds - cost
            pnl_pct = (proceeds/cost - 1.0) * 100.0 if cost>0 else 0.0
            for t in reversed(executed_trades):
                if t["symbol"]==m and "exit_eur" not in t:
                    t.update({"exit_eur": proceeds, "sell_fee_eur": sell_fee,
                              "pnl_eur": pnl_eur, "pnl_pct": pnl_pct,
                              "exit_time": time.time()})
                    break
            active_trade=None
        send_message(f"💰 بيع {m.replace('-EUR','')} (Maker) | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%) {('— '+reason) if reason else ''}")
    except Exception as e:
        traceback.print_exc()
        send_message(f"🐞 خطأ أثناء الإغلاق: {e}")

# ========= Summary =========
def build_summary():
    lines=[]
    with lk:
        at=active_trade
        closed=[x for x in executed_trades if "exit_eur" in x]
    if at:
        ob = fetch_orderbook(at["symbol"])
        cur = float(ob["bids"][0][0]) if ob else at["entry"]
        pnl = ((cur/at["entry"])-1.0)*100.0
        lines.append("📌 صفقة نشطة:")
        lines.append(f"• {at['symbol'].replace('-EUR','')} @ €{at['entry']:.8f} | PnL {pnl:+.2f}% | Peak {at['peak_pct']:.2f}% | SL {at.get('dyn_stop_pct',-2.0):+.2f}%")
    else:
        lines.append("📌 لا صفقات نشطة.")
    pnl_eur=sum(float(x["pnl_eur"]) for x in closed)
    wins=sum(1 for x in closed if float(x.get("pnl_eur",0))>=0)
    lines.append(f"\n📊 صفقات مكتملة: {len(closed)} | محققة: {pnl_eur:+.2f}€ | فوز/خسارة: {wins}/{len(closed)-wins}")
    lines.append(f"\n⚙️ buy=Maker | sell=Maker | سلم الوقف: -2%→-1%→0%→+1%…")
    return "\n".join(lines)

# ========= Telegram Webhook =========
def _tele_reply(chat_id, text):
    if not BOT_TOKEN: return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": chat_id, "text": text}, timeout=8)
    except Exception: pass

def _auth_ok(msg):
    if not TELE_ALLOWED_USERS: return True
    uid = str(((msg.get("from") or {}).get("id")) or "")
    return (uid in TELE_ALLOWED_USERS)

@app.route("/telehook", methods=["POST"])
def telehook():
    try:
        update = request.get_json(force=True)
        msg = update.get("message") or update.get("edited_message") or {}
        chat_id = (msg.get("chat") or {}).get("id") or CHAT_ID
        text = (msg.get("text") or "").strip()

        if not _auth_ok(msg):
            _tele_reply(chat_id, "⛔ غير مصرح.")
            return jsonify({"ok":True})

        m = re.match(r"^/buy\s+([A-Za-z0-9]+)(?:\s+([\d\.]+))?$", text)
        if m:
            coin = m.group(1).upper()
            eur  = float(m.group(2)) if m.group(2) else None
            market = coin_to_market(coin)
            if not market:
                _tele_reply(chat_id, f"⛔ {coin}-EUR غير متاح على Bitvavo.")
                return jsonify({"ok":True})
            _tele_reply(chat_id, f"⏳ بدء شراء {coin}...")
            do_open_maker(market, eur)
            return jsonify({"ok":True})

        if text.startswith("/close"):
            do_close_maker("Manual"); _tele_reply(chat_id,"⏳ إغلاق..."); return jsonify({"ok":True})

        if text.startswith("/summary"):
            _tele_reply(chat_id, build_summary()); return jsonify({"ok":True})

        if text.startswith("/enable"):
            global enabled; enabled=True; _tele_reply(chat_id,"✅ تم التفعيل."); return jsonify({"ok":True})
        if text.startswith("/disable"):
            enabled=False; _tele_reply(chat_id,"🛑 تم الإيقاف."); return jsonify({"ok":True})

        _tele_reply(chat_id, "الأوامر: /buy COIN [eur] /close /summary /enable /disable")
        return jsonify({"ok":True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok":False,"err":str(e)}), 500

# ========= Health =========
@app.route("/", methods=["GET"])
def home(): return "Saqer Maker Executor ✅"

# (اختياري) Webhook قديم للاستدعاء المباشر
@app.route("/hook", methods=["POST"])
def hook():
    try:
        data = request.get_json(silent=True) or {}
        cmd  = (data.get("cmd") or "").strip().lower()
        if cmd=="buy":
            coin=(data.get("coin") or "").strip().upper()
            market=coin_to_market(coin)
            if not market:
                return jsonify({"ok":False,"err":"market_unavailable"})
            eur = float(data.get("eur")) if data.get("eur") is not None else None
            do_open_maker(market, eur)
            return jsonify({"ok":True})
        if cmd=="close": do_close_maker("Manual"); return jsonify({"ok":True})
        if cmd=="summary": return jsonify({"ok":True,"summary":build_summary()})
        return jsonify({"ok":False,"err":"bad_cmd"})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok":False,"err":str(e)}), 500

# ========= Main =========
if __name__ == "__main__":
    load_markets()
    app.run(host="0.0.0.0", port=PORT)