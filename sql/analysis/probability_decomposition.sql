-- How much do ensemble blending and calibration each move the probability?
-- Best run after enough evaluation history accumulates, around 50+ settled
-- predictions per target.
SELECT
    target_id,
    AVG(llm_probability)                 AS avg_llm_prob,
    AVG(pre_cal_probability)             AS avg_pre_cal_prob,
    AVG(final_probability)               AS avg_final_prob,
    AVG(llm_probability - final_probability) AS avg_llm_vs_final_delta,
    AVG(brier_score)                     AS avg_brier,
    COUNT(*)                             AS n
FROM ml.training_examples
GROUP BY target_id;
