# -*- coding: utf-8 -*-
"""
Saqer — Maker-Only (Bitvavo / EUR) — Telegram /tg (Arabic-only, hardened + smart-precision)
الأوامر:
- "اشتري COIN [EUR]"
- "بيع COIN [AMOUNT]"
- "الغ COIN"
- "اشتري+ COIN [EUR]"       ← جديد: مطاردة + TP ذكي (5 سنت افتراضيًا)
- "ربح VALUE"               ← جديد: ضبط الربح المطلق باليورو (مثال: ربح 0.07)
"""

import os, re, time, json, hmac, hashlib, requests
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
CANCEL_WAIT_SEC     = float(os.getenv("CANCEL_WAIT_SEC", "12.0"))
SHORT_FOLLOW_SEC    = float(os.getenv("SHORT_FOLLOW_SEC", "2.0"))

# إعدادات المطاردة + الربح المطلق (جديدة)
CHASE_WINDOW_SEC    = float(os.getenv("CHASE_WINDOW_SEC", "18"))
CHASE_REPRICE_PAUSE = float(os.getenv("CHASE_REPRICE_PAUSE","0.35"))
TAKE_PROFIT_EUR     = float(os.getenv("TAKE_PROFIT_EUR", "0.05"))   # 5 سنت
MAKER_FEE_RATE      = float(os.getenv("MAKER_FEE_RATE",  "0.001"))  # 0.10%

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
    """يرجع JSON الخام من Bitvavo أو dict فيه 'error' عند الفشل النصّي."""
    url = f"{BASE_URL}{path}"
    ts  = str(int(time.time() * 1000))
    m = method.upper()
    if m in ("GET", "DELETE"):
        body_str = ""; payload = None
    else:
        body_str = json.dumps(body or {}, separators=(',',':'))
        payload  = (body or {})
    headers = {
        "Bitvavo-Access-Key": API_KEY,
        "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Signature": _sign(ts, m, f"/v2{path}", body_str),
        "Bitvavo-Access-Window": "10000",
        "Content-Type": "application/json",
    }
    r = requests.request(m, url, headers=headers, json=payload, timeout=timeout)
    try:
        return r.json()
    except Exception:
        return {"error": r.text, "status_code": r.status_code}

# ========= Precision helpers =========
def _count_decimals_of_step(step: float) -> int:
    s = f"{step:.16f}".rstrip("0").rstrip(".")
    if "." in s: return len(s.split(".", 1)[1])
    return 0

def _parse_amount_precision(ap, min_base_hint: float | int = 0) -> tuple[int, float]:
    """
    يرجع (amountDecimals, step)
    قاعدة خاصة: لو min_base_hint عدد صحيح ≥ 1 → السوق غالبًا لوت=1 ⇒ (0 منازل، step=1)
    غير ذلك:
      - ap عدد صحيح ⇒ اعتبره عدد منازل (decimals) → step = 10^-ap
      - ap عشري (0.01 / 0.5 / 1.0) ⇒ اعتبره خطوة مباشرة
    """
    try:
        mb = float(min_base_hint)
        # lot size = 1 (مثل PUMP)
        if mb >= 1.0 and abs(mb - round(mb)) < 1e-9:
            return 0, 1.0

        v = float(ap)
        if float(v).is_integer():
            decs = max(0, int(v))
            step = float(Decimal(1) / (Decimal(10) ** decs))
            return decs, step

        # غير صحيح → step مباشر (مثل 0.01 أو 0.5)
        step = float(v)
        s = f"{step:.16f}".rstrip("0").rstrip(".")
        decs = len(s.split(".", 1)[1]) if "." in s else 0
        return decs, step

    except Exception:
        # fallback آمن
        return 8, 1e-8

def _parse_price_precision(pp) -> int:
    """pricePrecision قد تكون عدد منازل أو خطوة؛ رجّع عدد المنازل."""
    try:
        v = float(pp)
        if not float(v).is_integer() or v < 1.0:
            return _count_decimals_of_step(v)
        return int(v)
    except Exception:
        return 6

def load_markets_once():
    """يجلب /markets مرة واحدة ويثبت دقّات السعر/الكمية والحدود الدنيا."""
    global MARKET_MAP, MARKET_META
    if MARKET_MAP and MARKET_META: return
    rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
    m, meta = {}, {}
    for r in rows:
        if r.get("quote") != "EUR": continue
        market = r.get("market"); base = (r.get("base") or "").upper()
        if not base or not market: continue

        min_quote = float(r.get("minOrderInQuoteAsset", 0) or 0.0)
        min_base  = float(r.get("minOrderInBaseAsset",  0) or 0.0)
        price_dec = _parse_price_precision(r.get("pricePrecision", 6))
        amt_dec, step = _parse_amount_precision(r.get("amountPrecision", 8), min_base)

        meta[market] = {
            "priceDecimals": price_dec,
            "amountDecimals": amt_dec,
            "step": float(step),
            "minQuote": min_quote,
            "minBase":  min_base,
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
    """قصّ الكمية على amountDecimals ثم على step (لأسواق مثل PUMP)."""
    decs = _amount_decimals(market)
    st   = Decimal(str(_step(market) or 0))
    a    = Decimal(str(amount))
    q = Decimal(10) ** -decs
    a = a.quantize(q, rounding=ROUND_DOWN)
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
    bid = float(ob["bids"][0][0]); ask = float(ob["asks"][0][0])
    return bid, ask

# ========= Cancel (strict with operatorId) =========
def cancel_order_blocking(market: str, orderId: str, clientOrderId: str | None = None,
                          wait_sec=CANCEL_WAIT_SEC):
    """
    DELETE /order بجسم يتضمن operatorId. نجاح إذا:
    - status صار canceled/filled، أو اختفى من /orders.
    """
    deadline = time.time() + max(wait_sec, 12.0)

    def _poll_order():
        st = bv_request("GET", f"/order?market={market}&orderId={orderId}")
        s  = (st or {}).get("status","").lower() if isinstance(st, dict) else ""
        return s, (st if isinstance(st, dict) else {})

    def _gone_from_open():
        lst = bv_request("GET", f"/orders?market={market}")
        if isinstance(lst, list):
            return not any(o.get("orderId")==orderId for o in lst)
        return False

    # DELETE /order (JSON body) + operatorId
    body = {"orderId": orderId, "market": market, "operatorId": ""}
    url  = f"{BASE_URL}/order"
    ts   = str(int(time.time() * 1000))
    body_str = json.dumps(body, separators=(',',':'))
    headers = {
        "Bitvavo-Access-Key": API_KEY,
        "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Signature": _sign(ts, "DELETE", "/v2/order", body_str),
        "Bitvavo-Access-Window": "10000",
        "Content-Type": "application/json",
    }
    r = requests.request("DELETE", url, headers=headers, json=body, timeout=10)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}
    print("DELETE(json+operatorId)", r.status_code, data)

    # انتظر لتغيّر الحالة أو اختفاء الطلب
    last_s, last_st = "unknown", {}
    while time.time() < deadline:
        s, st = _poll_order()
        last_s, last_st = s, st
        if s in ("canceled","filled"): return True, s, st
        if _gone_from_open():          return True, "canceled", {"note":"gone_after_delete"}
        time.sleep(0.5)

    # محاولة أخيرة: cancelAll
    end2 = time.time() + 6.0
    while time.time() < end2:
        _ = bv_request("DELETE", f"/orders?market={market}")
        time.sleep(0.6)
        s, st = _poll_order()
        last_s, last_st = s, st
        if s in ("canceled","filled") or _gone_from_open():
            return True, "canceled" if s not in ("canceled","filled") else s, st

    return False, last_s or "new", last_st

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

# ========= (إضافات مطاردة/تيك/TP) لا تغيّر منطق الأوامر القديمة =========
def _price_tick(market: str) -> Decimal:
    return Decimal(1) / (Decimal(10) ** _price_decimals(market))

def maker_buy_price_now(market: str) -> float:
    bid, ask = get_best_bid_ask(market)
    p = Decimal(str(bid)) - _price_tick(market)
    if p <= 0: p = Decimal(str(bid))
    return float(fmt_price_dec(market, p))

def maker_sell_price_now(market: str) -> float:
    bid, ask = get_best_bid_ask(market)
    p = Decimal(str(ask)) + _price_tick(market)
    return float(fmt_price_dec(market, p))

def place_limit_postonly_safe(market: str, side: str, price: float, amount: float, max_reprice=2):
    body, resp = place_limit_postonly(market, side, price, amount)
    if isinstance(resp, dict) and not resp.get("error"):
        return body, resp
    err = (resp or {}).get("error", "")
    if isinstance(err, str) and ("postOnly" in err or "taker" in err.lower()):
        tick = float(_price_tick(market))
        p = float(price)
        for _ in range(max_reprice):
            if side == "buy":
                p -= tick
            else:
                p += tick
            p = float(fmt_price_dec(market, p))
            body, resp = place_limit_postonly(market, side, p, amount)
            if isinstance(resp, dict) and not resp.get("error"):
                return body, resp
    return body, resp

def chase_maker_buy_until_filled(market: str, spend_eur: float) -> dict:
    """مطاردة Maker-Only لفترة قصيرة لالتقاط تنفيذ سريع دون Taker."""
    minq, minb = _min_quote(market), _min_base(market)
    deadline = time.time() + max(CHASE_WINDOW_SEC, 6.0)
    last_oid, last_body = None, None
    try:
        while time.time() < deadline:
            price = maker_buy_price_now(market)
            amount = round_amount_down(market, spend_eur / price)
            if amount < minb:
                return {"ok": False, "err": f"minBase={minb}", "ctx":"amount_too_small"}

            if last_oid:
                cancel_order_blocking(market, last_oid, None, wait_sec=3.0)

            body, resp = place_limit_postonly_safe(market, "buy", price, amount)
            last_body = body
            if isinstance(resp, dict) and resp.get("error"):
                return {"ok": False, "request": body, "response": resp, "err": resp.get("error")}
            oid = resp.get("orderId"); last_oid = oid

            t0 = time.time()
            while time.time() - t0 < max(0.8, CHASE_REPRICE_PAUSE):
                st = bv_request("GET", f"/order?market={market}&orderId={oid}")
                status = (st or {}).get("status","").lower()
                if status in ("filled","partiallyfilled"):
                    filled_base  = float((st or {}).get("filledAmount", 0) or 0)
                    filled_quote = float((st or {}).get("filledAmountQuote", 0) or 0)
                    if filled_base > 0 and filled_quote > 0:
                        avg_price = filled_quote / filled_base
                    else:
                        avg_price = float(body["price"])
                    return {"ok": True, "status": status, "order": resp,
                            "avg_price": avg_price, "filled_base": filled_base, "spent_eur": filled_quote}
                time.sleep(0.2)

            time.sleep(CHASE_REPRICE_PAUSE)

        if last_oid:
            st = bv_request("GET", f"/order?market={market}&orderId={last_oid}")
            status = (st or {}).get("status","").lower()
            return {"ok": False, "status": status or "unknown", "order": st, "ctx":"deadline_reached", "request": last_body}
        return {"ok": False, "err":"no_order_placed"}
    finally:
        pass  # لا نلغي تلقائيًا بعد انتهاء النافذة؛ سلوك محافظ.

def _target_sell_price_for_profit(market: str, avg_entry: float, base_amount: float,
                                  profit_eur_abs: float = None,
                                  fee_rate: float = None) -> float:
    """سعر بيع يضمن ربحًا مطلقًا باليورو بعد رسوم الصانع (تقريب شراء+بيع)."""
    if profit_eur_abs is None: profit_eur_abs = TAKE_PROFIT_EUR
    if fee_rate is None:       fee_rate = MAKER_FEE_RATE
    fee_mult = (1 + fee_rate) * (1 + fee_rate)  # تقريب
    base_adj = profit_eur_abs / max(base_amount, 1e-12)
    raw = avg_entry * fee_mult + base_adj
    maker_min = maker_sell_price_now(market)
    target = max(raw, maker_min)
    return float(fmt_price_dec(market, target))

def place_smart_takeprofit_sell(market: str, avg_entry: float, filled_base: float,
                                profit_eur_abs: float = None, fee_rate: float = None):
    """يضع أمر بيع Maker يحقق الربح المطلق المحدد."""
    if filled_base <= 0:
        return {"ok": False, "err": "no_filled_amount"}
    target = _target_sell_price_for_profit(market, avg_entry, filled_base, profit_eur_abs, fee_rate)
    body, resp = place_limit_postonly_safe(market, "sell", target, round_amount_down(market, filled_base))
    if isinstance(resp, dict) and resp.get("error"):
        return {"ok": False, "request": body, "response": resp, "err": resp.get("error")}
    return {"ok": True, "request": body, "response": resp, "target": target}

def buy_chase_and_tp(market: str, eur_amount: float | None):
    """شراء بالمطاردة ثم وضع TP ذكي — لا يغيّر buy_open الأصلي."""
    eur_avail = get_balance("EUR")
    spend = float(eur_avail) if (eur_amount is None or eur_amount <= 0) else float(eur_amount)
    spend = max(0.0, spend - HEADROOM_EUR)
    if spend <= 0:
        return {"ok": False, "err": f"No EUR to spend (avail={eur_avail:.2f})"}

    res = chase_maker_buy_until_filled(market, spend)
    if not res.get("ok"):
        return {"ok": False, "ctx":"buy_chase_failed", **res}

    status = res.get("status","")
    avg_price = float(res.get("avg_price") or 0.0)
    filled_base = float(res.get("filled_base") or 0.0)

    if status == "filled":
        OPEN_ORDERS.pop(market, None)
    else:
        order = res.get("order") or {}
        OPEN_ORDERS[market] = {
            "orderId": order.get("orderId") or order.get("orderID"),
            "clientOrderId": order.get("clientOrderId"),
            "amount_init": filled_base if filled_base > 0 else None,
            "side": "buy"
        }

    tp = place_smart_takeprofit_sell(market, avg_price, filled_base)
    return {"ok": True, "buy": res, "tp": tp}

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

    ob = requests.get(f"{BASE_URL}/{market}/book?depth=1", timeout=8).json()
    bid = float(ob["bids"][0][0]); ask = float(ob["asks"][0][0])
    price  = min(bid, ask*(1-1e-6))
    amount = round_amount_down(market, spend/price)
    if amount < minb:
        return {"ok": False, "err": f"minBase={minb}"}

    body, resp = place_limit_postonly(market, "buy", price, amount)
    if isinstance(resp, dict) and resp.get("error"):
        return {"ok": False, "request": body, "response": resp,
                "err": resp.get("error"), "hint": {
                    "amount": body["amount"], "price": body["price"],
                    "minQuote": _min_quote(market), "minBase": _min_base(market),
                    "amountDecimals": _amount_decimals(market)
                }}
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

    ob = requests.get(f"{BASE_URL}/{market}/book?depth=1", timeout=8).json()
    bid = float(ob["bids"][0][0]); ask = float(ob["asks"][0][0])
    price = max(ask, bid*(1+1e-6))

    body, resp = place_limit_postonly(market, "sell", price, amt)
    if isinstance(resp, dict) and resp.get("error"):
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
        # ----- اشتري (لا تغيير في المنطق) -----
        if low.startswith("اشتري "):
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

        # ----- بيع (لا تغيير) -----
        if low.startswith("بيع "):
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

        # ----- الغ (لا تغيير) -----
        if low.startswith("الغ "):
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

        # ----- اشتري+ (جديد: مطاردة + TP ذكي) -----
        if low.startswith("اشتري+"):
            parts = text.split()
            if len(parts) < 2:
                tg_send("صيغة: اشتري+ COIN [EUR]"); return jsonify(ok=True)
            market = _norm_market_from_text(parts[1])
            if not market:
                tg_send("⛔ عملة غير صالحة."); return jsonify(ok=True)
            eur = None
            if len(parts) >= 3:
                try: eur = float(parts[2])
                except: eur = None
            res = buy_chase_and_tp(market, eur)
            tg_send(("✅ شراء مطارد + TP" if res.get("ok") else "⚠️ فشل اشتري+") + f" — {market}\n"
                    f"{json.dumps(res, ensure_ascii=False)}")
            return jsonify(ok=True)

        # ----- ربح VALUE (جديد: ضبط الهدف المطلق) -----
        if low.startswith("ربح"):
            parts = text.split()
            if len(parts) < 2:
                tg_send(f"الهدف الحالي: {TAKE_PROFIT_EUR:.4f}€\nصيغة: ربح VALUE  (مثال: ربح 0.07)")
                return jsonify(ok=True)
            try:
                val = float(parts[1])
                if val <= 0:
                    raise ValueError("<=0")
                # تحديث المتغير العالمي فقط (جلسة التشغيل الحالية)
                global TAKE_PROFIT_EUR
                TAKE_PROFIT_EUR = val
                tg_send(f"✅ تم ضبط الربح المطلق إلى: {TAKE_PROFIT_EUR:.4f}€")
            except Exception:
                tg_send("⛔ قيمة غير صالحة. مثال: ربح 0.05")
            return jsonify(ok=True)

        tg_send("الأوامر: «اشتري COIN [EUR]» ، «بيع COIN [AMOUNT]» ، «الغ COIN» ، «اشتري+ COIN [EUR]» ، «ربح VALUE»")
        return jsonify(ok=True)

    except Exception as e:
        tg_send(f"🐞 خطأ: {type(e).__name__}: {e}")
        return jsonify(ok=True)

# ========= Health =========
@app.route("/", methods=["GET"])
def home():
    return "Saqer Maker — Arabic-only on /tg (smart-precision) ✅"

# ========= Main =========
if __name__ == "__main__":
    load_markets_once()
    app.run(host="0.0.0.0", port=PORT)