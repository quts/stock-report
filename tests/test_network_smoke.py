"""Opt-in official-source smoke tests.

Deselected by default (`-m "not network"`). Run explicitly and record the result:

    ./scripts/runtime.sh test -m network
    ./scripts/runtime.sh smoke --allow-network

These tests assert the recorded field contract against the live endpoints. They
never assert any licensing outcome beyond "still unknown", because reaching an
endpoint establishes no right to reuse its contents.
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
import pytest
from stock_report.collect import SEC_CONTACT_ENV, collect, persist, persistence_blockers
from stock_report.licensing import load_registry

pytestmark = pytest.mark.network
OUTPUT = Path('reports/phase-0/source-smoke')


@pytest.mark.parametrize('source,ticker', [('twse-openapi-stock-day-all', '2330.TW'),
                                           ('tpex-openapi-mainboard-daily-close', '6488.TWO')])
def test_taiwan_official_endpoint_contract(source, ticker):
    started = datetime.now(timezone.utc)
    collection = collect(source, ticker, registry=load_registry('config/source-licenses.json'), allow_network=True)
    assert collection.capture_mode == 'network'
    assert collection.collected_at >= started
    assert collection.entity.entity_id == ticker and collection.entity.currency == 'TWD'
    # Confirmed live contract: both endpoints carry the target fields and a ROC trading date.
    assert collection.field_binding['code'] and collection.field_binding['close']
    assert collection.field_binding['date'] and collection.field_binding['date_format'] == 'roc'
    close = next(o for o in collection.observations if o.role == 'close')
    assert close.value is not None and close.event_time <= collection.collected_at
    assert all(o.publication_basis == 'observed_availability' for o in collection.observations)
    assert collection.license['status'] == 'unknown' and not collection.license['pdf_share_allowed']
    # Terms are still unconfirmed, so only an inspection record may be written.
    assert persistence_blockers(collection)[0]['code'] == 'source_values_not_cleared_for_local_persistence'
    path, artifact = persist(collection, OUTPUT)
    assert artifact == 'inspection_record'
    text = path.read_text()
    assert collection.entity.name not in text and str(close.value) not in text


@pytest.mark.parametrize('source,ticker,market', [('twse-openapi-income-general', '2330.TW', 'twse'),
                                                  ('tpex-openapi-income-general', '6488.TWO', 'tpex')])
def test_taiwan_income_statement_endpoint_contract(source, ticker, market):
    started = datetime.now(timezone.utc)
    collection = collect(source, ticker, registry=load_registry('config/source-licenses.json'), allow_network=True)
    assert collection.capture_mode == 'network' and collection.collected_at >= started
    assert collection.entity.entity_id == ticker and collection.entity.currency == 'TWD'
    binding = collection.field_binding
    assert all(binding[name] for name in ('code', 'name', 'year', 'quarter', 'table_date', 'revenue',
                                          'net_income_parent', 'eps_basic'))
    assert binding['table_date_format'] == 'roc' and binding['period_basis'] == 'cumulative_ytd'
    roles = {o.role: o for o in collection.observations if o.role}
    # Revenue and EPS are present for both targets; a blank parent net income would stay missing.
    assert {'revenue_current', 'eps_current'} <= set(roles)
    for observation in roles.values():
        assert observation.period_basis == 'cumulative_ytd'
        assert observation.period_start.month == 1 and observation.period_start.day == 1
        assert observation.period_start.year == observation.period_end.year
        assert observation.event_time < collection.collected_at
        assert observation.publication_basis == 'observed_availability'
    assert roles['revenue_current'].source_unit_raw is None and roles['eps_current'].source_unit_raw == '元'
    assert collection.license['status'] == 'unknown' and not collection.license['internal_use_allowed']
    path, artifact = persist(collection, OUTPUT)
    assert artifact == 'inspection_record'
    text = path.read_text()
    assert collection.entity.name not in text
    assert all(o.provider_record_id not in text for o in collection.observations)
    # The dataset does not declare its cumulative basis; re-check it against the monthly filing.
    sys.path.insert(0, 'scripts')
    try:
        from contract_probe import cumulative_basis
    finally:
        sys.path.remove('scripts')
    check = cumulative_basis(market, ticker.split('.')[0])
    assert check['conclusion'] in ('cumulative_ytd_same_scale_as_monthly_filing', 'not_checkable_for_this_period')


@pytest.mark.skipif(not os.environ.get(SEC_CONTACT_ENV),
                    reason=f'{SEC_CONTACT_ENV} is not configured by the operator')
@pytest.mark.parametrize('ticker', ['NVDA', 'AMD'])
def test_sec_official_filing_contract(ticker):
    collection = collect('sec-xbrl-companyconcept', ticker, registry=load_registry('config/source-licenses.json'),
                         allow_network=True)
    roles = {o.role for o in collection.observations}
    assert {'revenue_current', 'revenue_prior', 'net_income'} <= roles
    assert all(o.publication_basis == 'source_declared_date' for o in collection.observations)
    assert collection.license['status'] == 'unknown' and not collection.license['internal_use_allowed']
    path, artifact = persist(collection, OUTPUT)
    assert artifact == 'inspection_record'
    assert 'observations' in path.read_text()
