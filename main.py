# -*- coding: utf-8 -*-
"""
Nems — Momentum+Accel Scalper (Bitvavo EUR) — 100% نسخة جديدة مبنية على الاستراتيجية الأخيرة
- اختيار الأسواق: Top40 حسب "ذبذبة 1m + حركة 5m" (vol+move) بدل حجم 24h فقط
- Backfill: تعبئة 1m candles سريع لكل سوق جديد (90 دقيقة) كي تعمل المقاييس من أول دقيقة
- إشارة الدخول: Score (زخم + تسارع + دفتر أوامر) أو Sniper Trigger
- Replacement: استبدال أضعف صفقة بفرصة أقوى إذا الفرق كبير
- إدارة مركز: شراء موحد 50% ثم 50%، حد يومي للخسارة، تبريد بن/كولدَون، حظر 24h بعد خسارتين
- خروج: هدف طبقي سريع، Giveback أخف من القمة، SL ديناميكي، Time-Stop مبكر
- WebSocket للأسعار + REST فallback
- أوامر تلغرام: /start /stop /summary /settings /balance /reset /banlist /unban COIN /buy COIN

ملاحظات:
- اضبط رسوم Bitvavo ونقطة التعادل عندك (taker/maker) ثم عدّل TP/SL حسبها.
- هذه النسخة تشدد على السرعة وتكرار الصفقات مع حراسة السيولة/السبريد.
"""

import os, re, time, json, math, traceback, statistics as st
import requests, redis, threading
from threading import Thread, Lock
from collections import deque, defaultdict
from uuid import uuid4
from flask import Flask, request
from dotenv import load_dotenv
import websocket

# ========= Boot / ENV =========
load_dotenv()
app = Flask(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHAT_ID     = os.getenv("CHAT_ID")
API_KEY     = os.getenv("BITVAVO_API_KEY")
API_SECRET  = os.getenv("BITVAVO_API_SECRET")
REDIS_URL   = os.getenv("REDIS_URL")
RUN_LOCAL   = os.getenv("RUN_LOCAL", "0") == "1"

r  = redis.from_url(REDIS_URL) if REDIS_URL else redis.Redis()
lk = Lock()

BASE_URL    = "https://api.bitvavo.com/v2"
WS_URL      = "wss://ws.bitvavo.com/v2/"

# ========= إعدادات عامة =========
MAX_TRADES              = 2            # دائمًا صفقتان كحد أقصى
ENGINE_INTERVAL_SEC     = 0.5          # حلقة الإشارة أسرع
TOPN_WATCH              = 40           # أسواق مراقبة فعّالة
AUTO_THRESHOLD          = 40.0         # عتبة دخول عامة للـ Score
THRESH_SPREAD_BP_MAX    = 140.0        # سقف السبريد المقبول
THRESH_IMB_MIN          = 0.85         # حد أدنى لميل الـ bids/asks
DAILY_STOP_EUR          = -8.0         # حد خسارة يومي
BUY_COOLDOWN_SEC        = 90           # تهدئة بعد شراء/بيع
BAN_LOSS_PCT            = -3.0         # حظر 24h لو خسارة كبيرة
CONSEC_LOSS_BAN         = 2            # حظر بعد خسارتين متتاليتين
BLACKLIST_EXPIRE_SECONDS= 300

# خروج / SL / TP
EARLY_WINDOW_SEC        = 9*60         # نافذة مبكرة أقصر
DYN_SL_START            = -2.2         # SL ابتدائي (سلمي)
DYN_SL_STEP             = 1.0          # يعلو 1% لكل +1%
DROP_FROM_PEAK_EXIT     = 1.2          # انعكاس من القمة
PEAK_TRIGGER            = 2.2          # بدء giveback من القمة
GIVEBACK_CAP            = 1.1          # سقف giveback
GIVEBACK_RATIO          = 0.40         # نسبة giveback من القمة
TP_WEAK                 = 1.2          # هدف سريع عند OB/ZM ضعيفين
TP_GOOD                 = 1.8          # هدف جيد عندما الظروف جيدة
TIME_STOP_MIN           = 10*60        # وقت توقف مبكر
TIME_STOP_PNL_LO        = -0.3
TIME_STOP_PNL_HI        = 0.6

# WS & Price Cache
_ws_lock      = Lock()
_ws_prices    = {}   # market -> {price, ts}
_ws_conn      = None
_ws_running   = False

# Watchlist & Histories
WATCHLIST_MARKETS = set()
_prev_watch       = set()
HISTS             = {}   # market -> {"hist": deque[(ts,price)], "last_new_high": ts}
OB_CACHE          = {}   # market -> {"data": ob, "ts": time}

# State
enabled        = True
auto_enabled   = True
active_trades  = []
executed_trades= []
SINCE_RESET_KEY= "nems:since_reset"

# ========= Utilities =========
def send_message(text: str):
    try:
        if not (BOT_TOKEN and CHAT_ID):
            print("TG:", text); return
        key = "dedup:" + str(abs(hash(text)) % (10**12))
        if r.setnx(key, 1):
            r.expire(key, 60)
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": text},
                timeout=8
            )
    except Exception as e:
        print("telegram err:", e)

def create_sig(ts, method, path, body_str=""):
    import hmac, hashlib
    msg = f"{ts}{method}{path}{body_str}"
    return hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def bv_request(method: str, path: str, body=None, timeout=10):
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
        return resp.json()
    except Exception as e:
        print("bv_request error:", e)
        return {"error":"request_failed"}

def get_eur_available()->float:
    try:
        bals = bv_request("GET","/balance")
        if isinstance(bals, list):
            for b in bals:
                if b.get("symbol") == "EUR":
                    return max(0.0, float(b.get("available",0) or 0))
    except Exception: pass
    return 0.0

def fetch_price_ws_first(market: str, staleness=2.0):
    now = time.time()
    with _ws_lock:
        rec = _ws_prices.get(market)
    if rec and (now-rec["ts"])<=staleness:
        return rec["price"]
    try:
        res = requests.get(f"{BASE_URL}/ticker/price?market={market}", timeout=6).json()
        p = float(res.get("price",0) or 0)
        if p>0:
            with _ws_lock:
                _ws_prices[market] = {"price":p, "ts":now}
            return p
    except Exception: pass
    return None

def _today_key(): return time.strftime("pnl:%Y%m%d", time.gmtime())
def _accum_realized(pnl):
    try:
        r.incrbyfloat(_today_key(), float(pnl))
        r.expire(_today_key(), 3*24*3600)
    except Exception: pass
def _today_pnl():
    try: return float(r.get(_today_key()) or 0.0)
    except Exception: return 0.0

# ========= WebSocket =========
def _ws_sub_payload(markets): return {"action":"subscribe","channels":[{"name":"ticker","markets":markets}]}
def _ws_on_open(ws):
    try:
        mkts = sorted(WATCHLIST_MARKETS | {t["symbol"] for t in active_trades})
        if mkts: ws.send(json.dumps(_ws_sub_payload(mkts)))
    except Exception: traceback.print_exc()
def _ws_on_message(ws, message):
    try: msg = json.loads(message)
    except Exception: return
    if isinstance(msg, dict) and msg.get("event") in ("subscribe","subscribed"):
        return
    if isinstance(msg, dict) and msg.get("event")=="ticker":
        mkt = msg.get("market")
        price = msg.get("price") or msg.get("lastPrice") or msg.get("open")
        try:
            p = float(price)
            if p>0:
                with _ws_lock:
                    _ws_prices[mkt] = {"price":p, "ts":time.time()}
        except Exception: pass
def _ws_on_error(ws, err): print("WS error:", err)
def _ws_on_close(ws, code, reason):
    global _ws_running; _ws_running=False
    print("WS closed:", code, reason)
def _ws_thread():
    global _ws_conn,_ws_running
    while True:
        try:
            _ws_running=True
            _ws_conn = websocket.WebSocketApp(
                WS_URL, on_open=_ws_on_open, on_message=_ws_on_message,
                on_error=_ws_on_error, on_close=_ws_on_close
            )
            _ws_conn.run_forever(ping_interval=25, ping_timeout=10)
        except Exception as e:
            print("ws loop ex:", e)
        finally:
            _ws_running=False
            time.sleep(2)
Thread(target=_ws_thread, daemon=True).start()

# ========= Histories =========
def _init_hist_key(market):
    if market not in HISTS:
        HISTS[market] = {"hist": deque(maxlen=900), "last_new_high": time.time()}
def _update_hist_key(market, ts, price):
    _init_hist_key(market)
    HISTS[market]["hist"].append((ts, price))
    cutoff = ts - 300  # 5 دقائق
    while HISTS[market]["hist"] and HISTS[market]["hist"][0][0] < cutoff:
        HISTS[market]["hist"].popleft()

def _mom_metrics_symbol(market, price_now):
    _init_hist_key(market)
    hist = HISTS[market]["hist"]
    if not hist: return 0.0,0.0,0.0,False
    now_ts = hist[-1][0]
    p15=p30=p60=None; hi=price_now; lo=price_now
    for ts,p in hist:
        hi=max(hi,p); lo=min(lo,p)
        age = now_ts - ts
        if p15 is None and age>=15: p15=p
        if p30 is None and age>=30: p30=p
        if p60 is None and age>=60: p60=p
    base = hist[0][1]
    if p15 is None: p15=base
    if p30 is None: p30=base
    if p60 is None: p60=base
    r15 = (price_now/p15-1.0)*100.0 if p15>0 else 0.0
    r30 = (price_now/p30-1.0)*100.0 if p30>0 else 0.0
    r60 = (price_now/p60-1.0)*100.0 if p60>0 else 0.0
    new_high = price_now >= hi*0.999
    if new_high: HISTS[market]["last_new_high"] = now_ts
    return r15, r30, r60, new_high

def _backfill_hist_1m(market, minutes=90):
    try:
        url = f"{BASE_URL}/{market}/candles?interval=1m&limit={minutes}"
        rows = requests.get(url, timeout=6).json()
        if not isinstance(rows, list): return
        for ts,o,h,l,c,v in rows[-minutes:]:
            _update_hist_key(market, ts/1000.0, float(c))
        p = fetch_price_ws_first(market)
        if p: _update_hist_key(market, time.time(), p)
    except Exception as e:
        print("backfill err:", e)

# ========= Orderbook =========
def fetch_orderbook(market, ttl=2.5):
    now=time.time()
    rec=OB_CACHE.get(market)
    if rec and (now-rec["ts"])<ttl: return rec["data"]
    try:
        data = requests.get(f"{BASE_URL}/{market}/book", timeout=5).json()
        if data and data.get("bids") and data.get("asks"):
            OB_CACHE[market]={"data":data,"ts":now}
            return data
    except Exception: pass
    return None

def orderbook_guard(market, min_bid_eur=60.0, req_imb=THRESH_IMB_MIN, max_spread_bp=THRESH_SPREAD_BP_MAX, depth_used=3):
    ob = fetch_orderbook(market)
    if not ob or not ob.get("bids") or not ob.get("asks"):
        return False,"no_book",{}
    try:
        bid_p=float(ob["bids"][0][0]); ask_p=float(ob["asks"][0][0])
        bid_eur=sum(float(p)*float(q) for p,q,*_ in ob["bids"][:depth_used])
        ask_eur=sum(float(p)*float(q) for p,q,*_ in ob["asks"][:depth_used])
    except Exception:
        return False,"bad_book",{}
    spread_bp = (ask_p-bid_p)/((ask_p+bid_p)/2.0)*10000.0
    imb = bid_eur/max(1e-9, ask_eur)
    if bid_eur < min_bid_eur:
        return False,f"low_liq:{bid_eur:.0f}",{"spread_bp":spread_bp,"imb":imb,"bid_eur":bid_eur}
    if spread_bp > max_spread_bp:
        return False,f"wide_spread:{spread_bp:.0f}",{"spread_bp":spread_bp,"imb":imb,"bid_eur":bid_eur}
    if imb < req_imb:
        return False,f"weak_imb:{imb:.2f}",{"spread_bp":spread_bp,"imb":imb,"bid_eur":bid_eur}
    return True,"ok",{"spread_bp":spread_bp,"imb":imb,"bid_eur":bid_eur}

# ========= اختيار الأسواق =========
_ticker24_cache = {"ts":0, "rows":[]}
def _t_rows(): return _ticker24_cache.get("rows",[])
def _t_set(ts,rows): _ticker24_cache.update({"ts":ts,"rows":rows})
def _t_fresh():
    now=time.time()
    if now-_ticker24_cache.get("ts",0)<60: return _t_rows()
    try:
        rows = requests.get(f"{BASE_URL}/ticker/24h", timeout=8).json()
        if isinstance(rows,list):
            _t_set(now, rows)
            return rows
    except Exception: pass
    return _t_rows()

def _score_market_volvol(market):
    """يعطي Score للترتيب الأولي: 0..100 تقريبًا حسب std% 1m و |move 5m|"""
    try:
        cs = requests.get(f"{BASE_URL}/{market}/candles?interval=1m&limit=20", timeout=4).json()
        closes = [float(c[4]) for c in cs if isinstance(c,list) and len(c)>=5]
        if len(closes)<6: return 0.0
        rets = [closes[i]/closes[i-1]-1.0 for i in range(1,len(closes))]
        v1 = abs(sum(rets[-5:]))*100.0          # حركة 5د (%)
        stdv = (st.pstdev(rets)*100.0) if len(rets)>2 else 0.0
        return 0.6*stdv + 0.4*v1
    except Exception:
        return 0.0

def build_watchlist(n=TOPN_WATCH):
    rows = _t_fresh()
    picks=[]
    for r0 in rows:
        mkt=r0.get("market","")
        if not mkt.endswith("-EUR"): continue
        try:
            last=float(r0.get("last",0) or 0)
            if last<=0: continue
        except Exception: continue
        s = _score_market_volvol(mkt)
        if s>0: picks.append((s,mkt))
    picks.sort(reverse=True)
    return [m for _,m in picks[:n]]

def refresh_watchlist():
    global WATCHLIST_MARKETS,_prev_watch
    new_list = set(build_watchlist(TOPN_WATCH))
    with _ws_lock:
        WATCHLIST_MARKETS = set(new_list)
    newly = new_list - _prev_watch
    for m in newly:
        _backfill_hist_1m(m, minutes=90)
    _prev_watch = set(new_list)

# ========= Scoring / Triggers =========
def score_exploder(market, price_now):
    r15,r30,r60,_ = _mom_metrics_symbol(market, price_now)
    accel = r30 - r60
    ok,_,feats = orderbook_guard(market, max_spread_bp=THRESH_SPREAD_BP_MAX, req_imb=THRESH_IMB_MIN)
    spread = feats.get("spread_bp", 999.0); imb = feats.get("imb", 0.0)

    mom_pts = max(0.0, min(70.0, 2.0*r15 + 2.0*r30 + 0.5*r60))
    acc_pts = max(0.0, min(24.0, 8.0*max(0.0, accel)))
    ob_pts  = 0.0
    if ok:
        if spread <= 120.0: ob_pts += max(0.0, min(10.0, (120.0-spread)*0.12))
        if imb >= 1.0:      ob_pts += max(0.0, min(10.0, (imb-1.0)*12.0))
        if spread <= 50.0:  ob_pts += 3.0
        if imb >= 1.20:     ob_pts += 4.0

    score = max(0.0, min(100.0, mom_pts + acc_pts + ob_pts))
    sniper = (r15>=0.25 and accel>=0.20 and spread<=80.0 and imb>=1.0)
    return score, r15, r30, r60, accel, spread, imb, sniper

# ========= Trading =========
def totals_from_fills_eur(fills):
    tb=0.0; tq=0.0; fee=0.0
    for f in (fills or []):
        amt=float(f["amount"]); price=float(f["price"]); fe=float(f.get("fee",0) or 0)
        tb+=amt; tq+=amt*price; fee+=fe
    return tb,tq,fee

def place_order(side, market, amount=None, amount_quote=None):
    body = {
        "market": market, "side": side, "orderType": "market",
        "clientOrderId": str(uuid4()), "operatorId": ""
    }
    if side=="buy":
        body["amountQuote"]=f"{amount_quote:.2f}"
    else:
        body["amount"]=f"{amount:.10f}"
    return bv_request("POST","/order", body)

def buy(base_symbol: str):
    base = base_symbol.upper().strip()
    if _today_pnl() <= DAILY_STOP_EUR:
        send_message("⛔ توقف شراء لباقي اليوم (حد الخسارة)."); return
    if r.exists(f"ban24:{base}") or r.exists(f"cooldown:{base}"):
        send_message(f"⏳ {base} محظورة/تهدئة مؤقتة."); return

    market=f"{base}-EUR"
    # دفتر أوامر تكيفي حسب السعر
    price_now=fetch_price_ws_first(market) or 0.0
    if price_now<0.02:
        min_bid=max(30.0, price_now*3000); max_spread=350.0; req_imb=0.25
    elif price_now<0.2:
        min_bid=max(60.0, price_now*1200); max_spread=120.0; req_imb=0.45
    else:
        min_bid=max(100.0, price_now*80);  max_spread=140.0; req_imb=0.35

    ok, why, feats = orderbook_guard(market, min_bid_eur=min_bid, req_imb=req_imb, max_spread_bp=max_spread)
    if not ok:
        send_message(f"⛔ رفض شراء {base} ({why}) | spread={feats.get('spread_bp',0):.0f}bp, imb={feats.get('imb',0):.2f}, bid€={feats.get('bid_eur',0):.0f}")
        r.setex(f"cooldown:{base}", 180, 1)
        return

    with lk:
        if any(t["symbol"]==market for t in active_trades):
            send_message(f"⛔ صفقة مفتوحة أصلاً على {base}."); return
        if len(active_trades)>=MAX_TRADES:
            send_message("🚫 ممتلئ: صفقتان بالفعل."); return

    eur = get_eur_available()
    if eur<5.0:
        send_message("💤 لا يوجد رصيد EUR كافٍ."); return

    # شراء موحد 50% ثم 50%
    amt_quote = round((eur/2.0) if len(active_trades)==0 else eur, 2)
    if amt_quote<5.0:
        send_message(f"⚠️ مبلغ صغير (€{amt_quote:.2f}). تجاهل."); return

    res = place_order("buy", market, amount_quote=amt_quote)
    if not (isinstance(res,dict) and res.get("status")=="filled"):
        r.setex(f"blacklist:buy:{base}", BLACKLIST_EXPIRE_SECONDS, 1)
        send_message(f"❌ فشل شراء {base}."); return

    fills=res.get("fills",[])
    tb,tq,fee = totals_from_fills_eur(fills)
    if tb<=0 or (tq+fee)<=0:
        send_message(f"❌ فشل شراء {base} (fills)."); return

    avg_incl = (tq+fee)/tb
    trade={
        "symbol":market,"entry":avg_incl,"amount":tb,"cost_eur":tq+fee,"buy_fee_eur":fee,
        "opened_at":time.time(),"peak_pct":0.0,"sl_dyn":DYN_SL_START,
        "last_exit_try":0.0,"exit_in_progress":False
    }
    with lk:
        active_trades.append(trade)
        executed_trades.append(trade.copy())
        r.set("nems:active_trades", json.dumps(active_trades))
        r.rpush("nems:executed_trades", json.dumps(trade))

    with _ws_lock:
        WATCHLIST_MARKETS.add(market)
    send_message(f"✅ شراء {base} | قيمة €{amt_quote:.2f} | SL ابتدائي {DYN_SL_START:.1f}% | spread≤{max_spread:.0f}bp/imb≥{req_imb:.2f}")

def sell_trade(trade: dict):
    market=trade["symbol"]; base = market.replace("-EUR","")
    amt=float(trade.get("amount",0) or 0); 
    if amt<=0: return
    if r.exists(f"blacklist:sell:{base}"): return

    ok=False; resp=None
    for _ in range(6):
        resp = place_order("sell", market, amount=amt)
        if isinstance(resp,dict) and resp.get("status")=="filled":
            ok=True; break
        time.sleep(3)
    if not ok:
        r.setex(f"blacklist:sell:{base}", BLACKLIST_EXPIRE_SECONDS, 1)
        send_message(f"❌ فشل بيع {base} بعد محاولات."); return

    fills=resp.get("fills",[])
    tb,tq,fee = totals_from_fills_eur(fills)
    proceeds = tq - fee
    orig_cost = float(trade.get("cost_eur", trade["entry"]*amt))
    pnl_eur = proceeds - orig_cost
    pnl_pct = (proceeds/orig_cost - 1.0)*100.0 if orig_cost>0 else 0.0
    _accum_realized(pnl_eur)

    try:
        if pnl_pct <= BAN_LOSS_PCT:
            r.setex(f"ban24:{base}", 24*3600, 1); send_message(f"🧊 حظر {base} 24h (خسارة {pnl_pct:.2f}%).")
        k=f"lossstreak:{base}"
        if pnl_eur<0:
            streak=int(r.incr(k)); r.expire(k,24*3600)
            if streak>=CONSEC_LOSS_BAN:
                r.setex(f"ban24:{base}", 24*3600, 1); send_message(f"🧊 حظر {base} 24h (سلسلة خسائر).")
        else:
            r.delete(k)
    except Exception: pass

    with lk:
        try: active_trades.remove(trade)
        except ValueError: pass
        r.set("nems:active_trades", json.dumps(active_trades))
        # أغلق النسخة الأقدم المطابقة
        for i in range(len(executed_trades)-1,-1,-1):
            t=executed_trades[i]
            if t["symbol"]==market and "exit_eur" not in t:
                t.update({
                    "exit_eur": proceeds, "sell_fee_eur": fee,
                    "pnl_eur": pnl_eur, "pnl_pct": pnl_pct, "exit_time": time.time()
                })
                break
        r.delete("nems:executed_trades")
        for t in executed_trades: r.rpush("nems:executed_trades", json.dumps(t))

    cd = 60 if pnl_pct>-0.5 else max(180, BUY_COOLDOWN_SEC)
    r.setex(f"cooldown:{base}", cd, 1)
    send_message(f"💰 بيع {base} | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%)")

# ========= Monitor / Exits =========
def _price_n_seconds_ago_trade(tr, now_ts, sec):
    hist=tr.get("hist",deque())
    cutoff=now_ts-sec
    for ts,p in reversed(hist):
        if ts<=cutoff: return p
    return None

def _fast_drop(tr, now_ts, price_now, window=15, drop=1.2):
    p20=_price_n_seconds_ago_trade(tr, now_ts, window)
    if p20 and p20>0:
        dpct=(price_now/p20-1.0)*100.0
        return dpct<=-drop, dpct
    return False,0.0

def _update_trade_hist(tr, now_ts, price):
    if "hist" not in tr: tr["hist"]=deque(maxlen=600)
    tr["hist"].append((now_ts,price))
    cutoff = now_ts - 120
    while tr["hist"] and tr["hist"][0][0]<cutoff:
        tr["hist"].popleft()

def monitor_loop():
    while True:
        try:
            with lk: snapshot=list(active_trades)
            now=time.time()
            for tr in snapshot:
                market=tr["symbol"]; entry=float(tr["entry"])
                cur=fetch_price_ws_first(market)
                if not cur: continue
                _update_trade_hist(tr, now, cur)

                # زخم/قمة/SL
                pnl_pct = ((cur-entry)/entry)*100.0
                tr["peak_pct"]=max(tr.get("peak_pct",0.0), pnl_pct)

                # تحديث SL سلمي
                inc=int(max(0.0, pnl_pct)//1); dyn_base=DYN_SL_START + inc*DYN_SL_STEP
                tr["sl_dyn"]=max(tr.get("sl_dyn", DYN_SL_START), dyn_base)

                # حساب r30/r90 بسيط من الهيستوري الخاص بالصفقة
                # (نستخدم نفس الديك؛ تقدير تقريبي يكفي لقرارات الخروج)
                hist=tr.get("hist",deque())
                if hist:
                    now_ts=hist[-1][0]
                    p30=p90=None; hi=cur
                    for ts,p in hist:
                        hi=max(hi,p)
                        age=now_ts-ts
                        if p30 is None and age>=30: p30=p
                        if p90 is None and age>=90: p90=p
                    base=hist[0][1]
                    if p30 is None: p30=base
                    if p90 is None: p90=base
                    r30=(cur/p30-1.0)*100.0 if p30>0 else 0.0
                    r90=(cur/p90-1.0)*100.0 if p90>0 else 0.0
                else:
                    r30=r90=0.0

                # هدف طبقي سريع حسب ظروف OB/زخم
                ob_ok,_,obf = orderbook_guard(market)
                spread = obf.get("spread_bp", 999.0) if obf else 999.0
                imb    = obf.get("imb", 0.0) if obf else 0.0
                weak_env = (not ob_ok) or (spread>120.0) or (imb<0.95) or (r30<0 and r90<0)
                tp = TP_WEAK if weak_env else TP_GOOD
                if pnl_pct >= tp and tr.get("peak_pct",0.0) < (tp+0.6):
                    tr["exit_in_progress"]=True; tr["last_exit_try"]=now
                    send_message(f"🔔 خروج {market} (هدف {tp:.2f}%)")
                    sell_trade(tr); tr["exit_in_progress"]=False; continue

                # Giveback من القمة (أخف)
                peak = tr.get("peak_pct",0.0)
                if peak>=PEAK_TRIGGER:
                    giveback = min(GIVEBACK_CAP, GIVEBACK_RATIO*peak)
                    drop = peak - pnl_pct
                    # تعزيز SL إلى (peak - giveback) إن كان أعلى من الحالي
                    desired_lock = peak - giveback
                    if desired_lock > tr.get("sl_dyn", DYN_SL_START):
                        prev = tr["sl_dyn"]; tr["sl_dyn"]=desired_lock
                        if tr["sl_dyn"]-prev>=0.4 and not tr.get("boost_lock_notified"):
                            send_message(f"🔒 تعزيز SL {market} → {tr['sl_dyn']:.2f}% (قمة {peak:.2f}%)")
                            tr["boost_lock_notified"]=True
                    if drop >= giveback and (r30<=-0.40 or r90<=0.0):
                        tr["exit_in_progress"]=True; tr["last_exit_try"]=now
                        send_message(f"🔔 خروج {market} (Giveback {drop:.2f}%≥{giveback:.2f}%)")
                        sell_trade(tr); tr["exit_in_progress"]=False; continue

                # SL ضرب
                if pnl_pct <= tr.get("sl_dyn", DYN_SL_START):
                    tr["exit_in_progress"]=True; tr["last_exit_try"]=now
                    send_message(f"🔔 خروج {market} (SL {tr['sl_dyn']:.2f}%، الآن {pnl_pct:.2f}%)")
                    sell_trade(tr); tr["exit_in_progress"]=False; continue

                # Time-stop مبكر
                age_total = now - tr.get("opened_at", now)
                if age_total>=TIME_STOP_MIN and TIME_STOP_PNL_LO<=pnl_pct<=TIME_STOP_PNL_HI and r90<=0.0:
                    tr["exit_in_progress"]=True; tr["last_exit_try"]=now
                    send_message(f"⏱️ خروج {market} (Time-stop {int(age_total//60)}د)")
                    sell_trade(tr); tr["exit_in_progress"]=False; continue

                # كراش سريع حماية
                crash, d20 = _fast_drop(tr, now, cur, window=15, drop=1.2)
                if crash and pnl_pct < -1.5:
                    tr["exit_in_progress"]=True; tr["last_exit_try"]=now
                    send_message(f"🔔 خروج {market} (هبوط سريع d15s={d20:.2f}%)")
                    sell_trade(tr); tr["exit_in_progress"]=False; continue

            time.sleep(0.25)
        except Exception as e:
            print("monitor err:", e)
            time.sleep(1)

Thread(target=monitor_loop, daemon=True).start()

# ========= Engine (signals + replacement) =========
def weakest_open_trade():
    """نختار الأضعف تقريبياً: أدنى (pnl الآن) مع اعتبار العُمر."""
    with lk: arr=list(active_trades)
    if not arr: return None
    worst=None; worst_score=None
    now=time.time()
    for t in arr:
        cur=fetch_price_ws_first(t["symbol"]) or t["entry"]
        pnl = (cur/t["entry"]-1.0)*100.0
        age_min = (now - t.get("opened_at",now))/60.0
        sc = pnl - 0.2*min(age_min, 15.0)  # كلما طال بدون تقدّم، أسوأ
        if worst_score is None or sc < worst_score:
            worst, worst_score = t, sc
    return worst

def engine_loop():
    global WATCHLIST_MARKETS
    while True:
        try:
            if not (enabled and auto_enabled):
                time.sleep(1); continue
            if _today_pnl() <= DAILY_STOP_EUR:
                time.sleep(3); continue

            with lk:
                if len(active_trades)>=MAX_TRADES:
                    time.sleep(ENGINE_INTERVAL_SEC); continue

            refresh_watchlist()
            watch=list(WATCHLIST_MARKETS)
            if not watch:
                time.sleep(1); continue

            now=time.time()
            best=None
            for mkt in watch:
                p=fetch_price_ws_first(mkt)
                if not p: continue
                _update_hist_key(mkt, now, p)
                sc, r15, r30, r60, acc, spr, imb, snp = score_exploder(mkt, p)
                if spr>THRESH_SPREAD_BP_MAX or imb<THRESH_IMB_MIN: continue
                base=mkt.replace("-EUR","")
                if r.exists(f"ban24:{base}") or r.exists(f"cooldown:{base}"): continue
                cand=(sc, mkt, r15, r30, r60, acc, spr, imb, snp)
                if (best is None) or (cand[0]>best[0]): best=cand

            if best:
                score, mkt, r15, r30, r60, acc, spr, imb, sniper = best
                trigger = (score>=AUTO_THRESHOLD) or sniper
                if trigger:
                    # Replacement إذا عندنا صفقتين ممتلئة
                    with lk:
                        full = len(active_trades)>=MAX_TRADES
                    if full:
                        w = weakest_open_trade()
                        if w and score >= (AUTO_THRESHOLD+10.0) and (time.time()-w.get("opened_at",0))>30:
                            send_message("🔄 Replacement: إغلاق الأضعف والدخول بالأقوى")
                            sell_trade(w)
                            buy(mkt.replace("-EUR",""))
                    else:
                        buy(mkt.replace("-EUR",""))

            time.sleep(ENGINE_INTERVAL_SEC)
        except Exception as e:
            print("engine err:", e)
            time.sleep(1)

Thread(target=engine_loop, daemon=True).start()

# ========= Summary =========
def build_summary():
    lines=[]; now=time.time()
    with lk:
        act=list(active_trades); ex=list(executed_trades)
    if act:
        def cur_pnl(t):
            cur=fetch_price_ws_first(t["symbol"]) or t["entry"]
            return (cur/t["entry"]-1.0)
        arr=sorted(act, key=cur_pnl, reverse=True)
        tot_val=0.0; tot_cost=0.0
        lines.append(f"📌 الصفقات النشطة ({len(arr)}):")
        for i,t in enumerate(arr,1):
            sym=t["symbol"].replace("-EUR","")
            entry=float(t["entry"]); amt=float(t["amount"])
            cur=fetch_price_ws_first(t["symbol"]) or entry
            pnl=((cur-entry)/entry)*100.0
            val=amt*cur; tot_val+=val; tot_cost+=float(t.get("cost_eur", entry*amt))
            peak=float(t.get("peak_pct",0.0))
            dyn=float(t.get("sl_dyn", DYN_SL_START))
            lines.append(f"{i}. {sym}: {pnl:+.2f}% | Peak {peak:.2f}% | SL {dyn:.2f}%")
        fl = tot_val - tot_cost
        pct = ((tot_val/tot_cost)-1.0)*100.0 if tot_cost>0 else 0.0
        lines.append(f"💼 قيمة الصفقات: €{tot_val:.2f} | عائم: {fl:+.2f}€ ({pct:+.2f}%)")
    else:
        lines.append("📌 لا صفقات نشطة.")

    since_ts=float(r.get(SINCE_RESET_KEY) or 0.0)
    closed=[t for t in ex if "pnl_eur" in t and "exit_time" in t and float(t["exit_time"])>=since_ts]
    closed.sort(key=lambda x: float(x["exit_time"]))
    wins=sum(1 for t in closed if float(t["pnl_eur"])>=0); losses=len(closed)-wins
    pnl_eur=sum(float(t["pnl_eur"]) for t in closed)
    avg_eur=(pnl_eur/len(closed)) if closed else 0.0
    avg_pct=(sum(float(t.get("pnl_pct",0)) for t in closed)/len(closed)) if closed else 0.0
    lines.append("\n📊 صفقات مكتملة منذ آخر Reset:")
    if not closed:
        lines.append("• لا يوجد.")
    else:
        lines.append(f"• العدد: {len(closed)} | محققة: {pnl_eur:+.2f}€ | متوسط/صفقة: {avg_eur:+.2f}€ ({avg_pct:+.2f}%)")
        lines.append(f"• فوز/خسارة: {wins}/{losses}")
        lines.append("🧾 أحدث الصفقات:")
        for t in sorted(closed, key=lambda x: float(x["exit_time"]), reverse=True)[:10]:
            sym=t["symbol"].replace("-EUR","")
            lines.append(f"- {sym}: {float(t['pnl_eur']):+,.2f}€ ({float(t.get('pnl_pct',0)):+.2f}%)")
    lines.append(f"\n⛔ حد اليوم: {_today_pnl():+.2f}€ / {DAILY_STOP_EUR:+.2f}€")
    return "\n".join(lines)

def send_chunks(txt, chunk=3500):
    if not txt: return
    buf=""; 
    for line in txt.splitlines(True):
        if len(buf)+len(line)>chunk:
            send_message(buf); buf=""
        buf+=line
    if buf: send_message(buf)

# ========= Telegram Webhook =========
@app.route("/", methods=["POST"])
def webhook():
    global enabled, auto_enabled
    data = request.get_json(silent=True) or {}
    text = (data.get("message",{}).get("text") or data.get("text") or "").strip()
    if not text: return "ok"
    low=text.lower()

    def has(*ks): return any(k in low for k in ks)
    def starts(*ks): return any(low.startswith(k) for k in ks)

    if has("start","تشغيل","ابدأ"):
        enabled=True; auto_enabled=True
        send_message("✅ تم التفعيل."); return "ok"
    if has("stop","قف","ايقاف","إيقاف"):
        enabled=False; auto_enabled=False
        send_message("🛑 تم الإيقاف."); return "ok"
    if has("summary","ملخص","الملخص"):
        send_chunks(build_summary()); return "ok"
    if has("settings","اعدادات","إعدادات"):
        send_message(
            f"⚙️ ENGINE: thr={AUTO_THRESHOLD}, topN={TOPN_WATCH}, interval={ENGINE_INTERVAL_SEC}s | "
            f"spread≤{THRESH_SPREAD_BP_MAX}bp, imb≥{THRESH_IMB_MIN}\n"
            f"TP={TP_WEAK}/{TP_GOOD}% | SL start={DYN_SL_START}% step={DYN_SL_STEP}% | daily={DAILY_STOP_EUR}€"
        ); return "ok"
    if has("reset","انسى","أنسى"):
        with lk:
            active_trades.clear(); executed_trades.clear()
            r.delete("nems:active_trades"); r.delete("nems:executed_trades")
            r.set(SINCE_RESET_KEY, time.time())
        send_message("🧠 تم Reset الإحصائيات والحالة."); return "ok"
    if has("ban list","قائمة الحظر","banlist","ban list"):
        keys=[k.decode() if isinstance(k,bytes) else k for k in r.keys("ban24:*")]
        if not keys: send_message("🧊 لا توجد عملات محظورة."); return "ok"
        names=sorted(k.split("ban24:")[-1] for k in keys)
        send_message("🧊 محظورة 24h:\n- " + "\n- ".join(names)); return "ok"
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
        total=eur
        with lk: ex=list(executed_trades)
        winners,losers=[],[]
        for b in bals:
            sym=b.get("symbol"); 
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

# ========= Load State =========
try:
    at=r.get("nems:active_trades"); 
    if at: active_trades=json.loads(at)
    et=r.lrange("nems:executed_trades",0,-1)
    executed_trades=[json.loads(t) for t in et]
    if not r.exists(SINCE_RESET_KEY): r.set(SINCE_RESET_KEY, 0)
except Exception as e:
    print("state load err:", e)

# ========= Local Run =========
if __name__=="__main__" and RUN_LOCAL:
    app.run(host="0.0.0.0", port=5000)