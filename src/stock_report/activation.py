"""Read-only checks for a source activation package.

An activation package is a review-ready proposal prepared by an assistant: the exact endpoint
and field binding, the identity/time/licence findings, and the narrow scope it recommends. It
grants nothing. A registry update needs the operator's explicit source review, quoted
terms evidence and named attestation; an assistant may record the authorized edit.

This module never writes the registry. It answers two questions:

- does the package still describe the code, the instrument configuration and the registry?
- does a human-edited candidate registry open exactly the packaged scope and nothing more?
"""
from typing import Literal
from pydantic import AwareDatetime, Field, model_validator
from .licensing import LicenseRegistry, PERMISSIONS, Scope
from .models import Model
from .sources import PROFILES
from .storage import read_json

# Labels that identify an assistant or a test fixture. A candidate attested under one of them
# is rejected: an assistant's check is recorded as `checked_by=assistant`, never as the human one.
NON_HUMAN_MARKERS = ('codex', 'claude', 'assistant', 'chatgpt', 'fixture', 'synthetic',
                     '助手', '助理', '機器人')
# Fields of a registry entry that an activation may never alter.
FROZEN_FIELDS = ('source_id', 'publisher', 'source_class', 'endpoint_scope', 'terms_url')


class Target(Model):
    ticker: str
    code: str
    exchange: Literal['TWSE', 'TPEx']
    currency: Literal['TWD']
    share_class: str
    instrument_config: str


class ActivationItem(Model):
    source_id: str
    recommendation: Literal['ready_for_human_review', 'not_recommended_yet']
    endpoint: str
    dataset_page: str
    dataset_resource: str
    license_name: str
    license_url: str
    field_binding: dict[str, list[str]]
    evidence_produced: list[str]
    retained_normalized_fields: list[str]
    not_retained: list[str]
    identity_findings: list[str]
    time_findings: list[str]
    license_findings: list[str]
    proposed_scope: Scope
    proposed_attribution: str = Field(max_length=200)
    report_effect: str
    human_checklist: list[str]
    open_questions: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def narrow_scope_only(self):
        scope = self.proposed_scope
        if scope.pdf_share_allowed or scope.store_response_hash or scope.store_raw_response:
            raise ValueError(f'{self.source_id}: a package may only propose internal use of normalized evidence; '
                             f'redistribution, response hashes and raw responses stay closed')
        if self.recommendation == 'not_recommended_yet' and scope.internal_use_allowed:
            raise ValueError(f'{self.source_id}: an item that is not recommended cannot propose any permission')
        return self


class ActivationPackage(Model):
    schema_version: Literal['1'] = '1'
    package_id: str = Field(pattern=r'^[a-z0-9][a-z0-9.\-]{2,63}$')
    prepared_at: AwareDatetime
    prepared_by: str
    prepared_by_kind: Literal['assistant']
    status: Literal['awaiting_human_attestation']
    registry_baseline_version: str
    target: Target
    items: list[ActivationItem] = Field(min_length=1, max_length=8)
    # The package itself never carries an attestation; the human records it in the registry.
    human_attestation: None = None
    notes: list[str] = Field(default_factory=list)


def looks_non_human(text):
    lowered = (text or '').lower()
    return any(marker in lowered for marker in NON_HUMAN_MARKERS)


def package_drift(package, registry, instruments):
    """Facts in the package that no longer match code, instrument config or registry."""
    problems = []
    target = package.target
    entry = next((row for row in instruments.get('instruments', []) if row.get('ticker') == target.ticker), None)
    if entry is None:
        problems.append(f'{target.ticker} 不在 {target.instrument_config}；未設定的標的不得啟用')
    else:
        for key in ('code', 'exchange', 'share_class'):
            if entry.get(key) != getattr(target, key):
                problems.append(f'標的設定 {key}={entry.get(key)!r} 與審查包 {getattr(target, key)!r} 不一致')
    if registry.registry_version != package.registry_baseline_version:
        problems.append(f'登錄檔版本為 {registry.registry_version}，審查包基準為 {package.registry_baseline_version}；'
                        f'需重新核對審查包')
    known = {s.source_id: s for s in registry.sources}
    for item in package.items:
        profile, source = PROFILES.get(item.source_id), known.get(item.source_id)
        if profile is None or source is None:
            problems.append(f'{item.source_id} 不在來源契約或授權登錄檔')
            continue
        if not (item.endpoint == profile['endpoint'] == source.endpoint_scope):
            problems.append(f'{item.source_id}：端點與來源契約或登錄範圍不一致')
        if profile.get('exchange') != target.exchange or profile.get('currency') != target.currency:
            problems.append(f'{item.source_id}：來源市場／幣別與標的不符')
        if item.field_binding != {key: list(names) for key, names in profile['fields'].items()}:
            problems.append(f'{item.source_id}：欄位對應與來源契約不同')
    return problems


def candidate_violations(package, current, candidate):
    """Everything a candidate registry changes beyond the packaged, human-attested scope."""
    violations = []
    items = {item.source_id: item for item in package.items}
    before = {s.source_id: s for s in current.sources}
    after = {s.source_id: s for s in candidate.sources}
    if set(before) != set(after):
        violations.append('候選登錄檔新增或移除了來源；啟用只能修改審查包列出的來源')
    if candidate.registry_version == current.registry_version:
        violations.append('候選登錄檔必須提升 registry_version，既有採集才會顯示版本差異')
    changed = []
    for source_id in sorted(set(before) & set(after)):
        old, new = before[source_id], after[source_id]
        if old == new:
            continue
        changed.append(source_id)
        item = items.get(source_id)
        if item is None or item.recommendation != 'ready_for_human_review':
            violations.append(f'{source_id}：不在審查包建議啟用的範圍，卻被修改')
            continue
        for name in FROZEN_FIELDS:
            if getattr(old, name) != getattr(new, name):
                violations.append(f'{source_id}：{name} 不得在啟用時變更')
        if new.declared_status != 'confirmed':
            violations.append(f'{source_id}：狀態為 {new.declared_status}，不是完整的人工確認')
            continue
        for name in PERMISSIONS:
            if getattr(new.scope, name) != getattr(item.proposed_scope, name):
                violations.append(f'{source_id}：{name}={getattr(new.scope, name)} 超出或不同於審查包建議的 '
                                  f'{getattr(item.proposed_scope, name)}')
        if new.attribution != item.proposed_attribution:
            violations.append(f'{source_id}：來源標示必須採用審查包的顯名文字')
        human = [t for t in new.terms_evidence if t.checked_by == 'human']
        allowed_urls = {item.dataset_page, item.license_url}
        if not {t.terms_url for t in human} >= allowed_urls:
            violations.append(f'{source_id}：人工逐字引用必須同時涵蓋資料集頁面與授權條款頁')
        if any(looks_non_human(t.checker) for t in human) or looks_non_human(new.attested_by):
            violations.append(f'{source_id}：簽認人或人工查核者標示為助理／測試，不能視為人工簽認')
        if new.attested_at and any(t.checked_at > new.attested_at for t in human):
            violations.append(f'{source_id}：簽認時間早於人工查核時間')
        if [t for t in old.terms_evidence if t not in new.terms_evidence]:
            violations.append(f'{source_id}：既有的助理查核紀錄不得刪除或改寫')
    if not changed:
        violations.append('候選登錄檔與目前登錄檔相同，沒有任何啟用')
    return violations, changed


def check(package_path, registry, instruments, candidate_path=None):
    package = ActivationPackage.model_validate(read_json(package_path))
    drift = package_drift(package, registry, instruments)
    known = {s.source_id: s for s in registry.sources}
    items = [{'source_id': item.source_id, 'recommendation': item.recommendation,
              'registry_status': known[item.source_id].declared_status if item.source_id in known else 'absent',
              'endpoint': item.endpoint, 'proposed_scope': item.proposed_scope.model_dump(),
              'open_questions': len(item.open_questions)} for item in package.items]
    result = {'package_id': package.package_id, 'target': package.target.ticker,
              'registry_version': registry.registry_version, 'items': items, 'package_drift': drift,
              'registry_modified': False}
    if drift:
        return {**result, 'status': 'blocked', 'blockers': drift}
    if candidate_path is None:
        ready = [item for item in package.items if item.recommendation == 'ready_for_human_review']
        return {**result, 'status': 'awaiting_human_attestation',
                'required_human_actions': [f'{item.source_id}：{step}' for item in ready for step in item.human_checklist],
                'warnings': ['審查包不授與任何權限；登錄檔未被修改，來源維持封閉']}
    # A candidate that is not a valid registry (for example confirmed without human evidence)
    # fails model validation here and is reported as a failure by the caller.
    candidate = LicenseRegistry.model_validate(read_json(candidate_path))
    violations, changed = candidate_violations(package, registry, candidate)
    if violations:
        return {**result, 'status': 'blocked', 'candidate_changed_sources': changed, 'blockers': violations}
    return {**result, 'status': 'candidate_matches_package', 'candidate_changed_sources': changed,
            'candidate_registry_version': candidate.registry_version,
            'warnings': ['此檢查只比對範圍，不能證明簽認人確實為人類或條款引用正確；'
                         '是否採用仍須依使用者明示的來源核對結果；可由 Codex 執行已授權的修改']}
