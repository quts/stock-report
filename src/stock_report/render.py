"""Render only policy-approved projections of evidence confirmed for internal use.

The PDF is a personal-use document that is never distributed, so redistribution rights are not
a rendering condition. Unknown, restricted or internally unlicensed records never reach it.
"""
from importlib.resources import files
from jinja2 import Environment, FileSystemLoader, select_autoescape
from .policy import reportable
from .quant import consensus, calculate

LABELS = {'bullish': '偏多', 'neutral': '中性', 'bearish': '偏空', 'insufficient': '資料不足'}
METRICS = {'revenue_growth': '營收成長率', 'eps_growth': 'EPS 成長率', 'net_margin': '淨利率',
           'free_cash_flow': '自由現金流', 'pe': '本益比'}


def projection(data, decision, mapping):
    entity_ids = {e.entity_id for e in data.entities}
    if data.request.mode == 'event':
        entity_ids.add('EVENT')
    allowed = {e.evidence_id: e for e in data.evidence if e.entity_id in entity_ids and reportable(e, data.request)}
    computed = calculate(data.evidence, data.request.research_as_of)
    companies = []
    for entity in data.entities:
        identity_ok = any(e.entity_id == entity.entity_id and e.kind == 'identity' for e in allowed.values())
        if not identity_ok:
            raise ValueError('Cannot render a report without a verified, internally licensed company identity')
        metrics = [m for m in computed if m.entity_id == entity.entity_id and m.value is not None
                   and all(eid in allowed for eid in m.evidence_ids)]
        company_evidence = [e for e in allowed.values() if e.entity_id == entity.entity_id]
        quotes = [e for e in company_evidence if e.kind == 'price' and e.metric_key == 'close'
                  and e.value is not None and e.value > 0 and e.currency == entity.currency]
        latest_quote = max(quotes, key=lambda e: (e.event_time, e.published_at, e.evidence_id), default=None)
        relation = next((r for r in data.relationships if r.entity_id == entity.entity_id), None)
        valid_relation = relation and relation.evidence_ids and all(eid in allowed and allowed[eid].kind == 'relationship' and allowed[eid].entity_id == entity.entity_id for eid in relation.evidence_ids)
        companies.append({'entity': entity, 'metrics': metrics, 'latest_quote': latest_quote,
            'events': [e for e in company_evidence if e.kind == 'event'],
            'outlooks': [p for p in decision.outlooks if p.entity_id == entity.entity_id],
            'consensus': consensus(company_evidence, entity.entity_id, data.request.research_as_of, mapping),
            'relationship': relation.relationship if valid_relation else None,
            'relationship_evidence_ids': relation.evidence_ids if valid_relation else []})
    return {'data': data, 'decision': decision, 'companies': companies, 'labels': LABELS, 'metric_labels': METRICS,
            'event_records': [allowed[eid] for eid in data.event_evidence_ids if eid in allowed and allowed[eid].kind == 'event' and allowed[eid].entity_id == 'EVENT'],
            'sources': list(allowed.values()), 'synthetic': data.dataset_kind == 'mock'}


def render(data, decision, mapping, path):
    from weasyprint import HTML
    def deny_resources(url, *args, **kwargs):
        raise ValueError('External and local URL resource fetching is disabled')
    env = Environment(loader=FileSystemLoader(str(files('stock_report').joinpath('templates'))),
                      autoescape=select_autoescape(['html']))
    env.filters['brief'] = lambda value, n=140: str(value) if len(str(value)) <= n else str(value)[:n] + '…（完整內容見本機 JSON）'
    html = env.get_template('report.html').render(**projection(data, decision, mapping))
    document = HTML(string=html, url_fetcher=deny_resources).render()
    if not 1 <= len(document.pages) <= data.request.max_pdf_pages:
        raise ValueError(f'PDF layout exceeds page budget: {len(document.pages)} pages; no report published')
    document.write_pdf(path)
    return len(document.pages)
