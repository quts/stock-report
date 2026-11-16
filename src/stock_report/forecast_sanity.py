"""Review signals and attribution, never numerical caps or statistical validation."""


def assess(anchor, scenario):
    statement = scenario.get('statements')
    if not statement:
        return {'status': 'unavailable', 'signals': ['forecast_initialization_incomplete']}
    history = anchor['series']['quarters'].get('revenue', [])
    rev0 = anchor['base']['revenue_ttm']
    latest = history[-1] if history else None
    quarters = scenario.get('quarterly_forecast', [])
    free = [q for q in quarters if q['revenue_origin']['method'] != 'management_guidance_midpoint']
    sensitivity = None
    if len(history) >= 2 and rev0 and any(q.get('growth_policy') == 'level_carry_forward' for q in quarters):
        alternative = (history[-1]['value'] + history[-2]['value']) / 2
        delta = len(free) * (alternative - latest['value'])
        sensitivity = {'method': 'replace_latest_level_with_latest_two_mean; guidance_unchanged',
                       'applies_to_policy': 'level_carry_forward', 'latest_level': latest['value'],
                       'alternative_level': alternative, 'ntm_revenue_effect': delta,
                       'ntm_growth_effect_pp': delta / rev0['value'] * 100,
                       'evidence_ids': sorted({i for p in history[-2:] for i in p['evidence_ids']}),
                       'limitation': 'A mechanical sensitivity, not a confidence interval or a more accurate estimate.'}
    rates = []
    from .fin_series import year_ago
    for p in history:
        prior = year_ago(history, p)
        if prior and prior['value']:
            rates.append((p['value'] / prior['value'] - 1) * 100)
    forecast_growth = statement['revenue_growth']
    acceleration = forecast_growth - rates[-1] if rates else None
    above_history = bool(rates and forecast_growth > max(rates))
    divergence = []
    for q in quarters:
        origin = q['revenue_origin']
        if origin['method'] == 'management_guidance_midpoint' and latest and latest['value']:
            divergence.append({'period_end': str(q['end']), 'guidance_revenue': q['statements']['revenue'],
                               'no_new_growth_level_reference': latest['value'],
                               'difference_percent': (q['statements']['revenue'] / latest['value'] - 1) * 100,
                               'evidence_ids': origin['evidence_ids'],
                               'interpretation': 'Actual-period guidance overrides the reference only here; no future-quarter guidance is inferred.'})
    expected_eps = statement['net_income'] / statement['diluted_shares'] if statement.get('net_income') is not None and statement.get('diluted_shares') else None
    eps_gap = statement['eps'] - expected_eps if statement.get('eps') is not None and expected_eps is not None else None
    signals = []
    if above_history:
        signals.append('forecast_growth_above_observed_history; review_evidence_not_cap_growth')
    if sensitivity and abs(sensitivity['ntm_growth_effect_pp']) >= 10:
        signals.append('latest_quarter_level_sensitivity_over_10pp; review_threshold_not_cap')
    if eps_gap is not None and abs(eps_gap) > 1e-6 * max(1,abs(expected_eps)):
        signals.append('unexplained_eps_arithmetic')
    return {'status': 'review_required' if signals else 'computed_not_validated', 'signals': signals,
            'growth_acceleration_pp': acceleration, 'forecast_above_historical_yoy_range': above_history,
            'latest_two_quarter_sensitivity': sensitivity, 'guidance_divergence': divergence,
            'eps_identity_gap': eps_gap, 'precision': 'two_significant_digits',
            'confidence': scenario['confidence_class'],
            'note': 'Review thresholds do not reject supported high growth. Lack of PIT evidence does not remove computable scenarios.'}
