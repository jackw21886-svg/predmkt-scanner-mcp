"""
Core logic for the prediction-market mispricing scanner.

Pure functions (normalization + fuzzy matching) are kept separate from the
network fetches so they can be unit-tested against fixtures offline.

Everything here works off PUBLIC data:
  - Kalshi:     https://api.elections.kalshi.com/trade-api/v2/events   (no auth)
  - Polymarket: https://gamma-api.polymarket.com/public-search         (no auth)

IMPORTANT: a "gap" surfaced here is a CANDIDATE to investigate, not a
guaranteed arbitrage. Apparent gaps are routinely eaten by differing
resolution criteria/settlement times, bid/ask spread, fees, and USDC-vs-USD
frictions. Always verify the two markets resolve on the *same* condition
before treating a gap as real.
"""
import json
import re
from difflib import SequenceMatcher

# --- text normalization ------------------------------------------------------

_FILLER = {
    "will", "the", "a", "an", "be", "to", "of", "in", "on", "by", "at", "for",
    "before", "after", "during", "this", "that", "is", "are", "who", "what",
    "when", "which", "reach", "reaches", "hit", "hits", "than", "or", "and",
    "market", "resolve", "resolves", "yes", "no", "up", "down", "above", "below",
}


def _stem(tok):
    """Very light stemmer so 'cuts'/'cut', 'rates'/'rate', 'declines'/'decline' match."""
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 4 and tok.endswith("es"):
        return tok[:-2]
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def normalize_title(text):
    """Lowercase, strip punctuation, drop filler, light-stem -> token list + cleaned str."""
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s.$%]", " ", text)
    tokens = [_stem(t) for t in text.split() if t and t not in _FILLER]
    return tokens, " ".join(tokens)


# tokens that carry little discriminating power on their own
_COMMON = {"rate", "rates", "price", "year", "month", "day", "2024", "2025", "2026", "2027", "2028"}


def title_similarity(a, b):
    """Blend of sequence ratio and (rarity-weighted) token Jaccard on titles (0..1)."""
    ta, sa = normalize_title(a)
    tb, sb = normalize_title(b)
    if not sa or not sb:
        return 0.0
    seq = SequenceMatcher(None, sa, sb).ratio()
    seta, setb = set(ta), set(tb)
    union = seta | setb
    inter = seta & setb
    jac = len(inter) / len(union) if union else 0.0
    # bonus: share of DISTINCTIVE tokens that overlap (numbers, names, thresholds)
    distinct_a = {t for t in seta if t not in _COMMON}
    distinct_b = {t for t in setb if t not in _COMMON}
    d_union = distinct_a | distinct_b
    d_inter = distinct_a & distinct_b
    d_overlap = len(d_inter) / len(d_union) if d_union else 0.0
    return 0.30 * seq + 0.40 * jac + 0.30 * d_overlap


# --- normalization of each venue's market into a common shape ----------------

def _mid(bid, ask, last=None):
    bid = float(bid or 0)
    ask = float(ask or 0)
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2, 4)
    if last is not None and float(last or 0) > 0:
        return round(float(last), 4)
    if ask > 0:
        return round(ask, 4)
    if bid > 0:
        return round(bid, 4)
    return None


def normalize_kalshi_market(m, event_title=None, category=None):
    """Kalshi market object -> common dict, or None if not usable."""
    yes_bid = m.get("yes_bid_dollars")
    yes_ask = m.get("yes_ask_dollars")
    last = m.get("last_price_dollars")
    prob = _mid(yes_bid, yes_ask, last)
    if prob is None:
        return None
    title = m.get("title") or event_title or ""
    sub = m.get("yes_sub_title") or ""
    display = title if not sub or sub.lower() in title.lower() else f"{title} - {sub}"
    return {
        "venue": "kalshi",
        "title": display,
        "match_title": f"{title} {sub}".strip(),
        "yes_prob": prob,
        "yes_bid": float(yes_bid or 0),
        "yes_ask": float(yes_ask or 0),
        "volume": float(m.get("volume_fp") or 0),
        "close_time": m.get("close_time"),
        "category": category,
        "ticker": m.get("ticker"),
        "url": f"https://kalshi.com/markets/{m.get('ticker','')}",
    }


def normalize_polymarket_market(m, event_title=None):
    """Polymarket (gamma) market object -> common dict, or None if not usable.

    Only handles binary Yes/No markets (the comparable kind)."""
    outcomes = m.get("outcomes")
    prices = m.get("outcomePrices")
    yes_prob = None
    try:
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(prices, str):
            prices = json.loads(prices)
        if outcomes and prices and len(outcomes) == len(prices):
            for name, px in zip(outcomes, prices):
                if str(name).strip().lower() == "yes":
                    yes_prob = round(float(px), 4)
                    break
    except (ValueError, TypeError):
        pass
    if yes_prob is None:
        yes_prob = _mid(m.get("bestBid"), m.get("bestAsk"), m.get("lastTradePrice"))
    if yes_prob is None:
        return None
    if outcomes and isinstance(outcomes, list) and len(outcomes) not in (0, 2):
        return None
    title = m.get("question") or event_title or ""
    return {
        "venue": "polymarket",
        "title": title,
        "match_title": title,
        "yes_prob": yes_prob,
        "yes_bid": float(m.get("bestBid") or 0),
        "yes_ask": float(m.get("bestAsk") or 0),
        "volume": float(m.get("volumeNum") or m.get("volume") or 0),
        "close_time": m.get("endDateIso") or m.get("endDate"),
        "category": None,
        "ticker": m.get("slug"),
        "url": f"https://polymarket.com/event/{m.get('slug','')}",
    }


# --- matching ---------------------------------------------------------------

def match_markets(kalshi, polymarket, min_similarity=0.33):
    """Greedy best-match Kalshi<->Polymarket by title similarity.

    Returns (pairs, kalshi_only, polymarket_only). Each pair carries the
    signed gap (polymarket_yes - kalshi_yes) and an informational note on
    which side is cheaper. Ranked by absolute gap descending."""
    candidates = []
    for i, k in enumerate(kalshi):
        for j, p in enumerate(polymarket):
            s = title_similarity(k["match_title"], p["match_title"])
            if s >= min_similarity:
                candidates.append((s, i, j))
    candidates.sort(reverse=True)

    used_k, used_p, pairs = set(), set(), []
    for s, i, j in candidates:
        if i in used_k or j in used_p:
            continue
        used_k.add(i)
        used_p.add(j)
        k, p = kalshi[i], polymarket[j]
        gap = round(p["yes_prob"] - k["yes_prob"], 4)
        if gap > 0:
            note = "Polymarket Yes richer; Kalshi Yes cheaper"
        elif gap < 0:
            note = "Kalshi Yes richer; Polymarket Yes cheaper"
        else:
            note = "in line"
        pairs.append({
            "similarity": round(s, 3),
            "gap": gap,
            "abs_gap": abs(gap),
            "note": note,
            "kalshi": k,
            "polymarket": p,
        })
    pairs.sort(key=lambda x: x["abs_gap"], reverse=True)
    kalshi_only = [k for i, k in enumerate(kalshi) if i not in used_k]
    polymarket_only = [p for j, p in enumerate(polymarket) if j not in used_p]
    return pairs, kalshi_only, polymarket_only
