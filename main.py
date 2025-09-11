# -*- coding: utf-8 -*-
"""
Saqer — Maker-Only Relay (Bitvavo / EUR)
- شراء/بيع Maker فقط (postOnly) مع تجميع partial fills.
- تصحيح amount/price حسب precision.
- كنس أي أوامر قديمة تمنع الرصيد.
- منع رسائل سبام.
"""

import os, re, time, json, math, traceback
import requests, redis, websocket
from threading import Thread, Lock
from uuid import uuid4
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ========= Boot =========
load_dotenv()
app = Flask(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHAT_ID     = os.getenv("CHAT_ID")
API_KEY     = os.getenv("BITVAVO_API_KEY")
API_SECRET  = os.getenv("BITVAVO_API_SECRET")
REDIS_URL   = os.getenv("REDIS_URL")
RUN_LOCAL   = os.getenv("RUN_LOCAL", "0") == "1"
PORT        = int(os.getenv("PORT", "5000"))
buy_in_progress = False
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
    except: pass

_last_notif = {}
def send_message_throttled(key: str, text: str, min_interval=5.0):
    now = time.time()
    if now - _last_notif.get(key, 0) >= min_interval:
        _last_notif[key] = now
        send_message(text)

import hmac, hashlib
def create_sig(ts, method, path, body_str=""):
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
    resp = requests.request(method, url, headers=headers,
                            json=(body or {}) if method!="GET" else None,
                            timeout=timeout)
    return resp.json()

def get_eur_available():
    try:
        for b in bv_request("GET","/balance"):
            if b.get("symbol")=="EUR":
                return float(b.get("available",0) or 0)
    except: pass
    return 0.0

def get_asset_available(sym: str):
    try:
        for b in bv_request("GET","/balance"):
            if b.get("symbol")==sym.upper():
                return float(b.get("available",0) or 0)
    except: pass
    return 0.0

# ========= Market helpers =========
def _tick(market):
    v=(MARKET_META.get(market,{}) or {}).get("tick")
    return v if (v and v>0) else 1e-6

def _step(market):
    v=(MARKET_META.get(market,{}) or {}).get("step")
    return v if (v and v>0) else 1e-8

def _decimals_from_step(step: float) -> int:
    s = ("%.16f" % float(step)).rstrip("0").rstrip(".")
    return len(s.split(".")[1]) if "." in s else 0

def _round_amount(market, amount):
    step=_step(market); decs=_decimals_from_step(step)
    a = math.floor(float(amount)/step)*step
    return round(max(step,a),decs)

def _format_amount(market, amount) -> str:
    decs=_decimals_from_step(_step(market))
    return f"{_round_amount(market, amount):.{decs}f}"

def _round_price_down(market, price):
    tk=_tick(market); decs=_decimals_from_step(tk)
    p=math.floor(float(price)/tk)*tk
    return round(max(tk,p),decs)

def _format_price(market, price) -> str:
    tk=_tick(market); decs=_decimals_from_step(tk)
    return f"{_round_price_down(market, price):.{decs}f}"

# ========= Orders =========
def _place_limit_postonly(market, side, price, amount):
    body = {
        "market": market, "side": side, "orderType": "limit", "postOnly": True,
        "clientOrderId": str(uuid4()), "price": _format_price(market, price),
        "amount": _format_amount(market, amount)
    }
    return bv_request("POST","/order",body)

def _fetch_order(mkt, oid):   return bv_request("GET",f"/order?market={mkt}&orderId={oid}")
def _cancel_order(mkt, oid):  return bv_request("DELETE",f"/order?market={mkt}&orderId={oid}")

def _list_open_orders(market):
    try: return bv_request("GET", f"/orders?market={market}") or []
    except: return []

def _cancel_all_open_orders(market, wait_each=True):
    try:
        opens=_list_open_orders(market)
        for o in (opens or []):
            oid=o.get("orderId")
            if not oid: continue
            try: _cancel_order(market, oid)
            except: pass
            if wait_each:
                t0=time.time()
                while time.time()-t0<6:
                    st=_fetch_order(market, oid) or {}
                    if (st.get("status") or "").lower() in ("canceled","filled"):
                        break
                    time.sleep(0.2)
    except: pass
def _run_buy_async(market: str, eur: float):
    global buy_in_progress
    try:
        res = open_maker_buy(market, eur)
        if res:
            # … لو بدك أرسل رسالة تلغرام هنا بتفاصيل النجاح
            send_message(f"✅ تمت عملية شراء {market}.")
        else:
            send_message("⚠️ لم يكتمل شراء Maker ضمن المهلة.")
    except Exception as e:
        traceback.print_exc()
        send_message(f"🐞 خطأ أثناء الشراء: {e}")
    finally:
        buy_in_progress = False
# ========= Buy flow =========
def open_maker_buy(market: str, eur_amount: float):
    eur_avail=get_eur_available()
    spend=min(eur_amount, eur_avail-0.05)
    if spend<=0:
        send_message("⛔ لا يوجد رصيد كافٍ"); return None

    # تنظيف قبل البدء
    _cancel_all_open_orders(market)

    patience=45; deadline=time.time()+patience
    last_order=None; all_fills=[]

    try:
        while time.time()<deadline:
            # افتح أمر جديد
            price=fetch_price_ws_first(market)
            if not price: time.sleep(0.3); continue
            amt=_round_amount(market, spend/price)
            res=_place_limit_postonly(market,"buy",price,amt)
            if res.get("orderId"):
                last_order=res["orderId"]

            # تحقق سريع
            st=_fetch_order(market,last_order) or {}
            if (st.get("status") or "").lower() in ("filled","partiallyfilled"):
                all_fills+=st.get("fills",[])
                break

            # insufficient balance → كنس
            if "error" in res and "insufficient" in str(res["error"]).lower():
                _cancel_all_open_orders(market)
                eur_avail=get_eur_available()
                spend=min(spend, eur_avail)
                time.sleep(0.5)
                continue

            time.sleep(1)

        if last_order: _cancel_all_open_orders(market)

    except Exception as e:
        print("open_maker_buy err:", e)

    if all_fills:
        return {"fills": all_fills}
    return None

# ========= WS Price =========
def _ws_on_message(ws,msg):
    try:
        d=json.loads(msg)
        if d.get("event")=="ticker":
            m=d.get("market"); p=float(d.get("price",0))
            if p>0:
                with _ws_lock: _ws_prices[m]={"price":p,"ts":time.time()}
    except: pass

def _ws_thread():
    while True:
        try:
            ws=websocket.WebSocketApp(WS_URL,on_message=_ws_on_message)
            ws.run_forever()
        except: time.sleep(2)
Thread(target=_ws_thread,daemon=True).start()

def fetch_price_ws_first(market):
    now=time.time()
    with _ws_lock: rec=_ws_prices.get(market)
    if rec and (now-rec["ts"])<2: return rec["price"]
    try:
        j=requests.get(f"{BASE_URL}/ticker/price?market={market}",timeout=6).json()
        return float(j.get("price",0))
    except: return None

# ========= Telegram & Flask =========
@app.route("/hook", methods=["POST"])
def hook():
    global buy_in_progress
    try:
        data = request.get_json(silent=True) or {}
        cmd  = (data.get("cmd") or "").strip().lower()
        if cmd != "buy":
            return jsonify({"ok": False, "err": "only_buy"}), 400

        coin = (data.get("coin") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{2,15}", coin or ""):
            return jsonify({"ok": False, "err": "bad_coin"}), 400
        market = f"{coin}-EUR"

        eur = float(data.get("eur")) if data.get("eur") is not None else None

        if buy_in_progress:
            return jsonify({"ok": False, "err": "busy"}), 429

        buy_in_progress = True
        Thread(target=_run_buy_async, args=(market, eur), daemon=True).start()
        return jsonify({"ok": True, "msg": "buy_started", "market": market})
    except Exception as e:
        traceback.print_exc()
        buy_in_progress = False
        return jsonify({"ok": False, "err": str(e)}), 500

@app.route("/",methods=["GET"])
def home(): return "Saqer Maker Relay ✅"

if __name__=="__main__" or RUN_LOCAL:
    app.run(host="0.0.0.0",port=PORT)