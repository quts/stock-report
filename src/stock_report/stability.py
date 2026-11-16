"""Stability of candidate paths and reference EPS under explicit, reproducible perturbations.

Each perturbation changes one input: drop the latest quarter (the horizon is rebased and the
comparison is flagged as a period mismatch), normalize one exceptional-growth quarter, remove or
move guidance within its own range, change training windows, or remove one data source.
Thresholds flag changes for review only; they never cap or tune a scenario.
"""
from statistics import median
from . import candidates, fin_analysis, fin_series, forecast

MATERIAL_EPS_PERCENT = 10.0
MATERIAL_GROWTH_PP = 5.0


def _anchor(data, entity_id, series=None):
    series = series or fin_series.build(data, entity_id)
    if not series['quarters'].get('revenue'):
        return None
    return forecast.anchors(series, fin_analysis.analyse(data, series, entity_id))


def _paths(anchor, guidance, fx_rate):
    if anchor is None or anchor['base']['revenue_ttm'] is None:
        return None
    built = candidates.build(anchor, guidance, fx_rate)
    return {'horizon': anchor['horizon'], 'ttm_revenue': anchor['base']['revenue_ttm']['value'],
            'candidates': {n: {'ntm_revenue': r['ntm_revenue'], 'ntm_growth_percent': r['ntm_growth_percent'],
                               'reference_eps': r['reference_eps'], 'status': r['status'],
                               'quarters': [{'end': q['end'], 'value': q['value'], 'guided': q['guided']} for q in r['quarters']]}
                           for n, r in built['candidates'].items()}}


def _compare(baseline, variant, explanation):
    if variant is None:
        return {'status': 'unavailable', 'explanation': explanation}
    same = baseline['horizon'] == variant['horizon']
    rows = {}
    for name, b in baseline['candidates'].items():
        v = variant['candidates'].get(name, {})
        row = {'baseline_ntm_revenue': b['ntm_revenue'], 'variant_ntm_revenue': v.get('ntm_revenue'),
               'baseline_eps': b['reference_eps'], 'variant_eps': v.get('reference_eps'), 'variant_status': v.get('status'),
               'comparable': same}
        # Different horizons are not comparable: no NTM or EPS deltas, only identical-quarter overlaps.
        if same and b['ntm_revenue'] is not None and v.get('ntm_revenue') is not None:
            row['growth_change_pp'] = v['ntm_growth_percent'] - b['ntm_growth_percent']
        if same and b['reference_eps'] and v.get('reference_eps') is not None:
            row['eps_change_percent'] = (v['reference_eps'] / b['reference_eps'] - 1) * 100
        # Only quarters with identical end dates are compared like for like.
        common = {q['end']: q['value'] for q in b['quarters']}
        row['overlapping_quarters'] = [{'end': q['end'], 'baseline': common[q['end']], 'variant': q['value'],
                                        'change_percent': (q['value'] / common[q['end']] - 1) * 100 if common[q['end']] else None}
                                       for q in v.get('quarters', []) if q['end'] in common]
        row['material'] = bool(abs(row.get('eps_change_percent') or 0) >= MATERIAL_EPS_PERCENT or
                               abs(row.get('growth_change_pp') or 0) >= MATERIAL_GROWTH_PP)
        rows[name] = row
    return {'status': 'computed', 'same_forecast_period': same,
            'period_mismatch_flag': None if same else f'noncomparable horizons: {baseline["horizon"]["label"]} vs {variant["horizon"]["label"]}',
            'candidates': rows, 'material_candidates': sorted(n for n, r in rows.items() if r['material']),
            'explanation': explanation}


def _drop(data, keep):
    return data.model_copy(update={'evidence': [e for e in data.evidence if keep(e)]})


def _guidance_for(data, entity_id, fx_rate, fx_ids):
    """Guidance and FX are recomputed from the reduced evidence; nothing removed survives hidden."""
    guidance = forecast.guidance_records(data, entity_id)
    present = {e.evidence_id for e in data.evidence}
    fx = fx_rate if fx_rate and (not fx_ids or all(i in present for i in fx_ids)) else None
    return guidance, fx


def summarize(tests):
    """Quantified, same-period sensitivity per rule; no rule is chosen from these numbers."""
    windows = {}
    for key, v in (tests.get('training_windows') or {}).get('variants', {}).items():
        name = key.split('@')[0]
        if v.get('growth_change_pp') is not None:
            windows.setdefault(name, []).append((abs(v['growth_change_pp']), key, v['growth_change_pp']))
    window_rows = {n: {'variants': len(r), 'max_abs_growth_change_pp': max(r)[0], 'max_variant': max(r)[1],
                       'signed_changes_pp': {k: c for _, k, c in sorted(r, key=lambda x: x[1])},
                       'material': max(r)[0] >= MATERIAL_GROWTH_PP} for n, r in windows.items()}
    exceptional = tests.get('normalize_exceptional_quarter') or {}
    ex_rows = {n: {'growth_change_pp': r.get('growth_change_pp'), 'eps_change_percent': r.get('eps_change_percent'),
                   'material': r.get('material')}
               for n, r in (exceptional.get('candidates') or {}).items()}
    return {'training_window_sensitivity': window_rows,
            'exceptional_quarter': {'normalized_quarter': (exceptional.get('normalized_quarter') or {}).get('end'),
                                    'sequential_growth_percent': (exceptional.get('normalized_quarter') or {}).get('sequential_growth_percent'),
                                    'replacement_growth_percent': (exceptional.get('normalized_quarter') or {}).get('replacement_growth_percent'),
                                    'by_candidate': ex_rows,
                                    'material_candidates': sorted(n for n, r in ex_rows.items() if r['material'])},
            'out_of_sample_window_errors': 'see retrospective.point_in_time.window_sensitivity (identical keys, descriptive only)',
            'selection': 'none: the most optimistic, pessimistic or smoothest variant is never selected'}


def assess(data, entity_id, guidance=(), fx_rate=None, fx_evidence_ids=()):
    anchor = _anchor(data, entity_id)
    baseline = _paths(anchor, guidance, fx_rate)
    if baseline is None:
        return {'status': 'unavailable', 'tests': {}}
    tests = {}
    revenue = anchor['series']['quarters']['revenue']
    latest = revenue[-1]

    # 1. Exclude the latest reported quarter and everything first preserved with or after it, so
    # guidance released alongside that quarter does not leak in. Publication clocks are unchanged.
    release = min((e.published_at for e in data.evidence if e.entity_id == entity_id and e.kind == 'financial'
                   and e.period_end == latest['end']), default=None)
    excluded = _drop(data, lambda e: not (e.entity_id == entity_id and (
        (e.kind == 'financial' and e.period_end and e.period_end >= latest['end']) or
        (e.statement_kind == 'management_guidance' and release is not None and e.published_at >= release))))
    g, fx = _guidance_for(excluded, entity_id, fx_rate, fx_evidence_ids)
    variant = _paths(_anchor(excluded, entity_id), g, fx)
    result = _compare(baseline, variant, f'排除 {latest["end"]} 季，以及與該季同時或之後才保留的資料與指引；預測期間前移一季，不同期間不比較 NTM，只比較相同季度。')
    if variant:
        # The excluded quarter is the rebased path's first quarter: a single revised-data check, not a score.
        result['excluded_quarter_check'] = {
            name: {'predicted': r['quarters'][0]['value'], 'actual': latest['value'],
                   'error_percent': (r['quarters'][0]['value'] / latest['value'] - 1) * 100}
            for name, r in variant['candidates'].items() if r['quarters'] and r['quarters'][0]['end'] == latest['end']}
        result['excluded_quarter_check_note'] = 'One revised-data observation per rule; no ranking or reliability claim.'
    tests['exclude_latest_quarter'] = result

    # 2. Normalize the single largest sequential-growth quarter among the latest four.
    recent = fin_series.contiguous_tail(revenue, 8) or fin_series.contiguous_tail(revenue, 5)
    if recent:
        growth = [(recent[i]['value'] / recent[i - 1]['value'] - 1, i) for i in range(1, len(recent))]
        last_four = growth[-4:]
        rate, idx = max(last_four)
        others = [g for g, i in growth if i != idx]
        normal = median(others) if others else 0.0
        # A sensitivity-only copy: evidence and the published series keep the reported value.
        series = fin_series.build(data, entity_id)
        target = recent[idx]
        replaced = recent[idx - 1]['value'] * (1 + normal)
        series['quarters']['revenue'] = [{**p, 'value': replaced, 'reported_value': p['value'], 'derived': True,
                                          'sensitivity_only': True,
                                          'formula': 'stability: prior quarter * (1 + median other sequential growth)'}
                                         if p['end'] == target['end'] else p for p in series['quarters']['revenue']]
        result = _compare(baseline, _paths(_anchor(data, entity_id, series), guidance, fx_rate),
                          f'把 {target["end"]} 季（季增 {rate * 100:.1f}%）改為前一季 ×（1＋其他季增中位數 {normal * 100:.1f}%），其餘不變。')
        result['normalized_quarter'] = {'end': target['end'], 'reported': target['value'], 'normalized': replaced,
                                        'sensitivity_only': True, 'evidence_values_unchanged': True,
                                        'sequential_growth_percent': rate * 100, 'replacement_growth_percent': normal * 100,
                                        'evidence_ids': target['evidence_ids']}
        tests['normalize_exceptional_quarter'] = result

    # 3. Guidance: removed, or moved to the low/high end of its own published range.
    if guidance:
        tests['guidance_removed'] = _compare(baseline, _paths(anchor, (), fx_rate), '移除公司指引，所有季度回到候選規則。')
        for end, label in (('low', '下限'), ('high', '上限')):
            moved = [{**g, 'low': g[end], 'high': g[end]} for g in guidance]
            tests[f'guidance_{end}_end'] = _compare(baseline, _paths(anchor, moved, fx_rate), f'指引季度改用公司區間{label}，不外推其他季度。')

    # 4. Training windows for the rules that estimate a level or trend.
    windows = {}
    for name, alternatives in (('drift', (2, 8)), ('trailing_mean_level', (1, 4)), ('seasonal_yoy_trend', (2,))):
        base_row = baseline['candidates'][name]
        for w in alternatives:
            p = candidates.path(name, revenue, window=w)
            entry = {'window_quarters': w, 'status': p['status'], 'reason': p['reason'],
                     'default_window': candidates.DEFAULT_WINDOWS[name]}
            if p['status'] == 'applicable' and anchor['horizon']:
                rows, _, _ = candidates.overlay_guidance(forecast.quarter_windows(anchor['series']), p['values'], guidance, fx_rate)
                total = sum(r['value'] for r in rows)
                entry['ntm_revenue'] = total
                entry['ntm_growth_percent'] = (total / baseline['ttm_revenue'] - 1) * 100
                if base_row['ntm_revenue'] is not None:
                    entry['growth_change_pp'] = entry['ntm_growth_percent'] - base_row['ntm_growth_percent']
                    entry['material'] = abs(entry['growth_change_pp']) >= MATERIAL_GROWTH_PP
            windows[f'{name}@{w}'] = entry
    tests['training_windows'] = {'status': 'computed', 'variants': windows,
                                 'explanation': '只改變估計水準或趨勢的季度數；相同預測期間。'}

    # 5. Remove one data source at a time.
    sources = sorted({e.license_source_id for e in data.evidence if e.entity_id == entity_id and e.license_source_id
                      and (e.kind == 'financial' or e.statement_kind == 'management_guidance')})
    for source in sources:
        # Every record of the source goes (facts, guidance, FX support); identity records stay for rendering only.
        reduced = _drop(data, lambda e, s=source: e.license_source_id != s or e.kind == 'identity')
        g, fx = _guidance_for(reduced, entity_id, fx_rate, fx_evidence_ids)
        result = _compare(baseline, _paths(_anchor(reduced, entity_id), g, fx),
                          f'移除來源 {source} 的全部紀錄（含指引與匯率依據）後重建序列與指引。')
        result['guidance_records_after_removal'] = len(g)
        result['fx_after_removal'] = fx
        tests[f'source_removed:{source}'] = result
    material = sorted(k for k, t in tests.items() if t.get('material_candidates') or
                      any(v.get('material') for v in t.get('variants', {}).values()))
    return {'status': 'computed', 'baseline': baseline, 'tests': tests, 'material_tests': material,
            'summary': summarize(tests),
            'thresholds': {'eps_percent': MATERIAL_EPS_PERCENT, 'growth_pp': MATERIAL_GROWTH_PP,
                           'purpose': 'review flags only; never a cap, decay or target'},
            'limitation': '穩定性檢查只顯示規則對單一輸入的敏感度，不是信賴區間或預測能力證明。'}
