# -*- coding: utf-8 -*-
"""
Saqer — Maker-Only (Bitvavo / EUR)
- شراء وبيع Maker (postOnly) مع تجميع fills.
- مراقبة ذكية للسعر + sanity checks (لا مزح مع "5€" بعد اليوم).
- محاولات حقيقية مع فواصل ثابتة + jitter، وإلغاء أوامر عند الخروج.
- حماية رصيد + backoff لخطأ 216، ودقة rounding حسب tick/step.
- صفقة واحدة في نفس الوقت. أوامر الإدارة من تيليجرام. الشراء يُسمح عبر /hook فقط.
"""

import os, re, time, json, math, traceback, random
import requests
from uuid import uuid4
from threading import Thread, Lock
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ========= Boot =========
load_dotenv()
app = Flask(__name__)

# --- مفاتيح وبيئة ---
BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHAT_ID     = os.getenv("CHAT_ID")
API_KEY     = os.getenv("BITVAVO_API_KEY")
API_SECRET  = os.getenv("BITVAVO_API_SECRET")
PORT        = int(os.getenv("PORT", "5000"))
RUN_LOCAL   = os.getenv("RUN_LOCAL", "0") == "1"

BASE_URL = "https://api.bitvavo.com/v2"

# --- حماية الرصيد / إدارة المحاولات ---
FEE_RATE_EST        = float(os.getenv("FEE_RATE_EST", "0.0025"))     # ≈0.25%
HEADROOM_EUR_MIN    = float(os.getenv("HEADROOM_EUR_MIN", "0.50"))   # هامش ثابت
MAX_SPEND_FRACTION  = float(os.getenv("MAX_SPEND_FRACTION", "0.90")) # نسبة من المتاح
FIXED_EUR_PER_TRADE = float(os.getenv("FIXED_EUR", "0"))             # 0=معطل

BUY_MIN_EUR         = 5.0     # حد بيتفافو
WAIT_BASE_SEC       = 45      # صبر مبدئي
WAIT_MAX_SEC        = 300     # سقف الصبر
WAIT_STEP_UP        = 15      # زيادة الصبر بعد فشل
WAIT_STEP_DOWN      = 10      # تقليل الصبر بعد نجاح

REPRICE_EVERY       = 2.0     # كل كم ثانية نعيد التسعير
REPRICE_THRESH      = 0.0005  # 0.05% تغيير يُعتبر كبير

TRY_BACKOFF_FACTOR  = 0.96    # عند 216 نقلّص الميزانية قليلاً
TRY_BACKOFF_MAX     = 6       # أقصى مرات backoff
TRY_MIN_SEP         = 0.80    # فاصل بين المحاولات (ثابت)
TRY_MIN_SEP_JITTER  = 0.25    # + عشوائي خفيف

AFTER_PLACE_POLL    = 0.35    # فاصل متابعة قصير بعد وضع أمر
API_TOAST_GAP       = 0.10    # راحة قصيرة بعد أخطاء API

# ========= حالة عامّة =========
enabled        = True
active_trade   = None         # {symbol, entry, amount, ...}
executed_trades= []
lk             = Lock()

# ========= إرسال تيليجرام =========
def tg(msg: str):
    try:
        if BOT_TOKEN and CHAT_ID:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": msg}, timeout=8
            )
        else:
            print("TG>", msg)
    except Exception as e:
        print("TG error:", e)

# ========= Bitvavo REST =========
def _sig(ts, method, path, body=""):
    import hmac, hashlib
    msg = f"{ts}{method}{path}{body}"
    return hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def bv_request(method: str, path: str, body: dict=None, timeout=12):
    url = f"{BASE_URL}{path}"
    ts  = str(int(time.time()*1000))
    body_str = "" if method=="GET" else json.dumps(body or {}, separators=(",",":"))
    headers = {
        "Bitvavo-Access-Key": API_KEY,
        "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Signature": _sig(ts, method, f"/v2{path}", body_str),
        "Bitvavo-Access-Window": "10000",
    }
    try:
        resp = requests.request(method, url, headers=headers,
                                json=(body if method!="GET" else None),
                                timeout=timeout)
        j = resp.json()
        if isinstance(j, dict) and j.get("error"):
            print("Bitvavo error:", j)
        return j
    except Exception as e:
        print("bv_request error:", e)
        return {"error":"request_failed"}

def get_eur_available() -> float:
    try:
        bals = bv_request("GET", "/balance")
        if isinstance(bals, list):
            for b in bals:
                if b.get("symbol")=="EUR":
                    return float(b.get("available",0) or 0)
    except: pass
    return 0.0

# ========= أسواق / دقّات =========
MARKETS = {}   # "ADA" -> "ADA-EUR"
META    = {}   # "ADA-EUR" -> {"minQuote","minBase","tick","step"}

def load_markets():
    global MARKETS, META
    try:
        rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
        mm, mt = {}, {}
        for r in rows:
            if r.get("quote")!="EUR": continue
            base = (r.get("base") or "").upper()
            market = r.get("market")
            if not base or not market: continue
            mm[base] = market
            mt[market] = {
                "minQuote": float(r.get("minOrderInQuoteAsset", 5) or 5.0),
                "minBase":  float(r.get("minOrderInBaseAsset", 0) or 0.0),
                "tick":     float(r.get("pricePrecision", 1e-6) or 1e-6),
                "step":     float(r.get("amountPrecision", 1e-8) or 1e-8),
            }
        if mm: MARKETS = mm
        if mt: META = mt
    except Exception as e:
        print("load_markets error:", e)

def coin_to_market(coin: str):
    if not MARKETS: load_markets()
    return MARKETS.get((coin or "").upper())

def _decimals(step: float) -> int:
    try:
        if step>=1: return 0
        return max(0, int(round(-math.log10(step))))
    except: return 8

def _round_price(mkt, p):
    tick = (META.get(mkt) or {}).get("tick", 1e-6)
    d = _decimals(tick)
    p = round(float(p), d)
    return max(tick, p)

def _round_amount(mkt, a):
    step = (META.get(mkt) or {}).get("step", 1e-8)
    a = math.floor(float(a)/step)*step
    d = _decimals(step)
    return max(step, round(a, d))

def fmt_price(mkt, p):
    tick = (META.get(mkt) or {}).get("tick", 1e-6)
    return f"{_round_price(mkt,p):.{_decimals(tick)}f}"

def fmt_amount(mkt, a):
    step = (META.get(mkt) or {}).get("step", 1e-8)
    return f"{_round_amount(mkt,a):.{_decimals(step)}f}"

def min_quote(mkt): return (META.get(mkt) or {}).get("minQuote", BUY_MIN_EUR)
def min_base(mkt):  return (META.get(mkt) or {}).get("minBase", 0.0)

# ========= بيانات السوق =========
def fetch_book(mkt):
    try:
        j = requests.get(f"{BASE_URL}/{mkt}/book", timeout=6).json()
        if j and j.get("bids") and j.get("asks"): return j
    except: pass
    return None

def fetch_last_price(mkt):
    try:
        j = requests.get(f"{BASE_URL}/ticker/price?market={mkt}", timeout=6).json()
        return float(j.get("price", 0) or 0)
    except: return 0.0

def pick_sane_price(mkt):
    """
    يختار سعر منطقي للـ maker buy:
    - الأساس: min(bestBid, bestAsk*(1-ε))
    - sanity: يجب أن يقع ضمن [0.98*bestBid, 1.00*bestAsk]
      وإذا الشراء بعيد عن هذا النطاق نستخدم وسطًا آمنًا.
    - إن تبيّن أن الأرقام شاذّة نfallback إلى ticker/price.
    """
    ob = fetch_book(mkt)
    if not ob: return 0.0, 0.0, 0.0, 0.0
    try:
        bid = float(ob["bids"][0][0]); ask = float(ob["asks"][0][0])
        if not (bid>0 and ask>0): raise ValueError("bad book")
        raw = min(bid, ask*(1-1e-6))
        use = raw
        lo  = bid*0.98
        hi  = ask*1.00
        if not (lo <= use <= hi):
            use = max(lo, min(hi, (bid+ask)/2.0))
        # sanity مع ticker
        last = fetch_last_price(mkt)
        if last>0:
            # إذا use انحرف كثيرًا عن last نقرّبه
            if abs(use/last - 1.0) > 0.10:
                use = max(lo, min(hi, last))
        return bid, ask, use, last
    except Exception:
        last = fetch_last_price(mkt)
        return 0.0, 0.0, last, last

# ========= أوامر Maker =========
def _place_postonly(mkt, side, price, amount):
    if not (mkt and side and price>0 and amount>0):
        return {"error":"bad_params"}
    body = {
        "market": mkt,
        "side": side,
        "orderType": "limit",
        "postOnly": True,
        "clientOrderId": str(uuid4()),
        "price": fmt_price(mkt, price),
        "amount": fmt_amount(mkt, amount),
        "operatorId": ""  # ✅ مطلوب لدى Bitvavo SDK الجديد
    }
    return bv_request("POST", "/order", body)

def _fetch_order(orderId):  return bv_request("GET",    f"/order?orderId={orderId}")
def _cancel_order(orderId): return bv_request("DELETE", f"/order?orderId={orderId}")

def _cancel_all_open(mkt):
    """إلغاء أي أمر باقٍ لهذا الماركت كإجراء أمان."""
    try:
        j = bv_request("GET", f"/ordersOpen?market={mkt}")
        if isinstance(j, list):
            for it in j:
                if it.get("orderId"): _cancel_order(it["orderId"])
    except: pass

def _fills_totals(fills):
    base=quote=fee=0.0
    for f in (fills or []):
        a=float(f["amount"]); p=float(f["price"]); fe=float(f.get("fee",0) or 0)
        base += a; quote += a*p; fee += fe
    return base, quote, fee

def _calc_base_from_eur(mkt, eur, use_price):
    eur = float(eur); use_price = max(1e-12, float(use_price))
    out = eur/use_price
    out = max(out, min_base(mkt))
    return _round_amount(mkt, out)

# ========= صبر متكيّف (بسيط داخل الذاكرة) =========
_patience = {}
def get_patience(mkt): return int(_patience.get(mkt, WAIT_BASE_SEC))
def bump_patience(mkt):
    _patience[mkt] = min(WAIT_MAX_SEC, get_patience(mkt)+WAIT_STEP_UP)
def relax_patience(mkt):
    _patience[mkt] = max(WAIT_BASE_SEC, get_patience(mkt)-WAIT_STEP_DOWN)

# ========= BUY (Maker) =========
def open_maker_buy(mkt: str, eur_amount: float=None):
    eur_av = get_eur_available()
    # أولوية المبلغ
    if FIXED_EUR_PER_TRADE>0: target = float(FIXED_EUR_PER_TRADE)
    elif eur_amount and eur_amount>0: target=float(eur_amount)
    else: target=float(eur_av)

    target = min(target, eur_av * MAX_SPEND_FRACTION)
    buffer_ = max(HEADROOM_EUR_MIN, target*FEE_RATE_EST*2.0, 0.05)
    spendable = max(0.0, eur_av - buffer_)
    need_min  = max(min_quote(mkt), BUY_MIN_EUR)

    if spendable < need_min:
        tg(f"⛔ الرصيد غير كافٍ. متاح: €{eur_av:.2f} | بعد الهامش: €{spendable:.2f} "
           f"| الهامش: €{buffer_:.2f} | المطلوب ≥ €{need_min:.2f}.")
        return None

    tg(f"💰 EUR متاح: €{eur_av:.2f} | سننفق: €{min(spendable,target):.2f} "
       f"(هامش €{buffer_:.2f} | هدف €{target:.2f})")

    patience   = get_patience(mkt)
    t0_global  = time.time()
    attempts_real = 0
    inner_calls   = 0
    last_order = None
    all_fills  = []
    remaining  = float(min(spendable, target))

    try:
        while (time.time()-t0_global) < patience and remaining >= (need_min*0.999):
            bid, ask, use_price, last = pick_sane_price(mkt)
            if not use_price or use_price<=0:
                time.sleep(0.25); continue

            # إذا يوجد أمر جارٍ: راقبه
            if last_order:
                st = _fetch_order(last_order); s = st.get("status")
                if s in ("filled","partiallyFilled"):
                    fills = st.get("fills",[]) or []
                    if fills:
                        base, qeur, fee = _fills_totals(fills)
                        remaining = max(0.0, remaining - (qeur + fee))
                        all_fills += fills
                if s=="filled" or remaining < (need_min*0.999):
                    try: _cancel_order(last_order)
                    except: pass
                    last_order=None
                    break

                # إعادة تسعير إن تحرّك السعر
                try_bid = bid
                if abs(try_bid/float(st.get("price",use_price)) - 1.0) >= REPRICE_THRESH:
                    try: _cancel_order(last_order)
                    except: pass
                    last_order=None
                else:
                    t_poll = time.time()
                    while time.time()-t_poll < REPRICE_EVERY:
                        st = _fetch_order(last_order); s = st.get("status")
                        if s in ("filled","partiallyFilled"):
                            fills = st.get("fills",[]) or []
                            if fills:
                                base, qeur, fee = _fills_totals(fills)
                                remaining = max(0.0, remaining - (qeur + fee))
                                all_fills += fills
                            if s=="filled" or remaining < (need_min*0.999):
                                try: _cancel_order(last_order)
                                except: pass
                                last_order=None
                                break
                        time.sleep(AFTER_PLACE_POLL)
                    if last_order:
                        continue

            # لا يوجد أمر: ضع أمرًا جديدًا (مع backoff مُدار)
            if not last_order and remaining >= (need_min*0.999):
                cur_budget = float(remaining)
                backoff_tries = 0
                placed = False

                while backoff_tries <= TRY_BACKOFF_MAX and cur_budget >= (need_min*0.999):
                    amt = _calc_base_from_eur(mkt, cur_budget, use_price)
                    if amt <= 0: break
                    exp_eur = amt*use_price

                    # رسالة محاولة واحدة (حقيقية) + أرقام السعر
                    attempts_real += 1
                    tg(f"🧪 محاولة شراء #{attempts_real}: amount={fmt_amount(mkt,amt)} "
                       f"| EUR≈{exp_eur:.2f} | bid={fmt_price(mkt,bid)} / ask={fmt_price(mkt,ask)} "
                       f"| use={fmt_price(mkt,use_price)}")

                    inner_calls += 1
                    res = _place_postonly(mkt, "buy", use_price, amt)
                    oid = (res or {}).get("orderId")
                    err = (res or {}).get("error","").lower()

                    if oid:
                        last_order = oid
                        # متابعة قصيرة
                        t_poll = time.time()
                        while time.time()-t_poll < REPRICE_EVERY:
                            st = _fetch_order(last_order); s = st.get("status")
                            if s in ("filled","partiallyFilled"):
                                fills = st.get("fills",[]) or []
                                if fills:
                                    base, qeur, fee = _fills_totals(fills)
                                    remaining = max(0.0, remaining - (qeur + fee))
                                    all_fills += fills
                                if s=="filled" or remaining < (need_min*0.999):
                                    try: _cancel_order(last_order)
                                    except: pass
                                    last_order=None
                                    break
                            time.sleep(AFTER_PLACE_POLL)
                        placed = True
                        break

                    # خطأ رصيد 216 → backoff
                    if "insufficient balance" in err or "not have sufficient balance" in err:
                        cur_budget *= TRY_BACKOFF_FACTOR
                        backoff_tries += 1
                        time.sleep(API_TOAST_GAP)
                        continue

                    # أي خطأ آخر: لا نعيد الكرّة فورًا
                    time.sleep(API_TOAST_GAP)
                    break

                # فاصل حقيقي بين المحاولات
                time.sleep(TRY_MIN_SEP + random.random()*TRY_MIN_SEP_JITTER)
                if not placed:
                    # سنحاول دورة أخرى ضمن الصبر
                    continue

        # بعد الحلقة: إلغاء أي أمر باقٍ
        if last_order:
            try: _cancel_order(last_order)
            except: pass

    except Exception as e:
        print("open_maker_buy error:", e)

    if not all_fills:
        bump_patience(mkt)
        elapsed = time.time()-t0_global
        tg("⚠️ لم يكتمل شراء Maker.\n"
           f"• أوامر موضوعة: {1 if last_order else 0}\n"
           f"• محاولات داخلية: {inner_calls}\n"
           f"• زمن: {elapsed:.1f}s\n"
           "سنعزّز الصبر تلقائيًا وسنحاول لاحقًا.")
        return None

    base, qeur, fee = _fills_totals(all_fills)
    if base <= 0:
        bump_patience(mkt); return None

    relax_patience(mkt)
    avg = (qeur + fee)/base
    return {"amount": base, "avg": avg, "cost_eur": qeur + fee, "fee_eur": fee}

# ========= SELL (Maker) =========
def close_maker_sell(mkt: str, amount: float):
    patience = get_patience(mkt)
    t0 = time.time()
    remaining = float(amount)
    all_fills = []
    last_order = None

    try:
        while (time.time()-t0) < patience and remaining > 0:
            ob = fetch_book(mkt)
            if not ob: time.sleep(0.25); continue
            bid = float(ob["bids"][0][0]); ask = float(ob["asks"][0][0])
            use = max(ask, bid*(1+1e-6))
            use = _round_price(mkt, use)

            if last_order:
                st = _fetch_order(last_order); s = st.get("status")
                if s in ("filled","partiallyFilled"):
                    fills = st.get("fills",[]) or []
                    if fills:
                        sold, _, _ = _fills_totals(fills)
                        remaining = max(0.0, remaining - sold); all_fills += fills
                if s=="filled" or remaining<=0:
                    try: _cancel_order(last_order)
                    except: pass
                    last_order=None
                    break

                # إعادة تسعير
                t_poll = time.time()
                while time.time()-t_poll < REPRICE_EVERY:
                    st = _fetch_order(last_order); s = st.get("status")
                    if s in ("filled","partiallyFilled"):
                        fills = st.get("fills",[]) or []
                        if fills:
                            sold, _, _ = _fills_totals(fills)
                            remaining=max(0.0, remaining-sold); all_fills+=fills
                        if s=="filled" or remaining<=0:
                            try: _cancel_order(last_order)
                            except: pass
                            last_order=None
                            break
                    time.sleep(AFTER_PLACE_POLL)
                if last_order: continue

            if remaining > 0:
                amt_to_place = _round_amount(mkt, remaining)
                res = _place_postonly(mkt, "sell", use, amt_to_place)
                oid = res.get("orderId")
                if not oid:
                    # محاولة على ask مباشرة
                    res = _place_postonly(mkt, "sell", _round_price(mkt, ask), amt_to_place)
                    oid = res.get("orderId")
                if not oid:
                    time.sleep(TRY_MIN_SEP); continue
                last_order = oid
                time.sleep(REPRICE_EVERY)

        if last_order:
            try: _cancel_order(last_order)
            except: pass

    except Exception as e:
        print("close_maker_sell error:", e)

    sold=quote=fee=0.0
    for f in all_fills:
        a=float(f["amount"]); p=float(f["price"]); fe=float(f.get("fee",0) or 0)
        sold += a; quote += a*p; fee += fe
    return sold, (quote-fee), fee

# ========= إدارة صفقات =========
def do_open_maker(mkt, eur=None):
    def runner():
        global active_trade
        try:
            with lk:
                if active_trade:
                    tg("⛔ توجد صفقة نشطة."); return
            res = open_maker_buy(mkt, eur)
            if not res:
                tg("⏳ لم يكتمل شراء Maker. سنحاول لاحقاً (الصبر يتكيّف).")
                _cancel_all_open(mkt)
                return
            with lk:
                active_trade = {
                    "symbol": mkt,
                    "entry": float(res["avg"]),
                    "amount": float(res["amount"]),
                    "cost_eur": float(res["cost_eur"]),
                    "buy_fee_eur": float(res["fee_eur"]),
                    "opened_at": time.time()
                }
                executed_trades.append(active_trade.copy())
            tg(f"✅ شراء {mkt.replace('-EUR','')} (Maker) @ €{active_trade['entry']:.8f} "
               f"| كمية {active_trade['amount']:.8f}")
        except Exception as e:
            traceback.print_exc()
            tg(f"🐞 خطأ أثناء الفتح: {e}")
    Thread(target=runner, daemon=True).start()

def do_close_maker(reason=""):
    global active_trade
    with lk:
        at = active_trade
    if not at: return
    m = at["symbol"]; amt = float(at["amount"]); cost=float(at["cost_eur"])
    sold, proceeds, fee = close_maker_sell(m, amt)
    with lk:
        pnl_eur = proceeds - cost
        pnl_pct = (proceeds/cost - 1.0)*100.0 if cost>0 else 0.0
        for t in reversed(executed_trades):
            if t["symbol"]==m and "exit_eur" not in t:
                t.update({"exit_eur": proceeds, "sell_fee_eur": fee,
                          "pnl_eur": pnl_eur, "pnl_pct": pnl_pct,
                          "exit_time": time.time()})
                break
        active_trade = None
    _cancel_all_open(m)
    tg(f"💰 بيع {m.replace('-EUR','')} | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%) {('— '+reason) if reason else ''}")

def summary_text():
    lines=[]
    with lk:
        at=active_trade
        closed=[x for x in executed_trades if "exit_eur" in x]
    if at:
        cur = fetch_last_price(at["symbol"]) or at["entry"]
        pnl = ((cur/at["entry"])-1.0)*100.0
        lines.append("📌 صفقة نشطة:")
        lines.append(f"• {at['symbol'].replace('-EUR','')} @ €{at['entry']:.8f} | PnL {pnl:+.2f}%")
    else:
        lines.append("📌 لا صفقات نشطة.")
    pnl_eur = sum(float(x["pnl_eur"]) for x in closed)
    wins = sum(1 for x in closed if float(x.get("pnl_eur",0))>=0)
    lines.append(f"\n📊 صفقات مكتملة: {len(closed)} | محققة: {pnl_eur:+.2f}€ | فوز/خسارة: {wins}/{len(closed)-wins}")
    lines.append("\n⚙️ buy=Maker | sell=Maker | sanity للسعر مفعل ✔️")
    return "\n".join(lines)

# ========= Web (أوامر) =========
@app.route("/", methods=["GET"])
def health(): return "Saqer Maker Relay ✅", 200

@app.route("/hook", methods=["POST"])
def hook():
    """
    يستقبل فقط أمر شراء من بوت الإشارات:
    { "cmd":"buy", "coin":"DATA", "eur": 12.5 }  # eur اختياري
    باقي الأوامر من تيليجرام أدناه.
    """
    try:
        data = request.get_json(silent=True) or {}
        if (data.get("cmd") or "").lower() != "buy":
            return jsonify(ok=False, err="only_buy_allowed_here"), 400
        coin = (data.get("coin") or "").upper()
        mkt  = coin_to_market(coin)
        if not mkt: return jsonify(ok=False, err="market_not_found"), 400
        eur = float(data.get("eur")) if data.get("eur") is not None else None
        do_open_maker(mkt, eur)
        return jsonify(ok=True, started=True, market=mkt)
    except Exception as e:
        traceback.print_exc()
        return jsonify(ok=False, err=str(e)), 500

@app.route("/tg", methods=["POST"])
def tg_cmd():
    """
    أوامر تيليجرام (أرسل webhook تيليجرام هنا):
    نصوص مدعومة: /enable /disable /summary /close
    """
    try:
        upd = request.get_json(silent=True) or {}
        msg = (upd.get("message") or upd.get("edited_message") or {})
        chat_id = str(msg.get("chat",{}).get("id", CHAT_ID))
        text = (msg.get("text") or "").strip()
        def say(t): 
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          data={"chat_id": chat_id, "text": t}, timeout=8)

        if text.startswith("/enable"):
            global enabled; enabled=True; say("✅ تم التفعيل."); return jsonify(ok=True)
        if text.startswith("/disable"):
            enabled=False; say("🛑 تم الإيقاف."); return jsonify(ok=True)
        if text.startswith("/summary"):
            say(summary_text()); return jsonify(ok=True)
        if text.startswith("/close"):
            do_close_maker("Manual"); say("⏳ closing..."); return jsonify(ok=True)
        say("أوامر: /summary /close /enable /disable")
        return jsonify(ok=True)
    except Exception as e:
        traceback.print_exc()
        return jsonify(ok=False, err=str(e)), 200

# ========= Main =========
if __name__ == "__main__" or RUN_LOCAL:
    load_markets()
    app.run(host="0.0.0.0", port=PORT)