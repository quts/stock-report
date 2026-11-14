from datetime import timedelta
import pytest
from stock_report.policy import evaluate
from stock_report.quant import calculate
from stock_report.mock import fixture
from stock_report.analysis import synthetic_proposal


def decision(data,proposal):
    return evaluate(data,proposal)

def test_independent_horizons_and_missing_ratings(data,proposal):
    result=decision(data,proposal)
    assert result.status=='APPROVED_WITH_WARNINGS'
    assert [p.outlook for p in result.outlooks]==['bullish','neutral']
    assert 'RATINGS_MISSING' in {f.rule_id for f in result.findings}

@pytest.mark.parametrize('fault,rule',[
    ('unknown','UNKNOWN_EVIDENCE'),('future','FUTURE_DATA'),('unverified','UNVERIFIED'),
    ('conflicting','UNVERIFIED'),('license','LICENSE'),('restricted','LICENSE'),
    ('wrong_entity','WRONG_ENTITY'),('quant','QUANT_MISMATCH'),
    ('core','CORE_INSUFFICIENT'),('conflict','UNRESOLVED_CONFLICT')])
def test_fault_injection_does_not_release_direction(data,proposal,fault,rule):
    first=proposal.outlooks[0]
    target=next(e for e in data.evidence if e.evidence_id==first.supporting_evidence_ids[0])
    if fault=='unknown':first.supporting_evidence_ids.append('not-real')
    elif fault=='future':target.published_at=data.request.research_as_of+timedelta(seconds=1)
    elif fault in ('unverified','conflicting'):target.verification_status='unverified' if fault=='unverified' else 'conflicting'
    elif fault=='license':target.license_status='unknown'
    elif fault=='restricted':target.shareable_in_pdf=False
    elif fault=='wrong_entity':target.entity_id='AMD'
    elif fault=='quant':data.metrics[0].value=999
    elif fault=='core':first.supporting_evidence_ids=[]
    elif fault=='conflict':first.unresolved_conflicts=['量化與事件方向衝突，尚未解決']
    result=decision(data,proposal)
    assert result.outlooks[0].outlook=='insufficient'
    assert any(f.rule_id==rule and f.horizon=='3-6m' for f in result.findings)
    assert not result.outlooks[0].supporting_evidence_ids
    assert '合成案例' not in result.outlooks[0].interpretation

def test_bad_id_only_affects_one_horizon(data,proposal):
    proposal.outlooks[0].supporting_evidence_ids.append('fake')
    result=decision(data,proposal)
    assert [p.outlook for p in result.outlooks]==['insufficient','neutral']

def test_missing_metrics_is_soft(data,proposal):
    data.evidence=[e for e in data.evidence if e.source_record_id!='eps_current']
    data.metrics=calculate(data.evidence,data.request.research_as_of)
    result=decision(data,proposal)
    assert result.outlooks[0].outlook=='bullish'
    assert any(f.rule_id=='METRICS_MISSING' for f in result.findings)

def test_missing_proposal_pair_rejected(data,proposal):
    proposal.outlooks.pop()
    with pytest.raises(ValueError):decision(data,proposal)

def test_duplicate_pair_rejected(data,proposal):
    proposal.outlooks[1]=proposal.outlooks[0]
    with pytest.raises(ValueError):decision(data,proposal)

def test_event_hypothesis_and_fake_event(request_model):
    request=request_model.model_copy(update={'mode':'event','ticker':None,'exchange':None,'event_query':'合成事件'})
    data=fixture(request);proposal=synthetic_proposal(data)
    result=decision(data,proposal)
    assert all(p.outlook=='insufficient' for p in result.outlooks if p.entity_id=='6488.TWO')
    assert any(p.outlook=='bullish' for p in result.outlooks if p.entity_id=='NVDA')
    data.evidence[-1].verification_status='unverified'
    result=decision(data,proposal)
    assert all(p.outlook=='insufficient' for p in result.outlooks)
    assert any(f.rule_id=='EVENT_UNVERIFIED' for f in result.findings)


def test_quant_ai_conflict_requires_explicit_resolution(data,proposal):
    next(e for e in data.evidence if e.source_record_id=='revenue_current').value=800
    data.metrics=calculate(data.evidence,data.request.research_as_of)
    result=decision(data,proposal)
    assert result.outlooks[0].outlook=='insufficient'
    assert any(f.rule_id=='QUANT_AI_UNRESOLVED' for f in result.findings)
    proposal.outlooks[0].conflict_resolution='營收下滑為反證；合成事件支持復甦的假設，仍須檢驗。'
    result=decision(data,proposal)
    assert result.outlooks[0].outlook=='bullish'
    assert any(f.rule_id=='QUANT_AI_DIVERGENCE' for f in result.findings)


def test_event_quant_corruption_scoped_to_company(request_model):
    request=request_model.model_copy(update={'mode':'event','ticker':None,'exchange':None,'event_query':'合成事件'})
    data=fixture(request);proposal=synthetic_proposal(data)
    next(m for m in data.metrics if m.entity_id=='AMD').value=999
    result=decision(data,proposal)
    assert next(p for p in result.outlooks if p.entity_id=='AMD').outlook=='insufficient'
    assert next(p for p in result.outlooks if p.entity_id=='NVDA').outlook=='bullish'
