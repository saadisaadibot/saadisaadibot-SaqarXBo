# -*- coding: utf-8 -*-
"""
Toto Premium — Telegram Webhook + Bitvavo Maker-Only
- تيليغرام ويبهوك بنمطك القديم (يرد فورًا).
- شراء Maker-Only بكل رصيد EUR (لا أرقام 5، لا amountQuote للـlimit).
- بيع Maker-Only لكل الكمية.
- توقيع Bitvavo صحيح + operatorId="".
- تقليم السعر/الكمية بدقة السوق الفعلية.
"""

import os, time, json, math, hmac, hashlib, requests, traceback
from uuid import uuid4
from flask import Flask, request
from threading import Thread

# ========= إعدادات من البيئة =========
BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
CHAT_ID     = os.getenv("CHAT_ID", "")
API_KEY     = os.getenv("BITVAVO_API_KEY", "")
API_SECRET  = os.getenv("BITVAVO_API_SECRET", "")
PORT        = int(os.getenv("PORT", "8080"))

# هامش بسيط للحماية من خطأ الرصيد (يمكن تصفيره إن أردت)
FEE_EST          = float(os.getenv("FEE_RATE_EST", "0.0025"))  # ≈0.25%
HEADROOM_EUR_MIN = float(os.getenv("HEADROOM_EUR_MIN", "0.00")) # 0 مسموح
SPEND_FRACTION   = float(os.getenv("MAX_SPEND_FRACTION", "1.00")) # 100%

BASE_URL = "https://api.bitvavo.com/v2"
app = Flask(__name__)

# ذاكرة خفيفة (بدل ريدِس) — غيّرها إن أردت
MEM = {
    "orders": {},   # symbol -> "شراء"/"بيع"
    "entry":  {},   # symbol -> entry price
    "peak":   {},   # symbol -> peak price
    "source": {},   # symbol -> source
    "profits": {}   # id -> {"profit": float, "source": str}
}

# ======== تيليغرام ========
def send_message(text: str):
    try:
        if not BOT_TOKEN or not CHAT_ID:
            print("TG:", text)
            return
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text},
            timeout=8
        )
    except Exception as e:
        print("TG err:", e)

# ======== Bitvavo REST (توقيع صحيح) ========
def _sign(ts, method, path, body_str=""):
    msg = f"{ts}{method}{path}{body_str}"
    return hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def bv_request(method: str, path: str, body: dict | None = None, timeout=12):
    """
    path مثل: '/balance' أو '/order'
    نضيف '/v2' داخل التوقيع كما تتطلب Bitvavo.
    """
    url = f"{BASE_URL}{path}"
    ts  = str(int(time.time()*1000))
    body_str = "" if method == "GET" else json.dumps(body or {}, separators=(',',':'))
    sig = _sign(ts, method, f"/v2{path}", body_str)
    headers = {
        'Bitvavo-Access-Key': API_KEY,
        'Bitvavo-Access-Timestamp': ts,
        'Bitvavo-Access-Signature': sig,
        'Bitvavo-Access-Window': '10000',
        'Content-Type': 'application/json'
    }
    try:
        resp = requests.request(
            method, url, headers=headers,
            json=(body if method != "GET" else None), timeout=timeout
        )
        j = resp.json() if resp.content else {}
        if isinstance(j, dict) and j.get("error"):
            print("Bitvavo error:", j)
        return j
    except Exception as e:
        print("bv_request err:", e)
        return {"error":"request_failed"}

# ======== أسواق ودقة ========
MARKET_META = {}  # "DATA-EUR" -> {"tick":0.00001,"step":0.00000001,"minBase":..., "minQuote":...}

def load_markets():
    global MARKET_META
    try:
        rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
        meta = {}
        for r in rows:
            mkt = r.get("market")
            if not mkt or not mkt.endswith("-EUR"): 
                continue
            # Bitvavo توفر الدقة كقيمة step (amountPrecision/pricePrecision)
            price_prec = float(r.get("pricePrecision", 1e-6) or 1e-6)
            amt_prec   = float(r.get("amountPrecision", 1e-8) or 1e-8)
            meta[mkt] = {
                "tick": price_prec,
                "step": amt_prec,
                # القيم الدنيا الحقيقية للسوق (لا نضع 5 أبداً)
                "minQuote": float(r.get("minOrderInQuoteAsset", 0) or 0.0),
                "minBase":  float(r.get("minOrderInBaseAsset",  0) or 0.0),
            }
        if meta:
            MARKET_META = meta
    except Exception as e:
        print("load_markets err:", e)

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
    # floor على step حتى لا نزيد عن المتاح/الدقة
    floored = math.floor(float(amount) / step) * step
    decs = _decimals_from_step(step)
    return round(max(step, floored), decs)

# ======== رصيد + سعر ========
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

def ticker_price(market: str) -> float | None:
    try:
        j = requests.get(f"{BASE_URL}/ticker/price?market={market}", timeout=6).json()
        p = float(j.get("price", 0) or 0)
        return p if p > 0 else None
    except Exception:
        return None

# ======== Maker-Only: وضع أمر ========
def place_maker_limit(market: str, side: str, price: float, amount_base: float):
    """
    limit + postOnly = True
    Bitvavo لا يقبل amountQuote للـlimit، لذا نرسل amount (Base) + price فقط.
    """
    body = {
        "market": market,
        "side": side,
        "orderType": "limit",
        "postOnly": True,
        "clientOrderId": str(uuid4()),
        "operatorId": "",
        "price": f"{price:.20f}".rstrip("0").rstrip("."),
        "amount": f"{amount_base:.20f}".rstrip("0").rstrip("."),
    }
    res = bv_request("POST", "/order", body)
    return res

def cancel_order(order_id: str, market: str | None = None):
    path = f"/order?orderId={order_id}"
    if market:
        path += f"&market={market}"
    try:
        return bv_request("DELETE", path)
    except Exception:
        return {}

def fetch_order(order_id: str, market: str | None = None):
    path = f"/order?orderId={order_id}"
    if market:
        path += f"&market={market}"
    return bv_request("GET", path)

# ======== شراء بكل الرصيد (بدون 5) ========
def maker_buy_all(market: str, prefer_bid=True):
    """
    يحسب الكمية = كل رصيد EUR مع هامش صغير للعمولة، ثم يضع أمر Maker عند أفضل Bid (أو أسوأ من ask بشعرة لمنع التاكل).
    """
    # 1) الرصيد
    eur = get_eur_available()
    if eur <= 0:
        send_message(f"⛔ لا يوجد رصيد EUR متاح (free={eur:.2f}).")
        return None

    # 2) سعر مرجعي
    p = ticker_price(market)
    if not p or p <= 0:
        send_message("⚠️ تعذّر جلب السعر.")
        return None

    # 3) استخدام كل الرصيد مع حماية
    #   use = min(eur*SPEND_FRACTION, eur - buffer) لكن من غير أية أرقام دنيا
    buffer = max(HEADROOM_EUR_MIN, eur * FEE_EST * 1.5)
    use_eur = max(0.0, min(eur * SPEND_FRACTION, eur - buffer))
    if use_eur <= 0:
        send_message(f"⛔ الرصيد بعد الهامش غير كافٍ. متاح {eur:.2f}€ | هامش {buffer:.2f}€.")
        return None

    # 4) التقريب للدقة
    px = round_price(market, p * (1.0 - 1e-6) if prefer_bid else p)
    amount = round_amount(market, use_eur / px)

    if amount <= 0:
        send_message("⛔ الكمية الناتجة صفريّة بعد التقريب.")
        return None

    # 5) جرّب مع backoff لو رجع 216 (رصيد غير كافٍ) — ننقص الاستخدام قليلًا
    tries, backoff = 0, 0.985
    last_err = None
    while tries < 8:
        res = place_maker_limit(market, "buy", px, amount)
        oid = (res or {}).get("orderId")
        err = (res or {}).get("error","")
        if oid:
            send_message(f"🟢 وُضع أمر شراء Maker id={oid} | amount={amount} | px≈€{px:.8f} | EUR≈{amount*px:.2f}")
            return {"orderId": oid, "price": px, "amount": amount}
        # معالجة 216
        if isinstance(err, str) and ("insufficient balance" in err.lower() or "not have sufficient balance" in err.lower()):
            use_eur *= backoff
            amount = round_amount(market, use_eur / px)
            tries += 1
            last_err = err
            time.sleep(0.2)
            continue
        last_err = err or "unknown_error"
        break

    send_message(f"❌ فشل وضع الأمر: {last_err}")
    return None

# ======== بيع Maker-Only لكل الكمية ========
def maker_sell_all(market: str):
    # مبدئيًا نبيع كل رصيد الـ Base (نجلبه من /balance)
    base = market.split("-")[0]
    try:
        bals = bv_request("GET", "/balance")
        avail = 0.0
        for b in (bals or []):
            if b.get("symbol") == base:
                avail = float(b.get("available",0) or 0)
                break
        if avail <= 0:
            send_message(f"⛔ لا يوجد رصيد {base} للبيع.")
            return None
        p = ticker_price(market)
        if not p or p<=0:
            send_message("⚠️ تعذّر جلب السعر.")
            return None
        px = round_price(market, p * (1.0 + 1e-6))
        amt= round_amount(market, avail)
        if amt<=0:
            send_message("⛔ الكمية صفريّة بعد التقريب.")
            return None

        res = place_maker_limit(market, "sell", px, amt)
        oid = (res or {}).get("orderId")
        if oid:
            send_message(f"🟢 وُضع أمر بيع Maker id={oid} | amount={amt} | px≈€{px:.8f}")
            return {"orderId": oid, "price": px, "amount": amt}
        else:
            send_message(f"❌ فشل وضع أمر البيع: {(res or {}).get('error')}")
            return None
    except Exception as e:
        send_message(f"❌ خطأ البيع: {e}")
        return None

# ======== Webhook تيليغرام (بنمطك) ========
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(silent=True) or {}
        if "message" not in data:
            return "", 200

        text = (data["message"].get("text") or "").strip()
        lower = text.lower()

        # --- ملخص
        if ("الملخص" in text) or ("/summary" in lower):
            records = MEM["profits"]
            total = sum(v["profit"] for v in records.values()) if records else 0.0

            # تفصيل المصدر كما كنت تعمل
            sources = {}
            sums = {}
            for v in records.values():
                s = v.get("source","manual")
                sources[s] = sources.get(s,0)+1
                sums[s]    = sums.get(s,0)+float(v["profit"])
            total_trades = sum(sources.values())
            percent_total = round((total / total_trades)*100,2) if total_trades and total!=0 else 0
            msg = "📊 ملخص الأرباح:\n"
            msg += f"إجمالي الربح: {round(total,2)} EUR ({percent_total}%)\n"
            if sources:
                for s in sources:
                    msg += f"• {s.capitalize()}: {round(sums[s],2)} EUR من {sources[s]} صفقة\n"
            else:
                msg += "لا توجد سجلات بعد."
            send_message(msg)
            return "", 200

        # --- مسح
        if "امسح الذاكرة" in text:
            for k in MEM: MEM[k].clear()
            send_message("🧹 تم مسح الذاكرة.")
            return "", 200

        # --- الرصيد
        if "الرصيد" in text:
            bals = bv_request("GET", "/balance")
            try:
                eur = next((float(b['available']) for b in bals if b.get('symbol')=='EUR'), 0.0)
                send_message(f"💰 الرصيد المتاح: {eur:.2f} EUR")
            except:
                send_message("❌ فشل جلب الرصيد.")
            return "", 200

        # --- شراء: "اشتري ADA يا توتو"
        if ("اشتري" in text) and ("يا توتو" in text):
            parts = text.split()
            # نتوقع: اشتري  COIN  يا  توتو
            if len(parts) < 2:
                send_message("❌ صيغة الشراء: اشتري COIN يا توتو")
                return "", 200
            coin = parts[1].upper()
            market = f"{coin}-EUR"

            # تأكد أن لدينا دقة السوق
            if market not in MARKET_META:
                load_markets()
            if market not in MARKET_META:
                send_message(f"❌ {market} غير متاح.")
                return "", 200

            # نفذ شراء Maker بكل الرصيد
            res = maker_buy_all(market)
            if not res:
                # السبب تم إرساله مسبقًا
                return "", 200

            # سجل أولي (نفس منطقك)
            MEM["orders"][market] = "شراء"
            MEM["entry"][market]  = float(res["price"])
            MEM["peak"][market]   = float(res["price"])
            MEM["source"][market] = "manual"

            # متابعة بسيطة: نلغي بعد مهلة إن لم يُملأ (حتى لا يترك أمر عالق)
            def _watch_order(oid: str, m: str):
                t0 = time.time()
                filled_any = False
                while time.time()-t0 < 60:
                    st = fetch_order(oid, m)
                    status = (st or {}).get("status","")
                    if status in ("filled","partiallyFilled"):
                        filled_any = True
                        break
                    time.sleep(1.0)
                if not filled_any:
                    cancel_order(oid, m)
                    send_message("⚠️ لم يُنفّذ الأمر ضمن المهلة، تم الإلغاء تلقائيًا.")
            Thread(target=_watch_order, args=(res["orderId"], market), daemon=True).start()

            return "", 200

        # --- بيع: "بيع ADA يا توتو"
        if ("بيع" in text) and ("يا توتو" in text):
            parts = text.split()
            if len(parts) < 2:
                send_message("❌ صيغة البيع: بيع COIN يا توتو")
                return "", 200
            coin = parts[1].upper()
            market = f"{coin}-EUR"
            if market not in MARKET_META:
                load_markets()
            if market not in MARKET_META:
                send_message(f"❌ {market} غير متاح.")
                return "", 200

            res = maker_sell_all(market)
            return "", 200

        # غير ذلك: تجاهل بهدوء
        return "", 200

    except Exception as e:
        traceback.print_exc()
        send_message(f"🐞 خطأ ويبهوك: {e}")
        return "", 200

# ======== فحص سريع =========
@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "GET":
        return "Toto Premium 🟢", 200
    # أي POST على / يعتبر خطأ طريقة عند Rails/NGINX، نخليه 405 متوافقًا
    return "", 405

# ======== تشغيل =========
if __name__ == "__main__":
    load_markets()
    send_message("🚀 Toto Premium بدأ العمل!")
    app.run(host="0.0.0.0", port=PORT)