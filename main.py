# -*- coding: utf-8 -*-
"""
Saqer — Maker-Only (Bitvavo / EUR) — Telegram /tg + AbuSiyah /hook
Chase Buy (from Abosiyah) + Smart TP + SL -2% + Robust Cancel + Reconcile + Ready

يتلقى من أبو صياح:
  POST /hook  JSON: {"action":"buy","coin":"ADA","tp_eur":0.05,"sl_pct":-2}

يرسل إلى أبو صياح عند الانتهاء:
  POST ABUSIYAH_READY_URL JSON: {"coin":"ADA","reason":"tp_filled|sl_triggered|buy_failed","pnl_eur":null}
  مع هيدر X-Link-Secret إذا محدد.

ENV:
  BOT_TOKEN, CHAT_ID, BITVAVO_API_KEY, BITVAVO_API_SECRET
  ABUSIYAH_READY_URL              # مثال: https://abosiyah.up.railway.app/ready
  LINK_SECRET                     # اختياري: نفس السر عند أبو صياح
  HEADROOM_EUR=0.30, CANCEL_WAIT_SEC=12, SHORT_FOLLOW_SEC=2
  CHASE_WINDOW_SEC=30, CHASE_REPRICE_PAUSE=0.35
  TAKE_PROFIT_EUR=0.05, MAKER_FEE_RATE=0.001
  SL_PCT=-2, SL_CHECK_SEC=0.8
"""

import os, re, time, json, hmac, hashlib, requests, threading
from uuid import uuid4
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from decimal import Decimal, ROUND_DOWN, getcontext

load_dotenv()
app = Flask(__name__)
getcontext().prec = 28

BOT_TOKEN   = os.getenv("BOT_TOKEN","").strip()
CHAT_ID     = os.getenv("CHAT_ID","").strip()
API_KEY     = os.getenv("BITVAVO_API_KEY","").strip()
API_SECRET  = os.getenv("BITVAVO_API_SECRET","").strip()
PORT        = int(os.getenv("PORT","8080"))
BASE_URL    = "https://api.bitvavo.com/v2"

HEADROOM_EUR        = float(os.getenv("HEADROOM_EUR","0.30"))
CANCEL_WAIT_SEC     = float(os.getenv("CANCEL_WAIT_SEC","12.0"))
SHORT_FOLLOW_SEC    = float(os.getenv("SHORT_FOLLOW_SEC","2.0"))
CHASE_WINDOW_SEC    = float(os.getenv("CHASE_WINDOW_SEC","30"))
CHASE_REPRICE_PAUSE = float(os.getenv("CHASE_REPRICE_PAUSE","0.35"))
TAKE_PROFIT_EUR     = float(os.getenv("TAKE_PROFIT_EUR","0.05"))
MAKER_FEE_RATE      = float(os.getenv("MAKER_FEE_RATE","0.001"))

ABUSIYAH_READY_URL  = os.getenv("ABUSIYAH_READY_URL","").strip()
LINK_SECRET         = os.getenv("LINK_SECRET","").strip()

SL_PCT              = float(os.getenv("SL_PCT","-2"))
SL_CHECK_SEC        = float(os.getenv("SL_CHECK_SEC","0.8"))

MARKET_MAP, MARKET_META, OPEN_ORDERS, POSITIONS = {}, {}, {}, {}

# ==== Telegram ====
def tg_send(text):
    if not BOT_TOKEN: print("TG:", text); return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID or None, "text": text}, timeout=8)
    except Exception as e:
        print("tg err:", e)

def _auth_chat(cid: str) -> bool: return (not CHAT_ID) or (str(cid)==str(CHAT_ID))

# ==== Bitvavo helpers ====
def _sign(ts, method, path, body=""):
    return hmac.new(API_SECRET.encode(), f"{ts}{method}{path}{body}".encode(), hashlib.sha256).hexdigest()

def bv_request(method: str, path: str, body: dict | None = None, timeout=10):
    url = f"{BASE_URL}{path}"
    ts  = str(int(time.time()*1000)); m = method.upper()
    if m in ("GET","DELETE"): body_str=""; payload=None
    else: body_str=json.dumps(body or {}, separators=(',',':')); payload=(body or {})
    headers = {
        "Bitvavo-Access-Key": API_KEY, "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Signature": _sign(ts, m, f"/v2{path}", body_str),
        "Bitvavo-Access-Window": "10000","Content-Type":"application/json",
    }
    r = requests.request(m, url, headers=headers, json=payload, timeout=timeout)
    try: return r.json()
    except: return {"error": r.text, "status_code": r.status_code}

def load_markets_once():
    global MARKET_MAP, MARKET_META
    if MARKET_MAP and MARKET_META: return
    rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
    m, meta = {}, {}
    for r in rows:
        if r.get("quote") != "EUR": continue
        market = r.get("market"); base = (r.get("base") or "").upper()
        if not base or not market: continue
        min_quote = float(r.get("minOrderInQuoteAsset",0) or 0.0)
        min_base  = float(r.get("minOrderInBaseAsset", 0) or 0.0)
        price_dec = _parse_price_precision(r.get("pricePrecision",6))
        amt_dec, step = _parse_amount_precision(r.get("amountPrecision",8), min_base)
        meta[market] = {"priceDecimals":price_dec,"amountDecimals":amt_dec,"step":float(step),
                        "minQuote":min_quote,"minBase":min_base}
        m[base] = market
    MARKET_MAP, MARKET_META = m, meta

def coin_to_market(c): load_markets_once(); return MARKET_MAP.get((c or "").upper())
def _meta(m): load_markets_once(); return MARKET_META.get(m,{})
def _price_decimals(m): return int(_meta(m).get("priceDecimals",6))
def _amount_decimals(m): return int(_meta(m).get("amountDecimals",8))
def _step(m): return float(_meta(m).get("step",1e-8))
def _min_quote(m): return float(_meta(m).get("minQuote",0.0))
def _min_base(m):  return float(_meta(m).get("minBase",0.0))

def _count_decimals_of_step(step: float) -> int:
    s = f"{step:.16f}".rstrip("0").rstrip("."); return len(s.split(".",1)[1]) if "." in s else 0
def _parse_amount_precision(ap, min_base_hint=0):
    try:
        mb = float(min_base_hint)
        if mb >= 1.0 and abs(mb - round(mb)) < 1e-9: return 0, 1.0
        v = float(ap)
        if float(v).is_integer():
            decs = max(0,int(v)); step = float(Decimal(1)/(Decimal(10)**decs)); return decs, step
        step = float(v); decs = _count_decimals_of_step(step); return decs, step
    except: return 8, 1e-8
def _parse_price_precision(pp):
    try:
        v = float(pp); 
        return _count_decimals_of_step(v) if (not float(v).is_integer() or v < 1.0) else int(v)
    except: return 6

def fmt_price_dec(m, p):
    q = Decimal(10) ** -_price_decimals(m)
    s = f"{(Decimal(str(p))).quantize(q, rounding=ROUND_DOWN):f}"
    if "." in s:
        w,f = s.split(".",1); f = f[:_price_decimals(m)].rstrip("0"); return w if not f else f"{w}.{f}"
    return s

def round_amount_down(m, a):
    decs = _amount_decimals(m); st = Decimal(str(_step(m) or 0)); A = Decimal(str(a))
    A = A.quantize(Decimal(10)**-decs, rounding=ROUND_DOWN)
    if st>0: A = (A // st)*st
    return float(A)

def fmt_amount(m,a):
    q = Decimal(10) ** -_amount_decimals(m)
    s = f"{(Decimal(str(a))).quantize(q, rounding=ROUND_DOWN):f}"
    if "." in s:
        w,f = s.split(".",1); f = f[:_amount_decimals(m)].rstrip("0"); return w if not f else f"{w}.{f}"
    return s

def get_balance(sym):
    bals = bv_request("GET","/balance")
    if isinstance(bals,list):
        for b in bals:
            if b.get("symbol")==sym.upper(): return float(b.get("available",0) or 0.0)
    return 0.0

def get_best_bid_ask(m):
    ob = requests.get(f"{BASE_URL}/{m}/book?depth=1", timeout=8).json()
    return float(ob["bids"][0][0]), float(ob["asks"][0][0])

# ==== Cancel (kept body/operatorId) ====
def cancel_order_blocking(market, orderId, clientOrderId=None, wait_sec=CANCEL_WAIT_SEC):
    deadline = time.time()+max(wait_sec,12.0)
    def _poll():
        st = bv_request("GET", f"/order?market={market}&orderId={orderId}")
        s = (st or {}).get("status","").lower() if isinstance(st,dict) else ""; return s, (st if isinstance(st,dict) else {})
    def _gone():
        lst = bv_request("GET", f"/orders?market={market}")
        return (isinstance(lst,list) and not any(o.get("orderId")==orderId for o in lst))
    body = {"orderId":orderId,"market":market,"operatorId":""}
    ts = str(int(time.time()*1000)); bstr = json.dumps(body,separators=(',',':'))
    headers = {"Bitvavo-Access-Key":API_KEY,"Bitvavo-Access-Timestamp":ts,
               "Bitvavo-Access-Signature":_sign(ts,"DELETE","/v2/order",bstr),
               "Bitvavo-Access-Window":"10000","Content-Type":"application/json"}
    r = requests.request("DELETE", f"{BASE_URL}/order", headers=headers, json=body, timeout=10)
    try: data = r.json()
    except: data = {"raw": r.text}
    print("DELETE(json+operatorId)", r.status_code, data)
    last_s, last_st = "unknown", {}
    while time.time() < deadline:
        s, st = _poll(); last_s, last_st = s, st
        if s in ("canceled","filled"): return True,s,st
        if _gone(): s2, st2 = _poll(); return True,(s2 or s or "unknown"),(st2 or st or {"note":"gone_after_delete"})
        time.sleep(0.5)
    end2 = time.time()+6.0
    while time.time() < end2:
        _ = bv_request("DELETE", f"/orders?market={market}")
        time.sleep(0.6); s, st = _poll(); last_s, last_st = s, st
        if s in ("canceled","filled") or _gone():
            s2, st2 = _poll(); fin = s2 or s or ("canceled" if _gone() else "unknown")
            return True, fin, (st2 or st or {"note":"gone_after_delete"})
    return False, last_s or "new", last_st

# ==== place maker (kept logic) ====
AMOUNT_DIGITS_RE = re.compile(r"with\s+(\d+)\s+decimal digits", re.IGNORECASE)
def _override_amount_decimals(m,decs):
    load_markets_once(); meta = MARKET_META.get(m,{}) or {}; meta["amountDecimals"]=int(decs)
    try:
        mb = float(meta.get("minBase",0))
        meta["step"] = 1.0 if (mb>=1.0 and abs(mb-round(mb))<1e-9) else float(Decimal(1)/(Decimal(10)**int(decs)))
    except: meta["step"] = float(Decimal(1)/(Decimal(10)**int(decs)))
    MARKET_META[m]=meta
def _round_to_decimals(v,decs): return float((Decimal(str(v))).quantize(Decimal(10)**-int(decs), rounding=ROUND_DOWN))
def _price_tick(m): return Decimal(1)/(Decimal(10)**_price_decimals(m))
def maker_buy_price_now(m): bid,_ = get_best_bid_ask(m); return float(fmt_price_dec(m, Decimal(str(bid))))
def maker_sell_price_now(m): _,ask = get_best_bid_ask(m); return float(fmt_price_dec(m, Decimal(str(ask))+_price_tick(m)))
def place_limit_postonly(market, side, price, amount):
    def _send(p,a):
        body = {"market":market, "side":side, "orderType":"limit", "postOnly":True,
                "clientOrderId":str(uuid4()), "price":fmt_price_dec(market,p),
                "amount":fmt_amount(market,a), "operatorId":""}
        ts=str(int(time.time()*1000))
        headers={"Bitvavo-Access-Key":API_KEY,"Bitvavo-Access-Timestamp":ts,
                 "Bitvavo-Access-Signature":_sign(ts,"POST","/v2/order",json.dumps(body,separators=(',',':'))),
                 "Bitvavo-Access-Window":"10000","Content-Type":"application/json"}
        r=requests.post(f"{BASE_URL}/order",headers=headers,json=body,timeout=10)
        try: data=r.json()
        except: data={"error":r.text}
        return body, data
    body, resp = _send(price, amount); err = (resp or {}).get("error","")
    if isinstance(err,str) and "too many decimal digits" in err.lower():
        m = AMOUNT_DIGITS_RE.search(err)
        if m:
            decs=int(m.group(1)); _override_amount_decimals(market,decs)
            adj=_round_to_decimals(float(amount),decs); adj=round_amount_down(market,adj)
            return _send(price, adj)
    if isinstance(err,str) and ("postonly" in err.lower() or "taker" in err.lower()):
        tick=float(_price_tick(market)); adj = float(price)-tick if side=="buy" else float(price)+tick
        return _send(adj, amount)
    return body, resp

# ==== reconcile + TP/SL ====
def _avg_from_order_fills(o):
    try:
        fa=float(o.get("filledAmount",0) or 0); fq=float(o.get("filledAmountQuote",0) or 0)
        if fa>0 and fq>0: return (fq/fa), fa
    except: pass
    fills=o.get("fills") or []; tq=tb=0.0
    for f in fills:
        try: a=float(f.get("amount",0) or 0); p=float(f.get("price",0) or 0); tb+=a; tq+=a*p
        except: continue
    return ((tq/tb), tb) if (tb>0 and tq>0) else (0.0,0.0)

def reconcile_recent_buys_and_place_tp(market, lookback_sec=45):
    now_ms=int(time.time()*1000)
    hist=bv_request("GET", f"/orders?market={market}")
    if isinstance(hist,list):
        rec=sorted(hist, key=lambda o:int(o.get("updated",0) or 0), reverse=True)
        for o in rec[:6]:
            try:
                ts=int(o.get("updated",0) or 0)
                if now_ms-ts>lookback_sec*1000: continue
                if (o.get("side","").lower()=="buy") and (o.get("status","").lower()=="filled"):
                    avg,base=_avg_from_order_fills(o)
                    if base>0 and avg>0:
                        r=place_smart_takeprofit_sell(market, avg, round_amount_down(market,base), TAKE_PROFIT_EUR, MAKER_FEE_RATE)
                        if r.get("ok"):
                            POSITIONS[market]={"avg":avg,"base":round_amount_down(market,base),
                                               "tp_oid":(r["response"] or {}).get("orderId"),
                                               "sl_price":avg*(1.0+(SL_PCT/100.0)),
                                               "tp_target":float(r.get("target",0))}
                        return r
            except: continue
    trades=bv_request("GET", f"/trades?market={market}&limit=50")
    if isinstance(trades,list):
        tb=tq=0.0
        for t in trades:
            try:
                if t.get("side","").lower()!="buy": continue
                ts=int(t.get("timestamp",0) or 0)
                if now_ms-ts>lookback_sec*1000: continue
                a=float(t.get("amount",0) or 0); p=float(t.get("price",0) or 0); tb+=a; tq+=a*p
            except: continue
        if tb>0 and tq>0:
            avg=tq/tb
            r=place_smart_takeprofit_sell(market, avg, round_amount_down(market,tb), TAKE_PROFIT_EUR, MAKER_FEE_RATE)
            if r.get("ok"):
                POSITIONS[market]={"avg":avg,"base":round_amount_down(market,tb),
                                   "tp_oid":(r["response"] or {}).get("orderId"),
                                   "sl_price":avg*(1.0+(SL_PCT/100.0)),"tp_target":float(r.get("target",0))}
            return r
    return {"ok":False,"err":"no_recent_buys_found"}

def _target_sell_price_for_profit(m, avg, base, profit_eur_abs=None, fee_rate=None):
    if profit_eur_abs is None: profit_eur_abs=TAKE_PROFIT_EUR
    if fee_rate is None: fee_rate=MAKER_FEE_RATE
    fee_mult=(1+fee_rate)*(1+fee_rate); base_adj=profit_eur_abs/max(base,1e-12)
    raw=avg*fee_mult+base_adj; maker_min=maker_sell_price_now(m)
    return float(fmt_price_dec(m, max(raw, maker_min)))

def place_smart_takeprofit_sell(m, avg, base, profit_eur_abs=None, fee_rate=None):
    if base<=0: return {"ok":False,"err":"no_filled_amount"}
    tgt=_target_sell_price_for_profit(m, avg, base, profit_eur_abs, fee_rate)
    body,resp=place_limit_postonly(m,"sell",tgt,round_amount_down(m,base))
    if isinstance(resp,dict) and resp.get("error"):
        return {"ok":False,"request":body,"response":resp,"err":resp.get("error")}
    return {"ok":True,"request":body,"response":resp,"target":tgt}

def emergency_taker_sell(m, amount):
    bid,_=get_best_bid_ask(m); price=max(0.0, bid*(1-0.001))
    body={"market":m,"side":"sell","orderType":"limit","postOnly":False,"clientOrderId":str(uuid4()),
          "price":fmt_price_dec(m,price),"amount":fmt_amount(m,round_amount_down(m,amount)),"operatorId":""}
    ts=str(int(time.time()*1000))
    headers={"Bitvavo-Access-Key":API_KEY,"Bitvavo-Access-Timestamp":ts,
             "Bitvavo-Access-Signature":_sign(ts,"POST","/v2/order",json.dumps(body,separators=(',',':'))),
             "Bitvavo-Access-Window":"10000","Content-Type":"application/json"}
    r=requests.post(f"{BASE_URL}/order",headers=headers,json=body,timeout=10)
    try: data=r.json()
    except: data={"error":r.text}
    return {"ok":not bool((data or {}).get("error")), "request":body, "response":data}

# ==== chase buy flow ====
def chase_maker_buy_until_filled(market, spend_eur):
    minq, minb = _min_quote(market), _min_base(market)
    deadline=time.time()+max(CHASE_WINDOW_SEC,6.0); last_oid=None; last_body=None
    try:
        while time.time()<deadline:
            price=maker_buy_price_now(market)
            amount=round_amount_down(market, spend_eur/price)
            if amount<minb: return {"ok":False,"err":f"minBase={minb}","ctx":"amount_too_small"}
            if last_oid: cancel_order_blocking(market,last_oid,None,wait_sec=3.0)
            body,resp=place_limit_postonly(market,"buy",price,amount); last_body=body
            if isinstance(resp,dict) and resp.get("error"):
                return {"ok":False,"request":body,"response":resp,"err":resp.get("error")}
            oid=resp.get("orderId"); last_oid=oid
            if oid: OPEN_ORDERS[market]={"orderId":oid,"clientOrderId":body.get("clientOrderId"),
                                          "amount_init":amount,"side":"buy"}
            t0=time.time()
            while time.time()-t0 < max(0.8, CHASE_REPRICE_PAUSE):
                st=bv_request("GET", f"/order?market={market}&orderId={oid}")
                s=(st or {}).get("status","").lower()
                if s in ("filled","partiallyfilled"):
                    fb=float((st or {}).get("filledAmount",0) or 0)
                    fq=float((st or {}).get("filledAmountQuote",0) or 0)
                    avg=(fq/fb) if (fb>0 and fq>0) else float(body["price"])
                    return {"ok":True,"status":s,"order":resp,"avg_price":avg,"filled_base":fb,"spent_eur":fq}
                time.sleep(0.2)
            time.sleep(CHASE_REPRICE_PAUSE)
        if last_oid:
            st=bv_request("GET", f"/order?market={market}&orderId={last_oid}")
            return {"ok":False,"status":(st or {}).get("status","unknown").lower(),"order":st,
                    "ctx":"deadline_reached","request":last_body,"last_oid":last_oid}
        return {"ok":False,"err":"no_order_placed"}
    finally:
        pass

def buy_open(market, eur_amount=None, tp_override=None, sl_pct_override=None):
    if market in OPEN_ORDERS: return {"ok":False,"err":"order_already_open","open":OPEN_ORDERS[market]}
    eur_avail=get_balance("EUR"); spend=max(0.0, float(eur_avail)-HEADROOM_EUR)
    if spend<=0: return {"ok":False,"err":f"No EUR to spend (avail={eur_avail:.2f})"}
    buy_res=chase_maker_buy_until_filled(market, spend)
    if not buy_res.get("ok"):
        tp_try=reconcile_recent_buys_and_place_tp(market,lookback_sec=45)
        if tp_try.get("ok"):
            br={"ok":True,"buy":"reconciled_fill","tp":tp_try,"open":OPEN_ORDERS.get(market)}
            avg=POSITIONS.get(market,{}).get("avg"); base=POSITIONS.get(market,{}).get("base")
            if avg and base:
                _sl=sl_pct_override if sl_pct_override is not None else SL_PCT
                POSITIONS[market]["sl_price"]=avg*(1.0+(_sl/100.0))
            return br
        if buy_res.get("last_oid"):
            req=buy_res.get("request") or {}
            OPEN_ORDERS[market]={"orderId":buy_res["last_oid"],"clientOrderId":req.get("clientOrderId"),
                                 "amount_init":None,"side":"buy"}
        return {"ok":False,"ctx":"buy_chase_failed",**buy_res}
    status=buy_res.get("status",""); avg=float(buy_res.get("avg_price") or 0.0)
    base=float(buy_res.get("filled_base") or 0.0)
    if status=="filled": OPEN_ORDERS.pop(market, None)
    _tp=tp_override if tp_override is not None else TAKE_PROFIT_EUR
    tp_res=place_smart_takeprofit_sell(market, avg, base, _tp, MAKER_FEE_RATE)
    if tp_res.get("ok"):
        _sl=sl_pct_override if sl_pct_override is not None else SL_PCT
        POSITIONS[market]={"avg":avg,"base":round_amount_down(market,base),
                           "tp_oid":(tp_res["response"] or {}).get("orderId"),
                           "sl_price":avg*(1.0+(_sl/100.0)),"tp_target":float(tp_res.get("target",0))}
    return {"ok":True,"buy":buy_res,"tp":tp_res,"open":OPEN_ORDERS.get(market)}

def maker_sell(market, amount=None):
    base = market.split("-")[0]
    if amount is None or amount<=0:
        bals=bv_request("GET","/balance"); amt=0.0
        if isinstance(bals,list):
            for b in bals:
                if b.get("symbol")==base.upper(): amt=float(b.get("available",0) or 0.0); break
    else: amt=float(amount)
    amt=round_amount_down(market, amt)
    if amt<=0: return {"ok":False,"err":f"No {base} to sell"}
    if amt<_min_base(market): return {"ok":False,"err":f"minBase={_min_base(market)}"}
    bid,ask=get_best_bid_ask(market); price=max(ask, bid*(1+1e-6))
    body,resp=place_limit_postonly(market,"sell",price,amt)
    if isinstance(resp,dict) and resp.get("error"):
        return {"ok":False,"request":body,"response":resp,"err":(resp or {}).get("error")}
    t0=time.time(); oid=resp.get("orderId")
    while time.time()-t0 < SHORT_FOLLOW_SEC and oid:
        st=bv_request("GET", f"/order?market={market}&orderId={oid}")
        if (st or {}).get("status","").lower() in ("filled","partiallyfilled"): break
        time.sleep(0.25)
    return {"ok":True,"request":body,"response":resp}

# ==== SL watchdog ====
def _check_tp_filled(m, oid):
    st=bv_request("GET", f"/order?market={m}&orderId={oid}")
    return isinstance(st,dict) and (st.get("status","").lower()=="filled")

def _cancel_if_open(m, oid):
    if not oid: return
    try: cancel_order_blocking(m, oid, None, wait_sec=3.0)
    except: pass

def notify_abusiyah_ready(market, reason="sold", pnl_eur=None):
    if not ABUSIYAH_READY_URL: tg_send(f"ready — {market} ({reason})"); return
    coin = market.split("-")[0]
    payload = {"coin": coin, "reason": reason, "pnl_eur": pnl_eur}
    headers = {"Content-Type":"application/json"}
    if LINK_SECRET: headers["X-Link-Secret"]=LINK_SECRET
    try:
        requests.post(ABUSIYAH_READY_URL, json=payload, headers=headers, timeout=6)
    except Exception as e:
        tg_send(f"⚠️ فشل نداء ready: {e}")

def _sl_loop():
    while True:
        try:
            for market, pos in list(POSITIONS.items()):
                avg=float(pos.get("avg",0)); base=float(pos.get("base",0))
                slp=float(pos.get("sl_price",0)); tp_oid=pos.get("tp_oid")
                if base<=0 or avg<=0 or slp<=0: continue
                if tp_oid and _check_tp_filled(market, tp_oid):
                    POSITIONS.pop(market, None); notify_abusiyah_ready(market, reason="tp_filled"); continue
                bid,_=get_best_bid_ask(market)
                if bid <= slp:
                    _cancel_if_open(market, tp_oid)
                    res=emergency_taker_sell(market, base)
                    POSITIONS.pop(market, None)
                    notify_abusiyah_ready(market, reason=("sl_triggered" if res.get("ok") else "sl_fail"))
            time.sleep(max(0.4, SL_CHECK_SEC))
        except Exception as e:
            print("SL loop err:", e); time.sleep(1.0)

threading.Thread(target=_sl_loop, daemon=True).start()

# ==== Telegram ====
COIN_RE = re.compile(r"^[A-Z0-9]{2,15}$")
def _norm_market_from_text(arg):
    s=(arg or "").strip().upper()
    if not s: return None
    if s.endswith("-EUR") and COIN_RE.match(s.split("-")[0]): return s
    if COIN_RE.match(s): return f"{s}-EUR"
    return None

@app.route("/tg", methods=["GET"])
def tg_alive(): return "OK /tg", 200

@app.route("/tg", methods=["POST"])
def tg_webhook():
    upd=request.get_json(silent=True) or {}
    msg=upd.get("message") or upd.get("edited_message") or {}
    chat=msg.get("chat") or {}; chat_id=str(chat.get("id") or "")
    text=(msg.get("text") or "").strip()
    if not chat_id or not _auth_chat(chat_id) or not text: return jsonify(ok=True)
    global TAKE_PROFIT_EUR
    low=text.lower()
    try:
        if low.startswith("اشتري"):
            tg_send("ℹ️ الشراء الآن حصراً عبر أبو صياح → POST /hook."); return jsonify(ok=True)
        if low.startswith("بيع"):
            parts=text.split()
            if len(parts)<2: tg_send("صيغة: بيع COIN [AMOUNT]"); return jsonify(ok=True)
            market=_norm_market_from_text(parts[1]); 
            if not market: tg_send("⛔ عملة غير صالحة."); return jsonify(ok=True)
            amt=None
            if len(parts)>=3:
                try: amt=float(parts[2])
                except: amt=None
            res=maker_sell(market, amt)
            tg_send(("✅ أُرسل أمر بيع" if res.get("ok") else "⚠️ فشل البيع")+f" — {market}\n{json.dumps(res,ensure_ascii=False)}")
            return jsonify(ok=True)
        if low.startswith("الغ"):
            parts=text.split()
            if len(parts)<2: tg_send("صيغة: الغ COIN"); return jsonify(ok=True)
            market=_norm_market_from_text(parts[1])
            if not market: tg_send("⛔ عملة غير صالحة."); return jsonify(ok=True)
            info=OPEN_ORDERS.get(market); ok=False; final="unknown"; last={}
            if info and info.get("orderId"):
                ok,final,last=cancel_order_blocking(market, info["orderId"], info.get("clientOrderId"), CANCEL_WAIT_SEC)
                if ok: OPEN_ORDERS.pop(market, None); tg_send(f"✅ تم الإلغاء — status={final}\n{json.dumps(last,ensure_ascii=False)}")
                else: tg_send(f"⚠️ فشل الإلغاء — status={final}\n{json.dumps(last,ensure_ascii=False)}")
            if not info or not ok:
                _=bv_request("DELETE", f"/orders?market={market}")
                time.sleep(0.7); open_after=bv_request("GET", f"/orders?market={market}")
                still=[o for o in open_after if (isinstance(open_after,list) and (o.get("status","").lower() in ("new","partiallyfilled")))] if isinstance(open_after,list) else []
                if not still: OPEN_ORDERS.pop(market, None); tg_send("✅ تم إلغاء كل أوامر السوق (fallback).")
                else: tg_send("⚠️ لم أستطع إلغاء كل الأوامر.\n"+json.dumps(still[:3],ensure_ascii=False))
            tp_try=reconcile_recent_buys_and_place_tp(market, lookback_sec=45)
            if tp_try.get("ok"): tg_send("ℹ️ رُصد تنفيذ شراء أثناء الإلغاء — تم وضع TP تلقائيًا.\n"+json.dumps(tp_try,ensure_ascii=False))
            return jsonify(ok=True)
        if low.startswith("ربح"):
            parts=text.split()
            if len(parts)<2: tg_send(f"الهدف الحالي: {TAKE_PROFIT_EUR:.4f}€\nصيغة: ربح VALUE"); return jsonify(ok=True)
            try:
                val=float(parts[1]); 
                if val<=0: raise ValueError("<=0")
                TAKE_PROFIT_EUR=val; tg_send(f"✅ تم ضبط الربح المطلق إلى: {TAKE_PROFIT_EUR:.4f}€")
            except: tg_send("⛔ قيمة غير صالحة. مثال: ربح 0.05")
            return jsonify(ok=True)
        tg_send("الأوامر: «بيع COIN [AMOUNT]» ، «الغ COIN» ، «ربح VALUE» — الشراء عبر أبو صياح /hook"); 
        return jsonify(ok=True)
    except Exception as e:
        tg_send(f"🐞 خطأ: {type(e).__name__}: {e}"); return jsonify(ok=True)

# ==== Hook from Abosiyah ====
@app.route("/hook", methods=["POST"])
def abusiyah_hook():
    try:
        if LINK_SECRET and request.headers.get("X-Link-Secret","") != LINK_SECRET:
            return jsonify(ok=False, err="bad secret"), 401
        data=request.get_json(silent=True) or {}
        action=(data.get("action") or data.get("cmd") or "").lower().strip()
        coin  =(data.get("coin") or "").upper().strip()
        tp_eur=data.get("tp_eur", None); sl_pct=data.get("sl_pct", None)
        if action!="buy" or not re.match(r"^[A-Z0-9]{2,15}$", coin):
            return jsonify(ok=False, err="invalid_payload"), 400
        market=coin_to_market(coin)
        if not market:
            tg_send(f"⛔ سوق غير مدعوم — {coin}"); return jsonify(ok=False, err="unsupported_market"), 400
        res=buy_open(market, None,
                     tp_override=(float(tp_eur) if tp_eur is not None else None),
                     sl_pct_override=(float(sl_pct) if sl_pct is not None else None))
        if res.get("ok"):
            tg_send(f"✅ اشترى + TP — {market}\n{json.dumps(res,ensure_ascii=False)}")
            return jsonify(ok=True, result=res)
        else:
            info=res.get("open") or {}
            if info.get("orderId"):
                cancel_order_blocking(market, info["orderId"], info.get("clientOrderId"), CANCEL_WAIT_SEC)
            tg_send(f"⚠️ فشل الشراء بعد مطاردة — {market}\n{json.dumps(res,ensure_ascii=False)}")
            notify_abusiyah_ready(market, reason="buy_failed")
            return jsonify(ok=False, result=res), 409
    except Exception as e:
        tg_send(f"🐞 hook error: {type(e).__name__}: {e}")
        return jsonify(ok=False, err=str(e)), 500

@app.get("/")
def home(): return "Saqer Maker — unified with Abosiyah ✅", 200

if __name__ == "__main__":
    load_markets_once()
    app.run(host="0.0.0.0", port=PORT)