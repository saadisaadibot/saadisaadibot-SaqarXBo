# -*- coding: utf-8 -*-
"""
Saqer — Maker-Only (Bitvavo / EUR) — 3 cmds: /buy /sell /cancel
- /buy COIN     → شراء Maker بكل اليورو المتاح (مع هامش) + مطاردة تلقائية للسعر حتى الامتلاء أو انتهاء المهلة.
- /sell COIN    → بيع Maker لكل الكمية المتاحة.
- /cancel       → إلغاء واحد يلغي كل أوامر الماركت النشط ويؤكد الإلغاء.

ملاحظات:
- صفقة/ماركت واحد نشط للشراء في نفس الوقت.
- تصحيح amountPrecision بدقة (Decimal ROUND_DOWN).
"""

import os, re, time, json, hmac, hashlib, math, requests, threading
from uuid import uuid4
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from decimal import Decimal, ROUND_DOWN, InvalidOperation

# ========= Boot / ENV =========
load_dotenv()
app = Flask(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID     = os.getenv("CHAT_ID", "").strip()  # اختياري لتقييد المحادثة
API_KEY     = os.getenv("BITVAVO_API_KEY", "").strip()
API_SECRET  = os.getenv("BITVAVO_API_SECRET", "").strip()
PORT        = int(os.getenv("PORT", "8080"))

BASE_URL    = "https://api.bitvavo.com/v2"

HEADROOM_EUR        = float(os.getenv("HEADROOM_EUR", "0.30"))   # اترك هامش بسيط من EUR
CANCEL_WAIT_SEC     = float(os.getenv("CANCEL_WAIT_SEC", "8.0")) # مهلة تأكيد الإلغاء
SHORT_FOLLOW_SEC    = float(os.getenv("SHORT_FOLLOW_SEC", "2.0"))
REPRICE_EVERY       = float(os.getenv("REPRICE_EVERY", "1.5"))   # فاصل المطاردة
PATIENCE_SEC        = float(os.getenv("PATIENCE_SEC", "120"))    # مهلة مطاردة إجمالية
BACKOFF_BALANCE     = float(os.getenv("BACKOFF_BALANCE", "0.97"))# تقليل المبلغ لو تأخر تحرير onHold

# كاش للماركتات
MARKET_MAP  = {}   # "GMX" -> "GMX-EUR"
MARKET_META = {}   # "GMX-EUR" -> {"priceSig","amountDecimals","minQuote","minBase"}

# تتبع أمر شراء نشط واحد
OPEN_ORDERS = {}   # market -> {"orderId": "...", "clientOrderId": "...", "amount_init": float}
ACTIVE_MARKET = None
LOCK = threading.Lock()

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
def _as_int(v, default=None):
    try:
        if isinstance(v, bool): return default
        if isinstance(v, int): return v
        s = str(v).strip()
        if s.isdigit(): return int(s)
        if "." in s and s.replace(".","",1).isdigit():
            f = float(s)
            if abs(f - int(f)) < 1e-9: return int(f)
    except Exception:
        pass
    return default

def load_markets_once():
    """نقرأ amountPrecision كعدد منازل عشرية مباشرة."""
    global MARKET_MAP, MARKET_META
    if MARKET_MAP and MARKET_META: return
    rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
    m, meta = {}, {}
    for r in rows:
        if r.get("quote") != "EUR": continue
        market = r.get("market"); base = (r.get("base") or "").upper()
        if not base or not market: continue

        priceSig = _as_int(r.get("pricePrecision"), 6) or 6
        amountDecimals = _as_int(r.get("amountPrecision"), None)
        if amountDecimals is None:
            step = r.get("amountStep") or r.get("amountStepSize") or r.get("step")
            if step:
                s = str(step)
                amountDecimals = len(s.split(".")[1].rstrip("0")) if isinstance(s, str) and "." in s else 0
            else:
                amountDecimals = 8

        meta[market] = {
            "priceSig":       int(priceSig),
            "amountDecimals": int(amountDecimals),
            "minQuote":       float(r.get("minOrderInQuoteAsset", 0) or 0.0),
            "minBase":        float(r.get("minOrderInBaseAsset",  0) or 0.0),
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

def fmt_amount(market: str, amount: float) -> str:
    dec = _amount_decimals(market)
    if dec < 0: dec = 0
    q = Decimal(10) ** -dec
    try:
        a = (Decimal(str(amount))).quantize(q, rounding=ROUND_DOWN)
    except (InvalidOperation, Exception):
        a = (Decimal(0)).quantize(q, rounding=ROUND_DOWN)
    return f"{a:.{dec}f}"

# ========= Simple helpers =========
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

def orders_open(market: str):
    lst = bv_request("GET", f"/ordersOpen?market={market}")
    return lst if isinstance(lst, list) else []

# ========= Place / Cancel =========
def place_limit_postonly(market: str, side: str, price: float, amount_num: float):
    body = {
        "market": market, "side": side, "orderType": "limit", "postOnly": True,
        "clientOrderId": str(uuid4()),
        "price": fmt_price_sig(market, price),
        "amount": fmt_amount(market, amount_num),
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

def cancel_all_market_blocking(market: str, wait_sec=CANCEL_WAIT_SEC):
    _ = bv_request("DELETE", f"/orders?market={market}")
    t0 = time.time()
    while time.time() - t0 < wait_sec:
        open_list = orders_open(market)
        if len(open_list) == 0:
            return True, {"msg": "cleared"}
        time.sleep(0.25)
    # رجّع الحالة مع اللائحة المتبقية
    return False, {"still_open": orders_open(market)}

# ========= BUY (with auto-chase) =========
def buy_open_and_chase(market: str):
    """يشغل في Thread: مطاردة تلقائية حتى الامتلاء أو انتهاء PATIENCE_SEC."""
    global OPEN_ORDERS, ACTIVE_MARKET
    start_ts = time.time()
    eur_avail = get_balance("EUR")
    spend = max(0.0, eur_avail - HEADROOM_EUR)
    if spend <= 0:
        tg_send(f"⚠️ BUY فشل — {market}\nno_eur (avail={eur_avail:.2f})")
        with LOCK:
            OPEN_ORDERS.pop(market, None)
            ACTIVE_MARKET = None
        return

    minq, minb = _min_quote(market), _min_base(market)
    if spend < minq:
        tg_send(f"⚠️ BUY فشل — {market}\nminQuote={minq:.4f} EUR (have {spend:.2f})")
        with LOCK:
            OPEN_ORDERS.pop(market, None); ACTIVE_MARKET = None
        return

    # نحسب amount أوليًا، لكن سنعدّل إذا لزم بسبب onHold
    bid, ask = get_best_bid_ask(market)
    price  = round_price_sig_down(min(bid, ask*(1-1e-6)), _price_sig(market))
    amount = float(spend) / float(price)
    amount_num = float(fmt_amount(market, amount))
    if amount_num < minb:
        tg_send(f"⚠️ BUY فشل — {market}\nminBase={minb}")
        with LOCK:
            OPEN_ORDERS.pop(market, None); ACTIVE_MARKET = None
        return

    # محاولة أولى
    body, resp = place_limit_postonly(market, "buy", price, amount_num)
    if (resp or {}).get("error"):
        tg_send(f"⚠️ BUY فشل — {market}\n{json.dumps({'request': body, 'response': resp}, ensure_ascii=False)}")
        with LOCK:
            OPEN_ORDERS.pop(market, None); ACTIVE_MARKET = None
        return

    with LOCK:
        OPEN_ORDERS[market] = {"orderId": resp.get("orderId"),
                               "clientOrderId": body.get("clientOrderId"),
                               "amount_init": amount_num}

    tg_send(f"✅ BUY مبدئيًا أُرسل (Maker) — {market}\n{json.dumps({'request': body, 'response': resp}, ensure_ascii=False)}")

    # حلقة مطاردة
    while time.time() - start_ts < PATIENCE_SEC:
        time.sleep(REPRICE_EVERY)

        # فحص الحالة سريعًا
        st = bv_request("GET", f"/order?market={market}&orderId={OPEN_ORDERS[market]['orderId']}")
        s = (st or {}).get("status", "").lower()
        if s in ("filled", "partiallyfilled"):
            if s == "filled":
                tg_send(f"✅ BUY امتلأ — {market}")
                with LOCK:
                    OPEN_ORDERS.pop(market, None); ACTIVE_MARKET = None
                return
            # لو partial، تابع المطاردة على الباقي (نلغي ونعيد تسعير)
        # 1) ألغِ الكل
        ok, info = cancel_all_market_blocking(market, CANCEL_WAIT_SEC)
        if not ok:
            # لو ما فضي، جرّب ثانية صغيرة ثم أكمل
            time.sleep(0.6)

        # 2) تأكّد من الأوامر المفتوحة فارغة
        if len(orders_open(market)) > 0:
            # ما قدر يفضي → حاول بالمرة الجاية
            continue

        # 3) قد يتحجز onHold لحظات: إقرأ الرصيد من جديد
        eur_avail2 = get_balance("EUR")
        spend2 = max(0.0, eur_avail2 - HEADROOM_EUR)
        if spend2 <= 0:
            # خفّض المبلغ قليلاً لتجاوز تأخّر تحرير onHold
            amount_num = amount_num * BACKOFF_BALANCE
        else:
            # احسب من جديد على السعر الحالي
            bid, ask = get_best_bid_ask(market)
            price = round_price_sig_down(min(bid, ask*(1-1e-6)), _price_sig(market))
            amount_num = float(fmt_amount(market, spend2 / price))
        if amount_num < minb:
            amount_num = float(fmt_amount(market, minb))

        # 4) أعد وضع الأمر عند أفضل Bid الحالي
        body2, resp2 = place_limit_postonly(market, "buy", price, amount_num)
        if (resp2 or {}).get("error"):
            # لو فشل بسبب الدقة/الرصيد، جرّب تقليل الكمية قليلاً
            amount_num = float(fmt_amount(market, amount_num * BACKOFF_BALANCE))
            body2, resp2 = place_limit_postonly(market, "buy", price, amount_num)
            if (resp2 or {}).get("error"):
                tg_send(f"⚠️ Reprice فشل — {market}\n{json.dumps({'request': body2, 'response': resp2}, ensure_ascii=False)}")
                continue

        with LOCK:
            OPEN_ORDERS[market] = {"orderId": resp2.get("orderId"),
                                   "clientOrderId": body2.get("clientOrderId"),
                                   "amount_init": amount_num}

    # انتهت المهلة
    tg_send(f"⏳ BUY لم يكتمل ضمن المهلة — {market}")
    # ألغِ في النهاية
    cancel_all_market_blocking(market, CANCEL_WAIT_SEC)
    with LOCK:
        OPEN_ORDERS.pop(market, None)
        ACTIVE_MARKET = None

# ========= SELL (simple maker sell) =========
def maker_sell(market: str):
    base = market.split("-")[0]
    # كل الرصيد المتاح
    bals = bv_request("GET","/balance")
    amt  = 0.0
    if isinstance(bals, list):
        for b in bals:
            if b.get("symbol")==base.upper():
                amt = float(b.get("available",0) or 0.0); break
    amt_num = float(fmt_amount(market, amt))
    if amt_num <= 0: return {"ok": False, "err": f"No {base} to sell"}

    minb = _min_base(market)
    if amt_num < minb: return {"ok": False, "err": f"minBase={minb}"}

    bid, ask = get_best_bid_ask(market)
    price = round_price_sig_down(max(ask, bid*(1+1e-6)), _price_sig(market))
    body, resp = place_limit_postonly(market, "sell", price, amt_num)
    if (resp or {}).get("error"):
        return {"ok": False, "request": body, "response": resp, "err": (resp or {}).get("error")}
    # متابعة قصيرة
    t0 = time.time(); oid = resp.get("orderId")
    while time.time()-t0 < SHORT_FOLLOW_SEC and oid:
        st = bv_request("GET", f"/order?market={market}&orderId={oid}")
        if (st or {}).get("status","").lower() in ("filled","partiallyfilled"): break
        time.sleep(0.25)
    return {"ok": True, "request": body, "response": resp}

# ========= CANCEL (أمر واحد يلغي الكل) =========
def cancel_all_for_active():
    global ACTIVE_MARKET
    with LOCK:
        market = ACTIVE_MARKET
    if not market:
        return {"ok": False, "err": "no_active_market"}
    ok, info = cancel_all_market_blocking(market, CANCEL_WAIT_SEC)
    if ok:
        with LOCK:
            OPEN_ORDERS.pop(market, None)
            ACTIVE_MARKET = None
        return {"ok": True, "msg": f"canceled_all_for_{market}"}
    return {"ok": False, "err": "not_cleared", "detail": info}

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

            with LOCK:
                global ACTIVE_MARKET
                if ACTIVE_MARKET and ACTIVE_MARKET != market:
                    tg_send(f"⛔ لديك ماركت نشط بالفعل: {ACTIVE_MARKET}. ألغِه أولًا بـ /cancel.")
                    return jsonify(ok=True)
                if market in OPEN_ORDERS:
                    tg_send(f"⛔ يوجد أمر مفتوح: {OPEN_ORDERS[market]}")
                    return jsonify(ok=True)
                ACTIVE_MARKET = market
                OPEN_ORDERS[market] = {"orderId": "", "clientOrderId": "", "amount_init": 0.0}

            # شغّل المطاردة في Thread
            th = threading.Thread(target=buy_open_and_chase, args=(market,), daemon=True)
            th.start()
            tg_send(f"🚀 بدأ الشراء والمطاردة — {market}")
            return jsonify(ok=True)

        if low.startswith("/sell"):
            parts = text.split()
            if len(parts)<2: tg_send("صيغة: /sell COIN"); return jsonify(ok=True)
            market=_norm_market(parts[1].upper())
            if not market: tg_send("⛔ عملة غير صالحة."); return jsonify(ok=True)
            res = maker_sell(market)
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
    return "Saqer Maker (BUY / SELL / CANCEL + Auto-Chase) ✅"

# ========= Main =========
if __name__ == "__main__":
    load_markets_once()
    app.run(host="0.0.0.0", port=PORT)