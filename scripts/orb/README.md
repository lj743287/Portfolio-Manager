# ORB continuation screener

Moved from lj743287/orb-screener to use this repository's working Alpaca
credentials. The strategy calculations and parameters are unchanged.

Workflow: `.github/workflows/orb.yml`. Runs Monday-Friday at 22:15 UTC
(23:15 BST, 22:15 GMT), or manually from GitHub Actions.

Uses Alpaca IEX daily bars, the same feed as this repository's market-conditions
job. IEX is a single-exchange feed, not consolidated whole-market SIP data;
prices, volumes and screening results can differ from Twelve Data.
The ORB screener has no Twelve Data fallback. Historical bars and secrets are
not committed. Only the resulting watchlist is published under `orb/`.

The existing portfolio pages and market-conditions workflow are unchanged.
The shared concurrency group serialises both jobs to avoid competing for the
Alpaca API request allowance. No brokerage orders are placed.

The older `fetch_bars.py` module is retained solely for its universe and session
helpers imported by `fetch_bars_alpaca.py`; it is never executed by this workflow.
