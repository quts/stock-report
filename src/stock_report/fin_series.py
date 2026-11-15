"""Quarterly financial time series built from evidence, deterministically.

* Reported single quarters are used as is. A missing single quarter is derived only by
  subtracting two cumulative windows of the same metric, accounting bucket, unit and start date
  (Q2 = 6M - Q1, Q4 = FY - 9M). Per-share figures, share counts and ratios are never derived
  by subtraction.
* Two usable figures for the same metric and window that differ by more than the tolerance are
  a conflict: the window is excluded and reported, never silently resolved.
* TTM sums the latest four contiguous single quarters; it is unavailable otherwise.
"""
from collections import defaultdict
from datetime import timedelta
from .policy import reportable
from .research_contract import accounting_bucket

FLOW_METRICS = ('revenue', 'gross_profit', 'operating_income', 'net_income', 'operating_cash_flow',
                'capital_expenditure', 'depreciation_amortization', 'income_before_tax', 'income_tax')
INSTANT_METRICS = ('cash_equivalents', 'accounts_receivable_net', 'accounts_receivable', 'inventory',
                   'accounts_payable', 'short_term_debt', 'long_term_debt', 'bonds_payable', 'total_debt',
                   'total_equity', 'total_assets', 'total_liabilities', 'shares_outstanding',
                   'financing_receivables_net')
CONFLICT_TOLERANCE = 0.005


def _point(e_list, value, start, end, basis, derived=False, formula=None):
    e = e_list[0]
    return {'metric': e.financial_metric, 'value': value, 'start': start, 'end': end, 'basis': basis,
            'unit': e.unit, 'currency': e.currency, 'bucket': accounting_bucket(e),
            'evidence_ids': sorted({x.evidence_id for x in e_list}), 'derived': derived, 'formula': formula,
            'source_type': e.source_type}


def usable_facts(data, entity_id):
    return [e for e in data.evidence
            if e.entity_id == entity_id and e.kind == 'financial' and e.statement_kind == 'fact'
            and e.financial_metric and e.value is not None and e.period_end is not None
            and e.unit != 'source_scale_undeclared' and reportable(e, data.request)]


def build(data, entity_id):
    groups, conflicts = defaultdict(list), []
    for e in usable_facts(data, entity_id):
        groups[(e.financial_metric, accounting_bucket(e), e.unit, e.currency, e.period_start, e.period_end,
                e.period_basis)].append(e)
    windows = {}
    for key, rows in groups.items():
        values = [r.value for r in rows]
        low, high = min(values), max(values)
        if high - low > CONFLICT_TOLERANCE * max(abs(high), abs(low), 1e-12):
            conflicts.append({'metric': key[0], 'period_end': key[5].isoformat(), 'basis': key[6],
                              'values': sorted(set(values)), 'evidence_ids': sorted(r.evidence_id for r in rows),
                              'status': 'conflicting'})
            continue
        rows = sorted(rows, key=lambda r: (r.source_type != 'official', r.evidence_id))
        windows[key] = _point(rows, rows[0].value, key[4], key[5], key[6])
    # Depreciation and amortization reported on separate lines combine into one metric per window.
    for key in [k for k in list(windows) if k[0] == 'depreciation']:
        twin = ('amortization',) + key[1:]
        target = ('depreciation_amortization',) + key[1:]
        if twin in windows and target not in windows:
            a, b = windows[key], windows[twin]
            windows[target] = {**a, 'metric': 'depreciation_amortization', 'value': a['value'] + b['value'], 'derived': True,
                               'formula': 'depreciation + amortization (same window)',
                               'evidence_ids': sorted(set(a['evidence_ids']) | set(b['evidence_ids']))}
    by_metric = defaultdict(list)
    for key, point in windows.items():
        by_metric[(key[0], key[1], key[2], key[3])].append(point)
    quarters, instants, cumulative = {}, {}, {}
    for (metric, bucket, unit, currency), points in by_metric.items():
        if bucket != 'reported':
            continue  # adjusted figures are reported separately, never in the statement series
        name = metric
        singles = {(p['start'], p['end']): p for p in points if p['basis'] == 'single_quarter'}
        cums = [p for p in points if p['basis'] in ('cumulative_ytd', 'fiscal_year') and p['start']]
        if metric in FLOW_METRICS and unit in ('USD', 'TWD'):
            for longer in cums:
                shorter = [p for p in cums + list(singles.values())
                           if p['start'] == longer['start'] and 80 <= (longer['end'] - p['end']).days <= 100]
                if len(shorter) != 1:
                    continue
                prior = shorter[0]
                start = prior['end'] + timedelta(days=1)
                if (start, longer['end']) in singles:
                    continue
                singles[(start, longer['end'])] = {
                    **_point_from(longer, prior), 'start': start, 'end': longer['end'], 'basis': 'single_quarter',
                    'value': longer['value'] - prior['value'], 'derived': True,
                    'formula': f'{longer["basis"]}({longer["start"]}~{longer["end"]}) - '
                               f'{prior["basis"]}({prior["start"]}~{prior["end"]})'}
        if singles:
            quarters[name] = sorted(singles.values(), key=lambda p: p['end'])
        if cums:
            cumulative[name] = sorted(cums, key=lambda p: p['end'])
        inst = [p for p in points if p['basis'] == 'instant']
        if inst:
            instants[name] = sorted(inst, key=lambda p: p['end'])
    return {'quarters': quarters, 'instants': instants, 'cumulative': cumulative, 'conflicts': conflicts}


def _point_from(longer, prior):
    return {'metric': longer['metric'], 'unit': longer['unit'], 'currency': longer['currency'],
            'bucket': longer['bucket'], 'source_type': longer['source_type'],
            'evidence_ids': sorted(set(longer['evidence_ids']) | set(prior['evidence_ids']))}


def contiguous_tail(points, count):
    """The latest `count` single quarters if each starts the day after the previous one ends."""
    if len(points) < count:
        return None
    tail = points[-count:]
    for a, b in zip(tail, tail[1:]):
        if a['end'] + timedelta(days=1) != b['start']:
            return None
    return tail


def ttm(series, metric):
    tail = contiguous_tail(series['quarters'].get(metric, []), 4)
    if tail is None:
        # Per-share figures and share counts are never derived by subtracting cumulative windows.
        return ttm_from_cumulative(series, metric) if metric in FLOW_METRICS else None
    return {'metric': metric, 'value': sum(p['value'] for p in tail), 'start': tail[0]['start'], 'end': tail[-1]['end'],
            'basis': 'trailing_twelve_months', 'unit': tail[0]['unit'], 'currency': tail[0]['currency'],
            'evidence_ids': sorted({i for p in tail for i in p['evidence_ids']}),
            'derived': True, 'formula': 'sum of four contiguous single quarters',
            'includes_derived_quarters': any(p['derived'] for p in tail)}


def ttm_from_cumulative(series, metric):
    """TTM = last fiscal year + current year-to-date - prior-year year-to-date (same span).

    Used when single quarters are not reported, e.g. cash flow disclosed only year-to-date."""
    rows = series['cumulative'].get(metric, [])
    current = next((p for p in reversed(rows) if p['basis'] == 'cumulative_ytd'), None)
    if current is None:
        return None
    fiscal = next((p for p in rows if p['basis'] == 'fiscal_year' and p['end'] + timedelta(days=1) == current['start']), None)
    if fiscal is None:
        return None
    span = (current['end'] - current['start']).days
    prior = next((p for p in rows if p['basis'] == 'cumulative_ytd' and p['start'] == fiscal['start']
                  and abs((p['end'] - p['start']).days - span) <= 7), None)
    if prior is None:
        return None
    return {'metric': metric, 'value': fiscal['value'] + current['value'] - prior['value'],
            'start': prior['end'] + timedelta(days=1), 'end': current['end'], 'basis': 'trailing_twelve_months',
            'unit': current['unit'], 'currency': current['currency'],
            'evidence_ids': sorted(set(fiscal['evidence_ids']) | set(current['evidence_ids']) | set(prior['evidence_ids'])),
            'derived': True, 'formula': 'last fiscal year + current YTD - prior-year YTD', 'includes_derived_quarters': True}


def year_ago(points, point):
    """Same-length window ending about one year earlier."""
    span = (point['end'] - point['start']).days if point.get('start') else None
    for p in reversed(points):
        if 355 <= (point['end'] - p['end']).days <= 375 and (
                span is None or p.get('start') is None or abs((p['end'] - p['start']).days - span) <= 14):
            return p
    return None


def latest_instant(series, metric, on_or_before=None):
    rows = [p for p in series['instants'].get(metric, []) if on_or_before is None or p['end'] <= on_or_before]
    return rows[-1] if rows else None
