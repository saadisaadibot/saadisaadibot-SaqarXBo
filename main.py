# -*- coding: utf-8 -*-
"""
Saqer — Maker Buy + Reprice (Bitvavo / EUR)
ويبهوك بسيط: /hook — buy / reprice / cancel / status
- احترام precision والحدود الدنيا من /markets
- التعديل بالسعر عبر PUT /order أولًا
- سقوط إلى cancel+wait ثم create عند الحاجة
- شراء كل الرصيد المتاح (مع هامش رسوم بسيط)
"""

import os, time, json, math, hmac, hashlib, requests
from flask import Flask, request, jsonify

# ========= ENV / App =========
API_KEY    = os.getenv("BITVAVO_API_KEY", "").strip()
API_SECRET = os.getenv("BITVAVO_API_SECRET", "").strip().encode()
OPERATOR_ID= int(os.getenv("OPERATOR_ID", "2001"))  # إلزامي لدى Bitvavo
HEADROOM_EUR = float(os.getenv("HEADROOM_EUR", "0.30"))  # هامش بسيط للرسوم
BASE       = "https://api.bitvavo.com/v2"

app = Flask(__name__)

# ========= Signing / HTTP =========
def _ts() -> str: return str(int(time.time()*1000))

def _sig(method: str, path: str, body: str = "") -> str:
    msg = _ts() + method + path + body
    return hmac.new(API_SECRET, msg.encode(), hashlib.sha256).hexdigest()

def _headers(method: str, path: str, body: str = "") -> dict:
    return {
        "Bitvavo-Access-Key": API_KEY,
        "Bitvavo-Access-Signature": _sig(method, path, body),
        "Bitvavo-Access-Timestamp": _ts(),
        "Bitvavo-Access-Window": "10000",
        "Content-Type": "application/json"
    }

def _req(method: str, path: str, body: dict | None = None, auth: bool = True, timeout=10):
    url = f"{BASE}{path}"
    if method == "GET":
        hdr = _headers("GET", path, "") if auth else {}
        r = requests.get(url, headers=hdr, timeout=timeout)
    else:
        b = json.dumps(body or {}, separators=(",",":"))
        hdr = _headers(method, path, b) if auth else {"Content-Type":"application/json"}
        r = requests.request(method, url, headers=hdr, data=b, timeout=timeout)
    try:
        return r.json()
    except Exception:
        return {"error": r.text, "status": r.status_code}

# ========= Market meta / precision =========
_meta_cache = {}

def load_market_meta(market: str) -> dict:
    m = _meta_cache.get(market)
    if m: return m
    rows = _req("GET", "/markets", auth=False) or []
    for r in rows:
        if r.get("market") == market:
            meta = {
                "pricePrecision": int(r["pricePrecision"]),     # number of significant digits
                "amountPrecision": int(r["amountPrecision"]),   # decimals
                "minQuote": float(r["minOrderInQuoteAsset"]),
                "minBase": float(r["minOrderInBaseAsset"])
            }
            _meta_cache[market] = meta
            return meta
    raise RuntimeError(f"market {market} not found")

def round_sig(x: float, sig: int) -> float:
    """تقريب بعدد خانات فعّالة (للسعر)."""
    if x == 0: return 0.0
    d = int(math.floor(math.log10(abs(x))))
    factor = 10 ** (sig - 1 - d)
    return round(x * factor) / factor

def fmt_sig(x: float, sig: int) -> str:
    """تمثيل السعر بخانات فعالة صحيحة (بدون مبالغة بالعشرية)."""
    return f"{round_sig(x, sig):.16g}"

def fmt_dec(x: float, decimals: int) -> str:
    """تمثيل الكمية بعدد من الخانات العشرية."""
    q = 10 ** decimals
    x = math.floor(x * q) / q
    return f"{x:.{decimals}f}"

# ========= Balances =========
def eur_available() -> float:
    bals = _req("GET", "/balance")
    if isinstance(bals, list):
        for b in bals:
            if b.get("symbol") == "EUR":
                return max(0.0, float(b.get("available", 0) or 0))
    return 0.0

# ========= Orders =========
def book_best_bid(market: str) -> float:
    j = requests.get(f"{BASE}/{market}/book?depth=1", timeout=6).json()
    return float(j["bids"][0][0])

def orders_open(market: str):
    return _req("GET", f"/ordersOpen?market={market}")

def create_maker_buy(market: str, price: str, amount: str):
    body = {
        "market": market, "side": "buy", "orderType": "limit",
        "postOnly": True, "clientOrderId": "", "operatorId": OPERATOR_ID,
        "price": price, "amount": amount
    }
    return _req("POST", "/order", body)

def fetch_order(market: str, orderId: str):
    return _req("GET", f"/order?market={market}&orderId={orderId}")

def update_order_price(market: str, orderId: str, new_price: str):
    body = {"market": market, "orderId": orderId, "price": new_price, "operatorId": OPERATOR_ID}
    return _req("PUT", "/order", body)

def cancel_order(market: str, orderId: str):
    return _req("DELETE", f"/order?market={market}&orderId={orderId}", {})

def wait_canceled_or_filled(market: str, orderId: str, timeout=10, poll=0.25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = fetch_order(market, orderId)
        s  = (st.get("status") or "").lower()
        if s in ("canceled", "filled"): return s
        time.sleep(poll)
    return None

# ========= Core flows =========
def place_all_eur_maker_buy(market: str, eur_hint: float | None = None):
    """يشتري كل الرصيد المتاح (أو eur_hint إن أعطيته) كأمر Maker بدقة صحيحة."""
    meta = load_market_meta(market)
    eur_av = eur_available()
    if eur_av <= HEADROOM_EUR:
        return {"ok": False, "err": f"No EUR to spend (avail={eur_av:.2f})"}

    spend = float(eur_hint) if eur_hint and eur_hint > 0 else (eur_av - HEADROOM_EUR)
    bid   = book_best_bid(market)
    px    = round_sig(bid, meta["pricePrecision"])
    min_needed = max(meta["minQuote"], meta["minBase"] * px)
    if spend < min_needed:
        return {"ok": False, "err": f"below_min_quote (need≥{min_needed:.6g})"}

    amt = spend / px
    req = {
        "market": market,
        "price": fmt_sig(px, meta["pricePrecision"]),
        "amount": fmt_dec(amt, meta["amountPrecision"])
    }
    resp = create_maker_buy(req["market"], req["price"], req["amount"])
    if "orderId" not in resp:
        return {"ok": False, "err": resp.get("error", resp), "request": req, "response": resp}
    return {"ok": True, "request": req, "response": resp}

def reprice_open_buy(market: str, reprice_thresh=0.0004):
    """يحاول تعديل السعر عبر PUT؛ إن فشل يلغي ثم يعيد الإنشاء بالسعر الجديد."""
    meta = load_market_meta(market)
    open_ = orders_open(market) or []
    open_buys = [o for o in open_ if o.get("side")=="buy" and o.get("status") in ("new","open","awaitingMarket")]
    if not open_buys:
        return {"ok": False, "err": "no_open_buy"}

    o      = open_buys[0]
    oid    = o["orderId"]
    old_px = float(o["price"])
    new_bid= book_best_bid(market)
    new_px = round_sig(new_bid, meta["pricePrecision"])

    if abs(new_px/old_px - 1.0) < reprice_thresh:
        return {"ok": True, "msg": "no_reprice_needed", "old": old_px, "new": new_px}

    # 1) حاول PUT
    u = update_order_price(market, oid, fmt_sig(new_px, meta["pricePrecision"]))
    if not u.get("error"):
        return {"ok": True, "msg": "repriced_via_put", "orderId": oid, "price": new_px}

    # 2) فشل PUT → cancel ثم تأكيد الإلغاء
    cancel_order(market, oid)
    st = wait_canceled_or_filled(market, oid, timeout=12)
    if st != "canceled":
        return {"ok": False, "err": f"cancel_failed_status={st}", "state": fetch_order(market, oid)}

    # 3) أعد الإنشاء بالسعر الجديد (نفس الكمية المتبقية)
    amt_rem = float(o.get("amountRemaining", o.get("amount")))
    # تأكد أنّ الكمية ما زالت ≥ الحدود
    min_quote = max(meta["minQuote"], meta["minBase"]*new_px)
    if (amt_rem * new_px) < min_quote:
        # أنشئ بحد أدنى من اليورو الممكن (لن يحدث عادةً إذا لم تتغيّر الكمية)
        amt_rem = min_quote / new_px

    req_amount = fmt_dec(amt_rem, meta["amountPrecision"])
    req_price  = fmt_sig(new_px, meta["pricePrecision"])
    c2 = create_maker_buy(market, req_price, req_amount)
    if "orderId" in c2:
        return {"ok": True, "msg": "recreated_after_cancel", "request":{"price":req_price,"amount":req_amount}, "response": c2}
    return {"ok": False, "err": c2.get("error", c2)}

def cancel_open_buy(market: str):
    open_ = orders_open(market) or []
    open_buys = [o for o in open_ if o.get("side")=="buy" and o.get("status") in ("new","open","awaitingMarket")]
    if not open_buys:
        return {"ok": False, "err": "no_open_buy"}
    oid = open_buys[0]["orderId"]
    cancel_order(market, oid)
    st = wait_canceled_or_filled(market, oid, timeout=12)
    if st == "canceled":
        return {"ok": True, "msg": "canceled"}
    return {"ok": False, "err": f"cancel_failed_status={st}", "state": fetch_order(market, oid)}

# ========= Webhook =========
COIN_MAP_CACHE = {}
def coin_to_market(coin: str) -> str | None:
    if coin in COIN_MAP_CACHE: return COIN_MAP_CACHE[coin]
    rows = _req("GET", "/markets", auth=False) or []
    for r in rows:
        if r.get("quote")=="EUR" and r.get("base")==coin.upper():
            COIN_MAP_CACHE[coin] = r["market"]
            return r["market"]
    return None

@app.route("/hook", methods=["POST"])
def hook():
    try:
        data = request.get_json(force=True) or {}
        cmd  = (data.get("cmd") or "").lower()
        coin = (data.get("coin") or "").upper()
        market = coin_to_market(coin) if coin else (data.get("market") or "")
        if not market:
            return jsonify({"ok": False, "err": "no market/coin"})

        if cmd == "buy":
            # eur=None → كل الرصيد
            eur = data.get("eur")
            eur = float(eur) if eur is not None else None
            res = place_all_eur_maker_buy(market, eur_hint=eur)
            return jsonify(res)

        if cmd == "reprice":
            res = reprice_open_buy(market)
            return jsonify(res)

        if cmd == "cancel":
            res = cancel_open_buy(market)
            return jsonify(res)

        if cmd == "status":
            return jsonify({"ok": True, "open": orders_open(market)})

        return jsonify({"ok": False, "err": "bad cmd"})

    except Exception as e:
        return jsonify({"ok": False, "err": str(e)}), 500

@app.route("/", methods=["GET"])
def health():
    return "Saqer Maker Buy/Reprice ✅"

# ========= Run =========
if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        print("Set BITVAVO_API_KEY / BITVAVO_API_SECRET env vars!")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))