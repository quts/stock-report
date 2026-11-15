"""Fail-closed report decisions scoped to each company and horizon.

Reports are for the operator's personal use only and are never distributed, so the gate asks
whether a record is confirmed for internal use. The legacy redistribution flag
(`shareable_in_pdf`) is recorded for audit compatibility and is never consulted here.
Personal use is not a permission in itself: unknown or restricted sources stay blocked.
"""
import re
from .models import Decision, Finding, HorizonProposal, event_review_state, financial_review_state
from .quant import calculate, usable

VERSION = 'phase0-4'
GENERIC_INSUFFICIENT = '核心資料不足，無方向性結論。'
# An insufficient outlook may explain what is missing; it may never read as a direction. This is a
# lexical guard, not comprehension: any hit discards the text in favour of the generic wording.
DIRECTIONAL_TEXT = re.compile(
    r'偏多|看多|看好|看漲|利多|樂觀|買進|買入|加碼|上漲|上行|上修|成長動能|受惠|偏空|看空|看壞|看跌|利空|悲觀|賣出|減碼|下跌|下行|下修|'
    r'bullish|bearish|upside|downside|outperform|underperform|\bbuy\b|\bsell\b|\blong\b|\bshort\b', re.IGNORECASE)
EVENT_REVIEW_MESSAGES = {
    'EVENT_REVIEW_MISSING': '近期重大事件查核未執行（證據包沒有事件查核紀錄）；僅為財務快照，不發布方向性結論',
    'EVENT_REVIEW_NOT_RUN': '近期重大事件查核未執行；僅為財務快照，不發布方向性結論',
    'EVENT_REVIEW_FAILED': '近期重大事件查核失敗；不代表沒有重大事件，不發布方向性結論',
    'MATERIAL_LEAD_UNRESOLVED': '存在未解決的重大事件線索；不發布方向性結論',
}
FINANCIAL_REVIEW_MESSAGES = {
    'LATEST_FINANCIAL_REPORT_MISSING': '尚未查核並納入截止日前最新一期財報',
    'LATEST_FINANCIAL_REVIEW_INVALID': '最新財報查核的身份或時點不一致',
    'LATEST_FINANCIAL_DISCOVERY_UNVERIFIED': '最新申報清單未經核實來源確認',
    'LATEST_FINANCIAL_STATEMENTS_INCOMPLETE': '最新申報缺少必要的收入、淨利或營業現金流',
    'LATEST_QUARTER_MISSING': '最新季報尚未納入單季收入',
    'LATEST_FINANCIAL_REPORT_STALE': '已納入的財報早於截止日前依法應已公告的最新一期',
    'EARNINGS_PREVIEW_SEARCH_MISSING': '尚未執行下一次財報預期新聞搜尋',
}


def validate_company_research(data, proposal):
    """Factual business analysis survives a valuation downgrade, but never a bad citation."""
    sections, seen = [], set()
    index = {e.evidence_id: e for e in data.evidence}
    entities = {e.entity_id for e in data.entities}
    for section in proposal.company_research:
        if section.entity_id not in entities or section.entity_id in seen:
            raise ValueError('Company research must name a unique supported entity')
        seen.add(section.entity_id)
        if data.dataset_kind == 'real' and data.request.max_pdf_pages >= 4 and any(
                not getattr(section, field) for field in ('overview', 'key_takeaways', 'fundamentals',
                                                        'news', 'earnings_preview', 'monitoring')):
            raise ValueError('Four-page research requires all substantive company analysis sections')
        for field in ('overview', 'key_takeaways', 'fundamentals', 'news', 'earnings_preview', 'monitoring'):
            for paragraph in getattr(section, field):
                if any(eid not in index or index[eid].entity_id != section.entity_id
                       or not reportable(index[eid], data.request) for eid in paragraph.evidence_ids):
                    raise ValueError('Company research contains unavailable or unlicensed evidence')
        sections.append(section)
    return sections


def directional_text(*texts):
    return any(DIRECTIONAL_TEXT.search(text or '') for text in texts)


def internally_licensed(e):
    """Confirmed terms that allow internal use. Redistribution rights are not required."""
    return e.license_status == 'confirmed' and e.internal_use_allowed


def reportable(e, request):
    """May this record appear in, or support, the personal-use report?"""
    return (usable(e, request.research_as_of) and e.research_as_of == request.research_as_of
            and internally_licensed(e))


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
    review_state = event_review_state(data) + financial_review_state(data)
    if data.dataset_kind == 'real' and data.request.mode == 'stock' and (
            data.earnings_preview is None or data.earnings_preview.status == 'not_run'):
        review_state.append('EARNINGS_PREVIEW_SEARCH_MISSING')
    for p in proposal.outlooks:
        problems, bad_refs, reasons = [], set(), []
        def add(rule, message, ids=(), severity='hard'):
            f = Finding(rule_id=rule, entity_id=p.entity_id, horizon=p.horizon,
                        evidence_ids=list(ids), message=message, severity=severity)
            findings.append(f)
            if severity == 'hard':
                problems.append(rule)
                bad_refs.update(ids)
                if message not in reasons:
                    reasons.append(message)
        refs = list(dict.fromkeys(p.supporting_evidence_ids + p.counter_evidence_ids))
        if p.entity_id in bad_quant:
            add('QUANT_MISMATCH', '量化結果與原始證據重算不符')
        identity = [e for e in data.evidence if e.entity_id == p.entity_id and e.kind == 'identity' and reportable(e, data.request)]
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
            if not internally_licensed(e):
                add('LICENSE', '來源未確認內部使用權限，不得進入個人報告', [eid])
        core = [index[eid] for eid in set(p.supporting_evidence_ids) if eid in index
                and reportable(index[eid], data.request) and index[eid].kind in ('financial', 'price', 'event')
                and index[eid].entity_id == p.entity_id]
        # Phase 0 threshold is deliberately conservative and remains uncalibrated.
        independent = {(e.publisher, e.source_record_id) for e in core}
        if p.outlook != 'insufficient' and (len(independent) < 2 or not p.invalidation_conditions):
            add('CORE_INSUFFICIENT', '核心證據不足或缺少反轉條件；門檻尚待 Phase 0 校準')
        for code in review_state:
            add(code, {**EVENT_REVIEW_MESSAGES, **FINANCIAL_REVIEW_MESSAGES}[code])
        if data.request.mode == 'event':
            event_refs = [index.get(eid) for eid in data.event_evidence_ids]
            if not event_refs or not all(e and e.kind == 'event' and e.entity_id == 'EVENT' and reportable(e, data.request) for e in event_refs):
                add('EVENT_UNVERIFIED', '事件狀態無法以合法且已核實資料確認')
            rels = [r for r in data.relationships if r.entity_id == p.entity_id]
            if len(rels) != 1:
                add('RELATIONSHIP', '缺少唯一可追溯的公司關聯')
            else:
                rel = rels[0]
                valid = rel.evidence_ids and all(eid in index and index[eid].entity_id == p.entity_id
                    and index[eid].kind == 'relationship' and reportable(index[eid], data.request) for eid in rel.evidence_ids)
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
        if not any(e.kind == 'rating' and e.entity_id == p.entity_id and reportable(e, data.request)
                   and e.valid_until and e.valid_until >= data.request.research_as_of for e in data.evidence):
            add('RATINGS_MISSING', '投顧共識資料不足', severity='soft')
        if any(m.value is None for m in data.metrics if m.entity_id == p.entity_id):
            add('METRICS_MISSING', '部分財務或估值缺失，已省略不可驗證指標', severity='soft')
        add('PHASE0_THRESHOLD', 'Phase 0 規則未經實際投資效能驗證', severity='soft')
        if p.outlook == 'insufficient' or problems:
            # The Gate's own reasons always lead. A proposal that was itself `insufficient` keeps its
            # explanation and the evidence that passed every check; a downgraded directional proposal
            # keeps neither, because its text and citations argued for a direction.
            own = p.outlook == 'insufficient'
            texts = [p.interpretation, p.conflict_resolution, *p.assumptions, *p.invalidation_conditions]
            if own and directional_text(*texts):
                add('INSUFFICIENT_DIRECTIONAL_TEXT', '資料不足的說明含方向性用語，已改用通用說明', severity='soft')
                own = False
            gate = ('未形成方向性結論：' + '；'.join(reasons)) if problems else ''
            if own and p.interpretation.strip():
                text = (gate + '｜分析說明：' if gate else '') + p.interpretation.strip()
            else:
                text = gate or GENERIC_INSUFFICIENT
            keep = lambda ids: [eid for eid in ids if eid in index and eid not in bad_refs] if own else []
            approved.append(HorizonProposal(
                entity_id=p.entity_id, horizon=p.horizon, outlook='insufficient',
                supporting_evidence_ids=keep(p.supporting_evidence_ids),
                counter_evidence_ids=keep(p.counter_evidence_ids),
                interpretation=text if len(text) <= 350 else text[:349] + '…',
                assumptions=list(p.assumptions) if own else [],
                unresolved_conflicts=['；'.join(problems)[:300]] if problems else [],
                invalidation_conditions=list(p.invalidation_conditions) if own else []))
        else:
            approved.append(p)
    rejected = sum(p.outlook == 'insufficient' for p in approved)
    hard = any(f.severity == 'hard' for f in findings)
    status = 'REJECTED' if hard and rejected == len(approved) else 'APPROVED_WITH_WARNINGS' if findings else 'APPROVED'
    return Decision(status=status, findings=findings, outlooks=approved, policy_version=VERSION)
