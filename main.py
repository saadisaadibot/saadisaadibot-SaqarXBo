# -*- coding: utf-8 -*-
"""
Saqer — Maker-Only Relay Executor (Bitvavo / EUR)
- Maker buy/sell فقط (postOnly) + تجميع partial fills.
- Trail SL سلّمي حسب القمة.
- Precision مضبوط من الميتا (بدون amountQuote للـ limit).
- Patience ديناميكي + stale-cancel + backoff لرصيد غير كافٍ.
- تنظيف شامل: لا يترك أمر Limit "New" بلا مراقبة.
- أوامر:
  • /hook (POST JSON) من بوت الإشارة: {"cmd":"buy","coin":"ADA","eur":8.5}
  • /hook: {"cmd":"close"} | {"cmd":"summary"} | {"cmd":"enable/disable"}
  • /tg (اختياري) Webhook تلغرام للأوامر النصية: /buy ADA 10 | /close | /summary
"""

import os, re, time, json, math, traceback
import requests, redis, websocket
from threading import Thread, Lock
from uuid import uuid4
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ========= Boot / ENV =========
load_dotenv()
app = Flask(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHAT_ID     = os.getenv("CHAT_ID")
API_KEY     = os.getenv("BITVAVO_API_KEY")
API_SECRET  = os.getenv("BITVAVO_API_SECRET")
REDIS_URL   = os.getenv("REDIS_URL")
RUN_LOCAL   = os.getenv("RUN_LOCAL", "0") == "1"
PORT        = int(os.getenv("PORT", "5000"))

BASE_URL = "https://api.bitvavo.com/v2"
WS_URL   = "wss://ws.bitvavo.com/v2/"

# ====== حماية الرصيد / الصبر / الستال ======
EST_FEE_RATE        = float(os.getenv("FEE_RATE_EST", "0.0025"))   # ≈0.25%
HEADROOM_EUR_MIN    = float(os.getenv("HEADROOM_EUR_MIN", "0.50"))
MAX_SPEND_FRACTION  = float(os.getenv("MAX_SPEND_FRACTION", "0.90"))
FIXED_EUR_PER_TRADE = float(os.getenv("FIXED_EUR", "0"))           # 0=معطّل

IB_BACKOFF_FACTOR   = float(os.getenv("IB_BACKOFF_FACTOR", "0.96"))
IB_BACKOFF_TRIES    = int(os.getenv("IB_BACKOFF_TRIES", "5"))

MAKER_REPRICE_EVERY = float(os.getenv("MAKER_REPRICE_EVERY", "2.0"))
MAKER_REPRICE_THRESH= float(os.getenv("MAKER_REPRICE_THRESH","0.0005"))  # 0.05%

MAKER_WAIT_BASE_SEC = int(os.getenv("MAKER_WAIT_BASE_SEC","300"))  # 5 دقائق
MAKER_WAIT_MAX_SEC  = int(os.getenv("MAKER_WAIT_MAX_SEC","600"))   # سقف 10 دق
MAKER_WAIT_STEP_UP  = int(os.getenv("MAKER_WAIT_STEP_UP","30"))
MAKER_WAIT_STEP_DOWN= int(os.getenv("MAKER_WAIT_STEP_DOWN","15"))
STALE_ORDER_SEC     = float(os.getenv("STALE_ORDER_SEC","8.0"))

BUY_MIN_EUR         = float(os.getenv("BUY_MIN_EUR","5.0"))
WS_STALENESS_SEC    = float(os.getenv("WS_STALENESS_SEC","2.0"))

# ====== Trail SL سلّمي ======
STOP_LADDER = [
    (0.0,  -2.0),
    (1.0,  -1.0),
    (2.0,   0.0),
    (3.0,  +1.0),
    (4.0,  +2.0),
    (5.0,  +3.0),
    (7.0,  +4.0),
    (10.0, +5.0),
]

# ========= Runtime =========
r  = redis.from_url(REDIS_URL) if REDIS_URL else redis.Redis()
lk = Lock()
enabled        = True
active_trade   = None
executed_trades= []
MARKET_MAP     = {}   # "ADA" -> "ADA-EUR"
MARKET_META    = {}   # "ADA-EUR" -> meta
_ws_prices     = {}
_ws_lock       = Lock()

# ========= Utils =========
def send_message(text: str):
    try:
        if BOT_TOKEN and CHAT_ID:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": text}, timeout=8)
        else:
            print("TG:", text)
    except Exception as e:
        print("TG err:", e)

def create_sig(ts, method, path, body_str=""):
    import hmac, hashlib
    msg = f"{ts}{method}{path}{body_str}"
    return hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def bv_request(method, path, body=None, timeout=12):
    url = f"{BASE_URL}{path}"
    ts  = str(int(time.time()*1000))
    body_str = "" if method=="GET" else json.dumps(body or {}, separators=(',',':'))
    sig = create_sig(ts, method, f"/v2{path}", body_str)
    headers = {
        'Bitvavo-Access-Key': API_KEY,
        'Bitvavo-Access-Timestamp': ts,
        'Bitvavo-Access-Signature': sig,
        'Bitvavo-Access-Window': '10000'
    }
    try:
        resp = requests.request(method, url, headers=headers,
                                json=(body or {}) if method!="GET" else None,
                                timeout=timeout)
        try:
            j = resp.json()
        except Exception:
            j = {"error":"non_json", "status": resp.status_code, "text": resp.text[:240]}
        if isinstance(j, dict) and j.get("error"):
            print("Bitvavo error:", j)
        return j
    except Exception as e:
        print("bv_request err:", e)
        return {"error":"request_failed"}

def get_eur_available() -> float:
    try:
        bals = bv_request("GET", "/balance")
        if isinstance(bals, list):
            for b in bals:
                if b.get("symbol") == "EUR":
                    return max(0.0, float(b.get("available",0) or 0))
    except Exception:
        pass
    return 0.0

# ========= Markets / Meta =========
def load_markets():
    global MARKET_MAP, MARKET_META
    try:
        rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
        m, meta = {}, {}
        for r0 in rows:
            base = r0.get("base"); quote= r0.get("quote"); market = r0.get("market")
            if base and quote == "EUR":
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
    return MARKET_MAP.get(coin.upper())

def _decimals_from_step(step: float) -> int:
    try:
        if step >= 1: return 0
        return max(0, int(round(-math.log10(step))))
    except Exception:
        return 8

def _round_price(market, price):
    tick = (MARKET_META.get(market, {}) or {}).get("tick", 1e-6)
    decs = _decimals_from_step(tick)
    p = round(price, decs)
    p = max(tick, p)
    return p

def _round_amount(market, amount):
    step = (MARKET_META.get(market, {}) or {}).get("step", 1e-8)
    floored = math.floor(float(amount) / step) * step
    decs = _decimals_from_step(step)
    return round(max(step, floored), decs)

def _format_price(market, price) -> str:
    tick = (MARKET_META.get(market, {}) or {}).get("tick", 1e-6)
    decs = _decimals_from_step(tick)
    return f"{_round_price(market, price):.{decs}f}"

def _format_amount(market, amount) -> str:
    step = (MARKET_META.get(market, {}) or {}).get("step", 1e-8)
    decs = _decimals_from_step(step)
    return f"{_round_amount(market, amount):.{decs}f}"

def _min_quote(market): return (MARKET_META.get(market, {}) or {}).get("minQuote", BUY_MIN_EUR)
def _min_base(market):  return (MARKET_META.get(market, {}) or {}).get("minBase", 0.0)

# ========= Open Orders Helpers =========
def list_open_orders(market: str):
    try:
        j = bv_request("GET", f"/orders?market={market}")
        if isinstance(j, list):
            return j
    except Exception:
        pass
    return []

def cancel_all_open_orders(market: str):
    try:
        bv_request("DELETE", f"/orders?market={market}")
    except Exception:
        pass

def _order_last_update_ts(order_json):
    try:
        if "updated" in order_json and order_json["updated"]:
            v = float(order_json["updated"])
            return v / (1000.0 if v > 1e12 else 1.0)
        if "created" in order_json and order_json["created"]:
            v = float(order_json["created"])
            return v / (1000.0 if v > 1e12 else 1.0)
        fills = order_json.get("fills") or []
        if fills:
            ts = max(float(f.get("timestamp", 0)) for f in fills)
            if ts > 0:
                return ts / (1000.0 if ts > 1e12 else 1.0)
    except Exception:
        pass
    return None

# ========= WS Prices =========
def _ws_on_open(ws): pass
def _ws_on_message(ws, msg):
    try: data = json.loads(msg)
    except Exception: return
    if isinstance(data, dict) and data.get("event") == "ticker":
        m = data.get("market")
        price = data.get("price") or data.get("lastPrice") or data.get("open")
        try:
            p = float(price)
            if p > 0:
                with _ws_lock: _ws_prices[m] = {"price": p, "ts": time.time()}
        except Exception: pass
def _ws_on_error(ws, err): print("WS error:", err)
def _ws_on_close(ws, c, r): pass
def _ws_thread():
    while True:
        try:
            ws = websocket.WebSocketApp(
                WS_URL, on_open=_ws_on_open, on_message=_ws_on_message,
                on_error=_ws_on_error, on_close=_ws_on_close
            )
            ws.run_forever(ping_interval=25, ping_timeout=10)
        except Exception as e:
            print("WS loop ex:", e)
        time.sleep(2)
Thread(target=_ws_thread, daemon=True).start()

def ws_sub(markets):
    if not markets: return
    try:
        payload = {"action":"subscribe","channels":[{"name":"ticker","markets":markets}]}
        ws = websocket.create_connection(WS_URL, timeout=5)
        ws.send(json.dumps(payload)); ws.close()
    except Exception: pass

def fetch_price_ws_first(market: str, staleness=WS_STALENESS_SEC):
    now = time.time()
    with _ws_lock:
        rec = _ws_prices.get(market)
    if rec and (now - rec["ts"]) <= staleness:
        return rec["price"]
    ws_sub([market])
    try:
        j = requests.get(f"{BASE_URL}/ticker/price?market={market}", timeout=6).json()
        p = float(j.get("price", 0) or 0)
        if p > 0:
            with _ws_lock: _ws_prices[market] = {"price": p, "ts": now}
            return p
    except Exception: pass
    return None

# ========= Order Helpers =========
def _place_limit_postonly(market, side, price, amount=None):
    if amount is None or float(amount) <= 0:
        raise ValueError("amount is required for limit postOnly")
    body={"market":market,"side":side,"orderType":"limit","postOnly":True,
          "clientOrderId":str(uuid4()),
          "price": _format_price(market, price),
          "amount": _format_amount(market, float(amount)),
          "operatorId": ""}   # مطلوب ولو فاضي
    return bv_request("POST","/order", body)

def _fetch_order(orderId):   return bv_request("GET",    f"/order?orderId={orderId}")
def _cancel_order(orderId):  return bv_request("DELETE", f"/order?orderId={orderId}")

def totals_from_fills(fills):
    tb=tq=fee=0.0
    for f in (fills or []):
        amt=float(f["amount"]); price=float(f["price"]); fe=float(f.get("fee",0) or 0)
        tb+=amt; tq+=amt*price; fee+=fe
    return tb, tq, fee

# ========= Patience Learning (Redis) =========
def _patience_key(market): return f"maker:patience:{market}"
def get_patience_sec(market):
    try:
        v = r.get(_patience_key(market))
        if v is not None:
            return min(MAKER_WAIT_MAX_SEC, max(MAKER_WAIT_BASE_SEC, int(v)))
    except Exception: pass
    return MAKER_WAIT_BASE_SEC
def bump_patience_on_fail(market):
    try:
        cur = get_patience_sec(market)
        r.set(_patience_key(market), min(MAKER_WAIT_MAX_SEC, cur + MAKER_WAIT_STEP_UP))
    except Exception: pass
def relax_patience_on_success(market):
    try:
        cur = get_patience_sec(market)
        base = MAKER_WAIT_BASE_SEC
        r.set(_patience_key(market), max(base, cur - MAKER_WAIT_STEP_DOWN))
    except Exception: pass

# ========= Maker Buy =========
def _calc_buy_amount_base(market: str, target_eur: float, use_price: float) -> float:
    price = max(1e-12, float(use_price))
    base_amt = float(target_eur) / price
    base_amt = max(base_amt, _min_base(market))
    return _round_amount(market, base_amt)

def open_maker_buy(market: str, eur_amount: float):
    """Maker buy مع تنظيف شامل، stale-cancel، backoff، ودقّة عالية."""
    # تنظيف قبل البدء
    cancel_all_open_orders(market)

    eur_available = get_eur_available()
    if FIXED_EUR_PER_TRADE and FIXED_EUR_PER_TRADE > 0:
        target_eur = float(FIXED_EUR_PER_TRADE)
    elif eur_amount is None or eur_amount <= 0:
        target_eur = float(eur_available)
    else:
        target_eur = float(eur_amount)
    target_eur = min(target_eur, eur_available * MAX_SPEND_FRACTION)

    minq       = _min_quote(market)
    buffer_eur = max(HEADROOM_EUR_MIN, target_eur * EST_FEE_RATE * 2.0, 0.05)
    spendable  = min(target_eur, max(0.0, eur_available - buffer_eur))

    if spendable < max(minq, BUY_MIN_EUR):
        need = max(minq, BUY_MIN_EUR)
        send_message(f"⛔ الرصيد غير كافٍ: متاح {eur_available:.2f}€ | بعد الهامش {spendable:.2f}€ | هامش {buffer_eur:.2f}€ | المطلوب ≥ {need:.2f}€.")
        return None

    send_message(f"💰 EUR متاح: {eur_available:.2f}€ | سننفق: {spendable:.2f}€ (هامش {buffer_eur:.2f}€ | هدف {target_eur:.2f}€)")

    patience      = min(MAKER_WAIT_MAX_SEC, get_patience_sec(market))
    started       = time.time()
    last_order    = None
    last_bid      = None
    all_fills     = []
    remaining_eur = float(spendable)

    try:
        while (time.time() - started) < patience and remaining_eur >= (minq * 0.999):
            ob = requests.get(f"{BASE_URL}/{market}/book", timeout=6).json()
            if not ob or not ob.get("bids") or not ob.get("asks"):
                time.sleep(0.25); continue
            best_bid = float(ob["bids"][0][0])
            best_ask = float(ob["asks"][0][0])
            price    = _round_price(market, min(best_bid, best_ask*(1.0-1e-6)))  # على طرف الـ bid

            # أمر قائم؟
            if last_order:
                st = _fetch_order(last_order) or {}
                st_status = (st.get("status") or "").lower()

                if st_status in ("filled","partiallyfilled"):
                    fills = st.get("fills") or []
                    if fills:
                        all_fills += fills
                        base, quote_eur, fee_eur = totals_from_fills(fills)
                        remaining_eur = max(0.0, remaining_eur - (quote_eur + fee_eur))

                if st_status == "filled" or remaining_eur < (minq * 0.999):
                    try: _cancel_order(last_order)
                    except: pass
                    last_order = None
                    break

                # stale أو تحرّك الـ bid بشكل معتبر → ألغِ وأعد التسعير
                stale = False
                last_ts = _order_last_update_ts(st)
                if last_ts:
                    stale = (time.time() - last_ts) >= STALE_ORDER_SEC
                if stale or (last_bid is None) or (abs(best_bid/last_bid - 1.0) >= MAKER_REPRICE_THRESH):
                    try: _cancel_order(last_order)
                    except: pass
                    last_order = None
                else:
                    t0 = time.time()
                    while time.time() - t0 < MAKER_REPRICE_EVERY:
                        st = _fetch_order(last_order) or {}
                        st_status = (st.get("status") or "").lower()
                        if st_status in ("filled","partiallyfilled"):
                            fills = st.get("fills") or []
                            if fills:
                                all_fills += fills
                                base, quote_eur, fee_eur = totals_from_fills(fills)
                                remaining_eur = max(0.0, remaining_eur - (quote_eur + fee_eur))
                            if st_status == "filled" or remaining_eur < (minq * 0.999):
                                try: _cancel_order(last_order)
                                except: pass
                                last_order = None
                                break
                        time.sleep(0.35)
                    if last_order:
                        continue

            # ضع أمر جديد + backoff
            if not last_order and remaining_eur >= (minq * 0.999):
                attempt   = 0
                placed    = False
                cur_price = price
                while attempt < IB_BACKOFF_TRIES and remaining_eur >= (minq * 0.999):
                    amt_base  = _calc_buy_amount_base(market, remaining_eur, cur_price)
                    if amt_base <= 0: break
                    exp_quote = amt_base * cur_price
                    send_message(f"🧪 محاولة شراء #{attempt+1}: amount={_format_amount(market, amt_base)} | سعر≈{_format_price(market, cur_price)} | EUR≈{exp_quote:.2f}")
                    res = _place_limit_postonly(market, "buy", cur_price, amount=amt_base)
                    orderId = (res or {}).get("orderId")
                    err_txt = str((res or {}).get("error","")).lower()
                    if orderId:
                        last_order = orderId
                        last_bid   = best_bid
                        placed     = True
                        break
                    if "insufficient balance" in err_txt or "not have sufficient balance" in err_txt:
                        remaining_eur *= IB_BACKOFF_FACTOR
                        attempt += 1
                        time.sleep(0.15)
                        continue
                    attempt = IB_BACKOFF_TRIES
                    break

                if not placed:
                    time.sleep(0.3)
                    continue

                # متابعة قصيرة
                t0 = time.time()
                while time.time() - t0 < MAKER_REPRICE_EVERY:
                    st = _fetch_order(last_order) or {}
                    st_status = (st.get("status") or "").lower()
                    if st_status in ("filled","partiallyfilled"):
                        fills = st.get("fills") or []
                        if fills:
                            all_fills += fills
                            base, quote_eur, fee_eur = totals_from_fills(fills)
                            remaining_eur = max(0.0, remaining_eur - (quote_eur + fee_eur))
                        if st_status == "filled" or remaining_eur < (minq * 0.999):
                            try: _cancel_order(last_order)
                            except: pass
                            last_order = None
                            break
                    time.sleep(0.35)

        if last_order:
            try: _cancel_order(last_order)
            except: pass

    except Exception as e:
        print("open_maker_buy err:", e)
    finally:
        # أمان مضاعف
        cancel_all_open_orders(market)

    if not all_fills:
        bump_patience_on_fail(market)
        send_message("⚠️ لم يكتمل شراء Maker ضمن المهلة (سنزيد الصبر تلقائياً لهذا الماركت).")
        return None

    base_amt, quote_eur, fee_eur = totals_from_fills(all_fills)
    if base_amt <= 0:
        bump_patience_on_fail(market)
        return None

    relax_patience_on_success(market)
    avg = (quote_eur + fee_eur) / base_amt
    return {"amount": base_amt, "avg": avg, "cost_eur": quote_eur + fee_eur, "fee_eur": fee_eur}

# ========= Maker Sell =========
def close_maker_sell(market: str, amount: float):
    """Maker sell على أفضل Ask، مع تجميع partial fills حتى التصفير."""
    cancel_all_open_orders(market)
    patience = min(MAKER_WAIT_MAX_SEC, get_patience_sec(market))
    started  = time.time()
    remaining = float(amount)
    all_fills = []
    last_order = None
    last_ask   = None

    try:
        while (time.time() - started) < patience and remaining > 0:
            ob = requests.get(f"{BASE_URL}/{market}/book", timeout=6).json()
            if not ob or not ob.get("bids") or not ob.get("asks"):
                time.sleep(0.25); continue

            best_bid = float(ob["bids"][0][0])
            best_ask = float(ob["asks"][0][0])
            price    = _round_price(market, max(best_ask, best_bid*(1.0+1e-6)))

            if last_order:
                st = _fetch_order(last_order) or {}
                st_status = (st.get("status") or "").lower()
                if st_status in ("filled","partiallyfilled"):
                    fills = st.get("fills") or []
                    if fills:
                        sold_base, _, _ = totals_from_fills(fills)
                        remaining = max(0.0, remaining - sold_base)
                        all_fills += fills
                if st_status == "filled" or remaining <= 0:
                    try: _cancel_order(last_order)
                    except: pass
                    last_order = None
                    break

                stale = False
                last_ts = _order_last_update_ts(st)
                if last_ts:
                    stale = (time.time() - last_ts) >= STALE_ORDER_SEC
                if stale or (last_ask is None) or (abs(best_ask/last_ask - 1.0) >= MAKER_REPRICE_THRESH):
                    try: _cancel_order(last_order)
                    except: pass
                    last_order = None
                else:
                    t0 = time.time()
                    while time.time() - t0 < MAKER_REPRICE_EVERY:
                        st = _fetch_order(last_order) or {}
                        st_status = (st.get("status") or "").lower()
                        if st_status in ("filled","partiallyfilled"):
                            fills = st.get("fills") or []
                            if fills:
                                sold_base, _, _ = totals_from_fills(fills)
                                remaining = max(0.0, remaining - sold_base)
                                all_fills += fills
                            if st_status == "filled" or remaining <= 0:
                                try: _cancel_order(last_order)
                                except: pass
                                last_order = None
                                break
                        time.sleep(0.35)
                    if last_order:
                        continue

            if remaining > 0:
                amt_to_place = _round_amount(market, remaining)
                res = _place_limit_postonly(market, "sell", price, amount=amt_to_place)
                orderId = (res or {}).get("orderId")
                if not orderId:
                    price2 = _round_price(market, best_ask)
                    res = _place_limit_postonly(market, "sell", price2, amount=amt_to_place)
                    orderId = (res or {}).get("orderId")
                if not orderId:
                    time.sleep(0.3); continue

                last_order = orderId
                last_ask   = best_ask

                t0 = time.time()
                while time.time() - t0 < MAKER_REPRICE_EVERY:
                    st = _fetch_order(last_order) or {}
                    st_status = (st.get("status") or "").lower()
                    if st_status in ("filled","partiallyfilled"):
                        fills = st.get("fills") or []
                        if fills:
                            sold_base, _, _ = totals_from_fills(fills)
                            remaining = max(0.0, remaining - sold_base)
                            all_fills += fills
                        if st_status == "filled" or remaining <= 0:
                            try: _cancel_order(last_order)
                            except: pass
                            last_order = None
                            break
                    time.sleep(0.35)

        if last_order:
            try: _cancel_order(last_order)
            except: pass

    except Exception as e:
        print("close_maker_sell err:", e)
    finally:
        cancel_all_open_orders(market)

    proceeds_eur = 0.0; fee_eur = 0.0; sold_base = 0.0
    for f in all_fills:
        amt=float(f["amount"]); price=float(f["price"]); fe=float(f.get("fee",0) or 0)
        sold_base += amt; proceeds_eur += amt*price; fee_eur += fe
    proceeds_eur -= fee_eur
    return sold_base, proceeds_eur, fee_eur

# ========= Monitor / Auto-exit =========
def current_stop_from_peak(peak_pct: float) -> float:
    stop = -999.0
    for th, val in STOP_LADDER:
        if peak_pct >= th:
            stop = val
    return stop

def monitor_loop():
    global active_trade
    while True:
        try:
            with lk:
                at = active_trade.copy() if active_trade else None
            if not at:
                time.sleep(0.25); continue

            m   = at["symbol"]
            ent = at["entry"]
            cur = fetch_price_ws_first(m)
            if not cur:
                time.sleep(0.25); continue

            pnl = ((cur/ent) - 1.0) * 100.0
            updated_peak = max(at["peak_pct"], pnl)
            new_stop = current_stop_from_peak(updated_peak)

            changed = False
            with lk:
                if active_trade:
                    if updated_peak > active_trade["peak_pct"] + 1e-9:
                        active_trade["peak_pct"] = updated_peak; changed = True
                    if abs(new_stop - active_trade.get("dyn_stop_pct", -999.0)) > 1e-9:
                        active_trade["dyn_stop_pct"] = new_stop; changed = True

            if changed:
                send_message(f"📈 تحديث: Peak={updated_peak:.2f}% → SL {new_stop:+.2f}%")

            with lk:
                at2 = active_trade.copy() if active_trade else None
            if at2 and pnl <= at2.get("dyn_stop_pct", -2.0):
                do_close_maker("Dynamic stop"); time.sleep(0.5); continue

            time.sleep(0.12)
        except Exception as e:
            print("monitor err:", e)
            time.sleep(0.5)
Thread(target=monitor_loop, daemon=True).start()

# ========= Trade Flow =========
def do_open_maker(market: str, eur: float):
    def _runner():
        global active_trade
        try:
            with lk:
                if active_trade:
                    send_message("⛔ توجد صفقة نشطة. أغلقها أولاً."); return
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
            m   = active_trade["symbol"]
            amt = float(active_trade["amount"])
            cost= float(active_trade["cost_eur"])
        sold_base, proceeds, sell_fee = close_maker_sell(m, amt)
        with lk:
            pnl_eur = proceeds - cost
            pnl_pct = (proceeds/cost - 1.0) * 100.0 if cost>0 else 0.0
            for t in reversed(executed_trades):
                if t["symbol"]==m and "exit_eur" not in t:
                    t.update({"exit_eur": proceeds, "sell_fee_eur": sell_fee,
                              "pnl_eur": pnl_eur, "pnl_pct": pnl_pct,
                              "exit_time": time.time()})
                    break
            active_trade = None
        send_message(f"💰 بيع {m.replace('-EUR','')} (Maker) | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%) {('— '+reason) if reason else ''}")
    except Exception as e:
        traceback.print_exc()
        send_message(f"🐞 خطأ أثناء الإغلاق: {e}")

# ========= Summary =========
def build_summary():
    lines=[]
    with lk:
        at = active_trade
        closed=[x for x in executed_trades if "exit_eur" in x]
    if at:
        cur = fetch_price_ws_first(at["symbol"]) or at["entry"]
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

# ========= HTTP =========
@app.route("/", methods=["GET"])
def home():
    return "Maker-Only Relay Executor ✅"

# JSON Relay (لبوت الإشارة)
@app.route("/hook", methods=["POST"])
def hook():
    try:
        data = request.get_json(silent=True) or {}
        cmd  = (data.get("cmd") or "").strip().lower()

        if cmd in ("enable","start","on"):
            global enabled; enabled=True
            send_message("✅ تم التفعيل."); return jsonify({"ok":True})

        if cmd in ("disable","stop","off"):
            enabled=False; send_message("🛑 تم الإيقاف."); return jsonify({"ok":True})

        if cmd in ("summary","/summary"):
            txt = build_summary(); send_message(txt); return jsonify({"ok":True,"summary":txt})

        if cmd in ("close","sell","exit"):
            with lk: has_position = active_trade is not None
            if has_position: do_close_maker("Manual"); return jsonify({"ok":True,"msg":"closing"})
            else: send_message("لا توجد صفقة لإغلاقها."); return jsonify({"ok":False,"err":"no_active_trade"})

        if cmd == "cancel_open":
            coin = (data.get("coin") or "").strip().upper()
            market = coin_to_market(coin) if coin else None
            if market:
                cancel_all_open_orders(market); send_message(f"🧹 ألغيت كل أوامر {market}.")
                return jsonify({"ok":True})
            return jsonify({"ok":False,"err":"bad_coin"})

        if cmd == "buy":
            if not enabled: return jsonify({"ok":False,"err":"bot_disabled"})
            coin = (data.get("coin") or "").strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{2,15}", coin or ""):
                return jsonify({"ok":False,"err":"bad_coin"})
            market = coin_to_market(coin)
            if not market:
                send_message(f"⛔ {coin}-EUR غير متاح على Bitvavo.")
                return jsonify({"ok":False,"err":"market_unavailable"})
            eur = float(data.get("eur")) if data.get("eur") is not None else None
            do_open_maker(market, eur)
            return jsonify({"ok":True,"msg":"buy_started","market":market})

        return jsonify({"ok":False,"err":"bad_cmd"})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok":False,"err":str(e)}), 500

# اختياري: Webhook تلغرام للأوامر النصية
@app.route("/tg", methods=["POST"])
def tg_webhook():
    try:
        upd = request.json or {}
        msg = upd.get("message") or upd.get("edited_message") or {}
        text = (msg.get("text") or "").strip()
        chat = str(msg.get("chat",{}).get("id",""))
        if chat and CHAT_ID and str(chat) != str(CHAT_ID):
            # حدّث CHAT_ID تلقائياً أول مرة
            pass
        if not text: return jsonify(ok=True)

        if text.startswith("/start"):
            send_message("أهلاً! أوامر: /buy ADA 8.5 | /close | /summary | /enable | /disable")
        elif text.startswith("/enable"): 
            global enabled; enabled=True; send_message("✅ تم التفعيل.")
        elif text.startswith("/disable"):
            enabled=False; send_message("🛑 تم الإيقاف.")
        elif text.startswith("/summary"):
            send_message(build_summary())
        elif text.startswith("/close"):
            with lk: has = active_trade is not None
            if has: do_close_maker("Manual")
            else: send_message("لا توجد صفقة.")
        elif text.startswith("/buy"):
            try:
                parts = text.split()
                coin  = parts[1].upper()
                eur   = float(parts[2]) if len(parts)>=3 else None
                market = coin_to_market(coin)
                if not market: send_message(f"⛔ {coin}-EUR غير مدعوم."); 
                else: do_open_maker(market, eur)
            except Exception as e:
                send_message(f"❌ صيغة: /buy ADA 10 — ({e})")
        elif text.startswith("/cancelopen"):
            try:
                coin  = text.split()[1].upper()
                market = coin_to_market(coin)
                if market: cancel_all_open_orders(market); send_message(f"🧹 ألغيت كل أوامر {market}.")
            except: send_message("صيغة: /cancelopen ADA")
        else:
            send_message("أوامر: /buy COIN [EUR] | /close | /summary | /enable | /disable | /cancelopen COIN")

        return jsonify(ok=True)
    except Exception as e:
        print("tg_webhook err:", e)
        return jsonify(ok=False), 200

# ========= Main =========
if __name__ == "__main__" or RUN_LOCAL:
    load_markets()
    app.run(host="0.0.0.0", port=PORT)