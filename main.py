# -*- coding: utf-8 -*-
"""
Saqer — Maker-Only (Bitvavo/EUR) + Telegram Webhook  ✅ النسخة المقفولة
- يرد على نفس محادثة تيليغرام (chat_id) عبر /telehook (ومسار /webhook alias).
- شراء/بيع Maker (postOnly) فقط مع تجميع partial fills.
- صفقة واحدة فقط، والشراء دائمًا بكامل رصيد EUR (لا أرقام ثابتة، لا 5).
- لا نستخدم amountQuote لل-limit (Bitvavo لا يقبلها).
- دقة السعر/الكمية حسب سوق Bitvavo (pricePrecision/amountPrecision).
- Backoff خفيف على 216 + إلغاء الأمر عند إعادة التسعير + نوم بين المحاولات.
- أوامر: /buy COIN  |  /close  |  /summary  |  /enable  |  /disable
"""

import os, re, time, json, math, hmac, hashlib, traceback
import requests, redis, websocket
from threading import Thread, Lock
from uuid import uuid4
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ========= Boot / ENV =========
load_dotenv()
app = Flask(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID_FALLBACK = os.getenv("CHAT_ID", "").strip()   # يُستخدم فقط إن ما قدرنا نقرأ chat_id من الرسالة
API_KEY     = os.getenv("BITVAVO_API_KEY", "").strip()
API_SECRET  = os.getenv("BITVAVO_API_SECRET", "").strip()
REDIS_URL   = os.getenv("REDIS_URL")
PORT        = int(os.getenv("PORT", "5000"))

# إعدادات تباطؤ ومحاولات
MAKER_REPRICE_EVERY    = 2.0     # نتابع الأمر قبل اتخاذ قرار إعادة التسعير
MAKER_REPRICE_THRESH   = 0.0005  # 0.05% تغيّر معتبر بالـ bid/ask
POLL_INTERVAL          = 0.35    # فحص حالة الأمر
SLEEP_BETWEEN_ATTEMPTS = 1.0     # نوم بين محاولات وضع أمر جديد
IB_BACKOFF_FACTOR      = 0.985   # تقليل بسيط للكمية في حال 216
IB_BACKOFF_TRIES       = 8       # أقصى محاولات داخلية لكل أمر
MAKER_WAIT_BASE_SEC    = 90      # مهلة إجمالية افتراضية للتنفيذ
MAKER_WAIT_MAX_SEC     = 300
MAKER_WAIT_STEP_UP     = 15
MAKER_WAIT_STEP_DOWN   = 10

# *لا* نستخدم أي حد أدنى ثابت باليورو. سنقرأ minQuote/minBase من الـ API فقط.
BUY_MIN_EUR = 0.0

# Bitvavo endpoints
BASE_URL = "https://api.bitvavo.com/v2"
WS_URL   = "wss://ws.bitvavo.com/v2/"

# ========= Runtime =========
r  = redis.from_url(REDIS_URL) if REDIS_URL else redis.Redis()
lk = Lock()
enabled        = True
active_trade   = None
executed_trades= []
MARKET_MAP     = {}   # "ADA" -> "ADA-EUR"
MARKET_META    = {}   # "ADA-EUR" -> {"minQuote","minBase","tick","step"}
_ws_prices     = {}
_ws_lock       = Lock()

# ========= Telegram =========
def tg_send(text: str, chat_id: int | str | None = None):
    """يرد دائمًا على نفس المحادثة. CHAT_ID_FALLBACK فقط كـ fallback."""
    if not text:
        return
    dest = chat_id or CHAT_ID_FALLBACK
    try:
        if not BOT_TOKEN or not dest:
            print("TG:", text); return
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": dest, "text": text},
            timeout=8
        )
    except Exception as e:
        print("TG err:", e)

# ========= Bitvavo (توقيع صحيح) =========
def _sign(ts, method, path, body_str=""):
    msg = f"{ts}{method}{path}{body_str}"
    return hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def bv_request(method: str, path: str, body: dict | None = None, timeout=12):
    url = f"{BASE_URL}{path}"
    ts  = str(int(time.time()*1000))
    body_str = "" if method == "GET" else json.dumps(body or {}, separators=(',',':'))
    sig = _sign(ts, method, f"/v2{path}", body_str)
    headers = {
        'Bitvavo-Access-Key': API_KEY,
        'Bitvavo-Access-Timestamp': ts,
        'Bitvavo-Access-Signature': sig,
        'Bitvavo-Access-Window': '60000',
        'Content-Type': 'application/json'
    }
    try:
        resp = requests.request(method, url, headers=headers,
                                json=(body if method!="GET" else None),
                                timeout=timeout)
        j = resp.json() if resp.content else {}
        if isinstance(j, dict) and j.get("error"):
            print("Bitvavo error:", j)
        return j
    except Exception as e:
        print("bv_request err:", e)
        return {"error":"request_failed"}

# ========= Markets / Meta =========
def load_markets():
    global MARKET_MAP, MARKET_META
    try:
        rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
        m, meta = {}, {}
        for r0 in rows:
            base = r0.get("base"); quote= r0.get("quote"); market = r0.get("market")
            if base and quote == "EUR" and market:
                price_prec = float(r0.get("pricePrecision", 1e-6) or 1e-6)
                amt_prec   = float(r0.get("amountPrecision", 1e-8) or 1e-8)
                m[base.upper()] = market
                meta[market] = {
                    # ✅ لا default = 5 إطلاقًا
                    "minQuote": float(r0.get("minOrderInQuoteAsset", 0) or 0.0),
                    "minBase":  float(r0.get("minOrderInBaseAsset",  0) or 0.0),
                    "tick":     price_prec,
                    "step":     amt_prec,
                }
        if m: MARKET_MAP = m
        if meta: MARKET_META = meta
    except Exception as e:
        print("load_markets err:", e)

def coin_to_market(coin: str):
    if not MARKET_MAP:
        load_markets()
    return MARKET_MAP.get((coin or "").upper())

def _decimals_from_step(step: float) -> int:
    try:
        if step >= 1: return 0
        return max(0, int(round(-math.log10(step))))
    except Exception:
        return 8

def round_price(market: str, price: float) -> float:
    tick = (MARKET_META.get(market, {}) or {}).get("tick", 1e-6)
    decs = _decimals_from_step(tick)
    p = round(float(price), decs)
    return max(tick, p)

def round_amount(market: str, amount: float) -> float:
    step = (MARKET_META.get(market, {}) or {}).get("step", 1e-8)
    floored = math.floor(float(amount) / step) * step
    decs = _decimals_from_step(step)
    return round(max(step, floored), decs)

def fmt_price(market, price) -> str:
    tick = (MARKET_META.get(market, {}) or {}).get("tick", 1e-6)
    decs = _decimals_from_step(tick)
    return f"{round_price(market, price):.{decs}f}"

def fmt_amount(market, amount) -> str:
    step = (MARKET_META.get(market, {}) or {}).get("step", 1e-8)
    decs = _decimals_from_step(step)
    return f"{round_amount(market, amount):.{decs}f}"

def _min_quote(market): return (MARKET_META.get(market, {}) or {}).get("minQuote", 0.0)
def _min_base(market):  return (MARKET_META.get(market, {}) or {}).get("minBase",  0.0)

# ========= Helpers: Balance / Orderbook =========
def get_eur_available() -> float:
    try:
        bals = bv_request("GET", "/balance")
        if isinstance(bals, list):
            for b in bals:
                if b.get("symbol") == "EUR":
                    return max(0.0, float(b.get("available", 0) or 0))
    except Exception:
        pass
    return 0.0

def fetch_orderbook(market):
    try:
        j = requests.get(f"{BASE_URL}/{market}/book", timeout=6).json()
        if j and j.get("bids") and j.get("asks"):
            return j
    except Exception:
        pass
    return None

# ========= Patience learning =========
def _patience_key(market): return f"maker:patience:{market}"
def get_patience_sec(market):
    try:
        v = r.get(_patience_key(market))
        if v is not None:
            return min(MAKER_WAIT_MAX_SEC, max(MAKER_WAIT_BASE_SEC, int(v)))
    except Exception: pass
    return MAKER_WAIT_BASE_SEC
def bump_patience_on_fail(market):
    try:
        r.set(_patience_key(market), min(MAKER_WAIT_MAX_SEC, get_patience_sec(market)+MAKER_WAIT_STEP_UP))
    except Exception: pass
def relax_patience_on_success(market):
    try:
        r.set(_patience_key(market), max(MAKER_WAIT_BASE_SEC, get_patience_sec(market)-MAKER_WAIT_STEP_DOWN))
    except Exception: pass

# ========= Maker Buy (صفقة واحدة بكامل الرصيد) =========
def _calc_amount_from_eur(market: str, eur: float, px: float) -> float:
    base_amt = float(eur) / max(1e-12, float(px))
    base_amt = max(base_amt, _min_base(market))
    return round_amount(market, base_amt)

def open_maker_buy_full(market: str):
    """يشتري *صفقة واحدة* بكامل رصيد EUR كأمر Maker limit، مع إعادة تسعير عند تحرك الـbid."""
    if not market or market not in MARKET_META:
        tg_send("⛔ الماركت غير معروف. حدّث الأسواق أولاً."); return None

    eur_avail = get_eur_available()
    if eur_avail <= 0:
        tg_send("⛔ لا يوجد رصيد EUR متاح."); return None

    minq = _min_quote(market)
    if eur_avail < max(minq, BUY_MIN_EUR):
        tg_send(f"⛔ الرصيد {eur_avail:.2f}€ أقل من الحد الأدنى للسوق {max(minq, BUY_MIN_EUR):.2f}€."); return None

    patience = get_patience_sec(market)
    started  = time.time()

    last_order = None
    last_bid   = None
    all_fills  = []

    try:
        while (time.time()-started) < patience:
            ob = fetch_orderbook(market)
            if not ob: time.sleep(0.25); continue

            best_bid = float(ob["bids"][0][0])
            best_ask = float(ob["asks"][0][0])
            price    = round_price(market, min(best_bid, best_ask*(1.0-1e-6)))

            # تابع أمر قائم
            if last_order:
                st = bv_request("GET", f"/order?orderId={last_order}")
                st_status = (st or {}).get("status")
                if st_status in ("filled","partiallyFilled"):
                    fills = st.get("fills", []) or []
                    if fills:
                        all_fills += fills
                if st_status == "filled":
                    try: bv_request("DELETE", f"/order?orderId={last_order}")
                    except: pass
                    last_order = None
                    break

                # تحرّك الـbid → ألغِ وأعد التسعير
                if (last_bid is None) or (abs(best_bid/last_bid - 1.0) >= MAKER_REPRICE_THRESH):
                    try: bv_request("DELETE", f"/order?orderId={last_order}")
                    except: pass
                    last_order = None
                else:
                    # مهلة قصيرة قبل إعادة الفحص
                    t0 = time.time()
                    while time.time()-t0 < MAKER_REPRICE_EVERY:
                        st = bv_request("GET", f"/order?orderId={last_order}")
                        if (st or {}).get("status") == "filled":
                            all_fills += (st.get("fills", []) or [])
                            try: bv_request("DELETE", f"/order?orderId={last_order}")
                            except: pass
                            last_order = None
                            break
                        time.sleep(POLL_INTERVAL)
                    if last_order:
                        continue  # لسه بنفس الأمر، نرجع للأعلى

            # مافي أمر قائم → ضع أمر جديد بكامل الرصيد المتبقي
            amount = _calc_amount_from_eur(market, eur_avail, price)
            if amount <= 0:
                tg_send("⛔ الكمية صفرية بعد التقريب."); return None

            attempt = 0
            while attempt < IB_BACKOFF_TRIES:
                res = bv_request("POST", "/order", body={
                    "market": market,
                    "side": "buy",
                    "orderType": "limit",
                    "postOnly": True,
                    "clientOrderId": str(uuid4()),
                    "operatorId": "",
                    "price": fmt_price(market, price),
                    "amount": fmt_amount(market, amount)
                })
                oid = (res or {}).get("orderId")
                err = str((res or {}).get("error","")).lower()

                if oid:
                    last_order = oid
                    last_bid   = best_bid
                    tg_send(f"🟢 وضع أمر Maker شراء id={oid} | amount={fmt_amount(market, amount)} | px≈€{fmt_price(market, price)}")
                    break

                # 216: نقص رصيد (نقلّل الكمية شعرة)
                if "insufficient balance" in err or "not have sufficient balance" in err:
                    amount = round_amount(market, amount * IB_BACKOFF_FACTOR)
                    attempt += 1
                    time.sleep(SLEEP_BETWEEN_ATTEMPTS)
                    continue

                # أخطاء أخرى: نوم بسيط ثم إعادة دورة
                tg_send(f"⚠️ فشل وضع الأمر: {err or 'unknown'}")
                time.sleep(SLEEP_BETWEEN_ATTEMPTS)
                break

            # متابعة قصيرة بعد وضع الأمر
            if last_order:
                t0 = time.time()
                while time.time()-t0 < MAKER_REPRICE_EVERY:
                    st = bv_request("GET", f"/order?orderId={last_order}")
                    if (st or {}).get("status") in ("filled","partiallyFilled"):
                        all_fills += (st.get("fills", []) or [])
                        if (st or {}).get("status") == "filled":
                            try: bv_request("DELETE", f"/order?orderId={last_order}")
                            except: pass
                            last_order = None
                            break
                    time.sleep(POLL_INTERVAL)

        # تنظيف
        if last_order:
            try: bv_request("DELETE", f"/order?orderId={last_order}")
            except: pass

    except Exception as e:
        traceback.print_exc()
        tg_send(f"🐞 خطأ أثناء الشراء: {e}")

    if not all_fills:
        bump_patience_on_fail(market)
        tg_send("⚠️ لم يتم تنفيذ شراء Maker ضمن المهلة.")
        return None

    # إجماليات
    tb=tq=fee=0.0
    for f in all_fills:
        amt=float(f["amount"]); price=float(f["price"]); fe=float(f.get("fee",0) or 0)
        tb+=amt; tq+=amt*price; fee+=fe
    if tb <= 0:
        bump_patience_on_fail(market); return None

    relax_patience_on_success(market)
    avg = (tq + fee) / tb
    return {"amount": tb, "avg": avg, "cost_eur": tq + fee, "fee_eur": fee}

# ========= Maker Sell =========
def maker_sell_all(market: str):
    """بيع Maker لكل رصيد الـ Base المتاح."""
    base = market.split("-")[0]
    try:
        bals = bv_request("GET", "/balance")
        avail = 0.0
        for b in (bals or []):
            if b.get("symbol") == base:
                avail = float(b.get("available",0) or 0); break
        if avail <= 0:
            tg_send(f"⛔ لا يوجد رصيد {base} للبيع."); return None

        ob = fetch_orderbook(market)
        if not ob:
            tg_send("⚠️ تعذّر جلب دفتر الأوامر."); return None

        best_bid = float(ob["bids"][0][0]); best_ask = float(ob["asks"][0][0])
        price = round_price(market, max(best_ask, best_bid*(1.0+1e-6)))
        amt   = round_amount(market, avail)
        if amt <= 0:
            tg_send("⛔ الكمية صفريّة بعد التقريب."); return None

        res = bv_request("POST","/order", body={
            "market": market, "side": "sell", "orderType": "limit", "postOnly": True,
            "clientOrderId": str(uuid4()), "operatorId": "",
            "price": fmt_price(market, price), "amount": fmt_amount(market, amt)
        })
        oid = (res or {}).get("orderId")
        if oid:
            tg_send(f"🟢 وضع أمر بيع Maker id={oid} | amount={fmt_amount(market, amt)} | px≈€{fmt_price(market, price)}")
            return {"orderId": oid, "price": price, "amount": amt}
        else:
            tg_send(f"❌ فشل وضع أمر البيع: {(res or {}).get('error')}")
            return None
    except Exception as e:
        tg_send(f"🐞 خطأ البيع: {e}")
        return None

# ========= Monitor (وقف ديناميكي بسيط) =========
STOP_LADDER = [(0.0,-2.0),(1.0,-1.0),(2.0,0.0),(3.0,1.0),(4.0,2.0),(5.0,3.0)]
def _stop_from_peak(peak_pct: float) -> float:
    s=-999.0
    for th,v in STOP_LADDER:
        if peak_pct>=th: s=v
    return s

def monitor_loop():
    global active_trade
    while True:
        try:
            with lk:
                at = active_trade.copy() if active_trade else None
            if not at:
                time.sleep(0.3); continue

            m=at["symbol"]; ent=at["entry"]
            ob = fetch_orderbook(m)
            cur = float(ob["bids"][0][0]) if ob else ent
            pnl = ((cur/ent)-1.0)*100.0
            updated_peak = max(at["peak_pct"], pnl)
            new_stop = _stop_from_peak(updated_peak)

            changed=False
            with lk:
                if active_trade:
                    if updated_peak>active_trade["peak_pct"]+1e-9:
                        active_trade["peak_pct"]=updated_peak; changed=True
                    if abs(new_stop-active_trade.get("dyn_stop_pct",-999.0))>1e-9:
                        active_trade["dyn_stop_pct"]=new_stop; changed=True
            if changed:
                tg_send(f"📈 Peak={updated_peak:.2f}% → SL {new_stop:+.2f}%")

            with lk:
                at2 = active_trade.copy() if active_trade else None
            if at2 and pnl <= at2.get("dyn_stop_pct",-2.0):
                do_close("Dynamic stop")
                time.sleep(0.5); continue

            time.sleep(0.2)
        except Exception as e:
            print("monitor err:", e); time.sleep(0.6)

Thread(target=monitor_loop, daemon=True).start()

# ========= Trade Flow =========
def do_open(market: str):
    def _runner():
        global active_trade
        try:
            with lk:
                if active_trade:
                    tg_send("⛔ توجد صفقة نشطة."); return
            res = open_maker_buy_full(market)
            if not res:
                tg_send("⏳ لم يكتمل الشراء (Maker)."); return
            with lk:
                active_trade = {
                    "symbol": market,
                    "entry":  float(res["avg"]),
                    "amount": float(res["amount"]),
                    "cost_eur": float(res["cost_eur"]),
                    "buy_fee_eur": float(res["fee_eur"]),
                    "opened_at": time.time(),
                    "peak_pct": 0.0,
                    "dyn_stop_pct": -2.0
                }
                executed_trades.append(active_trade.copy())
            tg_send(f"✅ شراء {market.replace('-EUR','')} (Maker) @ €{active_trade['entry']:.8f} | كمية {active_trade['amount']:.8f}")
        except Exception as e:
            traceback.print_exc(); tg_send(f"🐞 خطأ أثناء الفتح: {e}")
    Thread(target=_runner, daemon=True).start()

def do_close(reason=""):
    global active_trade
    try:
        with lk:
            if not active_trade: return
            m   = active_trade["symbol"]
            amt = float(active_trade["amount"])
            cost= float(active_trade["cost_eur"])
        sold, proceeds, sell_fee = maker_sell_all(m), 0.0, 0.0
        # sell_all يرجع dict للأمر فقط؛ ننتظر التنفيذ خارجيًا أو نكتفي بالإشعار
        with lk:
            pnl_eur = 0.0; pnl_pct=0.0  # لا نحسب حتى يكتمل البيع فعلاً
            active_trade=None
        tg_send(f"💰 بيع {m.replace('-EUR','')} (Maker) {('— '+reason) if reason else ''}")
    except Exception as e:
        traceback.print_exc(); tg_send(f"🐞 خطأ أثناء الإغلاق: {e}")

# ========= Summary =========
def build_summary():
    lines=[]
    with lk:
        at=active_trade
        closed=[x for x in executed_trades if "exit_eur" in x]
    if at:
        ob = fetch_orderbook(at["symbol"])
        cur = float(ob["bids"][0][0]) if ob else at["entry"]
        pnl = ((cur/at["entry"])-1.0)*100.0
        lines.append("📌 صفقة نشطة:")
        lines.append(f"• {at['symbol'].replace('-EUR','')} @ €{at['entry']:.8f} | PnL {pnl:+.2f}% | Peak {at['peak_pct']:.2f}% | SL {at.get('dyn_stop_pct',-2.0):+.2f}%")
    else:
        lines.append("📌 لا صفقات نشطة.")
    pnl_eur=sum(float(x.get("pnl_eur",0)) for x in closed)
    wins   =sum(1 for x in closed if float(x.get("pnl_eur",0))>=0)
    lines.append(f"\n📊 صفقات مكتملة: {len(closed)} | محققة: {pnl_eur:+.2f}€ | فوز/خسارة: {wins}/{len(closed)-wins}")
    lines.append(f"\n⚙️ buy=Maker | sell=Maker | وقف ديناميكي متدرج")
    return "\n".join(lines)

# ========= Telegram Webhook (يرد على نفس المحادثة) =========
def _handle_telegram(update):
    msg     = update.get("message") or update.get("edited_message") or {}
    chat_id = (msg.get("chat") or {}).get("id") or CHAT_ID_FALLBACK
    text    = (msg.get("text") or "").strip()
    if not chat_id: return jsonify({"ok": True})

    lower = text.lower()

    # /buy COIN
    m = re.match(r"^/buy\s+([A-Za-z0-9]+)$", text.strip())
    if m:
        coin = m.group(1).upper()
        market = coin_to_market(coin)
        if not market:
            tg_send(f"⛔ {coin}-EUR غير متاح على Bitvavo.", chat_id); return jsonify({"ok": True})
        tg_send(f"⏳ بدء شراء {coin} بكامل الرصيد...", chat_id)
        do_open(market)
        return jsonify({"ok": True})

    if lower.startswith("/close"):
        do_close("Manual"); tg_send("⏳ إغلاق...", chat_id); return jsonify({"ok": True})

    if lower.startswith("/summary"):
        tg_send(build_summary(), chat_id); return jsonify({"ok": True})

    if lower.startswith("/enable"):
        global enabled; enabled=True; tg_send("✅ تم التفعيل.", chat_id); return jsonify({"ok": True})

    if lower.startswith("/disable"):
        enabled=False; tg_send("🛑 تم الإيقاف.", chat_id); return jsonify({"ok": True})

    tg_send("الأوامر: /buy COIN  |  /close  |  /summary  |  /enable  |  /disable", chat_id)
    return jsonify({"ok": True})

@app.route("/telehook", methods=["POST"])
def telehook():
    try:
        update = request.get_json(silent=True) or {}
        return _handle_telegram(update)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "err": str(e)}), 200

# alias اختياري لو عامل Webhook قديم على /webhook
@app.route("/webhook", methods=["POST"])
def webhook_alias():
    try:
        update = request.get_json(silent=True) or {}
        return _handle_telegram(update)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "err": str(e)}), 200

# ========= Health =========
@app.route("/", methods=["GET"])
def home(): return "Saqer Maker Executor ✅", 200

# ========= Main =========
if __name__ == "__main__":
    load_markets()
    app.run(host="0.0.0.0", port=PORT)