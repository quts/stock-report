"""Shared quarterly revenue candidate paths and the forecast-confidence hierarchy.

A candidate is a transparent rule that maps reported quarterly revenue (up to an origin) to the
next four quarters. Candidates are conditional paths, not predictions: no candidate is selected
unless point-in-time walk-forward evidence on identical origins supports it. Management guidance
replaces a candidate only in the quarter it actually covers. Seasonality is diagnosed, never
assumed: one trend-contaminated cycle does not establish a seasonal pattern.
"""
from statistics import mean, median
from .fin_series import contiguous_tail

# Ordered from least to most assumption-laden. `family` tells the reader what each rule assumes.
SPECS = {
    'seasonal_naive': {'family': 'baseline', 'min_history': 4, 'label': '去年同季水準',
                       'formula': 'R[t+h] = R[t+h-4]',
                       'assumes': '各季回到去年同季水準（非引導季度年增 0%）；把去年形狀（季節性＋趨勢）原樣重複'},
    'level_carry_forward': {'family': 'baseline', 'min_history': 1, 'label': '最新季水準延續',
                            'formula': 'R[t+h] = R[t]',
                            'assumes': '各季維持最新已報水準；不另加成長，也不假設季節性'},
    'trailing_mean_level': {'family': 'normalization', 'min_history': 2, 'label': '近兩季平均水準',
                            'formula': 'R[t+h] = (R[t] + R[t-1]) / 2',
                            'assumes': '最新一季可能異常，以近兩季平均作水準；不另加成長'},
    'drift': {'family': 'trend', 'min_history': 5, 'label': '近四季平均季增延續',
              'formula': 'R[t+h] = R[t] + h * (R[t] - R[t-4]) / 4',
              'assumes': '過去四季的平均單季增加額持續；為線性動能外推，不是需求證據'},
    'seasonal_yoy_trend': {'family': 'trend', 'min_history': 8, 'label': '去年同季×近四季年增中位數',
                           'formula': 'R[t+h] = R[t+h-4] * (1 + median(YoY[t-3..t]))',
                           'assumes': '近期年增率延續於去年同季；高成長期會重複例外成長'},
}
CANDIDATES = tuple(SPECS)
BASELINES = ('seasonal_naive', 'level_carry_forward')
HIERARCHY = ('mechanical_sensitivity', 'evidence_supported_forecast', 'backtested_forecast')
HIERARCHY_LABELS = {'mechanical_sensitivity': '機械敏感度', 'evidence_supported_forecast': '證據支持的預測',
                    'backtested_forecast': '經時點回測的預測'}
# Hierarchy levels apply to the revenue path only. There is deliberately no alias to the legacy
# `validated_forecast` class: margins, tax, shares, multiples and cash flow stay reference inputs.
HIERARCHY_SCOPE = 'revenue_path_only'
COVERAGE_FLOOR = 0.8


def history_tail(points, count):
    """The latest `count` contiguous single quarters, or None."""
    return contiguous_tail(points, count)


DEFAULT_WINDOWS = {'drift': 4, 'trailing_mean_level': 2, 'seasonal_yoy_trend': 4}


def min_history(name, window=None):
    w = window or DEFAULT_WINDOWS.get(name)
    return {'drift': (w or 4) + 1, 'trailing_mean_level': w or 2, 'seasonal_yoy_trend': 4 + (w or 4)}.get(name, SPECS[name]['min_history'])


def path(name, points, horizon=4, window=None):
    """Revenue values for h=1..horizon from contiguous history ending at the origin.

    `window` changes only the training window of drift, trailing mean or YoY median, for
    stability checks; the default reproduces the published candidate definitions."""
    w = window or DEFAULT_WINDOWS.get(name)
    need = min_history(name, window)
    tail = history_tail(points, need)
    if tail is None:
        available = 0
        for n in range(len(points), 0, -1):
            if history_tail(points, n):
                available = n
                break
        return {'status': 'not_applicable', 'values': None, 'evidence_ids': [],
                'reason': f'需要 {need} 個連續季度，只有 {available} 個'}
    v = [p['value'] for p in tail]
    ids = sorted({i for p in tail for i in p['evidence_ids']})
    if name == 'level_carry_forward':
        values = [v[-1]] * horizon
    elif name == 'trailing_mean_level':
        values = [sum(v[-w:]) / w] * horizon
    elif name == 'seasonal_naive':
        values = [v[-4 + (h % 4)] for h in range(horizon)]
    elif name == 'drift':
        step = (v[-1] - v[-1 - w]) / w
        values = [v[-1] + (h + 1) * step for h in range(horizon)]
    else:
        yoy = [v[i] / v[i - 4] - 1 for i in range(len(v) - w, len(v)) if v[i - 4] > 0]
        if len(yoy) != w:
            return {'status': 'not_applicable', 'values': None, 'evidence_ids': [], 'reason': '比較基期非正值'}
        g = median(yoy)
        values = [v[-4 + (h % 4)] * (1 + g) for h in range(horizon)]
    if any(x is None or x < 0 for x in values):
        return {'status': 'not_applicable', 'values': None, 'evidence_ids': ids, 'reason': '規則產生負營收'}
    return {'status': 'applicable', 'values': values, 'evidence_ids': ids, 'reason': None,
            'training_window': [tail[0]['start'], tail[-1]['end']]}


def seasonality(points):
    """Ratio-to-centred-moving-average diagnostic. Never reports a verified seasonal pattern."""
    tail = None
    for n in range(len(points), 0, -1):
        tail = history_tail(points, n)
        if tail:
            break
    tail = tail or []
    ratios = {}
    for i in range(2, len(tail) - 2):
        window = [p['value'] for p in tail[i - 2:i + 3]]
        centred = (window[0] / 2 + sum(window[1:4]) + window[4] / 2) / 4
        if centred > 0:
            # Position within the fiscal year relative to the latest reported quarter.
            ratios.setdefault((len(tail) - 1 - i) % 4, []).append(tail[i]['value'] / centred)
    cycles = min((len(r) for r in ratios.values()), default=0) if len(ratios) == 4 else 0
    status = 'insufficient_history'
    if cycles >= 2:
        same_side = all(all(x > 1 for x in r) or all(x < 1 for x in r) for r in ratios.values())
        status = 'pattern_repeated_not_validated' if same_side else 'not_consistent_across_cycles'
    return {'status': status, 'detrended_complete_cycles': cycles, 'contiguous_quarters': len(tail),
            'ratios_by_position': {str(k): v for k, v in sorted(ratios.items())},
            'method': 'ratio to centred 2x4 moving average; position 0 = latest reported quarter',
            'limitation': '少於兩個去趨勢完整循環時，不能把去年同季形狀當作季節性；即使重複出現也不是統計驗證。'}


def overlay_guidance(windows, values, guidance, fx_rate=None):
    """Replace a candidate value only for the quarter a verified quarterly guidance covers."""
    rows, used, rejected = [], [], []
    for w, value in zip(windows, values):
        applicable = []
        for g in guidance:
            if g['metric'] != 'revenue' or g['target_period_end'] != w['end'].isoformat():
                continue
            if (not g.get('quarterly_period_verified') or g['bucket'] != 'reported' or
                    (g.get('declared_start') and g['declared_start'] != w['start'].isoformat())):
                rejected.append({'evidence_ids': g['evidence_ids'], 'reason': 'guidance_period_or_basis_unverified'})
                continue
            conversion = 1 if g['currency'] == w['prior']['currency'] else fx_rate
            if not conversion or conversion <= 0:
                rejected.append({'evidence_ids': g['evidence_ids'], 'reason': 'missing_explicit_fx'})
                continue
            applicable.append((g, conversion))
        row = {'start': w['start'], 'end': w['end'], 'raw_value': value, 'value': value, 'guided': False}
        if applicable:
            latest_date = max(g['published'] for g, _ in applicable)
            latest = [(g, f) for g, f in applicable if g['published'] == latest_date]
            mids = [(g['low'] + g['high']) / 2 * f for g, f in latest]
            if max(mids) - min(mids) > max(mids) * 0.005:
                rejected.append({'reason': 'conflicting_guidance', 'evidence_ids': [i for g, _ in latest for i in g['evidence_ids']]})
            else:
                g, f = latest[0]
                row.update(value=mids[0], guided=True, guidance_evidence_ids=g['evidence_ids'], fx_rate=f,
                           guidance_vs_rule_percent=(mids[0] / value - 1) * 100 if value else None)
                used.append({'evidence_ids': g['evidence_ids'], 'period_end': w['end'].isoformat()})
        rows.append(row)
    return rows, used, rejected


def build(anchor, guidance=(), fx_rate=None):
    """All candidate NTM paths at the latest origin, with the reference income structure."""
    from .forecast import quarter_windows
    series, base = anchor['series'], anchor['base']
    points = series['quarters'].get('revenue', [])
    windows = quarter_windows(series)
    ttm = base['revenue_ttm']
    margin = anchor['drivers']['operating_margin']['value']
    nonop = anchor['drivers']['non_operating_ratio']['value']
    tax = anchor['drivers']['tax_rate']['value']
    shares = (base.get('shares') or {}).get('value')
    out = {}
    for name, spec in SPECS.items():
        p = path(name, points)
        row = {'name': name, 'label': spec['label'], 'family': spec['family'], 'formula': spec['formula'],
               'assumes': spec['assumes'], 'min_history': spec['min_history'], 'status': p['status'],
               'reason': p['reason'], 'evidence_ids': p['evidence_ids'], 'hierarchy': 'mechanical_sensitivity',
               'hierarchy_scope': HIERARCHY_SCOPE, 'deployed_method': 'raw_rule',
               'is_baseline': name in BASELINES, 'quarters': [], 'ntm_revenue': None, 'ntm_growth_percent': None,
               'reference_eps': None}
        if p['status'] == 'applicable' and len(windows) == 4 and ttm:
            quarters, used, rejected = overlay_guidance(windows, p['values'], guidance, fx_rate)
            row.update(quarters=quarters, guidance_used=used, guidance_rejected=rejected,
                       training_window=p['training_window'])
            row['ntm_revenue'] = sum(q['value'] for q in quarters)
            row['ntm_growth_percent'] = (row['ntm_revenue'] / ttm['value'] - 1) * 100
            row['latest_quarter_change_percent'] = [(q['value'] / points[-1]['value'] - 1) * 100 for q in quarters]
            if None not in (margin, nonop, tax) and shares:
                # Reference structure: TTM margins, tax and latest diluted shares; a sensitivity anchor only.
                row['reference_eps'] = row['ntm_revenue'] * (margin + nonop) / 100 * (1 - tax / 100) / shares
                row['reference_structure'] = {'operating_margin': margin, 'non_operating_ratio': nonop,
                                              'tax_rate': tax, 'diluted_shares': shares}
            guided = [q for q in quarters if q['guided']]
            # The evaluated method must match what is deployed: a guided path is a different method.
            row['deployed_method'] = 'guidance_overlay' if guided else 'raw_rule'
            if guided and all(q['guided'] for q in quarters):
                row['hierarchy'] = 'evidence_supported_forecast'
            row['quarter_hierarchy'] = ['evidence_supported_forecast' if q['guided'] else 'mechanical_sensitivity' for q in quarters]
        out[name] = row
    applicable = [r for r in out.values() if r['ntm_revenue'] is not None]
    spread = None
    if applicable:
        low, high = min(applicable, key=lambda r: r['ntm_revenue']), max(applicable, key=lambda r: r['ntm_revenue'])
        spread = {'low': low['name'], 'high': high['name'], 'low_revenue': low['ntm_revenue'], 'high_revenue': high['ntm_revenue'],
                  'range_percent_of_ttm': (high['ntm_revenue'] - low['ntm_revenue']) / ttm['value'] * 100 if ttm else None}
    return {'candidates': out, 'spread': spread, 'seasonality': seasonality(points),
            'horizon_basis': 'four_fiscal_quarters_after_last_reported_period; legacy_ntm_keys_are_F4Q',
            'horizon': anchor.get('horizon'), 'selected': None,
            'selection_status': 'no_candidate_selected_insufficient_point_in_time_evidence',
            'hierarchy_levels': list(HIERARCHY),
            'note': '候選路徑是條件式機械敏感度；沒有時點回測證據時不選出「最可能」路徑。公司指引只覆蓋其實際季度。'}


def promotion_check(row, pit, min_samples):
    """Every prerequisite for a backtested revenue path; a count plus a lower MAPE is never enough."""
    failed = []
    if row['is_baseline']:
        failed.append('baseline_rule_is_a_benchmark_not_a_candidate')
    if row['status'] != 'applicable':
        failed.append('rule_not_applicable_at_latest_origin')
    if (pit.get('integrity') or {}).get('status') != 'pass':
        failed.append('point_in_time_integrity_not_established')
    method = row.get('deployed_method', 'raw_rule')
    # Only origins whose filing identity and date are catalog-verified can promote a path; the
    # preserved-clock subset stays a diagnostic because its vintages are inferred.
    evaluated = (pit.get('pairwise_by_method') or {}).get(method, {}).get(row['name'], {})
    comparisons = (pit.get('verified_pairwise_by_method') or {}).get(method, {}).get(row['name'], {})
    if not evaluated:
        failed.append(f'deployed_method_{method}_not_evaluated_point_in_time')
    elif not comparisons or not any(c.get('matched_ntm_samples') for c in comparisons.values()):
        failed.append('origin_vintages_not_catalog_verified')
    for base in BASELINES:
        c = comparisons.get(base)
        if base == row['name'] or not comparisons:
            continue
        if c is None or c.get('method') != method:
            failed.append(f'method_mismatch_vs_{base}')
            continue
        if c['matched_ntm_samples'] < min_samples:
            failed.append(f'matched_samples_below_floor_vs_{base}')
        if c.get('ntm_coverage', 0) < COVERAGE_FLOOR:
            failed.append(f'coverage_below_floor_vs_{base}')
        if c['candidate_ntm_mape'] is None or c['baseline_ntm_mape'] is None or c['candidate_ntm_mape'] >= c['baseline_ntm_mape']:
            failed.append(f'not_lower_error_than_{base}_on_identical_origins')
        matched = c.get('ntm', {})
        if matched.get('mae_skill') is not None and matched['mae_skill'] <= 0:
            failed.append(f'not_lower_mae_than_{base}_on_identical_origins')
    return {'eligible': not failed, 'failed_prerequisites': failed, 'evaluated_method_required': method,
            'scope': HIERARCHY_SCOPE}


def select(candidates, evaluation, min_samples):
    """Promote a revenue path only when every prerequisite holds on point-in-time evidence.

    Prerequisites: point-in-time integrity, the deployed method (raw rule or guidance overlay)
    evaluated as deployed, identical origins and actuals against every baseline, sample and
    coverage floors, and lower error than every baseline. Revised-data retrospectives never
    promote. Promotion is revenue-path-only and never validates margins, multiples or a price.
    """
    pit = (evaluation or {}).get('point_in_time', {})
    promoted = []
    for name, row in candidates['candidates'].items():
        row['promotion_check'] = promotion_check(row, pit, min_samples)
        if row['promotion_check']['eligible']:
            promoted.append(name)
    for name in promoted:
        candidates['candidates'][name]['hierarchy'] = 'backtested_forecast'
    if len(promoted) == 1:
        candidates['selected'] = promoted[0]
        candidates['selection_status'] = 'revenue_path_selected_by_point_in_time_walk_forward_not_statistical_validation'
    elif promoted:
        candidates['selection_status'] = 'multiple_candidates_backtested_no_unique_selection'
    return candidates


def summary_mape(values):
    return mean(values) if values else None
