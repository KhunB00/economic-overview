#!/usr/bin/env python3
"""
Economic Overview — a daily macro dashboard as a single static HTML page.

WHAT THIS IS
    A once-a-day briefing you read in ~5 minutes. Top fold = what you need to
    know. Below the fold = institutional depth (rates, policy, credit, oil curve,
    positioning). Everything links back to the original publisher.

SETUP (already done if you ran the installer)
    python3 -m venv .venv
    ./.venv/bin/python -m pip install feedparser requests

RUN
    ./.venv/bin/python world_briefing.py --open      # build + open in browser
    ./.venv/bin/python world_briefing.py --demo      # fake data, no internet
    ./.venv/bin/python world_briefing.py --check-data  # where each number came from
    ./.venv/bin/python world_briefing.py --hours 48  # wider news window (Mondays)

FRED KEY (optional, enables real yields / breakevens / credit spreads)
    Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html
    Then put it in a file next to this script:
        echo "YOUR_KEY_HERE" > fred_key.txt
    Or set the env var FRED_API_KEY. The key is never written into this script.

SCHEDULE DAILY AT 07:30 BANGKOK TIME (macOS)
    See SCHEDULING.md next to this script.

DESIGN RULES (why this does not rot)
    1. No HTML scraping anywhere. Only RSS feeds and JSON APIs. Scraping a
       website's markup is what causes constant breakage.
    2. Every number is cached. If a source fails, you see the last good value
       labelled with its age — never a blank, never a silently wrong number.
    3. The page diagnoses itself. The health line at the bottom tells you
       exactly what degraded, so a fix is one paste away.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import os
import re
import statistics
import sys
import unicodedata
import warnings
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")

try:
    import feedparser
    import requests
except ImportError:
    sys.exit(
        "Missing dependencies. Run:\n"
        "  python3 -m venv .venv\n"
        "  ./.venv/bin/python -m pip install feedparser requests\n"
        "then run this script with ./.venv/bin/python"
    )

# ----------------------------------------------------------------------------
# PATHS & CONSTANTS
# ----------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
OUT_HTML = HERE / "world_briefing.html"
MARKET_CACHE = HERE / "market_cache.json"
HISTORY_FILE = HERE / "briefing_history.json"
SNAPSHOT_FILE = HERE / "last_snapshot.json"
FRED_KEY_FILE = HERE / "fred_key.txt"
CALENDAR_CACHE = HERE / "calendar_cache.json"
HEALTH_FILE = HERE / "health.json"

BKK = ZoneInfo("Asia/Bangkok")
NEW_YORK = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 world-briefing/1.0 (personal daily digest)"
    )
}
TIMEOUT = 12

# "What changed" compares against the newest build at least this many hours old,
# so the box stays meaningful whether the script runs hourly or once a day.
COMPARE_AGAINST_HOURS = 8

# Thai gold: one "baht weight" is 15.244 g at 96.5% purity (ornament standard).
BAHT_WEIGHT_G = 15.244
THAI_PURITY = 0.965
GRAMS_PER_TROY_OZ = 31.1034768

# ----------------------------------------------------------------------------
# CONFIG 1 — EVENTS. Edit this list by hand. Do not guess dates.
# ----------------------------------------------------------------------------

EVENTS = [
    # (date, time_note_in_bangkok, label)
    #
    # Only put things here that FRED does not publish — central bank meetings,
    # mostly. The big US data releases below are fetched automatically, so you
    # never have to maintain those.
    ("2026-10-27", "two-day meeting begins", "FOMC meeting (decision ~01:00 BKK on the 29th)"),
    ("2026-10-28", "decision ~01:00 BKK on the 29th", "FOMC decision + press conference"),
]

# Scheduled US data releases, pulled from FRED's official calendar (needs the
# key). These are the releases that reliably move markets. All are published at
# 08:30 New York time; the Bangkok time is worked out per date, so it stays
# correct across US daylight-saving changes.
CALENDAR_RELEASES = [
    (10, "US inflation (CPI)"),
    (50, "US jobs report (non-farm payrolls)"),
    (54, "US core PCE — the Fed's preferred inflation gauge"),
    (53, "US GDP"),
    (9, "US retail sales"),
    (46, "US producer prices (PPI)"),
    (180, "US jobless claims (weekly)"),
]
RELEASE_TIME_ET = dt.time(8, 30)

# ----------------------------------------------------------------------------
# CONFIG 2 — MARKET INSTRUMENTS
#
# kind:  "price" normal % change
#        "yield" value IS a percent; show change in basis points
#        "ffr"   fed funds futures; implied rate = 100 - price
# All symbols below were verified working against Yahoo's chart endpoint.
# Deliberately excluded: ^MOVE (returns the wrong instrument — a Northern Trust
# ETF, not the bond vol index) and XAUUSD=X (returns empty; GC=F used instead).
# ----------------------------------------------------------------------------

TICKERS = {
    "headline": [
        ("^GSPC", "S&P 500", "price"),
        ("GC=F", "Gold", "price"),
        ("CL=F", "WTI crude", "price"),
        ("DX-Y.NYB", "Dollar index (DXY)", "price"),
        ("^TNX", "US 10-year yield", "yield"),
        ("^VIX", "VIX (equity vol)", "price"),
    ],
    "equities": [
        ("^GSPC", "S&P 500 (US)", "price"),
        ("^IXIC", "Nasdaq Composite (US)", "price"),
        ("^DJI", "Dow Jones (US)", "price"),
        ("^RUT", "Russell 2000 (US small-cap)", "price"),
        ("^STOXX50E", "Euro Stoxx 50", "price"),
        ("^FTSE", "FTSE 100 (UK)", "price"),
        ("^GDAXI", "DAX (Germany)", "price"),
        ("^N225", "Nikkei 225 (Japan)", "price"),
        ("^HSI", "Hang Seng (HK)", "price"),
        ("000001.SS", "Shanghai Composite", "price"),
        ("^KS11", "KOSPI (Korea)", "price"),
        ("^NSEI", "Nifty 50 (India)", "price"),
        ("^AXJO", "ASX 200 (Australia)", "price"),
        ("^SET.BK", "SET (Thailand)", "price"),
        # Yahoo serves the SET level but no history at any range, so this ETF
        # carries the trend until the local history above fills in.
        ("THD", "Thailand ETF (THD, in USD)", "price"),
        ("EEM", "Emerging markets (EEM)", "price"),
    ],
    "rates": [
        ("^IRX", "US 3-month T-bill", "yield"),
        ("^FVX", "US 5-year yield", "yield"),
        ("^TNX", "US 10-year yield", "yield"),
        ("^TYX", "US 30-year yield", "yield"),
        ("ZQ=F", "Fed funds futures (implied rate)", "ffr"),
        ("ZN=F", "10-year T-note future", "price"),
        ("TLT", "Long Treasuries (TLT)", "price"),
    ],
    "fx": [
        ("DX-Y.NYB", "Dollar index (DXY)", "price"),
        ("EURUSD=X", "EUR/USD", "price"),
        ("JPY=X", "USD/JPY", "price"),
        ("CNY=X", "USD/CNY", "price"),
        ("KRW=X", "USD/KRW", "price"),
        ("INR=X", "USD/INR", "price"),
        ("THB=X", "USD/THB", "price"),
        # The cleanest war hedge in FX — it bids before headlines land.
        ("CHF=X", "USD/CHF (Swiss franc, safe haven)", "price"),
        # Emerging markets: where capital flight shows up first.
        ("TRY=X", "USD/TRY (Turkey)", "price"),
        ("ZAR=X", "USD/ZAR (South Africa)", "price"),
        ("BRL=X", "USD/BRL (Brazil)", "price"),
        ("MXN=X", "USD/MXN (Mexico)", "price"),
    ],
    "energy": [
        ("CL=F", "WTI crude", "price"),
        ("BZ=F", "Brent crude", "price"),
        ("NG=F", "US natural gas", "price"),
        ("^OVX", "Oil volatility (OVX)", "price"),
    ],
    "commodities": [
        ("GC=F", "Gold", "price"),
        ("SI=F", "Silver", "price"),
        ("HG=F", "Copper", "price"),
        ("ALI=F", "Aluminium", "price"),
        ("ZW=F", "Wheat", "price"),
        ("ZC=F", "Corn", "price"),
        ("ZS=F", "Soybeans", "price"),
    ],
    "risk": [
        ("^VIX", "VIX (equity vol)", "price"),
        ("^VVIX", "VVIX (vol of vol)", "price"),
        ("^GVZ", "GVZ (gold vol)", "price"),
        ("^OVX", "OVX (oil vol)", "price"),
        ("HYG", "High-yield credit (HYG)", "price"),
        ("LQD", "Investment-grade credit (LQD)", "price"),
        ("BTC-USD", "Bitcoin", "price"),
    ],
}

# Everything we need to fetch, de-duplicated.
ALL_SYMBOLS = sorted({sym for grp in TICKERS.values() for sym, _, _ in grp})

# ----------------------------------------------------------------------------
# CONFIG 3 — FRED SERIES (needs a free API key; skipped silently without one)
#
# transform: "level" show as-is | "yoy" year-over-year % | "chg" change vs prior
# ----------------------------------------------------------------------------

FRED_RATES = [
    ("DFII10", "Real 10-year yield (inflation-adjusted)", "level", "%"),
    ("DFII5", "Real 5-year yield", "level", "%"),
    ("T10YIE", "10-year breakeven inflation (market expectation)", "level", "%"),
    ("T5YIE", "5-year breakeven inflation", "level", "%"),
    # The measure the Fed itself cites: long-run expectations with near-term
    # energy noise stripped out. If this breaks higher, nothing else matters.
    ("T5YIFR", "5y5y forward inflation (are expectations anchored?)", "level", "%"),
    ("DGS2", "US 2-year yield", "level", "%"),
    ("T10Y2Y", "2s10s curve slope (10y minus 2y)", "level", "%"),
    ("BAMLH0A0HYM2", "High-yield credit spread", "level", "%"),
    ("BAMLC0A0CM", "Investment-grade credit spread", "level", "%"),
    ("DFEDTARU", "Fed funds target (upper bound)", "level", "%"),
]

FRED_DATA = [
    # Weekly, so it moves between the monthly releases — the earliest honest
    # read on the labour market.
    ("ICSA", "US jobless claims (weekly)", "thousands", "k"),
    ("CPIAUCSL", "US CPI inflation", "yoy", "%"),
    ("CPILFESL", "US core CPI inflation", "yoy", "%"),
    ("PCEPILFE", "US core PCE (the Fed's preferred gauge)", "yoy", "%"),
    ("UNRATE", "US unemployment rate", "level", "%"),
    ("PAYEMS", "US payroll jobs added", "chg", "k"),
    ("RSAFS", "US retail sales", "yoy", "%"),
    ("INDPRO", "US industrial production", "yoy", "%"),
    ("GDPC1", "US real GDP", "yoy", "%"),
]

# Liquidity and financial conditions. Prices tell you what happened; these tell
# you how easy money is, which is the regime that decides whether a rally holds.
FRED_LIQUIDITY = [
    ("NFCI", "Financial conditions (0 = average; + is tight, - is loose)", "level", ""),
    ("STLFSI4", "Financial stress (0 = normal)", "level", ""),
    ("WALCL", "Fed balance sheet", "trillions", "T"),
    ("RRPONTSYD", "Fed reverse repo (cash parked at the Fed)", "level", "B"),
    ("SOFR", "Overnight funding rate", "level", "%"),
    ("BAMLH0A3HYC", "CCC credit spread (riskiest borrowers)", "level", "%"),
]

# ----------------------------------------------------------------------------
# CONFIG 4 — CFTC POSITIONING (free, keyless, official; weekly)
# Exact contract names confirmed against the live API.
# ----------------------------------------------------------------------------

CFTC_MARKETS = [
    ("GOLD - COMMODITY EXCHANGE INC.", "Gold"),
    ("SILVER - COMMODITY EXCHANGE INC.", "Silver"),
    ("COPPER- #1 - COMMODITY EXCHANGE INC.", "Copper"),
    ("CRUDE OIL, LIGHT SWEET-WTI - ICE FUTURES EUROPE", "WTI crude"),
    ("NAT GAS NYME - NEW YORK MERCANTILE EXCHANGE", "Natural gas"),
    ("S&P 500 Consolidated - CHICAGO MERCANTILE EXCHANGE", "S&P 500"),
    ("EURO FX - CHICAGO MERCANTILE EXCHANGE", "Euro"),
]

# ----------------------------------------------------------------------------
# CONFIG 5 — NEWS FEEDS
#
# window_h: how far back to look for THIS feed. Central banks publish every few
#   days, so a 24h window would show zero policy news — the single most
#   important category. Measured: Fed 36h / ECB 44h / BoJ 49h between items.
# paywall: "none" | "soft" | "hard". When several sources cover one story the
#   lowest-paywall source becomes the clickable headline.
# ----------------------------------------------------------------------------

FEEDS = {
    "Policy & central banks": [
        {"name": "US Federal Reserve", "url": "https://www.federalreserve.gov/feeds/press_all.xml",
         "window_h": 240, "paywall": "none", "primary": True},
        {"name": "European Central Bank", "url": "https://www.ecb.europa.eu/rss/press.html",
         "window_h": 240, "paywall": "none", "primary": True},
        {"name": "Bank of Japan", "url": "https://www.boj.or.jp/en/rss/whatsnew.xml",
         "window_h": 240, "paywall": "none", "primary": True},
        {"name": "Bank of England", "url": "https://www.bankofengland.co.uk/rss/news",
         "window_h": 240, "paywall": "none", "primary": True},
    ],
    "Global economy & markets": [
        {"name": "Guardian Business", "url": "https://www.theguardian.com/business/rss",
         "window_h": 36, "paywall": "none"},
        {"name": "BBC Business", "url": "https://feeds.bbci.co.uk/news/business/rss.xml",
         "window_h": 36, "paywall": "none"},
        {"name": "MarketWatch", "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
         "window_h": 36, "paywall": "soft"},
        {"name": "CNBC Economy", "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
         "window_h": 48, "paywall": "none"},
        {"name": "CNBC World Markets", "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839135",
         "window_h": 48, "paywall": "none"},
        {"name": "CNBC Finance", "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664",
         "window_h": 48, "paywall": "none"},
        {"name": "Investing.com Economy", "url": "https://www.investing.com/rss/news_14.rss",
         "window_h": 36, "paywall": "none"},
        {"name": "Financial Times", "url": "https://www.ft.com/rss/home",
         "window_h": 36, "paywall": "hard"},
        {"name": "The Economist", "url": "https://www.economist.com/finance-and-economics/rss.xml",
         "window_h": 168, "paywall": "hard"},
    ],
    "Energy": [
        {"name": "US EIA", "url": "https://www.eia.gov/rss/todayinenergy.xml",
         "window_h": 168, "paywall": "none", "primary": True},
    ],
    "Asia & Thailand": [
        {"name": "Nikkei Asia", "url": "https://asia.nikkei.com/rss/feed/nar",
         "window_h": 48, "paywall": "hard"},
        {"name": "SCMP Economy", "url": "https://www.scmp.com/rss/92/feed",
         "window_h": 48, "paywall": "soft"},
        {"name": "Bangkok Post Business", "url": "https://www.bangkokpost.com/rss/data/business.xml",
         "window_h": 48, "paywall": "none"},
    ],
    # Google News search acts as a free backstop: it finds whichever outlet
    # covered a theme without a paywall and links straight to the publisher.
    "Wire backstop": [
        {"name": "Reuters / wires", "url": "https://news.google.com/rss/search?q=when:1d+reuters+economy+OR+markets&hl=en-US&gl=US&ceid=US:en",
         "window_h": 30, "paywall": "none"},
        {"name": "Central bank wires", "url": "https://news.google.com/rss/search?q=when:2d+%22central+bank%22+OR+%22interest+rates%22+decision&hl=en-US&gl=US&ceid=US:en",
         "window_h": 48, "paywall": "none"},
        {"name": "Oil & energy wires", "url": "https://news.google.com/rss/search?q=when:1d+oil+prices+OR+OPEC&hl=en-US&gl=US&ceid=US:en",
         "window_h": 30, "paywall": "none"},
        {"name": "Thailand economy wires", "url": "https://news.google.com/rss/search?q=when:2d+Thailand+economy+OR+baht&hl=en-US&gl=US&ceid=US:en",
         "window_h": 48, "paywall": "none"},
    ],
    "Geopolitics & supply": [
        {"name": "Sanctions & export controls",
         "url": "https://news.google.com/rss/search?q=when:2d+sanctions+OR+%22export+controls%22+OR+embargo&hl=en-US&gl=US&ceid=US:en",
         "window_h": 48, "paywall": "none"},
        {"name": "Supply chains & shipping",
         "url": "https://news.google.com/rss/search?q=when:2d+%22supply+chain%22+OR+shipping+OR+%22Red+Sea%22+OR+%22Suez%22+OR+%22Hormuz%22&hl=en-US&gl=US&ceid=US:en",
         "window_h": 48, "paywall": "none"},
        {"name": "Conflict & security",
         "url": "https://news.google.com/rss/search?q=when:2d+(war+OR+strike+OR+attack)+(oil+OR+trade+OR+economy+OR+shipping)&hl=en-US&gl=US&ceid=US:en",
         "window_h": 48, "paywall": "none"},
        {"name": "Tariffs & trade policy",
         "url": "https://news.google.com/rss/search?q=when:2d+tariffs+OR+%22trade+war%22+OR+%22trade+deal%22&hl=en-US&gl=US&ceid=US:en",
         "window_h": 48, "paywall": "none"},
        {"name": "Central bank gold buying",
         "url": "https://news.google.com/rss/search?q=when:7d+%22central+bank%22+gold+reserves+OR+bullion&hl=en-US&gl=US&ceid=US:en",
         "window_h": 168, "paywall": "none"},
    ],
}

# Feeds tested and found broken — kept here as a record so we don't retry blind.
#   Bank of Thailand  https://www.bot.or.th/en/rss.html          404
#   BIS press         https://www.bis.org/press/index.rss        404
#   IMF news          https://www.imf.org/external/rss/...       403 (blocks bots)
#   OECD newsroom     https://www.oecd.org/en/rss.xml            403
#   Eurostat          .../euro-indicators/-/rss                  404
#   Nation Thailand   https://www.nationthailand.com/rss/business  200 but 0 items
#   World Bank        https://www.worldbank.org/en/news/all?format=rss  200 but 0 items
#   Yahoo Finance     https://finance.yahoo.com/news/rssindex    stale (11 days)
#   AP via rsshub / Trading Economics                            403

# ----------------------------------------------------------------------------
# CONFIG 6 — THEME TAGGING
# ----------------------------------------------------------------------------

THEMES = {
    "policy": {
        "label": "Policy & rates",
        "kw": ["federal reserve", "fed ", "fomc", "ecb", "european central bank",
               "bank of japan", "boj", "bank of england", "pboc", "central bank",
               "interest rate", "rate cut", "rate hike", "rate decision", "monetary policy",
               "policy rate", "quantitative", "powell", "lagarde", "basis points",
               "hawkish", "dovish", "bond yield", "treasury yield", "bank of thailand"],
    },
    "inflation": {
        "label": "Inflation",
        "kw": ["inflation", "cpi", "consumer price", "producer price", "ppi", "pce",
               "deflation", "disinflation", "cost of living", "price pressure", "wage growth"],
    },
    "growth": {
        "label": "Growth & jobs",
        "kw": ["gdp", "growth", "recession", "jobs", "payroll", "unemployment",
               "employment", "jobless", "labour market", "labor market", "pmi",
               "manufacturing", "retail sales", "consumer confidence", "industrial production"],
    },
    "energy": {
        "label": "Energy",
        "kw": ["oil", "crude", "brent", "wti", "opec", "gas", "lng", "petrol",
               "refinery", "energy price", "electricity", "pipeline", "barrel"],
    },
    "trade": {
        "label": "Trade & geopolitics",
        "kw": ["tariff", "trade war", "trade deal", "export", "import", "sanction",
               "customs", "wto", "supply chain", "war", "conflict", "strike",
               "military", "geopolitic", "embargo", "blockade"],
    },
    "asia": {
        "label": "Asia & China",
        "kw": ["china", "chinese", "beijing", "japan", "japanese", "korea", "india",
               "asia", "yuan", "renminbi", "yen", "hong kong", "taiwan", "asean",
               "vietnam", "indonesia", "singapore"],
    },
    "thailand": {
        "label": "Thailand",
        "kw": ["thailand", "thai", "bangkok", "baht", "set index", "bot "],
    },
    "gold": {
        "label": "Gold",
        "kw": ["gold", "bullion", "precious metal", "silver", "real yield",
               "safe haven", "central bank buying", "xau"],
    },
    "equities": {
        "label": "Stocks",
        "kw": ["stock", "equities", "shares", "s&p", "nasdaq", "dow", "wall street",
               "earnings", "index fell", "index rose", "selloff", "rally", "ipo"],
    },
}

# Weights used to rank the top stories.
RANK_WEIGHTS = {
    "policy": 5.0, "inflation": 4.5, "growth": 3.5, "trade": 3.0,
    "energy": 2.5, "gold": 2.0, "asia": 1.5, "equities": 1.5, "thailand": 1.0,
}
HIGH_IMPACT_KW = ["fomc", "rate decision", "rate cut", "rate hike", "emergency",
                  "cpi", "payroll", "gdp", "recession", "tariff", "sanction",
                  "default", "crisis", "intervention", "opec"]

MONTH_CODES = {1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
               7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z"}


# ============================================================================
# SMALL UTILITIES
# ============================================================================

def now_bkk() -> dt.datetime:
    return dt.datetime.now(UTC).astimezone(BKK)


def http_get(url: str, params: dict | None = None, timeout: int = TIMEOUT):
    """Single GET with our User-Agent. Returns the response or raises."""
    return requests.get(url, params=params, headers=UA, timeout=timeout)


def load_json(path: Path, default):
    try:
        with path.open() as fh:
            return json.load(fh)
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w") as fh:
            json.dump(data, fh, indent=1)
        tmp.replace(path)
    except Exception as exc:
        print(f"  ! could not write {path.name}: {exc}", file=sys.stderr)


def fmt_num(v, decimals: int | None = None) -> str:
    """Human-friendly number: thousands separators, sensible decimals."""
    if v is None:
        return "n/a"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "n/a"
    if decimals is None:
        av = abs(v)
        decimals = 0 if av >= 1000 else 2 if av >= 1 else 4
    return f"{v:,.{decimals}f}"


def fmt_signed(v, decimals: int = 2, suffix: str = "") -> str:
    if v is None:
        return "n/a"
    return f"{v:+,.{decimals}f}{suffix}"


def pct_class(v) -> str:
    if v is None:
        return "flat"
    if v > 0.0005:
        return "up"
    if v < -0.0005:
        return "down"
    return "flat"


def age_label(iso: str | None) -> str:
    """'3h old' / '2 days old' for a cached timestamp."""
    if not iso:
        return ""
    try:
        then = dt.datetime.fromisoformat(iso)
    except Exception:
        return ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    hrs = (dt.datetime.now(UTC) - then).total_seconds() / 3600
    if hrs < 1:
        return "just now"
    if hrs < 24:
        return f"{hrs:.0f}h old"
    return f"{hrs / 24:.0f} day{'s' if hrs >= 48 else ''} old"


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


# ============================================================================
# MARKET DATA — Yahoo chart JSON, called directly
#
# We deliberately do NOT use yfinance: it scrapes Yahoo's web pages and breaks
# whenever they change their markup. The endpoint below is a plain JSON API and
# has been stable for years.
# ============================================================================

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{}"


def fetch_quote(symbol: str) -> dict:
    """
    Fetch one instrument and compute every horizon we show.
    Returns {"ok": bool, ...}. Never raises.
    """
    out = {"symbol": symbol, "ok": False, "source": "live", "error": None}
    try:
        resp = http_get(YAHOO_CHART.format(requests.utils.quote(symbol, safe="")),
                        params={"range": "1y", "interval": "1d"})
        if resp.status_code != 200:
            out["error"] = f"http {resp.status_code}"
            return out
        payload = resp.json()
        results = (payload.get("chart") or {}).get("result")
        if not results:
            err = (payload.get("chart") or {}).get("error")
            out["error"] = (err or {}).get("description", "empty result") if err else "empty result"
            return out

        res = results[0]
        meta = res.get("meta", {})
        stamps = res.get("timestamp") or []
        quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        closes_raw = quote.get("close") or []

        # Keep only (time, close) pairs where close is a real number.
        series = [(t, c) for t, c in zip(stamps, closes_raw) if c is not None]
        if not series:
            out["error"] = "no closes"
            return out

        times = [dt.datetime.fromtimestamp(t, UTC) for t, _ in series]
        closes = [float(c) for _, c in series]

        last = meta.get("regularMarketPrice")
        last = float(last) if isinstance(last, (int, float)) else closes[-1]

        def pct_from(idx: int):
            if idx < 0 or idx >= len(closes) or closes[idx] in (0, None):
                return None
            return (last / closes[idx] - 1.0) * 100.0

        def abs_from(idx: int):
            if idx < 0 or idx >= len(closes):
                return None
            return last - closes[idx]

        # 1 day = previous close. 1 week ~ 5 sessions. 1 month ~ 21 sessions.
        i_1d = len(closes) - 2
        i_1w = len(closes) - 6
        i_1m = len(closes) - 22

        # YTD = last close of the previous calendar year.
        this_year = now_bkk().year
        i_ytd = -1
        for i, t in enumerate(times):
            if t.year == this_year:
                i_ytd = i - 1
                break

        # Volatility of the last 20 daily returns, used for the sigma flag.
        sigma = None
        rets = []
        for a, b in zip(closes[-21:-1], closes[-20:]):
            if a:
                rets.append((b / a - 1.0) * 100.0)
        if len(rets) >= 10:
            try:
                sigma = statistics.stdev(rets)
            except statistics.StatisticsError:
                sigma = None

        out.update({
            "ok": True,
            "last": last,
            "prev_close": closes[i_1d] if i_1d >= 0 else None,
            "pct_1d": pct_from(i_1d),
            "pct_1w": pct_from(i_1w),
            "pct_1m": pct_from(i_1m),
            "pct_ytd": pct_from(i_ytd),
            "abs_1d": abs_from(i_1d),
            "abs_1w": abs_from(i_1w),
            "abs_1m": abs_from(i_1m),
            "abs_ytd": abs_from(i_ytd),
            "n_points": len(closes),
            "sigma_20d": sigma,
            "currency": meta.get("currency"),
            "name": meta.get("shortName") or symbol,
            "market_time": meta.get("regularMarketTime"),
            "fetched_at": dt.datetime.now(UTC).isoformat(timespec="seconds"),
        })

        # How unusual is today's move, in standard deviations?
        if sigma and out["pct_1d"] is not None and sigma > 0:
            out["zscore"] = out["pct_1d"] / sigma
        return out
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out


def fill_from_local_history(row: dict) -> dict:
    """
    Derive change columns from the history this script has recorded itself.
    Used only for instruments where the provider returns a price but no series.
    """
    hist = row.get("hist") or {}
    if len(hist) < 2:
        return row
    dates = sorted(hist)
    last = row["last"]
    today = dt.date.fromisoformat(dates[-1])

    def nearest(days_back: int):
        target = today - dt.timedelta(days=days_back)
        best, best_gap = None, None
        for d in dates[:-1]:
            gap = abs((dt.date.fromisoformat(d) - target).days)
            if best_gap is None or gap < best_gap:
                best, best_gap = d, gap
        # Only trust it if we actually have a reading near that date.
        if best is None or best_gap > max(3, days_back * 0.4):
            return None
        return hist[best]

    for key, days in (("1d", 1), ("1w", 7), ("1m", 30)):
        ref = nearest(days)
        if ref:
            row[f"pct_{key}"] = (last / ref - 1.0) * 100.0
            row[f"abs_{key}"] = last - ref
    row["local_history"] = True
    return row


def fetch_all_quotes(symbols: list[str]) -> dict:
    """Fetch every symbol in parallel, then fall back to cache for failures."""
    cache = load_json(MARKET_CACHE, {})
    results: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_quote, s): s for s in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                results[sym] = fut.result()
            except Exception as exc:
                results[sym] = {"symbol": sym, "ok": False, "error": str(exc)}

    today_key = now_bkk().date().isoformat()

    for sym in symbols:
        row = results.get(sym, {"symbol": sym, "ok": False, "error": "not fetched"})
        if row.get("ok"):
            # Keep our own rolling price history. Some instruments (the Thai SET
            # index, for one) return a live level but no history from Yahoo at
            # any range, so we build one ourselves: after a few days of running,
            # their change columns start working.
            hist = dict((cache.get(sym) or {}).get("hist") or {})
            hist[today_key] = row["last"]
            row["hist"] = dict(sorted(hist.items())[-420:])
            if row.get("pct_1d") is None and len(row["hist"]) > 1:
                row = fill_from_local_history(row)
            cache[sym] = row  # refresh the cache with a good value
        else:
            stale = cache.get(sym)
            if stale and stale.get("ok"):
                # Rule 2: a stale labelled number beats a blank.
                row = dict(stale)
                row["source"] = "cache"
                row["stale_age"] = age_label(stale.get("fetched_at"))
                row["ok"] = True
                row["degraded"] = True
            else:
                row["source"] = "missing"
            results[sym] = row

    save_json(MARKET_CACHE, cache)
    return results


def fetch_oil_curve(months: int = 6) -> list[dict]:
    """
    WTI futures for the next few months, to read the curve shape.

    Front month above later months = backwardation = tight physical market.
    Front month below later months = contango = oversupply.

    Contract codes are generated from today's date, so this never expires.
    """
    today = now_bkk().date()
    symbols = []
    for i in range(1, months + 2):
        m = today.month + i
        y = today.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        symbols.append(f"CL{MONTH_CODES[m]}{str(y)[2:]}.NYM")

    curve = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_quote, s): s for s in symbols}
        got = {}
        for fut in as_completed(futures):
            try:
                got[futures[fut]] = fut.result()
            except Exception:
                pass
    for s in symbols:  # keep calendar order
        q = got.get(s)
        if q and q.get("ok"):
            curve.append({"symbol": s, "price": q["last"], "label": s[2:5]})
        if len(curve) >= months:
            break
    return curve


def curve_shape(curve: list[dict]) -> tuple[str, str]:
    """Describe the oil curve in plain language."""
    if len(curve) < 2:
        return ("unknown", "Not enough contracts returned data today.")
    spread = curve[0]["price"] - curve[-1]["price"]
    span = f"{curve[0]['label']}→{curve[-1]['label']}"
    if spread > 0.5:
        return ("backwardation",
                f"Backwardation ({span}: {spread:+.2f}). Oil for delivery now costs "
                f"more than later — buyers are paying a premium for prompt barrels, "
                f"which signals a physically tight market.")
    if spread < -0.5:
        return ("contango",
                f"Contango ({span}: {spread:+.2f}). Later barrels cost more than "
                f"prompt ones — the market is comfortably supplied, often a sign of "
                f"building inventories.")
    return ("flat", f"Flat curve ({span}: {spread:+.2f}). Supply and demand "
                    f"are roughly balanced.")


def thai_gold_price(gold_usd_oz: float | None, usdthb: float | None) -> dict | None:
    """
    Indicative Thai gold price in baht per baht-weight (15.244 g at 96.5%).
    This is the arithmetic conversion only — actual shop quotes from the Gold
    Traders Association include a dealer premium.
    """
    if not gold_usd_oz or not usdthb:
        return None
    usd_per_gram = gold_usd_oz / GRAMS_PER_TROY_OZ
    usd_per_baht_weight = usd_per_gram * BAHT_WEIGHT_G * THAI_PURITY
    return {
        "thb_per_baht_weight": usd_per_baht_weight * usdthb,
        "thb_per_gram": usd_per_gram * usdthb,
    }


# ============================================================================
# FRED — official US economic data (free key required)
# ============================================================================

def fetch_release_calendar(key: str | None, days_ahead: int = 120) -> list[dict]:
    """
    Upcoming US data releases, straight from FRED's official calendar.

    This is why the "Coming up" box never needs maintaining: the dates come from
    the agencies themselves, not from anything typed into this file. Falls back
    to the last good fetch if FRED is unreachable.
    """
    if not key:
        return []
    today = now_bkk().date()
    horizon = today + dt.timedelta(days=days_ahead)
    out: list[dict] = []

    def one(release_id: int, label: str) -> list[dict]:
        resp = http_get("https://api.stlouisfed.org/fred/release/dates", params={
            "release_id": release_id, "api_key": key, "file_type": "json",
            "realtime_start": today.isoformat(),
            "include_release_dates_with_no_data": "true",
            "sort_order": "asc", "limit": 12,
        }, timeout=15)
        if resp.status_code != 200:
            return []
        rows = []
        for item in resp.json().get("release_dates", []):
            try:
                d = dt.date.fromisoformat(item["date"])
            except Exception:
                continue
            if not (today <= d <= horizon):
                continue
            # 08:30 in New York, expressed in Bangkok time for that exact date,
            # so US daylight saving is handled without us thinking about it.
            bkk = dt.datetime.combine(d, RELEASE_TIME_ET, NEW_YORK).astimezone(BKK)
            rows.append({"date": d.isoformat(),
                         "time_note": f"{bkk:%H:%M} BKK",
                         "label": label})
        return rows

    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(one, rid, lbl) for rid, lbl in CALENDAR_RELEASES]
            for fut in as_completed(futures):
                try:
                    out.extend(fut.result())
                except Exception:
                    continue
    except Exception:
        pass

    if out:
        save_json(CALENDAR_CACHE, {"fetched_at": dt.datetime.now(UTC).isoformat(timespec="seconds"),
                                   "events": out})
        return out
    cached = load_json(CALENDAR_CACHE, {}).get("events", [])
    return [e for e in cached if e.get("date", "") >= today.isoformat()]


def fred_key() -> str | None:
    key = os.environ.get("FRED_API_KEY", "").strip()
    if key:
        return key
    try:
        key = FRED_KEY_FILE.read_text().strip()
        return key or None
    except Exception:
        return None


def fetch_fred_series(series_id: str, transform: str, key: str) -> dict:
    """Latest observation for one FRED series, plus the comparison we need."""
    out = {"id": series_id, "ok": False, "error": None}
    try:
        resp = http_get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": series_id, "api_key": key, "file_type": "json",
                    "sort_order": "desc", "limit": 400},
        )
        if resp.status_code != 200:
            out["error"] = f"http {resp.status_code}"
            return out
        obs = resp.json().get("observations", [])
        clean = [(o["date"], float(o["value"])) for o in obs
                 if o.get("value") not in (".", "", None)]
        if not clean:
            out["error"] = "no observations"
            return out

        date, value = clean[0]
        out.update({"ok": True, "date": date, "raw": value,
                    "prev_raw": clean[1][1] if len(clean) > 1 else None})

        if transform == "level":
            out["value"] = value
            out["prev"] = clean[1][1] if len(clean) > 1 else None
        elif transform == "yoy":
            # Find the observation ~12 months before the latest.
            target = dt.date.fromisoformat(date) - dt.timedelta(days=365)
            best = min(clean, key=lambda kv: abs((dt.date.fromisoformat(kv[0]) - target).days))
            out["value"] = (value / best[1] - 1.0) * 100.0 if best[1] else None
            # Previous month's YoY, for direction.
            if len(clean) > 1:
                d2, v2 = clean[1]
                t2 = dt.date.fromisoformat(d2) - dt.timedelta(days=365)
                b2 = min(clean, key=lambda kv: abs((dt.date.fromisoformat(kv[0]) - t2).days))
                out["prev"] = (v2 / b2[1] - 1.0) * 100.0 if b2[1] else None
        elif transform == "thousands":
            out["value"] = value / 1000.0
            out["prev"] = clean[1][1] / 1000.0 if len(clean) > 1 else None
        elif transform == "trillions":
            out["value"] = value / 1_000_000.0   # FRED reports millions
            out["prev"] = clean[1][1] / 1_000_000.0 if len(clean) > 1 else None
        elif transform == "chg":
            out["value"] = value - clean[1][1] if len(clean) > 1 else None
            out["prev"] = clean[1][1] - clean[2][1] if len(clean) > 2 else None
        # How old is this reading, allowing for how often it is published?
        try:
            age_days = (dt.date.today() - dt.date.fromisoformat(date)).days
            out["age_days"] = age_days
            limit = STALE_LIMIT_DAYS.get(transform_frequency(age_days), 120)
            out["stale"] = age_days > limit
        except Exception:
            out["stale"] = False

        out["fetched_at"] = dt.datetime.now(UTC).isoformat(timespec="seconds")
        return out
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out


def transform_frequency(age_days: int) -> str:
    """Rough guess at a series' publication rhythm from how old its last point is."""
    if age_days <= 10:
        return "daily"
    if age_days <= 45:
        return "monthly"
    return "quarterly"


# A reading older than this is almost certainly a dead series, not news.
STALE_LIMIT_DAYS = {"daily": 14, "monthly": 75, "quarterly": 210}


def fetch_fred_block(spec: list[tuple], key: str | None) -> list[dict]:
    if not key:
        return []
    rows = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_fred_series, sid, tr, key): (sid, label, tr, unit)
                   for sid, label, tr, unit in spec}
        got = {}
        for fut in as_completed(futures):
            sid, label, tr, unit = futures[fut]
            try:
                got[sid] = (fut.result(), label, unit)
            except Exception:
                pass
    for sid, label, tr, unit in spec:
        if sid in got:
            row, lbl, un = got[sid]
            row["label"] = lbl
            row["unit"] = un
            rows.append(row)
    return rows


# ============================================================================
# CFTC POSITIONING — free, keyless, official (weekly)
# ============================================================================

def fetch_cftc() -> list[dict]:
    """
    Managed-money (non-commercial) net positioning for major futures.
    Tells you whether speculators are already crowded into a trade.
    """
    rows = []
    base = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
    for contract, label in CFTC_MARKETS:
        try:
            resp = http_get(base, params={
                "$select": ("report_date_as_yyyy_mm_dd,noncomm_positions_long_all,"
                            "noncomm_positions_short_all,open_interest_all"),
                "$where": f"market_and_exchange_names='{contract}'",
                "$order": "report_date_as_yyyy_mm_dd DESC",
                "$limit": 8,
            }, timeout=20)
            if resp.status_code != 200:
                continue
            obs = resp.json()
            if not obs:
                continue

            def net(o):
                return int(float(o["noncomm_positions_long_all"])) - \
                       int(float(o["noncomm_positions_short_all"]))

            latest = obs[0]
            net_now = net(latest)
            net_prev = net(obs[1]) if len(obs) > 1 else None
            net_4w = net(obs[4]) if len(obs) > 4 else None
            rows.append({
                "label": label,
                "date": latest["report_date_as_yyyy_mm_dd"][:10],
                "net": net_now,
                "chg_1w": (net_now - net_prev) if net_prev is not None else None,
                "chg_4w": (net_now - net_4w) if net_4w is not None else None,
                "open_interest": int(float(latest.get("open_interest_all", 0) or 0)),
            })
        except Exception:
            continue
    return rows



# ============================================================================
# RELATIONSHIPS — is gold still trading the way it normally does?
#
# Gold usually moves inversely to real yields and to the dollar. When those
# relationships break, the usual playbook stops working and something
# structural is going on (central-bank buying, debasement fear, a squeeze).
# Showing the correlation is what turns a price into an explanation.
# ============================================================================

CORRELATION_WINDOW = 60  # trading days


def fetch_price_series(symbol: str) -> dict:
    """Daily closes as {date: close}. Separate from fetch_quote, which summarises."""
    try:
        resp = http_get(YAHOO_CHART.format(requests.utils.quote(symbol, safe="")),
                        params={"range": "1y", "interval": "1d"})
        if resp.status_code != 200:
            return {}
        res = (resp.json().get("chart") or {}).get("result")
        if not res:
            return {}
        stamps = res[0].get("timestamp") or []
        closes = ((res[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        return {dt.datetime.fromtimestamp(t, UTC).date().isoformat(): float(c)
                for t, c in zip(stamps, closes) if c is not None}
    except Exception:
        return {}


def fetch_fred_series_full(series_id: str, key: str) -> dict:
    """Daily observations as {date: value}."""
    try:
        resp = http_get("https://api.stlouisfed.org/fred/series/observations",
                        params={"series_id": series_id, "api_key": key,
                                "file_type": "json", "sort_order": "desc", "limit": 400})
        if resp.status_code != 200:
            return {}
        return {o["date"]: float(o["value"]) for o in resp.json().get("observations", [])
                if o.get("value") not in (".", "", None)}
    except Exception:
        return {}


def correlation(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation. Returns None when there is not enough to say."""
    n = len(xs)
    if n < 20:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    vx = math.sqrt(sum((a - mx) ** 2 for a in xs))
    vy = math.sqrt(sum((b - my) ** 2 for b in ys))
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy)


def daily_changes(series: dict, dates: list[str]) -> list[float]:
    """Day-on-day change for the given dates. Correlating levels would be junk."""
    out = []
    for prev, cur in zip(dates, dates[1:]):
        a, b = series.get(prev), series.get(cur)
        if a is None or b is None or a == 0:
            out.append(None)
        else:
            out.append(b - a)
    return out


def build_relationships(key: str | None) -> list[dict]:
    """Gold's rolling correlation with the things that are supposed to drive it."""
    gold = fetch_price_series("GC=F")
    if not gold:
        return []

    others = {"Real 10-year yield": fetch_fred_series_full("DFII10", key) if key else {},
              "Dollar index (DXY)": fetch_price_series("DX-Y.NYB"),
              "US 10-year yield": fetch_price_series("^TNX"),
              "Silver": fetch_price_series("SI=F")}

    rows = []
    for label, series in others.items():
        if not series:
            continue
        shared = sorted(set(gold) & set(series))[-(CORRELATION_WINDOW + 1):]
        if len(shared) < 25:
            continue
        g = daily_changes(gold, shared)
        o = daily_changes(series, shared)
        pairs = [(a, b) for a, b in zip(g, o) if a is not None and b is not None]
        if len(pairs) < 20:
            continue
        corr = correlation([a for a, _ in pairs], [b for _, b in pairs])
        if corr is None:
            continue
        rows.append({"label": label, "corr": corr, "days": len(pairs),
                     "reading": describe_correlation(label, corr)})
    return rows


def describe_correlation(label: str, c: float) -> str:
    """Say in plain words what the number means for someone learning."""
    strength = ("strongly" if abs(c) >= 0.5 else
                "moderately" if abs(c) >= 0.25 else "barely")
    direction = "with" if c > 0 else "against"

    if "yield" in label.lower() or "Dollar" in label:
        # These normally move opposite to gold.
        if c <= -0.25:
            return f"Normal: gold is moving {strength} {direction} it, as textbook says."
        if c >= 0.25:
            return ("Unusual: gold is rising WITH it, which the textbook says "
                    "should not happen. Often a sign of central-bank buying or "
                    "debasement fear rather than ordinary rate trading.")
        return "Relationship has gone quiet — gold is being driven by something else."
    if "Silver" in label:
        if c >= 0.5:
            return "Normal: the precious metals complex is moving together."
        return "Gold and silver have decoupled — usually means a gold-specific story."
    return ""



# ============================================================================
# NEWS
# ============================================================================

def normalize_title(title: str) -> str:
    """Lowercase, strip accents/punctuation — for fuzzy duplicate matching."""
    t = unicodedata.normalize("NFKD", title or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    # Google News appends " - Publisher"; drop a trailing source attribution.
    return t


SEPARATORS = (" - ", " — ", " – ", " | ")


def split_source_suffix(title: str, passes: int = 2) -> tuple[str, str | None]:
    """
    'Headline - News-Times' -> ('Headline', 'News-Times').

    Google News appends the publisher to every headline, so this both cleans the
    title and recovers the real publisher — which is what should be credited,
    rather than the name of our search query. We split on the LAST separator so
    publishers containing a hyphen survive intact, and allow a second pass
    because aggregators sometimes stack two attributions.
    """
    title = (title or "").strip()
    publisher = None
    for _ in range(passes):
        cut = max((title.rfind(sep), sep) for sep in SEPARATORS)
        idx, sep = cut
        if idx < 0:
            break
        head, tail = title[:idx].strip(), title[idx + len(sep):].strip()
        # Guard against chopping a headline that genuinely contains a dash.
        if len(head) < 25 or not looks_like_publisher(tail):
            break
        title, publisher = head, publisher or tail
    return title, publisher


def looks_like_publisher(text: str) -> bool:
    """
    Is this trailing fragment a masthead, or the rest of the headline?

    'Reuters', 'News-Times', 'UA.NEWS' -> yes.
    'what it means for your mortgage'  -> no, that is the actual headline.
    """
    if not (2 <= len(text) <= 45):
        return False
    words = text.split()
    if not words or len(words) > 5:
        return False
    if text[-1] in ".,;:!?" and not text.isupper():
        return False
    # Mastheads are proper nouns: nearly every word is capitalised.
    capitalised = sum(1 for w in words if w[:1].isupper() or w[:1].isdigit())
    return capitalised >= max(1, len(words) - 1)


def tag_themes(text: str) -> list[str]:
    low = f" {text.lower()} "
    tags = []
    for key, cfg in THEMES.items():
        if any(k in low for k in cfg["kw"]):
            tags.append(key)
    return tags


def clean_snippet(raw: str, limit: int = 200) -> str:
    """Feed summary only, tags removed, cut to `limit` characters."""
    if not raw:
        return ""
    txt = re.sub(r"<[^>]+>", " ", raw)
    txt = html.unescape(txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    if len(txt) > limit:
        txt = txt[:limit].rsplit(" ", 1)[0] + "…"
    return txt


def fetch_feed(feed: dict, group: str, window_override: int | None) -> dict:
    """Fetch and parse one feed. Never raises."""
    out = {"feed": feed, "group": group, "items": [], "error": None}
    window_h = window_override or feed.get("window_h", 24)
    cutoff = dt.datetime.now(UTC) - dt.timedelta(hours=window_h)
    try:
        resp = http_get(feed["url"])
        if resp.status_code != 200:
            out["error"] = f"http {resp.status_code}"
            return out
        parsed = feedparser.parse(resp.content)
        if not parsed.entries:
            out["error"] = "no entries"
            return out

        is_aggregator = "news.google.com" in feed["url"]

        for entry in parsed.entries[:60]:
            link = entry.get("link") or ""
            title, publisher = split_source_suffix(entry.get("title") or "")
            if not title or not link:
                continue
            # Credit the actual publisher, not the name of our search query.
            source_name = publisher if (is_aggregator and publisher) else feed["name"]

            tp = entry.get("published_parsed") or entry.get("updated_parsed")
            when = dt.datetime(*tp[:6], tzinfo=UTC) if tp else None
            # Nikkei Asia serves no parseable dates — keep such items but mark
            # them undated so they never crowd out the top stories.
            if when is None:
                undated = True
            else:
                undated = False
                if when < cutoff:
                    continue

            text = f"{title} {clean_snippet(entry.get('summary', ''), 400)}"
            out["items"].append({
                "title": title,
                "link": link,
                "source": source_name,
                "group": group,
                "paywall": feed.get("paywall", "none"),
                "primary": bool(feed.get("primary")),
                "when": when.isoformat() if when else None,
                "undated": undated,
                "snippet": clean_snippet(entry.get("summary", "")),
                "themes": tag_themes(text),
                "norm": normalize_title(title),
            })
        return out
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out


def fetch_all_news(window_override: int | None) -> tuple[list[dict], list[dict], list[str]]:
    """Returns (clusters, failed_feeds, ok_feed_names)."""
    jobs = [(f, g) for g, lst in FEEDS.items() for f in lst]
    raw_items: list[dict] = []
    failed: list[dict] = []
    ok_names: list[str] = []

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_feed, f, g, window_override): (f, g) for f, g in jobs}
        for fut in as_completed(futures):
            feed, group = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:
                failed.append({"name": feed["name"], "error": str(exc)})
                continue
            if res["error"]:
                failed.append({"name": feed["name"], "error": res["error"]})
            else:
                ok_names.append(feed["name"])
                raw_items.extend(res["items"])

    return cluster_items(raw_items), failed, ok_names


PAYWALL_RANK = {"none": 0, "soft": 1, "hard": 2}

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "with",
    "as", "at", "by", "from", "is", "are", "was", "were", "be", "been", "it",
    "its", "that", "this", "these", "those", "has", "have", "had", "will",
    "says", "said", "say", "new", "after", "over", "amid", "up", "down", "more",
    "than", "into", "about", "could", "would", "may", "can", "not", "no",
}


def content_tokens(norm_title: str) -> set:
    """Meaningful words only — the fingerprint used to spot the same story."""
    return {w for w in norm_title.split() if len(w) > 3 and w not in STOPWORDS}


def cluster_items(items: list[dict]) -> list[dict]:
    """
    Group near-identical headlines from different sources into one story.
    The clickable headline becomes the least paywalled source in the cluster.
    """
    # Newest first so the lead item of each cluster is the freshest.
    items.sort(key=lambda it: (it["when"] or ""), reverse=True)
    clusters: list[dict] = []

    for item in items:
        tokens = content_tokens(item["norm"])
        placed = False
        for cl in clusters:
            # Two tests, because wire stories get reworded between outlets:
            # character similarity catches light edits, shared-keyword overlap
            # catches the same story written from scratch.
            if SequenceMatcher(None, cl["norm"], item["norm"]).ratio() >= 0.80:
                placed = True
            elif tokens and cl["tokens"]:
                overlap = len(tokens & cl["tokens"]) / min(len(tokens), len(cl["tokens"]))
                if overlap >= 0.70 and len(tokens & cl["tokens"]) >= 3:
                    placed = True
            if placed:
                cl["members"].append(item)
                cl["tokens"] |= tokens
                break
        if not placed:
            clusters.append({"norm": item["norm"], "tokens": tokens, "members": [item]})

    out = []
    for cl in clusters:
        members = cl["members"]
        # Pick the lead: free sources first, then primary sources, then newest.
        lead = sorted(members, key=lambda m: (
            PAYWALL_RANK.get(m["paywall"], 3),
            0 if m["primary"] else 1,
            -(dt.datetime.fromisoformat(m["when"]).timestamp() if m["when"] else 0),
        ))[0]
        themes = sorted({t for m in members for t in m["themes"]})
        seen_sources, others = {lead["source"]}, []
        for m in members:
            if m["source"] not in seen_sources:
                seen_sources.add(m["source"])
                others.append(m)
        out.append({
            **lead,
            "themes": themes,
            "n_sources": len(seen_sources),
            "others": others[:4],
        })
    return out


def score_cluster(cl: dict, seen_links: set[str]) -> float:
    """Rank for the top-stories box."""
    score = 0.0
    score += 2.4 * (cl["n_sources"] - 1)            # corroboration across sources
    for theme in cl["themes"]:
        score += RANK_WEIGHTS.get(theme, 0.5)        # subject-matter weight
    low = cl["title"].lower()
    score += 2.0 * sum(1 for k in HIGH_IMPACT_KW if k in low)
    if cl["primary"]:
        score += 2.5                                 # straight from a central bank
    if cl["when"]:
        hrs = (dt.datetime.now(UTC) - dt.datetime.fromisoformat(cl["when"])).total_seconds() / 3600
        score += max(0.0, 3.0 - hrs / 8.0)           # recency
    if cl["undated"]:
        score -= 4.0
    if cl["link"] in seen_links:
        score -= 6.0                                 # you have already seen this
    return score


# ============================================================================
# "WHAT CHANGED SINCE YOU LAST LOOKED"
# ============================================================================

def build_change_summary(quotes: dict, snapshot: dict) -> dict:
    """Compare today's prices with the snapshot saved on the previous run."""
    # Compare against the most recent build that is genuinely OLD enough to be
    # interesting. The script may run hourly, so comparing with "last run" would
    # mean comparing with an hour ago and showing a wall of +0.00%. We reach back
    # through the saved snapshots for one at least COMPARE_AGAINST_HOURS old,
    # which makes this box behave the same whatever the schedule.
    history = snapshot.get("snapshots") or []
    if not history and snapshot.get("prices"):
        history = [snapshot]  # migrate the old single-snapshot format

    now = dt.datetime.now(UTC)
    chosen = None
    for snap in sorted(history, key=lambda s: s.get("built_at") or "", reverse=True):
        try:
            age_h = (now - dt.datetime.fromisoformat(snap["built_at"])).total_seconds() / 3600
        except Exception:
            continue
        if age_h >= COMPARE_AGAINST_HOURS:
            chosen = snap
            break
    if not chosen:
        return {"since": None, "moves": []}

    prev = chosen.get("prices", {})
    prev_time = chosen.get("built_at")
    moves = []
    for sym, label, kind in TICKERS["headline"]:
        q = quotes.get(sym)
        if not q or not q.get("ok") or sym not in prev:
            continue
        before, nowv = prev[sym], q["last"]
        if not before:
            continue

        # Display units differ by instrument, but ranking must not: basis points
        # and percent are not comparable numbers. We rank on the percentage move
        # scaled by the instrument's own volatility, which is the same "is this
        # move actually big?" test used by the sigma flags elsewhere.
        pct_delta = (nowv / before - 1.0) * 100.0
        if kind == "yield":
            text = f"{(nowv - before) * 100.0:+.0f} bp"
        elif kind == "ffr":
            text = f"{((100 - nowv) - (100 - before)) * 100.0:+.0f} bp"
        else:
            text = f"{pct_delta:+.2f}%"

        sigma = q.get("sigma_20d")
        rank = abs(pct_delta) / sigma if sigma else abs(pct_delta)
        moves.append({"label": label, "text": text, "dir": pct_class(pct_delta),
                      "rank": rank, "pct": pct_delta})
    moves = [m for m in moves if abs(m["pct"]) >= 0.01]  # drop unchanged rows
    moves.sort(key=lambda m: m["rank"], reverse=True)
    return {"since": prev_time, "moves": moves[:6]}


def unusual_moves(quotes: dict, threshold: float = 2.0) -> list[dict]:
    """Instruments whose daily move is large relative to their recent volatility."""
    flagged = []
    labels = {sym: lbl for grp in TICKERS.values() for sym, lbl, _ in grp}
    for sym, q in quotes.items():
        z = q.get("zscore")
        if q.get("ok") and z is not None and abs(z) >= threshold:
            flagged.append({"label": labels.get(sym, sym), "pct": q.get("pct_1d"),
                            "z": z, "symbol": sym})
    flagged.sort(key=lambda f: abs(f["z"]), reverse=True)
    return flagged[:6]


# ============================================================================
# DEMO DATA
# ============================================================================

def demo_payload() -> dict:
    """Plausible fake data so the layout can be checked without internet."""
    import random
    random.seed(11)
    quotes = {}
    base = {
        "^GSPC": 6180, "^IXIC": 20350, "^DJI": 51100, "^RUT": 2830,
        "^STOXX50E": 6240, "^FTSE": 9120, "^GDAXI": 24300, "^N225": 68300,
        "^HSI": 23970, "000001.SS": 3842, "^KS11": 7003, "^NSEI": 22420,
        "^AXJO": 8682, "^SET.BK": 1571, "EEM": 67.7, "^IRX": 3.99, "^FVX": 5.05,
        "^TNX": 5.28, "^TYX": 5.63, "ZQ=F": 96.07, "ZN=F": 104.36, "TLT": 77.5,
        "DX-Y.NYB": 101.9, "EURUSD=X": 1.1257, "JPY=X": 157.8, "CNY=X": 6.70,
        "KRW=X": 1342, "INR=X": 96.3, "THB=X": 33.53, "CL=F": 91.1, "BZ=F": 102.3,
        "NG=F": 3.03, "^OVX": 51.0, "GC=F": 4162, "SI=F": 60.4, "HG=F": 6.55,
        "ALI=F": 3214, "ZW=F": 683, "ZC=F": 497, "ZS=F": 1278, "^VIX": 15.3,
        "^VVIX": 87.0, "^GVZ": 23.2, "HYG": 76.9, "LQD": 101.8, "BTC-USD": 85069,
    }
    for sym, px in base.items():
        d1 = random.uniform(-1.8, 1.8)
        quotes[sym] = {
            "symbol": sym, "ok": True, "source": "demo", "last": px,
            "prev_close": px / (1 + d1 / 100), "pct_1d": d1,
            "pct_1w": d1 * random.uniform(0.5, 2.5),
            "pct_1m": d1 * random.uniform(1.0, 4.0),
            "pct_ytd": random.uniform(-14, 26),
            "abs_1d": px * d1 / 100, "sigma_20d": abs(d1) / random.uniform(0.4, 2.2),
            "zscore": random.uniform(-2.8, 2.8), "name": sym,
            "fetched_at": dt.datetime.now(UTC).isoformat(timespec="seconds"),
        }
    curve = [{"symbol": f"CL{c}26.NYM", "label": lbl, "price": p}
             for c, lbl, p in [("X", "X26", 91.1), ("Z", "Z26", 89.4),
                               ("F", "F27", 87.8), ("H", "H27", 84.7),
                               ("K", "K27", 82.0), ("M", "M27", 80.9)]]
    heads = [
        ("Fed holds rates steady, signals patience on further cuts", "US Federal Reserve", "none", True, ["policy"]),
        ("US core inflation cools to 2.4%, below forecasts", "Guardian Business", "none", False, ["inflation", "policy"]),
        ("OPEC+ extends output curbs through the first quarter", "Reuters / wires", "none", False, ["energy", "trade"]),
        ("China factory activity contracts for a third month", "SCMP Economy", "soft", False, ["asia", "growth"]),
        ("ECB officials split on timing of next move", "European Central Bank", "none", True, ["policy"]),
        ("Thai baht strengthens as tourism receipts beat estimates", "Bangkok Post Business", "none", False, ["thailand", "asia"]),
        ("Gold holds near record as real yields slip", "MarketWatch", "soft", False, ["gold", "policy"]),
        ("Wall Street closes higher as megacap earnings beat", "BBC Business", "none", False, ["equities"]),
        ("New tariffs on steel imports take effect Monday", "Investing.com Economy", "none", False, ["trade"]),
        ("Euro-area unemployment steady at record low", "Guardian Business", "none", False, ["growth"]),
        ("Japan wage talks point to third year of large raises", "Nikkei Asia", "hard", False, ["asia", "inflation"]),
        ("US jobless claims edge up from historic lows", "CNBC Economy", "none", False, ["growth"]),
    ]
    clusters = []
    for i, (title, source, pw, primary, themes) in enumerate(heads):
        clusters.append({
            "title": title, "link": f"https://example.com/story{i}", "source": source,
            "group": "Global economy & markets", "paywall": pw, "primary": primary,
            "when": (dt.datetime.now(UTC) - dt.timedelta(hours=i * 1.6)).isoformat(),
            "undated": False,
            "snippet": "Demo snippet — this is placeholder text used only by --demo "
                       "so the layout can be checked without an internet connection.",
            "themes": themes, "norm": normalize_title(title),
            "n_sources": 1 + (i % 3), "others": [],
        })
    fred_rates = [{"id": sid, "ok": True, "label": lbl, "unit": un, "value": v,
                   "prev": v - 0.04, "date": "2026-10-02"}
                  for (sid, lbl, _, un), v in zip(
                      FRED_RATES, [1.82, 2.38, 4.61, 0.67, 3.21, 0.98, 4.00])]
    fred_data = [{"id": sid, "ok": True, "label": lbl, "unit": un, "value": v,
                  "prev": v - 0.1, "date": "2026-09-30"}
                 for (sid, lbl, _, un), v in zip(
                     FRED_DATA, [2.9, 2.4, 2.3, 4.3, 118.0, 3.4, 1.1, 2.0])]
    cftc = [{"label": l, "date": "2026-09-29", "net": n, "chg_1w": c1, "chg_4w": c4,
             "open_interest": oi}
            for l, n, c1, c4, oi in [
                ("Gold", 214_500, 8_300, -12_400, 540_000),
                ("Silver", 61_200, -2_100, 9_800, 150_000),
                ("Copper", 38_400, 1_400, 5_200, 210_000),
                ("WTI crude", 178_900, -9_600, 24_300, 1_900_000),
                ("Natural gas", -62_300, 4_100, -8_700, 1_200_000),
                ("S&P 500", -41_000, 12_000, -30_500, 2_400_000),
                ("Euro", 94_300, -3_300, 11_900, 700_000)]]
    return {"quotes": quotes, "curve": curve, "clusters": clusters,
            "fred_rates": fred_rates, "fred_data": fred_data, "cftc": cftc,
            "failed": [{"name": "Demo feed that failed", "error": "http 500"}],
            "ok_feeds": [f["name"] for lst in FEEDS.values() for f in lst][:14]}


# ============================================================================
# HTML RENDERING
# ============================================================================

CSS = """
:root{
  --bg:#fbfaf8; --panel:#ffffff; --ink:#15171a; --muted:#5d6672; --faint:#8b95a3;
  --line:#e4e2dd; --line-soft:#efedea; --up:#0a7d4f; --down:#c0392b; --flat:#6b7280;
  --accent:#1f4e79; --accent-soft:#eaf1f8; --warn:#9a6700; --warn-soft:#fdf6e3;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#111317; --panel:#181b20; --ink:#e8eaed; --muted:#a2abb8; --faint:#78818f;
    --line:#2a2e36; --line-soft:#22262d; --up:#3ecf8e; --down:#ff6b5e; --flat:#8b95a3;
    --accent:#7fb3e3; --accent-soft:#1b2838; --warn:#e0b350; --warn-soft:#2a2418;
  }
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
  font-size:16px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1040px;margin:0 auto;padding:28px 16px 64px}
a{color:var(--accent);text-decoration:none;border-bottom:1px solid transparent}
a:hover{border-bottom-color:currentColor}
a:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:2px}
h1{font-size:1.65rem;line-height:1.2;margin:0 0 4px;letter-spacing:-.02em}
h2{font-size:1.12rem;margin:0 0 2px;letter-spacing:-.01em}
h3{font-size:.95rem;margin:0 0 10px;color:var(--muted);font-weight:600}
.sub{color:var(--muted);font-size:.86rem;margin:0}
header.top{border-bottom:2px solid var(--ink);padding-bottom:14px;margin-bottom:22px}
.tier{margin:0 0 30px}
.tier-label{display:flex;align-items:baseline;gap:10px;border-bottom:1px solid var(--line);
  padding-bottom:7px;margin:0 0 16px}
.tier-label .n{font:600 .72rem/1 var(--mono);color:var(--faint);letter-spacing:.09em;
  text-transform:uppercase;white-space:nowrap}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:16px 18px;margin:0 0 16px}
.card.accent{background:var(--accent-soft);border-color:transparent}
.card.warn{background:var(--warn-soft);border-color:transparent}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums;font-feature-settings:"tnum"}
.up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--flat)}
table{width:100%;border-collapse:collapse;font-size:.88rem}
.tscroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
th{text-align:right;font-weight:600;color:var(--muted);font-size:.74rem;
  text-transform:uppercase;letter-spacing:.05em;padding:0 0 7px;
  border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child{text-align:left}
td{padding:7px 0;border-bottom:1px solid var(--line-soft);text-align:right;white-space:nowrap}
td:first-child{text-align:left;white-space:normal;padding-right:14px}
tr:last-child td{border-bottom:none}
td.v{font-family:var(--mono);font-variant-numeric:tabular-nums}
th+th,td+td{padding-left:14px}
.sig{display:inline-block;margin-left:6px;font:600 .66rem/1 var(--mono);
  color:var(--warn);border:1px solid currentColor;border-radius:3px;padding:2px 4px;
  vertical-align:middle}
.tape{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.tape div{background:var(--panel);padding:11px 13px}
.tape .k{font-size:.72rem;color:var(--muted);text-transform:uppercase;
  letter-spacing:.04em;margin-bottom:3px}
.tape .p{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:1.1rem;
  font-weight:600;letter-spacing:-.01em}
.tape .c{font-family:var(--mono);font-size:.8rem;margin-top:1px}
ol.stories{list-style:none;counter-reset:s;margin:0;padding:0}
ol.stories li{counter-increment:s;position:relative;padding:0 0 14px 34px;
  margin-bottom:14px;border-bottom:1px solid var(--line-soft)}
ol.stories li:last-child{border-bottom:none;margin-bottom:0;padding-bottom:0}
ol.stories li::before{content:counter(s);position:absolute;left:0;top:1px;
  font:600 .8rem/1.5 var(--mono);color:var(--faint);width:22px;height:22px;
  text-align:center;border:1px solid var(--line);border-radius:50%}
.hl{font-weight:600;font-size:1rem;line-height:1.38;display:block;margin-bottom:3px}
.why{color:var(--muted);font-size:.87rem;margin:4px 0 0}
.meta{font-size:.76rem;color:var(--faint);margin-top:4px;display:flex;
  flex-wrap:wrap;gap:4px 9px;align-items:center}
.tag{display:inline-block;font-size:.68rem;padding:2px 7px;border-radius:999px;
  background:var(--accent-soft);color:var(--accent);font-weight:600;white-space:nowrap}
.pw{font-size:.68rem;color:var(--warn);border:1px solid currentColor;
  border-radius:3px;padding:1px 4px;font-weight:600}
.seen{font-size:.68rem;color:var(--faint);border:1px solid var(--line);
  border-radius:3px;padding:1px 4px}
.also{font-size:.76rem;color:var(--faint);margin-top:3px}
.filters{display:flex;flex-wrap:wrap;gap:7px;margin:0 0 16px}
.filters button{font:600 .8rem var(--sans);color:var(--muted);background:var(--panel);
  border:1px solid var(--line);border-radius:999px;padding:6px 13px;cursor:pointer}
.filters button:hover{border-color:var(--accent);color:var(--accent)}
.filters button[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);
  color:#fff}
.filters button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.newsgrp{margin:0 0 22px}
ul.items{list-style:none;margin:0;padding:0}
ul.items li{padding:9px 0;border-bottom:1px solid var(--line-soft)}
ul.items li:last-child{border-bottom:none}
.curve{display:flex;gap:1px;background:var(--line);border-radius:8px;overflow:hidden;
  margin:12px 0 10px;border:1px solid var(--line)}
.curve div{flex:1;background:var(--panel);padding:9px 6px;text-align:center}
.curve .m{font:600 .7rem var(--mono);color:var(--muted)}
.curve .q{font:600 .95rem var(--mono);font-variant-numeric:tabular-nums}
.note{font-size:.84rem;color:var(--muted);margin:8px 0 0}
.events{list-style:none;margin:0;padding:0}
.events li{display:flex;gap:12px;padding:7px 0;border-bottom:1px solid var(--line-soft);
  font-size:.88rem}
.events li:last-child{border-bottom:none}
.events .d{font:600 .8rem var(--mono);color:var(--accent);min-width:88px;white-space:nowrap}
.stale{color:var(--warn);font-size:.72rem;margin-left:5px}
footer{margin-top:40px;padding-top:16px;border-top:1px solid var(--line);
  font-size:.79rem;color:var(--faint)}
footer p{margin:5px 0}
.health{font-family:var(--mono);font-size:.74rem}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;
  margin-top:4px}
.kv div{padding:9px 11px;background:var(--bg);border:1px solid var(--line);border-radius:8px}
.kv .k{font-size:.71rem;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.kv .v{font:600 1rem var(--mono);font-variant-numeric:tabular-nums;margin-top:2px}
.corrbar{display:inline-block;width:70px;height:7px;border-radius:4px;
  background:var(--line);overflow:hidden;vertical-align:middle}
.corrbar i{display:block;height:100%;background:var(--accent)}
.hide{display:none !important}
#stale{display:none;margin:0 0 20px;padding:13px 16px;border-radius:10px;
  border:1px solid var(--warn);background:var(--warn-soft);color:var(--ink)}
#stale.show{display:block}
#stale.bad{border-color:var(--down);background:transparent;
  box-shadow:inset 0 0 0 2px var(--down)}
#stale b{display:block;font-size:1rem;margin-bottom:3px}
#stale span{font-size:.86rem;color:var(--muted)}
@media (max-width:560px){
  .wrap{padding:20px 14px 48px} h1{font-size:1.4rem}
  .tape{grid-template-columns:repeat(auto-fit,minmax(132px,1fr))}
  table{font-size:.83rem}
}
@media print{.filters{display:none}body{background:#fff}}
"""

# Runs in the reader's browser, not on any server — which is the whole point.
# If the automatic build stops, nothing server-side is alive to warn you, but
# this still fires the moment you open the page.
STALE_JS_TEMPLATE = """
(function(){
  var built = new Date("__BUILT_ISO__");
  var hours = (Date.now() - built.getTime()) / 3600000;
  // Builds run 06:00-23:00 Bangkok, so an overnight gap of ~7h is normal.
  if (hours < 10) return;
  var el = document.getElementById('stale');
  if (!el) return;
  var age = hours < 48
    ? Math.round(hours) + ' hours'
    : Math.round(hours / 24) + ' days';
  el.className = hours >= 36 ? 'show bad' : 'show';
  el.innerHTML = '<b>This briefing is ' + age + ' old.</b>' +
    '<span>The hourly build has not run. The numbers below are stale \\u2014 ' +
    'check github.com/KhunB00/economic-overview for a failed or paused build.</span>';
})();
"""


JS = """
(function(){
  var btns=document.querySelectorAll('.filters button[data-theme]');
  var items=document.querySelectorAll('[data-themes]');
  function apply(theme){
    btns.forEach(function(b){b.setAttribute('aria-pressed',b.dataset.theme===theme);});
    items.forEach(function(el){
      var show = theme==='all' || (' '+el.dataset.themes+' ').indexOf(' '+theme+' ')>-1;
      el.classList.toggle('hide',!show);
    });
    document.querySelectorAll('.newsgrp').forEach(function(g){
      var any=g.querySelectorAll('[data-themes]:not(.hide)').length;
      g.classList.toggle('hide',any===0);
    });
  }
  btns.forEach(function(b){b.addEventListener('click',function(){apply(b.dataset.theme);});});
  apply('all');
})();
"""


def render_quote_row(sym: str, label: str, kind: str, q: dict) -> str:
    """One row of a market table, with the right units for the instrument type."""
    if not q or not q.get("ok"):
        err = esc((q or {}).get("error") or "no data")
        return (f'<tr><td>{esc(label)}</td><td class="v">n/a</td>'
                f'<td class="v" colspan="4"><span class="stale">{err}</span></td></tr>')

    stale = (f'<span class="stale">{esc(q.get("stale_age", "cached"))}</span>'
             if q.get("degraded") else "")

    if kind == "yield":
        value = f'{q["last"]:.2f}%'
        d1 = q.get("abs_1d")
        c1 = f'{d1 * 100:+.0f} bp' if d1 is not None else "n/a"
        cls1 = pct_class(d1)
    elif kind == "ffr":
        implied = 100.0 - q["last"]
        value = f"{implied:.2f}%"
        d1 = -(q.get("abs_1d") or 0) * 100 if q.get("abs_1d") is not None else None
        c1 = f"{d1:+.0f} bp" if d1 is not None else "n/a"
        cls1 = pct_class(d1)
    else:
        value = fmt_num(q["last"])
        d1 = q.get("pct_1d")
        c1 = f"{d1:+.2f}%" if d1 is not None else "n/a"
        cls1 = pct_class(d1)

    z = q.get("zscore")
    flag = (f'<span class="sig">{abs(z):.1f}&sigma;</span>'
            if z is not None and abs(z) >= 2.0 else "")

    def horizon(key):
        # For yields and the implied policy rate, every horizon belongs in basis
        # points. Showing "+26% YTD" for a bond yield is technically a percent
        # change of a percentage, and badly misleading.
        if kind in ("yield", "ffr"):
            v = q.get(key.replace("pct_", "abs_"))
            if v is None:
                return '<td class="v flat">n/a</td>'
            bp = v * 100 * (-1 if kind == "ffr" else 1)
            return f'<td class="v {pct_class(bp)}">{bp:+.0f} bp</td>'
        v = q.get(key)
        return (f'<td class="v {pct_class(v)}">{v:+.2f}%</td>' if v is not None
                else '<td class="v flat">n/a</td>')

    return (
        f'<tr><td>{esc(label)}{stale}</td>'
        f'<td class="v">{value}</td>'
        f'<td class="v {cls1}">{c1}{flag}</td>'
        f'{horizon("pct_1w")}{horizon("pct_1m")}{horizon("pct_ytd")}</tr>'
    )


def render_table(group: str, quotes: dict) -> str:
    rows = "".join(render_quote_row(s, l, k, quotes.get(s)) for s, l, k in TICKERS[group])
    return (
        '<div class="tscroll"><table><thead><tr>'
        '<th>Instrument</th><th>Last</th><th>1 day</th><th>1 week</th>'
        '<th>1 month</th><th>YTD</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
    )


def why_it_matters(cl: dict) -> str:
    """
    A plain-language reason, built only from the headline's own themes and the
    feed's snippet. We never invent facts that are not in the feed.
    """
    themes = cl["themes"]
    parts = []
    if "policy" in themes:
        parts.append("central-bank policy drives borrowing costs across every market")
    if "inflation" in themes:
        parts.append("inflation data shapes how soon rates can be cut")
    if "growth" in themes:
        parts.append("growth and jobs data set expectations for company earnings")
    if "energy" in themes:
        parts.append("energy prices feed straight back into inflation")
    if "trade" in themes:
        parts.append("trade and geopolitical shifts reroute supply chains and prices")
    if "gold" in themes and "policy" not in themes:
        parts.append("gold responds to real yields and the dollar")
    if "thailand" in themes:
        parts.append("directly relevant to the Thai economy and the baht")
    if "asia" in themes and "thailand" not in themes:
        parts.append("Asian demand is a swing factor for global growth")
    if "equities" in themes and not parts:
        parts.append("equity moves show how investors are pricing risk")

    reason = ""
    if parts:
        reason = "Why it matters: " + parts[0][0].upper() + parts[0][1:] + "."
    if cl["n_sources"] > 1:
        reason += f" Covered by {cl['n_sources']} sources."
    if cl["primary"]:
        reason += " Primary source."
    return reason


def render_meta(cl: dict, seen: set[str]) -> str:
    bits = [f'<span>{esc(cl["source"])}</span>']
    if cl["when"]:
        t = dt.datetime.fromisoformat(cl["when"]).astimezone(BKK)
        bits.append(f'<span>{t.strftime("%d %b, %H:%M")} BKK</span>')
    elif cl["undated"]:
        bits.append('<span>no date in feed</span>')
    if cl["paywall"] == "hard":
        bits.append('<span class="pw" title="Likely behind a paywall">$</span>')
    if cl["link"] in seen:
        bits.append('<span class="seen">seen</span>')
    for t in cl["themes"][:4]:
        bits.append(f'<span class="tag">{esc(THEMES[t]["label"])}</span>')
    return '<div class="meta">' + "".join(bits) + "</div>"


def render_story(cl: dict, seen: set[str], with_why: bool) -> str:
    also = ""
    if cl["others"]:
        links = ", ".join(
            f'<a href="{esc(o["link"])}" target="_blank" rel="noopener">{esc(o["source"])}</a>'
            for o in cl["others"])
        also = f'<div class="also">Also: {links}</div>'
    why = f'<p class="why">{esc(why_it_matters(cl))}</p>' if with_why else ""
    snippet = ""
    if not with_why and cl["snippet"]:
        snippet = f'<p class="why">{esc(cl["snippet"])}</p>'
    return (
        f'<a class="hl" href="{esc(cl["link"])}" target="_blank" rel="noopener">'
        f'{esc(cl["title"])}</a>{why}{snippet}{render_meta(cl, seen)}{also}'
    )


def build_html(data: dict, args, fred_available: bool) -> str:
    quotes = data["quotes"]
    clusters = data["clusters"]
    seen = set(data.get("seen_links", []))
    built = now_bkk()

    # ---- Tier 1: tape -----------------------------------------------------
    tape = []
    for sym, label, kind in TICKERS["headline"]:
        q = quotes.get(sym) or {}
        if not q.get("ok"):
            tape.append(f'<div><div class="k">{esc(label)}</div>'
                        f'<div class="p">n/a</div><div class="c flat">no data</div></div>')
            continue
        if kind == "yield":
            price = f'{q["last"]:.2f}%'
            d = q.get("abs_1d")
            chg = f'{d * 100:+.0f} bp' if d is not None else "n/a"
            cls = pct_class(d)
        else:
            price = fmt_num(q["last"])
            d = q.get("pct_1d")
            chg = f"{d:+.2f}%" if d is not None else "n/a"
            cls = pct_class(d)
        stale = ' <span class="stale">stale</span>' if q.get("degraded") else ""
        tape.append(f'<div><div class="k">{esc(label)}</div><div class="p">{price}</div>'
                    f'<div class="c {cls}">{chg}{stale}</div></div>')
    tape_html = '<div class="tape">' + "".join(tape) + "</div>"

    # ---- Tier 1: what changed --------------------------------------------
    chg = data.get("change_summary") or {}
    changed_html = ""
    if chg.get("moves"):
        since = ""
        if chg.get("since"):
            try:
                st = dt.datetime.fromisoformat(chg["since"]).astimezone(BKK)
                since = f' since your last briefing ({st.strftime("%d %b %H:%M")})'
            except Exception:
                pass
        items = "".join(
            f'<div><div class="k">{esc(m["label"])}</div>'
            f'<div class="v {m["dir"]}">{esc(m["text"])}</div></div>'
            for m in chg["moves"])
        changed_html = (f'<div class="card accent"><h2>What changed{esc(since)}</h2>'
                        f'<div class="kv">{items}</div></div>')

    # ---- Tier 1: unusual moves ------------------------------------------
    flagged = data.get("unusual") or []
    unusual_html = ""
    if flagged:
        items = "".join(
            f'<div><div class="k">{esc(f["label"])}</div>'
            f'<div class="v {pct_class(f["pct"])}">{f["pct"]:+.2f}% '
            f'<span class="sig">{abs(f["z"]):.1f}&sigma;</span></div></div>'
            for f in flagged if f.get("pct") is not None)
        if items:
            unusual_html = (
                '<div class="card warn"><h2>Unusual moves today</h2>'
                '<h3>Bigger than normal against each instrument&rsquo;s own '
                '20-day volatility &mdash; these are the moves worth a look</h3>'
                f'<div class="kv">{items}</div></div>')

    # ---- Tier 1: top stories --------------------------------------------
    ranked = sorted(clusters, key=lambda c: score_cluster(c, seen), reverse=True)
    top = ranked[:5]
    top_html = "".join(f"<li>{render_story(c, seen, True)}</li>" for c in top)
    if not top_html:
        top_html = ('<li>No stories in the current window. Try a wider window: '
                    '<code>--hours 48</code></li>')

    # ---- Filters ---------------------------------------------------------
    present = sorted({t for c in clusters for t in c["themes"]},
                     key=lambda t: -RANK_WEIGHTS.get(t, 0))
    filters = ['<button data-theme="all" aria-pressed="true">All</button>']
    for t in present:
        filters.append(f'<button data-theme="{esc(t)}">{esc(THEMES[t]["label"])}</button>')
    filters_html = '<div class="filters">' + "".join(filters) + "</div>"

    # ---- Tier 2: news by group ------------------------------------------
    news_html = []
    for group in list(FEEDS.keys()):
        in_group = [c for c in ranked if c["group"] == group][:6]
        if not in_group:
            continue
        lis = "".join(
            f'<li data-themes="{esc(" ".join(c["themes"]))}">{render_story(c, seen, False)}</li>'
            for c in in_group)
        news_html.append(f'<div class="newsgrp"><h2>{esc(group)}</h2>'
                         f'<ul class="items">{lis}</ul></div>')
    news_block = "".join(news_html) or '<p class="note">No headlines in this window.</p>'

    # ---- Tier 2: oil curve ----------------------------------------------
    curve = data.get("curve") or []
    curve_html = ""
    if curve:
        shape, explain = curve_shape(curve)
        cells = "".join(f'<div><div class="m">{esc(c["label"])}</div>'
                        f'<div class="q">{c["price"]:.2f}</div></div>' for c in curve)
        curve_html = (f'<div class="card"><h2>WTI forward curve</h2>'
                      f'<h3>What oil costs for delivery in each coming month</h3>'
                      f'<div class="curve">{cells}</div>'
                      f'<p class="note">{esc(explain)}</p></div>')

    # ---- Tier 2: FRED ----------------------------------------------------
    def fred_rows(rows):
        out = []
        for r in rows:
            if not r.get("ok") or r.get("value") is None:
                out.append(f'<tr><td>{esc(r.get("label", r["id"]))}</td>'
                           f'<td class="v">n/a</td><td class="v flat">'
                           f'<span class="stale">{esc(r.get("error") or "no value")}'
                           f'</span></td><td class="v"></td></tr>')
                continue
            u = r["unit"]
            if u == "%":
                val = f'{r["value"]:,.2f}%'
            elif u == "k":
                val = f'{r["value"]:,.0f}k'
            elif u == "T":
                val = f'${r["value"]:,.2f}T'
            elif u == "B":
                val = f'${r["value"]:,.0f}B'
            else:
                val = f'{r["value"]:,.2f}'

            prev = r.get("prev")
            if prev is None:
                delta, cls = "n/a", "flat"
            else:
                d = r["value"] - prev
                # Match the unit of the value beside it.
                if u in ("%", ""):
                    delta = f"{d:+.2f}"
                elif u == "k":
                    delta = f"{d:+,.0f}k"
                elif u == "T":
                    # A weekly balance-sheet move is billions, not trillions —
                    # "-0.00T" says nothing.
                    delta = f"{d * 1000:+,.0f}B"
                else:
                    delta = f"{d:+,.0f}B"
                cls = pct_class(d)

            # A series that quietly stopped updating must never look current.
            flag = (f'<span class="stale">series stale &mdash; '
                    f'{r.get("age_days", "?")}d old</span>') if r.get("stale") else ""
            out.append(f'<tr><td>{esc(r["label"])}{flag}</td><td class="v">{val}</td>'
                       f'<td class="v {cls}">{delta}</td>'
                       f'<td class="v">{esc(r.get("date", ""))}</td></tr>')
        return "".join(out)

    fred_html = ""
    if fred_available and (data.get("fred_rates") or data.get("fred_data")):
        fred_html = (
            '<div class="card"><h2>Rates, inflation expectations &amp; credit</h2>'
            '<h3>The real yield is the single most reliable driver of gold: it is '
            'what a bond pays you after inflation, so it is what gold costs you '
            'to hold</h3>'
            '<div class="tscroll"><table><thead><tr><th>Measure</th><th>Latest</th>'
            '<th>Change</th><th>As of</th></tr></thead><tbody>'
            + fred_rows(data.get("fred_rates", [])) +
            '</tbody></table></div></div>'
            '<div class="card"><h2>Growth &amp; inflation data</h2>'
            '<h3>Official US releases, year-over-year unless noted</h3>'
            '<div class="tscroll"><table><thead><tr><th>Indicator</th><th>Latest</th>'
            '<th>vs prior</th><th>As of</th></tr></thead><tbody>'
            + fred_rows(data.get("fred_data", [])) +
            '</tbody></table></div></div>')
    elif not fred_available:
        fred_html = (
            '<div class="card warn"><h2>Rates, inflation expectations &amp; credit</h2>'
            '<p class="note">Not shown: these need a free FRED API key. Get one at '
            '<a href="https://fred.stlouisfed.org/docs/api/api_key.html" '
            'target="_blank" rel="noopener">fred.stlouisfed.org</a>, then run:<br>'
            '<code>echo "YOUR_KEY" &gt; fred_key.txt</code><br>'
            'This unlocks real yields (the main gold driver), breakeven inflation, '
            'credit spreads and the US data board.</p></div>')

    # ---- Tier 2: liquidity & conditions ---------------------------------
    liquidity_html = ""
    if data.get("fred_liquidity"):
        liquidity_html = (
            '<div class="card"><h2>Liquidity &amp; financial conditions</h2>'
            '<h3>Prices tell you what happened; these tell you how easy money is '
            '&mdash; the regime that decides whether a rally holds. Negative '
            'financial-conditions numbers mean looser than average.</h3>'
            '<div class="tscroll"><table><thead><tr><th>Measure</th><th>Latest</th>'
            '<th>Change</th><th>As of</th></tr></thead><tbody>'
            + fred_rows(data["fred_liquidity"]) +
            '</tbody></table></div></div>')

    # ---- Tier 2: is gold behaving normally? -----------------------------
    rel = data.get("relationships") or []
    rel_html = ""
    if rel:
        rows = []
        for r in rel:
            c = r["corr"]
            # Colour by whether the relationship is behaving as expected, not by
            # the sign of the number.
            expected_negative = ("yield" in r["label"].lower() or "Dollar" in r["label"])
            normal = (c <= -0.25) if expected_negative else (c >= 0.5)
            cls = "up" if normal else ("down" if abs(c) >= 0.25 else "flat")
            bar = int(abs(c) * 100)
            rows.append(
                f'<tr><td>Gold vs {esc(r["label"])}</td>'
                f'<td class="v {cls}">{c:+.2f}</td>'
                f'<td><span class="corrbar"><i style="width:{bar}%"></i></span></td>'
                f'<td class="note">{esc(r["reading"])}</td></tr>')
        rel_html = (
            '<div class="card"><h2>Is gold behaving normally?</h2>'
            f'<h3>How closely gold has moved with each driver over the last '
            f'{CORRELATION_WINDOW} trading days. &minus;1.00 means they move exactly '
            'opposite, +1.00 exactly together, 0.00 means no relationship. Gold is '
            '<em>supposed</em> to move against real yields and the dollar &mdash; when '
            'it stops, that itself is the story.</h3>'
            '<div class="tscroll"><table><thead><tr><th>Relationship</th>'
            '<th>Correlation</th><th>Strength</th><th>What it means</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div></div>')

    # ---- Tier 2: CFTC positioning ---------------------------------------
    cftc = data.get("cftc") or []
    cftc_html = ""
    if cftc:
        rows = "".join(
            f'<tr><td>{esc(c["label"])}</td>'
            f'<td class="v {pct_class(c["net"])}">{c["net"]:+,}</td>'
            f'<td class="v {pct_class(c.get("chg_1w"))}">'
            f'{fmt_signed(c.get("chg_1w"), 0)}</td>'
            f'<td class="v {pct_class(c.get("chg_4w"))}">'
            f'{fmt_signed(c.get("chg_4w"), 0)}</td>'
            f'<td class="v">{esc(c["date"])}</td></tr>' for c in cftc)
        cftc_html = (
            '<div class="card"><h2>Speculative positioning (CFTC)</h2>'
            '<h3>Net futures position held by speculators. A large positive number '
            'means the bullish trade is already crowded, which cuts both ways '
            '&mdash; published weekly, with a few days&rsquo; lag</h3>'
            '<div class="tscroll"><table><thead><tr><th>Market</th><th>Net position</th>'
            '<th>1 week</th><th>4 weeks</th><th>As of</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div></div>')

    # ---- Tier 2: Thai gold ----------------------------------------------
    gold_q, thb_q = quotes.get("GC=F"), quotes.get("THB=X")
    thai_html = ""
    tg = thai_gold_price(gold_q.get("last") if gold_q and gold_q.get("ok") else None,
                         thb_q.get("last") if thb_q and thb_q.get("ok") else None)
    if tg:
        thai_html = (
            '<div class="card"><h2>Gold in Thai terms</h2>'
            '<div class="kv">'
            f'<div><div class="k">Per baht-weight</div>'
            f'<div class="v">{fmt_num(tg["thb_per_baht_weight"], 0)} THB</div></div>'
            f'<div><div class="k">Per gram (pure)</div>'
            f'<div class="v">{fmt_num(tg["thb_per_gram"], 0)} THB</div></div>'
            f'<div><div class="k">World gold</div>'
            f'<div class="v">${fmt_num(gold_q["last"], 2)}/oz</div></div>'
            f'<div><div class="k">USD/THB</div>'
            f'<div class="v">{fmt_num(thb_q["last"], 2)}</div></div>'
            '</div>'
            '<p class="note">Arithmetic conversion only: one baht-weight is 15.244 g '
            'at 96.5% purity. Real shop quotes from the Gold Traders Association add '
            'a dealer premium, so treat this as a reference, not a dealing price.</p>'
            '</div>')

    # ---- Tier 2: events --------------------------------------------------
    today = built.date()
    # Hand-kept entries (central bank meetings) plus the automatic US data
    # releases fetched from FRED's official calendar.
    combined = [{"date": d, "time_note": t, "label": l} for d, t, l in EVENTS]
    combined += data.get("calendar") or []

    upcoming = []
    for ev in sorted(combined, key=lambda e: (e["date"], e["label"])):
        try:
            d = dt.date.fromisoformat(ev["date"])
        except Exception:
            continue
        if d >= today:
            days = (d - today).days
            when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
            upcoming.append(
                f'<li><span class="d">{d.strftime("%d %b")}</span>'
                f'<span>{esc(ev["label"])}<br><span class="note">'
                f'{esc(ev["time_note"])} &middot; {when}</span></span></li>')
    events_html = (
        '<div class="card"><h2>Coming up</h2>'
        + (f'<ul class="events">{"".join(upcoming[:10])}</ul>' if upcoming else
           '<p class="note">Nothing scheduled in the next few months. US data '
           'releases load automatically once a FRED key is set; central bank '
           'meetings go in the EVENTS list at the top of world_briefing.py.</p>')
        + '</div>')

    # ---- Footer / health -------------------------------------------------
    ok_feeds = data.get("ok_feeds", [])
    failed = data.get("failed", [])
    total_feeds = sum(len(v) for v in FEEDS.values())
    live = sum(1 for q in quotes.values() if q.get("ok") and not q.get("degraded"))
    stale_n = sum(1 for q in quotes.values() if q.get("degraded"))
    missing = sum(1 for q in quotes.values() if not q.get("ok"))
    health = (f"{len(ok_feeds)}/{total_feeds} feeds OK &middot; "
              f"{live}/{len(quotes)} prices live")
    if stale_n:
        health += f" &middot; {stale_n} from cache"
    if missing:
        health += f" &middot; {missing} unavailable"
    if fred_available:
        fr = data.get("fred_rates", []) + data.get("fred_data", [])
        health += f" &middot; FRED {sum(1 for r in fr if r.get('ok'))}/{len(fr)}"
    else:
        health += " &middot; FRED off (no key)"
    health += f" &middot; CFTC {len(cftc)}/{len(CFTC_MARKETS)}"
    if data.get("calendar"):
        health += f" &middot; calendar {len(data['calendar'])} events"

    failed_html = ""
    if failed:
        names = ", ".join(f'{esc(f["name"])} ({esc(f["error"])})' for f in failed)
        failed_html = f'<p>Feeds that failed today: {names}</p>'

    window_note = (f"News window: {args.hours}h (per-feed windows overridden)"
                   if args.hours else
                   "News window: 24h for news feeds, up to 10 days for central banks "
                   "(they publish less often)")

    stale_js = STALE_JS_TEMPLATE.replace("__BUILT_ISO__",
                                         built.isoformat(timespec="seconds"))

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Economic Overview &mdash; {built.strftime('%d %b %Y')}</title>
<meta name="description" content="Daily macro briefing: markets, policy, data and headlines.">
<style>{CSS}</style>
</head><body><div class="wrap">

<div id="stale" role="status"></div>

<header class="top">
  <h1>Economic Overview</h1>
  <p class="sub">{built.strftime('%A %d %B %Y, %H:%M')} Bangkok time
  {' &middot; <strong>DEMO DATA</strong>' if args.demo else ''}</p>
</header>

<section class="tier">
  <div class="tier-label"><span class="n">Tier 1</span>
    <h2>The 60-second read</h2></div>
  {tape_html}
  {changed_html}
  {unusual_html}
  <div class="card">
    <h2>Top stories</h2>
    <h3>Ranked by how many sources carry the story and how much it moves markets</h3>
    <ol class="stories">{top_html}</ol>
  </div>
</section>

<section class="tier">
  <div class="tier-label"><span class="n">Tier 2</span>
    <h2>The detail</h2></div>

  {fred_html}

  <div class="card"><h2>Equities</h2>{render_table('equities', quotes)}</div>
  <div class="card"><h2>Rates &amp; policy expectations</h2>
    <h3>Fed funds futures show the policy rate the market expects, derived from
    the futures price</h3>
    {render_table('rates', quotes)}</div>
  {liquidity_html}
  <div class="card"><h2>Currencies</h2>{render_table('fx', quotes)}</div>
  <div class="card"><h2>Energy</h2>{render_table('energy', quotes)}</div>
  {curve_html}
  <div class="card"><h2>Commodities</h2>{render_table('commodities', quotes)}</div>
  {rel_html}
  {thai_html}
  <div class="card"><h2>Risk appetite &amp; volatility</h2>
    <h3>When these rise together, investors are getting defensive</h3>
    {render_table('risk', quotes)}</div>
  {cftc_html}
  {events_html}
</section>

<section class="tier">
  <div class="tier-label"><span class="n">Tier 3</span>
    <h2>All headlines</h2></div>
  {filters_html}
  {news_block}
</section>

<footer>
  <p>Headlines link to the original publishers. Education only, not financial advice.</p>
  <p class="health">{health}</p>
  {failed_html}
  <p>{esc(window_note)}. Prices from Yahoo Finance; economic data from FRED
  (St. Louis Fed); positioning from the CFTC. A &ldquo;$&rdquo; marks a link
  likely behind a paywall.</p>
  <p>Built {built.strftime('%Y-%m-%d %H:%M')} Bangkok time.</p>
</footer>

</div><script>{stale_js}</script><script>{JS}</script></body></html>
"""


# ============================================================================
# MAIN
# ============================================================================

def check_data_report(quotes: dict, fred_rows: list, cftc: list, ok_feeds, failed) -> None:
    print("\n--- WHERE EACH NUMBER CAME FROM ---")
    labels = {s: l for g in TICKERS.values() for s, l, _ in g}
    for sym in sorted(quotes):
        q = quotes[sym]
        src = q.get("source", "?")
        if q.get("degraded"):
            src = f"cache ({q.get('stale_age')})"
        status = "ok " if q.get("ok") else "FAIL"
        extra = "" if q.get("ok") else f"  <- {q.get('error')}"
        print(f"  {status} {sym:<12} {labels.get(sym, ''):<32} {src}{extra}")
    print(f"\n  FRED: {sum(1 for r in fred_rows if r.get('ok'))}/{len(fred_rows)} series")
    for r in fred_rows:
        if not r.get("ok"):
            print(f"    FAIL {r['id']}: {r.get('error')}")
    print(f"  CFTC: {len(cftc)}/{len(CFTC_MARKETS)} markets")
    print(f"  Feeds OK: {len(ok_feeds)}")
    for f in failed:
        print(f"    FAIL {f['name']}: {f['error']}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the daily Economic Overview.")
    ap.add_argument("--open", action="store_true", help="open the page when built")
    ap.add_argument("--demo", action="store_true", help="build from fake data, no internet")
    ap.add_argument("--hours", type=int, default=None,
                    help="override every feed's lookback window (e.g. 48 on Mondays)")
    ap.add_argument("--check-data", action="store_true",
                    help="print where each number came from")
    args = ap.parse_args()

    key = None if args.demo else fred_key()

    if args.demo:
        print("Building from demo data (no network calls)...")
        payload = demo_payload()
        data = {
            "quotes": payload["quotes"], "curve": payload["curve"],
            "clusters": payload["clusters"], "fred_rates": payload["fred_rates"],
            "fred_data": payload["fred_data"], "cftc": payload["cftc"],
            "failed": payload["failed"], "ok_feeds": payload["ok_feeds"],
            "seen_links": [], "unusual": unusual_moves(payload["quotes"]),
            "fred_liquidity": [{"id": sid, "ok": True, "label": lbl, "unit": un,
                                "value": v, "prev": v * 0.98, "date": "2026-10-02"}
                               for (sid, lbl, _, un), v in zip(
                                   FRED_LIQUIDITY, [-0.42, -0.15, 6.8, 118.0, 4.08, 7.4])],
            "relationships": [
                {"label": "Real 10-year yield", "corr": -0.61, "days": 59,
                 "reading": "Normal: gold is moving strongly against it, as textbook says."},
                {"label": "Dollar index (DXY)", "corr": -0.38, "days": 59,
                 "reading": "Normal: gold is moving moderately against it, as textbook says."},
                {"label": "Silver", "corr": 0.74, "days": 59,
                 "reading": "Normal: the precious metals complex is moving together."}],
            "calendar": [
                {"date": (dt.date.today() + dt.timedelta(days=3)).isoformat(),
                 "time_note": "19:30 BKK", "label": "US inflation (CPI)"},
                {"date": (dt.date.today() + dt.timedelta(days=11)).isoformat(),
                 "time_note": "19:30 BKK", "label": "US jobs report (non-farm payrolls)"}],
            "change_summary": {"since": None, "moves": []},
        }
        fred_available = True
    else:
        print("Fetching market data...")
        quotes = fetch_all_quotes(ALL_SYMBOLS)
        live = sum(1 for q in quotes.values() if q.get("ok") and not q.get("degraded"))
        print(f"  prices: {live}/{len(quotes)} live")

        print("Fetching oil forward curve...")
        curve = fetch_oil_curve()
        print(f"  curve: {len(curve)} contracts")

        print("Fetching news feeds...")
        clusters, failed, ok_feeds = fetch_all_news(args.hours)
        print(f"  feeds: {len(ok_feeds)} ok, {len(failed)} failed; "
              f"{len(clusters)} stories after dedupe")

        if key:
            print("Fetching FRED data...")
            fred_rates = fetch_fred_block(FRED_RATES, key)
            fred_data = fetch_fred_block(FRED_DATA, key)
            print(f"  FRED: {sum(1 for r in fred_rates + fred_data if r.get('ok'))}"
                  f"/{len(fred_rates) + len(fred_data)} series")
        else:
            fred_rates, fred_data = [], []
            print("  FRED: skipped (no key — see fred_key.txt in the header notes)")

        print("Fetching liquidity & conditions...")
        fred_liquidity = fetch_fred_block(FRED_LIQUIDITY, key) if key else []
        print(f"  liquidity: {sum(1 for r in fred_liquidity if r.get('ok'))}/{len(FRED_LIQUIDITY)}")

        print("Analysing gold's relationships...")
        relationships = build_relationships(key)
        print(f"  correlations: {len(relationships)} computed")

        print("Fetching release calendar...")
        calendar = fetch_release_calendar(key)
        print(f"  calendar: {len(calendar)} upcoming US releases")

        print("Fetching CFTC positioning...")
        cftc = fetch_cftc()
        print(f"  CFTC: {len(cftc)}/{len(CFTC_MARKETS)} markets")

        history = load_json(HISTORY_FILE, {"links": []})
        seen_links = set(history.get("links", []))
        snapshot = load_json(SNAPSHOT_FILE, {})

        data = {
            "quotes": quotes, "curve": curve, "clusters": clusters,
            "fred_rates": fred_rates, "fred_data": fred_data, "cftc": cftc,
            "failed": failed, "ok_feeds": ok_feeds, "seen_links": list(seen_links),
            "calendar": calendar, "fred_liquidity": fred_liquidity,
            "relationships": relationships,
            "unusual": unusual_moves(quotes),
            "change_summary": build_change_summary(quotes, snapshot),
        }
        fred_available = bool(key)

        if args.check_data:
            check_data_report(quotes, fred_rates + fred_data, cftc, ok_feeds, failed)

        # Remember what we showed, so repeats get marked "seen" tomorrow.
        new_links = [c["link"] for c in clusters]
        save_json(HISTORY_FILE, {"links": (list(seen_links | set(new_links)))[-4000:]})
        snaps = snapshot.get("snapshots") or ([snapshot] if snapshot.get("prices") else [])
        snaps.append({
            "built_at": dt.datetime.now(UTC).isoformat(timespec="seconds"),
            "prices": {s: q["last"] for s, q in quotes.items() if q.get("ok")},
        })
        # Keep a few days of builds so the comparison still works after a gap.
        save_json(SNAPSHOT_FILE, {"snapshots": snaps[-80:]})

    # Write a machine-readable health summary. The cloud job reads this and
    # emails you if the briefing has quietly degraded.
    if not args.demo:
        q = data["quotes"]
        save_json(HEALTH_FILE, {
            "built_at": dt.datetime.now(UTC).isoformat(timespec="seconds"),
            "feeds_ok": len(data.get("ok_feeds", [])),
            "feeds_total": sum(len(v) for v in FEEDS.values()),
            "prices_live": sum(1 for x in q.values() if x.get("ok") and not x.get("degraded")),
            "prices_total": len(q),
            "fred_ok": sum(1 for r in data.get("fred_rates", []) + data.get("fred_data", [])
                           if r.get("ok")),
            "fred_total": len(data.get("fred_rates", [])) + len(data.get("fred_data", [])),
            "cftc_ok": len(data.get("cftc", [])),
            "cftc_total": len(CFTC_MARKETS),
            "calendar_events": len(data.get("calendar", [])),
            "stories": len(data.get("clusters", [])),
            "failed_feeds": [f["name"] for f in data.get("failed", [])],
        })

    html_out = build_html(data, args, fred_available)
    OUT_HTML.write_text(html_out, encoding="utf-8")
    size_kb = OUT_HTML.stat().st_size / 1024
    print(f"\nWrote {OUT_HTML} ({size_kb:.0f} KB)")

    if args.open:
        webbrowser.open(OUT_HTML.as_uri())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
