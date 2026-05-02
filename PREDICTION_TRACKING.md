# Prediction Tracking & Training Surface

Branch: `feature/prediction-improvements` (continue here) or new branch

Goal: make every probability in the pipeline a first-class queryable column, then expose a
clean training surface so the ensemble/calibration models — and any future model — can train
on `(features, probabilities, outcome)` without reconstructing multi-table joins.

---

## Step 1 — First-class probability columns

Right now `llm_probability` and `pre_calibration_probability` are buried in the `rationale`
JSON column. They can't be aggregated, indexed, or trained on without JSON parsing.

### [x] 1a. SQL migration
Add two nullable columns to `predictions.predictions`:

```sql
ALTER TABLE predictions.predictions
    ADD COLUMN llm_probability      NUMERIC(7,5),
    ADD COLUMN pre_cal_probability  NUMERIC(7,5);
```

- `llm_probability` — the raw LLM output before ensemble blending (NULL for heuristic fallbacks)
- `pre_cal_probability` — the blended probability before isotonic calibration (NULL when no
  calibrator was applied)
- Both are NULL-safe: old rows stay valid; new rows populate them going forward

### [x] 1b. Update `PredictionRecord` (predictions/logic.py)
Add `llm_probability: float | None = None` and `pre_cal_probability: float | None = None`
to the dataclass.

### [x] 1c. Capture values in service.py
- Record `llm_prob = prediction_input.probability` immediately after
  `generate_llm_prediction_input` returns (before blend)
- Record `pre_cal_prob = prediction_input.probability` after `maybe_blend_with_ensemble`
  returns (before calibration)
- Pass both into `build_prediction_record`

### [x] 1d. Update `write_prediction_record` INSERT
Add the two new columns to the INSERT statement in service.py.

### [x] 1e. Tests
- Unit test that service captures `llm_probability` before blending
- Unit test that service captures `pre_cal_probability` before calibration
- Verify both are NULL when heuristic fallback is used

---

## Step 2 — Training examples view

A single view that gives any training or analysis query a clean
`(prediction metadata, probability decomposition, outcome)` row per settled prediction.
No feature vectors here — those stay in `features.feature_values` and are joined at
training time by the model trainers.

### [x] 2a. Create `ml.training_examples` view (or schema `evaluation`)

```sql
CREATE SCHEMA IF NOT EXISTS ml;

CREATE OR REPLACE VIEW ml.training_examples AS
SELECT
    p.id                        AS prediction_id,
    p.target_id,
    p.asset_id,
    p.feature_snapshot_id,
    p.created_at,
    p.model_version_id,
    p.prediction_mode,
    p.llm_probability,
    p.pre_cal_probability,
    p.probability               AS final_probability,
    p.hallucination_risk,
    p.probability_extreme_flag,
    er.directional_correct,
    er.brier_score,
    er.return_pct,
    er.cost_adjusted_return_pct,
    er.calibration_bucket,
    er.actual_outcome,
    er.evaluated_at
FROM predictions.predictions p
JOIN evaluation.evaluation_results er
    ON er.prediction_id = p.id
   AND er.evaluation_state = 'evaluated';
```

### [x] 2b. Refactor ensemble `_load_training_data`
Replace the current multi-join query in `ensemble_engine.py` with a join against
`ml.training_examples`. The feature vector join against `feature_values` stays —
only the outcome/probability side simplifies.

### [x] 2c. Refactor calibration `_load_calibration_data`
Replace the current query in `calibration.py` with a query against `ml.training_examples`
(`SELECT final_probability, directional_correct ... ORDER BY final_probability ASC`).

---

## Step 3 — Probability decomposition analysis query

Once Step 1 is live, a single query becomes possible that was previously impractical:

```sql
-- How much does ensemble blending and calibration each move the probability?
SELECT
    target_id,
    AVG(llm_probability)           AS avg_llm_prob,
    AVG(pre_cal_probability)       AS avg_pre_cal_prob,
    AVG(final_probability)         AS avg_final_prob,
    AVG(llm_probability - final_probability)      AS avg_llm_vs_final_delta,
    AVG(brier_score)               AS avg_brier,
    COUNT(*)                       AS n
FROM ml.training_examples
GROUP BY target_id;
```

### [x] 3a. Add this as a named query / script in `sql/analysis/probability_decomposition.sql`
- Useful for understanding whether ensemble and calibration are each adding value
- Run after enough evaluation history accumulates (~50+ settled predictions per target)

---

## Notes
- Steps 1 and 2 are independent of each other; 2b/2c depend on 1 being deployed so the
  new columns are populated in new rows
- Step 3 is analysis-only; no code changes needed beyond the SQL file
- Future direction: once `ml.training_examples` has sufficient history, can train a
  sequence model (e.g. LSTM or simple RNN) on `(feature_snapshot, llm_probability, outcome)`
  ordered by `created_at` — the temporal ordering is the signal the ensemble logistic
  regression ignores
