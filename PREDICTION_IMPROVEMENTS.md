# Prediction Improvement Tasks

Branch: `feature/prediction-improvements`

Prioritized list of improvements to the LLM-based financial prediction system. Work through these in order — each builds on the previous.

---

## Priority 1 — Fast Wins (Low effort, high signal)

### [x] 1a. BTC cross-asset feature for altcoin predictions
- Add `btc_return_1h` and `btc_return_24h` as input features for ETH, SOL, AVAX targets
- BTC leads altcoins by 15–60 min; this is the single highest signal-to-effort addition
- Implementation: query BTC price_bars when computing features for any crypto asset, store in `features.feature_values` under new feature set "cross-asset-v1"
- Feed as structured context into LLM prompt alongside existing price features

### [x] 1b. Calibration feedback loop
- Query `evaluation.evaluation_results` for each target's recent prediction history
- Compute: directional accuracy, mean confidence, and Brier score over last 20 predictions
- Inject into LLM prompt as context: "Your recent 20 BTC predictions averaged X% confidence and were correct Y% of the time"
- Location: `app/predictions/llm_engine.py` — add calibration query before prompt assembly
- This is free accuracy from data already stored; no new APIs needed

---

## Priority 2 — New Data Sources (Medium effort, strong new signal)

### [x] 2a. Add 2-year treasury yield from FRED
- Already authenticated with FRED API (`app/connectors/fred.py`)
- Add series `DGS2` (2-Year Treasury Constant Maturity Rate)
- Compute and store yield curve slope = 10Y yield − 2Y yield
- Add `yield_curve_slope` to macro feature context injected into LLM prompt
- Key signal for risk-on/risk-off regime detection

### [x] 2b. Economic calendar (FOMC/CPI dates)
- Source: FRED release schedule API or Econoday
- Store upcoming high-impact events (FOMC meetings, CPI, PPI, NFP) in `ingestion.normalized_events`
- Add `days_until_next_fomc` and `days_until_next_cpi` as features
- Context: pre-FOMC drift and CPI-day volatility are well-documented — LLM should know timing

---

## Priority 3 — Feature Engineering (Medium effort, structured signal)

### [x] 3a. Rolling cross-asset correlation matrix
- Compute 7-day and 30-day rolling Pearson correlation between all asset pairs
- Key pairs: BTC↔ETH, BTC↔SPY, GLD↔10Y yield, USO↔WTI crude
- Store in `features.feature_values` as JSON arrays under new feature set
- Feed correlation regime context to LLM: "BTC-SPY 30d correlation is currently 0.82 (high)"
- A shift in correlation regime is itself a predictive signal

### [x] 3b. Volume-weighted features
- OHLCV already stored in `market_data.price_bars` — volume column is unused
- Add: volume ratio (current vs 20-bar avg), price move × volume confirmation flag
- A 2% move on 3× avg volume is a stronger signal than thin-volume moves
- Add to "price-baseline-v1" feature set or create "price-volume-v1"

### [x] 3c. Temporal and regime features
- Day of week (crypto has Mon/Fri patterns; equities have Monday effect)
- Realized volatility (rolling std of returns, separate from price std)
- Days since last >3% daily move (mean reversion timing)
- Store in feature_values, pass to LLM as structured fields

---

## Priority 4 — New External Data (Higher effort, unique alpha)

### [x] 4a. On-chain data for crypto (Glassnode or CryptoQuant)
- Exchange net inflows/outflows (coins moving to/from exchanges = selling/buying pressure)
- Miner outflows, long-term holder behavior
- Glassnode has a free tier; CryptoQuant is paid
- Add new connector `app/connectors/glasschain.py`

### [x] 4b. CFTC Commitment of Traders (COT) reports
- Public API at cftc.gov — no auth needed
- Weekly net institutional positioning on futures (gold, crude, BTC CME)
- Strong signal for commodity targets (GLD, USO)
- Add connector, store in `ingestion.raw_source_records`

### [x] 4c. Options flow / put-call ratio
- CBOE publishes free daily put/call ratios
- VIX term structure (spot vs futures) as a fear measure
- Complements Fear & Greed Index already collected from Alternative.me

---

## Priority 5 — Model Architecture (Larger effort, systematic improvement)

### [x] 5a. Statistical ensemble model alongside LLM
- Train logistic regression or LightGBM on historical `feature_values` + `evaluation_results`
- Store predictions from both models in `predictions.model_versions` (already supports multi-model)
- Average or weight-blend LLM probability and statistical model probability
- Requires enough evaluation history to train on (~100+ settled predictions per target)

### [x] 5b. Probability calibration layer
- Apply isotonic regression calibration to LLM output probabilities
- LLMs tend to be overconfident — calibration corrects systematic bias
- Train calibrator per target using `evaluation_results`
- Requires evaluation history first (do 5a prerequisites first)

---

## Schema / Infrastructure Notes
- New features → `features.feature_values` with appropriate `feature_set_id`
- New connectors → register in `ops.api_sources` via `seed.py`
- Cross-asset features need `asset_id` = NULL or a special "cross-asset" asset row
- All new features need `available_at` timestamps to prevent future data leakage
- LLM prompt assembly lives in `app/predictions/llm_engine.py`

## Status
- [x] Branch created: `feature/prediction-improvements`
- [x] 1a BTC cross-asset feature
- [x] 1b Calibration feedback loop
- [x] 2a 2Y treasury / yield curve slope
- [x] 2b Economic calendar features
- [x] 3a Cross-asset correlation matrix
- [x] 3b Volume-weighted features
- [x] 3c Temporal/regime features
- [x] 4a On-chain data connector
- [x] 4b COT reports connector
- [x] 4c Options flow connector
- [x] 5a Statistical ensemble model
- [x] 5b Probability calibration layer
