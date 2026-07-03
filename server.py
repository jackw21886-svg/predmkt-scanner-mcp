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

import httpx
from mcp.server.fastmcp import FastMCP

import scanner_core as sc

mcp = FastMCP("predmkt-scanner")

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA = "https://gamma-api.polymarket.com"


def _query_tokens(query):
    toks, _ = sc.normalize_title(query)
    return [t for t in toks if len(t) >= 3]


def fetch_kalshi(query, max_pages=8, page_size=200):
    """Page open Kalshi events and keep markets whose title matches a query token."""
    qtokens = _query_tokens(query)
    out, cursor = [], None
    with httpx.Client(timeout=25) as client:
        for _ in range(max_pages):
            params = {"status": "open", "with_nested_markets": "true", "limit": page_size}
            if cursor:
                params["cursor"] = cursor
            r = client.get(f"{KALSHI}/events", params=params)
            r.raise_for_status()
            data = r.json()
            for ev in data.get("events", []):
                ev_title = ev.get("title", "")
                cat = ev.get("category")
                for m in ev.get("markets", []):
                    hay, _ = sc.normalize_title(
                        f"{ev_title} {m.get('title','')} {m.get('yes_sub_title','')}"
                    )
                    if qtokens and not any(t in hay for t in qtokens):
                        continue
                    nm = sc.normalize_kalshi_market(m, ev_title, cat)
                    if nm:
                        out.append(nm)
            cursor = data.get("cursor")
            if not cursor:
                break
    return out


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
def scan(query: str, min_gap: float = 0.0, min_similarity: float = 0.45) -> str:
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
    kalshi = fetch_kalshi(query)
    poly = fetch_polymarket(query)
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
        "disclaimer": (
            "Gaps are candidates to investigate, not guaranteed arbitrage. "
            "Verify both markets resolve on the same condition and account for "
            "bid/ask spread, fees, and settlement differences before acting. "
            "Not financial advice."
        ),
    }
    return json.dumps(result)


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
