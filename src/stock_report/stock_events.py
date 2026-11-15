"""Merge a reviewed stock event dossier into an existing stock evidence bundle.

A financial bundle only says what the filings' figures were. Whether anything material happened
to the company inside the request window is a separate question that needs its own recorded
review. This module takes a reviewer-curated *stock event dossier* and, without any network
access, checks it against the request, the bundle and the versioned source registry before the
verified events and the review record are added to the bundle.

Nothing here searches for news. A review that failed or was never run is recorded as such, and an
event that the dossier lists but usable evidence cannot establish becomes an unresolved material
lead instead of being dropped. All dossier text is untrusted data and is never executed.
"""
from datetime import timedelta
import re
from typing import Literal
from pydantic import AwareDatetime, Field, model_validator
from .events import EventSource, Review, effective_tier, independent_origins, record_problem, SECONDARY_ORIGINS_REQUIRED
from .models import (EventCategory, EventReview, EventReviewStatus, Evidence, MaterialLead, Model, Request,
                     ResearchInput, ReviewedEvent)
from .quant import calculate
from .sources import FILING_PROFILES
from .storage import digest, read_json

VERSION = '1'
ID_PATTERN = r'^[A-Za-z0-9_.-]+$'


class StockEvent(Model):
    event_id: str = Field(pattern=ID_PATTERN, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    category: EventCategory
    materiality: Literal['major', 'notable'] = 'major'
    status: Literal['occurred', 'in_effect', 'officially_announced', 'proposed', 'rumored']
    event_time: AwareDatetime
    sources: list[EventSource] = Field(min_length=1, max_length=6)


class EventScan(Model):
    """What the reviewer searched, over which window, and what stayed open."""
    status: EventReviewStatus
    window_start: AwareDatetime | None = None
    window_end: AwareDatetime | None = None
    sources_checked: list[str] = Field(default_factory=list, max_length=24)
    failure_reason: str | None = Field(default=None, max_length=300)
    unresolved_leads: list[MaterialLead] = Field(default_factory=list, max_length=24)


class StockEventDossier(Model):
    schema_version: Literal['1'] = '1'
    dossier_kind: Literal['real', 'mock']
    request: Request
    scan: EventScan
    events: list[StockEvent] = Field(default_factory=list, max_length=24)
    evidence: list[Evidence] = Field(default_factory=list, max_length=96)
    review: Review

    @model_validator(mode='after')
    def coherent(self):
        if self.request.mode != 'stock':
            raise ValueError('A stock event dossier requires a stock-mode request')
        ids = [e.evidence_id for e in self.evidence]
        if len(ids) != len(set(ids)) or len({e.event_id for e in self.events}) != len(self.events):
            raise ValueError('Duplicate evidence or event IDs in dossier')
        if any(e.kind != 'event' or e.entity_id != self.request.ticker for e in self.evidence):
            raise ValueError('A stock event dossier only carries event evidence of the requested company')
        synthetic = [e.source_type == 'synthetic' for e in self.evidence]
        # The two kinds never mix, exactly as for an event-mode dossier.
        if self.dossier_kind == 'real' and (any(synthetic) or self.review.reviewed_by == 'synthetic-fixture'):
            raise ValueError('Synthetic evidence or a synthetic fixture review cannot appear in a real dossier')
        if self.dossier_kind == 'mock' and (not all(synthetic) or self.review.reviewed_by != 'synthetic-fixture'):
            raise ValueError('A mock dossier carries synthetic evidence and a synthetic fixture review only')
        return self


def filing_url_problem(profile, url, ticker, entities=None):
    """Bind an archive document URL to the configured issuer: the path CIK is the identity key."""
    config = entities if entities is not None else read_json(profile['entities_config'])
    rows = [row for row in config.get('entities', []) if isinstance(row, dict) and row.get('ticker') == ticker]
    if len(rows) != 1 or not re.fullmatch(r'\d{10}', str(rows[0].get('cik', ''))):
        return 'issuer_not_configured_for_filing_source'
    pattern = re.escape(profile['endpoint'])
    for name in ('cik', 'accession', 'document'):
        pattern = pattern.replace(re.escape('{%s}' % name), f'(?P<{name}>{profile["segments"][name]})')
    match = re.fullmatch(pattern, url)
    if match is None:
        return 'filing_url_does_not_match_archive_contract'
    if match['cik'] != str(int(rows[0]['cik'])):
        return 'filing_url_belongs_to_another_issuer'
    return None


def registry_problem(evidence, registry, ticker, entities):
    """Checks a real record against its registered source beyond the shared dossier checks."""
    source = next((s for s in registry.sources if s.source_id == evidence.license_source_id), None)
    if source is None:
        return 'source_not_in_registry'
    if source.review_basis() is None or not source.permits('internal_use_allowed'):
        return 'registry_keeps_source_closed'
    if evidence.license_registry_version != registry.registry_version:
        return 'license_registry_version_mismatch'
    if (evidence.publisher, evidence.terms_url, evidence.attribution) != (source.publisher, source.terms_url,
                                                                          source.attribution):
        return 'license_fields_differ_from_registry'
    if evidence.source_type != source.source_class:
        return 'source_class_differs_from_registry'
    # Raw audit retention is a separate permission that no dossier may assume.
    if evidence.source_hash is not None and not source.permits('store_response_hash'):
        return 'response_hash_retention_not_permitted'
    if evidence.shareable_in_pdf != source.permits('pdf_share_allowed'):
        return 'legacy_share_flag_differs_from_registry'
    profile = FILING_PROFILES.get(source.source_id)
    if profile is not None:
        return filing_url_problem(profile, evidence.source_url, ticker, entities)
    return None


def merge_stock_events(bundle, dossier, registry=None, sec_entities=None):
    """Return (merged_research_input_or_None, report). Deterministic; no clock, no network."""
    request = bundle.request
    cutoff = request.research_as_of
    window_start = cutoff - timedelta(days=request.lookback_days)
    scan, review = dossier.scan, dossier.review
    blockers, warnings, exclusions = [], [], []

    def block(code, detail):
        blockers.append({'code': code, 'detail': detail})

    if request.mode != 'stock':
        block('bundle_is_not_stock_research', '只有個股證據包可以併入個股事件卷宗')
    if bundle.event_review is not None:
        block('bundle_already_has_event_review', '證據包已有事件查核紀錄；請以原始財務證據包重新併入，不覆寫既有查核')
    if dossier.dossier_kind != bundle.dataset_kind:
        block('dataset_kind_mismatch', f'卷宗為 {dossier.dossier_kind}、證據包為 {bundle.dataset_kind}；'
                                       f'合成與真實資料不得互相併入')
    if dossier.request != request:
        fields = [name for name in ('ticker', 'exchange', 'research_as_of', 'lookback_days', 'mode')
                  if getattr(dossier.request, name) != getattr(request, name)]
        block('request_mismatch', f'卷宗與證據包的 Request 不同：{fields or ["other fields"]}')
    if dossier.dossier_kind == 'real':
        if not review.qualified():
            block('dossier_requires_qualified_review', '真實卷宗必須有人工查核，或載明使用者授權依據的 AI 查核')
        if registry is None:
            block('registry_required', '真實卷宗必須以授權登錄檔核對每一筆來源')
        if review.reviewed_by == 'assistant':
            warnings.append('本卷宗為 AI 查核（非人工簽認）：僅採用登錄檔已查核開放來源的證據')
    if scan.status == 'completed':
        if scan.window_start is None or scan.window_end != cutoff or scan.window_start > window_start:
            block('scan_window_does_not_cover_request',
                  f'完成的事件查核必須涵蓋 {window_start.isoformat()} 至 {cutoff.isoformat()} 的完整窗口')
        if not scan.sources_checked:
            block('scan_sources_not_recorded', '完成的事件查核必須列出實際查過的來源')
        if review.reviewed_at < cutoff:
            block('review_predates_cutoff', '查核時間早於研究截止時點，無法涵蓋到截止時點為止的事件')
    elif scan.status == 'failed' and not scan.failure_reason:
        block('scan_failure_reason_missing', '失敗的事件查核必須說明原因')
    existing = {e.evidence_id for e in bundle.evidence}
    collisions = sorted(existing & {e.evidence_id for e in dossier.evidence})
    if collisions:
        block('duplicate_evidence_id', collisions[:8])
    lead_ids = [lead.lead_id for lead in scan.unresolved_leads]
    if len(lead_ids) != len(set(lead_ids)) or set(lead_ids) & {e.event_id for e in dossier.events}:
        block('duplicate_lead_id', '未解決線索的 ID 重複，或與事件 ID 相同')

    index = {e.evidence_id: e for e in dossier.evidence}
    problems = {}
    for evidence in dossier.evidence:
        problem = record_problem(evidence, dossier, registry, request.ticker, 'event')
        if not problem and dossier.dossier_kind == 'real' and registry is not None:
            problem = registry_problem(evidence, registry, request.ticker, sec_entities)
        if not problem and evidence.published_at < window_start:
            problem = 'event_source_outside_news_window'
        problems[evidence.evidence_id] = problem
    cited = {s.evidence_id for event in dossier.events for s in event.sources}
    for evidence_id in sorted(index):
        reason = problems[evidence_id] or (None if evidence_id in cited else 'not_cited_by_any_event')
        if reason:
            exclusions.append({'evidence_id': evidence_id, 'reason': reason})

    verified, leads, assessed = [], list(scan.unresolved_leads), []
    for event in sorted(dossier.events, key=lambda item: (item.event_time, item.event_id), reverse=True):
        usable = [(s, index[s.evidence_id]) for s in event.sources
                  if s.evidence_id in index and problems[s.evidence_id] is None]
        official = [(s.origin_id, e) for s, e in usable if effective_tier(s.tier, e) == 'official']
        secondary = [(s.origin_id, e) for s, e in usable if effective_tier(s.tier, e) == 'secondary']
        reasons = sorted({problems.get(s.evidence_id, 'unknown_evidence_id') or 'usable' for s in event.sources}
                         - {'usable'})
        if event.status == 'rumored':
            level, reasons = 'unverified', ['event_status_rumored'] + reasons
        elif event.event_time > cutoff:
            level, reasons = 'unverified', ['event_time_after_research_as_of'] + reasons
        elif official:
            level = 'official'
        elif min(independent_origins(secondary), len({e.publisher for _, e in secondary})) >= SECONDARY_ORIGINS_REQUIRED:
            level = 'secondary_corroborated'
        else:
            level, reasons = 'unverified', reasons or ['secondary_sources_not_independently_corroborated']
        assessed.append({'event_id': event.event_id, 'category': event.category, 'materiality': event.materiality,
                         'verification': level, 'usable_evidence_ids': sorted(e.evidence_id for _, e in usable),
                         'reasons': reasons})
        if level == 'unverified':
            # Never dropped: an event the reviewer raised but could not establish stays visible.
            leads.append(MaterialLead(lead_id=event.event_id, summary=event.title,
                                      reason=('卷宗列出但未能以可用證據核實：' + ', '.join(reasons))[:200]))
        else:
            keep = official if level == 'official' else secondary
            verified.append((ReviewedEvent(event_id=event.event_id, title=event.title, category=event.category,
                                           materiality=event.materiality, status=event.status,
                                           event_time=event.event_time, verification=level,
                                           evidence_ids=sorted(e.evidence_id for _, e in keep)),
                             [e for _, e in keep]))
    if len(leads) > 24:
        block('too_many_unresolved_leads', '未解決線索超過上限；請先整理卷宗')
    report = {'artifact': 'stock_event_merge', 'schema_version': '1', 'merge_version': VERSION,
              'status': 'blocked' if blockers else 'ready', 'dossier_kind': dossier.dossier_kind,
              'dossier_sha256': digest(dossier), 'source_bundle_sha256': digest(bundle),
              'request': {'ticker': request.ticker, 'exchange': request.exchange,
                          'research_as_of': cutoff.isoformat(), 'lookback_days': request.lookback_days,
                          'window_start': window_start.isoformat()},
              'review': {'reviewed_by': review.reviewed_by, 'reviewer': review.reviewer,
                         'qualified': review.qualified(), 'authorization': review.authorization,
                         'reviewed_at': review.reviewed_at.isoformat()},
              'registry_version': registry.registry_version if registry is not None else None,
              'event_review_status': scan.status, 'events': assessed,
              'verified_events': [event.event_id for event, _ in verified],
              'unresolved_leads': [lead.lead_id for lead in leads],
              'evidence_exclusions': exclusions, 'blockers': blockers, 'warnings': warnings,
              'network_called': False, 'raw_response_retained': False}
    if blockers:
        return None, report
    if scan.status != 'completed':
        warnings.append(f'事件查核狀態為 {scan.status}：報告將標示為未完成，不代表沒有重大事件')
    if leads:
        warnings.append('存在未解決的重大線索：Gate 不會發布方向性結論')
    added = list({e.evidence_id: e for _, rows in verified for e in rows}.values())
    event_review = EventReview(status=scan.status, window_start=scan.window_start, window_end=scan.window_end,
                               reviewed_by=review.reviewed_by, reviewer=review.reviewer,
                               reviewed_at=review.reviewed_at, authorization=review.authorization,
                               sources_checked=scan.sources_checked, failure_reason=scan.failure_reason,
                               events=[event for event, _ in verified], unresolved_leads=leads)
    evidence = [*bundle.evidence, *added]
    data = ResearchInput(dataset_kind=bundle.dataset_kind, request=request, entities=bundle.entities,
                         evidence=evidence, metrics=calculate(evidence, cutoff),
                         relationships=bundle.relationships, event_evidence_ids=bundle.event_evidence_ids,
                         event_review=event_review, latest_financials=bundle.latest_financials,
                         earnings_preview=bundle.earnings_preview, versions=bundle.versions)
    report['bundle_sha256'] = digest(data)
    return data, report
