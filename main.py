# -*- coding: utf-8 -*-
import hmac
import hashlib
import os
import time
import math
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

LATE_FALLBACK_SEC   = 10 * 60
LATE_LOCK_BACKSTEP  = 0.8       # قفل أرخى بعد 10د ليسمح للموجة تمتد
LATE_MIN_LOCK       = 0.5
LATE_WEAK_R         = 0.10

DYN_SL_START        = -2.0
DYN_SL_STEP         = 1.0

MOM_LOOKBACK_SEC    = 120
STALL_SEC           = 150       # تسامح أكبر مع توقف التقدم
DROP_FROM_PEAK_EXIT = 1.2       # خروج أقل حساسية للتراجع الطفيف

MOM_R30_STRONG      = 0.50
MOM_R90_STRONG      = 0.80

SELL_RETRY_DELAY = 5
SELL_MAX_RETRIES = 6

EARLY_WINDOW_SEC = 15 * 60

BLACKLIST_EXPIRE_SECONDS = 300
BUY_COOLDOWN_SEC = 180        # دخول أسرع

# حماية سريعة
GRACE_SEC         = 45
EARLY_CRASH_SL    = -3.4       # أوسع قليلاً
FAST_DROP_WINDOW  = 20
FAST_DROP_PCT     = 1.3

# ربح صغير
MICRO_PROFIT_MIN  = 0.7
MICRO_PROFIT_MAX  = 1.3
MICRO_FAST_DROP   = 1.1

# Trailing من القمة
GIVEBACK_RATIO    = 0.32       # يسمح بإرجاع أكبر ليمسك +6%+
GIVEBACK_MIN      = 3.8
GIVEBACK_MAX      = 7.0
PEAK_TRIGGER      = 8.5        # فعّل مبكرًا

# زخم سلبي قوي
FAST_DROP_R30     = -1.3
FAST_DROP_R90     = -3.2

# حدود نزيف
DAILY_STOP_EUR     = -8.0
CONSEC_LOSS_BAN    = 2

# فلتر دفتر الأوامر
OB_MIN_BID_EUR     = 100.0
OB_REQ_IMB         = 1.0
OB_MAX_SPREAD_BP   = 60.0
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
# 🧾 دفتر أوامر + فلتر
# =========================
def fetch_orderbook(market: str, depth: int = 1):
    try:
        url = f"https://api.bitvavo.com/v2/{market}/book"  # <-- هذا هو الصحيح
        resp = requests.get(url, timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            # لازم يحتوي قوائم bids/asks
            if data and data.get("bids") and data.get("asks"):
                return data
    except Exception:
        pass
    return None

def orderbook_guard(market: str,
                    min_bid_eur: float = OB_MIN_BID_EUR,
                    req_imb: float = OB_REQ_IMB,
                    max_spread_bp: float = OB_MAX_SPREAD_BP):
    """
    يرجّع (ok, why, feats) لفحص سريع قبل الدخول/الإشعار.
    feats: dict يحتوي سبريد، أحجام، الخ..
    """
    ob = fetch_orderbook(market)
    if not ob or not ob.get("bids") or not ob.get("asks"):
        return False, "no_orderbook", {}
    try:
        bid_p, bid_q = float(ob["bids"][0][0]), float(ob["bids"][0][1])
        ask_p, ask_q = float(ob["asks"][0][0]), float(ob["asks"][0][1])
    except Exception:
        return False, "bad_book", {}

    spread_bp = (ask_p - bid_p) / ((ask_p + bid_p)/2.0) * 10000.0
    bid_eur   = bid_p * bid_q
    imb       = (bid_q / max(1e-9, ask_q))

    if bid_eur < min_bid_eur:
        return False, f"low_liquidity:{bid_eur:.0f}", {"spread_bp":spread_bp,"bid_eur":bid_eur,"imb":imb}
    if spread_bp > max_spread_bp:
        return False, f"wide_spread:{spread_bp:.1f}bp", {"spread_bp":spread_bp,"bid_eur":bid_eur,"imb":imb}
    if imb < req_imb:
        return False, f"weak_bid_imb:{imb:.2f}", {"spread_bp":spread_bp,"bid_eur":bid_eur,"imb":imb}

    return True, "ok", {"spread_bp":spread_bp,"bid_eur":bid_eur,"imb":imb}

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
# 💱 البيع (مع إعادة المحاولة) — صيغة البيع كما طلبت
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

def _today_key():
    # حسب توقيت UTC لتبسيط التخزين
    return time.strftime("pnl:%Y%m%d", time.gmtime())

def _accum_realized(pnl):
    try:
        r.incrbyfloat(_today_key(), float(pnl))
        r.expire(_today_key(), 3*24*3600)
    except Exception:
        pass

def _today_pnl():
    try:
        return float(r.get(_today_key()) or 0.0)
    except Exception:
        return 0.0

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

        _accum_realized(pnl_eur)

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
    _accum_realized(pnl_eur)
    send_message(f"💰 بيع {market} | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%)")

    # حظر 24سا عند خسارة كبيرة
    try:
        base = market.replace("-EUR", "")
        if pnl_pct <= -3.0:
            r.setex(f"ban24:{base}", 24*3600, 1)
            send_message(f"🧊 تم حظر {base} لمدة 24 ساعة (خسارة {pnl_pct:.2f}%).")
        # خسارتين متتاليتين لنفس العملة → حظر 24h
        if pnl_eur < 0:
            k = f"lossstreak:{base}"
            streak = int(r.incr(k))
            r.expire(k, 24*3600)
            if streak >= CONSEC_LOSS_BAN:
                r.setex(f"ban24:{base}", 24*3600, 1)
                send_message(f"🧊 حظر {base} 24h (خسارتين متتاليتين).")
        else:
            r.delete(f"lossstreak:{base}")
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

    # حدّ خسارة يومي
    if _today_pnl() <= DAILY_STOP_EUR:
        send_message("⛔ تم إيقاف الشراء لباقي اليوم (تجاوز حد الخسارة اليومي).")
        return

    if r.exists(f"ban24:{symbol}"):
        send_message(f"🧊 {symbol} محظورة 24 ساعة بسبب خسارة سابقة/متتالية. تجاهلت الإشارة.")
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

    # فلتر دفتر الأوامر
    ok, why, feats = orderbook_guard(market)
    if not ok:
        send_message(f"⛔ رفض الشراء {symbol} ({why}). "
             f"spread={feats.get('spread_bp',0):.1f}bp | bid€={feats.get('bid_eur',0):.0f} | imb={feats.get('imb',0):.2f}")
        r.setex(f"cooldown:{symbol}", 180, 1)  # تهدئة قصيرة
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
    send_message(
        f"✅ شراء {symbol} | صفقة #{slot_idx}/2 | {tranche} | قيمة: €{amount_quote:.2f} | "
        f"SL مبكر مفعّل | سيولة مقبولة (spread≤{OB_MAX_SPREAD_BP}bp)"
    )

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

def _price_n_seconds_ago(trade, now_ts, sec):
    cutoff = now_ts - sec
    back = None
    for ts, p in reversed(trade.get("hist", [])):
        if ts <= cutoff:
            back = p
            break
    return back

def _fast_drop_detect(trade, now_ts, price_now):
    p20 = _price_n_seconds_ago(trade, now_ts, FAST_DROP_WINDOW)
    if p20 and p20 > 0:
        dpct = (price_now/p20 - 1.0)*100.0
        return dpct <= -FAST_DROP_PCT, dpct
    return False, 0.0

def _peak_giveback(peak):
    return max(GIVEBACK_MIN, min(GIVEBACK_MAX, GIVEBACK_RATIO * peak))

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

                # تحضير
                _update_hist(trade, now, current)
                r30, r90, _ = _mom_metrics(trade, current)

                pnl_pct = ((current - entry) / entry) * 100.0
                trade["peak_pct"] = max(trade.get("peak_pct", 0.0), pnl_pct)

                # SL سلّمي أساسي
                inc = int(max(0.0, pnl_pct) // 1)
                dyn_sl_base = DYN_SL_START + inc * DYN_SL_STEP
                trade["sl_dyn"] = max(trade.get("sl_dyn", DYN_SL_START), dyn_sl_base)

                # ===== قفل ربحي مبكّر متدرّج (جديد) =====
                if pnl_pct >= 3.0:
                    trade["sl_dyn"] = max(trade["sl_dyn"], 1.6)
                elif pnl_pct >= 2.0:
                    trade["sl_dyn"] = max(trade["sl_dyn"], 0.8)
                elif pnl_pct >= 1.2:
                    trade["sl_dyn"] = max(trade["sl_dyn"], 0.1)

                age = now - trade.get("opened_at", now)
                if "grace_until" not in trade:
                    trade["grace_until"] = trade.get("opened_at", now) + GRACE_SEC
                if "sl_breach_at" not in trade:
                    trade["sl_breach_at"] = 0.0

                # ===== Trailing من القمة دائماً (يحافظ على أعلى نسبة) =====
                peak = trade.get("peak_pct", 0.0)
                if peak >= PEAK_TRIGGER:
                    giveback = _peak_giveback(peak)
                    min_lock = peak - giveback
                    if min_lock > trade["sl_dyn"]:
                        prev = trade["sl_dyn"]
                        trade["sl_dyn"] = min_lock
                        if (not trade.get("boost_lock_notified")) and (trade["sl_dyn"] - prev >= 0.4):
                            send_message(f"🔒 تعزيز SL {market}: {trade['sl_dyn']:.2f}% (قمة {peak:.2f}%)")
                            trade["boost_lock_notified"] = True

                    drop_from_peak = peak - pnl_pct
                    if drop_from_peak >= giveback and (r30 <= -0.40 or r90 <= 0.0):
                        trade["exit_in_progress"] = True; trade["last_exit_try"] = now
                        send_message(f"🔔 خروج {market} (Giveback {drop_from_peak:.2f}%≥{giveback:.2f}%)")
                        sell_trade(trade); trade["exit_in_progress"] = False
                        continue

                # ===== المرحلة المبكرة =====
                if age < EARLY_WINDOW_SEC:
                    if now < trade["grace_until"]:
                        crash_fast, d20 = _fast_drop_detect(trade, now, current)
                        if pnl_pct <= EARLY_CRASH_SL or (crash_fast and pnl_pct < -1.5):
                            trade["exit_in_progress"] = True; trade["last_exit_try"] = now
                            send_message(f"🔔 خروج {market} (Crash مبكر {pnl_pct:.2f}% | d{FAST_DROP_WINDOW}s={d20:.2f}%)")
                            sell_trade(trade); trade["exit_in_progress"] = False
                        continue

                    crash_fast, d20 = _fast_drop_detect(trade, now, current)
                    if MICRO_PROFIT_MIN <= pnl_pct <= MICRO_PROFIT_MAX and crash_fast and d20 <= -MICRO_FAST_DROP and r30 <= -0.7:
                        trade["exit_in_progress"] = True; trade["last_exit_try"] = now
                        send_message(f"🔔 خروج {market} (حماية ربح صغير {pnl_pct:.2f}% | d{FAST_DROP_WINDOW}s={d20:.2f}%)")
                        sell_trade(trade); trade["exit_in_progress"] = False
                        continue

                    if pnl_pct <= trade["sl_dyn"]:
                        prev = trade.get("sl_breach_at", 0.0)
                        if prev and (now - prev) >= 2.0:
                            trade["exit_in_progress"] = True; trade["last_exit_try"] = now
                            send_message(f"🔔 خروج {market} (SL مبكر {trade['sl_dyn']:.2f}% | الآن {pnl_pct:.2f}%)")
                            sell_trade(trade); trade["exit_in_progress"] = False
                        else:
                            trade["sl_breach_at"] = now
                        continue
                    else:
                        trade["sl_breach_at"] = 0.0

                    continue  # لا نفحص باقي الشروط ضمن النافذة المبكرة

                # ===== بعد 15 دقيقة =====
                if trade.get("exit_in_progress") and (now - trade.get("last_exit_try", 0)) < 15:
                    continue

                peak = trade.get("peak_pct", 0.0)
                lock_from_peak = peak - LATE_LOCK_BACKSTEP
                desired_lock = max(lock_from_peak, LATE_MIN_LOCK)
                if desired_lock > trade.get("sl_dyn", DYN_SL_START):
                    prev_lock = trade["sl_dyn"]
                    trade["sl_dyn"] = desired_lock
                    if (desired_lock - prev_lock) >= 0.4 and not trade.get("late_lock_notified"):
                        send_message(f"🔒 تفعيل قفل ربح {market}: SL ⇧ إلى {desired_lock:.2f}% (قمة {peak:.2f}%)")
                        trade["late_lock_notified"] = True

                # خروج: SL الحالي
                if pnl_pct <= trade["sl_dyn"]:
                    trade["exit_in_progress"] = True; trade["last_exit_try"] = now
                    send_message(f"🔔 خروج {market} (SL {trade['sl_dyn']:.2f}% | الآن {pnl_pct:.2f}%)")
                    sell_trade(trade); trade["exit_in_progress"] = False
                    continue

                # خروج: انعكاس زخم بعد قمة
                drop_from_peak = trade["peak_pct"] - pnl_pct
                if trade["peak_pct"] >= 1.0 and drop_from_peak >= DROP_FROM_PEAK_EXIT and r30 <= -0.15 and r90 <= 0.0:
                    trade["exit_in_progress"] = True; trade["last_exit_try"] = now
                    send_message(f"🔔 خروج {market} (انعكاس زخم: قمة {trade['peak_pct']:.2f}% → {pnl_pct:.2f}%)")
                    sell_trade(trade); trade["exit_in_progress"] = False
                    continue

                # خروج: توقّف تقدّم + زخم سلبي
                last_hi = trade.get("last_new_high", trade.get("opened_at", now))
                stalled = (now - last_hi) >= max(90, STALL_SEC)
                if stalled and r30 <= -0.10 and r90 <= -0.10 and pnl_pct > trade["sl_dyn"] + 0.3:
                    trade["exit_in_progress"] = True; trade["last_exit_try"] = now
                    send_message(f"🔔 خروج {market} (STALL {int(now-last_hi)}ث | r30 {r30:.2f}% r90 {r90:.2f}%)")
                    sell_trade(trade); trade["exit_in_progress"] = False
                    continue

                # قراءة دفتر الطلبات (اختياري – إبقيناها)
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

            peak_pct   = float(t.get("peak_pct", 0.0))
            dyn_sl     = float(t.get("sl_dyn", DYN_SL_START))
            last_hi_ts = float(t.get("last_new_high", opened))
            since_last_hi = int(now - last_hi_ts)
            drop_from_peak = max(0.0, peak_pct - pnl_pct)

            try:
                r30, r90, _ = _mom_metrics(t, current)
            except Exception:
                r30 = r90 = 0.0

            early_phase = age_sec < EARLY_WINDOW_SEC
            if early_phase:
                if now < t.get("grace_until", opened + GRACE_SEC):
                    state = "⏳ Grace — تجاهل الضجيج (بيع فقط عند كراش)"
                elif pnl_pct <= dyn_sl:
                    state = "🛑 قريب/ضرب SL المبكر (بتأكيد)"
                else:
                    state = "⏳ مراقبة مبكرة"
                lock_hint = f"SL الحالي: {dyn_sl:.2f}%"
            else:
                lock_hint = f"قفل ديناميكي ≥ {dyn_sl:.2f}% (قمة {peak_pct:.2f}%)"
                if peak_pct >= PEAK_TRIGGER and drop_from_peak >= _peak_giveback(peak_pct):
                    state = "🔔 خروج محتمل: Giveback كبير"
                elif r30 <= FAST_DROP_R30 or r90 <= FAST_DROP_R90:
                    state = "🔔 خروج محتمل: زخم سلبي"
                else:
                    state = "⏳ مراقبة/تريلينغ"

            emoji = "✅" if pnl_pct >= 0 else "❌"
            lines.append(f"{i}. {symbol}: €{entry:.6f} → €{current:.6f} {emoji} {pnl_pct:+.2f}% | منذ {age_min}د")
            lines.append(f"   • كمية: {amount:.5f} | أعلى: {peak_pct:.2f}% | SL: {dyn_sl:.2f}% | من آخر قمة: {since_last_hi}s")
            lines.append(f"   • زخم: r30 {r30:+.2f}% / r90 {r90:+.2f}% | {lock_hint} | حالة: {state}")

        floating_pnl_eur = total_value - total_cost
        floating_pnl_pct = ((total_value / total_cost) - 1.0) * 100 if total_cost > 0 else 0.0
        lines.append(f"💼 قيمة الصفقات: €{total_value:.2f} | عائم: {floating_pnl_eur:+.2f}€ ({floating_pnl_pct:+.2f}%)")
    else:
        lines.append("📌 لا توجد صفقات نشطة.")

    # ===== صفقات مكتملة بعد «انسى» — تحليل كامل + سرد كامل =====
    realized_pnl_eur = 0.0
    realized_pnl_pct_sum = 0.0
    realized_count = 0
    wins = 0
    losses = 0
    buy_fees = 0.0
    sell_fees = 0.0
    best_trade = None  # (pnl_eur, pnl_pct, sym)
    worst_trade = None
    max_win_streak = 0
    max_loss_streak = 0
    cur_win_streak = 0
    cur_loss_streak = 0

    since_ts = 0.0
    raw = r.get(SINCE_RESET_KEY)
    if raw:
        since_ts = float(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)

    # نمرّ من الأقدم للأحدث لحساب السلاسل (streaks) صح
    closed_since = []
    for t in exec_copy:
        if "pnl_eur" in t and "exit_time" in t and float(t["exit_time"]) >= since_ts:
            closed_since.append(t)

    closed_since.sort(key=lambda x: float(x["exit_time"]))  # من الأقدم للأحدث

    for t in closed_since:
        pnl_eur = float(t["pnl_eur"])
        pnl_pct = float(t.get("pnl_pct", 0.0))
        sym = t["symbol"].replace("-EUR","")
        buy_fees += float(t.get("buy_fee_eur", 0))
        sell_fees += float(t.get("sell_fee_eur", 0))

        realized_pnl_eur += pnl_eur
        realized_pnl_pct_sum += pnl_pct
        realized_count += 1

        # streaks
        if pnl_eur >= 0:
            wins += 1
            cur_win_streak += 1
            max_win_streak = max(max_win_streak, cur_win_streak)
            cur_loss_streak = 0
        else:
            losses += 1
            cur_loss_streak += 1
            max_loss_streak = max(max_loss_streak, cur_loss_streak)
            cur_win_streak = 0

        # best / worst
        if best_trade is None or pnl_eur > best_trade[0]:
            best_trade = (pnl_eur, pnl_pct, sym)
        if worst_trade is None or pnl_eur < worst_trade[0]:
            worst_trade = (pnl_eur, pnl_pct, sym)

    total_fees = buy_fees + sell_fees
    win_rate = (wins / realized_count * 100.0) if realized_count else 0.0
    avg_pnl_eur = (realized_pnl_eur / realized_count) if realized_count else 0.0
    avg_pnl_pct = (realized_pnl_pct_sum / realized_count) if realized_count else 0.0

    lines.append("\n📊 الصفقات المكتملة منذ آخر «انسى»:")
    if realized_count == 0:
        lines.append("• لا توجد صفقات مكتملة بعد آخر «انسى».")
    else:
        lines.append(f"• العدد: {realized_count} | ربح/خسارة محققة: {realized_pnl_eur:+.2f}€ | متوسط/صفقة: {avg_pnl_eur:+.2f}€ ({avg_pnl_pct:+.2f}%)")
        lines.append(f"• فوز/خسارة: {wins}/{losses} | نسبة الفوز: {win_rate:.1f}%")
        if best_trade:
            lines.append(f"• أفضل صفقة: {best_trade[2]} → {best_trade[0]:+.2f}€ ({best_trade[1]:+.2f}%)")
        if worst_trade:
            lines.append(f"• أسوأ صفقة: {worst_trade[2]} → {worst_trade[0]:+.2f}€ ({worst_trade[1]:+.2f}%)")
        lines.append(f"• سلاسل: أطول رابحة = {max_win_streak} | أطول خاسرة = {max_loss_streak}")
        lines.append(f"• الرسوم: المجموع {total_fees:.2f}€ (شراء: {buy_fees:.2f}€ / بيع: {sell_fees:.2f}€)")
        lines.append("\n🧾 كل الصفقات (من الأحدث للأقدم):")

        # نسرد من الأحدث للأقدم لقراءة أسهل
        for t in sorted(closed_since, key=lambda x: float(x["exit_time"]), reverse=True):
            sym = t["symbol"].replace("-EUR","")
            pnl_eur = float(t["pnl_eur"])
            pnl_pct = float(t.get("pnl_pct", 0.0))
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(t["exit_time"])))
            mark = "✅" if pnl_eur >= 0 else "❌"
            lines.append(f"- {ts} | {sym}: {mark} {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%)")

    # حد اليوم (مباشر من التخزين)
    lines.append(f"\n⛔ حد اليوم: {_today_pnl():+.2f}€ / {DAILY_STOP_EUR:+.2f}€")

    return "\n".join(lines)

def send_text_chunks(text: str, chunk_size: int = 3800):
    if not text:
        return
    buf = []
    cur = 0
    lines = text.splitlines(keepends=True)
    for ln in lines:
        if cur + len(ln) > chunk_size and buf:
            send_message("".join(buf))
            buf, cur = [], 0
        if len(ln) > chunk_size:
            # سطر أطول من الحد: قصّه قطع صغيرة
            i = 0
            while i < len(ln):
                part = ln[i:i+chunk_size]
                if part:
                    send_message(part)
                i += chunk_size
            buf, cur = [], 0
        else:
            buf.append(ln); cur += len(ln)
    if buf:
        send_message("".join(buf))

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
        send_text_chunks(build_summary())
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
            names = sorted(k.split("ban24:")[-1] for k in keys)
            send_message("🧊 العملات المحظورة 24h:\n- " + "\n- ".join(names))
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