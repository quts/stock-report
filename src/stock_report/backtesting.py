"""Rolling-origin full-forecast backtest. No fallback from PIT into revised data.

The origin cutoff rule is shared with the candidate retrospective (`origin_release`): the
earliest preserved version of the origin quarter, used only when it precedes the end of the
first target quarter. Every evaluated row carries the exact vintage of its origin version; the
catalog-verified subset is scored separately. Each metric is also compared, on the very same
rows and actuals, with simple same-metric baselines computed from the same point-in-time data.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import mean
from . import fin_series, fin_analysis, forecast
from .policy import reportable
from .error_metrics import summarize

METRICS = ('revenue', 'operating_income', 'eps', 'operating_cash_flow', 'free_cash_flow')
MIN_SAMPLES = 8  # A minimum disclosure floor, never a statistical validation criterion.
# Same-metric naive baselines, computed only from data visible at the cutoff:
#   ttm_carry_forward:  NTM = trailing four quarters at the origin (the seasonal-naive sum)
#   latest_quarter_x4:  NTM = 4 x the origin quarter (the latest-level rule)
BASELINE_RULES = ('ttm_carry_forward', 'latest_quarter_x4')


def origin_release(data, entity_id, origin):
    """Earliest preserved clock of the origin quarter's revenue, shared by both retrospectives."""
    rows = [e for e in data.evidence if e.entity_id == entity_id and e.kind == 'financial'
            and e.financial_metric == 'revenue' and e.statement_kind == 'fact' and e.value is not None
            and e.period_end == origin and reportable(e, data.request)
            and (e.publication_basis != 'observed_availability' or e.availability_observed_at is not None)]
    if not rows:
        return {'clock': None, 'evidence_id': None}
    def available(e):
        clocks = [e.published_at, e.event_time]
        if e.publication_basis == 'observed_availability':
            clocks.append(e.availability_observed_at)
        return max(clocks)
    first = min(rows, key=lambda e: (available(e), e.evidence_id))
    return {'clock': available(first), 'evidence_id': first.evidence_id}


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


def _metric_stats(valid):
    relative = [v['error_percent'] for v in valid if v['error_percent'] is not None]
    return {**summarize(valid),
            'mape_percent': mean(abs(v) for v in relative) if relative else None,
            'bias_percent': mean(relative) if relative else None,
            'percentage_sample_size': len(relative)}


def verified_metric_rows(rows):
    """Mask unverified metrics without shrinking the original coverage denominator."""
    result = []
    for row in rows:
        errors = {m: (v if row['origin_availability_verified'] and row['metric_input_verification'][m]['verified'] else
                          {**v, 'predicted': None, 'error_percent': None, 'unavailable_reason': 'unverified_contributing_input'})
                      for m, v in row['errors'].items()}
        result.append({**row, 'errors': errors})
    return result


def scores(rows):
    out = {}
    for name in METRICS:
        valid = [r['errors'][name] for r in rows if r['errors'][name]['predicted'] is not None and r['errors'][name]['actual'] is not None]
        out[name] = {'sample_size': len(valid), 'eligible_origins': len(rows),
                     'coverage': len(valid) / len(rows) if rows else 0, **_metric_stats(valid)}
    return out


def baseline_comparison(rows):
    """Forecast versus each same-metric baseline on identical rows and actuals (matched pairs)."""
    out = {}
    for name in METRICS:
        out[name] = {}
        for rule in BASELINE_RULES:
            pairs = []
            for r in rows:
                f, b = r['errors'][name], (r.get('baselines') or {}).get(rule, {}).get(name)
                if f['predicted'] is None or f['actual'] is None or not b or b['predicted'] is None:
                    continue
                pairs.append((f, {**b, 'actual': f['actual'],
                                  'error_percent': (b['predicted'] - f['actual']) / abs(f['actual']) * 100 if f['actual'] else None}))
            available = sum(1 for r in rows if ((r.get('baselines') or {}).get(rule, {}).get(name) or {}).get('predicted') is not None)
            fs, bs = _metric_stats([p[0] for p in pairs]), _metric_stats([p[1] for p in pairs])
            diffs = [abs(b['error_percent']) - abs(f['error_percent']) for f, b in pairs
                     if f['error_percent'] is not None and b['error_percent'] is not None]
            out[name][rule] = {'matched_samples': len(pairs), 'baseline_available_rows': available,
                               'forecast': fs, 'baseline': bs,
                               'mae_skill': 1 - fs['mae'] / bs['mae'] if bs['mae'] else None,
                               'wape_improvement_pp': bs['wape_percent'] - fs['wape_percent'] if bs['wape_percent'] is not None else None,
                               'mape_improvement_pp': (bs['mape_percent'] - fs['mape_percent'])
                               if fs['mape_percent'] is not None and bs['mape_percent'] is not None else None,
                               'forecast_lower_error': sum(d > 0 for d in diffs),
                               'baseline_lower_error': sum(d < 0 for d in diffs)}
    return out


def _baselines(series, origin):
    """NTM baselines per metric from the point-in-time series only; none when the origin is not visible."""
    out = {rule: {} for rule in BASELINE_RULES}
    for metric in METRICS:
        source = 'diluted_eps' if metric == 'eps' else metric
        if metric == 'free_cash_flow':
            ocf, capex = fin_series.ttm(series, 'operating_cash_flow'), fin_series.ttm(series, 'capital_expenditure')
            ok = ocf and capex and ocf['end'] == capex['end'] == origin and ocf['start'] == capex['start']
            out['ttm_carry_forward'][metric] = {'predicted': ocf['value'] - capex['value'] if ok else None}
            q = {m: (series['quarters'].get(m) or [None])[-1] for m in ('operating_cash_flow', 'capital_expenditure')}
            okq = all(p and p['end'] == origin for p in q.values())
            out['latest_quarter_x4'][metric] = {'predicted': 4 * (q['operating_cash_flow']['value'] - q['capital_expenditure']['value']) if okq else None}
            continue
        t = fin_series.ttm(series, source)
        out['ttm_carry_forward'][metric] = {'predicted': t['value'] if t and t['end'] == origin else None}
        last = (series['quarters'].get(source) or [None])[-1]
        out['latest_quarter_x4'][metric] = {'predicted': 4 * last['value'] if last and last['end'] == origin else None}
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


def rolling_origin(data, entity_id, include_revised=True, dataset=None):
    from .retrospective import filing_dataset
    dataset = dataset or filing_dataset(data, entity_id)
    by_id = {r['evidence_id']: r for r in dataset['rows']}
    full = fin_series.build(data, entity_id)
    revenue = full['quarters'].get('revenue', [])
    rows, revised_rows = [], []
    for i in range(3, len(revenue) - 4):
        origin = revenue[i]['end']
        scheduled = datetime.combine(origin + timedelta(days=45), datetime.min.time(), timezone.utc)
        target = revenue[i+1:i+5]
        rel = origin_release(data, entity_id, origin)
        first_publication = rel['clock']
        origin_row = by_id.get(rel['evidence_id']) or {}
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
            used_ids = [e.evidence_id for e in observed.evidence if e.kind == 'financial']
            dependencies = {'revenue': (),
                            'operating_income': ('operating_margin',),
                            'eps': ('operating_margin', 'non_operating_ratio', 'tax_rate', 'share_change'),
                            'operating_cash_flow': tuple(a['drivers']), 'free_cash_flow': tuple(a['drivers'])}
            verified_inputs = {}
            for metric, names in dependencies.items():
                ids = {i for name in names for i in a['drivers'].get(name, {}).get('evidence_ids', [])}
                ids.update((a['base'].get('revenue_ttm') or {}).get('evidence_ids', []))
                ids.update(i for q in projected for i in q.get('revenue_origin', {}).get('evidence_ids', []))
                missing = [name for name in names if a['drivers'].get(name, {}).get('value') is None]
                if a['base'].get('revenue_ttm') is None:
                    missing.append('four_quarter_revenue_base')
                verified_inputs[metric] = {'evidence_ids': sorted(ids), 'missing_features': missing,
                                          'verified': not missing and bool(ids) and all(
                    by_id.get(i, {}).get('availability_verified', False) for i in ids)}
            dest.append({'origin': origin, 'information_cutoff': cutoff.isoformat(), 'target_start': target[0]['start'],
                         'target_end': end, 'vintage_status': 'revised_diagnostic' if revised else 'strict_point_in_time',
                         'cutoff_basis': 'first_preserved_origin_publication' if timely_origin else 'scheduled_diagnostic_no_original_vintage',
                         'first_preserved_publication': first_publication, 'diagnostics': diagnostic,
                         'origin_evidence_id': rel['evidence_id'], 'origin_vintage': origin_row.get('vintage'),
                         'origin_availability_verified': bool(timely_origin and origin_row.get('availability_verified')),
                         'metric_input_verification': verified_inputs,
                         'input_vintage_counts': dict(Counter(by_id[i]['vintage'] for i in used_ids if i in by_id)),
                         'training_evidence_ids': [e.evidence_id for e in observed.evidence],
                         'max_input_published_at': max((e.published_at for e in observed.evidence), default=None),
                         'max_input_event_time': max((e.event_time for e in observed.evidence), default=None), 'errors': errors,
                         'baselines': _baselines(series, origin)})
    metrics = scores(rows)
    verified_rows = verified_metric_rows(rows)
    root_counts = Counter(reason for row in rows for reason in row['diagnostics']['root_causes'])
    funnel = {'catalog_status': dataset['catalog']['status'], 'catalog_entries': dataset['catalog'].get('entries', 0),
              'financial_periods': len(revenue), 'origins_with_four_target_quarters': len(rows),
              'timely_origins': sum(r['cutoff_basis'] == 'first_preserved_origin_publication' for r in rows),
              'initialized_predictions': {m: sum(r['errors'][m]['predicted'] is not None for r in rows) for m in METRICS},
              'catalog_verified_origins': sum(r['origin_availability_verified'] for r in rows),
              'catalog_verified_original_origins': sum(r['origin_availability_verified'] and
                                                       r['origin_vintage'] == 'verified_original' for r in rows),
              'verified_feature_origins': {m: sum(r['origin_availability_verified'] and
                  r['metric_input_verification'][m]['verified'] for r in rows) for m in METRICS},
              'forecast_eligible_origins': {m: sum(r['origin_availability_verified'] and
                  r['metric_input_verification'][m]['verified'] and r['errors'][m]['predicted'] is not None
                  for r in rows) for m in METRICS},
              'verified_evaluated_samples': {m: scores(verified_rows)[m]['sample_size'] for m in METRICS},
              'note': 'Catalog entries, financial periods, initialized predictions and verified evaluated samples are different counts.'}
    return {'status': 'insufficient', 'method': 'strict_point_in_time', 'reporting_lag_days': 45,
            'horizon_basis': 'four_fiscal_quarters_after_last_reported_period; legacy_ntm_keys_are_F4Q',
            'root_cause_counts': dict(root_counts),
            'cutoff_policy': 'first_preserved_origin_filing_publication (shared origin_release rule); scheduled45_only_for_unavailable_vintage_diagnostics',
            'fcf_test_count': metrics['free_cash_flow']['sample_size'], 'rows': rows, 'metrics': metrics,
            'verified_metrics': scores(verified_rows), 'funnel': funnel,
            'origin_vintage_counts': dict(Counter(r['origin_vintage'] or 'absent' for r in rows)),
            'baseline_comparison': baseline_comparison(rows),
            'verified_baseline_comparison': baseline_comparison(verified_rows),
            'baseline_rules': {'ttm_carry_forward': 'NTM = trailing four quarters visible at the cutoff',
                               'latest_quarter_x4': 'NTM = 4 x origin quarter visible at the cutoff'},
            'revised_diagnostic': {'method': 'revised_historical_data_not_point_in_time',
                                   'rows': revised_rows, 'metrics': scores(revised_rows)},
            'reliability': 'not_validated', 'minimum_sample_floor': MIN_SAMPLES,
            'limitation': '以保留的首次申報公布時間為截止；若只有晚於預測季的比較數，僅保留缺資料診斷。僅使用截止前資訊。修訂資料與時點樣本分開；四季窗口可能重疊，樣本足量也不是統計驗證。'}
