# -*- coding: utf-8 -*-
"""
Saqer — Maker-Only (Bitvavo/EUR) + Telegram Webhook
- شراء/بيع Maker postOnly مع تجميع partial fills.
- تسعير آمن من orderbook فقط + rounding بدقة السوق.
- حماية رصيد + backoff ذكي قبل الإرسال (حل 216).
- ضبط كمية على step السوق (حل 429).
- تنظيف أوتوماتيكي لكل الأوامر المعلّقة.
- أوامر تلغرام عبر Webhook: /buy /close /summary /enable /disable
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

# Telegram
BOT_TOKEN   = os.getenv("BOT_TOKEN")     # توكن البوت
CHAT_ID     = os.getenv("CHAT_ID")       # الشات المسموح (رقمك)
TG_SECRET   = os.getenv("TG_SECRET","")  # اختياري: كلمة سر في URL

# Bitvavo
API_KEY     = os.getenv("BITVAVO_API_KEY")
API_SECRET  = os.getenv("BITVAVO_API_SECRET")

# Redis
REDIS_URL   = os.getenv("REDIS_URL")

RUN_LOCAL   = os.getenv("RUN_LOCAL", "0") == "1"
PORT        = int(os.getenv("PORT", "5000"))

# حماية الرصيد
EST_FEE_RATE        = float(os.getenv("FEE_RATE_EST", "0.0025"))   # ≈0.25%
HEADROOM_EUR_MIN    = float(os.getenv("HEADROOM_EUR_MIN", "0.50")) # ≥ €0.50
MAX_SPEND_FRACTION  = float(os.getenv("MAX_SPEND_FRACTION", "0.90"))
FIXED_EUR_PER_TRADE = float(os.getenv("FIXED_EUR", "0"))           # 0=معطّل

# Backoff عند رفض الرصيد
IB_BACKOFF_FACTOR   = float(os.getenv("IB_BACKOFF_FACTOR", "0.94"))
IB_BACKOFF_MIN_EUR  = float(os.getenv("IB_BACKOFF_MIN_EUR","1.00"))
IB_BACKOFF_TRIES    = int(os.getenv("IB_BACKOFF_TRIES", "5"))

# حلقات التنفيذ/تسعير
MAX_TRADES            = 1
MAKER_REPRICE_EVERY   = 2.0
MAKER_REPRICE_THRESH  = 0.0005   # 0.05%
MAKER_WAIT_BASE_SEC   = 45
MAKER_WAIT_MAX_SEC    = 300
MAKER_WAIT_STEP_UP    = 15
MAKER_WAIT_STEP_DOWN  = 10
BUY_MIN_EUR           = 5.0
WS_STALENESS_SEC      = 2.0
POLL_INTERVAL         = 0.35

# محاولات حقيقية فقط
ATTEMPT_SPACING_SEC   = 1.10   # فاصل زمني حقيقي بين محاولات إرسال الأوامر
NET_WAIT_AFTER_POST   = 0.25   # مهلة قصيرة بعد الإرسال قبل فحص الحالة
MAX_ATTEMPTS_PER_RUN  = 12     # سقف المحاولات ضمن جولة واحدة

STOP_LADDER = [
    (0.0,  -2.0),
    (1.0,  -1.0),
    (2.0,   0.0),
    (3.0,  +1.0),
    (4.0,  +2.0),
    (5.0,  +3.0),
]

BASE_URL = "https://api.bitvavo.com/v2"
WS_URL   = "wss://ws.bitvavo.com/v2/"

# ========= Runtime =========
enabled        = True
active_trade   = None
executed_trades= []
MARKET_MAP     = {}   # "ADA" -> "ADA-EUR"
MARKET_META    = {}   # "ADA-EUR" -> {"minQuote","minBase","tick","step"}
_ws_prices     = {}
_ws_lock       = Lock()
lk             = Lock()
r  = redis.from_url(REDIS_URL) if REDIS_URL else redis.Redis()

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
        j = resp.json()
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

# ========= WS Prices =========
def _ws_on_open(ws): pass
def _ws_on_message(ws, msg):
    try:
        data = json.loads(msg)
    except Exception:
        return
    if isinstance(data, dict) and data.get("event") == "ticker":
        m = data.get("market")
        price = data.get("price") or data.get("lastPrice") or data.get("open")
        try:
            p = float(price)
            if p > 0:
                with _ws_lock:
                    _ws_prices[m] = {"price": p, "ts": time.time()}
        except Exception:
            pass
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

def fetch_orderbook(market):
    try:
        j = requests.get(f"{BASE_URL}/{market}/book", timeout=6).json()
        if j and j.get("bids") and j.get("asks"):
            return j
    except Exception:
        pass
    return None

def best_prices(market: str):
    ob = fetch_orderbook(market)
    if not ob or not ob.get("bids") or not ob.get("asks"):
        return None, None
    bb = float(ob["bids"][0][0]); aa = float(ob["asks"][0][0])
    return _round_price(market, bb), _round_price(market, aa)

# ========= Bitvavo helpers =========
def _place_limit_postonly(market, side, price, amount=None):
    if amount is None or float(amount) <= 0:
        raise ValueError("amount is required for limit postOnly")
    body={"market":market,
          "side":side,
          "orderType":"limit",
          "postOnly":True,
          "clientOrderId":str(uuid4()),
          "price": _format_price(market, price),
          "amount": _format_amount(market, float(amount)),
          "operatorId": ""}   # إلزامي
    return bv_request("POST","/order", body)

def _fetch_order(orderId):   return bv_request("GET",    f"/order?orderId={orderId}")
def _cancel_order(orderId):  return bv_request("DELETE", f"/order?orderId={orderId}")

def _list_open_orders(market: str):
    try:
        j = bv_request("GET", f"/orders?market={market}")
        if isinstance(j, list):
            return [o.get("orderId") for o in j if o.get("orderId")]
    except Exception:
        pass
    return []

def _cancel_all_open(market: str):
    ids = _list_open_orders(market)
    for oid in ids:
        try: _cancel_order(oid)
        except: pass
    return len(ids)

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
    except Exception:
        pass
    return MAKER_WAIT_BASE_SEC
def bump_patience_on_fail(market):
    try:
        cur = get_patience_sec(market)
        r.set(_patience_key(market), min(MAKER_WAIT_MAX_SEC, cur + MAKER_WAIT_STEP_UP))
    except Exception:
        pass
def relax_patience_on_success(market):
    try:
        cur = get_patience_sec(market)
        base = MAKER_WAIT_BASE_SEC
        r.set(_patience_key(market), max(base, cur - MAKER_WAIT_STEP_DOWN))
    except Exception:
        pass

# ========= Maker Buy (نهائي) =========
def _calc_buy_amount_base_safe(market: str, target_eur: float, use_price: float) -> float:
    step = (MARKET_META.get(market, {}) or {}).get("step", 1e-8)
    minq = _min_quote(market)
    price = max(1e-12, float(use_price))
    want = max(target_eur, minq)
    base = want / price
    base = math.floor(base / step) * step
    base = _round_amount(market, max(base, step))
    if base * price < minq:
        base = _round_amount(market, base + step)
    return base

def open_maker_buy(market: str, eur_amount: float):
    started = time.time()

    eur_avail = get_eur_available()
    if FIXED_EUR_PER_TRADE and FIXED_EUR_PER_TRADE > 0:
        target = float(FIXED_EUR_PER_TRADE)
    elif eur_amount is None or eur_amount <= 0:
        target = float(eur_avail)
    else:
        target = float(eur_amount)

    target = min(target, eur_avail * MAX_SPEND_FRACTION)
    minq   = _min_quote(market)
    buffer = max(HEADROOM_EUR_MIN, target * EST_FEE_RATE * 2.0, 0.05)
    spend  = min(target, max(0.0, eur_avail - buffer))

    if spend < max(minq, BUY_MIN_EUR):
        send_message(f"⛔ الرصيد غير كافٍ. متاح: €{eur_avail:.2f} | بعد الهامش: €{spend:.2f} "
                     f"| المطلوب ≥ €{max(minq, BUY_MIN_EUR):.2f} (هامش €{buffer:.2f}).")
        return None

    send_message(f"💰 EUR متاح: €{eur_avail:.2f} | سننفق: €{spend:.2f} (هامش €{buffer:.2f})")

    patience    = get_patience_sec(market)
    attempts    = 0
    placed_id   = None
    all_fills   = []
    last_post_t = 0.0
    remaining   = float(spend)

    try:
        while (time.time() - started) < patience and attempts < MAX_ATTEMPTS_PER_RUN:
            # spacing حقيقي
            if time.time() - last_post_t < ATTEMPT_SPACING_SEC:
                time.sleep(ATTEMPT_SPACING_SEC - (time.time() - last_post_t))

            eur_avail = get_eur_available()
            spend_cap = min(remaining, eur_avail - HEADROOM_EUR_MIN)
            if spend_cap < minq:
                remaining = max(IB_BACKOFF_MIN_EUR, remaining * IB_BACKOFF_FACTOR)
                if remaining < minq:
                    break

            bid, ask = best_prices(market)
            if not bid or not ask:
                time.sleep(0.25); continue

            price = _round_price(market, min(bid, ask * (1.0 - 1e-6)))
            amt_base = _calc_buy_amount_base_safe(market, remaining, price)

            quote_need = amt_base * price
            # لا تتجاوز القدرة مع الرسوم
            if quote_need + (quote_need * EST_FEE_RATE) > eur_avail - 1e-8:
                step = (MARKET_META.get(market, {}) or {}).get("step", 1e-8)
                amt_base = _round_amount(market, max(step, amt_base - step))
                quote_need = amt_base * price

            if quote_need < minq:
                step = (MARKET_META.get(market, {}) or {}).get("step", 1e-8)
                test_amt = _round_amount(market, amt_base + step)
                if test_amt * price <= eur_avail - HEADROOM_EUR_MIN:
                    amt_base = test_amt
                    quote_need = amt_base * price

            if amt_base <= 0:
                break

            attempts += 1
            send_message(f"🧪 محاولة شراء #{attempts}: amount={_format_amount(market, amt_base)} "
                         f"| EUR≈{quote_need:.2f} | bid≈{_format_price(market, bid)} / ask≈{_format_price(market, ask)}")

            res = _place_limit_postonly(market, "buy", price, amount=amt_base)
            last_post_t = time.time()

            orderId = (res or {}).get("orderId")
            err_txt = str((res or {}).get("error","")).lower()

            if not orderId:
                if "insufficient balance" in err_txt or "not have sufficient balance" in err_txt:
                    remaining = max(IB_BACKOFF_MIN_EUR, remaining * IB_BACKOFF_FACTOR)
                    continue
                if "market parameter is required" in err_txt:
                    send_message("⚠️ رفض الطلب: market مطلوب. سنوقف هذه الجولة.")
                    break
                if "amount" in err_txt and "decimal" in err_txt:
                    step = (MARKET_META.get(market, {}) or {}).get("step", 1e-8)
                    remaining = max(minq, (amt_base - step) * price)
                    continue
                time.sleep(0.35)
                continue

            placed_id = orderId
            time.sleep(NET_WAIT_AFTER_POST)

            t0 = time.time()
            while time.time() - t0 < MAKER_REPRICE_EVERY:
                st = _fetch_order(placed_id)
                st_status = st.get("status")
                if st_status in ("filled", "partiallyFilled"):
                    fills = st.get("fills", []) or []
                    if fills:
                        all_fills += fills
                        base, quote_eur, fee_eur = totals_from_fills(fills)
                        remaining = max(0.0, remaining - (quote_eur + fee_eur))
                    if st_status == "filled" or remaining < minq:
                        try: _cancel_order(placed_id)
                        except: pass
                        placed_id = None
                        break
                time.sleep(0.35)

            if placed_id:
                cur_bid, _ = best_prices(market)
                if cur_bid and abs(cur_bid / bid - 1.0) >= MAKER_REPRICE_THRESH:
                    try: _cancel_order(placed_id)
                    except: pass
                    placed_id = None

            if remaining < minq:
                break

        _cancel_all_open(market)

    except Exception as e:
        print("open_maker_buy err:", e)
        _cancel_all_open(market)

    if not all_fills:
        bump_patience_on_fail(market)
        took = time.time() - started
        send_message(f"⚠️ لم يكتمل شراء Maker.\n• أوامر موضوعة: {1 if placed_id else 0}\n"
                     f"• محاولات داخلية: {attempts}\n• زمن: {took:.1f}s\n"
                     f"سنُعزّز الصبر تلقائيًا وسنحاول لاحقًا.")
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
    last_order = None
    last_ask   = None

    try:
        while (time.time() - started) < patience and remaining > 0:
            ob = fetch_orderbook(market)
            if not ob or not ob.get("bids") or not ob.get("asks"):
                time.sleep(0.25); continue

            best_bid = float(ob["bids"][0][0])
            best_ask = float(ob["asks"][0][0])
            price    = _round_price(market, max(best_ask, best_bid*(1.0+1e-6)))

            if last_order:
                st = _fetch_order(last_order); st_status = st.get("status")
                if st_status in ("filled","partiallyFilled"):
                    fills = st.get("fills", []) or []
                    if fills:
                        sold_base, _, _ = totals_from_fills(fills)
                        remaining = max(0.0, remaining - sold_base)
                        all_fills += fills
                if st_status == "filled" or remaining <= 0:
                    try: _cancel_order(last_order)
                    except: pass
                    last_order = None
                    break

                if (last_ask is None) or (abs(best_ask/last_ask - 1.0) >= MAKER_REPRICE_THRESH):
                    try: _cancel_order(last_order)
                    except: pass
                    last_order = None
                else:
                    t0 = time.time()
                    while time.time() - t0 < MAKER_REPRICE_EVERY:
                        st = _fetch_order(last_order); st_status = st.get("status")
                        if st_status in ("filled","partiallyFilled"):
                            fills = st.get("fills", []) or []
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
                orderId = res.get("orderId")
                if not orderId:
                    price2 = _round_price(market, best_ask)
                    res = _place_limit_postonly(market, "sell", price2, amount=amt_to_place)
                    orderId = res.get("orderId")
                if not orderId:
                    time.sleep(0.3); continue

                last_order = orderId
                last_ask   = best_ask

                t0 = time.time()
                while time.time() - t0 < MAKER_REPRICE_EVERY:
                    st = _fetch_order(last_order); st_status = st.get("status")
                    if st_status in ("filled","partiallyFilled"):
                        fills = st.get("fills", []) or []
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

    proceeds_eur = 0.0; fee_eur = 0.0; sold_base = 0.0
    for f in all_fills:
        amt=float(f["amount"]); price=float(f["price"]); fe=float(f.get("fee",0) or 0)
        sold_base += amt
        proceeds_eur += amt*price
        fee_eur += fe

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

            # نستخدم السعر الأخير من orderbook أيضاً
            bid, ask = best_prices(m)
            cur = bid if bid else ent
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
            if not active_trade:
                send_message("لا توجد صفقة لإغلاقها.")
                return
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
        bid, _ = best_prices(at["symbol"])
        cur = bid or at["entry"]
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
def _parse_cmd(txt: str):
    t = (txt or "").strip()
    if not t: return None, {}
    if t.startswith("/"):
        t = t.split()[0] if " " not in t else t
    parts = t.split()
    cmd = parts[0].lower()
    args = parts[1:]
    return cmd, args

@app.route("/tg-webhook", methods=["POST"])
def tg_webhook():
    if TG_SECRET and request.args.get("key","") != TG_SECRET:
        return jsonify({"ok":False}), 403
    try:
        upd = request.get_json(silent=True) or {}
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat_id = str(((msg.get("chat") or {}).get("id")) or "")
        if CHAT_ID and chat_id and chat_id != str(CHAT_ID):
            return jsonify({"ok":True})  # نتجاهل غير المصرّح

        text = (msg.get("text") or "").strip()
        cmd, args = _parse_cmd(text)
        if not cmd: return jsonify({"ok":True})

        if cmd in ("/enable","/start","enable"):
            global enabled
            enabled = True
            send_message("✅ تم التفعيل.")
            return jsonify({"ok":True})

        if cmd in ("/disable","/stop","disable"):
            enabled = False
            send_message("🛑 تم الإيقاف.")
            return jsonify({"ok":True})

        if cmd in ("/summary","summary"):
            send_message(build_summary()); return jsonify({"ok":True})

        if cmd in ("/close","close","sell","exit"):
            do_close_maker("Manual"); return jsonify({"ok":True})

        if cmd in ("/buy","buy"):
            if not enabled:
                send_message("⛔ البوت مُعطّل."); return jsonify({"ok":True})
            if not args:
                send_message("اكتب: /buy COIN [EUR]"); return jsonify({"ok":True})
            coin = args[0].upper()
            if not re.fullmatch(r"[A-Z0-9]{2,15}", coin):
                send_message("رمز غير صالح."); return jsonify({"ok":True})
            market = coin_to_market(coin)
            if not market:
                send_message(f"⛔ {coin}-EUR غير متاح على Bitvavo."); return jsonify({"ok":True})
            eur = None
            if len(args) >= 2:
                try: eur = float(args[1])
                except: eur = None
            do_open_maker(market, eur)
            return jsonify({"ok":True})

        # أوامر مساعدة
        if cmd in ("/help","help"):
            send_message("أوامر: /buy COIN [EUR] | /close | /summary | /enable | /disable")
            return jsonify({"ok":True})

        return jsonify({"ok":True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok":False,"err":str(e)}), 500

# ========= Health =========
@app.route("/", methods=["GET","POST"])
def home():
    if request.method == "GET":
        return "Maker-Only Relay Executor ✅"
    return "Method Not Allowed", 405

# ========= Main =========
if __name__ == "__main__" or RUN_LOCAL:
    load_markets()
    app.run(host="0.0.0.0", port=PORT)