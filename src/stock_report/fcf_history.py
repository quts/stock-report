"""Nonpredictive historical free-cash-flow sensitivity and cash-flow mapping coverage.

Uses only verified, reported-basis operating cash flow, capital expenditure and revenue for the
same window. It never fills the blocked OCF/FCF forecast fields; applying a historical ratio to a
hypothetical revenue is labelled as arithmetic on history, not a forecast. Balance-sheet changes
are never presented as a cash-flow reconciliation.
"""
from .fin_series import contiguous_tail, ttm

FLOW = ('operating_cash_flow', 'capital_expenditure', 'revenue', 'net_income')


def _windows(series):
    """Rolling TTM from contiguous single quarters, plus fiscal years; deduplicated by dates."""
    out = {}
    q = series['quarters']
    ocf = q.get('operating_cash_flow', [])
    for i in range(3, len(ocf)):
        tail = contiguous_tail(ocf[:i + 1], 4)
        if tail:
            out[(tail[0]['start'], tail[-1]['end'])] = 'trailing_twelve_months'
    for p in series['cumulative'].get('operating_cash_flow', []):
        if p['basis'] == 'fiscal_year':
            out.setdefault((p['start'], p['end']), 'fiscal_year')
    latest = ttm(series, 'operating_cash_flow')
    if latest:
        out.setdefault((latest['start'], latest['end']), 'trailing_twelve_months')
    return sorted(out.items())


def _value(series, metric, start, end, basis):
    """Same-window reported value with its evidence IDs; never mixes windows."""
    reported = next((p for p in series['cumulative'].get(metric, []) if (p['start'], p['end']) == (start, end)), None)
    if reported:
        return reported
    points = series['quarters'].get(metric, [])
    window = [p for p in points if start <= p['start'] and p['end'] <= end]
    tail = contiguous_tail(window, 4) if len(window) >= 4 else None
    if tail and (tail[0]['start'], tail[-1]['end']) == (start, end) and \
            len({(p['currency'], p['unit'], p.get('bucket')) for p in tail}) == 1:
        return {'value': sum(p['value'] for p in tail), 'evidence_ids': sorted({i for p in tail for i in p['evidence_ids']}),
                'currency': tail[0]['currency'], 'unit': tail[0]['unit'], 'bucket': tail[0].get('bucket'),
                'start': start, 'end': end, 'derived': any(p['derived'] for p in tail),
                'formula': 'sum of four contiguous single quarters'}
    p = ttm(series, metric)
    if p and (p['start'], p['end']) == (start, end):
        return p
    return None


def history(series, adjusted_ids=()):
    conflicts = {c['metric'] for c in series.get('conflicts', [])}
    rows, excluded = [], []
    for (start, end), basis in _windows(series):
        values = {m: _value(series, m, start, end, basis) for m in FLOW}
        missing = [m for m in ('operating_cash_flow', 'capital_expenditure', 'revenue') if values[m] is None]
        if missing:
            excluded.append({'start': start, 'end': end, 'reason': 'missing_same_window_inputs', 'missing': missing})
            continue
        if {'operating_cash_flow', 'capital_expenditure', 'revenue'} & conflicts:
            excluded.append({'start': start, 'end': end, 'reason': 'conflicting_source_values_excluded'})
            continue
        core = ('operating_cash_flow', 'capital_expenditure', 'revenue')
        # Same window, currency, unit and reported accounting bucket for every input; otherwise excluded.
        keys = {(values[m].get('start', start), values[m].get('end', end), values[m]['currency'], values[m].get('unit'),
                 values[m].get('bucket', 'reported')) for m in core}
        if len(keys) != 1:
            excluded.append({'start': start, 'end': end, 'reason': 'window_currency_unit_or_basis_mismatch',
                             'keys': sorted(str(k) for k in keys)})
            continue
        if next(iter(keys))[4] != 'reported':
            excluded.append({'start': start, 'end': end, 'reason': 'non_reported_accounting_basis'})
            continue
        ocf, capex, rev = (values[m]['value'] for m in core)
        if capex < 0:
            # Payments are stored positive; a negative value means the sign convention is unverified.
            excluded.append({'start': start, 'end': end, 'reason': 'capex_sign_convention_unverified', 'value': capex})
            continue
        fcf = ocf - capex
        ni = values['net_income']
        row = {'start': start, 'end': end, 'window': basis, 'operating_cash_flow': ocf, 'capital_expenditure': capex,
               'revenue': rev, 'free_cash_flow': fcf,
               'fcf_margin_percent': fcf / rev * 100 if rev > 0 else None,
               'ocf_margin_percent': ocf / rev * 100 if rev > 0 else None,
               'capex_intensity_percent': capex / rev * 100 if rev > 0 else None,
               'fcf_to_net_income': fcf / ni['value'] if ni and ni['value'] > 0 else None,
               'negative_fcf': fcf < 0, 'negative_ocf': ocf < 0,
               'includes_derived_quarters': any(values[m].get('derived') or values[m].get('includes_derived_quarters')
                                                for m in ('operating_cash_flow', 'capital_expenditure', 'revenue')),
               'evidence_ids': sorted({i for m in FLOW if values[m] for i in values[m]['evidence_ids']}),
               'formula': 'FCF = reported OCF - reported capital expenditure (same window); margin = FCF / revenue'}
        if ni is None or ni['value'] <= 0:
            row['fcf_to_net_income_note'] = 'net income missing or non-positive; ratio not computed'
        rows.append(row)
    margins = [r['fcf_margin_percent'] for r in rows if r['fcf_margin_percent'] is not None]
    summary = None
    if margins:
        summary = {'windows': len(margins), 'min_fcf_margin_percent': min(margins), 'max_fcf_margin_percent': max(margins),
                   'latest_fcf_margin_percent': rows[-1]['fcf_margin_percent'], 'latest_window': [rows[-1]['start'], rows[-1]['end']],
                   'overlapping_windows': sum(r['window'] == 'trailing_twelve_months' for r in rows) > 1,
                   # Negative historical margins are kept: they describe cash burn, not a missing input.
                   'contains_negative_fcf': min(margins) < 0,
                   'negative_fcf_note': '負 FCF 率代表歷史上的現金消耗；套用後是燒錢／資金缺口敏感度，不是估值倍數或殖利率。' if min(margins) < 0 else None}
    return {'predictive': False, 'rows': rows, 'excluded': excluded, 'summary': summary,
            'adjusted_inputs_excluded': sorted(adjusted_ids),
            'basis': 'reported_statement_values_only', 'forecast_fields_populated': False,
            'limitation': '歷史同期間 OCF、資本支出與營收的實際比率；不是現金流預測，不填入情境 OCF／FCF。重疊的 TTM 窗口不是獨立樣本。公司調整後 FCF 不納入。'}


def sensitivity(hist, revenues, price=None, shares=None):
    """Arithmetic only: historical FCF margins applied to hypothetical NTM revenue paths."""
    s = hist['summary']
    if not s or not revenues:
        return None
    out = []
    for name, revenue in revenues.items():
        if revenue is None:
            continue
        cells = {}
        for key in ('min_fcf_margin_percent', 'latest_fcf_margin_percent', 'max_fcf_margin_percent'):
            fcf = revenue * s[key] / 100
            # A negative cash flow has no meaningful yield; it is reported as burn, never as a valuation ratio.
            cells[key] = {'fcf': fcf, 'cash_burn': fcf < 0,
                          'fcf_yield_percent': fcf / (price * shares) * 100 if price and shares and fcf >= 0 else None}
        out.append({'path': name, 'revenue': revenue, 'cells': cells})
    return {'predictive': False, 'rows': out, 'contains_cash_burn': s['contains_negative_fcf'],
            'cash_burn_note': s.get('negative_fcf_note'), 'margins': {k: s[k] for k in ('min_fcf_margin_percent', 'latest_fcf_margin_percent', 'max_fcf_margin_percent')},
            'formula': 'illustrative FCF = hypothetical NTM revenue * historical FCF margin',
            'label': '非預測：把歷史 FCF 率套用在假設營收上的算術，不代表未來營運資金、資本支出或收款。'}


def mapping_coverage(data, series, entity_id, reconciliation):
    """Explain why the statement bridge is incomplete: mapping, sign, period and custom-tag causes."""
    from .cashflow import NONCASH, WORKING_CAPITAL, IFRS_NONCASH, IFRS_WORKING_CAPITAL
    evidence = [e for e in data.evidence if e.entity_id == entity_id and e.kind == 'financial']
    metrics = {e.financial_metric for e in evidence}
    sources = sorted({e.license_source_id for e in evidence if e.license_source_id})
    cf_present = sorted(m for m in metrics if m and m.startswith('cf_'))
    ocf_bases = sorted({e.period_basis for e in evidence if e.financial_metric == 'operating_cash_flow'})
    diagnoses = []
    if not cf_present:
        diagnoses.append({'code': 'cash_flow_detail_roles_absent_from_bundle',
                          'detail': 'The reviewed bundle contains no cf_* lines; the bridge cannot be completed and no residual is set to zero.'})
    if 'sec-xbrl-companyconcept' in sources and reconciliation.get('status') != 'reconciled':
        diagnoses.append({'code': 'standard_us_gaap_concepts_only',
                          'detail': 'companyconcept returns entity-wide us-gaap tags; issuer custom extension lines and other operating assets/liabilities need filing-instance review.'})
    if any(s.startswith('sec-edgar-archives') for s in sources) and entity_id.endswith('.TW') and not cf_present:
        diagnoses.append({'code': 'ifrs_issuer_statements_not_machine_mapped',
                          'detail': 'IFRS/TWD statements from 6-K documents are normalized facts; cash-flow detail lines were not extracted.'})
    if 'financing_receivables_net' in metrics and 'cf_financing_receivables' not in metrics:
        diagnoses.append({'code': 'financing_receivable_balance_without_cash_movement',
                          'detail': 'A balance exists, but its change is not a cash-flow-statement movement (originations, collections, transfers and FX differ).'})
    if ocf_bases and 'single_quarter' not in ocf_bases:
        diagnoses.append({'code': 'cash_flow_reported_year_to_date_only',
                          'detail': 'Quarterly OCF is derived as YTD differences; window alignment is checked, never assumed.'})
    adjusted = sorted(e.evidence_id for e in evidence if e.accounting_basis in ('non_gaap', 'company_adjusted')
                      or (e.financial_metric or '').startswith('adjusted_'))
    if adjusted:
        diagnoses.append({'code': 'company_adjusted_cash_measures_excluded',
                          'detail': 'Company-adjusted FCF and add-backs stay outside the reported bridge and the historical sensitivity.'})
    expected = IFRS_NONCASH + IFRS_WORKING_CAPITAL + ('cf_statement_starting_profit', 'cf_income_taxes_paid') if reconciliation.get('starting_profit_basis') == 'income_before_tax' else NONCASH + WORKING_CAPITAL
    return {'status': reconciliation.get('status'), 'detail_roles_present': cf_present,
            'detail_roles_expected': list(expected), 'missing_components': reconciliation.get('missing_components', []),
            'operating_cash_flow_period_bases': ocf_bases, 'sources': sources, 'diagnoses': diagnoses,
            'sign_convention': 'cash_sign normalizes asset-increase tags to negative cash; financing receivables use the cash-flow line, never balance changes',
            'residual': reconciliation.get('residual'), 'residual_set_to_zero': False,
            'balance_sheet_reconciliation_claimed': False, 'adjusted_evidence_ids': adjusted}
