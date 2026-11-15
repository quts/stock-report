"""Authored synthetic contracts; no captured market prices or network access."""
from datetime import datetime, timezone, date, timedelta
import pytest
from stock_report.sources import build_adapter, SourceContractError, merge_profile
from stock_report.collect import collect, inspection_record, to_evidence
from stock_report.licensing import load_registry
from stock_report.models import Request
from stock_report.quant import calculate, months_before

SOURCE = 'yahoo-chart-daily-close'
NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


def payload():
    days = [(2025, 11, 14), (2026, 5, 14), (2026, 8, 14), (2026, 11, 14), (2026, 11, 15)]
    return {'chart': {'error': None, 'result': [{
        'meta': {'symbol': 'NVDA', 'currency': 'USD', 'exchangeName': 'NMS', 'instrumentType': 'EQUITY',
                 'dataGranularity': '1d', 'longName': 'NVIDIA synthetic contract'},
        'timestamp': [int(datetime(*day, 14, tzinfo=timezone.utc).timestamp()) for day in days],
        'indicators': {'quote': [{'close': [50, 80, 100, 120, 999]}]}}]}}


def parse(body=None):
    return build_adapter(SOURCE).parse('NVDA', {'chart': body or payload()}, NOW)


def result(body):
    return body['chart']['result'][0]


def test_completed_close_and_observed_availability():
    parsed = parse()
    rows = {o.role: o for o in parsed.observations if o.role}
    assert rows['close'].value == 120
    assert rows['close'].period_end == date(2026, 11, 14)
    assert set(rows) == {'close', 'close_anchor_3m', 'close_anchor_6m', 'close_anchor_12m'}
    assert all(o.published_at == NOW and o.publication_basis == 'observed_availability'
               for o in parsed.observations)
    assert all(o.price_adjustment == 'unknown' for o in rows.values())


@pytest.mark.parametrize('key,value', [('symbol', 'AMD'), ('currency', 'EUR'), ('exchangeName', 'BAD'),
    ('instrumentType', 'ETF'), ('dataGranularity', '1h'), ('longName', 'Other company')])
def test_response_identity_must_match(key, value):
    body = payload()
    result(body)['meta'][key] = value
    with pytest.raises(SourceContractError):
        parse(body)


@pytest.mark.parametrize('fault', ['duplicate', 'reverse', 'future', 'old', 'nan', 'zero', 'bool', 'unaligned', 'error'])
def test_bad_series_rejected(fault):
    body = payload()
    row = result(body)
    closes = row['indicators']['quote'][0]['close']
    if fault == 'duplicate': row['timestamp'][1] = row['timestamp'][0]
    if fault == 'reverse': row['timestamp'].reverse()
    if fault == 'future': row['timestamp'][-1] += 86400
    if fault == 'old': row['timestamp'][0] -= 900 * 86400
    if fault == 'nan': closes[0] = float('nan')
    if fault == 'zero': closes[0] = 0
    if fault == 'bool': closes[0] = True
    if fault == 'unaligned': closes.pop()
    if fault == 'error': body['chart']['error'] = {'code': 'Not Found'}
    with pytest.raises(SourceContractError):
        parse(body)


def test_missing_anchor_is_not_filled():
    body = payload()
    result(body)['indicators']['quote'][0]['close'][2] = None
    parsed = parse(body)
    assert 'close_anchor_3m' not in {o.role for o in parsed.observations}


@pytest.mark.parametrize('key', ['adjustment_basis', 'source_class', 'price_field', 'adapter'])
def test_provenance_cannot_be_asserted_by_override(key):
    with pytest.raises(ValueError, match='cannot be overridden'):
        merge_profile(SOURCE, {key: 'split_adjusted'})


def test_unknown_license_never_retains_values():
    class Client:
        def get_json(self, url):
            return payload(), {'response_sha256': 'a' * 64}
    registry = load_registry()
    capture = collect(SOURCE, 'NVDA', registry=registry, allow_network=True, client=Client(), now=NOW)
    record = inspection_record(capture)
    assert capture.source_class == 'secondary'
    assert capture.license['status'] == 'unknown'
    assert not capture.license['internal_use_allowed']
    assert 'value' not in record['observations'][1]
    assert all('response_sha256' not in row for row in capture.transport)


def evidence():
    request = Request(mode='stock', ticker='NVDA', exchange='NASDAQ', research_as_of=NOW)
    snapshot = load_registry().snapshot(SOURCE)
    rows = [to_evidence(o, request, snapshot, 'network', True, source_class='secondary')
            for o in parse().observations]
    # These are authored test records, with explicit test-only permissions.
    for row in rows:
        row.verification_status = 'verified'
        row.license_status = 'confirmed'
        row.internal_use_allowed = True
        row.price_adjustment = 'split_adjusted' if row.kind == 'price' else None
    return rows


def test_price_changes_and_cutoff():
    rows = evidence()
    metrics = {m.name: m for m in calculate(rows, NOW)}
    assert [metrics[f'price_change_{m}m'].value for m in (3, 6, 12)] == [20, 50, 140]
    assert all(len(metrics[f'price_change_{m}m'].evidence_ids) == 2 for m in (3, 6, 12))
    assert all(m.value is None for m in calculate(rows, NOW - timedelta(seconds=1)))


@pytest.mark.parametrize('basis', [None, 'unknown', 'unadjusted', 'split_dividend_adjusted'])
def test_undocumented_or_other_adjustment_is_na(basis):
    rows = evidence()
    for row in rows:
        if row.kind == 'price': row.price_adjustment = basis
    metrics = [m for m in calculate(rows, NOW) if m.name.startswith('price_change_')]
    assert len(metrics) == 3 and all(m.value is None and m.missing_reason for m in metrics)


def test_calendar_month_clamping():
    assert months_before(date(2024, 5, 31), 3) == date(2024, 2, 29)


@pytest.mark.parametrize('fault', ['currency', 'snapshot', 'adjustment', 'zero', 'anchor'])
def test_incomparable_price_points_are_na(fault):
    rows = evidence()
    anchor = next(e for e in rows if e.metric_key == 'close_anchor_3m')
    if fault == 'currency': anchor.currency = 'EUR'
    if fault == 'snapshot': anchor.retrieved_at += timedelta(seconds=1)
    if fault == 'adjustment': anchor.price_adjustment = 'unknown'
    if fault == 'zero': anchor.value = 0
    if fault == 'anchor':
        anchor.period_start = anchor.period_end = date(2026, 8, 1)
    metric = next(m for m in calculate(rows, NOW) if m.name == 'price_change_3m')
    assert metric.value is None and metric.missing_reason


@pytest.mark.network
@pytest.mark.parametrize('ticker', ['NVDA', 'AMD'])
def test_live_us_contract_without_retention(ticker):
    # Opt-in smoke: no numeric values, raw bodies or hashes are persisted or printed.
    capture = collect(SOURCE, ticker, registry=load_registry(), allow_network=True)
    assert capture.entity.entity_id == ticker
    assert capture.source_class == 'secondary'
    assert not capture.license['internal_use_allowed']
    assert all(o.price_adjustment == 'unknown' for o in capture.observations if o.kind == 'price')
