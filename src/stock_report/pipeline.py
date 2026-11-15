"""Run isolation, immutable snapshots, policy execution and audited replay."""
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
import hashlib
import uuid
from . import __version__
from .analysis import exchange
from .models import Request, ResearchInput, Proposal, event_review_state, research_scope, financial_review_state
from .mock import fixture, VERSIONS
from .policy import evaluate, reportable, validate_company_research, validate_thesis
from . import engine
from .quant import calculate, consensus
from .render import render
from .storage import digest, read_json, write_json, save_evidence

# Every output is for the operator's personal use; there is no sharing profile or feature.
DISTRIBUTION = 'personal_use_only'


def byte_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def runtime_versions(mapping, data=None):
    # The dataset label follows the snapshot, so a real run is never stamped as synthetic.
    dataset = data.versions.get('dataset', VERSIONS['dataset']) if data is not None else VERSIONS['dataset']
    return {**VERSIONS, 'dataset': dataset, 'package': __version__, 'ratings': digest(mapping),
            'template_sha256': digest({p.name: byte_hash(p) for p in Path(str(files('stock_report').joinpath('templates'))).glob('*.html')}),
            'code_sha256': digest({p.name: byte_hash(p) for p in Path(str(files('stock_report'))).glob('*.py')})}


def coverage(data):
    result = {}
    for entity in data.entities:
        fields = {}
        for kind in ('identity', 'financial', 'price', 'event', 'rating', 'relationship'):
            rows = [e for e in data.evidence if e.entity_id == entity.entity_id and e.kind == kind]
            fields[kind] = 'unavailable' if not rows else 'available' if all(reportable(e, data.request) for e in rows) else 'partial' if any(reportable(e, data.request) for e in rows) else 'license_unknown' if any(e.license_status == 'unknown' for e in rows) else 'unavailable'
        result[entity.entity_id] = fields
    return result


SNAPSHOT_NOTICE = ('僅為財務快照：近期重大事件查核未完成，不是完整個股研究；'
                   '不得回報為研究完成，也不得解讀為沒有重大事件')


def completion(data):
    """Explicit, machine-readable answer to "is this complete stock research?" for every delivery."""
    state = event_review_state(data) + financial_review_state(data)
    if data.request.mode == 'stock' and data.dataset_kind == 'real':
        if data.earnings_preview is None or data.earnings_preview.status == 'not_run':
            state.append('EARNINGS_PREVIEW_SEARCH_MISSING')
    review = data.event_review
    result = {'research_scope': research_scope(data), 'completion_blockers': state,
              'event_review': None if review is None else {
                  'status': review.status, 'reviewed_by': review.reviewed_by,
                  'window_start': review.window_start.isoformat() if review.window_start else None,
                  'window_end': review.window_end.isoformat() if review.window_end else None,
                  'verified_events': [e.event_id for e in review.events],
                  'unresolved_leads': [lead.lead_id for lead in review.unresolved_leads]}}
    if data.request.mode == 'stock':
        result['research_complete'] = not state
        result['event_review_status'] = review.status if review is not None else 'not_run'
        result['latest_financial_report_status'] = financial_review_state(data) or ['covered']
        result['latest_financials'] = data.latest_financials.model_dump(mode='json') if data.latest_financials else None
        result['earnings_preview'] = data.earnings_preview.model_dump(mode='json') if data.earnings_preview else None
    return result


def completion_warnings(data):
    scope = research_scope(data)
    notes = [SNAPSHOT_NOTICE] if scope == 'financial_snapshot' else []
    if data.event_review is not None and data.event_review.unresolved_leads:
        notes.append('存在未解決的重大事件線索；各期限展望維持資料不足')
    return notes


def prepare(request, root, source=None, synthetic_source=False):
    if synthetic_source and source is None:
        raise ValueError('A synthetic event run requires the bundle written by event-rank')
    data = ResearchInput.model_validate(source) if source is not None else fixture(request)
    if data.request != request:
        raise ValueError('Imported request does not match the validated request')
    if synthetic_source:
        # The explicit synthetic path accepts authored records only, and stays labelled as a demo.
        if (data.dataset_kind != 'mock' or request.mode != 'event'
                or any(e.source_type != 'synthetic' for e in data.evidence)):
            raise ValueError('The synthetic event provider only accepts a mock event bundle of synthetic evidence; '
                             'real evidence must use --provider import')
    elif source is not None and data.dataset_kind != 'real':
        raise ValueError('Import is reserved for explicitly reviewed real evidence bundles')
    # Personal use is not a permission: local persistence still needs confirmed internal-use terms.
    if any(not e.internal_use_allowed for e in data.evidence):
        raise ValueError('Do not persist source contents without confirmed internal-use permission')
    if any(e.license_status != 'confirmed' for e in data.evidence):
        raise ValueError('Internal-use permission cannot be claimed for a source whose terms are unknown or restricted')
    # Recompute imported quantities; never trust provider-supplied calculations.
    data.metrics = calculate(data.evidence, request.research_as_of)
    # Pin the executing code versions, but never relabel a real import as synthetic data.
    dataset = (data.versions.get('dataset', 'real-import-1') if data.dataset_kind == 'real'
               else data.versions.get('dataset', VERSIONS['dataset']) if synthetic_source else VERSIONS['dataset'])
    data.versions = {**VERSIONS, 'dataset': dataset}
    run_id = uuid.uuid4().hex
    directory = Path(root).resolve() / run_id
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / 'research_input.json', data)
    write_json(directory / 'evidence.json', data.evidence and [e.model_dump(mode='json') for e in data.evidence] or [])
    save_evidence(directory / 'evidence.sqlite', data.evidence)
    exchange(data, directory)
    write_json(directory / 'prepared.json', {'run_id': run_id, 'input_sha256': digest(data), 'status': 'prepared'})
    return {'run_id': run_id, 'status': 'prepared', 'research_input_path': str(directory / 'research_input.json'),
            'research_as_of': request.research_as_of.isoformat(), 'coverage': coverage(data), **completion(data),
            'warnings': (['DEMO / SYNTHETIC DATA'] if data.dataset_kind == 'mock' else ['Real evidence import requires a recorded source/licence review (human attestation or qualified assistant review)'])
                        + completion_warnings(data)}


def load_prepared(directory):
    directory = Path(directory).resolve()
    data = ResearchInput.model_validate(read_json(directory / 'research_input.json'))
    prepared = read_json(directory / 'prepared.json')
    if prepared['input_sha256'] != digest(data):
        raise ValueError('Prepared snapshot changed; create a new run')
    return directory, data, prepared


def finalize(directory, proposal_path, mapping_path):
    directory, data, prepared = load_prepared(directory)
    if (directory / 'manifest.json').exists():
        raise ValueError('Run already finalized; use replay to write a new isolated run')
    proposal = Proposal.model_validate(read_json(proposal_path))
    if data.dataset_kind == 'real' and data.request.mode == 'stock' and data.request.max_pdf_pages >= 4:
        if financial_review_state(data):
            raise ValueError('The latest published financial report is required for a four-page stock report')
        if data.earnings_preview is None or data.earnings_preview.status == 'not_run':
            raise ValueError('Next-earnings news and expectations must be searched before publication')
        if not proposal.company_research:
            raise ValueError('Four-page stock research requires substantive cited company analysis')
    mapping = read_json(mapping_path)
    if any(v not in ('Buy', 'Hold', 'Sell') for table in [mapping.get('default', {})] + list(mapping.get('institutions', {}).values()) for v in table.values()):
        raise ValueError('Invalid rating mapping')
    if proposal.input_sha256 != digest(data):
        raise ValueError('Proposal input hash does not match prepared snapshot')
    if data.dataset_kind == 'real' and proposal.model.startswith('synthetic'):
        raise ValueError('Synthetic proposal cannot be applied to real data')
    decision = evaluate(data, proposal)
    company_research = validate_company_research(data, proposal)
    validate_thesis(data, proposal)
    # One deterministic research pass; its quality gates decide whether the report is complete.
    investment = engine.run(data, proposal, decision)
    if investment is not None:
        write_json(directory / 'investment.json', engine.public(investment))
    # File output is transactional: the personal PDF is written only after all checks pass.
    temp_pdf = directory / '.report.pending.pdf'
    try:
        pages = render(data, decision, mapping, temp_pdf, company_research=company_research, investment=investment)
    except Exception:
        temp_pdf.unlink(missing_ok=True)
        raise
    report_path = directory / 'report.pdf'
    temp_pdf.replace(report_path)
    report = {'run_id': prepared['run_id'], 'dataset_kind': data.dataset_kind,
              'request': data.request.model_dump(mode='json'), 'coverage': coverage(data),
              'metrics': [m.model_dump(mode='json') for m in calculate(data.evidence, data.request.research_as_of)],
              'approved_outlooks': [p.model_dump(mode='json') for p in decision.outlooks],
              'consensus': {e.entity_id: consensus(data.evidence, e.entity_id, data.request.research_as_of, mapping) for e in data.entities},
              'distribution': DISTRIBUTION, **completion(data),
              'privacy': 'Personal use only. Local audit JSON; never distributed'}
    report['company_research'] = [section.model_dump(mode='json') for section in company_research]
    if investment is not None:
        gates = investment['gates']
        report['report_grade'] = gates['grade']
        report['quality_gates'] = gates['checks']
        report['data_completeness'] = investment['completeness']
        report['valuation'] = engine.jsonable(investment['valuation'])
        report['scenarios'] = engine.jsonable(investment['scenarios'])
        report['thesis'] = [t.model_dump(mode='json') for t in proposal.thesis]
    write_json(directory / 'research.json', report)
    write_json(directory / 'decision.json', decision)
    write_json(directory / 'ai_proposal.json', proposal)
    write_json(directory / 'rating_mapping.json', mapping)
    artifacts = ['research_input.json', 'ai_proposal.json', 'rating_mapping.json', 'evidence.json', 'evidence.sqlite', 'decision.json', 'research.json', 'report.pdf']
    for optional in ('statement_metrics.json', 'investment.json', 'investment_inputs.json'):
        if (directory / optional).exists():
            artifacts.append(optional)
    manifest = {'run_id': prepared['run_id'], 'created_at': datetime.now(timezone.utc).isoformat(),
                'research_as_of': data.request.research_as_of.isoformat(), 'dataset_kind': data.dataset_kind,
                'versions': runtime_versions(mapping, data), 'proposal_model': proposal.model, 'usage': proposal.usage,
                'input_sha256': digest(data), 'decision_sha256': digest(decision), 'pdf_pages': pages,
                # Nothing is cleared for redistribution. `shareable_files` is a legacy key kept empty.
                'distribution': DISTRIBUTION, 'personal_report_files': ['report.pdf'], **completion(data),
                'shareable_files': [], 'audit_files_private': True,
                'hashes': {name: byte_hash(directory / name) for name in artifacts}}
    write_json(directory / 'manifest.json', manifest)
    warnings = completion_warnings(data) + sorted({f.message for f in decision.findings})
    if investment is not None and investment['gates']['grade'] == 'limited':
        warnings.insert(0, '受限報告：品質檢查未通過 ' + '、'.join(investment['gates']['critical_failures']))
    if data.dataset_kind == 'mock':
        warnings.insert(0, 'DEMO / SYNTHETIC DATA')
    return {'run_id': prepared['run_id'], 'status': 'partial' if warnings else 'success',
            'report_grade': investment['gates']['grade'] if investment else None,
            'gate_status': decision.status, 'distribution': DISTRIBUTION, **completion(data), 'report_path': str(report_path), 'json_path': str(directory / 'research.json'),
            'research_as_of': data.request.research_as_of.isoformat(), 'coverage': coverage(data), 'warnings': warnings}


def replay(directory, root):
    directory, data, _ = load_prepared(directory)
    manifest = read_json(directory / 'manifest.json')
    mapping = read_json(directory / 'rating_mapping.json')
    if manifest['versions'] != runtime_versions(mapping, data):
        raise ValueError('Replay requires the same code/policy/template/rating versions')
    for name, expected in manifest['hashes'].items():
        if Path(name).name != name or byte_hash(directory / name) != expected:
            raise ValueError('Replay artifact integrity check failed')
    target = Path(root).resolve() / uuid.uuid4().hex
    target.mkdir(parents=True, exist_ok=False)
    write_json(target / 'research_input.json', data)
    write_json(target / 'evidence.json', [e.model_dump(mode='json') for e in data.evidence])
    save_evidence(target / 'evidence.sqlite', data.evidence)
    write_json(target / 'prepared.json', {'run_id': target.name, 'input_sha256': digest(data), 'status': 'prepared'})
    exchange(data, target)
    result = finalize(target, directory / 'ai_proposal.json', directory / 'rating_mapping.json')
    new_manifest = read_json(target / 'manifest.json')
    if new_manifest['decision_sha256'] != manifest['decision_sha256']:
        raise ValueError('Replay decision mismatch')
    new_manifest['replay_of'] = manifest['run_id']
    write_json(target / 'manifest.json', new_manifest)
    result['replay_of'] = manifest['run_id']
    return result
