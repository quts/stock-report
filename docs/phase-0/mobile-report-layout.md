# Mobile reading edition

The user requested larger, phone-readable PDF text on 2026-11-17, superseding the earlier fixed-layout/page-count preference for this companion edition.

Run `scripts/runtime.sh mobile-report reports/COMPANY/RUN` against a finalized investment run. The command verifies the original manifest hashes, renders the saved approved analysis into `report-mobile.pdf`, and writes `mobile-manifest.json`. Research inputs, decisions, forecasts, scenarios, source permissions and the standard PDF remain byte-identical.

The 135 × 240 mm portrait page uses 14 pt body text, 13 pt record values and 12.5 pt notes. Original body/notes were 9.6/7.6 pt. Wide tables become vertical records with labels beside values, charts become single-column, and long explanations wrap. All table cell contents and citations survive the reflow. The mobile edition permits additional pages rather than shrinking content to the standard report budget; no research gate is changed. The current complete TSMC and DELL editions each have 43 smaller pages.

The source PDF's numerical tokens are checked against the mobile PDF with layout-aware extraction, including exact partitions of adjacent table cells that the old PDF concatenates. Source hashes are rechecked after rendering. This supplements visual inspection; it does not replace reading the actual layout. Existing `scripts/runtime.sh qa --report .../report-mobile.pdf --output .../mobile-qa` renders every page for inspection. Standard rendering keeps its existing page budget and defaults.
