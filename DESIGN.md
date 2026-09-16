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

## 1. Estimand and research questions

**Estimand**, stated precisely because the project is easy to over-claim:

> The conditional probability that a drive **already in Backblaze's observed
> fleet** experiences a Backblaze-recorded failure within the next 30 days, given
> its accumulated power-on hours, drive model, and SMART telemetry up to the
> prediction time; and the use of those probabilities to evaluate cost-based
> replacement policies on temporally held-out quarters.

Three things that is not. It is not hard drive lifespan from manufacture: the
data begins when a drive enters Backblaze's fleet, and earlier history is
unobserved. It is not a physical definition of failure: failure is Backblaze's
operational determination. And it is not a statement about drives in general: it
describes one operator's datacenters, workload and procurement.


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

**Censoring, and why there is no competing risks model.** A spell ending on the
last day of the window is right-censored. A spell ending earlier without a
failure flag has left observable follow-up for a reason the public data does not
record.

The event structure is therefore:

- **Event**: a Backblaze-recorded drive failure
- **Censoring**: the drive leaves observable follow-up without a recorded failure

and **not** failure against a known non-failure removal. Fine-Gray was in an
earlier version of this document as a sensitivity analysis and has been
**removed**, because a subdistribution hazard model requires an observed
cause-of-exit label and no such label exists in this dataset. Our "removed"
category is inferred from the timing of disappearance, not observed. Fitting
Fine-Gray to an inferred label would imply information the data does not contain.

What can be done instead, and was: test whether the censoring is informative.
`scripts/s2_censoring_check.py` shows that conditional on drive model and age,
health does not predict removal, and that removals are concentrated by model in a
pattern indicating wholesale retirement rather than selection on individual
drives. That conditioning set is exactly the one the models already adjust for,
so censoring is conditionally independent given the covariates in use. The
residual ambiguity is stated as limitation 2 rather than modelled away.

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

Every model from B1 onward is fit and scored in the **landmark frame** defined in
section 5: one row per spell per landmark, predicting P(failure within 30 days |
alive at the landmark). The spell panel is retained only for B0, which asks a
lifetime question rather than a horizon-specific one.

- **B0** Kaplan-Meier with delayed entry on the spell panel, no covariates, converted to a 30-day conditional risk given current age. Also refit on landmark rows as age alone, so it is directly comparable with the rest.
- **B1** Nonparametric hazard by drive model and half-year age band, estimated as events over exposure with Gamma-Poisson shrinkage toward the model-level and global rates. No SMART attributes.
- **B2** Cox on landmark rows with the Cohort A universal SMART set, stratified by drive model
- **B3** As B2 plus the Seagate-only attributes, Cohort B only
- **M2** Boosted trees on the same target as B2: XGBoost with a Poisson objective, the B1 hazard as a per-row offset, and log exposure carried through `base_margin`. Identical target, identical offset, identical rows as B2; only the functional form changes, from log-linear to boosted trees. That is what makes it a clean test of whether nonlinearity and interactions among SMART attributes buy anything.
- **M3** DeepHit, phase 2 only (section 11)

**M1, Random Survival Forest, was in this ladder and has been removed.** Two
reasons, neither of them that it would have performed poorly. It cannot be fitted
on the same rows: a survival forest will not take 7.5 million rows and would
require case-control subsampling, breaking the identical-rows comparability that
every other model here follows. And it is not a different model family from M2 in
any meaningful sense, since both are tree ensembles, so it answers the same
question as M2 with more caveats and less comparability rather than providing
independent corroboration.

B1 is the baseline that matters. Beating B0 is trivial; beating a model-and-age
model is the real test of whether SMART telemetry carries information. B1 is
therefore estimated nonparametrically rather than as a Cox model: with two
predictors and millions of rows a Cox fit buys nothing over the direct estimate
while imposing a proportional hazards assumption that B0 already suggests will
fail. A weak B1 would let E2 pass trivially and prove nothing.

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
- **E2** B2 beats B1. Declared **before B2 exists**, on raw predictions, with every quantity from a spell-level bootstrap of the *paired* difference between the two models scored on identical rows:
  - **Primary**: IPCW AUC improves, interval on the paired difference excluding zero.
  - **Secondary**: IPCW Brier improves, interval on the paired difference excluding zero. No fixed percentage.
  - **Constraint**: calibration does not degrade, judged by the decile ratio table.

  No fixed percentage is set because a percentage cannot be calibrated in advance at this event rate. B1's measured result makes the point: drive model raises AUC by 6.7 points with non-overlapping intervals, which is a substantial gain, while moving Brier 0.06% relative. With events near 1 per 1000 landmark rows, the Brier score is dominated by correct near-zero predictions and compresses large effects into tiny numbers. A threshold that a genuinely predictive model could fail on scale alone would not distinguish the hypothesis from its negation. Direction plus a paired interval excluding zero does. If E2 fails, the project's premise fails and the README says so in the first paragraph.
- **E3** On Cohort B, B3 improves on B2. Declared **before B3 exists**, judged the same way as E2 and for the same reasons:
  - **Primary**: IPCW AUC improves, paired bootstrap interval on the difference excluding zero.
  - **Secondary**: IPCW Brier, reported both raw and at oracle level, with no threshold.
  - **Directional**: among the five attributes actually under test, 187 (reported uncorrectable errors) carries most of any gain, while 188, 190, 241 and 242 contribute little. This is checked by refitting with 187 alone added and comparing against the full set.

  Both arms are fitted on the same Seagate cohort, so the comparison isolates the attributes rather than the population. Note that attribute 197 is in the universal set and already present in B2, so it cannot contribute anything incremental: the attributes under test are 187, 188, 190, 241 and 242 only. An earlier version of E3 named 197 among the incremental attributes and set a 3 to 8% relative Brier threshold; both were errors, corrected before B3 was fitted (amendment log).
- **E4** M2 beats B2 on discrimination by a small margin and is no better calibrated. Declared **before M2 exists**, judged as E2 and E3 are: IPCW AUC improves with a spell-level paired bootstrap interval on the difference excluding zero, and IPCW Brier reported raw and at oracle level with no threshold. "Small" is a directional expectation, not a criterion: B2's log-linear form already captures most of the signal in monotone SMART counters, so the margin from nonlinearity should be well under the +0.187 that SMART itself bought over B1. A large gain would mean interactions matter far more than expected and is worth reporting as a surprise. No recalibration step is applied to any model (section 10), so calibration is reported as the models produce it.
- **E5** The delayed-entry estimate and an estimate fit only on the incident cohort agree within confidence intervals, **compared like for like**, meaning restricted to the same drive models and the same installation vintage. Disagreement under that comparison would indicate the truncation handling is wrong. A pooled comparison across all vintages does not test truncation, because at any given age the full cohort and the incident cohort contain different manufacturing vintages by construction (see section 12, limitation 10).
- **E6** Proportional hazards is rejected by Schoenfeld residuals for at least the age term. Stratification by model absorbs part of this; a time-varying coefficient or an accelerated failure time specification is the documented fallback.

---

## 10. Decision layer

The true cost of an unplanned failure relative to a planned replacement is not
public, so it is not invented. It is parameterised as a ratio k and swept across
a plausible range.

**Two quantities are swept, not estimated.** The cost ratio k is unknown because
it is not public. The fleet's overall hazard level is unknown for a different
reason: it is genuinely volatile. Measured landmark event rates per 1000 rows run
1.077, 1.542, 1.554, 1.204, 1.186, 1.037, 1.287, 0.807, 0.901 across the nine
quarters, a coefficient of variation near 21% with no trend. Models trained on
past quarters therefore carry a level that is right on average and wrong in any
particular quarter, by roughly 20% in either direction.

A recalibration step fitted on the preceding quarter was tried and **removed**,
because the quantity being corrected is the same size as the quarter-to-quarter
noise the correction is estimated from. It made calibration worse rather than
better (amendment log, 2026-09-15).

The honest treatment is the same as for k: report how the optimal replacement
threshold moves when the realised failure rate comes in 20% above or below what
the model expected. An operator does not know next quarter's failure rate either,
so a threshold that holds across that range is worth more than one tuned to a
level nobody can predict.

For each k and each replacement threshold p, expected cost per drive-year is
computed on held-out data. The deliverable is a family of curves showing where
the optimal threshold sits and how strongly it depends on an assumption outside
the analyst's control. A single fabricated cost ratio would be less honest and
less useful.

**The decision rule.** With `C_R` the cost of a planned replacement, `C_F` the
cost of an unplanned failure, and `p_i(t)` the predicted probability of failure
within the horizon, the myopic comparison is

    cost of keeping  = p_i(t) * C_F
    cost of replacing = C_R

so replace when `p_i(t) > C_R / C_F = 1/k`. The threshold is therefore a direct
function of the cost ratio, which is why sweeping k and sweeping the threshold are
the same exercise.

That rule ignores the remaining useful life discarded by replacing early, so the
reported policy is the thresholded form

    pi_tau:  replace drive i at time t if p_i(t) > tau

evaluated over the held-out quarters by searching tau, rather than taken from the
closed form above.

**Policies compared**, so the risk-based policy is measured against real
alternatives rather than against nothing:

1. no predictive replacement, drives run to failure
2. age-based replacement at a fixed power-on-hours threshold, itself swept
3. risk-based replacement under `pi_tau`

**Objective**, with the denominator defined rather than implied:

    fleet cost rate = (replacement costs + failure costs) / total drive operating years

**Reported quantities**: replacements triggered, observed failures, **projected**
failures prevented, unnecessary replacements, fleet cost rate, and sensitivity to
both `k` and the fleet hazard level.

**A causal caution that governs how every result here is worded.** No
intervention took place. If the model would have replaced a drive on day 3 and
that drive was observed to fail on day 20, the correct statement is that the
policy *would have removed the drive from service before its observed failure*
under the simulator's assumptions. It is **not** that a failure was avoided: the
counterfactual world in which the drive was replaced was never observed, and the
replacement itself carries its own failure risk. Every such figure is labelled
*projected* or *simulated*, and kept separate from measured held-out predictive
performance.

---

## 11. Scope

**Phase 1**, the shipped scope: ingest pipeline, survival tables with spell
splitting, B0 through B3, M2, the full evaluation suite, the
informative-censoring test, the incident-cohort and vintage-matched validation of
delayed entry, the decision layer and fleet simulation, and **the README**. The
README is a deliverable, not documentation of one: it carries the estimand, the
pre-committed verdicts including the failures, the design decisions and their
reasoning, and the limitations. A repository with results and no README is not a
finished project.

**Phase 2**, explicitly deferred and not a condition of completion: DeepHit for
competing risks, shared frailty by manufacturing batch, and landmark-supermodel
alternatives.

**Out of scope**: serving infrastructure, orchestration, monitoring, retraining
pipelines. This is a data science project. Production engineering belongs
elsewhere.

---

## 12. Stated limitations

1. Failure time is interval-censored. Backblaze records one snapshot per day, so a drive observed healthy on its last recorded day and gone the next failed somewhere inside that interval. Every failure row is retained regardless of the weekly sampling, so the interval is under 24 hours against lifetimes of tens of thousands of hours, and the analysis takes the recorded final day as the event time. Real, and immaterial at this scale.
2. Weekly sampling means power-on hours at spell entry are known to within roughly 168 hours. Immaterial against lifetimes in the tens of thousands of hours, but stated.
3. Backblaze does not publish why a drive left the fleet, so failure and non-failure removal cannot be separated from the source data. This was tested rather than assumed (`scripts/s2_censoring_check.py`). Removed drives carry roughly twice the prevalence of non-zero reallocated, pending and offline uncorrectable sectors as surviving drives, but matched on drive model and age that excess falls to 0.6, 0.3 and 0.3 percentage points respectively. Removals are also heavily concentrated by model, with a single model accounting for 50 to 99 percent of removals in most quarters. Both findings indicate wholesale retirement of ageing models rather than selection on individual drive health, which makes censoring conditionally independent given the model stratum and the power-on-hours time scale that the primary analysis already conditions on. A residual tail of under one percent of removals, concentrated in the HGST 12 TB models, does show genuine health selection and is reported separately. This is why no competing risks model is fitted (section 3): the finding is that censoring is conditionally independent given the covariates already in use, and the dataset carries no observed cause-of-exit label that a subdistribution hazard model would require.
4. Failure is Backblaze's operational definition, not a physical one.
5. Raw SMART values are not comparable across manufacturers. Models are stratified accordingly, and no cross-vendor comparison of raw magnitudes is made.
6. The window opens on 2024-01-01, so 84.9% of the cohort is left-truncated and the fleet's earlier history is unobserved.
7. Fold 1 trains on six quarters against fold 3's nine, so early-fold results rest on less data. Per-fold reporting makes this visible rather than hiding it in a pooled average.
8. Results describe one operator's datacenters, workload and procurement decisions. They do not describe hard drives in general.
9. 726 of 9,790 events (7.4%) fall into no landmark window and are invisible to every model. Composition, measured in `scripts/s1b_coverage_audit.py`: 378 failed before the first landmark, which is burn-in from the 30 day change feature and costs training data only; 143 had a spell of roughly one day, giving a landmark model no history to predict from; 198 were excluded by the staleness rule because their most recent telemetry predated the landmark by more than 14 days; 7 entered after the last landmark. These exclusions are common to every model, so the comparison between models is unaffected, but reported performance is conditional on a drive being scorable at all. In particular **the model does not address infant mortality**: drives failing within days of installation are structurally outside a landmark framework, and no claim is made about them. The uncovered share rises from 0.7% in 2025 Q1 to 12.3% in 2026 Q1 as the fleet grows and newly installed drives make up more of the population, which reduces fold 3's effective event count from 998 to 875.
10. E5 as originally specified compared pooled full-cohort survival against the incident cohort at fixed ages, and failed at age 2: 0.9831 against a band of [0.9842, 0.9865], a gap of 0.23 percentage points. That specification was not like for like. At age 2 the full cohort is 37% 2022 install vintage, 48% 2023 and 15% 2024, while the incident cohort is almost entirely 2024, since nothing installed later can reach age 2 within a 27 month window. Holding drive model fixed changed the gap by 0.000 pp, ruling out model composition. Holding installation vintage fixed resolves it: the 2024 vintage under delayed entry gives 0.98583 against the incident arm's 0.98622, a difference of 0.04 pp. Survival at age 2 across the 2022, 2023 and 2024 vintages spans 0.240 pp, which by itself exceeds the original gap. Delayed entry is therefore validated on the comparison that tests it. One residual anomaly is left unexplained rather than rationalised: pooled full-cohort survival at age 2 (0.98393) falls below all three individual vintage estimates, where a risk-set-weighted pooling should place it inside their range. The likely mechanism is that 2025 and 2026 installations contribute hazard at young ages without ever reaching age 2, but this was not verified. All quantities here are under a quarter of a percentage point, against a project whose predictions are 30 day risks at landmarks.

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

**2026-09-14, section 9 expectation E5 restated, section 12 limitation 9 added.**
No model had been fit beyond B0, which is nonparametric and unaffected. E5
originally called for a pooled comparison between the delayed entry estimate and
the incident cohort. That comparison is confounded by installation vintage: at
any fixed age the two arms necessarily contain different vintages, because
drives installed late in the window cannot have reached older ages. E5 now
specifies a model matched and vintage matched comparison, which is the
comparison that actually tests truncation handling. Both the original pooled
result and the corrected result are reported, and the residual pooling anomaly
is recorded as unexplained.

**2026-09-14, section 7 rewritten for a single modelling frame, section 9
expectation E2 given a numeric threshold.** B0 had been fit; B1 had not, and no
model touching covariates existed. Two changes.

First, section 7 originally described B2 as counting-process Cox on the spell
panel while section 5 defined a landmark prediction task. Those are different
frames on different tables and do not compose. All models from B1 onward now live
in the landmark frame, because that is the frame producing the calibrated
horizon-specific probability the decision layer in section 10 consumes, and
because comparing six models is only meaningful on identical rows with identical
metrics. B1 also changes from a Cox model to a nonparametric estimate, which
strengthens the baseline rather than weakening it.

Second, E2 originally said B2 must beat B1 "substantially" without defining the
word. That is not a pre-commitment, since any outcome could be argued into or out
of it afterwards. E2 now carries a numeric threshold on two metrics, fixed before
B2 was fit.

**2026-09-14, expectation E2 restated again, section 10 gains a required
recalibration step.** B1 had been fit; B2 had not. Two changes, both prompted by
B1's measured behaviour.

E2's 2% relative Brier threshold, set earlier the same day, was calibrated from a
synthetic check and was wrong by more than an order of magnitude. On real data B1
improves AUC over B0 by 6.7 points with non-overlapping intervals while improving
Brier by 0.06%. A 2% bar could therefore have failed a strongly predictive model
for reasons unrelated to the hypothesis. E2 now uses direction plus a paired
bootstrap interval excluding zero on both metrics, which is scale free. The
comparison also moves from marginal intervals to the paired difference, which is
the correct and considerably more powerful test when two models are scored on
identical rows.

Section 10 now requires recalibration because B1 over-predicts by about 20%
pooled, driven by a declining fleet failure rate across the window rather than by
any modelling error. This is exactly the failure mode temporal validation exists
to expose, and a random split would have hidden it entirely.

**2026-09-15, section 10 recalibration step removed, one day after it was added.**
B1 had been fit; B2 had not. The step was added on the reasoning that the fleet's
failure rate had declined across the window, so models trained on older data
would over-predict. That reasoning was wrong. The quarterly landmark event rates
oscillate without a trend, and the inference of a decline came from looking at
three folds rather than the full series.

Fitting the factor on the quarter preceding each test quarter therefore corrects
using a level that does not predict the next one. In fold 1 the validation
quarter ran at 1.037 events per 1000 and the test quarter at 1.287, so the fitted
factor of 0.803 pushed predictions down when they needed to rise. Measured
effect: decile calibration ratios moved from a range of 0.74 to 1.05 without the
correction to 0.80 to 1.51 with it. The correction is the same magnitude as the
noise it is estimated from, so it cannot be fitted honestly on one quarter.

Level uncertainty is now handled where it belongs, as a swept parameter in the
decision layer alongside the cost ratio, rather than as a correction pretending
to knowledge nobody has.

**2026-09-15, expectation E3 restated before B3 was fitted.** B2 had been fitted;
B3 had not. Two errors in the original wording.

It set a 3 to 8% relative IPCW Brier threshold, repeating the scale mistake that
E2 was already amended for earlier the same day. Measured relative Brier gains on
this data are 0.06% for B1 over B0 and not distinguishable from zero for B2 over
B1, so a 3 to 8% bar could not have been met whatever the vendor attributes do.

It also named attribute 197 among the attributes whose incremental value was
being tested. 197 is in the universal feature set and is already in B2, so its
contribution is not incremental by construction. The attributes actually under
test are 187, 188, 190, 241 and 242.

The directional part of E3, that 187 dominates and the rest contribute little,
was scale free and testable and is retained unchanged.

**2026-09-16, Fine-Gray removed, decision layer reformulated, estimand and README
added to scope.** B0 through B3 fitted; M1, M2 and the decision layer not yet
built. Prompted by an external critique of the project plan, several points of
which were correct.

Fine-Gray is removed from all scope rather than demoted. A subdistribution hazard
model requires an observed cause-of-exit label, and this dataset has none: the
"removed" category is inferred from the timing of disappearance. Fitting it would
imply information the public data does not contain. The informative-censoring
test in `s2_censoring_check.py` is what the data actually supports, and it has
already been run.

Section 10 previously said the policy result would report how many real failures
"would have been pre-empted". That is a causal claim about an unobserved
counterfactual and is not supportable: no intervention occurred, and a replacement
drive carries its own failure risk. All such quantities are now labelled projected
or simulated. The section also now states the cost equation explicitly, defines
the cost rate denominator, and compares the risk-based policy against
run-to-failure and age-based alternatives rather than against nothing.

An explicit estimand was added as section 1, and the README was added to Phase 1
scope, having been absent from it.

**2026-09-16, M1 removed from the ladder, M2 respecified, E4 restated.** B0
through B3 fitted; no ML model fitted.

M1 (Random Survival Forest) is dropped. Not because it would have performed
poorly, but because it cannot be fitted on the same rows as every other model:
a survival forest will not take 7.5 million rows and would need case-control
subsampling, which breaks the identical-rows comparability the ladder depends on.
It is also not an independent model family, since M2 is likewise a tree ensemble,
so it would answer the same question with more caveats rather than corroborating
it.

M2 changes from "gradient-boosted Cox" to XGBoost with a Poisson objective and
the B1 hazard as a per-row offset. The reason is nesting: this form has exactly
the same target, offset and exposure handling as B2, so the only difference
between them is log-linear against boosted trees. A gradient-boosted Cox model
would have changed the estimator and the target at once, making any difference
uninterpretable.

E4 previously referenced M1 and carried no decision criterion. It now applies the
same paired-bootstrap test as E2 and E3, fixed before M2 was fitted.
