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


def _provider_state(provider, registry, records, need_history):
    """Distinguish blocked, latest-only, not supplied, failed and available providers."""
    from .sources import PROFILES
    status = provider_status(provider, registry)
    if provider == 'operator-import':
        return ('available' if records else 'not_supplied_requires_user_rights_attestation')
    if status == 'open':
        if need_history and (PROFILES.get(provider) or {}).get('coverage_mode') == 'latest_only':
            return 'latest_only_no_history'
        return 'available' if records else 'open_but_no_record_for_this_cutoff'
    if status.startswith('closed:'):
        return 'blocked_source_review_insufficient'
    return status


def dependencies(data, entity, series, refs):
    """What each valuation reference needs, and exactly why it is or is not available.

    A generic source-unavailable gap cannot establish a failed retrieval without attempt
    details; blocked or unreviewed sources never imply the data does not exist."""
    from .multiples import MIN_HISTORY_POINTS, MIN_PEERS
    config = read_json('config/market-data-providers.json')
    exchange = config['exchanges'].get(entity.exchange, {})
    registry = _registry()
    gaps = {g.field: g for g in data.data_gaps}
    counts = defaultdict(int)
    for e in data.evidence:
        if e.entity_id == entity.entity_id and e.kind == 'price' and reportable(e, data.request):
            counts[e.license_source_id] += 1

    def item(name, field, providers, need_history, have, required_for, need):
        states = [{'provider': p, 'state': _provider_state(p, registry, counts.get(p, 0), need_history),
                   'records': counts.get(p, 0)} for p in providers]
        gap = gaps.get(field)
        if have:
            status = 'available'
        elif any(s['state'] == 'available' for s in states):
            status = 'insufficient_observations'
        elif gap is not None and gap.status == 'source_unavailable' and any(
                s['state'] == 'open_but_no_record_for_this_cutoff' for s in states):
            status = 'source_unavailable_attempt_details_required'
        elif states and all(s['state'] in ('blocked_source_review_insufficient', 'latest_only_no_history',
                                           'not_supplied_requires_user_rights_attestation', 'not_registered')
                            for s in states):
            status = 'blocked_or_not_supplied'
        else:
            status = 'missing'
        return {'input': name, 'status': status, 'required_for': required_for, 'requirement': need,
                'providers': states, 'recorded_gap': None if gap is None else gap.model_dump(mode='json')}

    hist = refs.get('historical_pe') or {}
    peers = refs.get('peers') or {}
    forward = any(e.entity_id == entity.entity_id and e.statement_kind == 'consensus_estimate'
                  and e.financial_metric in ('eps', 'diluted_eps') and reportable(e, data.request) for e in data.evidence)
    out = [item('latest_close', 'price_latest', exchange.get('latest_close', []), False,
                series['status'] == 'available', ['pe_trailing', 'ev_ebitda', 'p_fcf', 'fcf_yield', 'p_b', 'reverse_valuation'],
                f'one close within {MAX_PRICE_LAG_TRADING_DAYS} trading days of the cutoff'),
           item('quarter_end_close_history', 'price_history', exchange.get('history', []), True,
                hist.get('status') == 'sufficient', ['historical_range'],
                f'>= {MIN_HISTORY_POINTS} quarter-end closes paired with point-in-time TTM EPS'),
           {'input': 'peer_multiples', 'status': 'available' if any(v['status'] == 'sufficient' for v in peers.values()) else
            ('insufficient_observations' if peers else 'missing'), 'required_for': ['peer_relative'],
            'requirement': f'>= {MIN_PEERS} identity-reviewed peers with a licensed multiple on the same basis and date',
            'providers': [{'provider': 'operator-import', 'state': 'available' if peers else 'not_supplied_requires_user_rights_attestation',
                           'records': sum(len(v['peers']) for v in peers.values())}],
            'recorded_gap': gaps['peers'].model_dump(mode='json') if 'peers' in gaps else None},
           {'input': 'consensus_eps', 'status': 'available' if forward else 'missing', 'required_for': ['pe_forward'],
            'requirement': 'named, terms-reviewed consensus source; company guidance is never a substitute',
            'providers': [], 'recorded_gap': gaps['consensus'].model_dump(mode='json') if 'consensus' in gaps else None}]
    return {'inputs': out, 'historical_pe_exclusions': hist.get('exclusions', {}),
            'rejected_providers': config.get('rejected', []),
            'policy': 'No provider is enabled without a complete five-aspect source review and activation-check; operator '
                      'imports need the user\'s own rights attestation. Historical or peer multiples are never estimated.'}


def close_on_or_before(series, day, max_gap_days=7):
    rows = [p for p in series['points'] if p['date'] <= day and (day - p['date']).days <= max_gap_days]
    return rows[-1] if rows else None
