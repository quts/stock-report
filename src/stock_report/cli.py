"""Fixed CLI arguments; never execute source text or invoke a shell."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from .activation import check as activation_check
from .analysis import exchange, synthetic_proposal
from .collect import (SEC_CONTACT_ENV, build_bundle, collect, load_collection, persist, persistence_blockers,
                      request_template)
from .events import EventDossier, load_universe, rank_dossier
from .discovery import discover
from .rss import discover_feed
from .diagnostics import doctor
from .licensing import load_registry
from .models import Request, ResearchInput
from .pipeline import prepare, load_prepared, finalize, replay
from .providers import probes
from .sources import PROFILES, FILING_PROFILES
from .reviewed_collect import REVIEWED_PROFILES
from .stock_events import StockEventDossier, merge_stock_events
from .storage import read_json, write_json


class JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def aware_datetime(text):
    if not text:
        return None
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise ValueError('A research cutoff must carry an explicit timezone offset')
    return value


def parser():
    p = JSONArgumentParser(prog='stock-report')
    sub = p.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare', help='Validate and prepare a snapshot')
    prep.add_argument('--request', required=True, type=Path)
    prep.add_argument('--provider', choices=['mock', 'import', 'synthetic-event'], required=True)
    prep.add_argument('--evidence-bundle', type=Path)
    prep.add_argument('--output', type=Path, default=Path('reports'))
    analyze = sub.add_parser('analyze', help='Export schema/prompt for Codex; no paid API')
    analyze.add_argument('--run', type=Path, required=True)
    analyze.add_argument('--synthetic', action='store_true', help='Generate an explicitly synthetic proposal for mock testing only')
    final = sub.add_parser('finalize', help='Validate proposal, apply gate and render')
    final.add_argument('--run', type=Path, required=True)
    final.add_argument('--proposal', type=Path, required=True)
    final.add_argument('--rating-map', type=Path, default=Path('config/ratings.json'))
    rep = sub.add_parser('replay', help='Verify snapshot and write a new isolated report')
    rep.add_argument('--run', required=True, type=Path)
    rep.add_argument('--output', type=Path, default=Path('reports'))
    col = sub.add_parser('collect', help='Collect official-source records before any research cutoff is applied')
    col.add_argument('--source', required=True, choices=sorted({**PROFILES, **FILING_PROFILES, **REVIEWED_PROFILES}))
    col.add_argument('--ticker', required=True)
    col.add_argument('--output', type=Path, default=Path('reports/collections'))
    col.add_argument('--allow-network', action='store_true', help='Required for real collection; no mock fallback')
    col.add_argument('--fixture', type=Path, help='Offline contract capture; never accepted as real evidence')
    col.add_argument('--license-registry', type=Path, default=None)
    col.add_argument('--source-profile', type=Path, help='JSON overrides for the recorded field contract')
    col.add_argument('--sec-contact', help=f'Prefer the {SEC_CONTACT_ENV} environment variable; never logged or stored')
    col.add_argument('--user-agent', default=None)
    col.add_argument('--retries', type=int, default=1)
    col.add_argument('--interval', type=float, default=1.0)
    bun = sub.add_parser('bundle', help='Apply the supplied research cutoff and emit a real evidence bundle')
    bun.add_argument('--request', required=True, type=Path)
    bun.add_argument('--collection', required=True, type=Path, action='append')
    bun.add_argument('--output', required=True, type=Path)
    bun.add_argument('--license-registry', type=Path, default=None)
    bun.add_argument('--allow-fixture-capture', action='store_true', help='Offline tests only')
    bun.add_argument('--earnings-preview', type=Path, default=None,
                     help='EarningsPreviewReview JSON recording the next-earnings search; cited IDs must be in the bundle')
    bun.add_argument('--operator-import', type=Path, action='append', default=None,
                     help='Normalized import file written by import-market-data (operator-attested data)')
    bun.add_argument('--data-gaps', type=Path, default=None,
                     help='JSON list of DataGap records: required fields searched for but not usable, with status and sources tried')
    imp = sub.add_parser('import-market-data', help='Validate operator-supplied prices/splits/consensus/peers CSV with a rights attestation')
    imp.add_argument('--kind', required=True, choices=['prices', 'splits', 'consensus', 'peers'])
    imp.add_argument('--csv', required=True, type=Path)
    imp.add_argument('--ticker', required=True)
    imp.add_argument('--exchange', required=True, choices=['TWSE', 'TPEx', 'NYSE', 'NASDAQ'])
    imp.add_argument('--attestation', required=True, type=Path,
                     help='JSON written by the operator: attested_by, attested_at, rights_statement, source_name, source_url')
    imp.add_argument('--output', required=True, type=Path)
    tpl = sub.add_parser('request-template', help='Write a request whose cutoff follows the collection time')
    tpl.add_argument('--mode', choices=['stock'], default='stock')
    tpl.add_argument('--ticker', required=True)
    tpl.add_argument('--exchange', choices=['NASDAQ', 'NYSE', 'TWSE', 'TPEx'])
    tpl.add_argument('--collection', required=True, type=Path, action='append')
    tpl.add_argument('--research-as-of')
    tpl.add_argument('--output', required=True, type=Path)
    probe = sub.add_parser('probe', help='Explicitly opt into official-source network probes')
    probe.add_argument('--allow-network', action='store_true', required=True)
    probe.add_argument('--user-agent', required=True)
    probe.add_argument('--output', type=Path, default=Path('reports/feasibility'))
    probe.add_argument('--retries', type=int, default=1)
    rank = sub.add_parser('event-rank', help='Verify an event dossier and rank its candidates deterministically')
    rank.add_argument('--dossier', required=True, type=Path)
    rank.add_argument('--output', required=True, type=Path)
    rank.add_argument('--universe', type=Path, default=None)
    rank.add_argument('--license-registry', type=Path, default=None)
    sev = sub.add_parser('stock-events', help='Merge a reviewed stock event dossier into a stock evidence bundle; no network')
    sev.add_argument('--bundle', required=True, type=Path, help='Existing stock evidence bundle without an event review')
    sev.add_argument('--dossier', required=True, type=Path)
    sev.add_argument('--output', required=True, type=Path)
    sev.add_argument('--license-registry', type=Path, default=None)
    news = sub.add_parser('news-discover', help='Bounded news-index discovery; mentions are unverified leads')
    news.add_argument('--request', required=True, type=Path)
    news.add_argument('--output', required=True, type=Path)
    news.add_argument('--allow-network', action='store_true')
    news.add_argument('--fixture', type=Path, help='Authored synthetic index fixture only')
    news.add_argument('--max-results', type=int, default=50)
    news.add_argument('--source', choices=['gdelt-doc-v2', 'nvidia-press-rss'], default='gdelt-doc-v2')
    act = sub.add_parser('activation-check', help='Read-only check of a source activation package')
    act.add_argument('--package', required=True, type=Path)
    act.add_argument('--candidate-registry', type=Path, help='Candidate registry (human-attested or assistant-reviewed) to compare; never applied')
    act.add_argument('--license-registry', type=Path, default=None)
    act.add_argument('--instruments', '--target-config', dest='instruments', type=Path, default=None,
                     help='Target configuration; defaults to config/tw-instruments.json for a Taiwan target '
                          'and config/sec-entities.json for a SEC target')
    cat = sub.add_parser('filing-catalog', help='Verify cited SEC accessions against reviewed SEC indexes; metadata only')
    cat.add_argument('--input', required=True, type=Path, help='Reviewed research_input.json whose cited accessions are verified')
    cat.add_argument('--output', required=True, type=Path, help='New research input carrying the catalog (input is not modified)')
    cat.add_argument('--catalog-output', required=True, type=Path, help='Normalized catalog audit file')
    cat.add_argument('--allow-network', action='store_true', help='Required; no fixture fallback')
    cat.add_argument('--license-registry', type=Path, default=None)
    cat.add_argument('--sec-contact', help=f'Prefer the {SEC_CONTACT_ENV} environment variable; never logged or stored')
    cat.add_argument('--retries', type=int, default=1)
    cat.add_argument('--interval', type=float, default=1.0)
    schema = sub.add_parser('schema')
    sub.add_parser('doctor', help='Read-only readiness and recorded validation status; no network')
    schema.add_argument('--output', required=True, type=Path)
    return p


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        if args.command == 'prepare':
            if args.provider == 'import' and not args.evidence_bundle:
                raise ValueError('Import requires --evidence-bundle; no fallback to mock')
            if args.provider == 'mock' and args.evidence_bundle:
                raise ValueError('Mock provider does not accept real evidence bundles')
            if args.provider == 'synthetic-event' and not args.evidence_bundle:
                raise ValueError('The synthetic event provider requires the --evidence-bundle written by event-rank')
            request = Request.model_validate(read_json(args.request))
            result = prepare(request, args.output, read_json(args.evidence_bundle) if args.evidence_bundle else None,
                             synthetic_source=args.provider == 'synthetic-event')
        elif args.command == 'analyze':
            directory, data, prepared = load_prepared(args.run)
            exchange(data, directory)
            result = {'run_id': prepared['run_id'], 'status': 'awaiting_proposal',
                      'schema_path': str(directory / 'proposal.schema.json'),
                      'instructions_path': str(directory / 'analysis-instructions.txt')}
            if args.synthetic:
                write_json(directory / 'ai_proposal.json', synthetic_proposal(data))
                result.update(status='synthetic_proposal_ready', proposal_path=str(directory / 'ai_proposal.json'))
        elif args.command == 'finalize':
            result = finalize(args.run, args.proposal, args.rating_map)
        elif args.command == 'replay':
            result = replay(args.run, args.output)
        elif args.command == 'collect':
            if args.fixture and args.allow_network:
                raise ValueError('Choose either a real network collection or an offline fixture capture')
            collection = collect(args.source, args.ticker, registry=load_registry(args.license_registry),
                                 allow_network=args.allow_network, fixture=args.fixture,
                                 sec_contact=args.sec_contact, user_agent=args.user_agent,
                                 profile_overrides=read_json(args.source_profile) if args.source_profile else None,
                                 retries=args.retries, interval=args.interval)
            path, artifact = persist(collection, args.output)
            result = {'status': 'collected' if artifact == 'collection' else 'inspection_only',
                      'collection_id': collection.collection_id, 'persisted': artifact,
                      'collection_path': str(path), 'source_id': collection.source_id,
                      'capture_mode': collection.capture_mode, 'coverage_mode': collection.coverage_mode,
                      'collected_at': collection.collected_at.isoformat(),
                      'entity': collection.entity.model_dump(mode='json') if artifact == 'collection' else
                                {'entity_id': collection.requested_ticker, 'exchange': collection.entity.exchange,
                                 'currency': collection.entity.currency, 'share_class': collection.entity.share_class},
                      'observations': len(collection.observations), 'field_binding': collection.field_binding,
                      'license_status': collection.license['status'],
                      'license_registry_version': collection.license['registry_version'],
                      'retention': collection.retention,
                      'research_as_of': 'not applied; supply the cutoff with request-template or bundle',
                      'persistence_blockers': persistence_blockers(collection),
                      'warnings': ['採集本身不建立任何內部使用或留存權限'] + collection.notes}
        elif args.command == 'filing-catalog':
            from . import filing_catalog
            from .collect import user_agent_for
            from .providers import HTTPClient
            if args.output.exists() or args.catalog_output.exists():
                raise ValueError('Catalog outputs must be new immutable files')
            if not args.allow_network:
                raise ValueError('Filing catalog verification requires --allow-network; no fixture fallback')
            if args.interval < 1.0:
                raise ValueError('SEC requests stay at or below one request per second')
            source = read_json(args.input)
            checked = ResearchInput.model_validate(source)
            if checked.dataset_kind != 'real' or checked.request.mode != 'stock':
                raise ValueError('Catalog verification applies to a real stock research input')
            client = HTTPClient(user_agent_for(filing_catalog.SUBMISSIONS, None, args.sec_contact),
                                retries=args.retries, interval=args.interval)
            catalog = filing_catalog.build(checked.request.ticker, source, registry=load_registry(args.license_registry),
                                           client=client, as_of=checked.request.research_as_of)
            from .models import FilingCatalog
            write_json(args.catalog_output, FilingCatalog.model_validate(catalog))
            updated = ResearchInput.model_validate(filing_catalog.attach(source, read_json(args.catalog_output)))
            write_json(args.output, updated)
            result = {'status': 'cataloged', 'entity_id': catalog['entity_id'], 'entries': len(catalog['entries']),
                      'cited_accessions': catalog['cited_accessions'],
                      'cited_accessions_cataloged': catalog['cited_accessions_cataloged'],
                      'uncataloged': len(catalog['cited_accessions_uncataloged']),
                      'header_lookups': {s: sum(h['status'] == s for h in catalog['header_lookups'])
                                         for s in sorted({h['status'] for h in catalog['header_lookups']})},
                      'catalog_path': str(args.catalog_output), 'research_input_path': str(args.output),
                      'retention': catalog['retention']}
        elif args.command == 'bundle':
            request = Request.model_validate(read_json(args.request))
            collections = [load_collection(path) for path in args.collection]
            data, report = build_bundle(request, collections, registry=load_registry(args.license_registry),
                                        allow_fixture_capture=args.allow_fixture_capture,
                                        earnings_preview=read_json(args.earnings_preview) if args.earnings_preview else None,
                                        data_gaps=read_json(args.data_gaps) if args.data_gaps else None,
                                        operator_imports=[read_json(p) for p in args.operator_import or []])
            args.output.parent.mkdir(parents=True, exist_ok=True)
            report_path = args.output.with_name(args.output.stem + '-report.json')
            write_json(report_path, report)
            result = {'status': report['status'], 'report_path': str(report_path),
                      'dataset_kind': report['dataset_kind'],
                      'research_as_of': report['request']['research_as_of'],
                      'core_source_records': report['core_source_records'],
                      'coverage_warnings': report['coverage_warnings'],
                      'excluded': [row['reason'] for row in report['exclusions']],
                      'blockers': report['blockers']}
            if data is not None:
                write_json(args.output, data)
                result['bundle_path'] = str(args.output)
        elif args.command == 'import-market-data':
            from .operator_import import read_csv
            imported = read_csv(args.kind, args.csv, args.ticker, args.exchange, read_json(args.attestation))
            write_json(args.output, imported)
            result = {'status': 'imported', 'kind': args.kind, 'records': len(imported.rows), 'output': str(args.output),
                      'attested_by': imported.attestation.attested_by}
        elif args.command == 'request-template':
            if args.output.exists():
                raise ValueError('Refusing to overwrite an existing request; a supplied cutoff is never rewritten')
            template = request_template(args.mode, args.ticker, args.exchange,
                                        [load_collection(path) for path in args.collection],
                                        aware_datetime(args.research_as_of))
            args.output.parent.mkdir(parents=True, exist_ok=True)
            write_json(args.output, template)
            result = {'status': 'success', 'request_path': str(args.output),
                      'research_as_of': template.research_as_of.isoformat()}
        elif args.command == 'news-discover':
            if args.fixture and args.allow_network:
                raise ValueError('Choose either an authored fixture or explicit network discovery')
            if args.output.exists():
                raise ValueError('Refusing to overwrite an existing discovery report')
            request = Request.model_validate(read_json(args.request))
            method = discover_feed if args.source == 'nvidia-press-rss' else discover
            result = method(request, allow_network=args.allow_network, fixture=args.fixture,
                              max_results=args.max_results)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            write_json(args.output, result)
            result = {**result, 'discovery_path': str(args.output.resolve())}
        elif args.command == 'event-rank':
            dossier = EventDossier.model_validate(read_json(args.dossier))
            # A real dossier is always checked against the versioned registry; a mock one never needs it.
            registry = load_registry(args.license_registry) if dossier.dossier_kind == 'real' else None
            data, report = rank_dossier(dossier, load_universe(args.universe), registry)
            directory = args.output / report['dossier_sha256'][:16]
            directory.mkdir(parents=True, exist_ok=True)
            write_json(directory / 'event-ranking.json', report)
            result = {'status': report['status'], 'dossier_kind': report['dossier_kind'],
                      'data_label': report['data_label'], 'ranking_path': str(directory / 'event-ranking.json'),
                      'event_verification': report['event']['verification']['level'],
                      'ranking': [{'rank': row['rank'], 'entity_id': row['entity_id'],
                                   'relationship': row['relationship'], 'selected': row['selected']}
                                  for row in report['ranking']],
                      'excluded': [{'entity_id': row['entity_id'], 'reason': row['reason']}
                                   for row in report['excluded_candidates']],
                      'research_as_of': report['request']['research_as_of'],
                      'blockers': report['blockers'], 'warnings': report['warnings']}
            for name in ('event-bundle.json', 'request.json'):
                (directory / name).unlink(missing_ok=True)
            if data is not None:
                write_json(directory / 'event-bundle.json', data)
                write_json(directory / 'request.json', data.request)
                result.update(bundle_path=str(directory / 'event-bundle.json'),
                              request_path=str(directory / 'request.json'),
                              next_provider='import' if data.dataset_kind == 'real' else 'synthetic-event')
        elif args.command == 'stock-events':
            if args.output.exists():
                raise ValueError('Refusing to overwrite an existing bundle; write the merged bundle to a new path')
            bundle = ResearchInput.model_validate(read_json(args.bundle))
            dossier = StockEventDossier.model_validate(read_json(args.dossier))
            registry = load_registry(args.license_registry) if dossier.dossier_kind == 'real' else None
            data, report = merge_stock_events(bundle, dossier, registry)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            report_path = args.output.with_name(args.output.stem + '-report.json')
            write_json(report_path, report)
            result = {key: report[key] for key in ('status', 'dossier_kind', 'event_review_status', 'verified_events',
                                                   'unresolved_leads', 'evidence_exclusions', 'blockers', 'warnings')}
            result.update(report_path=str(report_path), research_as_of=report['request']['research_as_of'])
            if data is not None:
                write_json(args.output, data)
                result.update(bundle_path=str(args.output),
                              next_provider='import' if data.dataset_kind == 'real' else None)
        elif args.command == 'activation-check':
            result = activation_check(args.package, load_registry(args.license_registry),
                                      args.instruments, args.candidate_registry)
        elif args.command == 'doctor':
            result = doctor()
        elif args.command == 'probe':
            result = probes(args.output, args.user_agent, args.retries)
        else:
            args.output.mkdir(parents=True, exist_ok=True)
            from .models import Evidence, Proposal
            for model in (Request, Evidence, ResearchInput, Proposal, StockEventDossier):
                write_json(args.output / f'{model.__name__}.schema.json', model.model_json_schema())
            result = {'status': 'success', 'schema_path': str(args.output.resolve())}
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get('status') in ('blocked', 'failed') else 0
    except Exception as exc:
        # Do not echo exception text from providers, source contents or credentials.
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__,
                          'message': str(exc)[:600] if isinstance(exc, (ValueError, FileNotFoundError)) else 'Operation failed; no report completion claimed'}, ensure_ascii=False))
        return 1

if __name__ == '__main__':
    sys.exit(main())
