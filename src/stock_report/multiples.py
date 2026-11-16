"""Valuation references: historical trading multiples and peer multiples, with reliability.

A reference is `sufficient` only with enough independent observations; otherwise a multiple
chosen for a scenario cannot be called evidence-based and the scenario stays hypothetical.
"""
from datetime import date, datetime, time, timedelta, timezone
from collections import Counter
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


def historical_pe(series, prices, data=None, entity_id=None):
    """Quarter-end multiples using only EPS versions already published by the quote date."""
    from . import fin_series
    eps = series['quarters'].get('diluted_eps', [])
    splits = [{'date': date.fromisoformat(s['date']), 'ratio': s['ratio']} for s in prices['diagnostics']['splits']]
    points = []
    excluded = Counter()
    seen_quotes = set()
    for i in range(3, len(eps)):
        tail = contiguous_tail(eps[:i + 1], 4)
        if tail is None:
            continue
        end = tail[-1]['end']
        quote = close_on_or_before(prices, end)
        if quote is None:
            excluded['quarter_end_price_unavailable'] += 1
            continue
        from .market_data import TZ_HOURS
        entity = next((e for e in data.entities if e.entity_id == entity_id), None) if data else None
        tz = timezone(timedelta(hours=TZ_HOURS.get(entity.exchange, 0))) if entity else timezone.utc
        quote_close_bound = datetime.combine(quote['date'] + timedelta(days=1), time.min, tz)
        if quote.get('available_at') and quote['available_at'] >= quote_close_bound:
            excluded['historical_price_not_available_on_observation_date'] += 1
            continue
        if quote['evidence_id'] in seen_quotes:
            continue
        seen_quotes.add(quote['evidence_id'])
        if data is None:
            excluded['historical_versions_unavailable'] += 1
            continue
        cutoff = datetime.combine(quote['date'], time.min, tz)
        visible = [e for e in data.evidence if max(e.published_at, e.event_time) < cutoff
                   and (e.publication_basis != 'observed_availability' or
                        e.availability_observed_at is not None and e.availability_observed_at < cutoff)]
        historic = fin_series.build(data.model_copy(update={'evidence': visible}), entity_id)
        tail = contiguous_tail(historic['quarters'].get('diluted_eps', []), 4)
        if tail is None or tail[-1]['end'] > quote['date']:
            excluded['four_published_eps_quarters_unavailable'] += 1
            continue
        if any(tail[0]['start'] <= s['date'] <= quote['date'] for s in splits):
            # A split after the historical quote cannot invalidate pre-split observations.
            # A split inside this earnings/price window still requires an explicit basis audit.
            excluded['split_eps_share_basis_unverified'] += 1
            continue
        # EPS and price are both expressed on the share basis of the quote date.
        ttm = sum(p['value'] / split_factor(splits, p['end']) * split_factor(splits, quote['date']) for p in tail)
        if ttm <= 0:
            excluded['nonpositive_ttm_eps'] += 1
            continue
        points.append({'date': quote['date'].isoformat(), 'sampling_period_end': end.isoformat(),
                       'price': quote['close'], 'eps_ttm': ttm, 'pe': quote['close'] / ttm,
                       'share_basis': 'as_filed_diluted_eps_and_unadjusted_close; no_supplied_split_within_earnings_price_window',
                       'eps_period_end': tail[-1]['end'].isoformat(), 'availability_cutoff': cutoff.isoformat(),
                       'evidence_ids': [quote['evidence_id']] + [i for p in tail for i in p['evidence_ids']]})
    if not points:
        return {'status': 'insufficient', 'points': [], 'exclusions': dict(excluded),
                'note': '沒有同時具備季末收盤價、當時已公布四季 EPS 及已核對股數基準的估值點'}
    quality = 'sufficient' if len(points) >= MIN_HISTORY_POINTS else 'limited'
    return {'status': quality, 'points': points, 'exclusions': dict(excluded), 'stats': _stats([p['pe'] for p in points]),
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
                       'stats': _stats(values), 'evidence_ids': sorted(e.evidence_id for e in latest.values()),
                       'comparability': 'identity, earnings basis and business mix require explicit review'}
        # A count of issuer labels is not proof of comparable valuation inputs.
        out[metric]['status'] = 'limited'
    return out


def references(data, series, prices, entity_id):
    return {'historical_pe': historical_pe(series, prices, data, entity_id), 'peers': peer_multiples(data, entity_id)}
