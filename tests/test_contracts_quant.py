from datetime import timedelta
import math
import pytest
from pydantic import ValidationError
from stock_report.models import Request, ResearchInput
from stock_report.mock import fixture
from stock_report.quant import calculate, consensus

@pytest.mark.parametrize('change', [
    {'research_as_of': '2026-11-14T17:00:00'}, {'max_companies': 6},
    {'horizons': ['3-6m', '3-6m']}, {'event_query': 'bad combination'},
    {'language': 'en'}, {'max_pdf_pages': 3}, {'unexpected': True}])
def test_invalid_requests(request_model, change):
    payload = request_model.model_dump(mode='json')
    payload.update(change)
    with pytest.raises(ValidationError):
        Request.model_validate(payload)

@pytest.mark.parametrize('ticker,exchange,currency', [
    ('NVDA','NASDAQ','USD'), ('AMD','NASDAQ','USD'), ('2330.TW','TWSE','TWD'), ('6488.TWO','TPEx','TWD')])
def test_market_identity(request_model, ticker, exchange, currency):
    request = request_model.model_copy(update={'ticker': ticker, 'exchange': exchange})
    data = fixture(request)
    assert data.entities[0].exchange == exchange
    assert data.entities[0].currency == currency

def test_wrong_exchange_rejected(request_model):
    with pytest.raises(ValueError):
        fixture(request_model.model_copy(update={'exchange': 'TWSE'}))

def test_deterministic_formulas_and_missing(data):
    metrics = {m.name: m for m in data.metrics}
    assert metrics['revenue_growth'].value == 20
    assert metrics['net_margin'].value == 20
    assert metrics['free_cash_flow'].value == 220
    assert metrics['free_cash_flow'].unit == 'million'
    assert metrics['free_cash_flow'].currency == 'USD'
    assert metrics['pe'].value == 25
    assert metrics['eps_growth'].value is None
    assert metrics['eps_growth'].missing_reason
    assert calculate(data.evidence, data.request.research_as_of) == data.metrics

@pytest.mark.parametrize('change', [{'value': -4}, {'value': 0}, {'period': 'FY2025'}, {'currency': 'TWD'}])
def test_pe_invalid_denominator_is_not_applicable(data, change):
    for e in data.evidence:
        if e.source_record_id == 'eps_current':
            for k,v in change.items():setattr(e,k,v)
    result = {m.name:m for m in calculate(data.evidence,data.request.research_as_of)}
    assert result['pe'].value is None
    assert result['pe'].missing_reason

def test_future_values_excluded_from_quant(data):
    for e in data.evidence:
        if e.source_record_id == 'revenue_current':
            e.published_at = data.request.research_as_of + timedelta(seconds=1)
    result = {m.name:m for m in calculate(data.evidence,data.request.research_as_of)}
    assert result['revenue_growth'].value is None


@pytest.mark.parametrize('status', ['unknown', 'restricted'])
def test_unconfirmed_values_cannot_influence_metrics_even_with_internal_flag(data, status):
    record = next(e for e in data.evidence if e.metric_key == 'revenue_current')
    record.license_status = status
    assert record.internal_use_allowed
    metrics = {m.name: m for m in calculate(data.evidence, data.request.research_as_of)}
    assert metrics['revenue_growth'].value is None
    assert metrics['net_margin'].value is None
    assert all(record.evidence_id not in m.evidence_ids for m in metrics.values())


def test_different_research_cutoff_cannot_influence_metrics(data):
    record = next(e for e in data.evidence if e.metric_key == 'revenue_current')
    record.research_as_of -= timedelta(days=1)
    metrics = {m.name: m for m in calculate(data.evidence, data.request.research_as_of)}
    assert metrics['revenue_growth'].value is None
    assert all(record.evidence_id not in m.evidence_ids for m in metrics.values())

def test_nonfinite_evidence_rejected(data):
    payload=data.model_dump(mode='json')
    payload['evidence'][1]['value'] = math.inf
    with pytest.raises(ValidationError):ResearchInput.model_validate(payload)

def test_synthetic_cannot_be_renamed_real(data):
    payload=data.model_dump(mode='json');payload['dataset_kind']='real'
    with pytest.raises(ValidationError):ResearchInput.model_validate(payload)

def test_rating_dedup_validity_and_unclassified(data,mapping):
    base = data.evidence[0]
    cutoff = data.request.research_as_of
    def rating(eid,institution,raw,days=1,valid=True,analyst='A'):
        return base.model_copy(update={'evidence_id':eid,'kind':'rating','institution':institution,
            'raw_rating':raw,'analyst':analyst,'published_at':cutoff-timedelta(days=days),
            'valid_until':cutoff+timedelta(days=1) if valid else None})
    rows=[rating('r1','Firm One','Buy',days=2),rating('r2','Firm One','Hold'),
          rating('r3','Firm Two','Outperform',analyst='B'),rating('r4','Expired','Buy',valid=False),
          rating('r5','Future','Sell',days=-1)]
    result=consensus(rows,'NVDA',cutoff,mapping)
    assert result['institutions']==2
    assert result['counts']=={'Buy':0,'Hold':1,'Sell':0,'Unclassified':1}
    assert result['analysts']==2
    assert result['ratios']['Hold']==0.5
    mapping['institutions']['Firm Two']={'Outperform':'Buy'}
    assert consensus(rows,'NVDA',cutoff,mapping)['counts']['Buy']==1

def test_no_ratings_has_null_ratios(data,mapping):
    result=consensus(data.evidence,'NVDA',data.request.research_as_of,mapping)
    assert result['institutions']==0
    assert all(v is None for v in result['ratios'].values())
    assert result['warning']=='投顧共識資料不足'

@pytest.mark.parametrize('period',['FY2025','FY2022','Q12024','TTM',None])
def test_growth_requires_comparable_adjacent_fiscal_years(data,period):
    next(e for e in data.evidence if e.source_record_id=='revenue_prior').period=period
    result={m.name:m for m in calculate(data.evidence,data.request.research_as_of)}
    assert result['revenue_growth'].value is None


def test_imported_market_currency_mismatch_rejected(data):
    payload=data.model_dump(mode='json')
    payload['entities'][0]['currency']='TWD'
    with pytest.raises(ValidationError):ResearchInput.model_validate(payload)
