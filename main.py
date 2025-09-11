# -*- coding: utf-8 -*-
"""
Saqer — Ultra Mini (Bitvavo / EUR)
أوامر تيليجرام فقط:
  /buy COIN     → شراء Maker بكل رصيد EUR (مع هامش)
  /sell COIN    → بيع Maker لكل رصيد العملة
  /cancel       → إلغاء واحد يلغي كل أوامر الصفقة النشطة

- صفقة واحدة فقط في نفس الوقت.
- متابعة سعر تلقائية قصيرة بعد /buy (chase) لتبقى على أفضل Bid.
"""

import os, re, time, math, json, hmac, hashlib, requests, threading
from uuid import uuid4
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ========= ENV / App =========
load_dotenv()
app = Flask(__name__)

BOT_TOKEN    = os.getenv("BOT_TOKEN","").strip()
CHAT_ID      = os.getenv("CHAT_ID","").strip()
API_KEY      = os.getenv("BITVAVO_API_KEY","").strip()
API_SECRET   = os.getenv("BITVAVO_API_SECRET","").strip().encode()
PORT         = int(os.getenv("PORT","8080"))

BASE         = "https://api.bitvavo.com/v2"
OPERATOR_ID  = int(os.getenv("OPERATOR_ID","2001"))
HEADROOM_EUR = float(os.getenv("HEADROOM_EUR","0.30"))  # اترك شوية يورو للرسوم

# متابعة السعر بعد الشراء
CHASE_SECONDS  = float(os.getenv("CHASE_SECONDS","20"))  # مدة التلاحق
CHASE_INTERVAL = float(os.getenv("CHASE_INTERVAL","0.6"))# فاصل التعديل

# ========= Telegram =========
def tg_send(text:str):
    if not BOT_TOKEN:
        print("TG:", text); return
    data={"chat_id": CHAT_ID or None, "text": text}
    try:
        if CHAT_ID:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=data, timeout=8)
        else:
            print("TG(no CHAT_ID):", text)
    except Exception as e:
        print("tg_send err:", e)

def _auth_chat(chat_id:str)->bool:
    return (not CHAT_ID) or (str(chat_id)==str(CHAT_ID))

# ========= Bitvavo base =========
def _ts()->str: return str(int(time.time()*1000))
def _sig(method:str, path:str, body:str="")->str:
    msg=_ts()+method+path+body
    return hmac.new(API_SECRET, msg.encode(), hashlib.sha256).hexdigest()
def _hdr(method:str, path:str, body:str="")->dict:
    return {
        "Bitvavo-Access-Key": API_KEY,
        "Bitvavo-Access-Signature": _sig(method, path, body),
        "Bitvavo-Access-Timestamp": _ts(),
        "Bitvavo-Access-Window": "10000",
        "Content-Type": "application/json"
    }

def bv_get(path:str, auth=True, timeout=10):
    r=requests.get(f"{BASE}{path}", headers=_hdr("GET",path,"") if auth else {}, timeout=timeout)
    try: return r.json()
    except: return {"error": r.text, "status": r.status_code}

def bv_send(method:str, path:str, body:dict|None=None, timeout=10):
    b=json.dumps(body or {}, separators=(",",":"))
    r=requests.request(method, f"{BASE}{path}", headers=_hdr(method, path, b), data=b, timeout=timeout)
    try: return r.json()
    except: return {"error": r.text, "status": r.status_code}

# ========= Markets meta / precision =========
MARKET_MAP, MARKET_META = {}, {}

def load_markets_once():
    global MARKET_MAP, MARKET_META
    if MARKET_MAP and MARKET_META: return
    rows=bv_get("/markets", auth=False) or []
    m,meta={},{}
    for r in rows:
        if r.get("quote")!="EUR": continue
        base=(r.get("base") or "").upper()
        market=r.get("market")
        if not base or not market: continue
        priceSig=int(r.get("pricePrecision",6) or 6)  # significant digits
        ap=r.get("amountPrecision",8)
        step=10.0**(-int(ap)) if isinstance(ap,int) else float(ap or 1e-8)
        meta[market]={"priceSig":priceSig,"step":float(step),
                      "minQuote":float(r.get("minOrderInQuoteAsset",0) or 0.0),
                      "minBase": float(r.get("minOrderInBaseAsset",  0) or 0.0)}
        m[base]=market
    MARKET_MAP, MARKET_META=m,meta

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

# ========= Single active trade state =========
ACTIVE = {
    "market": None,
    "orderId": None,
    "clientOrderId": None,
    "chase_stop": False
}

def _set_active(m=None, oid=None, coid=None):
    ACTIVE["market"]=m; ACTIVE["orderId"]=oid; ACTIVE["clientOrderId"]=coid

def _clear_active():
    ACTIVE["market"]=None; ACTIVE["orderId"]=None; ACTIVE["clientOrderId"]=None; ACTIVE["chase_stop"]=False

# ========= Core: place/cancel/update =========
def create_maker(market:str, side:str, price:float, amount:float):
    body={
        "market": market, "side": side, "orderType": "limit", "postOnly": True,
        "clientOrderId": str(uuid4()), "operatorId": OPERATOR_ID,
        "price": fmt_price(market, price),
        "amount": fmt_amount(market, amount)
    }
    return bv_send("POST","/order", body)

def update_price_put(market:str, orderId:str, new_price:float):
    body={"market":market, "orderId":orderId, "price":fmt_price(market, new_price), "operatorId": OPERATOR_ID}
    return bv_send("PUT","/order", body)

def cancel_all_market(market:str):
    # إلغاء واحد — يلغي كل أوامر هذا الماركت
    body={"operatorId": OPERATOR_ID}
    return bv_send("DELETE", f"/orders?market={market}", body)

def wait_orders_cleared(market:str, timeout=8, poll=0.25):
    t0=time.time()
    while time.time()-t0<timeout:
        open_=bv_get(f"/ordersOpen?market={market}")
        if isinstance(open_, list) and len(open_)==0: return True
        time.sleep(poll)
    return False

def fetch_order(market:str, orderId:str):
    return bv_get(f"/order?market={market}&orderId={orderId}")

# ========= BUY / SELL / CANCEL =========
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
    if spend<max(minq, minb*px): return {"ok":False, "err":"below_minimums"}

    amt=spend/px
    j=create_maker(market, "buy", px, amt)
    if "orderId" not in j: return {"ok":False, "err": j.get("error", j), "request":{"market":market,"price":fmt_price(market,px),"amount":fmt_amount(market,amt)}}

    _set_active(market, j["orderId"], j.get("clientOrderId"))
    tg_send(f"✅ BUY أُرسل (Maker) — {market}\nالسعر: {fmt_price(market,px)} | الكمية: {fmt_amount(market,amt)}")

    # ابدأ التلاحق (chase) بخيط منفصل — بدون أي أوامر من المستخدم
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
    tg_send(f"✅ SELL أُرسل (Maker) — {market}\nالسعر: {fmt_price(market,px)} | الكمية: {fmt_amount(market,amt)}")
    return {"ok":True, "orderId": j["orderId"]}

def do_cancel():
    market=ACTIVE["market"]
    if not market:
        return {"ok":False, "err":"no_active_order"}
    ACTIVE["chase_stop"]=True  # أوقف التلاحق فورًا
    cancel_all_market(market)
    ok=wait_orders_cleared(market, timeout=10)
    if ok:
        _clear_active()
        tg_send(f"✅ Cancel — تم إلغاء جميع أوامر {market}")
        return {"ok":True}
    else:
        # حتى لو ما تأكّد 100%، نبقي الحالة نشطة للسلامة
        tg_send(f"⚠️ Cancel لم يتأكّد بعد — حاول لاحقًا")
        return {"ok":False, "err":"not_cleared"}

# ========= Chase loop =========
def _chase_loop(market:str, orderId:str):
    """يلحق أفضل Bid لفترة قصيرة باستخدام PUT /order. يتوقف إذا أُلغي الأمر أو امتلأ أو استُدعي /cancel."""
    t_end=time.time()+CHASE_SECONDS
    last_price=None
    while time.time()<t_end and ACTIVE["market"]==market and not ACTIVE["chase_stop"]:
        try:
            st=fetch_order(market, orderId)
            s=(st or {}).get("status","").lower()
            if s in ("filled","canceled"):  # انتهى
                _clear_active()
                tg_send(f"ℹ️ الحالة: {s} — {market}")
                return
            # السعر الحالي في الدفتر
            bid,ask=best_bid_ask(market)
            target=round_price_sig_down(min(bid, ask*(1-1e-6)), _price_sig(market))
            if last_price is None or abs(target - last_price) >= max(1e-12, target*1e-4):  # تحرك واضح ~0.01%
                upd=update_price_put(market, orderId, target)
                if upd.get("error"):
                    # إذا رفض السيرفر، لا نلغي ولا نعمل إعادة إنشاء — نحاول لاحقًا فقط
                    pass
                else:
                    last_price=target
            time.sleep(CHASE_INTERVAL)
        except Exception:
            time.sleep(0.8)
    # انتهاء مهلة التلاحق — نكتفي بالوضع الحالي
    ACTIVE["chase_stop"]=False

# ========= Telegram Webhook =========
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
            tg_send(("✅ تم الإرسال" if res.get("ok") else "⚠️ فشل") + f"\n{json.dumps(res, ensure_ascii=False)}")
            return jsonify(ok=True)

        if low.startswith("/sell"):
            parts=text.split()
            if len(parts)<2: tg_send("صيغة: /sell COIN"); return jsonify(ok=True)
            coin=parts[1].upper()
            if not COIN_RE.match(coin): tg_send("⛔ عملة غير صالحة."); return jsonify(ok=True)
            res=do_sell(coin)
            tg_send(("✅ تم الإرسال" if res.get("ok") else "⚠️ فشل") + f"\n{json.dumps(res, ensure_ascii=False)}")
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

# ========= Health =========
@app.route("/", methods=["GET"])
def home():
    return "Saqer Ultra Mini ✅"

# ========= Run =========
if __name__ == "__main__":
    load_markets_once()
    app.run(host="0.0.0.0", port=PORT)