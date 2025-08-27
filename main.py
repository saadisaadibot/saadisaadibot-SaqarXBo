# -*- coding: utf-8 -*-
import re
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

# ========= إعدادات =========
MAX_TRADES = 2

TAKE_PROFIT_HARD   = 1.5
LATE_FALLBACK_SEC  = 10 * 60
LATE_LOCK_BACKSTEP = 0.8
LATE_MIN_LOCK      = 0.5
LATE_WEAK_R        = 0.10

DYN_SL_START       = -2.0
DYN_SL_STEP        = 1.0

MOM_LOOKBACK_SEC   = 120
STALL_SEC          = 150
DROP_FROM_PEAK_EXIT= 1.2

MOM_R30_STRONG     = 0.50
MOM_R90_STRONG     = 0.80

SELL_RETRY_DELAY   = 5
SELL_MAX_RETRIES   = 6

EARLY_WINDOW_SEC   = 15 * 60

BLACKLIST_EXPIRE_SECONDS = 300
BUY_COOLDOWN_SEC   = 180  # غير مستخدم عمليًا هنا

# حماية سريعة
GRACE_SEC          = 45
EARLY_CRASH_SL     = -3.4
FAST_DROP_WINDOW   = 20
FAST_DROP_PCT      = 1.3

# ربح صغير
MICRO_PROFIT_MIN   = 0.7
MICRO_PROFIT_MAX   = 1.3
MICRO_FAST_DROP    = 1.1

# Trailing من القمة
GIVEBACK_RATIO     = 0.32
GIVEBACK_MIN       = 3.8
GIVEBACK_MAX       = 7.0
PEAK_TRIGGER       = 8.5

# زخم سلبي قوي
FAST_DROP_R30      = -1.3
FAST_DROP_R90      = -3.2

# حدود نزيف (لن نوقف الشراء بناءً عليها في هذه النسخة)
DAILY_STOP_EUR     = -8.0
CONSEC_LOSS_BAN    = 2

# ========= تهيئة عامة =========
load_dotenv()
app = Flask(__name__)
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BITVAVO_API_KEY = os.getenv("BITVAVO_API_KEY")
BITVAVO_API_SECRET = os.getenv("BITVAVO_API_SECRET")
r = redis.from_url(os.getenv("REDIS_URL"))
lock = Lock()
active_trades = []     # صفقات مفتوحة
executed_trades = []   # يُضاف عند الشراء ويُستكمل عند الإغلاق

SINCE_RESET_KEY = "nems:since_reset"

# ========= تلغرام (للتنبيهات فقط) =========
def send_message(text: str):
    try:
        if not BOT_TOKEN or not CHAT_ID:
            return
        key = "dedup:" + hashlib.sha1(text.encode("utf-8")).hexdigest()
        if r.setnx(key, 1):
            r.expire(key, 60)
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": text},
                timeout=6
            )
    except Exception as e:
        print("Telegram error:", e)

# ========= Bitvavo REST =========
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
            method, url, headers=headers,
            json=(body or {}) if method != "GET" else None,
            timeout=10 if method != "GET" else 6
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

# ========= WebSocket أسعار لحظية =========
import websocket
import threading
import traceback

WS_URL = "wss://ws.bitvavo.com/v2/"
_ws_prices = {}     # market -> {"price": float, "ts": epoch}
_ws_lock = Lock()
_ws_conn = None
_ws_running = False
_ws_wanted = set()  # الماركتات المطلوب تتبّعها

def _ws_subscribe_payload(markets):
    return {"action": "subscribe", "channels": [{"name": "ticker", "markets": markets}]}

def _ws_on_open(ws):
    try:
        mkts = list(_ws_wanted)
        if mkts:
            ws.send(json.dumps(_ws_subscribe_payload(mkts)))
    except Exception:
        traceback.print_exc()

def _ws_on_message(ws, message):
    try:
        msg = json.loads(message)
    except Exception:
        return
    if isinstance(msg, dict) and msg.get("event") in ("subscribe", "subscribed"):
        return
    if isinstance(msg, dict) and msg.get("event") == "ticker":
        market = msg.get("market")
        price  = msg.get("price") or msg.get("lastPrice") or msg.get("open")
        try:
            p = float(price)
            if p > 0:
                with _ws_lock:
                    _ws_prices[market] = {"price": p, "ts": time.time()}
        except Exception:
            pass

def _ws_on_error(ws, err):
    print("WS error:", err)

def _ws_on_close(ws, code, reason):
    global _ws_running
    _ws_running = False
    print("WS closed:", code, reason)

def _ws_thread():
    global _ws_conn, _ws_running
    while True:
        try:
            _ws_running = True
            _ws_conn = websocket.WebSocketApp(
                WS_URL,
                on_open=_ws_on_open,
                on_message=_ws_on_message,
                on_error=_ws_on_error,
                on_close=_ws_on_close
            )
            _ws_conn.run_forever(ping_interval=25, ping_timeout=10)
        except Exception as e:
            print("WS loop exception:", e)
        finally:
            _ws_running = False
            time.sleep(2)

Thread(target=_ws_thread, daemon=True).start()

def fetch_price_ws_first(market_symbol: str, staleness_sec: float = 2.0):
    now = time.time()
    with _ws_lock:
        rec = _ws_prices.get(market_symbol)
    if rec and now - rec["ts"] <= staleness_sec:
        return rec["price"]
    try:
        res = bitvavo_request("GET", f"/ticker/price?market={market_symbol}")
        price = float(res.get("price", 0) or 0)
        if price > 0:
            with _ws_lock:
                _ws_prices[market_symbol] = {"price": price, "ts": now}
            return price
    except Exception:
        pass
    return None

# ========= Fills =========
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
        fee_eur    += fee
    return total_base, total_eur, fee_eur

# ========= البيع (كما هو) =========
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

    try:
        base = market.replace("-EUR", "")
        if pnl_pct <= -3.0:
            r.setex(f"ban24:{base}", 24*3600, 1)
            send_message(f"🧊 تم حظر {base} لمدة 24 ساعة (خسارة {pnl_pct:.2f}%).")
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

# ========= مساعدة صغيرة: انتظر توقف الهبوط لحظيًا =========
def _wait_until_not_falling(market: str, max_wait=2.0, tick=0.05):
    """
    ينتظر حتى يتوقف الهبوط المتتالي (ثوانٍ قليلة) قبل الشراء.
    لا يغيّر أي شيء في body.
    """
    last = None
    flat_up_streak = 0
    t0 = time.time()
    while time.time() - t0 < max_wait:
        p = fetch_price_ws_first(market)
        if p is None:
            time.sleep(tick); continue
        if last is not None:
            if p >= last:
                flat_up_streak += 1
                if flat_up_streak >= 3:  # ثلاث تيكات غير هابطة تكفي
                    break
            else:
                flat_up_streak = 0
        last = p
        time.sleep(tick)

# ========= الشراء (بلا فلترة/حظر — body كما هو) =========
def buy(symbol: str):
    symbol = symbol.upper().strip()
    market = f"{symbol}-EUR"

    with lock:
        if any(t["symbol"] == market for t in active_trades):
            send_message(f"⛔ عندك صفقة مفتوحة على {symbol}.")
            return
        if len(active_trades) >= MAX_TRADES:
            send_message("🚫 وصلنا الحد الأقصى (صفقتان).")
            return

    eur_avail = get_eur_available()
    if eur_avail <= 0:
        send_message("💤 لا يوجد رصيد EUR متاح للشراء.")
        return

    amount_quote = eur_avail/2.0 if len(active_trades)==0 else eur_avail
    amount_quote = round(amount_quote, 2)
    if amount_quote < 5.0:
        send_message(f"⚠️ المبلغ المتاح صغير (€{amount_quote:.2f}).")
        return

    # انتظار وجيز لتوقف الهبوط ثم تنفيذ مباشر
    _wait_until_not_falling(market, max_wait=2.0, tick=0.05)

    # IMPORTANT: لا تغيير في الـ body
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
        send_message(f"❌ فشل شراء {symbol}")
        return

    fills = res.get("fills", [])
    print("📦 BUY FILLS:", json.dumps(fills, ensure_ascii=False))

    tb, tq_eur, fee_eur = totals_from_fills_eur(fills)
    amount_net = tb
    cost_eur   = tq_eur + fee_eur
    if amount_net <= 0 or cost_eur <= 0:
        send_message(f"❌ فشل شراء {symbol} - بيانات fills غير صالحة")
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

    with _ws_lock:
        _ws_wanted.add(market)

    slot_idx = len(active_trades)
    send_message(f"✅ شراء {symbol} | صفقة #{slot_idx}/2 | قيمة: €{amount_quote:.2f} | SL مبكر مفعّل")

# ========= تاريخ لحظي + مؤشرات (كما هو) =========
def _init_hist(trade):
    if "hist" not in trade:
        trade["hist"] = deque(maxlen=600)
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

# ========= حلقة المراقبة (كما هي) =========
def monitor_loop():
    while True:
        try:
            with lock:
                snapshot = list(active_trades)
            now = time.time()
            for trade in snapshot:
                market = trade["symbol"]
                entry  = float(trade["entry"])
                current = fetch_price_ws_first(market)
                if not current:
                    continue

                _update_hist(trade, now, current)
                r30, r90, _ = _mom_metrics(trade, current)

                pnl_pct = ((current - entry) / entry) * 100.0
                trade["peak_pct"] = max(trade.get("peak_pct", 0.0), pnl_pct)

                # خروج فوري عند ربح ثابت
                if pnl_pct >= TAKE_PROFIT_HARD:
                    trade["exit_in_progress"] = True
                    trade["last_exit_try"] = now
                    send_message(f"🔔 خروج {market} (هدف ثابت {pnl_pct:.2f}%≥{TAKE_PROFIT_HARD:.2f}%)")
                    sell_trade(trade)
                    trade["exit_in_progress"] = False
                    continue

                # SL سلّمي أساسي
                inc = int(max(0.0, pnl_pct) // 1)
                dyn_sl_base = DYN_SL_START + inc * DYN_SL_STEP
                trade["sl_dyn"] = max(trade.get("sl_dyn", DYN_SL_START), dyn_sl_base)

                # قفل ربحي مبكّر متدرّج
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

                # Trailing من القمة
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

                # المرحلة المبكرة
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

                    continue

                # بعد 15 دقيقة
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

                if pnl_pct <= trade["sl_dyn"]:
                    trade["exit_in_progress"] = True; trade["last_exit_try"] = now
                    send_message(f"🔔 خروج {market} (SL {trade['sl_dyn']:.2f}% | الآن {pnl_pct:.2f}%)")
                    sell_trade(trade); trade["exit_in_progress"] = False
                    continue

                drop_from_peak = trade["peak_pct"] - pnl_pct
                if trade["peak_pct"] >= 1.0 and drop_from_peak >= DROP_FROM_PEAK_EXIT and r30 <= -0.15 and r90 <= 0.0:
                    trade["exit_in_progress"] = True; trade["last_exit_try"] = now
                    send_message(f"🔔 خروج {market} (انعكاس زخم: قمة {trade['peak_pct']:.2f}% → {pnl_pct:.2f}%)")
                    sell_trade(trade); trade["exit_in_progress"] = False
                    continue

                last_hi = trade.get("last_new_high", trade.get("opened_at", now))
                stalled = (now - last_hi) >= max(90, STALL_SEC)
                if stalled and r30 <= -0.10 and r90 <= -0.10 and pnl_pct > trade["sl_dyn"] + 0.3:
                    trade["exit_in_progress"] = True; trade["last_exit_try"] = now
                    send_message(f"🔔 خروج {market} (STALL {int(now-last_hi)}ث | r30 {r30:.2f}% r90 {r90:.2f}%)")
                    sell_trade(trade); trade["exit_in_progress"] = False
                    continue

                # حائط بيع واضح
                try:
                    ob = requests.get(f"https://api.bitvavo.com/v2/{market}/book", timeout=4).json()
                    if ob and ob.get("asks") and ob.get("bids"):
                        ask_wall = float(ob["asks"][0][1])
                        bid_wall  = float(ob["bids"][0][1])
                        if ask_wall > bid_wall * 2 and pnl_pct > 0.5:
                            send_message(f"⚠️ {market} حائط بيع ضخم ({ask_wall:.0f} ضد {bid_wall:.0f}) → خروج حذر")
                            trade["exit_in_progress"] = True; trade["last_exit_try"] = now
                            sell_trade(trade); trade["exit_in_progress"] = False
                            continue
                except Exception:
                    pass

            time.sleep(0.25)
        except Exception as e:
            print("خطأ في المراقبة:", e)
            time.sleep(1)

Thread(target=monitor_loop, daemon=True).start()

# ========= Endpoint بسيط لإشارات الشراء =========
@app.route("/signal", methods=["POST"])
def signal_in():
    data = request.get_json(silent=True) or {}
    sym = (data.get("symbol") or data.get("coin") or "").upper().strip()
    if not sym:
        return "no symbol", 400
    Thread(target=buy, args=(sym,), daemon=True).start()
    return "ok"

# ========= تحميل الحالة =========
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

# ========= تشغيل محلي =========
if __name__ == "__main__" and os.getenv("RUN_LOCAL") == "1":
    app.run(host="0.0.0.0", port=5000)