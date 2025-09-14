# -*- coding: utf-8 -*-
# saqer_core.py — ثابت (DO NOT EDIT)
# تشغيل Flask + اتصالات Bitvavo + Redis state + Telegram + Ready callback
# واجهة CoreAPI ثابتة يستهلكها strategy.py فقط

__SAQER_CORE_VERSION__ = "core-1.0"

import os, json, time, hmac, hashlib, threading, requests
from uuid import uuid4
from decimal import Decimal, ROUND_DOWN, getcontext
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ===== تحميل أسرار فقط من .env =====
load_dotenv()
BOT_TOKEN  = os.getenv("BOT_TOKEN","").strip()
CHAT_ID    = os.getenv("CHAT_ID","").strip()
API_KEY    = os.getenv("BITVAVO_API_KEY","").strip()
API_SECRET = os.getenv("BITVAVO_API_SECRET","").strip()
REDIS_URL  = os.getenv("REDIS_URL","").strip()
ABUSIYAH_READY_URL = os.getenv("ABUSIYAH_READY_URL","").strip()
LINK_SECRET        = os.getenv("LINK_SECRET","").strip()

PORT = int(os.getenv("PORT","8080"))
BASE_URL = "https://api.bitvavo.com/v2"
MAKER_FEE_RATE = float(os.getenv("MAKER_FEE_RATE","0.001"))

# مصادر الأسعار (اختياري)
PRICE_SOURCE     = os.getenv("PRICE_SOURCE","redis_http").lower()  # redis_only|http_only|redis_http
BOOK_HASH_NS     = os.getenv("BOOK_HASH_NS","saqer:book")
SESSION_NS       = os.getenv("SESSION_NS","saqer:sessions")
OPEN_NS          = os.getenv("OPEN_NS","saqer:open")

# تردد فحص SL/TP
SL_CHECK_SEC     = float(os.getenv("SL_CHECK_SEC","0.8"))

getcontext().prec = 28
app = Flask(__name__)

# ===== Redis =====
try:
    import redis
    R = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2, socket_connect_timeout=2) if REDIS_URL else None
except Exception:
    R = None

# ===== Telegram =====
def tg_send(text: str):
    if not BOT_TOKEN:
        print("TG:", text); return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID or None, "text": text}, timeout=8)
    except Exception as e:
        print("tg_send err:", e)

def _auth_chat(chat_id: str) -> bool:
    return (not CHAT_ID) or (str(chat_id) == str(CHAT_ID))

# ===== Bitvavo Signing + REST =====
def _sign(ts, method, path, body=""):
    msg = f"{ts}{method}{path}{body}"
    return hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def bv_request(method: str, path: str, body=None, timeout=10):
    url = f"{BASE_URL}{path}"
    ts  = str(int(time.time() * 1000))
    m = method.upper()
    body_str = "" if m in ("GET","DELETE") else json.dumps(body or {}, separators=(',',':'))
    payload  = None if m in ("GET","DELETE") else (body or {})
    headers = {
        "Bitvavo-Access-Key": API_KEY,
        "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Signature": _sign(ts, m, f"/v2{path}", body_str),
        "Bitvavo-Access-Window": "10000",
        "Content-Type":"application/json",
    }
    r = requests.request(m, url, headers=headers, json=payload, timeout=timeout)
    try: return r.json()
    except: return {"error": r.text, "status_code": r.status_code}

# ===== Meta & Precision =====
MARKET_MAP, MARKET_META = {}, {}
def _count_decimals_of_step(step: float) -> int:
    s = f"{step:.16f}".rstrip("0").rstrip(".")
    return len(s.split(".",1)[1]) if "." in s else 0

def _parse_amount_precision(ap, min_base_hint: float|int=0) -> tuple[int,float]:
    try:
        mb = float(min_base_hint)
        if mb >= 1.0 and abs(mb-round(mb)) < 1e-9: return 0, 1.0
        v=float(ap)
        if float(v).is_integer():
            decs=max(0,int(v)); step=float(Decimal(1)/(Decimal(10)**decs)); return decs, step
        step=float(v); return _count_decimals_of_step(step), step
    except: return 8, 1e-8

def _parse_price_precision(pp) -> int:
    try:
        v=float(pp); return _count_decimals_of_step(v) if (not float(v).is_integer() or v<1.0) else int(v)
    except: return 6

def load_markets_once():
    global MARKET_MAP, MARKET_META
    if MARKET_MAP and MARKET_META: return
    rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
    m, meta = {}, {}
    for r in rows:
        if r.get("quote")!="EUR": continue
        market=r.get("market"); base=(r.get("base") or "").upper()
        if not base or not market: continue
        min_quote=float(r.get("minOrderInQuoteAsset",0) or 0.0)
        min_base=float(r.get("minOrderInBaseAsset",0) or 0.0)
        price_dec=_parse_price_precision(r.get("pricePrecision",6))
        amt_dec, st=_parse_amount_precision(r.get("amountPrecision",8), min_base)
        meta[market]={"priceDecimals":price_dec,"amountDecimals":amt_dec,"step":float(st),
                      "minQuote":min_quote,"minBase":min_base}
        m[base]=market
    MARKET_MAP, MARKET_META=m, meta

def coin_to_market(coin:str)->str|None:
    load_markets_once(); return MARKET_MAP.get((coin or "").upper())

def price_decimals(market:str)->int: return int(MARKET_META.get(market,{}).get("priceDecimals",6))
def amount_decimals(market:str)->int: return int(MARKET_META.get(market,{}).get("amountDecimals",8))
def step(market:str)->float: return float(MARKET_META.get(market,{}).get("step",1e-8))
def min_base(market:str)->float: return float(MARKET_META.get(market,{}).get("minBase",0.0))

def fmt_price(market:str, price: float|Decimal)->str:
    q = Decimal(10)**-price_decimals(market)
    s = f"{(Decimal(str(price))).quantize(q, rounding=ROUND_DOWN):f}"
    if "." in s:
        w,f = s.split(".",1); f=f[:price_decimals(market)].rstrip("0")
        return w if not f else f"{w}.{f}"
    return s

def round_amount_down(market:str, amount: float|Decimal)->float:
    decs=amount_decimals(market); st=Decimal(str(step(market) or 0))
    a = Decimal(str(amount)).quantize(Decimal(10)**-decs, rounding=ROUND_DOWN)
    if st>0: a=(a//st)*st
    return float(a)

def fmt_amount(market:str, amount: float|Decimal)->str:
    q = Decimal(10)**-amount_decimals(market)
    s = f"{(Decimal(str(amount))).quantize(q, rounding=ROUND_DOWN):f}"
    if "." in s:
        w,f = s.split(".",1); f=f[:amount_decimals(market)].rstrip("0")
        return w if not f else f"{w}.{f}"
    return s

# ===== دفتر أوامر (Redis→HTTP) =====
def get_best_bid_ask(market: str) -> tuple[float,float]:
    now_ms = int(time.time()*1000)
    if R and PRICE_SOURCE in ("redis_only","redis_http"):
        h = R.hgetall(f"{BOOK_HASH_NS}:{market}") or {}
        try:
            bid=float(h.get("bid","0") or 0); ask=float(h.get("ask","0") or 0); ts=int(h.get("ts","0") or 0)
            if bid>0 and ask>0 and (now_ms-ts) <= 2000:
                return bid, ask
        except: pass
        if PRICE_SOURCE=="redis_only": return 0.0, 0.0
    ob = requests.get(f"{BASE_URL}/{market}/book?depth=1", timeout=8).json()
    bid=float(ob["bids"][0][0]); ask=float(ob["asks"][0][0])
    if R:
        try: R.hset(f"{BOOK_HASH_NS}:{market}", mapping={"bid":str(bid),"ask":str(ask),"ts":str(now_ms)})
        except: pass
    return bid, ask

# ===== أوامر (place / cancel / status / balance) =====
def place_limit_postonly(market:str, side:str, price:float, amount:float):
    body = {
        "market": market, "side": side, "orderType":"limit", "postOnly": True,
        "clientOrderId": str(uuid4()),
        "price": fmt_price(market, price),
        "amount": fmt_amount(market, amount),
        "operatorId": ""
    }
    ts=str(int(time.time()*1000))
    sig=_sign(ts,"POST","/v2/order", json.dumps(body, separators=(',',':')))
    headers={"Bitvavo-Access-Key":API_KEY,"Bitvavo-Access-Timestamp":ts,"Bitvavo-Access-Signature":sig,
             "Bitvavo-Access-Window":"10000","Content-Type":"application/json"}
    r = requests.post(f"{BASE_URL}/order", headers=headers, json=body, timeout=10)
    try: data=r.json()
    except: data={"error": r.text}
    # إصلاح "postOnly → taker": لو أرجع خطأ postOnly، نزّح السعر بـ tick
    if isinstance(data,dict) and isinstance(data.get("error"),str) and "postOnly" in data["error"]:
        tick = float(Decimal(1) / (Decimal(10)**price_decimals(market)))
        adj = price - tick if side=="buy" else price + tick
        body["price"] = fmt_price(market, adj)
        r = requests.post(f"{BASE_URL}/order", headers=headers, json=body, timeout=10)
        try: data=r.json()
        except: data={"error": r.text}
    return body, data

def cancel_order_blocking(market:str, orderId:str, wait_sec:float=12.0):
    deadline=time.time()+max(wait_sec,6.0)
    _ = bv_request("DELETE", f"/order?market={market}&orderId={orderId}")
    last={}
    while time.time()<deadline:
        st=bv_request("GET", f"/order?market={market}&orderId={orderId}"); last=st
        s=(st or {}).get("status","").lower()
        if s in ("canceled","filled"): return True, s, st
        time.sleep(0.4)
    return False, (last or {}).get("status","unknown"), last

def order_status(market:str, orderId:str)->dict:
    return bv_request("GET", f"/order?market={market}&orderId={orderId}") or {}

def balance(symbol:str)->float:
    bals=bv_request("GET","/balance")
    if isinstance(bals, list):
        for b in bals:
            if b.get("symbol")==symbol.upper():
                return float(b.get("available",0) or 0.0)
    return 0.0

def emergency_taker_sell(market:str, amount:float):
    bid,_=get_best_bid_ask(market)
    price=max(0.0, bid*(1-0.001))
    body={
        "market":market,"side":"sell","orderType":"limit","postOnly":False,"clientOrderId":str(uuid4()),
        "price": fmt_price(market, price),
        "amount": fmt_amount(market, round_amount_down(market, amount)),
        "operatorId":""
    }
    ts=str(int(time.time()*1000))
    sig=_sign(ts,"POST","/v2/order", json.dumps(body, separators=(',',':')))
    headers={"Bitvavo-Access-Key":API_KEY,"Bitvavo-Access-Timestamp":ts,"Bitvavo-Access-Signature":sig,
             "Bitvavo-Access-Window":"10000","Content-Type":"application/json"}
    r=requests.post(f"{BASE_URL}/order", headers=headers, json=body, timeout=10)
    try: data=r.json()
    except: data={"error": r.text}
    return {"ok": not bool((data or {}).get("error")), "request": body, "response": data}

# ===== State (Redis) =====
def _key(ns, market): return f"{ns}:{market}"

def pos_get(market:str)->dict:
    if not R: return {}
    d=R.hgetall(_key(SESSION_NS, market)) or {}
    out={}
    for k,v in d.items():
        try: out[k]=json.loads(v)
        except: out[k]=v
    return out

def pos_set(market:str, pos:dict):
    if not R: return
    R.hset(_key(SESSION_NS, market), mapping={k: json.dumps(v) for k,v in pos.items()})

def pos_clear(market:str):
    if R: R.delete(_key(SESSION_NS, market))

def open_set(market:str, info:dict):
    if R: R.hset(_key(OPEN_NS, market), mapping={k: json.dumps(v) for k,v in info.items()})

def open_get(market:str)->dict:
    if not R: return {}
    d=R.hgetall(_key(OPEN_NS, market)) or {}
    out={}
    for k,v in d.items():
        try: out[k]=json.loads(v)
        except: out[k]=v
    return out

def open_clear(market:str):
    if R: R.delete(_key(OPEN_NS, market))

# ===== Ready Notify =====
def notify_ready(market:str, reason:str, pnl_eur=None):
    if not ABUSIYAH_READY_URL:
        tg_send(f"ready — {market} ({reason})"); return
    headers={"Content-Type":"application/json"}
    if LINK_SECRET: headers["X-Link-Secret"]=LINK_SECRET
    coin=market.split("-")[0]
    try:
        requests.post(ABUSIYAH_READY_URL,
                      json={"coin":coin,"reason":reason,"pnl_eur":pnl_eur},
                      headers=headers, timeout=6)
    except Exception as e:
        tg_send(f"⚠️ فشل نداء ready: {e}")

# ===== Helpers: average sell & PnL =====
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
            total_b += a; total_q += a * p
        except Exception:
            continue
    return ((total_q / total_b), total_b) if (total_b > 0 and total_q > 0) else (0.0, 0.0)

def _recent_sell_avg(market: str, lookback_sec: int = 45, want_base: float | None = None) -> tuple[float, float]:
    now_ms = int(time.time() * 1000)
    trades = bv_request("GET", f"/trades?market={market}&limit=50")
    if not isinstance(trades, list): 
        return 0.0, 0.0
    total_b = 0.0; total_q = 0.0
    for t in trades:
        try:
            if (t.get("side","").lower() != "sell"): 
                continue
            ts = int(t.get("timestamp", 0) or 0)
            if now_ms - ts > lookback_sec * 1000:
                continue
            a = float(t.get("amount", 0) or 0)
            p = float(t.get("price", 0) or 0)
            total_b += a; total_q += a * p
        except Exception:
            continue
    if total_b > 0 and total_q > 0:
        if want_base is not None and want_base > 0:
            if abs(total_b - want_base) / want_base > 0.1:
                pass
        return (total_q / total_b), total_b
    return 0.0, 0.0

def _send_sale_notifications(market: str, avg_in: float, sold_base: float, sell_avg: float, reason: str):
    if sell_avg <= 0 or sold_base <= 0 or avg_in <= 0:
        tg_send(f"💬 عملية بيع {market} ({reason}) — لا توجد بيانات كافية لحساب الربح/الخسارة.")
        notify_ready(market, reason=reason, pnl_eur=None)
        return
    pnl_eur = (sell_avg - avg_in) * sold_base
    pnl_pct = ((sell_avg / avg_in) - 1.0) * 100.0
    tg_send(
        f"💰 تم البيع — {market} ({reason})\n"
        f"🧾 Avg In: {avg_in:.8f}\n"
        f"🏷️ Avg Out: {sell_avg:.8f}\n"
        f"📦 Base: {sold_base}\n"
        f"📊 PnL: €{pnl_eur:.2f} ({pnl_pct:+.2f}%)"
    )
    notify_ready(market, reason=reason, pnl_eur=round(pnl_eur, 4))

# ===== CoreAPI واجهة =====
class CoreAPI:
    # اتصالات/أدوات
    tg_send = staticmethod(tg_send)
    notify_ready = staticmethod(notify_ready)
    bv_request = staticmethod(bv_request)

    # سوق/أوامر
    get_best_bid_ask = staticmethod(get_best_bid_ask)
    place_limit_postonly = staticmethod(place_limit_postonly)
    cancel_order_blocking = staticmethod(cancel_order_blocking)
    order_status = staticmethod(order_status)
    emergency_taker_sell = staticmethod(emergency_taker_sell)
    balance = staticmethod(balance)

    # Meta/Precision
    coin_to_market = staticmethod(coin_to_market)
    min_base = staticmethod(min_base)
    fmt_price = staticmethod(fmt_price)
    fmt_amount = staticmethod(fmt_amount)
    round_amount_down = staticmethod(round_amount_down)
    price_decimals = staticmethod(price_decimals)

    # State
    pos_get = staticmethod(pos_get); pos_set = staticmethod(pos_set); pos_clear = staticmethod(pos_clear)
    open_get= staticmethod(open_get); open_set= staticmethod(open_set); open_clear= staticmethod(open_clear)

    # ثوابت
    fee_rate = MAKER_FEE_RATE

CORE = CoreAPI()

# ===== Watchdog: يرصد TP/SL ويحسب PnL ويبلّغ =====
def start_watchdog():
    from strategy import maybe_move_sl  # قابل للتعديل
    def loop():
        while True:
            try:
                if not R:
                    time.sleep(1.0); continue
                for key in R.scan_iter(match=f"{SESSION_NS}:*"):
                    market = key.split(":", 2)[-1]
                    pos = pos_get(market)
                    if not pos: continue
                    base=float(pos.get("base") or 0); avg=float(pos.get("avg") or 0)
                    slp=float(pos.get("sl_price") or 0); tp_oid=pos.get("tp_oid")
                    # TP filled؟
                    if tp_oid:
                        st = order_status(market, tp_oid)
                        if (st or {}).get("status","").lower() == "filled":
                            sell_avg, sold_b = _avg_from_order_fills(st)
                            if sell_avg <= 0 or sold_b <= 0:
                                sell_avg, sold_b = _recent_sell_avg(market, lookback_sec=45, want_base=base)
                            _send_sale_notifications(market, avg, (sold_b or base), (sell_avg or 0.0), reason="tp_filled")
                            pos_clear(market)
                            continue
                    # تحديث SL ذكي (قفل ربح)
                    bid,_ = get_best_bid_ask(market)
                    new_sl = maybe_move_sl(CORE, market, avg, base, bid, slp)
                    if new_sl and new_sl>0 and (slp<=0 or new_sl>slp):
                        pos["sl_price"]=new_sl; pos_set(market, pos); tg_send(f"🔒 قفل ربح — SL={new_sl:.8f} ({market})")
                    # SL
                    if bid>0 and slp>0 and bid <= slp:
                        if tp_oid:
                            try: cancel_order_blocking(market, tp_oid, wait_sec=3.0)
                            except: pass
                        sell_res = emergency_taker_sell(market, base)
                        sell_avg, sold_b = _recent_sell_avg(market, lookback_sec=45, want_base=base)
                        if sell_avg <= 0 or sold_b <= 0:
                            sell_avg, sold_b = bid, base
                        _send_sale_notifications(market, avg, sold_b, sell_avg, reason=("sl_triggered" if sell_res.get("ok") else "sl_fail"))
                        pos_clear(market)
                        continue
                time.sleep(max(0.4, SL_CHECK_SEC))
            except Exception as e:
                print("watchdog err:", e); time.sleep(1.0)
    threading.Thread(target=loop, daemon=True).start()

# ===== Webhook & Telegram =====
COIN_RE = __import__("re").compile(r"^[A-Z0-9]{2,15}$")

@app.route("/tg", methods=["GET"])
def tg_alive(): return "OK /tg", 200

@app.route("/tg", methods=["POST"])
def tg_webhook():
    from strategy import on_tg_command
    upd = request.get_json(silent=True) or {}
    msg  = upd.get("message") or upd.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    text = (msg.get("text") or "").strip()
    if not chat_id or not _auth_chat(chat_id) or not text:
        return jsonify(ok=True)
    try: on_tg_command(CORE, text)
    except Exception as e: tg_send(f"🐞 TG err: {type(e).__name__}: {e}")
    return jsonify(ok=True)

@app.route("/hook", methods=["POST"])
def abusiyah_hook():
    from strategy import on_hook_buy
    try:
        if LINK_SECRET and request.headers.get("X-Link-Secret","") != LINK_SECRET:
            return jsonify(ok=False, err="bad secret"), 401
        data = request.get_json(silent=True) or {}
        action=(data.get("action") or data.get("cmd") or "").lower().strip()
        coin  =(data.get("coin") or "").upper().strip()
        if action!="buy" or not COIN_RE.match(coin):
            return jsonify(ok=False, err="invalid_payload"), 400
        threading.Thread(target=lambda: on_hook_buy(CORE, coin), daemon=True).start()
        return jsonify(ok=True, msg="buy started"), 202
    except Exception as e:
        tg_send(f"🐞 hook error: {type(e).__name__}: {e}")
        return jsonify(ok=False, err=str(e)), 500

@app.route("/", methods=["GET"])
def home(): return f"Saqer — {__SAQER_CORE_VERSION__} ✅", 200

# ===== فحص واجهة strategy =====
def _check_strategy_interface():
    import strategy
    required = ["on_hook_buy","on_tg_command","chase_buy","maybe_move_sl"]
    missing = [f for f in required if not hasattr(strategy,f)]
    if missing:
        raise RuntimeError(f"strategy.py missing: {missing}")

if __name__ == "__main__":
    load_markets_once()
    _check_strategy_interface()
    start_watchdog()
    app.run(host="0.0.0.0", port=PORT)