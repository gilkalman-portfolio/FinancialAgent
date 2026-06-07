"""
Macro Signals Engine
====================
מטפל בזיהוי macro signals מכתבות וממפה אותם לתגובות שוק צפויות.

הגישה:
  1. LLM מזהה macro signals מהטקסט (oil_up, rates_up, inflation_high, ...)
  2. מטריצת קורלציות hardcoded ממפה כל signal → סקטורים/מניות מושפעים
  3. grounding: נתוני מחיר אמיתיים (RSI, short%, volume) מחליטים מי באמת רלוונטי
  4. surprise_factor: האם הידיעה מפתיעה או צפויה — גורם המכפיל הכי חשוב

כל הקורלציות מבוססות על ידע כלכלי מבוסס היסטורי, לא ניחוש LLM.
"""

from typing import Dict, List, Any, Optional
from loguru import logger

# ══════════════════════════════════════════════════════════════════════════════
# מטריצת Macro Signals → השפעה על סקטורים ומניות ספציפיות
# ══════════════════════════════════════════════════════════════════════════════
#
# מבנה כל רשומה:
#   "signal_key": {
#       "description": "תיאור קצר",
#       "positive_sectors": ["sector1", ...],   # סקטורים שנהנים
#       "negative_sectors": ["sector1", ...],   # סקטורים שנפגעים
#       "positive_tickers": ["TICK", ...],      # מניות ספציפיות שנהנות
#       "negative_tickers": ["TICK", ...],      # מניות ספציפיות שנפגעות
#       "strength": 1-3,                        # עוצמת הקורלציה ההיסטורית
#       "conditions": "הערות על תנאים מיוחדים",
#   }

MACRO_CORRELATION_MATRIX: Dict[str, Dict] = {

    # ── נפט ───────────────────────────────────────────────────────────────────
    "oil_up": {
        "description": "מחיר נפט עולה",
        "positive_sectors": ["Energy"],
        "negative_sectors": ["Industrials", "Consumer Discretionary", "Transportation"],
        "positive_tickers": ["XOM", "CVX", "COP", "OXY", "SLB", "HAL", "MPC", "VLO"],
        "negative_tickers": ["DAL", "AAL", "UAL", "LUV", "UPS", "FDX", "JBLU"],
        "strength": 3,
        "conditions": "השפעה מיידית על airlines וlogistics. E&P חברות נהנות יותר מ-refiners.",
    },
    "oil_down": {
        "description": "מחיר נפט יורד",
        "positive_sectors": ["Consumer Discretionary", "Transportation", "Industrials"],
        "negative_sectors": ["Energy"],
        "positive_tickers": ["DAL", "AAL", "UAL", "LUV", "UPS", "FDX", "AMZN"],
        "negative_tickers": ["XOM", "CVX", "COP", "OXY", "SLB"],
        "strength": 3,
        "conditions": "טוב לחברות עם עלויות דלק גבוהות.",
    },

    # ── ריבית / פד ─────────────────────────────────────────────────────────────
    "rates_up": {
        "description": "ריבית עולה / פד hawkish",
        "positive_sectors": ["Financials"],
        "negative_sectors": ["Real Estate", "Utilities", "Technology", "Consumer Discretionary"],
        "positive_tickers": ["JPM", "BAC", "GS", "MS", "WFC", "C", "USB", "TFC"],
        "negative_tickers": ["TLT", "IEF", "XLRE", "AMT", "PLD", "NEE", "DUK", "ARKK"],
        "strength": 3,
        "conditions": "growth stocks נפגעים הכי קשה כי DCF מציג ערך נמוך יותר. Banks נהנים מ-net interest margin.",
    },
    "rates_down": {
        "description": "ריבית יורדת / פד dovish",
        "positive_sectors": ["Real Estate", "Utilities", "Technology", "Consumer Discretionary"],
        "negative_sectors": ["Financials"],
        "positive_tickers": ["TLT", "IEF", "XLRE", "AMT", "PLD", "NEE", "DUK", "MSFT", "GOOGL", "AMZN"],
        "negative_tickers": ["JPM", "BAC", "WFC", "USB"],
        "strength": 3,
        "conditions": "REITs ו-utilities נהנים מ-yield compression. Growth stocks נהנים מDCF.",
    },
    "fed_pause": {
        "description": "פד עוצר העלאות / pause",
        "positive_sectors": ["Technology", "Consumer Discretionary", "Real Estate"],
        "negative_sectors": [],
        "positive_tickers": ["QQQ", "SPY", "ARKK", "AMZN", "NVDA"],
        "negative_tickers": [],
        "strength": 2,
        "conditions": "שוק בד\"כ עולה על הודעת pause אם לא צפויה.",
    },

    # ── אינפלציה ────────────────────────────────────────────────────────────────
    "inflation_high": {
        "description": "אינפלציה גבוהה / CPI beat",
        "positive_sectors": ["Energy", "Materials", "Financials"],
        "negative_sectors": ["Technology", "Consumer Discretionary", "Real Estate", "Utilities"],
        "positive_tickers": ["GLD", "SLV", "GDX", "XLE", "FCX", "NEM", "TIPS"],
        "negative_tickers": ["TLT", "IEF", "ARKK", "ZM", "SHOP"],
        "strength": 3,
        "conditions": "זהב ומתכות יקרות כ-inflation hedge. Real assets עולים. Bonds נפגעים.",
    },
    "inflation_cooling": {
        "description": "אינפלציה מתמתנת / CPI miss",
        "positive_sectors": ["Technology", "Consumer Discretionary", "Real Estate"],
        "negative_sectors": ["Energy", "Materials"],
        "positive_tickers": ["TLT", "QQQ", "ARKK", "AMZN", "MSFT"],
        "negative_tickers": ["GLD", "XLE", "FCX"],
        "strength": 3,
        "conditions": "ירידת אינפלציה → ציפיות לריבית נמוכה יותר → growth נהנה.",
    },

    # ── שוק העבודה ────────────────────────────────────────────────────────────
    "jobs_strong": {
        "description": "משרות חזקות / NFP beat / אבטלה נמוכה",
        "positive_sectors": ["Consumer Discretionary", "Financials", "Industrials"],
        "negative_sectors": ["Utilities", "Real Estate"],
        "positive_tickers": ["AMZN", "HD", "MCD", "SBUX", "JPM", "BAC", "CAT", "DE"],
        "negative_tickers": ["TLT", "NEE", "DUK"],
        "strength": 2,
        "conditions": "שוק עבודה חזק → צרכנים מוציאים יותר. אבל גם → פד עלול להעלות ריבית.",
    },
    "jobs_weak": {
        "description": "משרות חלשות / NFP miss / אבטלה עולה",
        "positive_sectors": ["Utilities", "Real Estate", "Consumer Staples"],
        "negative_sectors": ["Consumer Discretionary", "Financials"],
        "positive_tickers": ["TLT", "GLD", "NEE", "WM", "KO", "PG"],
        "negative_tickers": ["JPM", "BAC", "AMZN", "HD"],
        "strength": 2,
        "conditions": "שוק עבודה חלש → ציפיות להורדת ריבית → defensive נהנות.",
    },

    # ── דולר ──────────────────────────────────────────────────────────────────
    "dollar_strong": {
        "description": "דולר מתחזק / DXY עולה",
        "positive_sectors": ["Financials", "Domestic pure-plays"],
        "negative_sectors": ["Technology", "Materials", "Energy"],
        "positive_tickers": ["UUP"],
        "negative_tickers": ["AAPL", "MSFT", "GOOGL", "AMZN", "FCX", "NEM", "GLD"],
        "strength": 2,
        "conditions": "חברות עם הכנסות גלובליות גבוהות נפגעות מהמרה חזרה לדולר.",
    },
    "dollar_weak": {
        "description": "דולר נחלש / DXY יורד",
        "positive_sectors": ["Technology", "Materials", "Energy", "Multinationals"],
        "negative_sectors": [],
        "positive_tickers": ["AAPL", "MSFT", "GOOGL", "GLD", "SLV", "FCX", "NEM"],
        "negative_tickers": ["UUP"],
        "strength": 2,
        "conditions": "multinationals נהנות מהמרה חזרה. Commodities עולות כי נקובות בדולר.",
    },

    # ── GDP / צמיחה ────────────────────────────────────────────────────────────
    "gdp_strong": {
        "description": "GDP חזק / צמיחה גבוהה מהצפוי",
        "positive_sectors": ["Consumer Discretionary", "Industrials", "Financials", "Technology"],
        "negative_sectors": ["Utilities", "Consumer Staples"],
        "positive_tickers": ["SPY", "QQQ", "CAT", "DE", "JPM"],
        "negative_tickers": ["TLT", "GLD"],
        "strength": 2,
        "conditions": "risk-on environment. Cyclicals ו-growth נהנים.",
    },
    "recession_fear": {
        "description": "חשש ממיתון / GDP שלילי / yield curve inversion",
        "positive_sectors": ["Utilities", "Consumer Staples", "Health Care"],
        "negative_sectors": ["Consumer Discretionary", "Financials", "Industrials", "Technology"],
        "positive_tickers": ["GLD", "TLT", "WM", "KO", "PG", "JNJ", "UNH", "LMT"],
        "negative_tickers": ["JPM", "BAC", "CAT", "DE", "AMZN", "NVDA"],
        "strength": 3,
        "conditions": "flight to safety. Defensive sectors נהנים. Cyclicals נפגעים.",
    },

    # ── גיאופוליטיקה ──────────────────────────────────────────────────────────
    "geopolitical_risk": {
        "description": "מתח גיאופוליטי / מלחמה / sanctions",
        "positive_sectors": ["Defense", "Energy", "Materials"],
        "negative_sectors": ["Travel", "Consumer Discretionary", "Technology"],
        "positive_tickers": ["LMT", "RTX", "NOC", "GD", "BA", "GLD", "XOM", "CVX"],
        "negative_tickers": ["DAL", "AAL", "MAR", "HLT", "BKNG"],
        "strength": 3,
        "conditions": "defense stocks עולים מיד. נפט עולה אם אזור המתח הוא producer. זהב כ-safe haven.",
    },
    "geopolitical_ease": {
        "description": "הפגת מתחים גיאופוליטיים / הסכם שלום",
        "positive_sectors": ["Travel", "Consumer Discretionary", "Technology"],
        "negative_sectors": ["Defense", "Energy"],
        "positive_tickers": ["DAL", "AAL", "MAR", "HLT", "BKNG", "ABNB"],
        "negative_tickers": ["LMT", "RTX", "NOC", "GD"],
        "strength": 2,
        "conditions": "risk-on. Travel stocks נהנים הכי מהר.",
    },

    # ── שרשרת אספקה / טכנולוגיה ──────────────────────────────────────────────
    "semiconductor_shortage": {
        "description": "מחסור בשבבים / supply chain disruption",
        "positive_sectors": ["Semiconductors"],
        "negative_sectors": ["Automotive", "Consumer Electronics", "Industrials"],
        "positive_tickers": ["NVDA", "AMD", "INTC", "TSM", "ASML", "AMAT", "LRCX", "KLAC"],
        "negative_tickers": ["F", "GM", "TSLA", "AAPL", "DELL", "HPQ"],
        "strength": 3,
        "conditions": "fabless designers נהנים (pricing power). OEMs נפגעים.",
    },
    "ai_boom": {
        "description": "AI demand surge / breakthroughs",
        "positive_sectors": ["Technology", "Semiconductors", "Cloud"],
        "negative_sectors": [],
        "positive_tickers": ["NVDA", "AMD", "MSFT", "GOOGL", "META", "AMZN", "AVGO", "ARM"],
        "negative_tickers": [],
        "strength": 3,
        "conditions": "NVDA נהנית הכי ישיר (GPUs). Cloud providers נהנים מ-AI workloads.",
    },
    "supply_chain_disruption": {
        "description": "שיבוש שרשרת אספקה כללי",
        "positive_sectors": ["Domestic manufacturing", "Logistics"],
        "negative_sectors": ["Consumer Discretionary", "Automotive", "Electronics"],
        "positive_tickers": ["UPS", "FDX", "XPO", "CHRW"],
        "negative_tickers": ["AAPL", "TSLA", "F", "GM", "NKE"],
        "strength": 2,
        "conditions": "חברות עם supply chain מגוון נפגעות פחות.",
    },

    # ── Consumer ──────────────────────────────────────────────────────────────
    "consumer_confidence_up": {
        "description": "אמון הצרכן עולה",
        "positive_sectors": ["Consumer Discretionary", "Travel", "Retail"],
        "negative_sectors": ["Utilities", "Consumer Staples"],
        "positive_tickers": ["AMZN", "HD", "MCD", "SBUX", "BKNG", "MAR", "NKE"],
        "negative_tickers": [],
        "strength": 2,
        "conditions": "high-end discretionary נהנה יותר מ-value retail.",
    },
    "consumer_confidence_down": {
        "description": "אמון הצרכן יורד",
        "positive_sectors": ["Consumer Staples", "Utilities", "Discount Retail"],
        "negative_sectors": ["Consumer Discretionary", "Travel"],
        "positive_tickers": ["WMT", "COST", "DG", "KO", "PG", "WM"],
        "negative_tickers": ["AMZN", "HD", "BKNG", "MAR", "NKE"],
        "strength": 2,
        "conditions": "trade-down effect: WMT ו-COST נהנים על חשבון premium brands.",
    },

    # ── סחר עולמי ────────────────────────────────────────────────────────────
    "tariffs_up": {
        "description": "מכסי יבוא עולים / trade war",
        "positive_sectors": ["Domestic manufacturing", "Steel", "Aluminum"],
        "negative_sectors": ["Retail", "Technology", "Automotive"],
        "positive_tickers": ["NUE", "X", "AA", "CLF"],
        "negative_tickers": ["AAPL", "NKE", "WMT", "COST", "F", "GM", "TSLA"],
        "strength": 3,
        "conditions": "חברות עם manufacturing בחו\"ל נפגעות. Domestic producers נהנים.",
    },
    "tariffs_down": {
        "description": "הורדת מכסים / trade deal",
        "positive_sectors": ["Technology", "Retail", "Automotive"],
        "negative_sectors": ["Domestic manufacturing"],
        "positive_tickers": ["AAPL", "NKE", "WMT", "AMZN", "TSLA"],
        "negative_tickers": ["NUE", "X", "AA"],
        "strength": 2,
        "conditions": "multinationals עם supply chain בסין נהנות הכי מהר.",
    },

    # ── אנרגיה ירוקה / ESG ───────────────────────────────────────────────────
    "clean_energy_boost": {
        "description": "תמיכה ממשלתית באנרגיה ירוקה / IRA / subsidies",
        "positive_sectors": ["Clean Energy", "Utilities", "Materials"],
        "negative_sectors": ["Traditional Energy"],
        "positive_tickers": ["ENPH", "FSLR", "NEE", "BEP", "PLUG", "RUN", "ALB", "MP"],
        "negative_tickers": ["XOM", "CVX", "COP"],
        "strength": 2,
        "conditions": "lithium ו-rare earth miners נהנים מbitery supply chain.",
    },
    "crypto_up": {
        "description": "crypto surge / institutional adoption",
        "positive_sectors": ["Crypto", "Fintech"],
        "negative_sectors": [],
        "positive_tickers": ["COIN", "MSTR", "MARA", "RIOT", "SQ", "PYPL"],
        "negative_tickers": [],
        "strength": 2,
        "conditions": "COIN הכי ישיר. MSTR מוחזקת כ-Bitcoin proxy.",
    },

    # ── Healthcare ────────────────────────────────────────────────────────────
    "drug_approval": {
        "description": "FDA drug approval / breakthrough",
        "positive_sectors": ["Health Care", "Biotech"],
        "negative_sectors": [],
        "positive_tickers": ["JNJ", "PFE", "MRK", "ABBV", "AMGN", "BIIB", "REGN"],
        "negative_tickers": [],
        "strength": 2,
        "conditions": "השפעה ישירה על המניה הספציפית. Sector-wide אם מדובר בפלטפורמה.",
    },
    "healthcare_regulation": {
        "description": "רגולציה/pricing pressure על תרופות",
        "positive_sectors": [],
        "negative_sectors": ["Health Care", "Pharma"],
        "positive_tickers": [],
        "negative_tickers": ["JNJ", "PFE", "MRK", "ABBV", "UNH"],
        "strength": 2,
        "conditions": "drug pricing reform פוגע בmargin של big pharma.",
    },

    # ── Housing / Real Estate ──────────────────────────────────────────────────
    "housing_strong": {
        "description": "שוק הנדל\"ן חזק / housing starts beat",
        "positive_sectors": ["Real Estate", "Materials", "Consumer Discretionary"],
        "negative_sectors": [],
        "positive_tickers": ["DHI", "LEN", "PHM", "NVR", "HD", "LOW", "SHW", "MAS"],
        "negative_tickers": [],
        "strength": 2,
        "conditions": "homebuilders ו-home improvement נהנים ישירות.",
    },
    "housing_weak": {
        "description": "שוק הנדל\"ן חלש / mortgage rates גבוהות",
        "positive_sectors": [],
        "negative_sectors": ["Real Estate", "Materials", "Consumer Discretionary"],
        "positive_tickers": [],
        "negative_tickers": ["DHI", "LEN", "PHM", "HD", "LOW", "XLRE"],
        "strength": 2,
        "conditions": "rising mortgage rates → fewer transactions → homebuilders נפגעים.",
    },

    # ── Banking / Credit ──────────────────────────────────────────────────────
    "credit_tightening": {
        "description": "הידוק אשראי / bank stress / credit crunch",
        "positive_sectors": ["Consumer Staples", "Utilities"],
        "negative_sectors": ["Financials", "Consumer Discretionary", "Real Estate"],
        "positive_tickers": ["GLD", "TLT", "WM", "KO"],
        "negative_tickers": ["JPM", "BAC", "WFC", "C", "AMZN", "HD"],
        "strength": 3,
        "conditions": "SME financing נפגע קשה. Large caps עם cash נפגעים פחות.",
    },
    "earnings_season_strong": {
        "description": "עונת דוחות חזקה / beats widespread",
        "positive_sectors": ["Technology", "Consumer Discretionary", "Financials"],
        "negative_sectors": [],
        "positive_tickers": ["SPY", "QQQ"],
        "negative_tickers": [],
        "strength": 1,
        "conditions": "broad market rally. High-beta stocks נהנים יותר.",
    },
}

# ══════════════════════════════════════════════════════════════════════════════
# מיפוי מילות מפתח → signal keys (לשימוש ה-LLM prompt)
# ══════════════════════════════════════════════════════════════════════════════

SIGNAL_KEYWORDS: Dict[str, List[str]] = {
    "oil_up":                ["oil prices rise", "crude up", "brent higher", "WTI surge", "oil rally", "OPEC cut"],
    "oil_down":              ["oil prices fall", "crude down", "oil slump", "OPEC increase production"],
    "rates_up":              ["rate hike", "fed raises", "interest rate increase", "hawkish", "tightening", "higher rates"],
    "rates_down":            ["rate cut", "fed cuts", "interest rate decrease", "dovish", "easing", "lower rates"],
    "fed_pause":             ["fed pause", "hold rates", "no rate change", "rates unchanged"],
    "inflation_high":        ["inflation rises", "CPI beat", "hot inflation", "price pressures", "above expectations"],
    "inflation_cooling":     ["inflation cools", "CPI miss", "disinflation", "prices easing", "below expectations"],
    "jobs_strong":           ["jobs beat", "NFP strong", "unemployment falls", "payrolls beat", "low unemployment", "hiring surge"],
    "jobs_weak":             ["jobs miss", "NFP weak", "unemployment rises", "layoffs", "weak payrolls"],
    "dollar_strong":         ["dollar strengthens", "DXY rises", "USD gains", "dollar rally"],
    "dollar_weak":           ["dollar weakens", "DXY falls", "USD drops", "dollar decline"],
    "gdp_strong":            ["GDP beat", "strong growth", "economy expands", "GDP above"],
    "recession_fear":        ["recession", "economic contraction", "GDP negative", "yield curve inversion", "slowdown fears"],
    "geopolitical_risk":     ["war", "conflict", "sanctions", "military", "geopolitical tension", "attack"],
    "geopolitical_ease":     ["ceasefire", "peace deal", "sanctions lifted", "diplomatic", "trade agreement"],
    "semiconductor_shortage":["chip shortage", "semiconductor supply", "chip crisis", "wafer shortage"],
    "ai_boom":               ["AI demand", "artificial intelligence", "GPU shortage", "AI investment", "AI revenue"],
    "supply_chain_disruption":["supply chain", "logistics disruption", "shipping delays", "port congestion"],
    "consumer_confidence_up":["consumer confidence rises", "consumer sentiment up", "spending increase"],
    "consumer_confidence_down":["consumer confidence falls", "sentiment down", "spending cut"],
    "tariffs_up":            ["tariffs", "trade war", "import duties", "trade restrictions", "sanctions"],
    "tariffs_down":          ["tariffs removed", "trade deal", "trade agreement", "trade truce"],
    "clean_energy_boost":    ["clean energy", "solar", "wind", "EV incentives", "green energy", "IRA"],
    "crypto_up":             ["bitcoin", "crypto rally", "cryptocurrency", "institutional crypto"],
    "drug_approval":         ["FDA approval", "drug approved", "clinical trial success", "breakthrough therapy"],
    "healthcare_regulation": ["drug pricing", "Medicare negotiation", "pharma regulation", "price caps"],
    "housing_strong":        ["housing starts beat", "home sales up", "real estate boom", "mortgage demand"],
    "housing_weak":          ["housing starts fall", "home sales down", "mortgage rates high", "real estate slowdown"],
    "credit_tightening":     ["credit crunch", "bank stress", "lending tightens", "credit conditions"],
    "earnings_season_strong":["earnings beat", "strong earnings", "profit beat", "revenue beat", "EPS beat"],
}


def get_signals_for_llm_prompt() -> str:
    """מחזיר רשימת signal keys לשימוש ב-prompt של ה-LLM."""
    lines = []
    for key, keywords in SIGNAL_KEYWORDS.items():
        desc = MACRO_CORRELATION_MATRIX.get(key, {}).get("description", "")
        lines.append(f'  "{key}" — {desc} (e.g., {", ".join(keywords[:3])})')
    return "\n".join(lines)


def get_affected_by_signals(
    signals: List[Dict[str, Any]]
) -> Dict[str, List]:
    """
    קבל רשימת signals מה-LLM והחזר את הסקטורים והטיקרים המושפעים.

    Args:
        signals: [{"signal": "oil_up", "direction": "positive", "surprise": True}, ...]

    Returns:
        {
            "positive_tickers": [...],
            "negative_tickers": [...],
            "positive_sectors": [...],
            "negative_sectors": [...],
            "signal_details": [{"signal": ..., "strength": ..., "surprise_multiplier": ...}]
        }
    """
    pos_tickers: Dict[str, float] = {}
    neg_tickers: Dict[str, float] = {}
    pos_sectors: Dict[str, float] = {}
    neg_sectors: Dict[str, float] = {}
    signal_details = []

    for s in signals:
        key       = s.get("signal", "")
        surprise  = s.get("surprise", False)
        direction = s.get("direction", "positive")  # positive/negative/mixed

        if key not in MACRO_CORRELATION_MATRIX:
            logger.debug(f"Unknown macro signal: {key}")
            continue

        matrix  = MACRO_CORRELATION_MATRIX[key]
        strength = matrix["strength"]

        # surprise multiplier — ידיעה מפתיעה מזיזה שוק יותר מצפויה
        surprise_mult = 1.5 if surprise else 1.0

        score = strength * surprise_mult

        # אם direction=negative → הפוך את ההשפעה (e.g., oil_up אבל בכתבה זה שלילי)
        flip = (direction == "negative")

        pos_t = matrix["positive_tickers"] if not flip else matrix["negative_tickers"]
        neg_t = matrix["negative_tickers"] if not flip else matrix["positive_tickers"]
        pos_s = matrix["positive_sectors"] if not flip else matrix["negative_sectors"]
        neg_s = matrix["negative_sectors"] if not flip else matrix["positive_sectors"]

        for t in pos_t:
            pos_tickers[t] = max(pos_tickers.get(t, 0), score)
        for t in neg_t:
            neg_tickers[t] = max(neg_tickers.get(t, 0), score)
        for s_name in pos_s:
            pos_sectors[s_name] = max(pos_sectors.get(s_name, 0), score)
        for s_name in neg_s:
            neg_sectors[s_name] = max(neg_sectors.get(s_name, 0), score)

        signal_details.append({
            "signal":              key,
            "description":         matrix["description"],
            "strength":            strength,
            "surprise":            surprise,
            "surprise_multiplier": surprise_mult,
            "effective_score":     score,
            "conditions":          matrix.get("conditions", ""),
        })

    # מיון לפי score
    sorted_pos = sorted(pos_tickers.items(), key=lambda x: x[1], reverse=True)
    sorted_neg = sorted(neg_tickers.items(), key=lambda x: x[1], reverse=True)

    return {
        "positive_tickers": [t for t, _ in sorted_pos],
        "negative_tickers": [t for t, _ in sorted_neg],
        "positive_sectors":  sorted(pos_sectors, key=pos_sectors.get, reverse=True),
        "negative_sectors":  sorted(neg_sectors, key=neg_sectors.get, reverse=True),
        "signal_details":    signal_details,
        "ticker_scores":     {"positive": dict(sorted_pos), "negative": dict(sorted_neg)},
    }
