"""Valuation references: historical trading multiples and peer multiples, with reliability.

A reference is `sufficient` only with enough independent observations; otherwise a multiple
chosen for a scenario cannot be called evidence-based and the scenario stays hypothetical.
"""
from datetime import date
from statistics import median
from .fin_series import contiguous_tail
from .market_data import close_on_or_before
from .policy import reportable

MIN_HISTORY_POINTS = 8
MIN_PEERS = 3


def _stats(values):
    v = sorted(values)
    q = lambda f: v[min(len(v) - 1, max(0, round(f * (len(v) - 1))))]
    return {'count': len(v), 'min': v[0], 'p25': q(0.25), 'median': median(v), 'p75': q(0.75), 'max': v[-1]}


def split_factor(splits, after):
    factor = 1.0
    for s in splits:
        if s['date'] > after:
            factor *= s['ratio']
    return factor


def historical_pe(series, prices):
    """P/E at each quarter end: close on (or shortly before) the quarter end / TTM diluted EPS then."""
    eps = series['quarters'].get('diluted_eps', [])
    splits = [{'date': date.fromisoformat(s['date']), 'ratio': s['ratio']} for s in prices['diagnostics']['splits']]
    points = []
    for i in range(3, len(eps)):
        tail = contiguous_tail(eps[:i + 1], 4)
        if tail is None:
            continue
        end = tail[-1]['end']
        quote = close_on_or_before(prices, end)
        if quote is None:
            continue
        # EPS and price are both expressed on the share basis of the quote date.
        ttm = sum(p['value'] / split_factor(splits, p['end']) * split_factor(splits, quote['date']) for p in tail)
        if ttm <= 0:
            continue
        points.append({'date': end.isoformat(), 'price': quote['close'], 'eps_ttm': ttm, 'pe': quote['close'] / ttm,
                       'evidence_ids': [quote['evidence_id']] + [i for p in tail for i in p['evidence_ids']]})
    if not points:
        return {'status': 'insufficient', 'points': [], 'note': '沒有同時具備季末收盤價與 TTM EPS 的期間'}
    quality = 'sufficient' if len(points) >= MIN_HISTORY_POINTS else 'limited'
    return {'status': quality, 'points': points, 'stats': _stats([p['pe'] for p in points]),
            'note': None if quality == 'sufficient' else f'只有 {len(points)} 個季末估值點，少於 {MIN_HISTORY_POINTS} 個'}


def peer_multiples(data, entity_id):
    out = {}
    for metric in ('pe_trailing', 'ev_ebitda', 'p_fcf', 'p_b'):
        rows = [e for e in data.evidence if e.entity_id == entity_id and e.financial_metric == f'peer_{metric}'
                and e.value and e.value > 0 and reportable(e, data.request)]
        latest = {}
        for e in rows:
            if e.institution not in latest or e.period_end > latest[e.institution].period_end:
                latest[e.institution] = e
        if not latest:
            continue
        values = [e.value for e in latest.values()]
        out[metric] = {'status': 'sufficient' if len(values) >= MIN_PEERS else 'limited', 'peers': sorted(latest),
                       'stats': _stats(values), 'evidence_ids': sorted(e.evidence_id for e in latest.values())}
    return out


def references(data, series, prices, entity_id):
    return {'historical_pe': historical_pe(series, prices), 'peers': peer_multiples(data, entity_id)}
