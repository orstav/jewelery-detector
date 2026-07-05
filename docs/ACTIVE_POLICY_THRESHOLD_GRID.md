# Active Policy Threshold Grid

Read-only threshold search over the deployed live-only crop candidate policy.

Tool:

```text
tools/evaluate_active_policy_threshold_grid.py
```

The tool rebuilds runtime-style candidates, then evaluates candidate/review/auto thresholds through `jewelry_detector_db.decide_match(...)`. It does not write to the DB and uses product IDs only as evaluation labels.

## Current active policy baseline on dev split

From `workbench/active-policy-threshold-grid/all.json`:

| Policy | Auto total | Auto correct | Auto wrong | Auto precision | Correct auto recall |
|---|---:|---:|---:|---:|---:|
| current active DB policy (`0.82/0.86/0.93/margin 0.03`) | 442 | 308 | 134 | 69.68% | 30.95% |

This is too permissive for autonomous matching.

## Strict safe candidate

From `workbench/active-policy-threshold-grid/all_strict.json`:

```text
candidate_min_score = 0.94
review_min_score    = 0.96
auto_match_score    = 0.96
margin_threshold    = 0.12
```

| Split | Total | Auto correct | Auto wrong | Auto precision | Correct auto recall | Review | No match |
|---|---:|---:|---:|---:|---:|---:|---:|
| live | 325 | 6 | 0 | 100.00% | 1.85% | 263 | 56 |
| studio | 663 | 47 | 0 | 100.00% | 7.09% | 444 | 172 |
| unknown | 7 | 0 | 0 | — | 0.00% | 5 | 2 |
| all | 995 | 53 | 0 | 100.00% | 5.33% | 712 | 230 |

## Decision

This is deployable only as a **safe auto-match policy**, not as a recall improvement. It deliberately sends many cases to review/no-match instead of making risky automatic choices.

Deployment shape:

- insert/activate a new matching policy row, e.g. `jewelry-siglip-live-crop-safe-v1`
- same model and preprocess versions as `jewelry-siglip-live-crop-v1`
- thresholds above
- exactly one active policy after transaction

Rollback:

```sql
BEGIN;
UPDATE matching_policies SET active = false WHERE active = true;
UPDATE matching_policies SET active = true WHERE name = 'jewelry-siglip-live-crop-v1';
COMMIT;
```
