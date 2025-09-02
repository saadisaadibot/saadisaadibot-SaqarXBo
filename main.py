# -*- coding: utf-8 -*-
"""
Nems — ULTRA ONE-SHOT (Bitvavo EUR)
• صفقة واحدة فقط – شراء بكل الرصيد.
• لا بيع إذا الصفقة غير خسرانة إلا لو ظهرت فرصة أقوى (Replacement ذكي).
• مخارج أمان فقط: SL ديناميكي + Crash + حد يومي.
"""

import os, re, time, json, traceback, statistics as st
import requests, redis
from threading import Thread, Lock
from collections import deque
from uuid import uuid4
from flask import Flask, request
from dotenv import load_dotenv
import websocket

load_dotenv()
app = Flask(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN"); CHAT_ID=os.getenv("CHAT_ID")
API_KEY     = os.getenv("BITVAVO_API_KEY"); API_SECRET=os.getenv("BITVAVO_API_SECRET")
REDIS_URL   = os.getenv("REDIS_URL"); RUN_LOCAL=os.getenv("RUN_LOCAL","0")=="1"

r  = redis.from_url(REDIS_URL) if REDIS_URL else redis.Redis()
lk = Lock()

BASE_URL = "https://api.bitvavo.com/v2"
WS_URL   = "wss://ws.bitvavo.com/v2/"

# ===== Settings =====
MAX_TRADES              = 1                 # صفقة واحدة
ENGINE_INTERVAL_SEC     = 0.25
TOPN_WATCH              = 80

AUTO_THRESHOLD          = 18.0
THRESH_SPREAD_BP_MAX    = 140.0
THRESH_IMB_MIN          = 1.00

DAILY_STOP_EUR          = -30.0
BUY_COOLDOWN_SEC        = 90
BAN_LOSS_PCT            = -6.0
CONSEC_LOSS_BAN         = 3
BLACKLIST_EXPIRE_SECONDS= 180

# SL/Crash فقط (لا TP ولا TimeStop ولا Giveback)
DYN_SL_START            = -3.6
DYN_SL_STEP             = 0.8
CRASH_DROP_PCT          = 1.2              # هبوط سريع خلال 12s + خاسرة أقل من -1.6%
CRASH_WINDOW_SEC        = 12
CRASH_EXTRA_PNL         = -1.6

# Replacement: فقط إن كانت الصفقة الحالية غير خاسرة تقريبًا
NO_CHURN_WINDOW_S       = 90               # لا تبديل قبل 90s
REPL_BETTER_DELTA       = 8.0              # لازم المرشح يتفوّق على الحالي بهذا الهامش
REPL_ALLOW_IF_PNL_GE    = -0.10            # نعتبرها غير خسرانة لو ≥ -0.10%
REPL_MIN_AGE_S          = 90

# ===== Caches/State =====
_ws_lock=Lock(); _ws_prices={}; _ws_conn=None; _ws_running=False
WATCHLIST_MARKETS=set(); _prev_watch=set()
HISTS={}; OB_CACHE={}; VOL_CACHE={}
WATCH_REFRESH_SEC=150; _last_watch=0

enabled=True; auto_enabled=True
active_trades=[]; executed_trades=[]
SINCE_RESET_KEY="nems:since_reset"

# ===== Utils =====
def send_message(text):
    try:
        if not (BOT_TOKEN and CHAT_ID): print("TG:", text); return
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
    url=f"{BASE_URL}{path}"; ts=str(int(time.time()*1000))
    body_str="" if method=="GET" else json.dumps(body or {}, separators=(',',':'))
    sig=create_sig(ts,method,f"/v2{path}",body_str)
    headers={'Bitvavo-Access-Key':API_KEY,'Bitvavo-Access-Timestamp':ts,
             'Bitvavo-Access-Signature':sig,'Bitvavo-Access-Window':'10000'}
    try:
        resp=requests.request(method,url,headers=headers,
                              json=(body or {}) if method!="GET" else None, timeout=timeout)
        return resp.json()
    except Exception as e:
        print("bv_request err:", e); return {"error":"request_failed"}

def get_eur_available()->float:
    try:
        bals=bv_request("GET","/balance")
        if isinstance(bals,list):
            for b in bals:
                if b.get("symbol")=="EUR": return max(0.0, float(b.get("available",0) or 0))
    except Exception: pass
    return 0.0

def fetch_price_ws_first(market, staleness=2.0):
    now=time.time()
    with _ws_lock: rec=_ws_prices.get(market)
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
            p=float(price)
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
    while HISTS[m]["hist"] and HISTS[m]["hist"][0][0]<cutoff: HISTS[m]["hist"].popleft()

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

def orderbook_guard(market, min_bid_eur=120.0, req_imb=THRESH_IMB_MIN, max_spread_bp=THRESH_SPREAD_BP_MAX, depth_used=3):
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

    mom_pts = max(0.0, min(70.0, 2.3*r15 + 2.3*r30 + 0.3*r60))
    acc_pts = max(0.0, min(24.0, 10.0*max(0.0, accel)))
    ob_pts  = 0.0
    if ok:
        if spread <= 120.0: ob_pts += max(0.0, min(10.0, (120.0-spread)*0.12))
        if imb >= 1.00:     ob_pts += max(0.0, min(10.0, (imb-1.00)*12.0))
        if spread <= 80.0:  ob_pts += 3.0
        if imb >= 1.20:     ob_pts += 4.0

    score = max(0.0, min(100.0, mom_pts + acc_pts + ob_pts))
    sniper = (r15>=0.06 and accel>=0.07 and spread<=120.0 and imb>=1.00)
    return score, r15, r30, r60, accel, spread, imb, sniper

# ===== Trading =====
def totals_from_fills_eur(fills):
    tb=tq=fee=0.0
    for f in (fills or []):
        amt=float(f["amount"]); price=float(f["price"]); fe=float(f.get("fee",0) or 0)
        tb+=amt; tq+=amt*price; fee+=fe
    return tb,tq,fee

def place_order(side, market, amount=None, amount_quote=None):
    body={"market":market,"side":side,"orderType":"market","clientOrderId":str(uuid4()),"operatorId":""}
    if side=="buy": body["amountQuote"]=f"{amount_quote:.2f}"
    else: body["amount"]=f"{amount:.10f}"
    return bv_request("POST","/order", body)

def _adaptive_ob_requirements(price_now):
    if price_now < 0.02:  return max(60.0, price_now*1200), 220.0, 1.00
    elif price_now < 0.2: return max(100.0, price_now*400), 160.0, 1.00
    else:                 return max(150.0, price_now*80), 140.0, 1.00

def buy(base_symbol):
    base=base_symbol.upper().strip()
    if _today_pnl() <= DAILY_STOP_EUR: send_message("⛔ توقف شراء لباقي اليوم."); return
    if r.exists(f"ban24:{base}") or r.exists(f"cooldown:{base}"): return
    market=f"{base}-EUR"

    price_now=fetch_price_ws_first(market) or 0.0
    min_bid,max_spread,req_imb=_adaptive_ob_requirements(price_now)
    ok,_,_=orderbook_guard(market, min_bid_eur=min_bid, req_imb=req_imb, max_spread_bp=max_spread)
    if not ok: r.setex(f"cooldown:{base}", 90, 1); return

    with lk:
        if any(t["symbol"]==market for t in active_trades): return
        if len(active_trades)>=MAX_TRADES: return

    eur=get_eur_available()
    if eur<5.0: return
    amt_quote=round(max(5.0, eur*0.98), 2)   # 98% لتفادي خطأ الرسوم
    res=place_order("buy", market, amount_quote=amt_quote)
    if not (isinstance(res,dict) and res.get("status")=="filled"):
        r.setex(f"blacklist:buy:{base}", BLACKLIST_EXPIRE_SECONDS, 1); return

    tb,tq,fee=totals_from_fills_eur(res.get("fills",[]))
    if tb<=0 or (tq+fee)<=0: return
    avg_incl=(tq+fee)/tb
    tr={"symbol":market,"entry":avg_incl,"amount":tb,"cost_eur":tq+fee,"buy_fee_eur":fee,
        "opened_at":time.time(),"peak_pct":0.0,"sl_dyn":DYN_SL_START,"last_exit_try":0.0,"exit_in_progress":False}
    with lk:
        active_trades.append(tr)
        executed_trades.append(tr.copy())
        r.set("nems:active_trades", json.dumps(active_trades))
        r.rpush("nems:executed_trades", json.dumps(tr))
    with _ws_lock: WATCHLIST_MARKETS.add(market)
    send_message(f"✅ شراء {base} | €{amt_quote:.2f} (100%) | SL {DYN_SL_START:.1f}%")

def sell_trade(tr):
    market=tr["symbol"]; base=market.replace("-EUR","")
    amt=float(tr.get("amount",0) or 0)
    if amt<=0: return
    ok=False; resp=None
    for _ in range(6):
        resp=place_order("sell", market, amount=amt)
        if isinstance(resp,dict) and resp.get("status")=="filled": ok=True; break
        time.sleep(2)
    if not ok:
        r.setex(f"blacklist:sell:{base}", BLACKLIST_EXPIRE_SECONDS, 1); return

    tb,tq,fee=totals_from_fills_eur(resp.get("fills",[]))
    proceeds=tq-fee
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
    r.setex(f"cooldown:{base}", BUY_COOLDOWN_SEC, 1)
    send_message(f"💰 بيع {base} | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%)")

# ===== Monitor (SL/Crash فقط) =====
def _update_trade_hist(tr, ts, price):
    if "hist" not in tr: tr["hist"]=deque(maxlen=600)
    tr["hist"].append((ts,price))
    cutoff=ts-120
    while tr["hist"] and tr["hist"][0][0]<cutoff: tr["hist"].popleft()

def _price_n_seconds_ago(tr, now_ts, sec):
    hist=tr.get("hist",deque()); cutoff=now_ts-sec
    for ts,p in reversed(hist):
        if ts<=cutoff: return p
    return None

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

                inc=int(max(0.0,pnl)//1); base=DYN_SL_START + inc*DYN_SL_STEP
                tr["sl_dyn"]=max(tr.get("sl_dyn",DYN_SL_START), base)

                # SL فقط
                if pnl<=tr.get("sl_dyn",DYN_SL_START):
                    tr["exit_in_progress"]=True; tr["last_exit_try"]=now
                    sell_trade(tr); tr["exit_in_progress"]=False; continue

                # Crash سريع
                p=_price_n_seconds_ago(tr, now, CRASH_WINDOW_SEC)
                if p and p>0:
                    d=(cur/p-1.0)*100.0
                    if d<=-CRASH_DROP_PCT and pnl<=CRASH_EXTRA_PNL:
                        tr["exit_in_progress"]=True; tr["last_exit_try"]=now
                        sell_trade(tr); tr["exit_in_progress"]=False; continue

            time.sleep(0.2)
        except Exception as e:
            print("monitor err:", e); time.sleep(1)
Thread(target=monitor_loop, daemon=True).start()

# ===== Replacement Logic (فرصة أقوى فقط) =====
def current_trade_strength(tr):
    """نحوّل الربحية الآنية + زخم السوق لدرجة 0..100 للمقارنة."""
    m=tr["symbol"]; price=fetch_price_ws_first(m) or tr["entry"]
    _update_hist(m, time.time(), price)
    sc, r15, r30, r60, acc, spr, imb, snp = score_exploder(m, price)
    pnl=((price/float(tr["entry"]))-1.0)*100.0
    return sc + max(0.0, pnl*2.0)  # نضيف وزن بسيط للربح الحالي

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
                sc, r15, r30, r60, acc, spr, imb, snp = score_exploder(m, p)
                if spr>THRESH_SPREAD_BP_MAX or imb<THRESH_IMB_MIN: continue
                base=m.replace("-EUR","")
                if r.exists(f"ban24:{base}") or r.exists(f"cooldown:{base}"): continue
                cand=(sc,m,snp)
                if (best is None) or (cand[0]>best[0]): best=cand

            # لا صفقة حالياً → ادخل
            with lk: has_pos = len(active_trades)>0
            if best and not has_pos and (best[0] >= AUTO_THRESHOLD or best[2]):
                buy(best[1].replace("-EUR",""))
                time.sleep(ENGINE_INTERVAL_SEC); continue

            # عندنا صفقة → نسمح باستبدال فقط إذا غير خسرانة و المرشح أقوى بوضوح
            if best and has_pos:
                with lk: tr=active_trades[0]
                age=time.time()-tr.get("opened_at",time.time())
                if age >= NO_CHURN_WINDOW_S and age >= REPL_MIN_AGE_S:
                    cur_strength=current_trade_strength(tr)
                    cand_strength=best[0]
                    cur_price=fetch_price_ws_first(tr["symbol"]) or tr["entry"]
                    pnl=((cur_price/tr["entry"])-1.0)*100.0
                    if pnl >= REPL_ALLOW_IF_PNL_GE and (cand_strength >= cur_strength + REPL_BETTER_DELTA or best[2]):
                        send_message("🔄 Replacement: بيع الحالي وشراء فرصة أقوى")
                        sell_trade(tr); buy(best[1].replace("-EUR",""))

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
        sym=t["symbol"].replace("-EUR",""); entry=float(t["entry"])
        cur=fetch_price_ws_first(t["symbol"]) or entry
        pnl=((cur-entry)/entry)*100.0
        peak=float(t.get("peak_pct",0.0)); dyn=float(t.get("sl_dyn",DYN_SL_START))
        val=float(t["amount"])*cur; cost=float(t.get("cost_eur", entry*float(t["amount"])))
        fl=val-cost; pct=((val/cost)-1.0)*100.0 if cost>0 else 0.0
        lines.append("📌 الصفقة النشطة (1):")
        lines.append(f"{sym}: {pnl:+.2f}% | Peak {peak:.2f}% | SL {dyn:.2f}%")
        lines.append(f"💼 قيمة الصفقة: €{val:.2f} | عائم: {fl:+.2f}€ ({pct:+.2f}%)")
    else: lines.append("📌 لا صفقات نشطة.")

    since=float(r.get(SINCE_RESET_KEY) or 0.0)
    closed=[t for t in ex if "pnl_eur" in t and "exit_time" in t and float(t["exit_time"])>=since]
    wins=sum(1 for t in closed if float(t["pnl_eur"])>=0); losses=len(closed)-wins
    pnl_eur=sum(float(t["pnl_eur"]) for t in closed)
    avg_eur=(pnl_eur/len(closed)) if closed else 0.0
    avg_pct=(sum(float(t.get("pnl_pct",0)) for t in closed)/len(closed)) if closed else 0.0
    lines.append("\n📊 صفقات مكتملة منذ Reset:")
    lines.append("• لا يوجد." if not closed else f"• العدد: {len(closed)} | محققة: {pnl_eur:+.2f}€ | متوسط/صفقة: {avg_eur:+.2f}€ ({avg_pct:+.2f}%)")
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
            f"⚙️ one-trade, thr={AUTO_THRESHOLD}, interval={ENGINE_INTERVAL_SEC}s | "
            f"spread≤{THRESH_SPREAD_BP_MAX}bp, imb≥{THRESH_IMB_MIN} | SL start/step={DYN_SL_START}/{DYN_SL_STEP}% | "
            f"daily={DAILY_STOP_EUR}€ | replacement: Δ≥{REPL_BETTER_DELTA}, pnl≥{REPL_ALLOW_IF_PNL_GE}%, age≥{REPL_MIN_AGE_S}s"
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
        buy(sym); return "ok"
    if has("balance","الرصيد","رصيد"):
        bals=bv_request("GET","/balance")
        if not isinstance(bals,list): send_message("❌ تعذر جلب الرصيد."); return "ok"
        eur=sum(float(b.get("available",0))+float(b.get("inOrder",0)) for b in bals if b.get("symbol")=="EUR")
        lines=[f"💰 الرصيد: €{eur:.2f}"]
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