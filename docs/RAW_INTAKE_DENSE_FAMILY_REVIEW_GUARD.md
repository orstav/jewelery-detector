# Dense-family review guard

Date: 2026-07-04
Branch: `raw-intake-embedding-consensus`

## Scope

Adds a production-safety guard to the existing detector DB decision policy. It does not change retrieval, does not use filenames, and does not use probe catalog ID/prefix as a feature.

The guard runs after candidate aggregation and before auto-match:

```text
candidate embeddings -> product candidates -> dense-family low-margin guard -> matched / needs_review / no_match
```

## Rule

If the top product has a high score but another close candidate from the same product-code family is nearby, require a stronger margin before auto-matching.

Default behavior for policy-grid work:

```text
dense_family_guard_enabled = false unless explicitly set by policy
same_design_review_margin = max(policy.margin_threshold * 2, 0.08) when enabled
dense_family_min_score = policy.auto_match_score
dense_family_score_delta = same_design_review_margin
same_family_numeric_window = unset, meaning same product-letter family such as R/R or E/E
```

The first all-prefix prototype was intentionally conservative and is documented below as a safety bound, not a final policy.

If triggered, decision becomes:

```json
{
  "status": "needs_review",
  "reason": "dense_family_low_margin"
}
```

## Why

Hard-negative mining showed most wrong Top-1 errors are not broad retrieval misses. They are high-confidence sibling/family collisions.

From the hard-negative run:

```text
549 hard negatives from 995 dev probes
450 wrong Top-1 scores >= 0.95
278 same-prefix/family risks
94 close-margin sibling risks
```

## Dev policy impact

Simulation on the 995-probe dev split, using current candidate aggregation and policy values:

### Before guard

| Outcome | Count |
|---|---:|
| Auto matched | 430 |
| Auto matched correct | 292 |
| Auto matched wrong | 138 |
| Review | 558 |
| No match | 7 |

### After guard

| Outcome | Count |
|---|---:|
| Auto matched | 221 |
| Auto matched correct | 192 |
| Auto matched wrong | 29 |
| Dense-family review | 567 |
| Other review | 192 |
| No match | 15 |

## Interpretation

The guard deliberately sacrifices automatic coverage to avoid the highest-risk sibling collisions:

```text
wrong auto matches: 138 -> 29
```

This is not final matching improvement; it is a safety gate. Next work should reduce the review burden by improving crop/localization and reranking on the mined hard-negative pairs.

## Verification

Focused detector DB tests:

```text
10 passed, 66 deselected
```
