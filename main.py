# -*- coding: utf-8 -*-
import os, time, json, hmac, hashlib, requests, threading, traceback
from uuid import uuid4
from collections import deque
from threading import Thread, Lock
from flask import Flask, request

# ================= إعدادات سريعة =================
MAX_TRADES = 2

# ربح فوري صغير جداً + SL ديناميكي كما هو
TAKE_PROFIT_HARD   = 1.0        # بيع فوري عند +1%
DYN_SL_START       = -2.0
DYN_SL_STEP        = 1.0

# نُبقي منطقك الأصلي للمرحلة المبكرة والحماية والزخم
EARLY_WINDOW_SEC   = 15 * 60
GRACE_SEC          = 45
EARLY_CRASH_SL     = -3.4
FAST_DROP_WINDOW   = 20
FAST_DROP_PCT      = 1.3
MICRO_PROFIT_MIN   = 0.7
MICRO_PROFIT_MAX   = 1.3
MICRO_FAST_DROP    = 1.1

GIVEBACK_RATIO     = 0.32
GIVEBACK_MIN       = 3.8
GIVEBACK_MAX       = 7.0
PEAK_TRIGGER       = 8.5

MOM_LOOKBACK_SEC   = 120
STALL_SEC          = 150
DROP_FROM_PEAK_EXIT= 1.2
FAST_DROP_R30      = -1.3
FAST_DROP_R90      = -3.2

SELL_RETRY_DELAY   = 0.15       # سريع
SELL_MAX_RETRIES   = 2

# ================= مفاتيح/جلسة =================
BITVAVO_API_KEY    = os.getenv("BITVAVO_API_KEY")
BITVAVO_API_SECRET = os.getenv("BITVAVO_API_SECRET")

app  = Flask(__name__)
lock = Lock()

active_trades   = []   # صفقات مفتوحة
executed_trades = []   # تاريخ

# طباعة فقط بدل تلغرام (بدون شبكات إضافية)
def send_message(msg: str):
    print(msg, flush=True)

# جلسة HTTP سريعة
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64, max_retries=0)
session.mount("https://", adapter)

def create_signature(timestamp, method, path, body_str=""):
    msg = f"{timestamp}{method}{path}{body_str}"
    return hmac.new(BITVAVO_API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def bitvavo_request(method: str, path: str, body=None):
    timestamp = str(int(time.time() * 1000))
    url = f"https://api.bitvavo.com/v2{path}"
    body_str = "" if method == "GET" else json.dumps(body or {}, separators=(',', ':'))
    sig = create_signature(timestamp, method, f"/v2{path}", body_str)
    headers = {
        'Bitvavo-Access-Key': BITVAVO_API_KEY,
        'Bitvavo-Access-Timestamp': timestamp,
        'Bitvavo-Access-Signature': sig,
        'Bitvavo-Access-Window': '10000'
    }
    resp = session.request(method, url, headers=headers,
                           json=(body or {}) if method != "GET" else None,
                           timeout=3 if method != "GET" else 4)
    return resp.json()

def get_eur_available() -> float:
    try:
        balances = bitvavo_request("GET", "/balance")
        for b in balances:
            if b.get("symbol") == "EUR":
                return max(0.0, float(b.get("available", 0) or 0))
    except Exception:
        pass
    return 0.0

# ================= WS أسعار لحظية =================
import websocket
WS_URL = "wss://ws.bitvavo.com/v2/"

_ws_prices = {}  # market -> {"price": float, "ts": epoch}
_ws_lock = Lock()
_ws_conn = None
_ws_running = False
_ws_wanted = set()

def _ws_subscribe_payload(markets):
    return {"action": "subscribe", "channels": [{"name": "ticker", "markets": markets}]}

def _ws_on_open(ws):
    try:
        mkts = list(_ws_wanted)
        if mkts: ws.send(json.dumps(_ws_subscribe_payload(mkts)))
    except Exception:
        traceback.print_exc()

def _ws_on_message(ws, message):
    try:
        msg = json.loads(message)
        if isinstance(msg, dict) and msg.get("event") == "ticker":
            market = msg.get("market")
            price = msg.get("price") or msg.get("lastPrice") or msg.get("open")
            p = float(price)
            if p > 0:
                with _ws_lock:
                    _ws_prices[market] = {"price": p, "ts": time.time()}
    except Exception:
        pass

def _ws_on_error(ws, err): print("WS error:", err)
def _ws_on_close(ws, code, reason):
    global _ws_running; _ws_running=False; print("WS closed:", code, reason)

def _ws_thread():
    global _ws_conn, _ws_running
    while True:
        try:
            _ws_running=True
            _ws_conn = websocket.WebSocketApp(
                WS_URL, on_open=_ws_on_open, on_message=_ws_on_message,
                on_error=_ws_on_error, on_close=_ws_on_close
            )
            _ws_conn.run_forever(ping_interval=25, ping_timeout=10)
        except Exception as e:
            print("WS loop exception:", e)
        finally:
            _ws_running=False
            time.sleep(1.5)

def _ws_manager_thread():
    last = set()
    while True:
        try:
            with lock:
                wanted = sorted(set(t["symbol"] for t in active_trades))
            add = list(set(wanted) - last)
            rem = list(last - set(wanted))
            if _ws_conn and _ws_running:
                if add: _ws_conn.send(json.dumps(_ws_subscribe_payload(add)))
                if rem: _ws_conn.send(json.dumps({"action":"unsubscribe","channels":[{"name":"ticker","markets":rem}]}))
            last = set(wanted)
            with _ws_lock:
                _ws_wanted.clear(); _ws_wanted.update(last)
        except Exception as e:
            print("ws_manager error:", e)
        time.sleep(0.5)

Thread(target=_ws_thread, daemon=True).start()
Thread(target=_ws_manager_thread, daemon=True).start()

def fetch_price_ws_first(market: str, staleness_sec: float = 0.7):
    now = time.time()
    with _ws_lock:
        rec = _ws_prices.get(market)
    if rec and now - rec["ts"] <= staleness_sec:
        return rec["price"]
    try:
        res = bitvavo_request("GET", f"/ticker/price?market={market}")
        p = float(res.get("price", 0) or 0)
        if p > 0:
            with _ws_lock:
                _ws_prices[market] = {"price": p, "ts": now}
            return p
    except Exception:
        pass
    return None

# ================= أدوات مساعدة من منطقك =================
from collections import deque
def _init_hist(trade):
    if "hist" not in trade: trade["hist"] = deque(maxlen=600)
    if "last_new_high" not in trade: trade["last_new_high"] = trade.get("opened_at", time.time())

def _update_hist(trade, now_ts, price):
    _init_hist(trade); trade["hist"].append((now_ts, price))
    cutoff = now_ts - MOM_LOOKBACK_SEC
    while trade["hist"] and trade["hist"][0][0] < cutoff: trade["hist"].popleft()

def _mom_metrics(trade, price_now):
    _init_hist(trade)
    if not trade["hist"]: return 0.0, 0.0, False
    now_ts = trade["hist"][-1][0]
    p30 = p90 = None; hi = lo = price_now
    for ts, p in trade["hist"]:
        hi = max(hi, p); lo = min(lo, p); age = now_ts - ts
        if p30 is None and age >= 30: p30 = p
        if p90 is None and age >= 90: p90 = p
    if p30 is None: p30 = trade["hist"][0][1]
    if p90 is None: p90 = trade["hist"][0][1]
    r30 = (price_now / p30 - 1.0) * 100.0 if p30>0 else 0.0
    r90 = (price_now / p90 - 1.0) * 100.0 if p90>0 else 0.0
    new_high = price_now >= hi * 0.999
    if new_high: trade["last_new_high"] = now_ts
    return r30, r90, new_high

def _price_n_seconds_ago(trade, now_ts, sec):
    cutoff = now_ts - sec
    for ts, p in reversed(trade.get("hist", [])):
        if ts <= cutoff: return p
    return None

def _fast_drop_detect(trade, now_ts, price_now):
    p20 = _price_n_seconds_ago(trade, now_ts, FAST_DROP_WINDOW)
    if p20 and p20>0:
        dpct = (price_now/p20 - 1.0)*100.0
        return dpct <= -FAST_DROP_PCT, dpct
    return False, 0.0

def _peak_giveback(peak): return max(GIVEBACK_MIN, min(GIVEBACK_MAX, GIVEBACK_RATIO*peak))

# ================= تنفيذ أوامر البيع/الشراء =================
def place_market_buy_quote(market, amount_quote_eur: float):
    body = {
        "market": market, "side": "buy", "orderType": "market",
        "amountQuote": f"{amount_quote_eur:.2f}",
        "clientOrderId": str(uuid4()), "operatorId": ""
    }
    return bitvavo_request("POST", "/order", body)

def place_market_sell(market, amt_base):
    body = {
        "market": market, "side": "sell", "orderType": "market",
        "amount": f"{amt_base:.10f}",
        "clientOrderId": str(uuid4()), "operatorId": ""
    }
    return bitvavo_request("POST", "/order", body)

def totals_from_fills_eur(fills):
    tb=tq=fee=0.0
    for f in (fills or []):
        amt=float(f["amount"]); price=float(f["price"]); fee_f=float(f.get("fee",0) or 0)
        tb+=amt; tq+=amt*price; fee+=fee_f
    return tb, tq, fee

def sell_trade(trade: dict):
    market = trade["symbol"]; amt = float(trade.get("amount",0) or 0)
    if amt<=0: return
    ok=False; resp=None
    for _ in range(SELL_MAX_RETRIES):
        resp = place_market_sell(market, amt)
        if isinstance(resp, dict) and resp.get("status")=="filled":
            ok=True; break
        time.sleep(SELL_RETRY_DELAY)
    if not ok:
        send_message(f"❌ فشل بيع {market}")
        return

    fills = resp.get("fills", [])
    tb, tq_eur, fee_eur = totals_from_fills_eur(fills)
    proceeds_eur = tq_eur - fee_eur
    orig_cost = float(trade.get("cost_eur", trade["entry"]*trade["amount"]))
    pnl_eur = proceeds_eur - orig_cost
    pnl_pct = (proceeds_eur / orig_cost - 1.0) * 100.0

    send_message(f"💰 بيع {market} | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%)")

    with lock:
        try: active_trades.remove(trade)
        except ValueError: pass

def sell_trade_async(trade): Thread(target=sell_trade, args=(trade,), daemon=True).start()

# --- انتظار استقرار قصير قبل الشراء إذا كان هابط ---
def wait_stabilize_before_buy(market: str, max_wait=5.0, tick=0.05):
    """
    ينتظر بضعة ثوانٍ إذا كان السعر يهبط بشكل واضح؛ يشتري بمجرد أن يتوقف الهبوط
    (آخر 3 قراءات غير أدنى من السابقة) أو ينتهي max_wait.
    """
    prices = deque(maxlen=4)
    start = time.time()
    last = None
    while time.time() - start < max_wait:
        p = fetch_price_ws_first(market)
        if not p:
            time.sleep(tick); continue
        prices.append(p)
        if len(prices) >= 4:
            # تحقق أن الهبوط توقّف: p3>=p2 أو على الأقل لم يعد نزول متتالي
            if not (prices[3] < prices[2] < prices[1] < prices[0]):
                break
        last = p
        time.sleep(tick)

# ================= BUY (منطقك مع تبسيط وتسريع) =================
def buy(symbol: str):
    symbol = symbol.upper().strip()
    market = f"{symbol}-EUR"

    with lock:
        if any(t["symbol"] == market for t in active_trades):
            send_message(f"⛔ صفقة موجودة على {symbol}."); return
        if len(active_trades) >= MAX_TRADES:
            send_message("🚫 الحد صفقتان."); return

    # تقسيم الرصيد نصف/نصف
    eur_avail = get_eur_available()
    if eur_avail <= 0:
        send_message("💤 لا يوجد EUR كافي."); return
    if len(active_trades) == 0:
        amount_quote = eur_avail/2.0
    else:
        amount_quote = eur_avail
    amount_quote = round(amount_quote, 2)
    if amount_quote < 5.0:
        send_message(f"⚠️ المبلغ صغير (€{amount_quote:.2f})."); return

    # إذا هابط، انتظر استقراراً لحظياً ثم اشترِ
    wait_stabilize_before_buy(market)

    res = place_market_buy_quote(market, amount_quote)
    if not (isinstance(res, dict) and res.get("status")=="filled"):
        send_message(f"❌ فشل شراء {symbol}"); return

    fills = res.get("fills", [])
    tb, tq_eur, fee_eur = totals_from_fills_eur(fills)
    amount_net = tb; cost_eur = tq_eur + fee_eur
    if amount_net<=0 or cost_eur<=0:
        send_message(f"❌ فشل شراء {symbol} - بيانات fills"); return
    avg_price_incl_fees = cost_eur/amount_net

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
    with _ws_lock:
        _ws_wanted.add(market)

    send_message(f"✅ شراء {symbol} بقيمة €{amount_quote:.2f}")

# ================= حلقة مراقبة/بيع سريعة =================
def monitor_loop():
    while True:
        try:
            with lock: snapshot = list(active_trades)
            now = time.time()
            for trade in snapshot:
                market = trade["symbol"]
                entry  = float(trade["entry"])
                current = fetch_price_ws_first(market)
                if not current: continue

                _update_hist(trade, now, current)
                r30, r90, _ = _mom_metrics(trade, current)

                pnl_pct = ((current - entry) / entry) * 100.0
                trade["peak_pct"] = max(trade.get("peak_pct", 0.0), pnl_pct)

                # (1) هدف فوري صغير جداً
                if pnl_pct >= TAKE_PROFIT_HARD:
                    trade["exit_in_progress"] = True
                    sell_trade_async(trade)
                    continue

                # (2) SL سلّمي من منطقك
                inc = int(max(0.0, pnl_pct) // 1)
                dyn_sl_base = DYN_SL_START + inc * DYN_SL_STEP
                trade["sl_dyn"] = max(trade.get("sl_dyn", DYN_SL_START), dyn_sl_base)

                if pnl_pct >= 3.0:
                    trade["sl_dyn"] = max(trade["sl_dyn"], 1.6)
                elif pnl_pct >= 2.0:
                    trade["sl_dyn"] = max(trade["sl_dyn"], 0.8)
                elif pnl_pct >= 1.2:
                    trade["sl_dyn"] = max(trade["sl_dyn"], 0.1)

                age = now - trade.get("opened_at", now)
                if "grace_until" not in trade: trade["grace_until"] = trade.get("opened_at", now) + GRACE_SEC
                if "sl_breach_at" not in trade: trade["sl_breach_at"] = 0.0

                # Trailing من القمة
                peak = trade.get("peak_pct", 0.0)
                if peak >= PEAK_TRIGGER:
                    giveback = _peak_giveback(peak)
                    min_lock = peak - giveback
                    if min_lock > trade["sl_dyn"]:
                        trade["sl_dyn"] = min_lock
                    drop_from_peak = peak - pnl_pct
                    if drop_from_peak >= giveback and (r30 <= -0.40 or r90 <= 0.0):
                        trade["exit_in_progress"] = True
                        sell_trade_async(trade)
                        continue

                # المرحلة المبكرة
                if age < EARLY_WINDOW_SEC:
                    if now < trade["grace_until"]:
                        crash_fast, d20 = _fast_drop_detect(trade, now, current)
                        if pnl_pct <= EARLY_CRASH_SL or (crash_fast and pnl_pct < -1.5):
                            trade["exit_in_progress"] = True
                            sell_trade_async(trade)
                        continue

                    crash_fast, d20 = _fast_drop_detect(trade, now, current)
                    if MICRO_PROFIT_MIN <= pnl_pct <= MICRO_PROFIT_MAX and crash_fast and d20 <= -MICRO_FAST_DROP and r30 <= -0.7:
                        trade["exit_in_progress"] = True
                        sell_trade_async(trade)
                        continue

                    if pnl_pct <= trade["sl_dyn"]:
                        prev = trade.get("sl_breach_at", 0.0)
                        if prev and (now - prev) >= 2.0:
                            trade["exit_in_progress"] = True
                            sell_trade_async(trade)
                        else:
                            trade["sl_breach_at"] = now
                        continue
                    else:
                        trade["sl_breach_at"] = 0.0
                    continue

                # بعد المرحلة المبكرة: أقفال وربح سريع
                peak = trade.get("peak_pct", 0.0)
                lock_from_peak = peak - 0.8
                trade["sl_dyn"] = max(trade.get("sl_dyn", DYN_SL_START), max(lock_from_peak, 0.5))

                if pnl_pct <= trade["sl_dyn"]:
                    trade["exit_in_progress"] = True
                    sell_trade_async(trade)
                    continue

                drop_from_peak = trade["peak_pct"] - pnl_pct
                if trade["peak_pct"] >= 1.0 and drop_from_peak >= DROP_FROM_PEAK_EXIT and r30 <= -0.15 and r90 <= 0.0:
                    trade["exit_in_progress"] = True
                    sell_trade_async(trade)
                    continue

                last_hi = trade.get("last_new_high", trade.get("opened_at", now))
                stalled = (now - last_hi) >= max(90, STALL_SEC)
                if stalled and r30 <= -0.10 and r90 <= -0.10 and pnl_pct > trade["sl_dyn"] + 0.3:
                    trade["exit_in_progress"] = True
                    sell_trade_async(trade)
                    continue

            time.sleep(0.05)  # سريع جداً
        except Exception as e:
            print("خطأ في المراقبة:", e)
            time.sleep(0.5)

Thread(target=monitor_loop, daemon=True).start()

# ================= واجهة بسيطة لاستقبال الإشارة =================
# POST /signal  JSON: {"symbol":"ADA"}
@app.route("/signal", methods=["POST"])
def signal():
    data = request.get_json(silent=True) or {}
    sym = (data.get("symbol") or "").upper().strip()
    if not sym: return {"ok": False, "err": "missing symbol"}, 400
    Thread(target=buy, args=(sym,), daemon=True).start()
    return {"ok": True, "accepted": sym}

# تشغيل محلي
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))