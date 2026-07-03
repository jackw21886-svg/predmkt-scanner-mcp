# Prediction-market mispricing scanner (MCP, read-only)

Compares **Kalshi** vs **Polymarket** implied probabilities for the same
real-world event and ranks the biggest cross-venue Yes-price gaps. Both data
sources are public/unauthenticated, so this server needs **no API key, wallet,
or secret**, and it places **no orders**.

## Tools

- `scan(query, min_gap, min_similarity)` — fetch both venues for a topic,
  fuzzy-match titles, return ranked cross-venue gaps + unmatched markets.
- `kalshi_snapshot(query)` — matching Kalshi markets with live Yes bid/ask.
- `polymarket_snapshot(query)` — matching Polymarket markets with live Yes prob.

## Deploy (Railway)

1. Push this folder to a new GitHub repo (public or private — no secrets).
2. Railway: New Project > GitHub Repository > select the repo.
3. Settings > Networking > Generate Domain. Check the deploy logs for the
   bound port (Railway injects `PORT`, usually 8080) and set the domain's
   target port to match.
4. No environment variables needed.
5. MCP endpoint is `https://<your-app>.up.railway.app/mcp`.

## Add to Claude

Customize > Connectors > "+" > Add custom connector, paste the `/mcp` URL.
No OAuth needed.

## IMPORTANT — this is a research tool, not a money printer

A surfaced gap is a **candidate to investigate**, not guaranteed arbitrage.
Before treating any gap as real:

- Confirm both markets resolve on the **exact same condition** (wording,
  threshold, date, settlement source). "Bitcoin $150k in 2026" on one venue
  may use a different index/cutoff than the other.
- Account for **bid/ask spread** (the gap uses mid/last prices), **fees**, and
  **settlement timing**.
- Remember Polymarket settles in **USDC (crypto)** and Kalshi in **USD** — the
  cross-venue capital/FX/withdrawal friction is real.

Not financial advice. This tool never trades on your behalf.
