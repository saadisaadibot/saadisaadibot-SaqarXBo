# -*- coding: utf-8 -*-
"""
Saqer — Maker-Only (Bitvavo / EUR) — Telegram /tg + Abosiyah /hook
Chase Buy (live) + Smart TP + SL -2% + Strong Cancel + Reconcile + Ready callback

الأوامر (Telegram):
- "بيع COIN [AMOUNT]" ← بيع يدوي Maker (نفس التنسيق)
- "الغ COIN"          ← إلغاء الأمر المفتوح (نسخة أقوى)
- "ربح VALUE"         ← ضبط هدف الربح الذكي باليورو
(الشراء حصراً عبر POST /hook من “أبو صياح”)

Webhook من “أبو صياح”:
POST /hook
  JSON: {"action":"buy","coin":"ADA","tp_eur":0.05,"sl_pct":-2}

Ready إلى “أبو صياح”:
POST ABUSIYAH_READY_URL
  JSON: {"coin":"ADA","reason":"tp_filled|sl_triggered|buy_failed","pnl_eur":null}

ENV الأساسية:
  BOT_TOKEN, CHAT_ID,
  BITVAVO_API_KEY, BITVAVO_API_SECRET
  ABUSIYAH_READY_URL, LINK_SECRET (اختياري للهيدر X-Link-Secret)
  HEADROOM_EUR=0.30, CANCEL_WAIT_SEC=12, SHORT_FOLLOW_SEC=2
  CHASE_WINDOW_SEC=30, CHASE_REPRICE_PAUSE=0.35
  TAKE_PROFIT_EUR=0.05, MAKER_FEE_RATE=0.001
  SL_PCT=-2, SL_CHECK_SEC=0.8
"""

import os, re, time, json, hmac, hashlib, requests, threading
from uuid import uuid4
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from decimal import Decimal, ROUND_DOWN, getcontext

# ===== Boot / ENV =====
load_dotenv()
app = Flask(__name__)
getcontext().prec = 28

BOT_TOKEN   = os.getenv("BOT_TOKEN","").strip()
CHAT_ID     = os.getenv("CHAT_ID","").strip()
API_KEY     = os.getenv("BITVAVO_API_KEY","").strip()
API_SECRET  = os.getenv("BITVAVO_API_SECRET","").strip()
PORT        = int(os.getenv("PORT","8080"))

BASE_URL    = "https://api.bitvavo.com/v2"

HEADROOM_EUR        = float(os.getenv("HEADROOM_EUR","0.30"))
CANCEL_WAIT_SEC     = float(os.getenv("CANCEL_WAIT_SEC","12.0"))
SHORT_FOLLOW_SEC    = float(os.getenv("SHORT_FOLLOW_SEC","2.0"))

CHASE_WINDOW_SEC    = float(os.getenv("CHASE_WINDOW_SEC","30"))
CHASE_REPRICE_PAUSE = float(os.getenv("CHASE_REPRICE_PAUSE","0.35"))

TAKE_PROFIT_EUR     = float(os.getenv("TAKE_PROFIT_EUR","0.05"))
MAKER_FEE_RATE      = float(os.getenv("MAKER_FEE_RATE","0.001"))

SL_PCT              = float(os.getenv("SL_PCT","-2"))
SL_CHECK_SEC        = float(os.getenv("SL_CHECK_SEC","0.8"))

ABUSIYAH_READY_URL  = os.getenv("ABUSIYAH_READY_URL","").strip()
LINK_SECRET         = os.getenv("LINK_SECRET","").strip()

# كاش السوق والحالة
MARKET_MAP  = {}   # "ADA" → "ADA-EUR"
MARKET_META = {}   # meta per market
OPEN_ORDERS = {}   # market -> {"orderId": "...", "clientOrderId": "...", "amount_init": float, "side": "buy"/"sell"}
POSITIONS   = {}   # market -> {"avg":float,"base":float,"tp_oid":str|None,"sl_price":float,"tp_target":float}

# ===== Telegram =====
def tg_send(text: str):
    if not BOT_TOKEN:
        print("TG:", text); return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID or None, "text": text},
            timeout=8
        )
    except Exception as e:
        print("tg_send err:", e)

def _auth_chat(chat_id: str) -> bool:
    return (not CHAT_ID) or (str(chat_id) == str(CHAT_ID))

# ===== Bitvavo =====
def _sign(ts, method, path, body=""):
    msg = f"{ts}{method}{path}{body}"
    return hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def bv_request(method: str, path: str, body: dict | None = None, timeout=10):
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

# ===== Precision helpers =====
def _count_decimals_of_step(step: float) -> int:
    s = f"{step:.16f}".rstrip("0").rstrip(".")
    return len(s.split(".", 1)[1]) if "." in s else 0

def _parse_amount_precision(ap, min_base_hint: float | int = 0) -> tuple[int, float]:
    try:
        mb = float(min_base_hint)
        if mb >= 1.0 and abs(mb - round(mb)) < 1e-9:
            return 0, 1.0
        v = float(ap)
        if float(v).is_integer():
            decs = max(0, int(v))
            step = float(Decimal(1) / (Decimal(10) ** decs))
            return decs, step
        step = float(v)
        return _count_decimals_of_step(step), step
    except Exception:
        return 8, 1e-8

def _parse_price_precision(pp) -> int:
    try:
        v = float(pp)
        return _count_decimals_of_step(v) if (not float(v).is_integer() or v < 1.0) else int(v)
    except Exception:
        return 6

def load_markets_once():
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
    q = Decimal(10) ** -_price_decimals(market)
    s = f"{(Decimal(str(price))).quantize(q, rounding=ROUND_DOWN):f}"
    if "." in s:
        whole, frac = s.split(".", 1)
        frac = frac[:_price_decimals(market)].rstrip("0")
        return whole if not frac else f"{whole}.{frac}"
    return s

def round_amount_down(market: str, amount: float | Decimal) -> float:
    decs = _amount_decimals(market)
    st   = Decimal(str(_step(market) or 0))
    a    = Decimal(str(amount)).quantize(Decimal(10) ** -decs, rounding=ROUND_DOWN)
    if st > 0:
        a = (a // st) * st
    return float(a)

def fmt_amount(market: str, amount: float | Decimal) -> str:
    q = Decimal(10) ** -_amount_decimals(market)
    s = f"{(Decimal(str(amount))).quantize(q, rounding=ROUND_DOWN):f}"
    if "." in s:
        whole, frac = s.split(".", 1)
        frac = frac[:_amount_decimals(market)].rstrip("0")
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
    return float(ob["bids"][0][0]), float(ob["asks"][0][0])

# ===== Cancel (strict + multi-strategy) =====
def cancel_order_blocking(market: str, orderId: str, clientOrderId: str | None = None,
                          wait_sec=CANCEL_WAIT_SEC):
    """
    DELETE /order بجسم + operatorId مع Poll حتى final-state.
    """
    deadline = time.time() + max(wait_sec, 12.0)

    def _poll():
        st = bv_request("GET", f"/order?market={market}&orderId={orderId}")
        s  = (st or {}).get("status","").lower() if isinstance(st, dict) else ""
        return s, (st if isinstance(st, dict) else {})

    def _gone():
        lst = bv_request("GET", f"/orders?market={market}")
        if isinstance(lst, list):
            return not any(o.get("orderId")==orderId for o in lst)
        return False

    body = {"orderId": orderId, "market": market, "operatorId": ""}
    ts   = str(int(time.time() * 1000))
    body_str = json.dumps(body, separators=(',',':'))
    headers = {
        "Bitvavo-Access-Key": API_KEY,
        "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Signature": _sign(ts, "DELETE", "/v2/order", body_str),
        "Bitvavo-Access-Window": "10000",
        "Content-Type": "application/json",
    }
    r = requests.request("DELETE", f"{BASE_URL}/order", headers=headers, json=body, timeout=10)
    try: data = r.json()
    except Exception: data = {"raw": r.text}
    print("DELETE(json+operatorId)", r.status_code, data)

    last_s, last_st = "unknown", {}
    while time.time() < deadline:
        s, st = _poll()
        last_s, last_st = s, st
        if s in ("canceled","filled"): return True, s, st
        if _gone():
            s2, st2 = _poll()
            fin = s2 or s or "unknown"
            return True, fin, (st2 or st or {"note":"gone_after_delete"})
        time.sleep(0.4)

    return False, last_s or "new", last_st

def _cancel_by_querystring(market: str, orderId: str = None, clientOrderId: str = None):
    try:
        if orderId:
            _ = bv_request("DELETE", f"/order?market={market}&orderId={orderId}")
        if clientOrderId:
            _ = bv_request("DELETE", f"/order?market={market}&clientOrderId={clientOrderId}")
    except Exception:
        pass

def force_cancel_market(market: str, max_wait: float = 15.0) -> tuple[bool, list]:
    """
    إلغاء قوي متعدد الإستراتيجيات مع polling.
    يرجع (ok, remaining_open_list)
    """
    deadline = time.time() + max(max_wait, 6.0)

    def _list_open():
        lst = bv_request("GET", f"/orders?market={market}")
        if isinstance(lst, list):
            return [o for o in lst if (o.get("status","").lower() in ("new","partiallyfilled"))]
        return []

    last_open = []
    while time.time() < deadline:
        open_now = _list_open()
        last_open = open_now
        if not open_now:
            return True, []

        for o in open_now:
            oid = o.get("orderId"); cid = o.get("clientOrderId")
            try: cancel_order_blocking(market, oid, cid, wait_sec=3.0)
            except Exception: pass
            _cancel_by_querystring(market, orderId=oid, clientOrderId=cid)

        # cancelAll على كل دورة
        try: _ = bv_request("DELETE", f"/orders?market={market}")
        except Exception: pass

        time.sleep(0.8)

    still = _list_open()
    return (len(still) == 0), still

# ===== Place Maker (postOnly + fixes) =====
AMOUNT_DIGITS_RE = re.compile(r"with\s+(\d+)\s+decimal digits", re.IGNORECASE)

def _override_amount_decimals(market: str, decs: int):
    load_markets_once()
    meta = MARKET_META.get(market, {}) or {}
    meta["amountDecimals"] = int(decs)
    try:
        mb = float(meta.get("minBase", 0))
        meta["step"] = 1.0 if (mb >= 1.0 and abs(mb - round(mb)) < 1e-9) else float(Decimal(1) / (Decimal(10) ** int(decs)))
    except Exception:
        meta["step"] = float(Decimal(1) / (Decimal(10) ** int(decs)))
    MARKET_META[market] = meta

def _round_to_decimals(value: float, decs: int) -> float:
    return float((Decimal(str(value))).quantize(Decimal(10) ** -int(decs), rounding=ROUND_DOWN))

def _price_tick(market: str) -> Decimal:
    return Decimal(1) / (Decimal(10) ** _price_decimals(market))

def maker_buy_price_now(market: str) -> float:
    bid, _ = get_best_bid_ask(market)
    return float(fmt_price_dec(market, Decimal(str(bid))))

def maker_sell_price_now(market: str) -> float:
    _, ask = get_best_bid_ask(market)
    return float(fmt_price_dec(market, Decimal(str(ask)) + _price_tick(market)))

def place_limit_postonly(market: str, side: str, price: float, amount: float):
    def _send(p: float, a: float):
        body = {
            "market": market, "side": side, "orderType": "limit", "postOnly": True,
            "clientOrderId": str(uuid4()),
            "price": fmt_price_dec(market, p),
            "amount": fmt_amount(market, a),
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
        try: data = r.json()
        except Exception: data = {"error": r.text}
        return body, data

    body, resp = _send(price, amount)
    err = (resp or {}).get("error", "")

    if isinstance(err, str) and "too many decimal digits" in err.lower():
        m = AMOUNT_DIGITS_RE.search(err)
        if m:
            allowed_decs = int(m.group(1))
            _override_amount_decimals(market, allowed_decs)
            adj_amount = round_amount_down(market, _round_to_decimals(float(amount), allowed_decs))
            return _send(price, adj_amount)

    if isinstance(err, str) and ("postonly" in err.lower() or "taker" in err.lower()):
        tick = float(_price_tick(market))
        adj_price = float(price) - tick if side == "buy" else float(price) + tick
        return _send(adj_price, amount)

    return body, resp

# ===== Reconciliation + TP/SL =====
def _avg_from_order_fills(order_obj: dict) -> tuple[float, float]:
    try:
        fa = float(order_obj.get("filledAmount", 0) or 0)
        fq = float(order_obj.get("filledAmountQuote", 0) or 0)
        if fa > 0 and fq > 0:
            return (fq / fa), fa
    except Exception:
        pass
    fills = order_obj.get("fills") or []
    total_q = 0.0; total_b = 0.0
    for f in fills:
        try:
            a = float(f.get("amount", 0) or 0)
            p = float(f.get("price", 0) or 0)
            total_b += a; total_q += a*p
        except Exception:
            continue
    return ((total_q / total_b), total_b) if (total_b > 0 and total_q > 0) else (0.0, 0.0)

def reconcile_recent_buys_and_place_tp(market: str, lookback_sec: int = 45) -> dict:
    now_ms = int(time.time()*1000)
    hist = bv_request("GET", f"/orders?market={market}")
    if isinstance(hist, list):
        recent = sorted(hist, key=lambda o: int(o.get("updated", 0) or 0), reverse=True)
        for o in recent[:6]:
            try:
                ts = int(o.get("updated", 0) or 0)
                if now_ms - ts > lookback_sec*1000: 
                    continue
                if (o.get("side","").lower() == "buy") and (o.get("status","").lower() == "filled"):
                    avg, base = _avg_from_order_fills(o)
                    if base > 0 and avg > 0:
                        res = place_smart_takeprofit_sell(market, avg, round_amount_down(market, base), TAKE_PROFIT_EUR, MAKER_FEE_RATE)
                        if res.get("ok"):
                            POSITIONS[market] = {"avg": avg, "base": round_amount_down(market, base),
                                                 "tp_oid": (res["response"] or {}).get("orderId"),
                                                 "sl_price": avg*(1.0 + (SL_PCT/100.0)),
                                                 "tp_target": float(res.get("target",0))}
                        return res
            except Exception:
                continue

    trades = bv_request("GET", f"/trades?market={market}&limit=50")
    if isinstance(trades, list):
        total_b = 0.0; total_q = 0.0
        for t in trades:
            try:
                if (t.get("side","").lower() != "buy"): 
                    continue
                ts = int(t.get("timestamp", 0) or 0)
                if now_ms - ts > lookback_sec*1000:
                    continue
                a = float(t.get("amount", 0) or 0)
                p = float(t.get("price", 0) or 0)
                total_b += a; total_q += a*p
            except Exception:
                continue
        if total_b > 0 and total_q > 0:
            avg = total_q/total_b
            res = place_smart_takeprofit_sell(market, avg, round_amount_down(market, total_b), TAKE_PROFIT_EUR, MAKER_FEE_RATE)
            if res.get("ok"):
                POSITIONS[market] = {"avg": avg, "base": round_amount_down(market, total_b),
                                     "tp_oid": (res["response"] or {}).get("orderId"),
                                     "sl_price": avg*(1.0 + (SL_PCT/100.0)),
                                     "tp_target": float(res.get("target",0))}
            return res
    return {"ok": False, "err": "no_recent_buys_found"}

def _target_sell_price_for_profit(market: str, avg_entry: float, base_amount: float,
                                  profit_eur_abs: float = None,
                                  fee_rate: float = None) -> float:
    if profit_eur_abs is None: profit_eur_abs = TAKE_PROFIT_EUR
    if fee_rate is None:       fee_rate = MAKER_FEE_RATE
    fee_mult = (1 + fee_rate) * (1 + fee_rate)  # شراء+بيع
    base_adj = profit_eur_abs / max(base_amount, 1e-12)
    raw = avg_entry * fee_mult + base_adj
    maker_min = maker_sell_price_now(market)
    return float(fmt_price_dec(market, max(raw, maker_min)))

def place_smart_takeprofit_sell(market: str, avg_entry: float, filled_base: float,
                                profit_eur_abs: float = None, fee_rate: float = None):
    if filled_base <= 0: return {"ok": False, "err": "no_filled_amount"}
    target = _target_sell_price_for_profit(market, avg_entry, filled_base, profit_eur_abs, fee_rate)
    body, resp = place_limit_postonly(market, "sell", target, round_amount_down(market, filled_base))
    if isinstance(resp, dict) and resp.get("error"):
        return {"ok": False, "request": body, "response": resp, "err": resp.get("error")}
    return {"ok": True, "request": body, "response": resp, "target": target}

def emergency_taker_sell(market: str, amount: float):
    """بيع فوري (تكر) للـ SL — limit بدون postOnly عند/تحت الـBid."""
    bid, _ = get_best_bid_ask(market)
    price = max(0.0, bid * (1 - 0.001))
    body = {
        "market": market, "side": "sell", "orderType": "limit",
        "postOnly": False, "clientOrderId": str(uuid4()),
        "price": fmt_price_dec(market, price),
        "amount": fmt_amount(market, round_amount_down(market, amount)),
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
    try: data = r.json()
    except Exception: data = {"error": r.text}
    return {"ok": not bool((data or {}).get("error")), "request": body, "response": data}

# ===== Chase buy (LIVE reprice) =====
def chase_maker_buy_until_filled(market: str, spend_eur: float) -> dict:
    """
    مطاردة حيّة: يعيد التسعير فور تحرّك الـBid بقدر tick، مع متابعة سريعة.
    """
    minq, minb = _min_quote(market), _min_base(market)
    deadline   = time.time() + max(CHASE_WINDOW_SEC, 6.0)
    last_oid   = None
    last_body  = None
    last_price = None
    tick       = float(_price_tick(market))

    try:
        while time.time() < deadline:
            # لو ما في أمر مفتوح — ضع أمر جديد على الـBid حالاً
            if not last_oid:
                price  = maker_buy_price_now(market)
                amount = round_amount_down(market, spend_eur / max(price, 1e-12))
                if amount < minb:
                    return {"ok": False, "err": f"minBase={minb}", "ctx": "amount_too_small"}
                body, resp = place_limit_postonly(market, "buy", price, amount)
                if isinstance(resp, dict) and resp.get("error"):
                    return {"ok": False, "request": body, "response": resp, "err": resp.get("error")}
                last_oid, last_body, last_price = resp.get("orderId"), body, price
                if last_oid:
                    OPEN_ORDERS[market] = {"orderId": last_oid, "clientOrderId": body.get("clientOrderId"),
                                           "amount_init": amount, "side": "buy"}

            # متابعة سريعة جدًا: filled? reprice?
            t0 = time.time()
            while time.time() - t0 < max(0.8, CHASE_REPRICE_PAUSE):
                st = bv_request("GET", f"/order?market={market}&orderId={last_oid}")
                status = (st or {}).get("status","").lower()
                if status in ("filled","partiallyfilled"):
                    fb = float((st or {}).get("filledAmount", 0) or 0)
                    fq = float((st or {}).get("filledAmountQuote", 0) or 0)
                    avg = (fq/fb) if (fb>0 and fq>0) else float(last_body["price"])
                    return {"ok": True, "status": status, "order": {"orderId": last_oid},
                            "avg_price": avg, "filled_base": fb, "spent_eur": fq}

                bid, _ = get_best_bid_ask(market)
                if last_price is not None and (bid - last_price) >= tick:
                    # Reprice فوري
                    cancel_order_blocking(market, last_oid, None, wait_sec=3.0)
                    price  = float(fmt_price_dec(market, Decimal(str(bid))))
                    amount = round_amount_down(market, spend_eur / max(price, 1e-12))
                    body, resp = place_limit_postonly(market, "buy", price, amount)
                    if isinstance(resp, dict) and resp.get("error"):
                        return {"ok": False, "request": body, "response": resp, "err": resp.get("error")}
                    last_oid, last_body, last_price = resp.get("orderId"), body, price
                    if last_oid:
                        OPEN_ORDERS[market] = {"orderId": last_oid, "clientOrderId": body.get("clientOrderId"),
                                               "amount_init": amount, "side": "buy"}
                    t0 = time.time()  # أعِد نافذة المتابعة بعد reprice
                time.sleep(0.15)

            # ريبرايس روتيني لو تغير السعر بمقدار تيك
            bid, _ = get_best_bid_ask(market)
            if last_price is None or abs(bid - last_price) >= tick:
                cancel_order_blocking(market, last_oid, None, wait_sec=3.0)
                price  = float(fmt_price_dec(market, Decimal(str(bid))))
                amount = round_amount_down(market, spend_eur / max(price, 1e-12))
                body, resp = place_limit_postonly(market, "buy", price, amount)
                if isinstance(resp, dict) and resp.get("error"):
                    return {"ok": False, "request": body, "response": resp, "err": resp.get("error")}
                last_oid, last_body, last_price = resp.get("orderId"), body, price
                if last_oid:
                    OPEN_ORDERS[market] = {"orderId": last_oid, "clientOrderId": body.get("clientOrderId"),
                                           "amount_init": amount, "side": "buy"}

        if last_oid:
            st = bv_request("GET", f"/order?market={market}&orderId={last_oid}")
            status = (st or {}).get("status","").lower()
            return {"ok": False, "status": status or "unknown", "order": st,
                    "ctx":"deadline_reached", "request": last_body, "last_oid": last_oid}
        return {"ok": False, "err":"no_order_placed"}
    finally:
        pass

def buy_open(market: str, eur_amount: float | None, tp_override: float | None = None, sl_pct_override: float | None = None):
    if market in OPEN_ORDERS:
        return {"ok": False, "err": "order_already_open", "open": OPEN_ORDERS[market]}

    eur_avail = get_balance("EUR")
    spend = max(0.0, float(eur_avail) - HEADROOM_EUR)
    if spend <= 0:
        return {"ok": False, "err": f"No EUR to spend (avail={eur_avail:.2f})"}

    buy_res = chase_maker_buy_until_filled(market, spend)
    if not buy_res.get("ok"):
        tp_try = reconcile_recent_buys_and_place_tp(market, lookback_sec=45)
        if tp_try.get("ok"):
            res = {"ok": True, "buy":"reconciled_fill", "tp": tp_try, "open": OPEN_ORDERS.get(market)}
            avg = POSITIONS.get(market, {}).get("avg"); base = POSITIONS.get(market, {}).get("base")
            if avg and base:
                _slpct = sl_pct_override if sl_pct_override is not None else SL_PCT
                POSITIONS[market]["sl_price"] = avg * (1.0 + (_slpct/100.0))
            return res

        if buy_res.get("last_oid"):
            req = buy_res.get("request") or {}
            OPEN_ORDERS[market] = {
                "orderId": buy_res["last_oid"],
                "clientOrderId": req.get("clientOrderId"),
                "amount_init": None,
                "side": "buy"
            }
        return {"ok": False, "ctx":"buy_chase_failed", **buy_res}

    status = buy_res.get("status","")
    avg_price = float(buy_res.get("avg_price") or 0.0)
    filled_base = float(buy_res.get("filled_base") or 0.0)

    if status == "filled":
        OPEN_ORDERS.pop(market, None)

    _tp_eur = tp_override if tp_override is not None else TAKE_PROFIT_EUR
    tp_res = place_smart_takeprofit_sell(market, avg_price, filled_base, _tp_eur, MAKER_FEE_RATE)
    if tp_res.get("ok"):
        _slpct = sl_pct_override if sl_pct_override is not None else SL_PCT
        POSITIONS[market] = {
            "avg": avg_price,
            "base": round_amount_down(market, filled_base),
            "tp_oid": (tp_res["response"] or {}).get("orderId"),
            "sl_price": avg_price*(1.0 + (_slpct/100.0)),
            "tp_target": float(tp_res.get("target",0))
        }
    return {"ok": True, "buy": buy_res, "tp": tp_res, "open": OPEN_ORDERS.get(market)}

def maker_sell(market: str, amount: float | None):
    base = market.split("-")[0]
    if amount is None or amount <= 0:
        bals = bv_request("GET","/balance"); amt = 0.0
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

# ===== Ready notify =====
def notify_abusiyah_ready(market: str, reason: str = "sold", pnl_eur=None):
    if not ABUSIYAH_READY_URL:
        tg_send(f"ready — {market} ({reason})"); return
    coin = market.split("-")[0]
    payload = {"coin": coin, "reason": reason, "pnl_eur": pnl_eur}
    headers = {"Content-Type":"application/json"}
    if LINK_SECRET: headers["X-Link-Secret"] = LINK_SECRET
    try:
        requests.post(ABUSIYAH_READY_URL, json=payload, headers=headers, timeout=6)
    except Exception as e:
        tg_send(f"⚠️ فشل نداء ready: {e}")

# ===== SL Watchdog =====
def _check_tp_filled(market: str, oid: str) -> bool:
    st = bv_request("GET", f"/order?market={market}&orderId={oid}")
    return isinstance(st, dict) and (st.get("status","").lower() == "filled")

def _cancel_if_open(market: str, oid: str):
    if not oid: return
    try:
        cancel_order_blocking(market, oid, None, wait_sec=3.0)
    except Exception:
        pass

def _sl_loop():
    while True:
        try:
            for market, pos in list(POSITIONS.items()):
                avg = float(pos.get("avg",0))
                base= float(pos.get("base",0))
                slp = float(pos.get("sl_price",0))
                tp_oid = pos.get("tp_oid")
                if base <= 0 or avg <= 0 or slp <= 0:
                    continue

                if tp_oid and _check_tp_filled(market, tp_oid):
                    POSITIONS.pop(market, None)
                    notify_abusiyah_ready(market, reason="tp_filled")
                    continue

                bid, _ = get_best_bid_ask(market)
                if bid <= slp:
                    _cancel_if_open(market, tp_oid)
                    sell_res = emergency_taker_sell(market, base)
                    POSITIONS.pop(market, None)
                    notify_abusiyah_ready(market, reason=("sl_triggered" if sell_res.get("ok") else "sl_fail"))
            time.sleep(max(0.4, SL_CHECK_SEC))
        except Exception as e:
            print("SL loop err:", e)
            time.sleep(1.0)

threading.Thread(target=_sl_loop, daemon=True).start()

# ===== Telegram Webhook =====
COIN_RE = re.compile(r"^[A-Z0-9]{2,15}$")
def _norm_market_from_text(arg: str) -> str | None:
    s = (arg or "").strip().upper()
    if not s: return None
    if s.endswith("-EUR") and COIN_RE.match(s.split("-")[0]): return s
    if COIN_RE.match(s): return f"{s}-EUR"
    return None

@app.route("/tg", methods=["GET"])
def tg_alive(): return "OK /tg", 200

@app.route("/tg", methods=["POST"])
def tg_webhook():
    upd = request.get_json(silent=True) or {}
    msg  = upd.get("message") or upd.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    text = (msg.get("text") or "").strip()
    if not chat_id or not _auth_chat(chat_id) or not text:
        return jsonify(ok=True)

    global TAKE_PROFIT_EUR
    low = text.lower()
    try:
        if low.startswith("اشتري"):
            tg_send("ℹ️ الشراء الآن حصراً عبر أبو صياح → POST /hook.")
            return jsonify(ok=True)

        if low.startswith("بيع"):
            parts = text.split()
            if len(parts) < 2:
                tg_send("صيغة: بيع COIN [AMOUNT]"); return jsonify(ok=True)
            market = _norm_market_from_text(parts[1])
            if not market:
                tg_send("⛔ عملة غير صالحة."); return jsonify(ok=True)
            amt = None
            if len(parts) >= 3:
                try: amt = float(parts[2])
                except: amt = None
            res = maker_sell(market, amt)
            tg_send(("✅ أُرسل أمر بيع" if res.get("ok") else "⚠️ فشل البيع") + f" — {market}\n"
                    f"{json.dumps(res, ensure_ascii=False)}")
            return jsonify(ok=True)

        if low.startswith("الغ"):
            parts = text.split()
            if len(parts) < 2:
                tg_send("صيغة: الغ COIN"); return jsonify(ok=True)
            market = _norm_market_from_text(parts[1])
            if not market:
                tg_send("⛔ عملة غير صالحة."); return jsonify(ok=True)

            info = OPEN_ORDERS.get(market)
            ok = False; final = "unknown"; last = {}

            if info and info.get("orderId"):
                ok, final, last = cancel_order_blocking(
                    market, info["orderId"], info.get("clientOrderId"), CANCEL_WAIT_SEC)
                if ok:
                    OPEN_ORDERS.pop(market, None)
                    tg_send(f"✅ تم الإلغاء — status={final}\n{json.dumps(last, ensure_ascii=False)}")
                else:
                    tg_send(f"⚠️ فشل الإلغاء — status={final}\n{json.dumps(last, ensure_ascii=False)}")

            if not info or not ok:
                ok2, still = force_cancel_market(market, max_wait=15.0)
                if ok2:
                    OPEN_ORDERS.pop(market, None)
                    tg_send("✅ تم إلغاء كل أوامر السوق.")
                else:
                    tg_send("⚠️ لم أستطع إلغاء كل الأوامر.\n" +
                            json.dumps((still[:3] if isinstance(still, list) else still), ensure_ascii=False))

            # مصالحة: لو تم شراء أثناء الإلغاء، ضع TP فورًا
            tp_try = reconcile_recent_buys_and_place_tp(market, lookback_sec=45)
            if tp_try.get("ok"):
                tg_send("ℹ️ رُصد تنفيذ شراء أثناء الإلغاء — تم وضع TP تلقائيًا.\n" +
                        json.dumps(tp_try, ensure_ascii=False))
            return jsonify(ok=True)

        if low.startswith("ربح"):
            parts = text.split()
            if len(parts) < 2:
                tg_send(f"الهدف الحالي: {TAKE_PROFIT_EUR:.4f}€\nصيغة: ربح VALUE  (مثال: ربح 0.10)")
                return jsonify(ok=True)
            try:
                val = float(parts[1])
                if val <= 0:
                    raise ValueError("<=0")
                TAKE_PROFIT_EUR = val
                tg_send(f"✅ تم ضبط الربح المطلق إلى: {TAKE_PROFIT_EUR:.4f}€")
            except Exception:
                tg_send("⛔ قيمة غير صالحة. مثال: ربح 0.05")
            return jsonify(ok=True)

        tg_send("الأوامر: «بيع COIN [AMOUNT]» ، «الغ COIN» ، «ربح VALUE» — الشراء عبر أبو صياح /hook")
        return jsonify(ok=True)

    except Exception as e:
        tg_send(f"🐞 خطأ: {type(e).__name__}: {e}")
        return jsonify(ok=True)

# ===== Abosiyah Hook (NON-BLOCKING) =====
@app.route("/hook", methods=["POST"])
def abusiyah_hook():
    """
    يستقبل: {"action":"buy","coin":"ADA","tp_eur":0.05,"sl_pct":-2}
    يشغّل الشراء بخيط خلفي ويرد فوراً (لتفادي مهلة العامل).
    """
    try:
        if LINK_SECRET and request.headers.get("X-Link-Secret","") != LINK_SECRET:
            return jsonify(ok=False, err="bad secret"), 401

        data = request.get_json(silent=True) or {}
        action = (data.get("action") or data.get("cmd") or "").lower().strip()
        coin   = (data.get("coin") or "").upper().strip()
        tp_eur = data.get("tp_eur", None)
        sl_pct = data.get("sl_pct", None)

        if action != "buy" or not COIN_RE.match(coin):
            return jsonify(ok=False, err="invalid_payload"), 400

        market = coin_to_market(coin)
        if not market:
            tg_send(f"⛔ سوق غير مدعوم — {coin}")
            return jsonify(ok=False, err="unsupported_market"), 400

        def _run():
            try:
                res = buy_open(
                    market, None,
                    tp_override=(float(tp_eur) if tp_eur is not None else None),
                    sl_pct_override=(float(sl_pct) if sl_pct is not None else None),
                )
                if res.get("ok"):
                    tg_send(f"✅ اشترى + TP — {market}\n{json.dumps(res,ensure_ascii=False)}")
                else:
                    info = res.get("open") or {}
                    if info.get("orderId"):
                        cancel_order_blocking(market, info["orderId"], info.get("clientOrderId"), CANCEL_WAIT_SEC)
                    tg_send(f"⚠️ فشل الشراء بعد مطاردة — {market}\n{json.dumps(res,ensure_ascii=False)}")
                    notify_abusiyah_ready(market, reason="buy_failed")
            except Exception as e:
                tg_send(f"🐞 hook worker error: {type(e).__name__}: {e}")

        threading.Thread(target=_run, daemon=True).start()
        return jsonify(ok=True, msg="buy started"), 202

    except Exception as e:
        tg_send(f"🐞 hook error: {type(e).__name__}: {e}")
        return jsonify(ok=False, err=str(e)), 500

# ===== Health =====
@app.route("/", methods=["GET"])
def home():
    return "Saqer Maker — live chase + strong cancel + ready ✅", 200

# ===== Main =====
if __name__ == "__main__":
    load_markets_once()
    # ملاحظة: لو تستخدم Gunicorn على Railway، يفضّل عامل واحد:
    # web: gunicorn main:app --workers 1 --timeout 120
    app.run(host="0.0.0.0", port=PORT)