"""Explicitly synthetic fixtures, never a fallback for failed real requests."""
from datetime import timedelta
from .models import Entity, Evidence, ResearchInput, Relationship
from .quant import calculate

ENTITIES = {
    'NVDA': ('NVIDIA（合成測資）', 'NASDAQ', 'USD'),
    'AMD': ('AMD（合成測資）', 'NASDAQ', 'USD'),
    '2330.TW': ('台積電（合成測資）', 'TWSE', 'TWD'),
    '6488.TWO': ('環球晶（合成測資）', 'TPEx', 'TWD'),
}
VERSIONS = {'quant': '1', 'policy': 'phase0-1', 'prompt': '1', 'template': '1', 'dataset': 'synthetic-1'}


def fixture(request):
    tickers = [request.ticker] if request.mode == 'stock' else list(ENTITIES)[:request.max_companies]
    if any(t not in ENTITIES for t in tickers):
        raise ValueError('Unknown mock ticker; supported: NVDA, AMD, 2330.TW, 6488.TWO')
    entities, evidence, relations = [], [], []
    cutoff = request.research_as_of
    def record(entity, suffix, kind, claim, **kwargs):
        e = Evidence(evidence_id=f'{entity}-{suffix}', entity_id=entity, claim=claim, kind=kind,
            source_url=f'https://example.invalid/synthetic/{entity}/{suffix}', publisher='Synthetic fixture',
            source_type='synthetic', source_record_id=suffix, event_time=cutoff-timedelta(days=3),
            published_at=cutoff-timedelta(days=2), publication_basis='synthetic',
            retrieved_at=cutoff, research_as_of=cutoff,
            verification_status='verified', license_status='confirmed', attribution='專案自行生成合成測資',
            shareable_in_pdf=True, internal_use_allowed=True, terms_url='https://example.invalid/synthetic-terms',
            license_checked_at=cutoff, **kwargs)
        evidence.append(e)
        return e.evidence_id
    for t in tickers:
        name, exchange, currency = ENTITIES[t]
        if request.exchange and request.exchange != exchange:
            raise ValueError('Requested exchange does not match ticker')
        entities.append(Entity(entity_id=t, name=name, exchange=exchange, currency=currency))
        record(t, 'identity', 'identity', f'{name}，{exchange}，{currency} 普通股；僅為測試身份')
        for key, value, period, unit in [('revenue_current', 1200, 'FY2025', 'million'),
                ('revenue_prior', 1000, 'FY2024', 'million'), ('net_income', 240, 'FY2025', 'million'),
                ('operating_cash_flow', 300, 'FY2025', 'million'), ('capital_expenditure', 80, 'FY2025', 'million'),
                ('eps_current', 4, 'TTM', 'per_share'), ('close', 100, 'close', 'per_share')]:
            record(t, key, 'price' if key == 'close' else 'financial', f'合成 {key} = {value} {unit}，期別 {period}',
                   value=value, unit=unit, currency=currency, period=period, metric_key=key)
        record(t, 'risk', 'event', '合成風險：需求放緩可能壓低獲利；不是實際新聞')
        if request.mode == 'event':
            eid = record(t, 'relationship', 'relationship', '合成事件與該公司之關聯，僅供測試')
            relations.append(Relationship(entity_id=t, relationship='hypothetical' if t == '6488.TWO' else 'direct' if t == 'NVDA' else 'indirect', evidence_ids=[eid]))
    event_ids = []
    if request.mode == 'event':
        event_ids.append(record('EVENT', 'announcement', 'event', 'DEMO 合成跨市場產業事件，並非真實政策公告'))
    return ResearchInput(dataset_kind='mock', request=request, entities=entities, evidence=evidence,
        metrics=calculate(evidence, cutoff), relationships=relations, event_evidence_ids=event_ids, versions=VERSIONS)
