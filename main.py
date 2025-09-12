# -*- coding: utf-8 -*-
"""
Saqer — Maker Buy + Chaser (Bitvavo / EUR) — Telegram: /buy /cancel (/sell اختياري)
- /buy COIN      → يفتح أمر Maker عند أفضل Bid ويطارد السعر (cancel & replace) حتى الامتلاء أو الإلغاء.
- /cancel        → يلغي جميع أوامر الماركت النشط ويوقف المطاردة فورًا.
- /sell COIN     → بيع Maker لكل الرصيد (اختياري).
"""

import os, re, time, json, math, hmac, hashlib, requests
from uuid import uuid4
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ========= Boot / ENV =========
load_dotenv()
app = Flask(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID     = os.getenv("CHAT_ID", "").strip()
API_KEY     = os.getenv("BITVAVO_API_KEY", "").strip()
API_SECRET  = os.getenv("BITVAVO_API_SECRET", "").strip()
PORT        = int(os.getenv("PORT", "8080"))

BASE_URL    = "https://api.bitvavo.com/v2"

# سلوك مالي
HEADROOM_EUR      = float(os.getenv("HEADROOM_EUR", "0.00"))   # اترك 0€ افتراضيًا
MIN_CHASE_SEC     = float(os.getenv("MIN_CHASE_SEC", "15"))
CHASE_POLL_SEC    = float(os.getenv("CHASE_POLL_SEC", "0.4"))
REPRICE_EVERY_SEC = float(os.getenv("REPRICE_EVERY_SEC","1.6"))
CANCEL_TIMEOUT_SEC= float(os.getenv("CANCEL_TIMEOUT_SEC","5"))

# ========= Caches / State =========
MARKET_MAP  = {}   # "GMX" -> "GMX-EUR"
MARKET_META = {}   # "GMX-EUR" -> {"priceSig","amountDec","minQuote","minBase"}
ACTIVE = {
    "market": None,
    "orderId": None,
    "clientOrderId": None,
    "amount_rem": 0.0,
    "started": 0.0,
    "last_place_ts": 0.0,
    "chasing": False
}

# ========= Utils =========
def tg_send(text: str):
    if not BOT_TOKEN:
        print("TG:", text); return
    try:
        data = {"chat_id": CHAT_ID or None, "text": text}
        if CHAT_ID:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=data, timeout=8)
        else:
            print("TG(no CHAT_ID):", text)
    except Exception as e:
        print("tg_send err:", e)

def _auth_chat(chat_id: str) -> bool:
    return (not CHAT_ID) or (str(chat_id) == str(CHAT_ID))

def _sign(ts, method, path, body=""):
    msg = f"{ts}{method}{path}{body}"
    return hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def bv_request(method: str, path: str, body: dict | None = None, timeout=10):
    url = f"{BASE_URL}{path}"
    ts  = str(int(time.time() * 1000))
    body_str = "" if method == "GET" else json.dumps(body or {}, separators=(',',':'))
    headers = {
        "Bitvavo-Access-Key": API_KEY,
        "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Signature": _sign(ts, method, f"/v2{path}", body_str),
        "Bitvavo-Access-Window": "10000",
        "Content-Type": "application/json",
    }
    r = requests.request(method, url, headers=headers,
                         json=(body or {}) if method != "GET" else None, timeout=timeout)
    try:
        return r.json()
    except Exception:
        return {"error": r.text}

# ========= Markets =========
def _decimals_from_step(step: float) -> int:
    s = f"{step:.16f}".rstrip("0").rstrip(".")
    if "." in s:
        return len(s.split(".")[1])
    return 0

def load_markets_once():
    global MARKET_MAP, MARKET_META
    if MARKET_MAP and MARKET_META: return
    rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
    m, meta = {}, {}
    for r in rows:
        if r.get("quote") != "EUR": continue
        market = r.get("market"); base = (r.get("base") or "").upper()
        if not base or not market: continue

        # price precision (significant digits)
        priceSig = int(r.get("pricePrecision", 6) or 6)

        # amount decimals (robust across field names)
        amount_dec = None
        # explicit decimals fields
        for key in ("orderAmountDecimals", "amountDecimals", "amountPrecision"):
            if r.get(key) is not None:
                try:
                    amount_dec = int(r.get(key))
                    break
                except:
                    pass
        # increment/step field → derive decimals
        if amount_dec is None:
            for key in ("orderAmountIncrement", "amountStep", "orderSizeIncrement"):
                if r.get(key):
                    try:
                        amount_dec = _decimals_from_step(float(r.get(key)))
                        break
                    except:
                        pass
        if amount_dec is None:
            amount_dec = 8  # fallback

        meta[market] = {
            "priceSig":  priceSig,
            "amountDec": int(amount_dec),
            "minQuote":  float(r.get("minOrderInQuoteAsset", 0) or 0.0),
            "minBase":   float(r.get("minOrderInBaseAsset",  0) or 0.0),
        }
        m[base] = market
    MARKET_MAP, MARKET_META = m, meta

def coin_to_market(coin: str) -> str | None:
    load_markets_once(); return MARKET_MAP.get(coin.upper())
def _meta(market: str) -> dict: load_markets_once(); return MARKET_META.get(market, {})

# ========= Rounding =========
def round_price_sig_down(price: float, sig: int) -> float:
    if price <= 0 or sig <= 0: return 0.0
    exp = math.floor(math.log10(abs(price)))
    dec = max(0, sig - exp - 1)
    factor = 10 ** dec
    return math.floor(price * factor) / factor

def fmt_price_sig(market: str, price: float) -> str:
    sig = int(_meta(market).get("priceSig", 6))
    p = round_price_sig_down(price, sig)
    s = f"{p:.12f}".rstrip("0").rstrip(".")
    return s if s else "0"

def fmt_amount(market: str, amount: float, force_decimals: int | None = None) -> str:
    dec = int(_meta(market).get("amountDec", 8)) if force_decimals is None else int(force_decimals)
    dec = max(0, dec)
    factor = 10 ** dec
    a = math.floor(max(0.0, float(amount)) * factor) / factor
    return f"{a:.{dec}f}"

# ========= REST helpers =========
def get_balance(symbol: str) -> float:
    bals = bv_request("GET", "/balance")
    if isinstance(bals, list):
        for b in bals:
            if b.get("symbol") == symbol.upper():
                return float(b.get("available", 0) or 0.0)
    return 0.0

def get_best_bid_ask(market: str) -> tuple[float, float]:
    ob = requests.get(f"{BASE_URL}/{market}/book?depth=1", timeout=8).json()
    bid = float(ob["bids"][0][0]); ask = float(ob["asks"][0][0])
    return bid, ask

def open_orders_list(market: str):
    j = bv_request("GET", f"/ordersOpen?market={market}")
    return j if isinstance(j, list) else []

# ========= Place / Cancel =========
AMT_DEC_ERR_RE = re.compile(r"(\d+)\s+decimal digits", re.IGNORECASE)

def _post_order(body: dict):
    ts  = str(int(time.time() * 1000))
    sig = _sign(ts, "POST", "/v2/order", json.dumps(body, separators=(',',':')))
    headers = {
        "Bitvavo-Access-Key": API_KEY, "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Signature": sig, "Bitvavo-Access-Window": "10000",
        "Content-Type": "application/json",
    }
    r = requests.post(f"{BASE_URL}/order", headers=headers, json=body, timeout=10)
    try:
        return r.json()
    except Exception:
        return {"error": r.text}

def place_limit_postonly(market: str, side: str, price: float, amount: float):
    """يرسل الطلب، ولو رجع خطأ 'amount … too many decimal digits' يعيد الإرسال بقصّ الكمية للحدّ المسموح."""
    body = {
        "market": market, "side": side, "orderType": "limit", "postOnly": True,
        "clientOrderId": str(uuid4()),
        "price": fmt_price_sig(market, price),
        "amount": fmt_amount(market, amount),
        "operatorId": ""
    }
    resp = _post_order(body)

    err = (resp or {}).get("error", "")
    if err and "too many decimal digits" in err.lower():
        m = AMT_DEC_ERR_RE.search(err)
        if m:
            allowed = int(m.group(1))
            # قصّ الكمية للدقّة المسموح بها وأعد الإرسال بنفس clientOrderId جديد
            body["clientOrderId"] = str(uuid4())
            body["amount"] = fmt_amount(market, float(body["amount"]), force_decimals=allowed)
            resp = _post_order(body)

    return body, resp

def cancel_all_orders(market: str) -> bool:
    _ = bv_request("DELETE", f"/orders?market={market}")
    t0 = time.time()
    while time.time() - t0 < CANCEL_TIMEOUT_SEC:
        if not open_orders_list(market):
            return True
        time.sleep(0.2)
    return False

# ========= BUY + CHASER =========
def maker_buy_chase(market: str):
    if ACTIVE["chasing"]:
        return {"ok": False, "err": "already_chasing", "state": {k:ACTIVE[k] for k in ("market","orderId")}}

    eur_avail = get_balance("EUR")
    spend = max(0.0, eur_avail - HEADROOM_EUR)
    meta = _meta(market)
    if spend < float(meta.get("minQuote", 0.0)):
        return {"ok": False, "err": f"minQuote={meta.get('minQuote',0.0):.2f}, have {spend:.2f}"}

    bid, ask = get_best_bid_ask(market)
    price = round_price_sig_down(min(bid, ask*(1-1e-6)), int(meta.get("priceSig",6)))
    amount = max(0.0, spend / max(price, 1e-12))

    # احترم الحد الأدنى للـ base
    amount_down = float(fmt_amount(market, amount))
    if amount_down < float(meta.get("minBase", 0.0)):
        return {"ok": False, "err": f"minBase={meta.get('minBase',0.0)}"}

    body, resp = place_limit_postonly(market, "buy", price, amount_down)
    if resp.get("error"):
        return {"ok": False, "request": body, "response": resp, "err": resp.get("error")}

    oid = resp.get("orderId"); coid = body.get("clientOrderId")
    ACTIVE.update({
        "market": market, "orderId": oid, "clientOrderId": coid,
        "amount_rem": amount_down, "started": time.time(),
        "last_place_ts": time.time(), "chasing": True
    })
    tg_send(f"✅ BUY مبدئيًا أُرسل (Maker) — {market}\n{json.dumps(resp)}")

    last_status = "new"
    last_reprice_ts = time.time()
    while ACTIVE["chasing"] and ACTIVE["market"] == market:
        st = bv_request("GET", f"/order?market={market}&orderId={oid}") or {}
        status = (st.get("status") or "").lower()
        if status in ("filled","partiallyfilled"):
            try:
                rem = float(st.get("amountRemaining", st.get("amount","0")) or 0.0)
            except Exception:
                rem = ACTIVE["amount_rem"]
            ACTIVE["amount_rem"] = rem
            if status == "filled" or rem <= 0:
                tg_send(f"🎯 BUY اكتمل — {market}")
                ACTIVE.update({"market":None,"orderId":None,"clientOrderId":None,"chasing":False})
                return {"ok": True, "filled": True, "state": st}
        last_status = status or last_status

        if time.time() - last_reprice_ts >= REPRICE_EVERY_SEC:
            bid, ask = get_best_bid_ask(market)
            target = round_price_sig_down(min(bid, ask*(1-1e-6)), int(meta.get("priceSig",6)))
            cur_price = float(body["price"])
            if target > cur_price:
                ok = cancel_all_orders(market)
                if not ok:
                    last_reprice_ts = time.time()
                    time.sleep(CHASE_POLL_SEC)
                    continue
                try:
                    rem = float(st.get("amountRemaining", st.get("amount","0")) or 0.0)
                except Exception:
                    rem = ACTIVE["amount_rem"]
                rem = max(rem, float(meta.get("minBase", 0.0)))
                body, resp2 = place_limit_postonly(market, "buy", target, rem)
                if resp2.get("error"):
                    tg_send(f"⚠️ Reprice فشل — {market}\n{json.dumps(resp2)}")
                else:
                    oid = resp2.get("orderId"); coid = body.get("clientOrderId")
                    ACTIVE.update({"orderId": oid, "clientOrderId": coid, "last_place_ts": time.time()})
            last_reprice_ts = time.time()

        time.sleep(CHASE_POLL_SEC)

    return {"ok": False, "canceled": True}

# ========= SELL (اختياري) =========
def maker_sell_all(market: str):
    base = market.split("-")[0]
    bals = bv_request("GET","/balance")
    amt  = 0.0
    if isinstance(bals, list):
        for b in bals:
            if b.get("symbol")==base.upper():
                amt = float(b.get("available",0) or 0.0); break
    dec = int(_meta(market).get("amountDec",8))
    if amt <= 0: return {"ok": False, "err": f"No {base} to sell"}
    amt = math.floor(amt * (10**dec)) / (10**dec)
    if amt <= 0: return {"ok": False, "err": f"amountDown=0"}

    bid, ask = get_best_bid_ask(market)
    price = round_price_sig_down(max(ask, bid*(1+1e-6)), int(_meta(market).get("priceSig",6)))
    body, resp = place_limit_postonly(market, "sell", price, amt)
    if (resp or {}).get("error"):
        return {"ok": False, "request": body, "response": resp, "err": (resp or {}).get("error")}
    tg_send(f"✅ SELL أُرسل — {market}\n{json.dumps(resp)}")
    return {"ok": True, "request": body, "response": resp}

# ========= Telegram =========
COIN_RE = re.compile(r"^[A-Z0-9]{2,15}$")
def _norm_market(arg: str) -> str | None:
    s = (arg or "").strip().upper()
    if not s: return None
    if s.endswith("-EUR") and COIN_RE.match(s.split("-")[0]): return s
    if COIN_RE.match(s): return f"{s}-EUR"
    return None

@app.route("/tg", methods=["POST"])
def tg_webhook():
    upd = request.get_json(silent=True) or {}
    msg = upd.get("message") or upd.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    text = (msg.get("text") or "").strip()
    if not chat_id: return jsonify(ok=True)
    if not _auth_chat(chat_id): return jsonify(ok=True)

    low = text.lower()
    try:
        if low.startswith("/buy"):
            parts = text.split()
            if len(parts)<2: tg_send("صيغة: /buy COIN"); return jsonify(ok=True)
            market=_norm_market(parts[1].upper())
            if not market: tg_send("⛔ عملة غير صالحة."); return jsonify(ok=True)
            if ACTIVE["chasing"]:
                tg_send("⛔ يوجد مطاردة نشطة. نفّذ /cancel أولاً."); return jsonify(ok=True)
            res = maker_buy_chase(market)
            tg_send(("✅ BUY اكتمل" if res.get("ok") else "⚠️ BUY لم يكتمل") + f" — {market}\n{json.dumps(res)}")
            return jsonify(ok=True)

        if low.startswith("/cancel"):
            m = ACTIVE["market"]
            if not m:
                tg_send("لا يوجد أمر نشط."); return jsonify(ok=True)
            ACTIVE["chasing"] = False
            ok = cancel_all_orders(m)
            ACTIVE.update({"market":None,"orderId":None,"clientOrderId":None})
            tg_send(("✅ أُلغيت كل الأوامر" if ok else "⚠️ لم تُمسح كل الأوامر") + f" — {m}")
            return jsonify(ok=True)

        if low.startswith("/sell"):
            parts = text.split()
            if len(parts)<2: tg_send("صيغة: /sell COIN"); return jsonify(ok=True)
            market=_norm_market(parts[1].upper())
            if not market: tg_send("⛔ عملة غير صالحة."); return jsonify(ok=True)
            res = maker_sell_all(market)
            tg_send(("✅ SELL تم" if res.get("ok") else "⚠️ SELL فشل") + f" — {market}\n{json.dumps(res)}")
            return jsonify(ok=True)

        tg_send("الأوامر: /buy COIN — /cancel — (/sell COIN)")
        return jsonify(ok=True)
    except Exception as e:
        tg_send(f"🐞 خطأ: {e}")
        return jsonify(ok=True)

# ========= Health =========
@app.route("/", methods=["GET"])
def home():
    return "Saqer Maker Chaser ✅"

# ========= Main =========
if __name__ == "__main__":
    load_markets_once()
    app.run(host="0.0.0.0", port=PORT)