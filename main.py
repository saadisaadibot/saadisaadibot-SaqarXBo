import hmac
import hashlib
import os
import time
import requests
import json
import redis
from flask import Flask, request
from threading import Thread
from uuid import uuid4
from dotenv import load_dotenv

# 📌 إعدادات قابلة للتعديل
load_dotenv()
BUY_AMOUNT_EUR = float(os.getenv("BUY_AMOUNT_EUR", 10))
MAX_TRADES = int(os.getenv("MAX_ACTIVE_TRADES", 2))
TRAIL_START = 2
TRAIL_BACKSTEP = 0.5
STOP_LOSS = -1.8
BLACKLIST_EXPIRE_SECONDS = int(os.getenv("BLACKLIST_EXPIRE_SECONDS", 300))

# 🧠 تهيئة المتغيرات
app = Flask(__name__)
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BITVAVO_API_KEY = os.getenv("BITVAVO_API_KEY")
BITVAVO_API_SECRET = os.getenv("BITVAVO_API_SECRET")
r = redis.from_url(os.getenv("REDIS_URL"))

enabled = True
max_trades = MAX_TRADES
active_trades = []
executed_trades = []

# ✅ جلب العملات المدعومة من Bitvavo
def load_supported_symbols():
    try:
        res = requests.get("https://api.bitvavo.com/v2/markets")
        data = res.json()
        return set(m["market"].replace("-EUR", "").upper() for m in data if m["market"].endswith("-EUR"))
    except:
        return set()

SUPPORTED_SYMBOLS = load_supported_symbols()

# 📦 تحميل الصفقات من Redis
try:
    at = r.get("nems:active_trades")
    if at:
        active_trades = json.loads(at)
    et = r.lrange("nems:executed_trades", 0, -1)
    executed_trades = [json.loads(t) for t in et]
except:
    pass

def send_message(text):
    print(">>", text)
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data={
            "chat_id": CHAT_ID, "text": text
        })
    except:
        pass

def create_signature(timestamp, method, path, body):
    body_str = json.dumps(body, separators=(',', ':')) if body else ""
    msg = f"{timestamp}{method}{path}{body_str}"
    return hmac.new(BITVAVO_API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def bitvavo_request(method, path, body=None):
    timestamp = str(int(time.time() * 1000))
    signature = create_signature(timestamp, method, f"/v2{path}", body)
    headers = {
        'Bitvavo-Access-Key': BITVAVO_API_KEY,
        'Bitvavo-Access-Timestamp': timestamp,
        'Bitvavo-Access-Signature': signature,
        'Bitvavo-Access-Window': '10000'
    }
    try:
        response = requests.request(method, f"https://api.bitvavo.com/v2{path}", headers=headers, json=body or {})
        return response.json()
    except Exception as e:
        return {"error": str(e)}

def fetch_price(symbol):
    try:
        res = bitvavo_request("GET", f"/ticker/price?market={symbol}")
        return float(res.get("price", 0))
    except:
        return None

def buy(symbol):
    if symbol not in SUPPORTED_SYMBOLS:
        send_message(f"❌ العملة {symbol} غير مدعومة على Bitvavo.")
        return

    if r.exists(f"blacklist:buy:{symbol}"):
        return

    if len(active_trades) >= max_trades:
        weakest = None
        lowest_pnl = float('inf')
        for trade in active_trades:
            current = fetch_price(trade["symbol"])
            if not current: continue
            pnl = ((current - trade["entry"]) / trade["entry"]) * 100
            if pnl < lowest_pnl:
                lowest_pnl = pnl
                weakest = trade
        if weakest:
            send_message(f"♻️ استبدال أضعف صفقة: {weakest['symbol']} (ربح {lowest_pnl:.2f}%)")
            sell(weakest["symbol"], weakest["entry"])
            active_trades.remove(weakest)
            r.set("nems:active_trades", json.dumps(active_trades))
        else:
            send_message("❌ لا يمكن تنفيذ الاستبدال.")
            return

    body = {
        "market": f"{symbol}-EUR",
        "side": "buy",
        "orderType": "market",
        "amountQuote": f"{BUY_AMOUNT_EUR:.2f}",
        "clientOrderId": str(uuid4()),
        "operatorId": ""
    }

    res = bitvavo_request("POST", "/order", body)

    if isinstance(res, dict) and res.get("status") == "filled":
        fills = res.get("fills", [])
        total_amount = sum(float(f["amount"]) for f in fills)
        total_price = sum(float(f["amount"]) * float(f["price"]) for f in fills)
        avg_price = total_price / total_amount if total_amount > 0 else 0

        if total_amount == 0 or avg_price == 0:
            send_message(f"❌ فشل شراء {symbol} - السعر أو الكمية غير صالحة")
            r.setex(f"blacklist:buy:{symbol}", 1, BLACKLIST_EXPIRE_SECONDS)
            return

        trade = {
            "symbol": f"{symbol}-EUR",
            "entry": avg_price,
            "amount": total_amount,
            "trail": avg_price,
            "max_profit": 0
        }

        active_trades.append(trade)
        executed_trades.append(trade)
        r.set("nems:active_trades", json.dumps(active_trades))
        r.rpush("nems:executed_trades", json.dumps(trade))
        send_message(f"✅ شراء {symbol} بسعر {avg_price:.10f}")
    else:
        r.setex(f"blacklist:buy:{symbol}", 1, BLACKLIST_EXPIRE_SECONDS)
        send_message(f"❌ فشل شراء {symbol}")

def sell(symbol, entry):
    if r.exists(f"blacklist:sell:{symbol}"):
        return

    balances = bitvavo_request("GET", "/balance")
    base = symbol.replace("-EUR", "")
    amount = next((float(b["available"]) for b in balances if b["symbol"] == base), 0)

    if amount < 0.0001:
        r.setex(f"blacklist:sell:{symbol}", 1, BLACKLIST_EXPIRE_SECONDS)
        return

    body = {
        "market": symbol,
        "side": "sell",
        "orderType": "market",
        "amount": str(amount),
        "clientOrderId": str(uuid4()),
        "operatorId": ""
    }

    res = bitvavo_request("POST", "/order", body)
    if isinstance(res, dict) and res.get("status") == "filled":
        fills = res.get("fills", [])
        price = float(fills[0]["price"]) if fills else entry
        pnl = ((price - entry) / entry) * 100
        send_message(f"💰 بيع {symbol} بسعر {price:.4f} | ربح: {pnl:.2f}%")
    else:
        r.setex(f"blacklist:sell:{symbol}", 1, BLACKLIST_EXPIRE_SECONDS)
        send_message(f"❌ فشل بيع {symbol}")

def monitor_loop():
    while True:
        try:
            for trade in list(active_trades):
                symbol = trade["symbol"]
                entry = trade["entry"]
                current = fetch_price(symbol)
                if not current:
                    continue

                profit = ((current - entry) / entry) * 100
                if profit > trade["max_profit"]:
                    trade["max_profit"] = profit
                    trade["trail"] = current

                if trade["max_profit"] >= TRAIL_START and profit <= trade["max_profit"] - TRAIL_BACKSTEP:
                    sell(symbol, entry)
                    active_trades.remove(trade)
                    r.set("nems:active_trades", json.dumps(active_trades))

                elif profit <= STOP_LOSS:
                    sell(symbol, entry)
                    active_trades.remove(trade)
                    r.set("nems:active_trades", json.dumps(active_trades))

            time.sleep(1)
        except Exception as e:
            print("خطأ في المراقبة:", e)
            time.sleep(5)

Thread(target=monitor_loop, daemon=True).start()

@app.route("/", methods=["POST"])
def webhook():
    global enabled, max_trades
    data = request.json
    if not data or "message" not in data:
        return "ok"

    text = data["message"].get("text", "").strip().lower()

    if "اشتري" in text:
        if not enabled:
            send_message("🚫 البوت متوقف عن الشراء.")
            return "ok"
        if len(active_trades) >= max_trades:
            send_message("📛 عدد الصفقات الحالية وصل للحد الأقصى.")
            return "ok"
        try:
            symbol = text.split("اشتري", 1)[-1].strip().upper()
            buy(symbol)
        except:
            send_message("❌ الصيغة غير صحيحة. مثال: اشتري ADA")

    elif "الملخص" in text:
        lines = []
        if active_trades:
            lines.append("📌 الصفقات النشطة:")
            for t in active_trades:
                symbol = t['symbol']
                entry = t['entry']
                amount = t['amount']
                current = fetch_price(symbol)
                pnl = ((current - entry) / entry) * 100 if current else 0
                emoji = "✅" if pnl >= 0 else "❌"
                lines.append(f"{emoji} {symbol} @ {entry:.4f} → {current:.4f} | كمية: {amount:.4f} | ربح: {pnl:.2f}%")
        else:
            lines.append("📌 لا توجد صفقات نشطة.")

        if executed_trades:
            lines.append("\n📊 صفقات سابقة:")
            for i, t in enumerate(executed_trades[-5:], 1):
                symbol = t['symbol']
                entry = t['entry']
                current = fetch_price(symbol)
                pnl = ((current - entry) / entry) * 100 if current else 0
                emoji = "📈" if pnl >= 0 else "📉"
                lines.append(f"{i}. {emoji} {symbol} | دخول: {entry:.4f} → الآن: {current:.4f} | {pnl:.2f}%")
        else:
            lines.append("\n📊 لا توجد صفقات سابقة.")

        send_message("\n".join(lines))

    elif "قف" in text:
        enabled = False
        send_message("🛑 تم إيقاف الشراء.")

    elif "ابدأ" in text:
        enabled = True
        send_message("✅ تم تفعيل الشراء.")

    elif "انسى" in text:
        active_trades.clear()
        executed_trades.clear()
        r.delete("nems:active_trades")
        r.delete("nems:executed_trades")
        send_message("🧠 تم نسيان كل شيء! البوت نضاف 🤖")

    elif "عدد الصفقات" in text or "عدل الصفقات" in text:
        try:
            num = int(text.split()[-1])
            if 1 <= num <= 4:
                max_trades = num
                send_message(f"⚙️ تم تعديل عدد الصفقات إلى: {num}")
            else:
                send_message("❌ فقط بين 1 و 4.")
        except:
            send_message("❌ الصيغة: عدل الصفقات 2")

    elif "الرصيد" in text:
        balances = bitvavo_request("GET", "/balance")
        eur = sum(float(b["available"]) for b in balances if b["symbol"] == "EUR")
        total = eur
        winners, losers = [], []

        for b in balances:
            sym = b.get("symbol")
            if sym == "EUR":
                continue
            qty = float(b.get("available", 0)) + float(b.get("inOrder", 0))
            if qty < 0.0001:
                continue
            pair = f"{sym}-EUR"
            entry = next((t["entry"] for t in executed_trades if t["symbol"] == pair), None)
            price = fetch_price(pair)
            if not entry or not price:
                continue
            value = qty * price
            pnl = ((price - entry) / entry) * 100
            total += value
            line = f"{sym}: {qty:.2f} @ {price:.3f} → {pnl:+.2f}%"
            (winners if pnl >= 0 else losers).append(line)

        lines = [f"💰 الرصيد الكلي: €{total:.2f}"]
        if winners: lines.append("\n📈 رابحين:\n" + "\n".join(winners))
        if losers:  lines.append("\n📉 خاسرين:\n" + "\n".join(losers))
        if not winners and not losers:
            lines.append("\n🚫 لا توجد عملات قيد التداول.")
        send_message("\n".join(lines))

if __name__ == "__main__":
    app.run(port=5000)