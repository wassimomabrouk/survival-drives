# Fleet reliability and replacement policy: design document

Status: locked before any model is fit. Baselines, metrics, validation protocol
and directional expectations are all pre-committed below. Nothing in sections 5
to 9 may be changed after the first model is trained.

---

## 0. Feasibility (completed)

Section 0 ran over eight quarters of Backblaze Drive Stats, 2024 Q1 through 2026
Q1, ingested to Parquet with weekly sampling and all failure rows retained.

| quantity | value |
|---|---|
| drives | 384,213 |
| drive days observed | 36,142,935 |
| failure events | 9,716 (data drives only) |
| models with 100+ drives | 50 |
| models with 130+ events | 20 |
| models with 200+ events | 14 |

Validation: the ingest reproduces Backblaze's published Q1 2026 figure of 1,030
failures exactly, and the exposure-based annualized failure rate reproduces their
published 1.24% for that quarter.

**Q1, does the table build cleanly.** Yes. No schema break across the eight
quarters and no duplicate drive days. Power-on hours are missing on 1.2% of
drives and capacity is malformed on under 0.06% of rows.

The one real problem: `(serial_number, model)` is not a stable key over a
two-year window. 11,590 drives (3.0%) show non-monotonic power-on hours, 164
continue reporting after being flagged failed, and 24 carry more than one failure
flag. These are serial reuse and drive re-insertion, and they are handled by
spell splitting (section 3).

**Q2, are there enough events.** Decisively yes. 9,716 events is far above what
this design requires, and 14 models individually exceed 200 events, which
supports per-model stratification without falling back to manufacturer level.

**Q3, how much left truncation is present.**

| entry year | drives | fraction used on entry | median hours at entry |
|---|---|---|---|
| 2024 | 322,206 | 84.9% | 22,098 |
| 2025 | 43,791 | 6.5% | 211 |
| 2026 | 9,387 | 0.6% | 251 |

Left truncation affects 84.9% of the cohort, but the cause is the observation
window, not fleet procurement. Drives arriving after the window opened are
essentially new, a median of roughly nine days of prior use. Backblaze is not
installing second-hand drives.

This matters for framing. Delayed entry is mandatory because most drives were
already in service on 2024-01-01, not because used drives keep arriving. It also
yields a free validation: the 53,178 drives entering in 2025 and 2026 form an
incident cohort with negligible truncation, against which the delayed-entry model
can be checked (section 9, expectation E5).

**Q4, which SMART attributes are usable.** Coverage is binary at model level,
confirming it is a firmware property rather than a data quality issue.

| attribute | Seagate | Toshiba | WDC | HGST |
|---|---|---|---|---|
| 5, 9, 12, 193, 194, 198 | 1.00 | 1.00 | 1.00 | 1.00 |
| 197 | 1.00 | 0.94 | 1.00 | 1.00 |
| 187 reported uncorrectable | 1.00 | 0.00 | 0.00 | 0.00 |
| 188 command timeout | 1.00 | 0.00 | 0.002 | 0.00 |
| 190 airflow temperature difference | 1.00 | 0.00 | 0.00 | 0.00 |
| 241, 242 LBAs written and read | 1.00 | 0.16 | 0.05 | 0.09 |

Attributes 187, 188, 190, 241 and 242 exist only on Seagate. The Toshiba figure
of 0.94 on attribute 197 is entirely attributable to one model,
`TOSHIBA MG08ACA16TEY`, which reports zero coverage on 197 while every other
model in the fleet reports full coverage.

This constraint is not a defect. It supplies the project's second research
question (section 1).

---

## 1. Research questions

**RQ1, dynamic risk.** Given a drive's SMART history up to today, what is its
probability of failing within the next 30 days, and is that probability
calibrated well enough to act on?

**RQ2, the vendor gap.** Attributes 187, 188, 190, 241 and 242 are among the most
predictive in the published literature and exist only on Seagate firmware. Fitted
on the same Seagate cohort, how much predictive value do they add beyond the
attributes every manufacturer reports? A null result here is a genuine finding:
it would mean the fleet-wide model sacrifices nothing.

**RQ3, the decision.** At what predicted risk threshold does proactive
replacement minimise expected cost per drive-year, and how sensitive is that
threshold to the cost ratio between an unplanned failure and a planned swap?

---

## 2. Data

Backblaze Drive Stats, 2024-01-01 to 2026-03-31. One row per drive per day,
sampled to one day in seven with all failure-flagged rows retained
unconditionally. Cited per Backblaze's terms of use.

Retained columns: date, serial number, model, capacity, failure flag, and raw
SMART attributes 5, 9, 12, 187, 188, 190, 193, 194, 197, 198, 241, 242.

---

## 3. Survival table construction

**Time scale.** Power-on hours (`smart_9_raw`), not calendar days in fleet. A
drive enters the risk set at the power-on hours it reported on its first observed
day. Using days in fleet would assume every drive was new on arrival, which is
false for 84.9% of this cohort, and would bias the early hazard downward through
immortal time bias.

**Spell splitting.** A serial number is not a drive. A `(serial_number, model)`
pair is split into separate spells at either of:

1. a decrease in power-on hours (counter reset or reused serial), 11,590 drives
2. any observation after a failure flag, 164 drives

Each resulting spell is treated as an independent unit with its own entry hours,
exit hours and outcome. The affected counts are reported in the README rather
than silently absorbed.

Observation gaps are deliberately **not** a splitting condition, despite
affecting 1,641 drives. Because the time scale is power-on hours rather than
calendar time, a drive that goes offline for three weeks accrues no exposure and
produces no gap in analysis time. Splitting there would manufacture a spurious
truncated entry for a drive that is physically continuous and whose power-on
hours remain monotonic. Serial reuse, the real concern, is already caught by
condition 1. What gaps do affect is scoring, which is handled at landmark time
(section 5).

**Event.** A spell is an event if it carries the failure flag on its final
observed day.

**Censoring and competing risks.** A spell ending on the last day of the window is
right-censored. A spell ending earlier without a failure flag is a removal of
unknown cause, because Backblaze does not publish why a drive left the fleet.
This is treated two ways, both reported:

- Primary: independent censoring
- Sensitivity: Fine-Gray subdistribution hazard with removal as a competing event

If the two disagree materially, the ambiguity is stated in the README as an
irreducible limitation of the source, not resolved by assertion.

**Exclusions**, each with its count reported:

- boot devices and SSDs, identified by model capacity under 1 TB: 4,735 drives
- drives with no usable power-on hours reading: 4,641 drives
- models with fewer than 100 drives, for the stratified arms

---

## 4. Cohorts and feature sets

**Cohort A, universal.** All four manufacturers. Features: SMART 5, 12, 193, 194,
197, 198, plus capacity, plus drive model as a stratum. `TOSHIBA MG08ACA16TEY` is
excluded because it does not report attribute 197, costing 5,276 drives and 408
events out of 9,716.

That exclusion is deliberate. Backblaze's canonical predictive set is attributes
5, 187, 188, 197 and 198, of which only 5, 197 and 198 exist fleet-wide. Dropping
197 to retain one model would leave the universal arm with two of the five, which
is the more expensive trade. A sensitivity fit including the model with 197
dropped is reported alongside, to confirm the excluded population is not
systematically different from the rest.

**Cohort B, Seagate.** Seagate only, 4,075 events. Features: the Cohort A set plus
SMART 187, 188, 190, 241, 242.

For each attribute the model sees the current value, the change over the
preceding 30 days, and a binary indicator for whether the value is non-zero,
since several of these attributes are zero-inflated in a way that makes the raw
magnitude less informative than the fact of being non-zero.

---

## 5. Prediction task

At landmark time L, for every spell still at risk at L, using only covariate
history up to and including L, estimate

    P(failure in (L, L + 30 days] | alive at L)

Landmarks are placed every 28 days. A spell contributes an observation at every
landmark it survives to, which is how the model would be used in practice: every
drive, every day, re-scored on its history to date.

The spacing is 28 days rather than monthly because calendar months run 28 to 31
days, so a monthly grid against a 30 day horizon leaves uncovered days in every
31 day month. Under the original monthly grid 463 of 9,790 events (4.7%) fell
into no landmark window at all and were silently invisible to every model. A
fixed 28 day step covers the timeline completely, at the cost of a two day
overlap between consecutive windows. The overlap is harmless: landmark
observations within a spell are already correlated, and the bootstrap resamples
at spell level rather than row level.

The grid is anchored to its **end**, at (last observed date minus horizon), not
to its start. Stepping forward from the first landmark leaves a remainder at the
far end of the window, which is exactly where the rolling origin test folds sit.
Anchoring backwards moves that remainder into the 2024 burn in, where it costs
training data only.

**Staleness rule.** A spell is scorable at landmark L only if its most recent
SMART observation falls within 14 days of L, which is two sampling periods. A
drive whose telemetry is older than that has covariates too stale to act on, and
is excluded from that landmark rather than scored on outdated values. It re-enters
at the next landmark where the rule is satisfied. This is where observation gaps
are handled.

---

## 6. Validation protocol

Temporal, never random. Fleet composition shifts materially across the window, so
a random split would leak future model mix into training.

Evaluation is **rolling origin** across the final three quarters. At each fold the
model is refit on everything before the test quarter, and hyperparameters are
chosen on an inner split consisting of the last quarter of that fold's training
window.

| fold | training window | test quarter |
|---|---|---|
| 1 | 2024-01-01 to 2025-06-30 | 2025 Q3 |
| 2 | 2024-01-01 to 2025-09-30 | 2025 Q4 |
| 3 | 2024-01-01 to 2025-12-31 | 2026 Q1 |

Roughly 3,200 events across the three test quarters. A single held-out quarter
would carry only about 1,030, which is too few to separate six competing models
on IPCW Brier with usable confidence intervals. Rolling origin also shows whether
performance is stable over time rather than at one arbitrary cut.

Headline metrics are reported per fold and pooled. The protocol is fixed here and
executed once. Any result that prompts a change to the model is reported as
such.

---

## 7. Baselines (pre-committed)

- **B0** Kaplan-Meier by model with delayed entry, no covariates, converted to a 30-day conditional risk given current age
- **B1** Cox on age and capacity, stratified by model, no SMART attributes
- **B2** Cox with the Cohort A universal SMART set, time-varying, counting-process format
- **B3** Cox with the full Seagate set, Cohort B only
- **M1** Random Survival Forest
- **M2** Gradient-boosted Cox
- **M3** DeepHit, phase 2 only (section 11)

B1 is the baseline that matters. Beating B0 is trivial; beating an age-and-model
model is the real test of whether SMART telemetry carries information.

---

## 8. Metrics (pre-committed)

- **Primary**: IPCW Brier score at the 30-day horizon, averaged across landmarks
- **Discrimination**: Uno's concordance and time-dependent AUC at 30 days
- **Calibration**: predicted against observed risk by decile, plus calibration slope and intercept

Harrell's C is not reported as a headline. It is biased under heavy censoring and
rewards ranking while saying nothing about whether the probabilities are usable
for a cost calculation, which is what section 10 requires.

Uncertainty comes from a bootstrap at the spell level, not the observation level,
since a spell contributes many correlated landmark observations.

---

## 9. Directional expectations (pre-committed)

Recorded now so that neither outcome can be rationalised afterwards.

- **E1** B1 beats B0 modestly. Age and model carry real information.
- **E2** B2 beats B1 substantially on IPCW Brier. If it does not, the project's premise fails and the README says so in the first paragraph.
- **E3** On Cohort B, B3 improves on B2 but by a smaller margin than the literature on attributes 187 and 197 would suggest, in the range of 3 to 8% relative IPCW Brier. Most of any gain comes from 187 and 197; 188, 190, 241 and 242 contribute little.
- **E4** M1 and M2 beat B2 on discrimination by a small margin and are worse calibrated before recalibration. This is the usual finding in risk prediction and is expected here.
- **E5** The delayed-entry model and a model fit only on the 53,178-drive incident cohort agree within bootstrap confidence intervals. Disagreement would indicate the truncation handling is wrong.
- **E6** Proportional hazards is rejected by Schoenfeld residuals for at least the age term. Stratification by model absorbs part of this; a time-varying coefficient or an accelerated failure time specification is the documented fallback.

---

## 10. Decision layer

The true cost of an unplanned failure relative to a planned replacement is not
public, so it is not invented. It is parameterised as a ratio k and swept across
a plausible range.

For each k and each replacement threshold p, expected cost per drive-year is
computed on held-out data. The deliverable is a family of curves showing where
the optimal threshold sits and how strongly it depends on an assumption outside
the analyst's control. A single fabricated cost ratio would be less honest and
less useful.

The closing result is a counterfactual over the test quarter: under threshold p,
this many real failures would have been pre-empted, at the cost of this many
unnecessary replacements, for a net expected saving of this much at cost ratio k.

---

## 11. Scope

**Phase 1**, the shipped scope: ingest pipeline, survival table with spell
splitting, B0 through B3, M1 and M2, the full evaluation suite, the Fine-Gray
sensitivity analysis, the incident-cohort validation, and the decision layer.

**Phase 2**, explicitly deferred and not a condition of completion: DeepHit for
competing risks, shared frailty by manufacturing batch, and landmark-supermodel
alternatives.

**Out of scope**: serving infrastructure, orchestration, monitoring, retraining
pipelines. This is a data science project. Production engineering belongs
elsewhere.

---

## 12. Stated limitations

1. Weekly sampling means power-on hours at spell entry are known to within roughly 168 hours. Immaterial against lifetimes in the tens of thousands of hours, but stated.
2. Backblaze does not publish why a drive left the fleet, so failure and non-failure removal cannot be separated from the source data. This was tested rather than assumed (`scripts/s2_censoring_check.py`). Removed drives carry roughly twice the prevalence of non-zero reallocated, pending and offline uncorrectable sectors as surviving drives, but matched on drive model and age that excess falls to 0.6, 0.3 and 0.3 percentage points respectively. Removals are also heavily concentrated by model, with a single model accounting for 50 to 99 percent of removals in most quarters. Both findings indicate wholesale retirement of ageing models rather than selection on individual drive health, which makes censoring conditionally independent given the model stratum and the power-on-hours time scale that the primary analysis already conditions on. A residual tail of under one percent of removals, concentrated in the HGST 12 TB models, does show genuine health selection and is reported separately. Fine-Gray therefore remains a sensitivity analysis, as pre-committed, rather than becoming the primary specification.
3. Failure is Backblaze's operational definition, not a physical one.
4. Raw SMART values are not comparable across manufacturers. Models are stratified accordingly, and no cross-vendor comparison of raw magnitudes is made.
5. The window opens on 2024-01-01, so 84.9% of the cohort is left-truncated and the fleet's earlier history is unobserved.
6. Fold 1 trains on six quarters against fold 3's nine, so early-fold results rest on less data. Per-fold reporting makes this visible rather than hiding it in a pooled average.
7. Results describe one operator's datacenters, workload and procurement decisions. They do not describe hard drives in general.
8. 726 of 9,790 events (7.4%) fall into no landmark window and are invisible to every model. Composition, measured in `scripts/s1b_coverage_audit.py`: 378 failed before the first landmark, which is burn-in from the 30 day change feature and costs training data only; 143 had a spell of roughly one day, giving a landmark model no history to predict from; 198 were excluded by the staleness rule because their most recent telemetry predated the landmark by more than 14 days; 7 entered after the last landmark. These exclusions are common to every model, so the comparison between models is unaffected, but reported performance is conditional on a drive being scorable at all. In particular **the model does not address infant mortality**: drives failing within days of installation are structurally outside a landmark framework, and no claim is made about them. The uncovered share rises from 0.7% in 2025 Q1 to 12.3% in 2026 Q1 as the fleet grows and newly installed drives make up more of the population, which reduces fold 3's effective event count from 998 to 875.

---

## 13. Amendment log

Pre-commitment is only meaningful if changes are recorded rather than made
silently. Every amendment to this document after its first commit is listed here
with its date, its reason, and whether any model had been fit at the time.

**2026-09-14, section 5, landmark spacing changed from monthly to every 28
days.** No model had been fit, so section 5 was still open under the terms in the
header. Reason: the monthly grid left 463 of 9,790 events (4.7%) inside no
landmark window, because calendar months run up to 31 days while the horizon is
30. This is a mechanical coverage defect, not a modelling choice, and the fix
does not alter any baseline, metric, expectation or protocol.

**2026-09-14, section 12, limitation 2 rewritten.** No model had been fit. The
original text asserted that informative censoring could not be assessed from the
data. It can be, and was: conditional on model and age, health does not predict
removal. The limitation now reports the test and its result instead of stating an
untested worry.

**2026-09-14, section 5, landmark grid anchored to its end rather than its
start.** No model had been fit. Reason: forward anchoring left the last landmark
at 2026-02-25, so failures after 2026-03-27 were visible to no landmark, and
14.9% of 2026 Q1 events were uncovered inside a test fold. End anchoring
eliminated that category entirely. The residual 12.3% in 2026 Q1 is a different
cause, fleet growth and infant mortality, and is documented as limitation 8
rather than engineered away.

**2026-09-14, section 12, limitation 8 added.** No model had been fit. An earlier
claim in conversation that 463 events were uncovered was arithmetically wrong: it
subtracted a sum over landmark rows from a count of spells, which double counts
failures seen by overlapping windows. `scripts/s1b_coverage_audit.py` measures it
correctly with a distinct count and attributes every uncovered event to a cause.
