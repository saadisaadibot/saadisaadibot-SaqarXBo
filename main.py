# -*- coding: utf-8 -*-
"""
Daily One-Pick Trader — Bitvavo EUR (Slow Scan, One Buy/Day)
- مسح بطيء لكل السوق (5m, ~3.5d) مرة كل 24 ساعة → Top 10
- شراء Top1 (Maker-first) ومراقبة بخروج SL -3% + Trailing 1.2% بعد +3%
- إذا خرج بسبب SL: جرّب المرشّح رقم 2 لنفس اليوم ثم توقف
تحذير: التداول ينطوي على مخاطر. اضبط الرسوم/الحدود حسب حسابك.

ENV المطلوبة:
  BITVAVO_API_KEY, BITVAVO_API_SECRET
  BOT_TOKEN, CHAT_ID          (اختياري للإشعارات على تليغرام)
  REDIS_URL                   (اختياري للتخزين المؤقت)
"""

import os, time, json, math, statistics as st, traceback, re
from collections import deque
from uuid import uuid4
from threading import Thread, Lock

import requests, redis, websocket

# ======== ENV / Consts ========
BASE_URL = "https://api.bitvavo.com/v2"
WS_URL   = "wss://ws.bitvavo.com/v2/"

API_KEY    = os.getenv("BITVAVO_API_KEY")
API_SECRET = os.getenv("BITVAVO_API_SECRET")
BOT_TOKEN  = os.getenv("BOT_TOKEN")
CHAT_ID    = os.getenv("CHAT_ID")
REDIS_URL  = os.getenv("REDIS_URL")

# رسوم تقديرية (bps = جزء من %)
FEE_MAKER_BPS = float(os.getenv("FEE_MAKER_BPS", 12))   # 0.12%
FEE_TAKER_BPS = float(os.getenv("FEE_TAKER_BPS", 25))   # 0.25%

# إعدادات المسح/الترتيب
TOPN_WATCH      = int(os.getenv("TOPN_WATCH", 10))
SCAN_EVERY_SEC  = 24*3600   # مرة كل 24 ساعة
REQUEST_SLEEP   = 0.12      # إبطاء الاستدعاءات

# شروط الترتيب (Features → Score)
MAX_SPREAD_BP   = 220.0
MIN_IMB         = 0.40

# شراء Maker-first
MAKER_POSTONLY        = True
MAKER_PRICE_OFFSET_BP = 3.0     # ندخل فوق أفضل Bid بقليل
MAKER_MAX_REQUOTES    = 3
MAKER_REQUOTE_SEC     = 6
MAKER_WAIT_AFTER_LAST = 10

# إدارة مركز (خروج فقط بالستوبات)
SL_FIXED        = -3.0
TRAIL_ON_AT     = 3.0           # تفعيل التريلينغ بعد +3%
TRAIL_GIVEBACK  = 1.2           # 1.2% من القمة
HOLD_MIN_SEC    = 60            # عدم إغلاق فورًا بعد الدخول

EUR_RESERVE     = 0.00
BUY_MIN_EUR     = 5.00

# ======== Infra ========
r  = redis.from_url(REDIS_URL) if REDIS_URL else None
lk = Lock()

active_trade = {}         # صفقة اليوم (واحدة فقط)
today_rank   = []         # قائمة اليوم (TopN)
used_rank2   = False      # هل جرّبنا #2 بعد SL؟
_last_scan_at = 0

# WS price cache
_ws_prices = {}
_ws_lock   = Lock()
WATCHED=set()
WS_RUN=False

# ======== Utils ========
def tg(txt):
    if not (BOT_TOKEN and CHAT_ID):
        print(txt); return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      data={"chat_id":CHAT_ID,"text":txt}, timeout=8)
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

def fetch_orderbook(m, depth=3):
    try:
        j=requests.get(f"{BASE_URL}/{m}/book", timeout=6).json()
        if j and j.get("bids") and j.get("asks"):
            bids=[(float(p),float(q)) for p,q,*_ in j["bids"][:depth]]
            asks=[(float(p),float(q)) for p,q,*_ in j["asks"][:depth]]
            bb,aa=bids[0][0], asks[0][0]
            spread=((aa-bb)/((aa+bb)/2.0))*10000.0
            bid_eur=sum(p*q for p,q in bids); ask_eur=sum(p*q for p,q in asks)
            imb=bid_eur/max(1e-9, ask_eur)
            return {"spread":spread, "imb":imb, "bb":bb, "aa":aa}
    except Exception: pass
    return None

def place_limit(side, market, price, amount, post_only=True):
    body={
        "market":market, "side":side, "orderType":"limit",
        "price":f"{price:.10f}", "amount":f"{amount:.10f}",
        "timeInForce":"GTC", "clientOrderId":str(uuid4())
    }
    if post_only: body["postOnly"]=True
    return bv_request("POST","/order", body)

def place_market(side, market, amount=None, amount_quote=None):
    body={
        "market":market, "side":side, "orderType":"market",
        "clientOrderId":str(uuid4())
    }
    if side=="buy":  body["amountQuote"]=f"{amount_quote:.2f}"
    else:            body["amount"]=f"{amount:.10f}"
    return bv_request("POST","/order", body)

def cancel_order(market, order_id):
    return bv_request("DELETE", f"/order?market={market}&orderId={order_id}")

def totals_from_fills_eur(fills):
    tb=tq=fee=0.0
    for f in (fills or []):
        amt=float(f["amount"]); price=float(f["price"]); fe=float(f.get("fee",0) or 0)
        tb+=amt; tq+=amt*price; fee+=fe
    return tb,tq,fee

# ======== WS prices ========
def _ws_on_open(ws):
    try:
        mkts=sorted(WATCHED)
        if mkts:
            ws.send(json.dumps({"action":"subscribe","channels":[{"name":"ticker","markets":mkts}]}))
    except Exception: traceback.print_exc()

def _ws_on_message(ws,msg):
    try: d=json.loads(msg)
    except Exception: return
    if d.get("event")=="ticker":
        m=d.get("market"); p=float(d.get("price") or d.get("lastPrice") or 0)
        if p>0:
            with _ws_lock: _ws_prices[m]={"p":p,"ts":time.time()}

def _ws_on_error(ws,err): print("WS err:", err)
def _ws_on_close(ws,c,rn): print("WS closed:", c, rn)

def ws_thread():
    global WS_RUN
    while True:
        try:
            WS_RUN=True
            w=websocket.WebSocketApp(WS_URL, on_open=_ws_on_open,
                                     on_message=_ws_on_message,
                                     on_error=_ws_on_error, on_close=_ws_on_close)
            w.run_forever(ping_interval=25, ping_timeout=10)
        except Exception as e:
            print("WS loop ex:", e)
        finally:
            WS_RUN=False; time.sleep(2)

Thread(target=ws_thread, daemon=True).start()

def price_now(market, staleness=2.0):
    now=time.time()
    with _ws_lock:
        rec=_ws_prices.get(market)
    if rec and (now-rec["ts"])<=staleness: return rec["p"]
    try:
        j=requests.get(f"{BASE_URL}/ticker/price?market={market}", timeout=6).json()
        p=float(j.get("price",0) or 0)
        if p>0:
            with _ws_lock: _ws_prices[market]={"p":p,"ts":now}
            return p
    except Exception: pass
    return None

# ======== Scanner (بطيء، 5m ~3.5d) ========
def list_markets_eur():
    try:
        rows=requests.get(f"{BASE_URL}/ticker/24h", timeout=10).json()
        mkts=[]
        if isinstance(rows,list):
            for r0 in rows:
                m=r0.get("market","")
                if m.endswith("-EUR"):
                    try:
                        last=float(r0.get("last",0) or 0)
                        if last>0: mkts.append(m)
                    except: pass
        return mkts
    except Exception: return []

def candles(market, interval="5m", limit=1000):
    try:
        rows=requests.get(f"{BASE_URL}/{market}/candles?interval={interval}&limit={limit}", timeout=10).json()
        return rows if isinstance(rows,list) else []
    except Exception: return []

def linreg_slope(y):
    n=len(y); 
    if n<5: return 0.0
    xs=range(n); xbar=(n-1)/2.0; ybar=sum(y)/n
    num=sum((x-xbar)*(y[x]-ybar) for x in xs)
    den=sum((x-xbar)*(x-xbar) for x in xs)
    return num/den if den>0 else 0.0

def compute_features(market):
    cs = candles(market, "5m", 1000)   # ~3.5 يوم
    if len(cs)<300: return None
    closes=[float(c[4]) for c in cs]
    vols  =[float(c[5]) for c in cs]
    logs  =[math.log(max(1e-12,p)) for p in closes]

    # اتجاه 3 أيام (تقريبي) مقابل 24 ساعة
    last_1d = logs[-288:] if len(logs)>=288 else logs
    last_3d = logs[-min(1000, len(logs)):]
    slope1d = linreg_slope(last_1d) * 12   # لكل ساعة تقريبًا (5m * 12)
    slope3d = linreg_slope(last_3d) * 12
    accel   = slope1d - slope3d

    # r15 على إطار 5m ≈ 15m = 3 شمعات
    p_now = closes[-1]; p_3 = closes[-4] if len(closes)>=4 else closes[0]
    r15   = ((p_now/p_3)-1.0)*100.0 if p_3>0 else 0.0

    # Bollinger Bandwidth (انقباض)
    def boll_width(arr, n=20):
        if len(arr)<n: return 0.0
        sub=arr[-n:]; m=sum(sub)/n
        var=sum((x-m)**2 for x in sub)/n
        std=math.sqrt(var)
        up, dn = m+2*std, m-2*std
        return (up-dn)/max(1e-12, m)
    bw_now = boll_width(closes, 20)
    # وسط 3 أيام
    bws=[]
    for i in range(60, min(600, len(closes))):
        w=boll_width(closes[:i], 20)
        if w: bws.append(w)
    median_bw = st.median(bws) if bws else bw_now
    squeeze_ratio = bw_now / max(1e-9, median_bw)  # أصغر = انقباض

    # حجم: آخر 30 دقيقة / متوسط 12 ساعة
    v6  = sum(vols[-6:]) / 6.0
    v144= sum(vols[-144:]) / 144.0 if len(vols)>=144 else (sum(vols)/max(1,len(vols)))
    vol_push = v6 / max(1e-9, v144)

    # دفتر أوامر
    ob = fetch_orderbook(market) or {}
    spread=ob.get("spread", 999.0); imb=ob.get("imb", 0.0)

    return {
        "market": market, "price": p_now,
        "slope1d": slope1d, "slope3d": slope3d, "accel": accel, "r15": r15,
        "squeeze": squeeze_ratio, "volx": vol_push,
        "spread": spread, "imb": imb
    }

def score_row(f):
    # تطبيع نقاط 0–100
    accel = max(-0.5, min(0.5, f["accel"]))
    r15   = max(-1.5, min(1.5, f["r15"]))
    sq    = f["squeeze"]
    volx  = max(0.4, min(3.0, f["volx"]))
    spr   = f["spread"]
    imb   = f["imb"]

    mom = 30.0 * max(0.0, min(1.0, (accel-0.02)/0.18)) + 15.0 * max(0.0, min(1.0, (r15-0.05)/0.30))
    sqp = 20.0 * max(0.0, min(1.0, (1.2 - sq)/1.0))         # أصغر أفضل
    vpp = 15.0 * max(0.0, min(1.0, (volx - 0.9)/1.6))
    obp = 10.0 * max(0.0, min(1.0, (200.0 - spr)/150.0)) + 10.0 * max(0.0, min(1.0, (imb - 0.95)/0.6))
    return mom + sqp + vpp + obp

def slow_scan_topN(n=TOPN_WATCH):
    mkts=list_markets_eur()
    rows=[]
    for m in mkts:
        try:
            f=compute_features(m)
            if not f: 
                time.sleep(REQUEST_SLEEP); continue
            if (f["spread"]>MAX_SPREAD_BP) or (f["imb"]<MIN_IMB):
                time.sleep(REQUEST_SLEEP); continue
            s=score_row(f); f["score"]=round(s,2)
            rows.append(f)
        except Exception as e:
            print("scan err", m, e)
        time.sleep(REQUEST_SLEEP)
    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows[:n]

# ======== Trading (Maker-first then fallback) ========
def _round_amt(x): return float(f"{x:.10f}")

def buy_maker_first(market, eur_to_spend):
    ob=fetch_orderbook(market)
    if not ob: return None, "no_ob"
    bb, aa = ob["bb"], ob["aa"]
    price = bb * (1.0 + MAKER_PRICE_OFFSET_BP/10000.0)
    amount=_round_amt(eur_to_spend/price)
    order_id=None
    for attempt in range(MAKER_MAX_REQUOTES+1):
        res=place_limit("buy", market, price, amount, post_only=MAKER_POSTONLY)
        if isinstance(res,dict) and res.get("status") in ("new","partiallyFilled","filled"):
            order_id=res.get("orderId")
            t0=time.time(); wait=MAKER_REQUOTE_SEC if attempt<MAKER_MAX_REQUOTES else MAKER_WAIT_AFTER_LAST
            while time.time()-t0 < wait:
                stt=bv_request("GET", f"/order?market={market}&orderId={order_id}")
                if isinstance(stt,dict) and stt.get("status")=="filled":
                    fills=stt.get("fills",[])
                    tb,tq,fee=totals_from_fills_eur(fills)
                    if tb>0: return {"fills":fills,"maker":True}, "filled_maker"
                time.sleep(1.2)
            cancel_order(market, order_id)
            ob=fetch_orderbook(market)
            if not ob: break
            bb=ob["bb"]; price = bb * (1.0 + MAKER_PRICE_OFFSET_BP/10000.0)
            amount=_round_amt(eur_to_spend/price)
    # fallback taker
    res=place_market("buy", market, amount_quote=eur_to_spend)
    if isinstance(res,dict) and res.get("status")=="filled":
        return {"fills":res.get("fills",[]),"maker":False}, "filled_taker"
    return None, "failed"

def sell_any(market, amount):
    # جرّب Maker أولاً بشكل سريع، ثم تاجر
    ob=fetch_orderbook(market)
    if not ob: return None, "no_ob"
    aa=ob["aa"]; price = max(aa*(1.0+0.0001), aa*(1.0- MAKER_PRICE_OFFSET_BP/10000.0))
    res=place_limit("sell", market, price, amount, post_only=MAKER_POSTONLY)
    if isinstance(res,dict) and res.get("status") in ("new","partiallyFilled","filled"):
        t0=time.time()
        while time.time()-t0 < MAKER_WAIT_AFTER_LAST:
            stt=bv_request("GET", f"/order?market={market}&orderId={res.get('orderId')}")
            if isinstance(stt,dict) and stt.get("status")=="filled":
                return {"fills":stt.get("fills",[]),"maker":True}, "filled_maker"
            time.sleep(1.2)
        cancel_order(market, res.get("orderId"))
    # fallback taker
    res=place_market("sell", market, amount=amount)
    if isinstance(res,dict) and res.get("status")=="filled":
        return {"fills":res.get("fills",[]),"maker":False}, "filled_taker"
    return None, "failed"

# ======== Open / Monitor / Exit ========
def open_from_rank(idx=0):
    global active_trade
    if idx>=len(today_rank): return False
    mkt=today_rank[idx]["market"]
    sym=mkt.replace("-EUR","")
    eur=max(0.0, get_eur_available()-EUR_RESERVE)
    eur=round(eur,2)
    if eur<BUY_MIN_EUR:
        tg(f"🚫 رصيد EUR غير كافٍ للشراء ({eur:.2f})."); return False
    res,how=buy_maker_first(mkt, eur)
    if not res:
        tg(f"❌ فشل شراء {sym}."); return False
    tb,tq,fee=totals_from_fills_eur(res["fills"])
    avg=(tq+fee)/tb if tb>0 else 0.0
    with lk:
        active_trade={"symbol":mkt,"amount":tb,"entry":avg,"opened":time.time(),
                      "peak_pct":0.0,"trail_on":False,"rank_idx":idx}
        WATCHED.add(mkt)  # WS متابعة
    style="Maker" if res.get("maker") else "Taker"
    tg(f"✅ شراء {sym} | €{eur:.2f} | {style} @ €{avg:.6f}")
    return True

def close_and_maybe_try_rank2(reason=""):
    global active_trade, used_rank2
    with lk:
        tr=dict(active_trade) if active_trade else None
    if not tr: return
    m=tr["symbol"]; base=m.replace("-EUR","")
    amt=float(tr["amount"])
    res,how=sell_any(m, amt)
    if isinstance(res,dict):
        tb,tq,fee=totals_from_fills_eur(res.get("fills",[]))
        proceeds=tq-fee
        orig=tr["entry"]*amt
        pnl=(proceeds/orig - 1.0)*100.0 if orig>0 else 0.0
        with lk:
            active_trade={}
        tg(f"💰 بيع {base} | {pnl:+.2f}% — {reason}")
        # إذا كان السبب SL وثاني مرشح متاح ولم يُستخدم:
        if reason.startswith("SL") and (not used_rank2):
            used_rank2=True
            tg("↩️ محاولة المرشّح #2 لليوم…")
            time.sleep(2)
            open_from_rank(1)

def monitor_loop():
    while True:
        try:
            with lk: tr=dict(active_trade) if active_trade else None
            if not tr: time.sleep(0.6); continue
            m=tr["symbol"]; entry=tr["entry"]
            p=price_now(m)
            if not p: time.sleep(0.4); continue
            pnl=((p/entry)-1.0)*100.0
            tr["peak_pct"]=max(tr.get("peak_pct",0.0), pnl)

            # تفعيل التريلينغ بعد +3%
            if (not tr.get("trail_on")) and tr["peak_pct"]>=TRAIL_ON_AT:
                tr["trail_on"]=True

            # Trailing giveback 1.2% من القمّة بعد +3%
            if tr.get("trail_on"):
                drop=tr["peak_pct"] - pnl
                if drop >= TRAIL_GIVEBACK and (time.time()-tr["opened"])>=HOLD_MIN_SEC:
                    close_and_maybe_try_rank2(f"Giveback {drop:.2f}%"); 
                    continue

            # SL ثابت -3%
            if pnl <= SL_FIXED and (time.time()-tr["opened"])>=HOLD_MIN_SEC:
                close_and_maybe_try_rank2(f"SL {SL_FIXED:.2f}%"); 
                continue

            with lk: active_trade.update(tr)
            time.sleep(0.25)
        except Exception as e:
            print("monitor err:", e); time.sleep(1)

Thread(target=monitor_loop, daemon=True).start()

# ======== Daily scheduler ========
def daily_cycle():
    global today_rank, _last_scan_at, used_rank2
    while True:
        now=time.time()
        if (now - _last_scan_at) >= SCAN_EVERY_SEC:
            used_rank2=False
            tg("🔎 بدء مسح بطيء للسوق (3 أيام)…")
            rank=slow_scan_topN(TOPN_WATCH)
            today_rank=rank
            _last_scan_at=now
            if not rank:
                tg("❌ لا مرشحين اليوم."); 
            else:
                lines=["📈 Daily Top Candidates:"]
                for i,f in enumerate(rank,1):
                    lines.append(f"{i:>2}. {f['market'].replace('-EUR',''):<7} | score {f['score']:.1f} | acc {f['accel']:.3f} | r15 {f['r15']:+.2f}% | sq {f['squeeze']:.2f} | vol×{f['volx']:.2f} | ob {f['spread']:.0f}bp/{f['imb']:.2f}")
                tg("\n".join(lines))
                # افتح المرشح الأول
                open_from_rank(0)
                # جهّز WS لمراقبته
                with _ws_lock: 
                    for f in rank[:2]: WATCHED.add(f["market"])
        time.sleep(5)

if __name__=="__main__":
    assert API_KEY and API_SECRET, "Please set BITVAVO_API_KEY / BITVAVO_API_SECRET"
    tg("🟢 Daily One-Pick Trader بدأ العمل.")
    daily_cycle()