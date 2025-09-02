# -*- coding: utf-8 -*-
"""
Nems — ULTRA NITRO (Single Position, Hybrid Maker→Taker) — Bitvavo EUR
- صفقة واحدة فقط
- شراء بكامل الرصيد المتاح
- تنفيذ هجيني: نجرب Maker (limit postOnly + reprice) لثوانٍ، ثم نحول المتبقي لتاكر
- Replacement: لو فرصة أقوى بوضوح، حاول نخرج (Maker أولاً) وندخل الجديد (Maker أولاً) بأقل رسوم/انزلاق
- بدون تجزئة مبلغ، بدون سلّميات، بدون TP صريح. قفل Breakeven+ مبكر + SL ديناميكي + Time-Stop قصير.

تنبيه: مخاطرة عالية. عدّل الرسوم/الحدود حسب حسابك.
"""

import os, re, time, json, traceback, statistics as st
import requests, redis
from threading import Thread, Lock
from collections import deque
from uuid import uuid4
from flask import Flask, request
from dotenv import load_dotenv
import websocket

# ===== Boot / ENV =====
load_dotenv()
app = Flask(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHAT_ID     = os.getenv("CHAT_ID")
API_KEY     = os.getenv("BITVAVO_API_KEY")
API_SECRET  = os.getenv("BITVAVO_API_SECRET")
REDIS_URL   = os.getenv("REDIS_URL")
RUN_LOCAL   = os.getenv("RUN_LOCAL","0")=="1"

r  = redis.from_url(REDIS_URL) if REDIS_URL else redis.Redis()
lk = Lock()

BASE_URL    = "https://api.bitvavo.com/v2"
WS_URL      = "wss://ws.bitvavo.com/v2/"

# ===== Core Settings =====
MAX_TRADES              = 1              # صفقة واحدة فقط
ENGINE_INTERVAL_SEC     = 0.25
TOPN_WATCH              = 60

AUTO_THRESHOLD          = 16.0           # عتبة دخول عامة
REPLACE_EXTRA           = 2.0            # لازم الأفضلية تزيد عن العتبة بهالقدر على الأقل
MIN_TRADE_AGE_REPL_S    = 10             # أقل عمر للصفقة قبل السماح باستبدال
MAX_PNL_FOR_REPL        = 0.6            # لا نستبدل إذا الربح الحالي أعلى من هذا (ندعه يجري)

THRESH_SPREAD_BP_MAX    = 280.0
THRESH_IMB_MIN          = 0.55

DAILY_STOP_EUR          = -20.0
COOLDOWN_SEC            = 35
BAN_LOSS_PCT            = -5.0
CONSEC_LOSS_BAN         = 3
BLACKLIST_EXPIRE_SECONDS= 180

# SL / Locks / Stops
DYN_SL_START            = -3.2
DYN_SL_STEP             = 0.8
LOCK_AT_PEAK_GE         = 0.50           # قفل BE+ عند أول ذروة ≥0.5%
LOCK_EXTRA              = 0.12           # BE + 0.12% (يغطي الرسوم)
PEAK_TRIGGER            = 1.4
GIVEBACK_RATIO          = 0.55
GIVEBACK_CAP            = 1.6
TIME_STOP_MIN           = 6*60
TIME_STOP_PNL_LO        = -0.6
TIME_STOP_PNL_HI        = 0.9

# Hybrid Maker Execution
MAKER_WAIT_SEC          = 2.5
REPRICE_EVERY_SEC       = 0.5
MIN_SPREAD_FOR_MAKER_BP = 25.0           # تحتها، ماركت غالبًا أفضل

# ===== WS & caches =====
_ws_lock = Lock()
_ws_prices = {}         # market -> {price,ts}
_ws_conn=None; _ws_running=False

WATCHLIST_MARKETS=set()
_prev_watch=set()
HISTS={}
OB_CACHE={}
VOL_CACHE={}
WATCH_REFRESH_SEC=120
_last_watch=0

enabled=True; auto_enabled=True
active_trades=[]; executed_trades=[]
SINCE_RESET_KEY="nems:since_reset"

# ===== Utils =====
def send_message(text):
    try:
        if not (BOT_TOKEN and CHAT_ID):
            print("TG:", text); return
        key="dedup:"+str(abs(hash(text))%(10**12))
        if r.setnx(key,1):
            r.expire(key,60)
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          data={"chat_id":CHAT_ID,"text":text}, timeout=8)
    except Exception as e: print("TG err:", e)

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

def _today_key(): return time.strftime("pnl:%Y%m%d", time.gmtime())
def _accum_realized(p):
    try: r.incrbyfloat(_today_key(), float(p)); r.expire(_today_key(), 3*24*3600)
    except Exception: pass
def _today_pnl():
    try: return float(r.get(_today_key()) or 0.0)
    except Exception: return 0.0

# ===== WS =====
def _ws_sub_payload(mkts): return {"action":"subscribe","channels":[{"name":"ticker","markets":mkts}]}
def _ws_on_open(ws):
    try:
        mkts=sorted(WATCHLIST_MARKETS | {t["symbol"] for t in active_trades})
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
def _ws_on_close(ws,c,r): 
    global _ws_running; _ws_running=False; print("WS closed:",c,r)
def _ws_thread():
    global _ws_conn,_ws_running
    while True:
        try:
            _ws_running=True
            _ws_conn=websocket.WebSocketApp(WS_URL, on_open=_ws_on_open,
                                            on_message=_ws_on_message,
                                            on_error=_ws_on_error, on_close=_ws_on_close)
            _ws_conn.run_forever(ping_interval=25, ping_timeout=10)
        except Exception as e:
            print("WS loop ex:", e)
        finally:
            _ws_running=False; time.sleep(2)
Thread(target=_ws_thread, daemon=True).start()

# ===== Hist / OB =====
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

def _backfill_hist_1m(m, minutes=45):
    try:
        rows=requests.get(f"{BASE_URL}/{m}/candles?interval=1m&limit={minutes}", timeout=6).json()
        if not isinstance(rows,list): return
        for ts,o,h,l,c,v in rows[-minutes:]:
            _update_hist(m, ts/1000.0, float(c))
        p=fetch_price_ws_first(m); 
        if p: _update_hist(m, time.time(), p)
    except Exception as e: print("backfill err:", e)

def fetch_orderbook(m, ttl=1.6):
    now=time.time(); rec=OB_CACHE.get(m)
    if rec and (now-rec["ts"])<ttl: return rec["data"]
    try:
        j=requests.get(f"{BASE_URL}/{m}/book", timeout=5).json()
        if j and j.get("bids") and j.get("asks"):
            OB_CACHE[m]={"data":j,"ts":now}; return j
    except Exception: pass
    return None

def orderbook_guard(market, min_bid_eur=40.0, req_imb=THRESH_IMB_MIN, max_spread_bp=THRESH_SPREAD_BP_MAX, depth_used=3):
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

# ===== Watchlist =====
_ticker24_cache={"ts":0,"rows":[]}
def _t_rows(): return _ticker24_cache.get("rows",[])
def _t_set(ts,rows): _ticker24_cache.update({"ts":ts,"rows":rows})
def _t_fresh():
    now=time.time()
    if now-_ticker24_cache.get("ts",0)<60: return _t_rows()
    try:
        rows=requests.get(f"{BASE_URL}/ticker/24h", timeout=8).json()
        if isinstance(rows,list): _t_set(now,rows); return rows
    except Exception: pass
    return _t_rows()

def _score_market_volvol(market):
    now=time.time()
    rec=VOL_CACHE.get(market)
    if rec and (now-rec[0])<120: return rec[1]
    try:
        cs=requests.get(f"{BASE_URL}/{market}/candles?interval=1m&limit=20", timeout=4).json()
        closes=[float(c[4]) for c in cs if isinstance(c,list) and len(c)>=5]
        if len(closes)<6: score=0.0
        else:
            rets=[closes[i]/closes[i-1]-1.0 for i in range(1,len(closes))]
            v5=abs(sum(rets[-5:]))*100.0
            stdv=(st.pstdev(rets)*100.0) if len(rets)>2 else 0.0
            score=0.6*stdv + 0.4*v5
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
    for m in newly: _backfill_hist_1m(m, minutes=45)
    _prev_watch=set(new)

# ===== Scoring =====
def score_exploder(market, price_now):
    r15,r30,r60,_ = _mom_metrics_symbol(market, price_now)
    accel = r30 - r60
    ok,_,feats = orderbook_guard(market, max_spread_bp=THRESH_SPREAD_BP_MAX, req_imb=THRESH_IMB_MIN)
    spread = feats.get("spread_bp", 999.0); imb=feats.get("imb", 0.0)

    mom_pts = max(0.0, min(70.0, 2.2*r15 + 2.2*r30 + 0.35*r60))
    acc_pts = max(0.0, min(24.0, 9.0*max(0.0, accel)))
    ob_pts  = 0.0
    if ok:
        if spread <= 180.0: ob_pts += max(0.0, min(10.0, (180.0-spread)*0.07))
        if imb >= 0.95:     ob_pts += max(0.0, min(10.0, (imb-0.95)*12.0))
        if spread <= 90.0:  ob_pts += 3.0
        if imb >= 1.20:     ob_pts += 4.0

    score = max(0.0, min(100.0, mom_pts + acc_pts + ob_pts))
    sniper = (r15>=0.08 and accel>=0.10 and spread<=180.0 and imb>=0.90)
    return score, r15, r30, r60, accel, spread, imb, sniper

# ===== Orders (Hybrid Maker→Taker) =====
def place_market(side, market, amount=None, amount_quote=None):
    body={"market":market,"side":side,"orderType":"market","clientOrderId":str(uuid4())}
    if side=="buy": body["amountQuote"]=f"{amount_quote:.2f}"
    else: body["amount"]=f"{amount:.10f}"
    return bv_request("POST","/order", body)

def place_limit(side, market, price, amount=None, amount_quote=None, post_only=True):
    body={"market":market,"side":side,"orderType":"limit","price":f"{price:.10f}",
          "clientOrderId":str(uuid4())}
    if amount is not None: body["amount"]=f"{amount:.10f}"
    if amount_quote is not None: body["amountQuote"]=f"{amount_quote:.2f}"
    body["postOnly"] = True if post_only else False
    return bv_request("POST","/order", body)

def cancel_by_cid(cid):
    try: return bv_request("DELETE", f"/order?clientOrderId={cid}")
    except Exception: return {"error":"cancel_failed"}

def hybrid_buy_full(market, eur_to_spend):
    """يحاول Maker لثوانٍ ثم يحول الباقي لتاكر. يرجع (amount_base, cost_eur, fee_eur)."""
    ob = fetch_orderbook(market)
    if not ob: return 0.0,0.0,0.0
    bid=float(ob["bids"][0][0]); ask=float(ob["asks"][0][0]); mid=(bid+ask)/2.0
    spread_bp=(ask-bid)/((ask+bid)/2.0)*10000.0
    try_maker = spread_bp >= MIN_SPREAD_FOR_MAKER_BP

    got=0.0; spent=0.0; fee=0.0
    if try_maker:
        px = max(bid, min(mid, ask*0.999))
        cid = str(uuid4())
        res = bv_request("POST","/order",{
            "market":market,"side":"buy","orderType":"limit","price":f"{px:.10f}",
            "amountQuote":f"{eur_to_spend:.2f}","clientOrderId":cid,"postOnly":True
        })
        t0=time.time()
        while time.time()-t0 < MAKER_WAIT_SEC:
            time.sleep(REPRICE_EVERY_SEC)
            ob=fetch_orderbook(market)
            if not ob: break
            nb=float(ob["bids"][0][0]); na=float(ob["asks"][0][0]); nmid=(nb+na)/2.0
            npx=max(nb, min(nmid, na*0.999))
            if abs(npx-px)/px > 0.0001:
                cancel_by_cid(cid)
                cid=str(uuid4())
                res=bv_request("POST","/order",{
                    "market":market,"side":"buy","orderType":"limit","price":f"{npx:.10f}",
                    "amountQuote":f"{eur_to_spend-spent:.2f}","clientOrderId":cid,"postOnly":True
                })
                px=npx
        # ما نفترضش أنها اتعبّت بالكامل؛ نعالج المتبقي:
        # Bitvavo يرجّع fills فقط عند "filled" غالباً؛ سنكمل بالباقي ماركت
    # المتبقي → ماركت
    resM = place_market("buy", market, amount_quote=max(0.0, eur_to_spend-spent) if try_maker else eur_to_spend)
    if not (isinstance(resM,dict) and resM.get("status")=="filled"):
        return 0.0,0.0,0.0
    fills = resM.get("fills",[])
    for f in fills:
        amt=float(f["amount"]); pr=float(f["price"]); fe=float(f.get("fee",0) or 0)
        got+=amt; spent+=amt*pr+fe; fee+=fe
    return got, spent, fee

def hybrid_sell_all(market, amount):
    """يحاول Maker على الـask ثم يتحول لتاكر للباقي. يرجع (proceeds_eur, fee_eur)."""
    ob=fetch_orderbook(market)
    if not ob: return 0.0,0.0
    bid=float(ob["bids"][0][0]); ask=float(ob["asks"][0][0]); mid=(bid+ask)/2.0
    spread_bp=(ask-bid)/((ask+bid)/2.0)*10000.0
    try_maker = spread_bp >= MIN_SPREAD_FOR_MAKER_BP

    proceeds=0.0; fee=0.0
    if try_maker:
        px = min(ask, max(mid, bid*1.001))
        cid=str(uuid4())
        res=bv_request("POST","/order",{
            "market":market,"side":"sell","orderType":"limit","price":f"{px:.10f}",
            "amount":f"{amount:.10f}","clientOrderId":cid,"postOnly":True
        })
        t0=time.time()
        while time.time()-t0 < MAKER_WAIT_SEC:
            time.sleep(REPRICE_EVERY_SEC)
            ob=fetch_orderbook(market)
            if not ob: break
            nb=float(ob["bids"][0][0]); na=float(ob["asks"][0][0]); nmid=(nb+na)/2.0
            npx=min(na, max(nmid, nb*1.001))
            if abs(npx-px)/px > 0.0001:
                cancel_by_cid(cid)
                cid=str(uuid4())
                res=bv_request("POST","/order",{
                    "market":market,"side":"sell","orderType":"limit","price":f"{npx:.10f}",
                    "amount":f"{amount:.10f}","clientOrderId":cid,"postOnly":True
                })
                px=npx

    # أي متبقّي → ماركت (الأبسط: كل الكمية)
    resM=place_market("sell", market, amount=amount)
    if not (isinstance(resM,dict) and resM.get("status")=="filled"):
        return 0.0,0.0
    for f in resM.get("fills",[]):
        amt=float(f["amount"]); pr=float(f["price"]); fe=float(f.get("fee",0) or 0)
        proceeds+=amt*pr - fe; fee+=fe
    return proceeds, fee

# ===== Trading =====
def totals_from_fills_eur(fills):
    tb=tq=fee=0.0
    for f in (fills or []):
        amt=float(f["amount"]); price=float(f["price"]); fe=float(f.get("fee",0) or 0)
        tb+=amt; tq+=amt*price; fee+=fe
    return tb,tq,fee

def buy_full(base_symbol):
    base=base_symbol.upper().strip()
    if _today_pnl() <= DAILY_STOP_EUR:
        send_message("⛔ توقف شراء لباقي اليوم."); return
    if r.exists(f"ban24:{base}") or r.exists(f"cooldown:{base}"):
        return
    market=f"{base}-EUR"

    with lk:
        if any(t["symbol"]==market for t in active_trades): return
        if len(active_trades)>=MAX_TRADES: return

    eur=get_eur_available()
    if eur<5.0: return

    # حراسة دفتر الأوامر
    price_now=fetch_price_ws_first(market) or 0.0
    min_bid=40.0 if price_now>=0.2 else (25.0 if price_now>=0.02 else 15.0)
    ok,why,feats=orderbook_guard(market, min_bid_eur=min_bid)
    if not ok:
        r.setex(f"cooldown:{base}", COOLDOWN_SEC, 1); return

    amt_base, cost_eur, fee_eur = hybrid_buy_full(market, eur)
    if amt_base<=0 or cost_eur<=0: return

    avg_incl = cost_eur/amt_base
    tr={"symbol":market,"entry":avg_incl,"amount":amt_base,"cost_eur":cost_eur,"buy_fee_eur":fee_eur,
        "opened_at":time.time(),"peak_pct":0.0,"sl_dyn":DYN_SL_START,"last_exit_try":0.0,"exit_in_progress":False}
    with lk:
        active_trades.append(tr)
        executed_trades.append(tr.copy())
        r.set("nems:active_trades", json.dumps(active_trades))
        r.rpush("nems:executed_trades", json.dumps(tr))
    with _ws_lock: WATCHLIST_MARKETS.add(market)
    send_message(f"✅ شراء {base} | كامل الرصيد | SL {DYN_SL_START:.1f}%")

def sell_all(tr):
    market=tr["symbol"]; base=market.replace("-EUR","")
    amt=float(tr.get("amount",0) or 0)
    if amt<=0: return
    proceeds, fee = hybrid_sell_all(market, amt)
    if proceeds<=0: return

    orig_cost=float(tr.get("cost_eur", tr["entry"]*amt))
    pnl_eur=proceeds-orig_cost
    pnl_pct=(proceeds/orig_cost-1.0)*100.0 if orig_cost>0 else 0.0
    _accum_realized(pnl_eur)

    try:
        if pnl_pct <= BAN_LOSS_PCT: r.setex(f"ban24:{base}", 24*3600, 1)
        k=f"lossstreak:{base}"
        if pnl_eur<0:
            st=int(r.incr(k)); r.expire(k, 24*3600)
            if st>=CONSEC_LOSS_BAN: r.setex(f"ban24:{base}", 24*3600, 1)
        else: r.delete(k)
    except Exception: pass

    with lk:
        try: active_trades.remove(tr)
        except ValueError: pass
        r.set("nems:active_trades", json.dumps(active_trades))
        for i in range(len(executed_trades)-1,-1,-1):
            t=executed_trades[i]
            if t["symbol"]==market and "exit_eur" not in t:
                t.update({"exit_eur":proceeds,"sell_fee_eur":fee,"pnl_eur":pnl_eur,"pnl_pct":pnl_pct,"exit_time":time.time()})
                break
        r.delete("nems:executed_trades")
        for t in executed_trades: r.rpush("nems:executed_trades", json.dumps(t))
    r.setex(f"cooldown:{base}", COOLDOWN_SEC, 1)
    send_message(f"💰 بيع {base} | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%)")

# ===== Monitor / Exits =====
def _update_trade_hist(tr, ts, price):
    if "hist" not in tr: tr["hist"]=deque(maxlen=600)
    tr["hist"].append((ts,price))
    cutoff=ts-120
    while tr["hist"] and tr["hist"][0][0]<cutoff: tr["hist"].popleft()

def monitor_loop():
    while True:
        try:
            with lk: snap=list(active_trades)
            now=time.time()
            for tr in snap:
                m=tr["symbol"]; entry=float(tr["entry"])
                cur=fetch_price_ws_first(m)
                if not cur: continue
                _update_trade_hist(tr, now, cur)

                pnl=((cur-entry)/entry)*100.0
                tr["peak_pct"]=max(tr.get("peak_pct",0.0), pnl)

                # SL سلمي + قفل BE+
                inc=int(max(0.0,pnl)//1); base=DYN_SL_START + inc*DYN_SL_STEP
                tr["sl_dyn"]=max(tr.get("sl_dyn",DYN_SL_START), base)

                peak=tr.get("peak_pct",0.0)
                if peak >= LOCK_AT_PEAK_GE:
                    be_lock = max(0.0, LOCK_EXTRA)
                    if tr["sl_dyn"] < be_lock:
                        tr["sl_dyn"] = be_lock

                # Giveback أخف من القمة
                if peak >= PEAK_TRIGGER:
                    give=min(GIVEBACK_CAP, GIVEBACK_RATIO*peak)
                    desired=peak-give
                    if desired>tr.get("sl_dyn",DYN_SL_START): tr["sl_dyn"]=desired

                # ضرب SL
                if pnl <= tr.get("sl_dyn",DYN_SL_START):
                    tr["exit_in_progress"]=True; tr["last_exit_try"]=now
                    sell_all(tr); tr["exit_in_progress"]=False; continue

                # Time-stop
                age=now - tr.get("opened_at",now)
                if age>=TIME_STOP_MIN and TIME_STOP_PNL_LO<=pnl<=TIME_STOP_PNL_HI:
                    tr["exit_in_progress"]=True; tr["last_exit_try"]=now
                    sell_all(tr); tr["exit_in_progress"]=False; continue

            time.sleep(0.18)
        except Exception as e:
            print("monitor err:", e); time.sleep(1)
Thread(target=monitor_loop, daemon=True).start()

# ===== Engine + Replacement =====
def weakest_open_trade():   # مع صفقة واحدة يُستخدم للمرونة فقط
    with lk: return active_trades[0] if active_trades else None

def engine_loop():
    global WATCHLIST_MARKETS
    while True:
        try:
            if not (enabled and auto_enabled): time.sleep(1); continue
            if _today_pnl() <= DAILY_STOP_EUR: time.sleep(3); continue

            refresh_watchlist()
            watch=list(WATCHLIST_MARKETS)
            if not watch: time.sleep(0.6); continue

            now=time.time(); best=None
            for m in watch:
                p=fetch_price_ws_first(m)
                if not p: continue
                _update_hist(m, now, p)
                sc,r15,r30,r60,acc,spr,imb,snp = score_exploder(m, p)
                if spr>THRESH_SPREAD_BP_MAX or imb<THRESH_IMB_MIN: continue
                base=m.replace("-EUR","")
                if r.exists(f"ban24:{base}") or r.exists(f"cooldown:{base}"): continue
                cand=(sc,m,snp)
                if (best is None) or (cand[0]>best[0]): best=cand

            if not best:
                time.sleep(ENGINE_INTERVAL_SEC); continue

            score, mkt, sniper = best
            base = mkt.replace("-EUR","")
            trigger = (score>=AUTO_THRESHOLD) or sniper

            with lk:
                have_pos = len(active_trades)>0

            if not have_pos and trigger:
                buy_full(base)

            # Replacement: إن وُجدت صفقة مفتوحة وكانت الفرصة أقوى بوضوح
            if have_pos and trigger:
                tr = weakest_open_trade()
                age = time.time() - tr.get("opened_at",0)
                cur = fetch_price_ws_first(tr["symbol"]) or tr["entry"]
                pnl = (cur/tr["entry"]-1.0)*100.0
                if age>=MIN_TRADE_AGE_REPL_S and pnl <= MAX_PNL_FOR_REPL and score >= (AUTO_THRESHOLD+REPLACE_EXTRA):
                    # خروج ثم دخول جديد — كلاهما Hybrid لتقليل الرسوم
                    send_message("🔄 Replacement: فرصة أقوى → تبديل المركز")
                    sell_all(tr)
                    buy_full(base)

            time.sleep(ENGINE_INTERVAL_SEC)
        except Exception as e:
            print("engine err:", e); time.sleep(1)
Thread(target=engine_loop, daemon=True).start()

# ===== Summary & Telegram =====
def build_summary():
    lines=[]; now=time.time()
    with lk: act=list(active_trades); ex=list(executed_trades)
    if act:
        t=act[0]
        sym=t["symbol"].replace("-EUR",""); entry=float(t["entry"]); amt=float(t["amount"])
        cur=fetch_price_ws_first(t["symbol"]) or entry
        pnl=((cur-entry)/entry)*100.0
        peak=float(t.get("peak_pct",0.0)); dyn=float(t.get("sl_dyn",DYN_SL_START))
        val=amt*cur; cost=float(t.get("cost_eur", entry*amt))
        fl=val-cost; pct=((val/cost)-1.0)*100.0 if cost>0 else 0.0
        lines.append(f"📌 الصفقة النشطة: {sym} {pnl:+.2f}% | Peak {peak:.2f}% | SL {dyn:.2f}%")
        lines.append(f"💼 قيمة: €{val:.2f} | عائم: {fl:+.2f}€ ({pct:+.2f}%)")
    else:
        lines.append("📌 لا صفقات نشطة.")

    since=float(r.get(SINCE_RESET_KEY) or 0.0)
    closed=[t for t in ex if "pnl_eur" in t and "exit_time" in t and float(t["exit_time"])>=since]
    closed.sort(key=lambda x: float(x["exit_time"]))
    wins=sum(1 for t in closed if float(t["pnl_eur"])>=0); losses=len(closed)-wins
    pnl_eur=sum(float(t["pnl_eur"]) for t in closed)
    avg_eur=(pnl_eur/len(closed)) if closed else 0.0
    avg_pct=(sum(float(t.get("pnl_pct",0)) for t in closed)/len(closed)) if closed else 0.0
    lines.append("\n📊 منذ Reset:")
    if not closed: lines.append("• لا يوجد.")
    else:
        lines.append(f"• العدد: {len(closed)} | محققة: {pnl_eur:+.2f}€ | متوسط/صفقة: {avg_eur:+.2f}€ ({avg_pct:+.2f}%)")
        lines.append(f"• فوز/خسارة: {wins}/{losses}")
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

@app.route("/", methods=["POST"])
def webhook():
    global enabled, auto_enabled
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
            active_trades.clear(); executed_trades.clear()
            r.delete("nems:active_trades"); r.delete("nems:executed_trades"); r.set(SINCE_RESET_KEY, time.time())
        send_message("🧠 Reset."); return "ok"
    if has("settings","اعدادات","إعدادات"):
        send_message(
            f"⚙️ thr={AUTO_THRESHOLD}, replace+{REPLACE_EXTRA}, topN={TOPN_WATCH}, interval={ENGINE_INTERVAL_SEC}s | "
            f"spread≤{THRESH_SPREAD_BP_MAX}bp, imb≥{THRESH_IMB_MIN} | "
            f"SL start={DYN_SL_START}% step={DYN_SL_STEP}% | BE+ @ {LOCK_AT_PEAK_GE}% (+{LOCK_EXTRA}%) | "
            f"daily={DAILY_STOP_EUR}€ | maker_wait={MAKER_WAIT_SEC}s"
        ); return "ok"
    if starts("unban","الغ حظر","الغاء حظر","إلغاء حظر"):
        try:
            sym=re.sub(r"[^A-Z0-9]","", text.split()[-1].upper())
            if r.delete(f"ban24:{sym}"): send_message(f"✅ أُلغي حظر {sym}.")
            else: send_message(f"ℹ️ لا يوجد حظر على {sym}.")
        except Exception: send_message("❌ الصيغة: unban ADA")
        return "ok"
    if starts("buy","اشتري","إشتري"):
        try:
            sym=re.search(r"[A-Za-z0-9\-]+", text).group(0).upper()
            if "-" in sym: sym=sym.split("-")[0]
            if sym.endswith("EUR") and len(sym)>3: sym=sym[:-3]
            sym=re.sub(r"[^A-Z0-9]","", sym)
        except Exception:
            send_message("❌ الصيغة: buy ADA"); return "ok"
        buy_full(sym); return "ok"
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

# ===== Load state =====
try:
    at=r.get("nems:active_trades"); 
    if at: active_trades=json.loads(at)
    et=r.lrange("nems:executed_trades",0,-1)
    executed_trades=[json.loads(t) for t in et]
    if not r.exists(SINCE_RESET_KEY): r.set(SINCE_RESET_KEY, 0)
except Exception as e: print("state load err:", e)

if __name__=="__main__" and RUN_LOCAL:
    app.run(host="0.0.0.0", port=5000)