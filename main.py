# -*- coding: utf-8 -*-
"""
Saqer — Mini Maker-Buy (Bitvavo)
- /hook: يستقبل أمر شراء من بوت الإشارة ويضع أمر Maker (postOnly) واحد.
- صيغ مقبولة:
  {"market":"GMX-EUR","eur":5}
  {"cmd":"buy","coin":"GMX","eur":5}
  {"coin":"GMX","eur":5}
  {"symbol":"GMX"}
"""

import os, time, hmac, hashlib, json, requests
from flask import Flask, request, jsonify
from uuid import uuid4

API_KEY    = os.getenv("BITVAVO_API_KEY", "")
API_SECRET = os.getenv("BITVAVO_API_SECRET", "")
BASE_URL   = "https://api.bitvavo.com/v2"
PORT       = int(os.getenv("PORT", "8080"))

app = Flask(__name__)

# ----- Bitvavo signing -----
def sign_headers(method: str, path: str, body_str: str = "") -> dict:
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

# ----- Helpers -----
def fetch_best_bid_ask(market: str):
    try:
        j = requests.get(f"{BASE_URL}/{market}/book?depth=1", timeout=6).json()
        bid = float(j["bids"][0][0]); ask = float(j["asks"][0][0])
        return bid, ask
    except Exception:
        return None, None

def place_one_maker_buy(market: str, eur_amount: float):
    bid, ask = fetch_best_bid_ask(market)
    if not bid or not ask:
        return {"ok": False, "err": "no_orderbook"}

    price  = bid
    amount = eur_amount / price

    body = {
        "market": market,
        "side": "buy",
        "orderType": "limit",
        "postOnly": True,
        "clientOrderId": str(uuid4()),
        "price": f"{price:.8f}",
        "amount": f"{amount:.8f}",
        "operatorId": ""  # فارغ كما طلبت
    }
    body_str = json.dumps(body, separators=(',',':'))
    headers  = sign_headers("POST", "/order", body_str)
    resp     = requests.post(f"{BASE_URL}/order", headers=headers, data=body_str, timeout=10)

    try:
        j = resp.json()
    except Exception:
        return {"ok": False, "status": resp.status_code, "raw": resp.text}

    if j.get("orderId"):
        return {"ok": True, "request": body, "response": j}
    else:
        return {"ok": False, "request": body, "response": j, "err": j.get("error","unknown")}

# ----- HTTP -----
@app.route("/", methods=["GET"])
def home():
    return "Saqer Mini Maker-Buy ✅"

@app.route("/hook", methods=["POST"])
def hook():
    data = request.get_json(silent=True) or {}
    raw  = {"received": data}  # نرجّعها للتشخيص

    # استخرج الماركت من أي صيغة
    market = (data.get("market") or "").strip().upper()
    if not market:
        coin = (data.get("coin") or data.get("symbol") or data.get("asset") or "").strip().upper()
        cmd  = (data.get("cmd") or "").strip().lower()
        if coin:
            market = f"{coin}-EUR"
        elif cmd == "buy" and coin:
            market = f"{coin}-EUR"

    if not market or not market.endswith("-EUR"):
        return jsonify({"ok": False, "err": "no_market_detected", **raw}), 200

    # EUR
    try:
        eur = float(data.get("eur")) if data.get("eur") is not None else 5.0
        if eur <= 0:
            return jsonify({"ok": False, "err": "eur_must_be_positive", **raw}), 200
    except Exception:
        return jsonify({"ok": False, "err": "eur_not_numeric", **raw}), 200

    if not API_KEY or not API_SECRET:
        return jsonify({"ok": False, "err": "missing_api_keys", **raw}), 200

    result = place_one_maker_buy(market, eur)
    return jsonify({**raw, **result}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)