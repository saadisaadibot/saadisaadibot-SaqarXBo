# -*- coding: utf-8 -*-
"""
Saqer — Maker-Only (Bitvavo / EUR) — Telegram (BUY / SELL / CANCEL) + Auto-Chase + Strict Cancel
- /buy COIN      → يفتح شراء Maker بكل الرصيد المتاح (مع هامش) ويطارد السعر تلقائياً
- /sell COIN     → يبيع Maker كل الرصيد من العملة
- /cancel        → يلغي كل أوامر الماركت النشط ويتحقق حتى تزول فعلاً

ملاحظات:
- مطاردة السعر: تعيد التسعير كل ثانية تقريباً إلى أفضل Bid (مع ضبط دقة السعر)، حتى الامتلاء أو انتهاء المهلة.
- إلغاء واحد فقط: يرسل DELETE /orders?market=... ثم يفحص /ordersOpen و/أو حالة orderId للتأكد.
- معالجة دقيقة لمنازل كمية الـ amount (amountPrecision) لتفادي خطأ "too many decimal digits".
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

HEADROOM_EUR        = float(os.getenv("HEADROOM_EUR", "0.30"))  # اترك هامش بسيط للرسوم
CANCEL_WAIT_SEC     = float(os.getenv("CANCEL_WAIT_SEC", "12.0"))
SHORT_FOLLOW_SEC    = float(os.getenv("SHORT_FOLLOW_SEC", "2.0"))

# مطاردة السعر
CHASE_REPRICE_EVERY = float(os.getenv("CHASE_REPRICE_EVERY", "1.0"))
CHASE_TIMEOUT_SEC   = float(os.getenv("CHASE_TIMEOUT_SEC", "90.0"))

# ========= Markets Cache =========
MARKET_MAP  = {}   # "GMX" -> "GMX-EUR"
MARKET_META = {}   # "GMX-EUR" -> {"priceSig":6,"amountDecimals":8,"minQuote":5.0,"minBase":0.0001}

# ========= Open Orders (واحدة فعلياً) =========
OPEN_ORDERS = {}    # market -> {"orderId","clientOrderId","amount_init"}
CHASE_THREADS = {}  # market -> thread

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
        # pricePrecision = significant digits للسعر
        priceSig = int(r.get("pricePrecision", 6) or 6)
        # amountPrecision = عدد المنازل العشرية للكمية (هنا المفتاح لمعالجة خطأ PUMP)
        ap = r.get("amountPrecision", 8)
        amountDecimals = int(ap) if isinstance(ap, int) else 8
        meta[market] = {
            "priceSig": priceSig,
            "amountDecimals": amountDecimals,
            "minQuote": float(r.get("minOrderInQuoteAsset", 0) or 0.0),
            "minBase":  float(r.get("minOrderInBaseAsset",  0) or 0.0),
        }
        m[base] = market
    MARKET_MAP, MARKET_META = m, meta

def coin_to_market(coin: str) -> str | None:
    load_markets_once(); return MARKET_MAP.get(coin.upper())

def _meta(market: str) -> dict: load_markets_once(); return MARKET_META.get(market, {})
def _price_sig(market: str) -> int: return int(_meta(market).get("priceSig", 6))
def _amount_decimals(market: str) -> int: return int(_meta(market).get("amountDecimals", 8))
def _min_quote(market: str) -> float: return float(_meta(market).get("minQuote", 0.0))
def _min_base(market: str) -> float:  return float(_meta(market).get("minBase", 0.0))

# ========= Formatting / Rounding =========
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

def fmt_amount(market: str, amount: float) -> str:
    dec = _amount_decimals(market)
    if dec < 0: dec = 0
    # نقطع لأسفل بدقة dec
    factor = 10 ** dec
    a = math.floor(float(amount) * factor) / factor
    return f"{a:.{dec}f}"

# ========= Helpers =========
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

# ========= Strict Cancel =========
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

def strict_cancel_all(market: str, known_order_id: str | None, wait_sec=CANCEL_WAIT_SEC):
    # أرسل إلغاء شامل للماركت
    _ = bv_request("DELETE", f"/orders?market={market}")
    deadline = time.time() + wait_sec

    # إن كنا نعرف orderId، راقبه حتى يصبح canceled/filled
    if known_order_id:
        ok, st, last = _poll_status_until(market, known_order_id, deadline)
        if ok:
            return True, st, last

    # تحقق من /ordersOpen حتى تُفرغ
    while time.time() < deadline:
        open_list = bv_request("GET", f"/ordersOpen?market={market}")
        if isinstance(open_list, list) and len(open_list) == 0:
            return True, "canceled", {"ordersOpen": []}
        time.sleep(0.3)

    # محاولة أخيرة سريعة
    open_list = bv_request("GET", f"/ordersOpen?market={market}")
    return (isinstance(open_list, list) and len(open_list) == 0), "unknown", {"ordersOpen": open_list}

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

    bid, ask = get_best_bid_ask(market)
    # افضل سعر للشراء maker: عند أفضل bid أو أقل بقليل من ask (مع بريدج بسيط)
    price  = round_price_sig_down(min(bid, ask*(1 - 1e-6)), _price_sig(market))
    amount = spend / price
    # صياغة الكمية بدقة amountPrecision
    amt_str = fmt_amount(market, amount)
    amount  = float(amt_str)

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
    if amt <= 0: return {"ok": False, "err": f"No {base} to sell"}

    # صياغة بدقة amountPrecision
    amt_str = fmt_amount(market, amt)
    amt     = float(amt_str)

    minb = _min_base(market)
    if amt < minb: return {"ok": False, "err": f"minBase={minb}"}

    bid, ask = get_best_bid_ask(market)
    price = round_price_sig_down(max(ask, bid*(1+1e-6)), _price_sig(market))

    body, resp = place_limit_postonly(market, "sell", price, amt)
    if (resp or {}).get("error"):
        return {"ok": False, "request": body, "response": resp, "err": (resp or {}).get("error")}

    # متابعة قصيرة
    t0 = time.time(); oid = resp.get("orderId")
    while time.time()-t0 < SHORT_FOLLOW_SEC and oid:
        st = bv_request("GET", f"/order?market={market}&orderId={oid}")
        if (st or {}).get("status","").lower() in ("filled","partiallyfilled"): break
        time.sleep(0.25)
    return {"ok": True, "request": body, "response": resp}

# ========= Auto Chase =========
def chase_buy_until_filled(market: str):
    """يطارد السعر للأمر المفتوح على هذا الماركت حتى الامتلاء أو انتهاء المهلة."""
    info = OPEN_ORDERS.get(market)
    if not info: return
    start = time.time()

    while time.time() - start < CHASE_TIMEOUT_SEC:
        oid = (OPEN_ORDERS.get(market) or {}).get("orderId")
        if not oid: return

        # حالة الطلب الحالية
        st = bv_request("GET", f"/order?market={market}&orderId={oid}")
        s = (st or {}).get("status","").lower()
        if s == "filled":
            OPEN_ORDERS.pop(market, None)
            tg_send(f"✅ تم تنفيذ الشراء — {market}")
            return

        # كمية متبقية
        try:
            amt_rem = float((st or {}).get("amountRemaining", st.get("amount","0")) or 0.0)
        except Exception:
            amt_rem = (OPEN_ORDERS.get(market) or {}).get("amount_init", 0.0)
        if amt_rem <= 0:
            amt_rem = (OPEN_ORDERS.get(market) or {}).get("amount_init", 0.0)

        # إعادة تسعير: ألغِ الكل ثم ضع أمر جديد عند أفضل Bid الحالي
        ok, final, _ = strict_cancel_all(market, oid, wait_sec=CANCEL_WAIT_SEC)
        if final == "filled":
            OPEN_ORDERS.pop(market, None)
            tg_send(f"✅ تم تنفيذ الشراء أثناء الإلغاء — {market}")
            return
        if not ok and final not in ("canceled", "filled"):
            # جرّب جولة أخرى بعد مهلة صغيرة
            time.sleep(0.4)
            continue

        bid, ask = get_best_bid_ask(market)
        new_price = round_price_sig_down(min(bid, ask*(1-1e-6)), _price_sig(market))
        # صياغة amount المتبقية بالدقة الصحيحة
        amt_str = fmt_amount(market, max(amt_rem, _min_base(market)))
        amt_rem = float(amt_str)

        body, resp = place_limit_postonly(market, "buy", new_price, amt_rem)
        if (resp or {}).get("error"):
            tg_send(f"⚠️ Reprice فشل — {market}\n{json.dumps({'request':body,'response':resp}, ensure_ascii=False)}")
            time.sleep(CHASE_REPRICE_EVERY)
            continue

        # خزّن الـ orderId الجديد
        OPEN_ORDERS[market] = {"orderId": resp.get("orderId"), "clientOrderId": body.get("clientOrderId"), "amount_init": amt_rem}

        # انتظر قليلًا قبل الجولة التالية
        time.sleep(CHASE_REPRICE_EVERY)

    tg_send(f"⚠️ انتهت مهلة المطاردة بدون امتلاء — {market}")

# ========= CANCEL (أمر واحد) =========
def cancel_all_for_active():
    if not OPEN_ORDERS:
        return {"ok": False, "err": "no_open_order"}
    market, info = next(iter(OPEN_ORDERS.items()))
    ok, st, last = strict_cancel_all(market, info.get("orderId"), wait_sec=CANCEL_WAIT_SEC)
    if ok:
        OPEN_ORDERS.pop(market, None)
        return {"ok": True, "status": st, "state": last}
    return {"ok": False, "err": f"not_cleared (status={st})", "state": last}

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

            res = buy_open(market, eur_amount=None)  # كل الرصيد (مع هامش)
            tg_send(("✅ BUY أُرسل" if res.get("ok") else "⚠️ BUY فشل") + f" — {market}\n{json.dumps(res, ensure_ascii=False)}")

            # إن نجح الفتح، شغّل مطاردة تلقائيًا
            if res.get("ok") and market not in CHASE_THREADS:
                th = threading.Thread(target=chase_buy_until_filled, args=(market,), daemon=True)
                CHASE_THREADS[market] = th
                th.start()
            return jsonify(ok=True)

        if low.startswith("/sell"):
            parts = text.split()
            if len(parts)<2: tg_send("صيغة: /sell COIN"); return jsonify(ok=True)
            market=_norm_market(parts[1].upper())
            if not market: tg_send("⛔ عملة غير صالحة."); return jsonify(ok=True)
            res = maker_sell(market, amount=None)
            tg_send(("✅ SELL أُرسل" if res.get("ok") else "⚠️ SELL فشل") + f" — {market}\n{json.dumps(res, ensure_ascii=False)}")
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
    return "Saqer Maker (BUY / SELL / CANCEL + Auto-Chase + Strict Cancel) ✅"

# ========= Main =========
if __name__ == "__main__":
    load_markets_once()
    app.run(host="0.0.0.0", port=PORT)