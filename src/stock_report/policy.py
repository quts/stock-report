"""Fail-closed publication decisions scoped to each company and horizon."""
from .models import Decision, Finding, HorizonProposal
from .quant import calculate, usable

VERSION = 'phase0-1'


def publishable(e, request):
    return (usable(e, request.research_as_of) and e.research_as_of == request.research_as_of
            and e.license_status == 'confirmed' and e.shareable_in_pdf)


def evaluate(data, proposal):
    findings, approved = [], []
    index = {e.evidence_id: e for e in data.evidence}
    entities = {e.entity_id: e for e in data.entities}
    computed = calculate(data.evidence, data.request.research_as_of)
    bad_quant = {entity for entity in entities if [m for m in data.metrics if m.entity_id == entity] != [m for m in computed if m.entity_id == entity]}
    if any(m.entity_id not in entities for m in data.metrics):
        bad_quant.update(entities)
    pairs = [(p.entity_id, p.horizon) for p in proposal.outlooks]
    expected = {(e, h) for e in entities for h in data.request.horizons}
    if len(pairs) != len(set(pairs)) or set(pairs) != expected:
        raise ValueError('Proposal must contain exactly one outlook per entity and horizon')
    for p in proposal.outlooks:
        problems = []
        def add(rule, message, ids=(), severity='hard'):
            f = Finding(rule_id=rule, entity_id=p.entity_id, horizon=p.horizon,
                        evidence_ids=list(ids), message=message, severity=severity)
            findings.append(f)
            if severity == 'hard':
                problems.append(rule)
        refs = list(dict.fromkeys(p.supporting_evidence_ids + p.counter_evidence_ids))
        if p.entity_id in bad_quant:
            add('QUANT_MISMATCH', '量化結果與原始證據重算不符')
        identity = [e for e in data.evidence if e.entity_id == p.entity_id and e.kind == 'identity' and publishable(e, data.request)]
        if not identity or (data.request.exchange and entities[p.entity_id].exchange != data.request.exchange):
            add('IDENTITY', '標的身份或交易市場無法確認')
        for eid in refs:
            e = index.get(eid)
            if not e:
                add('UNKNOWN_EVIDENCE', '引用不存在的證據', [eid])
                continue
            if e.entity_id not in (p.entity_id, 'EVENT'):
                add('WRONG_ENTITY', '引用其他標的證據', [eid])
            if e.published_at > data.request.research_as_of or e.event_time > data.request.research_as_of or e.research_as_of != data.request.research_as_of:
                add('FUTURE_DATA', '資料不屬於研究截止時點', [eid])
            if e.verification_status != 'verified':
                add('UNVERIFIED', '未核實或矛盾資料不得作為已確認事實', [eid])
            if not e.internal_use_allowed or e.license_status != 'confirmed' or not e.shareable_in_pdf:
                add('LICENSE', '內容未取得報告使用權限', [eid])
        core = [index[eid] for eid in set(p.supporting_evidence_ids) if eid in index
                and publishable(index[eid], data.request) and index[eid].kind in ('financial', 'price', 'event')
                and index[eid].entity_id == p.entity_id]
        # Phase 0 threshold is deliberately conservative and remains uncalibrated.
        independent = {(e.publisher, e.source_record_id) for e in core}
        if p.outlook != 'insufficient' and (len(independent) < 2 or not p.invalidation_conditions):
            add('CORE_INSUFFICIENT', '核心證據不足或缺少反轉條件；門檻尚待 Phase 0 校準')
        if data.request.mode == 'event':
            event_refs = [index.get(eid) for eid in data.event_evidence_ids]
            if not event_refs or not all(e and e.kind == 'event' and e.entity_id == 'EVENT' and publishable(e, data.request) for e in event_refs):
                add('EVENT_UNVERIFIED', '事件狀態無法以合法且已核實資料確認')
            rels = [r for r in data.relationships if r.entity_id == p.entity_id]
            if len(rels) != 1:
                add('RELATIONSHIP', '缺少唯一可追溯的公司關聯')
            else:
                rel = rels[0]
                valid = rel.evidence_ids and all(eid in index and index[eid].entity_id == p.entity_id
                    and index[eid].kind == 'relationship' and publishable(index[eid], data.request) for eid in rel.evidence_ids)
                if rel.relationship == 'hypothetical' or not valid:
                    add('HYPOTHETICAL', '假設或無依據關聯不得發布方向性結論', rel.evidence_ids)
        growth = [m.value for m in computed if m.entity_id == p.entity_id and m.name in ('revenue_growth', 'eps_growth') and m.value is not None]
        quant_conflict = (p.outlook == 'bullish' and any(v < 0 for v in growth)) or (p.outlook == 'bearish' and growth and all(v > 0 for v in growth))
        if quant_conflict:
            if p.conflict_resolution:
                add('QUANT_AI_DIVERGENCE', 'Quant 與 AI 有分歧；保留具體說明，不平均為中性', severity='soft')
            else:
                add('QUANT_AI_UNRESOLVED', 'Quant 與 AI 方向分歧，尚無具體解釋')
        if len({e.publisher for e in core}) == 1:
            add('SINGLE_SOURCE', '核心證據只有單一發布者，並非獨立來源交叉驗證', severity='soft')
        if p.unresolved_conflicts:
            add('UNRESOLVED_CONFLICT', '重大分歧尚未解決，保留分歧並降級')
        if not any(e.kind == 'rating' and e.entity_id == p.entity_id and publishable(e, data.request)
                   and e.valid_until and e.valid_until >= data.request.research_as_of for e in data.evidence):
            add('RATINGS_MISSING', '投顧共識資料不足', severity='soft')
        if any(m.value is None for m in data.metrics if m.entity_id == p.entity_id):
            add('METRICS_MISSING', '部分財務或估值缺失，已省略不可驗證指標', severity='soft')
        add('PHASE0_THRESHOLD', 'Phase 0 規則未經實際投資效能驗證', severity='soft')
        if problems:
            approved.append(HorizonProposal(entity_id=p.entity_id, horizon=p.horizon, outlook='insufficient',
                supporting_evidence_ids=[], counter_evidence_ids=[], interpretation='證據或發布條件不足；請查閱決策紀錄。',
                assumptions=[], unresolved_conflicts=['；'.join(problems)], invalidation_conditions=[]))
        elif p.outlook == 'insufficient':
            approved.append(HorizonProposal(entity_id=p.entity_id, horizon=p.horizon, outlook='insufficient',
                supporting_evidence_ids=[], counter_evidence_ids=[], interpretation='核心資料不足，無方向性結論。'))
        else:
            approved.append(p)
    rejected = sum(p.outlook == 'insufficient' for p in approved)
    hard = any(f.severity == 'hard' for f in findings)
    status = 'REJECTED' if hard and rejected == len(approved) else 'APPROVED_WITH_WARNINGS' if findings else 'APPROVED'
    return Decision(status=status, findings=findings, outlooks=approved, policy_version=VERSION)
