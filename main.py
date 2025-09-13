# -*- coding: utf-8 -*-
"""
Saqar — Signal-Only Runner with Smart Trailing (Bitvavo / EUR)

- يستقبل إشارات من Auto-Scanner عبر /hook
- عملة واحدة نشطة فقط
- دخول Maker-Only بكل EUR المتاح
- TP صغير + SL -2% + Trailing SL عند الارتفاع
- تبديل هادئ بين العملات عند وصول إشارة جديدة
"""

import os, time, json, hmac, hashlib, requests, threading
from uuid import uuid4
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from decimal import Decimal, ROUND_DOWN

# ===== Boot / ENV =====
load_dotenv()
app = Flask(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID     = os.getenv("CHAT_ID", "").strip()
API_KEY     = os.getenv("BITVAVO_API_KEY", "").strip()
API_SECRET  = os.getenv("BITVAVO_API_SECRET", "").strip()
PORT        = int(os.getenv("PORT", "8080"))
BASE_URL    = "https://api.bitvavo.com/v2"

HEADROOM_EUR   = float(os.getenv("HEADROOM_EUR", "0.30"))
MAKER_FEE_RATE = float(os.getenv("MAKER_FEE_RATE", "0.001"))

TP_TARGET_PCT  = float(os.getenv("TP_TARGET_PCT", "0.5"))   # +0.5%
SL_INIT_PCT    = float(os.getenv("SL_INIT_PCT", "2.0"))     # -2.0%
TRAIL_START    = float(os.getenv("TRAIL_START", "0.7"))     # يبدأ التريل عند +0.7%
TRAIL_STEP     = float(os.getenv("TRAIL_STEP", "0.3"))      # كل +0.3% نرفع SL

# ===== Global state =====
ACTIVE = {}   # { market, avg_entry, amount, tp_price, sl_price, high_price }
LOCK   = threading.Lock()

# ===== Telegram =====
def tg_send(text):
    if not BOT_TOKEN:
        print("TG:", text); return
    try:
        data = {"chat_id": CHAT_ID, "text": text}
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=data, timeout=8)
    except Exception as e:
        print("tg_send err:", e)

# ===== Bitvavo helpers =====
def _sign(ts, method, path, body=""):
    msg = f"{ts}{method}{path}{body}"
    return hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def bv_request(method, path, body=None):
    url = f"{BASE_URL}{path}"
    ts  = str(int(time.time() * 1000))
    m   = method.upper()
    if m in ("GET", "DELETE"):
        body_str = ""; payload = None
    else:
        body_str = json.dumps(body or {}, separators=(',',':'))
        payload  = (body or {})
    headers = {
        "Bitvavo-Access-Key": API_KEY,
        "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Signature": _sign(ts, m, f"/v2{path}", body_str),
        "Bitvavo-Access-Window": "10000",
        "Content-Type": "application/json",
    }
    r = requests.request(m, url, headers=headers, json=payload, timeout=10)
    try: return r.json()
    except: return {"error": r.text}

def get_balance(symbol):
    bals = bv_request("GET", "/balance")
    if isinstance(bals, list):
        for b in bals:
            if b.get("symbol") == symbol.upper():
                return float(b.get("available",0) or 0.0)
    return 0.0

def get_best_bid_ask(market):
    ob = requests.get(f"{BASE_URL}/{market}/book?depth=1", timeout=8).json()
    bid = float(ob["bids"][0][0]); ask = float(ob["asks"][0][0])
    return bid, ask

def place_limit_postonly(market, side, price, amount):
    body = {
        "market": market, "side": side, "orderType": "limit", "postOnly": True,
        "clientOrderId": str(uuid4()), "price": str(price), "amount": str(amount)
    }
    ts  = str(int(time.time() * 1000))
    sig = _sign(ts, "POST", "/v2/order", json.dumps(body,separators=(',',':')))
    headers = {
        "Bitvavo-Access-Key": API_KEY, "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Signature": sig, "Bitvavo-Access-Window": "10000",
        "Content-Type": "application/json",
    }
    r = requests.post(f"{BASE_URL}/order", headers=headers, json=body, timeout=10)
    try: return r.json()
    except: return {"error": r.text}

def cancel_all(market):
    return bv_request("DELETE", f"/orders?market={market}")

# ===== Trading logic =====
def enter_position(market):
    eur = get_balance("EUR") - HEADROOM_EUR
    if eur <= 0:
        tg_send("⛔ لا يوجد EUR متاح.")
        return False
    bid, ask = get_best_bid_ask(market)
    price = bid
    amount = eur/price
    resp = place_limit_postonly(market,"buy",price,amount)
    if resp.get("error"):
        tg_send("⚠️ فشل شراء: "+str(resp)); return False
    ACTIVE.update({
        "market": market, "avg_entry": price, "amount": amount,
        "tp_price": price*(1+TP_TARGET_PCT/100),
        "sl_price": price*(1-SL_INIT_PCT/100),
        "high_price": price
    })
    tg_send(f"🚀 دخول {market} @ {price:.6f} | TP≈{ACTIVE['tp_price']:.6f} | SL≈{ACTIVE['sl_price']:.6f}")
    return True

def exit_position(reason="exit"):
    if not ACTIVE: return
    market = ACTIVE["market"]
    amt    = ACTIVE["amount"]
    bid, ask = get_best_bid_ask(market)
    price = bid
    resp = place_limit_postonly(market,"sell",price,amt)
    tg_send(f"💰 خروج {market} @ {price:.6f} ({reason})")
    ACTIVE.clear()
    return resp

def manage_loop():
    while True:
        time.sleep(2)
        if not ACTIVE: continue
        try:
            market = ACTIVE["market"]
            bid, ask = get_best_bid_ask(market)
            last = (bid+ask)/2
            # حدّث high
            if last > ACTIVE["high_price"]:
                ACTIVE["high_price"] = last
            # Trailing SL
            profit_pct = (last - ACTIVE["avg_entry"])/ACTIVE["avg_entry"]*100
            if profit_pct >= TRAIL_START:
                new_sl = last*(1-TRAIL_STEP/100)
                if new_sl > ACTIVE["sl_price"]:
                    ACTIVE["sl_price"] = new_sl
                    tg_send(f"🔒 رفع SL إلى {new_sl:.6f} (ربح مؤمّن)")
            # تحقق TP
            if last >= ACTIVE["tp_price"]:
                exit_position("TP hit")
            # تحقق SL
            if last <= ACTIVE["sl_price"]:
                exit_position("SL hit")
        except Exception as e:
            print("manage_loop err:",e)

threading.Thread(target=manage_loop, daemon=True).start()

# ===== Flask endpoints =====
@app.route("/hook", methods=["POST"])
def hook():
    data = request.get_json(force=True)
    coin = data.get("coin","").upper()
    if not coin: return jsonify(ok=False,err="no coin"),400
    market = f"{coin}-EUR"
    with LOCK:
        if ACTIVE and ACTIVE["market"]!=market:
            exit_position("switch to "+market)
        if not ACTIVE:
            enter_position(market)
    return jsonify(ok=True)

@app.route("/status", methods=["GET"])
def status():
    return jsonify(ACTIVE or {"status":"idle"})

@app.route("/", methods=["GET"])
def home():
    return "Saqar Signal-Only Runner ✅",200

# ===== Main =====
if __name__=="__main__":
    app.run(host="0.0.0.0", port=PORT)