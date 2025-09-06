# -*- coding: utf-8 -*-
"""
Simple Signal Executor — Maker Buy (Bitvavo EUR) + Fast Market Sell
- نفس منطق النسخة القديمة، مع تقوية فولباك الشراء Market كي لا يفشل.
"""

import os, re, time, json, traceback, math
import requests, redis, websocket
from threading import Thread, Lock
from collections import deque
from uuid import uuid4
from flask import Flask, request
from dotenv import load_dotenv

# ========= Boot / ENV =========
load_dotenv()
app = Flask(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHAT_ID     = os.getenv("CHAT_ID")          # اختياري؛ لو تركته فاضي رح نطبع بدل الإرسال
API_KEY     = os.getenv("BITVAVO_API_KEY")
API_SECRET  = os.getenv("BITVAVO_API_SECRET")
REDIS_URL   = os.getenv("REDIS_URL")
RUN_LOCAL   = os.getenv("RUN_LOCAL", "0") == "1"

r  = redis.from_url(REDIS_URL) if REDIS_URL else redis.Redis()
lk = Lock()

BASE_URL = "https://api.bitvavo.com/v2"
WS_URL   = "wss://ws.bitvavo.com/v2/"

# ========= Settings =========
MAX_TRADES            = 1
MAKER_BID_OFFSET_BP   = 10.0
MAKER_REPRICE_EVERY   = 0.8
MAKER_WAIT_TOTAL_SEC  = 20    # ← بالثواني
SELL_MARKET_ALWAYS    = True         # البيع دائمًا Market (حسب رغبتك)

SL_PCT                = -3.0
TRAIL_ACTIVATE_PCT    = +3.0
TRAIL_GIVEBACK_PCT    = 1.0

BUY_MIN_EUR           = 5.0          # أقل قيمة شراء
WS_STALENESS_SEC      = 2.0

# فولباك ماركت (تحسين فقط)
MARKET_CONFIRM_TIMEOUT= 15.0         # ننتظر تعبئة حتى 15ث لو رجع orderId بلا fills
MARKET_RETRIES        = 2            # محاولات إضافية
MARKET_SAFETY         = 0.995        # خصم 0.5% لتجاوز الحد الأدنى/الرسوم
POLL_INTERVAL         = 0.35

# ========= Runtime =========
enabled        = True
signals_on     = True          # شغّل/أطفئ التقاط إشارات القنوات المعاد توجيهها
active_trade   = None          # dict أو None
executed_trades= []
MARKET_MAP     = {}            # 'ADA' -> 'ADA-EUR'
_ws_prices     = {}
_ws_lock       = Lock()

# ========= Utils =========
def send_message(text: str):
    """يرسل تلغرام أو يطبع محليًا."""
    try:
        if BOT_TOKEN and CHAT_ID:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": text}, timeout=8)
        else:
            print("TG:", text)
    except Exception as e:
        print("TG err:", e)

def create_sig(ts, method, path, body_str=""):
    import hmac, hashlib
    msg = f"{ts}{method}{path}{body_str}"
    return hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def bv_request(method, path, body=None, timeout=10):
    url = f"{BASE_URL}{path}"
    ts  = str(int(time.time()*1000))
    body_str = "" if method=="GET" else json.dumps(body or {}, separators=(',',':'))
    sig = create_sig(ts, method, f"/v2{path}", body_str)
    headers = {
        'Bitvavo-Access-Key': API_KEY,
        'Bitvavo-Access-Timestamp': ts,
        'Bitvavo-Access-Signature': sig,
        'Bitvavo-Access-Window': '10000'
    }
    try:
        resp = requests.request(method, url, headers=headers,
                                json=(body or {}) if method!="GET" else None,
                                timeout=timeout)
        return resp.json()
    except Exception as e:
        print("bv_request err:", e)
        return {"error":"request_failed"}

def get_eur_available() -> float:
    try:
        bals = bv_request("GET", "/balance")
        if isinstance(bals, list):
            for b in bals:
                if b.get("symbol") == "EUR":
                    return max(0.0, float(b.get("available",0) or 0))
    except Exception:
        pass
    return 0.0

def load_markets():
    """ابني خريطة COIN -> COIN-EUR مرة كل تشغيل."""
    global MARKET_MAP
    try:
        rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
        m = {}
        for r0 in rows:
            base = r0.get("base")
            quote= r0.get("quote")
            market= r0.get("market")
            if base and quote=="EUR":
                m[base.upper()] = market
        if m: MARKET_MAP = m
    except Exception as e:
        print("load_markets err:", e)

def coin_to_market(coin: str):
    if not MARKET_MAP:
        load_markets()
    return MARKET_MAP.get(coin.upper())

# ========= Prices (WS + fallback) =========
def _ws_on_open(ws):
    pass

def _ws_on_message(ws, msg):
    try:
        data = json.loads(msg)
    except Exception:
        return
    if isinstance(data, dict) and data.get("event") == "ticker":
        m = data.get("market")
        price = data.get("price") or data.get("lastPrice") or data.get("open")
        try:
            p = float(price)
            if p > 0:
                with _ws_lock:
                    _ws_prices[m] = {"price": p, "ts": time.time()}
        except Exception:
            pass

def _ws_on_error(ws, err): print("WS error:", err)
def _ws_on_close(ws, c, r): pass

def _ws_thread():
    while True:
        try:
            ws = websocket.WebSocketApp(
                WS_URL, on_open=_ws_on_open, on_message=_ws_on_message,
                on_error=_ws_on_error, on_close=_ws_on_close
            )
            ws.run_forever(ping_interval=25, ping_timeout=10)
        except Exception as e:
            print("WS loop ex:", e)
        time.sleep(2)

Thread(target=_ws_thread, daemon=True).start()

def ws_sub(markets):
    if not markets: return
    try:
        payload = {"action":"subscribe","channels":[{"name":"ticker","markets":markets}]}
        ws = websocket.create_connection(WS_URL, timeout=5)
        ws.send(json.dumps(payload))
        ws.close()
    except Exception:
        pass

def fetch_price_ws_first(market: str, staleness=WS_STALENESS_SEC):
    now = time.time()
    with _ws_lock:
        rec = _ws_prices.get(market)
    if rec and (now - rec["ts"]) <= staleness:
        return rec["price"]
    ws_sub([market])
    try:
        j = requests.get(f"{BASE_URL}/ticker/price?market={market}", timeout=5).json()
        p = float(j.get("price", 0) or 0)
        if p > 0:
            with _ws_lock:
                _ws_prices[market] = {"price": p, "ts": now}
            return p
    except Exception:
        pass
    return None

def fetch_orderbook(market):
    try:
        j = requests.get(f"{BASE_URL}/{market}/book", timeout=6).json()
        if j and j.get("bids") and j.get("asks"):
            return j
    except Exception:
        pass
    return None

# ========= Orders =========
def totals_from_fills(fills):
    tb=tq=fee=0.0
    for f in (fills or []):
        amt=float(f["amount"]); price=float(f["price"]); fe=float(f.get("fee",0) or 0)
        tb+=amt; tq+=amt*price; fee+=fe
    return tb, tq, fee

def _place_limit_postonly(market, side, price, amount=None, amountQuote=None):
    body={"market":market,"side":side,"orderType":"limit","postOnly":True,
          "clientOrderId":str(uuid4()),"operatorId":"", "price":f"{price:.10f}"}
    if side=="buy": body["amountQuote"]=f"{amountQuote:.2f}"
    else:           body["amount"]=f"{amount:.10f}"
    return bv_request("POST","/order", body)

def _place_market(market, side, amount=None, amountQuote=None):
    body={"market":market,"side":side,"orderType":"market",
          "clientOrderId":str(uuid4()),"operatorId":""}
    if side=="buy": body["amountQuote"]=f"{amountQuote:.2f}"
    else:           body["amount"]=f"{amount:.10f}"
    return bv_request("POST","/order", body)

def _fetch_order(orderId):   return bv_request("GET",    f"/order?orderId={orderId}")
def _cancel_order(orderId):  return bv_request("DELETE", f"/order?orderId={orderId}")

# ========= Market fallback (المحسّن) =========
def _safe_quote(eur: float) -> float:
    # خصم بسيط لتفادي رفض الحد الأدنى/الرسوم + التقريب لليورو سنت
    return max(BUY_MIN_EUR, round(eur * MARKET_SAFETY, 2))

def market_buy_with_confirm(market: str, eur_amount: float):
    """
    تنفيذ Market مع:
    - هامش أمان على amountQuote
    - انتظار وصول fills عبر orderId حتى MARKET_CONFIRM_TIMEOUT
    - إعادة محاولة تلقائية MARKET_RETRIES مرة عند عدم التعبئة/الرفض
    """
    amt_q = _safe_quote(eur_amount)
    last_err = None

    for attempt in range(1, MARKET_RETRIES + 2):  # المحاولة الأولى + retries
        res = _place_market(market, "buy", amountQuote=amt_q)
        if res.get("error") or res.get("code"):
            last_err = res.get("error") or res.get("message") or str(res)
            send_message(f"⚠️ رفض Market buy (محاولة {attempt}): {last_err}")
            time.sleep(0.6)
            amt_q = _safe_quote(amt_q)  # خصم إضافي بسيط للمحاولة التالية
            continue

        fills = res.get("fills") or []
        oid   = res.get("orderId")
        status= res.get("status")

        # انتظر وصول التعبئة إن لم تأتِ فورًا
        deadline = time.time() + MARKET_CONFIRM_TIMEOUT
        while not fills and oid and time.time() < deadline:
            time.sleep(POLL_INTERVAL)
            st = _fetch_order(oid)
            status = st.get("status") or status
            if status in ("filled", "partiallyFilled"):
                fills = st.get("fills", [])
                break
            if status in ("canceled","rejected"):
                last_err = status; break

        base_amt, quote_eur, fee_eur = totals_from_fills(fills)
        if base_amt > 0:
            avg = (quote_eur + fee_eur) / base_amt
            return {"amount": base_amt, "avg": avg, "cost_eur": quote_eur + fee_eur, "fee_eur": fee_eur}

        send_message(f"⚠️ Market buy بلا تعبئة (محاولة {attempt}) — status={status}")
        time.sleep(0.8)

    send_message(f"❌ فشل الشراء Market بعد محاولات متعددة. {last_err or ''}")
    return None

# ========= Trade Ops =========
def open_maker_buy(market: str, eur_amount: float):
    """يحاول Maker فقط لمدة محدودة مع إعادة تسعير؛ إن لم ينجح يرجع None."""
    if eur_amount < BUY_MIN_EUR:
        send_message(f"⛔ المبلغ أقل من {BUY_MIN_EUR}€.")
        return None

    ob = fetch_orderbook(market)
    if not ob:
        send_message("⛔ لا يمكن قراءة دفتر الأوامر.")
        return None

    started = time.time()
    last_order = None
    fills = []

    try:
        while time.time() - started < MAKER_WAIT_TOTAL_SEC:
            ob = fetch_orderbook(market)
            best_bid = float(ob["bids"][0][0])
            tick_price = best_bid * (1 + MAKER_BID_OFFSET_BP/10000.0)  # فوق أفضل Bid بقليل

            # ألغِ الأمر السابق (إن وجد)
            if last_order:
                try: _cancel_order(last_order)
                except Exception: pass

            # ضع أمر Maker جديد
            res = _place_limit_postonly(market, "buy", tick_price, amountQuote=eur_amount)
            orderId = res.get("orderId")
            last_order = orderId

            # انتظر تعبئة قصيرة
            t0 = time.time()
            while time.time() - t0 < MAKER_REPRICE_EVERY:
                st = _fetch_order(orderId)
                if st.get("status") == "filled":
                    fills = st.get("fills", [])
                    last_order = None
                    break
                time.sleep(0.4)

            if fills: break  # تم الشراء Maker
        # نظّف أمرًا معلقًا إن بقي
        if last_order:
            try: _cancel_order(last_order)
            except Exception: pass
    except Exception as e:
        print("open_maker_buy err:", e)

    if not fills:
        send_message("⚠️ لم يكتمل شراء Maker ضمن المهلة.")
        return None

    base_amt, quote_eur, fee_eur = totals_from_fills(fills)
    if base_amt <= 0: return None
    avg = (quote_eur + fee_eur) / base_amt
    return {"amount": base_amt, "avg": avg, "cost_eur": quote_eur + fee_eur, "fee_eur": fee_eur}

def close_market_sell(market: str, amount: float):
    res = _place_market(market, "sell", amount=amount)
    fills = res.get("fills", [])
    base, quote_eur, fee_eur = totals_from_fills(fills)
    proceeds = quote_eur - fee_eur
    return proceeds, fee_eur

# ========= Monitor =========
def monitor_loop():
    global active_trade
    while True:
        try:
            if not active_trade:
                time.sleep(0.25)
                continue

            m   = active_trade["symbol"]
            ent = active_trade["entry"]
            cur = fetch_price_ws_first(m)
            if not cur:
                time.sleep(0.25)
                continue

            pnl = ((cur/ent) - 1.0) * 100.0
            # حدّث القمة
            active_trade["peak_pct"] = max(active_trade["peak_pct"], pnl)

            # تفعيل التريلينغ
            if (not active_trade["trailing_on"]) and pnl >= TRAIL_ACTIVATE_PCT:
                active_trade["trailing_on"] = True
                send_message(f"⛳ تفعيل التريلينغ عند {TRAIL_ACTIVATE_PCT:.1f}%")

            # SL ثابت
            if pnl <= SL_PCT:
                do_close("SL -3%")
                time.sleep(0.5)
                continue

            # تريلينغ: إذا فعال وتراجع من القمة 1%
            if active_trade["trailing_on"]:
                peak = active_trade["peak_pct"]
                if (peak - pnl) >= TRAIL_GIVEBACK_PCT:
                    do_close("Trailing giveback 1%")
                    time.sleep(0.5)
                    continue

            time.sleep(0.12)
        except Exception as e:
            print("monitor err:", e)
            time.sleep(0.5)

Thread(target=monitor_loop, daemon=True).start()

def do_open(market: str, eur: float):
    global active_trade
    if active_trade:
        send_message("⛔ توجد صفقة نشطة. أغلقها أولًا."); return
    if eur is None or eur <= 0:
        eur = get_eur_available()
    if eur < BUY_MIN_EUR:
        send_message(f"⛔ رصيد غير كافٍ. EUR المتاح {eur:.2f}€."); return

    # 1) محاولة Maker لمدة MAKER_WAIT_TOTAL_SEC (مع إعادة تسعير)
    res = open_maker_buy(market, eur)

    # 2) إن لم ينجح، افعل Market مؤكد (محسّن)
    if not res:
        send_message("⚠️ لم يكتمل شراء Maker خلال المهلة — التحويل إلى Market.")
        # ملاحظة: بعد إلغاء أوامر Maker قد يبقى حجز لحظي على الرصيد؛ انتظر نصف ثانية
        time.sleep(0.6)
        res = market_buy_with_confirm(market, eur)
        if not res:
            # فشل نهائي — نخرج بهدوء (لا صفقة)
            return

    active_trade = {
        "symbol": market,
        "entry":  float(res["avg"]),
        "amount": float(res["amount"]),
        "cost_eur": float(res["cost_eur"]),
        "buy_fee_eur": float(res["fee_eur"]),
        "opened_at": time.time(),
        "peak_pct": 0.0,
        "trailing_on": False
    }
    executed_trades.append(active_trade.copy())
    mode = "Maker" if res.get("fee_eur",0)==0 else "Taker"
    send_message(f"✅ شراء {market.replace('-EUR','')} ({mode}) @ €{active_trade['entry']:.6f} | كمية {active_trade['amount']:.8f}")

def do_close(reason=""):
    global active_trade
    if not active_trade: return
    m   = active_trade["symbol"]
    amt = float(active_trade["amount"])
    proceeds, sell_fee = close_market_sell(m, amt)
    pnl_eur = proceeds - float(active_trade["cost_eur"])
    pnl_pct = (proceeds/float(active_trade["cost_eur"]) - 1.0) * 100.0 if active_trade["cost_eur"]>0 else 0.0

    # سجّل الإغلاق
    for t in reversed(executed_trades):
        if t["symbol"]==m and "exit_eur" not in t:
            t.update({"exit_eur": proceeds, "sell_fee_eur": sell_fee,
                      "pnl_eur": pnl_eur, "pnl_pct": pnl_pct,
                      "exit_time": time.time()})
            break

    send_message(f"💰 بيع {m.replace('-EUR','')} (Market) | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%) {('— '+reason) if reason else ''}")
    # انهِ الصفقة
    active_trade = None

# ========= Signal Parsing (Telegram forwarded VIP) =========
COIN_PATTS = [
    re.compile(r"#([A-Z0-9]{2,15})/USDT", re.I),
    re.compile(r"\b([A-Z0-9]{2,15})USDT\b", re.I),
    re.compile(r"\b([A-Z0-9]{2,15})\s*-\s*USDT\b", re.I),
]

def extract_coin_from_text(txt: str):
    for rx in COIN_PATTS:
        m = rx.search(txt or "")
        if m:
            return m.group(1).upper()
    return None

# ========= Summary =========
def build_summary():
    lines=[]
    if active_trade:
        cur = fetch_price_ws_first(active_trade["symbol"]) or active_trade["entry"]
        pnl = ((cur/active_trade["entry"])-1.0)*100.0
        lines.append("📌 صفقة نشطة:")
        lines.append(f"• {active_trade['symbol'].replace('-EUR','')} @ €{active_trade['entry']:.6f} | PnL {pnl:+.2f}% | Peak {active_trade['peak_pct']:.2f}% | Trailing {'ON' if active_trade['trailing_on'] else 'OFF'}")
    else:
        lines.append("📌 لا صفقات نشطة.")
    closed=[x for x in executed_trades if "exit_eur" in x]
    pnl_eur=sum(float(x["pnl_eur"]) for x in closed)
    wins=sum(1 for x in closed if float(x.get("pnl_eur",0))>=0)
    lines.append(f"\n📊 صفقات مكتملة: {len(closed)} | محققة: {pnl_eur:+.2f}€ | فوز/خسارة: {wins}/{len(closed)-wins}")
    lines.append(f"\n⚙️ signals={'ON' if signals_on else 'OFF'} | buy=Maker | sell=Market | SL={SL_PCT}% | trail +{TRAIL_ACTIVATE_PCT}/-{TRAIL_GIVEBACK_PCT}%")
    return "\n".join(lines)

# ========= Telegram Webhook =========
def handle_text_command(text_raw: str):
    global enabled, signals_on
    t = (text_raw or "").strip()
    low = t.lower()

    def starts(*k): return any(low.startswith(x) for x in k)
    def has(*k):    return any(x in low for x in k)

    # أوامر أساسية
    if starts("/start") or has("تشغيل","ابدأ"):
        enabled=True; send_message("✅ تم التفعيل."); return

    if has("stop","ايقاف","إيقاف","قف"):
        enabled=False; send_message("🛑 تم الإيقاف."); return

    if has("signals on","signal on","/signals_on","اشارات تشغيل","إشارات تشغيل"):
        signals_on=True; send_message("📡 الإشارات: ON"); return

    if has("signals off","signal off","/signals_off","اشارات ايقاف","إشارات إيقاف"):
        signals_on=False; send_message("📡 الإشارات: OFF"); return

    if has("/summary","summary","ملخص","الملخص"):
        send_message(build_summary()); return

    if has("/reset","reset","انسى","أنسى"):
        global active_trade, executed_trades
        active_trade=None; executed_trades.clear()
        send_message("🧠 Reset."); return

    if has("/sell","بيع الان","بيع الآن"):
        if active_trade: do_close("Manual"); 
        else: send_message("لا توجد صفقة لإغلاقها.")
        return

    # شراء يدوي: /buy ADA [eur]
    if starts("/buy") or starts("buy") or starts("اشتري") or starts("اشتر"):
        parts = re.split(r"\s+", t)
        coin = None; eur=None
        for p in parts[1:]:
            if re.fullmatch(r"[A-Za-z0-9]{2,15}", p): coin=p.upper()
            elif re.fullmatch(r"\d+(\.\d+)?", p): eur=float(p)
        if not coin:
            send_message("اكتب: /buy ADA [eur]")
            return
        market = coin_to_market(coin)
        if not market:
            send_message(f"⛔ {coin}-EUR غير متاح على Bitvavo.")
            return
        do_open(market, eur)
        return

    # التقط إشارات VIP المعاد توجيهها
    if signals_on:
        coin = extract_coin_from_text(t)
        if coin:
            market = coin_to_market(coin)
            if market:
                send_message(f"📥 إشارة VIP ملتقطة: #{coin}/USDT → {market} — بدء شراء Maker…")
                do_open(market, None)
            else:
                send_message(f"⚠️ {coin}-EUR غير متوفر على Bitvavo.")
        # إن لم نجد عملة، نتجاهل بصمت

@app.route("/", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    # يدعم: {message:{text:..}} أو {text:..}
    text = (data.get("message",{}).get("text") or data.get("text") or "").strip()
    if not text:
        return "ok"
    try:
        handle_text_command(text)
    except Exception as e:
        traceback.print_exc()
        send_message(f"🐞 خطأ: {e}")
    return "ok"

# ========= Local run =========
if __name__ == "__main__" and RUN_LOCAL:
    load_markets()
    app.run(host="0.0.0.0", port=5000)