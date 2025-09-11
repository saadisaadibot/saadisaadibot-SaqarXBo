# -*- coding: utf-8 -*-
"""
Saqer — Maker-Only Relay (Bitvavo / EUR) — Clean
- شراء/بيع Maker فقط (postOnly) مع تجميع partial fills.
- تصحيح دقيق للـ amount (amountPrecision) والسعر (price significant digits).
- صفقة واحدة فقط + منع شراء متوازي.
- أقل رسائل تيليغرام ممكنة (مضاد سبام).
- الشراء فقط عبر /hook. أوامر تيليغرام: /summary /enable /disable /close عبر /webhook أو /tg.
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

# ========= Tunables (مختصرة) =========
EST_FEE_RATE        = float(os.getenv("FEE_RATE_EST", "0.0025"))
HEADROOM_EUR_MIN    = float(os.getenv("HEADROOM_EUR_MIN", "0.30"))
MAX_SPEND_FRACTION  = float(os.getenv("MAX_SPEND_FRACTION", "1.0"))
FIXED_EUR_PER_TRADE = float(os.getenv("FIXED_EUR", "0"))

MAKER_WAIT_BASE_SEC = int(os.getenv("MAKER_WAIT_BASE_SEC", "45"))
MAKER_WAIT_MAX_SEC  = int(os.getenv("MAKER_WAIT_MAX_SEC", "240"))
MAKER_WAIT_STEP_UP  = 15
MAKER_WAIT_STEP_DOWN= 10

MAKER_GIVEUP_MIN_SEC= float(os.getenv("MAKER_GIVEUP_MIN_SEC", 10))
CANCEL_WAIT_SEC     = float(os.getenv("CANCEL_WAIT_SEC", 6))
ORDER_CHECK_EVERY   = float(os.getenv("ORDER_CHECK_EVERY", 0.6))
WS_STALENESS_SEC    = 2.0

# مطاردة bid (بهدوء)
BID_CHASE_TICKS     = int(os.getenv("BID_CHASE_TICKS", 0))     # 0 = على أفضل bid
REPRICE_ON_TICKS    = int(os.getenv("REPRICE_ON_TICKS", 1))
REPRICE_MAX_AGE_SEC = float(os.getenv("REPRICE_MAX_AGE_SEC", 4.0))

# ========= Runtime =========
enabled         = True
active_trade    = None
executed_trades = []
MARKET_MAP      = {}
MARKET_META     = {}
lk              = Lock()
r               = redis.from_url(REDIS_URL) if REDIS_URL else redis.Redis()

BASE_URL = "https://api.bitvavo.com/v2"
WS_URL   = "wss://ws.bitvavo.com/v2/"
_ws_prices = {}; _ws_lock = Lock()

# ========= Utils =========
def send_message(text: str):
    try:
        if BOT_TOKEN and CHAT_ID:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          json={"chat_id": CHAT_ID, "text": text}, timeout=8)
        else:
            print("TG:", text)
    except Exception as e:
        print("TG err:", e)

_last_notif = {}
def send_message_throttled(key: str, text: str, min_interval=5.0):
    now = time.time()
    if now - _last_notif.get(key, 0) >= min_interval:
        _last_notif[key] = now
        send_message(text)

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
        'Bitvavo-Access-Key': API_KEY, 'Bitvavo-Access-Timestamp': ts,
        'Bitvavo-Access-Signature': sig, 'Bitvavo-Access-Window': '10000'
    }
    try:
        resp = requests.request(method, url, headers=headers,
                                json=(body or {}) if method!="GET" else None,
                                timeout=timeout)
        return resp.json()
    except Exception as e:
        print("bv_request err:", e); return {"error":"request_failed"}

def get_eur_available() -> float:
    try:
        for b in bv_request("GET","/balance"):
            if b.get("symbol")=="EUR": return max(0.0, float(b.get("available",0) or 0))
    except Exception: pass
    return 0.0

def get_asset_available(sym: str) -> float:
    try:
        for b in bv_request("GET","/balance"):
            if b.get("symbol")==sym.upper(): return max(0.0, float(b.get("available",0) or 0))
    except Exception: pass
    return 0.0

# ========= Markets / Meta =========
def _as_float(x, d=0.0):
    try: return float(x)
    except: return d

def load_markets():
    """ pricePrecision قد يكون significant-digits (int) أو tick (float). """
    global MARKET_MAP, MARKET_META
    try:
        rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
        m, meta = {}, {}
        for r0 in rows:
            base, quote, market = r0.get("base"), r0.get("quote"), r0.get("market")
            if base and quote=="EUR":
                # price precision
                pp = r0.get("pricePrecision", 6); priceSig=None; tick=None
                try:
                    if isinstance(pp,int) or (isinstance(pp,str) and pp.isdigit()):
                        priceSig = int(pp)
                    else:
                        v=float(pp); tick = v if 0 < v < 1 else None
                except: tick = 1e-6
                # amount precision → step
                ap = r0.get("amountPrecision", 8)
                try:
                    if isinstance(ap,int) or (isinstance(ap,str) and ap.isdigit()):
                        step = 10.0**(-int(ap))
                    else:
                        v=float(ap); step = v if 0 < v < 1 else 1e-8
                except: step = 1e-8
                m[base.upper()] = market
                meta[market] = {
                    "minQuote": _as_float(r0.get("minOrderInQuoteAsset",0),0.0),
                    "minBase":  _as_float(r0.get("minOrderInBaseAsset", 0),0.0),
                    "tick": tick, "priceSig": priceSig, "step": step
                }
        if m: MARKET_MAP = m
        if meta: MARKET_META = meta
    except Exception as e:
        print("load_markets err:", e)

def coin_to_market(coin: str):
    if not MARKET_MAP: load_markets()
    return MARKET_MAP.get(coin.upper())

def _decimals_from_step(step: float) -> int:
    try:
        s = ("%.16f" % float(step)).rstrip("0").rstrip(".")
        return len(s.split(".")[1]) if "." in s else 0
    except: return 8

def _tick(market):  return (MARKET_META.get(market, {}) or {}).get("tick", 1e-6)
def _step(market):  return (MARKET_META.get(market, {}) or {}).get("step", 1e-8)

# ===== price rounding (significant digits aware) =====
def _round_price_sig(p, sig, direction="down"):
    p = float(p)
    if p <= 0: return p
    k = int(math.floor(math.log10(abs(p)))) + 1  # digits before decimal
    d = sig - k
    factor = 10.0 ** d
    return (math.floor(p*factor)/factor) if direction=="down" else (math.ceil(p*factor)/factor)

def _round_price_down(market, price):
    meta = MARKET_META.get(market,{})
    if meta.get("priceSig"): return _round_price_sig(price, meta["priceSig"], "down")
    tk = meta.get("tick") or 1e-6
    decs = _decimals_from_step(tk)
    p = math.floor(float(price)/tk)*tk
    return round(max(tk,p),decs)

def _round_price_up(market, price):
    meta = MARKET_META.get(market,{})
    if meta.get("priceSig"): return _round_price_sig(price, meta["priceSig"], "up")
    tk = meta.get("tick") or 1e-6
    decs = _decimals_from_step(tk)
    p = math.ceil(float(price)/tk)*tk
    return round(max(tk,p),decs)

def _format_price(market, price) -> str:
    meta = MARKET_META.get(market,{})
    if meta.get("priceSig"):
        p = _round_price_sig(price, meta["priceSig"], "down")
        # تمثيل بخانات معنوية بدون مبالغة
        return f"{p:.{max(1,meta['priceSig'])}g}"
    tk = meta.get("tick") or 1e-6
    decs = _decimals_from_step(tk)
    return f"{_round_price_down(market, price):.{decs}f}"

# ===== amount rounding =====
def _round_amount(market, amount):
    step = _step(market)
    decs = _decimals_from_step(step)
    try:
        a = math.floor(float(amount)/step)*step
        return round(max(step,a),decs)
    except: return round(float(amount),decs)

def _format_amount(market, amount) -> str:
    decs = _decimals_from_step(_step(market))
    return f"{_round_amount(market, amount):.{decs}f}"

def _min_quote(market): return (MARKET_META.get(market,{}) or {}).get("minQuote",0.0)
def _min_base(market):  return (MARKET_META.get(market,{}) or {}).get("minBase", 0.0)

# ========= WS =========
def _ws_on_message(ws, msg):
    try:
        data=json.loads(msg)
        if data.get("event")=="ticker":
            m=data.get("market"); p=float(data.get("price") or data.get("lastPrice") or 0)
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
    try:
        ws = websocket.create_connection(WS_URL, timeout=5)
        ws.send(json.dumps({"action":"subscribe","channels":[{"name":"ticker","markets":markets}]}))
        ws.close()
    except: pass

def fetch_price_ws_first(market: str, staleness=WS_STALENESS_SEC):
    now=time.time()
    with _ws_lock:
        rec=_ws_prices.get(market)
    if rec and (now-rec["ts"])<=staleness: return rec["price"]
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

# ========= Error Parsers =========
def _digits_from_error(err_txt: str):
    m=re.search(r"numbers with\s+(\d+)\s+decimal", err_txt, re.IGNORECASE)
    return int(m.group(1)) if m else None

def _price_sig_from_error(err_txt: str):
    m=re.search(r"precision.*?(\d+)", err_txt, re.IGNORECASE)
    return int(m.group(1)) if m else None

# ========= Order helpers =========
def _place_limit_postonly(market, side, price, amount):
    body = {
        "market": market, "side": side, "orderType": "limit", "postOnly": True,
        "clientOrderId": str(uuid4()), "price": _format_price(market, price),
        "amount": _format_amount(market, amount), "operatorId": ""
    }
    return bv_request("POST","/order",body)

def _fetch_order(mkt, oid):   return bv_request("GET",   f"/order?market={mkt}&orderId={oid}")
def _cancel_order(mkt, oid):  return bv_request("DELETE",f"/order?market={mkt}&orderId={oid}")

def _cancel_order_blocking(market, orderId):
    try: _cancel_order(market, orderId)
    except: pass
    t0=time.time()
    while time.time()-t0 < CANCEL_WAIT_SEC:
        try:
            st=_fetch_order(market, orderId) or {}
            if (st.get("status") or "").lower() in ("canceled","filled"): return
        except: pass
        time.sleep(0.25)

def totals_from_fills(fills):
    tb=tq=fee=0.0
    for f in (fills or []):
        amt=float(f["amount"]); price=float(f["price"]); fe=float(f.get("fee",0) or 0)
        tb+=amt; tq+=amt*price; fee+=fe
    return tb, tq, fee

# ========= Patience learn =========
def _pkey(mkt): return f"maker:patience:{mkt}"
def get_patience_sec(mkt):
    try:
        v=r.get(_pkey(mkt)); 
        if v is not None: return min(MAKER_WAIT_MAX_SEC, max(MAKER_WAIT_BASE_SEC, int(v)))
    except: pass
    return MAKER_WAIT_BASE_SEC
def bump_patience(mkt, up=True):
    try:
        cur=get_patience_sec(mkt); base=MAKER_WAIT_BASE_SEC
        r.set(_pkey(mkt), min(MAKER_WAIT_MAX_SEC, cur+MAKER_WAIT_STEP_UP) if up else max(base, cur-MAKER_WAIT_STEP_DOWN))
    except: pass

# ========= Buy (Maker) =========
def _calc_buy_amount_base(market, target_eur, use_price):
    price=max(1e-12,float(use_price)); min_base=_min_base(market)
    if (target_eur/price) < (min_base-1e-15): return 0.0
    return _round_amount(market, float(target_eur)/price)

def open_maker_buy(market: str, eur_amount: float):
    eur_avail = get_eur_available()

    # مبلغ الصفقة
    target = float(FIXED_EUR_PER_TRADE) if FIXED_EUR_PER_TRADE>0 else (float(eur_amount) if (eur_amount or 0)>0 else eur_avail)
    target = min(target, eur_avail*MAX_SPEND_FRACTION)
    minq   = _min_quote(market)

    buffer = max(HEADROOM_EUR_MIN, target*EST_FEE_RATE*2.0, 0.05)
    spend  = min(target, max(0.0, eur_avail-buffer))
    if spend < minq:
        send_message_throttled("bal", f"⛔ رصيد غير كافٍ. متاح €{eur_avail:.2f} | بعد الهامش €{spend:.2f} | المطلوب ≥ €{minq:.2f}")
        return None

    base_sym    = market.split("-")[0]
    base_before = get_asset_available(base_sym)
    send_message_throttled("hdr", f"💰 EUR متاح: €{eur_avail:.2f} | سننفق: €{spend:.2f} | ماركت: {market}", 15)

    patience      = get_patience_sec(market)
    deadline      = time.time()+patience
    placed_at     = None
    last_order    = None
    my_price      = None
    all_fills     = []
    remaining_eur = float(spend)
    last_seen     = None

    try:
        while time.time()<deadline:
            ob = fetch_orderbook(market)
            if not ob or not ob.get("bids") or not ob.get("asks"):
                time.sleep(0.25); continue

            best_bid = float(ob["bids"][0][0]); best_ask = float(ob["asks"][0][0]); tk=_tick(market)
            target_price = min(best_bid + BID_CHASE_TICKS*tk, best_ask - tk)
            target_price = _round_price_down(market, target_price)
            last_seen = target_price

            if last_order:
                st=_fetch_order(market, last_order) or {}; st_status=(st.get("status") or "").lower()
                if st_status in ("filled","partiallyfilled"):
                    fills=st.get("fills",[]) or []
                    if fills:
                        b, q, fee = totals_from_fills(fills); all_fills+=fills
                        remaining_eur=max(0.0, remaining_eur-(q+fee))
                    if st_status=="filled": break

                # إعادة تسعير لو ابتعدنا أو صار قديم
                need=False
                if my_price is not None:
                    ticks_away=int(round((best_bid-my_price)/tk))
                    if ticks_away>=REPRICE_ON_TICKS: need=True
                if not need and placed_at and (time.time()-placed_at)>=REPRICE_MAX_AGE_SEC: need=True
                if need:
                    _cancel_order_blocking(market, last_order); last_order=None; continue

                if placed_at and (time.time()-placed_at)<MAKER_GIVEUP_MIN_SEC:
                    time.sleep(ORDER_CHECK_EVERY); continue
                if time.time()>=deadline: break

                time.sleep(ORDER_CHECK_EVERY); continue

            # لا يوجد أمر ⇒ ضَع أمرًا
            if remaining_eur < (minq*0.999): break
            amt=_calc_buy_amount_base(market, remaining_eur, target_price)
            if amt<=0: break

            exp=amt*target_price
            send_message_throttled(f"try:{market}", f"🧪 محاولة شراء {market}: amount={_format_amount(market,amt)} | سعر≈{_format_price(market,target_price)} | EUR≈{exp:.2f}", 10)
            res=_place_limit_postonly(market,"buy",target_price,amount=amt)
            oid=(res or {}).get("orderId"); raw_err=(res or {}).get("error",""); err=str(raw_err).lower()

            if oid:
                last_order=oid; my_price=target_price; placed_at=time.time()
                time.sleep(ORDER_CHECK_EVERY); continue

            # تصحيح precision للكمية
            if "too many decimal digits" in err and "amount" in err:
                d=_digits_from_error(str(raw_err))
                if d is not None:
                    MARKET_META.setdefault(market,{}).update({"step": 10.0**(-d)})
                    amt=_calc_buy_amount_base(market, remaining_eur, target_price)
                    res2=_place_limit_postonly(market,"buy",target_price,amount=amt)
                    oid2=(res2 or {}).get("orderId")
                    if oid2:
                        last_order=oid2; my_price=target_price; placed_at=time.time()
                        time.sleep(ORDER_CHECK_EVERY); continue
                    else:
                        raw_err=(res2 or {}).get("error",""); err=str(raw_err).lower()

            # تصحيح precision للسعر (significant digits)
            if "price is too detailed" in err:
                sig=_price_sig_from_error(str(raw_err))
                if sig:
                    MARKET_META.setdefault(market,{}).update({"priceSig": int(sig)})
                    target_price=_round_price_down(market, target_price)
                    res2=_place_limit_postonly(market,"buy",target_price,amount=amt)
                    oid2=(res2 or {}).get("orderId")
                    if oid2:
                        last_order=oid2; my_price=target_price; placed_at=time.time()
                        time.sleep(ORDER_CHECK_EVERY); continue
                    else:
                        raw_err=(res2 or {}).get("error",""); err=str(raw_err).lower()

            # أي خطأ آخر → هدنة قصيرة بدون سبام
            if err:
                send_message_throttled(f"err:{market}", f"⚠️ تعذّر وضع أمر Maker: {raw_err}", 20)
            time.sleep(0.8); continue

        if last_order: _cancel_order_blocking(market, last_order)

    except Exception as e:
        print("open_maker_buy err:", e)

    # النتيجة
    if all_fills:
        base_amt, qeur, feur = totals_from_fills(all_fills)
        if base_amt>0:
            bump_patience(market, up=False)
            avg=(qeur+feur)/base_amt
            return {"amount": base_amt, "avg": avg, "cost_eur": qeur+feur, "fee_eur": feur}

    # فحص رصيد fallback/late
    base_after = get_asset_available(base_sym)
    delta = base_after - base_before
    if delta > (_step(market)*0.5):
        px = fetch_price_ws_first(market) or last_seen or 0.0
        avg = float(px) if px and px>0 else (last_seen or 0.0)
        send_message_throttled("late", f"ℹ️ رُصد شراء فعلي (~{delta:.8f} {base_sym}).")
        bump_patience(market, up=False)
        return {"amount": delta, "avg": avg, "cost_eur": (delta*avg if avg>0 else 0.0), "fee_eur": 0.0}

    bump_patience(market, up=True)
    send_message_throttled("fail", "⚠️ لم يكتمل شراء Maker ضمن المهلة.", 15)
    return None

# ========= Sell (Maker) مختصر =========
def close_maker_sell(market: str, amount: float):
    patience=get_patience_sec(market)
    started=time.time()
    remaining=float(amount); all_fills=[]; last_order=None

    try:
        while (time.time()-started)<patience and remaining>0:
            ob=fetch_orderbook(market)
            if not ob or not ob.get("bids") or not ob.get("asks"):
                time.sleep(0.25); continue
            best_bid=float(ob["bids"][0][0]); best_ask=float(ob["asks"][0][0])
            price=_round_price_up(market, max(best_ask, best_bid*(1.0+1e-6)))

            if last_order:
                st=_fetch_order(market,last_order) or {}; st_status=(st.get("status") or "").lower()
                if st_status in ("filled","partiallyfilled"):
                    fills=st.get("fills",[]) or []
                    if fills:
                        sold,_,_=totals_from_fills(fills); remaining=max(0.0,remaining-sold); all_fills+=fills
                if st_status=="filled" or remaining<=0:
                    try:_cancel_order(market,last_order)
                    except: pass
                    last_order=None; break
                time.sleep(ORDER_CHECK_EVERY); continue

            amt_to_place=_round_amount(market, remaining)
            res=_place_limit_postonly(market,"sell",price,amount=amt_to_place)
            oid=(res or {}).get("orderId")
            if not oid:
                time.sleep(0.4); continue
            last_order=oid
            time.sleep(ORDER_CHECK_EVERY)

        if last_order:
            try:_cancel_order(market,last_order)
            except: pass
    except Exception as e:
        print("close_maker_sell err:", e)

    proceeds=fee=sold=0.0
    for f in all_fills:
        a=float(f["amount"]); p=float(f["price"]); fe=float(f.get("fee",0) or 0)
        sold+=a; proceeds+=a*p; fee+=fe
    return sold, max(0.0, proceeds-fee), fee

# ========= Monitor: وقف ديناميكي بسيط =========
STOP_LADDER=[(0.0,-2.0),(1.0,-1.0),(2.0,0.0),(3.0,1.0),(4.0,2.0),(5.0,3.0)]
def _stop_from_peak(peak):
    s=-999.0
    for th,val in STOP_LADDER:
        if peak>=th: s=val
    return s

def monitor_loop():
    global active_trade
    while True:
        try:
            with lk: at=active_trade.copy() if active_trade else None
            if not at: time.sleep(0.25); continue
            cur=fetch_price_ws_first(at["symbol"])
            if not cur: time.sleep(0.25); continue
            pnl=((cur/at["entry"])-1.0)*100.0
            peak=max(at["peak_pct"],pnl); new_sl=_stop_from_peak(peak)
            upd=False
            with lk:
                if active_trade:
                    if peak>active_trade["peak_pct"]+1e-9: active_trade["peak_pct"]=peak; upd=True
                    if abs(new_sl-active_trade.get("dyn_stop_pct",-999.0))>1e-9: active_trade["dyn_stop_pct"]=new_sl; upd=True
            if upd: send_message_throttled("upd", f"📈 Peak {peak:.2f}% → SL {new_sl:+.2f}%", 10)
            with lk: at2=active_trade.copy() if active_trade else None
            if at2 and pnl<=at2.get("dyn_stop_pct",-2.0):
                do_close_maker("Dynamic stop")
            time.sleep(0.12)
        except Exception as e:
            print("monitor err:", e); time.sleep(0.5)
Thread(target=monitor_loop, daemon=True).start()

# ========= Trade Flow =========
buy_in_progress=False
def do_open_maker(market: str, eur: float):
    def _runner():
        global active_trade, buy_in_progress
        try:
            with lk:
                if active_trade: send_message("⛔ توجد صفقة نشطة."); return
                if buy_in_progress: send_message("⛔ عملية شراء جارية."); return
                buy_in_progress=True
            res=open_maker_buy(market, eur)
            if not res:
                return
            with lk:
                active_trade={
                    "symbol": market, "entry": float(res["avg"]), "amount": float(res["amount"]),
                    "cost_eur": float(res["cost_eur"]), "buy_fee_eur": float(res["fee_eur"]),
                    "opened_at": time.time(), "peak_pct": 0.0, "dyn_stop_pct": -2.0
                }
                executed_trades.append(active_trade.copy())
            send_message(f"✅ شراء {market.replace('-EUR','')} @ €{active_trade['entry']:.8f} | كمية {active_trade['amount']:.8f}")
        except Exception as e:
            traceback.print_exc(); send_message(f"🐞 خطأ أثناء الفتح: {e}")
        finally:
            with lk: buy_in_progress=False
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
            pnl_pct = (proceeds/cost - 1.0)*100.0 if cost>0 else 0.0
            for t in reversed(executed_trades):
                if t["symbol"]==m and "exit_eur" not in t:
                    t.update({"exit_eur": proceeds, "sell_fee_eur": sell_fee,
                              "pnl_eur": pnl_eur, "pnl_pct": pnl_pct, "exit_time": time.time()})
                    break
            active_trade=None
        send_message(f"💰 بيع {m.replace('-EUR','')} | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%) {('— '+reason) if reason else ''}")
    except Exception as e:
        traceback.print_exc(); send_message(f"🐞 خطأ أثناء الإغلاق: {e}")

# ========= Summary =========
def build_summary():
    lines=[]; 
    with lk:
        at=active_trade; closed=[x for x in executed_trades if "exit_eur" in x]
    if at:
        cur=fetch_price_ws_first(at["symbol"]) or at["entry"]
        pnl=((cur/at["entry"])-1.0)*100.0
        lines.append("📌 صفقة نشطة:")
        lines.append(f"• {at['symbol'].replace('-EUR','')} @ €{at['entry']:.8f} | PnL {pnl:+.2f}% | Peak {at['peak_pct']:.2f}% | SL {at.get('dyn_stop_pct',-2.0):+.2f}%")
    else:
        lines.append("📌 لا صفقات نشطة.")
    pnl_eur=sum(float(x.get("pnl_eur",0)) for x in closed)
    wins=sum(1 for x in closed if float(x.get("pnl_eur",0))>=0)
    lines.append(f"\n📊 مكتملة: {len(closed)} | محققة: {pnl_eur:+.2f}€ | فوز/خسارة: {wins}/{len(closed)-wins}")
    lines.append("\n⚙️ buy=Maker | sell=Maker")
    return "\n".join(lines)

# ========= Telegram =========
def _tg_reply(chat_id: str, text: str):
    if not BOT_TOKEN: print("TG OUT:", text); return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": chat_id, "text": text}, timeout=8)
    except Exception as e: print("TG send err:", e)

def _auth_chat(chat_id: str) -> bool:
    return (not CHAT_ID) or (str(chat_id)==str(CHAT_ID))

def _handle_tg_update(upd: dict):
    global enabled
    msg=upd.get("message") or upd.get("edited_message") or {}
    chat=msg.get("chat") or {}
    chat_id=str(chat.get("id") or ""); text=(msg.get("text") or "").strip()
    if not chat_id: return
    if not _auth_chat(chat_id): _tg_reply(chat_id,"⛔ غير مصرّح."); return

    low=text.lower()
    if low.startswith("/start"):
        _tg_reply(chat_id,"أوامر: /summary /enable /disable /close\nالشراء فقط من /hook"); return
    if low.startswith("/summary"):
        _tg_reply(chat_id, build_summary()); return
    if low.startswith("/enable"):
        enabled=True; _tg_reply(chat_id,"✅ تم التفعيل."); return
    if low.startswith("/disable"):
        enabled=False; _tg_reply(chat_id,"🛑 تم الإيقاف."); return
    if low.startswith("/close"):
        with lk: has=active_trade is not None
        if has: do_close_maker("Manual"); _tg_reply(chat_id,"⏳ إغلاق (Maker)…")
        else: _tg_reply(chat_id,"لا توجد صفقة.")
        return
    _tg_reply(chat_id,"/summary /enable /disable /close")

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    try:
        upd=request.get_json(silent=True) or {}
        _handle_tg_update(upd)
        return jsonify(ok=True)
    except Exception as e:
        print("Telegram webhook err:", e); return jsonify(ok=True)

@app.route("/tg", methods=["POST"])
def telegram_webhook_alias(): return telegram_webhook()

# ========= Hook =========
@app.route("/hook", methods=["POST"])
def hook():
    try:
        data=request.get_json(silent=True) or {}
        if (data.get("cmd") or "").strip().lower()!="buy":
            return jsonify({"ok":False,"err":"only_buy"}),400
        if not enabled: return jsonify({"ok":False,"err":"disabled"}),400
        coin=(data.get("coin") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{2,15}", coin or ""):
            return jsonify({"ok":False,"err":"bad_coin"}),400
        market=coin_to_market(coin)
        if not market: send_message(f"⛔ {coin}-EUR غير متاح."); return jsonify({"ok":False,"err":"no_market"}),400
        eur = float(data.get("eur")) if data.get("eur") is not None else None
        do_open_maker(market, eur)
        return jsonify({"ok":True,"msg":"buy_started","market":market})
    except Exception as e:
        traceback.print_exc(); return jsonify({"ok":False,"err":str(e)}),500

# ========= Health =========
@app.route("/", methods=["GET"])
def home(): return "Saqer Maker Relay ✅"

@app.route("/summary", methods=["GET"])
def http_summary(): return f"<pre>{build_summary()}</pre>"

# ========= Main =========
if __name__ == "__main__" or RUN_LOCAL:
    load_markets()
    app.run(host="0.0.0.0", port=PORT)