"""Fixed CLI arguments; never execute source text or invoke a shell."""
import argparse
import json
from pathlib import Path
import sys
from .analysis import exchange, synthetic_proposal
from .models import Request
from .pipeline import prepare, load_prepared, finalize, replay
from .providers import probes
from .storage import read_json, write_json


class JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


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
        elif args.command == 'probe':
            result = probes(args.output, args.user_agent, args.retries)
        else:
            args.output.mkdir(parents=True, exist_ok=True)
            from .models import Evidence, ResearchInput, Proposal
            for model in (Request, Evidence, ResearchInput, Proposal):
                write_json(args.output / f'{model.__name__}.schema.json', model.model_json_schema())
            result = {'status': 'success', 'schema_path': str(args.output.resolve())}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        # Do not echo exception text from providers, source contents or credentials.
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__,
                          'message': str(exc)[:600] if isinstance(exc, (ValueError, FileNotFoundError)) else 'Operation failed; no report completion claimed'}, ensure_ascii=False))
        return 1

if __name__ == '__main__':
    sys.exit(main())
