"""Opt-in official-source smoke tests.

Deselected by default (`-m "not network"`). Run explicitly and record the result:

    ./scripts/runtime.sh test -m network
    ./scripts/runtime.sh smoke --allow-network

These tests assert the recorded field contract against the live endpoints. They
never assert any licensing outcome beyond "still unknown", because reaching an
endpoint establishes no right to reuse its contents.
"""
import os
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
