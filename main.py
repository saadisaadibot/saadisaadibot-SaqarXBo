# -*- coding: utf-8 -*-
"""
Saqer — Maker-Only (Bitvavo / EUR) — Telegram Webhook
- /buy <COIN> [EUR]  → Maker limit (postOnly) بسعر متوافق مع عدد الخانات المعنوية.
- /sell <COIN> [AMOUNT] → Maker limit (postOnly) للكمية المطلوبة أو كل الرصيد.
- /bal  → رصيد EUR وبعض الأصول.
ملاحظة: السعر يلتزم pricePrecision (significant digits)، والكمية تلتزم amountPrecision (decimal places).
"""

import os, re, time, json, hmac, hashlib, math, requests
from uuid import uuid4
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ========= Boot / ENV =========
load_dotenv()
app = Flask(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID     = os.getenv("CHAT_ID", "").strip()  # اختياري لتقييد المحادثة
API_KEY     = os.getenv("BITVAVO_API_KEY", "").strip()
API_SECRET  = os.getenv("BITVAVO_API_SECRET", "").strip()
PORT        = int(os.getenv("PORT", "8080"))

BASE_URL    = "https://api.bitvavo.com/v2"

# هامش بسيط لحماية الرسوم وتذبذبات السنتات
HEADROOM_EUR = float(os.getenv("HEADROOM_EUR", "0.30"))  # يُخصم من EUR قبل الشراء
# أقصى وقت انتظاره لمراقبة تعبئة سريعة بعد وضع الأمر (ثوانٍ)
SHORT_FOLLOW_SEC = float(os.getenv("SHORT_FOLLOW_SEC", "2.0"))

# كاش للماركتات
MARKET_MAP  = {}   # "GMX" -> "GMX-EUR"
MARKET_META = {}   # "GMX-EUR" -> {"priceSig":6, "step":1e-8, "minQuote":5.0, "minBase":0.0001}

# ========= Telegram =========
def tg_send(text: str):
    if not BOT_TOKEN:
        print("TG:", text); return
    try:
        chat_id = CHAT_ID or None
        data = {"chat_id": chat_id, "text": text}
        if chat_id:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=data, timeout=8)
        else:
            # ما في CHAT_ID → أرسل لنفسك؟ (لن نعرف مع من)
            print("TG(no CHAT_ID):", text)
    except Exception as e:
        print("TG err:", e)

def _auth_chat(chat_id: str) -> bool:
    return (not CHAT_ID) or (str(chat_id) == str(CHAT_ID))

# ========= Bitvavo helpers =========
def _sign(ts, method, path, body=""):
    msg = f"{ts}{method}{path}{body}"
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return sig

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
    if MARKET_MAP and MARKET_META:
        return
    rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
    m, meta = {}, {}
    for r in rows:
        if r.get("quote") != "EUR":
            continue
        market = r.get("market")
        base   = r.get("base", "").upper()
        if not base or not market:
            continue
        # السعر: عدد خانات معنوية pricePrecision
        priceSig = int(r.get("pricePrecision", 6) or 6)
        # الكمية: amountPrecision يمكن أن يكون رقم منازل عشرية أو خطوة مباشرة
        amt_prec = r.get("amountPrecision", 8)
        step = None
        try:
            # Bitvavo يرجع غالباً رقم منازل عشرية (مثلاً 8)
            if isinstance(amt_prec, int):
                step = 10.0 ** (-amt_prec)
            else:
                v = float(amt_prec)
                step = v if 0 < v < 1 else 10.0 ** (-int(v))
        except Exception:
            step = 1e-8
        meta[market] = {
            "priceSig": priceSig,
            "step": float(step),
            "minQuote": float(r.get("minOrderInQuoteAsset", 0) or 0.0),
            "minBase":  float(r.get("minOrderInBaseAsset",  0) or 0.0),
        }
        m[base] = market
    MARKET_MAP, MARKET_META = m, meta

def coin_to_market(coin: str) -> str | None:
    load_markets_once()
    return MARKET_MAP.get(coin.upper())

def _meta(market: str) -> dict:
    load_markets_once()
    return MARKET_META.get(market, {})

def _price_sig(market: str) -> int:
    return int(_meta(market).get("priceSig", 6))

def _step(market: str) -> float:
    return float(_meta(market).get("step", 1e-8))

def _min_quote(market: str) -> float:
    return float(_meta(market).get("minQuote", 0.0))

def _min_base(market: str) -> float:
    return float(_meta(market).get("minBase", 0.0))

def round_price_sig_down(price: float, sig: int) -> float:
    # قصّ السعر لعدد خانات معنوية محدد (نزولًا)
    if price <= 0 or sig <= 0:
        return 0.0
    exp = math.floor(math.log10(abs(price)))
    dec = max(0, sig - exp - 1)  # كم منزلة بعد الفاصلة نحتفظ فيها
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
    # استنتج عدد المنازل من الـ step
    s = f"{st:.16f}".rstrip("0").rstrip(".")
    dec = len(s.split(".")[1]) if "." in s else 0
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
    bid = float(ob["bids"][0][0])
    ask = float(ob["asks"][0][0])
    return bid, ask

# ========= Maker Orders =========
def place_limit_postonly(market: str, side: str, price: float, amount: float):
    body = {
        "market": market,
        "side": side,
        "orderType": "limit",
        "postOnly": True,
        "clientOrderId": str(uuid4()),
        "price": fmt_price_sig(market, price),
        "amount": fmt_amount(market, amount),
        "operatorId": ""  # Bitvavo يسمح به فارغاً
    }
    ts  = str(int(time.time() * 1000))
    sig = _sign(ts, "POST", "/v2/order", json.dumps(body, separators=(',',':')))
    headers = {
        "Bitvavo-Access-Key": API_KEY,
        "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Signature": sig,
        "Bitvavo-Access-Window": "10000",
        "Content-Type": "application/json",
    }
    r = requests.post(f"{BASE_URL}/order", headers=headers, json=body, timeout=10)
    try:
        return body, r.json()
    except Exception:
        return body, {"error": r.text}

def maker_buy_all_eur(market: str, eur_amount: float | None):
    eur_avail = get_balance("EUR")
    spend = float(eur_avail) if (eur_amount is None or eur_amount <= 0) else float(eur_amount)
    spend = max(0.0, spend - HEADROOM_EUR)  # اترك هامش بسيط
    if spend <= 0:
        return {"ok": False, "err": f"No EUR to spend (avail={eur_avail:.2f})"}

    bid, ask = get_best_bid_ask(market)
    raw_price = min(bid, ask * (1.0 - 1e-6))
    price = round_price_sig_down(raw_price, _price_sig(market))
    if price <= 0:
        return {"ok": False, "err": "bad price"}

    # تحقق الحدود الدنيا
    minq = _min_quote(market)
    minb = _min_base(market)
    if spend < minq:
        return {"ok": False, "err": f"minQuote={minq:.4f} EUR, you have {spend:.2f}"}

    amount = round_amount_down(market, spend / price)
    if amount <= 0 or amount < minb:
        return {"ok": False, "err": f"minBase={minb} {market.split('-')[0]}"}

    body, resp = place_limit_postonly(market, "buy", price, amount)

    # متابعة قصيرة
    t0 = time.time()
    orderId = (resp or {}).get("orderId")
    status  = (resp or {}).get("status", "").lower()
    if orderId:
        while time.time() - t0 < SHORT_FOLLOW_SEC:
            st = bv_request("GET", f"/order?market={market}&orderId={orderId}")
            st_status = (st or {}).get("status", "").lower()
            if st_status in ("filled", "partiallyfilled"):
                break
            time.sleep(0.2)

    return {
        "ok": not bool((resp or {}).get("error")),
        "request": body, "response": resp
    }

def maker_sell_all_base(market: str, amount: float | None):
    base = market.split("-")[0]
    amt  = get_balance(base) if (amount is None or amount <= 0) else float(amount)
    amt  = round_amount_down(market, amt)
    if amt <= 0:
        return {"ok": False, "err": f"No {base} to sell"}

    bid, ask = get_best_bid_ask(market)
    raw_price = max(ask, bid * (1.0 + 1e-6))
    price = round_price_sig_down(raw_price, _price_sig(market))
    if price <= 0:
        return {"ok": False, "err": "bad price"}

    if amt < _min_base(market):
        return {"ok": False, "err": f"minBase={_min_base(market)}"}

    body, resp = place_limit_postonly(market, "sell", price, amt)

    # متابعة قصيرة
    t0 = time.time()
    orderId = (resp or {}).get("orderId")
    if orderId:
        while time.time() - t0 < SHORT_FOLLOW_SEC:
            st = bv_request("GET", f"/order?market={market}&orderId={orderId}")
            st_status = (st or {}).get("status", "").lower()
            if st_status in ("filled", "partiallyfilled"):
                break
            time.sleep(0.2)

    return {"ok": not bool((resp or {}).get("error")), "request": body, "response": resp}

# ========= Telegram webhook =========
@app.route("/tg", methods=["POST"])
def tg_webhook():
    upd = request.get_json(silent=True) or {}
    msg = upd.get("message") or upd.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    text = (msg.get("text") or "").strip()
    if not chat_id:
        return jsonify(ok=True)
    if not _auth_chat(chat_id):
        # تجاهل بصمت
        return jsonify(ok=True)

    low = text.lower()
    try:
        if low.startswith("/start"):
            tg_send("أهلًا بك 👋\n"
                    "الأوامر:\n"
                    "• /buy <COIN> [EUR]\n"
                    "• /sell <COIN> [AMOUNT]\n"
                    "• /bal")
            return jsonify(ok=True)

        if low.startswith("/bal"):
            eur = get_balance("EUR")
            bals = bv_request("GET", "/balance")
            hold = []
            if isinstance(bals, list):
                for b in bals:
                    sym = b.get("symbol","")
                    av  = float(b.get("available",0) or 0)
                    if sym not in ("EUR","") and av>0:
                        hold.append(f"{sym}={av:.8f}")
            tg_send(f"💶 EUR avail: €{eur:.2f}\n" + ("📦 " + ", ".join(hold) if hold else "لا أصول أخرى."))
            return jsonify(ok=True)

        if low.startswith("/buy"):
            # /buy GMX [EUR]
            parts = text.split()
            if len(parts) < 2:
                tg_send("صيغة الأمر: /buy COIN [EUR]"); return jsonify(ok=True)
            coin = parts[1].upper()
            market = coin_to_market(coin)
            if not market:
                tg_send(f"⛔ {coin}-EUR غير متاح."); return jsonify(ok=True)
            eur = None
            if len(parts) >= 3:
                try: eur = float(parts[2])
                except: eur = None
            res = maker_buy_all_eur(market, eur)
            if res.get("ok"):
                tg_send(f"✅ BUY مبدئيًا أُرسل (Maker) — {market}\n{json.dumps(res, ensure_ascii=False)}")
            else:
                tg_send(f"⚠️ BUY فشل: {res.get('err') or res.get('response',{}).get('error','')}\n"
                        f"| {json.dumps(res, ensure_ascii=False)}")
            return jsonify(ok=True)

        if low.startswith("/sell"):
            # /sell GMX [AMOUNT]
            parts = text.split()
            if len(parts) < 2:
                tg_send("صيغة الأمر: /sell COIN [AMOUNT]"); return jsonify(ok=True)
            coin = parts[1].upper()
            market = coin_to_market(coin)
            if not market:
                tg_send(f"⛔ {coin}-EUR غير متاح."); return jsonify(ok=True)
            amt = None
            if len(parts) >= 3:
                try: amt = float(parts[2])
                except: amt = None
            res = maker_sell_all_base(market, amt)
            if res.get("ok"):
                tg_send(f"✅ SELL مبدئيًا أُرسل (Maker) — {market}\n{json.dumps(res, ensure_ascii=False)}")
            else:
                tg_send(f"⚠️ SELL فشل: {res.get('err') or res.get('response',{}).get('error','')}\n"
                        f"| {json.dumps(res, ensure_ascii=False)}")
            return jsonify(ok=True)

        tg_send("أوامر: /buy /sell /bal")
        return jsonify(ok=True)

    except Exception as e:
        tg_send(f"🐞 خطأ: {e}")
        return jsonify(ok=True)

# ========= Health =========
@app.route("/", methods=["GET"])
def home():
    return "Saqer Maker Mini ✅"

# ========= Main =========
if __name__ == "__main__":
    load_markets_once()
    app.run(host="0.0.0.0", port=PORT)