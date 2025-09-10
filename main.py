# -*- coding: utf-8 -*-
"""
SaqerX — Maker-Only Smart Executor (Bitvavo / EUR)
- يشتري/يبيع Maker (postOnly) فقط، مع دقة سعر/كمية حسب /markets.
- المحاولة = وضع أمر حقيقي فقط (لا سبام رسائل). تباطؤ/انتظار ذكي بين المحاولات.
- يجمع partial fills، ويراقب ويفسخ أي أمر معلق عند الفشل/الانتهاء.
- إصلاحات Bitvavo: operatorId=""، لا amountQuote للّيمت، دقة tick/step، Backoff 216.
- التحكم: يستقبل buy فقط من بوت الإشارة عبر /hook، وباقي الإشعارات على تيلغرام.
"""

import os, re, time, json, math, traceback, hmac, hashlib, requests, websocket
from threading import Thread, Lock
from uuid import uuid4
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ========= Boot / ENV =========
load_dotenv()
app = Flask(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN","").strip()
CHAT_ID     = os.getenv("CHAT_ID","").strip()
API_KEY     = os.getenv("BITVAVO_API_KEY","").strip()
API_SECRET  = os.getenv("BITVAVO_API_SECRET","").strip()
PORT        = int(os.getenv("PORT","5000"))
RUN_LOCAL   = os.getenv("RUN_LOCAL","0")=="1"

# حماية الرصيد
EST_FEE_RATE        = float(os.getenv("FEE_RATE_EST", "0.0025"))  # ~0.25%
HEADROOM_EUR_MIN    = float(os.getenv("HEADROOM_EUR_MIN", "0.50"))
MAX_SPEND_FRACTION  = float(os.getenv("MAX_SPEND_FRACTION", "0.90"))
FIXED_EUR_PER_TRADE = float(os.getenv("FIXED_EUR", "0"))          # 0=disabled

# سلوك المحاولة
MAKER_WAIT_BASE_SEC   = int(os.getenv("MAKER_WAIT_BASE_SEC","45"))
MAKER_WAIT_MAX_SEC    = int(os.getenv("MAKER_WAIT_MAX_SEC","300"))
MAKER_REPRICE_EVERY   = float(os.getenv("MAKER_REPRICE_EVERY","2.0"))  # متابعة نفس الأمر
MAKER_REPRICE_THRESH  = float(os.getenv("MAKER_REPRICE_THRESH","0.0005")) # 0.05%
ATTEMPT_COOLDOWN_SEC  = float(os.getenv("ATTEMPT_COOLDOWN_SEC","1.0"))   # تهدئة بين وضع الأوامر
REFRESH_OB_SLEEP_SEC  = float(os.getenv("REFRESH_OB_SLEEP_SEC","0.6"))   # تحديث دفتر الأوامر
REPORT_PROGRESS_EVERY = int(os.getenv("REPORT_PROGRESS_EVERY","6"))      # كل كم محاولة نرسل "محاولات مستمرة…"

BUY_MIN_EUR           = float(os.getenv("BUY_MIN_EUR","5.0"))

# Backoff لخطأ 216
IB_BACKOFF_FACTOR     = float(os.getenv("IB_BACKOFF_FACTOR","0.96"))
IB_BACKOFF_TRIES_MAX  = int(os.getenv("IB_BACKOFF_TRIES","5"))

# WS/API
BASE_URL = "https://api.bitvavo.com/v2"
WS_URL   = "wss://ws.bitvavo.com/v2/"

# ========= Runtime =========
enabled         = True
active_trade    = None
executed_trades = []
lk              = Lock()
MARKET_MAP      = {}   # "DATA" -> "DATA-EUR"
MARKET_META     = {}   # "DATA-EUR" -> {tick, step, minQuote, minBase}
_ws_prices      = {}
_ws_lock        = Lock()

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

def _sig(ts, method, path, body=""):
    return hmac.new(API_SECRET.encode(), f"{ts}{method}{path}{body}".encode(), hashlib.sha256).hexdigest()

def bv_request(method, path, body=None, timeout=12):
    url = f"{BASE_URL}{path}"
    ts = str(int(time.time()*1000))
    body_str = "" if method=="GET" else json.dumps(body or {}, separators=(',',':'))
    headers = {
        'Bitvavo-Access-Key': API_KEY,
        'Bitvavo-Access-Timestamp': ts,
        'Bitvavo-Access-Signature': _sig(ts, method, f"/v2{path}", body_str),
        'Bitvavo-Access-Window': '10000'
    }
    try:
        r = requests.request(method, url, headers=headers,
                             json=(body if method!="GET" else None), timeout=timeout)
        j = r.json()
        if isinstance(j, dict) and j.get("error"):
            print("Bitvavo error:", j)
        return j
    except Exception as e:
        print("bv_request err:", e)
        return {"error":"request_failed"}

def get_eur_available() -> float:
    try:
        bals = bv_request("GET","/balance")
        if isinstance(bals, list):
            for b in bals:
                if b.get("symbol")=="EUR":
                    return max(0.0, float(b.get("available",0) or 0))
    except: pass
    return 0.0

# ========= Markets =========
def load_markets():
    global MARKET_MAP, MARKET_META
    try:
        rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
        m, meta = {}, {}
        for r0 in rows:
            if r0.get("quote")!="EUR": continue
            base=r0.get("base"); market=r0.get("market")
            tick=float(r0.get("pricePrecision",1e-6) or 1e-6)
            step=float(r0.get("amountPrecision",1e-8) or 1e-8)
            meta[market]={
                "tick":tick, "step":step,
                "minQuote": float(r0.get("minOrderInQuoteAsset",5) or 5.0),
                "minBase":  float(r0.get("minOrderInBaseAsset",0) or 0.0)
            }
            m[base.upper()]=market
        if m: MARKET_MAP=m
        if meta: MARKET_META=meta
    except Exception as e:
        print("load_markets err:", e)

def coin_to_market(coin): 
    if not MARKET_MAP: load_markets()
    return MARKET_MAP.get(coin.upper())

def _decs_from_step(step: float)->int:
    try:
        return 0 if step>=1 else max(0,int(round(-math.log10(step))))
    except: return 8

def _round_price(market, price):
    tick=MARKET_META.get(market,{}).get("tick",1e-6)
    d=_decs_from_step(tick)
    p=round(float(price), d)
    return max(tick, p)

def _round_amount(market, amt):
    step=MARKET_META.get(market,{}).get("step",1e-8)
    floored=math.floor(float(amt)/step)*step
    d=_decs_from_step(step)
    return round(max(step, floored), d)

def _format_price(market, p): 
    d=_decs_from_step(MARKET_META.get(market,{}).get("tick",1e-6))
    return f"{_round_price(market,p):.{d}f}"

def _format_amount(market, a):
    d=_decs_from_step(MARKET_META.get(market,{}).get("step",1e-8))
    return f"{_round_amount(market,a):.{d}f}"

def _min_quote(market): return MARKET_META.get(market,{}).get("minQuote",BUY_MIN_EUR)
def _min_base(market):  return MARKET_META.get(market,{}).get("minBase",0.0)

# ========= Orderbook / Price =========
def fetch_orderbook(market):
    try:
        j = requests.get(f"{BASE_URL}/{market}/book", timeout=6).json()
        if j and j.get("bids") and j.get("asks"): return j
    except: pass
    return None

# ========= Orders =========
def _place_limit_postonly(market, side, price, amount):
    body = {
        "market": market,
        "side": side,
        "orderType": "limit",
        "postOnly": True,
        "price": _format_price(market, price),
        "amount": _format_amount(market, amount),
        "clientOrderId": str(uuid4()),
        "operatorId": ""  # ✅ إلزامي عندهم (حتى لو فاضي)
    }
    return bv_request("POST","/order",body)

def _fetch_order(orderId):         return bv_request("GET",    f"/order?orderId={orderId}")
def _cancel_order(orderId):        return bv_request("DELETE", f"/order?orderId={orderId}")
def _cancel_all_for_market(mkt):   return bv_request("DELETE", f"/orders?market={mkt}")
def _open_orders_market(mkt):
    j=bv_request("GET",f"/ordersOpen?market={mkt}")
    return j if isinstance(j,list) else []

def totals_from_fills(fills):
    tb=tq=fee=0.0
    for f in (fills or []):
        a=float(f["amount"]); pr=float(f["price"]); fe=float(f.get("fee",0) or 0)
        tb+=a; tq+=a*pr; fee+=fe
    return tb,tq,fee

# ========= Maker BUY =========
def _calc_buy_amount_base(market, target_eur, price):
    base=max(_min_base(market), float(target_eur)/max(1e-12, float(price)))
    return _round_amount(market, base)

def open_maker_buy(market: str, eur_amount: float):
    """محاولات حقيقية فقط + تهدئة + إلغاء أي أمر قبل الخروج."""
    # 0) إعداد مبلغ الشراء الآمن
    eur_avail=get_eur_available()
    target = FIXED_EUR_PER_TRADE if FIXED_EUR_PER_TRADE>0 else (eur_amount if eur_amount and eur_amount>0 else eur_avail)
    target = min(target, eur_avail*MAX_SPEND_FRACTION)
    buf    = max(HEADROOM_EUR_MIN, target*EST_FEE_RATE*2.0, 0.05)
    spend  = min(target, max(0.0, eur_avail-buf))
    need   = max(BUY_MIN_EUR, _min_quote(market))
    if spend < need:
        send_message(f"⛔ الرصيد غير كافٍ: متاح €{eur_avail:.2f} | بعد الهامش €{spend:.2f} (هامش €{buf:.2f}) | المطلوب ≥ €{need:.2f}.")
        return None
    send_message(f"💰 متاح: €{eur_avail:.2f} | سننفق: €{spend:.2f} (هامش €{buf:.2f} | هدف €{target:.2f})")

    # 1) حلقة تنفيذ
    patience=MAKER_WAIT_BASE_SEC
    start=time.time()
    last_order=None
    last_level=None
    placed_attempts=0   # ← فقط إذا حاولنا وضع أمر فعلاً
    ib_tries=0

    filled_fills=[]
    remaining_eur=float(spend)

    try:
        while (time.time()-start) < patience and remaining_eur >= (need*0.999):
            ob = fetch_orderbook(market)
            if not ob: 
                time.sleep(0.25); 
                continue

            best_bid=float(ob["bids"][0][0])
            best_ask=float(ob["asks"][0][0])

            # السعر المستخدَم للشراء Maker: لا نتجاوز الـbid، ونقترب من ask لكن تكة تحتها
            px = min(best_bid, best_ask*(1.0-5e-7))
            px = _round_price(market, px)

            # متابعة أمر قائم
            if last_order:
                st=_fetch_order(last_order); st_s=st.get("status")
                if st_s in ("filled","partiallyFilled"):
                    fills=st.get("fills",[]) or []
                    if fills:
                        a,q,f=totals_from_fills(fills)
                        filled_fills += fills
                        remaining_eur=max(0.0, remaining_eur - (q+f))
                if st_s=="filled" or remaining_eur < (need*0.999):
                    try:_cancel_order(last_order)
                    except:pass
                    last_order=None
                    break

                # إذا تغيّر الـbid بوضوح نعيد التسعير
                if (last_level is None) or (abs(best_bid/last_level-1.0) >= MAKER_REPRICE_THRESH):
                    try:_cancel_order(last_order)
                    except:pass
                    last_order=None
                    time.sleep(ATTEMPT_COOLDOWN_SEC)
                    continue
                else:
                    # نبقيه قليلاً قبل إعادة الفحص
                    time.sleep(MAKER_REPRICE_EVERY)
                    continue

            # لا يوجد أمر قائم → نحاول وضع أمر جديد (محاولة حقيقية)
            amt = _calc_buy_amount_base(market, remaining_eur, px)
            if amt <= 0:
                time.sleep(0.3); 
                continue

            # محاولة وضع الأمر
            res=_place_limit_postonly(market, "buy", px, amt)
            err=str(res.get("error","") or "").lower()
            oid=res.get("orderId")

            placed_attempts+=1
            if (placed_attempts==1) or (placed_attempts%REPORT_PROGRESS_EVERY==0):
                # تقرير تقدّم معقول (بدون سبام)
                send_message(f"🧪 محاولة شراء #{placed_attempts}: amount={_format_amount(market,amt)} | EUR≈{amt*px:.2f} | bid≈{best_bid} / ask≈{best_ask}")

            if oid:
                last_order=oid
                last_level=best_bid
                # ننتظر قليلاً قبل إعادة الفحص — فرصة للتعبئة
                elapsed=time.time()-start
                limit=min(MAKER_REPRICE_EVERY, max(0.6, ATTEMPT_COOLDOWN_SEC))
                t0=time.time()
                while time.time()-t0 < limit:
                    st=_fetch_order(last_order); st_s=st.get("status")
                    if st_s in ("filled","partiallyFilled"):
                        fills=st.get("fills",[]) or []
                        if fills:
                            a,q,f=totals_from_fills(fills)
                            filled_fills += fills
                            remaining_eur=max(0.0, remaining_eur - (q+f))
                        if st_s=="filled" or remaining_eur < (need*0.999):
                            try:_cancel_order(last_order)
                            except:pass
                            last_order=None
                            break
                    time.sleep(0.35)
                # إذا لم يُلغ، سنعود للدورة وسيجري الاختبار/الإلغاء إن لزم
                continue

            # لم يُنشَأ أمر
            if ("insufficient balance" in err) or ("not have sufficient balance" in err):
                ib_tries+=1
                remaining_eur = remaining_eur*IB_BACKOFF_FACTOR
                time.sleep(ATTEMPT_COOLDOWN_SEC)
                if ib_tries>=IB_BACKOFF_TRIES_MAX:
                    break
                continue

            # أي خطأ آخر: نهدي ونعاود
            time.sleep(ATTEMPT_COOLDOWN_SEC)
            # لا نعدّها إعادة محاولة وهمية؛ تم احتسابها ضمن placed_attempts بالفعل

            # تحديث دفتر الأوامر قبل الدورة التالية
            time.sleep(REFRESH_OB_SLEEP_SEC)

        # إلغاء أي أمر متبقّي
        if last_order:
            try:_cancel_order(last_order)
            except:pass

    except Exception as e:
        print("open_maker_buy err:", e)

    if not filled_fills:
        # لا أوامر مفتوحة + لا تعبئة → فشل فعلي: نذكر أرقام حقيقية
        oo=_open_orders_market(market)
        send_message("⚠️ لم يكتمل شراء Maker.\n"
                     f"• أوامر موضوعة: {len(oo)}\n"
                     f"• محاولات داخلية: {placed_attempts}\n"
                     f"• زمن: {time.time()-start:.1f}s\n"
                     "سنُعزّز الصبر تلقائيًا وسنحاول لاحقًا.")
        return None

    a,q,f = totals_from_fills(filled_fills)
    avg   = (q+f)/max(1e-12,a)
    return {"amount": a, "avg": avg, "cost_eur": q+f, "fee_eur": f}

# ========= Maker SELL (مماثل في الانضباط) =========
def close_maker_sell(market, amount):
    patience = MAKER_WAIT_BASE_SEC
    start=time.time()
    left=float(amount)
    last_order=None
    last_ask=None
    fills_all=[]

    try:
        while (time.time()-start)<patience and left>0:
            ob=fetch_orderbook(market)
            if not ob: time.sleep(0.3); continue
            bid=float(ob["bids"][0][0]); ask=float(ob["asks"][0][0])

            px=max(ask, bid*(1.0+5e-7))
            px=_round_price(market,px)

            if last_order:
                st=_fetch_order(last_order); s=st.get("status")
                if s in ("filled","partiallyFilled"):
                    fs=st.get("fills",[]) or []
                    if fs:
                        a,_,_=totals_from_fills(fs)
                        left=max(0.0,left-a); fills_all+=fs
                if s=="filled" or left<=0:
                    try:_cancel_order(last_order)
                    except:pass
                    last_order=None
                    break

                if (last_ask is None) or (abs(ask/last_ask-1.0)>=MAKER_REPRICE_THRESH):
                    try:_cancel_order(last_order)
                    except:pass
                    last_order=None
                    time.sleep(ATTEMPT_COOLDOWN_SEC)
                    continue
                else:
                    time.sleep(MAKER_REPRICE_EVERY)
                    continue

            amt=_round_amount(market,left)
            res=_place_limit_postonly(market,"sell",px,amt)
            oid=res.get("orderId")
            if oid:
                last_order=oid; last_ask=ask
                t0=time.time()
                while time.time()-t0 < max(0.6, ATTEMPT_COOLDOWN_SEC):
                    st=_fetch_order(last_order); s=st.get("status")
                    if s in ("filled","partiallyFilled"):
                        fs=st.get("fills",[]) or []
                        if fs:
                            a,_,_=totals_from_fills(fs)
                            left=max(0.0,left-a); fills_all+=fs
                        if s=="filled" or left<=0:
                            try:_cancel_order(last_order)
                            except:pass
                            last_order=None
                            break
                    time.sleep(0.35)
                continue

            time.sleep(ATTEMPT_COOLDOWN_SEC)

        if last_order:
            try:_cancel_order(last_order)
            except:pass

    except Exception as e:
        print("close_maker_sell err:", e)

    # تجميع عائد البيع
    sold=proceeds=fee=0.0
    for f in (fills_all or []):
        a=float(f["amount"]); p=float(f["price"]); fe=float(f.get("fee",0) or 0)
        sold+=a; proceeds+=a*p; fee+=fe
    proceeds -= fee
    return sold, proceeds, fee

# ========= Trade Flow / Summary =========
def do_open_maker(market, eur):
    def _run():
        global active_trade
        try:
            with lk:
                if active_trade:
                    send_message("⛔ توجد صفقة نشطة. أغلقها أولاً.")
                    return
            res=open_maker_buy(market, eur)
            if not res:
                send_message("⏳ لم يكتمل شراء Maker. سنحاول لاحقاً (الصبر يتكيف تلقائيًا).")
                return
            with lk:
                active_trade={
                    "symbol":market,
                    "entry":float(res["avg"]),
                    "amount":float(res["amount"]),
                    "cost_eur":float(res["cost_eur"]),
                    "fee_buy":float(res["fee_eur"]),
                    "opened_at":time.time()
                }
                executed_trades.append(active_trade.copy())
            send_message(f"✅ تم الشراء (Maker) {market.replace('-EUR','')} @ €{active_trade['entry']:.8f} | كمية {active_trade['amount']:.8f}")
        except Exception as e:
            traceback.print_exc()
            send_message(f"🐞 خطأ أثناء الفتح: {e}")
    Thread(target=_run, daemon=True).start()

def do_close_maker(reason="Manual"):
    global active_trade
    try:
        with lk:
            if not active_trade: 
                send_message("لا توجد صفقة لإغلاقها."); 
                return
            m=active_trade["symbol"]; amt=float(active_trade["amount"]); cost=float(active_trade["cost_eur"])
        sold, got, fee = close_maker_sell(m, amt)
        pnl=got-cost; pct=(got/cost-1.0)*100.0 if cost>0 else 0.0
        with lk:
            for t in reversed(executed_trades):
                if t["symbol"]==m and "exit_eur" not in t:
                    t.update({"exit_eur":got,"fee_sell":fee,"pnl_eur":pnl,"pnl_pct":pct,"exit_time":time.time()})
                    break
            active_trade=None
        send_message(f"💰 بيع {m.replace('-EUR','')}: {pnl:+.2f}€ ({pnl:+.2f}% إن لم تكن الرسالة نسبة فاقرأ السطر السابق) — {reason}")
    except Exception as e:
        traceback.print_exc()
        send_message(f"🐞 خطأ أثناء الإغلاق: {e}")

def build_summary():
    lines=[]
    with lk:
        at=active_trade
        closed=[x for x in executed_trades if "exit_eur" in x]
    if at:
        lines.append("📌 صفقة نشطة:")
        lines.append(f"• {at['symbol'].replace('-EUR','')} @ €{at['entry']:.8f} | كمية {at['amount']:.8f}")
    else:
        lines.append("📌 لا صفقات نشطة.")
    pnl=sum(float(x.get("pnl_eur",0)) for x in closed)
    wins=sum(1 for x in closed if float(x.get("pnl_eur",0))>=0)
    lines.append(f"\n📊 مكتملة: {len(closed)} | محققة: {pnl:+.2f}€ | فوز/خسارة: {wins}/{len(closed)-wins}")
    lines.append("\n⚙️ buy=Maker | sell=Maker | محاولات حقيقية فقط + إلغاء تلقائي لأي أمر معلق.")
    return "\n".join(lines)

# ========= Webhook =========
@app.route("/hook", methods=["POST"])
def hook():
    """
    يأخذ فقط إشارات الشراء من بوت الإشارة:
      {"cmd":"buy","coin":"DATA","eur":8.2}
    باقي الأوامر من تلغرام فقط (رسائل صادرة).
    """
    try:
        data=request.get_json(silent=True) or {}
        cmd=(data.get("cmd") or "").strip().lower()
        if cmd!="buy":
            return jsonify({"ok":False,"err":"only_buy_allowed"})

        if not enabled:
            return jsonify({"ok":False,"err":"bot_disabled"})

        coin=(data.get("coin") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{2,15}", coin or ""):
            return jsonify({"ok":False,"err":"bad_coin"})
        mkt=coin_to_market(coin)
        if not mkt:
            send_message(f"⛔ {coin}-EUR غير متاح على Bitvavo.")
            return jsonify({"ok":False,"err":"market_unavailable"})

        eur = float(data.get("eur")) if data.get("eur") is not None else None
        do_open_maker(mkt, eur)
        return jsonify({"ok":True,"msg":"buy_started","market":mkt})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok":False,"err":str(e)}), 500

# ========= Minimal HTTP =========
@app.route("/", methods=["GET"])
def health(): return "SaqerX Maker ✅", 200

@app.route("/summary", methods=["GET"])
def http_summary(): return f"<pre>{build_summary()}</pre>"

# ========= Simple commands via GET (اختياري للتسهيل) =========
@app.route("/enable", methods=["GET"])
def http_enable():
    global enabled; enabled=True; send_message("✅ تم التفعيل."); return "ok"
@app.route("/disable", methods=["GET"])
def http_disable():
    global enabled; enabled=False; send_message("🛑 تم الإيقاف."); return "ok"
@app.route("/close", methods=["GET"])
def http_close():
    do_close_maker("Manual"); return "ok"

# ========= Main =========
if __name__ == "__main__" or RUN_LOCAL:
    load_markets()
    app.run(host="0.0.0.0", port=PORT)