# Standard Tests

Standard Tests (multiple-choice tests) are the default workflow in EZ Mark.

## Scoring

- Each question carries a **points** value — the single canonical scoring weight.
  Fractional points are allowed (e.g. `2.5`).
- The total is the exact sum of earned points. Fractional totals are stored and
  exported as-is (`7.5`, never rounded up to `8`). The UI shows `7` for whole
  values and `7.5` for fractional ones.
- Sections/categories are optional groupings of questions. Per section the system
  reports earned points, possible points, and a percentage — no artificial scaled
  maximum score.

## Pass marks

Pass marks are explicit and optional:

- `pass_mark_value` — the threshold value.
- `pass_mark_type` — `"points"` (raw earned points) or `"percentage"` (0–100).

- No pass mark set → results get a neutral **Scored** status (no pass/fail).
- Points threshold → passes when earned points ≥ the threshold.
- Percentage threshold → passes when the correctness rate ≥ the threshold.

Pass marks are set on the template (inherited by its runs) and can be changed per
run from the results page; changing one recalculates results.

## Defaults

New questions default to continuous numbering, 4 choices, and 1 point. Question
count, choice count per question (2–5), correct answers, points, grouped numbering,
sections, and the printable answer-key sheets are all configurable in the builder.
