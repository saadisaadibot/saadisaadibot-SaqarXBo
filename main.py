# -*- coding: utf-8 -*-
"""
Nems — Daily Simple (Bitvavo EUR)
• مسح بطيء مرة/24h لآخر 3 أيام (5m) → Top10 (لم تنفجر بعد) → شراء Top1 فقط (Maker-first)
• مراقبة سريعة بعد الشراء (SL ثابت + Trailing Giveback)
• أوامر: start/stop, /top10, /summary, /balance
تحذير: التداول ينطوي على مخاطر.
"""

import os, re, time, json, math, statistics as st, traceback
from collections import deque
from threading import Thread, Lock
from uuid import uuid4

import requests, redis, websocket
from flask import Flask, request
from dotenv import load_dotenv
load_dotenv()

# ========= ENV / App =========
app = Flask(__name__)
BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHAT_ID     = os.getenv("CHAT_ID")
API_KEY     = os.getenv("BITVAVO_API_KEY")
API_SECRET  = os.getenv("BITVAVO_API_SECRET")
REDIS_URL   = os.getenv("REDIS_URL")
RUN_LOCAL   = os.getenv("RUN_LOCAL","0")=="1"

BASE_URL    = "https://api.bitvavo.com/v2"
WS_URL      = "wss://ws.bitvavo.com/v2/"

# ========= رسوم (bps) =========
FEE_MAKER_BPS = float(os.getenv("FEE_MAKER_BPS", 12))    # 0.12%
FEE_TAKER_BPS = float(os.getenv("FEE_TAKER_BPS", 25))    # 0.25%

# ========= Daily scan (3d, 5m) =========
DAILY_ENABLED         = True
DAILY_TOPN            = int(os.getenv("DAILY_TOPN", 10))
DAILY_SCAN_EVERY_SEC  = 24*3600      # مرة باليوم
DAILY_REQUEST_SLEEP   = 0.12         # سرعة الجمع (بطيء = دقيق)

# فلتر دفتر الأوامر خلال المسح
THRESH_SPREAD_BP_MAX  = 220.0
THRESH_IMB_MIN        = 0.40

# “لم تنفجر بعد” (يتم استبعاد الأزواج التي اندفعت مؤخرًا)
MAX_RET_1H            = 2.2          # % خلال 1h
MAX_RET_6H            = 6.0          # % خلال 6h

# ========= إدارة مركز بعد الشراء =========
EUR_RESERVE           = 0.00
BUY_MIN_EUR           = 5.00

SL_FIXED              = -3.0          # ستوب ثابت
TRAIL_ON_AT           = 3.0           # فعّل التريلينغ بعد +3%
TRAIL_GIVEBACK        = 1.2           # -1.2% من القمة
HOLD_MIN_SEC          = 60            # لا خروج فوري
WS_STALENESS_SEC      = 2.0

# ========= حالة =========
r  = redis.from_url(REDIS_URL) if REDIS_URL else redis.Redis()
lk = Lock()
enabled = True

active_trade = {}
executed_trades = []
today_top = []
_last_daily_scan = 0

# ========= إرسال تيليغرام =========
def send_message(text, force=False):
    try:
        if not (BOT_TOKEN and CHAT_ID):
            print("TG:", text); return
        if not force:
            key="dedup:"+str(abs(hash(text))%(10**12))
            if not r.setnx(key,1): return
            r.expire(key, 60)
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      data={"chat_id":CHAT_ID,"text":text}, timeout=8)
    except Exception as e: print("TG err:", e)

# ========= Bitvavo auth =========
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

# ========= Orderbook / WS (للمراقبة بعد الشراء فقط) =========
_ws_lock=Lock()
_ws_price={}
def _ws_on_open(ws):
    try:
        mkts=[active_trade["symbol"]] if active_trade else []
        if mkts:
            ws.send(json.dumps({"action":"subscribe","channels":[{"name":"ticker","markets":mkts}]}))
    except Exception: traceback.print_exc()

def _ws_on_message(ws,msg):
    try: d=json.loads(msg)
    except Exception: return
    if d.get("event")=="ticker":
        m=d.get("market"); p=float(d.get("price") or d.get("lastPrice") or 0)
        if p>0:
            with _ws_lock: _ws_price[m]={"p":p,"ts":time.time()}

def _ws_on_error(ws,err): print("WS err:", err)
def _ws_on_close(ws,c,rn): print("WS closed:", c, rn)

def ws_thread():
    while True:
        try:
            w=websocket.WebSocketApp(WS_URL, on_open=_ws_on_open, on_message=_ws_on_message,
                                     on_error=_ws_on_error, on_close=_ws_on_close)
            w.run_forever(ping_interval=25, ping_timeout=10)
        except Exception as e:
            print("WS loop ex:", e)
        time.sleep(2)
Thread(target=ws_thread, daemon=True).start()

def fetch_price_ws_first(market):
    now=time.time()
    with _ws_lock:
        rec=_ws_price.get(market)
    if rec and (now-rec["ts"])<=WS_STALENESS_SEC: return rec["p"]
    try:
        j=requests.get(f"{BASE_URL}/ticker/price?market={market}", timeout=5).json()
        p=float(j.get("price",0) or 0)
        if p>0:
            with _ws_lock: _ws_price[market]={"p":p,"ts":now}
            return p
    except Exception: pass
    return None

def fetch_orderbook(market, depth=3):
    try:
        j=requests.get(f"{BASE_URL}/{market}/book", timeout=6).json()
        if not j or not j.get("bids") or not j.get("asks"): return None
        bids=[(float(p),float(q)) for p,q,*_ in j["bids"][:depth]]
        asks=[(float(p),float(q)) for p,q,*_ in j["asks"][:depth]]
        bb,aa=bids[0][0], asks[0][0]
        spread=((aa-bb)/((aa+bb)/2.0))*10000.0
        bid_eur=sum(p*q for p,q in bids); ask_eur=sum(p*q for p,q in asks)
        imb=bid_eur/max(1e-9, ask_eur)
        return {"spread":spread,"imb":imb,"bb":bb,"aa":aa}
    except Exception: return None

# ========= أوامر تداول =========
def _round_amount(x): return float(f"{x:.10f}")

def place_limit(side, market, price, amount, post_only=True):
    body={"market":market,"side":side,"orderType":"limit",
          "price":f"{price:.10f}","amount":f"{amount:.10f}",
          "timeInForce":"GTC","clientOrderId":str(uuid4())}
    if post_only: body["postOnly"]=True
    return bv_request("POST","/order", body)

def place_market(side, market, amount=None, amount_quote=None):
    body={"market":market,"side":side,"orderType":"market","clientOrderId":str(uuid4())}
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

def maker_to_taker_buy(market, eur_to_spend):
    ob=fetch_orderbook(market)
    if not ob: return None, "no_ob"
    bb=ob["bb"]
    price = bb*(1.0 + 3.0/10000.0)     # +3bps فوق الـBid
    amount=_round_amount(eur_to_spend/price)
    order_id=None
    for attempt in range(3+1):
        res=place_limit("buy", market, price, amount, post_only=True)
        if isinstance(res,dict) and res.get("status") in ("new","partiallyFilled","filled"):
            order_id=res.get("orderId"); t0=time.time(); wait=6 if attempt<3 else 10
            while time.time()-t0<wait:
                stt=bv_request("GET", f"/order?market={market}&orderId={order_id}")
                if isinstance(stt,dict) and stt.get("status")=="filled":
                    tb,tq,fee=totals_from_fills_eur(stt.get("fills",[]))
                    if tb>0: return {"fills":stt.get("fills",[]),"maker":True},"filled_maker"
                time.sleep(1.2)
            cancel_order(market, order_id)
            ob=fetch_orderbook(market); 
            if not ob: break
            bb=ob["bb"]; price=bb*(1.0 + 3.0/10000.0)
            amount=_round_amount(eur_to_spend/price)
    # fallback taker
    res=place_market("buy", market, amount_quote=eur_to_spend)
    if isinstance(res,dict) and res.get("status")=="filled":
        return {"fills":res.get("fills",[]),"maker":False}, "filled_taker"
    return None, "failed"

def maker_to_taker_sell(market, amount):
    ob=fetch_orderbook(market)
    if not ob: return None, "no_ob"
    aa=ob["aa"]
    price = max(aa*(1.0+0.0001), aa*(1.0 - 3.0/10000.0))
    res=place_limit("sell", market, price, amount, post_only=True)
    if isinstance(res,dict) and res.get("status") in ("new","partiallyFilled","filled"):
        t0=time.time()
        while time.time()-t0<10:
            stt=bv_request("GET", f"/order?market={market}&orderId={res.get('orderId')}")
            if isinstance(stt,dict) and stt.get("status")=="filled":
                return {"fills":stt.get("fills",[]),"maker":True},"filled_maker"
            time.sleep(1.2)
        cancel_order(market, res.get("orderId"))
    # fallback
    res=place_market("sell", market, amount=amount)
    if isinstance(res,dict) and res.get("status")=="filled":
        return {"fills":res.get("fills",[]),"maker":False},"filled_taker"
    return None, "failed"

# ========= Daily Scan (3 days, 5m) =========
def _t_fresh():
    # إستعمال /ticker/24h لجلب قائمة الأسواق
    try:
        rows=requests.get(f"{BASE_URL}/ticker/24h", timeout=10).json()
        return rows if isinstance(rows,list) else []
    except Exception: return []

def _list_eur_markets():
    mk=[]
    for r0 in _t_fresh():
        m=r0.get("market","")
        if m.endswith("-EUR"):
            try:
                if float(r0.get("last",0) or 0) > 0: mk.append(m)
            except: pass
    return mk

def candles_5m(market, limit=1000):
    try:
        rows=requests.get(f"{BASE_URL}/{market}/candles?interval=5m&limit={limit}", timeout=10).json()
        return rows if isinstance(rows,list) else []
    except Exception: return []

def _linreg_slope(y):
    n=len(y); 
    if n<5: return 0.0
    xs=range(n); xbar=(n-1)/2.0; ybar=sum(y)/n
    num=sum((x-xbar)*(y[x]-ybar) for x in xs)
    den=sum((x-xbar)*(x-xbar) for x in xs)
    return num/den if den>0 else 0.0

def _boll_width(arr, n=20):
    if len(arr)<n: return 0.0
    sub=arr[-n:]; m=sum(sub)/n
    var=sum((x-m)**2 for x in sub)/n
    std=math.sqrt(var)
    up, dn = m+2*std, m-2*std
    return (up-dn)/max(1e-12, m)

def _compute_features(market):
    cs = candles_5m(market, 1000)     # ~3.5d
    if len(cs)<300: return None
    closes=[float(c[4]) for c in cs]
    vols  =[float(c[5]) for c in cs]
    logs  =[math.log(max(1e-12,p)) for p in closes]

    # 3 نوافذ: 1h/6h/3d
    def ret_n(n): 
        if len(closes)<=n: return 0.0
        return ((closes[-1]/closes[-n]) - 1.0)*100.0
    ret_1h  = ret_n(12)     # 12×5m
    ret_6h  = ret_n(72)     # 72×5m

    last_1d = logs[-288:] if len(logs)>=288 else logs
    last_3d = logs[-min(1000,len(logs)):]
    slope1d = _linreg_slope(last_1d) * 12
    slope3d = _linreg_slope(last_3d) * 12
    accel   = slope1d - slope3d

    # r15 على 5m (≈15m)
    p_now=closes[-1]; p_3=closes[-4] if len(closes)>=4 else closes[0]
    r15 = ((p_now/p_3)-1.0)*100.0 if p_3>0 else 0.0

    # انقباض
    bw_now=_boll_width(closes,20)
    bws=[_boll_width(closes[:i],20) for i in range(60, min(600,len(closes)))]
    med_bw = (sorted([w for w in bws if w])[len([w for w in bws if w])//2] if any(bws) else bw_now) or bw_now
    squeeze = bw_now / max(1e-9, med_bw)  # أصغر أفضل

    # حجم: آخر 30m / متوسط 12h
    v6  = sum(vols[-6:])/6.0
    v144= (sum(vols[-144:])/144.0) if len(vols)>=144 else (sum(vols)/max(1,len(vols)))
    volx = v6 / max(1e-9, v144)

    # دفتر
    ob = fetch_orderbook(market) or {}
    spread=ob.get("spread", 999.0); imb=ob.get("imb", 0.0)

    return {
        "market":market, "price":p_now,
        "accel":accel, "r15":r15, "squeeze":squeeze, "volx":volx,
        "spread":spread, "imb":imb,
        "ret_1h":ret_1h, "ret_6h":ret_6h
    }

def _score_row(f):
    # استبعاد “منفجرة” حديثاً
    if f["ret_1h"]>MAX_RET_1H or f["ret_6h"]>MAX_RET_6H:
        return -1.0
    if f["spread"]>THRESH_SPREAD_BP_MAX or f["imb"]<THRESH_IMB_MIN:
        return -1.0

    accel = max(-0.5, min(0.5, f["accel"]))
    r15   = max(-1.5, min(1.5, f["r15"]))
    sq    = f["squeeze"]
    volx  = max(0.4, min(3.0, f["volx"]))
    spr   = f["spread"]; imb=f["imb"]

    mom = 30.0*max(0.0, min(1.0, (accel-0.02)/0.18)) + 15.0*max(0.0, min(1.0, (r15-0.05)/0.30))
    sqp = 20.0*max(0.0, min(1.0, (1.2 - sq)/1.0))           # أصغر = أضيق
    vpp = 15.0*max(0.0, min(1.0, (volx - 0.9)/1.6))
    obp = 10.0*max(0.0, min(1.0, (200.0 - spr)/150.0)) + 10.0*max(0.0, min(1.0, (imb - 0.95)/0.6))
    return mom + sqp + vpp + obp

def daily_scan_top():
    mkts=_list_eur_markets()
    rows=[]
    for m in mkts:
        try:
            f=_compute_features(m)
            if not f: 
                time.sleep(DAILY_REQUEST_SLEEP); continue
            s=_score_row(f)
            if s>=0:
                f["score"]=round(s,2)
                rows.append(f)
        except Exception as e:
            print("scan err", m, e)
        time.sleep(DAILY_REQUEST_SLEEP)
    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows[:DAILY_TOPN]

# ========= فتح/إغلاق ومراقبة =========
def open_position_from_top(idx=0):
    global active_trade
    if idx>=len(today_top): return False
    mkt=today_top[idx]["market"]; base=mkt.replace("-EUR","")
    eur=round(max(0.0, get_eur_available()-EUR_RESERVE),2)
    if eur<BUY_MIN_EUR:
        send_message(f"🚫 EUR غير كافٍ: {eur:.2f}"); return False
    res,how=maker_to_taker_buy(mkt, eur)
    if not res: 
        send_message(f"❌ فشل شراء {base}"); return False
    tb,tq,fee=totals_from_fills_eur(res["fills"])
    avg=(tq+fee)/tb if tb>0 else 0.0
    with lk:
        active_trade={"symbol":mkt,"entry":avg,"amount":tb,"cost_eur":tq+fee,
                      "buy_fee_eur":fee,"opened_at":time.time(),"peak_pct":0.0}
    style="Maker" if res.get("maker") else "Taker"
    send_message(f"✅ شراء {base} (Top1) | €{eur:.2f} | {style} @ €{avg:.6f}")
    return True

def close_position(reason=""):
    global active_trade
    with lk: tr=dict(active_trade) if active_trade else None
    if not tr: return
    m=tr["symbol"]; base=m.replace("-EUR","")
    amt=float(tr["amount"])
    res,how=maker_to_taker_sell(m, amt)
    if isinstance(res,dict):
        tb,tq,fee=totals_from_fills_eur(res.get("fills",[]))
        proceeds=tq-fee
        orig=tr["entry"]*amt
        pnl_pct=(proceeds/orig - 1.0)*100.0 if orig>0 else 0.0
        with lk: active_trade={}
        send_message(f"💰 بيع {base} | {pnl_pct:+.2f}% — {reason}")

def monitor_loop():
    while True:
        try:
            with lk: tr=dict(active_trade) if active_trade else None
            if not tr: time.sleep(0.5); continue
            m=tr["symbol"]; p=fetch_price_ws_first(m)
            if not p: time.sleep(0.3); continue
            entry=tr["entry"]; pnl=((p/entry)-1.0)*100.0
            tr["peak_pct"]=max(tr.get("peak_pct",0.0), pnl)

            if (not tr.get("trail_on")) and tr["peak_pct"]>=TRAIL_ON_AT:
                tr["trail_on"]=True

            if tr.get("trail_on"):
                drop=tr["peak_pct"] - pnl
                if drop>=TRAIL_GIVEBACK and (time.time()-tr["opened_at"])>=HOLD_MIN_SEC:
                    close_position(f"Giveback {drop:.2f}%"); continue

            if pnl<=SL_FIXED and (time.time()-tr["opened_at"])>=HOLD_MIN_SEC:
                close_position(f"SL {SL_FIXED:.2f}%"); continue

            with lk: active_trade.update(tr)
            time.sleep(0.25)
        except Exception as e:
            print("monitor err:", e); time.sleep(1)
Thread(target=monitor_loop, daemon=True).start()

# ========= Daily scheduler =========
def daily_scheduler():
    global today_top, _last_daily_scan
    while True:
        try:
            now=time.time()
            if enabled and DAILY_ENABLED and (now-_last_daily_scan)>=DAILY_SCAN_EVERY_SEC:
                send_message("🔎 Daily: بدء مسح (3 أيام / 5m)…")
                top=daily_scan_top()
                today_top=top; _last_daily_scan=now
                if not top:
                    send_message("❌ Daily: لا مرشحين اليوم.")
                else:
                    lines=["📈 Daily Top 10 (لم تنفجر بعد):"]
                    for i,f in enumerate(top,1):
                        lines.append(f"{i:>2}. {f['market'].replace('-EUR',''):<7} | score {f['score']:.1f} | acc {f['accel']:.3f} | r15 {f['r15']:+.2f}% | sq {f['squeeze']:.2f} | vol×{f['volx']:.2f} | ob {f['spread']:.0f}bp/{f['imb']:.2f} | 1h {f['ret_1h']:+.2f}% | 6h {f['ret_6h']:+.2f}%")
                    send_message("\n".join(lines))
                    open_position_from_top(0)
            time.sleep(5)
        except Exception as e:
            print("daily sched err:", e); time.sleep(3)
Thread(target=daily_scheduler, daemon=True).start()

# ========= Summary / Balance (بدون تغيير تقريبًا) =========
def build_summary():
    lines=[]
    with lk: tr=dict(active_trade) if active_trade else {}
    if tr:
        cur=fetch_price_ws_first(tr["symbol"]) or tr["entry"]
        pnl=((cur-tr["entry"])/tr["entry"])*100.0
        peak=float(tr.get("peak_pct",0.0))
        base=tr["symbol"].replace("-EUR","")
        lines.append("📌 الصفقة النشطة:")
        lines.append(f"• {base}: {pnl:+.2f}% | Peak {peak:.2f}%")
    else:
        lines.append("📌 لا صفقات نشطة.")
    if today_top:
        best=today_top[0]
        lines.append("\n⭐️ مرشح اليوم: "+best["market"].replace("-EUR","")+f" | score {best['score']:.1f}")
    return "\n".join(lines)

# ========= Telegram =========
@app.route("/", methods=["POST"])
def webhook():
    global enabled, DAILY_ENABLED, _last_daily_scan
    data=request.get_json(silent=True) or {}
    text=(data.get("message",{}).get("text") or data.get("text") or "").strip()
    if not text: return "ok"
    low=text.lower()

    def has(*k): return any(x in low for x in k)
    def starts(*k): return any(low.startswith(x) for x in k)

    if has("start","تشغيل","ابدأ"):
        enabled=True; send_message("✅ تم التفعيل."); return "ok"
    if has("stop","قف","ايقاف","إيقاف"):
        enabled=False; send_message("🛑 تم الإيقاف."); return "ok"

    if has("daily on","daily_on","اختيار يومي تشغيل"):
        DAILY_ENABLED=True; send_message("🟢 Daily: ON"); return "ok"
    if has("daily off","daily_off","اختيار يومي ايقاف","اختيار يومي إيقاف"):
        DAILY_ENABLED=False; send_message("🔴 Daily: OFF"); return "ok"
    if has("daily now","daily scan","مسح يومي"):
        _last_daily_scan=0; send_message("⏱️ Daily: سيتم المسح الآن."); return "ok"

    if has("top10","قائمة","القائمة"):
        if not today_top: send_message("🚫 لا قائمة اليوم بعد."); return "ok"
        lines=["📈 Top 10 اليوم:"]
        for i,f in enumerate(today_top,1):
            lines.append(f"{i:>2}. {f['market'].replace('-EUR',''):<7} | score {f['score']:.1f} | acc {f['accel']:.3f} | r15 {f['r15']:+.2f}% | sq {f['squeeze']:.2f} | vol×{f['volx']:.2f}")
        send_message("\n".join(lines)); return "ok"

    if has("summary","ملخص","الملخص"):
        send_message(build_summary()); return "ok"

    if has("balance","الرصيد","رصيد"):
        bals=bv_request("GET","/balance")
        if not isinstance(bals,list): send_message("❌ تعذر جلب الرصيد."); return "ok"
        eur=sum(float(b.get("available",0))+float(b.get("inOrder",0)) for b in bals if b.get("symbol")=="EUR")
        total=eur; winners,losers=[],[]
        for b in bals:
            sym=b.get("symbol")
            if sym=="EUR": continue
            qty=float(b.get("available",0))+float(b.get("inOrder",0))
            if qty<0.0001: continue
            pair=f"{sym}-EUR"; price=fetch_price_ws_first(pair)
            if price is None: continue
            total+=qty*price
            line=f"{sym}: {qty:.4f} @ €{price:.4f}"
            winners.append(line)
        lines=[f"💰 الرصيد الإجمالي (تقديري): €{total:.2f}", f"💶 EUR: €{eur:.2f}"]
        if winners: lines.append("\n📦 مراكز:\n"+"\n".join(winners))
        send_message("\n".join(lines)); return "ok"

    if starts("buy","اشتري","إشتري"):
        try:
            sym=re.search(r"[A-Za-z0-9\-]+", text).group(0).upper()
            if "-" in sym: sym=sym.split("-")[0]
            if sym.endswith("EUR") and len(sym)>3: sym=sym[:-3]
            sym=re.sub(r"[^A-Z0-9]","", sym)
        except Exception:
            send_message("❌ الصيغة: buy ADA"); return "ok"
        open_position_from_top(0) if sym=="TOP1" else open_position_from_top(0)
        return "ok"

    if has("flat","اغلق","سكر","بيع الكل"):
        close_position("Manual"); return "ok"

    return "ok"

# ========= Local run =========
if __name__=="__main__" and RUN_LOCAL:
    app.run(host="0.0.0.0", port=5000)