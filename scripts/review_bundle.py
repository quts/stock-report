"""Build an honest Phase 0 review bundle from actual recorded artifacts."""
from pathlib import Path
from datetime import datetime, timezone
from shutil import copyfile
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
    'replay_verified':any('replay_of' in r for r in index), 'test_results':'junit.xml',
    'real_data_probe':'provider-probes.json', 'real_research_e2e':'NOT_PASSED'})
coverage={}
for row in index:
    if 'example' in row:coverage.update(row['coverage'])
probe=read_json(output/'provider-probes.json') if (output/'provider-probes.json').exists() else {'results':[]}
write_json(output/'data-coverage.json',{'mock_coverage':coverage,
    'real_coverage':{r['provider']:{'access':r['status'],'license_status':r['license_status'],
      'shareable_in_pdf':r['shareable_in_pdf'],'field_coverage':'NOT_VERIFIED; probe is not research E2E'} for r in probe['results']},
    'denominator':'No fabricated aggregate coverage percentage; each named field/provider is reported separately'})
llm_cases=['normal','conflicting_evidence','missing_financials','missing_valuation','missing_all_ratings',
    'unverified_event','unknown_evidence_id','future_data','license_unknown','prompt_injection']
write_json(output/'llm-evaluation.json',{'provider':'Codex interactive subscription', 'status':'NOT_RUN',
    'cases':[{'id':s,'status':'NOT_RUN','reason':'Deterministic fixtures do not establish LLM performance'} for s in llm_cases],
    'claude_implementation_assistant':'Stopped at account 5-hour usage limit; not the research LLM evaluation',
    'paid_api_used':False,'usage':None,'elapsed_seconds':None,'human_intervention_required':True})
for name in ('provider-feasibility.md','phase-0-review.md'):
    source=Path('docs/phase-0')/name
    if source.exists():copyfile(source,output/name)
print(output.resolve())
