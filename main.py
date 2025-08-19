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
from collections import deque

# =========================
# 📌 إعدادات يدوية (المستخدمة فقط)
# =========================
MAX_TRADES = 2

LATE_FALLBACK_SEC   = 15 * 60   # بعد 15 دقيقة نفعل قفل ربح ذكي
LATE_LOCK_BACKSTEP  = 0.6       # SL = القمة - 0.6%
LATE_MIN_LOCK       = 0.5       # أقل SL بعد 15د
LATE_WEAK_R         = 0.15      # r30/r90 ضعيف ≤ 0.15%

DYN_SL_START        = -2.0      # SL ابتدائي
DYN_SL_STEP         = 1.0       # يعلو 1% لكل 1% ربح

MOM_LOOKBACK_SEC    = 120       # نافذة الزخم (ثواني)
STALL_SEC           = 90        # توقّف تقدم
DROP_FROM_PEAK_EXIT = 0.8       # هبوط عن القمة (٪) + زخم سلبي

MOM_R30_STRONG      = 0.50
MOM_R90_STRONG      = 0.80

SELL_RETRY_DELAY = 5
SELL_MAX_RETRIES = 6

EARLY_WINDOW_SEC = 15 * 60

BLACKLIST_EXPIRE_SECONDS = 300  # 5 دقائق
BUY_COOLDOWN_SEC = 600          # 10 دقائق

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
executed_trades = []   # نسخ عند الشراء + تُستكمل عند الإغلاق

SINCE_RESET_KEY = "nems:since_reset"

# =========================
# 🔔 تلغرام (مع منع تكرار)
# =========================
def send_message(text: str):
    try:
        key = "dedup:" + hashlib.sha1(text.encode("utf-8")).hexdigest()
        if r.setnx(key, 1):
            r.expire(key, 60)
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": text},
                timeout=8
            )
    except Exception as e:
        print("Telegram error:", e)

# =========================
# 🔐 Bitvavo: توقيع + طلب
# =========================
def create_signature(timestamp, method, path, body_str=""):
    msg = f"{timestamp}{method}{path}{body_str}"
    return hmac.new(BITVAVO_API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def bitvavo_request(method: str, path: str, body=None):
    timestamp = str(int(time.time() * 1000))
    url = f"https://api.bitvavo.com/v2{path}"
    body_str = "" if method == "GET" else json.dumps(body or {}, separators=(',', ':'))
    signature = create_signature(timestamp, method, f"/v2{path}", body_str)
    headers = {
        'Bitvavo-Access-Key': BITVAVO_API_KEY,
        'Bitvavo-Access-Timestamp': timestamp,
        'Bitvavo-Access-Signature': signature,
        'Bitvavo-Access-Window': '10000'
    }
    try:
        resp = requests.request(
            method,
            url,
            headers=headers,
            json=(body or {}) if method != "GET" else None,
            timeout=12 if method != "GET" else 8
        )
        return resp.json()
    except Exception as e:
        print("bitvavo_request error:", e)
        return {"error": "request_failed"}

def get_eur_available() -> float:
    try:
        balances = bitvavo_request("GET", "/balance")
        for b in balances:
            if b.get("symbol") == "EUR":
                return max(0.0, float(b.get("available", 0) or 0))
    except Exception:
        pass
    return 0.0

# =========================
# 💶 الأسعار (كاش خفيف 2ث)
# =========================
_price_cache = {"t": 0, "map": {}}
def fetch_price(market_symbol: str):
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
# ✅ رموز مدعومة (تحديث كل 10د)
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
# 🧮 تجميع fills باليورو
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
# 💱 البيع (مع إعادة المحاولة)
# =========================
def place_market_sell(market, amt):
    body = {
        "market": market,
        "side": "sell",
        "orderType": "market",
        "amount": f"{amt:.10f}",
        "clientOrderId": str(uuid4()),
        "operatorId": ""
    }
    return bitvavo_request("POST", "/order", body)

def sell_trade(trade: dict):
    market = trade["symbol"]

    if r.exists(f"blacklist:sell:{market}"):
        return

    amt = float(trade.get("amount", 0) or 0)
    if amt <= 0:
        return

    ok = False
    resp = None
    for _ in range(SELL_MAX_RETRIES):
        resp = place_market_sell(market, amt)
        if isinstance(resp, dict) and resp.get("status") == "filled":
            ok = True
            break
        time.sleep(SELL_RETRY_DELAY)

    if not ok:
        r.setex(f"blacklist:sell:{market}", BLACKLIST_EXPIRE_SECONDS, 1)
        send_message(f"❌ فشل بيع {market} بعد محاولات متعددة.")
        return

    fills = resp.get("fills", [])
    print("📦 SELL FILLS:", json.dumps(fills, ensure_ascii=False))

    tb, tq_eur, fee_eur = totals_from_fills_eur(fills)
    proceeds_eur = tq_eur - fee_eur
    sold_amount  = tb

    orig_amt  = float(trade["amount"])
    orig_cost = float(trade.get("cost_eur", trade["entry"] * trade["amount"]))

    # بيع جزئي
    if sold_amount < orig_amt - 1e-10:
        ratio = sold_amount / orig_amt if orig_amt > 0 else 1.0
        attributed_cost = orig_cost * ratio
        pnl_eur = proceeds_eur - attributed_cost
        pnl_pct = (proceeds_eur / attributed_cost - 1.0) * 100.0

        remaining_amt  = orig_amt - sold_amount
        remaining_cost = orig_cost - attributed_cost
        with lock:
            trade["amount"]   = remaining_amt
            trade["cost_eur"] = remaining_cost

        send_message(f"💰 بيع جزئي {market} | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%)")

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

    # حظر 24سا عند خسارة كبيرة
    try:
        if pnl_pct <= -3.0:
            base = market.replace("-EUR", "")
            r.setex(f"ban24:{base}", 24*3600, 1)
            send_message(f"🧊 تم حظر {base} لمدة 24 ساعة (خسارة {pnl_pct:.2f}%).")
    except Exception:
        pass

    with lock:
        try:
            active_trades.remove(trade)
        except ValueError:
            pass
        r.set("nems:active_trades", json.dumps(active_trades))

        # استكمال سجل الصفقة
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

    base = market.replace("-EUR", "")
    r.setex(f"cooldown:{base}", BUY_COOLDOWN_SEC, 1)

# =========================
# 🛒 الشراء (1/2 الرصيد ثم الباقي)
# =========================
def buy(symbol: str):
    ensure_symbols_fresh()
    symbol = symbol.upper().strip()
    if symbol not in SUPPORTED_SYMBOLS:
        send_message(f"❌ العملة {symbol} غير مدعومة على Bitvavo.")
        return

    if r.exists(f"ban24:{symbol}"):
        send_message(f"🧊 {symbol} محظورة 24 ساعة بسبب خسارة سابقة. تجاهلت الإشارة.")
        return

    if r.exists(f"cooldown:{symbol}"):
        send_message(f"⏳ {symbol} تحت فترة تهدئة مؤقتة.")
        return

    market = f"{symbol}-EUR"

    with lock:
        if any(t["symbol"] == market for t in active_trades):
            send_message(f"⛔ عندك صفقة مفتوحة على {symbol}.")
            return
        if len(active_trades) >= MAX_TRADES:
            send_message("🚫 وصلنا الحد الأقصى (صفقتان). لا يوجد استبدال بعد الآن.")
            return

    if r.exists(f"blacklist:buy:{symbol}"):
        return

    eur_avail = get_eur_available()
    if eur_avail <= 0:
        send_message("💤 لا يوجد رصيد EUR متاح للشراء.")
        return

    if len(active_trades) == 0:
        amount_quote = eur_avail / 2.0
        tranche = "النصف (50%)"
    else:
        amount_quote = eur_avail
        tranche = "المبلغ المتبقي"

    amount_quote = round(amount_quote, 2)
    if amount_quote < 5.0:
        send_message(f"⚠️ المبلغ المتاح صغير (€{amount_quote:.2f}). لن أنفذ الشراء.")
        return

    body = {
        "market": market,
        "side": "buy",
        "orderType": "market",
        "amountQuote": f"{amount_quote:.2f}",
        "clientOrderId": str(uuid4()),
        "operatorId": ""
    }
    res = bitvavo_request("POST", "/order", body)

    if not (isinstance(res, dict) and res.get("status") == "filled"):
        r.setex(f"blacklist:buy:{symbol}", BLACKLIST_EXPIRE_SECONDS, 1)
        send_message(f"❌ فشل شراء {symbol}")
        return

    fills = res.get("fills", [])
    print("📦 BUY FILLS:", json.dumps(fills, ensure_ascii=False))

    tb, tq_eur, fee_eur = totals_from_fills_eur(fills)
    amount_net = tb
    cost_eur   = tq_eur + fee_eur  # تكلفة صافية €

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
        "opened_at": time.time(),
        "phase": "EARLY",
        "peak_pct": 0.0,
        "exit_in_progress": False,
        "last_profit": 0.0,
        "last_exit_try": 0.0
    }

    with lock:
        active_trades.append(trade)
        executed_trades.append(trade.copy())
        r.set("nems:active_trades", json.dumps(active_trades))
        r.rpush("nems:executed_trades", json.dumps(trade))

    slot_idx = len(active_trades)
    send_message(f"✅ شراء {symbol} | صفقة #{slot_idx}/2 | {tranche} | قيمة: €{amount_quote:.2f} | كمية: {amount_net:.5f}")

# =========================
# 📈 تاريخ لحظي + مؤشرات زخم
# =========================
def _init_hist(trade):
    if "hist" not in trade:
        trade["hist"] = deque(maxlen=600)  # ~ دقيقتين
    if "last_new_high" not in trade:
        trade["last_new_high"] = trade.get("opened_at", time.time())

def _update_hist(trade, now_ts, price):
    _init_hist(trade)
    trade["hist"].append((now_ts, price))
    cutoff = now_ts - MOM_LOOKBACK_SEC
    while trade["hist"] and trade["hist"][0][0] < cutoff:
        trade["hist"].popleft()

def _mom_metrics(trade, price_now):
    _init_hist(trade)
    if not trade["hist"]:
        return 0.0, 0.0, False

    now_ts = trade["hist"][-1][0]
    p30 = p90 = None
    hi = lo = price_now
    for ts, p in trade["hist"]:
        hi = max(hi, p); lo = min(lo, p)
        age = now_ts - ts
        if p30 is None and age >= 30:
            p30 = p
        if p90 is None and age >= 90:
            p90 = p
    if p30 is None: p30 = trade["hist"][0][1]
    if p90 is None: p90 = trade["hist"][0][1]

    r30 = (price_now / p30 - 1.0) * 100.0 if p30 > 0 else 0.0
    r90 = (price_now / p90 - 1.0) * 100.0 if p90 > 0 else 0.0
    new_high = price_now >= hi * 0.999
    if new_high:
        trade["last_new_high"] = now_ts
    return r30, r90, new_high

def fetch_orderbook(market: str, depth: int = 1):
    try:
        return bitvavo_request("GET", f"/orderbook?market={market}&depth={depth}")
    except Exception:
        return None

# =========================
# 👀 حلقة المراقبة (خروج ذكي)
# =========================
def monitor_loop():
    while True:
        try:
            with lock:
                snapshot = list(active_trades)

            now = time.time()
            for trade in snapshot:
                market = trade["symbol"]
                entry  = float(trade["entry"])
                current = fetch_price(market)
                if not current:
                    continue

                _update_hist(trade, now, current)
                r30, r90, _ = _mom_metrics(trade, current)

                pnl_pct = ((current - entry) / entry) * 100.0
                trade["peak_pct"] = max(trade.get("peak_pct", 0.0), pnl_pct)

                # ستوب سلّمي أساسي
                inc = int(max(0.0, pnl_pct) // 1)
                dyn_sl = DYN_SL_START + inc * DYN_SL_STEP
                trade["sl_dyn"] = dyn_sl

                age = now - trade.get("opened_at", now)

                # =====================
                # 🟢 المرحلة 1: أول 15 دقيقة
                # =====================
                if age < EARLY_WINDOW_SEC:
                    if pnl_pct <= dyn_sl:
                        trade["exit_in_progress"] = True; trade["last_exit_try"] = now
                        send_message(f"🔔 خروج {market} (نافذة مبكرة: SL سلّمي {dyn_sl:.2f}% | الآن {pnl_pct:.2f}%)")
                        sell_trade(trade); trade["exit_in_progress"] = False
                    continue  # ما نفحص أي شروط تانية بهالفترة

                # =====================
                # 🔵 المرحلة 2: بعد 15 دقيقة
                # =====================

                # 1) منع تكرار محاولات الخروج
                if trade.get("exit_in_progress") and (now - trade.get("last_exit_try", 0)) < 15:
                    continue

                # 2) قفل ربح متدرّج (late lock)
                peak = trade.get("peak_pct", 0.0)
                lock_from_peak = peak - LATE_LOCK_BACKSTEP
                desired_lock = max(lock_from_peak, LATE_MIN_LOCK)
                if desired_lock > trade.get("sl_dyn", DYN_SL_START):
                    prev_lock = trade["sl_dyn"]
                    trade["sl_dyn"] = desired_lock
                    if (desired_lock - prev_lock) >= 0.4 and not trade.get("late_lock_notified"):
                        send_message(f"🔒 تفعيل قفل ربح {market}: SL ⇧ إلى {desired_lock:.2f}% (قمة {peak:.2f}%)")
                        trade["late_lock_notified"] = True

                # 3) خروج: SL سلّمي
                if pnl_pct <= trade["sl_dyn"]:
                    trade["exit_in_progress"] = True; trade["last_exit_try"] = now
                    send_message(f"🔔 خروج {market} (SL {trade['sl_dyn']:.2f}% | الآن {pnl_pct:.2f}%)")
                    sell_trade(trade); trade["exit_in_progress"] = False
                    continue

                # 4) خروج: انعكاس زخم بعد قمة
                drop_from_peak = trade["peak_pct"] - pnl_pct
                if trade["peak_pct"] >= 1.0 and drop_from_peak >= DROP_FROM_PEAK_EXIT and r30 <= -0.15 and r90 <= 0.0:
                    trade["exit_in_progress"] = True; trade["last_exit_try"] = now
                    send_message(f"🔔 خروج {market} (انعكاس زخم: قمة {trade['peak_pct']:.2f}% → {pnl_pct:.2f}%)")
                    sell_trade(trade); trade["exit_in_progress"] = False
                    continue

                # 5) خروج: توقّف تقدم (STALL)
                last_hi = trade.get("last_new_high", trade.get("opened_at", now))
                stalled = (now - last_hi) >= max(90, STALL_SEC)
                if stalled and r30 <= -0.10 and r90 <= -0.10 and pnl_pct > trade["sl_dyn"] + 0.3:
                    trade["exit_in_progress"] = True; trade["last_exit_try"] = now
                    send_message(f"🔔 خروج {market} (STALL {int(now-last_hi)}ث | r30 {r30:.2f}% r90 {r90:.2f}%)")
                    sell_trade(trade); trade["exit_in_progress"] = False
                    continue

                # 6) قراءة دفتر الطلبات (خياري)
                try:
                    ob = fetch_orderbook(market)
                    if ob:
                        ask_wall = float(ob["asks"][0][1])
                        bid_wall = float(ob["bids"][0][1])
                        if ask_wall > bid_wall * 2 and pnl_pct > 0.5:
                            send_message(f"⚠️ {market} حائط بيع ضخم ({ask_wall:.0f} ضد {bid_wall:.0f}) → خروج حذر")
                            trade["exit_in_progress"] = True; trade["last_exit_try"] = now
                            sell_trade(trade); trade["exit_in_progress"] = False
                            continue
                except Exception:
                    pass

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

    # ===== الصفقات النشطة =====
    if active_copy:
        def cur_pnl(t):
            cur = fetch_price(t["symbol"]) or t["entry"]
            return (cur - t["entry"]) / t["entry"]

        sorted_trades = sorted(active_copy, key=cur_pnl, reverse=True)

        total_value = 0.0
        total_cost  = 0.0
        lines.append(f"📌 الصفقات النشطة ({len(sorted_trades)}):")

        for i, t in enumerate(sorted_trades, 1):
            symbol = t["symbol"].replace("-EUR", "")
            entry  = float(t["entry"])
            amount = float(t["amount"])
            opened = float(t.get("opened_at", now))
            age_sec = int(now - opened)
            age_min = age_sec // 60

            current = fetch_price(t["symbol"]) or entry
            pnl_pct = ((current - entry) / entry) * 100.0
            value   = amount * current
            total_value += value
            total_cost  += float(t.get("cost_eur", entry * amount))

            # قيَم ذكية/مشتقة
            peak_pct   = float(t.get("peak_pct", 0.0))
            dyn_sl     = float(t.get("sl_dyn", DYN_SL_START))  # SL ديناميكي الحالي
            last_hi_ts = float(t.get("last_new_high", opened))
            since_last_hi = int(now - last_hi_ts)
            drop_from_peak = max(0.0, peak_pct - pnl_pct)

            # الزخم اللحظي (آمن)
            try:
                r30, r90, _ = _mom_metrics(t, current)
            except Exception:
                r30 = r90 = 0.0

            # المرحلة: أول 15د أو بعد 15د
            early_phase = age_sec < LATE_FALLBACK_SEC

            # منطق حالة مفهومة ومتطابقة مع حلقة المراقبة
            if early_phase:
                # أول 15 دقيقة: فقط SL السلّمي (-2% + 1% لكل +1% ربح)، من غير تريلينغ من القمة
                if pnl_pct <= dyn_sl:
                    state = "🛑 قريب/ضرب SL المبكر"
                elif r30 >= MOM_R30_STRONG and r90 >= MOM_R90_STRONG:
                    state = "🚀 زخم قوي — استمرار (مرحلة مبكرة)"
                else:
                    state = "⏳ مراقبة (مرحلة مبكرة)"
                lock_hint = f"SL المبكر: {dyn_sl:.2f}% (−2% +1 لكل +1 ربح)"
            else:
                # بعد 15 دقيقة: قفل ربح ذكي من القمة
                lock_from_peak = peak_pct - LATE_LOCK_BACKSTEP
                desired_lock = max(lock_from_peak, LATE_MIN_LOCK)
                lock_hint = f"قفل مقترح ≥ {desired_lock:.2f}% (قمة {peak_pct:.2f}%)"

                if pnl_pct <= dyn_sl + 0.10 and r30 <= LATE_WEAK_R and r90 <= LATE_WEAK_R:
                    state = "🔔 خروج مرجّح: زخم ضعيف وكسر القفل"
                elif (peak_pct >= 1.0 and drop_from_peak >= DROP_FROM_PEAK_EXIT and r30 <= -0.15 and r90 <= 0.0):
                    state = "🔔 خروج مرجّح: انعكاس بعد قمة"
                elif (since_last_hi >= max(90, STALL_SEC) and r30 <= -0.10 and r90 <= -0.10 and pnl_pct > dyn_sl + 0.3):
                    state = "🔔 خروج مرجّح: توقّف تقدّم + زخم سلبي"
                elif r30 >= MOM_R30_STRONG and r90 >= MOM_R90_STRONG:
                    state = "🚀 زخم قوي — استمرار"
                else:
                    state = "⏳ مراقبة (مع قفل ربح)"

            emoji = "✅" if pnl_pct >= 0 else "❌"

            # سطر العنوان
            lines.append(f"{i}. {symbol}: €{entry:.6f} → €{current:.6f} {emoji} {pnl_pct:+.2f}% | منذ {age_min}د")

            # تفاصيل الصفقة
            lines.append(
                f"   • كمية: {amount:.5f} | أعلى: {peak_pct:.2f}% | SL ديناميكي: {dyn_sl:.2f}% | من آخر قمة: {since_last_hi}s"
            )
            lines.append(
                f"   • زخم: r30 {r30:+.2f}% / r90 {r90:+.2f}% | {lock_hint} | حالة: {state}"
            )

        # ملخص إجمالي عائم
        floating_pnl_eur = total_value - total_cost
        floating_pnl_pct = ((total_value / total_cost) - 1.0) * 100 if total_cost > 0 else 0.0
        lines.append(f"💼 قيمة الصفقات: €{total_value:.2f} | عائم: {floating_pnl_eur:+.2f}€ ({floating_pnl_pct:+.2f}%)")
    else:
        lines.append("📌 لا توجد صفقات نشطة.")

    # ===== صفقات منفذة/رسوم منذ آخر «انسى» =====
    realized_pnl = 0.0
    buy_fees = 0.0
    sell_fees = 0.0
    shown = 0

    if exec_copy:
        raw = r.get(SINCE_RESET_KEY)
        since_ts = float(raw.decode() if isinstance(raw, (bytes, bytearray)) else (raw or 0))

        lines.append("\n📊 آخر صفقات منفذة:")
        for t in reversed(exec_copy):
            if "pnl_eur" in t and "exit_time" in t:
                if float(t["exit_time"]) >= since_ts:
                    realized_pnl += float(t["pnl_eur"])
                    buy_fees  += float(t.get("buy_fee_eur", 0))
                    sell_fees += float(t.get("sell_fee_eur", 0))
                sign = "✅" if float(t["pnl_eur"]) >= 0 else "❌"
                sym = t["symbol"].replace("-EUR","")
                lines.append(f"- {sym}: {sign} {float(t['pnl_eur']):+.2f}€ ({float(t['pnl_pct']):+.2f}%)")
                shown += 1
                if shown >= 5:
                    break

        total_fees = buy_fees + sell_fees
        lines.append(f"\n🧮 منذ آخر انسى:")
        lines.append(f"• أرباح/خسائر محققة: {realized_pnl:+.2f}€")
        if active_copy:
            # نعيد استخدام آخر قيم محسوبة فوق لو في صفقات نشطة
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
    global enabled

    data = request.get_json(silent=True) or {}

    # استخراج النص من تيليغرام أو من بوت B
    if "message" in data and isinstance(data["message"], dict):
        text = (data["message"].get("text") or "").strip()
    else:
        text = (data.get("text") or "").strip()

    if not text:
        return "ok"

    t_lower = text.lower()

    if "اشتري" in t_lower:
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

    elif "الملخص" in t_lower:
        send_message(build_summary())
        return "ok"

    elif "الرصيد" in t_lower:
        balances = bitvavo_request("GET", "/balance")
        eur = sum(float(b.get("available", 0)) + float(b.get("inOrder", 0))
                  for b in balances if b.get("symbol") == "EUR")
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

    elif "قف" in t_lower:
        enabled = False
        send_message("🛑 تم إيقاف الشراء.")
        return "ok"

    elif "ابدأ" in t_lower:
        enabled = True
        send_message("✅ تم تفعيل الشراء.")
        return "ok"

    elif "قائمة الحظر" in t_lower:
        keys = [k.decode() if isinstance(k, bytes) else k for k in r.keys("ban24:*")]
        if not keys:
            send_message("🧊 لا توجد عملات محظورة حالياً.")
        else:
            names = [k.split("ban24:")[-1] for k in keys]
            send_message("🧊 العملات المحظورة 24h:\n- " + "\n- ".join(sorted(names)))
        return "ok"

    elif t_lower.startswith("الغ حظر"):
        try:
            coin = text.split("الغ حظر", 1)[-1].strip().upper()
            if r.delete(f"ban24:{coin}"):
                send_message(f"✅ أُلغي حظر {coin}.")
            else:
                send_message(f"ℹ️ لا يوجد حظر على {coin}.")
        except Exception:
            send_message("❌ الصيغة: الغ حظر ADA")
        return "ok"

    elif "انسى" in t_lower:
        with lock:
            active_trades.clear()
            executed_trades.clear()
            r.delete("nems:active_trades")
            r.delete("nems:executed_trades")
            r.set(SINCE_RESET_KEY, time.time())
        send_message("🧠 تم نسيان كل شيء! بدأنا عد جديد للإحصائيات 🤖")
        return "ok"

    elif "عدد الصفقات" in t_lower or "عدل الصفقات" in t_lower:
        send_message("ℹ️ عدد الصفقات ثابت: 2 (بدون استبدال).")
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
except Exception as e:
    print("state load error:", e)

# =========================
# 🚀 التشغيل المحلي (Railway يستخدم gunicorn)
# =========================
if __name__ == "__main__" and os.getenv("RUN_LOCAL") == "1":
    app.run(host="0.0.0.0", port=5000)