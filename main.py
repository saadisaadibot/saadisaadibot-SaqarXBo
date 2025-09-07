# -*- coding: utf-8 -*-
"""
Simple Signal Executor — Maker Buy (Bitvavo EUR) + Fast Market Sell
- شراء Maker (postOnly) على أفضل Bid مع قبول partial fills.
- SL = -3.0% ، Trailing: +3% تفعيل ثم -1% من القمة.
- الإشارات: /buy ADA [eur] أو إعادة توجيه #COIN/USDT.
- صفقة واحدة فقط في نفس الوقت.
"""

import os, re, time, json, traceback
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
lk = Lock()

BASE_URL = "https://api.bitvavo.com/v2"
WS_URL   = "wss://ws.bitvavo.com/v2/"

# ========= Settings =========
MAX_TRADES            = 1
SELL_MARKET_ALWAYS    = True

# Maker logic
MAKER_REPRICE_EVERY   = 2.0        # كم ثانية نعطي للأمر قبل التفكير بإعادة التسعير
MAKER_WAIT_TOTAL_SEC  = 25         # المهلة الكلية لمحاولات الشراء Maker
MAKER_REPRICE_THRESH  = 0.0005     # 0.05% تغيير حقيقي بالـ bid لنلغي ونعيد وضع الأمر

SL_PCT                = -3.0
TRAIL_ACTIVATE_PCT    = +3.0
TRAIL_GIVEBACK_PCT    = 1.0

BUY_MIN_EUR           = 5.0
WS_STALENESS_SEC      = 2.0

# Market fallback
MARKET_CONFIRM_TIMEOUT= 15.0
POLL_INTERVAL         = 0.35

# ========= Runtime =========
enabled        = True
signals_on     = True
active_trade   = None
executed_trades= []
MARKET_MAP     = {}
_ws_prices     = {}
_ws_lock       = Lock()

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
            base = r0.get("base"); quote= r0.get("quote"); market= r0.get("market")
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
def _ws_on_open(ws): pass

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
        ws.send(json.dumps(payload)); ws.close()
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

# ========= Trade Ops =========
def open_maker_buy(market: str, eur_amount: float):
    """Maker postOnly على أفضل Bid، مع قبول partial fills وتجميعها."""
    if eur_amount < BUY_MIN_EUR:
        send_message(f"⛔ المبلغ أقل من {BUY_MIN_EUR}€.")
        return None

    ob = fetch_orderbook(market)
    if not ob or not ob.get("bids") or not ob.get("asks"):
        send_message("⛔ لا يمكن قراءة دفتر الأوامر.")
        return None

    started      = time.time()
    last_order   = None
    last_bid     = None
    all_fills    = []
    remaining_q  = float(eur_amount)

    try:
        while time.time() - started < MAKER_WAIT_TOTAL_SEC and remaining_q >= BUY_MIN_EUR * 0.999:
            book = fetch_orderbook(market)
            if not book or not book.get("bids") or not book.get("asks"):
                time.sleep(0.25); continue

            best_bid = float(book["bids"][0][0])
            best_ask = float(book["asks"][0][0])
            price    = min(best_bid, best_ask*(1.0-1e-6))  # عملياً = أفضل Bid (آمن للـ postOnly)

            # لو عندنا أمر سابق
            if last_order:
                st = _fetch_order(last_order)
                st_status = st.get("status")

                if st_status in ("filled", "partiallyFilled"):
                    fills = st.get("fills", []) or []
                    if fills:
                        all_fills += fills
                        base, quote_eur, fee_eur = totals_from_fills(fills)
                        remaining_q = max(0.0, remaining_q - (quote_eur + fee_eur))
                    if st_status == "filled" or remaining_q < BUY_MIN_EUR * 0.999:
                        last_order = None
                        break

                # أعد التسعير فقط إذا تحرّك أفضل Bid فعلاً
                if (last_bid is None) or (abs(best_bid/last_bid - 1.0) >= MAKER_REPRICE_THRESH):
                    try: _cancel_order(last_order)
                    except Exception: pass
                    last_order = None
                else:
                    # أعطه وقتًا قبل أي تعديل
                    t0 = time.time()
                    while time.time() - t0 < MAKER_REPRICE_EVERY:
                        st = _fetch_order(last_order)
                        st_status = st.get("status")
                        if st_status in ("filled", "partiallyFilled"):
                            fills = st.get("fills", []) or []
                            if fills:
                                all_fills += fills
                                base, quote_eur, fee_eur = totals_from_fills(fills)
                                remaining_q = max(0.0, remaining_q - (quote_eur + fee_eur))
                            if st_status == "filled" or remaining_q < BUY_MIN_EUR * 0.999:
                                last_order = None
                                break
                        time.sleep(0.35)
                    if last_order:
                        continue  # ما زال قائمًا، نرجع لبداية الحلقة

            # لا يوجد أمر فعّال → ضع أمر جديد على أفضل Bid
            if not last_order and remaining_q >= BUY_MIN_EUR * 0.999:
                res = _place_limit_postonly(market, "buy", price, amountQuote=remaining_q)
                orderId = res.get("orderId")

                # لو رفض لأي سبب، جرب مرة ثانية على best_bid (نفسه) لتجاوز خطأ مؤقت
                if not orderId:
                    res = _place_limit_postonly(market, "buy", best_bid, amountQuote=remaining_q)
                    orderId = res.get("orderId")

                if not orderId:
                    time.sleep(0.25); continue

                last_order = orderId
                last_bid   = best_bid

                t0 = time.time()
                while time.time() - t0 < MAKER_REPRICE_EVERY:
                    st = _fetch_order(last_order)
                    st_status = st.get("status")
                    if st_status in ("filled", "partiallyFilled"):
                        fills = st.get("fills", []) or []
                        if fills:
                            all_fills += fills
                            base, quote_eur, fee_eur = totals_from_fills(fills)
                            remaining_q = max(0.0, remaining_q - (quote_eur + fee_eur))
                        if st_status == "filled" or remaining_q < BUY_MIN_EUR * 0.999:
                            last_order = None
                            break
                    time.sleep(0.35)

            if remaining_q < BUY_MIN_EUR * 0.999:
                break

        # نظّف أي أمر معلق
        if last_order:
            try: _cancel_order(last_order)
            except Exception: pass

    except Exception as e:
        print("open_maker_buy err:", e)

    if not all_fills:
        send_message("⚠️ لم يكتمل شراء Maker ضمن المهلة.")
        return None

    base_amt, quote_eur, fee_eur = totals_from_fills(all_fills)
    if base_amt <= 0:
        return None

    avg = (quote_eur + fee_eur) / base_amt
    return {"amount": base_amt, "avg": avg, "cost_eur": quote_eur + fee_eur, "fee_eur": fee_eur}

def close_market_sell(market: str, amount: float):
    res = _place_market(market, "sell", amount=amount)
    fills = res.get("fills", [])
    base, quote_eur, fee_eur = totals_from_fills(fills)
    proceeds = quote_eur - fee_eur
    return proceeds, fee_eur

# ========= Market fallback (مبسّط لكن متين) =========
def market_buy_fallback(market: str, eur_amount: float):
    res = _place_market(market, "buy", amountQuote=eur_amount)
    fills = res.get("fills") or []
    oid   = res.get("orderId")
    status= res.get("status")

    # إذا ما في تعبئات، راقب الطلب لفترة قصيرة
    if not fills and oid:
        deadline = time.time() + MARKET_CONFIRM_TIMEOUT
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL)
            st = _fetch_order(oid)
            status = st.get("status") or status
            if status in ("filled", "partiallyFilled"):
                fills = st.get("fills", []) or []
                break
            if status in ("canceled", "rejected"):
                break

    base_amt, quote_eur, fee_eur = totals_from_fills(fills)
    if base_amt <= 0:
        send_message("❌ فشل الشراء Market أيضًا.")
        return None
    avg = (quote_eur + fee_eur) / base_amt
    return {"amount": base_amt, "avg": avg, "cost_eur": quote_eur + fee_eur, "fee_eur": fee_eur}

# ========= Monitor =========
def monitor_loop():
    global active_trade
    while True:
        try:
            if not active_trade:
                time.sleep(0.25); continue

            m   = active_trade["symbol"]
            ent = active_trade["entry"]
            cur = fetch_price_ws_first(m)
            if not cur:
                time.sleep(0.25); continue

            pnl = ((cur/ent) - 1.0) * 100.0
            active_trade["peak_pct"] = max(active_trade["peak_pct"], pnl)

            if (not active_trade["trailing_on"]) and pnl >= TRAIL_ACTIVATE_PCT:
                active_trade["trailing_on"] = True
                send_message(f"⛳ تفعيل التريلينغ عند {TRAIL_ACTIVATE_PCT:.1f}%")

            if pnl <= SL_PCT:
                do_close("SL -3%"); time.sleep(0.5); continue

            if active_trade["trailing_on"]:
                peak = active_trade["peak_pct"]
                if (peak - pnl) >= TRAIL_GIVEBACK_PCT:
                    do_close("Trailing giveback 1%"); time.sleep(0.5); continue

            time.sleep(0.12)
        except Exception as e:
            print("monitor err:", e)
            time.sleep(0.5)

Thread(target=monitor_loop, daemon=True).start()

def _open_trade_flow(market: str, eur: float):
    """التنفيذ الحقيقي للفتح (يُشغّل في ثريد)."""
    global active_trade
    try:
        with lk:
            if active_trade:
                send_message("⛔ توجد صفقة نشطة. أغلقها أولًا."); return
        if eur is None or eur <= 0:
            eur = get_eur_available()
        if eur < BUY_MIN_EUR:
            send_message(f"⛔ رصيد غير كافٍ. EUR المتاح {eur:.2f}€."); return

        # 1) Maker
        res = open_maker_buy(market, eur)

        # 2) Market fallback
        if not res:
            send_message("⚠️ لم يكتمل شراء Maker خلال المهلة — التحويل إلى Market.")
            time.sleep(0.8)  # تحرر الرصيد لو كان محجوز
            res = market_buy_fallback(market, eur)
            if not res:
                return

        with lk:
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
    except Exception as e:
        traceback.print_exc()
        send_message(f"🐞 خطأ أثناء الفتح: {e}")

def do_open(market: str, eur: float):
    """مجرد مُطلق لثريد حتى لا نحجز الـ webhook."""
    Thread(target=_open_trade_flow, args=(market, eur), daemon=True).start()

def do_close(reason=""):
    global active_trade
    try:
        with lk:
            if not active_trade:
                return
            m   = active_trade["symbol"]
            amt = float(active_trade["amount"])
        proceeds, sell_fee = close_market_sell(m, amt)
        with lk:
            pnl_eur = proceeds - float(active_trade["cost_eur"])
            pnl_pct = (proceeds/float(active_trade["cost_eur"]) - 1.0) * 100.0 if active_trade["cost_eur"]>0 else 0.0
            # سجّل الإغلاق
            for t in reversed(executed_trades):
                if t["symbol"]==m and "exit_eur" not in t:
                    t.update({"exit_eur": proceeds, "sell_fee_eur": sell_fee,
                              "pnl_eur": pnl_eur, "pnl_pct": pnl_pct,
                              "exit_time": time.time()})
                    break
            active_trade = None
        send_message(f"💰 بيع {m.replace('-EUR','')} (Market) | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%) {('— '+reason) if reason else ''}")
    except Exception as e:
        traceback.print_exc()
        send_message(f"🐞 خطأ أثناء الإغلاق: {e}")

# ========= Signal Parsing =========
COIN_PATTS = [
    re.compile(r"#([A-Z0-9]{2,15})/USDT", re.I),
    re.compile(r"\b([A-Z0-9]{2,15})USDT\b", re.I),
    re.compile(r"\b([A-Z0-9]{2,15})\s*-\s*USDT\b", re.I),
]

def extract_coin_from_text(txt: str):
    for rx in COIN_PATTS:
        m = rx.search(txt or ""); 
        if m: return m.group(1).upper()
    return None

# ========= Summary =========
def build_summary():
    lines=[]
    with lk:
        at = active_trade
        closed=[x for x in executed_trades if "exit_eur" in x]
    if at:
        cur = fetch_price_ws_first(at["symbol"]) or at["entry"]
        pnl = ((cur/at["entry"])-1.0)*100.0
        lines.append("📌 صفقة نشطة:")
        lines.append(f"• {at['symbol'].replace('-EUR','')} @ €{at['entry']:.6f} | PnL {pnl:+.2f}% | Peak {at['peak_pct']:.2f}% | Trailing {'ON' if at['trailing_on'] else 'OFF'}")
    else:
        lines.append("📌 لا صفقات نشطة.")
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
        with lk:
            active_trade=None; executed_trades.clear()
        send_message("🧠 Reset."); return

    if has("/sell","بيع الان","بيع الآن"):
        with lk:
            has_position = active_trade is not None
        if has_position: do_close("Manual")
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
            send_message("اكتب: /buy ADA [eur]"); return
        market = coin_to_market(coin)
        if not market:
            send_message(f"⛔ {coin}-EUR غير متاح على Bitvavo."); return
        do_open(market, eur); return

    # التقط إشارات VIP
    if signals_on:
        coin = extract_coin_from_text(t)
        if coin:
            market = coin_to_market(coin)
            if market:
                send_message(f"📥 إشارة VIP ملتقطة: #{coin}/USDT → {market} — بدء شراء Maker…")
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