# Verified records to reproducible evaluation

The latest reviewed reports already contained verified revenue evaluations: DELL had 27–29 F4Q samples per raw rule and TSMC 2–5. The remaining all-statement insufficiency must not be described as zero usable revenue evaluation. No new report section or quality gate is introduced.

## Demonstrated defects and focused changes

- DELL's full-statement forecast always projected 52 weeks, excluding four origins across its 53-week fiscal year. The calendar now constructs quarter boundaries from the public Friday-nearest-January-31 rule, not realized targets. The rule appears in a [2018 SEC registration statement](https://www.sec.gov/Archives/edgar/data/1574135/000104746918002061/a2234898zs-1.htm), before every initialized DELL evaluation origin. Restored windows include the extra week in Q4. Revenue rules remain unnormalized level/trend assumptions; the extra week does not imply an earnings adjustment.
- Date verification, verified original status, distinct filing counts, metric feature verification, forecast initialization and completed scoring are separate outputs. Missing diluted shares and unverified EPS inputs are not repaired by verifying revenue. Missing forward cash-conversion assumptions still prevent OCF/FCF predictions.
- Zero actuals previously vanished from candidate scores because MAPE was used as an eligibility filter. They now count for MAE and signed bias, and for pooled WAPE when total absolute actuals are nonzero. MAPE stays undefined for individual zero actuals. Partial guidance paths cannot be scored as complete four-quarter totals.
- MAE, WAPE, signed bias, aggregate signed percentage bias and matched baseline MAE skill are reported. MAE skill is `1 - candidate_MAE / baseline_MAE`, undefined for zero baseline MAE. Pairing uses identical origins, horizons and actual evidence. A method that fails to beat baselines on MAE cannot be promoted by its MAPE alone. Raw and guidance-overlay methods remain separate; retrospective rule comparison is not prospective method selection or statistical validation.
- Observed availability bounds now also define the origin cutoff; they cannot silently become earlier publication timestamps. Later revisions, guidance and unavailable prices remain excluded. Evaluations use current reviewed target vintages, explicitly distinct from historical training vintages. Overlapping F4Q errors are not independent samples.
- F4Q is explicitly the four fiscal quarters after the last reported period. The as-of next-twelve-month start/end and completed-unreported quarters are separately recorded. No daily prorating or fabricated five-quarter forecast is used to pretend F4Q equals as-of NTM. Legacy JSON keys named `ntm` are retained for compatibility and explicitly qualified by `horizon_basis`. Spot-price / F4Q-earnings sensitivities are not discounted targets.
- Historical P/E records retain the actual price date, avoid duplicate quotes and use only EPS vintages available before that price day. A late-observed price cannot create a historical PIT multiple. A future split no longer blocks all earlier observations; a split within the earnings-to-price window still blocks that observation pending a share-basis audit. Fully post-split earnings windows may be used without double adjustment. Absence of supplied split records is disclosed, not an independent corporate-action completeness certification.

## Foreign issuer filing conventions

The catalog already supported 20-F and 6-K; this was not a domestic-only parsing defect. [SEC guidance](https://www.sec.gov/divisions/corpfin/internatl/foreign-private-issuers-overview.shtml) distinguishes annual 20-F and current 6-K from domestic 10-K/10-Q. TSMC's 6-K identity and date can be verified without establishing that it is an original quarterly periodic filing. Such records remain availability-eligible with their actual vintage label. Neither a 6-K header nor an annual 20-F creates missing quarterly observations.

TSMC's losses mainly reflect missing original financial values at early cutoffs and insufficient contiguous training history; DELL's include late comparative-only vintages and the calendar defect. A catalog entry verifies metadata, not uncollected historical financial features. The private comparison lists the exact origin and metric root causes.

## Historical-price source investigation

No source permission is changed and no operator attestation is created. Existing authorized `import-market-data` paths for prices, splits, consensus and peers are preserved. Existing fixed-cutoff evidence is reused; newly observed current snapshots cannot be backdated into these reports.

| Candidate | Evidence and outcome |
| --- | --- |
| TWSE licensed government open data | Existing reviewed dataset 11764 provides a current snapshot. [Dataset 11551](https://data.gov.tw/dataset/11551) supplies annual aggregates, not a daily close history. Historical web queries are outside the authorized open-data resource; [TWSE terms](https://www.twse.com.tw/en/terms/use.html) restrict automated downloading. No historical query is made. |
| TWMD secondary provider | Its [pricing](https://twmarketdata.com/en/pricing) describes a one-month free tier and paid longer history; [licensing](https://twmarketdata.com/en/legal/licensing) gives a general open-data statement, not a specific historical-price upstream resource binding. Five-symbol demos exist, but dataset-level history, identity, publication-clock semantics and upstream authorization have not been established to this project's five-aspect source-review standard. Remains unverified, not declared nonexistent or activated. |
| NYSE TAQ / Cboe | Official [NYSE historical products](https://www.nyse.com/market-data/historical) and [Cboe EOD summary](https://datashop.cboe.com/equity-eod-summary) offer historical prices under purchase/licensing arrangements. Not a free no-key acquisition path. No purchase is made. |
| Nasdaq website | [Terms](https://www.nasdaq.com/legal) restrict data capture. The website is not an authorized historical-data fallback. |
| DELL issuer cover-page prices | Existing audit identifies possible sparse annual prices, not a verified daily history. A price appearing in a later annual report cannot establish that this copy was available at the earlier price date. Not acquired or counted as PIT observations. |

The provider review is not exhaustive. Historical P/E references and DELL reverse valuation remain limited by actual licensed inputs. No arbitrary P/E range, consensus estimate or substituted ADR price is introduced.

## Reproduction and acceptance

Run `scripts/runtime.sh pit-review`. It freezes the two latest reviewed inputs, reads all four analyst contract files, changes period labels only in the prior proposal, finalizes into new runs, checks actual PDF/research/manifest, and asserts identical evidence, scenario EPS, multiples and implied values. It refuses a page-count increase. Runtime defaults to Apple Container and supports `CONTAINER_ENGINE=docker`; host dependencies are not installed.

Private outputs are `reports/pit-evaluation-comparison-20261117.json` and `.md`, plus each run's `evaluation-comparison.json`, `forecast_qa.json`, PDF, research and manifest. Old WAPE is recomputed from saved old predictions, not a rerun of the old model. The primary comparison measures coverage, eligible/scored samples, baseline errors, price coverage, valuation references and reverse-valuation availability. Gate counts are not a success metric. Raw financial artifacts remain ignored by Git and are for personal use only.
