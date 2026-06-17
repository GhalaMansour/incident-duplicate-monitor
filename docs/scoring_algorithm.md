# Duplicate Scoring Algorithm

> **Status:** Authoritative reference for the matching layer.
>
> **Originality:** The scorer described here was designed in-house for
> the Kidana ticket vocabulary. The Arabic normalization rules, the
> template-vs-numbers split, and the weights below are tuned against
> the live Mina / Arafat / Muzdalifah ticket corpus and are original
> work by the project owner.

## Scope

The matching layer answers a single question: *given two service
requests, are they reporting the same incident?* The answer is an
integer score and a list of human-readable reasons. A pair exceeding
`LM_MIN_SCORE` (default 7) is retained; a pair exceeding
`LM_ALERT_SCORE` (default 8) raises an alert.

The implementation is in:

- `src/duplicate_monitor/matching/normalize.py` — Arabic text
  normalization and HTML stripping.
- `src/duplicate_monitor/matching/scorer.py` — `smart_text_compare`,
  `score_pair`, `parse_date`, `format_arabic_gap`.
- `src/duplicate_monitor/matching/legacy.py` — the bulk detector that
  applies blocking, scoring, and Union-Find grouping over a whole
  DataFrame.

This document covers what the matching layer does and why; the code
documents how.

## Inputs

A normalized service-request record carries these fields:

| Field | Source | Used by |
|-------|--------|---------|
| `sr` | OSLC `ticketid` | Identity |
| `loc` | OSLC `location` — Maximo's structured location **code** (e.g. `MN03`), *not* the GPS coordinates | Blocking key, +4 points |
| `fault` | Last two comma-separated segments of `description` (taxonomy L3+L4) | Blocking key, +3 points |
| `asset` | OSLC `assetnum` | +4 points when matched |
| `detail` | `description_longdescription` / `longdescription` | Smart text compare |
| `requestor_no` | OSLC `zzrequestorno` | +2 points when matched |
| `reported_dt` | Parsed `reportdate` | Time-gap weighting |

## Pipeline

```
raw OSLC record
    |
    v
normalize_arabic(strip_html(detail))
    |
    v
blocking on (fault, location)
    |
    v
for each pair within a block:
    score_pair(record_a, record_b)
    |
    v
Union-Find grouping by SR id
    |
    v
duplicate groups
```

## Step 1: Arabic normalization

`normalize_arabic` collapses the cosmetic differences that should not
count toward duplication:

- Non-breaking space (`\xa0`) becomes a regular space.
- Alef variants (`إأآا`) collapse to plain `ا`.
- `ى` becomes `ي`.
- `ة` becomes `ه`.
- Latin letters are lowercased; whitespace is collapsed.

These rules were chosen because they match the variation seen in real
Kidana data — different keyboards, different copy-paste sources, and
different operator habits produce these specific differences.

`strip_html` removes the rich-text markup that arrives in the OSLC
`description_longdescription` field. Maximo stores the Details field
as HTML; the matcher only needs the plain text.

## Step 2: Blocking

The bulk detector blocks pairs by `(fault, location)`. A pair must
share both keys to be scored. The blocking is the dominant
performance optimization — without it, scoring is O(N^2) over the
open SR set; with it, it is bounded by the size of the largest
block.

Two design choices in the blocking:

- The fault key is the **last two comma-separated segments** of
  the taxonomy string (L3 + L4 in the four-level taxonomy). The
  last single segment was tried first but was too generic — many
  unrelated incidents share the same leaf fault name. Two
  segments give enough specificity to distinguish them while
  still matching the two-segment entries that some operators
  type, where L3+L4 are the only segments present.

### "Same location" means the Maximo location code, not GPS

This is the most common source of confusion: the algorithm's
location gate compares Maximo's `location` field — a structured
code from a controlled vocabulary, e.g. `MN03` for a specific
gate in Mina — and not the GPS latitude / longitude.

**Why not GPS.** During an early review of the live ticket
corpus we observed clear duplicate pairs — same caller, same
fault, same boilerplate description filed minutes apart — whose
GPS coordinates differed by tens or even hundreds of metres.
The coordinates carry real-world noise:

  * Some operators tap a map pin from inside an air-conditioned
    operations room; others read the GPS off a phone outside in
    the sun.
  * The Maximo field collects whatever the reporting app sends.
    The two phones in the example above sit at different desks
    and report slightly different lat/lon for the same incident.
  * A reporting app sometimes falls back to a default office
    coordinate when GPS is weak.

A GPS-based gate would have rejected those genuine duplicates.
The structured `location` code, by contrast, is picked from a
list — the operators who filed the two SRs were both pointing at
`MN03`, and the algorithm trusts that agreement.

**How the dashboard still uses GPS.** The coordinates are not
thrown away. Every duplicate card on the dashboard renders the
straight-line distance between the two SRs ("this SR is 23 m
from the matched SR") as an advisory readout. The reviewer can
glance at that number to decide:

  * Distance is small (a few metres) → both reports really are
    about the same incident at the same physical point, even if
    one of them has a slightly drifted GPS reading.
  * Distance is large (hundreds of metres) → the reviewer can
    flag the pair for a closer look even though they share the
    same `location` code.

So the algorithm makes the *match decision* using the controlled
code, and the dashboard hands the reviewer the GPS distance to
make a final human judgement on borderline cases. GPS is treated
as evidence presented to a human, not as a gate the algorithm
trusts on its own.
- An empty `loc` excludes the SR from blocking entirely. Without a
  location, the scorer would over-match — every ticket in the same
  fault category would be a candidate.

## Step 3: Smart text comparison

`smart_text_compare` returns `(classification, points, final_pct)`
based on **three** independent measurements computed on the two
descriptions:

| Measurement | What it captures |
|---|---|
| `template_pct` | Sentence structure — the description with numbers replaced by `#`, sequence-matched. Catches "same boilerplate" even when numbers differ. |
| `token_pct` | Word content — bag-of-tokens with fuzzy containment. Catches "same words" even when ordering differs. |
| `numbers_overlap` | Jaccard similarity of the raw numbers in the two descriptions. |

The pipeline then computes `final_pct = max(template_pct, token_pct)` —
the overall similarity, taken as whichever of the two measurements is
stronger.

### Classification (decision order)

The implementation evaluates these checks **in order**. The first one
that matches decides the class.

1. **`identical` → +5 points.** `final_pct ≥ 90%` **and**
   `numbers_overlap ≥ 50%`. The two SRs describe the same incident
   with the same asset/grid references.
2. **`template_only` → pair dropped.** `template_pct ≥ 90%` **and**
   `numbers_overlap < 30%`. The sentence structure matches but the
   asset / grid / signpost numbers differ — same phrasing template
   applied to a different incident (for example, "تسرب في شبكة المياه
   عند المربع 5" vs "تسرب في شبكة المياه عند المربع 47"). Score 0,
   metadata.gate set to ``text``, pair is not reported as a duplicate.
3. **`similar` → +3 points.** `final_pct ≥ 90%` and the pair did not
   match either rule above. Same wording or same content with
   ambiguous numeric overlap.
4. **`different` → 0 points, pair dropped.** Anything that does not
   reach `final_pct ≥ 90%`.

The `template_only` class is the most operationally important guard:
without it, boilerplate descriptions would drive false positives
during peak hours by passing the same-wording check on pairs that are
in fact different incidents at different physical assets.

## Step 4: Per-pair scoring

`score_pair` aggregates four signal sources:

| Signal | Condition | Points |
|--------|----------|--------|
| Same location | Always (guaranteed by blocking) | +4 |
| Same fault | Always (guaranteed by blocking) | +3 |
| Same asset | Asset matches and differs from location string | +4 |
| Text compare | identical / similar | +5 / +3 |
| Same requestor | `requestor_no` matches | +2 |
| Time gap < 1 day | reported within 24 h | +3 |
| Time gap < 2 days | reported within 24-48 h | +2 |
| Time gap <= 2 days | reported within 48 h | +1 |

The maximum theoretical score for a pair is 21
(4 + 3 + 4 + 5 + 2 + 3). In practice, a real duplicate scores 10-14.

## Step 5: Grouping

The bulk detector applies Union-Find over the surviving pairs to
collapse transitive duplicates into groups. If A is a duplicate of B
and B is a duplicate of C, all three appear in the same group even if
the (A, C) pair was not directly scored.

## Tuning history

The default weights and thresholds in the table above are the result
of iteration against the live ticket corpus. The decisions that drove
the current values:

- The `(template, numbers)` split exists because boilerplate
  descriptions ("انقطاع كهرباء") would otherwise drive false
  positives during peak hours.
- The same-day +3 bonus is what most reliably identifies "the same
  crew reported it twice" — the most common duplicate pattern.
- The asset bonus (+4) deliberately requires `asset != loc` so we
  don't double-count when the location string and the asset number
  refer to the same physical thing.

## Future work

- **Configurable weights.** Make every value in the scoring table an
  environment variable so each site can tune.
- **Explainability panel.** Render the per-signal contribution in the
  dashboard so reviewers can audit a decision.
- **Embedding-based fallback.** For the residual false negatives,
  evaluate a multilingual embedding model as a secondary scorer that
  runs only on pairs that the rule-based scorer scored 6-7.
