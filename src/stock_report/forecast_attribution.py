"""Deterministic counterfactual bridges and diagnostics; no causal or valuation endorsement."""


def compare_backtests(previous, current):
    """Never attribute a changed sample mix to better forecast accuracy."""
    from .backtesting import METRICS
    from statistics import mean
    result = {}
    for label,old,new in (('point_in_time', previous, current),
                          ('revised_diagnostic', previous.get('revised_diagnostic', {}), current.get('revised_diagnostic', {}))):
        metrics = {}
        for metric in METRICS:
            def scored(group):
                return {str(row['origin']): row['errors'][metric] for row in group.get('rows', [])
                        if metric in row['errors'] and row['errors'][metric].get('error_percent') is not None}
            left,right = scored(old),scored(new)
            common = [key for key in sorted(left.keys() & right.keys()) if left[key]['actual'] == right[key]['actual']]
            metrics[metric] = {'previous_samples': len(left), 'current_samples': len(right),
                               'matched_samples': len(common), 'matched_origins': common,
                               'previous_matched_mape': mean(abs(left[key]['error_percent']) for key in common) if common else None,
                               'current_matched_mape': mean(abs(right[key]['error_percent']) for key in common) if common else None,
                               'previous_matched_bias': mean(left[key]['error_percent'] for key in common) if common else None,
                               'current_matched_bias': mean(right[key]['error_percent'] for key in common) if common else None}
        result[label] = metrics
    result['limitation'] = 'Only identical-origin, identical-actual samples are compared. Revised-data results and tiny overlapping samples cannot establish statistical reliability.'
    return result


def revenue_bridge(anchor, previous):
    base = anchor['base']['revenue_ttm']['value']
    growth = previous['drivers']['revenue_growth'].get('input_value', previous['drivers']['revenue_growth']['value'])
    quarters = previous.get('quarterly_forecast', [])
    uniform = base * (1 + growth / 100)
    contributions = []
    history = anchor['series']['quarters']['revenue'][-4:]
    for q,p in zip(quarters, history):
        trend = p['value'] * (1 + growth / 100)
        contributions.append({'start': q['start'], 'end': q['end'], 'prior_revenue': p['value'],
                              'uniform_yoy_projection': trend, 'reported_projection': q['statements']['revenue'],
                              'guidance_override': q['statements']['revenue'] - trend,
                              'source': q['revenue_origin']})
    actual = previous['statements']['revenue']
    return {'prior_ttm': base, 'latest_two_yoy_percent': growth, 'uniform_yoy_ntm': uniform,
            'guidance_override_total': sum(q['guidance_override'] for q in contributions),
            'guidance_override_growth_pp': (actual - uniform) / base * 100,
            'ntm_growth_percent': (actual / base - 1) * 100, 'quarters': contributions,
            'not_compounded': True, 'calendar_dollar_effect': 0,
            'explanation': 'Each prior-year quarter was multiplied once by the same YoY rate. This repeats recent exceptional growth, not quarterly compounding. Raw prior-year shape combines seasonality and trend. Calendar changes alter dates, not this dollar identity.'}


def change_report(anchor, previous, current):
    old, new = previous['statements'], current['statements']
    fields = sorted(set(old) & set(new))
    changes = {key: {'before': old[key], 'after': new[key],
                     'change': new[key] - old[key] if isinstance(old[key], (int,float)) and isinstance(new[key], (int,float)) else None}
               for key in fields if isinstance(old[key], (int,float)) or isinstance(new[key], (int,float)) or key in ('operating_cash_flow','free_cash_flow')}
    # Exact sequential EPS counterfactuals: the ordering is explicit, not an assertion of causation.
    def eps(revenue, margin, nonop, tax, shares):
        return revenue * (margin + nonop) / 100 * (1 - tax/100) / shares if shares and all(v is not None for v in (revenue,margin,nonop,tax)) else None
    old_d, new_d = previous['drivers'], current['drivers']
    args = [old['revenue'], old['operating_margin'], old_d['non_operating_ratio']['value'], old_d['tax_rate']['value'], old['diluted_shares']]
    targets = [new['revenue'], new['operating_margin'], new_d['non_operating_ratio']['value'], new_d['tax_rate']['value'], new['diluted_shares']]
    bridge, prior = [], eps(*args)
    for i,name in enumerate(('revenue_path','operating_margin','non_operating_items','tax','shares')):
        args[i] = targets[i]
        value = eps(*args)
        bridge.append({'factor': name, 'eps_effect': value - prior if value is not None and prior is not None else None})
        prior = value
    old_m, new_m = previous['valuation']['multiple'], current['valuation']['multiple']
    price_bridge = {'eps_effect_at_old_multiple': (new['eps'] - old['eps']) * old_m if old['eps'] is not None and new['eps'] is not None else None,
                    'multiple_effect_at_new_eps': new['eps'] * (new_m - old_m) if new['eps'] is not None else None,
                    'basis': 'hypothetical_price_sensitivity_not_fair_value'}
    inflation = revenue_bridge(anchor, previous)
    guided = next((q for q in previous.get('quarterly_forecast', []) if q['revenue_origin']['method'] == 'management_guidance_midpoint'), None)
    if guided:
        annualized = (guided['statements']['revenue'] * 4 / inflation['prior_ttm'] - 1) * 100
        inflation['earlier_annualization_growth_percent'] = annualized
        inflation['growth_method_substitution_pp'] = inflation['latest_two_yoy_percent'] - annualized
        inflation['total_inflation_vs_annualization_pp'] = inflation['ntm_growth_percent'] - annualized
    formulas = {'revenue': 'sum of four period-specific revenue projections',
                'operating_income': 'sum of revenue_q * operating_margin_q',
                'pretax_income': 'sum of operating_income_q + revenue_q * non_operating_ratio',
                'net_income': 'sum of pretax_q * (1 - tax_rate)', 'eps': 'NTM net income / projected diluted shares',
                'capital_expenditure': 'sum of revenue_q * capex_intensity',
                'depreciation_amortization': 'sum of revenue_q * DA_intensity',
                'free_cash_flow': 'unavailable until cash-flow statement bridge reconciles; not set to zero',
                'operating_cash_flow': 'unavailable until cash-flow statement bridge reconciles; not set to zero'}
    for key, entry in changes.items():
        entry['derivation'] = formulas.get(key, 'derived from the four-quarter financial statements; audit driver inputs retained')
    growth_change = new['revenue_growth'] - old['revenue_growth']
    eps_change = (new['eps'] / old['eps'] - 1) * 100 if new.get('eps') is not None and old.get('eps') else None
    eps_residual = new['eps'] - old['eps'] - sum(r['eps_effect'] for r in bridge) if all(r['eps_effect'] is not None for r in bridge) and new.get('eps') is not None and old.get('eps') is not None else None
    return {'inflation_diagnosis': inflation, 'financial_changes': changes,
            'eps_sequential_bridge': bridge, 'valuation_change_bridge': price_bridge,
            'main_drivers': ['Repeated YoY extrapolation removed; future comparisons use a fixed reported level.',
                             'No unsupported seasonal multiplier or fitted decay coefficient is introduced.',
                             'TTM margin replaces latest-two-quarter margin plus past seasonal shifts.',
                             'Guidance remains restricted to its actual quarter; high supported guidance is never capped.'],
            'confidence': 'mechanical_scenario', 'display_precision': 'two_significant_digits',
            'checks': {'forecast_version_change': {'status': 'WARNING' if abs(growth_change) >= 10 or (eps_change is not None and abs(eps_change) >= 20) else 'PASS',
                                                   'growth_change_pp': growth_change, 'eps_change_percent': eps_change,
                                                   'threshold_purpose': 'Review at 10pp growth or 20% EPS change; never a forecast cap.'},
                       'unexplained_eps_change': {'status': 'NOT_APPLICABLE' if eps_residual is None else 'PASS' if abs(eps_residual) < 1e-6 else 'FAIL',
                                                  'unexplained_difference': eps_residual}},
            'limitation': 'A level reference is not a claim of zero economic growth or a statistically estimated mean reversion. High-base YoY changes arise from denominators. Audit calculations retain precision for reproduction.'}
