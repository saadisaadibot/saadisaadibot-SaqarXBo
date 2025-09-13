# -*- coding: utf-8 -*-
"""
Saqer — Maker-Only (Bitvavo / EUR) — Telegram /tg (Arabic-only, fixed decimals)
الأوامر:
- "اشتري COIN [EUR]"
- "بيع COIN [AMOUNT]"
- "الغ COIN"
"""

import os, re, time, json, hmac, hashlib, math, requests
from uuid import uuid4
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from decimal import Decimal, ROUND_DOWN, getcontext

# ========= Boot / ENV =========
load_dotenv()
app = Flask(__name__)
getcontext().prec = 28  # أمان حسابي (Decimal)

BOT_TOKEN   = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID     = os.getenv("CHAT_ID", "").strip()
API_KEY     = os.getenv("BITVAVO_API_KEY", "").strip()
API_SECRET  = os.getenv("BITVAVO_API_SECRET", "").strip()
PORT        = int(os.getenv("PORT", "8080"))

BASE_URL    = "https://api.bitvavo.com/v2"

HEADROOM_EUR        = float(os.getenv("HEADROOM_EUR", "0.30"))
CANCEL_WAIT_SEC     = float(os.getenv("CANCEL_WAIT_SEC", "8.0"))
SHORT_FOLLOW_SEC    = float(os.getenv("SHORT_FOLLOW_SEC", "2.0"))

# كاش الماركت + تتبع
MARKET_MAP  = {}   # "ADA" → "ADA-EUR"
MARKET_META = {}   # meta per market
OPEN_ORDERS = {}   # market -> {"orderId": "...", "clientOrderId": "...", "amount_init": float}

# ========= Telegram =========
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

# ========= Bitvavo =========
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

# --- helpers for precisions ---
def _infer_decimals(value) -> int:
    """
    يحوّل pricePrecision/amountPrecision التي قد تأتي كعدد منازل (مثل 8)
    أو كخطوة عشرية (مثل 0.01) إلى عدد منازل صحيح.
    """
    try:
        iv = int(value)
        if iv >= 0 and float(value) == iv:
            return iv
    except Exception:
        pass
    s = str(value)
    if "." in s:
        frac = s.split(".", 1)[1].rstrip("0")
        return max(0, len(frac))
    return 8  # fallback آمن

def load_markets_once():
    """يجلب /markets مرة واحدة ويثبت دقّات السعر/الكمية والحدود الدنيا."""
    global MARKET_MAP, MARKET_META
    if MARKET_MAP and MARKET_META:
        return
    rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
    m, meta = {}, {}
    for r in rows:
        if r.get("quote") != "EUR":
            continue
        market = r.get("market")
        base   = (r.get("base") or "").upper()
        if not base or not market:
            continue

        price_dec = _infer_decimals(r.get("pricePrecision", 6))
        amt_dec   = _infer_decimals(r.get("amountPrecision", 8))
        step      = float(Decimal(1) / (Decimal(10) ** amt_dec))

        meta[market] = {
            "priceDecimals": price_dec,
            "amountDecimals": amt_dec,
            "step": step,
            "minQuote": float(r.get("minOrderInQuoteAsset", 0) or 0.0),
            "minBase":  float(r.get("minOrderInBaseAsset",  0) or 0.0),
        }
        m[base] = market

    MARKET_MAP, MARKET_META = m, meta

def coin_to_market(coin: str) -> str | None:
    load_markets_once(); return MARKET_MAP.get((coin or "").upper())

def _meta(market: str) -> dict: load_markets_once(); return MARKET_META.get(market, {})
def _price_decimals(market: str) -> int: return int(_meta(market).get("priceDecimals", 6))
def _amount_decimals(market: str) -> int: return int(_meta(market).get("amountDecimals", 8))
def _step(market: str) -> float: return float(_meta(market).get("step", 1e-8))
def _min_quote(market: str) -> float: return float(_meta(market).get("minQuote", 0.0))
def _min_base(market: str) -> float:  return float(_meta(market).get("minBase", 0.0))

def fmt_price_dec(market: str, price: float | Decimal) -> str:
    """تنسيق السعر على عدد منازل السعر المسموح (ROUND_DOWN)."""
    decs = _price_decimals(market)
    q = Decimal(10) ** -decs
    p = (Decimal(str(price))).quantize(q, rounding=ROUND_DOWN)
    s = f"{p:f}"
    if "." in s:
        whole, frac = s.split(".", 1)
        frac = frac[:decs].rstrip("0")
        return whole if not frac else f"{whole}.{frac}"
    return s

def round_amount_down(market: str, amount: float | Decimal) -> float:
    """قصّ الكمية على amountDecimals ثم على step (احتياط)."""
    decs = _amount_decimals(market)
    q = Decimal(10) ** -decs
    a = (Decimal(str(amount))).quantize(q, rounding=ROUND_DOWN)
    st = Decimal(str(_step(market)))
    if st > 0:
        a = (a // st) * st
    return float(a)

def fmt_amount(market: str, amount: float | Decimal) -> str:
    decs = _amount_decimals(market)
    q = Decimal(10) ** -decs
    a = (Decimal(str(amount))).quantize(q, rounding=ROUND_DOWN)
    s = f"{a:f}"
    if "." in s:
        whole, frac = s.split(".", 1)
        frac = frac[:decs].rstrip("0")
        return whole if not frac else f"{whole}.{frac}"
    return s

def get_balance(symbol: str) -> float:
    bals = bv_request("GET", "/balance")
    if isinstance(bals, list):
        for b in bals:
            if b.get("symbol") == symbol.upper():
                return float(b.get("available", 0) or 0.0)
    return 0.0

def get_best_bid_ask(market: str) -> tuple[float, float]:
    ob = requests.get(f"{BASE_URL}/{market}/book?depth=1", timeout=8).json()
    try:
        bid = float(ob["bids"][0][0]); ask = float(ob["asks"][0][0])
    except Exception:
        raise RuntimeError(f"Orderbook empty for {market}")
    return bid, ask

# ========= Poll helper =========
def _poll_status_until(market: str, orderId: str, deadline_ts: float,
                       initial_sleep=0.20, max_sleep=1.2):
    last = None
    time.sleep(max(0.0, initial_sleep))
    sleep = 0.25
    while time.time() < deadline_ts:
        st = bv_request("GET", f"/order?market={market}&orderId={orderId}")
        last = st if isinstance(st, dict) else None
        s = (st or {}).get("status", "").lower()
        if s in ("canceled", "filled"):
            return True, s, last
        time.sleep(sleep)
        sleep = min(max_sleep, sleep * 1.5)
    st = bv_request("GET", f"/order?market={market}&orderId={orderId}")
    last = st if isinstance(st, dict) else last
    s = (st or {}).get("status", "").lower()
    return (s in ("canceled", "filled")), s or "unknown", last

# ========= Cancel (strict) =========
def cancel_order_blocking(market: str, orderId: str, clientOrderId: str | None = None,
                          wait_sec=CANCEL_WAIT_SEC):
    deadline = time.time() + wait_sec
    for _ in range(2):
        _ = bv_request("DELETE", f"/order?market={market}&orderId={orderId}")
        ok, st, last = _poll_status_until(market, orderId, deadline)
        if ok: return True, st, last
        if st not in ("new", "open", "awaitingmarket", ""):
            return False, st or "unknown", last
    if clientOrderId and time.time() < deadline:
        _ = bv_request("DELETE", f"/order?market={market}&clientOrderId={clientOrderId}")
        ok2, st2, last2 = _poll_status_until(market, orderId, deadline)
        if ok2: return True, st2, last2
        if st2 not in ("new", "open", "awaitingmarket", ""):
            return False, st2 or "unknown", last2
    _ = bv_request("DELETE", f"/orders?market={market}")
    ok3, st3, last3 = _poll_status_until(market, orderId, time.time() + 4.0)
    if ok3: return True, st3, last3
    return False, st3 or "unknown", last3

# ========= Place Maker =========
def place_limit_postonly(market: str, side: str, price: float, amount: float):
    body = {
        "market": market, "side": side, "orderType": "limit", "postOnly": True,
        "clientOrderId": str(uuid4()),
        "price": fmt_price_dec(market, price),
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

# ========= BUY / SELL =========
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
    try:
        bid, ask = get_best_bid_ask(market)
    except RuntimeError as e:
        return {"ok": False, "err": str(e)}
    price  = min(bid, ask*(1-1e-6))
    amount = round_amount_down(market, spend/price)
    if amount < minb:
        return {"ok": False, "err": f"minBase={minb}"}
    body, resp = place_limit_postonly(market, "buy", price, amount)
    if (resp or {}).get("error"):
        return {"ok": False, "request": body, "response": resp, "err": (resp or {}).get("error")}
    oid  = resp.get("orderId")
    coid = body.get("clientOrderId")
    OPEN_ORDERS[market] = {"orderId": oid, "clientOrderId": coid, "amount_init": amount}
    return {"ok": True, "request": body, "response": resp, "open": OPEN_ORDERS[market]}

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
    try:
        bid, ask = get_best_bid_ask(market)
    except RuntimeError as e:
        return {"ok": False, "err": str(e)}
    price = max(ask, bid*(1+1e-6))
    body, resp = place_limit_postonly(market, "sell", price, amt)
    if (resp or {}).get("error"):
        return {"ok": False, "request": body, "response": resp, "err": (resp or {}).get("error")}
    # متابعة قصيرة لالتقاط partial fill
    t0 = time.time(); oid = resp.get("orderId")
    while time.time()-t0 < SHORT_FOLLOW_SEC and oid:
        st = bv_request("GET", f"/order?market={market}&orderId={oid}")
        if (st or {}).get("status","").lower() in ("filled","partiallyfilled"):
            break
        time.sleep(0.25)
    return {"ok": True, "request": body, "response": resp}

# ========= Parsing (Arabic) =========
COIN_RE = re.compile(r"^[A-Z0-9]{2,15}$")
def _norm_market_from_text(arg: str) -> str | None:
    s = (arg or "").strip().upper()
    if not s: return None
    if s.endswith("-EUR") and COIN_RE.match(s.split("-")[0]): return s
    if COIN_RE.match(s): return f"{s}-EUR"
    return None

# ========= Telegram Webhook (Arabic-only على /tg) =========
@app.route("/tg", methods=["GET"])
def tg_alive():
    return "OK /tg", 200

@app.route("/tg", methods=["POST"])
def tg_webhook():
    upd = request.get_json(silent=True) or {}
    msg  = upd.get("message") or upd.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    text = (msg.get("text") or "").strip()
    if not chat_id or not _auth_chat(chat_id) or not text:
        return jsonify(ok=True)

    low = text.lower()
    try:
        # ----- اشتري -----
        if low.startswith("اشتري"):
            parts = text.split()
            if len(parts) < 2:
                tg_send("صيغة: اشتري COIN [EUR]"); return jsonify(ok=True)
            market = _norm_market_from_text(parts[1])
            if not market: tg_send("⛔ عملة غير صالحة."); return jsonify(ok=True)
            eur = None
            if len(parts) >= 3:
                try: eur = float(parts[2])
                except: eur = None
            res = buy_open(market, eur)
            tg_send(("✅ تم فتح أمر شراء" if res.get("ok") else "⚠️ فشل الشراء") + f" — {market}\n"
                    f"{json.dumps(res, ensure_ascii=False)}")
            return jsonify(ok=True)

        # ----- بيع -----
        if low.startswith("بيع"):
            parts = text.split()
            if len(parts) < 2:
                tg_send("صيغة: بيع COIN [AMOUNT]"); return jsonify(ok=True)
            market = _norm_market_from_text(parts[1])
            if not market: tg_send("⛔ عملة غير صالحة."); return jsonify(ok=True)
            amt = None
            if len(parts) >= 3:
                try: amt = float(parts[2])
                except: amt = None
            res = maker_sell(market, amt)
            tg_send(("✅ أُرسل أمر بيع" if res.get("ok") else "⚠️ فشل البيع") + f" — {market}\n"
                    f"{json.dumps(res, ensure_ascii=False)}")
            return jsonify(ok=True)

        # ----- الغ (Cancel) -----
        if low.startswith("الغ"):
            parts = text.split()
            if len(parts) < 2:
                tg_send("صيغة: الغ COIN"); return jsonify(ok=True)
            market = _norm_market_from_text(parts[1])
            if not market: tg_send("⛔ عملة غير صالحة."); return jsonify(ok=True)
            info = OPEN_ORDERS.get(market)
            if not info: tg_send("لا يوجد أمر مفتوح لهذه العملة."); return jsonify(ok=True)
            ok, final, last = cancel_order_blocking(market, info["orderId"], info.get("clientOrderId"), CANCEL_WAIT_SEC)
            if ok:
                OPEN_ORDERS.pop(market, None)
                tg_send(f"✅ تم الإلغاء — status={final}\n{json.dumps(last, ensure_ascii=False)}")
            else:
                tg_send(f"⚠️ فشل الإلغاء — status={final}\n{json.dumps(last, ensure_ascii=False)}")
            return jsonify(ok=True)

        # لا أوامر أخرى
        tg_send("الأوامر: «اشتري COIN [EUR]» ، «بيع COIN [AMOUNT]» ، «الغ COIN»")
        return jsonify(ok=True)

    except Exception as e:
        tg_send(f"🐞 خطأ: {e}")
        return jsonify(ok=True)

# ========= Health =========
@app.route("/", methods=["GET"])
def home():
    return "Saqer Maker — Arabic-only on /tg (fixed decimals) ✅"

# ========= Main =========
if __name__ == "__main__":
    load_markets_once()
    app.run(host="0.0.0.0", port=PORT)