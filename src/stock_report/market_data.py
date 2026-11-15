"""Market-data abstraction shared by Taiwan and US equities.

Prices come from evidence that is already licensed for internal use (an open registry source or
an attested operator import). Providers are tried in the configured priority per exchange; the
result records which provider supplied each point, conflicts between providers, split
adjustments, calendar-aware staleness and why each other provider was not used.
"""
from collections import defaultdict
from datetime import timedelta, timezone
from .calendars import trading_days_after
from .policy import reportable
from .storage import read_json

MAX_PRICE_LAG_TRADING_DAYS = 3
CONFLICT_TOLERANCE = 0.005
TZ_HOURS = {'TWSE': 8, 'TPEx': 8, 'NYSE': -5, 'NASDAQ': -5}


def _registry():
    from .licensing import load_registry
    try:
        return load_registry()
    except Exception:  # registry problems are reported by the licensing gate, not here
        return None


def provider_status(source_id, registry):
    if source_id == 'operator-import':
        return 'operator_attested_import'
    if registry is None:
        return 'registry_unavailable'
    source = next((s for s in registry.sources if s.source_id == source_id), None)
    if source is None:
        return 'not_registered'
    return 'open' if source.review_basis() and source.permits('internal_use_allowed') else f'closed:{source.declared_status}'


def prices(data, entity):
    tz = timezone(timedelta(hours=TZ_HOURS.get(entity.exchange, 0)))
    config = read_json('config/market-data-providers.json')['exchanges'].get(entity.exchange, {})
    priority = config.get('latest_close', [])
    quotes = [e for e in data.evidence if e.entity_id == entity.entity_id and e.kind == 'price'
              and e.metric_key == 'close' and reportable(e, data.request)]
    bad = [e.evidence_id for e in quotes if not e.value or e.value <= 0 or e.currency != entity.currency]
    quotes = [e for e in quotes if e.evidence_id not in bad]
    by_day = defaultdict(list)
    for e in quotes:
        by_day[e.event_time.astimezone(tz).date()].append(e)
    splits = sorted((e.period_end, e.value, e.evidence_id) for e in data.evidence
                    if e.entity_id == entity.entity_id and e.financial_metric == 'split_ratio' and e.value
                    and reportable(e, data.request))
    points, conflicts = [], []
    rank = {p: i for i, p in enumerate(priority)}
    for day, rows in sorted(by_day.items()):
        values = [r.value for r in rows]
        if max(values) - min(values) > CONFLICT_TOLERANCE * max(values):
            conflicts.append({'date': day.isoformat(), 'values': sorted(set(values)),
                              'evidence_ids': sorted(r.evidence_id for r in rows)})
            continue
        chosen = min(rows, key=lambda r: (rank.get(r.license_source_id, 99), r.evidence_id))
        factor = 1.0
        for split_day, ratio, _ in splits:
            if split_day > day:
                factor *= ratio
        points.append({'date': day, 'close': chosen.value, 'adjusted_close': chosen.value / factor,
                       'provider': chosen.license_source_id, 'evidence_id': chosen.evidence_id})
    registry = _registry()
    providers = [{'provider': p, 'status': provider_status(p, registry),
                  'records': sum(1 for e in quotes if e.license_source_id == p)} for p in priority]
    gaps = {g.field: g for g in data.data_gaps}
    diagnostics = {'providers': providers, 'rejected_values': bad, 'conflicts': conflicts,
                   'splits': [{'date': d.isoformat(), 'ratio': r, 'evidence_id': i} for d, r, i in splits],
                   'recorded_gap': gaps['price_latest'].model_dump(mode='json') if 'price_latest' in gaps else None,
                   'action': None}
    cutoff_day = data.request.research_as_of.astimezone(tz).date()
    if not points:
        status = 'conflicting' if conflicts else ('source_unavailable' if 'price_latest' in gaps else 'not_found')
        diagnostics['action'] = ('沒有可用收盤價：' + '；'.join(f'{p["provider"]}={p["status"]}' for p in providers)
                                 + '。可用 cli import-market-data --kind prices 匯入有權使用的收盤價。')
        return {'status': status, 'points': [], 'latest': None, 'diagnostics': diagnostics,
                'note': diagnostics['action']}
    last = points[-1]
    lag, verified = trading_days_after(entity.exchange, last['date'], cutoff_day)
    status = 'available' if lag <= MAX_PRICE_LAG_TRADING_DAYS else 'stale'
    note = f'{last["provider"]} 收盤日 {last["date"]}，距截止 {lag} 個交易日'
    if not verified:
        note += '（該年度交易所假日表未取得，以週一至週五估算）'
    if status == 'stale':
        note = f'收盤價過期：{note}，超過 {MAX_PRICE_LAG_TRADING_DAYS} 個交易日'
    return {'status': status, 'points': points, 'latest': last, 'trading_days_lag': lag, 'calendar_verified': verified,
            'diagnostics': diagnostics, 'note': note, 'history_points': len(points)}


def close_on_or_before(series, day, max_gap_days=7):
    rows = [p for p in series['points'] if p['date'] <= day and (day - p['date']).days <= max_gap_days]
    return rows[-1] if rows else None
