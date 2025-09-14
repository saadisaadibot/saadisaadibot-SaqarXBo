# -*- coding: utf-8 -*-
"""
Saqer — Maker-Only (Bitvavo / EUR)
Chasing Buy (ask−tick) + Dual Maker Exits (TP & SL) + Robust Cancel + Ready Callback

ENV:
BOT_TOKEN, CHAT_ID,
BITVAVO_API_KEY, BITVAVO_API_SECRET,
ABOSIYAH_URL   # قاعدة أبو صياح، سنستدعي POST {ABOSIYAH_URL}/ready
PORT=8080,
HEADROOM_EUR=0.30,
CHASE_WINDOW_SEC=30, CHASE_REPRICE_PAUSE=0.25, CHASE_MAX_TOTAL_SEC=120,
TAKE_PROFIT_MIN_EUR=0.05, TAKE_PROFIT_MAX_EUR=0.20, MAKER_FEE_RATE=0.001,
SL_PCT=2.0
"""

import os, re, time, json, hmac, hashlib, requests
from uuid import uuid4
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from decimal import Decimal, ROUND_DOWN, getcontext

# ========= Boot / ENV =========
load_dotenv()
app = Flask(__name__)
getcontext().prec = 28

BOT_TOKEN   = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID     = os.getenv("CHAT_ID", "").strip()
API_KEY     = os.getenv("BITVAVO_API_KEY", "").strip()
API_SECRET  = os.getenv("BITVAVO_API_SECRET", "").strip()
PORT        = int(os.getenv("PORT", "8080"))

ABOSIYAH_URL = (os.getenv("ABOSIYAH_URL","") or "").rstrip("/")

BASE_URL    = "https://api.bitvavo.com/v2"

HEADROOM_EUR        = float(os.getenv("HEADROOM_EUR", "0.30"))
CANCEL_WAIT_SEC     = float(os.getenv("CANCEL_WAIT_SEC", "12.0"))
SHORT_FOLLOW_SEC    = float(os.getenv("SHORT_FOLLOW_SEC", "2.0"))

# مطاردة السعر
CHASE_WINDOW_SEC     = float(os.getenv("CHASE_WINDOW_SEC", "30"))
CHASE_REPRICE_PAUSE  = float(os.getenv("CHASE_REPRICE_PAUSE","0.25"))
CHASE_MAX_TOTAL_SEC  = float(os.getenv("CHASE_MAX_TOTAL_SEC","120"))

# ربح/خسارة
TP_MIN_EUR     = float(os.getenv("TAKE_PROFIT_MIN_EUR", "0.05"))
TP_MAX_EUR     = float(os.getenv("TAKE_PROFIT_MAX_EUR", "0.20"))
MAKER_FEE_RATE = float(os.getenv("MAKER_FEE_RATE",  "0.001"))
SL_PCT         = float(os.getenv("SL_PCT", "2.0"))  # -2%

# ====== Signal control ======
SIGNAL_COOLDOWN_SEC = float(os.getenv("SIGNAL_COOLDOWN_SEC","300"))
LAST_SIGNAL_AT      = {}

# كاش الماركت + تتبع
MARKET_MAP  = {}
MARKET_META = {}
OPEN_ORDERS = {}   # market -> {...}
ACTIVE_BASE = None

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

# ========= Notify Abosiyah =========
def notify_ready(base: str|None, reason: str, pnl_eur: float|None):
    if not ABOSIYAH_URL:
        return
    try:
        payload = {"coin": base, "reason": reason, "pnl_eur": pnl_eur}
        requests.post(f"{ABOSIYAH_URL}/ready", json=payload, timeout=8)
    except Exception as e:
        print("notify_ready err:", e)

# ========= Bitvavo =========
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

# ========= Precision helpers =========
def _count_decimals_of_step(step: float) -> int:
    s = f"{step:.16f}".rstrip("0").rstrip(".")
    if "." in s: return len(s.split(".", 1)[1])
    return 0

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
        s = f"{step:.16f}".rstrip("0").rstrip(".")
        decs = len(s.split(".", 1)[1]) if "." in s else 0
        return decs, step
    except Exception:
        return 8, 1e-8

def _parse_price_precision(pp) -> int:
    try:
        v = float(pp)
        if not float(v).is_integer() or v < 1.0:
            return _count_decimals_of_step(v)
        return int(v)
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

    # DELETE /order (JSON body) + operatorId ""
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

    last_s, last_st = "unknown", {}
    while time.time() < deadline:
        s, st = _poll_order()
        last_s, last_st = s, st
        if s in ("canceled","filled"): return True, s, st
        if _gone_from_open():
            s2, st2 = _poll_order()
            fin = s2 or s or "unknown"
            return True, fin, (st2 or st or {"note":"gone_after_delete"})
        time.sleep(0.5)

    # fallback: cancelAll
    _ = bv_request("DELETE", f"/orders?market={market}")
    time.sleep(0.6)
    s, st = _poll_order()
    return (s in ("canceled","filled") or _gone_from_open()), (s or "unknown"), st

# ========= Place Maker (with operatorId="") =========
AMOUNT_DIGITS_RE = re.compile(r"with\s+(\d+)\s+decimal digits", re.IGNORECASE)

def _override_amount_decimals(market: str, decs: int):
    load_markets_once()
    meta = MARKET_META.get(market, {}) or {}
    meta["amountDecimals"] = int(decs)
    try:
        mb = float(meta.get("minBase", 0))
        if mb >= 1.0 and abs(mb - round(mb)) < 1e-9:
            meta["step"] = 1.0
        else:
            meta["step"] = float(Decimal(1) / (Decimal(10) ** int(decs)))
    except Exception:
        meta["step"] = float(Decimal(1) / (Decimal(10) ** int(decs)))
    MARKET_META[market] = meta

def _round_to_decimals(value: float, decs: int) -> float:
    q = Decimal(10) ** -int(decs)
    return float((Decimal(str(value))).quantize(q, rounding=ROUND_DOWN))

def _price_tick(market: str) -> Decimal:
    return Decimal(1) / (Decimal(10) ** _price_decimals(market))

def maker_buy_price_now(market: str) -> float:
    """أفضل سعر ميكر سريع: على ask−tick إن كان السبريد يسمح، وإلا على الـbid."""
    bid, ask = get_best_bid_ask(market)
    tick = float(_price_tick(market))
    p = (ask - tick) if (ask - bid) > tick else bid
    return float(fmt_price_dec(market, p))

def maker_sell_price_now(market: str) -> float:
    _, ask = get_best_bid_ask(market)
    p = Decimal(str(ask)) + _price_tick(market)
    return float(fmt_price_dec(market, p))

def _signed_post(path: str, body: dict):
    ts  = str(int(time.time() * 1000))
    body_str = json.dumps(body, separators=(',',':'))
    sig = _sign(ts, "POST", f"/v2{path}", body_str)
    headers = {
        "Bitvavo-Access-Key": API_KEY, "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Signature": sig, "Bitvavo-Access-Window": "10000",
        "Content-Type": "application/json",
    }
    r = requests.post(f"{BASE_URL}{path}", headers=headers, data=body_str, timeout=10)
    try: return r.json()
    except: return {"error": r.text}

def place_limit_postonly(market: str, side: str, price: float, amount: float):
    def _send(p: float, a: float):
        body = {
            "market": market, "side": side, "orderType": "limit", "postOnly": True,
            "clientOrderId": str(uuid4()),
            "price": fmt_price_dec(market, p),
            "amount": fmt_amount(market, a),
            "operatorId": ""   # مهم
        }
        data = _signed_post("/order", body)
        return body, data

    body, resp = _send(price, amount)
    err = (resp or {}).get("error", "")

    if isinstance(err, str) and "too many decimal digits" in err.lower():
        m = AMOUNT_DIGITS_RE.search(err)
        if m:
            try:
                allowed_decs = int(m.group(1))
                _override_amount_decimals(market, allowed_decs)
                adj_amount = _round_to_decimals(float(amount), allowed_decs)
                adj_amount = round_amount_down(market, adj_amount)
                body, resp = _send(price, adj_amount)
                return body, resp
            except Exception:
                pass

    if isinstance(err, str) and ("postonly" in err.lower() or "taker" in err.lower()):
        tick = float(_price_tick(market))
        adj_price = float(price) - tick if side == "buy" else float(price) + tick
        body, resp = _send(adj_price, amount)
        return body, resp

    return body, resp

# ========= Reconciliation =========
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
            total_b += a
            total_q += a * p
        except Exception:
            continue
    if total_b > 0 and total_q > 0:
        return (total_q / total_b), total_b
    return 0.0, 0.0

def reconcile_recent_buys_and_place_tp(market: str, lookback_sec: int = 45,
                                        tp_eur: float | None = None) -> dict:
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
                        return place_dual_exits(market, avg, round_amount_down(market, base), tp_eur)
            except Exception:
                continue
    return {"ok": False, "err": "no_recent_buys_found"}

# ========= Exit helpers =========
def _auto_profit_eur(spent_eur: float) -> float:
    # ربح مطلق تقريبي ~0.8% من قيمة الصفقة ضمن [TP_MIN_EUR, TP_MAX_EUR]
    target = 0.008 * float(spent_eur)
    return max(TP_MIN_EUR, min(TP_MAX_EUR, target))

def _tp_price_from_abs(market: str, avg_entry: float, base_amount: float, profit_eur_abs: float):
    fee_mult = (1 + MAKER_FEE_RATE) * (1 + MAKER_FEE_RATE)
    base_adj = profit_eur_abs / max(base_amount, 1e-12)
    raw = avg_entry * fee_mult + base_adj
    maker_min = maker_sell_price_now(market)
    target = max(raw, maker_min)
    return float(fmt_price_dec(market, target))

def place_dual_exits(market: str, avg_entry: float, base_amount: float, profit_eur_abs: float | None):
    """يضع أمرين بيع Maker: TP أعلى و SL أدنى. يُعيد (oid_tp, oid_sl)."""
    if base_amount <= 0:
        return {"ok": False, "err": "no_amount"}

    if profit_eur_abs is None:
        profit_eur_abs = TP_MIN_EUR

    # TP
    tp_price = _tp_price_from_abs(market, avg_entry, base_amount, profit_eur_abs)
    body_tp, resp_tp = place_limit_postonly(market, "sell", tp_price, round_amount_down(market, base_amount))
    if isinstance(resp_tp, dict) and resp_tp.get("error"):
        return {"ok": False, "stage": "tp", "request": body_tp, "response": resp_tp}

    # SL (limit-maker تحت السعر)
    bid, _ = get_best_bid_ask(market)
    sl_floor = avg_entry * (1 - SL_PCT/100.0)
    sl_price = min(bid, avg_entry) * (1 - 1e-6)
    sl_price = max(sl_floor, sl_price)
    sl_price = float(fmt_price_dec(market, sl_price))
    body_sl, resp_sl = place_limit_postonly(market, "sell", sl_price, round_amount_down(market, base_amount))
    if isinstance(resp_sl, dict) and resp_sl.get("error"):
        # لو فشل SL، على الأقل نرجع TP موجود
        return {"ok": True, "tp": resp_tp, "tp_req": body_tp, "sl_error": resp_sl}

    return {"ok": True, "tp": resp_tp, "tp_req": body_tp, "sl": resp_sl, "sl_req": body_sl}

def wait_until_one_exit_filled(market: str, oid_tp: str | None, oid_sl: str | None,
                               max_wait_sec: float = 300.0):
    """يراقب أوامر الخروج؛ عند أول Fill يلغي الآخر ويرجع PnL تخميني."""
    t_end = time.time() + max_wait_sec
    def _st(oid):
        if not oid: return ""
        s = bv_request("GET", f"/order?market={market}&orderId={oid}")
        return (s or {}).get("status","").lower(), s

    while time.time() < t_end:
        st_tp, obj_tp = _st(oid_tp)
        st_sl, obj_sl = _st(oid_sl)
        if st_tp in ("filled","partiallyfilled"):
            # ألغِ SL
            if oid_sl:
                try: cancel_order_blocking(market, oid_sl, None, 4.0)
                except: pass
            # PnL تقريبي من بيانات TP
            fa = float((obj_tp or {}).get("filledAmount",0) or 0)
            fq = float((obj_tp or {}).get("filledAmountQuote",0) or 0)
            pnle = fq - fa * float((obj_tp or {}).get("averagePrice", obj_tp.get("price",0)) or 0)  # تقريبي
            return "tp_hit", pnle
        if st_sl in ("filled","partiallyfilled"):
            if oid_tp:
                try: cancel_order_blocking(market, oid_tp, None, 4.0)
                except: pass
            fa = float((obj_sl or {}).get("filledAmount",0) or 0)
            fq = float((obj_sl or {}).get("filledAmountQuote",0) or 0)
            pnle = fq - fa * float((obj_sl or {}).get("averagePrice", obj_sl.get("price",0)) or 0)
            return "sl_hit", pnle
        time.sleep(0.5)
    return "timeout", None

# ========= Chase buy + Dual exits =========
def chase_maker_buy_until_filled(market: str, spend_eur: float) -> dict:
    global OPEN_ORDERS
    minq, minb = _min_quote(market), _min_base(market)

    deadline = time.time() + CHASE_WINDOW_SEC
    hard_end = time.time() + CHASE_MAX_TOTAL_SEC
    last_oid, last_body = None, None
    spent_quote = 0.0

    try:
        while time.time() < hard_end:
            price  = maker_buy_price_now(market)
            amount = round_amount_down(market, spend_eur / price)
            if amount < minb:
                return {"ok": False, "err": f"minBase={minb}", "ctx":"amount_too_small"}

            if last_oid:
                cancel_order_blocking(market, last_oid, None, wait_sec=3.0)

            body, resp = place_limit_postonly(market, "buy", price, amount)
            last_body = body
            if isinstance(resp, dict) and resp.get("error"):
                return {"ok": False, "request": body, "response": resp, "err": resp.get("error")}
            oid = resp.get("orderId"); last_oid = oid

            if oid:
                OPEN_ORDERS[market] = {
                    "orderId": oid,
                    "clientOrderId": body.get("clientOrderId"),
                    "amount_init": amount,
                    "side": "buy"
                }

            # متابعة قصيرة لكل إعادة تسعير
            t0 = time.time()
            while time.time() - t0 < max(0.8, CHASE_REPRICE_PAUSE):
                st = bv_request("GET", f"/order?market={market}&orderId={oid}")
                status = (st or {}).get("status","").lower()
                if status in ("filled","partiallyfilled"):
                    filled_base  = float((st or {}).get("filledAmount", 0) or 0)
                    filled_quote = float((st or {}).get("filledAmountQuote", 0) or 0)
                    spent_quote += filled_quote
                    avg_price = (filled_quote/filled_base) if (filled_base>0 and filled_quote>0) else float(body["price"])
                    return {
                        "ok": True, "status": status, "order": resp,
                        "avg_price": avg_price, "filled_base": filled_base, "spent_eur": spent_quote
                    }
                time.sleep(0.2)

            if time.time() > deadline:
                # نعيد الضبط لنقترب أكثر في الدورة التالية
                deadline = time.time() + CHASE_REPRICE_PAUSE

        # انتهت النافذة الصلبة — ألغِ آخر أمر وانهِ بالفشل
        if last_oid:
            try: cancel_order_blocking(market, last_oid, None, wait_sec=3.0)
            except: pass
            st = bv_request("GET", f"/order?market={market}&orderId={last_oid}")
            status = (st or {}).get("status","").lower()
            return {"ok": False, "status": status or "unknown", "order": st,
                    "ctx":"deadline_reached", "request": last_body, "last_oid": last_oid}
        return {"ok": False, "err":"no_order_placed"}
    finally:
        pass

# ========= BUY (with exits) =========
def buy_open_and_exit(market: str):
    eur_avail = get_balance("EUR")
    spend = max(0.0, float(eur_avail) - HEADROOM_EUR)
    if spend <= 0:
        return {"ok": False, "err": f"No EUR to spend (avail={eur_avail:.2f})"}

    buy_res = chase_maker_buy_until_filled(market, spend)
    if not buy_res.get("ok"):
        # حاول مصالحة سريعة (إذا كان تم شراء فعلاً)
        tp_try = reconcile_recent_buys_and_place_tp(market, lookback_sec=30,
                                                    tp_eur=_auto_profit_eur(spend))
        if tp_try.get("ok"):
            # انتظر أي خروج (في حال وضع SL أيضاً)
            oid_tp = (tp_try.get("tp") or {}).get("orderId")
            oid_sl = (tp_try.get("sl") or {}).get("orderId")
            state, pnl = wait_until_one_exit_filled(market, oid_tp, oid_sl, 300)
            return {"ok": True, "mode":"reconciled", "exit_state": state, "pnl": pnl}
        return {"ok": False, "ctx":"buy_failed", **buy_res}

    # شراء ناجح
    avg_price   = float(buy_res.get("avg_price") or 0.0)
    filled_base = float(buy_res.get("filled_base") or 0.0)
    OPEN_ORDERS.pop(market, None)

    # ضع TP & SL معاً
    exits = place_dual_exits(market, avg_price, filled_base, _auto_profit_eur(buy_res.get("spent_eur", spend)))
    if not exits.get("ok"):
        return {"ok": False, "ctx":"exit_place_failed", **exits}

    oid_tp = (exits.get("tp") or {}).get("orderId")
    oid_sl = (exits.get("sl") or {}).get("orderId")
    state, pnl = wait_until_one_exit_filled(market, oid_tp, oid_sl, 300)
    return {"ok": True, "buy": buy_res, "exit_state": state, "pnl": pnl}

# ========= Utilities =========
def _sell_all_except(target_market: str | None):
    load_markets_once()
    # ألغِ أي أوامر مفتوحة بالكاش لدينا
    for m in list(OPEN_ORDERS.keys()):
        try:
            info = OPEN_ORDERS.get(m) or {}
            if info.get("orderId"):
                cancel_order_blocking(m, info["orderId"], info.get("clientOrderId"), CANCEL_WAIT_SEC)
        except Exception as e:
            print("cancel err", m, e)
        finally:
            OPEN_ORDERS.pop(m, None)

    # بيع الأرصدة غير الهدف
    bals = bv_request("GET","/balance")
    if not isinstance(bals, list): return
    for b in bals:
        base = (b.get("symbol") or "").upper()
        if not base or base=="EUR": continue
        mkt = MARKET_MAP.get(base)
        if not mkt or (target_market and mkt==target_market):
            continue
        try:
            amt = float(b.get("available",0) or 0.0)
            if amt <= 0: continue
            # بيع فوري Maker
            bid, ask = get_best_bid_ask(mkt)
            price = max(ask, bid*(1+1e-6))
            place_limit_postonly(mkt, "sell", price, round_amount_down(mkt, amt))
            tg_send(f"↩️ بيع كل {base} (تحضير للتبديل).")
        except Exception as e:
            print("sell-all err", base, e)

# ========= Signal Webhook =========
@app.route("/hook", methods=["POST"])
def hook():
    """
    يستقبل: {"coin":"ADA", "cmd":"buy","ttl":60,"ts":...}
    - يلغي/يبيع غير الهدف
    - يشتري الهدف بمطاردة Maker على ask−tick
    - يضع TP & SL معاً
    - عند أي خروج أو فشل شراء: يخبر أبو صياح /ready
    """
    load_markets_once()
    data = request.get_json(silent=True) or {}
    base = (data.get("coin") or "").upper().strip()
    ttl  = float(data.get("ttl") or 0)
    ts   = float(data.get("ts") or 0)
    if not base:
        return jsonify(ok=False, err="no coin"), 400
    market = coin_to_market(base)
    if not market:
        return jsonify(ok=False, err="unknown coin or no EUR market"), 400

    # TTL
    if ttl>0 and ts>0 and (time.time() - ts/1000.0) > ttl:
        tg_send(f"⏸ تجاهلت إشارة {base} (TTL انتهى).")
        return jsonify(ok=False, err="ttl_expired"), 200

    # Cooldown
    if base in LAST_SIGNAL_AT and (time.time() - LAST_SIGNAL_AT[base]) < SIGNAL_COOLDOWN_SEC:
        tg_send(f"⏸ {base} ضمن cooldown.")
        return jsonify(ok=True, skipped="cooldown"), 200

    _sell_all_except(market)

    res = buy_open_and_exit(market)
    LAST_SIGNAL_AT[base] = time.time()

    # إشعار أبو صياح
    if res.get("ok"):
        state = res.get("exit_state") or "unknown"
        notify_ready(base, reason=state, pnl_eur=res.get("pnl"))
        tg_send(f"✅ Signal {base} — exit={state} pnl={res.get('pnl')}")
    else:
        notify_ready(base, reason="buy_failed", pnl_eur=None)
        tg_send(f"⚠️ فشل شراء {base} — {json.dumps(res, ensure_ascii=False)}")

    return jsonify(ok=True, result=res), 200

# ========= Health =========
@app.route("/", methods=["GET"])
def home():
    return "Saqer — Maker chase (ask−tick) + dual exits + ready-callback ✅"

# ========= Main =========
if __name__ == "__main__":
    load_markets_once()
    app.run(host="0.0.0.0", port=PORT)