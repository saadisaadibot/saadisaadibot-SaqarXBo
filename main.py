# -*- coding: utf-8 -*-
"""
صقر — Mini Maker-Buy Executor (Bitvavo)
- يستقبل أمر شراء من بوت الإشارة ويضع أمر Maker (postOnly) واحد.
- يقبل:
  {"market":"FLOKI-EUR","eur":5}
  أو
  {"cmd":"buy","coin":"FLOKI","eur":5}
"""

import os, time, hmac, hashlib, json, requests
from flask import Flask, request, jsonify
from uuid import uuid4

# ========= ENV / App =========
API_KEY    = os.getenv("BITVAVO_API_KEY", "")
API_SECRET = os.getenv("BITVAVO_API_SECRET", "")
BASE_URL   = "https://api.bitvavo.com/v2"
PORT       = int(os.getenv("PORT", "8080"))

app = Flask(__name__)

# ========= Bitvavo signing =========
def sign_headers(method: str, path: str, body_str: str = "") -> dict:
    """
    method: "POST"|"GET"|...
    path:   "/v2/order" يجب أن نمرر "/v2" داخل التوقيع (حسب توثيق Bitvavo)
            بما أننا نرسل path بدون BASE_URL هنا، نمرر "/v2" + path_in_func.
    """
    ts  = str(int(time.time() * 1000))
    msg = ts + method + ("/v2" + path) + body_str
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {
        "Bitvavo-Access-Key": API_KEY,
        "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Signature": sig,
        "Bitvavo-Access-Window": "10000",
        "Content-Type": "application/json",
    }

# ========= Core =========
def fetch_best_bid_ask(market: str):
    try:
        j = requests.get(f"{BASE_URL}/{market}/book?depth=1", timeout=6).json()
        bid = float(j["bids"][0][0]); ask = float(j["asks"][0][0])
        return bid, ask
    except Exception:
        return None, None

def place_one_maker_buy(market: str, eur_amount: float):
    # 1) سعر من الـ orderbook
    bid, ask = fetch_best_bid_ask(market)
    if not bid or not ask:
        return {"ok": False, "err": "no_orderbook"}

    # نشتري على أفضل Bid
    price  = bid
    amount = eur_amount / price

    # حضّر الجسم بصيغة JSON مضبوطة (نفسها للتوقيع والإرسال)
    body = {
        "market": market,
        "side": "buy",
        "orderType": "limit",
        "postOnly": True,
        "clientOrderId": str(uuid4()),
        "price": f"{price:.8f}",
        "amount": f"{amount:.8f}",
        "operatorId": ""  # تتركه فاضي كما طلبت
    }
    body_str = json.dumps(body, separators=(',', ':'))

    # 2) إرسال الطلب
    headers = sign_headers("POST", "/order", body_str)
    resp = requests.post(f"{BASE_URL}/order", headers=headers, data=body_str, timeout=10)

    # 3) نتيجة واضحة
    try:
        j = resp.json()
    except Exception:
        return {"ok": False, "status": resp.status_code, "raw": resp.text}

    if j.get("orderId"):
        return {"ok": True, "request": body, "response": j}
    else:
        return {"ok": False, "request": body, "response": j, "err": j.get("error", "unknown")}

# ========= HTTP =========
@app.route("/", methods=["GET"])
def home():
    return "Saqer Mini Maker-Buy ✅"

@app.route("/hook", methods=["POST"])
def hook():
    """
    يقبل:
      {"market":"FLOKI-EUR","eur":5}
      أو {"cmd":"buy","coin":"FLOKI","eur":5}
    """
    data = request.get_json(silent=True) or {}

    # تطبيع الإدخال
    market = (data.get("market") or "").strip().upper()
    if not market:
        # نمط بوت الإشارة
        cmd  = (data.get("cmd") or "").strip().lower()
        coin = (data.get("coin") or "").strip().upper()
        if cmd == "buy" and coin:
            market = f"{coin}-EUR"

    if not market.endswith("-EUR"):
        return jsonify({"ok": False, "err": "bad_or_missing_market"}), 400

    try:
        eur = float(data.get("eur")) if data.get("eur") is not None else 5.0
        if eur <= 0: 
            return jsonify({"ok": False, "err": "eur_must_be_positive"}), 400
    except Exception:
        return jsonify({"ok": False, "err": "eur_not_numeric"}), 400

    if not API_KEY or not API_SECRET:
        return jsonify({"ok": False, "err": "missing_api_keys"}), 500

    try:
        result = place_one_maker_buy(market, eur)
        return jsonify(result), (200 if result.get("ok") else 200)
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)}), 500

# ========= Main =========
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)