"""Guidance, consensus and actuals compared only on the same metric, period, unit and basis.

Management guidance and analyst consensus are separate inputs. When consensus is missing the
guidance is shown as its own reference; it is never relabelled as a market expectation.
"""
from collections import defaultdict
from .policy import reportable
from .research_contract import accounting_bucket


def forward_records(data, entity_id, kinds):
    return [e for e in data.evidence if e.entity_id == entity_id and e.statement_kind in kinds
            and e.value is not None and e.financial_metric and e.target_period_end and reportable(e, data.request)]


def ranges(records):
    """Group forward values by provider, metric, target period end, unit and basis into point or range."""
    groups = defaultdict(list)
    for e in records:
        who = e.institution or ('公司自行揭露' if e.statement_kind == 'management_guidance' else e.publisher)
        groups[(who, e.financial_metric, e.target_period_end, e.unit, e.currency,
                accounting_bucket(e), e.published_at.date())].append(e)
    out = []
    for (who, metric, end, unit, currency, bucket, published), rows in groups.items():
        values = sorted(r.value for r in rows)
        out.append({'provider': who, 'metric': metric, 'target_period_end': end, 'unit': unit, 'currency': currency,
                    'bucket': bucket, 'published': published, 'low': values[0], 'high': values[-1],
                    'evidence_ids': sorted(r.evidence_id for r in rows)})
    return out


def actual_for(series, data, entity_id, metric, end, unit, currency):
    for p in series['quarters'].get(metric, []):
        if p['end'] == end and p['unit'] == unit and p['currency'] == currency:
            return p
    for e in data.evidence:
        if (e.entity_id == entity_id and e.statement_kind == 'fact' and e.financial_metric == metric
                and e.period_end == end and e.period_basis == 'single_quarter' and e.unit == unit
                and e.currency == currency and reportable(e, data.request)):
            return {'value': e.value, 'evidence_ids': [e.evidence_id]}
    return None


def compare(data, series, entity_id):
    guidance = ranges(forward_records(data, entity_id, ('management_guidance',)))
    consensus = ranges(forward_records(data, entity_id, ('consensus_estimate', 'analyst_estimate')))
    rows, notes = [], []
    cutoff = data.request.research_as_of.date()
    for g in guidance:
        if g['target_period_end'] > cutoff:
            continue
        actual = actual_for(series, data, entity_id, g['metric'], g['target_period_end'], g['unit'], g['currency'])
        if actual is None:
            notes.append(f'{g["metric"]} 指引期間已於 {g["target_period_end"]} 結束，截止時點前尚未取得同口徑實績（可能尚未公布）')
            continue
        position = 'within' if g['low'] <= actual['value'] <= g['high'] else (
            'above' if actual['value'] > g['high'] else 'below')
        rows.append({'type': 'actual_vs_guidance', 'metric': g['metric'], 'target_period_end': g['target_period_end'].isoformat(),
                     'guidance_low': g['low'], 'guidance_high': g['high'], 'actual': actual['value'], 'position': position,
                     'evidence_ids': g['evidence_ids'] + actual['evidence_ids']})
    for g in guidance:
        for c in consensus:
            if (c['metric'], c['target_period_end'], c['unit'], c['currency'], c['bucket']) == \
                    (g['metric'], g['target_period_end'], g['unit'], g['currency'], g['bucket']):
                mid_g, mid_c = (g['low'] + g['high']) / 2, (c['low'] + c['high']) / 2
                rows.append({'type': 'guidance_vs_consensus', 'metric': g['metric'],
                             'target_period_end': g['target_period_end'].isoformat(), 'guidance_mid': mid_g,
                             'consensus_mid': mid_c, 'consensus_provider': c['provider'],
                             'difference_percent': (mid_c / mid_g - 1) * 100 if mid_g else None,
                             'evidence_ids': g['evidence_ids'] + c['evidence_ids']})
    by_provider = defaultdict(list)
    for c in consensus:
        by_provider[(c['provider'], c['metric'], c['target_period_end'], c['unit'], c['bucket'])].append(c)
    for key, versions in by_provider.items():
        versions.sort(key=lambda c: c['published'])
        for old, new in zip(versions, versions[1:]):
            rows.append({'type': 'consensus_revision', 'metric': key[1], 'target_period_end': key[2].isoformat(),
                         'provider': key[0], 'from': (old['low'] + old['high']) / 2, 'to': (new['low'] + new['high']) / 2,
                         'from_date': old['published'].isoformat(), 'to_date': new['published'].isoformat(),
                         'evidence_ids': old['evidence_ids'] + new['evidence_ids']})
    for c in consensus:
        if c['target_period_end'] > cutoff:
            continue
        actual = actual_for(series, data, entity_id, c['metric'], c['target_period_end'], c['unit'], c['currency'])
        if actual:
            mid = (c['low'] + c['high']) / 2
            rows.append({'type': 'actual_vs_consensus', 'metric': c['metric'], 'target_period_end': c['target_period_end'].isoformat(),
                         'consensus_mid': mid, 'actual': actual['value'], 'provider': c['provider'],
                         'surprise_percent': (actual['value'] / mid - 1) * 100 if mid else None,
                         'evidence_ids': c['evidence_ids'] + actual['evidence_ids']})
    if not consensus:
        notes.append('沒有可驗證的市場共識；公司指引另列，不能代替市場共識')
    return {'guidance': [_iso(g) for g in guidance], 'consensus': [_iso(c) for c in consensus],
            'comparisons': rows, 'notes': notes, 'consensus_status': 'available' if consensus else 'not_found'}


def _iso(row):
    return {**row, 'target_period_end': row['target_period_end'].isoformat(), 'published': row['published'].isoformat()}
