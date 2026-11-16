# Filing catalogs and valuation validation

The engine keeps stored records, eligible origins, initialized predictions, and scored samples as separate counts. A source record count never establishes predictive reliability.

## Historical evaluation

`cli filing-catalog` verifies cited SEC accessions against the reviewed issuer submissions file and, when necessary, the accession's index header. It retains normalized identity, form, reporting period, filing date, amendment status, and the source-declared acceptance timestamp. Child submissions files are not fetched under the base-file permission. An issuer-name mismatch stays unresolved.

Acceptance is not an exact public-availability timestamp. SEC explains that there is no timestamp for first availability on sec.gov, and that dissemination may follow acceptance. See the [SEC webmaster FAQ](https://www.sec.gov/files/about/webmaster-faq.htm). The catalog does not convert unverified timestamp semantics or move evidence earlier. Date-only financial evidence retains the collector's conservative end-of-filing-day convention, at second resolution in fixed UTC-05. Catalog-confirmed date bounds are disclosed separately from exact source publication timestamps and observed-availability bounds. They are not independently preserved historical retrieval snapshots.

Each walk-forward origin filters financial versions and guidance by their original clocks. Revisions available later cannot enter training. Forecast metrics require verification of their own contributing inputs; a verified revenue origin does not verify shares, tax assumptions, or EPS inputs. Target actuals use the latest reviewed values and retain their identities. Revised-data retrospectives remain separate and cannot promote a method.

Catalog identity does not establish unchanged business scope across training and targets. Historical acquisitions, disposals, discontinued operations, and presentation changes require a separate comparability review; a shared GAAP label is insufficient. Unadjusted descriptive errors remain visible rather than selectively dropping adverse periods.

Revenue candidates and full-statement forecasts use matched origins, horizons, and actual values against simple baselines. Raw rules and guidance overlays are evaluated separately. Error, signed bias, coverage, training-window changes, and exceptional-quarter sensitivity are descriptive. Overlapping NTM windows are not independent samples. Sample floors are disclosure prerequisites, not statistical tests or a selection objective.

## Valuation coverage

Historical P/E uses EPS versions published before each price observation, rather than the earnings quarter ending on that price date. Missing historical EPS vintages remain missing. Split histories require an explicit EPS share-basis audit before historical multiples can be computed; published restated EPS must not be adjusted twice.

Imported peer multiples remain available as descriptive comparisons, but a count of peer labels cannot establish issuer identity, earnings basis, date alignment, or business comparability. Unreviewed comparisons cannot justify scenario multiples or fair values.

The reviewed free CSV source `twse-opendata-bwibbu-d-csv` is restricted to the exact government dataset resource:

`https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d?response=open_data`

The source has observed availability and latest-snapshot coverage. The identity review currently covers only 2330.TW. No date query, JSON alias, peer identity, raw-response retention, or response-hash permission is inherited. Activation requires the five-aspect source review and `activation-check`; fresh collection follows activation. The government dataset is [11764](https://data.gov.tw/dataset/11764), under the [Open Government Data License](https://data.gov.tw/license), subject to attribution. Its reported reference-earnings P/E does not establish a diluted-EPS basis. The current adapter collects identity and close only.

No approved free US closing-price/history/corporate-action dataset was established by this review. Restricted vendor quotes, paid exchange products, unavailable downloads, unsuccessful document retrievals, and unreviewed sources are separate dependencies. A generic source-unavailable gap alone does not prove a failed retrieval. Authorized operator import remains available, with the user's own rights attestation; an assistant cannot create that attestation.

## Cash flow and artifacts

Historical cash-flow bridges retain actual statement components, signed movements, and unexplained residuals. Reconciled history is assessed for coverage, overlap, cash conversion, working-capital intensity, and business-regime changes. Even a stable historical screen cannot authorize forward OCF or FCF without evidenced forward assumptions. Historical FCF-margin sensitivity is arithmetic, not a forecast, confidence interval, or economic bound.

Per-run private `forecast_qa.json`, `validation_summary.json`, `data-review.json`, and `version_attribution.json` contain diagnostics and comparisons. Reports and financial audit data remain outside Git. Report layout and section structure are preserved.
