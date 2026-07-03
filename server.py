"""
Prediction-market mispricing scanner MCP server (read-only, no secrets).

Exposes cross-venue tools that compare Kalshi vs Polymarket implied
probabilities for the same real-world event and surface the biggest gaps.

Data sources are both PUBLIC / unauthenticated:
  - Kalshi:     https://api.elections.kalshi.com/trade-api/v2/events
  - Polymarket: https://gamma-api.polymarket.com/public-search

NOT FINANCIAL ADVICE. A surfaced "gap" is a candidate to investigate, not a
guaranteed arbitrage: verify both markets resolve on the same condition and
account for spread, fees, and settlement differences before acting. This
server places no orders and holds no credentials.
"""
import json
import time

import httpx
from mcp.server.fastmcp import FastMCP

import scanner_core as sc

mcp = FastMCP("predmkt-scanner")

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def _query_tokens(query):
    toks, _ = sc.normalize_title(query)
    return [t for t in toks if len(t) >= 3]


def fetch_kalshi(query, max_pages=12, page_size=200, window_days=1000, enough=50):
    """Keyword-scan Kalshi's /markets, windowed to near-term close dates.

    Kalshi's /events feed is ordered close-date-descending, so far-future
    novelty markets (2050-2099) fill the first pages and near-term markets
    (World Cup, elections, Fed, EOY crypto) are never reached within a sane
    page budget. /markets honours min_close_ts / max_close_ts, so we window
    to [now, now+window_days] to drop the novelty noise and surface the
    markets people actually compare across venues. We keep only markets with
    some activity (volume or open interest), filter by query token, dedupe by
    ticker, and early-stop once we have enough matches."""
    qtokens = _query_tokens(query)
    now = int(time.time())
    base = {
        "status": "open",
        "limit": page_size,
        "min_close_ts": now,
        "max_close_ts": now + window_days * 86400,
    }
    out, cursor = {}, None
    with httpx.Client(timeout=25) as client:
        for page in range(max_pages):
            params = dict(base)
            if cursor:
                params["cursor"] = cursor
            # fetch with backoff on Kalshi's 429 rate limit
            data = None
            for attempt in range(5):
                r = client.get(f"{KALSHI}/markets", params=params)
                if r.status_code == 429:
                    ra = r.headers.get("Retry-After")
                    wait = float(ra) if ra and ra.replace(".", "").isdigit() else 0.8 * (2 ** attempt)
                    time.sleep(min(wait, 5.0))
                    continue
                r.raise_for_status()
                data = r.json()
                break
            if data is None:
                break  # persistent rate limiting -> return what we have so far
            for m in data.get("markets", []):
                # skip untraded placeholder markets (misleading default quotes)
                if float(m.get("volume_fp") or 0) == 0 and float(m.get("open_interest_fp") or 0) == 0:
                    continue
                hay, _ = sc.normalize_title(f"{m.get('title','')} {m.get('yes_sub_title','')}")
                if qtokens and not any(t in hay for t in qtokens):
                    continue
                nm = sc.normalize_kalshi_market(m)
                if nm:
                    out[nm["ticker"]] = nm
            cursor = data.get("cursor")
            if not cursor or len(out) >= enough:
                break
            time.sleep(0.25)  # gentle throttle to stay under the rate limit
    return list(out.values())


def fetch_polymarket(query, limit=40):
    """Use Polymarket's public search (already relevance-ranked)."""
    out = []
    with httpx.Client(timeout=25) as client:
        r = client.get(
            f"{GAMMA}/public-search",
            params={"q": query, "limit_per_type": limit, "events_status": "active"},
        )
        r.raise_for_status()
        data = r.json()
        events = data.get("events", []) if isinstance(data, dict) else []
        for ev in events:
            ev_title = ev.get("title", "")
            for m in ev.get("markets", []):
                if m.get("closed"):
                    continue
                nm = sc.normalize_polymarket_market(m, ev_title)
                if nm:
                    out.append(nm)
    return out


@mcp.tool()
def scan(query: str, min_gap: float = 0.0, min_similarity: float = 0.33) -> str:
    """Scan Kalshi vs Polymarket for a topic and rank cross-venue Yes-price gaps.

    query: topic/keyword, e.g. "bitcoin 150k", "fed rate cut", "government shutdown".
    min_gap: only return matched pairs whose absolute Yes-probability gap >= this
             (e.g. 0.05 for 5-cent gaps).
    min_similarity: title-match strictness (0-1); lower = more (looser) matches.

    Returns JSON: { query, counts, pairs[], kalshi_only[], polymarket_only[],
    disclaimer }. Each pair has both venues' Yes probability and the signed gap
    (polymarket_yes - kalshi_yes). Gaps are CANDIDATES, not guaranteed arbitrage
    - verify identical resolution terms, and account for spread/fees/settlement.
    """
    warnings = []
    try:
        kalshi = fetch_kalshi(query)
    except Exception as e:
        kalshi = []
        warnings.append(f"Kalshi fetch degraded ({type(e).__name__}); showing partial/Polymarket-only.")
    try:
        poly = fetch_polymarket(query)
    except Exception as e:
        poly = []
        warnings.append(f"Polymarket fetch degraded ({type(e).__name__}).")
    pairs, konly, ponly = sc.match_markets(kalshi, poly, min_similarity=min_similarity)
    if min_gap > 0:
        pairs = [p for p in pairs if p["abs_gap"] >= min_gap]
    result = {
        "query": query,
        "counts": {
            "kalshi_markets": len(kalshi),
            "polymarket_markets": len(poly),
            "matched_pairs": len(pairs),
        },
        "pairs": pairs,
        "kalshi_only": sorted(konly, key=lambda x: x["volume"], reverse=True)[:25],
        "polymarket_only": sorted(ponly, key=lambda x: x["volume"], reverse=True)[:25],
        "warnings": warnings,
        "disclaimer": (
            "Gaps are candidates to investigate, not guaranteed arbitrage. "
            "Verify both markets resolve on the same condition and account for "
            "bid/ask spread, fees, and settlement differences before acting. "
            "Not financial advice."
        ),
    }
    return json.dumps(result)


def fetch_kalshi_history(series, ticker, days=30):
    end = int(time.time())
    start = end - days * 86400
    r = httpx.get(
        f"{KALSHI}/series/{series}/markets/{ticker}/candlesticks",
        params={"period_interval": 1440, "start_ts": start, "end_ts": end},
        timeout=25,
    )
    r.raise_for_status()
    out = []
    for c in r.json().get("candlesticks", []):
        pr = c.get("price", {}) or {}
        p = pr.get("close_dollars") or pr.get("mean_dollars") or pr.get("previous_dollars")
        if p is not None:
            out.append({"t": c.get("end_period_ts"), "p": round(float(p), 4)})
    return out


def fetch_polymarket_history(token, days=30):
    interval = "1m" if days <= 31 else "max"
    r = httpx.get(
        f"{CLOB}/prices-history",
        params={"market": token, "interval": interval, "fidelity": 720},
        timeout=25,
    )
    r.raise_for_status()
    return [{"t": pt["t"], "p": round(float(pt["p"]), 4)} for pt in r.json().get("history", [])]


@mcp.tool()
def pair_history(kalshi_ticker: str = "", polymarket_token: str = "",
                 kalshi_series: str = "", days: int = 30) -> str:
    """Aligned Yes-price history for one Kalshi market and one Polymarket token.

    Use the `ticker`/`series` from a Kalshi market and the `clob_token` from a
    Polymarket market (both returned by `scan`) to chart how each venue's Yes
    price moved over the last `days` days. Returns JSON {kalshi:[{t,p}],
    polymarket:[{t,p}]} with p in 0-1 dollars and t as unix seconds."""
    series = kalshi_series or (kalshi_ticker.split("-")[0] if kalshi_ticker else "")
    k, p = [], []
    if kalshi_ticker and series:
        try:
            k = fetch_kalshi_history(series, kalshi_ticker, days)
        except Exception:
            k = []
    if polymarket_token:
        try:
            p = fetch_polymarket_history(polymarket_token, days)
        except Exception:
            p = []
    return json.dumps({"kalshi": k, "polymarket": p})


@mcp.tool()
def kalshi_snapshot(query: str) -> str:
    """List matching open Kalshi markets for a topic with live Yes bid/ask/prob."""
    kalshi = sorted(fetch_kalshi(query), key=lambda x: x["volume"], reverse=True)
    return json.dumps({"query": query, "count": len(kalshi), "markets": kalshi[:40]})


@mcp.tool()
def polymarket_snapshot(query: str) -> str:
    """List matching active Polymarket markets for a topic with live Yes prob."""
    poly = sorted(fetch_polymarket(query), key=lambda x: x["volume"], reverse=True)
    return json.dumps({"query": query, "count": len(poly), "markets": poly[:40]})


if __name__ == "__main__":
    mcp.run(transport="stdio")
