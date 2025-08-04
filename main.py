import hmac
import hashlib
import os
import time
import requests
import json
from flask import Flask, request
from threading import Thread
from uuid import uuid4
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BITVAVO_API_KEY = os.getenv("BITVAVO_API_KEY")
BITVAVO_API_SECRET = os.getenv("BITVAVO_API_SECRET")
BUY_AMOUNT_EUR = float(os.getenv("BUY_AMOUNT_EUR", 10))

# 🧠 حالة البوت
enabled = True
max_trades = 2
active_trades = []
executed_trades = []
buy_blacklist = {}
sell_blacklist = {}

# ✅ إرسال رسالة تلغرام
def send_message(text):
    print(">>", text)
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data={
            "chat_id": CHAT_ID,
            "text": text
        })
    except: pass

# 🔐 طلب موقع Bitvavo
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

# ✅ سعر السوق الحالي
def fetch_price(symbol):
    try:
        res = bitvavo_request("GET", f"/ticker/price?market={symbol}")
        return float(res.get("price", 0))
    except:
        return None

# ✅ شراء العملة
def buy(symbol):
    if symbol in buy_blacklist:
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
            buy_blacklist[symbol] = True
            return

        trade = {
            "symbol": f"{symbol}-EUR",
            "entry": avg_price,
            "amount": total_amount,
            "trail": avg_price,
            "max_profit": 0
        }

        active_trades.append(trade)
        executed_trades.append({
            "symbol": f"{symbol}-EUR",
            "entry": avg_price,
            "amount": total_amount
        })

        send_message(f"✅ شراء {symbol} بسعر {avg_price:.4f}")
    else:
        buy_blacklist[symbol] = True
        send_message(f"❌ فشل شراء {symbol}")

# ✅ بيع كل الكمية المتوفرة
def sell(symbol, entry):
    if symbol in sell_blacklist:
        return

    balances = bitvavo_request("GET", "/balance")
    base = symbol.replace("-EUR", "")
    amount = 0

    for b in balances:
        if b["symbol"] == base:
            amount = float(b.get("available", 0))
            break

    if amount < 0.0001:
        sell_blacklist[symbol] = True
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
        sell_blacklist[symbol] = True
        send_message(f"❌ فشل بيع {symbol}")

# ✅ مراقبة الصفقات
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

                if trade["max_profit"] >= 2 and profit <= trade["max_profit"] - 0.5:
                    sell(symbol, entry)
                    active_trades.remove(trade)

                elif profit <= -2:
                    sell(symbol, entry)
                    active_trades.remove(trade)

            time.sleep(5)
        except Exception as e:
            print("خطأ في المراقبة:", e)
            time.sleep(5)

Thread(target=monitor_loop, daemon=True).start()

# ✅ أوامر تلغرام عبر Webhook
@app.route("/", methods=["POST"])
def webhook():
    global enabled, max_trades  # ✅ لازم يكون بأول السطر داخل الدالة

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

    elif "قف" in text:
        enabled = False
        send_message("🛑 تم إيقاف الشراء.")

    elif "رصيد" in text:
        balance = bitvavo_request("GET", "/balance")
        send_message(json.dumps(balance, indent=2))
    
    elif "ابدأ" in text:
        enabled = True
        send_message("✅ تم تفعيل الشراء.")

    elif "عدل الصفقات" in text:
        try:
            num = int(text.split(" ")[-1])
            if 1 <= num <= 4:
                max_trades = num
                send_message(f"⚙️ تم تعديل عدد الصفقات إلى: {num}")
            else:
                send_message("❌ فقط بين 1 و 4.")
        except:
            send_message("❌ الصيغة: عدل الصفقات 2")

    elif "الملخص" in text:
        lines = []
        if active_trades:
            lines.append("📌 الصفقات النشطة:")
            for t in active_trades:
                lines.append(f"{t['symbol']} @ {t['entry']:.4f} | الكمية: {t['amount']:.4f}")
        else:
            lines.append("📌 لا توجد صفقات نشطة.")

        if executed_trades:
            lines.append("\n📊 صفقات سابقة:")
            for t in executed_trades:
                current = fetch_price(t["symbol"])
                if current:
                    pnl = ((current - t["entry"]) / t["entry"]) * 100
                    emoji = "✅" if pnl >= 0 else "❌"
                    lines.append(f"{emoji} {t['symbol']} @ {t['entry']:.4f} → {current:.4f} | ربح: {pnl:.2f}%")
        else:
            lines.append("\n📊 لا توجد صفقات سابقة.")

        send_message("\n".join(lines))

    return "ok"

if __name__ == "__main__":
    app.run(port=5000)
