# Fleet reliability and replacement policy: design document

Status: locked before any model was fit. Baselines, metrics, validation protocol
and directional expectations were all pre-committed below, and each was committed
to git before the model it concerns existed. Nothing in sections 5 to 9 could be
changed after the first model was trained.

**Section 14 records what happened against each pre-commitment, including the one
that failed. Section 13 logs every amendment made during the work, with its date,
its reason, and which models had been fitted at the time.** Changes were made;
they were made in the open and before the results they could have been fitted to.

---

## 0. Feasibility (completed)

Section 0 was first run over nine quarters of Backblaze Drive Stats, 2024 Q1
through 2026 Q1 (earlier versions of this document said eight, a miscount), and
rerun when the window was extended to twenty-one quarters, 2021 Q1 through 2026 Q1
(section 13a). The figures below are from the 21-quarter run. The nine-quarter
version of this section is in git history at commit 29a87d1. Data was ingested to
Parquet with weekly sampling and every failure row retained.

| quantity | value |
|---|---|
| drives / spells after splitting | 429,870 / 441,894 |
| sampled drive-day rows | 70,347,832, one day in seven plus every failure row |
| failure events, data drives | 19,245 before spell splitting, 18,738 after |
| models with 100+ drives | 54 |
| models with 130+ events | 24 |
| models with 200+ events | 20 |

The nine-quarter version labelled the row count "drive days observed". Each row
stands for a week of service, so that label understated the observed drive days by
roughly a factor of seven.

Validation: the ingest reproduces Backblaze's published Q1 2026 figure of 1,030
failures exactly.

**Q1, does the table build cleanly.** Yes. No schema break across the twenty-one
quarters and no duplicate drive days. Power-on hours are missing on 2.8% of drives.

The one real problem: `(serial_number, model)` is not a stable key over a long
window. 11,587 drives (2.7%) show non-monotonic power-on hours, 308 continue
reporting after being flagged failed, and 39 carry more than one failure flag.
These are serial reuse and drive re-insertion, and they are handled by spell
splitting (section 3).

**Q2, are there enough events.** Decisively yes. 18,738 events after spell
splitting, and 20 models individually exceed 200, which supports per-model
stratification without falling back to manufacturer level.

**Q3, how much left truncation is present.**

| entry year | drives | fraction used on entry | median hours at entry |
|---|---|---|---|
| 2021 | 230,116 | 76.5% | 7,389 |
| 2022 | 31,214 | 39.8% | 502 |
| 2023 | 52,354 | 40.3% | 512 |
| 2024 | 51,737 | 10.8% | 250 |
| 2025 | 43,730 | 6.4% | 211 |
| 2026 | 9,383 | 0.5% | 251 |

52.1% of drives had more than 30 days of use when first observed, mostly drives
already in the fleet when the window opened. Drives arriving from 2024 onward are
essentially new. The nine-quarter version concluded from that that Backblaze is not
installing second-hand drives; on the longer window about 40% of drives first seen
in 2022 and 2023 already had more than 30 days of use, and the cause (drives moved
into the reported fleet, or spells created by splitting) was not determined. The
conclusion holds for 2024 onward and is not made in general.

Delayed entry is mandatory because most drives were already in service when the
window opened. It also yields a validation: drives first observed with under 30 days
of use form an incident cohort with negligible truncation, against which the
delayed-entry estimate can be checked (section 9, expectation E5).

**Q4, which SMART attributes are usable.** Coverage is binary at model level,
confirming it is a firmware property rather than a data quality issue.

| attribute | Seagate | Toshiba | WDC | HGST |
|---|---|---|---|---|
| 5, 9, 12, 193, 194, 198 | 1.00 | 1.00 | 1.00 | 1.00 |
| 197 | 1.00 | 0.93 | 1.00 | 1.00 |
| 187 reported uncorrectable | 1.00 | 0.00 | 0.00 | 0.00 |
| 188 command timeout | 1.00 | 0.00 | 0.002 | 0.00 |
| 190 airflow temperature difference | 1.00 | 0.00 | 0.00 | 0.00 |
| 241, 242 LBAs written and read | 1.00 | 0.13 | 0.07 | 0.07 |

Attributes 187, 188, 190, 241 and 242 exist only on Seagate. The Toshiba shortfall
on attribute 197 is attributable to two models, `TOSHIBA MG08ACA16TEY` and the much
smaller `TOSHIBA MG07ACA14TEY`, which report zero coverage on 197 while every other
model in the fleet reports full coverage. The second was missed when this section
was first written and found on 2026-10-02 (section 13).

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

Backblaze Drive Stats, 2021-01-01 to 2026-03-31 (2024-01-01 to 2026-03-31 on the
first run). One row per drive per day,
sampled to one day in seven with all failure-flagged rows retained
unconditionally. Cited per Backblaze's terms of use.

Retained columns: date, serial number, model, capacity, failure flag, and raw
SMART attributes 5, 9, 12, 187, 188, 190, 193, 194, 197, 198, 241, 242.

---

## 3. Survival table construction

**Time scale.** Power-on hours (`smart_9_raw`), not calendar days in fleet. A
drive enters the risk set at the power-on hours it reported on its first observed
day. Using days in fleet would assume every drive was new on arrival, which is
false for 52.1% of drives (84.9% on the nine-quarter window): the failures of drives
that arrived old would be attributed to young ages, biasing the early hazard
upward. The opposite mistake, power-on hours with every drive counted at risk from
zero, credits drives with failure-free time before they were observed, which is
immortal time, and biases the early hazard downward. Delayed entry avoids both.
(Corrected 2026-10-02; the earlier wording attributed the first mistake to immortal
time bias, which describes the second.)

**Spell splitting.** A serial number is not a drive. A `(serial_number, model)`
pair is split into separate spells at either of:

1. a decrease in power-on hours (counter reset or reused serial), 11,587 drives
2. any observation after a failure flag, 308 drives

(Counts from the 21-quarter run; 11,590 and 164 on nine quarters.)

Each resulting spell is treated as an independent unit with its own entry hours,
exit hours and outcome. The affected counts are reported in the README rather
than silently absorbed.

Observation gaps are deliberately **not** a splitting condition, despite
affecting 6,467 drives (1,641 on nine quarters). Because the time scale is power-on hours rather than
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

- boot devices and SSDs, identified by model capacity under 1 TB: 5,657 drives
- drives with no usable power-on hours reading: 12,093 drives

(21-quarter counts; 4,735 and 4,641 on nine quarters.)
- models with fewer than 100 drives, for the stratified arms

---

## 4. Cohorts and feature sets

**Cohort A, universal.** All four manufacturers. Features: SMART 5, 12, 193, 194,
197, 198, plus capacity, plus drive model as a stratum. `TOSHIBA MG08ACA16TEY` is
excluded because it does not report attribute 197, costing 5,276 drives and 408
events out of 9,716 on the original window. `TOSHIBA MG07ACA14TEY` is excluded
for the same reason from 2026-10-02 (section 13); in the raw data, 2021 Q1 to
2026 Q1, it is 1,051 drives and 58 recorded failures.

That exclusion is deliberate. Backblaze's canonical predictive set is attributes
5, 187, 188, 197 and 198, of which only 5, 197 and 198 exist fleet-wide. Dropping
197 to retain one model would leave the universal arm with two of the five, which
is the more expensive trade. A sensitivity fit including the excluded models with
197 dropped was planned here and **was not run** (section 13, 2026-10-02).

**Cohort B, Seagate.** Seagate only, 10,294 events on the 21-quarter window (4,075
on nine). Features: the Cohort A set plus SMART 187, 188, 190, 241, 242.

For each counter the model sees log1p of the current value and log1p of its rise
over the preceding 30 days; temperature enters as a level. The original
specification also gave each attribute a binary non-zero indicator. It was removed
after B2's first fit showed a singular design: log1p(x) is zero exactly when x is,
so the indicator is very nearly collinear with it (section 13, 2026-10-02).

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
| 1 | 2021-01-01 to 2025-06-30 | 2025 Q3 |
| 2 | 2021-01-01 to 2025-09-30 | 2025 Q4 |
| 3 | 2021-01-01 to 2025-12-31 | 2026 Q1 |

(Training windows began on 2024-01-01 on the nine-quarter run.)

About 3,200 failures were recorded in the three test quarters (1,254, 945 and
1,030), of which 2,667 fall inside a landmark horizon on Cohort A rows. A single held-out quarter
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
- **B2** Cox on landmark rows with the Cohort A universal SMART set, stratified by drive model. **As implemented**: a piecewise-exponential (Poisson) hazard on landmark rows with log exposure as offset and the B1 hazard as a further offset, so drive model and age are held fixed through B1 rather than through stratification. The change was made before B2 was fitted and recorded at the time only under E6 in section 14; section 13 now logs it (2026-10-02).
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

Uno's concordance and the calibration slope and intercept were **not computed**.
The IPCW time-dependent AUC, the IPCW Brier score and the decile tables were, and
every verdict in section 14 rests on those. Recorded in section 13 rather than
dropped silently.

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
reason: it is volatile. On the nine-quarter window, landmark event rates per 1000
rows ran 1.077, 1.542, 1.554, 1.204, 1.186, 1.037, 1.287, 0.807, 0.901, a
coefficient of variation near 21% with no trend. On twenty-one quarters
(`reports/s1_landmark_by_quarter.csv`) the quarter-to-quarter change has a standard
deviation of about 21% on the log scale, and the series also drifts: about 0.85
through 2021, 1.1 to 1.7 through 2023 and 2024, 0.8 to 0.9 by the end of 2025.
Models trained on past quarters therefore carry a level that is wrong in any
particular quarter, by roughly 20% in either direction, and on the longer window
also biased toward the higher-rate years they were trained on.

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

Updated on 2026-10-02 to the 21-quarter window. Where a nine-quarter figure differs
materially it is given in brackets.

1. Failure time is interval-censored. Backblaze records one snapshot per day, so a drive observed healthy on its last recorded day and gone the next failed somewhere inside that interval. Every failure row is retained regardless of the weekly sampling, so the interval is under 24 hours against lifetimes of tens of thousands of hours, and the analysis takes the recorded final day as the event time. Real, and immaterial at this scale.
2. Weekly sampling means power-on hours at spell entry are known to within roughly 168 hours. Immaterial against lifetimes in the tens of thousands of hours, but stated.
3. Backblaze does not publish why a drive left the fleet, so failure and non-failure removal cannot be separated from the source data. This was tested rather than assumed (`scripts/s2_censoring_check.py`). Drives removed within 30 days carry roughly twice the prevalence of non-zero reallocated, pending and offline uncorrectable sectors as surviving drives, but matched on drive model and age that excess falls to 0.08, 0.32 and 0.28 percentage points (0.6, 0.3 and 0.3 on nine quarters). Removals are also concentrated by model: in 17 of 21 quarters a single model accounts for at least half of all removals. Both findings indicate wholesale retirement of ageing models rather than selection on individual drive health, which makes censoring close to conditionally independent given the model and the power-on-hours time scale the analysis already conditions on. Not entirely: in the model-by-age cells reported in `s2_health_excess_by_cell.csv`, cells where removed drives show a health excess above one percentage point hold about 6% of removed rows. This is why no competing risks model is fitted (section 3): the dataset carries no observed cause-of-exit label that a subdistribution hazard model would require.
4. Failure is Backblaze's operational definition, not a physical one.
5. Raw SMART values are not comparable across manufacturers. Models are stratified accordingly, and no cross-vendor comparison of raw magnitudes is made.
6. The window opens on 2021-01-01, so 52.1% of drives are left-truncated (84.9% when it opened on 2024-01-01) and the fleet's earlier history is unobserved.
7. Fold 1 trains on eighteen quarters against fold 3's twenty (six against eight on nine quarters; an earlier version of this item said nine, a miscount). Per-fold reporting makes the difference visible.
8. Results describe one operator's datacenters, workload and procurement decisions. They do not describe hard drives in general.
9. 627 of 18,738 events (3.3%) fall into no landmark window and are invisible to every model (726 of 9,790, 7.4%, on nine quarters). Composition, measured in `scripts/s1b_coverage_audit.py`: 151 failed before the first landmark, burn-in from the 30 day change feature that costs training data only; 258 had a spell shorter than the landmark step, a median of under one day, giving a landmark model no history to predict from; 211 were excluded by the staleness rule; 7 entered after the last landmark. In the three test quarters the uncovered share is 4.1%, 6.9% and 12.3%, 238 events in all, of which 180 are assigned to the staleness rule, 51 to short spells and 7 to late entry (`s1b_coverage_reason_by_quarter.csv`). The nine-quarter version attributed the 2026 Q1 figure to fleet growth and infant mortality without measuring it; measured, it is mostly the staleness rule. That category is the audit's residual, assigned when no other cause fits, and why it concentrates in 2026 Q1 was not determined. These exclusions are common to every model, so the comparison between models is unaffected, but reported performance is conditional on a drive being scorable at all. In particular **the model does not address infant mortality**: drives failing within days of installation are structurally outside a landmark framework, and no claim is made about them.
10. E5. On nine quarters the pooled comparison failed at age 2 by 0.23 pp, and matching on drive model and installation vintage resolved it (0.98583 against 0.98622 for the 2024 vintage). On twenty-one quarters the pooled model-matched comparison fails at age 2 by 0.16 pp, and the like-for-like comparison within installation year, run under a rule fixed beforehand (section 13), fails 6 of 20 times: at every age for 2020, where delayed entry sits 0.21 to 0.43 pp above the incident arm, and at 1.5 and 2 years for 2022, where it sits 0.18 and 0.55 pp below. The 2020 comparison is not like for like at monthly resolution, since the only 2020 drives that count as incident are those installed in roughly the last month of 2020; that is a design flaw in the test, found after the result. The 2022 gap is unexplained. The delayed-entry survival curves are therefore not validated for those two vintages. The landmark models do not depend on them.
11. **The models over-predict the overall failure rate.** On the held-out rows the observed 30-day risk is about 0.00092; B1 predicts 0.00112, B2 0.00134 and M2 0.00138. The decision layer is built to tolerate a level error (section 10), but the probabilities should not be read as calibrated rates.
12. **Savings depend on the simulated period.** A replacement is credited with any failure the drive would have had later in the simulated window, not only within the 30-day horizon. Measured in `s11b_window_check.py`: at k = 10 the hindsight-best saving is 5.2 to 18.5% over a single quarter, 15.4 and 20.2% over the two adjacent pairs, and 25.1% over all three. Adding a quarter raises the saving at every k from 5 up, and among equal-length windows the one with more failures saves more. Savings are therefore quoted with the period they were measured over.
13. **The simulator removes a replaced drive and does not model its replacement.** The replacement drive's own failure risk and its added service time are both omitted. At k = 10 the policy removes about 1% of drive-years, so the effect is likely small. The first README and the s9 docstring claimed the omission favours aggressive policies and that any advantage was therefore understated; those two statements contradict each other, and the direction of the effect was never measured.

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

**2026-10-02, section 13a added, expectations re-tested on a twenty-one quarter
window.** Drafted while s5, s6 and s8 were running on the extended data and before
any of their output was read; committed after the s5 E2 verdict had appeared on
screen. The window grew from nine quarters to twenty-one; the test quarters, the
expectations, their criteria and the evaluation code are all unchanged. Because
the 9-quarter verdicts were already known when the extension was decided, the
second run is recorded as a re-test rather than a pre-commitment, both sets of
verdicts will be reported, and the window will not be extended again in search of
a different answer. The reasons for extending it were the three limitations it
addresses, all of which were measured before any model was refitted.

**2026-10-02, second Toshiba model excluded from Cohort A; section 13a
corrected.** At the time: B0 through B3 refitted on the 21-quarter window, with
the s5 and s6 output read, including the E2 and E3 verdicts. M2 not refitted; its
run was stopped during the first fold, before producing any output.

`TOSHIBA MG07ACA14TEY` reports zero coverage on attribute 197 throughout the data,
2021 Q1 to 2026 Q1: 1,051 drives and 58 recorded failures in the raw data. This
is the defect that excluded `TOSHIBA MG08ACA16TEY` in section 4, and it is
excluded for the same reason. It was present in the original nine-quarter window
as well and was missed in section 0, which attributed the Toshiba gap on 197 to a
single model. The 9-quarter B2 and M2 results therefore include it. They are not
refitted: section 13a commits to reporting them as they were, and the model's
size in that window is given in section 14.

The reason is a property of the data, not of any result, and the exclusion would
be made whatever E2 had shown. No criterion in section 9 changes.

Expected effect, written before the refit. The model is about 0.3% of events.

- E2 will not flip. Its Brier difference interval before the fix,
  [+4.92e-06, +3.41e-05], sits clear of zero relative to its own width.
- E1, E3 and E5 are unaffected by construction. E1 and E5 use no SMART features,
  and E3's cohort is Seagate only.
- E4 has no pre-fix 21-quarter value, since M2 was stopped before producing one.
  Its 21-quarter verdict is a re-test in the sense of section 13a.

For comparison with the refit, the pre-fix s5 output on 21 quarters was: E2 AUC
difference +0.20044 [+0.18836, +0.21167], Brier difference +1.848e-05
[+4.921e-06, +3.412e-05], calibration decile spread 0.418 for B1 against 0.388
for B2, and B2's Brier at oracle level below B1's as a point estimate (the
script's "beats" flag; the interval was not recorded before the fix, and after
it the interval includes zero, see the next entry).

Section 13a's opening statement is corrected in the same commit. It said the
section preceded every 21-quarter result; it was drafted before any output was
read but committed after the E2 verdict had appeared on screen. It also gave the
windows as eight and twenty quarters. They are nine (2024 Q1 to 2026 Q1) and
twenty-one (2021 Q1 to 2026 Q1), confirmed by counting distinct quarters in the
ingested data. The original wording remains in commit 24949d2.

**2026-10-02, four evaluation defects corrected and E5 made like for like.**
At the time: every 21-quarter script, s0 through s11, had been run and its output
read. Found while checking the output against the README, before any
documentation was rewritten.

1. **E2's calibration constraint compared spreads computed on different rows.**
   s5 read B1's decile table from s4, which scores the whole fleet, while B2's is
   scored on Cohort A. s5 now computes B1's table on its own rows. On identical
   rows B1's spread is 0.494 (computed by s10), not 0.418; B2's is 0.375 either
   way, so the constraint is met under both and no 21-quarter verdict depends on
   it. The same mismatch was present on the 9-quarter run, where the calibration
   leg was recorded as degraded. That cannot be recomputed on identical rows,
   because the 9-quarter predictions were overwritten. The 9-quarter E2 verdict
   does not depend on it, since Brier failed on its own.
2. **The model ladder figure mixed row sets.** B0 and B1 came from s4 on the whole
   fleet, B2 and M2 from s8 on Cohort A, averaged across folds, so the step from
   B1 to B2 did not equal the E2 difference. s8 now also saves B0's predictions,
   and s10 scores all four models on the identical held-out rows, with paired
   intervals on each step. The 9-quarter README ladder (0.606, 0.672, 0.847,
   0.874) had the same mismatch.
3. **Uncovered events rise through the test quarters**, 4.1%, 6.9% and 12.3% of
   events in 2025 Q3, 2025 Q4 and 2026 Q1. s1b now attributes them by cause and
   quarter. (Corrected in the next entry: this rise was already documented on
   nine quarters, as limitation 9. What was new was attributing it by cause.)
4. **B2's oracle Brier advantage is a point estimate only.** The interval on B2
   minus B1 at oracle level is [-1.59e-05, +1.25e-07], which includes zero. The
   supportable statement is that B2's Brier loss disappears once level is
   corrected, not that B2 then beats B1.

**E5 made like for like.** On nine quarters the incident arm at age 2 was a single
installation vintage, so the vintage check in s3b could match it directly. On
twenty-one quarters incident drives reach age 2 from installation years 2020 to
2024, and the model-matched comparison in s3, which falls 0.161 pp outside the
incident band at age 2, is not the comparison section 9 specifies: same drive
models **and** same installation vintage. s3b now makes that comparison within
each installation year. The criterion in section 9 is unchanged; this implements
it on a window where the earlier shortcut no longer applies.

Decision rule, fixed here before the run. Within each installation year with at
least 2,000 spells at risk at age 2, the delayed-entry estimate on all of that
year's spells is compared with the estimate on its incident spells only, at ages
0.5, 1.0, 1.5 and 2.0, wherever the incident arm still has at least 1,000 drives
at risk. **E5 holds on this window if every compared delayed-entry estimate lies
inside the incident arm's 95% band, and fails otherwise**, with the number and
size of the misses reported. Two caveats are stated now rather than afterwards.
With up to about twenty comparisons at 95%, roughly one miss is expected by chance
even if delayed entry is correct; the rule is not relaxed for that, and a single
miss is reported as a failure under the rule. And in a vintage where nearly every
spell is incident the comparison is weak, so the incident share is reported for
each.

**Prediction for the rerun.** s5 and s8 are seeded. Every number they reported on
2026-10-02 should reproduce exactly, except B1's calibration spread in the E2
output. Any other change is a reproducibility defect and is reported as one.

**2026-10-02, results of the corrected run, documentation rewritten for 21
quarters, and items that had gone unlogged.** At the time: every script had been
run on the 21-quarter window and its output read, including the reruns of s1b,
s3b, s5, s8 and s10 that followed the previous entry.

*Outcomes against what the previous entry fixed in advance.*

- The rerun prediction held: every number s5 and s8 reported reproduced exactly,
  and the only change was B1's calibration spread in the E2 output, 0.418 to
  0.494, as predicted.
- **E5 fails on the 21-quarter window** under the rule fixed in the previous
  entry: 6 of 20 comparisons outside the incident band, at every age for the 2020
  installation year and at 1.5 and 2 years for 2022. This is a flip from the
  9-quarter verdict and is reported as one (section 13a, commitment 2). After the
  result it was noticed that the 2020 comparison is not like for like at monthly
  resolution, because the only 2020 drives that can count as incident are those
  installed in roughly the last month of 2020. That is a design flaw in the test.
  It is recorded, and the verdict is not changed because of it. The 2022 gap is
  unexplained.
- The uncovered-event audit by cause shows the test-quarter rise is mostly the
  staleness rule (180 of 238 events), not the fleet growth and infant mortality the
  nine-quarter text had asserted without measuring. Item 3 of the previous entry
  presented the rise itself as newly found; it had been documented on nine
  quarters.

*A diagnostic added after reading results.* `s11b_window_check.py` runs the
hindsight-best policy on every single quarter, adjacent pair and all three, to
explain why s11's two-quarter savings sit well below s9's three-quarter ones. The
expectation stated before it ran was that quarter composition would explain it.
Both effects appear and agree: adding a quarter raises the saving at every k from
5 up, and among equal-length windows the higher failure rate saves more. Section
12, limitation 12 now carries it, and the README quotes savings with the window
they were measured over. No verdict depends on this script.

*Corrections that change figures or wording only.*

- s10's check that the ladder and the E2 output score the same rows used a
  tolerance of 1e-9, below the floating-point noise between s5's and s8's separate
  refits, and stopped the run. Loosened to 1e-5; a row mismatch would differ in the
  third decimal.
- The per-model survival figure in s3 drew each model's curve from its first
  event, so a model observed only from old age (ST4000DM000) showed a large step on
  a risk set of 15 drives. Curves are now drawn only where at least 500 drives are
  at risk, as survival conditional on reaching that age. The KM tables in
  `reports/` are unchanged.
- s10's calibration figure used a fixed axis floor of 2e-4, which cut off the
  lowest deciles of B2 and M2 on this window. The floor now comes from the data.
- The s9 docstring and the first README stated that omitting the replacement
  drive's risk favours aggressive policies and that their advantage was therefore
  understated, which contradicts itself. Corrected in both; the direction was never
  measured (limitation 13).

*Items that should have been logged earlier and were not.*

- B2 and B3 were implemented as a piecewise-exponential hazard with the B1 offset,
  not the stratified Cox model section 7 specifies. The change preceded B2's fit
  but was recorded only under E6 in section 14. Section 7 now notes it.
- The non-zero indicators in section 4 were removed after B2's first fit showed a
  singular design. Recorded in the code at the time, not here.
- The sensitivity fit in section 4, including the excluded Toshiba models with 197
  dropped, was never run.
- Uno's concordance and the calibration slope and intercept in section 8 were
  never computed.
- Section 0 described a nine-quarter window as eight quarters, and limitation 7
  described a fold-3 training window of eight quarters as nine. It also labelled
  the weekly-sampled row count as drive days observed. All three corrected.
- Section 0 concluded from the 2025 and 2026 arrivals that Backblaze does not
  install second-hand drives. On the longer window about 40% of drives first seen in
  2022 and 2023 had more than 30 days of use, so the conclusion is now limited to
  2024 onward.

*Sections rewritten.* 0, 2, 3, 4, 6, 10 and 12 now carry 21-quarter figures, with
nine-quarter ones in brackets where they differ. Section 14 reports both windows
side by side, as section 13a requires. Section 9 is unchanged.

**2026-10-02, independent review of the documentation; threshold grid refined,
a drive-model-and-age policy added.** At the time: everything in the previous
entry, plus a review of README.md and this document by a separate agent that had
not seen the work, given only the documents and the CSVs. Its findings that change
code or results:

- **The threshold grid was coarse exactly where low cost ratios need it.** It held
  forty quantiles of predicted risk, which left nothing between tau = 0.079 and
  0.621, while the myopic threshold at k = 5 is 0.2. The README's claims for k of 5
  and below, that the saving appears only over three quarters and that break-even
  is near k = 2, could therefore reflect the grid rather than the policy.
  `threshold_grid` in s9 now adds forty log-spaced values up to the largest
  predicted risk, and s9, s11 and s11b all use it. The new grid contains the old
  one.
- **"Everything the policy is worth comes from SMART telemetry" was never tested.**
  Only the M2 policy was simulated. s9 is now also run on B1's predictions, drive
  model and age only, into `reports/policy_b1/`.

Predictions, written before the reruns:

- Hindsight savings in s9 and s11b can only stay equal or rise, since the new grid
  contains the old. At k = 10 and above the optimum already sat in the dense part
  of the grid, so those figures should move by under one percentage point. At k = 5
  and below they may rise materially, and single-quarter savings at k = 5 may no
  longer be zero.
- s11's optimism may rise, since a finer grid gives hindsight more to fit. If it
  exceeds the 5-point threshold s11 prints, the prospective figures become the ones
  to quote.
- The B1 policy should save much less than M2's at every k. If it saves
  comparably, the claim that the value comes from SMART telemetry is wrong, and the
  README will say so.

Wording findings, corrected without rerunning anything: E2 was not in the README's
first paragraph as section 9 requires; the headline savings did not say their
threshold is chosen in hindsight; "calibration did not degrade" judged spread and
ignored that B2's level worsened; section 3 attributed the wrong bias to immortal
time; the competing-risks justification did not say that "removed" is inferred
rather than observed; event counts were not reconciled; the s10 and s11 figures
carried two misleading labels.

---

## 13a. Status of the expectations after the window extension

**Drafted while the 21-quarter model scripts were running, before any of their
output had been read. Committed after the first 21-quarter verdict (E2, from s5)
had appeared on screen, and before any other 21-quarter result.** It cannot claim
to precede the run, which had already started, or the E2 verdict. None of the
commitments below depend on that verdict, and they would read the same had it
passed. (Corrected 2026-10-02; the first version claimed to precede every result,
see section 13.)

The observation window was extended from nine quarters (2024 Q1 to 2026 Q1) to
twenty-one (2021 Q1 to 2026 Q1). Nothing else changed: the three rolling-origin
test quarters are the same, the expectations in section 9 are the same, their
criteria are the same, and the code that evaluates them is the same. Only the
training windows grew, from six to eight quarters to eighteen to twenty.

**The second run is a re-test, not a fresh pre-commitment, and is reported as
one.** The 9-quarter verdicts were already known when the window was extended:
E1 held, E2 failed, E3 held on its primary criterion with its directional half
wrong, E4 held, E5 held once specified correctly. Re-running the same expectations
with that knowledge is not the same epistemic act as declaring them blind, however
unchanged the criteria.

Three commitments follow, made here rather than after the fact:

1. **Both sets of verdicts are reported**, 9-quarter and 21-quarter, side by side
   in section 14. Neither replaces the other.
2. **A verdict that flips is reported as a flip**, with both values, and is not
   presented as "the" result. In particular, if E2 now passes, the README still
   records that it failed on the original window and explains what changed.
3. **The window is not extended again** in pursuit of a different verdict. If a
   result is unstable between nine and twenty-one quarters, that instability is the
   finding and gets stated as such.

The reason for extending the window is recorded in the amendment log and was
decided before any 21-quarter model was fitted: it reduces the fold imbalance in
limitation 7 from six quarters against eight to eighteen against twenty, roughly
halves the left truncation from 84.9% to 52.1%, and halves the share of events no
landmark can see from 7.4% to 3.3%. None of those are about moving a verdict.

---

## 14. Results against the pre-commitments

Every expectation in section 9 was fixed and committed to git before the model it
concerns was fitted. This section records what happened, including the failures.
It is the reason the document exists: a design that can be quietly edited after
seeing results proves nothing.

The project was run twice: on nine quarters (2024 Q1 to 2026 Q1) and, after the
window was extended, on twenty-one (2021 Q1 to 2026 Q1), with the same test
quarters, expectations, criteria and evaluation code. The second run is a re-test,
not a fresh pre-commitment (section 13a), and both are reported. Nine-quarter
figures are as recorded at the time and are not recomputed.

### Expectations

| | expectation | 9 quarters | 21 quarters |
|---|---|---|---|
| **E1** | B1 beats B0 modestly | **held.** AUC +0.0667 [+0.0580, +0.0775], Brier -5.63e-07 [-6.47e-07, -4.98e-07]. | **held.** AUC +0.0503 [+0.0402, +0.0626], Brier -2.89e-07 [-3.59e-07, -2.31e-07], whole fleet as specified. On the Cohort A rows used for the ladder, +0.039 [+0.029, +0.048]. |
| **E2** | B2 beats B1 | **failed.** AUC +0.1873 [+0.1760, +0.1994]; Brier +6.1e-06, interval spanning zero; calibration recorded as degraded, 0.312 to 0.460, but B1's figure came from different rows (section 13), so that leg is not reliable. | **failed.** AUC +0.2007 [+0.1896, +0.2101]; Brier +1.86e-05 [+7.09e-06, +3.08e-05], reliably worse; calibration on identical rows not degraded, 0.494 to 0.375. At oracle level the Brier difference is -7.8e-06 [-1.59e-05, +1.25e-07], so the loss is level error. |
| **E3** | B3 beats B2 on the Seagate cohort; 187 carries most of the gain, the other four contribute little | **held on the primary criterion, directional claim half wrong.** AUC +0.0248 [+0.0183, +0.0337]; Brier improved raw and at oracle level. 187 carries 72.2%; the other four add +0.0069 [+0.0039, +0.0117]. | **held on the primary criterion, directional claim half wrong.** AUC +0.0203 [+0.0144, +0.0280]; Brier not distinguishable from zero raw or at oracle level. 187 carries 87.8%; the other four add +0.0025 [+0.00005, +0.0053]. |
| **E4** | M2 beats B2 by a small margin and is no better calibrated | **held, one part wrong in the favourable direction.** AUC +0.0287 [+0.0243, +0.0322]; Brier improved raw and at oracle level; calibration came out better, 0.486 to 0.461. | **held as written.** AUC +0.0247 [+0.0215, +0.0283]; Brier -3.87e-05 [-4.84e-05, -2.66e-05] raw and -2.67e-05 [-3.41e-05, -1.89e-05] at oracle level; calibration no better, 0.375 to 0.387. |
| **E5** | the delayed-entry estimate agrees with an untruncated cohort, like for like | **held once compared like for like.** The pooled comparison failed at age 2 by 0.23 pp; matched on model and vintage, 0.98583 against 0.98622. | **failed.** Within installation year, under a rule fixed beforehand, 6 of 20 comparisons fall outside the incident band: all four ages for 2020 (delayed entry 0.21 to 0.43 pp higher) and 1.5 and 2 years for 2022 (0.18 and 0.55 pp lower). The 2020 test is not like for like at monthly resolution, a design flaw found after the result; the 2022 gap is unexplained (section 12, limitation 10). |
| **E6** | proportional hazards is rejected by Schoenfeld residuals for the age term | **not tested, no longer testable.** E6 assumed a Cox model; B2 was implemented as a piecewise-exponential hazard with the B1 offset, which carries no proportional hazards assumption to reject. | superseded, as on nine quarters. |

**Tally.** On nine quarters, three of five testable expectations held, one failed,
and one held on its primary criterion with its directional claim half wrong. On
twenty-one quarters, two held, two failed, and one held on its primary criterion
with its directional claim half wrong. The flips are E5, from held to failed, and
the calibration part of E4, from wrong to right.

### Research questions

**RQ1, dynamic risk.** Yes for ranking, qualified for probabilities. On twenty-one
quarters M2 reaches IPCW AUC 0.874 pooled over the three held-out quarters (0.877,
0.872 and 0.874 individually), against 0.610 for age alone on the same rows (0.874
and 0.606 on nine quarters, the latter on different rows). Every SMART model
over-predicts: mean predicted risk 0.00138 for M2 and 0.00134 for B2 against about
0.00092 observed (0.00114 against 0.00092 for M2 on nine quarters). The level error
is larger on the longer window, whose training years ran at a higher failure rate
than the test quarters. That is why section 10 sweeps the level instead of
correcting it.

**RQ2, the vendor gap.** Small but real. On the Seagate cohort the universal model
reaches AUC 0.855 and all five Seagate-only attributes lift it to 0.875 (0.856 to
0.881 on nine quarters). A mixed-vendor fleet restricted to universal telemetry
gives up about two AUC points, of which attribute 187 alone would recover 88% (72%
on nine quarters). Attributes 197 and 198 are identical within Seagate, and 190 is
collinear with 194 to four decimal places; the nine-quarter text called 190 an exact
duplicate, which on twenty-one quarters it is not quite, since it survives the
exact-duplicate check.

**RQ3, the decision.** Over the three held-out quarters, risk-based replacement
lowers the fleet cost rate by 8.3% at k = 5, 25.1% at k = 10, 37.0% at k = 20 and
50.5% at k = 50 (9.3%, 24.7% and 50.6% at 5, 10 and 50 on nine quarters), with
break-even near k = 2. The result is stable under the fleet hazard level coming in
20% either way. A threshold chosen on 2025 Q3 and applied to the next two quarters
costs at most 0.46 percentage points against hindsight. The size of the saving
depends on the simulated period (section 12, limitation 12): at k = 10 it is 5 to
18% over any single quarter and 25% over three, and at k = 5 it appears only over
the full three quarters. Age-based replacement never pays at any cost ratio tested,
on either window.

### Measured quantities

| quantity | 9 quarters | 21 quarters |
|---|---|---|
| drives / spells | 384,213 / 391,275 | 429,870 / 441,894 |
| landmark rows | 8.5 million | 17.0 million |
| failure events, reconciling with source flags | 9,790 | 18,738 |
| left-truncated drives | 84.9% | 52.1% |
| events in no landmark window | 7.4% | 3.3% |
| fleet annualised failure rate, held-out window | 1.107% | 1.104% |
| peak hazard | 3.14% at 6.2 years | 2.67% at 6.75 years |
| AUC, B0 / B1 / B2 / M2 | 0.606 / 0.672 / 0.847 / 0.874, mixed rows | 0.610 / 0.649 / 0.849 / 0.874, identical rows |
| covered events in the three held-out quarters | 1,020 / 781 / 880 | 1,014 / 777 / 876 |

### Phase 1 status

Complete: ingest pipeline, survival tables, informative-censoring test,
delayed-entry validation, B0 through B3, M2, evaluation suite, decision layer, fleet
simulation, prospective and window checks on the policy, and the README.

Removed from Phase 1 during the work, each with a logged reason: Fine-Gray (no
observed cause-of-exit label in the data) and M1 (could not be fitted on the same
rows as the rest of the ladder). Planned and not done, logged in section 13: the
Toshiba sensitivity fit, Uno's concordance, and calibration slope and intercept.
