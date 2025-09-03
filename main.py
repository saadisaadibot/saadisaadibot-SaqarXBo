# -*- coding: utf-8 -*-
"""
Nems — PRO MAKER-FIRST (Railway, Bitvavo EUR)
صفقة واحدة فقط، دخول Maker أولاً ثم تَحوّل Taker عند الحاجة، Replacement محافظ، وخروج برسوم منخفضة قدر الإمكان.

تحذير: التداول ينطوي على مخاطر. اضبط الرسوم/الحدود حسب حسابك.
"""

import os, re, time, json, traceback, statistics as st
from collections import deque
from threading import Thread, Lock

import requests, redis, websocket
from flask import Flask, request
from dotenv import load_dotenv
from uuid import uuid4

# ========= Boot / ENV =========
load_dotenv()
app = Flask(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHAT_ID     = os.getenv("CHAT_ID")
API_KEY     = os.getenv("BITVAVO_API_KEY")
API_SECRET  = os.getenv("BITVAVO_API_SECRET")
REDIS_URL   = os.getenv("REDIS_URL")
RUN_LOCAL   = os.getenv("RUN_LOCAL", "0") == "1"

# رسوم (bps = جزء من %)
FEE_MAKER_BPS = float(os.getenv("FEE_MAKER_BPS", 12))   # 0.12%
FEE_TAKER_BPS = float(os.getenv("FEE_TAKER_BPS", 25))   # 0.25%

BASE_URL    = "https://api.bitvavo.com/v2"
WS_URL      = "wss://ws.bitvavo.com/v2/"

# ========= Settings (لطيفة وغير خانقة) =========
MAX_TRADES              = 1              # صفقة واحدة فقط
ENGINE_INTERVAL_SEC     = 0.40
TOPN_WATCH              = 120
WATCH_REFRESH_SEC       = 120

# اختيار/إشارة
AUTO_THRESHOLD          = 32.0           # عتبة Score عامة
THRESH_SPREAD_BP_MAX    = 220.0          # سقف السبريد المقبول
THRESH_IMB_MIN          = 0.40           # أدنى ميل طلبات (خفيف)

# Replacement (فقط عند ميزة واضحة تغطي الرسوم + هامش)
REPLACE_MIN_HOLD_SEC    = 90
REPLACE_GAP_SCORE       = 6.0            # فارق Score مطلوب
REPLACE_MIN_EDGE_PCT    = 0.30           # ربحية نظرية تغطي الرسوم وتستحق التبديل

# إدارة مركز
EUR_RESERVE             = 0.00           # احتياطي يورو لا يُستخدم
BUY_MIN_EUR             = 5.00
DAILY_STOP_EUR          = -20.0
COOLDOWN_SEC            = 45

# Maker-first سلوك
MAKER_POSTONLY          = True           # استخدم postOnly إن توفّر
MAKER_MAX_REQUOTES      = 3              # مرّات إعادة التسعير
MAKER_REQUOTE_SEC       = 6              # كل كم ثانية نعدّل السعر
MAKER_WAIT_AFTER_LAST   = 10             # بعد آخر re-quote كم ثانية ننتظر
MAKER_PRICE_OFFSET_BPS  = 3.0            # ندخل على جانب الدفتر: buy على bid+Δbps / sell على ask-Δbps

# خروج
TP_WEAK                 = 0.9
TP_GOOD                 = 1.5
TRAIL_START             = 2.2           # من هذه النقطة نفعّل giveback
TRAIL_GIVEBACK_RATIO    = 0.40
TRAIL_GIVEBACK_CAP      = 1.4
SL_START                = -2.8
SL_STEP                 = 0.9            # يعلو مع التقدم
HOLD_MIN_SEC            = 75             # لا بيع مبكر مباشرة بعد الدخول
TIME_STOP_MIN           = 14*60
TIME_STOP_NEUTRAL_LO    = -0.4
TIME_STOP_NEUTRAL_HI    = 0.9

# ========= State & infra =========
r  = redis.from_url(REDIS_URL) if REDIS_URL else redis.Redis()
lk = Lock()

enabled = True
auto_enabled = True
debug_tg = False

active_trade   = {}      # صفقة واحدة
executed_trades= []

SINCE_RESET_KEY = "nems:since_reset"

# WS / caches
_ws_lock    = Lock()
_ws_prices  = {}   # market -> {price, ts}
WS_CONN=None; WS_RUNNING=False

WATCHLIST_MARKETS=set()
_prev_watch=set()
_last_watch=0
HISTS={}
OB_CACHE={}
VOL_CACHE={}
TICK24_CACHE={"ts":0,"rows":[]}

# ========= Utils =========
def send_message(text, force=False):
    try:
        if not (BOT_TOKEN and CHAT_ID):
            print("TG:", text); return
        if not force:
            key="dedup:"+str(abs(hash(text))%(10**12))
            if not r.setnx(key,1):
                return
            r.expire(key, 60)
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id":CHAT_ID,"text":text},
            timeout=8
        )
    except Exception as e:
        print("TG err:", e)

def dbg(msg):
    if debug_tg:
        send_message(f"🐞 {msg}", force=True)

def create_sig(ts, method, path, body_str=""):
    import hmac, hashlib
    msg=f"{ts}{method}{path}{body_str}"
    return hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def bv_request(method, path, body=None, timeout=10):
    url=f"{BASE_URL}{path}"
    ts=str(int(time.time()*1000))
    body_str="" if method=="GET" else json.dumps(body or {}, separators=(',',':'))
    sig=create_sig(ts,method,f"/v2{path}",body_str)
    headers={
        'Bitvavo-Access-Key':API_KEY,
        'Bitvavo-Access-Timestamp':ts,
        'Bitvavo-Access-Signature':sig,
        'Bitvavo-Access-Window':'10000'
    }
    try:
        resp=requests.request(method,url,headers=headers,
                              json=(body or {}) if method!="GET" else None,
                              timeout=timeout)
        return resp.json()
    except Exception as e:
        print("bv_request err:", e); return {"error":"request_failed"}

def get_eur_available()->float:
    try:
        bals=bv_request("GET","/balance")
        if isinstance(bals,list):
            for b in bals:
                if b.get("symbol")=="EUR":
                    return max(0.0, float(b.get("available",0) or 0))
    except Exception: pass
    return 0.0

def _today_key(): return time.strftime("pnl:%Y%m%d", time.gmtime())
def _accum_realized(p):
    try: r.incrbyfloat(_today_key(), float(p)); r.expire(_today_key(), 3*24*3600)
    except Exception: pass
def _today_pnl():
    try: return float(r.get(_today_key()) or 0.0)
    except Exception: return 0.0

# ========= WS =========
def _ws_sub_payload(mkts): return {"action":"subscribe","channels":[{"name":"ticker","markets":mkts}]}
def _ws_on_open(ws):
    try:
        mkts=sorted(WATCHLIST_MARKETS | ({active_trade["symbol"]} if active_trade else set()))
        if mkts: ws.send(json.dumps(_ws_sub_payload(mkts)))
    except Exception: traceback.print_exc()
def _ws_on_message(ws,msg):
    try: d=json.loads(msg)
    except Exception: return
    if isinstance(d,dict) and d.get("event")=="ticker":
        m=d.get("market"); price=d.get("price") or d.get("lastPrice") or d.get("open")
        try:
            p=float(price); 
            if p>0:
                with _ws_lock: _ws_prices[m]={"price":p,"ts":time.time()}
        except Exception: pass
def _ws_on_error(ws,err): print("WS err:", err)
def _ws_on_close(ws,c,rn): 
    global WS_RUNNING; WS_RUNNING=False; print("WS closed:",c,rn)
def _ws_thread():
    global WS_CONN, WS_RUNNING
    while True:
        try:
            WS_RUNNING=True
            WS_CONN=websocket.WebSocketApp(WS_URL, on_open=_ws_on_open,
                                           on_message=_ws_on_message,
                                           on_error=_ws_on_error, on_close=_ws_on_close)
            WS_CONN.run_forever(ping_interval=25, ping_timeout=10)
        except Exception as e:
            print("WS loop ex:", e)
        finally:
            WS_RUNNING=False; time.sleep(2)
Thread(target=_ws_thread, daemon=True).start()

def fetch_price_ws_first(market, staleness=2.0):
    now=time.time()
    with _ws_lock:
        rec=_ws_prices.get(market)
    if rec and (now-rec["ts"])<=staleness: return rec["price"]
    try:
        j=requests.get(f"{BASE_URL}/ticker/price?market={market}", timeout=5).json()
        p=float(j.get("price",0) or 0)
        if p>0:
            with _ws_lock: _ws_prices[market]={"price":p,"ts":now}
            return p
    except Exception: pass
    return None

# ========= Hist / OB =========
def _init_hist(m):
    if m not in HISTS: HISTS[m]={"hist":deque(maxlen=900),"last_new_high":time.time()}
def _update_hist(m,ts,price):
    _init_hist(m); HISTS[m]["hist"].append((ts,price))
    cutoff=ts-300
    while HISTS[m]["hist"] and HISTS[m]["hist"][0][0]<cutoff:
        HISTS[m]["hist"].popleft()

def _mom_metrics_symbol(m, price_now):
    _init_hist(m); hist=HISTS[m]["hist"]
    if not hist: return 0.0,0.0,0.0,False
    now_ts=hist[-1][0]; p15=p30=p60=None; hi=price_now
    for ts,p in hist:
        hi=max(hi,p); age=now_ts-ts
        if p15 is None and age>=15: p15=p
        if p30 is None and age>=30: p30=p
        if p60 is None and age>=60: p60=p
    base=hist[0][1]
    p15=p15 or base; p30=p30 or base; p60=p60 or base
    r15=(price_now/p15-1.0)*100.0 if p15>0 else 0.0
    r30=(price_now/p30-1.0)*100.0 if p30>0 else 0.0
    r60=(price_now/p60-1.0)*100.0 if p60>0 else 0.0
    new_high=price_now>=hi*0.999
    if new_high: HISTS[m]["last_new_high"]=now_ts
    return r15,r30,r60,new_high

def _backfill_hist_1m(m, minutes=60):
    try:
        rows=requests.get(f"{BASE_URL}/{m}/candles?interval=1m&limit={minutes}", timeout=6).json()
        if not isinstance(rows,list): return
        for ts,o,h,l,c,v in rows[-minutes:]:
            _update_hist(m, ts/1000.0, float(c))
        p=fetch_price_ws_first(m); 
        if p: _update_hist(m, time.time(), p)
    except Exception as e: print("backfill err:", e)

def fetch_orderbook(m, ttl=1.8):
    now=time.time(); rec=OB_CACHE.get(m)
    if rec and (now-rec["ts"])<ttl: return rec["data"]
    try:
        j=requests.get(f"{BASE_URL}/{m}/book", timeout=5).json()
        if j and j.get("bids") and j.get("asks"):
            OB_CACHE[m]={"data":j,"ts":now}; return j
    except Exception: pass
    return None

def orderbook_guard(market, min_bid_eur=30.0, req_imb=THRESH_IMB_MIN, max_spread_bp=THRESH_SPREAD_BP_MAX, depth_used=3):
    ob=fetch_orderbook(market)
    if not ob or not ob.get("bids") or not ob.get("asks"): return False,"no_book",{}
    try:
        bid_p=float(ob["bids"][0][0]); ask_p=float(ob["asks"][0][0])
        bid_eur=sum(float(p)*float(q) for p,q,*_ in ob["bids"][:depth_used])
        ask_eur=sum(float(p)*float(q) for p,q,*_ in ob["asks"][:depth_used])
    except Exception: return False,"bad_book",{}
    spread_bp=(ask_p-bid_p)/((ask_p+bid_p)/2.0)*10000.0
    imb=bid_eur/max(1e-9, ask_eur)
    if bid_eur<min_bid_eur: return False,f"low_liq:{bid_eur:.0f}",{"spread_bp":spread_bp,"imb":imb,"bid_eur":bid_eur}
    if spread_bp>max_spread_bp: return False,f"wide_spread:{spread_bp:.0f}",{"spread_bp":spread_bp,"imb":imb,"bid_eur":bid_eur}
    if imb<req_imb: return False,f"weak_imb:{imb:.2f}",{"spread_bp":spread_bp,"imb":imb,"bid_eur":bid_eur}
    return True,"ok",{"spread_bp":spread_bp,"imb":imb,"bid_eur":bid_eur}

# ========= Watchlist (يغطي السوق) =========
def _t_rows(): return TICK24_CACHE.get("rows",[])
def _t_set(ts,rows): TICK24_CACHE.update({"ts":ts,"rows":rows})
def _t_fresh():
    now=time.time()
    if now-TICK24_CACHE.get("ts",0)<60: return _t_rows()
    try:
        rows=requests.get(f"{BASE_URL}/ticker/24h", timeout=8).json()
        if isinstance(rows,list): _t_set(now,rows); return rows
    except Exception: pass
    return _t_rows()

def _score_market_volvol(market):
    now=time.time(); rec=VOL_CACHE.get(market)
    if rec and (now-rec[0])<120: return rec[1]
    try:
        cs=requests.get(f"{BASE_URL}/{market}/candles?interval=1m&limit=20", timeout=4).json()
        closes=[float(c[4]) for c in cs if isinstance(c,list) and len(c)>=5]
        if len(closes)<6: score=0.0
        else:
            rets=[closes[i]/closes[i-1]-1.0 for i in range(1,len(closes))]
            v5=abs(sum(rets[-5:]))*100.0
            stdv=(st.pstdev(rets)*100.0) if len(rets)>2 else 0.0
            score=0.55*stdv + 0.45*v5
    except Exception: score=0.0
    VOL_CACHE[market]=(now,score)
    return score

def build_watchlist(n=TOPN_WATCH):
    rows=_t_fresh(); picks=[]
    for r0 in rows:
        m=r0.get("market","")
        if not m.endswith("-EUR"): continue
        try:
            last=float(r0.get("last",0) or 0)
            if last<=0: continue
        except Exception: continue
        s=_score_market_volvol(m)
        if s>0: picks.append((s,m))
    picks.sort(reverse=True)
    return [m for _,m in picks[:n]]

def refresh_watchlist():
    global WATCHLIST_MARKETS,_prev_watch,_last_watch
    now=time.time()
    if now-_last_watch < WATCH_REFRESH_SEC: return
    _last_watch=now
    new=set(build_watchlist(TOPN_WATCH))
    with _ws_lock: WATCHLIST_MARKETS=set(new)
    newly=new-_prev_watch
    for m in newly: _backfill_hist_1m(m, minutes=60)
    _prev_watch=set(new)
    dbg(f"[{time.strftime('%H:%M:%S')}] Watchlist size: {len(WATCHLIST_MARKETS)}")

# ========= Scoring =========
def score_exploder(market, price_now):
    r15,r30,r60,_ = _mom_metrics_symbol(market, price_now)
    accel = r30 - r60
    ok,_,feats = orderbook_guard(market, max_spread_bp=THRESH_SPREAD_BP_MAX, req_imb=THRESH_IMB_MIN)
    spread = feats.get("spread_bp", 999.0); imb=feats.get("imb", 0.0)

    mom_pts = max(0.0, min(70.0, 2.0*r15 + 2.0*r30 + 0.4*r60))
    acc_pts = max(0.0, min(20.0, 7.0*max(0.0, accel)))
    ob_pts  = 0.0
    if ok:
        if spread <= 180.0: ob_pts += max(0.0, min(10.0, (180.0-spread)*0.08))
        if imb >= 0.9:      ob_pts += max(0.0, min(10.0, (imb-0.9)*12.0))
        if spread <= 80.0:  ob_pts += 3.0
        if imb >= 1.20:     ob_pts += 4.0

    score = max(0.0, min(100.0, mom_pts + acc_pts + ob_pts))
    sniper = (r15>=0.10 and accel>=0.12 and spread<=160.0 and imb>=0.95)
    return score, r15, r30, r60, accel, spread, imb, sniper

# ========= Orders =========
def _round_amount(amount): return float(f"{amount:.10f}")

def place_limit(side, market, price, amount, post_only=True):
    body = {
        "market": market, "side": side, "orderType": "limit",
        "price": f"{price:.10f}", "amount": f"{amount:.10f}",
        "timeInForce": "GTC", "clientOrderId": str(uuid4()), "operatorId": ""
    }
    if post_only: body["postOnly"] = True
    return bv_request("POST","/order", body)

def place_market(side, market, amount=None, amount_quote=None):
    body = {
        "market": market, "side": side, "orderType": "market",
        "clientOrderId": str(uuid4()), "operatorId": ""
    }
    if side=="buy":
        body["amountQuote"]=f"{amount_quote:.2f}"
    else:
        body["amount"]=f"{amount:.10f}"
    return bv_request("POST","/order", body)

def cancel_order(market, order_id):
    return bv_request("DELETE", f"/order?market={market}&orderId={order_id}")

def totals_from_fills_eur(fills):
    tb=tq=fee=0.0
    for f in (fills or []):
        amt=float(f["amount"]); price=float(f["price"]); fe=float(f.get("fee",0) or 0)
        tb+=amt; tq+=amt*price; fee+=fe
    return tb,tq,fee

# ========= Entry (Maker-first) =========
def maker_to_taker_buy(market, eur_to_spend):
    """
    يحاول يدخل كـ Maker عبر Limit (postOnly) عدة مرات مع إعادة تسعير،
    ثم يتحول إلى Market taker إن لم يتنفّذ.
    """
    ob = fetch_orderbook(market)
    if not ob: return None, "no_ob"
    best_bid = float(ob["bids"][0][0]); best_ask = float(ob["asks"][0][0])
    mid = (best_bid+best_ask)/2.0
    # السعر المبدئي: على جانب المشترين (maker): فوق الـ bid بقليل
    price = best_bid * (1.0 + MAKER_PRICE_OFFSET_BPS/10000.0)
    amount = _round_amount(eur_to_spend / price)

    order_id=None
    for attempt in range(MAKER_MAX_REQUOTES+1):
        res = place_limit("buy", market, price, amount, post_only=MAKER_POSTONLY)
        if isinstance(res,dict) and res.get("status") in ("new","filled","partiallyFilled"):
            order_id = res.get("orderId")
            t0=time.time()
            wait = MAKER_REQUOTE_SEC if attempt < MAKER_MAX_REQUOTES else MAKER_WAIT_AFTER_LAST
            while time.time()-t0 < wait:
                # تحقق من حالة الطلب
                stt = bv_request("GET", f"/order?market={market}&orderId={order_id}")
                if isinstance(stt,dict) and stt.get("status")=="filled":
                    fills=stt.get("fills",[])
                    tb,tq,fee=totals_from_fills_eur(fills)
                    if tb>0:
                        return {"fills":fills,"status":"filled","maker":True}, "filled_maker"
                time.sleep(1.2)
            # لم يٌملأ كليًا: نلغيه ونعيد تسعير
            cancel_order(market, order_id)
            ob = fetch_orderbook(market)
            if not ob: break
            best_bid = float(ob["bids"][0][0]); best_ask = float(ob["asks"][0][0])
            price = best_bid * (1.0 + MAKER_PRICE_OFFSET_BPS/10000.0)
            amount = _round_amount(eur_to_spend / price)

    # تحوّل لتاكر (Market) إذا لسه المؤشرات صالحة
    res = place_market("buy", market, amount_quote=eur_to_spend)
    if isinstance(res,dict) and res.get("status")=="filled":
        return {"fills":res.get("fills",[]),"status":"filled","maker":False}, "filled_taker"
    return None, "failed"

def maker_to_taker_sell(market, amount):
    ob = fetch_orderbook(market)
    if not ob: return None, "no_ob"
    best_bid = float(ob["bids"][0][0]); best_ask=float(ob["asks"][0][0])
    price = best_ask * (1.0 - MAKER_PRICE_OFFSET_BPS/10000.0)  # بيع كـ Maker: فوق الـ bid؟ كصانع بنحط على ask جانب الصفقات (سعر ≥ ask)،
    # لضمان Maker حقيقي يجب أن يكون السعر >= أفضل Ask. سنرفع قليلًا:
    price = max(price, best_ask * (1.0 + 0.0001))

    order_id=None
    res = place_limit("sell", market, price, amount, post_only=MAKER_POSTONLY)
    if isinstance(res,dict) and res.get("status") in ("new","partiallyFilled","filled"):
        order_id = res.get("orderId")
        t0=time.time()
        # نمنح فرصة للتنفيذ الماكر
        while time.time()-t0 < MAKER_WAIT_AFTER_LAST:
            stt = bv_request("GET", f"/order?market={market}&orderId={order_id}")
            if isinstance(stt,dict) and stt.get("status")=="filled":
                fills=stt.get("fills",[])
                tb,tq,fee=totals_from_fills_eur(fills)
                if tb>0:
                    return {"fills":fills,"status":"filled","maker":True}, "filled_maker"
            time.sleep(1.2)
        cancel_order(market, order_id)

    # Taker فوري
    res = place_market("sell", market, amount=amount)
    if isinstance(res,dict) and res.get("status")=="filled":
        return {"fills":res.get("fills",[]),"status":"filled","maker":False}, "filled_taker"
    return None, "failed"

# ========= Trading helpers =========
def open_position(base):
    global active_trade
    if _today_pnl() <= DAILY_STOP_EUR:
        send_message("⛔ توقف شراء لباقي اليوم."); return
    with lk:
        if active_trade: return
    market=f"{base}-EUR"

    price_now=fetch_price_ws_first(market) or 0.0
    # فلتر OB متكيف بسيط
    if price_now<0.05:   min_bid,max_spread,req_imb=20.0, 320.0, 0.25
    elif price_now<0.5:  min_bid,max_spread,req_imb=40.0, 220.0, 0.35
    else:                min_bid,max_spread,req_imb=60.0, 200.0, 0.40
    ok,why,feats=orderbook_guard(market, min_bid_eur=min_bid, req_imb=req_imb, max_spread_bp=max_spread)
    if not ok:
        dbg(f"Skip {base}-EUR OB filter: spr={feats.get('spread_bp',0):.0f}bp imb={feats.get('imb',0):.2f}")
        r.setex(f"cooldown:{base}", COOLDOWN_SEC, 1); return

    eur=get_eur_available()-EUR_RESERVE
    eur=round(max(0.0, eur),2)
    if eur<BUY_MIN_EUR: return
    res,how = maker_to_taker_buy(market, eur)
    if not res: return
    tb,tq,fee=totals_from_fills_eur(res.get("fills",[]))
    avg_incl=(tq+fee)/tb if tb>0 else 0.0
    trade={
        "symbol":market,"entry":avg_incl,"amount":tb,
        "cost_eur":tq+fee,"buy_fee_eur":fee,"opened_at":time.time(),
        "maker_entry": res.get("maker",False), "peak_pct":0.0,
        "sl_dyn": SL_START, "last_eval": 0.0
    }
    with lk:
        active_trade = trade
        executed_trades.append(trade.copy())
        r.set("nems:active_trade", json.dumps(active_trade))
        r.rpush("nems:executed_trades", json.dumps(trade))
    style = "Maker" if trade["maker_entry"] else "Taker"
    send_message(f"✅ شراء {base} | €{eur:.2f} | {style}-first→{'Taker' if not trade['maker_entry'] else 'Maker'} دخول @€{avg_incl:.6f}")

def close_position(reason=""):
    global active_trade
    with lk:
        tr = dict(active_trade) if active_trade else None
    if not tr: return
    market=tr["symbol"]; base=market.replace("-EUR","")
    amt=float(tr["amount"])
    # خروج Maker أولًا (رسوم أقل) إلا إذا كان SL/انهيار سريع
    maker_ok = not reason.startswith("SL") and not reason.startswith("Crash")
    if maker_ok:
        res,how = maker_to_taker_sell(market, amt)
    else:
        res = place_market("sell", market, amount=amt); how="taker_direct"
        if not (isinstance(res,dict) and res.get("status")=="filled"):
            # محاولة أخيرة Maker
            res,how = maker_to_taker_sell(market, amt)

    if isinstance(res,dict) and res.get("status")=="filled":
        tb,tq,fee=totals_from_fills_eur(res.get("fills",[]))
        proceeds=tq-fee
        orig_cost= float(tr.get("cost_eur", tr["entry"]*amt))
        pnl_eur=proceeds-orig_cost
        pnl_pct=(proceeds/orig_cost - 1.0)*100.0 if orig_cost>0 else 0.0
        _accum_realized(pnl_eur)
        with lk:
            active_trade={}
            r.delete("nems:active_trade")
            for i in range(len(executed_trades)-1,-1,-1):
                t=executed_trades[i]
                if t["symbol"]==market and "exit_eur" not in t:
                    t.update({"exit_eur":proceeds,"sell_fee_eur":fee,"pnl_eur":pnl_eur,"pnl_pct":pnl_pct,"exit_time":time.time(),"exit_reason":reason})
                    break
            r.delete("nems:executed_trades")
            for t in executed_trades: r.rpush("nems:executed_trades", json.dumps(t))
        style = "Maker" if (isinstance(res,dict) and res.get("maker",False)) else ("Maker" if how=="filled_maker" else "Taker")
        send_message(f"💰 بيع {base} | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%) — {reason} | {style}")
        r.setex(f"cooldown:{base}", COOLDOWN_SEC, 1)

# ========= Monitor / Exit logic =========
def monitor_loop():
    while True:
        try:
            with lk: tr = dict(active_trade) if active_trade else None
            if not tr:
                time.sleep(0.4); continue
            m=tr["symbol"]; entry=float(tr["entry"])
            cur=fetch_price_ws_first(m)
            if not cur: time.sleep(0.3); continue

            pnl=((cur-entry)/entry)*100.0
            tr["peak_pct"]=max(tr.get("peak_pct",0.0), pnl)

            # SL سلّمي بسيط
            inc=int(max(0.0, pnl)//1)
            dyn = max(tr.get("sl_dyn", SL_START), SL_START + inc*SL_STEP)
            tr["sl_dyn"]=dyn

            # r30/r90 تقديري من هيستوري الصفقه
            if "hist" not in tr: tr["hist"]=deque(maxlen=600)
            tr["hist"].append((time.time(), cur))
            cutoff=time.time()-120
            while tr["hist"] and tr["hist"][0][0]<cutoff: tr["hist"].popleft()
            hist=tr["hist"]
            if hist:
                now_ts=hist[-1][0]; p30=p90=None
                for ts,p in hist:
                    age=now_ts-ts
                    if p30 is None and age>=30: p30=p
                    if p90 is None and age>=90: p90=p
                basep=hist[0][1]
                p30=p30 or basep; p90=p90 or basep
                r30=(cur/p30-1.0)*100.0 if p30>0 else 0.0
                r90=(cur/p90-1.0)*100.0 if p90>0 else 0.0
            else:
                r30=r90=0.0

            # هدف سريع حسب قوة OB/الزخم
            ob_ok,_,obf = orderbook_guard(m)
            spread=obf.get("spread_bp",999.0) if obf else 999.0
            imb=obf.get("imb",0.0) if obf else 0.0
            weak_env = (not ob_ok) or (spread>160.0) or (imb<0.95) or (r30<0 and r90<0)
            tp = TP_WEAK if weak_env else TP_GOOD
            if pnl>=tp and tr.get("peak_pct",0.0)<(tp+0.4) and (time.time()-tr.get("opened_at",0))>=HOLD_MIN_SEC:
                close_position(f"TP {tp:.2f}%"); continue

            # Trailing من القمة
            peak=tr.get("peak_pct",0.0)
            if peak>=TRAIL_START:
                give=min(TRAIL_GIVEBACK_CAP, TRAIL_GIVEBACK_RATIO*peak)
                desired=peak-give
                if desired>tr.get("sl_dyn",SL_START): tr["sl_dyn"]=desired
                drop=peak - pnl
                if drop>=give and (r30<=-0.25 or r90<=0.0):
                    close_position(f"Giveback {drop:.2f}%"); continue

            # SL
            if pnl<=tr.get("sl_dyn",SL_START):
                # لا نبيع فورًا إن كان قريبًا جدًا بعد الدخول (حماية)
                if (time.time()-tr.get("opened_at",0))>=HOLD_MIN_SEC:
                    close_position(f"SL {tr['sl_dyn']:.2f}%"); continue

            # Time-stop الذكي (عند خمول واضح)
            age=time.time()-tr.get("opened_at",0)
            if age>=TIME_STOP_MIN and TIME_STOP_NEUTRAL_LO<=pnl<=TIME_STOP_NEUTRAL_HI and r90<=0.0:
                close_position(f"Time-stop {int(age//60)}m"); continue

            # احفظ الحالة
            with lk: active_trade.update(tr)
            time.sleep(0.25)
        except Exception as e:
            print("monitor err:", e); time.sleep(1)
Thread(target=monitor_loop, daemon=True).start()

# ========= Engine + Replacement =========
def best_candidate():
    refresh_watchlist()
    watch=list(WATCHLIST_MARKETS)
    if not watch: return None
    now=time.time(); best=None
    for m in watch:
        p=fetch_price_ws_first(m)
        if not p: continue
        _update_hist(m, now, p)
        sc,r15,r30,r60,acc,spr,imb,snp = score_exploder(m, p)
        if spr>THRESH_SPREAD_BP_MAX or imb<THRESH_IMB_MIN: 
            dbg(f"Skip {m} OB filter: spr={spr:.0f}bp imb={imb:.2f}")
            continue
        base=m.replace("-EUR","")
        if r.exists(f"cooldown:{base}"): continue
        cand=(sc,m,r15,r30,r60,acc,spr,imb,snp)
        if (best is None) or (cand[0]>best[0]): best=cand
    return best

def engine_loop():
    while True:
        try:
            if not (enabled and auto_enabled): time.sleep(1); continue
            if _today_pnl() <= DAILY_STOP_EUR: time.sleep(3); continue

            best=best_candidate()
            if best:
                score,m,r15,r30,r60,acc,spr,imb,sniper = best
                trigger = (score>=AUTO_THRESHOLD) or sniper

                with lk:
                    have = bool(active_trade)
                    cur_sym = active_trade.get("symbol") if active_trade else None
                if not have and trigger:
                    open_position(m.replace("-EUR",""))

                # Replacement محافظ
                elif have and trigger:
                    with lk: tr=dict(active_trade)
                    age = time.time()-tr.get("opened_at",0)
                    if age < REPLACE_MIN_HOLD_SEC: 
                        time.sleep(ENGINE_INTERVAL_SEC); continue
                    if m != tr["symbol"]:
                        # تقدير "ميزة" بدائية: r30 الجديد - r30 الحالي، + فارق score
                        cur_p = fetch_price_ws_first(tr["symbol"]) or tr["entry"]
                        pnl_now = (cur_p/tr["entry"]-1.0)*100.0
                        cur_sc,cr15,cr30,cr60,cacc,cspr,cimb,csnp = score_exploder(tr["symbol"], cur_p)
                        gap = score - cur_sc
                        # كلفة الرسوم التقريبية round-trip
                        fee_cost_pct = (FEE_TAKER_BPS + FEE_MAKER_BPS)/100.0
                        min_edge = max(REPLACE_MIN_EDGE_PCT, fee_cost_pct*1.5)
                        if (gap>=REPLACE_GAP_SCORE and r30>0 and acc>0 and (pnl_now<=0.0 or r30>cr30)):
                            close_position("Replacement")
                            open_position(m.replace("-EUR",""))

            time.sleep(ENGINE_INTERVAL_SEC)
        except Exception as e:
            print("engine err:", e); time.sleep(1)
Thread(target=engine_loop, daemon=True).start()

# ========= Summary =========
def build_summary():
    lines=[]; now=time.time()
    with lk: tr=dict(active_trade) if active_trade else {}; ex=list(executed_trades)
    if tr:
        cur=fetch_price_ws_first(tr["symbol"]) or tr["entry"]
        pnl=((cur-tr["entry"])/tr["entry"])*100.0
        peak=float(tr.get("peak_pct",0.0)); dyn=float(tr.get("sl_dyn",SL_START))
        base=tr["symbol"].replace("-EUR","")
        lines.append("📌 الصفقة النشطة (1):")
        lines.append(f"1. {base}: {pnl:+.2f}% | Peak {peak:.2f}% | SL {dyn:.2f}%")
        val=tr["amount"]*cur; cost=float(tr.get("cost_eur", tr["amount"]*tr["entry"]))
        fl=val-cost; pct=((val/cost)-1.0)*100.0 if cost>0 else 0.0
        lines.append(f"💼 قيمة الصفقة: €{val:.2f} | عائم: {fl:+.2f}€ ({pct:+.2f}%)")
    else:
        lines.append("📌 لا صفقات نشطة.")

    since=float(r.get(SINCE_RESET_KEY) or 0.0)
    closed=[t for t in ex if "pnl_eur" in t and "exit_time" in t and float(t["exit_time"])>=since]
    closed.sort(key=lambda x: float(x["exit_time"]))
    wins=sum(1 for t in closed if float(t["pnl_eur"])>=0); losses=len(closed)-wins
    pnl_eur=sum(float(t["pnl_eur"]) for t in closed)
    avg_eur=(pnl_eur/len(closed)) if closed else 0.0
    avg_pct=(sum(float(t.get("pnl_pct",0)) for t in closed)/len(closed)) if closed else 0.0
    lines.append("\n📊 صفقات مكتملة منذ Reset:")
    if not closed: lines.append("• لا يوجد.")
    else:
        lines.append(f"• العدد: {len(closed)} | محققة: {pnl_eur:+.2f}€ | متوسط/صفقة: {avg_eur:+.2f}€ ({avg_pct:+.2f}%)")
        lines.append(f"• فوز/خسارة: {wins}/{losses}")
        lines.append("🧾 أحدث الصفقات:")
        for t in sorted(closed, key=lambda x: float(x["exit_time"]), reverse=True)[:8]:
            base=t["symbol"].replace("-EUR","")
            lines.append(f"- {base}: {float(t['pnl_eur']):+,.2f}€ ({float(t.get('pnl_pct',0)):+.2f}%) — {t.get('exit_reason','')}")

    lines.append(f"\n⛔ حد اليوم: {_today_pnl():+.2f}€ / {DAILY_STOP_EUR:+.2f}€")
    return "\n".join(lines)

def send_chunks(txt, chunk=3300):
    if not txt: return
    buf=""
    for line in txt.splitlines(True):
        if len(buf)+len(line)>chunk:
            send_message(buf); buf=""
        buf+=line
    if buf: send_message(buf)

# ========= Telegram =========
@app.route("/", methods=["POST"])
def webhook():
    global enabled, auto_enabled, debug_tg
    data=request.get_json(silent=True) or {}
    text=(data.get("message",{}).get("text") or data.get("text") or "").strip()
    if not text: return "ok"
    low=text.lower()

    def has(*k): return any(x in low for x in k)
    def starts(*k): return any(low.startswith(x) for x in k)

    if has("start","تشغيل","ابدأ"): enabled=True; auto_enabled=True; send_message("✅ تم التفعيل."); return "ok"
    if has("stop","قف","ايقاف","إيقاف"): enabled=False; auto_enabled=False; send_message("🛑 تم الإيقاف."); return "ok"
    if has("summary","ملخص","الملخص"): send_chunks(build_summary()); return "ok"
    if has("reset","انسى","أنسى"):
        with lk:
            active_trade.clear()
            executed_trades.clear()
            r.delete("nems:active_trade"); r.delete("nems:executed_trades")
            r.set(SINCE_RESET_KEY, time.time())
        send_message("🧠 Reset."); return "ok"
    if has("settings","اعدادات","إعدادات"):
        send_message(
            "⚙️ "
            f"thr={AUTO_THRESHOLD}, topN={TOPN_WATCH}, interval={ENGINE_INTERVAL_SEC}s | "
            f"spread≤{THRESH_SPREAD_BP_MAX}bp, imb≥{THRESH_IMB_MIN} | "
            f"TP={TP_WEAK}/{TP_GOOD}% | SL={SL_START}/{SL_STEP} | daily={DAILY_STOP_EUR}€ | "
            f"makerWait={MAKER_REQUOTE_SEC}s×{MAKER_MAX_REQUOTES}+{MAKER_WAIT_AFTER_LAST}s"
        ); return "ok"
    if has("debug on","debug: on","dbg on"):
        debug_tg=True; send_message("🐞 Debug TG: ON", force=True); return "ok"
    if has("debug off","debug: off","dbg off"):
        debug_tg=False; send_message("🐞 Debug TG: OFF", force=True); return "ok"
    if starts("buy","اشتري","إشتري"):
        try:
            sym=re.search(r"[A-Za-z0-9\-]+", text).group(0).upper()
            if "-" in sym: sym=sym.split("-")[0]
            if sym.endswith("EUR") and len(sym)>3: sym=sym[:-3]
            sym=re.sub(r"[^A-Z0-9]","", sym)
        except Exception:
            send_message("❌ الصيغة: buy ADA"); return "ok"
        open_position(sym); return "ok"
    if has("flat","اغلق","سكر","بيع الكل"):
        close_position("Manual"); return "ok"
    if has("balance","الرصيد","رصيد"):
        bals=bv_request("GET","/balance")
        if not isinstance(bals,list): send_message("❌ تعذر جلب الرصيد."); return "ok"
        eur=sum(float(b.get("available",0))+float(b.get("inOrder",0)) for b in bals if b.get("symbol")=="EUR")
        total=eur
        with lk: ex=list(executed_trades)
        winners,losers=[],[]
        for b in bals:
            sym=b.get("symbol")
            if sym=="EUR": continue
            qty=float(b.get("available",0))+float(b.get("inOrder",0))
            if qty<0.0001: continue
            pair=f"{sym}-EUR"; price=fetch_price_ws_first(pair)
            if price is None: continue
            total+=qty*price
            entry=None
            if active_trade and active_trade.get("symbol")==pair:
                entry=active_trade.get("entry")
            else:
                for t in reversed(ex):
                    if t.get("symbol")==pair: entry=t.get("entry"); break
            if entry:
                pnl=((price-entry)/entry)*100.0
                line=f"{sym}: {qty:.4f} @ €{price:.4f} → {pnl:+.2f}%"
                (winners if pnl>=0 else losers).append(line)
        lines=[f"💰 الرصيد: €{total:.2f}"]
        if winners: lines.append("\n📈 رابحين:\n"+"\n".join(winners))
        if losers:  lines.append("\n📉 خاسرين:\n"+"\n".join(losers))
        if not winners and not losers: lines.append("\n🚫 لا مراكز.")
        send_message("\n".join(lines)); return "ok"

    return "ok"

# ========= Load state =========
try:
    at=r.get("nems:active_trade"); 
    if at: active_trade=json.loads(at)
    et=r.lrange("nems:executed_trades",0,-1)
    executed_trades=[json.loads(t) for t in et]
    if not r.exists(SINCE_RESET_KEY): r.set(SINCE_RESET_KEY, 0)
except Exception as e: print("state load err:", e)

# ========= Local run =========
if __name__=="__main__" and RUN_LOCAL:
    app.run(host="0.0.0.0", port=5000)