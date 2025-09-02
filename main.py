# -*- coding: utf-8 -*-
"""
Nems — ULTRA NITRO (Bitvavo EUR) — DEBUG EDITION
صفقة واحدة فقط + Replacement + Hybrid Maker→Taker
لوغات تفصيلية لسبب عدم الدخول + فحوص يومية/حظر/كولدداون/دفتر أوامر

أهم مفاتيح الضبط:
- AUTO_THRESHOLD: عتبة دخول الإشارة
- REPLACE_EXTRA: كم نقطة لازم تكون الإشارة الجديدة أقوى ليعمل Replacement
- THRESH_*: شروط دفتر الأوامر
- DAILY_STOP_EUR: حد الخسارة اليومي (عطّله مؤقتاً للاختبار)
- DEBUG_TG: إذا True يرسل بعض اللوجات لتلغرام (خفيفة لتجنب السبام)
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

# ===== DEBUG switches =====
DEBUG      = True
DEBUG_TG   = False     # إذا بدك بعض اللوجات على تليغرام (خفيفة)
DBG_EVERY  = 30        # كل كم ثانية نسمح بلوج TG عام
_last_dbg_tg = 0.0

def _now(): return time.strftime("%H:%M:%S")

def dbg(*args):
    """لوغ كونسول دائماً، وتلغرام اختياري كل فترة."""
    global _last_dbg_tg
    msg = "[%s] " % _now() + " ".join(str(a) for a in args)
    print(msg, flush=True)
    if DEBUG_TG and (time.time() - _last_dbg_tg) >= DBG_EVERY:
        _last_dbg_tg = time.time()
        try:
            if BOT_TOKEN and CHAT_ID:
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                              data={"chat_id": CHAT_ID, "text": "🐞 " + msg}, timeout=8)
        except Exception:
            pass

# ===== Settings (ONE position only) =====
MAX_TRADES              = 1        # صفقة واحدة فقط
ENGINE_INTERVAL_SEC     = 0.35
TOPN_WATCH              = 60

AUTO_THRESHOLD          = 16.0     # سهّلناها لزيادة النشاط
REPLACE_EXTRA           = 2.0      # الفرق بين أفضل فرصة والصفقة الحالية لنفكر بالاستبدال
MIN_TRADE_AGE_REPL_S    = 12       # أقل عمر للصفقة الحالية قبل التفكير بالاستبدال
MAX_PNL_FOR_REPL        = 0.6      # لا نستبدل إذا الربح أعلى من هذا (بنسبة %)

THRESH_SPREAD_BP_MAX    = 260.0
THRESH_IMB_MIN          = 0.55

DAILY_STOP_EUR          = -9999.0  # عطّلت الحد اليومي مؤقتاً لسهولة التشخيص (رجّعه لاحقاً)
COOLDOWN_SEC            = 35
BAN_LOSS_PCT            = -5.0
CONSEC_LOSS_BAN         = 3
BLACKLIST_EXPIRE_SECONDS= 180

# خروج: محافظ (ما منبيع فوراً بعد الشراء)
DYN_SL_START            = -3.0
DYN_SL_STEP             = 0.8
PEAK_TRIGGER            = 1.4
GIVEBACK_RATIO          = 0.50
GIVEBACK_CAP            = 1.4
TP_WEAK                 = 0.8
TP_GOOD                 = 1.2
TIME_STOP_MIN           = 7*60
TIME_STOP_PNL_LO        = -0.7
TIME_STOP_PNL_HI        = 1.0

# ===== WS & caches =====
_ws_lock = Lock()
_ws_prices = {}    # market -> {price,ts}
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
        j = resp.json()
        return j
    except Exception as e:
        dbg("bv_request err:", e)
        return {"error":"request_failed"}

def get_eur_available()->float:
    try:
        bals=bv_request("GET","/balance")
        if isinstance(bals,list):
            for b in bals:
                if b.get("symbol")=="EUR":
                    return max(0.0, float(b.get("available",0) or 0))
    except Exception as e:
        dbg("balance error:", e)
    return 0.0

def fetch_price_ws_first(market, staleness=2.0):
    now=time.time()
    with _ws_lock:
        rec=_ws_prices.get(market)
    if rec and (now-rec["ts"])<=staleness:
        return rec["price"]
    try:
        j=requests.get(f"{BASE_URL}/ticker/price?market={market}", timeout=5).json()
        p=float(j.get("price",0) or 0)
        if p>0:
            with _ws_lock: _ws_prices[market]={"price":p,"ts":now}
            return p
    except Exception as e:
        dbg("price fetch error:", market, e)
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
        if mkts:
            ws.send(json.dumps(_ws_sub_payload(mkts)))
            dbg("WS open: subscribed", len(mkts))
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
def _ws_on_error(ws,err): dbg("WS err:", err)
def _ws_on_close(ws,c,r):
    global _ws_running; _ws_running=False
    dbg("WS closed:", c, r)
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
            dbg("WS loop ex:", e)
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
        dbg("Backfill done:", m, "mins:", minutes)
    except Exception as e:
        dbg("backfill err:", e)

def fetch_orderbook(m, ttl=1.6):
    now=time.time(); rec=OB_CACHE.get(m)
    if rec and (now-rec["ts"])<ttl: return rec["data"]
    try:
        j=requests.get(f"{BASE_URL}/{m}/book", timeout=5).json()
        if j and j.get("bids") and j.get("asks"):
            OB_CACHE[m]={"data":j,"ts":now}; return j
    except Exception as e:
        dbg("ob fetch err:", m, e)
    return None

def orderbook_guard(market, min_bid_eur=40.0, req_imb=THRESH_IMB_MIN, max_spread_bp=THRESH_SPREAD_BP_MAX, depth_used=3):
    ob=fetch_orderbook(market)
    if not ob or not ob.get("bids") or not ob.get("asks"): return False,"no_book",{}
    try:
        bid_p=float(ob["bids"][0][0]); ask_p=float(ob["asks"][0][0])
        bid_eur=sum(float(p)*float(q) for p,q,*_ in ob["bids"][:depth_used])
        ask_eur=sum(float(p)*float(q) for p,q,*_ in ob["asks"][:depth_used])
    except Exception:
        return False,"bad_book",{}
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
    except Exception as e:
        dbg("ticker24 err:", e)
    return _t_rows()

def _score_market_volvol(market):
    now=time.time()
    rec=VOL_CACHE.get(market)
    if rec and (now-rec[0])<90: return rec[1]
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
    wl=[m for _,m in picks[:n]]
    dbg("Watchlist size:", len(wl))
    return wl

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

    mom_pts = max(0.0, min(70.0, 2.3*r15 + 2.0*r30 + 0.4*r60))
    acc_pts = max(0.0, min(24.0, 10.0*max(0.0, accel)))
    ob_pts  = 0.0
    if ok:
        if spread <= 200.0: ob_pts += max(0.0, min(10.0, (200.0-spread)*0.06))
        if imb >= 0.90:     ob_pts += max(0.0, min(10.0, (imb-0.90)*12.0))
        if spread <= 120.0: ob_pts += 2.0
        if imb >= 1.15:     ob_pts += 3.0

    score = max(0.0, min(100.0, mom_pts + acc_pts + ob_pts))
    sniper = (r15>=0.07 and accel>=0.06 and spread<=220.0 and imb>=0.80)
    return score, r15, r30, r60, accel, spread, imb, sniper

# ===== Orders =====
def totals_from_fills_eur(fills):
    tb=tq=fee=0.0
    for f in (fills or []):
        amt=float(f["amount"]); price=float(f["price"]); fe=float(f.get("fee",0) or 0)
        tb+=amt; tq+=amt*price; fee+=fe
    return tb,tq,fee

def place_order(side, market, amount=None, amount_quote=None, post_only=False):
    body={"market":market,"side":side,"orderType":"market","clientOrderId":str(uuid4()),"operatorId":""}
    if side=="buy":
        if post_only:
            # Bitvavo لا يدعم postOnly مع market؛ لذا نستعمل limit عند الحاجة (نحافظ على نفس الدالة لسهولة اللوج)
            body={"market":market,"side":"buy","orderType":"limit","price":f"{fetch_price_ws_first(market):.8f}",
                  "amountQuote":f"{amount_quote:.2f}","postOnly":True,"timeInForce":"GTC",
                  "clientOrderId":str(uuid4()),"operatorId":""}
        else:
            body["amountQuote"]=f"{amount_quote:.2f}"
    else:
        if post_only:
            body={"market":market,"side":"sell","orderType":"limit","price":f"{fetch_price_ws_first(market):.8f}",
                  "amount":f"{amount:.10f}","postOnly":True,"timeInForce":"GTC",
                  "clientOrderId":str(uuid4()),"operatorId":""}
        else:
            body["amount"]=f"{amount:.10f}"
    j = bv_request("POST","/order", body)
    return j

# ===== Trading (ONE full-size position) =====
def _adaptive_ob_requirements(price_now):
    if price_now < 0.02:
        return max(20.0, price_now*800), 360.0, 0.22
    elif price_now < 0.2:
        return max(30.0, price_now*220), 260.0, 0.32
    else:
        return max(50.0, price_now*4),   240.0, 0.28

def _have_position():
    with lk: return len(active_trades)>0

def _current_trade():
    with lk: return active_trades[0] if active_trades else None

def _pnl_now(tr):
    cur=fetch_price_ws_first(tr["symbol"]) or tr["entry"]
    return (cur/tr["entry"]-1.0)*100.0

def hybrid_buy_full(base_symbol):
    base=base_symbol.upper().strip()
    market=f"{base}-EUR"

    eur=get_eur_available()
    dbg("Buy attempt:", base, "| EUR avail:", eur)
    if eur < 5.0:
        dbg("Skip buy: low EUR", eur)
        return False

    # 1) Maker (postOnly limit at/inside best bid) – محاولة بدون رسوم
    p = fetch_price_ws_first(market)
    if not p:
        dbg("Skip buy: no price")
        return False
    lim_price = p  # ممكن تحركه شوي لتحسين الالتقاط
    try:
        j = place_order("buy", market, amount_quote=eur, post_only=True)
        dbg("Maker response:", j)
        if isinstance(j, dict) and j.get("status")=="filled":
            fills=j.get("fills",[])
            tb,tq,fee=totals_from_fills_eur(fills)
            if tb>0 and (tq+fee)>0:
                _on_filled_new_trade(market, tb, tq, fee)
                send_message(f"✅ شراء (Maker) {base} | €{eur:.2f}")
                return True
        # إذا رفض postOnly أو ظل open بدون تعبئة، نحاول تاليًا تايكر
    except Exception as e:
        dbg("Maker exception:", e)

    # 2) Taker (market) – نفذ بسرعة
    j = place_order("buy", market, amount_quote=eur, post_only=False)
    dbg("Taker response:", j)
    if not (isinstance(j,dict) and j.get("status")=="filled"):
        dbg("Skip buy: taker not filled")
        return False
    tb,tq,fee=totals_from_fills_eur(j.get("fills",[]))
    if tb<=0 or (tq+fee)<=0:
        dbg("Skip buy: bad fills")
        return False
    _on_filled_new_trade(market, tb, tq, fee)
    send_message(f"✅ شراء (Taker) {base} | €{eur:.2f}")
    return True

def _on_filled_new_trade(market, tb, tq, fee):
    avg_incl=(tq+fee)/tb
    tr={"symbol":market,"entry":avg_incl,"amount":tb,"cost_eur":tq+fee,"buy_fee_eur":fee,
        "opened_at":time.time(),"peak_pct":0.0,"sl_dyn":DYN_SL_START,
        "last_exit_try":0.0,"exit_in_progress":False,"hist":deque(maxlen=600)}
    with lk:
        active_trades.clear()   # صفقة واحدة فقط
        active_trades.append(tr)
        executed_trades.append(tr.copy())
        r.set("nems:active_trades", json.dumps(active_trades))
        r.rpush("nems:executed_trades", json.dumps(tr))
    with _ws_lock: WATCHLIST_MARKETS.add(market)
    dbg("New position:", market, "entry:", avg_incl, "amount:", tb)

def sell_all(tr, reason=""):
    market=tr["symbol"]; base=market.replace("-EUR","")
    amt=float(tr.get("amount",0) or 0)
    if amt<=0:
        dbg("Sell skip: zero amount")
        return False
    ok=False; resp=None
    for i in range(6):
        resp=place_order("sell", market, amount=amt, post_only=False)
        dbg("Sell try", i+1, "resp:", resp)
        if isinstance(resp,dict) and resp.get("status")=="filled": ok=True; break
        time.sleep(2)
    if not ok:
        r.setex(f"blacklist:sell:{base}", BLACKLIST_EXPIRE_SECONDS, 1)
        dbg("Sell failed: blacklist", base)
        return False

    tb,tq,fee=totals_from_fills_eur(resp.get("fills",[]))
    proceeds=tq-fee
    orig_cost=float(tr.get("cost_eur", tr["entry"]*amt))
    pnl_eur=proceeds-orig_cost
    pnl_pct=(proceeds/orig_cost-1.0)*100.0 if orig_cost>0 else 0.0
    _accum_realized(pnl_eur)
    with lk:
        try: active_trades.remove(tr)
        except ValueError: pass
        r.set("nems:active_trades", json.dumps(active_trades))
        for i in range(len(executed_trades)-1,-1,-1):
            t=executed_trades[i]
            if t["symbol"]==market and "exit_eur" not in t:
                t.update({"exit_eur":proceeds,"sell_fee_eur":fee,"pnl_eur":pnl_eur,"pnl_pct":pnl_pct,"exit_time":time.time(),"exit_reason":reason})
                break
        r.delete("nems:executed_trades")
        for t in executed_trades: r.rpush("nems:executed_trades", json.dumps(t))
    r.setex(f"cooldown:{base}", COOLDOWN_SEC, 1)
    send_message(f"💰 بيع {base} | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%) {('— '+reason) if reason else ''}")
    dbg("Sold:", base, "pnl%", f"{pnl_pct:.2f}", "reason:", reason)
    return True

# ===== Monitor (Exits) =====
def _update_trade_hist(tr, ts, price):
    tr["hist"].append((ts,price))
    cutoff=ts-120
    while tr["hist"] and tr["hist"][0][0]<cutoff: tr["hist"].popleft()

def monitor_loop():
    while True:
        try:
            tr=_current_trade()
            now=time.time()
            if not tr:
                time.sleep(0.25); continue
            m=tr["symbol"]; entry=float(tr["entry"])
            cur=fetch_price_ws_first(m)
            if not cur:
                time.sleep(0.15); continue
            _update_trade_hist(tr, now, cur)

            pnl=((cur-entry)/entry)*100.0
            tr["peak_pct"]=max(tr.get("peak_pct",0.0), pnl)

            inc=int(max(0.0,pnl)//1); base=DYN_SL_START + inc*DYN_SL_STEP
            tr["sl_dyn"]=max(tr.get("sl_dyn",DYN_SL_START), base)

            # تقدير r30/r90 (لـ giveback فقط)
            hist=tr.get("hist",deque())
            if hist:
                now_ts=hist[-1][0]; p30=p90=None
                for ts,pp in hist:
                    age=now_ts-ts
                    if p30 is None and age>=30: p30=pp
                    if p90 is None and age>=90: p90=pp
                basep=hist[0][1]
                p30=p30 or basep; p90=p90 or basep
                r30=(cur/p30-1.0)*100.0 if p30>0 else 0.0
                r90=(cur/p90-1.0)*100.0 if p90>0 else 0.0
            else: r30=r90=0.0

            # TP سريع
            ob_ok,_,obf=orderbook_guard(m)
            spread=obf.get("spread_bp",999.0) if obf else 999.0
            imb=obf.get("imb",0.0) if obf else 0.0
            weak=(not ob_ok) or (spread>170.0) or (imb<0.92) or (r30<0 and r90<0)
            tp=TP_WEAK if weak else TP_GOOD
            if pnl>=tp and tr.get("peak_pct",0.0)<(tp+0.4):
                sell_all(tr, reason=f"TP {tp:.2f}%"); continue

            # Giveback من القمة
            peak=tr.get("peak_pct",0.0)
            if peak>=PEAK_TRIGGER:
                give=min(GIVEBACK_CAP, GIVEBACK_RATIO*peak)
                desired=peak-give
                if desired>tr.get("sl_dyn",DYN_SL_START): tr["sl_dyn"]=desired
                if (peak-pnl)>=give and (r30<=-0.25 or r90<=0.0):
                    sell_all(tr, reason=f"Giveback {give:.2f}%"); continue

            # SL
            if pnl<=tr.get("sl_dyn",DYN_SL_START):
                sell_all(tr, reason=f"SL {tr['sl_dyn']:.2f}%"); continue

            # Time-stop
            age=now - tr.get("opened_at",now)
            if age>=TIME_STOP_MIN and TIME_STOP_PNL_LO<=pnl<=TIME_STOP_PNL_HI and r90<=0.0:
                sell_all(tr, reason="Time-stop"); continue

            time.sleep(0.18)
        except Exception as e:
            dbg("monitor err:", e)
            time.sleep(1)

Thread(target=monitor_loop, daemon=True).start()

# ===== Engine (signals + replacement) =====
def build_watch_and_best():
    refresh_watchlist()
    watch=list(WATCHLIST_MARKETS)
    if not watch:
        dbg("No watch markets")
        return None, None
    now=time.time()
    best=None
    for m in watch:
        p=fetch_price_ws_first(m)
        if not p: continue
        _update_hist(m, now, p)
        sc,r15,r30,r60,acc,spr,imb,snp = score_exploder(m, p)
        if spr>THRESH_SPREAD_BP_MAX or imb<THRESH_IMB_MIN:
            dbg("Skip", m, "OB filter:", f"spr={spr:.0f}bp imb={imb:.2f}")
            continue
        base=m.replace("-EUR","")
        if r.exists(f"ban24:{base}") or r.exists(f"cooldown:{base}"):
            dbg("Skip", m, "ban/cooldown")
            continue
        cand=(sc,m,r15,r30,r60,acc,spr,imb,snp,p)
        if (best is None) or (cand[0]>best[0]): best=cand
    return watch, best

def engine_loop():
    while True:
        try:
            if not (enabled and auto_enabled):
                time.sleep(1); continue

            pnl_today=_today_pnl()
            if pnl_today <= DAILY_STOP_EUR:
                dbg("Engine paused: daily stop reached", pnl_today)
                time.sleep(3); continue

            have_pos=_have_position()
            watch, best = build_watch_and_best()
            if not best:
                time.sleep(ENGINE_INTERVAL_SEC); continue

            score,m,r15,r30,r60,acc,spr,imb,sniper,px = best
            base=m.replace("-EUR","")
            thr = AUTO_THRESHOLD
            trigger = (score>=thr) or sniper

            dbg("Best:", base, "| score=%.1f thr=%.1f sniper=%s spr=%.0fbp imb=%.2f r15=%.2f r30=%.2f r60=%.2f"
                % (score,thr, sniper, spr, imb, r15, r30, r60))

            if not have_pos and trigger:
                dbg("Action: BUY", base)
                ok = buy_full(base)
                dbg("BUY result:", ok)
            elif have_pos and trigger:
                # Replacement logic
                tr=_current_trade()
                cur_pnl=_pnl_now(tr)
                age = time.time() - tr.get("opened_at",0)
                if (score >= thr + REPLACE_EXTRA) and (age >= MIN_TRADE_AGE_REPL_S) and (cur_pnl <= MAX_PNL_FOR_REPL):
                    dbg("Action: REPLACE", base, "| old:", tr["symbol"], "age:", int(age), "pnl:", f"{cur_pnl:.2f}%")
                    if sell_all(tr, reason="Replacement"):
                        time.sleep(0.4)
                        ok = buy_full(base)
                        dbg("REPLACE buy result:", ok)
                else:
                    dbg("Hold current. Not replacing.", "age=", int(age), "pnl=", f"{cur_pnl:.2f}%", "need extra", REPLACE_EXTRA)

            time.sleep(ENGINE_INTERVAL_SEC)
        except Exception as e:
            dbg("engine err:", e)
            time.sleep(1)

def buy_full(base):
    # فلاتر قبل الشراء
    pnl_today=_today_pnl()
    if pnl_today <= DAILY_STOP_EUR:
        dbg("Skip buy: daily stop", pnl_today)
        return False
    if r.exists(f"ban24:{base}") or r.exists(f"cooldown:{base}"):
        dbg("Skip buy:", base, "ban/cooldown")
        return False

    market=f"{base}-EUR"
    price_now=fetch_price_ws_first(market) or 0.0
    min_bid,max_spread,req_imb=_adaptive_ob_requirements(price_now)
    ok,why,feats=orderbook_guard(market, min_bid_eur=min_bid, req_imb=req_imb, max_spread_bp=max_spread)
    if not ok:
        dbg("Skip buy (OB):", base, "why:", why, "| features:", feats, "| req:", (min_bid, max_spread, req_imb))
        r.setex(f"cooldown:{base}", COOLDOWN_SEC, 1)
        return False

    with lk:
        if len(active_trades)>=MAX_TRADES:
            dbg("Skip buy: already have position")
            return False

    # نفّذ شراء كامل الرصيد المتاح (مرن Maker→Taker)
    ok = hybrid_buy_full(base)
    if not ok:
        dbg("Buy failed:", base)
    return ok

Thread(target=engine_loop, daemon=True).start()

# ===== Summary & Telegram =====
def build_summary():
    lines=[]; now=time.time()
    with lk: act=list(active_trades); ex=list(executed_trades)
    if act:
        tr=act[0]
        sym=tr["symbol"].replace("-EUR",""); entry=float(tr["entry"]); amt=float(tr["amount"])
        cur=fetch_price_ws_first(tr["symbol"]) or entry
        pnl=((cur-entry)/entry)*100.0
        peak=float(tr.get("peak_pct",0.0)); dyn=float(tr.get("sl_dyn", DYN_SL_START))
        lines.append(f"📌 صفقة فعالة: {sym} | {pnl:+.2f}% | Peak {peak:.2f}% | SL {dyn:.2f}%")
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
        lines.append(f"• العدد: {len(closed)} | محققة: {pnl_eور:+.2f}€ | متوسط/صفقة: {avg_eور:+.2f}€ ({avg_pct:+.2f}%)")
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
    global enabled, auto_enabled, DEBUG_TG
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
    if starts("debug on"):
        DEBUG_TG=True; send_message("🐞 Debug TG: ON"); return "ok"
    if starts("debug off"):
        DEBUG_TG=False; send_message("🐞 Debug TG: OFF"); return "ok"
    if has("settings","اعدادات","إعدادات"):
        send_message(f"⚙️ thr={AUTO_THRESHOLD}, repl+{REPLACE_EXTRA}, topN={TOPN_WATCH}, interval={ENGINE_INTERVAL_SEC}s | spread≤{THRESH_SPREAD_BP_MAX}bp, imb≥{THRESH_IMB_MIN} | TP={TP_WEAK}/{TP_GOOD}% | SL={DYN_SL_START}/{DYN_SL_STEP} | daily={DAILY_STOP_EUR}€")
        return "ok"
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
        ok = buy_full(sym); send_message(f"Manual buy {sym}: {ok}")
        return "ok"
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
except Exception as e: dbg("state load err:", e)

if __name__=="__main__" and RUN_LOCAL:
    app.run(host="0.0.0.0", port=5000)