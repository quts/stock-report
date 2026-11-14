"""Run isolation, immutable snapshots, policy execution and audited replay."""
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
import hashlib
import uuid
from . import __version__
from .analysis import exchange
from .models import Request, ResearchInput, Proposal
from .mock import fixture, VERSIONS
from .policy import evaluate, publishable
from .quant import calculate, consensus
from .render import render
from .storage import digest, read_json, write_json, save_evidence


def byte_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def runtime_versions(mapping, data=None):
    # The dataset label follows the snapshot, so a real run is never stamped as synthetic.
    dataset = data.versions.get('dataset', VERSIONS['dataset']) if data is not None else VERSIONS['dataset']
    return {**VERSIONS, 'dataset': dataset, 'package': __version__, 'ratings': digest(mapping),
            'template_sha256': byte_hash(files('stock_report').joinpath('templates/report.html')),
            'code_sha256': digest({p.name: byte_hash(p) for p in Path(str(files('stock_report'))).glob('*.py')})}


def coverage(data):
    result = {}
    for entity in data.entities:
        fields = {}
        for kind in ('identity', 'financial', 'price', 'event', 'rating', 'relationship'):
            rows = [e for e in data.evidence if e.entity_id == entity.entity_id and e.kind == kind]
            fields[kind] = 'unavailable' if not rows else 'available' if all(publishable(e, data.request) for e in rows) else 'partial' if any(publishable(e, data.request) for e in rows) else 'license_unknown' if any(e.license_status == 'unknown' for e in rows) else 'unavailable'
        result[entity.entity_id] = fields
    return result


def prepare(request, root, source=None):
    data = ResearchInput.model_validate(source) if source is not None else fixture(request)
    if data.request != request:
        raise ValueError('Imported request does not match the validated request')
    if source is not None and data.dataset_kind != 'real':
        raise ValueError('Import is reserved for explicitly reviewed real evidence bundles')
    if any(not e.internal_use_allowed for e in data.evidence):
        raise ValueError('Do not persist source contents without confirmed internal-use permission')
    # Recompute imported quantities; never trust provider-supplied calculations.
    data.metrics = calculate(data.evidence, request.research_as_of)
    # Pin the executing code versions, but never relabel a real import as synthetic data.
    dataset = data.versions.get('dataset', 'real-import-1') if data.dataset_kind == 'real' else VERSIONS['dataset']
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
            'research_as_of': request.research_as_of.isoformat(), 'coverage': coverage(data),
            'warnings': ['DEMO / SYNTHETIC DATA'] if data.dataset_kind == 'mock' else ['Real evidence import requires human source/license verification']}


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
    mapping = read_json(mapping_path)
    if any(v not in ('Buy', 'Hold', 'Sell') for table in [mapping.get('default', {})] + list(mapping.get('institutions', {}).values()) for v in table.values()):
        raise ValueError('Invalid rating mapping')
    if proposal.input_sha256 != digest(data):
        raise ValueError('Proposal input hash does not match prepared snapshot')
    if data.dataset_kind == 'real' and proposal.model.startswith('synthetic'):
        raise ValueError('Synthetic proposal cannot be applied to real data')
    decision = evaluate(data, proposal)
    # File output is transactional: a PDF is published only after all checks pass.
    temp_pdf = directory / '.report.pending.pdf'
    try:
        pages = render(data, decision, mapping, temp_pdf)
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
              'privacy': 'Local audit JSON; not automatically cleared for redistribution'}
    write_json(directory / 'research.json', report)
    write_json(directory / 'decision.json', decision)
    write_json(directory / 'ai_proposal.json', proposal)
    write_json(directory / 'rating_mapping.json', mapping)
    artifacts = ['research_input.json', 'ai_proposal.json', 'rating_mapping.json', 'evidence.json', 'evidence.sqlite', 'decision.json', 'research.json', 'report.pdf']
    manifest = {'run_id': prepared['run_id'], 'created_at': datetime.now(timezone.utc).isoformat(),
                'research_as_of': data.request.research_as_of.isoformat(), 'dataset_kind': data.dataset_kind,
                'versions': runtime_versions(mapping, data), 'proposal_model': proposal.model, 'usage': proposal.usage,
                'input_sha256': digest(data), 'decision_sha256': digest(decision), 'pdf_pages': pages,
                'shareable_files': ['report.pdf'], 'audit_files_private': True,
                'hashes': {name: byte_hash(directory / name) for name in artifacts}}
    write_json(directory / 'manifest.json', manifest)
    warnings = sorted({f.message for f in decision.findings})
    if data.dataset_kind == 'mock':
        warnings.insert(0, 'DEMO / SYNTHETIC DATA')
    return {'run_id': prepared['run_id'], 'status': 'partial' if warnings else 'success',
            'gate_status': decision.status, 'report_path': str(report_path), 'json_path': str(directory / 'research.json'),
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
