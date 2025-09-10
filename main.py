# -*- coding: utf-8 -*-
"""
Saqer — Maker-Only (Bitvavo/EUR) + Telegram Webhook
- فلتر سلامة سعر: لا نضع أمر إذا السعر شاذ مقارنة بالـ bid/ask وWS.
- لا توجد "محاولات وهمية": إشعار المحاولة فقط مع إرسال أمر فعلي.
- Maker-only مع تجميع partial fills، ودقة السعر/الكمية من السوق.
- Backoff لخطأ 216 + إلغاء مضمون لأي أمر عالق.
- أوامر تيليغرام عبر /tg فقط. بوت الإشارة للشراء فقط عبر /hook.
"""

import os, re, time, json, math, traceback, hmac, hashlib
import requests, redis, websocket
from threading import Thread, Lock
from uuid import uuid4
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

# ---------- ENV ----------
BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHAT_ID     = os.getenv("CHAT_ID")
TG_SECRET   = os.getenv("TG_SECRET", "")  # اختياري للتحقق من webhook

API_KEY     = os.getenv("BITVAVO_API_KEY")
API_SECRET  = os.getenv("BITVAVO_API_SECRET")
BASE_URL    = "https://api.bitvavo.com/v2"
WS_URL      = "wss://ws.bitvavo.com/v2/"

REDIS_URL   = os.getenv("REDIS_URL")
r  = redis.from_url(REDIS_URL) if REDIS_URL else redis.Redis()

RUN_LOCAL   = os.getenv("RUN_LOCAL", "0") == "1"
PORT        = int(os.getenv("PORT", "5000"))

# ---------- Trading Settings ----------
BUY_MIN_EUR             = 5.0
EST_FEE_RATE            = float(os.getenv("FEE_RATE_EST", "0.0025"))
HEADROOM_EUR_MIN        = float(os.getenv("HEADROOM_EUR_MIN", "0.50"))
MAX_SPEND_FRACTION      = float(os.getenv("MAX_SPEND_FRACTION", "0.90"))
FIXED_EUR_PER_TRADE     = float(os.getenv("FIXED_EUR", "0"))

MAKER_REPRICE_EVERY     = 2.0
MAKER_REPRICE_THRESH    = 0.0005
MAKER_WAIT_BASE_SEC     = 45
MAKER_WAIT_MAX_SEC      = 300
MAKER_WAIT_STEP_UP      = 15
MAKER_WAIT_STEP_DOWN    = 10

# محوّلات محاولات واقعية
MIN_PLACE_GAP_SEC       = 1.2
MIN_STATUS_GAP_SEC      = 0.35

# Backoff ل216
IB_BACKOFF_FACTOR       = 0.96
IB_BACKOFF_TRIES        = 5

# WS
WS_STALENESS_SEC        = 2.0

# ---------- Runtime ----------
enabled        = True
active_trade   = None
executed_trades= []
MARKET_MAP     = {}
MARKET_META    = {}
_ws_prices     = {}
_ws_lock       = Lock()
lk             = Lock()

# ---------- Utils ----------
def send_message(text: str, chat_id=None):
    chat_id = chat_id or CHAT_ID
    try:
        if BOT_TOKEN and chat_id:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          data={"chat_id": chat_id, "text": text}, timeout=8)
        else:
            print("TG:", text)
    except Exception as e:
        print("TG err:", e)

def _sign(ts, method, path, body_str=""):
    msg = f"{ts}{method}{path}{body_str}"
    return hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def bv_request(method, path, body=None, timeout=12):
    url = f"{BASE_URL}{path}"
    ts  = str(int(time.time()*1000))
    body_str = "" if method=="GET" else json.dumps(body or {}, separators=(',',':'))
    sig = _sign(ts, method, f"/v2{path}", body_str)
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

# ---------- Markets/Precision ----------
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

def _decs(step: float) -> int:
    try:
        if step >= 1: return 0
        return max(0, int(round(-math.log10(step))))
    except Exception:
        return 8

def _round_price(market, price):
    tick = (MARKET_META.get(market, {}) or {}).get("tick", 1e-6)
    decs = _decs(tick)
    p = round(price, decs)
    return max(tick, p)

def _round_amount(market, amount):
    step = (MARKET_META.get(market, {}) or {}).get("step", 1e-8)
    floored = math.floor(float(amount) / step) * step
    decs = _decs(step)
    return round(max(step, floored), decs)

def _fmt_price(market, price): 
    tick = (MARKET_META.get(market, {}) or {}).get("tick", 1e-6)
    return f"{_round_price(market, price):.{_decs(tick)}f}"

def _fmt_amount(market, amount):
    step = (MARKET_META.get(market, {}) or {}).get("step", 1e-8)
    return f"{_round_amount(market, amount):.{_decs(step)}f}"

def _min_quote(market): return (MARKET_META.get(market, {}) or {}).get("minQuote", BUY_MIN_EUR)
def _min_base(market):  return (MARKET_META.get(market, {}) or {}).get("minBase", 0.0)

# ---------- WS prices ----------
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

def _ws_thread():
    while True:
        try:
            ws = websocket.WebSocketApp(WS_URL, on_message=_ws_on_message)
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
            with _ws_lock:
                _ws_prices[market] = {"price": p, "ts": now}
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

# ---------- Maker Helpers ----------
def _place_limit_postonly(market, side, price, amount):
    if amount is None or float(amount) <= 0:
        raise ValueError("amount is required for limit postOnly")
    body = {
        "market": market,
        "side": side,
        "orderType": "limit",
        "postOnly": True,
        "clientOrderId": str(uuid4()),
        "price": _fmt_price(market, price),
        "amount": _fmt_amount(market, float(amount)),
        "operatorId": ""
    }
    return bv_request("POST","/order", body)

def _fetch_order(orderId):  return bv_request("GET",    f"/order?orderId={orderId}")
def _cancel_order(orderId): return bv_request("DELETE", f"/order?orderId={orderId}")

def totals_from_fills(fills):
    tb=tq=fee=0.0
    for f in (fills or []):
        amt=float(f["amount"]); price=float(f["price"]); fe=float(f.get("fee",0) or 0)
        tb+=amt; tq+=amt*price; fee+=fe
    return tb, tq, fee

# ---------- Patience (Redis) ----------
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

# ---------- Price sanity ----------
def _price_ok(market, bid, ask, use_price):
    if bid <= 0 or ask <= 0: return False
    mid = (bid + ask) / 2.0
    # لا بد يكون داخل [0.5x..2x] من المِد
    if not (0.5*mid <= use_price <= 2.0*mid):
        return False
    pws = fetch_price_ws_first(market) or mid
    # ولا يبتعد عن WS أكثر من ×2
    if not (0.5*pws <= use_price <= 2.0*pws):
        return False
    return True

# ---------- Buy Maker ----------
def _calc_buy_amount_base(market: str, target_eur: float, use_price: float) -> float:
    min_base = _min_base(market)
    price    = max(1e-12, float(use_price))
    base_amt = float(target_eur) / price
    base_amt = max(base_amt, min_base)
    return _round_amount(market, base_amt)

def open_maker_buy(market: str, eur_amount: float, chat_id=None):
    eur_available = get_eur_available()

    if FIXED_EUR_PER_TRADE > 0:
        target_eur = float(FIXED_EUR_PER_TRADE)
    elif not eur_amount or eur_amount <= 0:
        target_eur = float(eur_available)
    else:
        target_eur = float(eur_amount)

    target_eur = min(target_eur, eur_available * MAX_SPEND_FRACTION)
    minq = _min_quote(market)

    buffer_eur = max(HEADROOM_EUR_MIN, target_eur * EST_FEE_RATE * 2.0, 0.05)
    spendable  = min(target_eur, max(0.0, eur_available - buffer_eur))

    if spendable < max(minq, BUY_MIN_EUR):
        need = max(minq, BUY_MIN_EUR)
        send_message(f"⛔ الرصيد غير كافٍ: متاح €{eur_available:.2f} | بعد الهامش €{spendable:.2f} | هامش €{buffer_eur:.2f} | المطلوب ≥ €{need:.2f}.", chat_id)
        return None

    send_message(f"💰 EUR متاح: €{eur_available:.2f} | سننفق: €{spendable:.2f} (هامش €{buffer_eur:.2f})", chat_id)

    patience        = get_patience_sec(market)
    started         = time.time()
    last_order      = None
    last_bid        = None
    all_fills       = []
    remaining_eur   = float(spendable)
    last_place_ts   = 0.0
    placed_count    = 0
    inner_tries     = 0

    try:
        while (time.time() - started) < patience and remaining_eur >= (minq * 0.999):
            ob = fetch_orderbook(market)
            if not ob or not ob.get("bids") or not ob.get("asks"):
                time.sleep(0.25); continue

            best_bid = float(ob["bids"][0][0])
            best_ask = float(ob["asks"][0][0])
            price    = _round_price(market, min(best_bid, best_ask*(1.0-1e-6)))

            # فلتر سلامة السعر
            if not _price_ok(market, best_bid, best_ask, price):
                time.sleep(0.2)
                continue

            # متابعة أمر قائم
            if last_order:
                st = _fetch_order(last_order)
                st_status = st.get("status")
                if st_status in ("filled","partiallyFilled"):
                    fills = st.get("fills", []) or []
                    if fills:
                        all_fills += fills
                        base, quote_eur, fee_eur = totals_from_fills(fills)
                        remaining_eur = max(0.0, remaining_eur - (quote_eur + fee_eur))
                if st_status == "filled" or remaining_eur < (minq * 0.999):
                    try: _cancel_order(last_order)
                    except: pass
                    last_order = None
                    break

                # إعادة تسعير
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
                            if st_status == "filled" or remaining_eur < (minq * 0.999):
                                try: _cancel_order(last_order)
                                except: pass
                                last_order = None
                                break
                        time.sleep(MIN_STATUS_GAP_SEC)
                    if last_order:
                        continue

            # وضع أمر جديد (throttle + backoff)
            if not last_order and remaining_eur >= (minq * 0.999):
                if time.time() - last_place_ts < MIN_PLACE_GAP_SEC:
                    time.sleep(0.05); continue

                cur_price = price
                attempt   = 0
                placed    = False

                while attempt < IB_BACKOFF_TRIES and remaining_eur >= (minq * 0.999):
                    # تحقق السعر كل مرة
                    if not _price_ok(market, best_bid, best_ask, cur_price):
                        break

                    amt_base = _calc_buy_amount_base(market, remaining_eur, cur_price)
                    if amt_base <= 0:
                        break

                    exp_quote = amt_base * cur_price
                    send_message(
                        f"🧪 محاولة شراء #{placed_count+1}: amount={_fmt_amount(market, amt_base)} | "
                        f"bid={_fmt_price(market, best_bid)} / ask={_fmt_price(market, best_ask)} | "
                        f"use={_fmt_price(market, cur_price)} | EUR≈{exp_quote:.2f}", chat_id
                    )

                    res = _place_limit_postonly(market, "buy", cur_price, amount=amt_base)
                    inner_tries += 1
                    last_place_ts = time.time()

                    orderId = (res or {}).get("orderId")
                    err_txt = str((res or {}).get("error","")).lower()

                    if orderId:
                        last_order = orderId
                        last_bid   = best_bid
                        placed     = True
                        placed_count += 1
                        break

                    if "insufficient balance" in err_txt or "not have sufficient balance" in err_txt:
                        remaining_eur = remaining_eur * IB_BACKOFF_FACTOR
                        attempt += 1
                        time.sleep(0.2)
                        continue

                    # جرّب تسعير bid مباشرة مرّة واحدة
                    if attempt == 0:
                        cur_price = _round_price(market, best_bid)
                        attempt += 1
                        continue

                    break

                if not placed:
                    time.sleep(0.2)
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
                        if st_status == "filled" or remaining_eur < (minq * 0.999):
                            try: _cancel_order(last_order)
                            except: pass
                            last_order = None
                            break
                    time.sleep(MIN_STATUS_GAP_SEC)

        if last_order:
            try: _cancel_order(last_order)
            except: pass

    except Exception as e:
        print("open_maker_buy err:", e)

    if not all_fills:
        bump_patience_on_fail(market)
        send_message(
            f"⚠️ لم يكتمل شراء Maker.\n• أوامر موضوعة: {placed_count}\n"
            f"• محاولات داخلية: {inner_tries}\n• زمن: {time.time()-started:.1f}s\n"
            "سنُعزّز الصبر تلقائيًا وسنحاول لاحقًا.", chat_id
        )
        return None

    base_amt, quote_eur, fee_eur = totals_from_fills(all_fills)
    if base_amt <= 0:
        bump_patience_on_fail(market)
        return None

    relax_patience_on_success(market)
    avg = (quote_eur + fee_eur) / base_amt
    return {"amount": base_amt, "avg": avg, "cost_eur": quote_eur + fee_eur, "fee_eur": fee_eur}

# ---------- Sell Maker ----------
def close_maker_sell(market: str, amount: float, chat_id=None):
    patience = get_patience_sec(market)
    started  = time.time()
    remaining = float(amount)
    all_fills = []
    last_order = None
    last_ask   = None
    last_place_ts = 0.0

    try:
        while (time.time() - started) < patience and remaining > 0:
            ob = fetch_orderbook(market)
            if not ob or not ob.get("bids") or not ob.get("asks"):
                time.sleep(0.25); continue

            best_bid = float(ob["bids"][0][0])
            best_ask = float(ob["asks"][0][0])
            price    = _round_price(market, max(best_ask, best_bid*(1.0+1e-6)))

            if not _price_ok(market, best_bid, best_ask, price):
                time.sleep(0.2); continue

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
                        time.sleep(MIN_STATUS_GAP_SEC)
                    if last_order:
                        continue

            if remaining > 0:
                if time.time() - last_place_ts < MIN_PLACE_GAP_SEC:
                    time.sleep(0.05); continue

                amt_to_place = _round_amount(market, remaining)
                res = _place_limit_postonly(market, "sell", price, amount=amt_to_place)
                last_place_ts = time.time()
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
                    time.sleep(MIN_STATUS_GAP_SEC)

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

# ---------- Summary ----------
def build_summary():
    lines=[]
    with lk:
        at = active_trade
        closed=[x for x in executed_trades if "exit_eur" in x]
    if at:
        cur = fetch_price_ws_first(at["symbol"]) or at["entry"]
        pnl = ((cur/at["entry"])-1.0)*100.0
        lines.append("📌 صفقة نشطة:")
        lines.append(f"• {at['symbol'].replace('-EUR','')} @ €{at['entry']:.8f} | PnL {pnl:+.2f}%")
    else:
        lines.append("📌 لا صفقات نشطة.")
    pnl_eur=sum(float(x["pnl_eur"]) for x in closed)
    wins=sum(1 for x in closed if float(x.get("pnl_eur",0))>=0)
    lines.append(f"\n📊 صفقات مكتملة: {len(closed)} | محققة: {pnl_eur:+.2f}€ | فوز/خسارة: {wins}/{len(closed)-wins}")
    lines.append("\n⚙️ buy=Maker | sell=Maker")
    return "\n".join(lines)

# ---------- Flow ----------
def do_open_maker(market: str, eur: float, chat_id=None):
    def _runner():
        global active_trade
        try:
            with lk:
                if active_trade:
                    send_message("⛔ توجد صفقة نشطة. أغلقها أولاً.", chat_id); return
            res = open_maker_buy(market, eur, chat_id)
            if not res: return
            with lk:
                active_trade = {
                    "symbol": market,
                    "entry":  float(res["avg"]),
                    "amount": float(res["amount"]),
                    "cost_eur": float(res["cost_eur"]),
                    "buy_fee_eur": float(res["fee_eur"]),
                    "opened_at": time.time(),
                }
                executed_trades.append(active_trade.copy())
            send_message(f"✅ شراء {market.replace('-EUR','')} (Maker) @ €{active_trade['entry']:.8f} | كمية {active_trade['amount']:.8f}", chat_id)
        except Exception as e:
            traceback.print_exc()
            send_message(f"🐞 خطأ أثناء الفتح: {e}", chat_id)
    Thread(target=_runner, daemon=True).start()

def do_close_maker(reason="", chat_id=None):
    global active_trade
    try:
        with lk:
            if not active_trade:
                send_message("لا توجد صفقة لإغلاقها.", chat_id); return
            m   = active_trade["symbol"]
            amt = float(active_trade["amount"])
            cost= float(active_trade["cost_eur"])
        sold_base, proceeds, sell_fee = close_maker_sell(m, amt, chat_id)
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
        send_message(f"💰 بيع {m.replace('-EUR','')} (Maker) | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%) {('— '+reason) if reason else ''}", chat_id)
    except Exception as e:
        traceback.print_exc()
        send_message(f"🐞 خطأ أثناء الإغلاق: {e}", chat_id)

# ---------- Telegram Webhook (/tg) ----------
@app.route("/tg", methods=["POST"])
def tg_webhook():
    if TG_SECRET:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_SECRET:
            return "forbidden", 403

    upd = request.get_json(silent=True) or {}
    msg = (upd.get("message") or upd.get("edited_message") or {})
    chat_id = str(((msg.get("chat") or {}).get("id")) or CHAT_ID)
    text = (msg.get("text") or "").strip()
    if not text:
        return jsonify({"ok":True})

    cmd = text.split()[0].lower()

    if cmd in ("/start","/help"):
        send_message("أوامر:\n/summary\n/enable\n/disable\n/close\n/buy COIN [EUR]\nمثال: /buy DATA 8.2", chat_id)
        return jsonify({"ok":True})

    if cmd == "/summary":
        send_message(build_summary(), chat_id); return jsonify({"ok":True})

    if cmd == "/enable":
        global enabled; enabled=True
        send_message("✅ تم التفعيل.", chat_id); return jsonify({"ok":True})

    if cmd == "/disable":
        enabled=False
        send_message("🛑 تم الإيقاف.", chat_id); return jsonify({"ok":True})

    if cmd == "/close":
        do_close_maker("Manual", chat_id); return jsonify({"ok":True})

    if cmd == "/buy":
        if not enabled:
            send_message("البوت متوقف. استخدم /enable", chat_id); return jsonify({"ok":False})
        parts = text.split()
        if len(parts) < 2:
            send_message("اكتب: /buy COIN [EUR]", chat_id); return jsonify({"ok":False})
        coin = parts[1].upper()
        eur  = float(parts[2]) if len(parts)>=3 else None
        market = coin_to_market(coin)
        if not market:
            send_message(f"⛔ {coin}-EUR غير متاح على Bitvavo.", chat_id); return jsonify({"ok":False})
        do_open_maker(market, eur, chat_id)
        return jsonify({"ok":True})

    send_message("أوامر: /summary /enable /disable /close /buy COIN [EUR]", chat_id)
    return jsonify({"ok":True})

# ---------- Signal bot hook (/hook) ----------
@app.route("/hook", methods=["POST"])
def hook():
    """
    JSON: {"cmd":"buy","coin":"DATA","eur":8.2}
    """
    try:
        data = request.get_json(silent=True) or {}
        if (data.get("cmd") or "").lower() != "buy":
            return jsonify({"ok":False,"err":"bad_cmd"})
        coin = (data.get("coin") or "").strip().upper()
        market = coin_to_market(coin)
        if not market:
            return jsonify({"ok":False,"err":"market_unavailable"})
        eur = float(data.get("eur")) if data.get("eur") is not None else None
        do_open_maker(market, eur, CHAT_ID)
        return jsonify({"ok":True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok":False,"err":str(e)}), 500

# ---------- Health ----------
@app.route("/", methods=["GET"])
def home():
    return "Saqer Maker-Only ✅"

if __name__ == "__main__" or RUN_LOCAL:
    load_markets()
    app.run(host="0.0.0.0", port=PORT)