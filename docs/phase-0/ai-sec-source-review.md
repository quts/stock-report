# SEC XBRL source review (2026-11-15)

This is an assistant review under the user's authorization in AGENTS.md, not a human attestation. The candidate registry and activation package must pass `activation-check` before activation.

## Official evidence

- API documentation: https://www.sec.gov/search-filings/edgar-application-programming-interfaces documents the `data.sec.gov` company-concept API, authentication-free access and updates as filings are disseminated.
- Reuse policy: https://www.sec.gov/about/webmaster-frequently-asked-questions explicitly permits free access and reuse of public EDGAR filing content. This review covers structured financial facts, not photographs, trademarks or content on external websites.
- Access policy: https://www.sec.gov/about/developer-resources requires efficient, identified and rate-limited access. The operator supplied a contact separately; it is passed in the environment only, never committed or written into the report. The collector requests sequentially with its default interval.
- Identity: https://www.sec.gov/Archives/edgar/data/1045810/000104581026000075/nvda-20260726.htm identifies NVIDIA Corporation, common stock, NVDA and the Nasdaq Global Select Market. CIK 0001045810 and the NVIDIA entity-name binding were also checked by the live adapter.

## Live contract and limits

Before activation, a network collection on 2026-11-15 passed identity and annual financial field checks. Only the inspection record was retained while permissions were closed. It contains field/role/unit shapes, retrieval statuses and blockers, not financial values or response hashes.

The adapter produces normalized annual revenue (current/prior), net income, EPS (current/prior), operating cash flow and capital expenditure where present. It selects annual 10-K facts, preserves reporting periods and filing dates, and uses a conservative end-of-day bound for a date-only publication stamp. This does not establish exact publication times, latest quarterly coverage, current prices or current events.

Only internal normalized-data use is proposed. Raw-response retention, response hashes and redistribution remain disabled. A fresh collection after activation is required; the earlier closed inspection cannot be promoted to usable evidence. NVDA is the live-tested issuer; other configured issuers are not claimed to have passed this live identity check.

Activation passed against registry 0.5.0; registry 0.5.1 opens only SEC internal use. The checked package baseline is now 0.5.1 for subsequent drift checks. Permissions apply to the SEC endpoint template, not to an individual CIK; enabling source reuse does not attest the identity of every configured issuer. The live report scope remains NVDA.

The live run exposed an obsolete revenue concept taking precedence over a newer series. Revenue selection now uses the newest annual period among configured concepts, with configured priority on ties; current and prior are taken from the same concept. Regression tests cover newer-series selection, missing comparatives, identity rejection and tie priority. Other roles keep their existing semantic priorities. Cross-period net margin and free cash flow remain unavailable rather than combining incompatible years.

The private report is not committed. Missing core evidence must produce `insufficient`, never a fabricated neutral or directional outlook.
