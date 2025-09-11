# -*- coding: utf-8 -*-
"""
Saqer — Mini Maker Executor (Bitvavo / EUR)
- /hook  : BUY maker بكل رصيد EUR المتاح (مع هامش أمان بسيط).
- /sell  : SELL maker بكل رصيد العملة الأساسية (Base) للماركت.
- تحقق دقيق من minQuote/minBase + تدوير السعر/الكمية حسب precision قبل الإرسال.
- يقبل من بوت الإشارة:
    شراء: {"market":"GMX-EUR"} أو {"cmd":"buy","coin":"GMX"}
    بيع : {"market":"GMX-EUR"} أو {"cmd":"sell","coin":"GMX"}
"""

import os, time, hmac, hashlib, json, math, requests
from uuid import uuid4
from flask import Flask, request, jsonify

# ====== ENV / App ======
API_KEY    = os.getenv("BITVAVO_API_KEY", "")
API_SECRET = os.getenv("BITVAVO_API_SECRET", "")
BASE_URL   = "https://api.bitvavo.com/v2"
PORT       = int(os.getenv("PORT", "8080"))

# هامش أمان بسيط عالرصيد (رسوم/انزلاق/تقريب)
BUFFER_EUR_MIN   = float(os.getenv("BUFFER_EUR_MIN", "0.30"))
EST_FEE_RATE     = float(os.getenv("EST_FEE_RATE",   "0.0025"))  # ~0.25%
BUFFER_FEE_MULT  = float(os.getenv("BUFFER_FEE_MULT","2.0"))     # مضاعِف تقديري

app = Flask(__name__)

# ====== Bitvavo signing/request ======
def _sign_headers(method: str, path: str, body_str: str = "") -> dict:
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

def _bv(method: str, path: str, body: dict | None = None, timeout=12):
    body_str = "" if method == "GET" else json.dumps(body or {}, separators=(',',':'))
    headers  = _sign_headers(method, path, body_str) if path not in (
        "/markets", f"/{{market}}/book"
    ) else {"Content-Type": "application/json"}  # public endpoints لا تحتاج توقيع
    url = f"{BASE_URL}{path}"
    if method == "GET":
        r = requests.get(url, headers=headers, timeout=timeout)
    else:
        r = requests.post(url, headers=headers, data=body_str, timeout=timeout)
    try:
        return r.json()
    except Exception:
        return {"status": r.status_code, "raw": r.text}

# ====== Market meta (precision & mins) ======
MARKET_META = {}  # "GMX-EUR": {"tick","step","minQuote","minBase"}

def _parse_step_from_precision(val, default_step):
    try:
        if isinstance(val, int) or (isinstance(val, str) and str(val).isdigit()):
            d = int(val)
            if 0 <= d <= 20:
                return 10.0 ** (-d)
        v = float(val)
        if 0 < v < 1: return v
    except Exception:
        pass
    return default_step

def load_markets_once():
    global MARKET_META
    if MARKET_META: return
    try:
        rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
        for r in rows:
            if r.get("quote") != "EUR": continue
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

def _meta(market): return MARKET_META.get(market) or {}

def _tick(market):
    v = _meta(market).get("tick", 1e-6)
    return v if (v and v > 0) else 1e-6

def _step(market):
    v = _meta(market).get("step", 1e-8)
    return v if (v and v > 0) else 1e-8

def _decs(step: float) -> int:
    s = ("%.16f" % float(step)).rstrip("0").rstrip(".")
    return len(s.split(".")[1]) if "." in s else 0

def round_price_down(market, price):
    tk=_tick(market); dec=_decs(tk)
    p=math.floor(max(0.0,float(price))/tk)*tk
    return round(max(tk,p), dec)

def round_amount_down(market, amount):
    st=_step(market); dec=_decs(st)
    a=math.floor(max(0.0,float(amount))/st)*st
    return round(max(st,a), dec)

def min_required_quote_eur(market: str, price: float) -> float:
    meta=_meta(market)
    return max(float(meta.get("minQuote",0.0)), float(meta.get("minBase",0.0))*max(0.0,float(price)))

# ====== Balance & Orderbook ======
def get_eur_available() -> float:
    j = _bv("GET", "/balance")
    if isinstance(j, list):
        for b in j:
            if b.get("symbol")=="EUR":
                return float(b.get("available",0) or 0.0)
    return 0.0

def get_base_available(base: str) -> float:
    j = _bv("GET", "/balance")
    if isinstance(j, list):
        for b in j:
            if b.get("symbol")==base.upper():
                return float(b.get("available",0) or 0.0)
    return 0.0

def fetch_best_bid_ask(market: str):
    try:
        j = requests.get(f"{BASE_URL}/{market}/book?depth=1", timeout=6).json()
        bid = float(j["bids"][0][0]); ask = float(j["asks"][0][0])
        return bid, ask
    except Exception:
        return None, None

# ====== Core: Maker BUY (all EUR) ======
def maker_buy_all_eur(market: str):
    # 0) meta + orderbook
    meta = _meta(market)
    if not meta: return {"ok": False, "err": f"market_meta_missing:{market}"}
    bid, ask = fetch_best_bid_ask(market)
    if not bid or not ask: return {"ok": False, "err": "no_orderbook"}

    # 1) صرف كل EUR المتاح بعد هامش أمان
    eur_avail = get_eur_available()
    buffer_eur = max(BUFFER_EUR_MIN, eur_avail * EST_FEE_RATE * BUFFER_FEE_MULT)
    eur_amount = max(0.0, eur_avail - buffer_eur)
    if eur_amount <= 0:
        return {"ok": False, "err": "insufficient_eur", "eur_avail": eur_avail, "buffer": buffer_eur}

    # 2) السعر والكمية وفق precision + فحص الحد الأدنى
    raw_price = min(bid, ask*(1.0-1e-6))
    price     = round_price_down(market, raw_price)
    need_eur  = min_required_quote_eur(market, price)
    if eur_amount + 1e-12 < need_eur:
        return {"ok": False, "err": "below_min_order", "need_eur": float(f"{need_eur:.6f}"),
                "eur_after_buffer": float(f"{eur_amount:.6f}")}

    amount    = round_amount_down(market, eur_amount / price)
    quote_val = amount * price
    if (amount + 1e-12) < meta.get("minBase", 0.0) or (quote_val + 1e-12) < meta.get("minQuote", 0.0):
        need2 = max(meta.get("minQuote",0.0), meta.get("minBase",0.0)*price)
        return {"ok": False, "err": "below_min_after_round", "need_eur": float(f"{need2:.6f}"),
                "quote_after": float(f"{quote_val:.8f}"), "amount_after": float(f"{amount:.8f}")}

    # 3) إرسال أمر postOnly (clientOrderId + operatorId="")
    body = {
        "market": market,
        "side": "buy",
        "orderType": "limit",
        "postOnly": True,
        "clientOrderId": str(uuid4()),
        "price":  f"{price:.{_decs(_tick(market))}f}",
        "amount": f"{amount:.{_decs(_step(market))}f}",
        "operatorId": ""
    }
    j = _bv("POST", "/order", body)
    if (j or {}).get("orderId"):
        return {"ok": True, "request": body, "response": j}
    return {"ok": False, "request": body, "response": j, "err": (j or {}).get("error","unknown")}

# ====== Core: Maker SELL (all base) ======
def maker_sell_all_base(market: str):
    # 0) meta + orderbook
    meta = _meta(market)
    if not meta: return {"ok": False, "err": f"market_meta_missing:{market}"}
    bid, ask = fetch_best_bid_ask(market)
    if not bid or not ask: return {"ok": False, "err": "no_orderbook"}

    base = market.split("-")[0]
    base_avail = get_base_available(base)
    if base_avail <= 0:
        return {"ok": False, "err": "insufficient_base", "base": base, "available": base_avail}

    # 1) السعر maker على أفضل Ask (أو أعلى بقليل) — مع تدوير
    raw_price = max(ask, bid*(1.0+1e-6))
    price     = round_price_down(market, raw_price)  # down كافي لتجنّب تجاوز tick
    amount    = round_amount_down(market, base_avail)

    # 2) تحقق الحد الأدنى
    quote_val = amount * price
    if (amount + 1e-12) < meta.get("minBase", 0.0) or (quote_val + 1e-12) < meta.get("minQuote", 0.0):
        need2 = max(meta.get("minQuote",0.0), meta.get("minBase",0.0)*price)
        return {"ok": False, "err": "below_min_after_round", "need_eur": float(f"{need2:.6f}"),
                "quote_after": float(f"{quote_val:.8f}"), "amount_after": float(f"{amount:.8f}")}

    # 3) إرسال أمر postOnly SELL
    body = {
        "market": market,
        "side": "sell",
        "orderType": "limit",
        "postOnly": True,
        "clientOrderId": str(uuid4()),
        "price":  f"{price:.{_decs(_tick(market))}f}",
        "amount": f"{amount:.{_decs(_step(market))}f}",
        "operatorId": ""
    }
    j = _bv("POST", "/order", body)
    if (j or {}).get("orderId"):
        return {"ok": True, "request": body, "response": j}
    return {"ok": False, "request": body, "response": j, "err": (j or {}).get("error","unknown")}

# ====== HTTP ======
@app.route("/", methods=["GET"])
def home():
    return "Saqer Mini Maker (BUY/SELL all) ✅"

def _normalize_market(data: dict):
    market = (data.get("market") or "").strip().upper()
    if not market:
        coin = (data.get("coin") or data.get("symbol") or data.get("asset") or "").strip().upper()
        cmd  = (data.get("cmd") or "").strip().lower()
        if coin:
            market = f"{coin}-EUR"
        elif cmd in ("buy","sell") and coin:
            market = f"{coin}-EUR"
    return market

@app.route("/hook", methods=["POST"])
def hook_buy_all():
    """
    شراء Maker بكل رصيد EUR.
    يقبل {"market":"GMX-EUR"} أو {"cmd":"buy","coin":"GMX"}.
    """
    load_markets_once()
    data   = request.get_json(silent=True) or {}
    market = _normalize_market(data)
    if not market or not market.endswith("-EUR"):
        return jsonify({"ok": False, "err": "no_market_detected", "received": data}), 200
    if not API_KEY or not API_SECRET:
        return jsonify({"ok": False, "err": "missing_api_keys"}), 200

    result = maker_buy_all_eur(market)
    return jsonify({"received": data, **result}), 200

@app.route("/sell", methods=["POST"])
def sell_all():
    """
    بيع Maker لكل رصيد العملة الأساسية للماركت.
    يقبل {"market":"GMX-EUR"} أو {"cmd":"sell","coin":"GMX"}.
    """
    load_markets_once()
    data   = request.get_json(silent=True) or {}
    market = _normalize_market(data)
    if not market or not market.endswith("-EUR"):
        return jsonify({"ok": False, "err": "no_market_detected", "received": data}), 200
    if not API_KEY or not API_SECRET:
        return jsonify({"ok": False, "err": "missing_api_keys"}), 200

    result = maker_sell_all_base(market)
    return jsonify({"received": data, **result}), 200

# ====== Main ======
if __name__ == "__main__":
    load_markets_once()
    app.run(host="0.0.0.0", port=PORT)