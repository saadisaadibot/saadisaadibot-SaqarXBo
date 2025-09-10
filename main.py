# -*- coding: utf-8 -*-
"""
Saqer — Maker-Only Relay Executor (Bitvavo / EUR)
- شراء/بيع Maker (postOnly) مع تجميع partial fills.
- وقف ديناميكي متدرّج حسب القمة.
- صفقة واحدة في نفس الوقت.
- أوامر تيليغرام عبر POST /webhook (نفس نمطك تمامًا) — بدون /buy في تيليغرام.
- الشراء يأتي فقط من بوت الإشارة عبر POST /hook.
- الشراء دائماً بكل الرصيد المتاح (نحجز هامش رسوم بسيط فقط)، بلا حد محلي 5€.
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

BOT_TOKEN   = os.getenv("BOT_TOKEN") or ""
CHAT_ID     = os.getenv("CHAT_ID") or ""      # (اختياري) قصر الأوامر على شات معيّن
API_KEY     = os.getenv("BITVAVO_API_KEY") or ""
API_SECRET  = os.getenv("BITVAVO_API_SECRET") or ""
REDIS_URL   = os.getenv("REDIS_URL") or ""
RUN_LOCAL   = os.getenv("RUN_LOCAL", "0") == "1"
PORT        = int(os.getenv("PORT", "5000"))

# حماية رصيد بسيطة (هامش رسوم فقط)
EST_FEE_RATE        = float(os.getenv("FEE_RATE_EST", "0.0025"))  # ≈0.25%
HEADROOM_EUR_MIN    = float(os.getenv("HEADROOM_EUR_MIN", "0.50"))# ≥0.50€
MAX_SPEND_FRACTION  = float(os.getenv("MAX_SPEND_FRACTION", "1.00"))  # 100% بشكل افتراضي
FIXED_EUR_PER_TRADE = 0.0  # مُعطّل: دائماً كل الرصيد

# Backoff عند رفض الرصيد أو قيود المنصة
IB_BACKOFF_FACTOR   = float(os.getenv("IB_BACKOFF_FACTOR", "0.96"))
IB_BACKOFF_TRIES    = int(os.getenv("IB_BACKOFF_TRIES", "6"))

r  = redis.from_url(REDIS_URL) if REDIS_URL else redis.Redis()
lk = Lock()

BASE_URL = "https://api.bitvavo.com/v2"
WS_URL   = "wss://ws.bitvavo.com/v2/"

# ========= Settings =========
MAKER_REPRICE_EVERY   = 2.0
MAKER_REPRICE_THRESH  = 0.0005  # 0.05%
MAKER_WAIT_BASE_SEC   = 45
MAKER_WAIT_MAX_SEC    = 300
MAKER_WAIT_STEP_UP    = 15
MAKER_WAIT_STEP_DOWN  = 10
WS_STALENESS_SEC      = 2.0

STOP_LADDER = [
    (0.0,  -2.0),
    (1.0,  -1.0),
    (2.0,   0.0),
    (3.0,  +1.0),
    (4.0,  +2.0),
    (5.0,  +3.0),
]

# ========= Runtime =========
enabled        = True
active_trade   = None
executed_trades= []
MARKET_MAP     = {}   # "ADA" -> "ADA-EUR"
MARKET_META    = {}   # "ADA-EUR" -> {"minQuote","minBase","tick","step"}
_ws_prices     = {}
_ws_lock       = Lock()

# ========= Utils =========
def tg_send_text(text: str, chat_id: str=None):
    try:
        if BOT_TOKEN and (chat_id or CHAT_ID):
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id or CHAT_ID, "text": text},
                timeout=8
            )
        else:
            print("TG:", text)
    except Exception as e:
        print("TG err:", e)

def send_message(text: str):
    tg_send_text(text)

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

def get_asset_available(symbol: str) -> float:
    try:
        bals = bv_request("GET", "/balance")
        if isinstance(bals, list):
            for b in bals:
                if b.get("symbol") == symbol.upper():
                    return max(0.0, float(b.get("available", 0) or 0))
    except Exception:
        pass
    return 0.0

# ========= Markets / Meta =========
def _as_float(x, default=0.0):
    try: return float(x)
    except: return default

def _parse_step(val, default_step):
    try:
        if isinstance(val, int) or (isinstance(val, str) and val.isdigit()):
            d = int(val);  return 10.0 ** (-d) if 0 <= d <= 20 else default_step
        v = float(val);   return v if 0 < v < 1 else default_step
    except: return default_step

def _decs(step: float) -> int:
    try:
        s = ("%.16f" % float(step)).rstrip("0").rstrip(".")
        return len(s.split(".")[1]) if "." in s else 0
    except: return 8

def load_markets():
    global MARKET_MAP, MARKET_META
    try:
        rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
        m, meta = {}, {}
        for r0 in rows:
            base = r0.get("base"); quote = r0.get("quote"); market = r0.get("market")
            if base and quote == "EUR":
                tick = _parse_step(r0.get("pricePrecision", 6), 1e-6)
                step = _parse_step(r0.get("amountPrecision", 8), 1e-8)
                m[base.upper()] = market
                meta[market] = {
                    "minQuote": _as_float(r0.get("minOrderInQuoteAsset", 0.0), 0.0),
                    "minBase":  _as_float(r0.get("minOrderInBaseAsset",  0.0), 0.0),
                    "tick":     tick,
                    "step":     step,
                }
        if m: MARKET_MAP = m
        if meta: MARKET_META = meta
    except Exception as e:
        print("load_markets err:", e)

def coin_to_market(coin: str):
    if not MARKET_MAP:
        load_markets()
    return MARKET_MAP.get(coin.upper())

def _round_price(market, price):
    tick = (MARKET_META.get(market, {}) or {}).get("tick", 1e-6)
    decs = _decs(tick)
    p = math.floor(float(price) / tick) * tick
    return round(max(tick, p), decs)

def _round_amount(market, amount):
    step = (MARKET_META.get(market, {}) or {}).get("step", 1e-8)
    a = math.floor(float(amount) / step) * step
    decs = _decs(step)
    return round(max(step, a), decs)

def _format_price(market, price) -> str:
    tick = (MARKET_META.get(market, {}) or {}).get("tick", 1e-6)
    decs = _decs(tick)
    return f"{_round_price(market, price):.{decs}f}"

def _format_amount(market, amount) -> str:
    step = (MARKET_META.get(market, {}) or {}).get("step", 1e-8)
    decs = _decs(step)
    return f"{_round_amount(market, amount):.{decs}f}"

def _min_quote(market): return (MARKET_META.get(market, {}) or {}).get("minQuote", 0.0)
def _min_base(market):  return (MARKET_META.get(market, {}) or {}).get("minBase", 0.0)

# ========= WS Prices =========
def _ws_on_open(ws): pass
def _ws_on_message(ws, msg):
    try: data = json.loads(msg)
    except: return
    if isinstance(data, dict) and data.get("event") == "ticker":
        m = data.get("market")
        price = data.get("price") or data.get("lastPrice") or data.get("open")
        try:
            p = float(price)
            if p > 0:
                with _ws_lock: _ws_prices[m] = {"price": p, "ts": time.time()}
        except: pass
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
    except Exception:
        pass

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

# ========= Maker-only Order Helpers =========
def _place_limit_postonly(market, side, price, amount=None):
    if amount is None or float(amount) <= 0:
        raise ValueError("amount is required for limit postOnly")
    body = {
        "market": market,
        "side": side,
        "orderType": "limit",
        "postOnly": True,
        "clientOrderId": str(uuid4()),
        "price": _format_price(market, price),
        "amount": _format_amount(market, float(amount)),
        "operatorId": ""  # إلزامي ولو فاضي
    }
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

# ========= Maker Buy (كل الرصيد) =========
def _calc_buy_amount_base(market: str, target_eur: float, use_price: float) -> float:
    price = max(1e-12, float(use_price))
    base_amt = float(target_eur) / price
    # ما نفرض minBase محليًا — نخلي المنصّة ترفض إذا ما بيمرّ
    return _round_amount(market, base_amt)

def open_maker_buy(market: str, eur_amount: float=None):
    """Maker postOnly على أفضل Bid — دائماً نصرف كل الرصيد المتاح (ناقص هامش الرسوم)."""
    eur_available = get_eur_available()

    # دائماً كل الرصيد (إلا إذا مرّر eur_amount صراحة)
    target_eur = float(eur_amount) if (eur_amount and eur_amount>0) else float(eur_available)
    target_eur = min(target_eur, eur_available * MAX_SPEND_FRACTION)

    buffer_eur = max(HEADROOM_EUR_MIN, target_eur * EST_FEE_RATE * 2.0, 0.05)
    spendable  = min(target_eur, max(0.0, eur_available - buffer_eur))

    send_message(f"💰 EUR متاح: €{eur_available:.2f} | سننفق: €{spendable:.2f} (هامش €{buffer_eur:.2f}) | ماركت: {market}")

    patience      = get_patience_sec(market)
    started       = time.time()
    last_order    = None
    last_bid      = None
    all_fills     = []
    remaining_eur = float(spendable)
    last_seen_price = None

    try:
        while (time.time() - started) < patience and remaining_eur > 0.0:
            ob = fetch_orderbook(market)
            if not ob or not ob.get("bids") or not ob.get("asks"):
                time.sleep(0.25); continue

            best_bid = float(ob["bids"][0][0])
            best_ask = float(ob["asks"][0][0])
            raw_price = min(best_bid, best_ask*(1.0-1e-6))
            price = _round_price(market, raw_price)
            last_seen_price = price

            # أمر قائم؟
            if last_order:
                st = _fetch_order(last_order); st_status = st.get("status")
                if st_status in ("filled","partiallyFilled"):
                    fills = st.get("fills", []) or []
                    if fills:
                        all_fills += fills
                        base, quote_eur, fee_eur = totals_from_fills(fills)
                        remaining_eur = max(0.0, remaining_eur - (quote_eur + fee_eur))
                if st_status == "filled" or remaining_eur <= 0.0:
                    try: _cancel_order(last_order)
                    except: pass
                    last_order = None
                    break

                if (last_bid is None) or (abs(best_bid/last_bid - 1.0) >= MAKER_REPRICE_THRESH):
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
                                all_fills += fills
                                base, quote_eur, fee_eur = totals_from_fills(fills)
                                remaining_eur = max(0.0, remaining_eur - (quote_eur + fee_eur))
                            if st_status == "filled" or remaining_eur <= 0.0:
                                try: _cancel_order(last_order)
                                except: pass
                                last_order = None
                                break
                        time.sleep(0.35)
                    if last_order:
                        continue

            # ضع أمر جديد بكل ما تبقى — مع backoff إذا رفضت المنصّة
            if not last_order and remaining_eur > 0.0:
                attempt   = 0
                placed    = False
                cur_price = price

                while attempt < IB_BACKOFF_TRIES and remaining_eur > 0.0:
                    amt_base = _calc_buy_amount_base(market, remaining_eur, cur_price)
                    if amt_base <= 0:
                        break

                    exp_quote = amt_base * cur_price
                    send_message(
                        f"🧪 محاولة شراء #{attempt+1} على {market}: "
                        f"amount={_format_amount(market, amt_base)} | سعر≈{_format_price(market, cur_price)} | EUR≈{exp_quote:.2f}"
                    )

                    res = _place_limit_postonly(market, "buy", cur_price, amount=amt_base)
                    orderId = (res or {}).get("orderId")
                    err_txt = str((res or {}).get("error","")).lower()

                    if orderId:
                        last_order = orderId
                        last_bid   = best_bid
                        placed     = True
                        break

                    # أي رفض (balance/min-order...) → خفّض المبلغ وجرب تاني
                    remaining_eur = remaining_eur * IB_BACKOFF_FACTOR
                    attempt += 1
                    time.sleep(0.15)

                if not placed:
                    time.sleep(0.3)
                    continue

                # متابعة قصيرة
                t0 = time.time()
                while time.time() - t0 < MAKER_REPRICE_EVERY:
                    st = _fetch_order(last_order); st_status = st.get("status")
                    if st_status in ("filled","partiallyFilled"):
                        fills = st.get("fills", []) or []
                        if fills:
                            all_fills += fills
                            base, quote_eur, fee_eur = totals_from_fills(fills)
                            remaining_eur = max(0.0, remaining_eur - (quote_eur + fee_eur))
                        if st_status == "filled" or remaining_eur <= 0.0:
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

    if not all_fills:
        send_message("⚠️ لم يكتمل شراء Maker ضمن المهلة.")
        bump_patience_on_fail(market)
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
def do_open_maker(market: str, eur: float=None):
    def _runner():
        global active_trade
        try:
            with lk:
                if active_trade:
                    send_message("⛔ توجد صفقة نشطة. أغلقها أولاً."); return
            res = open_maker_buy(market, eur)
            if not res:
                send_message("⏳ لم يكتمل شراء Maker.")
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

# ========= Telegram Webhook — نفس نمطك تمامًا =========
def _auth_chat(chat_id: str) -> bool:
    return (not CHAT_ID) or (str(chat_id) == str(CHAT_ID))

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    try:
        upd = request.get_json(silent=True) or {}
        msg = upd.get("message") or upd.get("edited_message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str(msg.get("chat", {}).get("id", CHAT_ID or ""))

        if not chat_id:
            return jsonify(ok=True)

        if not _auth_chat(chat_id):
            tg_send_text("⛔ غير مصرّح.", chat_id)
            return jsonify(ok=True)

        if text.startswith("/start"):
            tg_send_text(
                "أهلًا! أوامر تيليغرام:\n"
                "/summary — ملخّص\n"
                "/enable — تفعيل\n"
                "/disable — إيقاف\n"
                "/close — إغلاق الصفقة\n\n"
                "ملاحظة: الشراء فقط عبر بوت الإشارة (POST /hook).",
                chat_id
            )

        elif text.startswith("/summary"):
            tg_send_text(build_summary(), chat_id)

        elif text.startswith("/enable"):
            global enabled; enabled = True
            tg_send_text("✅ تم التفعيل.", chat_id)

        elif text.startswith("/disable"):
            globals()["enabled"] = False
            tg_send_text("🛑 تم الإيقاف.", chat_id)

        elif text.startswith("/close"):
            with lk:
                has_position = active_trade is not None
            if has_position:
                tg_send_text("⏳ جاري الإغلاق (Maker)…", chat_id)
                do_close_maker("Manual")
            else:
                tg_send_text("لا توجد صفقة لإغلاقها.", chat_id)

        else:
            tg_send_text("أوامر: /summary /enable /disable /close\n(الشراء فقط من بوت الإشارة)", chat_id)

        return jsonify(ok=True)

    except Exception as e:
        print("Telegram webhook err:", e)
        return jsonify(ok=True)

# ========= Relay Webhook — الشراء فقط من بوت الإشارة =========
@app.route("/hook", methods=["POST"])
def hook():
    """
    JSON متوقّع من البوت الخارجي:
      {"cmd":"buy","coin":"ADA"}        # eur اختياري — افتراضي كل الرصيد
      {"cmd":"buy","coin":"ADA","eur":50.0}
    """
    try:
        data = request.get_json(silent=True) or {}
        cmd  = (data.get("cmd") or "").strip().lower()
        if cmd != "buy":
            return jsonify({"ok":False,"err":"only_buy_allowed_here"}), 400

        if not enabled:
            return jsonify({"ok":False,"err":"bot_disabled"})

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

    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok":False,"err":str(e)}), 500

# ========= Health =========
@app.route("/", methods=["GET"])
def home():
    return "Maker-Only Relay Executor ✅"

@app.route("/summary", methods=["GET"])
def http_summary():
    txt = build_summary()
    return f"<pre>{txt}</pre>"

# ========= Main =========
if __name__ == "__main__" or RUN_LOCAL:
    load_markets()
    app.run(host="0.0.0.0", port=PORT)