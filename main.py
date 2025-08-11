# -*- coding: utf-8 -*-
import hmac
import hashlib
import os
import time
import requests
import json
import redis
from flask import Flask, request
from threading import Thread, Lock
from uuid import uuid4
from dotenv import load_dotenv

# =========================
# 📌 إعدادات يدوية (ثابتة هنا)
# =========================
BUY_AMOUNT_EUR = 15.0            # قيمة الشراء باليورو
MAX_TRADES = 2                   # الحد الأقصى للصفقات النشطة

# مرحلة A (0-10 دقائق)
PHASE_A_DURATION_SEC   = 600
PHASE_A_STOP_LOSS_PCT  = -2.0
PHASE_A_TRAIL_START    =  2.0
PHASE_A_BACKSTEP       =  0.5

# مرحلة B (>10 دقائق)
PHASE_B_STOP_LOSS_PCT  = -0.1
PHASE_B_TRAIL_START    =  1.0
PHASE_B_BACKSTEP       =  0.3

BLACKLIST_EXPIRE_SECONDS = 300   # حظر مؤقت بعد فشل شراء/بيع (ثوانٍ)
BUY_COOLDOWN_SEC = 600           # كولداون بعد الإغلاق لنفس العملة (10 د)

# =========================
# 🧠 التهيئة العامة
# =========================
load_dotenv()
app = Flask(__name__)
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BITVAVO_API_KEY = os.getenv("BITVAVO_API_KEY")
BITVAVO_API_SECRET = os.getenv("BITVAVO_API_SECRET")
r = redis.from_url(os.getenv("REDIS_URL"))
lock = Lock()

enabled = True
active_trades = []     # صفقات مفتوحة
executed_trades = []   # احتفاظ داخلي فقط (بدون ملخص)

# =========================
# 🔔 إرسال (مع منع تكرار)
# =========================
def send_message(text):
    try:
        key = "dedup:" + hashlib.sha1(text.encode("utf-8")).hexdigest()
        if r.setnx(key, 1):
            r.expire(key, 60)  # امنع تكرار نفس النص 60ث
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": text},
                timeout=8
            )
    except Exception as e:
        print("Telegram error:", e)

# =========================
# 🔐 التوقيع و الطلبات
# =========================
def create_signature(timestamp, method, path, body_str=""):
    msg = f"{timestamp}{method}{path}{body_str}"
    return hmac.new(BITVAVO_API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def bitvavo_request(method, path, body=None):
    timestamp = str(int(time.time() * 1000))
    url = f"https://api.bitvavo.com/v2{path}"

    if method == "GET":
        body_str = ""
        signature = create_signature(timestamp, method, f"/v2{path}", body_str)
        headers = {
            'Bitvavo-Access-Key': BITVAVO_API_KEY,
            'Bitvavo-Access-Timestamp': timestamp,
            'Bitvavo-Access-Signature': signature,
            'Bitvavo-Access-Window': '10000'
        }
        resp = requests.request(method, url, headers=headers, timeout=8)
    else:
        body_str = json.dumps(body or {}, separators=(',', ':'))
        signature = create_signature(timestamp, method, f"/v2{path}", body_str)
        headers = {
            'Bitvavo-Access-Key': BITVAVO_API_KEY,
            'Bitvavo-Access-Timestamp': timestamp,
            'Bitvavo-Access-Signature': signature,
            'Bitvavo-Access-Window': '10000'
        }
        resp = requests.request(method, url, headers=headers, json=body or {}, timeout=12)

    try:
        return resp.json()
    except Exception:
        return {"error": "invalid_json", "status_code": resp.status_code, "text": resp.text}

# =========================
# 💶 الأسعار (كاش خفيف)
# =========================
_price_cache = {"t": 0, "map": {}}
def fetch_price(market_symbol):
    # كاش 2 ثانية
    now = time.time()
    if now - _price_cache["t"] > 2:
        _price_cache["map"].clear()
        _price_cache["t"] = now
    if market_symbol in _price_cache["map"]:
        return _price_cache["map"][market_symbol]
    try:
        res = bitvavo_request("GET", f"/ticker/price?market={market_symbol}")
        price = float(res.get("price", 0) or 0)
        if price > 0:
            _price_cache["map"][market_symbol] = price
            return price
    except Exception:
        pass
    return None

# =========================
# ✅ العملات المدعومة (تحديث دوري)
# =========================
SUPPORTED_SYMBOLS = set()
_last_sym_refresh = 0

def ensure_symbols_fresh():
    global SUPPORTED_SYMBOLS, _last_sym_refresh
    now = time.time()
    if SUPPORTED_SYMBOLS and (now - _last_sym_refresh) < 600:
        return
    try:
        res = requests.get("https://api.bitvavo.com/v2/markets", timeout=8)
        data = res.json()
        SUPPORTED_SYMBOLS = set(
            m["market"].replace("-EUR", "").upper()
            for m in data if m.get("market", "").endswith("-EUR")
        )
        _last_sym_refresh = now
    except Exception as e:
        print("refresh symbols error:", e)

# =========================
# 🧮 تجميع قيم fills باليورو
# =========================
def totals_from_fills_eur(fills):
    total_base = 0.0
    total_eur  = 0.0
    fee_eur    = 0.0
    for f in (fills or []):
        amt   = float(f["amount"])
        price = float(f["price"])
        fee   = float(f.get("fee", 0) or 0)
        total_base += amt
        total_eur  += amt * price
        fee_eur    += fee  # feeCurrency = EUR
    return total_base, total_eur, fee_eur

# =========================
# 💱 البيع — صفقة-بصفقة (بدون رسائل وسطية)
# =========================
def sell_trade(trade):
    market = trade["symbol"]

    if r.exists(f"blacklist:sell:{market}"):
        return

    amt = float(trade.get("amount", 0) or 0)
    if amt <= 0:
        return

    # 🚨 صيغة البيع الثابتة - لا تُعدل 🚨
    body = {
        "market": market,
        "side": "sell",
        "orderType": "market",
        "amount": f"{amt:.10f}",
        "clientOrderId": str(uuid4()),
        "operatorId": ""
    }
    res = bitvavo_request("POST", "/order", body)

    if not isinstance(res, dict):
        r.setex(f"blacklist:sell:{market}", BLACKLIST_EXPIRE_SECONDS, 1)
        return

    if res.get("status") == "filled":
        fills = res.get("fills", [])
        avg_exit = 0.0
        total_amt = sum(float(f["amount"]) for f in fills) or 0.0
        if total_amt > 0:
            avg_exit = sum(float(f["amount"]) * float(f["price"]) for f in fills) / total_amt

        # إزالة من النشطة
        with lock:
            try:
                active_trades.remove(trade)
            except ValueError:
                pass

        # كولداون على الشراء لنفس الرمز
        base = market.replace("-EUR", "")
        r.setex(f"cooldown:{base}", BUY_COOLDOWN_SEC, 1)

        # إشعار بيع نهائي فقط
        send_message(f"✅ تم البيع {base} @ €{avg_exit:.6f}")
    else:
        r.setex(f"blacklist:sell:{market}", BLACKLIST_EXPIRE_SECONDS, 1)

# =========================
# 🛒 الشراء (لا استبدال ولا رسائل إلا عند التنفيذ)
# =========================
def buy(symbol):
    ensure_symbols_fresh()
    symbol = symbol.upper().strip()
    if symbol not in SUPPORTED_SYMBOLS:
        return  # أخرس

    if r.exists(f"cooldown:{symbol}"):
        return  # أخرس

    with lock:
        if any(t["symbol"] == f"{symbol}-EUR" for t in active_trades):
            return  # أخرس
        if len(active_trades) >= MAX_TRADES:
            return  # أخرس (لا استبدال)

    market = f"{symbol}-EUR"

    # شراء ماركت
    body = {
        "market": market,
        "side": "buy",
        "orderType": "market",
        "amountQuote": f"{BUY_AMOUNT_EUR:.2f}",
        "clientOrderId": str(uuid4()),
        "operatorId": ""
    }
    res = bitvavo_request("POST", "/order", body)

    if isinstance(res, dict) and res.get("status") == "filled":
        fills = res.get("fills", [])
        tb, tq_eur, fee_eur = totals_from_fills_eur(fills)
        amount_net = tb
        cost_eur   = tq_eur + fee_eur  # التكلفة الصافية €

        if amount_net <= 0 or cost_eur <= 0:
            r.setex(f"blacklist:buy:{symbol}", BLACKLIST_EXPIRE_SECONDS, 1)
            return

        avg_price_incl_fees = cost_eur / amount_net

        trade = {
            "symbol": market,
            "entry": avg_price_incl_fees,
            "amount": amount_net,
            "cost_eur": cost_eur,
            "timestamp": time.time(),
            "max_profit": 0.0
        }

        with lock:
            active_trades.append(trade)
            executed_trades.append(trade.copy())  # احتفاظ داخلي فقط

        # إشعار شراء نهائي فقط
        send_message(f"✅ تم الشراء {symbol} | كمية: {amount_net:.6f} @ €{avg_price_incl_fees:.6f}")
    else:
        r.setex(f"blacklist:buy:{symbol}", BLACKLIST_EXPIRE_SECONDS, 1)
        return  # أخرس

# =========================
# 👀 حلقة المراقبة — مرحلتين، بدون رسائل وسطية
# =========================
def monitor_loop():
    while True:
        try:
            with lock:
                snapshot = list(active_trades)

            for trade in snapshot:
                market = trade["symbol"]           # e.g. 'ADA-EUR'
                entry = trade["entry"]
                current = fetch_price(market)
                if not current:
                    continue

                profit = ((current - entry) / entry) * 100.0

                # تحديث أعلى ربح
                if profit > trade.get("max_profit", 0.0):
                    trade["max_profit"] = profit

                age = time.time() - trade["timestamp"]

                # مرحلة A
                if age <= PHASE_A_DURATION_SEC:
                    if profit <= PHASE_A_STOP_LOSS_PCT:
                        sell_trade(trade)
                        continue
                    if profit >= PHASE_A_TRAIL_START:
                        if profit <= trade["max_profit"] - PHASE_A_BACKSTEP:
                            sell_trade(trade)
                            continue
                # مرحلة B
                else:
                    if profit <= PHASE_B_STOP_LOSS_PCT:
                        sell_trade(trade)
                        continue
                    if profit >= PHASE_B_TRAIL_START:
                        if profit <= trade["max_profit"] - PHASE_B_BACKSTEP:
                            sell_trade(trade)
                            continue

        except Exception as e:
            print("Monitor loop error:", e)
        time.sleep(2)

# =========================
# 🌐 Webhook (يدعم أي مصدر + تيليجرام)
# =========================
@app.route("/", methods=["POST"])
@app.route("/webhook", methods=["POST"])
def webhook():
    global enabled
    data = request.json or {}
    txt = (data.get("message", {}).get("text") or "").strip().lower()

    if txt.startswith("اشتري"):
        symbol = txt.split("اشتري", 1)[-1].strip().upper()
        if enabled and symbol:
            Thread(target=buy, args=(symbol,)).start()
        return "ok"

    elif txt == "ابدأ":
        enabled = True
        send_message("✅ تم تفعيل الشراء.")
        return "ok"

    elif txt == "قف":
        enabled = False
        send_message("🛑 تم إيقاف الشراء.")
        return "ok"

    return "ok"

# =========================
# 🚀 التشغيل
# =========================
if __name__ == "__main__":
    Thread(target=monitor_loop, daemon=True).start()
    # على Railway يشتغل عبر gunicorn، ولو محليًا:
    if os.getenv("RUN_LOCAL") == "1":
        app.run(host="0.0.0.0", port=5000)