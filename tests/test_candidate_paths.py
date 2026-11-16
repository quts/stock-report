"""Candidate revenue paths, forecast hierarchy, filing vintages, retrospectives, stability and
nonpredictive historical FCF. Fixture values are test-only."""
from datetime import timedelta
import unicodedata
import pytest
from stock_report import candidates, engine, fin_series, forecast, retrospective, stability
from stock_report.fin_analysis import analyse
from stock_report.quality_gates import hierarchy_checks, economic_checks
from engine_fixtures import dell, tsmc
from test_investment_engine import proposal_for, scenario, gate
from test_investment_report import render_fixture


def anchor_for(data, entity):
    s = fin_series.build(data, entity)
    return forecast.anchors(s, analyse(data, s, entity))


def candidate_scenarios(fx=None, fx_ids=(), period=None):
    kw = {'period': period} if period else {}
    rows = [scenario('bear', 0, 10, margin=7.0, margin_basis='model_assumption', **kw), scenario('base', 0, 12, **kw),
            scenario('bull', 0, 14, margin=8.0, margin_basis='model_assumption', **kw)]
    for row, rule in zip(rows, ('seasonal_naive', 'level_carry_forward', 'drift')):
        row.revenue_candidate = rule
        row.guidance_fx_rate, row.guidance_fx_evidence_ids = fx, list(fx_ids)
    return rows


def with_accessions(data):
    """Each quarter's facts come from its own filing: an original vintage with an accession."""
    out = []
    for i, e in enumerate(data.evidence):
        if e.kind == 'financial' and e.period_end:
            acc = f'0009999999-{e.period_end:%y}-{e.period_end:%m%d}00'
            e = e.model_copy(update={'source_record_id': f'{acc}:{e.financial_metric}'})
        out.append(e)
    return data.model_copy(update={'evidence': out})


# --- Candidate rules and applicability ---------------------------------------------------------

def test_candidates_are_shared_by_tw_and_us_and_seasonal_naive_repeats_same_quarter():
    for data, entity in ((dell(), 'DELL'), (tsmc(), '2330.TW')):
        a = anchor_for(data, entity)
        built = candidates.build(a, forecast.guidance_records(data, entity))
        revenue = a['series']['quarters']['revenue']
        sn = built['candidates']['seasonal_naive']
        assert sn['status'] == 'applicable'
        assert [q['raw_value'] for q in sn['quarters']] == pytest.approx([p['value'] for p in revenue[-4:]])
        level = built['candidates']['level_carry_forward']
        assert [q['raw_value'] for q in level['quarters']] == pytest.approx([revenue[-1]['value']] * 4)
        assert built['selected'] is None and 'no_candidate_selected' in built['selection_status']
        assert all(c['hierarchy'] == 'mechanical_sensitivity' for c in built['candidates'].values())


def test_guidance_replaces_only_its_own_quarter_and_needs_verified_fx():
    data = dell()
    built = candidates.build(anchor_for(data, 'DELL'), forecast.guidance_records(data, 'DELL'))
    for c in built['candidates'].values():
        assert [q['guided'] for q in c['quarters']] == [True, False, False, False]
        assert c['quarters'][0]['value'] == 49e9
        assert c['quarter_hierarchy'] == ['evidence_supported_forecast'] + ['mechanical_sensitivity'] * 3
        assert c['hierarchy'] == 'mechanical_sensitivity'
    data = tsmc()
    a = anchor_for(data, '2330.TW')
    no_fx = candidates.build(a, forecast.guidance_records(data, '2330.TW'))
    assert not any(q['guided'] for q in no_fx['candidates']['level_carry_forward']['quarters'])
    assert {r['reason'] for r in no_fx['candidates']['level_carry_forward']['guidance_rejected']} == {'missing_explicit_fx'}
    with_fx = candidates.build(a, forecast.guidance_records(data, '2330.TW'), fx_rate=31.8)
    assert with_fx['candidates']['level_carry_forward']['quarters'][0]['value'] == pytest.approx(52.3e9 * 31.8)


def test_rules_needing_more_history_are_not_applicable_with_a_reason():
    data = dell(drop=[e.evidence_id for e in dell().evidence if e.financial_metric == 'revenue'
                      and e.period_end and e.period_end.year < 2025])
    built = candidates.build(anchor_for(data, 'DELL'))
    status = {n: c['status'] for n, c in built['candidates'].items()}
    assert status['seasonal_naive'] == status['level_carry_forward'] == 'applicable'
    assert status['seasonal_yoy_trend'] == 'not_applicable'
    assert '連續季度' in built['candidates']['seasonal_yoy_trend']['reason']
    assert built['candidates']['seasonal_yoy_trend']['ntm_revenue'] is None


def test_one_trend_contaminated_cycle_never_counts_as_seasonality():
    a = anchor_for(tsmc(), '2330.TW')
    season = candidates.seasonality(a['series']['quarters']['revenue'])
    assert season['status'] == 'insufficient_history' and season['detrended_complete_cycles'] < 2
    status = candidates.seasonality(anchor_for(dell(), 'DELL')['series']['quarters']['revenue'])['status']
    assert status in ('pattern_repeated_not_validated', 'not_consistent_across_cycles', 'insufficient_history')
    assert 'verified' not in status.replace('not_validated', '')


# --- Hierarchy and selection ---------------------------------------------------------------------

def test_without_point_in_time_evidence_base_is_only_a_reference():
    data = dell()
    r = engine.run(data, proposal_for(data, candidate_scenarios()))
    rows = {s['name']: s for s in r['scenarios']['scenarios']}
    assert r['scenarios']['base_role'] == 'reference_not_most_likely'
    assert rows['base']['investor_label'] == '參考路徑'
    assert all(s['hierarchy'] == 'mechanical_sensitivity' for s in rows.values())
    assert rows['bear']['statements']['revenue'] < rows['base']['statements']['revenue'] < rows['bull']['statements']['revenue']
    assert gate(r, 'forecast_hierarchy_integrity')['status'] == 'PASS'
    rows['base']['hierarchy'] = 'backtested_forecast'
    assert hierarchy_checks(r)[0]['status'] == 'FAIL'


def pit_eval(candidate_mape, baseline_mape, samples, method='raw_rule', integrity='pass', coverage=1.0, verified=True):
    pair = {'drift': {b: {'method': method, 'matched_ntm_samples': samples, 'ntm_coverage': coverage,
                          'candidate_ntm_mape': candidate_mape, 'baseline_ntm_mape': baseline_mape}
                      for b in candidates.BASELINES}}
    return {'point_in_time': {'integrity': {'status': integrity}, 'pairwise_by_method': {method: pair},
                              'verified_pairwise_by_method': {method: pair} if verified else {}}}


def test_preserved_clock_samples_without_catalog_verification_never_promote():
    a = anchor_for(dell(drop=['dell-guidance-rev']), 'DELL')
    built = candidates.select(candidates.build(a), pit_eval(5, 10, 50, verified=False), 8)
    assert built['selected'] is None
    assert 'origin_vintages_not_catalog_verified' in built['candidates']['drift']['promotion_check']['failed_prerequisites']


def test_sample_count_and_lower_mape_alone_never_promote_a_path():
    a = anchor_for(dell(drop=['dell-guidance-rev']), 'DELL')   # no guidance: the deployed method is the raw rule
    def chosen(evaluation):
        return candidates.select(candidates.build(a), evaluation, 8)
    assert chosen(pit_eval(10, 5, 50))['selected'] is None                       # many samples, worse
    assert chosen(pit_eval(5, 10, 3))['selected'] is None                        # better, too few
    assert chosen(pit_eval(5, 10, 50, integrity='fail'))['selected'] is None     # integrity not established
    assert chosen(pit_eval(5, 10, 50, coverage=0.5))['selected'] is None         # sample drawn from a thin subset
    revised_only = {'revised_data': pit_eval(1, 50, 50)['point_in_time']}
    assert chosen(revised_only)['selected'] is None
    ok = chosen(pit_eval(5, 10, 8))
    assert ok['selected'] == 'drift' and ok['candidates']['drift']['hierarchy'] == 'backtested_forecast'
    assert ok['candidates']['drift']['hierarchy_scope'] == 'revenue_path_only'
    assert 'not_statistical_validation' in ok['selection_status']
    assert 'validated_forecast' not in str(candidates.HIERARCHY)


def test_guidance_overlaid_path_cannot_be_promoted_by_a_raw_rule_evaluation():
    a = anchor_for(dell(), 'DELL')
    built = candidates.select(candidates.build(a, forecast.guidance_records(dell(), 'DELL')), pit_eval(1, 50, 50), 8)
    drift = built['candidates']['drift']
    assert drift['deployed_method'] == 'guidance_overlay' and built['selected'] is None
    assert 'deployed_method_guidance_overlay_not_evaluated_point_in_time' in drift['promotion_check']['failed_prerequisites']
    pit = retrospective.point_in_time(with_accessions(dell()), 'DELL')
    for row in pit['rows']:
        for result in row['candidates'].values():
            overlay = result.get('methods', {}).get('guidance_overlay')
            if overlay and overlay['errors'] is None:
                assert overlay['not_applicable_reason'].startswith(('no_preserved_historical_guidance', 'historical_guidance_rejected'))


def test_promoted_revenue_path_never_upgrades_the_whole_scenario():
    data = dell()
    r = engine.run(data, proposal_for(data, candidate_scenarios()))
    base = next(s for s in r['scenarios']['scenarios'] if s['name'] == 'base')
    base['revenue_candidate_hierarchy'] = 'backtested_forecast'      # not selected by evidence
    assert hierarchy_checks(r)[0]['status'] == 'FAIL'
    base['revenue_candidate_hierarchy'] = 'mechanical_sensitivity'
    base['confidence_class'] = 'validated_forecast'
    assert hierarchy_checks(r)[0]['status'] == 'FAIL'


# --- Filing vintages and retrospectives ----------------------------------------------------------

def test_filing_dataset_labels_inferences_and_never_claims_verified_originals_or_redates():
    data = with_accessions(dell())
    latest = next(e for e in data.evidence if e.financial_metric == 'revenue' and str(e.period_end) == '2026-07-31')
    prior = next(e for e in data.evidence if e.financial_metric == 'revenue' and str(e.period_end) == '2025-08-01')
    comparative = prior.model_copy(update={'evidence_id': 'comparative-in-later-filing', 'published_at': latest.published_at,
                                           'event_time': latest.published_at, 'source_record_id': latest.source_record_id})
    data.evidence.append(comparative)
    dataset = retrospective.filing_dataset(data, 'DELL')
    rows = {r['evidence_id']: r for r in dataset['rows']}
    assert rows[latest.evidence_id]['vintage'] == 'inferred_original'          # >=2 periods in the accession
    assert rows[prior.evidence_id]['vintage'] == 'unknown'                     # single preserved period
    assert rows['comparative-in-later-filing']['vintage'] == 'inferred_comparative'
    assert rows['comparative-in-later-filing']['preserved_version_clock'] == latest.published_at   # never redated
    assert not any(r['original_verified'] for r in dataset['rows']) and dataset['verified_original_count'] == 0
    plain = retrospective.filing_dataset(dell(), 'DELL')
    assert set(plain['vintage_counts']) == {'unknown'}
    assert plain['acquisition']['new_original_filings_acquired'] is False


def test_late_first_appearance_is_unknown_not_proof_of_a_comparative():
    data = with_accessions(dell())
    latest = next(e for e in data.evidence if e.financial_metric == 'revenue' and str(e.period_end) == '2025-10-31')
    late = latest.published_at + timedelta(days=100)       # 140 days after period end, still before the research cutoff
    data.evidence = [e.model_copy(update={'published_at': late, 'event_time': late})
                     if e.kind == 'financial' and e.period_end == latest.period_end else e for e in data.evidence]
    data.evidence.append(next(e for e in data.evidence if e.financial_metric == 'revenue' and str(e.period_end) == '2024-11-01')
                         .model_copy(update={'evidence_id': 'comp', 'published_at': late, 'event_time': late,
                                             'source_record_id': latest.source_record_id}))
    rows = {r['evidence_id']: r for r in retrospective.filing_dataset(data, 'DELL')['rows']}
    assert rows[latest.evidence_id]['vintage'] == 'unknown' and 'late original' in rows[latest.evidence_id]['vintage_basis']


def observed_only(data):
    out = []
    for e in data.evidence:
        if e.kind == 'financial':
            e = e.model_copy(update={'publication_basis': 'observed_availability', 'availability_observed_at': e.published_at})
        out.append(e)
    return data.model_copy(update={'evidence': out})


def test_observed_availability_is_a_conservative_clock_not_a_source_publication():
    data = observed_only(dell())
    dataset = retrospective.filing_dataset(data, 'DELL')
    assert dataset['source_publication_timestamp_missing'] == len(dataset['rows'])
    assert set(dataset['clock_basis_counts']) == {'observed_availability_upper_bound'}
    assert set(dataset['vintage_counts']) == {'unknown'}                         # no accession: never original
    pit = retrospective.point_in_time(data, 'DELL', dataset)
    eligible = [r for r in pit['rows'] if r.get('eligible')]
    assert eligible and all(r['cutoff_basis'] == 'observed_availability_upper_bound' for r in eligible)


def test_late_preserved_origin_is_ineligible_and_late_training_periods_are_named():
    data = with_accessions(dell())
    late = data.evidence[0].published_at + timedelta(days=900)
    for i, e in enumerate(data.evidence):
        if e.kind == 'financial' and str(e.period_end) == '2025-05-02':
            data.evidence[i] = e.model_copy(update={'published_at': late, 'event_time': late,
                                                    'source_record_id': '0009999999-27-000001:' + e.financial_metric})
    pit = retrospective.point_in_time(data, 'DELL')
    rows = {str(r['origin']): r for r in pit['rows']}
    assert [c['code'] for c in rows['2025-05-02']['root_causes']] == ['origin_preserved_only_after_first_target_end']
    later = rows['2025-08-01']
    assert later['eligible'] and later['information_cutoff'].date() < later['targets'][0]
    sn = later['candidates']['seasonal_naive']
    assert sn['not_applicable_reason'] == 'training_period_unavailable_at_cutoff'
    assert [str(m['period_end']) for m in sn['missing_periods']] == ['2025-05-02']
    assert sn['missing_periods'][0]['status'] == 'preserved_only_after_cutoff'
    assert pit['integrity']['status'] == 'pass'
    for row in pit['rows']:
        if row.get('eligible'):
            assert row['max_input_clock'] < row['information_cutoff']   # no leakage


def test_point_in_time_and_revised_retrospectives_are_separate_and_compare_identical_keys():
    data = with_accessions(dell())
    result = retrospective.evaluate(data, 'DELL')
    pit, revised = result['point_in_time'], result['revised_data']
    assert pit['method'].startswith('strict_point_in_time') and revised['method'] == 'revised_data_not_point_in_time'
    assert revised['promotes_candidates'] is False and 'no statistical reliability' in result['claims']
    pair = revised['scores']['pairwise']['drift']['seasonal_naive']
    keyed = {}
    for row in revised['rows']:
        for name in ('drift', 'seasonal_naive'):
            for h, err in (row['candidates'][name]['methods']['raw_rule']['errors'] or {}).items():
                if h != 'ntm':
                    keyed.setdefault(name, {})[(str(row['origin']), h)] = err['actual']
    common = keyed['drift'].keys() & keyed['seasonal_naive'].keys()
    assert pair['matched_quarter_samples'] == len(common) > 0
    assert all(keyed['drift'][k] == keyed['seasonal_naive'][k] for k in common)
    assert revised['evaluated_method'] == 'raw_rule_without_guidance'
    # Without any preserved clock before the first target ends, no origin is eligible and the reason is named.
    late = dell()
    late.evidence = [e.model_copy(update={'published_at': e.published_at + timedelta(days=400),
                                          'event_time': e.published_at + timedelta(days=400)})
                     if e.kind == 'financial' else e for e in late.evidence]
    zero = retrospective.point_in_time(late, 'DELL')
    assert zero['eligible_origins'] == 0
    assert zero['zero_valid_origin_reason'] in ('origin_preserved_only_after_first_target_end', 'origin_preserved_only_after_research_cutoff')
    assert retrospective.revised_data(late, 'DELL')['rows']


# --- Historical FCF (nonpredictive) versus blocked forecast --------------------------------------

def test_historical_fcf_sensitivity_is_nonpredictive_and_never_fills_forecast_fields():
    data = dell()
    r = engine.run(data, proposal_for(data, candidate_scenarios()))
    hist = r['historical_fcf']['history']
    assert hist['predictive'] is False and hist['forecast_fields_populated'] is False and hist['rows']
    row = hist['rows'][-1]
    assert row['free_cash_flow'] == pytest.approx(row['operating_cash_flow'] - row['capital_expenditure'])
    assert row['evidence_ids'] and 'FCF = reported OCF - reported capital expenditure' in row['formula']
    assert r['forecast']['reconciliation']['status'] != 'reconciled'
    assert all(s['statements']['free_cash_flow'] is None and s['statements']['operating_cash_flow'] is None
               for s in r['scenarios']['scenarios'])
    assert r['historical_fcf']['sensitivity']['predictive'] is False
    assert gate(r, 'historical_fcf_nonpredictive')['status'] == 'PASS'
    r['scenarios']['scenarios'][1]['statements']['free_cash_flow'] = 1.0
    assert hierarchy_checks(r)[1]['status'] == 'FAIL'


def test_negative_fcf_is_flagged_and_adjusted_measures_are_excluded():
    from engine_fixtures import fin, UTC
    from datetime import date, datetime
    data = dell()
    cutoff = data.request.research_as_of
    extra = [fin('DELL', cutoff, 'adjusted_free_cash_flow', 9e9, date(2026, 5, 2), date(2026, 7, 31), 'single_quarter',
                 'USD', 'USD', accounting_basis='company_adjusted')]
    data.evidence = [e.model_copy(update={'value': -abs(e.value)}) if e.financial_metric == 'operating_cash_flow' else e
                     for e in data.evidence] + extra
    r = engine.run(data)
    hist = r['historical_fcf']['history']
    assert hist['rows'] and all(row['negative_fcf'] for row in hist['rows'])     # kept, not dropped as missing
    assert hist['summary']['contains_negative_fcf'] and '燒錢' in hist['summary']['negative_fcf_note']
    sens = r['historical_fcf']['sensitivity']
    assert sens['contains_cash_burn'] and all(c['fcf_yield_percent'] is None for row in sens['rows']
                                              for c in row['cells'].values() if c['cash_burn'])
    assert 'dell-adjusted_free_cash_flow-2026-07-31-single_quarter' in hist['adjusted_inputs_excluded']
    assert any(d['code'] == 'company_adjusted_cash_measures_excluded' for d in r['cashflow_mapping']['diagnoses'])
    assert r['cashflow_mapping']['residual_set_to_zero'] is False
    assert r['cashflow_mapping']['balance_sheet_reconciliation_claimed'] is False
    assert any(d['code'] == 'cash_flow_detail_roles_absent_from_bundle' for d in r['cashflow_mapping']['diagnoses'])


# --- Stability -----------------------------------------------------------------------------------

def test_stability_flags_rebased_horizon_guidance_windows_and_source_removal():
    data = dell()
    result = stability.assess(data, 'DELL', forecast.guidance_records(data, 'DELL'))
    excluded = result['tests']['exclude_latest_quarter']
    assert excluded['same_forecast_period'] is False and 'noncomparable' in excluded['period_mismatch_flag']
    assert all(not c['comparable'] and 'growth_change_pp' not in c and 'eps_change_percent' not in c
               for c in excluded['candidates'].values())
    normalized = result['tests']['normalize_exceptional_quarter']['normalized_quarter']
    assert normalized['sensitivity_only'] and normalized['evidence_values_unchanged']
    end = normalized['end']
    assert next(e.value for e in data.evidence if e.financial_metric == 'revenue' and e.period_end == end) == normalized['reported']
    assert set(excluded['excluded_quarter_check']) >= {'level_carry_forward', 'seasonal_naive'}
    assert result['tests']['normalize_exceptional_quarter']['normalized_quarter']['evidence_ids']
    removed = result['tests']['guidance_removed']
    assert removed['same_forecast_period'] is True
    assert removed['candidates']['level_carry_forward']['overlapping_quarters'][0]['change_percent'] != 0
    assert all(q['change_percent'] == pytest.approx(0) for q in removed['candidates']['level_carry_forward']['overlapping_quarters'][1:])
    windows = result['tests']['training_windows']['variants']
    assert windows['drift@2']['default_window'] == 4 and 'growth_change_pp' in windows['drift@2']
    assert result['thresholds']['purpose'].startswith('review flags only')


def test_removing_the_only_financial_source_is_reported_unavailable_not_imputed():
    data = with_accessions(dell())
    data.evidence = [e.model_copy(update={'license_source_id': 'src-a'}) if e.kind == 'financial' else e for e in data.evidence]
    result = stability.assess(data, 'DELL')
    assert result['tests']['source_removed:src-a']['status'] == 'unavailable'


def test_source_removal_also_removes_that_sources_guidance_and_fx_support():
    data = dell()
    data.evidence = [e.model_copy(update={'license_source_id': 'filings'}) if e.kind == 'financial' else
                     e.model_copy(update={'license_source_id': 'guidance-src'}) if e.statement_kind == 'management_guidance' else e
                     for e in data.evidence]
    guidance = forecast.guidance_records(data, 'DELL')
    result = stability.assess(data, 'DELL', guidance, fx_rate=1.0, fx_evidence_ids=['dell-guidance-rev'])
    removed = result['tests']['source_removed:guidance-src']
    assert removed['guidance_records_after_removal'] == 0 and removed['fx_after_removal'] is None
    assert not any(q['guided'] for q in result['baseline']['candidates']['level_carry_forward']['quarters'][1:])
    assert removed['candidates']['level_carry_forward']['overlapping_quarters'][0]['change_percent'] != 0


def test_excluding_the_latest_quarter_also_excludes_guidance_released_with_it():
    data = dell()
    latest = max(e.period_end for e in data.evidence if e.financial_metric == 'revenue' and e.kind == 'financial')
    release = min(e.published_at for e in data.evidence if e.kind == 'financial' and e.period_end == latest)
    data.evidence = [e.model_copy(update={'published_at': release, 'event_time': release})
                     if e.statement_kind == 'management_guidance' else e for e in data.evidence]
    result = stability.assess(data, 'DELL', forecast.guidance_records(data, 'DELL'))
    rebased = result['tests']['exclude_latest_quarter']['candidates']['level_carry_forward']
    assert rebased['comparable'] is False
    guided_quarter = next(q for q in rebased['overlapping_quarters'] if str(q['end']) == '2026-10-30')
    assert guided_quarter['baseline'] == 49e9 and guided_quarter['variant'] != 49e9   # guidance did not leak back


# --- Report wording and precision ----------------------------------------------------------------

RAW_IDS = ('cash_flow_statement_reconciliation', 'forecast_sample_sufficiency', 'forecast_growth_sanity',
           'review_evidence_not_cap_growth', 'operating_income 0', 'import-market-data', 'yahoo-chart-daily-close=')


def test_report_uses_investor_language_and_keeps_raw_diagnostics_out_of_the_pdf(tmp_path):
    data = tsmc()
    pages, text, inv = render_fixture(tmp_path, data, proposal_for(data, candidate_scenarios_tw()))
    assert pages <= data.request.max_pdf_pages
    for phrase in ('參考路徑', '營收候選路徑（條件式，非預測）', '不是最可能情境', '最大敏感度', '反向估值（價格隱含什麼）',
                   '預測層級'):
        assert unicodedata.normalize('NFKC', phrase) in text, phrase
    for raw in RAW_IDS:
        assert raw not in text, raw
    assert inv['reverse_valuation']['status'] == 'computed'
    base = next(s for s in inv['scenarios']['scenarios'] if s['name'] == 'base')
    assert base['display_significant_digits'] == 2


def candidate_scenarios_tw():
    rows = candidate_scenarios(period=anchor_for(tsmc(), '2330.TW')['horizon']['label'])
    for r in rows:
        r.entity_id = '2330.TW'
    return rows


def test_missing_price_keeps_reverse_valuation_missing(tmp_path):
    data = dell(price=None)
    _, text, inv = render_fixture(tmp_path, data, proposal_for(data, candidate_scenarios()))
    assert inv['reverse_valuation']['status'] == 'unavailable'
    assert unicodedata.normalize('NFKC', 'FCF／營收') in text and inv['historical_fcf']['history']['summary']
    assert '不是信賴區間' in text or unicodedata.normalize('NFKC', '不是信賴區間') in text
    assert '沒有已查核的現價' in text and unicodedata.normalize('NFKC', '反向估值（價格隱含什麼）') not in text
    for raw in RAW_IDS:
        assert raw not in text, raw


def test_reconciled_history_cannot_authorize_cash_forecasts_without_forward_assumptions():
    data = dell()
    inv = engine.run(data, proposal_for(data, candidate_scenarios()))
    inv['forecast']['reconciliation']['status'] = 'reconciled'
    inv['forecast']['base']['cashflow_ratios'] = None
    inv['scenarios']['scenarios'][1]['statements']['operating_cash_flow'] = 1
    inv['scenarios']['scenarios'][1]['statements']['free_cash_flow'] = 1
    assert next(c for c in hierarchy_checks(inv) if c['check'] == 'historical_fcf_nonpredictive')['status'] == 'FAIL'
    inv['forecast']['reconciliation']['residual'] = 0
    inv['forecast']['reconciliation']['residual_percent_ocf'] = 0
    assert next(c for c in economic_checks(data, inv) if c['check'] == 'ocf_reconciliation')['status'] == 'PASS'
