# NVDA material-event report correction

The earlier NVDA output was an annual financial snapshot with no recent-event review. It omitted a CEO transition announced before its cutoff, so it was not acceptable as completed stock research.

The correction imports issuer-filed facts from SEC accession `0001045810-26-000082`: the November 11 CEO departure and interim appointment, the November 12 continuity statement, and the announced November 17 earnings schedule. The continuity statement is a company expectation, not independent proof of unchanged execution. The filing does not establish a resignation cause. Unresolved governance leads remain visible and prevent directional conclusions.

`sec-edgar-archives-filing` is now a separate source contract. Its recorded assistant review only opens private internal use of normalized facts; distribution, source response hashes, and raw responses stay closed. The activation package is `nvda-event-source-activation.json`; the baseline was registry 0.5.1 with the archive contract registered but closed, and the candidate 0.5.3 passed `activation-check` before activation. Existing XBRL permissions were unchanged.

## Bounded collection

Use `scripts/runtime.sh cli collect --source sec-edgar-archives-filing --ticker NVDA --allow-network --source-profile <reviewed-plan.json> --output <directory>`. Supply `STOCK_REPORT_SEC_CONTACT` through the operator environment; never put it in the plan. A plan has `reviewed_by`, `authorization`, and `facts`; each fact has an ID, official archive URL, normalized claim, exact reviewed excerpts, publication date, and event date. Dates and claim interpretation require source review. The collector checks configured issuer CIK, source permission, official host/path, issuer name, and exact live excerpts. It fetches each document once, stores normalized observations only, and does not accept fixture fallback.

This is reviewer-curated collection, not automatic news discovery or a guarantee that every material event has been found. Build the financial bundle, merge a reviewed stock-event dossier, prepare with `--provider import`, read the generated analysis inputs, supply an evidence-grounded proposal, and finalize. Preserve missing prices, current-quarter coverage, and unresolved governance matters as explicit limitations.

## Delivery checks

The PDF now places major events before financial metrics, prioritizes material over newer minor items, and shows unresolved review status. The two stock horizons retain full interpretation, conditional scenarios, and specific reassessment triggers. Insufficient proposals keep non-directional follow-up conditions; the directional-language gate remains active. Annual figures are labeled as historical. No target price is fabricated.

Inspect an actual run with `scripts/runtime.sh qa --report <run>/report.pdf --output <qa-directory>`. Default `qa` still inspects demos. Validation uses `scripts/runtime.sh test -q`, plus checks of actual PDF text, both rendered pages, `research.json`, and manifest hashes. Private generated artifacts remain in ignored `reports/`, never committed or distributed.
