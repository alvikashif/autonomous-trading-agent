"""Deep Company Research & Financial News Aggregator for Alvi Quantitative Terminal.

Provides multi-quarter financial trajectory, QoQ/YoY revenue & profit growth,
multi-year annual financial history, profit margins, and real-time financial news/catalysts.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def fetch_company_deep_profile(symbol: str) -> dict[str, Any]:
    """Fetch multi-quarter P&L, YoY/QoQ growth, valuation, and 4-year annual trajectory."""
    clean_sym = symbol.replace(".NS", "").replace(".BO", "").upper()
    try:
        import yfinance as yf
        t = yf.Ticker(f"{clean_sym}.NS")
        info = t.info or {}
        q_inc = t.quarterly_income_stmt
        ann_inc = t.income_stmt

        qoq_rev, qoq_pat = None, None
        yoy_rev = info.get("revenueGrowth")
        yoy_pat = info.get("earningsGrowth")

        # 1. Quarterly comparisons (QoQ)
        if q_inc is not None and not q_inc.empty:
            if "Total Revenue" in q_inc.index:
                revs = q_inc.loc["Total Revenue"].dropna()
                if len(revs) >= 2 and revs.iloc[1] > 0:
                    qoq_rev = round(float((revs.iloc[0] / revs.iloc[1] - 1) * 100), 1)

            net_key = next((k for k in ["Net Income Common Stockholders", "Net Income"] if k in q_inc.index), None)
            if net_key:
                pats = q_inc.loc[net_key].dropna()
                if len(pats) >= 2 and pats.iloc[1] != 0:
                    qoq_pat = round(float((pats.iloc[0] / pats.iloc[1] - 1) * 100), 1)

        # 2. Multi-year annual history
        annual_history: list[dict[str, Any]] = []
        if ann_inc is not None and not ann_inc.empty:
            rev_row = ann_inc.loc["Total Revenue"].dropna() if "Total Revenue" in ann_inc.index else None
            net_key_ann = next((k for k in ["Net Income Common Stockholders", "Net Income"] if k in ann_inc.index), None)
            pat_row = ann_inc.loc[net_key_ann].dropna() if net_key_ann else None

            years = sorted(list(ann_inc.columns), reverse=True)[:4]
            for col in years:
                yr_str = str(col.year if hasattr(col, "year") else col)[:4]
                r_val = round(float(rev_row.get(col, 0)) / 1e7, 1) if rev_row is not None and col in rev_row else None
                p_val = round(float(pat_row.get(col, 0)) / 1e7, 1) if pat_row is not None and col in pat_row else None
                annual_history.append({"year": yr_str, "revenue_cr": r_val, "net_profit_cr": p_val})

        return {
            "symbol": clean_sym,
            "company_name": info.get("shortName") or info.get("longName") or clean_sym,
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "pe_ratio": round(float(info.get("trailingPE")), 1) if info.get("trailingPE") else None,
            "forward_pe": round(float(info.get("forwardPE")), 1) if info.get("forwardPE") else None,
            "peg_ratio": round(float(info.get("trailingPegRatio")), 2) if info.get("trailingPegRatio") else None,
            "operating_margin_pct": round(float(info.get("operatingMargins", 0)) * 100, 1) if info.get("operatingMargins") else None,
            "profit_margin_pct": round(float(info.get("profitMargins", 0)) * 100, 1) if info.get("profitMargins") else None,
            "qoq_revenue_growth_pct": qoq_rev,
            "qoq_profit_growth_pct": qoq_pat,
            "yoy_revenue_growth_pct": round(float(yoy_rev) * 100, 1) if yoy_rev else None,
            "yoy_profit_growth_pct": round(float(yoy_pat) * 100, 1) if yoy_pat else None,
            "annual_history": annual_history,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M IST"),
        }
    except Exception as e:
        logger.warning(f"Failed to fetch deep profile for {clean_sym}: {e}")
        return {"symbol": clean_sym, "error": str(e), "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M IST")}


def fetch_company_news(symbol: str, company_name: str | None = None, max_items: int = 5) -> list[dict[str, str]]:
    """Fetch recent financial news and corporate announcements from Yahoo Finance and Google News RSS."""
    clean_sym = symbol.replace(".NS", "").replace(".BO", "").upper()
    news_items: list[dict[str, str]] = []

    # 1. Yahoo Finance News
    try:
        import yfinance as yf
        t = yf.Ticker(f"{clean_sym}.NS")
        for n in (t.news or [])[:3]:
            title = n.get("title") or (n.get("content", {}).get("title") if isinstance(n.get("content"), dict) else None)
            pub = n.get("providerPublishTime") or (n.get("content", {}).get("pubDate") if isinstance(n.get("content"), dict) else None)
            source = n.get("publisher") or "Yahoo Finance"
            link = n.get("link") or (n.get("content", {}).get("canonicalUrl", {}).get("url") if isinstance(n.get("content"), dict) else "")
            if title:
                news_items.append({
                    "title": str(title).strip(),
                    "source": str(source).strip(),
                    "published": str(pub)[:19] if pub else "",
                    "link": str(link) if link else "",
                })
    except Exception as e:
        logger.debug(f"Yahoo news fetch failed for {clean_sym}: {e}")

    # 2. Google News RSS for Indian Stock Updates
    try:
        query_name = company_name or clean_sym
        q = urllib.parse.quote(f"{query_name} stock share news NSE India")
        url = f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            root = ET.fromstring(resp.read())
        for it in root.findall(".//item")[:4]:
            t_elem = it.find("title")
            d_elem = it.find("pubDate")
            s_elem = it.find("source")
            l_elem = it.find("link")
            if t_elem is not None and t_elem.text:
                news_items.append({
                    "title": str(t_elem.text).strip(),
                    "source": str(s_elem.text if s_elem is not None and s_elem.text else "Google News").strip(),
                    "published": str(d_elem.text)[:16] if d_elem is not None and d_elem.text else "",
                    "link": str(l_elem.text) if l_elem is not None and l_elem.text else "",
                })
    except Exception as e:
        logger.debug(f"Google news fetch failed for {clean_sym}: {e}")

    # Deduplicate by normalized title
    seen = set()
    unique_news = []
    for item in news_items:
        norm = re.sub(r"[^a-zA-Z0-9]", "", item["title"].lower())[:45]
        if norm and norm not in seen:
            seen.add(norm)
            unique_news.append(item)

    return unique_news[:max_items]


def get_cached_research(symbol: str, base_dir: Path | str = "data", ttl_hours: float = 4.0) -> dict[str, Any]:
    """Retrieve or fetch cached deep profile & news for symbol."""
    clean_sym = symbol.replace(".NS", "").replace(".BO", "").upper()
    cache_path = Path(base_dir) / "research_cache.json"
    cache: dict[str, Any] = {}
    if cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    entry = cache.get(clean_sym, {})
    now_ts = time.time()
    if entry and (now_ts - entry.get("_fetched_ts", 0)) < (ttl_hours * 3600):
        return entry

    # Fetch fresh
    profile = fetch_company_deep_profile(clean_sym)
    company_name = profile.get("company_name")
    news = fetch_company_news(clean_sym, company_name=company_name)

    entry = {
        "profile": profile,
        "news": news,
        "_fetched_ts": now_ts,
        "symbol": clean_sym,
    }
    cache[clean_sym] = entry
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to write research cache: {e}")

    return entry
