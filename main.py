# -*- coding: utf-8 -*-
"""
Nems — Maker-First One-Position Scalper (Bitvavo EUR) — Clean & Unchoked

• صفقة واحدة فقط، دخول بكامل الرصيد، Maker أولاً (postOnly) مع fallback سريع.
• تغطية كل السوق: اختيار أفضل 120 زوج EUR حسب (spread+depth) و(vol 1m + move 5m).
• إشارة: momentum r15/r30/r60 + accel + نقاط دفتر أوامر خفيفة.
• خروج: TP سريع، Breakeven lock، Giveback، Time-stop.
• Replacement صارم: Δscore≥9 والصفقة الحالية ليست خاسرة.

ملاحظات: اضبط رسوم منصتك، وقد تعدّل TP/SL/المهل حسب تجربتك.
"""

import os, re, time, json, math, traceback, statistics as st
import requests, redis, websocket
from threading import Thread, Lock
from collections import deque
from uuid import uuid4
from flask import Flask, request
from dotenv import load_dotenv

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

BASE_URL = "https://api.bitvavo.com/v2"
WS_URL   = "wss://ws.bitvavo.com/v2/"

# ===== Core Settings (خفيفة وغير خانقة) =====
MAX_TRADES            = 1                 # صفقة واحدة فقط
ENGINE_INTERVAL_SEC   = 0.35
WIDE_SCAN_EVERY_SEC   = 60                # مسح واسع لكل السوق كل دقيقة
WATCHLIST_SIZE        = 120               # مراقبة فعّالة
MAKER_WAIT_SEC_IN     = 8                 # مهلة تعبئة أمر الشراء Maker
MAKER_WAIT_SEC_OUT    = 10                # مهلة تعبئة أمر البيع Maker
POST_ONLY_SLIPP_BP    = 3                 # انزياح بسيط عن أفضل Bid/Ask (3bp)

# فلاتر اقتصادية فقط (لا نخنق):
SPREAD_BP_MAX         = 240.0             # حد أعلى للسبريد المقبول
BID_DEPTH_MIN_EUR     = 40.0              # عمق € عند أفضل 3 مستويات
IMB_MIN               = 0.70              # ميل بسيط للطلبات (اختياري وخفيف)

# دخول/خروج
TP_WEAK               = 0.6               # عند ظروف أضعف (spread/imb/زخم)
TP_GOOD               = 0.9               # عند ظروف جيدة
BREAKEVEN_LOCK_AT     = 0.7               # عندها نرفع SL لتغطية الرسوم
BREAKEVEN_LEVEL       = 0.2
GIVEBACK_START        = 1.5               # بدء giveback من القمة
GIVEBACK_RATIO        = 0.55
GIVEBACK_CAP          = 1.6
TIME_STOP_MIN         = 7*60
TIME_STOP_RANGE       = (-0.6, 0.9)

# Replacement
REPLACEMENT_DELTA     = 9.0               # فرق السكور المطلوب
REPLACEMENT_MIN_AGE   = 60                # ثانية

# إدارة يومية
DAILY_STOP_EUR        = -20.0
BUY_COOLDOWN_SEC      = 35
BAN_LOSS_PCT          = -5.0
CONSEC_LOSS_BAN       = 3

# ===== Runtime State =====
enabled=True; auto_enabled=True
active_trade=None            # dict أو None
executed_trades=[]
SINCE_RESET_KEY="nems:since_reset"

# ===== WS & caches =====
_ws_lock=Lock(); _ws_prices={}
HISTS={}; OB_CACHE={}; VOL_CACHE={}
WATCH=set(); _last_wide_scan=0
debug_tg=False

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

# ===== Prices (WS + fallback) =====
def _ws_sub_payload(mkts): return {"action":"subscribe","channels":[{"name":"ticker","markets":mkts}]}
def _ws_on_open(ws):
    try: 
        with _ws_lock: mkts=sorted(list(WATCH) + ([active_trade["symbol"]] if active_trade else []))
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
def _ws_on_error(ws,err): 
    if debug_tg: send_message(f"🐞 WS error: {err}")
def _ws_on_close(ws,c,r): pass
def _ws_thread():
    while True:
        try:
            ws=websocket.WebSocketApp(WS_URL, on_open=_ws_on_open, on_message=_ws_on_message,
                                      on_error=_ws_on_error, on_close=_ws_on_close)
            ws.run_forever(ping_interval=25, ping_timeout=10)
        except Exception as e:
            print("WS loop ex:", e)
        time.sleep(2)
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

# ===== Histories & OB =====
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

def fetch_orderbook(m, ttl=1.5):
    now=time.time(); rec=OB_CACHE.get(m)
    if rec and (now-rec["ts"])<ttl: return rec["data"]
    try:
        j=requests.get(f"{BASE_URL}/{m}/book", timeout=5).json()
        if j and j.get("bids") and j.get("asks"):
            OB_CACHE[m]={"data":j,"ts":now}; return j
    except Exception: pass
    return None

def ob_features(market, depth_used=3):
    ob=fetch_orderbook(market)
    if not ob or not ob.get("bids") or not ob.get("asks"): return None
    try:
        bid_p=float(ob["bids"][0][0]); ask_p=float(ob["asks"][0][0])
        bid_eur=sum(float(p)*float(q) for p,q,*_ in ob["bids"][:depth_used])
        ask_eur=sum(float(p)*float(q) for p,q,*_ in ob["asks"][:depth_used])
        spread_bp=(ask_p-bid_p)/((ask_p+bid_p)/2.0)*10000.0
        imb=bid_eur/max(1e-9, ask_eur)
        return {"spread_bp":spread_bp,"bid_eur":bid_eur,"imb":imb}
    except Exception: 
        return None

# ===== Wide Scan: يغطي كل السوق =====
def _wide_scan_markets():
    global WATCH, _last_wide_scan
    now=time.time()
    if now-_last_wide_scan < WIDE_SCAN_EVERY_SEC: return
    _last_wide_scan=now

    rows = requests.get(f"{BASE_URL}/ticker/24h", timeout=8).json()
    if not isinstance(rows,list): return
    picks=[]
    for r0 in rows:
        m=r0.get("market","")
        if not m.endswith("-EUR"): continue
        try:
            last=float(r0.get("last",0) or 0)
            if last<=0: continue
        except Exception: 
            continue
        # سبريد وعمق سريعين من /book
        obf=ob_features(m)
        if not obf: continue
        spr=obf["spread_bp"]; bid_eur=obf["bid_eur"]; imb=obf["imb"]
        if spr>SPREAD_BP_MAX or bid_eur<BID_DEPTH_MIN_EUR or imb<IMB_MIN:
            continue  # فقط الفلاتر الاقتصادية الأساسية

        # VolScore: std لعوائد 1m آخر 20 شمعة + حركة 5m
        try:
            cs=requests.get(f"{BASE_URL}/{m}/candles?interval=1m&limit=20", timeout=4).json()
            closes=[float(c[4]) for c in cs if isinstance(c,list) and len(c)>=5]
            if len(closes)<6: 
                continue
            rets=[closes[i]/closes[i-1]-1.0 for i in range(1,len(closes))]
            v5=abs(sum(rets[-5:]))*100.0
            stdv=(st.pstdev(rets)*100.0) if len(rets)>2 else 0.0
            prescore = 0.6*stdv + 0.4*v5
        except Exception:
            continue

        picks.append((prescore, m))
    picks.sort(reverse=True)
    wanted=[m for _,m in picks[:WATCHLIST_SIZE]]
    with _ws_lock:
        WATCH=set(wanted)
    # Backfill 1m بسيط للِّي دخلوا جدد
    for m in wanted:
        if m not in HISTS:
            try:
                rows=requests.get(f"{BASE_URL}/{m}/candles?interval=1m&limit=60", timeout=6).json()
                if isinstance(rows,list):
                    for ts,o,h,l,c,v in rows[-60:]:
                        _update_hist(m, ts/1000.0, float(c))
            except Exception: pass
    if debug_tg: send_message(f"🐞 Watchlist size: {len(WATCH)}")

# ===== Scoring (خفيف) =====
def btc_regime_boost():
    try:
        m="BTC-EUR"
        cs=requests.get(f"{BASE_URL}/{m}/candles?interval=1m&limit=15", timeout=4).json()
        closes=[float(c[4]) for c in cs if isinstance(c,list) and len(c)>=5]
        if len(closes)<5: return 0.0
        rets=[closes[i]/closes[i-1]-1.0 for i in range(1,len(closes))]
        v5=abs(sum(rets[-5:]))*100.0
        return 6.0 if v5>=0.8 else (3.0 if v5>=0.5 else 0.0)
    except Exception: return 0.0

def score_symbol(m, price_now):
    r15,r30,r60,_ = _mom_metrics_symbol(m, price_now)
    accel = r30 - r60
    obf=ob_features(m) or {"spread_bp":999.0,"imb":0.0}
    spr=obf["spread_bp"]; imb=obf["imb"]

    mom_pts = max(0.0, min(70.0, 2.3*r15 + 2.3*r30 + 0.3*r60))
    acc_pts = max(0.0, min(24.0, 10.0*max(0.0, accel)))
    ob_pts  = max(0.0, min(10.0, max(0.0,(200.0-spr)*0.06))) + max(0.0, min(10.0, (imb-0.85)*10.0))
    score   = max(0.0, min(100.0, mom_pts + acc_pts + ob_pts))
    return score, r15, r30, r60, accel, spr, imb

# ===== Orders: Maker-first مع fallback =====
def totals_from_fills_eur(fills):
    tb=tq=fee=0.0
    for f in (fills or []):
        amt=float(f["amount"]); price=float(f["price"]); fe=float(f.get("fee",0) or 0)
        tb+=amt; tq+=amt*price; fee+=fe
    return tb,tq,fee

def _place_limit_postonly(market, side, price, amount=None, amountQuote=None):
    body={"market":market,"side":side,"orderType":"limit","postOnly":True,
          "clientOrderId":str(uuid4()),"operatorId":"", "price":f"{price:.10f}"}
    if side=="buy": body["amountQuote"]=f"{amountQuote:.2f}"
    else:           body["amount"]=f"{amount:.10f}"
    return bv_request("POST","/order", body)

def _place_market(market, side, amount=None, amountQuote=None):
    body={"market":market,"side":side,"orderType":"market",
          "clientOrderId":str(uuid4()),"operatorId":""}
    if side=="buy": body["amountQuote"]=f"{amountQuote:.2f}"
    else:           body["amount"]=f"{amount:.10f}"
    return bv_request("POST","/order", body)

def _cancel_order(orderId):
    return bv_request("DELETE", f"/order?orderId={orderId}")

def _fetch_order(orderId):
    return bv_request("GET", f"/order?orderId={orderId}")

# ===== Trading Logic =====
def can_trade_now():
    if _today_pnl() <= DAILY_STOP_EUR: 
        return False
    return True

def try_open(market):
    """صفقة واحدة بكامل الرصيد — Maker أولاً ثم fallback."""
    global active_trade
    if active_trade is not None: return
    if not can_trade_now(): return

    eur=get_eur_available()
    if eur < 5.0: return

    # سعر الآن + حساب سعر ليمت postOnly فوق أفضل Bid بقليل
    price=fetch_price_ws_first(market)
    if not price: return
    obf=ob_features(market)
    if not obf: return
    best_bid=float(fetch_orderbook(market)["bids"][0][0])
    tick_price = best_bid * (1 + POST_ONLY_SLIPP_BP/10000.0)  # +3bp تقريبًا

    # 1) محاولة Maker
    res=_place_limit_postonly(market, "buy", tick_price, amountQuote=eur)
    orderId=res.get("orderId")
    started=time.time(); filled=False
    if orderId:
        while time.time()-started < MAKER_WAIT_SEC_IN:
            st=_fetch_order(orderId)
            if st.get("status")=="filled":
                filled=True; fills=st.get("fills",[]); break
            time.sleep(0.5)
        if not filled:
            _cancel_order(orderId)
    if not filled:
        # 2) fallback Market
        res=_place_market(market, "buy", amountQuote=eur)
        fills=res.get("fills",[])

    tb,tq,fee=totals_from_fills_eur(fills)
    if tb<=0 or (tq+fee)<=0: 
        return

    avg=(tq+fee)/tb
    active_trade={
        "symbol":market,"entry":avg,"amount":tb,"cost_eur":tq+fee,"buy_fee_eur":fee,
        "opened_at":time.time(),"peak_pct":0.0,"sl":-2.5, "last_try":0.0
    }
    executed_trades.append(active_trade.copy())
    send_message(f"✅ شراء {market.replace('-EUR','')} | €{eur:.2f} | Maker-first{'✔️' if orderId and filled else '→Taker'} | دخول @€{avg:.6f}")

def try_close(reason=""):
    """بيع Maker أولاً ثم Market."""
    global active_trade
    if active_trade is None: return
    m=active_trade["symbol"]; amt=float(active_trade["amount"])
    if amt<=0: return

    # هدفنا بيع على أفضل Ask −3bp
    ob=fetch_orderbook(m); 
    if not ob: return
    ask=float(ob["asks"][0][0])
    limit_price = ask * (1 - POST_ONLY_SLIPP_BP/10000.0)

    # 1) Maker sell
    res=_place_limit_postonly(m, "sell", limit_price, amount=amt)
    orderId=res.get("orderId")
    started=time.time(); filled=False; fills=[]
    if orderId:
        while time.time()-started < MAKER_WAIT_SEC_OUT:
            st=_fetch_order(orderId)
            if st.get("status")=="filled":
                filled=True; fills=st.get("fills",[]); break
            time.sleep(0.5)
        if not filled:
            _cancel_order(orderId)
    if not filled:
        res=_place_market(m, "sell", amount=amt)
        fills=res.get("fills",[])

    tb,tq,fee=totals_from_fills_eur(fills)
    proceeds=tq-fee
    orig_cost=float(active_trade.get("cost_eur"))
    pnl_eur=proceeds-orig_cost
    pnl_pct=(proceeds/orig_cost-1.0)*100.0 if orig_cost>0 else 0.0
    _accum_realized(pnl_eur)

    base=m.replace("-EUR","")
    if pnl_pct <= BAN_LOSS_PCT:
        r.setex(f"ban24:{base}", 24*3600, 1)

    # انهِ الصفقة
    for t in reversed(executed_trades):
        if t["symbol"]==m and "exit_eur" not in t:
            t.update({"exit_eur":proceeds,"sell_fee_eur":fee,"pnl_eur":pnl_eur,"pnl_pct":pnl_pct,"exit_time":time.time()})
            break
    send_message(f"💰 بيع {base} | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%) {('— '+reason) if reason else ''}")
    active_trade=None
    r.setex(f"cooldown:{base}", BUY_COOLDOWN_SEC, 1)

# ===== Monitor (خروج و Replacement) =====
def monitor_loop():
    global active_trade
    while True:
        try:
            if active_trade:
                m=active_trade["symbol"]; entry=float(active_trade["entry"])
                cur=fetch_price_ws_first(m)
                if cur:
                    _update_hist(m, time.time(), cur)
                    pnl=((cur-entry)/entry)*100.0
                    active_trade["peak_pct"]=max(active_trade.get("peak_pct",0.0), pnl)

                    # Breakeven lock
                    if pnl>=BREAKEVEN_LOCK_AT and active_trade.get("sl", -2.5)<BREAKEVEN_LEVEL:
                        active_trade["sl"]=BREAKEVEN_LEVEL
                        if debug_tg: send_message(f"🔒 Breakeven lock {m}: SL→{BREAKEVEN_LEVEL}%")

                    # TP سريع حسب البيئة
                    obf=ob_features(m) or {"spread_bp":999.0,"imb":0.0}
                    weak = (obf["spread_bp"]>140.0 or obf["imb"]<0.95)
                    tp = TP_WEAK if weak else TP_GOOD
                    if pnl>=tp and active_trade["peak_pct"]<(tp+0.4):
                        try_close(f"TP {tp:.2f}%"); time.sleep(0.5); continue

                    # Giveback بعد قمة
                    peak=active_trade["peak_pct"]
                    if peak>=GIVEBACK_START:
                        give=min(GIVEBACK_CAP, GIVEBACK_RATIO*peak)
                        if (peak - pnl) >= give:
                            try_close(f"Giveback {give:.2f}%"); time.sleep(0.5); continue

                    # SL ديناميكي بسيط (يتحسن مع الربح)
                    inc=int(max(0.0,pnl)//1)
                    dyn = -2.5 + 0.8*inc
                    active_trade["sl"]=max(active_trade.get("sl",-2.5), dyn)
                    if pnl<=active_trade["sl"]:
                        try_close(f"SL {active_trade['sl']:.2f}%"); time.sleep(0.5); continue

                    # Time-stop
                    age=time.time()-active_trade.get("opened_at", time.time())
                    if age>=TIME_STOP_MIN and TIME_STOP_RANGE[0]<=pnl<=TIME_STOP_RANGE[1]:
                        try_close("Time-stop"); time.sleep(0.5); continue

            time.sleep(0.25)
        except Exception as e:
            print("monitor err:", e); time.sleep(1)
Thread(target=monitor_loop, daemon=True).start()

# ===== Engine (إشارة + Replacement) =====
def engine_loop():
    global active_trade, WATCH
    while True:
        try:
            if not (enabled and auto_enabled): time.sleep(1); continue
            if not can_trade_now(): time.sleep(2); continue

            _wide_scan_markets()
            watch=list(WATCH)
            if not watch: time.sleep(1); continue

            # العتبة المتكيفة
            base_thr=20.0
            thr = base_thr - btc_regime_boost()

            best=None
            now=time.time()
            for m in watch:
                p=fetch_price_ws_first(m)
                if not p: continue
                _update_hist(m, now, p)
                sc,r15,r30,r60,acc,spr,imb = score_symbol(m, p)
                if sc is None: continue
                if (best is None) or (sc>best[0]): best=(sc,m)

            if best:
                sc, m = best
                # إذا لا يوجد صفقة → ادخل
                if active_trade is None and sc>=thr:
                    try_open(m)
                # Replacement: لو الفرق واضح والصفقة الحالية ليست خاسرة
                elif active_trade is not None:
                    cur=fetch_price_ws_first(active_trade["symbol"]) or active_trade["entry"]
                    pnl=((cur/active_trade["entry"])-1.0)*100.0
                    open_sc,_m = score_symbol(active_trade["symbol"], cur)[0], active_trade["symbol"]
                    if (sc - open_sc) >= REPLACEMENT_DELTA and pnl >= -0.1 and (time.time()-active_trade.get("opened_at",0))>=REPLACEMENT_MIN_AGE:
                        try_close("Replacement"); time.sleep(0.3)
                        try_open(m)

            time.sleep(ENGINE_INTERVAL_SEC)
        except Exception as e:
            print("engine err:", e); time.sleep(1)
Thread(target=engine_loop, daemon=True).start()

# ===== Summary & Telegram =====
def build_summary():
    lines=[]; now=time.time()
    if active_trade:
        t=active_trade; m=t["symbol"]; cur=fetch_price_ws_first(m) or t["entry"]
        pnl=((cur/t["entry"])-1.0)*100.0
        lines.append("📌 صفقة نشطة واحدة:")
        lines.append(f"• {m.replace('-EUR','')}: {pnl:+.2f}% | Peak {t.get('peak_pct',0.0):.2f}% | SL {t.get('sl',-2.5):.2f}%")
    else:
        lines.append("📌 لا صفقات نشطة.")
    since=float(r.get(SINCE_RESET_KEY) or 0.0)
    closed=[x for x in executed_trades if "exit_eur" in x and float(x["exit_time"])>=since]
    pnl_eur=sum(float(x["pnl_eur"]) for x in closed)
    wins=sum(1 for x in closed if float(x["pnl_eur"])>=0)
    lines.append(f"\n📊 صفقات مكتملة منذ Reset: {len(closed)} | محققة: {pnl_eur:+.2f}€ | فوز/خسارة: {wins}/{len(closed)-wins}")
    lines.append(f"\n⛔ حد اليوم: {_today_pnl():+.2f}€ / {DAILY_STOP_EUR:+.2f}€")
    return "\n".join(lines)

def send_chunks(txt, chunk=3300):
    if not txt: return
    buf=""
    for line in txt.splitlines(True):
        if len(buf)+len(line)>chunk: send_message(buf); buf=""
        buf+=line
    if buf: send_message(buf)

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
        global active_trade, executed_trades
        active_trade=None; executed_trades.clear()
        r.set(SINCE_RESET_KEY, time.time()); send_message("🧠 Reset."); return "ok"
    if has("settings","اعدادات","إعدادات"):
        send_message(f"⚙️ one-pos | watch={WATCHLIST_SIZE} | spr≤{SPREAD_BP_MAX}bp | depth≥€{BID_DEPTH_MIN_EUR} | imb≥{IMB_MIN}\nTP={TP_WEAK}/{TP_GOOD}% | BE lock @{BREAKEVEN_LOCK_AT}%→{BREAKEVEN_LEVEL}% | giveback≥{GIVEBACK_START}% r={GIVEBACK_RATIO} cap={GIVEBACK_CAP}%\nmaker_wait in/out {MAKER_WAIT_SEC_IN}/{MAKER_WAIT_SEC_OUT}s")
        return "ok"
    if starts("debug on"):
        debug_tg=True; send_message("🐞 Debug TG: ON"); return "ok"
    if starts("debug off"):
        debug_tg=False; send_message("🐞 Debug TG: OFF"); return "ok"
    return "ok"

# ===== Local run =====
if __name__=="__main__" and RUN_LOCAL:
    app.run(host="0.0.0.0", port=5000)