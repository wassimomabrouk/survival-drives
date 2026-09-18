# Fleet reliability and replacement policy

**When should a datacenter replace a hard drive that has not failed yet?**

Thirteen years of Backblaze Drive Stats telemetry, framed as a survival analysis
problem, turned into a replacement policy and evaluated against the alternatives
on quarters the models never saw.

![What predictive replacement is worth](figures/s9_value_vs_cost_ratio.png)

**The answer.** If an unplanned drive failure costs at least twice as much as a
planned replacement, replacing on predicted risk lowers the fleet cost rate: 9.3%
cheaper at a cost ratio of 5, 24.7% at 10, 50.6% at 50. Below a ratio of about 2,
run the drives to failure.

**Replacing on drive age never pays, at any cost ratio tested.** Age is free and
requires no model. A policy built on it is worse than doing nothing, because it
replaces 2,428 drives while intercepting none. Everything the policy is worth
comes from SMART telemetry.

---

## What this estimates

> The conditional probability that a drive **already in Backblaze's observed
> fleet** experiences a Backblaze-recorded failure within the next 30 days, given
> its accumulated power-on hours, drive model, and SMART telemetry up to the
> prediction time; and the use of those probabilities to evaluate cost-based
> replacement policies on temporally held-out quarters.

Three things it is not. Not hard drive lifespan from manufacture, since the data
begins when a drive enters the fleet and earlier history is unobserved. Not a
physical definition of failure, since failure is Backblaze's operational
determination. Not a statement about drives in general, since it describes one
operator's datacenters, workload and procurement.

## Scale

| | |
|---|---|
| source | Backblaze Drive Stats, 2024 Q1 to 2026 Q1 |
| drives / spells | 384,213 / 391,275 |
| drive days observed | 36,142,935 |
| failure events | 9,790 |
| landmark prediction rows | 8.5 million |
| validation | rolling origin, three held-out quarters |

The ingest reproduces Backblaze's published figure of 1,030 failures for 2026 Q1
exactly, and their published 1.24% annualised failure rate for that quarter.

---

## Results

### 1. SMART telemetry predicts which drives fail

![Discrimination across the model ladder](figures/summary_model_ladder.png)

Drive age alone reaches an IPCW time-dependent AUC of 0.606. Adding the drive
model reaches 0.672. Adding SMART attributes reaches 0.847, and replacing the
log-linear form with boosted trees reaches 0.874. Every figure is pooled across
three quarters that the model producing it was never trained on.

The jump from 0.672 to 0.847 is the whole story. Everything after it is
functional form.

Current pending sector count dominates, both as a level and as a 30-day rise:

![M2 feature importance](figures/m2_feature_importance.png)

### 2. It does not predict how many will fail

![Calibration](figures/summary_calibration.png)

Every model over-predicts. The curves sit parallel to and below the diagonal,
which is level error rather than shape error: the ranking is right and the
overall rate is too high. Measured quarter-to-quarter variation in the fleet
failure rate is about 21% with no trend, so the level cannot be corrected by
fitting on the preceding quarter. That was attempted, measured, and rejected; the
attempt and the evidence against it are both in the repository.

The decision layer therefore treats the level as a swept parameter rather than an
estimated one. The policy recommendation is stable under the fleet rate coming in
20% either way.

### 3. Vendor-specific telemetry is worth about 2.5 AUC points

![RQ2 decomposition](figures/summary_rq2_decomposition.png)

Attributes 187, 188, 190, 241 and 242 exist only on Seagate firmware. Fitted on
the same Seagate cohort, the universal-attribute model reaches AUC 0.856 and
adding all five reaches 0.881. Attribute 187 carries 72% of that gain.

So a mixed-vendor fleet restricted to universally reported telemetry gives up
roughly 2.5 points, which is real but modest. Two further findings emerged:
attribute 190 duplicates the universal attribute 194 exactly on Seagate firmware
and contributes nothing, and 197 and 198 are likewise identical within Seagate
while differing across the full fleet.

### 4. Risk beats age, and age is worse than doing nothing

![Risk-based against age-based replacement](figures/s9_risk_vs_age_policy.png)

At a cost ratio of 10, the risk-based policy triggers 5,705 replacements, of
which 1,196 are drives observed to fail later and 4,509 are not. Roughly one in
five replacements intercepts a drive before its observed failure.

**No intervention took place.** These are labelled *projected* throughout. If the
policy would have replaced a drive on day 3 that was observed to fail on day 20,
the correct statement is that the policy would have removed it from service before
its observed failure, not that a failure was avoided. That counterfactual was
never observed, and the replacement drive would carry its own risk.

---

## Why the survival setup is not standard

Most published analyses of this dataset treat it as binary classification at a
fixed horizon. That discards censored drives, which are the overwhelming majority,
and cannot answer *when*. Four things here are done differently.

**Power-on hours as the time scale, with delayed entry.** A drive's first
observation typically shows tens of thousands of hours already accumulated: the
median standing drive has 3.3 years of prior use. Setting time zero at first
observation would assume every drive arrived new and would bias the early hazard
downward through immortal time bias. 84.9% of the cohort is left-truncated.

![Survival with delayed entry](figures/b0_survival_by_model.png)

**The hazard is not flat.** Annualised failure rate rises from 0.63% at one year
to a peak of 3.14% at 6.2 years, then declines as the surviving population
self-selects.

![Hazard by age](figures/b0_hazard.png)

**Delayed entry is validated, not assumed.** The risk-set construction agrees to
0.1% with a completely independent exposure-based estimator. And the delayed-entry
estimate matches an untruncated cohort of drives installed new during the window,
once matched on drive model and installation vintage.

![Delayed entry validation](figures/b0_incident_validation.png)

**Censoring inside the horizon is handled, not ignored.** Drives leave the fleet
without failing about four times as often as they fail. Every metric uses inverse
probability of censoring weights, and the uncertainty is bootstrapped at spell
level rather than row level, because one drive contributes many correlated
landmark rows.

Whether that censoring is informative was tested rather than assumed. Conditional
on drive model and age, health does not predict removal: removed drives carry
roughly twice the prevalence of non-zero sector counts, but matched on model and
age that excess falls to 0.6 percentage points. Removals are also concentrated by
model, with one model accounting for 50 to 99 percent of removals in most
quarters, which indicates wholesale retirement rather than selection on individual
drives.

No competing risks model is fitted. A subdistribution hazard model requires an
observed cause-of-exit label, and this dataset does not contain one.

---

## Pre-commitment

Every directional expectation was written into `DESIGN.md` and committed to git
**before** the model it concerns was fitted. The commit history shows each one
timestamped ahead of its result.

| | expectation | outcome |
|---|---|---|
| E1 | drive model adds to age | **held** |
| E2 | SMART adds to drive model and age | **failed** |
| E3 | vendor attributes add, mostly via 187 | **held**, directional half wrong |
| E4 | boosting adds a small margin over the linear form | **held** |
| E5 | delayed entry agrees with an untruncated cohort | **held** after the comparison was corrected |
| E6 | proportional hazards is rejected | **superseded** by a frame change |

**E2 failed.** SMART improved AUC by 0.187 with a comfortable interval, but did
not improve the Brier score and degraded calibration, and the criterion required
all three. The failure is informative: at roughly one event per thousand rows, the
Brier score is dominated by the overall rate rather than the ranking, so a large
ranking gain and a slightly worse level net to nothing.

`DESIGN.md` section 14 records each verdict in full, and section 13 logs every
amendment made during the work with its date, its reason, and which models had
been fitted at the time.

---

## Reproducing

The data is not in this repository. It is roughly 10 GB of downloads and 0.5 GB
of Parquet, and it is gitignored.

Commands below use the Windows `py` launcher, which is what the committed results
were produced with. On macOS or Linux substitute `python3` for `py` and drop the
`-m pip` prefix if `pip` is on your path.

```
py -m pip install -r requirements.txt
mkdir data\zips data\parquet reports figures
```

Download the eight quarterly archives from
[Backblaze Drive Stats](https://www.backblaze.com/cloud-storage/resources/hard-drive-test-data),
`data_Q1_2024` through `data_Q1_2026`, into `data/zips`. The ingest processes one
quarter at a time and deletes its scratch files, so peak disk use stays near 4 GB
even though the archives expand to about 12 GB each.

```
py scripts\s0_ingest.py         --zips data/zips --out data/parquet
py scripts\s0_diagnose.py       --parquet data/parquet --reports reports
py scripts\s1_build_tables.py   --parquet data/parquet --out data/tables --reports reports
py scripts\s1b_coverage_audit.py
py scripts\s2_censoring_check.py
py scripts\s3_b0_baseline.py
py scripts\s3b_vintage_check.py
py scripts\s4_b1_baseline.py
py scripts\s5_b2_smart.py
py scripts\s6_b3_seagate.py
py scripts\s6b_design_check.py
py scripts\s7_horizon_check.py
py scripts\s8_m2_boosted.py       # writes the held-out predictions
py scripts\s9_decision_layer.py   # requires s8
py scripts\s10_summary_figures.py
```

All scripts default to `data/tables`, `reports` and `figures`. Every number in
this README is written to a CSV in `reports/`, so any claim here can be checked
against the file that produced it.

No survival analysis library is used. Kaplan-Meier with delayed entry,
Nelson-Aalen, the IPCW Brier score, time-dependent AUC and the penalised Poisson
hazard model are implemented directly in `scripts/evaluation.py` and the model
scripts. Correct handling of left truncation and of censoring inside the horizon
is a central claim here, so those computations are visible rather than delegated.

---

## Limitations

1. **Failure time is interval-censored.** One snapshot per day, so a failure falls somewhere inside a 24 hour window. Immaterial against lifetimes in tens of thousands of hours.
2. **The reason a drive leaves the fleet is not published.** Tested rather than assumed, and found conditionally independent given the covariates in use, but a residual tail of under one percent of removals does show health selection.
3. **7.4% of events fall into no landmark window** and are invisible to every model. Most are burn-in from the 30-day change feature. 143 had a spell of roughly one day, so **the model does not address infant mortality**: drives failing within days of installation are structurally outside a landmark framework.
4. **Individual hazard ratios are not interpretable.** SMART counters are collinear, which produces sign flips. The predictive comparisons are reportable; the coefficients are not, and the forest plots were removed for that reason.
5. **The window opens in 2024**, so 84.9% of the cohort is left-truncated and earlier fleet history is unobserved.
6. **The cost ratio is not public.** All monetary quantities are in units of one planned replacement, and the ratio is swept rather than invented.
7. **The simulator assumes a replaced drive leaves service and carries no failure risk.** This favours aggressive policies, so any advantage found for risk-based replacement is if anything understated.
8. **One operator, one workload, one procurement history.**

## Deliberately not done

Deferred and not a condition of completion: DeepHit and other deep survival
models, shared frailty by manufacturing batch, landmark supermodels.

Removed during the work with reasons logged: Fine-Gray, because the data carries
no observed cause-of-exit label; and Random Survival Forest, because it could not
be fitted on the same rows as the rest of the ladder and is not an independent
model family from the boosted trees.

Out of scope by design: serving infrastructure, orchestration, monitoring,
retraining pipelines. This is a data science project.

---

## Repository

```
DESIGN.md              pre-committed design, amendment log, results against expectations
requirements.txt
scripts/               s0 ingest through s10 figures, plus evaluation.py
reports/               every table behind every number in this README
figures/
data/                  gitignored
```

Data from [Backblaze Drive Stats](https://www.backblaze.com/cloud-storage/resources/hard-drive-test-data),
used under their terms.
