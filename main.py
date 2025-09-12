# -*- coding: utf-8 -*-
"""
Saqer — Maker-Only (Bitvavo / EUR)
أوامر تيليجرام: /buy COIN — /sell COIN — /cancel
+ مطاردة تلقائيّة: تلغي وتعيد تسعير أمر الشراء Maker ليلحق أفضل bid بدقّة.
"""

import os, re, time, json, hmac, hashlib, math, threading, requests
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

HEADROOM_EUR        = float(os.getenv("HEADROOM_EUR", "0.30"))
CANCEL_WAIT_SEC     = float(os.getenv("CANCEL_WAIT_SEC", "8.0"))
SHORT_FOLLOW_SEC    = float(os.getenv("SHORT_FOLLOW_SEC", "2.0"))

# مطاردة تلقائيّة
CHASE_ENABLE        = os.getenv("CHASE_ENABLE", "true").lower() == "true"
CHASE_INTERVAL      = float(os.getenv("CHASE_INTERVAL", "1.0"))      # كل كم ثانية نفحص/نطارد
CHASE_SPREAD_BPS    = float(os.getenv("CHASE_SPREAD_BPS", "1"))      # هامش أمان تحت الـ ask (bps)

# كاش الماركتات
MARKET_MAP  = {}   # "GMX" -> "GMX-EUR"
MARKET_META = {}   # "GMX-EUR" -> {priceSig, step, minQuote, minBase}

# أمر واحد نشط فقط
OPEN_ORDERS = {}   # market -> {"orderId","clientOrderId","amount_init","last_price","last_action"}

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

# ========= Bitvavo REST =========
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

# ========= Markets / Meta =========
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

def _meta(market: str) -> dict: load_markets_once(); return MARKET_META.get(market, {})
def _price_sig(market: str) -> int: return int(_meta(market).get("priceSig", 6))
def _step(market: str) -> float:     return float(_meta(market).get("step", 1e-8))
def _min_quote(market: str) -> float:return float(_meta(market).get("minQuote", 0.0))
def _min_base(market: str) -> float: return float(_meta(market).get("minBase", 0.0))

# ========= Rounding / Formatting =========
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

# ========= Order Book / Balance =========
def get_best_bid_ask(market: str) -> tuple[float, float]:
    ob = requests.get(f"{BASE_URL}/{market}/book?depth=1", timeout=8).json()
    bid = float(ob["bids"][0][0]); ask = float(ob["asks"][0][0])
    return bid, ask

def get_balance(symbol: str) -> float:
    bals = bv_request("GET", "/balance")
    if isinstance(bals, list):
        for b in bals:
            if b.get("symbol") == symbol.upper():
                return float(b.get("available", 0) or 0.0)
    return 0.0

# ========= Place Maker =========
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

# ========= CANCEL صارم لماركت واحد =========
def cancel_market_all(market: str, wait_sec: float = CANCEL_WAIT_SEC):
    """يحذف كل أوامر الماركت وينتظر حتى تُفرَّغ قائمة ordersOpen."""
    _ = bv_request("DELETE", f"/orders?market={market}")
    deadline = time.time() + wait_sec
    last = None
    while time.time() < deadline:
        open_list = bv_request("GET", f"/ordersOpen?market={market}")
        last = open_list
        if isinstance(open_list, list) and len(open_list) == 0:
            return True, {"msg":"cleared"}
        time.sleep(0.25)
    return False, {"err":"not_cleared", "still_open": last}

# ========= BUY / SELL (نفس المنطق القديم) =========
def buy_open(market: str, eur_amount: float | None):
    if market in OPEN_ORDERS:
        return {"ok": False, "err": "order_already_open", "open": OPEN_ORDERS[market]}

    eur_avail = get_balance("EUR")
    spend = float(eur_avail) if (eur_amount is None or eur_amount <= 0) else float(eur_amount)
    spend = max(0.0, spend - HEADROOM_EUR)
    if spend <= 0:
        return {"ok": False, "err": f"no_eur (avail={eur_avail:.2f})"}

    minq, minb = _min_quote(market), _min_base(market)
    if spend < minq:
        return {"ok": False, "err": f"minQuote={minq:.4f} EUR, have {spend:.2f}"}

    bid, ask = get_best_bid_ask(market)
    # هدف الشراء: تحت الـ ask بشعرة (bps) ومع ذلك ≤ bid لضمان PostOnly
    ask_pad = ask * (1 - CHASE_SPREAD_BPS/10000.0)
    target  = min(bid, ask_pad)
    price   = round_price_sig_down(target, _price_sig(market))
    amount  = round_amount_down(market, spend/price)
    if amount < minb:
        return {"ok": False, "err": f"minBase={minb}"}

    body, resp = place_limit_postonly(market, "buy", price, amount)
    if (resp or {}).get("error"):
        return {"ok": False, "request": body, "response": resp, "err": (resp or {}).get("error")}

    oid  = resp.get("orderId")
    coid = body.get("clientOrderId")
    OPEN_ORDERS[market] = {"orderId": oid, "clientOrderId": coid, "amount_init": amount,
                           "last_price": float(body["price"]), "last_action": time.time()}
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

    bid, ask = get_best_bid_ask(market)
    # للبيع: فوق الـ bid بشعرة لضمان PostOnly
    bid_pad = bid * (1 + CHASE_SPREAD_BPS/10000.0)
    price   = round_price_sig_down(max(ask, bid_pad), _price_sig(market))
    body, resp = place_limit_postonly(market, "sell", price, amt)
    if (resp or {}).get("error"):
        return {"ok": False, "request": body, "response": resp, "err": (resp or {}).get("error")}
    t0 = time.time(); oid = resp.get("orderId")
    while time.time()-t0 < SHORT_FOLLOW_SEC and oid:
        st = bv_request("GET", f"/order?market={market}&orderId={oid}")
        if (st or {}).get("status","").lower() in ("filled","partiallyfilled"): break
        time.sleep(0.25)
    return {"ok": True, "request": body, "response": resp}

# ========= CANCEL (أمر واحد) =========
def cancel_all_for_active():
    if not OPEN_ORDERS:
        return {"ok": False, "err": "no_open_order"}
    market = next(iter(OPEN_ORDERS.keys()))
    ok, info = cancel_market_all(market, CANCEL_WAIT_SEC)
    if ok:
        OPEN_ORDERS.pop(market, None)
        return {"ok": True, "msg": f"canceled_all_for_{market}"}
    return {"ok": False, **info}

# ========= مطاردة تلقائيّة =========
def _need_reprice(market: str, current_state: dict) -> tuple[bool, float]:
    """يرجع (need, new_price)."""
    bid, ask = get_best_bid_ask(market)
    ask_pad = ask * (1 - CHASE_SPREAD_BPS/10000.0)
    target  = min(bid, ask_pad)
    new_p   = round_price_sig_down(target, _price_sig(market))

    try:
        cur_p = float(current_state.get("price", "0") or 0.0)
    except Exception:
        cur_p = 0.0
    # إذا تغيّر السعر الفعلي (بعد التقريب) — نعيد التسعير
    return (fmt_price_sig(market, new_p) != fmt_price_sig(market, cur_p)), new_p

def _remaining_amount(info: dict, state: dict, market: str) -> float:
    try:
        rem = float((state or {}).get("amountRemaining", state.get("amount","0")) or 0.0)
    except Exception:
        rem = info.get("amount_init", 0.0)
    if rem <= 0: rem = info.get("amount_init", 0.0)
    rem = max(rem, _min_base(market))
    return round_amount_down(market, rem)

def chase_worker():
    while True:
        try:
            if CHASE_ENABLE and OPEN_ORDERS:
                # صفقة واحدة فقط
                market, info = next(iter(OPEN_ORDERS.items()))
                oid = info.get("orderId")

                # حالة الطلب الحالية
                st = bv_request("GET", f"/order?market={market}&orderId={oid}")
                status = (st or {}).get("status","").lower()

                if status == "filled":
                    OPEN_ORDERS.pop(market, None)
                else:
                    need, new_price = _need_reprice(market, st or {})
                    if need:
                        # إلغاء كل الأوامر ثم وضع أمر جديد بنفس الكمية المتبقية
                        ok, _ = cancel_market_all(market, CANCEL_WAIT_SEC)
                        if ok:
                            amt = _remaining_amount(info, st or {}, market)
                            body, resp = place_limit_postonly(market, "buy", new_price, amt)
                            if not (resp or {}).get("error"):
                                OPEN_ORDERS[market] = {
                                    "orderId": resp.get("orderId"),
                                    "clientOrderId": body.get("clientOrderId"),
                                    "amount_init": amt,
                                    "last_price": float(body["price"]),
                                    "last_action": time.time()
                                }
                        # إن لم ينجح الإلغاء نبقي الوضع كما هو وسنحاول في الدورة القادمة
            time.sleep(CHASE_INTERVAL)
        except Exception as e:
            print("chase_worker err:", e)
            time.sleep(1.0)

# ========= Helpers =========
COIN_RE = re.compile(r"^[A-Z0-9]{2,15}$")
def _norm_market(arg: str) -> str | None:
    s = (arg or "").strip().upper()
    if not s: return None
    if s.endswith("-EUR") and COIN_RE.match(s.split("-")[0]): return s
    if COIN_RE.match(s): return f"{s}-EUR"
    return None

# ========= Telegram Webhook (3 أوامر فقط) =========
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
            if market in OPEN_ORDERS:
                tg_send(f"⛔ يوجد أمر شراء مفتوح: {OPEN_ORDERS[market]}"); return jsonify(ok=True)
            res = buy_open(market, eur_amount=None)
            tg_send(("✅ BUY تم الإرسال" if res.get("ok") else "⚠️ BUY فشل") + f" — {market}\n"
                    f"{json.dumps(res, ensure_ascii=False)}")
            return jsonify(ok=True)

        if low.startswith("/sell"):
            parts = text.split()
            if len(parts)<2: tg_send("صيغة: /sell COIN"); return jsonify(ok=True)
            market=_norm_market(parts[1].upper())
            if not market: tg_send("⛔ عملة غير صالحة."); return jsonify(ok=True)
            res = maker_sell(market, amount=None)
            tg_send(("✅ SELL تم الإرسال" if res.get("ok") else "⚠️ SELL فشل") + f" — {market}\n"
                    f"{json.dumps(res, ensure_ascii=False)}")
            return jsonify(ok=True)

        if low.startswith("/cancel"):
            res = cancel_all_for_active()
            tg_send(("✅ Cancel تم" if res.get("ok") else "⚠️ Cancel فشل") + f"\n{json.dumps(res, ensure_ascii=False)}")
            return jsonify(ok=True)

        tg_send("الأوامر: /buy COIN — /sell COIN — /cancel")
        return jsonify(ok=True)

    except Exception as e:
        tg_send(f"🐞 خطأ: {e}")
        return jsonify(ok=True)

# ========= Health =========
@app.route("/", methods=["GET"])
def home():
    return "Saqer Maker (BUY/SELL/CANCEL + Auto-Chase) ✅"

# ========= Main =========
if __name__ == "__main__":
    load_markets_once()
    if CHASE_ENABLE:
        t = threading.Thread(target=chase_worker, daemon=True)
        t.start()
    app.run(host="0.0.0.0", port=PORT)