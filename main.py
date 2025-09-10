# -*- coding: utf-8 -*-
"""
Saqer X — Maker-Only Relay (Bitvavo/EUR)
- شراء/بيع Maker (postOnly) فقط، مع تجميع partial fills
- تسعير دقيق بحسب orderbook + دقة السوق (tick/step)
- تباطؤ ذكي للمحاولات + backoff لخطأ 216
- تلخيص محاولات حقيقية فقط (لا spam)، وإلغاء أي أوامر عالقة عند الفشل
- صفقة واحدة في الوقت نفسه + وقف ديناميكي متدرّج (اختياري)
"""

import os, re, time, json, math, random, traceback, hmac, hashlib
import requests, websocket
from threading import Thread, Lock
from uuid import uuid4
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ================= Boot / ENV =================
load_dotenv()
app = Flask(__name__)

BOT_TOKEN  = os.getenv("BOT_TOKEN", "")
CHAT_ID    = os.getenv("CHAT_ID", "")
API_KEY    = os.getenv("BITVAVO_API_KEY", "")
API_SECRET = os.getenv("BITVAVO_API_SECRET", "")
RUN_LOCAL  = os.getenv("RUN_LOCAL","0") == "1"
PORT       = int(os.getenv("PORT","5000"))

BASE_URL = "https://api.bitvavo.com/v2"
WS_URL   = "wss://ws.bitvavo.com/v2/"

# ================= Settings =================
# حماية الرصيد
EST_FEE_RATE        = float(os.getenv("FEE_RATE_EST", "0.0025"))   # ~0.25%
HEADROOM_EUR_MIN    = float(os.getenv("HEADROOM_EUR_MIN", "0.50")) # ≥0.50€
MAX_SPEND_FRACTION  = float(os.getenv("MAX_SPEND_FRACTION","0.90"))
FIXED_EUR_PER_TRADE = float(os.getenv("FIXED_EUR","0"))            # 0=off

BUY_MIN_EUR         = 5.0  # متطلبات Bitvavo الدنيا عادةً

# صبر الـMaker
MAKER_WAIT_BASE_SEC = int(os.getenv("MAKER_WAIT_BASE_SEC","60"))
MAKER_WAIT_MAX_SEC  = int(os.getenv("MAKER_WAIT_MAX_SEC","300"))
MAKER_WAIT_STEP_UP  = int(os.getenv("MAKER_WAIT_STEP_UP","20"))
MAKER_WAIT_STEP_DOWN= int(os.getenv("MAKER_WAIT_STEP_DOWN","10"))

# إعادة التسعير والمتابعة
MAKER_REPRICE_EVERY  = float(os.getenv("MAKER_REPRICE_EVERY","2.0"))
MAKER_REPRICE_THRESH = float(os.getenv("MAKER_REPRICE_THRESH","0.0005"))  # 0.05%
POLL_INTERVAL        = 0.35

# Throttle للمحاولات والرسائل
PLACE_THROTTLE_SEC    = float(os.getenv("PLACE_THROTTLE_SEC","2.5"))
ATTEMPT_COOLDOWN_FAIL = float(os.getenv("ATTEMPT_COOLDOWN_FAIL","1.2"))
ANNOUNCE_TRY_FIRST    = int(os.getenv("ANNOUNCE_TRY_FIRST","1"))   # لا تغيّر منطقيًا
ANNOUNCE_EVERY_SEC    = float(os.getenv("ANNOUNCE_EVERY_SEC","5.0"))

# Backoff لخطأ 216
IB_BACKOFF_FACTOR   = float(os.getenv("IB_BACKOFF_FACTOR","0.96"))
IB_BACKOFF_TRIES    = int(os.getenv("IB_BACKOFF_TRIES","5"))

# وقف ديناميكي (اختياري)
STOP_LADDER = [(0.0,-2.0),(1.0,-1.0),(2.0,0.0),(3.0,1.0),(4.0,2.0),(5.0,3.0)]

# ================= Runtime =================
lk = Lock()
active_trade = None
executed_trades = []

MARKET_MAP  = {}   # "ADA" -> "ADA-EUR"
MARKET_META = {}   # "ADA-EUR" -> {"minQuote","minBase","tick","step"}

_ws_prices = {}
_ws_lock   = Lock()
_last_try_announce_ts = 0.0

# ================= Utils =================
def tg(text: str):
    try:
        if BOT_TOKEN and CHAT_ID:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": text}, timeout=8)
        else:
            print("TG:", text)
    except Exception as e:
        print("TG err:", e)

def create_sig(ts, method, path, body_str=""):
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
        bals = bv_request("GET","/balance")
        if isinstance(bals,list):
            for b in bals:
                if b.get("symbol")=="EUR":
                    return max(0.0, float(b.get("available",0) or 0))
    except Exception: pass
    return 0.0

# ================= Markets / Precision =================
def load_markets():
    global MARKET_MAP, MARKET_META
    try:
        rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
        m, meta = {}, {}
        for r0 in rows:
            base  = r0.get("base"); quote = r0.get("quote"); market = r0.get("market")
            if base and quote=="EUR":
                price_prec = float(r0.get("pricePrecision",1e-6) or 1e-6)
                amt_prec   = float(r0.get("amountPrecision",1e-8) or 1e-8)
                m[base.upper()] = market
                meta[market] = {
                    "minQuote": float(r0.get("minOrderInQuoteAsset", BUY_MIN_EUR) or BUY_MIN_EUR),
                    "minBase":  float(r0.get("minOrderInBaseAsset",  0) or 0.0),
                    "tick":     price_prec,
                    "step":     amt_prec,
                }
        if m: MARKET_MAP = m
        if meta: MARKET_META = meta
    except Exception as e:
        print("load_markets err:", e)

def coin_to_market(coin:str):
    if not MARKET_MAP: load_markets()
    return MARKET_MAP.get(coin.upper())

def _decimals(step: float) -> int:
    try:
        if step >= 1: return 0
        return max(0, int(round(-math.log10(step))))
    except: return 8

def _round_price(market, price):
    tick = (MARKET_META.get(market, {}) or {}).get("tick", 1e-6)
    decs = _decimals(tick)
    p = round(float(price), decs)
    return max(tick, p)

def _round_amount(market, amount):
    step = (MARKET_META.get(market, {}) or {}).get("step", 1e-8)
    floored = math.floor(float(amount)/step)*step
    return round(max(step, floored), _decimals(step))

def _fmt_price(market, price):  return f"{_round_price(market, price):.{_decimals((MARKET_META.get(market,{}) or {}).get('tick',1e-6))}f}"
def _fmt_amount(market, amt):   return f"{_round_amount(market, amt):.{_decimals((MARKET_META.get(market,{}) or {}).get('step',1e-8))}f}"
def _min_quote(market):         return (MARKET_META.get(market,{}) or {}).get("minQuote", BUY_MIN_EUR)
def _min_base(market):          return (MARKET_META.get(market,{}) or {}).get("minBase", 0.0)

# ================= WS Prices =================
def _ws_on_message(ws, msg):
    try: data = json.loads(msg)
    except: return
    if isinstance(data, dict) and data.get("event")=="ticker":
        m = data.get("market")
        price = data.get("price") or data.get("lastPrice") or data.get("open")
        try:
            p = float(price)
            if p>0:
                with _ws_lock:
                    _ws_prices[m]={"price":p,"ts":time.time()}
        except: pass

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
        payload={"action":"subscribe","channels":[{"name":"ticker","markets":markets}]}
        w = websocket.create_connection(WS_URL, timeout=5)
        w.send(json.dumps(payload)); w.close()
    except: pass

def fetch_price_ws_first(market, staleness=2.0):
    now=time.time()
    with _ws_lock:
        rec=_ws_prices.get(market)
    if rec and (now-rec["ts"])<=staleness:
        return rec["price"]
    ws_sub([market])
    try:
        j = requests.get(f"{BASE_URL}/ticker/price?market={market}", timeout=6).json()
        p = float(j.get("price",0) or 0)
        if p>0:
            with _ws_lock:
                _ws_prices[market]={"price":p,"ts":now}
            return p
    except: pass
    return None

def fetch_orderbook(market):
    try:
        j = requests.get(f"{BASE_URL}/{market}/book", timeout=6).json()
        if j and j.get("bids") and j.get("asks"): return j
    except: pass
    return None

# ================= Bitvavo Orders (Maker Only) =================
def _place_limit_postonly(market, side, price, amount):
    if not market: return {"error":"market_required"}
    body = {
        "market": market,
        "side": side,
        "orderType": "limit",
        "postOnly": True,
        "clientOrderId": str(uuid4()),
        "price": _fmt_price(market, price),
        "amount": _fmt_amount(market, amount),
        "operatorId": ""  # مطلوب من Bitvavo حتى لو فاضي
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

# ================= Helpers =================
def announce_try(text:str):
    global _last_try_announce_ts
    now=time.time()
    if _last_try_announce_ts==0.0:
        tg(text); _last_try_announce_ts=now; return
    if now-_last_try_announce_ts >= ANNOUNCE_EVERY_SEC:
        tg(text); _last_try_announce_ts=now

def _calc_base_from_eur(market:str, eur:float, price:float)->float:
    price=max(1e-12,float(price))
    base=float(eur)/price
    base=max(base,_min_base(market))
    return _round_amount(market, base)

def patience_key(market): return f"maker:patience:{market}"
_pmem = {}
def get_patience_sec(market):
    v=_pmem.get(patience_key(market))
    if v is None: return MAKER_WAIT_BASE_SEC
    return min(MAKER_WAIT_MAX_SEC, max(MAKER_WAIT_BASE_SEC, int(v)))
def bump_patience_on_fail(market):
    cur=get_patience_sec(market); _pmem[patience_key(market)]=min(MAKER_WAIT_MAX_SEC, cur+MAKER_WAIT_STEP_UP)
def relax_patience_on_success(market):
    cur=get_patience_sec(market); _pmem[patience_key(market)]=max(MAKER_WAIT_BASE_SEC, cur-MAKER_WAIT_STEP_DOWN)

# ================= Maker BUY =================
def open_maker_buy(market: str, eur_amount: float):
    # -------- (1) Budget --------
    eur_avail = get_eur_available()
    target = FIXED_EUR_PER_TRADE if FIXED_EUR_PER_TRADE>0 else (eur_amount if eur_amount and eur_amount>0 else eur_avail)
    target = min(target, eur_avail * MAX_SPEND_FRACTION)

    minq = _min_quote(market)
    buffer_eur = max(HEADROOM_EUR_MIN, target*EST_FEE_RATE*2.0, 0.05)
    spendable = min(target, max(0.0, eur_avail - buffer_eur))

    if spendable < max(minq, BUY_MIN_EUR):
        need = max(minq, BUY_MIN_EUR)
        tg(f"⛔ الرصيد غير كافٍ: متاح {eur_avail:.2f}€ | بعد الهامش {spendable:.2f}€ "
           f"| هامش {buffer_eur:.2f}€ | المطلوب ≥ {need:.2f}€.")
        return None

    tg(f"💰 EUR متاح: {eur_avail:.2f}€ | سننفق: {spendable:.2f}€ (هامش {buffer_eur:.2f}€ | هدف {target:.2f}€)")

    # -------- (2) Execution --------
    patience       = get_patience_sec(market)
    started        = time.time()
    last_order     = None
    last_bid       = None
    all_fills      = []
    remaining_eur  = float(spendable)
    inner_attempts = 0
    placed_orders  = 0
    last_place_ts  = 0.0

    try:
        while (time.time()-started) < patience and remaining_eur >= (minq*0.999):
            ob = fetch_orderbook(market)
            if not ob or not ob.get("bids") or not ob.get("asks"):
                time.sleep(0.25); continue

            best_bid = float(ob["bids"][0][0])
            best_ask = float(ob["asks"][0][0])
            # السعر الدقيق: أقرب للـ bid ويضمن maker
            price    = _round_price(market, min(best_bid, best_ask*(1.0-1e-6)))

            # --- متابعة أمر قائم ---
            if last_order:
                st=_fetch_order(last_order); st_status=st.get("status")
                if st_status in ("filled","partiallyFilled"):
                    fills=st.get("fills",[]) or []
                    if fills:
                        all_fills+=fills
                        base, quote_eur, fee_eur = totals_from_fills(fills)
                        remaining_eur = max(0.0, remaining_eur - (quote_eur + fee_eur))
                if st_status=="filled" or remaining_eur < (minq*0.999):
                    try: _cancel_order(last_order)
                    except: pass
                    last_order=None
                    break

                # إعادة تسعير إذا تغيّر bid بوضوح
                if (last_bid is None) or (abs(best_bid/last_bid - 1.0) >= MAKER_REPRICE_THRESH):
                    try: _cancel_order(last_order)
                    except: pass
                    last_order=None
                else:
                    t0=time.time()
                    while time.time()-t0 < MAKER_REPRICE_EVERY:
                        st=_fetch_order(last_order); st_status=st.get("status")
                        if st_status in ("filled","partiallyFilled"):
                            fills=st.get("fills",[]) or []
                            if fills:
                                all_fills+=fills
                                base, quote_eur, fee_eur = totals_from_fills(fills)
                                remaining_eur = max(0.0, remaining_eur - (quote_eur + fee_eur))
                            if st_status=="filled" or remaining_eur < (minq*0.999):
                                try: _cancel_order(last_order)
                                except: pass
                                last_order=None
                                break
                        time.sleep(0.35)
                    if last_order:  # ما زال قائمًا
                        continue

            # --- وضع أمر جديد (Throttle + Backoff 216) ---
            if not last_order and remaining_eur >= (minq*0.999):
                attempt   = 0
                placed    = False
                cur_price = price

                while attempt < IB_BACKOFF_TRIES and remaining_eur >= (minq*0.999):
                    # Throttle قبل كل محاولة (حتى لو السابقة فشلت)
                    wait_left = max(0.0, PLACE_THROTTLE_SEC - (time.time()-last_place_ts))
                    if wait_left > 0: time.sleep(wait_left)

                    inner_attempts += 1
                    amt_base = _calc_base_from_eur(market, remaining_eur, cur_price)
                    if amt_base <= 0: break

                    exp_eur = amt_base * cur_price
                    if attempt == 0:
                        announce_try(f"🧪 محاولة شراء #1: amount={_fmt_amount(market, amt_base)} | "
                                     f"سعر≈{_fmt_price(market, cur_price)} | EUR≈{exp_eur:.2f}")
                    else:
                        announce_try(f"🧪 محاولات مستمرة… EUR≈{exp_eur:.2f}")

                    last_place_ts = time.time()
                    res     = _place_limit_postonly(market, "buy", cur_price, amt_base)
                    orderId = (res or {}).get("orderId")
                    err_txt = str((res or {}).get("error","")).lower()

                    if orderId:
                        last_order = orderId
                        last_bid   = best_bid
                        placed     = True
                        placed_orders += 1
                        break

                    # Backoff لخطأ الرصيد
                    if ("insufficient balance" in err_txt or
                        "not have sufficient balance" in err_txt or
                        "code': 216" in err_txt):
                        remaining_eur *= IB_BACKOFF_FACTOR
                        attempt += 1
                        time.sleep(ATTEMPT_COOLDOWN_FAIL + random.uniform(0,0.3))
                        continue

                    # أخطاء أخرى—اخرج بهدوء
                    attempt = IB_BACKOFF_TRIES
                    break

                if not placed:
                    time.sleep(max(0.35, ATTEMPT_COOLDOWN_FAIL))
                    continue

                # متابعة قصيرة للأمر الجديد
                t0=time.time()
                while time.time()-t0 < MAKER_REPRICE_EVERY:
                    st=_fetch_order(last_order); st_status=st.get("status")
                    if st_status in ("filled","partiallyFilled"):
                        fills=st.get("fills",[]) or []
                        if fills:
                            all_fills+=fills
                            base, quote_eur, fee_eur = totals_from_fills(fills)
                            remaining_eur = max(0.0, remaining_eur - (quote_eur + fee_eur))
                        if st_status=="filled" or remaining_eur < (minq*0.999):
                            try: _cancel_order(last_order)
                            except: pass
                            last_order=None
                            break
                    time.sleep(0.35)

        # أمان: إلغاء أي أمر بقي
        if last_order:
            try: _cancel_order(last_order)
            except: pass

    except Exception as e:
        print("open_maker_buy err:", e)

    # تقرير نهائي
    if not all_fills:
        bump_patience_on_fail(market)
        tg("⚠️ لم يكتمل شراء Maker.\n"
           f"• أوامر موضوعة: {placed_orders}\n"
           f"• محاولات داخلية: {inner_attempts}\n"
           f"• زمن: {time.time()-started:.1f}s\n"
           "سنرفع الصبر تلقائيًا وسنحاول لاحقًا.")
        return None

    base_amt, quote_eur, fee_eur = totals_from_fills(all_fills)
    if base_amt <= 0:
        bump_patience_on_fail(market); return None

    relax_patience_on_success(market)
    avg = (quote_eur + fee_eur) / base_amt
    return {"amount":base_amt, "avg":avg, "cost_eur":quote_eur+fee_eur, "fee_eur":fee_eur}

# ================= Maker SELL =================
def close_maker_sell(market:str, amount:float):
    patience = get_patience_sec(market)
    started  = time.time()
    remaining = float(amount)
    all_fills = []
    last_order=None; last_ask=None; last_place_ts=0.0

    try:
        while (time.time()-started) < patience and remaining > 0:
            ob = fetch_orderbook(market)
            if not ob or not ob.get("bids") or not ob.get("asks"):
                time.sleep(0.25); continue
            best_bid=float(ob["bids"][0][0]); best_ask=float(ob["asks"][0][0])
            price=_round_price(market, max(best_ask, best_bid*(1.0+1e-6)))

            if last_order:
                st=_fetch_order(last_order); st_status=st.get("status")
                if st_status in ("filled","partiallyFilled"):
                    fills=st.get("fills",[]) or []
                    if fills:
                        sold_base, _, _ = totals_from_fills(fills)
                        remaining = max(0.0, remaining - sold_base)
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
                            fills=st.get("fills",[]) or []
                            if fills:
                                sold_base, _, _ = totals_from_fills(fills)
                                remaining = max(0.0, remaining - sold_base)
                                all_fills += fills
                            if st_status=="filled" or remaining<=0:
                                try: _cancel_order(last_order)
                                except: pass
                                last_order=None
                                break
                        time.sleep(0.35)
                    if last_order: continue

            if remaining>0:
                # throttle لطيف أيضًا
                wait_left = max(0.0, PLACE_THROTTLE_SEC - (time.time()-last_place_ts))
                if wait_left>0: time.sleep(wait_left)
                amt_to_place = _round_amount(market, remaining)
                res=_place_limit_postonly(market,"sell",price,amt_to_place)
                orderId=res.get("orderId")
                last_place_ts=time.time()
                if not orderId:
                    price2=_round_price(market,best_ask)
                    res=_place_limit_postonly(market,"sell",price2,amt_to_place)
                    orderId=res.get("orderId")
                    last_place_ts=time.time()
                if not orderId:
                    time.sleep(0.4); continue
                last_order=orderId; last_ask=best_ask

                t0=time.time()
                while time.time()-t0 < MAKER_REPRICE_EVERY:
                    st=_fetch_order(last_order); st_status=st.get("status")
                    if st_status in ("filled","partiallyFilled"):
                        fills=st.get("fills",[]) or []
                        if fills:
                            sold_base, _, _ = totals_from_fills(fills)
                            remaining = max(0.0, remaining - sold_base)
                            all_fills += fills
                        if st_status=="filled" or remaining<=0:
                            try: _cancel_order(last_order)
                            except: pass
                            last_order=None
                            break
                    time.sleep(0.35)

        if last_order:
            try: _cancel_order(last_order)
            except: pass

    except Exception as e:
        print("close_maker_sell err:", e)

    proceeds_eur=0.0; fee_eur=0.0; sold_base=0.0
    for f in all_fills:
        amt=float(f["amount"]); price=float(f["price"]); fe=float(f.get("fee",0) or 0)
        sold_base+=amt; proceeds_eur+=amt*price; fee_eur+=fe
    proceeds_eur -= fee_eur
    return sold_base, proceeds_eur, fee_eur

# ================= Trade Flow + Stop Ladder =================
def current_stop_from_peak(peak_pct: float) -> float:
    stop=-999.0
    for th,val in STOP_LADDER:
        if peak_pct>=th: stop=val
    return stop

def do_open_maker(market:str, eur:float):
    def _runner():
        global active_trade
        try:
            with lk:
                if active_trade:
                    tg("⛔ توجد صفقة نشطة. أغلقها أولاً."); return
            res = open_maker_buy(market, eur)
            if not res:
                tg("⏳ لم يكتمل شراء Maker. سنحاول لاحقًا (الصبر يتكيف تلقائيًا)."); return
            with lk:
                active_trade = {
                    "symbol": market,
                    "entry": float(res["avg"]),
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
            traceback.print_exc(); tg(f"🐞 خطأ أثناء الفتح: {e}")
    Thread(target=_runner, daemon=True).start()

def do_close_maker(reason=""):
    global active_trade
    try:
        with lk:
            if not active_trade: return
            m=active_trade["symbol"]; amt=float(active_trade["amount"]); cost=float(active_trade["cost_eur"])
        sold_base, proceeds, sell_fee = close_maker_sell(m, amt)
        with lk:
            pnl_eur = proceeds - cost
            pnl_pct = (proceeds/cost - 1.0)*100.0 if cost>0 else 0.0
            for t in reversed(executed_trades):
                if t["symbol"]==m and "exit_eur" not in t:
                    t.update({"exit_eur":proceeds,"sell_fee_eur":sell_fee,
                              "pnl_eur":pnl_eur,"pnl_pct":pnl_pct,"exit_time":time.time()})
                    break
            active_trade=None
        tg(f"💰 بيع {m.replace('-EUR','')} (Maker) | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%) "
           f"{('— '+reason) if reason else ''}")
    except Exception as e:
        traceback.print_exc(); tg(f"🐞 خطأ أثناء الإغلاق: {e}")

def build_summary():
    lines=[]
    with lk:
        at=active_trade
        closed=[x for x in executed_trades if "exit_eur" in x]
    if at:
        cur = fetch_price_ws_first(at["symbol"]) or at["entry"]
        pnl = ((cur/at["entry"])-1.0)*100.0
        lines.append("📌 صفقة نشطة:")
        lines.append(f"• {at['symbol'].replace('-EUR','')} @ €{at['entry']:.8f} | "
                     f"PnL {pnl:+.2f}% | Peak {at['peak_pct']:.2f}% | "
                     f"SL {at.get('dyn_stop_pct',-2.0):+.2f}%")
    else:
        lines.append("📌 لا صفقات نشطة.")
    pnl_eur=sum(float(x.get("pnl_eur",0)) for x in closed)
    wins=sum(1 for x in closed if float(x.get("pnl_eur",0))>=0)
    lines.append(f"\n📊 صفقات مكتملة: {len(closed)} | محققة: {pnl_eur:+.2f}€ | فوز/خسارة: {wins}/{len(closed)-wins}")
    lines.append("\n⚙️ buy=Maker | sell=Maker | سلم الوقف: -2%→-1%→0%→+1%…")
    return "\n".join(lines)

# ================= Webhook =================
@app.route("/hook", methods=["POST"])
def hook():
    """
    JSON:
      {"cmd":"buy","coin":"DATA","eur":8.5}
      {"cmd":"close"} / {"cmd":"summary"} / {"cmd":"enable"} / {"cmd":"disable"}
    """
    try:
        data = request.get_json(silent=True) or {}
        cmd  = (data.get("cmd") or "").strip().lower()

        if cmd in ("summary","/summary"):
            txt=build_summary(); tg(txt); return jsonify({"ok":True,"summary":txt})
        if cmd in ("enable","start","on"): tg("✅ تم التفعيل."); return jsonify({"ok":True})
        if cmd in ("disable","stop","off"): tg("🛑 تم الإيقاف."); return jsonify({"ok":True})

        if cmd in ("close","sell","exit"):
            with lk:
                has = active_trade is not None
            if has:
                do_close_maker("Manual"); return jsonify({"ok":True,"msg":"closing"})
            tg("لا توجد صفقة لإغلاقها."); return jsonify({"ok":False,"err":"no_active_trade"})

        if cmd=="buy":
            coin=(data.get("coin") or "").strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{2,15}", coin or ""):
                return jsonify({"ok":False,"err":"bad_coin"})
            market=coin_to_market(coin)
            if not market:
                tg(f"⛔ {coin}-EUR غير متاح على Bitvavo."); return jsonify({"ok":False,"err":"market_unavailable"})
            eur=float(data.get("eur")) if data.get("eur") is not None else None
            do_open_maker(market, eur); return jsonify({"ok":True,"market":market})

        return jsonify({"ok":False,"err":"bad_cmd"})
    except Exception as e:
        traceback.print_exc(); return jsonify({"ok":False,"err":str(e)}), 500

# ================= Health =================
@app.route("/", methods=["GET"])
def home(): return "Maker-Only Relay ✅"

@app.route("/summary", methods=["GET"])
def http_summary():
    return f"<pre>{build_summary()}</pre>"

# ================= Main =================
if __name__ == "__main__" or RUN_LOCAL:
    load_markets()
    app.run(host="0.0.0.0", port=PORT)