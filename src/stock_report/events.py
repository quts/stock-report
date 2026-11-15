"""Evidence-bound event verification and deterministic candidate ranking.

This module does not search the web and does not know which companies an event affects.
It takes an *event dossier*: a reviewer-curated file that names the event, the sources that
establish it, and for each candidate company the evidence records that link it to the event.
From that it derives, mechanically and reproducibly:

- whether the event itself is established by usable evidence;
- each candidate's relationship class (direct / indirect / hypothetical), taken from the
  strength of its usable link evidence and never from the reviewer's declared label;
- a total order over candidates and the at-most-`max_companies` selection.

Coverage is bounded: only entities listed in the supported-universe configuration can be
ranked, and every other candidate is reported as outside the universe instead of being
ranked on a guess. The order expresses how well the *relationship* is evidenced. It is not
a statement that a company benefits or is harmed.

All dossier text is untrusted data. Nothing here executes, fetches or interprets it, and the
ranking report carries evidence IDs only, never claim text or source values.
"""
from pathlib import Path
from datetime import timedelta
from typing import Literal
from pydantic import AwareDatetime, Field, model_validator
from .licensing import endpoint_in_scope
from .mock import VERSIONS, fixture
from .models import Entity, Evidence, Model, Relationship, Request, ResearchInput
from .quant import calculate
from .storage import digest, read_json

VERSION = '1'
DEFAULT_UNIVERSE_PATH = Path('config/event-universe.json')
EVENT_ENTITY = 'EVENT'

LinkBasis = Literal['named_in_event_source', 'issuer_disclosure', 'disclosed_business_link',
                    'sector_membership', 'inference']
SourceTier = Literal['official', 'secondary']
DIRECT_BASES = ('named_in_event_source', 'issuer_disclosure')
INDIRECT_BASES = ('disclosed_business_link',)
CLASS_RANK = {'direct': 0, 'indirect': 1, 'hypothetical': 2}
# `rumored` can never be established; `proposed` can be established as a proposal only.
CONFIRMABLE_STATUSES = ('occurred', 'in_effect', 'officially_announced', 'proposed')
SECONDARY_ORIGINS_REQUIRED = 2
ID_PATTERN = r'^[A-Za-z0-9_.-]+$'

LIMITS = [
    '排序只反映「關聯證據強度」，不代表受益或受害，也不是投資方向。',
    '候選公司與關聯證據來自匯入的事件卷宗；系統不自行搜尋新聞或推測關聯公司。',
    '僅能排序 config/event-universe.json 明列的標的；其餘候選一律標示為超出支援範圍，不予排序。',
    '來源獨立性由審閱者標示的 origin_id 與來源網址／紀錄去重後取較小值；無法證明兩家媒體未互相轉載。',
    '排序規則為字典序比較，未經實際投資效能校準。',
]


class EventRecord(Model):
    event_id: str = Field(pattern=ID_PATTERN, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    status: Literal['occurred', 'in_effect', 'officially_announced', 'proposed', 'rumored']
    event_time: AwareDatetime


class EventSource(Model):
    """One evidence record that establishes the event. Copies of one release share an origin."""
    evidence_id: str = Field(pattern=ID_PATTERN, max_length=80)
    origin_id: str = Field(pattern=ID_PATTERN, max_length=80)
    tier: SourceTier


class Link(Model):
    """Why one evidence record ties a candidate to the event."""
    evidence_id: str = Field(pattern=ID_PATTERN, max_length=80)
    origin_id: str = Field(pattern=ID_PATTERN, max_length=80)
    tier: SourceTier
    basis: LinkBasis
    # The directly affected counterparty a business link runs through.
    via: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode='after')
    def business_link_names_its_counterparty(self):
        if self.basis == 'disclosed_business_link' and not self.via:
            raise ValueError('A disclosed business link must name the counterparty it runs through')
        return self


class Candidate(Model):
    entity: Entity
    # The reviewer's own label. Recorded for audit and compared with the derived class; never used.
    declared_relationship: Literal['direct', 'indirect', 'hypothetical'] | None = None
    links: list[Link] = Field(default_factory=list, max_length=12)


class Review(Model):
    reviewed_by: Literal['human', 'assistant', 'synthetic-fixture']
    reviewer: str = Field(min_length=1, max_length=120)
    reviewed_at: AwareDatetime
    statement: str = Field(min_length=1, max_length=400)
    # Evidence the reviewer actually checked for identity, timing and permission.
    evidence_ids: list[str] = Field(default_factory=list, max_length=400)
    # Where the user's authorization for an assistant review is recorded. An assistant review
    # without it does not qualify; it is never needed for, nor a substitute for, a human review.
    authorization: str | None = Field(default=None, min_length=1, max_length=300)

    def qualified(self):
        """A human review, or an assistant review made under a recorded user authorization."""
        return self.reviewed_by == 'human' or (self.reviewed_by == 'assistant' and bool(self.authorization))


class EventDossier(Model):
    schema_version: Literal['1'] = '1'
    dossier_kind: Literal['real', 'mock']
    request: Request
    event: EventRecord
    event_sources: list[EventSource] = Field(max_length=12)
    candidates: list[Candidate] = Field(max_length=40)
    evidence: list[Evidence] = Field(max_length=400)
    review: Review

    @model_validator(mode='after')
    def coherent(self):
        if self.request.mode != 'event':
            raise ValueError('An event dossier requires an event-mode request')
        ids = [e.evidence_id for e in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate evidence IDs in dossier')
        companies = [c.entity.entity_id for c in self.candidates]
        if len(companies) != len(set(companies)) or EVENT_ENTITY in companies:
            raise ValueError('Duplicate or reserved candidate entity IDs in dossier')
        synthetic = [e.source_type == 'synthetic' for e in self.evidence]
        # The two kinds never mix: a real dossier cannot be padded with authored records, and a
        # mock dossier cannot smuggle real source content under a synthetic label.
        if self.dossier_kind == 'real' and any(synthetic):
            raise ValueError('Synthetic evidence cannot appear in a real dossier')
        if self.dossier_kind == 'mock' and not all(synthetic):
            raise ValueError('A mock dossier may only contain synthetic evidence')
        if self.dossier_kind == 'mock' and self.review.reviewed_by != 'synthetic-fixture':
            raise ValueError('A mock dossier must be labelled as a synthetic fixture review')
        if self.dossier_kind == 'real' and self.review.reviewed_by == 'synthetic-fixture':
            raise ValueError('A real dossier cannot carry a synthetic fixture review')
        return self


class UniverseEntity(Model):
    entity_id: str = Field(pattern=r'^[A-Z0-9.\-]+$', max_length=24)
    exchange: Literal['NASDAQ', 'NYSE', 'TWSE', 'TPEx']
    currency: Literal['USD', 'TWD']
    share_class: str
    identity_basis: str = Field(min_length=1, max_length=200)


class Universe(Model):
    schema_version: Literal['1'] = '1'
    config_version: str = Field(pattern=r'^\d+\.\d+\.\d+$')
    notes: str = Field(default='', max_length=1000)
    entities: list[UniverseEntity] = Field(min_length=1, max_length=64)

    @model_validator(mode='after')
    def unique(self):
        ids = [e.entity_id for e in self.entities]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate entity in supported universe')
        return self


def load_universe(path=None):
    path = Path(path or DEFAULT_UNIVERSE_PATH)
    if not path.is_file():
        raise FileNotFoundError(f'Supported-universe configuration not found at {path}')
    return Universe.model_validate(read_json(path))


def record_problem(evidence, dossier, registry, expected_entity=None, expected_kind=None):
    """Why a record may not support anything, or None. First failing check wins, in fixed order."""
    request = dossier.request
    if evidence is None:
        return 'unknown_evidence_id'
    if expected_entity is not None and evidence.entity_id != expected_entity:
        return 'evidence_belongs_to_another_entity'
    if expected_kind is not None and evidence.kind != expected_kind:
        return f'evidence_kind_is_not_{expected_kind}'
    if evidence.research_as_of != request.research_as_of:
        return 'research_as_of_mismatch'
    if evidence.publication_basis == 'observed_availability' and request.research_as_of < evidence.availability_observed_at:
        return 'historical_as_of_not_supported_by_observed_availability'
    if evidence.published_at > request.research_as_of:
        return 'published_after_research_as_of'
    if evidence.event_time > request.research_as_of:
        return 'event_time_after_research_as_of'
    if evidence.verification_status != 'verified':
        return 'source_not_verified'
    if evidence.license_status != 'confirmed' or not evidence.internal_use_allowed:
        return 'license_not_confirmed_for_internal_use'
    if dossier.dossier_kind == 'real':
        review = dossier.review
        if not review.qualified() or evidence.evidence_id not in review.evidence_ids:
            return 'not_covered_by_source_review'
        known = {s.source_id: s for s in registry.sources} if registry is not None else {}
        if review.reviewed_by == 'assistant':
            # An assistant's word alone opens nothing: each record must trace to a registry entry
            # whose own review (official evidence, free personal use) keeps the source open.
            source = known.get(evidence.license_source_id)
            if source is None:
                return 'assistant_review_requires_registered_source'
            if source.review_basis() is None:
                return 'registry_keeps_source_closed'
        # A dossier cannot open a source that the versioned registry keeps closed.
        if registry is not None:
            source = known.get(evidence.license_source_id)
            if evidence.license_source_id and source is None:
                return 'source_not_in_registry'
            if source is not None:
                if not source.permits('internal_use_allowed'):
                    return 'registry_keeps_source_closed'
                if not endpoint_in_scope(evidence.source_url, source.endpoint_scope):
                    return 'source_url_outside_registered_scope'
            # Omitting or replacing an ID must not bypass a closed registered endpoint.
            matches = [s for s in registry.sources if endpoint_in_scope(evidence.source_url, s.endpoint_scope)]
            if any(not s.permits('internal_use_allowed') for s in matches):
                return 'registry_keeps_source_closed'
    return None


def effective_tier(declared, evidence):
    """A declared official tier must be backed by an official record; synthetic records simulate."""
    if evidence.source_type == 'synthetic':
        return declared
    return 'official' if declared == 'official' and evidence.source_type == 'official' else 'secondary'


def independent_origins(rows):
    """Conservative count: reviewer origin labels, URLs and provider records must all differ."""
    if not rows:
        return 0
    return min(len({origin for origin, _ in rows}), len({e.source_url for _, e in rows}),
               len({(e.publisher, e.source_record_id) for _, e in rows}))


def verify_event(dossier, index, registry):
    usable, excluded = [], []
    for source in dossier.event_sources:
        evidence = index.get(source.evidence_id)
        problem = record_problem(evidence, dossier, registry, EVENT_ENTITY, 'event')
        if not problem and evidence.published_at < dossier.request.research_as_of - timedelta(days=dossier.request.lookback_days):
            problem = 'event_source_outside_news_window'
        if problem:
            excluded.append({'evidence_id': source.evidence_id, 'reason': problem})
        else:
            usable.append((source, evidence))
    official = [(s.origin_id, e) for s, e in usable if effective_tier(s.tier, e) == 'official']
    secondary = [(s.origin_id, e) for s, e in usable if effective_tier(s.tier, e) == 'secondary']
    secondary_publishers = len({e.publisher for _, e in secondary})
    blockers = []
    if dossier.event.status not in CONFIRMABLE_STATUSES:
        level = 'unverified'
        blockers.append({'code': 'event_status_unconfirmed',
                         'detail': f'事件狀態為 {dossier.event.status}；傳聞不得作為已核實事件'})
    elif dossier.event.event_time > dossier.request.research_as_of:
        level = 'unverified'
        blockers.append({'code': 'event_time_after_research_as_of', 'detail': '事件時間晚於研究截止時點'})
    elif official:
        level = 'official'
    elif min(independent_origins(secondary), secondary_publishers) >= SECONDARY_ORIGINS_REQUIRED:
        level = 'secondary_corroborated'
    else:
        level = 'unverified'
        blockers.append({'code': 'event_not_verified',
                         'detail': f'沒有可用的官方來源，且獨立次級來源少於 {SECONDARY_ORIGINS_REQUIRED} 個；'
                                   f'不以單一或同源轉載報導確認事件'})
    return {'level': level, 'official_origins': independent_origins(official),
            'secondary_origins': min(independent_origins(secondary), secondary_publishers),
            'usable_evidence_ids': sorted(e.evidence_id for _, e in usable),
            'excluded': sorted(excluded, key=lambda row: row['evidence_id'])}, blockers


def assess_candidate(candidate, dossier, index, registry):
    entity_id = candidate.entity.entity_id
    links, usable = [], []
    for link in sorted(candidate.links, key=lambda item: item.evidence_id):
        evidence = index.get(link.evidence_id)
        problem = record_problem(evidence, dossier, registry, entity_id, 'relationship')
        row = {'evidence_id': link.evidence_id, 'basis': link.basis, 'origin_id': link.origin_id,
               'declared_tier': link.tier, 'tier': effective_tier(link.tier, evidence) if evidence else None,
               'usable': problem is None, 'reason': problem}
        links.append(row)
        if problem is None:
            usable.append((link, evidence, row['tier']))
    direct = [(l, e) for l, e, tier in usable if l.basis in DIRECT_BASES and tier == 'official']
    indirect = [(l, e) for l, e, tier in usable if l.basis in INDIRECT_BASES
                or (l.basis in DIRECT_BASES and tier != 'official')]
    hypothetical = [(l, e) for l, e, _ in usable if l.basis not in DIRECT_BASES + INDIRECT_BASES]
    flags = []
    if direct:
        relationship, support = 'direct', direct
    elif indirect:
        relationship, support = 'indirect', indirect
        if any(l.basis in DIRECT_BASES for l, _ in indirect):
            flags.append('direct_claim_not_officially_confirmed')
    else:
        relationship, support = 'hypothetical', hypothetical
    if candidate.declared_relationship and CLASS_RANK[candidate.declared_relationship] < CLASS_RANK[relationship]:
        flags.append('declared_relationship_overclaimed')
    if any(not row['usable'] for row in links):
        flags.append('some_link_evidence_excluded')
    official = [(l.origin_id, e) for l, e in support if effective_tier(l.tier, e) == 'official']
    return {'entity_id': entity_id, 'exchange': candidate.entity.exchange, 'relationship': relationship,
            'declared_relationship': candidate.declared_relationship,
            'rank_key': {'class_rank': CLASS_RANK[relationship],
                         'official_origins': independent_origins(official),
                         'independent_origins': independent_origins([(l.origin_id, e) for l, e in support]),
                         'entity_id': entity_id},
            'supporting_evidence_ids': sorted(e.evidence_id for _, e in support),
            'links': links, 'flags': flags}


def order_key(row):
    key = row['rank_key']
    return (key['class_rank'], -key['official_origins'], -key['independent_origins'], key['entity_id'])


def rank_dossier(dossier, universe, registry=None):
    """Return (research_input_or_None, report). Deterministic: no clock, no randomness, no I/O."""
    request = dossier.request
    index = {e.evidence_id: e for e in dossier.evidence}
    supported = {e.entity_id: e for e in universe.entities}
    blockers, warnings, excluded, assessed = [], [], [], []
    if dossier.dossier_kind == 'real' and not dossier.review.qualified():
        blockers.append({'code': 'dossier_requires_qualified_review',
                         'detail': '真實事件卷宗必須有人工查核，或載明使用者授權依據的 AI 查核；'
                                   '未載明授權的助理查核不合格'})
    elif dossier.dossier_kind == 'real' and dossier.review.reviewed_by == 'assistant':
        if registry is None:
            blockers.append({'code': 'assistant_review_requires_registry',
                             'detail': 'AI 查核的卷宗只能使用授權登錄檔內已查核開放的來源；未提供登錄檔'})
        warnings.append('本卷宗為 AI 查核（非人工簽認）：僅採用登錄檔已查核開放來源的證據，其餘一律排除')
    verification, event_blockers = verify_event(dossier, index, registry)
    blockers.extend(event_blockers)
    if dossier.event.status == 'proposed':
        warnings.append('事件狀態為 proposed：僅確認「提案存在」，尚未生效，不得寫成已實施')
    coverage = {entity_id: 'not_in_dossier' for entity_id in supported}
    candidates = {c.entity.entity_id: c for c in dossier.candidates}
    for entity_id in sorted(candidates):
        candidate = candidates[entity_id]
        member = supported.get(entity_id)
        identity = (candidate.entity.exchange, candidate.entity.currency, candidate.entity.share_class)
        if member is None:
            excluded.append({'entity_id': entity_id, 'reason': 'outside_supported_universe'})
            continue
        if identity != (member.exchange, member.currency, member.share_class):
            coverage[entity_id] = 'identity_conflicts_with_universe'
            excluded.append({'entity_id': entity_id, 'reason': 'identity_conflicts_with_universe'})
            continue
        identity_rows = [e for e in dossier.evidence if e.entity_id == entity_id and e.kind == 'identity']
        if not any(record_problem(e, dossier, registry) is None for e in identity_rows):
            coverage[entity_id] = 'identity_evidence_unavailable'
            excluded.append({'entity_id': entity_id, 'reason': 'identity_evidence_unavailable'})
            continue
        if not candidate.links:
            coverage[entity_id] = 'no_relationship_evidence'
            excluded.append({'entity_id': entity_id, 'reason': 'no_relationship_evidence'})
            continue
        row = assess_candidate(candidate, dossier, index, registry)
        if not row['supporting_evidence_ids']:
            coverage[entity_id] = 'relationship_evidence_unusable'
            excluded.append({'entity_id': entity_id, 'reason': 'relationship_evidence_unusable',
                             'links': row['links']})
            continue
        coverage[entity_id] = 'ranked'
        assessed.append(row)
    ranking = sorted(assessed, key=order_key)
    for position, row in enumerate(ranking, start=1):
        row['rank'] = position
        row['selected'] = position <= request.max_companies
        if 'declared_relationship_overclaimed' in row['flags']:
            warnings.append(f'{row["entity_id"]}：宣告的關聯強於證據可支持的類別，採用推導結果 {row["relationship"]}')
        if row['relationship'] == 'hypothetical' and row['selected']:
            warnings.append(f'{row["entity_id"]}：關聯僅為假設，Gate 不會發布方向性結論')
    selected = [row for row in ranking if row['selected']]
    if not selected:
        blockers.append({'code': 'no_rankable_candidate',
                         'detail': '支援範圍內沒有具可用身份與關聯證據的候選公司'})
    if any(row['reason'] == 'outside_supported_universe' for row in excluded):
        warnings.append('部分候選公司超出支援範圍，未排序；這不代表它們與事件無關')
    report = {'artifact': 'event_candidate_ranking', 'schema_version': '1', 'ranking_version': VERSION,
              'status': 'blocked' if blockers else 'ready', 'dossier_kind': dossier.dossier_kind,
              'data_label': 'DEMO / SYNTHETIC DATA' if dossier.dossier_kind == 'mock'
                            else 'REAL (human-reviewed import)' if dossier.review.reviewed_by == 'human'
                            else 'REAL (assistant-reviewed import)',
              'dossier_sha256': digest(dossier),
              'request': {'event_query': request.event_query, 'max_companies': request.max_companies,
                          'research_as_of': request.research_as_of.isoformat()},
              'event': {'event_id': dossier.event.event_id, 'status': dossier.event.status,
                        'event_time': dossier.event.event_time.isoformat(), 'verification': verification},
              'review': {'reviewed_by': dossier.review.reviewed_by, 'reviewer': dossier.review.reviewer,
                         'qualified': dossier.review.qualified(), 'authorization': dossier.review.authorization,
                         'reviewed_at': dossier.review.reviewed_at.isoformat(),
                         'evidence_ids_reviewed': len(set(dossier.review.evidence_ids))},
              'universe': {'config_version': universe.config_version, 'supported': sorted(supported),
                           'coverage': dict(sorted(coverage.items()))},
              'ordering': ['relationship class: direct < indirect < hypothetical',
                           'more independent official origins first',
                           'more independent origins first', 'entity_id ascending'],
              'ranking': ranking, 'selected': [row['entity_id'] for row in selected],
              'excluded_candidates': excluded, 'blockers': blockers, 'warnings': warnings, 'limits': LIMITS}
    if blockers:
        return None, report
    chosen = [row['entity_id'] for row in selected]
    event_ids = verification['usable_evidence_ids']
    evidence, dropped = [index[eid] for eid in event_ids], []
    for e in sorted(dossier.evidence, key=lambda item: item.evidence_id):
        if e.entity_id not in chosen:
            continue
        problem = record_problem(e, dossier, registry)
        if problem:
            dropped.append({'evidence_id': e.evidence_id, 'reason': problem})
        else:
            evidence.append(e)
    dataset = 'real-event-dossier-1' if dossier.dossier_kind == 'real' else 'synthetic-event-dossier-1'
    data = ResearchInput(dataset_kind=dossier.dossier_kind, request=request,
                         entities=[candidates[entity_id].entity for entity_id in chosen], evidence=evidence,
                         metrics=calculate(evidence, request.research_as_of),
                         relationships=[Relationship(entity_id=row['entity_id'], relationship=row['relationship'],
                                                     evidence_ids=row['supporting_evidence_ids']) for row in selected],
                         event_evidence_ids=event_ids, versions={**VERSIONS, 'dataset': dataset})
    report['bundle_evidence_exclusions'] = dropped
    report['bundle_sha256'] = digest(data)
    return data, report


def synthetic_dossier(request):
    """Authored example built on the mock fixture. Every record is synthetic and labelled so."""
    base = fixture(request.model_copy(update={'max_companies': 5}))
    evidence = list(base.evidence)

    def clone(source_id, new_id, **changes):
        source = next(e for e in evidence if e.evidence_id == source_id)
        record = source.model_copy(update={'evidence_id': new_id, 'source_record_id': new_id,
                                           'source_url': f'https://example.invalid/synthetic/{new_id}', **changes})
        evidence.append(record)
        return new_id

    clone('2330.TW-relationship', '2330.TW-relationship-filing',
          claim='合成：公司自身公告揭露受該事件影響，僅供測試')
    clone('NVDA-relationship', 'INTC-relationship', entity_id='INTC',
          claim='合成：支援範圍外公司的關聯紀錄，僅供測試')
    entities = {e.entity_id: e for e in base.entities}
    entities['INTC'] = Entity(entity_id='INTC', name='範圍外公司（合成測資）', exchange='NASDAQ', currency='USD')

    def link(evidence_id, basis, tier, via=None):
        return Link(evidence_id=evidence_id, origin_id=evidence_id, tier=tier, basis=basis, via=via)

    candidates = [
        Candidate(entity=entities['NVDA'], declared_relationship='direct',
                  links=[link('NVDA-relationship', 'named_in_event_source', 'official')]),
        Candidate(entity=entities['2330.TW'], declared_relationship='direct',
                  links=[link('2330.TW-relationship', 'named_in_event_source', 'official'),
                         link('2330.TW-relationship-filing', 'issuer_disclosure', 'official')]),
        Candidate(entity=entities['AMD'], declared_relationship='direct',
                  links=[link('AMD-relationship', 'disclosed_business_link', 'secondary', via='NVDA')]),
        Candidate(entity=entities['6488.TWO'], declared_relationship='hypothetical',
                  links=[link('6488.TWO-relationship', 'sector_membership', 'secondary')]),
        Candidate(entity=entities['INTC'], declared_relationship='indirect',
                  links=[link('INTC-relationship', 'inference', 'secondary')]),
    ]
    return EventDossier(
        dossier_kind='mock', request=request,
        event=EventRecord(event_id='synthetic-policy-event', title='DEMO 合成跨市場產業事件，並非真實政策公告',
                          status='officially_announced', event_time=base.evidence[0].event_time),
        event_sources=[EventSource(evidence_id='EVENT-announcement', origin_id='EVENT-announcement', tier='official')],
        candidates=candidates, evidence=evidence,
        review=Review(reviewed_by='synthetic-fixture', reviewer='synthetic fixture (not a human review)',
                      reviewed_at=request.research_as_of, statement='合成測資；未經任何人工來源查證。',
                      evidence_ids=[e.evidence_id for e in evidence]))
