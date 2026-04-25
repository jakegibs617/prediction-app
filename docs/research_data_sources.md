# Free Public APIs — Candidate Data Sources

> Research note for **`feature/expand-data-sources`** branch.
> Goal: find genuinely free, no-paywall, programmatic APIs we could plumb into the prediction app to enrich features and predictions. All entries below were endpoint-tested live before being listed (`status=200` + sane payload), unless marked otherwise.

## Already wired (skip)
Alpha Vantage (equity/ETF prices), CoinGecko (crypto), FRED (US macro), NewsAPI (news), GDELT (geopolitical events), SEC EDGAR (filings), NOAA (weather), USGS (earthquakes), World Bank (long-term macro), IMF (macro).

---

## Tier 1 — Highest signal-to-effort ratio

These are the strongest additions for a probabilistic forecasting pipeline, all key-free or near-zero-friction.

### 1. Alternative.me — Crypto Fear & Greed Index ★
- **URL:** `https://api.alternative.me/fng/`
- **Auth:** none
- **Rate limit:** none documented
- **Payload size:** tiny (~200 B)
- **Signal:** classic crowd-sentiment index for crypto, 0–100 scale. **Highly correlated with crypto momentum reversals.** Strong feature for BTC/ETH targets.
- **Verified live:** ✓ (returned current value 31 / "Fear")
- **Effort:** ~30 LOC connector

### 2. mempool.space — Bitcoin Network Stats ★
- **URL:** `https://mempool.space/api/v1/fees/recommended`, plus `/api/v1/blocks`, `/api/v1/difficulty-adjustment`, `/api/mempool`
- **Auth:** none
- **Rate limit:** generous; community-funded
- **Signal:** real on-chain conditions (fees, block times, mempool depth, hashrate). **Directly orthogonal to price** — a strong tail signal for BTC predictions. Spikes in mempool fees often precede major price moves.
- **Verified live:** ✓
- **Effort:** ~50 LOC, multiple endpoints

### 3. blockchain.info Charts — Bitcoin On-chain Aggregates
- **URL:** `https://blockchain.info/charts/{metric}?format=json` (n-transactions, hash-rate, market-cap, mempool-size, miners-revenue, etc.)
- **Auth:** none
- **Signal:** smoothed long-horizon on-chain features (hash rate growth, transaction volume, miner revenue). Pairs well with mempool.space for short-term tactical signal.
- **Verified live:** ✓
- **Effort:** ~30 LOC

### 4. US Treasury — Fiscal Data API ★
- **URL:** `https://api.fiscaldata.treasury.gov/services/api/fiscal_service/...`
- **Auth:** none
- **Rate limit:** generous, no key
- **Signal:** daily Treasury rates (`avg_interest_rates`), debt-to-the-penny, daily cash balances. Filling the gap that FRED has slow updates on. Especially strong for **TLT predictions** and macro features for SPY.
- **Verified live:** ✓
- **Effort:** ~40 LOC

### 5. BLS — Bureau of Labor Statistics ★
- **URL:** `https://api.bls.gov/publicAPI/v1/timeseries/data/{seriesID}`
- **Auth:** key optional (without it: 25 req/day limit; with it: 500 req/day — free, instant signup)
- **Rate limit:** as above
- **Signal:** CPI (`CUUR0000SA0`), unemployment (`LNS14000000`), PPI, jobs reports. Authoritative macro feed; FRED republishes from BLS but lags. Good for SPY/QQQ predictions.
- **Verified live:** ✓
- **Effort:** ~50 LOC, key in `.env`

### 6. Reddit (public JSON) — Social Sentiment ★
- **URL:** `https://www.reddit.com/r/{sub}/{sort}.json` (public read; needs `User-Agent` header)
- **Auth:** none for read; OAuth optional for higher limits
- **Rate limit:** ~60 req/min/IP without OAuth
- **Signal:** WallStreetBets / cryptocurrency / stocks subreddits — top posts and rising posts as a contrarian/confirming sentiment signal. Pairs with the existing news pipeline.
- **Verified live:** ✓
- **Effort:** ~60 LOC; needs LLM extraction step similar to news pipeline

### 7. Hacker News (Firebase) — Tech-tilted News
- **URL:** `https://hacker-news.firebaseio.com/v0/topstories.json`, then `/item/{id}.json`
- **Auth:** none
- **Rate limit:** none documented
- **Signal:** front-page stories — leading indicator for tech-sector sentiment (relevant for QQQ). Lower-noise than Reddit.
- **Verified live:** ✓
- **Effort:** ~40 LOC

---

## Tier 2 — Useful but secondary

### 8. Frankfurter — FX Rates
- **URL:** `https://api.frankfurter.dev/v1/latest?from=USD`
- **Auth:** none (and no key needed at all)
- **Signal:** ECB-published FX rates. Useful for cross-currency context when extending to non-USD assets.
- **Verified live:** ✓
- **Effort:** ~25 LOC

### 9. Coinbase — Crypto Reference Rates
- **URL:** `https://api.coinbase.com/v2/exchange-rates?currency=BTC` and `https://api.exchange.coinbase.com/products/{pair}/candles`
- **Auth:** none for public data
- **Signal:** Coinbase's own price feed — independent corroboration of CoinGecko data, plus better US-aligned timestamps for crypto/USD pairs.
- **Verified live:** ✓
- **Effort:** ~40 LOC

### 10. EIA — Energy Information Administration
- **URL:** `https://api.eia.gov/v2/...`
- **Auth:** key required (free, instant signup)
- **Signal:** crude oil inventory, gasoline stocks, natural gas, electricity. **Direct signal for USO predictions.** Already in `.env` (`EIA_API_KEY`) but no connector — was on yesterday's deferred list.
- **Verified live:** key already provisioned
- **Effort:** ~80 LOC; the API is REST-but-with-quirky-pagination

### 11. CoinPaprika / CoinCap — Crypto Backup
- **URL:** `https://api.coinpaprika.com/v1/tickers` or `https://api.coincap.io/v2/assets`
- **Auth:** none
- **Signal:** redundant data feed for CoinGecko, useful as fail-over. Not signal-additive on its own.
- **Effort:** ~30 LOC

### 12. CoinDesk — BTC Price Index (legacy)
- **URL:** `https://api.coindesk.com/v1/bpi/currentprice.json`
- **Auth:** none
- **Status:** still up but deprecated; CoinDesk Data has moved to a paid product. Not recommended for new work.

---

## Tier 3 — Notable but not a priority

| API | Note |
|---|---|
| **Yahoo Finance (`yfinance` Python lib)** | Unofficial, rate-limited, breaks periodically. Already covered by Alpha Vantage. |
| **Polygon.io** | Excellent data but free tier is 5 req/min, mostly delayed end-of-day. Cheap paid tier ($30/mo) is competitive if budget opens up. |
| **CryptoCompare** | Free tier 100 K calls/mo, requires key. Nice extra crypto reference. |
| **AlphaVantage News & Sentiment** | Already have AV; the news endpoint is on free tier with daily limits. Worth flipping on if NewsAPI rate limits bite. |
| **Twitter/X API** | No longer practical on free tier. Skip. |
| **Binance** | US-restricted (returns 451 from US IPs). Skip. |
| **api.exchangerate.host** | Free tier removed in 2024. Skip. |
| **Polygon flat files** | Powerful for backtesting but not free. Defer. |
| **Polymarket / Kalshi** | Prediction markets — fascinating cross-check on our own predictions (calibration source). Free public APIs exist for both. **Worth a follow-up branch on its own.** |

---

## Recommended next steps (in priority order)

1. **Crypto Fear & Greed (Tier 1.1)** — tiny, zero-friction, immediate signal for BTC/ETH targets.
2. **mempool.space (Tier 1.2)** — orthogonal Bitcoin signal, no key.
3. **US Treasury (Tier 1.4)** — strong for TLT predictions and macro context.
4. **EIA (Tier 2.10)** — already have the key in `.env`, just needs the connector. Direct USO signal.
5. **Reddit WSB sentiment (Tier 1.6)** — needs LLM extraction step but pipeline already supports it.
6. **BLS (Tier 1.5)** — fast macro feed, decision later on whether to add a key.

The first four can each be added in a single afternoon — small, focused PRs against this branch.

## Pattern for adding a new connector

Mirror the shape of `app/connectors/coingecko.py` or `app/connectors/fred.py`:

1. Subclass `BaseConnector` from `app/connectors/base.py`.
2. Implement `discover()` (which assets / series the connector can fetch) and `fetch_window(...)` (return a list of `RawSourceRecord`-shaped dicts).
3. Register it in `app/ops/orchestrator.py`'s ingestion loop.
4. Insert a row into `ops.api_sources` with `name`, `category` (`market_data` | `macro` | `news` | `events`), and `auth_type`.
5. If needed, add corresponding `prediction_targets` rows that consume the new data.
6. Add unit tests in `tests/test_connector_<name>.py`.

The normalization pipeline already routes by `category`:
- `market_data` and `macro` records skip the LLM (validated structurally).
- `news` and `events` records run through `mistral:7b` / `qwen3.5:latest` for extraction.

So the choice of `category` determines whether each new source flows through the LLM step.
