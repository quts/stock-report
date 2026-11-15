"""Delivery regressions: material events and useful follow-up conditions stay visible."""
from datetime import timedelta
from stock_report.models import ReviewedEvent
from stock_report.policy import evaluate
from stock_report.render import projection


def test_material_event_precedes_newer_minor_items(data, proposal, mapping):
    event = next(e for e in data.evidence if e.kind == 'event')
    event.event_time -= timedelta(days=2)
    data.event_review.events = [ReviewedEvent(event_id='test-major', title='Test CEO transition',
        category='management_change', materiality='major', status='occurred',
        event_time=event.event_time, verification='official', evidence_ids=[event.evidence_id])]
    minor = event.model_copy(deep=True)
    minor.evidence_id = 'newer-minor'
    minor.event_time += timedelta(days=1)
    data.evidence.append(minor)
    view = projection(data, evaluate(data, proposal), mapping)
    assert view['companies'][0]['events'][0].evidence_id == event.evidence_id
    assert len(view['sources']) == len(data.evidence)


def test_insufficient_outlook_keeps_specific_non_directional_follow_up(data, proposal):
    for p in proposal.outlooks:
        p.outlook = 'insufficient'
        p.interpretation = '已核實交接；仍缺最新季度與價格。'
        p.assumptions = ['情境推演：需檢驗客戶驗收是否延後。']
        p.invalidation_conditions = ['下一次季度申報公布後核對營收與交付。']
    decision = evaluate(data, proposal)
    assert all(p.invalidation_conditions == ['下一次季度申報公布後核對營收與交付。']
               for p in decision.outlooks)
    assert all(p.supporting_evidence_ids for p in decision.outlooks)


def test_directional_text_still_cannot_hide_inside_insufficient_follow_up(data, proposal):
    for p in proposal.outlooks:
        p.outlook = 'insufficient'
        p.interpretation = '資料尚缺。'
        p.invalidation_conditions = ['建議買入']
    decision = evaluate(data, proposal)
    assert all(not p.invalidation_conditions for p in decision.outlooks)
    assert 'INSUFFICIENT_DIRECTIONAL_TEXT' in {f.rule_id for f in decision.findings}
