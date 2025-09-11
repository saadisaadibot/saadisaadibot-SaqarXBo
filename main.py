# -*- coding: utf-8 -*-
"""
Saqer — Telegram Maker (BUY/SELL all) — No Domain Needed
- وضع افتراضي: Telegram Long-Polling (لا يحتاج أي دومين/URL).
- أوامر:
  /buy  COIN | COIN-EUR  → BUY maker بكل رصيد EUR (مع هامش أمان)
  /sell COIN | COIN-EUR  → SELL maker لكل رصيد العملة الأساسية
  /bal                    → عرض الأرصدة
  /help                   → مساعدة
- دقة كاملة: minQuote/minBase + tick/step قبل إرسال الطلب إلى Bitvavo.
- (اختياري) Webhook mode: إذا ضبطت MODE=WEBHOOK و WEBHOOK_URL.
"""

import os, re, time, hmac, hashlib, json, math, requests, threading
from uuid import uuid4
from flask import Flask, request, jsonify

# ========= ENV =========
BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
CHAT_ID     = os.getenv("CHAT_ID", "")   # اختياري: قصر الأوامر على دردشة واحدة
API_KEY     = os.getenv("BITVAVO_API_KEY", "")
API_SECRET  = os.getenv("BITVAVO_API_SECRET", "")
BASE_URL    = "https://api.bitvavo.com/v2"

# تشغيل بدون دومين (افتراضي): POLL | أو WEBHOOK لو بدك
MODE        = os.getenv("MODE", "POLL").upper()  # POLL أو WEBHOOK
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")       # استخدمه فقط لو MODE=WEBHOOK

PORT        = int(os.getenv("PORT", "8080"))

# هامش أمان بسيط (رسوم/تقريب)
BUFFER_EUR_MIN   = float(os.getenv("BUFFER_EUR_MIN", "0.30"))
EST_FEE_RATE     = float(os.getenv("EST_FEE_RATE",   "0.0025"))  # ~0.25%
BUFFER_FEE_MULT  = float(os.getenv("BUFFER_FEE_MULT","2.0"))

# ========= Flask (لـ webhook أو health فقط) =========
app = Flask(__name__)

# ========= Telegram =========
def tg_send(chat_id: str, text: str):
    if not BOT_TOKEN:
        print(f"TG[{chat_id}]: {text}")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": chat_id, "text": text}, timeout=8)
    except Exception as e:
        print("tg_send err:", e)

def _auth_chat(chat_id: str) -> bool:
    return (not CHAT_ID) or (str(chat_id) == str(CHAT_ID))

# ========= Bitvavo signing/request =========
def _sign_headers(method: str, path: str, body_str: str = "") -> dict:
    ts  = str(int(time.time() * 1000))
    msg = ts + method + ("/v2" + path) + body_str
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {
        "Bitvavo-Access-Key": API_KEY,
        "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Signature": sig,
        "Bitvavo-Access-Window": "10000",
        "Content-Type": "application/json",
    }

def _bv(method: str, path: str, body: dict | None = None, timeout=12):
    # public endpoints
    if path.startswith("/markets") or path.endswith("/book") or path.startswith("/ticker"):
        url = f"{BASE_URL}{path}"
        if method == "GET":
            r = requests.get(url, timeout=timeout)
        else:
            r = requests.post(url, data=json.dumps(body or {}, separators=(',',':')), timeout=timeout)
        try: return r.json()
        except: return {"status": r.status_code, "raw": r.text}

    # signed endpoints
    body_str = "" if method == "GET" else json.dumps(body or {}, separators=(',',':'))
    headers  = _sign_headers(method, path, body_str)
    url      = f"{BASE_URL}{path}"
    if method == "GET":
        r = requests.get(url, headers=headers, timeout=timeout)
    else:
        r = requests.post(url, headers=headers, data=body_str, timeout=timeout)
    try: return r.json()
    except: return {"status": r.status_code, "raw": r.text}

# ========= Market meta =========
MARKET_META = {}  # "GMX-EUR": {"tick","step","minQuote","minBase"}

def _parse_step_from_precision(val, default_step):
    try:
        if isinstance(val, int) or (isinstance(val, str) and str(val).isdigit()):
            d = int(val); 
            if 0 <= d <= 20: return 10.0 ** (-d)
        v = float(val)
        if 0 < v < 1: return v
    except: pass
    return default_step

def load_markets_once():
    global MARKET_META
    if MARKET_META: return
    try:
        rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
        for r in rows:
            if r.get("quote") != "EUR": continue
            m = r.get("market")
            tick = _parse_step_from_precision(r.get("pricePrecision", 6), 1e-6)
            step = _parse_step_from_precision(r.get("amountPrecision", 8), 1e-8)
            MARKET_META[m] = {
                "tick":     tick or 1e-6,
                "step":     step or 1e-8,
                "minQuote": float(r.get("minOrderInQuoteAsset", 0) or 0.0),
                "minBase":  float(r.get("minOrderInBaseAsset",  0) or 0.0),
            }
    except Exception as e:
        print("load_markets error:", e)

def _meta(market): return MARKET_META.get(market) or {}
def _tick(market): v=_meta(market).get("tick",1e-6); return v if (v and v>0) else 1e-6
def _step(market): v=_meta(market).get("step",1e-8); return v if (v and v>0) else 1e-8

def _decs(step: float) -> int:
    s = ("%.16f" % float(step)).rstrip("0").rstrip(".")
    return len(s.split(".")[1]) if "." in s else 0

def round_price_down(market, price):
    tk=_tick(market); dec=_decs(tk)
    p=math.floor(max(0.0,float(price))/tk)*tk
    return round(max(tk,p), dec)

def round_amount_down(market, amount):
    st=_step(market); dec=_decs(st)
    a=math.floor(max(0.0,float(amount))/st)*st
    return round(max(st,a), dec)

def min_required_quote_eur(market: str, price: float) -> float:
    meta=_meta(market)
    return max(float(meta.get("minQuote",0.0)), float(meta.get("minBase",0.0))*max(0.0,float(price)))

# ========= Balance & Orderbook =========
def get_eur_available() -> float:
    j = _bv("GET", "/balance")
    if isinstance(j, list):
        for b in j:
            if b.get("symbol")=="EUR":
                return float(b.get("available",0) or 0.0)
    return 0.0

def get_base_available(base: str) -> float:
    j = _bv("GET", "/balance")
    if isinstance(j, list):
        for b in j:
            if b.get("symbol")==base.upper():
                return float(b.get("available",0) or 0.0)
    return 0.0

def fetch_best_bid_ask(market: str):
    try:
        j = requests.get(f"{BASE_URL}/{market}/book?depth=1", timeout=6).json()
        bid = float(j["bids"][0][0]); ask = float(j["asks"][0][0])
        return bid, ask
    except Exception:
        return None, None

# ========= Maker BUY/SELL =========
def maker_buy_all_eur(market: str):
    load_markets_once()
    meta = _meta(market)
    if not meta: return {"ok": False, "err": f"market_meta_missing:{market}"}
    bid, ask = fetch_best_bid_ask(market)
    if not bid or not ask: return {"ok": False, "err": "no_orderbook"}

    eur_avail = get_eur_available()
    buffer_eur = max(BUFFER_EUR_MIN, eur_avail * EST_FEE_RATE * BUFFER_FEE_MULT)
    eur_amount = max(0.0, eur_avail - buffer_eur)
    if eur_amount <= 0:
        return {"ok": False, "err": "insufficient_eur", "eur_avail": eur_avail, "buffer": buffer_eur}

    raw_price = min(bid, ask*(1.0-1e-6))
    price     = round_price_down(market, raw_price)

    need_eur  = min_required_quote_eur(market, price)
    if eur_amount + 1e-12 < need_eur:
        return {"ok": False, "err": "below_min_order",
                "need_eur": float(f"{need_eur:.6f}"),
                "eur_after_buffer": float(f"{eur_amount:.6f}")}

    amount    = round_amount_down(market, eur_amount / price)
    quote_val = amount * price
    if (amount + 1e-12) < meta.get("minBase", 0.0) or (quote_val + 1e-12) < meta.get("minQuote", 0.0):
        need2 = max(meta.get("minQuote",0.0), meta.get("minBase",0.0)*price)
        return {"ok": False, "err": "below_min_after_round",
                "need_eur": float(f"{need2:.6f}"),
                "quote_after": float(f"{quote_val:.8f}"),
                "amount_after": float(f"{amount:.8f}")}

    body = {
        "market": market,
        "side": "buy",
        "orderType": "limit",
        "postOnly": True,
        "clientOrderId": str(uuid4()),
        "price":  f"{price:.{_decs(_tick(market))}f}",
        "amount": f"{amount:.{_decs(_step(market))}f}",
        "operatorId": ""
    }
    j = _bv("POST", "/order", body)
    if (j or {}).get("orderId"):
        return {"ok": True, "request": body, "response": j}
    return {"ok": False, "request": body, "response": j, "err": (j or {}).get("error","unknown")}

def maker_sell_all_base(market: str):
    load_markets_once()
    meta = _meta(market)
    if not meta: return {"ok": False, "err": f"market_meta_missing:{market}"}
    bid, ask = fetch_best_bid_ask(market)
    if not bid or not ask: return {"ok": False, "err": "no_orderbook"}

    base = market.split("-")[0]
    base_avail = get_base_available(base)
    if base_avail <= 0:
        return {"ok": False, "err": "insufficient_base", "base": base, "available": base_avail}

    raw_price = max(ask, bid*(1.0+1e-6))
    price     = round_price_down(market, raw_price)
    amount    = round_amount_down(market, base_avail)

    quote_val = amount * price
    if (amount + 1e-12) < meta.get("minBase", 0.0) or (quote_val + 1e-12) < meta.get("minQuote", 0.0):
        need2 = max(meta.get("minQuote",0.0), meta.get("minBase",0.0)*price)
        return {"ok": False, "err": "below_min_after_round",
                "need_eur": float(f"{need2:.6f}"),
                "quote_after": float(f"{quote_val:.8f}"),
                "amount_after": float(f"{amount:.8f}")}

    body = {
        "market": market,
        "side": "sell",
        "orderType": "limit",
        "postOnly": True,
        "clientOrderId": str(uuid4()),
        "price":  f"{price:.{_decs(_tick(market))}f}",
        "amount": f"{amount:.{_decs(_step(market))}f}",
        "operatorId": ""
    }
    j = _bv("POST", "/order", body)
    if (j or {}).get("orderId"):
        return {"ok": True, "request": body, "response": j}
    return {"ok": False, "request": body, "response": j, "err": (j or {}).get("error","unknown")}

# ========= Command parsing =========
COIN_RE = re.compile(r"^[A-Z0-9]{2,15}$")

def _norm_market(arg: str) -> str | None:
    s = (arg or "").strip().upper()
    if not s: return None
    if s.endswith("-EUR") and COIN_RE.match(s.split("-")[0]): return s
    if COIN_RE.match(s): return f"{s}-EUR"
    return None

def handle_buy(chat_id: str, arg: str):
    market = _norm_market(arg)
    if not market:
        tg_send(chat_id, "❌ استخدم: /buy COIN أو /buy COIN-EUR"); return
    res = maker_buy_all_eur(market)
    if res.get("ok"):
        oid = res["response"]["orderId"]
        tg_send(chat_id, f"✅ BUY {market} (maker) — orderId: {oid}")
    else:
        tg_send(chat_id, f"⚠️ BUY فشل: {res.get('err')} | {json.dumps(res, ensure_ascii=False)}")

def handle_sell(chat_id: str, arg: str):
    market = _norm_market(arg)
    if not market:
        tg_send(chat_id, "❌ استخدم: /sell COIN أو /sell COIN-EUR"); return
    res = maker_sell_all_base(market)
    if res.get("ok"):
        oid = res["response"]["orderId"]
        tg_send(chat_id, f"✅ SELL {market} (maker) — orderId: {oid}")
    else:
        tg_send(chat_id, f"⚠️ SELL فشل: {res.get('err')} | {json.dumps(res, ensure_ascii=False)}")

def handle_bal(chat_id: str):
    j = _bv("GET","/balance")
    if not isinstance(j, list):
        tg_send(chat_id, f"⚠️ balance err: {j}"); return
    lines=[]; eur=0.0
    for b in j:
        sym=b.get("symbol"); avail=float(b.get("available",0) or 0.0)
        if avail<=0: continue
        if sym=="EUR": eur=avail
        else: lines.append(f"• {sym}: {avail:.8f}")
    lines.sort()
    lines.insert(0, f"EUR: {eur:.2f}")
    tg_send(chat_id, "📊 Balances:\n" + "\n".join(lines) if lines else f"📊 EUR: {eur:.2f}")

def handle_help(chat_id: str):
    tg_send(chat_id,
        "أوامر:\n"
        "/buy  COIN | COIN-EUR  — شراء Maker بكل رصيد EUR\n"
        "/sell COIN | COIN-EUR  — بيع Maker لكل رصيد العملة\n"
        "/bal                  — عرض الأرصدة\n"
        "/help                 — هذه المساعدة"
    )

# ========= Telegram: Webhook (اختياري) =========
@app.route("/tg", methods=["POST"])
def tg_webhook():
    try:
        upd = request.get_json(silent=True) or {}
        _handle_update(upd)
        return jsonify(ok=True)
    except Exception as e:
        print("tg webhook err:", e)
        return jsonify(ok=True)

@app.route("/", methods=["GET"])
def home():
    return "Saqer Telegram Maker (poll/webhook) ✅"

# ========= Telegram: Polling (بدون دومين) =========
last_update_id = 0
def _handle_update(upd: dict):
    msg = upd.get("message") or upd.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    if not chat_id: return
    if not _auth_chat(chat_id):
        tg_send(chat_id, "⛔ غير مصرّح."); return

    text = (msg.get("text") or "").strip()
    if not text: return
    low = text.lower()

    if low.startswith("/buy"):
        arg = text.split(maxsplit=1)[1] if len(text.split())>1 else ""
        handle_buy(chat_id, arg); return
    if low.startswith("/sell"):
        arg = text.split(maxsplit=1)[1] if len(text.split())>1 else ""
        handle_sell(chat_id, arg); return
    if low.startswith("/bal"):
        handle_bal(chat_id); return
    if low.startswith("/help") or low.startswith("/start"):
        handle_help(chat_id); return

    tg_send(chat_id, "أوامر: /buy /sell /bal /help")

def polling_loop():
    global last_update_id
    if not BOT_TOKEN:
        print("⚠️ BOT_TOKEN مفقود — polling لن يعمل."); return
    tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"timeout": 25}
    while True:
        try:
            if last_update_id:
                params["offset"] = last_update_id + 1
            r = requests.get(tg_url, params=params, timeout=35).json()
            if not r.get("ok"): time.sleep(1); continue
            for upd in r.get("result", []):
                last_update_id = max(last_update_id, upd.get("update_id", 0))
                _handle_update(upd)
        except Exception as e:
            print("poll err:", e)
            time.sleep(2)

# ========= Main =========
if __name__ == "__main__":
    load_markets_once()
    if MODE == "WEBHOOK" and WEBHOOK_URL and BOT_TOKEN:
        try:
            # ضبط ويبهوك (اختياري)
            resp = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                params={"url": WEBHOOK_URL+"/tg"}, timeout=8
            ).json()
            print("setWebhook:", resp)
        except Exception as e:
            print("setWebhook err:", e)
    else:
        # وضع بدون دومين: polling thread
        threading.Thread(target=polling_loop, daemon=True).start()

    app.run(host="0.0.0.0", port=PORT)