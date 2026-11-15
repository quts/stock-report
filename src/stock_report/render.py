"""Render only policy-approved projections of evidence confirmed for internal use.

The PDF is a personal-use document that is never distributed, so redistribution rights are not
a rendering condition. Unknown, restricted or internally unlicensed records never reach it.
"""
from importlib.resources import files
from jinja2 import Environment, FileSystemLoader, select_autoescape
from .policy import reportable
from .models import event_review_state, research_scope, financial_review_state
from .statement_quant import statement_metrics, format_amount, LABELS as STATEMENT_LABELS
from datetime import timezone, timedelta
from .quant import consensus, calculate

LABELS = {'bullish': '偏多', 'neutral': '中性', 'bearish': '偏空', 'insufficient': '資料不足'}
METRICS = {'revenue_growth': '營收成長率', 'eps_growth': 'EPS 成長率', 'net_margin': '淨利率',
           'free_cash_flow': '自由現金流', 'pe': '本益比'}


def projection(data, decision, mapping, company_research=None):
    entity_ids = {e.entity_id for e in data.entities}
    if data.request.mode == 'event':
        entity_ids.add('EVENT')
    allowed = {e.evidence_id: e for e in data.evidence if e.entity_id in entity_ids and reportable(e, data.request)}
    computed = calculate(data.evidence, data.request.research_as_of)
    major_ids = {eid for event in (data.event_review.events if data.event_review else [])
                 if event.materiality == 'major' for eid in event.evidence_ids}
    companies = []
    narrative = {s.entity_id: s for s in (company_research or [])}
    statements = statement_metrics(data)
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
            'events': sorted([e for e in company_evidence if e.kind == 'event'], key=lambda e: (e.evidence_id in major_ids, e.event_time, e.evidence_id), reverse=True),
            'outlooks': [p for p in decision.outlooks if p.entity_id == entity.entity_id],
            'consensus': consensus(company_evidence, entity.entity_id, data.request.research_as_of, mapping),
            'relationship': relation.relationship if valid_relation else None,
            'relationship_evidence_ids': relation.evidence_ids if valid_relation else []})
        company = companies[-1]
        company['research'] = narrative.get(entity.entity_id)
        # Every reviewed material event is shown; minor events only fill the remaining slots.
        major = [e for e in company['events'] if e.evidence_id in major_ids]
        company['display_events'] = major + [e for e in company['events'] if e.evidence_id not in major_ids][:max(0, 3 - len(major))]
        rows = [r for r in statements if r['entity_id'] == entity.entity_id and 'current_value' in r]
        core = ('revenue', 'gross_profit', 'operating_income', 'net_income', 'diluted_eps',
                'operating_cash_flow', 'free_cash_flow', 'adjusted_free_cash_flow')
        company['quarter_rows'] = sorted([r for r in rows if r['basis'] == 'single_quarter' and r['name'] in core],
                                         key=lambda r: core.index(r['name']))
        company['segment_rows'] = [r for r in rows if r['basis'] == 'single_quarter'
                                   and r['name'] in ('isg_revenue', 'isg_operating_income', 'csg_revenue', 'csg_operating_income')]
        company['balance_rows'] = [r for r in rows if r['basis'] == 'instant' and r['name'] in (
            'accounts_receivable_net', 'accounts_receivable', 'inventory', 'financing_receivables_net')]
        company['guidance'] = [e for e in company_evidence if e.statement_kind in (
            'management_guidance', 'analyst_estimate', 'consensus_estimate')]
        reviewed_ids = {eid for event in (data.event_review.events if data.event_review else []) for eid in event.evidence_ids}
        # Reviewed events the cited news analysis already covers are not repeated; any reviewed event
        # the analysis left out is still listed, newest first, so no material event disappears.
        cited_news = {eid for p in (company['research'].news if company['research'] else []) for eid in p.evidence_ids}
        company['timeline'] = sorted([e for e in company['events'] if e.evidence_id in reviewed_ids
                                      and e.statement_kind == 'fact' and e.evidence_id not in cited_news],
                                     key=lambda e: (e.event_time, e.evidence_id), reverse=True)
        company['reader_outlooks'] = [{'horizon': p.horizon, 'outlook': p.outlook,
            'text': p.interpretation.split('｜分析說明：')[-1] if '｜分析說明：' in p.interpretation
                    else p.interpretation, 'evidence_ids': p.supporting_evidence_ids + p.counter_evidence_ids,
            'conditions': [c.rstrip('。；; ') for c in p.invalidation_conditions]} for p in company['outlooks']]
    # Material events lead the short source list; duplicate financial endpoints add no context.
    unique_sources = list({(e.source_url, e.article_url): e for e in sorted(allowed.values(), key=lambda e: e.kind == 'event', reverse=True)}.values())
    source_numbers = {(e.source_url, e.article_url): i + 1 for i, e in enumerate(unique_sources)}
    references = {eid: source_numbers[(e.source_url, e.article_url)] for eid, e in allowed.items()}
    limitations = []
    if event_review_state(data):
        limitations.append('近期重大事件查核未完成；本報告是財務快照，不是完整個股研究，也不代表沒有重大事件。')
    if financial_review_state(data): limitations.append('最新一期財報尚未完整納入；本報告只涵蓋已列示的財務資料。')
    if data.request.mode == 'stock' and not any(e.financial_metric == 'operating_cash_flow' for e in allowed.values()):
        limitations.append('未取得可用的現金流量資料。')
    if not any(e.kind == 'price' for e in allowed.values()): limitations.append('未取得現價與可比較估值，未提供目標價或即時交易判斷。')
    if not any(e.kind == 'rating' for e in allowed.values()): limitations.append('未取得可驗證的機構評等共識。')
    if data.earnings_preview: limitations.extend(data.earnings_preview.limitations)
    if data.event_review: limitations.extend(lead.summary for lead in data.event_review.unresolved_leads)
    statement_periods = sorted({e.period_end for e in allowed.values() if e.kind == 'financial'
                                and e.statement_kind == 'fact' and e.period_end and e.financial_metric == 'revenue'})
    return {'latest_statement_period': statement_periods[-1] if statement_periods else None,
            'research_scope': research_scope(data), 'completion_blockers': event_review_state(data), 'data': data, 'decision': decision, 'companies': companies, 'labels': LABELS, 'metric_labels': METRICS,
            'event_records': [allowed[eid] for eid in data.event_evidence_ids if eid in allowed and allowed[eid].kind == 'event' and allowed[eid].entity_id == 'EVENT'],
            'sources': list(allowed.values()), 'display_sources': unique_sources, 'synthetic': data.dataset_kind == 'mock',
            'references': references, 'source_numbers': source_numbers, 'limitations': list(dict.fromkeys(limitations)),
            'statement_labels': STATEMENT_LABELS}


def render(data, decision, mapping, path, company_research=None, investment=None):
    from weasyprint import HTML
    def deny_resources(url, *args, **kwargs):
        raise ValueError('External and local URL resource fetching is disabled')
    env = Environment(loader=FileSystemLoader(str(files('stock_report').joinpath('templates'))),
                      autoescape=select_autoescape(['html']))
    env.filters['brief'] = lambda value, n=140: str(value) if len(str(value)) <= n else str(value)[:n] + '…（完整內容見本機 JSON）'
    context = projection(data, decision, mapping, company_research)
    env.filters['cite'] = lambda ids: ''.join(f'[{n}]' for n in sorted({context['references'][eid] for eid in ids if eid in context['references']}))
    env.filters['amount'] = lambda e: format_amount(e.value, e.unit, e.currency)
    env.filters['taipei'] = lambda stamp: stamp.astimezone(timezone(timedelta(hours=8))).strftime('%Y/%m/%d %H:%M')
    template = 'research-four.html' if data.request.mode == 'stock' and data.request.max_pdf_pages >= 4 and company_research else 'report.html'
    if investment is not None and data.request.report_profile == 'investment' and company_research:
        template = 'research-investment.html'
        from .engine import charts_for
        from .research_contract import STATUS_LABELS
        from .valuation import METHOD_LABELS
        from .materiality import AREA_LABELS
        currency = data.entities[0].currency
        env.filters['money'] = lambda v, cur=currency: format_amount(v, cur, cur)
        env.filters['pct'] = lambda v, d=1: '—' if v is None else f'{v:,.{d}f}%'
        env.filters['num'] = lambda v, d=2: '—' if v is None else f'{v:,.{d}f}'
        env.filters['status'] = lambda s: STATUS_LABELS.get(s, s)
        context.update(inv=investment, charts=charts_for(investment), currency=currency, method_labels=METHOD_LABELS,
                       basis_labels={'historical_fact': '歷史事實', 'management_guidance': '公司指引',
                                     'consensus': '市場共識', 'model_assumption': '模型假設'},
                       thesis=(investment.get('thesis') or [None])[0])
    elif investment is not None and investment.get('gates') and investment['gates']['grade'] == 'limited' \
            and data.dataset_kind == 'real':
        context['limitations'].append('研究品質檢查未全數通過：' + '、'.join(investment['gates']['critical_failures']) + '。')
    html = env.get_template(template).render(**context)
    document = HTML(string=html, url_fetcher=deny_resources).render()
    if not 1 <= len(document.pages) <= data.request.max_pdf_pages:
        raise ValueError(f'PDF layout exceeds page budget: {len(document.pages)} pages; no report published')
    document.write_pdf(path)
    return len(document.pages)
