"""Rolling-origin, fixed 45-day reporting lag. No fallback from PIT into revised data."""
from datetime import datetime, timedelta, timezone
from statistics import mean
from . import fin_series, fin_analysis, forecast
from .policy import reportable

METRICS = ('revenue', 'operating_income', 'eps', 'operating_cash_flow', 'free_cash_flow')
MIN_SAMPLES = 8  # A minimum disclosure floor, never a statistical validation criterion.


def snapshot(data, origin, cutoff, revised=False):
    chosen = []
    for e in data.evidence:
        if not reportable(e, data.request):
            continue
        historical = e.kind == 'financial' and e.statement_kind == 'fact' and e.period_end and e.period_end <= origin
        timely = e.published_at < cutoff and e.event_time < cutoff
        if e.publication_basis == 'observed_availability':
            timely = timely and e.availability_observed_at is not None and e.availability_observed_at < cutoff
        if (historical and (timely or revised)) or (timely and e.statement_kind == 'management_guidance'):
            chosen.append(e)
    # Within a source and financial window use only the latest version available at cutoff.
    groups = {}
    for e in chosen:
        if e.statement_kind != 'fact':
            continue
        key = (e.financial_metric, e.period_start, e.period_end, e.period_basis, e.unit,
               e.currency, e.accounting_basis, e.license_source_id)
        groups[key] = max(groups.get(key, e.published_at), e.published_at)
    selected = []
    for e in chosen:
        key = (e.financial_metric, e.period_start, e.period_end, e.period_basis, e.unit,
               e.currency, e.accounting_basis, e.license_source_id)
        if e.statement_kind != 'fact' or e.published_at == groups[key]:
            selected.append(e)
    if revised:
        # Revised diagnostics intentionally retain today's clock; never pretend those versions
        # existed at the historical cutoff. Financial periods are still truncated at the origin.
        return data.model_copy(update={'evidence': selected})
    # Rebind the evaluation clock; never change the underlying publication/event timestamps.
    return data.model_copy(update={'request': data.request.model_copy(update={'research_as_of': cutoff}),
                                   'evidence': [e.model_copy(update={'research_as_of': cutoff}) for e in selected]})


def scores(rows):
    out = {}
    for name in METRICS:
        valid = [r['errors'][name] for r in rows if r['errors'][name]['predicted'] is not None and r['errors'][name]['actual'] is not None]
        relative = [v['error_percent'] for v in valid if v['error_percent'] is not None]
        out[name] = {'sample_size': len(valid), 'eligible_origins': len(rows),
                     'coverage': len(valid) / len(rows) if rows else 0,
                     'mae': mean(abs(v['predicted'] - v['actual']) for v in valid) if valid else None,
                     'bias': mean(v['predicted'] - v['actual'] for v in valid) if valid else None,
                     'mape_percent': mean(abs(v) for v in relative) if relative else None,
                     'bias_percent': mean(relative) if relative else None,
                     'percentage_sample_size': len(relative)}
    return out


def initialization_diagnostics(data, observed, full, series, origin, anchor):
    """Explain absence rather than conflating a data outage with a model initialization rule."""
    historical = [p for p in full['quarters'].get('revenue', []) if p['end'] <= origin]
    visible = series['quarters'].get('revenue', [])
    reasons = []
    if not fin_series.contiguous_tail(visible, 4):
        if fin_series.contiguous_tail(historical, 4):
            reasons.append('missing_historical_filing_snapshots')
        else:
            reasons.append('insufficient_or_noncontiguous_historical_periods')
    if visible and visible[-1]['end'] != origin:
        reasons.append('origin_filing_unavailable_at_cutoff')
    if not anchor['base'].get('shares'):
        reasons.append('missing_point_in_time_diluted_shares')
    if anchor['drivers']['operating_margin']['value'] is None:
        reasons.append('missing_operating_income_inputs')
    if anchor['reconciliation']['status'] != 'reconciled':
        reasons.append('missing_cash_flow_statement_components')
    known = {e.evidence_id for e in observed.evidence}
    excluded = [e for e in data.evidence if e.entity_id == data.entities[0].entity_id and e.kind == 'financial'
                and e.period_end and e.period_end <= origin and e.evidence_id not in known]
    return {'root_causes': sorted(set(reasons)), 'historical_revenue_periods': len(historical),
            'visible_revenue_periods': len(visible), 'excluded_later_publications': len(excluded),
            'initialization_policy': 'four_contiguous_quarters_for_NTM; latest_level_needs_no_two_YoY_pairs',
            'legacy_initialization_limitation': 'previous_policy_required_two_comparable_YoY_pairs_and_operating_margin_even_for_revenue'}


def rolling_origin(data, entity_id, include_revised=True):
    full = fin_series.build(data, entity_id)
    revenue = full['quarters'].get('revenue', [])
    rows, revised_rows = [], []
    for i in range(3, len(revenue) - 4):
        origin = revenue[i]['end']
        scheduled = datetime.combine(origin + timedelta(days=45), datetime.min.time(), timezone.utc)
        target = revenue[i+1:i+5]
        publications = [max(e.published_at, e.event_time) for e in data.evidence if e.entity_id == entity_id
                        and e.financial_metric == 'revenue' and e.statement_kind == 'fact'
                        and e.period_end == origin and reportable(e, data.request)]
        first_publication = min(publications) if publications else None
        timely_origin = first_publication is not None and first_publication.date() < target[0]['end']
        cutoff = first_publication + timedelta(microseconds=1) if timely_origin else scheduled
        if cutoff >= data.request.research_as_of or not fin_series.contiguous_tail(target, 4):
            continue
        end = target[-1]['end']
        future = {key: {m: [p for p in pts if p['end'] <= end] for m,pts in full[key].items()}
                  for key in ('quarters', 'instants', 'cumulative')}
        actual = {}
        for metric in METRICS:
            p = fin_series.ttm(future, 'diluted_eps' if metric == 'eps' else metric)
            actual[metric] = p['value'] if p and (p['start'], p['end']) == (target[0]['start'], end) else None
        ocf, capex = fin_series.ttm(future, 'operating_cash_flow'), fin_series.ttm(future, 'capital_expenditure')
        actual['free_cash_flow'] = ocf['value'] - capex['value'] if (ocf and capex and
            (ocf['start'], ocf['end']) == (capex['start'], capex['end']) == (target[0]['start'], end)) else None
        for revised, dest in ((False, rows), (True, revised_rows)) if include_revised else ((False, rows),):
            observed = snapshot(data, origin, cutoff, revised)
            series = fin_series.build(observed, entity_id)
            a = forecast.anchors(series, fin_analysis.analyse(observed, series, entity_id))
            diagnostic = initialization_diagnostics(data, observed, full, series, origin, a)
            if not timely_origin:
                diagnostic['root_causes'].append('original_origin_filing_not_preserved; latest_repeat_is_after_first_target_quarter')
            elif first_publication >= scheduled:
                diagnostic['root_causes'].append('legacy_fixed_45_day_cutoff_would_exclude_origin_filing')
            pred, projected = (forecast.quarterly_project(a, forecast.trend_drivers(a), forecast.guidance_records(observed, entity_id))
                               if a['base']['revenue_ttm'] and a['base']['revenue_ttm']['end'] == origin else (None, []))
            # An unknown/changed fiscal calendar cannot be graded against another target window.
            aligned = projected and (projected[0]['start'], projected[-1]['end']) == (target[0]['start'], end)
            errors = {}
            for metric in METRICS:
                estimate = pred.get(metric) if pred and aligned else None
                value = actual[metric]
                errors[metric] = {'predicted': estimate, 'actual': value,
                                  'error_percent': (estimate - value) / abs(value) * 100 if estimate is not None and value else None,
                                  'unavailable_reason': ('target_actual_missing' if value is None else
                                                        'calendar_mismatch' if pred and not aligned else
                                                        'forecast_input_missing_or_unreconciled' if estimate is None else None)}
            dest.append({'origin': origin, 'information_cutoff': cutoff.isoformat(), 'target_start': target[0]['start'],
                         'target_end': end, 'vintage_status': 'revised_diagnostic' if revised else 'strict_point_in_time',
                         'cutoff_basis': 'first_preserved_origin_publication' if timely_origin else 'scheduled_diagnostic_no_original_vintage',
                         'first_preserved_publication': first_publication, 'diagnostics': diagnostic,
                         'training_evidence_ids': [e.evidence_id for e in observed.evidence],
                         'max_input_published_at': max((e.published_at for e in observed.evidence), default=None),
                         'max_input_event_time': max((e.event_time for e in observed.evidence), default=None), 'errors': errors})
    metrics = scores(rows)
    from collections import Counter
    root_counts = Counter(reason for row in rows for reason in row['diagnostics']['root_causes'])
    return {'status': 'insufficient', 'method': 'strict_point_in_time', 'reporting_lag_days': 45,
            'root_cause_counts': dict(root_counts),
            'cutoff_policy': 'first_preserved_origin_filing_publication; scheduled45_only_for_unavailable_vintage_diagnostics',
            'fcf_test_count': metrics['free_cash_flow']['sample_size'], 'rows': rows, 'metrics': metrics,
            'revised_diagnostic': {'method': 'revised_historical_data_not_point_in_time',
                                   'rows': revised_rows, 'metrics': scores(revised_rows)},
            'reliability': 'not_validated', 'minimum_sample_floor': MIN_SAMPLES,
            'limitation': '以保留的首次申報公布時間為截止；若只有晚於預測季的比較數，僅保留缺資料診斷。僅使用截止前資訊。修訂資料與時點樣本分開；四季窗口可能重疊，樣本足量也不是統計驗證。'}
