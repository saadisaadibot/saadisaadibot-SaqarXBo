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
# 📌 إعدادات قابلة للتعديل
# =========================
load_dotenv()
BUY_AMOUNT_EUR = float(os.getenv("BUY_AMOUNT_EUR", 10))
MAX_TRADES = int(os.getenv("MAX_ACTIVE_TRADES", 2))
TRAIL_START = float(os.getenv("TRAIL_START_PERCENT", 2) or 2)
TRAIL_BACKSTEP = float(os.getenv("TRAIL_BACKSTEP", 0.5) or 0.5)
STOP_LOSS = float(os.getenv("STOP_LOSS_PERCENT", -1.8) or -1.8)
BLACKLIST_EXPIRE_SECONDS = int(os.getenv("BLACKLIST_EXPIRE_SECONDS", 300))

# =========================
# 🧠 التهيئة العامة
# =========================
app = Flask(__name__)
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BITVAVO_API_KEY = os.getenv("BITVAVO_API_KEY")
BITVAVO_API_SECRET = os.getenv("BITVAVO_API_SECRET")
r = redis.from_url(os.getenv("REDIS_URL"))
lock = Lock()

enabled = True
max_trades = MAX_TRADES
active_trades = []
executed_trades = []

# =========================
# 🔔 مساعدات
# =========================
def send_message(text):
    print(">>", text)
    try:
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
    """
    - لا نرسل body مع GET.
    - نستخدم timeout.
    - نُضمّن في التوقيع الـ body الذي سنرسله فقط.
    """
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

def fetch_price(market_symbol):
    """ market_symbol مثل 'ADA-EUR' """
    try:
        res = bitvavo_request("GET", f"/ticker/price?market={market_symbol}")
        price = float(res.get("price", 0))
        return price if price > 0 else None
    except Exception:
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
# 💱 البيع
# =========================
def sell(symbol_with_eur, entry):
    """
    symbol_with_eur مثل 'ADA-EUR'
    يبيع كل الكمية المتوفرة + inOrder.
    يسجّل exit في executed_trades.
    """
    if r.exists(f"blacklist:sell:{symbol_with_eur}"):
        return

    balances = bitvavo_request("GET", "/balance")
    base = symbol_with_eur.replace("-EUR", "")
    amount = 0.0
    try:
        for b in balances:
            if b.get("symbol") == base:
                amount = float(b.get("available", 0)) + float(b.get("inOrder", 0))
                break
    except Exception:
        amount = 0.0

    if amount < 0.0001:
        r.setex(f"blacklist:sell:{symbol_with_eur}", 1, BLACKLIST_EXPIRE_SECONDS)
        return

    body = {
        "market": symbol_with_eur,
        "side": "sell",
        "orderType": "market",
        "amount": f"{amount:.10f}",
        "clientOrderId": str(uuid4()),
        "operatorId": ""
    }

    res = bitvavo_request("POST", "/order", body)
    if isinstance(res, dict) and res.get("status") == "filled":
        fills = res.get("fills", [])
        total_amount = sum(float(f["amount"]) for f in fills) if fills else amount
        total_value  = sum(float(f["amount"]) * float(f["price"]) for f in fills) if fills else (amount * (fetch_price(symbol_with_eur) or entry))
        real_price   = (total_value / total_amount) if total_amount > 0 else entry

        pnl = ((real_price - entry) / entry) * 100
        send_message(f"💰 بيع {symbol_with_eur} بسعر {real_price:.4f} | ربح: {pnl:+.2f}%")

        # تسجيل exit في executed_trades
        with lock:
            for t in reversed(executed_trades):
                if t["symbol"] == symbol_with_eur and "exit" not in t:
                    t["exit"] = real_price
                    t["exit_time"] = time.time()
                    break
            # تحديث قائمة Redis كاملة
            r.delete("nems:executed_trades")
            for t in executed_trades:
                r.rpush("nems:executed_trades", json.dumps(t))
    else:
        r.setex(f"blacklist:sell:{symbol_with_eur}", 1, BLACKLIST_EXPIRE_SECONDS)
        send_message(f"❌ فشل بيع {symbol_with_eur}")

# =========================
# 🛒 الشراء (مع الاستبدال الذكي)
# =========================
def buy(symbol):
    """
    symbol مثل 'ADA'
    - يتحقق من الدعم على Bitvavo.
    - لو امتلأت الصفقات: يستبدل الأضعف.
    - شراء ماركت بمبلغ EUR ثابت (amountQuote).
    - يحفظ الصفقة ويبلّغ.
    """
    ensure_symbols_fresh()
    symbol = symbol.upper().strip()
    if symbol not in SUPPORTED_SYMBOLS:
        send_message(f"❌ العملة {symbol} غير مدعومة على Bitvavo.")
        return

    if r.exists(f"blacklist:buy:{symbol}"):
        return

    market = f"{symbol}-EUR"

    # استبدال الأضعف إذا لزم
    with lock:
        if len(active_trades) >= max_trades:
            weakest = None
            lowest_pnl = float('inf')
            for t in active_trades:
                current = fetch_price(t["symbol"]) or 0
                if current <= 0:
                    continue
                pnl = ((current - t["entry"]) / t["entry"]) * 100
                if pnl < lowest_pnl:
                    lowest_pnl = pnl
                    weakest = t
            if weakest:
                send_message(f"♻️ استبدال أضعف صفقة: {weakest['symbol']} (ربح {lowest_pnl:.2f}%)")
                # بيع الضعيفة
                sell(weakest["symbol"], weakest["entry"])
                # شيلها من النشطة
                try:
                    active_trades.remove(weakest)
                except ValueError:
                    pass
                r.set("nems:active_trades", json.dumps(active_trades))
            else:
                send_message("❌ لا يمكن تنفيذ الاستبدال.")
                return

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
        total_amount = sum(float(f["amount"]) for f in fills) if fills else 0.0
        total_price = sum(float(f["amount"]) * float(f["price"]) for f in fills) if fills else 0.0
        avg_price = (total_price / total_amount) if total_amount > 0 else (fetch_price(market) or 0.0)

        if total_amount <= 0 or avg_price <= 0:
            send_message(f"❌ فشل شراء {symbol} - السعر أو الكمية غير صالحة")
            r.setex(f"blacklist:buy:{symbol}", 1, BLACKLIST_EXPIRE_SECONDS)
            return

        trade = {
            "symbol": market,
            "entry": avg_price,
            "amount": total_amount,
            "trail": avg_price,
            "max_profit": 0,
            "timestamp": time.time()
        }

        with lock:
            active_trades.append(trade)
            executed_trades.append(trade.copy())
            r.set("nems:active_trades", json.dumps(active_trades))
            r.rpush("nems:executed_trades", json.dumps(trade))

        send_message(f"✅ شراء {symbol} بسعر €{avg_price:.10f}")
    else:
        r.setex(f"blacklist:buy:{symbol}", 1, BLACKLIST_EXPIRE_SECONDS)
        send_message(f"❌ فشل شراء {symbol}")

# =========================
# 👀 حلقة المراقبة
# =========================
def monitor_loop():
    while True:
        try:
            with lock:
                snapshot = list(active_trades)
            for trade in snapshot:
                symbol = trade["symbol"]            # 'ADA-EUR'
                entry = trade["entry"]
                current = fetch_price(symbol)
                if not current:
                    continue

                # حدّث أعلى ربح وتتبع الترايل
                profit = ((current - entry) / entry) * 100
                with lock:
                    # جدّد max_profit و trail على العنصر الأصلي
                    for t in active_trades:
                        if t is trade:
                            if profit > t["max_profit"]:
                                t["max_profit"] = profit
                                t["trail"] = current
                            break

                # شروط البيع: Trailing أو Stop Loss
                should_sell = False
                with lock:
                    mp = trade["max_profit"]
                if mp >= TRAIL_START and profit <= mp - TRAIL_BACKSTEP:
                    should_sell = True
                elif profit <= STOP_LOSS:
                    should_sell = True

                if should_sell:
                    sell(symbol, entry)
                    with lock:
                        try:
                            active_trades.remove(trade)
                        except ValueError:
                            pass
                        r.set("nems:active_trades", json.dumps(active_trades))
            time.sleep(1)
        except Exception as e:
            print("خطأ في المراقبة:", e)
            time.sleep(5)

Thread(target=monitor_loop, daemon=True).start()

# =========================
# 🤖 Webhook
# =========================
@app.route("/", methods=["POST"])
def webhook():
    global enabled, max_trades
    data = request.json
    if not data or "message" not in data:
        return "ok"

    text = (data["message"].get("text") or "").strip().lower()

    if "اشتري" in text:
        if not enabled:
            send_message("🚫 البوت متوقف عن الشراء.")
            return "ok"
        try:
            symbol = text.split("اشتري", 1)[-1].strip().upper()
            if not symbol:
                raise ValueError("no symbol")
            buy(symbol)
        except Exception:
            send_message("❌ الصيغة غير صحيحة. مثال: اشتري ADA")
        return "ok"

    elif "الملخص" in text:
        lines = []
        with lock:
            active_copy = list(active_trades)
            exec_copy = list(executed_trades)

        if active_copy:
            sorted_trades = sorted(
                active_copy,
                key=lambda t: ((fetch_price(t["symbol"]) or t["entry"]) - t["entry"]) / t["entry"],
                reverse=True
            )
            total_value = 0.0
            lines.append(f"📌 الصفقات النشطة ({len(sorted_trades)}):")
            for i, t in enumerate(sorted_trades, 1):
                symbol = t['symbol'].replace("-EUR", "")
                entry = t['entry']
                amount = t['amount']
                current = fetch_price(t['symbol']) or entry
                pnl = ((current - entry) / entry) * 100
                emoji = "✅" if pnl >= 0 else "❌"
                value = amount * current
                total_value += value
                duration = int((time.time() - t.get("timestamp", time.time())) / 60)
                lines.append(f"{i}. {symbol}: €{entry:.3f} → €{current:.3f} {emoji} {pnl:+.2f}%")
                lines.append(f"   • كمية: {amount:.4f} | منذ: {duration} د")
            lines.append(f"💼 قيمة الصفقات: €{total_value:.2f}")
        else:
            lines.append("📌 لا توجد صفقات نشطة.")

        if exec_copy:
            lines.append("\n📊 آخر صفقات منفذة:")
            shown = 0
            for t in reversed(exec_copy):
                if shown >= 5:
                    break
                symbol = t['symbol'].replace("-EUR", "")
                entry = t['entry']
                exit_price = t.get("exit")
                if exit_price is None:
                    # لو ما في exit، اعرض آخر سعر للتوضيح
                    exit_price = fetch_price(t['symbol']) or entry
                pnl = ((exit_price - entry) / entry) * 100 if exit_price else 0
                emoji = "✅" if pnl >= 0 else "❌"
                lines.append(f"- {symbol}: دخول @ €{entry:.3f} → خروج @ €{exit_price:.3f} {emoji} {pnl:+.2f}%")
                shown += 1
        else:
            lines.append("\n📊 لا توجد صفقات سابقة.")

        send_message("\n".join(lines))
        return "ok"

    elif "الرصيد" in text:
        balances = bitvavo_request("GET", "/balance")
        eur = sum(float(b.get("available", 0)) + float(b.get("inOrder", 0)) for b in balances if b.get("symbol") == "EUR")
        total = eur
        winners, losers = [], []

        with lock:
            exec_copy = list(executed_trades)

        for b in balances:
            sym = b.get("symbol")
            if sym == "EUR":
                continue
            qty = float(b.get("available", 0)) + float(b.get("inOrder", 0))
            if qty < 0.0001:
                continue
            pair = f"{sym}-EUR"
            price = fetch_price(pair)
            if not price:
                continue
            value = qty * price
            total += value

            # ابحث عن آخر entry لنفس الزوج (حتى لو تم الخروج)
            entry = None
            for t in reversed(exec_copy):
                if t["symbol"] == pair:
                    entry = t.get("entry")
                    break

            if entry:
                pnl = ((price - entry) / entry) * 100
                line = f"{sym}: {qty:.4f} @ €{price:.4f} → {pnl:+.2f}%"
                (winners if pnl >= 0 else losers).append(line)

        lines = [f"💰 الرصيد الكلي: €{total:.2f}"]
        if winners:
            lines.append("\n📈 رابحين:\n" + "\n".join(winners))
        if losers:
            lines.append("\n📉 خاسرين:\n" + "\n".join(losers))
        if not winners and not losers:
            lines.append("\n🚫 لا توجد عملات قيد التداول.")
        send_message("\n".join(lines))
        return "ok"

    elif "قف" in text:
        enabled = False
        send_message("🛑 تم إيقاف الشراء.")
        return "ok"

    elif "ابدأ" in text:
        enabled = True
        send_message("✅ تم تفعيل الشراء.")
        return "ok"

    elif "انسى" in text:
        with lock:
            active_trades.clear()
            executed_trades.clear()
            r.delete("nems:active_trades")
            r.delete("nems:executed_trades")
        send_message("🧠 تم نسيان كل شيء! البوت نضاف 🤖")
        return "ok"

    elif "عدد الصفقات" in text or "عدل الصفقات" in text:
        try:
            num = int(text.split()[-1])
            if 1 <= num <= 4:
                max_trades = num
                send_message(f"⚙️ تم تعديل عدد الصفقات إلى: {num}")
            else:
                send_message("❌ فقط بين 1 و 4.")
        except Exception:
            send_message("❌ الصيغة: عدل الصفقات 2")
        return "ok"

    return "ok"

# =========================
# 🔁 تحميل الحالة من Redis عند الإقلاع
# =========================
try:
    at = r.get("nems:active_trades")
    if at:
        active_trades = json.loads(at)
    et = r.lrange("nems:executed_trades", 0, -1)
    executed_trades = [json.loads(t) for t in et]
except Exception:
    pass

# =========================
# 🚀 التشغيل
# =========================
# على Railway/Gunicorn: شغّل بس gunicorn: web: gunicorn app:app
# للتشغيل المحلي فقط: ضع RUN_LOCAL=1
if __name__ == "__main__" and os.getenv("RUN_LOCAL") == "1":
    app.run(host="0.0.0.0", port=5000)