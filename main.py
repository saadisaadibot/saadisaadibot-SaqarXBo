# -*- coding: utf-8 -*-
"""
Minimal Maker BUY Relay (Bitvavo / EUR)
- /hook: يستقبل {"cmd":"buy","coin":"ADA","eur":10.0}  (eur اختياري)
- يضع أمر limit postOnly واحد على أفضل Bid الحالي ثم يرجع نتيجة واضحة.
"""

import os, re, time, json, math, hmac, hashlib, requests
from flask import Flask, request, jsonify

# ====== Config / ENV ======
BITVAVO_API_KEY    = os.getenv("BITVAVO_API_KEY", "")
BITVAVO_API_SECRET = os.getenv("BITVAVO_API_SECRET", "")
PORT               = int(os.getenv("PORT", "8080"))

BASE_URL = "https://api.bitvavo.com/v2"

# ====== Flask ======
app = Flask(__name__)

# ====== Bitvavo REST helpers ======
def _sig(ts, method, path, body_str=""):
    msg = f"{ts}{method}{path}{body_str}"
    return hmac.new(BITVAVO_API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def bv_request(method, path, body=None, timeout=12):
    url = f"{BASE_URL}{path}"
    ts  = str(int(time.time()*1000))
    body_str = "" if method=="GET" else json.dumps(body or {}, separators=(',',':'))
    headers = {
        "Bitvavo-Access-Key": BITVAVO_API_KEY,
        "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Signature": _sig(ts, method, f"/v2{path}", body_str),
        "Bitvavo-Access-Window": "10000",
    }
    resp = requests.request(method, url, headers=headers,
                            json=(body or {}) if method!="GET" else None,
                            timeout=timeout)
    return resp.json()

# ====== Market metadata (precision / mins) ======
MARKET_META = {}   # "GMX-EUR": {"tick":..., "step":..., "minQuote":..., "minBase":...}

def _parse_step_from_precision(val, default_step):
    try:
        # Bitvavo قد يرسلها كعدد خانات أو كخطوة
        if isinstance(val, int) or (isinstance(val, str) and str(val).isdigit()):
            d = int(val);  return 10.0 ** (-d) if 0 <= d <= 20 else default_step
        v = float(val);  return v if 0 < v < 1 else default_step
    except Exception:
        return default_step

def load_markets():
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

# حمّل الميتاداتا عند الاستيراد
load_markets()

def _tick(market): 
    v=(MARKET_META.get(market, {}) or {}).get("tick"); 
    return v if (v and v>0) else 1e-6

def _step(market): 
    v=(MARKET_META.get(market, {}) or {}).get("step"); 
    return v if (v and v>0) else 1e-8

def _decs(step):
    s = ("%.16f" % float(step)).rstrip("0").rstrip(".")
    return len(s.split(".")[1]) if "." in s else 0

def _round_price_down(market, price):
    tk=_tick(market); dec=_decs(tk)
    p=math.floor(max(0.0,float(price))/tk)*tk
    return round(max(tk,p), dec)

def _round_amount_down(market, amount):
    st=_step(market); dec=_decs(st)
    a=math.floor(max(0.0,float(amount))/st)*st
    return round(max(st,a), dec)

def _min_required_eur(market, price):
    meta = MARKET_META.get(market, {}) or {}
    minq = float(meta.get("minQuote", 0) or 0)
    minb = float(meta.get("minBase",  0) or 0)
    return max(minq, minb * max(0.0, float(price)))

# ====== Simple helpers ======
def get_eur_available():
    try:
        bals = bv_request("GET","/balance")
        for b in bals:
            if b.get("symbol")=="EUR":
                return float(b.get("available",0) or 0)
    except Exception:
        pass
    return 0.0

def fetch_orderbook_best(market):
    """رجّع (best_bid, best_ask) أو (None, None)."""
    try:
        j = requests.get(f"{BASE_URL}/{market}/book", timeout=6).json()
        bid = float(j["bids"][0][0]); ask = float(j["asks"][0][0])
        return bid, ask
    except Exception:
        return None, None

# ====== Core: place ONE postOnly order ======
def place_one_maker_buy(market: str, eur: float|None):
    # 1) تأكيد وجود meta
    if market not in MARKET_META:
        return {"ok": False, "err": f"market_meta_missing:{market}"}

    # 2) حدد كم نصرف
    eur_avail = max(0.0, get_eur_available())
    target = float(eur) if (eur is not None and eur>0) else eur_avail
    buffer = 0.05
    target = min(target, eur_avail)
    spend  = max(0.0, target - buffer)
    if spend <= 0:
        return {"ok": False, "err": "insufficient_eur_after_buffer", "eur_avail": eur_avail}

    # 3) سعر Maker: على أفضل Bid (أو أسفل الـ Ask بشعرة لو لزم)
    best_bid, best_ask = fetch_orderbook_best(market)
    if not best_bid or not best_ask:
        return {"ok": False, "err": "no_orderbook"}

    raw_price = min(best_bid, best_ask*(1.0-1e-6))
    price     = _round_price_down(market, raw_price)

    # 4) تحقق الحد الأدنى للمنصة
    need_eur = _min_required_eur(market, price)
    if spend + 1e-12 < need_eur:
        return {"ok": False, "err": "below_min_order", "need_eur": need_eur, "spend": spend}

    # 5) احسب الكمية ودوّرها للأسفل
    amount = _round_amount_down(market, spend / price)
    if amount <= 0:
        return {"ok": False, "err": "amount_zero_after_round", "step": _step(market)}

    # 6) ضع طلب postOnly واحد
    body = {
        "market": market,
        "side": "buy",
        "orderType": "limit",
        "postOnly": True,
        "clientOrderId": str(uuid4()),
        "price": f"{price:.{_decs(_tick(market))}f}",
        "amount": f"{amount:.{_decs(_step(market))}f}",
    }
    res = bv_request("POST","/order", body)

    # 7) ارجع النتيجة كما هي مع بعض المعلومات المفيدة
    out = {"request": body, "response": res}
    oid = (res or {}).get("orderId")
    if oid:
        # اجلب الحالة مرة واحدة (اختياري)
        st = bv_request("GET", f"/order?market={market}&orderId={oid}")
        out["status"] = st
        out["ok"] = True
    else:
        out["ok"] = False
        out["err"] = str((res or {}).get("error","unknown"))
    return out

# ====== HTTP endpoints ======
@app.route("/hook", methods=["POST"])
def hook():
    """
    JSON:
      { "cmd":"buy", "coin":"GMX", "eur": 8.85 }
      - eur اختياري، إذا غاب يشتري بكل رصيد EUR المتاح (مع خصم هامش صغير).
    """
    try:
        data = request.get_json(silent=True) or {}
        if (data.get("cmd") or "").lower() != "buy":
            return jsonify({"ok": False, "err": "only_buy_supported"}), 400

        coin = (data.get("coin") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{2,15}", coin or ""):
            return jsonify({"ok": False, "err": "bad_coin"}), 400

        eur = data.get("eur")
        eur = float(eur) if eur is not None else None

        market = f"{coin}-EUR"
        result = place_one_maker_buy(market, eur)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)}), 500

@app.route("/", methods=["GET"])
def home():
    return "Minimal Maker BUY Relay ✅"

# ====== Main ======
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)