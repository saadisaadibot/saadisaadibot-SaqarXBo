# -*- coding: utf-8 -*-
"""
Saqer — Mini Maker-Buy (Bitvavo) — with min order checks
- /hook يقبل:
  {"market":"GMX-EUR","eur":5}
  أو {"cmd":"buy","coin":"GMX","eur":5}
- يضع أمر Maker (postOnly) واحد فقط، بعد التحقق من حدود الماركت.
"""

import os, time, hmac, hashlib, json, math, requests
from flask import Flask, request, jsonify
from uuid import uuid4

API_KEY    = os.getenv("BITVAVO_API_KEY", "")
API_SECRET = os.getenv("BITVAVO_API_SECRET", "")
BASE_URL   = "https://api.bitvavo.com/v2"
PORT       = int(os.getenv("PORT", "8080"))

app = Flask(__name__)

# ====== Signing ======
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

# ====== Market meta (cached) ======
MARKET_META = {}  # "GMX-EUR": {"tick","step","minQuote","minBase"}

def _parse_step_from_precision(val, default_step):
    try:
        # قد تأتي كعدد منازل أو كخطوة
        if isinstance(val, int) or (isinstance(val, str) and str(val).isdigit()):
            d = int(val)
            if 0 <= d <= 20:
                return 10.0 ** (-d)
        v = float(val)
        if 0 < v < 1:
            return v
    except Exception:
        pass
    return default_step

def load_markets_once():
    global MARKET_META
    if MARKET_META:
        return
    try:
        rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
        for r in rows:
            if r.get("quote") != "EUR":
                continue
            m = r.get("market")
            tick = _parse_step_from_precision(r.get("pricePrecision", 6), 1e-6)
            step = _parse_step_from_precision(r.get("amountPrecision", 8), 1e-8)
            MARKET_META[m] = {
                "tick":     tick or 1e-6,
                "step":     step or 1e-8,
                "minQuote": float(r.get("minOrderInQuoteAsset", 0) or 0.0),
                "minBase":  float(r.get("minOrderInBaseAsset",  0) or 0.0),
            }
    except Exception as e:
        print("load_markets error:", e)

def get_meta(market: str):
    if not MARKET_META:
        load_markets_once()
    return MARKET_META.get(market)

def _decs(step: float) -> int:
    s = ("%.16f" % float(step)).rstrip("0").rstrip(".")
    return len(s.split(".")[1]) if "." in s else 0

def _tick(market): 
    meta = get_meta(market) or {}
    v = meta.get("tick", 1e-6)
    return v if (v and v > 0) else 1e-6

def _step(market): 
    meta = get_meta(market) or {}
    v = meta.get("step", 1e-8)
    return v if (v and v > 0) else 1e-8

def round_price_down(market, price):
    tk = _tick(market); dec = _decs(tk)
    p  = math.floor(max(0.0, float(price)) / tk) * tk
    return round(max(tk, p), dec)

def round_amount_down(market, amount):
    st = _step(market); dec = _decs(st)
    a  = math.floor(max(0.0, float(amount)) / st) * st
    return round(max(st, a), dec)

# ====== Core ======
def fetch_best_bid_ask(market: str):
    try:
        j = requests.get(f"{BASE_URL}/{market}/book?depth=1", timeout=6).json()
        bid = float(j["bids"][0][0]); ask = float(j["asks"][0][0])
        return bid, ask
    except Exception:
        return None, None

def min_required_eur(market: str, price: float) -> float:
    meta = get_meta(market) or {}
    minq = float(meta.get("minQuote", 0.0))
    minb = float(meta.get("minBase",  0.0))
    return max(minq, minb * max(0.0, float(price)))

def place_one_maker_buy(market: str, eur_amount: float):
    # 0) تأكد الميتاداتا
    meta = get_meta(market)
    if not meta:
        return {"ok": False, "err": f"market_meta_missing:{market}"}

    # 1) جِب أفضل Bid/Ask
    bid, ask = fetch_best_bid_ask(market)
    if not bid or not ask:
        return {"ok": False, "err": "no_orderbook"}

    # نشتري عند أفضل Bid (مع تدوير صحيح)
    raw_price = min(bid, ask * (1.0 - 1e-6))
    price     = round_price_down(market, raw_price)

    # 2) تحقق الحد الأدنى المطلوب باليورو
    need_eur = min_required_eur(market, price)
    if eur_amount + 1e-12 < need_eur:
        return {
            "ok": False,
            "err": "below_min_order",
            "need_eur": float(f"{need_eur:.6f}"),
            "sent_eur": float(f"{eur_amount:.6f}"),
            "hint": "ارفع eur إلى need_eur أو أكثر."
        }

    # 3) احسب الكمية ودوّرها للأسفل بالـ step
    amount = round_amount_down(market, eur_amount / price)

    # بعد التقريب، تأكد ثانية أن الشروط ما زالت محققة:
    quote_after = amount * price
    if amount + 1e-15 < meta.get("minBase", 0.0) or quote_after + 1e-12 < meta.get("minQuote", 0.0):
        # احسب أقل EUR يضمن الحد الأدنى بعد التقريب (تقريبي: minBase dominates عادة)
        need2 = max(meta.get("minQuote", 0.0), meta.get("minBase", 0.0) * price)
        return {
            "ok": False,
            "err": "below_min_after_round",
            "need_eur": float(f"{need2:.6f}"),
            "quote_after": float(f"{quote_after:.8f}"),
            "amount_after": float(f"{amount:.8f}")
        }

    # 4) أرسل أمر postOnly
    body = {
        "market": market,
        "side": "buy",
        "orderType": "limit",
        "postOnly": True,
        "clientOrderId": str(uuid4()),
        "price":  f"{price:.{_decs(_tick(market))}f}",
        "amount": f"{amount:.{_decs(_step(market))}f}",
        "operatorId": ""  # كما طلبت: فارغ
    }
    body_str = json.dumps(body, separators=(',', ':'))
    headers  = sign_headers("POST", "/order", body_str)
    resp     = requests.post(f"{BASE_URL}/order", headers=headers, data=body_str, timeout=10)

    try:
        j = resp.json()
    except Exception:
        return {"ok": False, "status": resp.status_code, "raw": resp.text}

    if j.get("orderId"):
        return {"ok": True, "request": body, "response": j}
    else:
        # بنرجّع خطأ المنصة كما هو لسهولة التشخيص
        return {"ok": False, "request": body, "response": j, "err": j.get("error", "unknown")}

# ====== HTTP ======
@app.route("/", methods=["GET"])
def home():
    return "Saqer Mini Maker-Buy ✅"

@app.route("/hook", methods=["POST"])
def hook():
    """
    يقبل:
      {"market":"GMX-EUR","eur":5}
      أو {"cmd":"buy","coin":"GMX","eur":5}
      أو {"coin":"GMX","eur":5}
    """
    data = request.get_json(silent=True) or {}
    raw  = {"received": data}

    # market normalization
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

    # eur
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

# ====== Main ======
if __name__ == "__main__":
    # نحمل markets لأول مرة الآن (ولو فشل ترجع لاحقًا)
    load_markets_once()
    app.run(host="0.0.0.0", port=PORT)