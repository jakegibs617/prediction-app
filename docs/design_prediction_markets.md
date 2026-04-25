# Design — Polymarket / Kalshi Integration

> **Status:** design only. No code shipped in this commit.
> **Branch:** `feature/expand-data-sources`
> **Author:** Bernard (assistant) — drafted 2026-04-25
> **Reviewer:** Jacob — please sanity check before implementation begins.

## Goal

Pull live **prediction-market-implied probabilities** from Polymarket and Kalshi and use them to:

1. **Calibrate** our LLM-generated predictions — when our model says "BTC up >2% in 24h with probability 0.65" and a Polymarket contract on roughly the same question is priced at 0.42, that delta is a measurable calibration signal we should track over time.
2. **Enrich** future predictions — pass nearby market-implied probabilities into the LLM prompt as a strong prior.
3. **Backtest** model accuracy against an established prior — Polymarket has been historically well-calibrated on macro/political questions ([Manifold's calibration analysis](https://manifold.markets/calibration), Polymarket internal metrics).

This is **fundamentally different** from every other data source we currently ingest. The other sources are inputs to a forecast. Prediction markets are competing forecasts.

## What's free + verified live

| Source | Endpoint | Auth | Status |
|---|---|---|---|
| Polymarket (Gamma API) | `https://gamma-api.polymarket.com/markets` | none | ✓ verified, returns `id, question, outcomes, lastTradePrice, liquidity, endDate, volume24hr` |
| Polymarket (CLOB API) | `https://clob.polymarket.com/markets` | none for read | live order books and last-trade prices |
| Kalshi (public) | `https://api.elections.kalshi.com/trade-api/v2/markets` | none for GET | ✓ verified, returns `event_ticker, last_price_dollars, status, close_time, custom_strike` |

Both APIs are read-free with no key. **Trading** would require auth + KYC + funded accounts — out of scope for now.

## Schema implications

Adding prediction-market data raises a question: do these belong in the existing tables, or do they need their own?

### Option A — squeeze into existing tables

Treat each market as a `raw_source_records` row under a new category, e.g. `prediction_market`. Pros: minimal schema changes. Cons:
- Markets are continuous-pricing instruments, not point-in-time observations — we'd want order-book history, not just the latest price.
- The `predictions.predictions` table is already crowded; injecting "external" predictions confuses the calibration math.

### Option B — new dedicated table (recommended) ★

```sql
CREATE TABLE predictions.market_implied_probabilities (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id uuid NOT NULL REFERENCES ops.api_sources(id),
    external_market_id text NOT NULL,             -- e.g. polymarket id, kalshi ticker
    market_question text NOT NULL,                -- raw market text
    market_url text,
    outcome_label text NOT NULL,                  -- e.g. "Yes", "No", "BTC up 2%"
    implied_probability numeric(6, 5) NOT NULL,   -- 0.0..1.0 from latest trade price
    liquidity_usd numeric(20, 2),
    volume_24h_usd numeric(20, 2),
    settles_at timestamptz NOT NULL,              -- market close / resolution date
    snapshot_at timestamptz NOT NULL DEFAULT now(),
    raw_payload jsonb NOT NULL,
    correlated_target_id uuid REFERENCES predictions.prediction_targets(id),
    correlation_quality text                       -- 'exact' | 'close' | 'related' | 'unrelated'
);

CREATE INDEX market_implied_probs_external_id_snapshot_idx
    ON predictions.market_implied_probabilities (external_market_id, snapshot_at DESC);

CREATE INDEX market_implied_probs_correlated_target_idx
    ON predictions.market_implied_probabilities (correlated_target_id, snapshot_at DESC);
```

Why a separate table:
1. The shape is fundamentally different: we record an **outcome → probability**, not a feature value.
2. We need cheap "what's the latest price for this market" lookups — separate table = simple `ORDER BY snapshot_at DESC LIMIT 1`.
3. We can join to `prediction_targets` for calibration metrics without polluting the main predictions feed.

### Option C — hybrid

Continue ingesting full market metadata into `raw_source_records` (preserves audit trail) AND project the latest price into `market_implied_probabilities`. Best of both. Mirrors how `market_data.price_bars` is derived from raw asset data.

**Recommended:** Option C.

## Question → target mapping (the hard part)

Polymarket has 30,000+ open markets. Most are unrelated to our 6 prediction targets. We need a strategy for which markets to track.

### Approach 1: hand-curated short list (start here)
At any moment, ~5-15 Polymarket markets directly correspond to our crypto/equity/commodity targets:
- "Will BTC reach $100K by [date]?"
- "Will the S&P 500 close higher in May?"
- "Will WTI crude average above $X this month?"

Maintain a SQL-seeded mapping table:
```sql
CREATE TABLE predictions.market_target_mappings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    target_id uuid NOT NULL REFERENCES predictions.prediction_targets(id),
    external_market_id text NOT NULL,
    source_id uuid NOT NULL REFERENCES ops.api_sources(id),
    correlation_quality text NOT NULL,             -- 'exact' | 'close' | 'related'
    notes text,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (target_id, external_market_id)
);
```

A human (us) seeds this once a month with a script that reads market questions and labels them. The connector then ONLY ingests markets in this mapping table — keeps the data volume sane.

### Approach 2: keyword-filtered ingestion (for future)
Use the same `_ASSET_TYPE_KEYWORDS` table that already drives news routing in `app/predictions/llm_engine.py`. Match market questions against keywords; store all matches but only auto-correlate the top hits.

### Approach 3: LLM auto-mapping (future, high effort)
Feed each new market question to qwen3.5 with the prompt "Does this market correspond to any of these prediction targets? Return JSON." High accuracy but burns LLM tokens at scale.

**Recommended start:** Approach 1, wired so we can graduate to Approach 2 later without schema changes.

## Connector design

Two separate connectors, one orchestration job (`market_implied_ingest`, runs hourly):

```
app/connectors/polymarket.py
app/connectors/kalshi.py
```

Each iterates over its mapped markets (joining `market_target_mappings` to get the list), GETs the current price, writes to `raw_source_records` (full payload) AND projects into `market_implied_probabilities` (latest snapshot). Pattern matches the existing AlphaVantage → `price_bars` derivation.

## Calibration loop

Once we have implied probabilities flowing in, build:

```
app/evaluation/calibration.py
```

A scheduled job (daily, after `evaluation`) that for every newly-evaluated `predictions.predictions` row:
1. Looks up the latest `market_implied_probabilities` snapshot whose `correlated_target_id` matches at the prediction's `created_at`.
2. Computes `prediction.probability - market_implied_probability` = our edge.
3. Stores it in a new `evaluation.calibration_deltas` table:
   ```
   prediction_id, market_implied_at_creation, our_probability, edge,
   actual_outcome, our_brier, market_brier
   ```
4. After enough samples, computes rolling stats: "We're 0.08 more bullish than Polymarket on crypto on average" / "Our Brier score is 0.04 better than market on equity targets".

This is the calibration goldmine.

## Telegram alert evolution

Current alert: "BTC up >2% in 24h, prob=0.65, summary=...".
Phase 2 alert with this data: "BTC up >2% in 24h. Our model: 0.65. Polymarket: 0.42. Edge: +0.23. Calibration error 30d: -0.05 (we run ~5 points hotter than market on crypto)."

Adds genuine signal to the alert. Implement as an optional extra block in `app/alerts/rules.py::format_alert_payload`.

## Open questions for review

1. **Storage cost.** Hourly snapshots × ~20 mapped markets × 30 days = ~14,400 rows/month. Trivial. ✓
2. **Polymarket UMA-resolution lag.** Markets sometimes show "0.99" for hours/days after the underlying event resolves while UMA verifies. Need a "freshness" filter: only use markets where `snapshot_at - last_trade_at < 12h`.
3. **Should we also ingest Manifold Markets?** Free, no auth, similar shape, less liquidity but more diverse questions. Probably yes as a Tier-2 follow-up.
4. **Trading.** Genuine open question. Once we have a calibrated edge, do we eventually open a Polymarket account and trade against it? Out of scope for v1, but the schema above doesn't preclude it.
5. **Resolution events as a normalized event type.** When a market resolves, that's a discrete "event" we should also ingest as a `prediction_market_resolution` event, mostly for backtesting.

## Implementation order (if we proceed)

1. **Migration** — `sql/003_prediction_markets.sql` creating `market_implied_probabilities` and `market_target_mappings`.
2. **PolymarketConnector** + tests + live verification.
3. **KalshiConnector** + tests + live verification.
4. **Seed one mapping** by hand for, say, BTC (find a real Polymarket market question that ~matches "BTC up >2% in 24h"), confirm end-to-end.
5. **Calibration job** — `app/evaluation/calibration.py` + table.
6. **Alert formatter** — extend `format_alert_payload` to optionally include the market-implied delta.
7. **Backfill UI / report** — small script that prints "calibration last 30d" by target.

Each step is an afternoon. Total: maybe 3-5 days of careful work.

## Tradeoffs

**Why this is worth doing:**
- It turns our prediction app from "one model talking to itself" into "one model competing against the wisdom of crowds, with measurable score".
- The calibration signal is the most valuable thing we could ever feed back into the model.
- Both Polymarket and Kalshi have liquid markets across exactly the asset classes we already target.

**Why we're not shipping it today:**
- Schema design needs review.
- Question-to-target mapping is genuinely subjective and we should agree on the seed list before code is written.
- Misaligned market mappings would poison the calibration data and be hard to reverse out.

I'd rather we talk through the schema and the mapping strategy first, then implement.

---

When you've reviewed this, ping me with:
- ✅ approve, build it
- 🔁 changes (point at sections)
- ❌ defer, focus elsewhere
