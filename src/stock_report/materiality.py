"""Deterministic materiality ranking of reviewed events by investment impact."""

AREA_WEIGHTS = {'revenue_earnings': 3, 'free_cash_flow': 3, 'capital_allocation': 2, 'competitive_position': 2,
                'valuation': 2, 'regulatory_geopolitical': 2}
AREA_LABELS = {'revenue_earnings': '營收與獲利', 'free_cash_flow': '自由現金流', 'capital_allocation': '資本配置',
               'competitive_position': '競爭地位', 'valuation': '估值', 'regulatory_geopolitical': '法規與地緣政治'}
LOW_IMPACT_SCORE = 3


def rank(data):
    review = data.event_review
    if review is None:
        return {'status': 'not_run', 'events': [], 'low_impact': []}
    ranked = []
    for event in review.events:
        score = (3 if event.materiality == 'major' else 1) + sum(AREA_WEIGHTS[a] for a in event.impact_areas)
        ranked.append({'event_id': event.event_id, 'title': event.title, 'category': event.category,
                       'materiality': event.materiality, 'status': event.status,
                       'announced_only': event.status in ('officially_announced', 'proposed'),
                       'event_time': event.event_time.isoformat(), 'verification': event.verification,
                       'impact_areas': [AREA_LABELS[a] for a in event.impact_areas], 'score': score,
                       'evidence_ids': event.evidence_ids})
    ranked.sort(key=lambda r: (r['score'], r['event_time']), reverse=True)
    return {'status': review.status, 'events': [r for r in ranked if r['score'] >= LOW_IMPACT_SCORE],
            'low_impact': [r for r in ranked if r['score'] < LOW_IMPACT_SCORE],
            'unresolved_leads': [lead.model_dump() for lead in review.unresolved_leads]}
