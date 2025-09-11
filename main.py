# -*- coding: utf-8 -*-
"""
Saqer — Maker-Only (Bitvavo / EUR) — Telegram + Strict Cancel/Reprice
- /buy  COIN [EUR]     → فتح أمر شراء Maker (يرفض لو في أمر مفتوح لنفس الماركت)
- /reprice COIN        → إعادة تسعير الأمر المفتوح (فقط بعد تأكيد الإلغاء)
- /cancel  COIN        → إلغاء صارم؛ لا نجاح إلا إذا status=cancelled/filled
- /status  COIN        → حالة الطلب المفتوح (status / remaining / onHold)
- /sell  COIN [AMOUNT] → بيع Maker
- /open                → عرض الأوامر المفتوحة
- /bal                 → عرض الأرصدة
"""

import os, re, time, json, hmac, hashlib, math, requests
from uuid import uuid4
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID     = os.getenv("CHAT_ID", "").strip()
API_KEY     = os.getenv("BITVAVO_API_KEY", "").strip()
API_SECRET  = os.getenv("BITVAVO_API_SECRET", "").strip()
PORT        = int(os.getenv("PORT", "8080"))

BASE_URL    = "https://api.bitvavo.com/v2"

HEADROOM_EUR        = float(os.getenv("HEADROOM_EUR", "0.30"))
REPRICE_EVERY_SEC   = float(os.getenv("REPRICE_EVERY_SEC", "2.0"))
REPRICE_THRESH_PCT  = float(os.getenv("REPRICE_THRESH_PCT", "0.05"))/100.0
CANCEL_WAIT_SEC     = float(os.getenv("CANCEL_WAIT_SEC", "8.0"))
SHORT_FOLLOW_SEC    = float(os.getenv("SHORT_FOLLOW_SEC", "2.0"))

MARKET_MAP  = {}
MARKET_META = {}
OPEN_ORDERS = {}  # market -> {"orderId": str, "amount_init": float}

# ========== Telegram ==========
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

# ========== Bitvavo ==========
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

def load_markets_once():
    global MARKET_MAP, MARKET_META
    if MARKET_MAP and MARKET_META: return
    rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
    m, meta = {}, {}
    for r in rows:
        if r.get("quote") != "EUR": continue
        market = r.get("market"); base = (r.get("base") or "").upper()
        if not base or not market: continue
        priceSig = int(r.get("pricePrecision", 6) or 6)  # significant digits
        ap = r.get("amountPrecision", 8)
        step = 10.0 ** (-int(ap)) if isinstance(ap, int) else float(ap or 1e-8)
        meta[market] = {
            "priceSig": priceSig,
            "step": float(step),
            "minQuote": float(r.get("minOrderInQuoteAsset", 0) or 0.0),
            "minBase":  float(r.get("minOrderInBaseAsset",  0) or 0.0),
        }
        m[base] = market
    MARKET_MAP, MARKET_META = m, meta

def coin_to_market(coin: str) -> str | None:
    load_markets_once(); return MARKET_MAP.get(coin.upper())

def _meta(market: str) -> dict: load_markets_once(); return MARKET_META.get(market, {})
def _price_sig(market: str) -> int: return int(_meta(market).get("priceSig", 6))
def _step(market: str) -> float:     return float(_meta(market).get("step", 1e-8))
def _min_quote(market: str) -> float:return float(_meta(market).get("minQuote", 0.0))
def _min_base(market: str) -> float: return float(_meta(market).get("minBase", 0.0))

def round_price_sig_down(price: float, sig: int) -> float:
    if price <= 0 or sig <= 0: return 0.0
    exp = math.floor(math.log10(abs(price)))
    dec = max(0, sig - exp - 1)
    factor = 10 ** dec
    return math.floor(price * factor) / factor

def fmt_price_sig(market: str, price: float) -> str:
    p = round_price_sig_down(price, _price_sig(market))
    s = f"{p:.12f}".rstrip("0").rstrip(".")
    return s if s else "0"

def round_amount_down(market: str, amount: float) -> float:
    st = _step(market)
    if st <= 0: return max(0.0, amount)
    return math.floor(float(amount) / st) * st

def fmt_amount(market: str, amount: float) -> str:
    st = _step(market)
    s = f"{st:.16f}".rstrip("0").rstrip("."); dec = len(s.split(".")[1]) if "." in s else 0
    a = round_amount_down(market, amount)
    return f"{a:.{dec}f}"

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

# ========== Cancel (strict) ==========
def cancel_order_blocking(market: str, orderId: str, wait_sec=CANCEL_WAIT_SEC):
    """
    يرجع (success:bool, final_status:str, last_state:dict|None).
    النجاح = status ∈ {canceled, filled}. غير ذلك فشل.
    """
    last = None
    # حاول الإلغاء
    del_resp = bv_request("DELETE", f"/order?market={market}&orderId={orderId}")
    # لو أعاد خطأ فوري، لا نتوقف—نكمل فحص الحالة (يمكن يكون ملغى أصلاً)
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        st = bv_request("GET", f"/order?market={market}&orderId={orderId}")
        last = st if isinstance(st, dict) else None
        s = (st or {}).get("status", "").lower()
        if s in ("canceled", "filled"):
            return True, s, last
        time.sleep(0.3)
    # فحص أخير
    st = bv_request("GET", f"/order?market={market}&orderId={orderId}")
    last = st if isinstance(st, dict) else last
    s = (st or {}).get("status", "").lower()
    return (s in ("canceled","filled")), s or "unknown", last

# ========== Order placement ==========
def place_limit_postonly(market: str, side: str, price: float, amount: float):
    body = {
        "market": market, "side": side, "orderType": "limit", "postOnly": True,
        "clientOrderId": str(uuid4()),
        "price": fmt_price_sig(market, price),
        "amount": fmt_amount(market, amount),
        "operatorId": ""
    }
    ts  = str(int(time.time() * 1000))
    sig = _sign(ts, "POST", "/v2/order", json.dumps(body, separators=(',',':')))
    headers = {
        "Bitvavo-Access-Key": API_KEY, "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Signature": sig, "Bitvavo-Access-Window": "10000",
        "Content-Type": "application/json",
    }
    r = requests.post(f"{BASE_URL}/order", headers=headers, json=body, timeout=10)
    try:
        return body, r.json()
    except Exception:
        return body, {"error": r.text}

# ========== BUY (open/reprice/cancel) ==========
def buy_open(market: str, eur_amount: float | None):
    if market in OPEN_ORDERS:
        return {"ok": False, "err": "order_already_open", "open": OPEN_ORDERS[market]}

    eur_avail = get_balance("EUR")
    spend = float(eur_avail) if (eur_amount is None or eur_amount <= 0) else float(eur_amount)
    spend = max(0.0, spend - HEADROOM_EUR)
    if spend <= 0:
        return {"ok": False, "err": f"No EUR to spend (avail={eur_avail:.2f})"}

    minq, minb = _min_quote(market), _min_base(market)
    if spend < minq:
        return {"ok": False, "err": f"minQuote={minq:.4f} EUR, have {spend:.2f}"}

    bid, ask = get_best_bid_ask(market)
    price  = round_price_sig_down(min(bid, ask*(1-1e-6)), _price_sig(market))
    amount = round_amount_down(market, spend/price)
    if amount < minb: return {"ok": False, "err": f"minBase={minb}"}

    body, resp = place_limit_postonly(market, "buy", price, amount)
    if (resp or {}).get("error"):
        return {"ok": False, "request": body, "response": resp, "err": (resp or {}).get("error")}

    oid = resp.get("orderId")
    OPEN_ORDERS[market] = {"orderId": oid, "amount_init": amount}
    return {"ok": True, "request": body, "response": resp, "open": OPEN_ORDERS[market]}

def buy_reprice(market: str):
    info = OPEN_ORDERS.get(market)
    if not info: return {"ok": False, "err": "no_open_order"}
    oid = info["orderId"]

    st = bv_request("GET", f"/order?market={market}&orderId={oid}")
    status = (st or {}).get("status","").lower()
    if status == "filled":
        OPEN_ORDERS.pop(market, None)
        return {"ok": True, "msg": "already_filled", "state": st}

    # المتبقي
    try:
        amt_rem = float((st or {}).get("amountRemaining", st.get("amount","0")) or 0.0)
    except Exception:
        amt_rem = info["amount_init"]
    if amt_rem <= 0: amt_rem = info["amount_init"]

    # ألغِ أولاً (صارم)
    ok, final, last = cancel_order_blocking(market, oid, CANCEL_WAIT_SEC)
    if not ok and final not in ("filled",):
        return {"ok": False, "err": f"cancel_failed_status={final}", "last": last}

    # إذا اتعبّى بالكامل أثناء الإلغاء
    if final == "filled":
        OPEN_ORDERS.pop(market, None)
        return {"ok": True, "msg": "filled_while_cancel"}

    # ضع أمر جديد بالسعر الحالي (لا نعتمد على EUR)
    bid, ask = get_best_bid_ask(market)
    new_price = round_price_sig_down(min(bid, ask*(1-1e-6)), _price_sig(market))
    amt_rem = max(amt_rem, _min_base(market))
    body, resp = place_limit_postonly(market, "buy", new_price, amt_rem)
    if (resp or {}).get("error"):
        return {"ok": False, "request": body, "response": resp, "err": (resp or {}).get("error")}

    OPEN_ORDERS[market] = {"orderId": resp.get("orderId"), "amount_init": amt_rem}
    return {"ok": True, "request": body, "response": resp, "open": OPEN_ORDERS[market]}

def buy_cancel(market: str):
    info = OPEN_ORDERS.get(market)
    if not info: return {"ok": False, "err": "no_open_order"}
    oid = info["orderId"]
    ok, final, last = cancel_order_blocking(market, oid, CANCEL_WAIT_SEC)
    if ok:
        OPEN_ORDERS.pop(market, None)
        return {"ok": True, "msg": final, "state": last}
    else:
        return {"ok": False, "err": f"cancel_failed_status={final}", "state": last}

# ========== SELL ==========
def maker_sell(market: str, amount: float | None):
    base = market.split("-")[0]
    if amount is None or amount <= 0:
        bals = bv_request("GET","/balance")
        amt  = 0.0
        if isinstance(bals, list):
            for b in bals:
                if b.get("symbol")==base.upper():
                    amt = float(b.get("available",0) or 0.0); break
    else:
        amt = float(amount)
    amt = round_amount_down(market, amt)
    if amt <= 0: return {"ok": False, "err": f"No {base} to sell"}

    minb = _min_base(market)
    if amt < minb: return {"ok": False, "err": f"minBase={minb}"}

    bid, ask = get_best_bid_ask(market)
    price = round_price_sig_down(max(ask, bid*(1+1e-6)), _price_sig(market))
    body, resp = place_limit_postonly(market, "sell", price, amt)
    if (resp or {}).get("error"):
        return {"ok": False, "request": body, "response": resp, "err": (resp or {}).get("error")}
    t0 = time.time(); oid = resp.get("orderId")
    while time.time()-t0 < SHORT_FOLLOW_SEC and oid:
        st = bv_request("GET", f"/order?market={market}&orderId={oid}")
        if (st or {}).get("status","").lower() in ("filled","partiallyfilled"): break
        time.sleep(0.25)
    return {"ok": True, "request": body, "response": resp}

# ========== Commands ==========
COIN_RE = re.compile(r"^[A-Z0-9]{2,15}$")
def _norm_market(arg: str) -> str | None:
    s = (arg or "").strip().upper()
    if not s: return None
    if s.endswith("-EUR") and COIN_RE.match(s.split("-")[0]): return s
    if COIN_RE.match(s): return f"{s}-EUR"
    return None

# ========== Telegram Webhook ==========
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
        if low.startswith("/start") or low.startswith("/help"):
            tg_send("أوامر:\n"
                    "/buy COIN [EUR]\n"
                    "/reprice COIN\n"
                    "/cancel COIN\n"
                    "/status COIN\n"
                    "/sell COIN [AMT]\n"
                    "/open\n"
                    "/bal")
            return jsonify(ok=True)

        if low.startswith("/open"):
            if not OPEN_ORDERS:
                tg_send("لا توجد أوامر شراء مفتوحة.")
            else:
                lines=[f"{m}: {info}" for m,info in OPEN_ORDERS.items()]
                tg_send("أوامر مفتوحة:\n" + "\n".join(lines))
            return jsonify(ok=True)

        if low.startswith("/bal"):
            eur = get_balance("EUR")
            bals = bv_request("GET","/balance")
            hold=[]
            if isinstance(bals,list):
                for b in bals:
                    sym=b.get("symbol",""); av=float(b.get("available",0) or 0)
                    if sym not in ("EUR","") and av>0: hold.append(f"{sym}={av:.8f}")
            tg_send(f"💶 EUR: €{eur:.2f}\n" + ("📦 "+", ".join(hold) if hold else "لا أصول أخرى."))
            return jsonify(ok=True)

        if low.startswith("/status"):
            parts = text.split()
            if len(parts)<2: tg_send("صيغة: /status COIN"); return jsonify(ok=True)
            market=_norm_market(parts[1].upper())
            if not market: tg_send("⛔ عملة غير صالحة."); return jsonify(ok=True)
            info = OPEN_ORDERS.get(market)
            if not info: tg_send("لا يوجد أمر مفتوح لهذه العملة."); return jsonify(ok=True)
            st = bv_request("GET", f"/order?market={market}&orderId={info['orderId']}")
            tg_send(json.dumps(st, ensure_ascii=False))
            return jsonify(ok=True)

        if low.startswith("/buy"):
            parts = text.split()
            if len(parts)<2: tg_send("صيغة: /buy COIN [EUR]"); return jsonify(ok=True)
            market=_norm_market(parts[1].upper())
            if not market: tg_send("⛔ عملة غير صالحة."); return jsonify(ok=True)
            if market in OPEN_ORDERS:
                tg_send(f"⛔ يوجد أمر شراء مفتوح: {OPEN_ORDERS[market]}"); return jsonify(ok=True)
            eur=None
            if len(parts)>=3:
                try: eur=float(parts[2])
                except: eur=None
            res = buy_open(market, eur)
            tg_send(("✅ BUY تم الفتح" if res.get("ok") else "⚠️ BUY فشل") + f" — {market}\n"
                    f"{json.dumps(res, ensure_ascii=False)}")
            return jsonify(ok=True)

        if low.startswith("/reprice"):
            parts = text.split()
            if len(parts)<2: tg_send("صيغة: /reprice COIN"); return jsonify(ok=True)
            market=_norm_market(parts[1].upper())
            if not market: tg_send("⛔ عملة غير صالحة."); return jsonify(ok=True)
            res = buy_reprice(market)
            tg_send(("✅ Repriced" if res.get("ok") else "⚠️ Reprice فشل") + f" — {market}\n"
                    f"{json.dumps(res, ensure_ascii=False)}")
            return jsonify(ok=True)

        if low.startswith("/cancel"):
            parts = text.split()
            if len(parts)<2: tg_send("صيغة: /cancel COIN"); return jsonify(ok=True)
            market=_norm_market(parts[1].upper())
            if not market: tg_send("⛔ عملة غير صالحة."); return jsonify(ok=True)
            res = buy_cancel(market)
            tg_send(("✅ Canceled" if res.get("ok") else "⚠️ Cancel فشل") + f" — {market}\n"
                    f"{json.dumps(res, ensure_ascii=False)}")
            return jsonify(ok=True)

        if low.startswith("/sell"):
            parts = text.split()
            if len(parts)<2: tg_send("صيغة: /sell COIN [AMOUNT]"); return jsonify(ok=True)
            market=_norm_market(parts[1].upper())
            if not market: tg_send("⛔ عملة غير صالحة."); return jsonify(ok=True)
            amt=None
            if len(parts)>=3:
                try: amt=float(parts[2])
                except: amt=None
            res = maker_sell(market, amt)
            tg_send(("✅ SELL أُرسل" if res.get("ok") else "⚠️ SELL فشل") + f" — {market}\n"
                    f"{json.dumps(res, ensure_ascii=False)}")
            return jsonify(ok=True)

        tg_send("أوامر: /buy /reprice /cancel /status /sell /open /bal")
        return jsonify(ok=True)

    except Exception as e:
        tg_send(f"🐞 خطأ: {e}")
        return jsonify(ok=True)

@app.route("/", methods=["GET"])
def home():
    return "Saqer Maker (strict cancel & reprice) ✅"

if __name__ == "__main__":
    load_markets_once()
    app.run(host="0.0.0.0", port=PORT)