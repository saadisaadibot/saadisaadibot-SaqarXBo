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
# 📌 إعدادات يدوية (بدون .env لهالقيم)
# =========================
BUY_AMOUNT_EUR = 10.0          # قيمة الشراء باليورو
MAX_TRADES = 2                 # الحد الأقصى للصفقات النشطة
TRAIL_START = 2.0              # يبدأ التريلينغ بعد ربح %
TRAIL_BACKSTEP = 0.5           # الرجوع المسموح قبل التريلينغ %
STOP_LOSS = -1.8               # ستوب لوس %
BLACKLIST_EXPIRE_SECONDS = 300 # مدة الحظر بعد فشل شراء/بيع (ثوانٍ)

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
# 🧮 حساب الصافي من الـfills (EUR)
# =========================
def totals_from_fills_eur(fills):
    """
    يجمع القيم من fills لما تكون الرسوم باليورو.
    يرجّع: (total_base, total_eur, fee_eur)
    """
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
# 💱 البيع
# =========================
def sell(symbol_with_eur, entry):
    """
    يبيع كل الكمية المتوفرة + inOrder ويحسب الربح الصافي باليورو.
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
        print("📦 SELL FILLS:", json.dumps(fills, ensure_ascii=False))  # debug

        tb, tq_eur, fee_eur = totals_from_fills_eur(fills)
        proceeds_eur = tq_eur - fee_eur  # العائد الصافي €

        # طابق الصفقة للحصول على cost_eur
        with lock:
            matched = None
            for t in reversed(executed_trades):
                if t["symbol"] == symbol_with_eur and "exit_eur" not in t:
                    matched = t
                    break

        if matched:
            cost_eur = matched.get("cost_eur", matched["entry"] * matched["amount"])
            pnl_eur  = proceeds_eur - cost_eur
            pnl_pct  = (proceeds_eur / cost_eur - 1.0) * 100.0

            send_message(f"💰 بيع {symbol_with_eur} | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%)")

            with lock:
                matched["exit_eur"] = proceeds_eur
                matched["pnl_eur"]  = pnl_eur
                matched["pnl_pct"]  = pnl_pct
                matched["exit_time"] = time.time()
                r.delete("nems:executed_trades")
                for t in executed_trades:
                    r.rpush("nems:executed_trades", json.dumps(t))
        else:
            send_message(f"💰 بيع {symbol_with_eur} (لم يُعثر على تكلفة الشراء)")
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
                # لا نستبدل صفقة تم شراؤها خلال آخر دقيقة
                if time.time() - weakest.get("timestamp", time.time()) < 60:
                    send_message("⏳ تجاهل الاستبدال: الصفقة الأضعف حديثة جدًا.")
                    return
                send_message(f"♻️ استبدال أضعف صفقة: {weakest['symbol']} (ربح {lowest_pnl:.2f}%)")
                sell(weakest["symbol"], weakest["entry"])
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
        print("📦 BUY FILLS:", json.dumps(fills, ensure_ascii=False))  # debug

        tb, tq_eur, fee_eur = totals_from_fills_eur(fills)
        amount_net = tb
        cost_eur   = tq_eur + fee_eur  # التكلفة الصافية €

        if amount_net <= 0 or cost_eur <= 0:
            send_message(f"❌ فشل شراء {symbol} - بيانات fills غير صالحة")
            r.setex(f"blacklist:buy:{symbol}", 1, BLACKLIST_EXPIRE_SECONDS)
            return

        avg_price_incl_fees = cost_eur / amount_net

        trade = {
            "symbol": market,
            "entry": avg_price_incl_fees,
            "amount": amount_net,
            "cost_eur": cost_eur,
            "trail": avg_price_incl_fees,
            "max_profit": 0,
            "timestamp": time.time()
        }

        with lock:
            active_trades.append(trade)
            executed_trades.append(trade.copy())
            r.set("nems:active_trades", json.dumps(active_trades))
            r.rpush("nems:executed_trades", json.dumps(trade))

        send_message(f"✅ شراء {symbol} | كمية: {amount_net:.5f} | تكلفة: €{cost_eur:.2f}")
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
                    for t in active_trades:
                        if t is trade:
                            if profit > t["max_profit"]:
                                t["max_profit"] = profit
                                t["trail"] = current
                            break

                # شروط البيع: Trailing أو Stop Loss
                should_sell = False
                reason = ""
                with lock:
                    mp = trade["max_profit"]
                if mp >= TRAIL_START and profit <= mp - TRAIL_BACKSTEP:
                    should_sell = True
                    reason = f"trailing mp={mp:.2f}%→{profit:.2f}%"
                elif profit <= STOP_LOSS:
                    should_sell = True
                    reason = f"stoploss {profit:.2f}% <= {STOP_LOSS:.2f}%"

                if should_sell:
                    send_message(f"🔔 خروج {symbol} بسبب {reason}")
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
                sym = t['symbol'].replace("-EUR", "")
                if "pnl_eur" in t and "pnl_pct" in t:
                    sign = "✅" if t["pnl_eur"] >= 0 else "❌"
                    lines.append(f"- {sym}: {sign} {t['pnl_eur']:+.2f}€ ({t['pnl_pct']:+.2f}%)")
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
if __name__ == "__main__" and os.getenv("RUN_LOCAL") == "1":
    app.run(host="0.0.0.0", port=5000)