"""Fixed CLI arguments; never execute source text or invoke a shell."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from .analysis import exchange, synthetic_proposal
from .collect import (SEC_CONTACT_ENV, build_bundle, collect, load_collection, persist, persistence_blockers,
                      request_template)
from .licensing import load_registry
from .models import Request
from .pipeline import prepare, load_prepared, finalize, replay
from .providers import probes
from .sources import PROFILES
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
    prep.add_argument('--provider', choices=['mock', 'import'], required=True)
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
    col.add_argument('--source', required=True, choices=sorted(PROFILES))
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
    schema = sub.add_parser('schema')
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
            request = Request.model_validate(read_json(args.request))
            result = prepare(request, args.output, read_json(args.evidence_bundle) if args.evidence_bundle else None)
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
                      'warnings': ['採集本身不建立任何分享或留存權限'] + collection.notes}
        elif args.command == 'bundle':
            request = Request.model_validate(read_json(args.request))
            collections = [load_collection(path) for path in args.collection]
            data, report = build_bundle(request, collections, registry=load_registry(args.license_registry),
                                        allow_fixture_capture=args.allow_fixture_capture)
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
        elif args.command == 'probe':
            result = probes(args.output, args.user_agent, args.retries)
        else:
            args.output.mkdir(parents=True, exist_ok=True)
            from .models import Evidence, ResearchInput, Proposal
            for model in (Request, Evidence, ResearchInput, Proposal):
                write_json(args.output / f'{model.__name__}.schema.json', model.model_json_schema())
            result = {'status': 'success', 'schema_path': str(args.output.resolve())}
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get('status') == 'blocked' else 0
    except Exception as exc:
        # Do not echo exception text from providers, source contents or credentials.
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__,
                          'message': str(exc)[:600] if isinstance(exc, (ValueError, FileNotFoundError)) else 'Operation failed; no report completion claimed'}, ensure_ascii=False))
        return 1

if __name__ == '__main__':
    sys.exit(main())
