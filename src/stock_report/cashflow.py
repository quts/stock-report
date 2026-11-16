"""Signed cash-flow-statement bridge. Balance changes are diagnostic proxies only."""
from .fin_series import ttm

# Totals exclude D&A. Detailed movements must use their cash-flow-statement sign, not XBRL
# balance-change signs; collectors must normalize those explicitly before using these metrics.
NONCASH = ('cf_share_based_compensation', 'cf_deferred_tax', 'cf_other_noncash')
WORKING_CAPITAL = ('cf_receivables', 'cf_inventory', 'cf_payables', 'cf_financing_receivables',
                   'cf_other_operating_assets_liabilities')
FLOW_METRICS = ('cf_other_noncash_adjustments', 'cf_working_capital_cash_movement', *NONCASH, *WORKING_CAPITAL)


def reconcile(series):
    ref = ttm(series, 'operating_cash_flow')
    if ref is None:
        return {'status': 'unavailable', 'residual': None, 'reason': '缺同期間 TTM 營業現金流',
                'noncash_components': {}, 'wc_components': {}, 'evidence_ids': []}

    def match(metric):
        p = ttm(series, metric)
        return p if p and (p['start'], p['end'], p['currency'], p['unit']) == (
            ref['start'], ref['end'], ref['currency'], ref['unit']) else None

    ni, da = match('net_income'), match('depreciation_amortization')
    if ni is None:
        return {'status': 'unavailable', 'residual': None, 'reason': '缺同期間報表淨利',
                'noncash_components': {}, 'wc_components': {}, 'evidence_ids': ref['evidence_ids']}
    ids = ref['evidence_ids'] + ni['evidence_ids']

    def category(total, details):
        p = match(total)
        selected = {total: p} if p else {name: v for name in details if (v := match(name)) is not None}
        ids.extend(i for v in selected.values() for i in v['evidence_ids'])
        return {name: v['value'] for name, v in selected.items()}, p is not None or all(name in selected for name in details)

    noncash, nc_total = category('cf_other_noncash_adjustments', NONCASH)
    wc, wc_total = category('cf_working_capital_cash_movement', WORKING_CAPITAL)
    missing = ([] if da else ['depreciation_amortization'])
    if not nc_total:
        missing += [name for name in NONCASH if match(name) is None]
    if not wc_total:
        missing += [name for name in WORKING_CAPITAL if match(name) is None]
    if da:
        ids.extend(da['evidence_ids'])
    # Missing categories remain explicitly incomplete even when the numeric residual is zero.
    reconstructed = ni['value'] + (da['value'] if da else 0) + sum(noncash.values()) + sum(wc.values())
    residual = ref['value'] - reconstructed
    tolerance = max(1.0, abs(ref['value']) * 1e-6)
    complete = da is not None and nc_total and wc_total
    status = ('reconciled' if abs(residual) <= tolerance else 'mismatch') if complete else 'partial'
    return {'status': status, 'start': ref['start'], 'end': ref['end'], 'currency': ref['currency'],
            'net_income': ni['value'], 'da': da['value'] if da else None,
            'noncash_components': noncash, 'wc_components': wc,
            'noncash_adjustments': sum(noncash.values()) if noncash else None,
            'working_capital_cash_movement': sum(wc.values()) if wc else None,
            'modelled_ocf': reconstructed, 'actual_ocf': ref['value'], 'residual': residual,
            'residual_percent_ocf': residual / abs(ref['value']) * 100 if ref['value'] else None,
            'complete_categories': complete, 'tolerance': tolerance, 'evidence_ids': sorted(set(ids)),
            'missing_components': missing,
            'mapping_limitations': ['Source contracts previously collected no cf_* roles.',
                'Companyconcept supplies standard entity-wide tags, not issuer custom taxonomy or segment contexts.',
                'Asset-increase tags require negative cash signs; financing receivables are cash movements, not financing-receivable balance changes.',
                'An exact total requires the statement presentation, all other operating lines and compatible periods; no zero residual is manufactured.'],
            'limitation': '僅使用現金流量表已核實項目；未取得項目不代表零。餘額變化另列近似診斷，未解釋殘差禁止外推。'}
