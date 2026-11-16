"""Catalog identity is distinct from conservative historical availability."""
from datetime import date, datetime, timezone
from stock_report.filing_catalog import classify, index, filing_day_upper_bound, parse_submissions
from stock_report.backtesting import baseline_comparison
import pytest


def entry(**changes):
    return dict(accession='0001571996-24-000001', form='10-Q', amendment=False,
                report_period=date(2024, 5, 3), filing_date=date(2024, 6, 4),
                acceptance_declared='2024-06-04T16:00:00', cik='0001571996',
                conformed_name='Dell Technologies Inc.', catalog_origin='submissions_recent', **changes)


def classify_entry(row, clock=None, end=date(2024, 5, 3)):
    return classify(row['accession'], end, clock or filing_day_upper_bound(row['filing_date']),
                    index({'entries': [row]}), row['cik'])


def test_acceptance_is_required_but_never_used_as_publication_time():
    row = entry()
    assert classify_entry(row)['verified_availability']
    row['acceptance_declared'] = None
    assert not classify_entry(row)['verified_availability']
    row['acceptance_declared'] = '2024-06-04T16:00:00'
    assert not classify_entry(row, datetime(2024, 6, 4, 21, tzinfo=timezone.utc))['verified_availability']


def test_amendment_and_comparative_are_not_originals():
    row = entry()
    assert classify_entry(row)['vintage'] == 'verified_original'
    row.update(form='10-Q/A', amendment=True)
    assert classify_entry(row)['vintage'] == 'verified_amendment'
    assert classify_entry(row, end=date(2023, 5, 3))['vintage'] == 'verified_comparative'
    assert not classify_entry(row, end=date(2025, 5, 3))['verified_availability']


def test_catalog_does_not_accept_another_issuer():
    payload = {'cik': 1, 'name': 'Dell'}
    with pytest.raises(ValueError, match='another CIK'):
        parse_submissions(payload, {'cik': '0001571996'}, datetime.now(timezone.utc))


def test_archive_header_binds_accession_cik_form_and_acceptance():
    from stock_report.filing_catalog import parse_index_header, CatalogError
    header = '''ACCESSION NUMBER: 0001571996-24-000001
CONFORMED SUBMISSION TYPE: 10-Q/A
CONFORMED PERIOD OF REPORT: 20240503
FILED AS OF DATE: 20240604
<ACCEPTANCE-DATETIME>20240604164941
COMPANY CONFORMED NAME: Dell Technologies Inc.
CENTRAL INDEX KEY: 0001571996
'''
    issuer = {'cik': '0001571996', 'expected_entity_name_contains': 'Dell Technologies'}
    cutoff = datetime(2024, 6, 5, 12, tzinfo=timezone.utc)
    row = parse_index_header(header, issuer, '0001571996-24-000001', cutoff)
    assert row['amendment'] and row['report_period'] == date(2024, 5, 3)
    assert row['acceptance_declared'].startswith('2024-06-04T16:49:41')
    assert parse_index_header(header, issuer, '0001571996-24-000001',
                              datetime(2024, 6, 3, 12, tzinfo=timezone.utc)) is None
    with pytest.raises(CatalogError, match='accession differs'):
        parse_index_header(header, issuer, '0001571996-24-000002', cutoff)
    with pytest.raises(CatalogError, match='issuer name'):
        parse_index_header(header.replace('Dell Technologies Inc.', 'Another Issuer'), issuer,
                           '0001571996-24-000001', cutoff)


def test_baselines_use_only_matched_observations():
    from stock_report.backtesting import METRICS, BASELINE_RULES
    errors = {m: {'predicted': 110, 'actual': 100, 'error_percent': 10} for m in METRICS}
    row = {'errors': errors, 'baselines': {b: {m: {'predicted': 120} for m in METRICS} for b in BASELINE_RULES}}
    comparison = baseline_comparison([row])
    assert comparison['revenue']['ttm_carry_forward']['matched_samples'] == 1
    row['baselines']['ttm_carry_forward']['revenue']['predicted'] = None
    assert baseline_comparison([row])['revenue']['ttm_carry_forward']['matched_samples'] == 0


def test_latest_free_csv_is_scoped_and_observed_not_historical():
    from stock_report.sources import build_adapter
    adapter = build_adapter('twse-opendata-bwibbu-d-csv')
    now = datetime(2026, 11, 16, 15, tzinfo=timezone.utc)
    rows = [{'日期': '20261116', '證券代號': '2330', '證券名稱': '台積電', '收盤價': '2,350'}]
    parsed = adapter.parse('2330.TW', {'daily': rows}, now)
    close = next(e for e in parsed.observations if e.kind == 'price')
    assert close.value == 2350 and close.availability_observed_at == now
    assert close.publication_basis == 'observed_availability'
    assert adapter.requests('2330.TW')[0].url.endswith('?response=open_data')
    with pytest.raises(ValueError, match='identity scope'):
        adapter.requests('2317.TW')
    with pytest.raises(ValueError, match='cannot be overridden'):
        build_adapter('twse-opendata-bwibbu-d-csv', {'allowed_tickers': ['2317.TW']})


def test_forward_cashflow_remains_disabled_after_historical_screen():
    from stock_report.fcf_history import forward_driver_assessment
    result = forward_driver_assessment({'quarters': {}, 'cumulative': {}, 'instants': {}})
    assert not result['forward_modeling_supported']


def test_verified_revenue_origin_does_not_verify_eps_training_inputs():
    from stock_report.backtesting import verified_metric_rows
    row = {'origin_availability_verified': True,
           'metric_input_verification': {'revenue': {'verified': True}, 'eps': {'verified': False}},
           'errors': {m: {'predicted': 110, 'actual': 100, 'error_percent': 10} for m in ('revenue', 'eps')}}
    result = verified_metric_rows([row])[0]
    assert result['errors']['revenue']['predicted'] == 110
    assert result['errors']['eps']['predicted'] is None
    assert row['errors']['eps']['predicted'] == 110  # Retain descriptive unverified evaluation.
    unverified = {**row, 'origin_availability_verified': False}
    results = verified_metric_rows([row, unverified])
    assert len(results) == 2 and results[1]['errors']['revenue']['predicted'] is None


def test_csv_transport_discards_raw_hash_and_unrelated_issuers():
    from stock_report.collect import collect
    from stock_report.licensing import load_registry
    class Client:
        def get_text(self, url):
            return ('\ufeff日期,證券代號,證券名稱,收盤價\n20261116,2330,台積電,100\n20261116,9999,Other,200\n',
                    {'attempts': 1, 'cached': False, 'response_sha256': 'must-not-persist'})
    result = collect('twse-opendata-bwibbu-d-csv', '2330.TW', registry=load_registry('config/source-licenses.json'),
                     allow_network=True, client=Client(), now=datetime(2026, 11, 16, 15, tzinfo=timezone.utc))
    assert len(result.observations) == 2
    assert all('response_sha256' not in t for t in result.transport)
    assert all(o.entity_id == '2330.TW' for o in result.observations)


def test_source_date_bound_is_not_an_exact_publication_timestamp():
    from engine_fixtures import dell
    from stock_report.retrospective import filing_dataset
    data = dell()
    first = next(e for e in data.evidence if e.financial_metric == 'revenue')
    data.evidence = [e.model_copy(update={'publication_basis': 'source_declared_date'})
                     if e.evidence_id == first.evidence_id else e for e in data.evidence]
    row = next(r for r in filing_dataset(data, 'DELL')['rows'] if r['evidence_id'] == first.evidence_id)
    assert row['source_publication_date_known']
    assert not row['source_publication_timestamp_known']
