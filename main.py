# -*- coding: utf-8 -*-
import hmac
import hashlib
import os
import time
import requests
import json
import redis
from flask import Flask, request
from threading import Thread, Lock
from uuid import uuid4
from dotenv import load_dotenv
from collections import deque

# =========================
# 📌 إعدادات يدوية (المستخدمة فقط)
# =========================
MAX_TRADES = 2

LATE_FALLBACK_SEC   = 15 * 60   # بعد 15 دقيقة نفعل قفل ربح ذكي
LATE_LOCK_BACKSTEP  = 0.6       # SL = القمة - 0.6%
LATE_MIN_LOCK       = 0.5       # أقل SL بعد 15د
LATE_WEAK_R         = 0.15      # r30/r90 ضعيف ≤ 0.15%

DYN_SL_START        = -2.0      # SL ابتدائي
DYN_SL_STEP         = 1.0       # يعلو 1% لكل 1% ربح

MOM_LOOKBACK_SEC    = 120       # نافذة الزخم (ثواني)
STALL_SEC           = 90        # توقّف تقدم
DROP_FROM_PEAK_EXIT = 0.8       # هبوط عن القمة (٪) + زخم سلبي

MOM_R30_STRONG      = 0.50
MOM_R90_STRONG      = 0.80

SELL_RETRY_DELAY = 5
SELL_MAX_RETRIES = 6

EARLY_WINDOW_SEC = 15 * 60

BLACKLIST_EXPIRE_SECONDS = 300  # 5 دقائق
BUY_COOLDOWN_SEC = 600          # 10 دقائق

# ====== إعدادات الحارس (شراء) + الخروج الذكي ======
# شراء — microstructure guard
GUARD_TRADES_LIMIT   = 170
GUARD_TBR_FAST       = 0.68     # taker buy ratio fast-lane
GUARD_CVD_FAST       = 0.25
GUARD_IMB_FAST       = 1.8
GUARD_SPREAD_FAST    = 12.0     # bps

GUARD_TBR_MIN        = 0.60
GUARD_CVD_MIN        = 0.20
GUARD_IMB_MIN        = 1.40
GUARD_SPREAD_MAX     = 20.0     # bps
GUARD_SCORE_NEED     = 4

# خروج — giveback + trail bands + قتل سريع
GIVEBACK_PCT   = 0.25   # لا نسترجع أكثر من 25% من القمّة
GIVEBACK_MIN   = 3.0    # %
GIVEBACK_MAX   = 7.0    # %

TRAIL_BANDS = [
    (3.0,   6.0,   1.0,  2.0),   # (من%, إلى%, backstep%, min_lock%)
    (6.0,  10.0,   1.6,  4.0),
    (10.0, 15.0,   2.2,  6.0),
    (15.0, 25.0,   3.0, 10.0),
    (25.0, 999.0,  4.0, 15.0),
]

FAST_DROP_R30 = 1.2     # r30 ≤ -1.2% → خروج
FAST_DROP_R90 = 3.0     # r90 ≤ -3.0% → خروج

# =========================
# 🧠 التهيئة العامة
# =========================
load_dotenv()
app = Flask(__name__)
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BITVAVO_API_KEY = os.getenv("BITVAVO_API_KEY")
BITVAVO_API_SECRET = os.getenv("BITVAVO_API_SECRET")
r = redis.from_url(os.getenv("REDIS_URL"))
lock = Lock()
enabled = True
active_trades = []     # صفقات مفتوحة
executed_trades = []   # نسخ عند الشراء + تُستكمل عند الإغلاق

SINCE_RESET_KEY = "nems:since_reset"

# =========================
# 🔔 تلغرام (مع منع تكرار)
# =========================
def send_message(text: str):
    try:
        key = "dedup:" + hashlib.sha1(text.encode("utf-8")).hexdigest()
        if r.setnx(key, 1):
            r.expire(key, 60)
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": text},
                timeout=8
            )
    except Exception as e:
        print("Telegram error:", e)

# =========================
# 🔐 Bitvavo: توقيع + طلب
# =========================
def create_signature(timestamp, method, path, body_str=""):
    msg = f"{timestamp}{method}{path}{body_str}"
    return hmac.new(BITVAVO_API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def bitvavo_request(method: str, path: str, body=None):
    timestamp = str(int(time.time() * 1000))
    url = f"https://api.bitvavo.com/v2{path}"
    body_str = "" if method == "GET" else json.dumps(body or {}, separators=(',', ':'))
    signature = create_signature(timestamp, method, f"/v2{path}", body_str)
    headers = {
        'Bitvavo-Access-Key': BITVAVO_API_KEY,
        'Bitvavo-Access-Timestamp': timestamp,
        'Bitvavo-Access-Signature': signature,
        'Bitvavo-Access-Window': '10000'
    }
    try:
        resp = requests.request(
            method,
            url,
            headers=headers,
            json=(body or {}) if method != "GET" else None,
            timeout=12 if method != "GET" else 8
        )
        return resp.json()
    except Exception as e:
        print("bitvavo_request error:", e)
        return {"error": "request_failed"}

def get_eur_available() -> float:
    try:
        balances = bitvavo_request("GET", "/balance")
        for b in balances:
            if b.get("symbol") == "EUR":
                return max(0.0, float(b.get("available", 0) or 0))
    except Exception:
        pass
    return 0.0

# =========================
# 💶 الأسعار (كاش خفيف 2ث)
# =========================
_price_cache = {"t": 0, "map": {}}
def fetch_price(market_symbol: str):
    now = time.time()
    if now - _price_cache["t"] > 2:
        _price_cache["map"].clear()
        _price_cache["t"] = now
    if market_symbol in _price_cache["map"]:
        return _price_cache["map"][market_symbol]
    try:
        res = bitvavo_request("GET", f"/ticker/price?market={market_symbol}")
        price = float(res.get("price", 0) or 0)
        if price > 0:
            _price_cache["map"][market_symbol] = price
            return price
    except Exception:
        pass
    return None

# =========================
# ✅ رموز مدعومة (تحديث كل 10د)
# =========================
SUPPORTED_SYMBOLS = set()
_last_sym_refresh = 0

def ensure_symbols_fresh():
    global SUPPORTED_SYMBOLS, _last_sym_refresh
    now = time.time()
    if SUPPORTED_SYMBOLS and (now - _last_sym_refresh) < 600:
        return
    try:
        res = requests.get("https://api.bitvavo.com/v2/markets", timeout=8)
        data = res.json()
        SUPPORTED_SYMBOLS = set(
            m["market"].replace("-EUR", "").upper()
            for m in data if m.get("market", "").endswith("-EUR")
        )
        _last_sym_refresh = now
    except Exception as e:
        print("refresh symbols error:", e)

# =========================
# 🧮 تجميع fills باليورو
# =========================
def totals_from_fills_eur(fills):
    total_base = 0.0
    total_eur  = 0.0
    fee_eur    = 0.0
    for f in (fills or []):
        amt   = float(f["amount"])
        price = float(f["price"])
        fee   = float(f.get("fee", 0) or 0)
        total_base += amt
        total_eur  += amt * price
        fee_eur    += fee  # feeCurrency = EUR
    return total_base, total_eur, fee_eur

# =========================
# 🧱 أدوات البيع الذكي
# =========================
def _available_amount_for(market: str) -> float:
    """يرجع كامل الكمية المتاحة في المحفظة لرمز الـ base (بيع كامل الرصيد)."""
    base = market.split('-')[0]
    try:
        balances = bitvavo_request("GET", "/balance") or []
        for b in balances:
            if (b.get("symbol") or "").upper() == base.upper():
                amt = float(b.get("available", 0.0)) + float(b.get("inOrder", 0.0))
                return max(0.0, amt)
    except Exception:
        pass
    return 0.0

def place_market_sell(market, amt):
    body = {
        "market": market,
        "side": "sell",
        "orderType": "market",
        "amount": f"{amt:.10f}",
        "clientOrderId": str(uuid4()),
        "operatorId": ""
    }
    return bitvavo_request("POST", "/order", body)

# =========================
# 💱 البيع (مع إعادة المحاولة والبيع الكامل)
# =========================
def sell_trade(trade: dict):
    market = trade["symbol"]

    if r.exists(f"blacklist:sell:{market}"):
        return

    # بيع كامل الكمية الموجودة بالمحفظة (وليس فقط المسجلة)
    amt_wallet = _available_amount_for(market)
    amt_trade  = float(trade.get("amount", 0) or 0)
    amt = max(amt_wallet, amt_trade)

    if amt <= 0:
        return

    ok = False
    resp = None
    for _ in range(SELL_MAX_RETRIES):
        resp = place_market_sell(market, amt)
        if isinstance(resp, dict) and resp.get("status") == "filled":
            ok = True
            break
        time.sleep(SELL_RETRY_DELAY)

    if not ok:
        r.setex(f"blacklist:sell:{market}", BLACKLIST_EXPIRE_SECONDS, 1)
        send_message(f"❌ فشل بيع {market} بعد محاولات متعددة.")
        return

    fills = resp.get("fills", [])
    print("📦 SELL FILLS:", json.dumps(fills, ensure_ascii=False))

    tb, tq_eur, fee_eur = totals_from_fills_eur(fills)
    proceeds_eur = tq_eur - fee_eur
    sold_amount  = tb

    orig_amt  = float(trade["amount"])
    orig_cost = float(trade.get("cost_eur", trade["entry"] * trade["amount"]))

    # بيع جزئي (أو إذا كانت محفظتك تحتوي أكثر من المسجّل)
    if sold_amount < orig_amt - 1e-10:
        ratio = sold_amount / orig_amt if orig_amt > 0 else 1.0
        attributed_cost = orig_cost * ratio
        pnl_eur = proceeds_eur - attributed_cost
        pnl_pct = (proceeds_eur / attributed_cost - 1.0) * 100.0

        remaining_amt  = orig_amt - sold_amount
        remaining_cost = orig_cost - attributed_cost
        with lock:
            trade["amount"]   = remaining_amt
            trade["cost_eur"] = remaining_cost

        send_message(f"💰 بيع جزئي {market} | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%)")

        closed = trade.copy()
        closed.update({
            "exit_eur": proceeds_eur,
            "sell_fee_eur": fee_eur,
            "pnl_eur": pnl_eur,
            "pnl_pct": pnl_pct,
            "exit_time": time.time(),
            "amount": sold_amount,
            "cost_eur": attributed_cost
        })
        with lock:
            executed_trades.append(closed)
            r.delete("nems:executed_trades")
            for t in executed_trades:
                r.rpush("nems:executed_trades", json.dumps(t))
            r.set("nems:active_trades", json.dumps(active_trades))
        return

    # بيع كامل
    pnl_eur = proceeds_eur - orig_cost
    pnl_pct = (proceeds_eur / orig_cost - 1.0) * 100.0
    send_message(f"💰 بيع {market} | {pnl_eur:+.2f}€ ({pnl_pct:+.2f}%)")

    # حظر 24سا عند خسارة كبيرة
    try:
        if pnl_pct <= -3.0:
            base = market.replace("-EUR", "")
            r.setex(f"ban24:{base}", 24*3600, 1)
            send_message(f"🧊 تم حظر {base} لمدة 24 ساعة (خسارة {pnl_pct:.2f}%).")
    except Exception:
        pass

    with lock:
        try:
            active_trades.remove(trade)
        except ValueError:
            pass
        r.set("nems:active_trades", json.dumps(active_trades))

        # استكمال سجل الصفقة
        for i in range(len(executed_trades)-1, -1, -1):
            if executed_trades[i]["symbol"] == market and "exit_eur" not in executed_trades[i]:
                executed_trades[i].update({
                    "exit_eur": proceeds_eur,
                    "sell_fee_eur": fee_eur,
                    "pnl_eur": pnl_eur,
                    "pnl_pct": pnl_pct,
                    "exit_time": time.time()
                })
                break
        r.delete("nems:executed_trades")
        for t in executed_trades:
            r.rpush("nems:executed_trades", json.dumps(t))

    base = market.replace("-EUR", "")
    r.setex(f"cooldown:{base}", BUY_COOLDOWN_SEC, 1)

# =========================
# 🛡️ حارس الشراء قبل التنفيذ
# =========================
def fetch_orderbook(market: str, depth: int = 1):
    try:
        return bitvavo_request("GET", f"/orderbook?market={market}&depth={depth}")
    except Exception:
        return None

def momentum_guard(market: str, r30=None, r90=None):
    """
    يتحقق بسرعة من صحة الزخم عبر /trades و /orderbook.
    يرجع (ok, why, feats)
    """
    try:
        # ---- Recent trades (آخر ~60s) ----
        trades = bitvavo_request("GET", f"/trades?market={market}&limit={GUARD_TRADES_LIMIT}")
        now_ms = int(time.time()*1000); t60 = now_ms - 60_000
        buy_vol = sell_vol = signed_vol = 0.0
        first_px = last_px = None
        for t in (trades or []):
            ts = int(t.get("timestamp", 0))
            if ts < t60: 
                continue
            px = float(t["price"]); amt = float(t["amount"])
            side = (t.get("side") or "").lower()
            if first_px is None: 
                first_px = px
            last_px = px
            v_eur = px * amt
            if side == "buy":
                buy_vol  += v_eur; signed_vol += v_eur
            else:
                sell_vol += v_eur; signed_vol -= v_eur

        total = buy_vol + sell_vol
        if total <= 0:
            # 🛡 fallback: orderbook check قبل ما نرفض
            book1 = fetch_orderbook(market, depth=5) or {}
            bids1, asks1 = book1.get("bids") or [], book1.get("asks") or []
            if bids1 and asks1:
                bid1 = float(bids1[0][0]); ask1 = float(asks1[0][0]); mid = 0.5*(bid1+ask1)
                spread_bps = (ask1 - bid1)/mid * 10000.0

                bid_notional = sum(float(p)*float(q) for p,q,*_ in bids1[:5])
                ask_notional = sum(float(p)*float(q) for p,q,*_ in asks1[:5])
                imb = bid_notional / max(ask_notional, 1e-9)

                # لقطة ثانية لمنع spoofing
                time.sleep(0.20)
                book2 = fetch_orderbook(market, depth=5) or {}
                bids2, asks2 = book2.get("bids") or [], book2.get("asks") or []
                stable = False
                if bids2 and asks2:
                    bid_notional2 = sum(float(p)*float(q) for p,q,*_ in bids2[:5])
                    if bid_notional > 0 and abs(bid_notional2 - bid_notional)/bid_notional <= 0.15:
                        stable = True

                MIN_BID_EUR   = 600.0   # حد أدنى للسيولة
                REQ_IMB       = 1.2      # تفوق واضح للـ bids
                MAX_SPREAD_BP = 10.0     # سبريد مقبول

                if (spread_bps <= MAX_SPREAD_BP and imb >= REQ_IMB and
                    bid_notional >= MIN_BID_EUR and stable):
                    return True, "book-only-pass", {
                        "imb": round(imb,2),
                        "spr": round(spread_bps,1),
                        "bid€": int(bid_notional)
                    }

            return False, "noRecentVol", {}

        # ---- لو في تداول حديث نكمل ----
        tbr = buy_vol / total
        cvd = (buy_vol - sell_vol) / total
        dpct_60 = (last_px/first_px - 1.0)*100.0 if first_px else 0.0
        lam = abs(dpct_60) / max(abs(signed_vol), 1e-9)

        # ---- orderbook ----
        book = fetch_orderbook(market, depth=5) or {}
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return False, "noBook", {}

        bid1 = float(bids[0][0]); ask1 = float(asks[0][0])
        mid = 0.5*(bid1+ask1)
        spread_bps = (ask1 - bid1)/mid * 10000.0
        sum_bids = sum(float(b[1]) * float(b[0]) for b in bids[:5])
        sum_asks = sum(float(a[1]) * float(a[0]) for a in asks[:5])
        imb = sum_bids / max(sum_asks, 1e-9)

        accel = None
        if (r30 is not None) and (r90 is not None):
            accel = r30 - 0.5*r90

        # Fast-lane
        if (tbr >= GUARD_TBR_FAST and cvd >= GUARD_CVD_FAST 
            and imb >= GUARD_IMB_FAST and spread_bps <= GUARD_SPREAD_FAST):
            return True, "fastlane", {
                "tbr":round(tbr,3),"cvd":round(cvd,3),
                "imb":round(imb,2),"spr":round(spread_bps,1)
            }

        # Score
        score = 0
        score += 1 if tbr >= GUARD_TBR_MIN else 0
        score += 1 if cvd >= GUARD_CVD_MIN else 0
        score += 1 if imb >= GUARD_IMB_MIN else 0
        score += 1 if spread_bps <= GUARD_SPREAD_MAX else 0
        if accel is not None and accel >= 0.15: 
            score += 1
        score += 1 if lam <= 1e-4 else 0  # مضخات هشة

        if spread_bps > 25 or (sell_vol > buy_vol and tbr < 0.45):
            return False, "killswitch", {
                "tbr":round(tbr,3), "spr":round(spread_bps,1)
            }

        return (score >= GUARD_SCORE_NEED), f"score={score}", {
            "tbr":round(tbr,3),"cvd":round(cvd,3),
            "imb":round(imb,2),"spr":round(spread_bps,1),
            "accel": None if accel is None else round(accel,2)
        }
    except Exception as e:
        return False, f"err:{e}", {}

# =========================
# 🛒 الشراء (1/2 الرصيد ثم الباقي) + الحارس
# =========================
def buy(symbol: str):
    ensure_symbols_fresh()
    symbol = symbol.upper().strip()
    if symbol not in SUPPORTED_SYMBOLS:
        send_message(f"❌ العملة {symbol} غير مدعومة على Bitvavo.")
        return

    if r.exists(f"ban24:{symbol}"):
        send_message(f"🧊 {symbol} محظورة 24 ساعة بسبب خسارة سابقة. تجاهلت الإشارة.")
        return

    if r.exists(f"cooldown:{symbol}"):
        send_message(f"⏳ {symbol} تحت فترة تهدئة مؤقتة.")
        return

    market = f"{symbol}-EUR"

    with lock:
        if any(t["symbol"] == market for t in active_trades):
            send_message(f"⛔ عندك صفقة مفتوحة على {symbol}.")
            return
        if len(active_trades) >= MAX_TRADES:
            send_message("🚫 وصلنا الحد الأقصى (صفقتان). لا يوجد استبدال بعد الآن.")
            return

    if r.exists(f"blacklist:buy:{symbol}"):
        return

    # 🛡️ تأكيد زخم سريع قبل الشراء
    ok, why, feats = momentum_guard(market)
    if not ok:
        send_message(f"⏳ تجاهل الشراء {market} ({why}) | {feats}")
        return

    eur_avail = get_eur_available()
    if eur_avail <= 0:
        send_message("💤 لا يوجد رصيد EUR متاح للشراء.")
        return

    if len(active_trades) == 0:
        amount_quote = eur_avail / 2.0
        tranche = "النصف (50%)"
    else:
        amount_quote = eur_avail
        tranche = "المبلغ المتبقي"

    amount_quote = round(amount_quote, 2)
    if amount_quote < 5.0:
        send_message(f"⚠️ المبلغ المتاح صغير (€{amount_quote:.2f}). لن أنفذ الشراء.")
        return

    body = {
        "market": market,
        "side": "buy",
        "orderType": "market",
        "amountQuote": f"{amount_quote:.2f}",
        "clientOrderId": str(uuid4()),
        "operatorId": ""
    }
    res = bitvavo_request("POST", "/order", body)

    if not (isinstance(res, dict) and res.get("status") == "filled"):
        r.setex(f"blacklist:buy:{symbol}", BLACKLIST_EXPIRE_SECONDS, 1)
        send_message(f"❌ فشل شراء {symbol}")
        return

    fills = res.get("fills", [])
    print("📦 BUY FILLS:", json.dumps(fills, ensure_ascii=False))

    tb, tq_eur, fee_eur = totals_from_fills_eur(fills)
    amount_net = tb
    cost_eur   = tq_eur + fee_eur  # تكلفة صافية €

    if amount_net <= 0 or cost_eur <= 0:
        send_message(f"❌ فشل شراء {symbol} - بيانات fills غير صالحة")
        r.setex(f"blacklist:buy:{symbol}", BLACKLIST_EXPIRE_SECONDS, 1)
        return

    avg_price_incl_fees = cost_eur / amount_net

    trade = {
        "symbol": market,
        "entry": avg_price_incl_fees,
        "amount": amount_net,
        "cost_eur": cost_eur,
        "buy_fee_eur": fee_eur,
        "opened_at": time.time(),
        "phase": "EARLY",
        "peak_pct": 0.0,
        "exit_in_progress": False,
        "last_profit": 0.0,
        "last_exit_try": 0.0,
        # إضافات للخروج الذكي
        "sl_price": None,        # غير مستخدم مباشرة هنا (نحتفظ به لو احتجناه لاحقًا)
        "sl_dyn": DYN_SL_START
    }

    with lock:
        active_trades.append(trade)
        executed_trades.append(trade.copy())
        r.set("nems:active_trades", json.dumps(active_trades))
        r.rpush("nems:executed_trades", json.dumps(trade))

    slot_idx = len(active_trades)
    send_message(f"✅ شراء {symbol} | صفقة #{slot_idx}/2 | {tranche} | قيمة: €{amount_quote:.2f} | كمية: {amount_net:.5f} | {feats} | {why}")

# =========================
# 📈 تاريخ لحظي + مؤشرات زخم
# =========================
def _init_hist(trade):
    if "hist" not in trade:
        trade["hist"] = deque(maxlen=600)  # ~ دقيقتين
    if "last_new_high" not in trade:
        trade["last_new_high"] = trade.get("opened_at", time.time())

def _update_hist(trade, now_ts, price):
    _init_hist(trade)
    trade["hist"].append((now_ts, price))
    cutoff = now_ts - MOM_LOOKBACK_SEC
    while trade["hist"] and trade["hist"][0][0] < cutoff:
        trade["hist"].popleft()

def _mom_metrics(trade, price_now):
    _init_hist(trade)
    if not trade["hist"]:
        return 0.0, 0.0, False

    now_ts = trade["hist"][-1][0]
    p30 = p90 = None
    hi = lo = price_now
    for ts, p in trade["hist"]:
        hi = max(hi, p); lo = min(lo, p)
        age = now_ts - ts
        if p30 is None and age >= 30:
            p30 = p
        if p90 is None and age >= 90:
            p90 = p
    if p30 is None: p30 = trade["hist"][0][1]
    if p90 is None: p90 = trade["hist"][0][1]

    r30 = (price_now / p30 - 1.0) * 100.0 if p30 > 0 else 0.0
    r90 = (price_now / p90 - 1.0) * 100.0 if p90 > 0 else 0.0
    new_high = price_now >= hi * 0.999
    if new_high:
        trade["last_new_high"] = now_ts
    return r30, r90, new_high

# =========================
# 🔧 مساعدات الخروج الذكي
# =========================
def _band_params(pft):
    for lo, hi, step, lock_min in TRAIL_BANDS:
        if lo <= pft < hi:
            return step, lock_min
    return 4.0, 15.0

def manage_exit_quick(trade, pnl_pct, r30=None, r90=None):
    """
    يحدّد SL ذكي كنسبة ربح ويقوم بقتل سريع عند انهيار.
    يرجع ('sell_all', reason) أو (None, None)
    """
    peak = float(trade.get("peak_pct", 0.0))
    if peak < 3.0:
        return (None, None)

    # سقف الاسترجاع من القمّة
    giveback = max(GIVEBACK_MIN, min(GIVEBACK_MAX, GIVEBACK_PCT * peak))

    # تريلينغ متدرّج + قفل أدنى + سقف الاسترجاع
    step_pct, min_lock = _band_params(peak)
    lock_by_step = peak - step_pct           # (% من الربح)
    lock_by_cap  = peak - giveback
    desired_lock = max(min_lock, lock_by_step, lock_by_cap)

    # Ratchet: لا نخفض SL
    if desired_lock > trade.get("sl_dyn", DYN_SL_START):
        trade["sl_dyn"] = desired_lock

    # قتل سريع من القمة
    drop_from_peak = peak - pnl_pct
    if drop_from_peak >= giveback:
        return ("sell_all", f"fast-fall {drop_from_peak:.2f}%≥{giveback:.2f}%")

    # زخم سلبي قوي
    if (r30 is not None and r30 <= -FAST_DROP_R30) or (r90 is not None and r90 <= -FAST_DROP_R90):
        return ("sell_all", f"momentum-fail r30={r30:.2f} r90={r90:.2f}")

    # ضرب SL الذكي
    if pnl_pct <= trade["sl_dyn"]:
        return ("sell_all", f"hit-SL {trade['sl_dyn']:.2f}%")

    return (None, None)

# =========================
# 👀 حلقة المراقبة (خروج ذكي)
# =========================
def monitor_loop():
    while True:
        try:
            with lock:
                snapshot = list(active_trades)

            now = time.time()
            for trade in snapshot:
                market = trade["symbol"]
                entry  = float(trade["entry"])
                current = fetch_price(market)
                if not current:
                    continue

                _update_hist(trade, now, current)
                r30, r90, _ = _mom_metrics(trade, current)

                pnl_pct = ((current - entry) / entry) * 100.0
                trade["peak_pct"] = max(trade.get("peak_pct", 0.0), pnl_pct)

                # ستوب سلّمي أساسي (مرحلة مبكرة): -2% + 1% لكل +1% ربح
                inc = int(max(0.0, pnl_pct) // 1)
                dyn_sl = DYN_SL_START + inc * DYN_SL_STEP
                trade["sl_dyn"] = max(trade.get("sl_dyn", DYN_SL_START), dyn_sl)

                age = now - trade.get("opened_at", now)

                # ====== خروج سريع ذكي قبل أي شرط آخر (يعمل في كل المراحل) ======
                action, why = manage_exit_quick(trade, pnl_pct, r30=r30, r90=r90)
                if action == "sell_all":
                    trade["exit_in_progress"] = True; trade["last_exit_try"] = now
                    send_message(f"🔔 خروج {market} ({why})")
                    sell_trade(trade); trade["exit_in_progress"] = False
                    continue

                # ====== منطقك السابق (للتأمين) ======

                # 🟢 المرحلة 1: أول 15 دقيقة — فقط SL السلّمي
                if age < EARLY_WINDOW_SEC:
                    if pnl_pct <= trade["sl_dyn"]:
                        trade["exit_in_progress"] = True; trade["last_exit_try"] = now
                        send_message(f"🔔 خروج {market} (نافذة مبكرة: SL سلّمي {trade['sl_dyn']:.2f}% | الآن {pnl_pct:.2f}%)")
                        sell_trade(trade); trade["exit_in_progress"] = False
                    continue  # لا نفحص شروط أخرى هنا

                # 🔵 المرحلة 2: بعد 15 دقيقة — قفل ربح تقليديك + تحسينات
                if trade.get("exit_in_progress") and (now - trade.get("last_exit_try", 0)) < 15:
                    continue

                # قفل ربح تدريجي السابق (يبقى لكن صار SL يُدار من manage_exit_quick)
                peak = trade.get("peak_pct", 0.0)
                lock_from_peak = peak - LATE_LOCK_BACKSTEP
                desired_lock_legacy = max(lock_from_peak, LATE_MIN_LOCK)
                if desired_lock_legacy > trade.get("sl_dyn", DYN_SL_START):
                    trade["sl_dyn"] = desired_lock_legacy

                # STALL (كما هو)
                last_hi = trade.get("last_new_high", trade.get("opened_at", now))
                stalled = (now - last_hi) >= max(90, STALL_SEC)
                if stalled and r30 <= -0.10 and r90 <= -0.10 and pnl_pct > trade["sl_dyn"] + 0.3:
                    trade["exit_in_progress"] = True; trade["last_exit_try"] = now
                    send_message(f"🔔 خروج {market} (STALL {int(now-last_hi)}ث | r30 {r30:.2f}% r90 {r90:.2f}%)")
                    sell_trade(trade); trade["exit_in_progress"] = False
                    continue

                # قراءة دفتر الطلبات (خياري)
                try:
                    ob = fetch_orderbook(market)
                    if ob:
                        ask_wall = float(ob["asks"][0][1])
                        bid_wall = float(ob["bids"][0][1])
                        if ask_wall > bid_wall * 2 and pnl_pct > 0.5:
                            send_message(f"⚠️ {market} حائط بيع ضخم ({ask_wall:.0f} ضد {bid_wall:.0f}) → خروج حذر")
                            trade["exit_in_progress"] = True; trade["last_exit_try"] = now
                            sell_trade(trade); trade["exit_in_progress"] = False
                            continue
                except Exception:
                    pass

            time.sleep(1)
        except Exception as e:
            print("خطأ في المراقبة:", e)
            time.sleep(5)

Thread(target=monitor_loop, daemon=True).start()

# =========================
# 🧾 ملخص ذكي (بدون تغيير جوهري)
# =========================
def build_summary():
    lines = []
    now = time.time()

    with lock:
        active_copy = list(active_trades)
        exec_copy = list(executed_trades)

    # ===== الصفقات النشطة =====
    if active_copy:
        def cur_pnl(t):
            cur = fetch_price(t["symbol"]) or t["entry"]
            return (cur - t["entry"]) / t["entry"]

        sorted_trades = sorted(active_copy, key=cur_pnl, reverse=True)

        total_value = 0.0
        total_cost  = 0.0
        lines.append(f"📌 الصفقات النشطة ({len(sorted_trades)}):")

        for i, t in enumerate(sorted_trades, 1):
            symbol = t["symbol"].replace("-EUR", "")
            entry  = float(t["entry"])
            amount = float(t["amount"])
            opened = float(t.get("opened_at", now))
            age_sec = int(now - opened)
            age_min = age_sec // 60

            current = fetch_price(t["symbol"]) or entry
            pnl_pct = ((current - entry) / entry) * 100.0
            value   = amount * current
            total_value += value
            total_cost  += float(t.get("cost_eur", entry * amount))

            # قيَم ذكية/مشتقة
            peak_pct   = float(t.get("peak_pct", 0.0))
            dyn_sl     = float(t.get("sl_dyn", DYN_SL_START))  # SL ديناميكي الحالي
            last_hi_ts = float(t.get("last_new_high", opened))
            since_last_hi = int(now - last_hi_ts)
            drop_from_peak = max(0.0, peak_pct - pnl_pct)

            # الزخم اللحظي
            try:
                r30, r90, _ = _mom_metrics(t, current)
            except Exception:
                r30 = r90 = 0.0

            early_phase = age_sec < LATE_FALLBACK_SEC

            if early_phase:
                if pnl_pct <= dyn_sl:
                    state = "🛑 قريب/ضرب SL المبكر"
                elif r30 >= MOM_R30_STRONG and r90 >= MOM_R90_STRONG:
                    state = "🚀 زخم قوي — استمرار (مرحلة مبكرة)"
                else:
                    state = "⏳ مراقبة (مرحلة مبكرة)"
                lock_hint = f"SL المبكر: {dyn_sl:.2f}% (−2% +1 لكل +1 ربح)"
            else:
                lock_hint = f"قفل ذكي ≥ {dyn_sl:.2f}% (قمة {peak_pct:.2f}% | هبوط {drop_from_peak:.2f}%)"
                if r30 <= -FAST_DROP_R30 or r90 <= -FAST_DROP_R90:
                    state = "🔔 خروج محتمل: فشل زخم"
                elif drop_from_peak >= max(GIVEBACK_MIN, min(GIVEBACK_MAX, GIVEBACK_PCT*peak_pct)):
                    state = "🔔 خروج محتمل: giveback"
                elif r30 >= MOM_R30_STRONG and r90 >= MOM_R90_STRONG:
                    state = "🚀 زخم قوي — استمرار"
                else:
                    state = "⏳ مراقبة (مع قفل ربح)"

            emoji = "✅" if pnl_pct >= 0 else "❌"
            lines.append(f"{i}. {symbol}: €{entry:.6f} → €{current:.6f} {emoji} {pnl_pct:+.2f}% | منذ {age_min}د")
            lines.append(f"   • كمية: {amount:.5f} | أعلى: {peak_pct:.2f}% | SL ديناميكي: {dyn_sl:.2f}% | من آخر قمة: {since_last_hi}s")
            lines.append(f"   • زخم: r30 {r30:+.2f}% / r90 {r90:+.2f}% | {lock_hint} | حالة: {state}")

        floating_pnl_eur = total_value - total_cost
        floating_pnl_pct = ((total_value / total_cost) - 1.0) * 100 if total_cost > 0 else 0.0
        lines.append(f"💼 قيمة الصفقات: €{total_value:.2f} | عائم: {floating_pnl_eur:+.2f}€ ({floating_pnl_pct:+.2f}%)")
    else:
        lines.append("📌 لا توجد صفقات نشطة.")

    # ===== صفقات منفذة/رسوم منذ آخر «انسى» =====
    realized_pnl = 0.0
    buy_fees = 0.0
    sell_fees = 0.0
    shown = 0

    if exec_copy:
        raw = r.get(SINCE_RESET_KEY)
        since_ts = float(raw.decode() if isinstance(raw, (bytes, bytearray)) else (raw or 0))

        lines.append("\n📊 آخر صفقات منفذة:")
        for t in reversed(exec_copy):
            if "pnl_eur" in t and "exit_time" in t:
                if float(t["exit_time"]) >= since_ts:
                    realized_pnl += float(t["pnl_eur"])
                    buy_fees  += float(t.get("buy_fee_eur", 0))
                    sell_fees += float(t.get("sell_fee_eur", 0))
                sign = "✅" if float(t["pnl_eur"]) >= 0 else "❌"
                sym = t["symbol"].replace("-EUR","")
                lines.append(f"- {sym}: {sign} {float(t['pnl_eur']):+.2f}€ ({float(t['pnl_pct']):+.2f}%)")
                shown += 1
                if shown >= 5:
                    break

        total_fees = buy_fees + sell_fees
        lines.append(f"\n🧮 منذ آخر انسى:")
        lines.append(f"• أرباح/خسائر محققة: {realized_pnl:+.2f}€")
        if active_copy:
            lines.append(f"• أرباح/خسائر عائمة حاليًا: {floating_pnl_eur:+.2f}€")
        lines.append(f"• الرسوم المدفوعة: {total_fees:.2f}€ (شراء: {buy_fees:.2f}€ / بيع: {sell_fees:.2f}€)")
    else:
        lines.append("\n📊 لا توجد صفقات سابقة.")

    return "\n".join(lines)

# =========================
# 🤖 Webhook
# =========================
@app.route("/", methods=["POST"])
def webhook():
    global enabled

    data = request.get_json(silent=True) or {}

    # استخراج النص من تيليغرام أو من بوت B
    if "message" in data and isinstance(data["message"], dict):
        text = (data["message"].get("text") or "").strip()
    else:
        text = (data.get("text") or "").strip()

    if not text:
        return "ok"

    t_lower = text.lower()

    if "اشتري" in t_lower:
        if not enabled:
            send_message("🚫 البوت متوقف عن الشراء.")
            return "ok"
        try:
            symbol = text.split("اشتري", 1)[-1].strip().upper()
            if not symbol:
                raise ValueError("no symbol")
            buy(symbol)
        except Exception:
            send_message("❌ الصيغة غير صحيحة. مثال: اشتري ADA")
        return "ok"

    elif "الملخص" in t_lower:
        send_message(build_summary())
        return "ok"

    elif "الرصيد" in t_lower:
        balances = bitvavo_request("GET", "/balance")
        eur = sum(float(b.get("available", 0)) + float(b.get("inOrder", 0))
                  for b in balances if b.get("symbol") == "EUR")
        total = eur
        winners, losers = [], []

        with lock:
            exec_copy = list(executed_trades)

        for b in balances:
            sym = b.get("symbol")
            if sym == "EUR":
                continue
            qty = float(b.get("available", 0)) + float(b.get("inOrder", 0))
            if qty < 0.0001:
                continue
            pair = f"{sym}-EUR"
            price = fetch_price(pair)
            if not price:
                continue
            value = qty * price
            total += value

            entry = None
            for t in reversed(exec_copy):
                if t["symbol"] == pair:
                    entry = t.get("entry")
                    break

            if entry:
                pnl = ((price - entry) / entry) * 100
                line = f"{sym}: {qty:.4f} @ €{price:.4f} → {pnl:+.2f}%"
                (winners if pnl >= 0 else losers).append(line)

        lines = [f"💰 الرصيد الكلي: €{total:.2f}"]
        if winners:
            lines.append("\n📈 رابحين:\n" + "\n".join(winners))
        if losers:
            lines.append("\n📉 خاسرين:\n" + "\n".join(losers))
        if not winners and not losers:
            lines.append("\n🚫 لا توجد عملات قيد التداول.")
        send_message("\n".join(lines))
        return "ok"

    elif "قف" in t_lower:
        enabled = False
        send_message("🛑 تم إيقاف الشراء.")
        return "ok"

    elif "ابدأ" in t_lower:
        enabled = True
        send_message("✅ تم تفعيل الشراء.")
        return "ok"

    elif "قائمة الحظر" in t_lower:
        keys = [k.decode() if isinstance(k, bytes) else k for k in r.keys("ban24:*")]
        if not keys:
            send_message("🧊 لا توجد عملات محظورة حالياً.")
        else:
            names = [k.split("ban24:")[-1] for k in keys]
            send_message("🧊 العملات المحظورة 24h:\n- " + "\n- ".join(sorted(names)))
        return "ok"

    elif t_lower.startswith("الغ حظر"):
        try:
            coin = text.split("الغ حظر", 1)[-1].strip().upper()
            if r.delete(f"ban24:{coin}"):
                send_message(f"✅ أُلغي حظر {coin}.")
            else:
                send_message(f"ℹ️ لا يوجد حظر على {coin}.")
        except Exception:
            send_message("❌ الصيغة: الغ حظر ADA")
        return "ok"

    elif "انسى" in t_lower:
        with lock:
            active_trades.clear()
            executed_trades.clear()
            r.delete("nems:active_trades")
            r.delete("nems:executed_trades")
            r.set(SINCE_RESET_KEY, time.time())
        send_message("🧠 تم نسيان كل شيء! بدأنا عد جديد للإحصائيات 🤖")
        return "ok"

    elif "عدد الصفقات" in t_lower or "عدل الصفقات" in t_lower:
        send_message("ℹ️ عدد الصفقات ثابت: 2 (بدون استبدال).")
        return "ok"

    return "ok"

# =========================
# 🔁 تحميل الحالة من Redis عند الإقلاع
# =========================
try:
    at = r.get("nems:active_trades")
    if at:
        active_trades = json.loads(at)
    et = r.lrange("nems:executed_trades", 0, -1)
    executed_trades = [json.loads(t) for t in et]
    if not r.exists(SINCE_RESET_KEY):
        r.set(SINCE_RESET_KEY, 0)
except Exception as e:
    print("state load error:", e)

# =========================
# 🚀 التشغيل المحلي (Railway يستخدم gunicorn)
# =========================
if __name__ == "__main__" and os.getenv("RUN_LOCAL") == "1":
    app.run(host="0.0.0.0", port=5000)