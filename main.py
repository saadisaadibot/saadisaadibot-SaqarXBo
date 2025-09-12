# -*- coding: utf-8 -*-
"""
Saqer — Mini Maker (Bitvavo / EUR)
أوامر تيليجرام فقط (3):
  /buy COIN     → شراء Maker بكل رصيد EUR (مع هامش بسيط) + مطاردة سعر تلقائية وسريعة
  /sell COIN    → بيع Maker لكل رصيد العملة
  /cancel       → إلغاء واحد يلغي كل أوامر الحساب (كل الماركتات) ويوقف المطاردة

- صفقة واحدة فقط في نفس الوقت.
- المطاردة (chase): PUT /order لتحديث السعر نحو أفضل Bid؛
  وإذا PUT فشل، Cancel All + Create فوراً على السعر الجديد.
"""

import os, re, time, math, json, hmac, hashlib, requests, threading
from uuid import uuid4
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ========== ENV / APP ==========
load_dotenv()
app = Flask(__name__)

BOT_TOKEN     = os.getenv("BOT_TOKEN","").strip()
CHAT_ID       = os.getenv("CHAT_ID","").strip()
API_KEY       = os.getenv("BITVAVO_API_KEY","").strip()
API_SECRET    = os.getenv("BITVAVO_API_SECRET","").strip().encode()
PORT          = int(os.getenv("PORT","8080"))

BASE          = "https://api.bitvavo.com/v2"
OPERATOR_ID   = int(os.getenv("OPERATOR_ID","2001"))      # مهم لكل POST/PUT/DELETE
HEADROOM_EUR  = float(os.getenv("HEADROOM_EUR","0.30"))   # اترك شوية يورو للرسوم

# مطاردة السعر
CHASE_SECONDS   = float(os.getenv("CHASE_SECONDS","35"))  # مدة المطاردة
CHASE_INTERVAL  = float(os.getenv("CHASE_INTERVAL","0.40")) # فاصل التعديل
MIN_MOVE_FRAC   = float(os.getenv("MIN_MOVE_FRAC","0.0001")) # ~0.01% حد أدنى للتغيير

# ========== Telegram ==========
def tg_send(text:str):
    if not BOT_TOKEN:
        print("TG:", text); return
    data={"chat_id": CHAT_ID or None, "text": text}
    try:
        if CHAT_ID:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          json=data, timeout=8)
        else:
            print("TG(no CHAT_ID):", text)
    except Exception as e:
        print("tg_send err:", e)

def _auth_chat(chat_id:str)->bool:
    return (not CHAT_ID) or (str(chat_id)==str(CHAT_ID))

# ========== Bitvavo base ==========
def _now_ms()->int: return int(time.time()*1000)

def _headers(method:str, path:str, body_str:str)->dict:
    ts = str(_now_ms())
    msg = ts + method + path + body_str
    sig = hmac.new(API_SECRET, msg.encode(), hashlib.sha256).hexdigest()
    return {
        "Bitvavo-Access-Key": API_KEY,
        "Bitvavo-Access-Signature": sig,
        "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Window": "10000",
        "Content-Type": "application/json"
    }

def bv_get(path:str, auth=True, timeout=10):
    url=f"{BASE}{path}"
    hdr=_headers("GET", path, "") if auth else {}
    r=requests.get(url, headers=hdr, timeout=timeout)
    try: return r.json()
    except: return {"error": r.text, "status": r.status_code}

def bv_send(method:str, path:str, body:dict|None=None, timeout=10):
    url=f"{BASE}{path}"
    b=json.dumps(body or {}, separators=(",",":"))
    hdr=_headers(method, path, b)
    r=requests.request(method, url, headers=hdr, data=b, timeout=timeout)
    try: return r.json()
    except: return {"error": r.text, "status": r.status_code}

# ========== Markets meta / precision ==========
MARKET_MAP, MARKET_META = {}, {}

def load_markets_once():
    global MARKET_MAP, MARKET_META
    if MARKET_MAP and MARKET_META: return
    rows=bv_get("/markets", auth=False) or []
    m, meta = {}, {}
    for r in rows:
        if r.get("quote")!="EUR": continue
        base=(r.get("base") or "").upper()
        market=r.get("market")
        if not base or not market: continue
        priceSig = int(r.get("pricePrecision",6) or 6)  # significant digits
        ap = r.get("amountPrecision",8)
        step = 10.0**(-int(ap)) if isinstance(ap,int) else float(ap or 1e-8)
        meta[market]={"priceSig":priceSig,"step":float(step),
                      "minQuote":float(r.get("minOrderInQuoteAsset",0) or 0.0),
                      "minBase": float(r.get("minOrderInBaseAsset",  0) or 0.0)}
        m[base]=market
    MARKET_MAP, MARKET_META = m, meta

def coin_to_market(coin:str)->str|None:
    load_markets_once(); return MARKET_MAP.get(coin.upper())

def _meta(market:str)->dict: load_markets_once(); return MARKET_META.get(market,{})
def _price_sig(market:str)->int: return int(_meta(market).get("priceSig",6))
def _step(market:str)->float:     return float(_meta(market).get("step",1e-8))
def _min_quote(market:str)->float:return float(_meta(market).get("minQuote",0.0))
def _min_base(market:str)->float: return float(_meta(market).get("minBase",0.0))

def round_price_sig_down(price: float, sig: int) -> float:
    if price<=0 or sig<=0: return 0.0
    exp=math.floor(math.log10(abs(price)))
    dec=max(0, sig-exp-1)
    factor=10**dec
    return math.floor(price*factor)/factor

def fmt_price(market:str, price:float)->str:
    p=round_price_sig_down(price, _price_sig(market))
    s=f"{p:.12f}".rstrip("0").rstrip(".")
    return s if s else "0"

def round_amount_down(market:str, amount:float)->float:
    st=_step(market)
    return max(0.0, math.floor(amount/st)*st)

def fmt_amount(market:str, amount:float)->str:
    st=_step(market)
    s=f"{st:.16f}".rstrip("0").rstrip("."); dec=len(s.split(".")[1]) if "." in s else 0
    a=round_amount_down(market, amount)
    return f"{a:.{dec}f}"

def best_bid_ask(market:str)->tuple[float,float]:
    ob=bv_get(f"/{market}/book?depth=1", auth=False)
    bid=float(ob["bids"][0][0]); ask=float(ob["asks"][0][0])
    return bid, ask

def balance(symbol:str)->float:
    bals=bv_get("/balance")
    if isinstance(bals,list):
        for b in bals:
            if b.get("symbol")==symbol.upper():
                return float(b.get("available",0) or 0.0)
    return 0.0

# ========== Active trade (واحدة فقط) ==========
ACTIVE = {
    "market": None,
    "orderId": None,
    "clientOrderId": None,
    "chasing": False,
    "stop_chase": False
}

def _set_active(m, oid, coid):
    ACTIVE["market"]=m; ACTIVE["orderId"]=oid; ACTIVE["clientOrderId"]=coid
    ACTIVE["chasing"]=False; ACTIVE["stop_chase"]=False

def _clear_active():
    ACTIVE["market"]=None; ACTIVE["orderId"]=None; ACTIVE["clientOrderId"]=None
    ACTIVE["chasing"]=False; ACTIVE["stop_chase"]=False

# ========== Core REST ops ==========
def create_maker(market:str, side:str, price:float, amount:float):
    body={
        "market": market, "side": side, "orderType": "limit", "postOnly": True,
        "clientOrderId": str(uuid4()),
        "operatorId": OPERATOR_ID,
        "price": fmt_price(market, price),
        "amount": fmt_amount(market, amount)
    }
    return bv_send("POST","/order", body)

def update_price_put(market:str, orderId:str, new_price:float):
    body={"market":market, "orderId":orderId,
          "price":fmt_price(market, new_price), "operatorId": OPERATOR_ID}
    return bv_send("PUT","/order", body)

def cancel_all_all():
    # إلغاء شامل لكل الأوامر بكل الماركتات — أمر واحد كما طلبت
    return bv_send("DELETE","/orders", {"operatorId": OPERATOR_ID})

def wait_all_cleared(timeout=10, poll=0.25):
    t0=time.time()
    while time.time()-t0<timeout:
        open_=bv_get("/ordersOpen")
        if isinstance(open_, list) and len(open_)==0:
            return True
        time.sleep(poll)
    return False

def fetch_order(market:str, orderId:str):
    return bv_get(f"/order?market={market}&orderId={orderId}")

# ========== BUY / SELL / CANCEL ==========
def do_buy(coin:str):
    if ACTIVE["market"]:
        return {"ok": False, "err":"another_order_active", "active": ACTIVE}

    market=coin_to_market(coin)
    if not market: return {"ok":False, "err":"bad_coin"}

    eur=balance("EUR")
    spend=max(0.0, eur-HEADROOM_EUR)
    if spend<=0: return {"ok":False, "err":f"no_eur (avail={eur:.2f})"}

    minq=_min_quote(market); minb=_min_base(market)
    bid,ask=best_bid_ask(market)
    px=round_price_sig_down(min(bid, ask*(1-1e-6)), _price_sig(market))

    if spend<max(minq, minb*px):
        return {"ok":False, "err":"below_minimums", "minQuote":minq, "minBase":minb, "price":px}

    amt=spend/px
    j=create_maker(market, "buy", px, amt)
    if "orderId" not in j:
        return {"ok":False, "err": j.get("error", j), "request":{"market":market,"price":fmt_price(market,px),"amount":fmt_amount(market,amt)}}

    _set_active(market, j["orderId"], j.get("clientOrderId"))
    tg_send(f"✅ BUY — {market}\npx={fmt_price(market,px)} | amt={fmt_amount(market,amt)}")

    # ابدأ مطاردة السعر تلقائيًا
    threading.Thread(target=_chase_loop, args=(market, j["orderId"]), daemon=True).start()
    return {"ok":True, "market":market, "orderId": j["orderId"]}

def do_sell(coin:str):
    market=coin_to_market(coin)
    if not market: return {"ok":False, "err":"bad_coin"}
    base=market.split("-")[0]
    amt=balance(base)
    amt=round_amount_down(market, amt)
    if amt<=0: return {"ok":False, "err":f"no_{base}"}
    minb=_min_base(market)
    if amt<minb: return {"ok":False, "err": f"minBase={minb}"}
    bid,ask=best_bid_ask(market)
    px=round_price_sig_down(max(ask, bid*(1+1e-6)), _price_sig(market))
    j=create_maker(market, "sell", px, amt)
    if "orderId" not in j: return {"ok":False, "err": j.get("error", j)}
    tg_send(f"✅ SELL — {market}\npx={fmt_price(market,px)} | amt={fmt_amount(market,amt)}")
    return {"ok":True, "orderId": j["orderId"]}

def do_cancel():
    # أمر واحد يلغي الكل
    ACTIVE["stop_chase"]=True
    cancel_all_all()
    ok=wait_all_cleared(timeout=12, poll=0.30)
    if ok:
        _clear_active()
        tg_send("✅ Cancel — تم إلغاء كل الأوامر")
        return {"ok":True}
    else:
        tg_send("⚠️ Cancel — لم تتصفّر الأوامر بعد")
        return {"ok":False, "err":"not_cleared", "still_open": bv_get("/ordersOpen")}

# ========== Chase: مطاردة السعر الذكية ==========
def _chase_loop(market:str, orderId:str):
    """
    نعدّل السعر ليطابق أفضل Bid تقريبًا.
    - إذا PUT نجح → ممتاز.
    - إذا PUT فشل (أحيانًا بسبب قيود أو سباق) → Cancel All + Create فورًا على السعر الجديد.
    - نتوقف عند: انتهاء المدة، امتلاء الطلب/إلغاؤه، أو استدعاء /cancel.
    """
    ACTIVE["chasing"]=True
    t_end=time.time()+CHASE_SECONDS
    last_px=None
    while time.time()<t_end and ACTIVE["market"]==market and not ACTIVE["stop_chase"]:
        try:
            st=fetch_order(market, orderId)
            s=(st or {}).get("status","").lower()
            if s in ("filled","canceled"):
                _clear_active()
                tg_send(f"ℹ️ حالة: {s} — {market}")
                return

            bid,ask=best_bid_ask(market)
            target=round_price_sig_down(min(bid, ask*(1-1e-6)), _price_sig(market))

            # لا تعدّل إلا إذا التحرّك واضح صغير (لتقليل السبام)
            if last_px is None or abs(target/last_px-1.0) >= max(1e-6, MIN_MOVE_FRAC):
                upd=update_price_put(market, orderId, target)
                if upd.get("error"):
                    # Fallback قوي: Cancel All + Create فوراً على السعر الجديد
                    cancel_all_all()
                    wait_all_cleared(timeout=6, poll=0.25)
                    try:
                        # الكمية المتبقية تقريبية: amountRemaining أو amount
                        amt_rem = float((st or {}).get("amountRemaining", st.get("amount","0")) or 0.0)
                        if amt_rem<=0:
                            amt_rem = float(OPEN.get("amount_init", 0.0)) if (OPEN:= {"amount_init":0}) else 0.0
                    except Exception:
                        amt_rem = 0.0
                    # إذا ما عرفنا المتبقي، نعيد التقدير من الرصيد
                    eur=balance("EUR")
                    if amt_rem<=0 and eur>0:
                        px_cur = target
                        minq=_min_quote(market); minb=_min_base(market)
                        if eur>=max(minq, minb*px_cur):
                            amt_rem = eur/px_cur
                    if amt_rem>0:
                        j=create_maker(market, "buy", target, amt_rem)
                        if "orderId" in j:
                            orderId=j["orderId"]
                            last_px=target
                            # نكمل المطاردة على الـ order الجديد
                        else:
                            # ما قدرنا نعيد — نوقف المطاردة
                            ACTIVE["stop_chase"]=True
                    else:
                        ACTIVE["stop_chase"]=True
                else:
                    last_px=target

            time.sleep(CHASE_INTERVAL)
        except Exception:
            time.sleep(0.6)

    ACTIVE["chasing"]=False
    # بعد انتهاء المطاردة، نترك الأمر كما هو

# ========== Telegram (3 أوامر فقط) ==========
COIN_RE = re.compile(r"^[A-Z0-9]{2,15}$")

@app.route("/tg", methods=["POST"])
def tg_webhook():
    upd=request.get_json(silent=True) or {}
    msg=upd.get("message") or upd.get("edited_message") or {}
    chat=msg.get("chat") or {}
    chat_id=str(chat.get("id") or "")
    text=(msg.get("text") or "").strip()
    if not chat_id: return jsonify(ok=True)
    if not _auth_chat(chat_id): return jsonify(ok=True)

    low=text.lower()
    try:
        if low.startswith("/buy"):
            parts=text.split()
            if len(parts)<2: tg_send("صيغة: /buy COIN"); return jsonify(ok=True)
            coin=parts[1].upper()
            if not COIN_RE.match(coin): tg_send("⛔ عملة غير صالحة."); return jsonify(ok=True)
            res=do_buy(coin)
            tg_send(("✅ تم" if res.get("ok") else "⚠️ فشل") + f"\n{json.dumps(res, ensure_ascii=False)}")
            return jsonify(ok=True)

        if low.startswith("/sell"):
            parts=text.split()
            if len(parts)<2: tg_send("صيغة: /sell COIN"); return jsonify(ok=True)
            coin=parts[1].upper()
            if not COIN_RE.match(coin): tg_send("⛔ عملة غير صالحة."); return jsonify(ok=True)
            res=do_sell(coin)
            tg_send(("✅ تم" if res.get("ok") else "⚠️ فشل") + f"\n{json.dumps(res, ensure_ascii=False)}")
            return jsonify(ok=True)

        if low.startswith("/cancel"):
            res=do_cancel()
            tg_send(("✅ تم" if res.get("ok") else "⚠️ فشل") + f"\n{json.dumps(res, ensure_ascii=False)}")
            return jsonify(ok=True)

        tg_send("الأوامر: /buy COIN — /sell COIN — /cancel")
        return jsonify(ok=True)

    except Exception as e:
        tg_send(f"🐞 خطأ: {e}")
        return jsonify(ok=True)

# ========== Health ==========
@app.route("/", methods=["GET"])
def home():
    return "Saqer Mini Maker ✅"

# ========== Run ==========
if __name__ == "__main__":
    load_markets_once()
    app.run(host="0.0.0.0", port=PORT)