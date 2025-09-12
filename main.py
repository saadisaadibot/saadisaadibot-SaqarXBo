def load_markets_once():
    """
    نقرأ ميتاداتا الأسواق مرة واحدة:
    - pricePrecision: عدد الأرقام المهمة للسعر (نفس القديم).
    - amountPrecision: قد تأتي بطريقتين:
        * عدد خانات عشرية (مثال: 2 أو 8)
        * أو خطوة كمية (مثال: 0.01 أو 0.000001)
      هون نحوّل أي تمثيل لعدد الخانات 'amountDecimals' بشكل موثوق.
    """
    import math
    global MARKET_MAP, MARKET_META
    if MARKET_MAP and MARKET_META:
        return

    rows = requests.get(f"{BASE_URL}/markets", timeout=10).json()
    m, meta = {}, {}

    def _infer_amount_decimals(raw):
        """
        يحوّل value المحتمل يكون:
          - int خانات (2 → خانتين)
          - float/int خطوة (0.01 → خانتين)
          - str لأحد الشكلين
        ويرجع عدد الخانات العشرية المسموحة للكمية.
        """
        if raw is None:
            return None
        # حاول عدد صحيح مباشر (خانات)
        try:
            if isinstance(raw, int):
                if 0 <= raw <= 20:
                    return int(raw)
            # حوّل لنص
            s = str(raw).strip()
            # لو شكله عدد صحيح كنص
            if s.isdigit():
                v = int(s)
                if 0 <= v <= 20:
                    return v
            # جرّب float
            v = float(s)
            # حالة "عدد خانات" معطى كـ 2.0 مثلًا
            if v >= 0 and abs(v - int(v)) < 1e-12 and v <= 20:
                return int(v)
            # حالة "خطوة" (أصغر من 1): 0.01 → خانتين، 0.0001 → 4 خانات...
            if 0 < v < 1:
                # حوّل إلى نص مضبوط ثم احسب عدد الخانات بعد الفاصلة بدون الأصفار الزائدة
                s2 = f"{v:.16f}".rstrip("0").rstrip(".")
                return len(s2.split(".")[1]) if "." in s2 else 0
        except Exception:
            pass
        return None

    for r in rows:
        if r.get("quote") != "EUR":
            continue

        market = r.get("market")
        base   = (r.get("base") or "").upper()
        if not base or not market:
            continue

        # pricePrecision كما هو
        try:
            priceSig = int(r.get("pricePrecision", 6) or 6)
        except Exception:
            priceSig = 6

        # حاول نستنتج amountDecimals من عدة حقول محتمَلة
        cand = (
            r.get("amountPrecision", None),
            r.get("amountStep", None),
            r.get("amountStepSize", None),
            r.get("stepSize", None),
            r.get("step", None),
        )
        amountDecimals = None
        for c in cand:
            amountDecimals = _infer_amount_decimals(c)
            if amountDecimals is not None:
                break
        if amountDecimals is None:
            amountDecimals = 8  # افتراضي آمن

        meta[market] = {
            "priceSig":       int(priceSig),
            "amountDecimals": int(max(0, amountDecimals)),
            "minQuote":       float(r.get("minOrderInQuoteAsset", 0) or 0.0),
            "minBase":        float(r.get("minOrderInBaseAsset",  0) or 0.0),
        }
        m[base] = market

    MARKET_MAP, MARKET_META = m, meta