# -*- coding: utf-8 -*-
"""
صقر — Mini Maker-Buy Executor
يستقبل أوامر شراء من بوت الإشارة → ينفذ أمر Maker (postOnly).
"""

import os, time, hmac, hashlib, requests
from flask import Flask, request, jsonify
from uuid import uuid4
from dotenv import load_dotenv

# ========= Boot / ENV =========
load_dotenv()
app = Flask(__name__)

API_KEY    = os.getenv("BITVAVO_API_KEY")
API_SECRET = os.getenv("BITVAVO_API_SECRET")
BASE_URL   = "https://api.bitvavo.com/v2"

# ========= Helpers =========
def _sign(path, body=""):
    ts   = str(int(time.time() * 1000))
    msg  = ts + "POST" + path + body
    sig  = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {"Bitvavo-Access-Key": API_KEY,
            "Bitvavo-Access-Signature": sig,
            "Bitvavo-Access-Timestamp": ts,
            "Content-Type": "application/json"}

def place_one_maker_buy(market: str, eur_amount: float):
    # نجيب السعر الحالي من orderbook
    ob = requests.get(f"{BASE_URL}/{market}/book?depth=1").json()
    best_bid = float(ob["bids"][0][0])
    price    = best_bid  # نشتري عند أفضل bid

    # كمية باليورو ÷ السعر
    amount = eur_amount / price

    body = {
        "market": market,
        "side": "buy",
        "orderType": "limit",
        "postOnly": True,
        "clientOrderId": str(uuid4()),
        "price": f"{price:.8f}",
        "amount": f"{amount:.8f}",
        "operatorId": ""   # ← فارغ
    }
    body_json = str(body).replace("'", '"')

    r = requests.post(f"{BASE_URL}/order", headers=_sign("/order", body_json), data=body_json)
    try:
        return r.json()
    except Exception:
        return {"err": r.text}

# ========= Webhook =========
@app.route("/hook", methods=["POST"])
def hook():
    data = request.get_json(force=True)
    market = data.get("market")
    eur    = float(data.get("eur", 5.0))  # افتراضي 5 يورو

    if not market:
        return jsonify({"ok": False, "err": "no market"})

    try:
        res = place_one_maker_buy(market, eur)
        return jsonify({"ok": True, "resp": res})
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})

# ========= Local Run =========
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)