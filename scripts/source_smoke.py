"""Record an explicit official-source smoke run.

Opt-in only: `./scripts/runtime.sh smoke --allow-network`. Writes
`reports/phase-0/source-smoke.json` with the observed field binding, record
identity, publication basis and timing for each source, plus every failure as it
happened. No source contents beyond the normalized target fields are stored, and
no licensing conclusion is derived from a successful request.
"""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import time
from contract_probe import cumulative_basis
from stock_report.collect import SEC_CONTACT_ENV, collect, persist, persistence_blockers
from stock_report.licensing import load_registry
from stock_report.storage import write_json

TARGETS = [('twse-openapi-stock-day-all', '2330.TW'), ('tpex-openapi-mainboard-daily-close', '6488.TWO'),
           ('twse-openapi-income-general', '2330.TW'), ('tpex-openapi-income-general', '6488.TWO'),
           ('sec-xbrl-companyconcept', 'NVDA'), ('sec-xbrl-companyconcept', 'AMD')]
CUMULATIVE_CHECKS = {'twse-openapi-income-general': 'twse', 'tpex-openapi-income-general': 'tpex'}

parser = argparse.ArgumentParser(prog='source_smoke')
parser.add_argument('--allow-network', action='store_true', required=True)
parser.add_argument('--output', type=Path, default=Path('reports/phase-0'))
args = parser.parse_args()

registry = load_registry()
collections = args.output / 'source-smoke'
results = []
for source_id, ticker in TARGETS:
    row = {'source_id': source_id, 'ticker': ticker, 'started_at': datetime.now(timezone.utc).isoformat()}
    started = time.monotonic()
    if source_id.startswith('sec') and not os.environ.get(SEC_CONTACT_ENV):
        row.update(status='skipped', reason=f'{SEC_CONTACT_ENV} not configured by the operator')
        results.append(row)
        continue
    try:
        collection = collect(source_id, ticker, registry=registry, allow_network=True)
        path, artifact = persist(collection, collections)
        blockers = persistence_blockers(collection)
        # Only contract metadata is recorded here; source values are never written for a source
        # whose internal-use permission is still unconfirmed.
        row.update(status='collected', artifact=artifact, artifact_path=str(path),
                   field_binding=collection.field_binding, coverage_mode=collection.coverage_mode,
                   collected_at=collection.collected_at.isoformat(),
                   entity_shape={'exchange': collection.entity.exchange, 'currency': collection.entity.currency,
                                 'share_class': collection.entity.share_class},
                   observations=[{'kind': o.kind, 'role': o.role, 'unit': o.unit,
                                  'publication_basis': o.publication_basis, 'period_basis': o.period_basis,
                                  'period_window_present': o.period_start is not None,
                                  'source_unit_declared': o.source_unit_raw is not None,
                                  'value_present': o.value is not None} for o in collection.observations],
                   transport=[{k: v for k, v in t.items() if k in ('key', 'status', 'attempts', 'cached')}
                              for t in collection.transport],
                   license_status=collection.license['status'], retention=collection.retention,
                   persistence_blockers=blockers,
                   license_registry_version=collection.license['registry_version'], notes=collection.notes)
        if source_id in CUMULATIVE_CHECKS:
            # The statement dataset does not declare that its figures are year-to-date. The
            # assumption is re-checked against the monthly revenue filing on every smoke run;
            # only the ratio is kept.
            try:
                row['cumulative_basis_check'] = cumulative_basis(CUMULATIVE_CHECKS[source_id], ticker.split('.')[0])
            except Exception as exc:
                row['cumulative_basis_check'] = {'conclusion': 'check_failed', 'error_type': type(exc).__name__}
    except Exception as exc:
        row.update(status='failed', error_type=type(exc).__name__, error=str(exc)[:300])
    row['elapsed_seconds'] = round(time.monotonic() - started, 3)
    results.append(row)

report = {'generated_at': datetime.now(timezone.utc).isoformat(), 'capture': 'network',
          'registry_version': registry.registry_version, 'results': results,
          'license_conclusion': 'NONE. Reaching an endpoint establishes no internal-use or retention right; personal use is not a permission.',
          'research_e2e': 'NOT_ESTABLISHED. A smoke collection is not a research run.'}
args.output.mkdir(parents=True, exist_ok=True)
write_json(args.output / 'source-smoke.json', report)
print((args.output / 'source-smoke.json').resolve())
if not any(r['status'] == 'collected' for r in results):
    raise SystemExit('No official source was collected; see source-smoke.json')
