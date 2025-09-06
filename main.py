# -*- coding: utf-8 -*-
"""
Simple Signal Executor — Maker Buy (Bitvavo EUR) + Fast Market Sell
- إصلاح -100% الوهمية: انتظار تعبئة أو رفض قبل حساب PnL.
- تحسين منطق Maker: تسعير كرأس قائمة الـ bids مع إعادة تسعير معقولة.
"""

import os, re, time, json, traceback, math
import requests, redis, websocket
from threading import Thread, Lock
from uuid import uuid4
from flask import Flask, request
from dotenv import load_dotenv

# ========= Boot / ENV =========
load_dotenv()
app = Flask(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHAT_ID     = os.getenv("CHAT_ID")
API_KEY     = os.getenv("BITVAVO_API_KEY")
API_SECRET  = os.getenv("BITVAVO_API_SECRET")
REDIS_URL   = os.getenv("REDIS_URL")
RUN_LOCAL   = os.getenv("RUN_LOCAL", "0") == "1"

r  = redis.from_url(REDIS_URL) if REDIS_URL else redis.Redis()
BASE_URL = "https://api.bitvavo.com/v2"
WS_URL   = "wss://ws.bitvavo.com/v2/"

# ========= Settings =========
MAX_TRADES            = 1
MAKER_BID_OFFSET_BP   = 3.0      # قريب جدًا من أفضل Bid (كان 10bp)
MAKER_REPRICE_EVERY   = 2.0      # لا نكسر الطابور كل < ثانية
MAKER_WAIT_TOTAL_SEC  = 30
MAKER_REPRICE_THRESH  = 0.0005   # نعيد التسعير فقط إذا تحرّك الـ bid بأكثر من 0.05%
SELL_CONFIRM_TIMEOUT  = 10.0     # ننتظر تعبئة أمر البيع Market (ثوانٍ)
POLL_INTERVAL         = 0.35

SELL_MARKET_ALWAYS    = True

SL_PCT                = -3.0
TRAIL_ACTIVATE_PCT    = +3.0
TRAIL_GIVEBACK_PCT    = 1.0

BUY_MIN_EUR           = 5.0
WS_STALENESS_SEC      = 2.0

# ========= Runtime =========
signals_on     = True
active_trade   = None             # dict أو None
executed_trades= []
MARKET_MAP     = {}
_ws_prices     = {}
_ws_lock       = Lock()
_state_lock    = Lock()

# ========= Utils =========
def send_message(text: str):
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

def _ws_thread():
    while True:
        try:
            ws = websocket.WebSocketApp(
                WS_URL, on_message=_ws_on_message
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
          "clientOrderId":str(uuid4()),"price":f"{price:.10f}"}
    if side=="buy": body["amountQuote"]=f"{amountQuote:.2f}"
    else:           body["amount"]=f"{amount:.10f}"
    return bv_request("POST","/order", body)

def _place_market(market, side, amount=None, amountQuote=None):
    body={"market":market,"side":side,"orderType":"market",
          "clientOrderId":str(uuid4())}
    if side=="buy": body["amountQuote"]=f"{amountQuote:.2f}"
    else:           body["amount"]=f"{amount:.10f}"
    return bv_request("POST","/order", body)

def _fetch_order(orderId):   return bv_request("GET",    f"/order?orderId={orderId}")
def _cancel_order(orderId):  return bv_request("DELETE", f"/order?orderId={orderId}")

# ========= Maker Buy (robust) =========
def open_maker_buy(market: str, eur_amount: float):
    if eur_amount < BUY_MIN_EUR:
        send_message(f"⛔ المبلغ أقل من {BUY_MIN_EUR}€.")
        return None

    ob = fetch_orderbook(market)
    if not ob:
        send_message("⛔ لا يمكن قراءة دفتر الأوامر.")
        return None

    started = time.time()
    last_order = None
    accum_fills = []

    try:
        while time.time() - started < MAKER_WAIT_TOTAL_SEC:
            ob = fetch_orderbook(market)
            best_bid = float(ob["bids"][0][0])
            best_ask = float(ob["asks"][0][0])

            # ضع سعرك كـ Top Bid بدون ملامسة الـ ask (postOnly)
            target = min(best_ask * (1 - 1e-6), best_bid * (1 + MAKER_BID_OFFSET_BP/10000.0))

            # أنشئ/حدّث الأمر فقط إذا تحرك الـ bid بما يفوق العتبة
            if last_order:
                st = _fetch_order(last_order)
                if st.get("status") == "filled":
                    accum_fills += st.get("fills", [])
                    break
                # إذا صار السعر بعيدًا كثيرًا عن Top Bid → أعد التسعير
                my_price = float(st.get("price", target))
                if abs((target / max(my_price, 1e-12)) - 1) >= MAKER_REPRICE_THRESH:
                    try: _cancel_order(last_order)
                    except Exception: pass
                    last_order = None

            if not last_order:
                res = _place_limit_postonly(market, "buy", target, amountQuote=eur_amount)
                if res.get("error"):
                    # postOnly قد يرفض لو لمس الـ ask
                    # ارجع خطوة بسيطة تحت الهدف
                    safe = best_bid
                    res = _place_limit_postonly(market, "buy", safe, amountQuote=eur_amount)
                orderId = res.get("orderId")
                last_order = orderId

            # فترة انتظار قبل التفكير بإعادة التسعير
            t0 = time.time()
            while time.time() - t0 < MAKER_REPRICE_EVERY:
                st = _fetch_order(last_order)
                if st.get("status") == "filled":
                    accum_fills += st.get("fills", [])
                    last_order = None
                    break
                elif st.get("status") == "partiallyFilled":
                    # اجمع أي تعبئة جزئية
                    accum_fills += st.get("fills", [])
                time.sleep(POLL_INTERVAL)

            if accum_fills:
                break

        # نظف أمر معلق
        if last_order:
            try: _cancel_order(last_order)
            except Exception: pass

    except Exception as e:
        print("open_maker_buy err:", e)

    if not accum_fills:
        send_message("⚠️ لم يكتمل شراء Maker ضمن المهلة.")
        return None

    base_amt, quote_eur, fee_eur = totals_from_fills(accum_fills)
    if base_amt <= 0: return None
    avg = (quote_eur + fee_eur) / base_amt
    return {"amount": base_amt, "avg": avg, "cost_eur": quote_eur + fee_eur, "fee_eur": fee_eur}

# ========= Sell (wait for fills) =========
def close_market_sell(market: str, amount: float):
    res = _place_market(market, "sell", amount=amount)
    fills = res.get("fills") or []
    orderId = res.get("orderId")
    status  = res.get("status")

    # انتظر التعبئة إذا لم تصل فوريًا
    deadline = time.time() + SELL_CONFIRM_TIMEOUT
    while not fills and orderId and time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        st = _fetch_order(orderId)
        status = st.get("status") or status
        if status in ("filled", "partiallyFilled"):
            fills = st.get("fills", [])
            break
        if status in ("canceled", "rejected"):
            break

    if not fills:
        return None, 0.0, orderId, status or "pending"   # ← لا نحسب PnL الآن

    base, quote_eur, fee_eur = totals_from_fills(fills)
    proceeds = quote_eur - fee_eur
    return proceeds, fee_eur, orderId, "filled"

# ========= Monitor =========
def monitor_loop():
    global active_trade
    while True:
        try:
            with _state_lock:
                t = active_trade
            if not t:
                time.sleep(0.25); continue

            if t.get("closing"):  # لا نراقب التريلينغ أثناء انتظار إغلاق
                time.sleep(0.25); continue

            m   = t["symbol"]
            ent = t["entry"]
            cur = fetch_price_ws_first(m)
            if not cur:
                time.sleep(0.25); continue

            pnl = ((cur/ent) - 1.0) * 100.0
            t["peak_pct"] = max(t["peak_pct"], pnl)

            if (not t["trailing_on"]) and pnl >= TRAIL_ACTIVATE_PCT:
                t["trailing_on"] = True
                send_message(f"⛳ تفعيل التريلينغ عند {TRAIL_ACTIVATE_PCT:.1f}%")

            if pnl <= SL_PCT:
                do_close("SL -3%"); time.sleep(0.5); continue

            if t["trailing_on"]:
                peak = t["peak_pct"]
                if (peak - pnl) >= TRAIL_GIVEBACK_PCT:
                    do_close("Trailing giveback 1%"); time.sleep(0.5); continue

            time.sleep(0.12)
        except Exception as e:
            print("monitor err:", e)
            time.sleep(0.5)

Thread(target=monitor_loop, daemon=True).start()

# ========= Open/Close =========
def do_open(market: str, eur: float):
    global active_trade
    with _state_lock:
        if active_trade:
            send_message("⛔ توجد صفقة نشطة. أغلقها أولًا."); return
    if eur is None or eur <= 0:
        eur = get_eur_available()
    if eur < BUY_MIN_EUR:
        send_message(f"⛔ رصيد غير كافٍ. EUR المتاح {eur:.2f}€."); return

    res = open_maker_buy(market, eur)
    if not res:
        send_message("⚠️ لم يكتمل شراء Maker خلال المهلة — التحويل إلى Market.")
        taker = _place_market(market, "buy", amountQuote=eur)
        fills = taker.get("fills", [])
        if not fills and taker.get("orderId"):
            # انتظر لحظات لو رجعت بدون fills
            deadline = time.time()+5
            while not fills and time.time()<deadline:
                st=_fetch_order(taker["orderId"])
                if st.get("status") in ("filled","partiallyFilled"):
                    fills = st.get("fills", [])
                    break
                time.sleep(POLL_INTERVAL)
        base_amt, quote_eur, fee_eur = totals_from_fills(fills)
        if base_amt <= 0:
            send_message("❌ فشل الشراء Market أيضًا."); return
        avg = (quote_eur + fee_eur) / base_amt
        res = {"amount": base_amt, "avg": avg, "cost_eur": quote_eur + fee_eur, "fee_eur": fee_eur}

    trade = {
        "symbol": market,
        "entry":  float(res["avg"]),
        "amount": float(res["amount"]),
        "cost_eur": float(res["cost_eur"]),
        "buy_fee_eur": float(res["fee_eur"]),
        "opened_at": time.time(),
        "peak_pct": 0.0,
        "trailing_on": False,
        "closing": False
    }
    with _state_lock:
        active_trade = trade
        executed_trades.append(trade.copy())
    mode = "Maker" if res.get("fee_eur",0)==0 else "Taker"
    send_message(f"✅ شراء {market.replace('-EUR','')} ({mode}) @ €{trade['entry']:.6f} | كمية {trade['amount']:.8f}")

def do_close(reason=""):
    global active_trade
    with _state_lock:
        t = active_trade
        if not t: return
        if t.get("closing"): 
            send_message("⏳ أمر بيع قيد التنفيذ."); 
            return
        t["closing"] = True

    m   = t["symbol"]
    amt = float(t["amount"])
    proceeds, sell_fee, oid, status = close_market_sell(m, amt)

    if proceeds is None:
        # لم تُسجّل أي تعبئة بعد — لا تحسب PnL ولا تُنهِ الصفقة
        send_message(f"🕒 أرسلت أمر بيع (Market) — بانتظار التعبئة… (status={status}) {('— '+reason) if reason else ''}")
        with _state_lock:
            if active_trade: active_trade["closing"] = False  # نسمح بإعادة المحاولة يدويًا
        return

    pnl_eur = proceeds - float(t["cost_eur"])
    pnl_pct = (proceeds/float(t["cost_eur"]) - 1.0) * 100.0 if t["cost_eur"]>0 else 0.0

    # سجّل الإغلاق
    for row in reversed(executed_trades):
        if row["symbol"]==m and "exit_eur" not in row:
            row.update({"exit_eur": proceeds, "sell_fee_eur": sell_fee,
                        "pnl_eur": pnl_eur, "pnl_pct": pnl_pct,
                        "exit_time": time.time()})
            break

    send_message(f"💰 بيع {m.replace('-EUR','')} (Market) | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%) {('— '+reason) if reason else ''}")
    with _state_lock:
        active_trade = None

# ========= Parsing =========
COIN_PATTS = [
    re.compile(r"#([A-Z0-9]{2,15})/USDT", re.I),
    re.compile(r"\b([A-Z0-9]{2,15})USDT\b", re.I),
    re.compile(r"\b([A-Z0-9]{2,15})\s*-\s*USDT\b", re.I),
]

def extract_coin_from_text(txt: str):
    for rx in COIN_PATTS:
        m = rx.search(txt or "")
        if m: return m.group(1).upper()
    return None

# ========= Summary =========
def build_summary():
    lines=[]
    with _state_lock:
        t = active_trade
    if t:
        cur = fetch_price_ws_first(t["symbol"]) or t["entry"]
        pnl = ((cur/t["entry"])-1.0)*100.0
        lines.append("📌 صفقة نشطة:")
        lines.append(f"• {t['symbol'].replace('-EUR','')} @ €{t['entry']:.6f} | PnL {pnl:+.2f}% | Peak {t['peak_pct']:.2f}% | Trailing {'ON' if t['trailing_on'] else 'OFF'}")
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
    global signals_on
    t = (text_raw or "").strip()
    low = t.lower()

    def starts(*k): return any(low.startswith(x) for x in k)
    def has(*k):    return any(x in low for x in k)

    if starts("/start") or has("تشغيل","ابدأ"):
        send_message("✅ تم التفعيل."); return

    if has("stop","ايقاف","إيقاف","قف"):
        send_message("🛑 تم الإيقاف."); return

    if has("signals on","signal on","/signals_on","اشارات تشغيل","إشارات تشغيل"):
        signals_on=True; send_message("📡 الإشارات: ON"); return

    if has("signals off","signal off","/signals_off","اشارات ايقاف","إشارات إيقاف"):
        signals_on=False; send_message("📡 الإشارات: OFF"); return

    if has("/summary","summary","ملخص","الملخص"):
        send_message(build_summary()); return

    if has("/reset","reset","انسى","أنسى"):
        global active_trade, executed_trades
        with _state_lock:
            active_trade=None; executed_trades.clear()
        send_message("🧠 Reset."); return

    if has("/sell","بيع الان","بيع الآن"):
        do_close("Manual"); return

    # شراء يدوي: /buy ADA [eur]
    if starts("/buy") or starts("buy") or starts("اشتري") or starts("اشتر"):
        parts = re.split(r"\s+", t)
        coin = None; eur=None
        for p in parts[1:]:
            if re.fullmatch(r"[A-Za-z0-9]{2,15}", p): coin=p.upper()
            elif re.fullmatch(r"\d+(\.\d+)?", p): eur=float(p)
        if not coin:
            send_message("اكتب: /buy ADA [eur]"); return
        market = coin_to_market(coin)
        if not market:
            send_message(f"⛔ {coin}-EUR غير متاح على Bitvavo."); return
        do_open(market, eur); return

    # إشارات VIP المعاد توجيهها
    if signals_on:
        coin = extract_coin_from_text(t)
        if coin:
            market = coin_to_market(coin)
            if market:
                send_message(f"📥 إشارة VIP: #{coin}/USDT → {market} — بدء شراء Maker…")
                do_open(market, None)
            else:
                send_message(f"⚠️ {coin}-EUR غير متوفر على Bitvavo.")

@app.route("/", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
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