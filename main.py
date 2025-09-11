# -*- coding: utf-8 -*-
"""
Saqer — Maker-Only Relay (Bitvavo / EUR)
- شراء Maker فقط (postOnly) مع تجميع fills.
- كنس الأوامر المفتوحة قبل/بعد الشراء لمنع حجز الرصيد.
- تنفيذ الشراء بثريد غير حاجب لمسار /hook (لا timeouts).
- تصحيح precision للسعر/الكمية حسب market meta، مع fallback تلقائي.
"""

import os, re, time, json, math, traceback, hmac, hashlib
import requests, redis, websocket
from threading import Thread, Lock
from uuid import uuid4
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ========== Boot / ENV ==========
load_dotenv()
app = Flask(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHAT_ID     = os.getenv("CHAT_ID")
API_KEY     = os.getenv("BITVAVO_API_KEY")
API_SECRET  = os.getenv("BITVAVO_API_SECRET")
REDIS_URL   = os.getenv("REDIS_URL")
RUN_LOCAL   = os.getenv("RUN_LOCAL", "0") == "1"
PORT        = int(os.getenv("PORT", "8080"))

BASE_URL = "https://api.bitvavo.com/v2"
WS_URL   = "wss://ws.bitvavo.com/v2/"

# ========== State ==========
enabled          = True
buy_in_progress  = False
lk               = Lock()
r                = redis.from_url(REDIS_URL) if REDIS_URL else redis.Redis()
MARKET_META      = {}   # { "COIN-EUR": {"tick":..., "step":..., "minQuote":..., "minBase":...} }

# ========== Telegram ==========
_last_notif = {}
def send_message(text: str):
    try:
        if BOT_TOKEN and CHAT_ID:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          json={"chat_id": CHAT_ID, "text": text}, timeout=8)
        else:
            print("TG:", text)
    except Exception:
        pass

def send_message_throttled(key: str, text: str, sec=5.0):
    now = time.time()
    if now - _last_notif.get(key, 0) >= sec:
        _last_notif[key] = now
        send_message(text)

# ========== Bitvavo REST ==========
def _sig(ts, method, path, body_str=""):
    msg = f"{ts}{method}{path}{body_str}"
    return hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def bv_request(method, path, body=None, timeout=12):
    url = f"{BASE_URL}{path}"
    ts  = str(int(time.time()*1000))
    body_str = "" if method=="GET" else json.dumps(body or {}, separators=(',',':'))
    headers = {
        'Bitvavo-Access-Key': API_KEY,
        'Bitvavo-Access-Timestamp': ts,
        'Bitvavo-Access-Signature': _sig(ts, method, f"/v2{path}", body_str),
        'Bitvavo-Access-Window': '10000'
    }
    resp = requests.request(method, url, headers=headers,
                            json=(body or {}) if method!="GET" else None,
                            timeout=timeout)
    return resp.json()

def get_eur_available() -> float:
    try:
        for b in bv_request("GET", "/balance"):
            if b.get("symbol")=="EUR":
                return float(b.get("available",0) or 0)
    except Exception:
        pass
    return 0.0

def get_asset_available(sym: str) -> float:
    try:
        for b in bv_request("GET", "/balance"):
            if b.get("symbol")==sym.upper():
                return float(b.get("available",0) or 0)
    except Exception:
        pass
    return 0.0

# ========== Market meta ==========
def _parse_step_from_precision(val, default_step):
    try:
        # bitvavo يعطي أحياناً precision كرقم خانات
        if isinstance(val, int) or (isinstance(val, str) and str(val).isdigit()):
            d = int(val)
            if 0 <= d <= 20: return 10.0 ** (-d)
        v = float(val)
        if 0 < v < 1: return v
    except Exception:
        pass
    return default_step

def load_markets():
    """حمّل precision والحدود؛ مع fallback آمن."""
    try:
        rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
        meta = {}
        for r0 in rows:
            if r0.get("quote") != "EUR": continue
            mkt  = r0.get("market")
            tick = _parse_step_from_precision(r0.get("pricePrecision", 6), 1e-6)
            step = _parse_step_from_precision(r0.get("amountPrecision", 8), 1e-8)
            meta[mkt] = {
                "tick": tick or 1e-6,
                "step": step or 1e-8,
                "minQuote": float(r0.get("minOrderInQuoteAsset", 0) or 0.0),
                "minBase":  float(r0.get("minOrderInBaseAsset",  0) or 0.0)
            }
        if meta: MARKET_META.update(meta)
    except Exception as e:
        print("load_markets err:", e)

def _tick(market):
    v = (MARKET_META.get(market, {}) or {}).get("tick")
    return v if (v and v>0) else 1e-6

def _step(market):
    v = (MARKET_META.get(market, {}) or {}).get("step")
    return v if (v and v>0) else 1e-8

def _decimals_from_step(step: float) -> int:
    s = ("%.16f" % float(step)).rstrip("0").rstrip(".")
    return len(s.split(".")[1]) if "." in s else 0

def _round_amount(market, amount):
    step=_step(market); decs=_decimals_from_step(step)
    a = math.floor(max(0.0, float(amount))/step)*step
    return round(max(step,a),decs)

def _format_amount(market, amount) -> str:
    decs=_decimals_from_step(_step(market))
    return f"{_round_amount(market, amount):.{decs}f}"

def _round_price_down(market, price):
    tk=_tick(market); decs=_decimals_from_step(tk)
    p=math.floor(max(0.0, float(price))/tk)*tk
    return round(max(tk,p),decs)

def _format_price(market, price) -> str:
    tk=_tick(market); decs=_decimals_from_step(tk)
    return f"{_round_price_down(market, price):.{decs}f}"

# ========== Orders ==========
def _place_limit_postonly(market, side, price, amount):
    body = {
        "market": market, "side": side, "orderType": "limit", "postOnly": True,
        "clientOrderId": str(uuid4()), "price": _format_price(market, price),
        "amount": _format_amount(market, amount)
    }
    return bv_request("POST", "/order", body)

def _fetch_order(mkt, oid):  return bv_request("GET",    f"/order?market={mkt}&orderId={oid}")
def _cancel_order(mkt, oid): return bv_request("DELETE", f"/order?market={mkt}&orderId={oid}")

def _list_open_orders(market):
    try: return bv_request("GET", f"/orders?market={market}") or []
    except Exception: return []

def _cancel_all_open_orders(market, wait_each=True):
    """إلغاء جماعي + تأكيد الحالة (canceled/filled)."""
    try:
        opens=_list_open_orders(market)
        for o in opens:
            oid=o.get("orderId"); 
            if not oid: continue
            try: _cancel_order(market, oid)
            except Exception: pass
            if wait_each:
                t0=time.time()
                while time.time()-t0<6:
                    st=_fetch_order(market, oid) or {}
                    if (st.get("status") or "").lower() in ("canceled","filled"): break
                    time.sleep(0.2)
    except Exception as e:
        print("cancel sweep err:", e)

# ========== WS price ==========
_ws_prices = {}; _ws_lock = Lock()

def _ws_on_message(ws,msg):
    try:
        d=json.loads(msg)
        if d.get("event")=="ticker":
            m=d.get("market")
            p=float(d.get("price", d.get("lastPrice", 0)) or 0)
            if p>0:
                with _ws_lock: _ws_prices[m]={"price":p,"ts":time.time()}
    except Exception:
        pass

def _ws_thread():
    while True:
        try:
            ws=websocket.WebSocketApp(WS_URL,on_message=_ws_on_message)
            ws.run_forever(ping_interval=25, ping_timeout=10)
        except Exception:
            time.sleep(2)

Thread(target=_ws_thread, daemon=True).start()

def fetch_price_ws_first(market, staleness=2.0):
    now=time.time()
    with _ws_lock: rec=_ws_prices.get(market)
    if rec and (now-rec["ts"])<=staleness: return rec["price"]
    try:
        j=requests.get(f"{BASE_URL}/ticker/price?market={market}",timeout=6).json()
        p=float(j.get("price",0) or 0)
        if p>0:
            with _ws_lock: _ws_prices[market]={"price":p,"ts":now}
            return p
    except Exception:
        pass
    return None

# ========== Buy flow ==========
def open_maker_buy(market: str, eur_amount: float | None):
    """شراء Maker مع كنس أوامر قديمة وحمايات None/الرصيد."""
    eur_avail = max(0.0, get_eur_available())

    # eur_amount يمكن أن يكون None → استخدم كل المتاح
    target = float(eur_amount) if (eur_amount is not None and eur_amount > 0) else eur_avail

    # لا نتجاوز المتاح، ونخصم هامش بسيط للرسوم/الانزلاق
    buffer = 0.05
    target = min(target, eur_avail)
    spend  = max(0.0, target - buffer)
    if spend <= 0:
        send_message("⛔ لا يوجد رصيد كافٍ بعد الهامش."); return None

    # كنس قبل البدء
    _cancel_all_open_orders(market, wait_each=True)

    patience = 45
    deadline = time.time() + patience
    last_order = None
    all_fills  = []

    try:
        while time.time() < deadline:
            price = fetch_price_ws_first(market)
            if not price or price <= 0:
                time.sleep(0.3); continue

            amt = _round_amount(market, spend / price)
            if amt <= 0:
                send_message("⛔ الكمية المحسوبة صفرية بعد التقريب."); break

            res = _place_limit_postonly(market, "buy", price, amt)
            oid = res.get("orderId")
            err = str(res.get("error","")).lower()

            if oid:
                last_order = oid
                st = _fetch_order(market, oid) or {}
                st_status = (st.get("status") or "").lower()
                if st_status in ("filled","partiallyfilled"):
                    all_fills += st.get("fills", []) or []
                    break

            elif "insufficient" in err:
                # أمر قديم يحجز الرصيد؟ كنس ثم حدّث spend
                _cancel_all_open_orders(market, wait_each=True)
                eur_avail = max(0.0, get_eur_available())
                target = min(target, eur_avail)
                spend  = max(0.0, target - buffer)
                if spend <= 0:
                    send_message("⛔ الرصيد غير كافٍ بعد الكنس."); break

            time.sleep(1.0)

        # تنظيف نهائي
        _cancel_all_open_orders(market, wait_each=True)

    except Exception as e:
        print("open_maker_buy err:", e)

    if all_fills:
        # بإمكانك جمعها وحساب المتوسط إن أردت
        send_message_throttled("buy_ok", f"✅ تم شراء {market} (fills={len(all_fills)})", 2.0)
        return {"fills": all_fills}

    send_message_throttled("buy_fail", "⚠️ لم يكتمل شراء Maker ضمن المهلة.", 2.0)
    return None

# ========== Async runner ==========
def _run_buy_async(market: str, eur: float | None):
    global buy_in_progress
    try:
        open_maker_buy(market, eur)
    except Exception as e:
        traceback.print_exc()
        send_message(f"🐞 خطأ أثناء الشراء: {e}")
    finally:
        buy_in_progress = False

# ========== HTTP ==========
@app.route("/hook", methods=["POST"])
def hook():
    global buy_in_progress, enabled
    try:
        if not enabled:
            return jsonify({"ok": False, "err": "disabled"}), 403

        data = request.get_json(silent=True) or {}
        cmd  = (data.get("cmd") or "").strip().lower()
        if cmd != "buy":
            return jsonify({"ok": False, "err": "only_buy"}), 400

        coin = (data.get("coin") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{2,15}", coin or ""):
            return jsonify({"ok": False, "err": "bad_coin"}), 400
        market = f"{coin}-EUR"

        eur = data.get("eur")
        eur = float(eur) if eur is not None else None

        if buy_in_progress:
            return jsonify({"ok": False, "err": "busy"}), 429

        buy_in_progress = True
        Thread(target=_run_buy_async, args=(market, eur), daemon=True).start()
        return jsonify({"ok": True, "msg": "buy_started", "market": market})
    except Exception as e:
        traceback.print_exc()
        buy_in_progress = False
        return jsonify({"ok": False, "err": str(e)}), 500

@app.route("/", methods=["GET"])
def home():
    return "Saqer Maker Relay ✅"

# ========== Main ==========
if __name__ == "__main__" or RUN_LOCAL:
    load_markets()
    app.run(host="0.0.0.0", port=PORT)