# -*- coding: utf-8 -*-
"""
Saqer — Maker-Only Relay Executor (Bitvavo / EUR)
- تلغرام: /webhook (بدون /buy). أوامر: /summary /enable /disable /close
- إشارات الشراء فقط من /hook {"cmd":"buy","coin":"ADA","eur":<اختياري>}
- شراء/بيع Maker-Only (postOnly) مع تجميع partial fills
- لا "5€" ثابتة إطلاقاً — الشراء بكامل الرصيد (مع هامش رسوم فقط)
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

BOT_TOKEN   = os.getenv("BOT_TOKEN","").strip()
CHAT_ID     = os.getenv("CHAT_ID","").strip()  # إن تركته فاضي يرد لأي شات
API_KEY     = os.getenv("BITVAVO_API_KEY","").strip()
API_SECRET  = os.getenv("BITVAVO_API_SECRET","").strip()
REDIS_URL   = os.getenv("REDIS_URL")
RUN_LOCAL   = os.getenv("RUN_LOCAL","0") == "1"
PORT        = int(os.getenv("PORT","5000"))

# حماية الرصيد (قابلة للضبط)
FEE_EST            = float(os.getenv("FEE_RATE_EST","0.0025"))   # ≈0.25%
HEADROOM_EUR_MIN   = float(os.getenv("HEADROOM_EUR_MIN","0.00")) # 0.00 مسموح
MAX_SPEND_FRACTION = float(os.getenv("MAX_SPEND_FRACTION","1.00")) # 100%
FIXED_EUR          = float(os.getenv("FIXED_EUR","0"))           # 0=off

# Backoff لخطأ الرصيد 216
IB_BACKOFF_FACTOR  = float(os.getenv("IB_BACKOFF_FACTOR","0.96")) # -4% كل فشل
IB_BACKOFF_TRIES   = int(os.getenv("IB_BACKOFF_TRIES","6"))
SLEEP_BETWEEN_ATTEMPTS = float(os.getenv("SLEEP_BETWEEN_ATTEMPTS","0.8"))

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
POLL_INTERVAL         = 0.35

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
    try:
        if BOT_TOKEN and (CHAT_ID or True):
            target = CHAT_ID or None
            # إذا CHAT_ID فاضي، ما منحدد (يرسل لاحقاً حسب webhook reply فقط)
            if target:
                requests.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": target, "text": text},
                    timeout=8
                )
            else:
                print("TG:", text)
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
        'Bitvavo-Access-Window': '60000',
        'Content-Type': 'application/json'
    }
    try:
        resp = requests.request(method, url, headers=headers,
                                json=(body if method!="GET" else None),
                                timeout=timeout)
        j = resp.json() if resp.content else {}
        if isinstance(j, dict) and j.get("error"):
            print("Bitvavo error:", j)
        return j
    except Exception as e:
        print("bv_request err:", e)
        return {"error":"request_failed"}

def get_eur_available() -> float:
    try:
        bals = bv_request("GET","/balance")
        if isinstance(bals, list):
            for b in bals:
                if b.get("symbol")=="EUR":
                    return max(0.0, float(b.get("available",0) or 0))
    except Exception: pass
    return 0.0

def get_asset_available(symbol: str) -> float:
    try:
        bals = bv_request("GET","/balance")
        if isinstance(bals, list):
            for b in bals:
                if b.get("symbol")==symbol.upper():
                    return max(0.0, float(b.get("available",0) or 0))
    except Exception: pass
    return 0.0

# ========= Markets / Meta =========
def _as_float(x, default=0.0):
    try: return float(x)
    except: return default

def _parse_step(val, default_step):
    try:
        if isinstance(val,int) or (isinstance(val,str) and val.isdigit()):
            d=int(val); 
            if 0<=d<=20: return 10.0**(-d)
        v=float(val)
        if 0 < v < 1: return v
    except: pass
    return default_step

def _decimals_from_step(step: float) -> int:
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
            base=r0.get("base"); quote=r0.get("quote"); market=r0.get("market")
            if base and quote=="EUR" and market:
                tick=_parse_step(r0.get("pricePrecision",6),1e-6)
                step=_parse_step(r0.get("amountPrecision",8),1e-8)
                m[base.upper()] = market
                meta[market] = {
                    # بدون أي default = 5.0
                    "minQuote": _as_float(r0.get("minOrderInQuoteAsset", 0.0), 0.0),
                    "minBase":  _as_float(r0.get("minOrderInBaseAsset",  0.0), 0.0),
                    "tick": tick,
                    "step": step,
                }
        if m: MARKET_MAP=m
        if meta: MARKET_META=meta
    except Exception as e:
        print("load_markets err:", e)

def coin_to_market(coin: str):
    if not MARKET_MAP:
        load_markets()
    return MARKET_MAP.get((coin or "").upper())

def _round_price(market, price):
    tick = (MARKET_META.get(market, {}) or {}).get("tick", 1e-6)
    decs = _decimals_from_step(tick)
    p = math.floor(float(price)/tick)*tick
    return round(max(tick, p), decs)

def _round_amount(market, amount):
    step = (MARKET_META.get(market, {}) or {}).get("step", 1e-8)
    a = math.floor(float(amount)/step)*step
    decs = _decimals_from_step(step)
    return round(max(step, a), decs)

def _format_price(market, price): 
    tick = (MARKET_META.get(market, {}) or {}).get("tick", 1e-6)
    decs=_decimals_from_step(tick); return f"{_round_price(market, price):.{decs}f}"

def _format_amount(market, amount):
    step=(MARKET_META.get(market,{}) or {}).get("step",1e-8)
    decs=_decimals_from_step(step); return f"{_round_amount(market, amount):.{decs}f}"

def _min_quote(market): return (MARKET_META.get(market,{}) or {}).get("minQuote", 0.0)
def _min_base(market):  return (MARKET_META.get(market,{}) or {}).get("minBase", 0.0)

def _min_required_eur(market: str, price: float) -> float:
    return max(_min_quote(market), _min_base(market)*float(price))

# ========= WS Prices =========
def _ws_on_message(ws, msg):
    try:
        data=json.loads(msg)
        if isinstance(data,dict) and data.get("event")=="ticker":
            m=data.get("market"); price=data.get("price") or data.get("lastPrice") or data.get("open")
            p=float(price or 0)
            if p>0:
                with _ws_lock: _ws_prices[m]={"price":p,"ts":time.time()}
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
        ws = websocket.create_connection(WS_URL, timeout=5)
        ws.send(json.dumps(payload)); ws.close()
    except: pass

def fetch_price_ws_first(market: str, staleness=WS_STALENESS_SEC):
    now=time.time()
    with _ws_lock:
        rec=_ws_prices.get(market)
    if rec and (now-rec["ts"])<=staleness:
        return rec["price"]
    ws_sub([market])
    try:
        j=requests.get(f"{BASE_URL}/ticker/price?market={market}", timeout=6).json()
        p=float(j.get("price",0) or 0)
        if p>0:
            with _ws_lock: _ws_prices[market]={"price":p,"ts":now}
            return p
    except: pass
    return None

def fetch_orderbook(market):
    try:
        j=requests.get(f"{BASE_URL}/{market}/book", timeout=6).json()
        if j and j.get("bids") and j.get("asks"): return j
    except: pass
    return None

# ========= Bitvavo orders =========
def _place_limit_postonly(market, side, price, amount):
    if not market or float(amount)<=0:
        return {"error":"bad_params"}
    body={
        "market":market,"side":side,"orderType":"limit","postOnly":True,
        "clientOrderId":str(uuid4()), "operatorId":"",
        "price": _format_price(market, price),
        "amount": _format_amount(market, float(amount)),
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

# ========= Patience (Redis) =========
def _pat_key(market): return f"maker:patience:{market}"
def get_patience_sec(market):
    try:
        v=r.get(_pat_key(market))
        if v is not None:
            return min(MAKER_WAIT_MAX_SEC, max(MAKER_WAIT_BASE_SEC, int(v)))
    except: pass
    return MAKER_WAIT_BASE_SEC
def bump_patience_on_fail(market):
    try: r.set(_pat_key(market), min(MAKER_WAIT_MAX_SEC, get_patience_sec(market)+MAKER_WAIT_STEP_UP))
    except: pass
def relax_patience_on_success(market):
    try: r.set(_pat_key(market), max(MAKER_WAIT_BASE_SEC, get_patience_sec(market)-MAKER_WAIT_STEP_DOWN))
    except: pass

# ========= Maker Buy =========
def _calc_buy_amount_base(market: str, eur: float, price: float) -> float:
    price=max(1e-12, float(price))
    base_amt=float(eur)/price
    # احترام minBase إن وُجد
    base_amt=max(base_amt, _min_base(market))
    return _round_amount(market, base_amt)

def open_maker_buy(market: str, eur_amount: float):
    """شراء Maker بكل الرصيد (لا أي حد 5€) + backoff + تبطيء محاولات + تنظيف."""
    eur_avail = get_eur_available()

    # نستخدم كل الرصيد (أو FIXED_EUR إن محدد)
    if FIXED_EUR>0: target_eur=FIXED_EUR
    elif eur_amount and eur_amount>0: target_eur=float(eur_amount)
    else: target_eur=float(eur_avail)

    target_eur = min(target_eur, eur_avail*MAX_SPEND_FRACTION)

    # هامش بسيط للرسوم والانزلاق
    buffer = max(HEADROOM_EUR_MIN, target_eur*FEE_EST*1.5)
    spendable = min(target_eur, max(0.0, eur_avail - buffer))
    if spendable <= 0:
        send_message(f"⛔ الرصيد بعد الهامش غير كافٍ. متاح {eur_avail:.2f}€ | هامش {buffer:.2f}€.")
        return None

    send_message(f"💰 متاح: €{eur_avail:.2f} | سننفق: €{spendable:.2f} (هامش €{buffer:.2f})")

    patience = get_patience_sec(market)
    started  = time.time()
    last_order=None; last_bid=None
    all_fills=[]; remaining_eur=float(spendable)

    try:
        while (time.time()-started)<patience and remaining_eur>0:
            ob=fetch_orderbook(market)
            if not ob: time.sleep(0.25); continue

            best_bid=float(ob["bids"][0][0])
            best_ask=float(ob["asks"][0][0])
            price=_round_price(market, min(best_bid, best_ask*(1.0-1e-6)))

            # شرط أقل تكلفة حقيقية لهذا السوق
            min_needed = _min_required_eur(market, price)
            if remaining_eur + 1e-9 < min_needed:
                send_message(f"⛔ يتطلب السوق ≥ €{min_needed:.2f} (minBase={_min_base(market)} @ { _format_price(market, price) }) | المتاح €{remaining_eur:.2f}.")
                break

            # لو في أمر قائم: راقبه/أعد تسعيره
            if last_order:
                st=_fetch_order(last_order); st_status=st.get("status")
                if st_status in ("filled","partiallyFilled"):
                    fills=st.get("fills",[]) or []
                    if fills:
                        all_fills+=fills
                        _, q_eur, fee = totals_from_fills(fills)
                        remaining_eur=max(0.0, remaining_eur-(q_eur+fee))
                if st_status=="filled" or remaining_eur<=0:
                    try: _cancel_order(last_order)
                    except: pass
                    last_order=None
                    break

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
                                _, q_eur, fee = totals_from_fills(fills)
                                remaining_eur=max(0.0, remaining_eur-(q_eur+fee))
                            if st_status=="filled" or remaining_eur<=0:
                                try: _cancel_order(last_order)
                                except: pass
                                last_order=None
                                break
                        time.sleep(POLL_INTERVAL)
                    if last_order: 
                        continue

            # ضع أمر جديد (محاولات حقيقية فقط، مع تهدئة)
            if not last_order and remaining_eur>0:
                attempt=0
                while attempt<IB_BACKOFF_TRIES and remaining_eur>0:
                    amt=_calc_buy_amount_base(market, remaining_eur, price)
                    if amt<=0: break
                    exp_eur=amt*price
                    send_message(f"🧪 محاولة شراء: amount={_format_amount(market,amt)} | سعر≈{_format_price(market,price)} | EUR≈{exp_eur:.2f}")

                    res=_place_limit_postonly(market,"buy",price,amt)
                    oid=(res or {}).get("orderId")
                    err=str((res or {}).get("error","")).lower()

                    if oid:
                        last_order=oid; last_bid=best_bid
                        break

                    if "insufficient balance" in err or "not have sufficient balance" in err:
                        remaining_eur *= IB_BACKOFF_FACTOR
                        attempt += 1
                        time.sleep(SLEEP_BETWEEN_ATTEMPTS)
                        continue

                    # أخطاء أخرى: انتقل للدورة التالية بعد تهدئة
                    attempt = IB_BACKOFF_TRIES
                    break

                if not last_order:
                    time.sleep(SLEEP_BETWEEN_ATTEMPTS)
                    continue

                # متابعة قصيرة للأمر الجديد
                t0=time.time()
                while time.time()-t0 < MAKER_REPRICE_EVERY:
                    st=_fetch_order(last_order); st_status=st.get("status")
                    if st_status in ("filled","partiallyFilled"):
                        fills=st.get("fills",[]) or []
                        if fills:
                            all_fills+=fills
                            _, q_eur, fee = totals_from_fills(fills)
                            remaining_eur=max(0.0, remaining_eur-(q_eur+fee))
                        if st_status=="filled" or remaining_eur<=0:
                            try: _cancel_order(last_order)
                            except: pass
                            last_order=None
                            break
                    time.sleep(POLL_INTERVAL)

        # تنظيف أخير
        if last_order:
            try: _cancel_order(last_order)
            except: pass

    except Exception as e:
        traceback.print_exc()
        send_message(f"🐞 خطأ أثناء الشراء: {e}")

    if not all_fills:
        bump_patience_on_fail(market)
        send_message("⚠️ لم يكتمل شراء Maker ضمن المهلة.")
        return None

    base_amt, quote_eur, fee_eur = totals_from_fills(all_fills)
    if base_amt<=0:
        bump_patience_on_fail(market); return None
    relax_patience_on_success(market)
    avg=(quote_eur+fee_eur)/base_amt
    return {"amount":base_amt,"avg":avg,"cost_eur":quote_eur+fee_eur,"fee_eur":fee_eur}

# ========= Maker Sell =========
def close_maker_sell(market: str, amount: float):
    patience=get_patience_sec(market); started=time.time()
    remaining=float(amount); all_fills=[]
    last_order=None; last_ask=None
    try:
        while (time.time()-started)<patience and remaining>0:
            ob=fetch_orderbook(market)
            if not ob: time.sleep(0.25); continue
            best_bid=float(ob["bids"][0][0]); best_ask=float(ob["asks"][0][0])
            price=_round_price(market, max(best_ask, best_bid*(1.0+1e-6)))

            if last_order:
                st=_fetch_order(last_order); st_status=st.get("status")
                if st_status in ("filled","partiallyFilled"):
                    fills=st.get("fills",[]) or []
                    if fills:
                        sold,_,_=totals_from_fills(fills)
                        remaining=max(0.0, remaining - sold); all_fills+=fills
                if st_status=="filled" or remaining<=0:
                    try: _cancel_order(last_order)
                    except: pass
                    last_order=None; break

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
                                sold,_,_=totals_from_fills(fills)
                                remaining=max(0.0,remaining-sold); all_fills+=fills
                            if st_status=="filled" or remaining<=0:
                                try: _cancel_order(last_order)
                                except: pass
                                last_order=None; break
                        time.sleep(POLL_INTERVAL)
                    if last_order: continue

            if remaining>0:
                amt=_round_amount(market, remaining)
                res=_place_limit_postonly(market,"sell",price,amt)
                oid=(res or {}).get("orderId")
                if not oid:
                    price2=_round_price(market, best_ask)
                    res=_place_limit_postonly(market,"sell",price2,amt)
                    oid=(res or {}).get("orderId")
                if not oid:
                    time.sleep(SLEEP_BETWEEN_ATTEMPTS); continue
                last_order=oid; last_ask=best_ask

                t0=time.time()
                while time.time()-t0 < MAKER_REPRICE_EVERY:
                    st=_fetch_order(last_order); st_status=st.get("status")
                    if st_status in ("filled","partiallyFilled"):
                        fills=st.get("fills",[]) or []
                        if fills:
                            sold,_,_=totals_from_fills(fills)
                            remaining=max(0.0,remaining-sold); all_fills+=fills
                        if st_status=="filled" or remaining<=0:
                            try: _cancel_order(last_order)
                            except: pass
                            last_order=None; break
                    time.sleep(POLL_INTERVAL)

        if last_order:
            try: _cancel_order(last_order)
            except: pass
    except Exception as e:
        traceback.print_exc(); send_message(f"🐞 خطأ أثناء البيع: {e}")

    proceeds=0.0; fee=0.0; sold=0.0
    for f in all_fills:
        amt=float(f["amount"]); price=float(f["price"]); fe=float(f.get("fee",0) or 0)
        sold+=amt; proceeds+=amt*price; fee+=fe
    proceeds-=fee
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
            with lk: at=active_trade.copy() if active_trade else None
            if not at: time.sleep(0.25); continue
            m=at["symbol"]; ent=at["entry"]
            ob=fetch_orderbook(m); price=float(ob["bids"][0][0]) if ob else ent
            pnl=((price/ent)-1.0)*100.0
            updated_peak=max(at["peak_pct"],pnl)
            new_stop=current_stop_from_peak(updated_peak)

            changed=False
            with lk:
                if active_trade:
                    if updated_peak>active_trade["peak_pct"]+1e-9:
                        active_trade["peak_pct"]=updated_peak; changed=True
                    if abs(new_stop-active_trade.get("dyn_stop_pct",-999.0))>1e-9:
                        active_trade["dyn_stop_pct"]=new_stop; changed=True
            if changed: send_message(f"📈 Peak={updated_peak:.2f}% → SL {new_stop:+.2f}%")

            with lk: at2=active_trade.copy() if active_trade else None
            if at2 and pnl <= at2.get("dyn_stop_pct",-2.0):
                do_close_maker("Dynamic stop"); time.sleep(0.5); continue
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
            res=open_maker_buy(market, eur)
            if not res:
                send_message("⏳ لم يكتمل شراء Maker."); return
            with lk:
                active_trade={
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
            traceback.print_exc(); send_message(f"🐞 خطأ أثناء الفتح: {e}")
    Thread(target=_runner, daemon=True).start()

def do_close_maker(reason=""):
    global active_trade
    try:
        with lk:
            if not active_trade: return
            m=active_trade["symbol"]; amt=float(active_trade["amount"]); cost=float(active_trade["cost_eur"])
        sold, proceeds, sell_fee = close_maker_sell(m, amt)
        with lk:
            pnl_eur=proceeds-cost
            pnl_pct=(proceeds/cost - 1.0)*100.0 if cost>0 else 0.0
            for t in reversed(executed_trades):
                if t["symbol"]==m and "exit_eur" not in t:
                    t.update({"exit_eur":proceeds,"sell_fee_eur":sell_fee,"pnl_eur":pnl_eur,"pnl_pct":pnl_pct,"exit_time":time.time()})
                    break
            active_trade=None
        send_message(f"💰 بيع {m.replace('-EUR','')} (Maker) | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%) {('— '+reason) if reason else ''}")
    except Exception as e:
        traceback.print_exc(); send_message(f"🐞 خطأ أثناء الإغلاق: {e}")

# ========= Summary =========
def build_summary():
    lines=[]
    with lk:
        at=active_trade
        closed=[x for x in executed_trades if "exit_eur" in x]
    if at:
        ob=fetch_orderbook(at["symbol"])
        cur=float(ob["bids"][0][0]) if ob else at["entry"]
        pnl=((cur/at["entry"])-1.0)*100.0
        lines.append("📌 صفقة نشطة:")
        lines.append(f"• {at['symbol'].replace('-EUR','')} @ €{at['entry']:.8f} | PnL {pnl:+.2f}% | Peak {at['peak_pct']:.2f}% | SL {at.get('dyn_stop_pct',-2.0):+.2f}%")
    else:
        lines.append("📌 لا صفقات نشطة.")
    pnl_eur=sum(float(x["pnl_eur"]) for x in closed)
    wins=sum(1 for x in closed if float(x.get("pnl_eur",0))>=0)
    lines.append(f"\n📊 صفقات مكتملة: {len(closed)} | محققة: {pnl_eur:+.2f}€ | فوز/خسارة: {wins}/{len(closed)-wins}")
    lines.append(f"\n⚙️ buy=Maker | sell=Maker | سلم الوقف: -2%→-1%→0%→+1%…")
    return "\n".join(lines)

# ========= Telegram Webhook (على /webhook) =========
def _auth_chat(chat_id: str) -> bool:
    return (not CHAT_ID) or (str(chat_id)==str(CHAT_ID))

def _tg_reply(chat_id: str, text: str):
    if not BOT_TOKEN: 
        print("TG OUT:", text); return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": chat_id, "text": text}, timeout=8)
    except Exception as e:
        print("TG send err:", e)

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    try:
        upd = request.get_json(force=True, silent=True) or {}
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or CHAT_ID or "")
        text = (msg.get("text") or "").strip()
        if not chat_id: return jsonify(ok=True)

        if not _auth_chat(chat_id):
            _tg_reply(chat_id, "⛔ غير مصرّح."); return jsonify(ok=True)

        low=text.lower()
        if low.startswith("/start"):
            _tg_reply(chat_id,"أوامر: /summary /enable /disable /close\n(الشراء فقط من بوت الإشارة عبر /hook)")
            return jsonify(ok=True)

        if low.startswith("/summary"):
            _tg_reply(chat_id, build_summary()); return jsonify(ok=True)

        if low.startswith("/enable"):
            global enabled; enabled=True; _tg_reply(chat_id,"✅ تم التفعيل."); return jsonify(ok=True)

        if low.startswith("/disable"):
            enabled=False; _tg_reply(chat_id,"🛑 تم الإيقاف."); return jsonify(ok=True)

        if low.startswith("/close"):
            with lk:
                has_position = active_trade is not None
            if has_position:
                _tg_reply(chat_id,"⏳ جاري الإغلاق…"); do_close_maker("Manual")
            else:
                _tg_reply(chat_id,"لا توجد صفقة لإغلاقها.")
            return jsonify(ok=True)

        _tg_reply(chat_id,"أوامر: /summary /enable /disable /close\n(الشراء فقط من بوت الإشارة)")
        return jsonify(ok=True)
    except Exception as e:
        print("Telegram webhook err:", e)
        return jsonify(ok=True)

# ========= Signal Relay (/hook) — BUY ONLY =========
@app.route("/hook", methods=["POST"])
def hook():
    try:
        data = request.get_json(silent=True) or {}
        cmd = (data.get("cmd") or "").strip().lower()
        if cmd!="buy":
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
    txt=build_summary()
    return f"<pre>{txt}</pre>"

# ========= Main =========
if __name__ == "__main__" or RUN_LOCAL:
    load_markets()
    app.run(host="0.0.0.0", port=PORT)