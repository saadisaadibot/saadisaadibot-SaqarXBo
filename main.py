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
BUY_AMOUNT_EUR = 25.0            # قيمة الشراء باليورو
MAX_TRADES = 2                   # الحد الأقصى للصفقات النشطة
TRAIL_START = 2.0                # يبدأ التريلينغ بعد ربح %
TRAIL_BACKSTEP = 0.5             # الرجوع المسموح قبل التريلينغ %
STOP_LOSS = -1.8                 # ستوب لوس %
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
swap_lock = Lock()  # قفل محلي لمنع استبدال متوازي
enabled = True
max_trades = MAX_TRADES
active_trades = []     # صفقات مفتوحة
executed_trades = []   # سجل جميع الصفقات (نسخة عند الشراء + تفاصيل الإغلاق لاحقًا)

# لحساب “منذ الانسى”
SINCE_RESET_KEY = "nems:since_reset"

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
# 💱 البيع — صفقة-بصفقة
# =========================
def sell_trade(trade):
    """
    يبيع كمية الصفقة نفسها فقط، ويحسب الربح المحقق بدقة.
    يتعامل مع البيع الجزئي بتحديث الكمية/التكلفة المتبقية.
    """
    market = trade["symbol"]

    # حظر بيع مؤقت؟
    if r.exists(f"blacklist:sell:{market}"):
        return

    amt = float(trade.get("amount", 0) or 0)
    if amt <= 0:
        return

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
        send_message(f"❌ فشل بيع {market}")
        return

    if res.get("status") == "filled":
        fills = res.get("fills", [])
        print("📦 SELL FILLS:", json.dumps(fills, ensure_ascii=False))

        tb, tq_eur, fee_eur = totals_from_fills_eur(fills)
        proceeds_eur = tq_eur - fee_eur  # عائد صافي €
        sold_amount  = tb

        orig_amt  = float(trade["amount"])
        orig_cost = float(trade.get("cost_eur", trade["entry"] * trade["amount"]))

        # مصالحة إن حصل بيع جزئي أو اختلاف بسيط
        if sold_amount < orig_amt - 1e-10:
            # بيع جزئي: نسب الكلفة على الجزء المباع
            ratio = sold_amount / orig_amt if orig_amt > 0 else 1.0
            attributed_cost = orig_cost * ratio
            pnl_eur = proceeds_eur - attributed_cost
            pnl_pct = (proceeds_eur / attributed_cost - 1.0) * 100.0

            # حدّث المتبقي
            remaining_amt  = orig_amt - sold_amount
            remaining_cost = orig_cost - attributed_cost
            with lock:
                trade["amount"]   = remaining_amt
                trade["cost_eur"] = remaining_cost

            send_message(f"💰 بيع جزئي {market} | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%)")

            # أضف صفقة منتهية جزئيًا إلى السجل
            closed = trade.copy()
            closed.update({
                "exit_eur": proceeds_eur,
                "sell_fee_eur": fee_eur,
                "pnl_eur": pnl_eur,
                "pnl_pct": pnl_pct,
                "exit_time": time.time(),
                "amount": sold_amount,
                "cost_eur": attributed_cost
            })
            with lock:
                executed_trades.append(closed)
                r.delete("nems:executed_trades")
                for t in executed_trades:
                    r.rpush("nems:executed_trades", json.dumps(t))
                r.set("nems:active_trades", json.dumps(active_trades))
            return

        # بيع كامل
        pnl_eur = proceeds_eur - orig_cost
        pnl_pct = (proceeds_eur / orig_cost - 1.0) * 100.0
        send_message(f"💰 بيع {market} | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%)")

        with lock:
            # أزل من النشطة
            try:
                active_trades.remove(trade)
            except ValueError:
                pass
            r.set("nems:active_trades", json.dumps(active_trades))

            # حدّث نسخة السجل المطابقة (آخر واحدة بدون exit)
            for i in range(len(executed_trades)-1, -1, -1):
                if executed_trades[i]["symbol"] == market and "exit_eur" not in executed_trades[i]:
                    executed_trades[i].update({
                        "exit_eur": proceeds_eur,
                        "sell_fee_eur": fee_eur,
                        "pnl_eur": pnl_eur,
                        "pnl_pct": pnl_pct,
                        "exit_time": time.time()
                    })
                    break
            r.delete("nems:executed_trades")
            for t in executed_trades:
                r.rpush("nems:executed_trades", json.dumps(t))

        # كولداون على الشراء لنفس الرمز
        base = market.replace("-EUR", "")
        r.setex(f"cooldown:{base}", BUY_COOLDOWN_SEC, 1)

    else:
        r.setex(f"blacklist:sell:{market}", BLACKLIST_EXPIRE_SECONDS, 1)
        send_message(f"❌ فشل بيع {market}")

# =========================
# 🛒 الشراء (لا تكرار لنفس الرمز)
# =========================
def buy(symbol):
    """
    - يتحقق من الدعم على Bitvavo.
    - يمنع شراء نفس الرمز إذا عندك صفقة مفتوحة أو في كولداون.
    - إذا امتلأت الصفقات: استبدال الأضعف (اختياري).
    - شراء ماركت بمبلغ EUR ثابت (amountQuote) وتسجيل fills بدقة.
    """
    ensure_symbols_fresh()
    symbol = symbol.upper().strip()
    if symbol not in SUPPORTED_SYMBOLS:
        send_message(f"❌ العملة {symbol} غير مدعومة على Bitvavo.")
        return

    if r.exists(f"cooldown:{symbol}"):
        send_message(f"⏳ {symbol} تحت فترة تهدئة مؤقتة.")
        return

    # ممنوع شراء نفس العملة مرتين
    with lock:
        if any(t["symbol"] == f"{symbol}-EUR" for t in active_trades):
            send_message(f"⛔ عندك صفقة مفتوحة على {symbol}.")
            return

    if r.exists(f"blacklist:buy:{symbol}"):
        return

    market = f"{symbol}-EUR"

    # استبدال الأضعف إذا امتلأت
    with lock:
        needs_swap = len(active_trades) >= max_trades

    if needs_swap:
        # امنع استبدالين مع بعض
        if not swap_lock.acquire(blocking=False):
            send_message("⏳ يوجد استبدال قيد التنفيذ، تجاهلت الطلب الحالي.")
            return

        try:
            # اختر الأضعف خارج الـlock الثقيل
            with lock:
                weakest = None
                lowest_pnl = float('inf')
                for t in active_trades:
                    current = fetch_price(t["symbol"]) or t["entry"]
                    pnl = ((current - t["entry"]) / t["entry"]) * 100
                    if pnl < lowest_pnl:
                        lowest_pnl = pnl
                        weakest = t

                if not weakest:
                    send_message("❌ لا يمكن تنفيذ الاستبدال.")
                    return

                # لا تستبدل صفقة جديدة جداً
                if time.time() - weakest.get("timestamp", time.time()) < 60:
                    send_message("⏳ تجاهل الاستبدال: الصفقة الأضعف حديثة جدًا.")
                    return

                send_message(f"♻️ استبدال أضعف صفقة: {weakest['symbol']} (ربح {lowest_pnl:.2f}%)")

            # نفّذ البيع
            sell_trade(weakest)

            # انتظر حتى يُحذف من active_trades (نجاح البيع) أو ينتهي المهلة
            deadline = time.time() + 12  # 12 ثانية مهلة
            while time.time() < deadline:
                with lock:
                    still_there = any(id(t) == id(weakest) for t in active_trades)
                if not still_there:
                    break
                time.sleep(0.25)
            else:
                send_message("⏳ إلغاء الاستبدال: البيع لم يكتمل خلال المهلة.")
                return
        finally:
            swap_lock.release()

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
        print("📦 BUY FILLS:", json.dumps(fills, ensure_ascii=False))

        tb, tq_eur, fee_eur = totals_from_fills_eur(fills)
        amount_net = tb
        cost_eur   = tq_eur + fee_eur  # التكلفة الصافية €

        if amount_net <= 0 or cost_eur <= 0:
            send_message(f"❌ فشل شراء {symbol} - بيانات fills غير صالحة")
            r.setex(f"blacklist:buy:{symbol}", BLACKLIST_EXPIRE_SECONDS, 1)
            return

        avg_price_incl_fees = cost_eur / amount_net

        trade = {
            "symbol": market,
            "entry": avg_price_incl_fees,
            "amount": amount_net,
            "cost_eur": cost_eur,
            "buy_fee_eur": fee_eur,
            "trail": avg_price_incl_fees,
            "max_profit": 0.0,
            "milestone_sent": False,   # إشعار +TRAIL_START%
            "timestamp": time.time(),
            # أعلام الخروج (لمنع السبام والعبور)
            "exit_in_progress": False,
            "last_profit": 0.0,
            "exit_armed": False,
            "last_exit_try": 0.0
        }

        with lock:
            active_trades.append(trade)
            executed_trades.append(trade.copy())
            r.set("nems:active_trades", json.dumps(active_trades))
            r.rpush("nems:executed_trades", json.dumps(trade))

        send_message(f"✅ شراء {symbol} | كمية: {amount_net:.5f} | تكلفة: €{cost_eur:.2f}")
    else:
        r.setex(f"blacklist:buy:{symbol}", BLACKLIST_EXPIRE_SECONDS, 1)
        send_message(f"❌ فشل شراء {symbol}")

# =========================
# 👀 حلقة المراقبة (edge + cooldown)
# =========================
def monitor_loop():
    while True:
        try:
            with lock:
                snapshot = list(active_trades)

            for trade in snapshot:
                symbol = trade["symbol"]             # e.g. 'ADA-EUR'
                entry = trade["entry"]
                current = fetch_price(symbol)
                if not current:
                    continue

                profit = ((current - entry) / entry) * 100
                mp = trade["max_profit"]

                # تحديث أعلى ربح + تسليح التريل
                with lock:
                    if profit > mp:
                        trade["max_profit"] = profit
                        trade["trail"] = current
                        mp = profit
                    if mp >= TRAIL_START:
                        trade["exit_armed"] = True

                    # خزّن آخر ربح لمقارنة العبور
                    prev_profit = trade.get("last_profit", profit)
                    trade["last_profit"] = profit

                # تهدئة لو بيع قيد التنفيذ
                if trade.get("exit_in_progress") and (time.time() - trade.get("last_exit_try", 0)) < 15:
                    continue

                # تحقّق عبور الشروط (edge crossing)
                crossed_trail = trade.get("exit_armed") and (profit <= mp - TRAIL_BACKSTEP) and (prev_profit > mp - TRAIL_BACKSTEP)
                crossed_sl    = (profit <= STOP_LOSS) and (prev_profit > STOP_LOSS)

                if crossed_trail or crossed_sl:
                    reason = f"trailing {mp:.2f}%→{profit:.2f}%" if crossed_trail else f"stoploss {profit:.2f}% <= {STOP_LOSS:.2f}%"
                    with lock:
                        trade["exit_in_progress"] = True
                        trade["last_exit_try"] = time.time()
                    send_message(f"🔔 خروج {symbol} بسبب {reason} (جارِ التنفيذ)")

                    # احترم blacklist البيع
                    if r.exists(f"blacklist:sell:{symbol}"):
                        continue

                    sell_trade(trade)

                    # بعد محاولة البيع (ناجحة/فاشلة) حرّر العلم للسماح بمحاولة لاحقة
                    with lock:
                        trade["exit_in_progress"] = False

            time.sleep(1)
        except Exception as e:
            print("خطأ في المراقبة:", e)
            time.sleep(5)

Thread(target=monitor_loop, daemon=True).start()

# =========================
# 🧾 ملخص ذكي
# =========================
def build_summary():
    lines = []
    now = time.time()

    with lock:
        active_copy = list(active_trades)
        exec_copy = list(executed_trades)

    if active_copy:
        def cur_pnl(t):
            cur = fetch_price(t["symbol"]) or t["entry"]
            return (cur - t["entry"]) / t["entry"]
        sorted_trades = sorted(active_copy, key=cur_pnl, reverse=True)

        total_value = 0.0
        total_cost  = 0.0
        lines.append(f"📌 الصفقات النشطة ({len(sorted_trades)}):")
        for i, t in enumerate(sorted_trades, 1):
            symbol = t['symbol'].replace("-EUR", "")
            entry  = t['entry']
            amount = t['amount']
            current = fetch_price(t['symbol']) or entry
            pnl_pct = ((current - entry) / entry) * 100
            value   = amount * current
            total_value += value
            total_cost  += t.get("cost_eur", entry * amount)
            duration_min = int((now - t.get("timestamp", now)) / 60)
            emoji = "✅" if pnl_pct >= 0 else "❌"
            lines.append(f"{i}. {symbol}: €{entry:.4f} → €{current:.4f} {emoji} {pnl_pct:+.2f}%")
            lines.append(f"   • كمية: {amount:.5f} | منذ: {duration_min} د | أعلى: {t.get('max_profit',0):.2f}%")

        floating_pnl_eur = total_value - total_cost
        floating_pnl_pct = ((total_value / total_cost) - 1.0) * 100 if total_cost > 0 else 0.0
        lines.append(f"💼 قيمة الصفقات: €{total_value:.2f} | عائم: {floating_pnl_eur:+.2f}€ ({floating_pnl_pct:+.2f}%)")
    else:
        lines.append("📌 لا توجد صفقات نشطة.")

    realized_pnl = 0.0
    buy_fees = 0.0
    sell_fees = 0.0
    shown = 0

    if exec_copy:
        since_ts = float(r.get(SINCE_RESET_KEY) or 0)
        lines.append("\n📊 آخر صفقات منفذة:")
        for t in reversed(exec_copy):
            if "pnl_eur" in t and "exit_time" in t:
                if t["exit_time"] >= since_ts:
                    realized_pnl += float(t["pnl_eur"])
                    buy_fees  += float(t.get("buy_fee_eur", 0))
                    sell_fees += float(t.get("sell_fee_eur", 0))
                sign = "✅" if t["pnl_eur"] >= 0 else "❌"
                sym = t["symbol"].replace("-EUR","")
                lines.append(f"- {sym}: {sign} {t['pnl_eur']:+.2f}€ ({t['pnl_pct']:+.2f}%)")
                shown += 1
                if shown >= 5:
                    break

    total_fees = buy_fees + sell_fees
    if exec_copy:
        lines.append(f"\n🧮 منذ آخر انسى:")
        lines.append(f"• أرباح/خسائر محققة: {realized_pnl:+.2f}€")
        if active_copy:
            lines.append(f"• أرباح/خسائر عائمة حاليًا: {floating_pnl_eur:+.2f}€")
        lines.append(f"• الرسوم المدفوعة: {total_fees:.2f}€ (شراء: {buy_fees:.2f}€ / بيع: {sell_fees:.2f}€)")
    else:
        lines.append("\n📊 لا توجد صفقات سابقة.")

    return "\n".join(lines)

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
        send_message(build_summary())
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
            r.set(SINCE_RESET_KEY, time.time())
        send_message("🧠 تم نسيان كل شيء! بدأنا عد جديد للإحصائيات 🤖")
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
    if not r.exists(SINCE_RESET_KEY):
        r.set(SINCE_RESET_KEY, 0)
except Exception:
    pass

# =========================
# 🚀 التشغيل المحلي (Railway يستخدم gunicorn)
# =========================
if __name__ == "__main__" and os.getenv("RUN_LOCAL") == "1":
    app.run(host="0.0.0.0", port=5000)