# -*- coding: utf-8 -*-
"""
Saqer — Maker-Only (Bitvavo / EUR)
Chasing Buy + Smart TP + Robust Cancel + Reconciliation
+ Signal /hook → يشتري ويبيع تلقائيًا حسب الإشارة (عملة واحدة فعّالة)
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

BASE_URL    = "https://api.bitvavo.com/v2"

HEADROOM_EUR        = float(os.getenv("HEADROOM_EUR", "0.30"))
CANCEL_WAIT_SEC     = float(os.getenv("CANCEL_WAIT_SEC", "12.0"))
SHORT_FOLLOW_SEC    = float(os.getenv("SHORT_FOLLOW_SEC", "2.0"))

# مطاردة السعر
CHASE_WINDOW_SEC    = float(os.getenv("CHASE_WINDOW_SEC", "30"))
CHASE_REPRICE_PAUSE = float(os.getenv("CHASE_REPRICE_PAUSE","0.35"))

# ربح ذكي (بسيط)
TAKE_PROFIT_EUR     = float(os.getenv("TAKE_PROFIT_EUR", "0.05"))
MAKER_FEE_RATE      = float(os.getenv("MAKER_FEE_RATE",  "0.001"))

# ====== Signal control ======
SIGNAL_COOLDOWN_SEC = float(os.getenv("SIGNAL_COOLDOWN_SEC","300"))   # لمنع تكرار نفس العملة بسرعة
LAST_SIGNAL_AT      = {}  # base -> ts

# كاش الماركت + تتبع
MARKET_MAP  = {}
MARKET_META = {}
OPEN_ORDERS = {}   # market -> {...}
ACTIVE_BASE = None # آخر عملة “فعّالة” (لنبيعها عند تبديل الإشارة)

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

# ========= Cancel (strict with operatorId) + final-state detect =========
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

    end2 = time.time() + 6.0
    while time.time() < end2:
        _ = bv_request("DELETE", f"/orders?market={market}")
        time.sleep(0.6)
        s, st = _poll_order()
        last_s, last_st = s, st
        if s in ("canceled","filled") or _gone_from_open():
            s2, st2 = _poll_order()
            fin = s2 or s or ("canceled" if _gone_from_open() else "unknown")
            return True, fin, (st2 or st or {"note":"gone_after_delete"})

    return False, last_s or "new", last_st

# ========= Place Maker (amount-decimals fix + postOnly reprice) =========
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
    bid, _ = get_best_bid_ask(market)
    p = Decimal(str(bid))
    return float(fmt_price_dec(market, p))

def maker_sell_price_now(market: str) -> float:
    _, ask = get_best_bid_ask(market)
    p = Decimal(str(ask)) + _price_tick(market)
    return float(fmt_price_dec(market, p))

def place_limit_postonly(market: str, side: str, price: float, amount: float):
    """
    يعالج أخطاء amount-decimals ويعيد التسعير لرفض postOnly.
    يحافظ على operatorId="" والتوقيع على نفس JSON.
    """
    def _send(p: float, a: float):
        body = {
            "market": market, "side": side, "orderType": "limit", "postOnly": True,
            "clientOrderId": str(uuid4()),
            "price": fmt_price_dec(market, p),
            "amount": fmt_amount(market, a),
            "operatorId": ""  # مهم
        }
        ts  = str(int(time.time() * 1000))
        body_str = json.dumps(body, separators=(',',':'))
        sig = _sign(ts, "POST", "/v2/order", body_str)
        headers = {
            "Bitvavo-Access-Key": API_KEY, "Bitvavo-Access-Timestamp": ts,
            "Bitvavo-Access-Signature": sig, "Bitvavo-Access-Window": "10000",
            "Content-Type": "application/json",
        }
        r = requests.post(f"{BASE_URL}/order", headers=headers, data=body_str, timeout=10)
        try:
            data = r.json()
        except Exception:
            data = {"error": r.text}
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
                        return place_smart_takeprofit_sell(
                            market, avg, round_amount_down(market, base), TAKE_PROFIT_EUR, MAKER_FEE_RATE)
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
            return place_smart_takeprofit_sell(
                market, avg, round_amount_down(market, total_b), TAKE_PROFIT_EUR, MAKER_FEE_RATE)

    return {"ok": False, "err": "no_recent_buys_found"}

# ========= Chase buy + Smart TP =========
def chase_maker_buy_until_filled(market: str, spend_eur: float) -> dict:
    global OPEN_ORDERS
    minq, minb = _min_quote(market), _min_base(market)
    deadline = time.time() + max(CHASE_WINDOW_SEC, 6.0)
    last_oid, last_body = None, None

    try:
        while time.time() < deadline:
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

            t0 = time.time()
            while time.time() - t0 < max(0.8, CHASE_REPRICE_PAUSE):
                st = bv_request("GET", f"/order?market={market}&orderId={oid}")
                status = (st or {}).get("status","").lower()
                if status in ("filled","partiallyfilled"):
                    filled_base  = float((st or {}).get("filledAmount", 0) or 0)
                    filled_quote = float((st or {}).get("filledAmountQuote", 0) or 0)
                    avg_price = (filled_quote/filled_base) if (filled_base>0 and filled_quote>0) else float(body["price"])
                    return {
                        "ok": True, "status": status, "order": resp,
                        "avg_price": avg_price, "filled_base": filled_base, "spent_eur": filled_quote
                    }
                time.sleep(0.2)

            time.sleep(CHASE_REPRICE_PAUSE)

        if last_oid:
            st = bv_request("GET", f"/order?market={market}&orderId={last_oid}")
            status = (st or {}).get("status","").lower()
            return {"ok": False, "status": status or "unknown", "order": st,
                    "ctx":"deadline_reached", "request": last_body, "last_oid": last_oid}
        return {"ok": False, "err":"no_order_placed"}
    finally:
        pass

# ========= Smart TP helpers =========
def _target_sell_price_for_profit(market: str, avg_entry: float, base_amount: float,
                                  profit_eur_abs: float = None,
                                  fee_rate: float = None) -> float:
    if profit_eur_abs is None: profit_eur_abs = TAKE_PROFIT_EUR
    if fee_rate is None:       fee_rate = MAKER_FEE_RATE
    fee_mult = (1 + fee_rate) * (1 + fee_rate)
    base_adj = profit_eur_abs / max(base_amount, 1e-12)
    raw = avg_entry * fee_mult + base_adj
    maker_min = maker_sell_price_now(market)
    target = max(raw, maker_min)
    return float(fmt_price_dec(market, target))

def place_smart_takeprofit_sell(market: str, avg_entry: float, filled_base: float,
                                profit_eur_abs: float = None, fee_rate: float = None):
    if filled_base <= 0:
        return {"ok": False, "err": "no_filled_amount"}
    target = _target_sell_price_for_profit(market, avg_entry, filled_base, profit_eur_abs, fee_rate)
    body, resp = place_limit_postonly(market, "sell", target, round_amount_down(market, filled_base))
    if isinstance(resp, dict) and resp.get("error"):
        return {"ok": False, "request": body, "response": resp, "err": resp.get("error")}
    return {"ok": True, "request": body, "response": resp, "target": target}

# ========= BUY / SELL =========
def buy_open(market: str, eur_amount: float | None):
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
            return {"ok": True, "buy":"reconciled_fill", "tp": tp_try}
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

    tp_res = place_smart_takeprofit_sell(market, avg_price, filled_base, TAKE_PROFIT_EUR, MAKER_FEE_RATE)
    return {"ok": True, "buy": buy_res, "tp": tp_res, "open": OPEN_ORDERS.get(market)}

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
    price = max(ask, bid*(1+1e-6))
    body, resp = place_limit_postonly(market, "sell", price, amt)
    if isinstance(resp, dict) and resp.get("error"):
        return {"ok": False, "request": body, "response": resp, "err": (resp or {}).get("error")}
    t0 = time.time(); oid = resp.get("orderId")
    while time.time()-t0 < SHORT_FOLLOW_SEC and oid:
        st = bv_request("GET", f"/order?market={market}&orderId={oid}")
        if (st or {}).get("status","").lower() in ("filled","partiallyfilled"):
            break
        time.sleep(0.25)
    return {"ok": True, "request": body, "response": resp}

# ========= Utilities (signals) =========
def _sell_all_except(target_market: str | None):
    """يُلغي أوامر كل الأسواق ويبيع أي رصيد متاح لكل عملة ≠ الهدف."""
    load_markets_once()
    # 1) الغِ جميع الأوامر المفتوحة لكل أسواق EUR
    for m in list(OPEN_ORDERS.keys()):
        try:
            info = OPEN_ORDERS.get(m) or {}
            if info.get("orderId"):
                cancel_order_blocking(m, info["orderId"], info.get("clientOrderId"), CANCEL_WAIT_SEC)
        except Exception as e:
            print("cancel err", m, e)
        finally:
            OPEN_ORDERS.pop(m, None)

    # 2) بيع كل الأرصدة عدا الهدف
    bals = bv_request("GET","/balance")
    if not isinstance(bals, list): return
    for b in bals:
        base = (b.get("symbol") or "").upper()
        if not base or base=="EUR": 
            continue
        mkt = MARKET_MAP.get(base)
        if not mkt or (target_market and mkt==target_market):
            continue
        try:
            amt = float(b.get("available",0) or 0.0)
            if amt <= 0: 
                continue
            maker_sell(mkt, amt)
            tg_send(f"↩️ بيع كل {base} (تحضير للتبديل).")
        except Exception as e:
            print("sell-all err", base, e)

# ========= Telegram Webhook =========
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

    global TAKE_PROFIT_EUR
    low = text.lower()
    try:
        if low.startswith("اشتري"):
            parts = text.split()
            if len(parts) < 2:
                tg_send("صيغة: اشتري COIN"); return jsonify(ok=True)
            market = _norm_market_from_text(parts[1])
            if not market:
                tg_send("⛔ عملة غير صالحة."); return jsonify(ok=True)
            _sell_all_except(market)
            res = buy_open(market, None)
            tg_send(("✅ شراء مطارد + TP" if res.get("ok") else "⚠️ فشل الشراء") + f" — {market}\n"
                    f"{json.dumps(res, ensure_ascii=False)}")
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
                _ = bv_request("DELETE", f"/orders?market={market}")
                time.sleep(0.7)
                open_after = bv_request("GET", f"/orders?market={market}")
                still = []
                if isinstance(open_after, list):
                    still = [o for o in open_after if (o.get("status","").lower() in ("new","partiallyfilled"))]
                if not still:
                    OPEN_ORDERS.pop(market, None)
                    tg_send("✅ تم إلغاء كل أوامر السوق (fallback).")
                else:
                    tg_send("⚠️ لم أستطع إلغاء كل الأوامر.\n" + json.dumps(still[:3], ensure_ascii=False))

            tp_try = reconcile_recent_buys_and_place_tp(market, lookback_sec=45)
            if tp_try.get("ok"):
                tg_send("ℹ️ رُصد تنفيذ شراء أثناء الإلغاء — تم وضع TP تلقائيًا.\n" +
                        json.dumps(tp_try, ensure_ascii=False))
            return jsonify(ok=True)

        if low.startswith("فحص"):
            load_markets_once()
            tg_send("📊 حالة صقر:\n" + json.dumps({
                "open_orders": OPEN_ORDERS, "active_base": ACTIVE_BASE
            }, ensure_ascii=False))
            return jsonify(ok=True)

        if low.startswith("ربح"):
            parts = text.split()
            if len(parts) < 2:
                tg_send(f"الهدف الحالي: {TAKE_PROFIT_EUR:.4f}€\nصيغة: ربح VALUE  (مثال: ربح 0.10)")
                return jsonify(ok=True)
            try:
                val = float(parts[1])
                if val <= 0: raise ValueError("<=0")
                TAKE_PROFIT_EUR = val
                tg_send(f"✅ تم ضبط الربح المطلق إلى: {TAKE_PROFIT_EUR:.4f}€")
            except Exception:
                tg_send("⛔ قيمة غير صالحة. مثال: ربح 0.05")
            return jsonify(ok=True)

        tg_send("الأوامر: «اشتري COIN» ، «بيع COIN [AMOUNT]» ، «الغ COIN» ، «ربح VALUE» ، «فحص»")
        return jsonify(ok=True)

    except Exception as e:
        tg_send(f"🐞 خطأ: {type(e).__name__}: {e}")
        return jsonify(ok=True)

# ========= Signal Webhook =========
@app.route("/hook", methods=["POST"])
def hook():
    """
    يستقبل: {"coin":"ADA", "cmd":"buy","ttl":60}
    - يلغي أوامر العملات الأخرى ويبيع أي رصيد قديم
    - يشتري العملة المطلوبة ويضع TP
    """
    load_markets_once()
    data = request.get_json(silent=True) or {}
    base = (data.get("coin") or "").upper().strip()
    ttl  = float(data.get("ttl") or 0)
    if not base:
        return jsonify(ok=False, err="no coin"), 400
    market = coin_to_market(base)
    if not market:
        return jsonify(ok=False, err="unknown coin or no EUR market"), 400

    # TTL: ارفض إذا الإشارة قديمة جدًا
    ts = float(data.get("ts") or 0)
    if ttl > 0 and ts > 0:
        if (time.time() - ts/1000.0) > ttl:
            tg_send(f"⏸ تجاهلت إشارة {base} (TTL انتهى).")
            return jsonify(ok=False, err="ttl_expired"), 200

    # Cooldown: لا نكرر نفس العملة بسرعة
    if base in LAST_SIGNAL_AT and (time.time() - LAST_SIGNAL_AT[base]) < SIGNAL_COOLDOWN_SEC:
        tg_send(f"⏸ {base} ضمن cooldown.")
        return jsonify(ok=True, skipped="cooldown"), 200

    # تحضير للتبديل: إلغاء وبيع غير الهدف
    _sell_all_except(market)

    # شراء الهدف بكل EUR
    res = buy_open(market, None)
    LAST_SIGNAL_AT[base] = time.time()
    global ACTIVE_BASE
    ACTIVE_BASE = base

    tg_send(("✅ Signal BUY + TP" if res.get("ok") else "⚠️ Signal buy failed")
            + f" — {market}\n{json.dumps(res, ensure_ascii=False)}")
    return jsonify(ok=True, result=res), 200

# ========= Health =========
@app.route("/", methods=["GET"])
def home():
    return "Saqer Maker — Signal+Telegram (chase+smartTP+robust-cancel+reconcile) ✅"

# ========= Main =========
if __name__ == "__main__":
    load_markets_once()
    app.run(host="0.0.0.0", port=PORT)