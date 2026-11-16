"""Regressions for matched scoring, fiscal calendars and explicit forecast horizons."""
from datetime import date, timedelta
import pytest
from stock_report.error_metrics import summarize
from stock_report.retrospective import _errors, _keyed, matched_pairs
from stock_report.forecast import quarter_windows, anchors
from stock_report.fin_series import build
from stock_report.fin_analysis import analyse
from engine_fixtures import dell


def test_zero_actuals_remain_in_mae_wape_and_signed_bias():
    rows = [{'predicted': 5, 'actual': 0}, {'predicted': 8, 'actual': 10}]
    score = summarize(rows)
    assert score['mae'] == 3.5
    assert score['bias'] == 1.5
    assert score['wape_percent'] == 70
    assert score['aggregate_bias_percent'] == 30
    assert summarize([rows[0]])['wape_percent'] is None
    assert summarize([])['mae'] is None
    err = {**rows[0], 'error_percent': None, 'actual_evidence_ids': ['a']}
    keyed = _keyed([{'origin': '2020-01-01', 'candidates': {'drift': {'inputs_verified': True,
                    'methods': {'raw_rule': {'errors': {'1': err}}}}}}], 'raw_rule', True)
    assert len(keyed['drift']) == 1
    score = matched_pairs({1: err}, {1: err}, [1])
    assert score['samples'] == 1 and score['candidate']['mae'] == 5
    assert score['mape_improvement_pp'] is None


def test_partial_guidance_path_cannot_be_scored_as_four_quarter_total():
    targets = [{'value': 10, 'evidence_ids': ['a']}] * 4
    assert 'ntm' not in _errors([12], targets)


def test_dell_calendar_includes_53rd_week_without_future_targets():
    # Only four historical windows, ending January 2022; no target facts are supplied.
    starts = [date(2021, 1, 30), date(2021, 5, 1), date(2021, 7, 31), date(2021, 10, 30)]
    ends = [s + timedelta(days=90) for s in starts]
    past = [{'start': s, 'end': e} for s, e in zip(starts, ends)]
    windows = quarter_windows({'quarters': {'revenue': past}, 'entity_id': 'DELL'})
    assert windows[0]['start'] == date(2022, 1, 29)
    assert windows[-1]['end'] == date(2023, 2, 3)
    assert (windows[-1]['end'] - windows[-1]['start']).days + 1 == 98
    assert all(a['end'] + timedelta(days=1) == b['start'] for a, b in zip(windows, windows[1:]))


def test_f4q_is_not_asof_ntm_and_completed_unreported_quarter_is_estimate():
    data = dell()
    series = build(data, 'DELL')
    h = anchors(series, analyse(data, series, 'DELL'))['horizon']
    assert h['label'].startswith('F4Q ')
    assert h['start'] == '2026-08-01'
    assert h['as_of_ntm_start'] == data.request.research_as_of.date().isoformat()
    assert h['completed_unreported_quarters'] == [{'start': '2026-08-01', 'end': '2026-10-30'}]
    assert h['as_of_ntm_computed'] is False


def test_split_in_earnings_price_window_requires_basis_audit():
    from stock_report import multiples, market_data
    from test_valuation_reliability import with_imports, import_file, quarter_end_prices
    data = with_imports(dell(price=None), import_file('prices', quarter_end_prices()),
                        import_file('splits', [{'date': '2024-09-03', 'ratio': 2}]))
    series = build(data, 'DELL')
    ref = multiples.historical_pe(series, market_data.prices(data, data.entities[0]), data, 'DELL')
    assert ref['exclusions']['split_eps_share_basis_unverified'] > 0
    # Fully post-split earnings windows become usable again; no second adjustment is applied.
    assert ref['points'] and all(p['date'] > '2025-09-03' for p in ref['points'])
    assert all(p['pe'] == pytest.approx(12, rel=0.01) for p in ref['points'])


def test_observed_availability_bound_defines_origin_cutoff():
    from stock_report.backtesting import origin_release, snapshot
    data = dell()
    e = next(e for e in data.evidence if e.financial_metric == 'revenue')
    observed = e.published_at + timedelta(days=10)
    e = e.model_copy(update={'publication_basis': 'observed_availability', 'availability_observed_at': observed})
    data = data.model_copy(update={'evidence': [e]})
    assert origin_release(data, 'DELL', e.period_end)['clock'] == observed
    assert snapshot(data, e.period_end, observed).evidence == []
    assert snapshot(data, e.period_end, observed + timedelta(microseconds=1)).evidence


def test_later_observed_historical_price_cannot_create_pit_multiple():
    from stock_report import multiples, market_data
    from test_valuation_reliability import with_imports, import_file, quarter_end_prices
    data = with_imports(dell(price=None), import_file('prices', quarter_end_prices()))
    data.evidence = [e.model_copy(update={'publication_basis': 'observed_availability',
                     'availability_observed_at': data.request.research_as_of}) if e.kind == 'price' else e
                     for e in data.evidence]
    ref = multiples.historical_pe(build(data, 'DELL'), market_data.prices(data, data.entities[0]), data, 'DELL')
    assert ref['points'] == []
    assert ref['exclusions']['historical_price_not_available_on_observation_date'] > 0
