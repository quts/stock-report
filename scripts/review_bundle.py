"""Build an honest Phase 0 review bundle from actual recorded artifacts."""
from pathlib import Path
from datetime import datetime, timezone
from shutil import copyfile
from stock_report.evaluation import load_evaluation
from stock_report.analysis import synthetic_proposal
from stock_report.mock import fixture
from stock_report.models import Request
from stock_report.policy import evaluate
from stock_report.quant import calculate
from stock_report.storage import read_json,write_json

output=Path('reports/phase-0')
output.mkdir(parents=True,exist_ok=True)
request=Request.model_validate(read_json('examples/stock-nvda.json'))
golden=read_json('tests/golden_cases.json')
actual=[]
for scenario,expected in golden['expected'].items():
    data=fixture(request)
    proposal=synthetic_proposal(data)
    first=proposal.outlooks[0]
    target=next(e for e in data.evidence if e.evidence_id==first.supporting_evidence_ids[0])
    if scenario=='unknown_evidence':first.supporting_evidence_ids.append('unknown')
    elif scenario=='future_data':target.published_at=request.research_as_of.replace(year=2027)
    elif scenario=='unverified_event':next(e for e in data.evidence if e.source_record_id=='risk').verification_status='unverified'
    elif scenario=='license_unknown':target.license_status='unknown'
    elif scenario=='quant_mismatch':data.metrics[0].value=999
    elif scenario=='partial_financials':
        data.evidence=[e for e in data.evidence if e.source_record_id!='eps_current']
        data.metrics=calculate(data.evidence,request.research_as_of)
    elif scenario=='core_missing':first.supporting_evidence_ids=[]
    elif scenario=='unresolved_conflict':first.unresolved_conflicts=['未解決的重大衝突']
    elif scenario=='wrong_entity':target.entity_id='AMD'
    decision=evaluate(data,proposal)
    value=decision.outlooks[0].outlook
    actual.append({'scenario':scenario,'expected':expected,'actual':value,'passed':value==expected,
                   'rules':[f.rule_id for f in decision.findings]})
    case=output/'policy-gate-tests'/scenario
    case.mkdir(parents=True,exist_ok=True)
    write_json(case/'research_input.json',data)
    write_json(case/'proposal.json',proposal)
    write_json(case/'decision.json',decision)
write_json(output/'policy-gate-tests'/'results.json',{'dataset_kind':'mock','cases':actual})
if not all(r['passed'] for r in actual):raise SystemExit('Golden fault injection failed')
index=read_json('reports/demo/index.json')
for name,label in [('stock-nvda','stock'),('event','event')]:
    row=next(r for r in index if r.get('example')==name)
    copyfile(row['report_path'],output/f'sample-{label}.pdf')
write_json(output/'research-manifest.json',{'generated_at':datetime.now(timezone.utc).isoformat(),
    'samples':'DEMO / SYNTHETIC DATA only', 'runs':[read_json(Path(r['report_path']).parent/'manifest.json') for r in index],
    'replay_verified':any('replay_of' in r for r in index), 'test_results':'provider-increment-junit.xml', 'network_test_results':'network-junit.xml',
    'real_data_probe':'provider-probes.json' if (output/'provider-probes.json').exists() else 'legacy probe archived privately',
    'source_binding':'taiwan-source-binding.json', 'interactive_evaluation':'llm-evaluation.json',
    'real_source_smoke':'source-smoke.json' if (output/'source-smoke.json').exists() else 'NOT_RUN',
    'license_registry':read_json('config/source-licenses.json')['registry_version'],
    'real_research_e2e':'NOT_PASSED'})
coverage={}
for row in index:
    if 'example' in row:coverage.update(row['coverage'])
probe=read_json(output/'provider-probes.json') if (output/'provider-probes.json').exists() else {'results':[]}
smoke=read_json(output/'source-smoke.json') if (output/'source-smoke.json').exists() else {'results':[]}
registry=read_json('config/source-licenses.json')
write_json(output/'data-coverage.json',{'mock_coverage':coverage,
    'real_coverage':{r['provider']:{'access':r['status'],'license_status':r['license_status'],
      'shareable_in_pdf':r['shareable_in_pdf'],'field_coverage':'NOT_VERIFIED; probe is not research E2E'} for r in probe['results']},
    'collected_sources':[{'source_id':r['source_id'],'ticker':r['ticker'],'status':r['status'],
      'roles':sorted({o['role'] for o in r.get('observations',[]) if o['role']}),
      'publication_basis':sorted({o['publication_basis'] for o in r.get('observations',[])}),
      'license_status':r.get('license_status','unknown')} for r in smoke['results']],
    'license_registry':{'version':registry['registry_version'],
      'statuses':{s['source_id']:s['declared_status'] for s in registry['sources']},
      'shareable_sources':[s['source_id'] for s in registry['sources'] if s['scope'].get('pdf_share_allowed')]},
    'denominator':'No fabricated aggregate coverage percentage; each named field/provider is reported separately'})
write_json(output/'llm-evaluation.json',load_evaluation(
    Path('reports/codex-evaluation/llm-evaluation.json'),
    Path('docs/phase-0/codex-interactive-evaluation.json'),Path('reports')))
for name in ('provider-feasibility.md','phase-0-review.md','codex-interactive-evaluation.md',
             'codex-interactive-evaluation.json','taiwan-license-review-evidence.md','taiwan-source-binding.json'):
    source=Path('docs/phase-0')/name
    if source.exists():copyfile(source,output/name)
print(output.resolve())
